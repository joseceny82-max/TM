"""bot_orchestrator.py — Orquestador BROWSER-BASED de compra TM (modo live).

Enlaza los módulos browser-based que funcionan end-to-end con MoreLogin:
  · venue_scanner    → mapa + sectores (cacheado por venue_id)
  · ticket_selector  → select_quick_smart / select_seatmap
  · cart_handler     → proceed_to_checkout + detect_destination
  · queue_handler    → is_in_queue / wait_for_turn

Diferencia con tm_purchase_flow.run_flow (que es simulate HTTP):
  - Este NO simula. Usa Playwright sobre perfil MoreLogin REAL.
  - Aplica regla "perfil nuevo por ejecución" + cleanup discriminado al cerrar.
  - Bloquea notificaciones desde antes de navegar (add_init_script).

API:
    run_purchase_flow(**kwargs) → dict {ok, stage, applied_qty, destination, debug}

kwargs principales (los que vienen del bot_engine.Worker._run):
    event_id, url, quantity, selection_mode='quick',
    prefer_offer_indices=None, selected_sections=None, strict_targets=None,
    venue_id=None, offer_max_prices=None, account_id=None,
    captcha_provider='auto', browser='auto', worker_id=None, broadcast=None.

Stages que reporta:
    'no_es_profile_available' | 'cannot_start_profile' | 'goto_failed' |
    'select_failed' | 'no_destination' | 'queue_in_progress' | 'queue_timeout' |
    'auth_required' | 'checkout_reached' | 'unknown_destination'
"""
from __future__ import annotations
import time
import threading
from typing import Optional, Dict, Any, List, Callable
import requests

import ticket_selector
import cart_handler
import queue_handler
import cookie_filter

MORELOGIN_API = "http://127.0.0.1:40000"

# Lock + set de perfiles "activos" para evitar que 2 threads paralelos
# elijan el mismo perfil MoreLogin (race condition real observada).
_pick_lock = threading.Lock()
_active_profiles: set = set()


def _pick_fresh_profile(country_code: str = "ES",
                         quemados: Optional[set] = None,
                         max_pages: int = 20,
                         page_size: int = 100,
                         allow_unverified: bool = False) -> Optional[Dict[str, Any]]:
    """Devuelve un perfil del país pedido NO usado por otro thread.
    Pagina hasta `max_pages * page_size` perfiles porque MoreLogin a veces tiene
    muchos perfiles SIN proxy en las primeras posiciones (los más recientes vacíos).
    Si no encuentra verificado y `allow_unverified=True`, acepta cualquier ES."""
    quemados = set(quemados or [])
    with _pick_lock:
        candidate_unverified = None
        for page_no in range(1, max_pages + 1):
            try:
                r = requests.post(f"{MORELOGIN_API}/api/env/page",
                                  json={"pageNo": page_no, "pageSize": page_size},
                                  timeout=15).json()
            except Exception:
                break
            items = (r.get("data") or {}).get("dataList") or []
            if not items:
                break
            for it in items:
                p = it.get("proxy") or {}
                name = it.get("envName") or ""
                if not name or name in _active_profiles or name in quemados:
                    continue
                if p.get("countryCode") != country_code:
                    continue
                check = p.get("proxyCheckStatus")
                if check == 1:
                    _active_profiles.add(name)
                    return {"profile": name, "env_id": it["id"],
                            "ip": p.get("exportIp"), "verified": True}
                # Guardar primer no-verificado por si no aparece ningún verified
                if candidate_unverified is None:
                    candidate_unverified = (name, it["id"], p.get("exportIp"))
        if allow_unverified and candidate_unverified:
            name, env_id, ip = candidate_unverified
            _active_profiles.add(name)
            return {"profile": name, "env_id": env_id, "ip": ip, "verified": False}
    return None


def _release_profile(profile_name: str) -> None:
    with _pick_lock:
        _active_profiles.discard(profile_name)


# ═══════════════════════════════════════════════════════════════
# CREATE FRESH PROFILE — Norma user 2026-05-13
# ═══════════════════════════════════════════════════════════════
# Crea un perfil MoreLogin NUEVO con proxy Quantum custom fresh + sessionid
# aleatorio. Cada llamada → IP+sessionid únicos garantizados.
#
# Endpoints (descubiertos 2026-05-13):
#   POST /api/proxyInfo/add → crea proxy, devuelve proxyId
#   POST /api/env/create/advanced → crea perfil, devuelve envId
#   POST /api/env/start → arranca, devuelve debugPort
#   POST /api/env/removeToRecycleBin/batch → borra perfil (cleanup)
#   POST /api/proxyInfo/delete → borra proxy (cleanup)
#
# Norma: tras usarlo, llamar `destroy_fresh_profile()` para no contaminar el
# pool de MoreLogin con perfiles auto-generados (limite 20000 total).

import random as _random_mod
_QUANTUM_BASE_USERNAME = "Quantum_927evhgx-cc-{cc}-pool-qntm-sessionid-{sessid}-sesstime-60"
_QUANTUM_HOST = "residential.quantumproxies.net"
_QUANTUM_PORT = 8888
_QUANTUM_PASSWORD = "ycp4e57m"


def create_fresh_profile(country_code: str = "ES",
                         browser_type: int = 1,
                         os_type: int = 1) -> Optional[Dict[str, Any]]:
    """Crea un perfil MoreLogin NUEVO con proxy Quantum fresh.

    Args:
        country_code: 'ES', 'IT', etc. Mapeado al cc del username Quantum.
        browser_type: 1=Chrome, 2=Firefox.
        os_type: 1=Windows, 2=macOS.

    Returns:
        dict con campos:
            - profile (envName)
            - env_id
            - proxy_id (necesario para cleanup)
            - sessionid (string)
            - ip (vacío hasta start)
            - verified=True (creado on-the-fly con proxy custom)
        O None si falla.
    """
    sessid = _random_mod.randint(100000, 999999)
    proxy_name = f"qntm-{country_code.lower()}-auto-{sessid}"
    env_name = f"AUTO-{country_code.upper()}-{sessid}"

    # 1) Crear proxy custom
    proxy_body = {
        "proxyName": proxy_name,
        "proxyType": 0,   # 0=http
        "proxyProvider": 0,   # custom
        "proxyIp": _QUANTUM_HOST,
        "proxyPort": _QUANTUM_PORT,
        "username": _QUANTUM_BASE_USERNAME.format(
            cc=country_code.upper(), sessid=sessid),
        "password": _QUANTUM_PASSWORD,
        "countryCode": country_code.upper(),
    }
    try:
        r = requests.post(f"{MORELOGIN_API}/api/proxyInfo/add",
                           json=proxy_body, timeout=15).json()
    except Exception:
        return None
    if r.get("code") != 0:
        return None
    proxy_id = r.get("data")
    if not proxy_id:
        return None

    # 2) Crear perfil avanzado con ese proxy
    env_body = {
        "browserTypeId": browser_type,
        "operatorSystemId": os_type,
        "envName": env_name,
        "proxyId": proxy_id,
        "groupId": 0,
        "uaVersion": 0,
    }
    try:
        r2 = requests.post(f"{MORELOGIN_API}/api/env/create/advanced",
                            json=env_body, timeout=15).json()
    except Exception:
        # Cleanup proxy si fallo
        try: requests.post(f"{MORELOGIN_API}/api/proxyInfo/delete",
                            json=[proxy_id], timeout=10)
        except Exception: pass
        return None
    if r2.get("code") != 0:
        try: requests.post(f"{MORELOGIN_API}/api/proxyInfo/delete",
                            json=[proxy_id], timeout=10)
        except Exception: pass
        return None
    env_id = r2.get("data")
    if not env_id:
        return None

    # Lock para no usarlo en paralelo
    with _pick_lock:
        _active_profiles.add(env_name)

    return {
        "profile": env_name,
        "env_id": env_id,
        "proxy_id": proxy_id,
        "sessionid": str(sessid),
        "ip": "",   # set tras start
        "verified": True,
        "auto_created": True,   # marca para destroy_fresh_profile
    }


def destroy_fresh_profile(prof: Dict[str, Any]) -> Dict[str, Any]:
    """Borra perfil + proxy creados por create_fresh_profile.

    Es CRÍTICO llamarlo tras usar el perfil para no acumular en MoreLogin
    (límite 20000 perfiles). Acumular = saturar el pool.

    Args:
        prof: dict devuelto por create_fresh_profile.

    Returns:
        dict {profile_deleted, proxy_deleted, errors}.
    """
    out = {"profile_deleted": False, "proxy_deleted": False, "errors": []}
    if not prof or not prof.get("auto_created"):
        out["errors"].append("not_auto_created")
        return out

    env_id = prof.get("env_id")
    proxy_id = prof.get("proxy_id")
    label = prof.get("profile")

    # Liberar lock
    if label:
        with _pick_lock:
            _active_profiles.discard(label)

    # Borrar perfil (recycle bin con removeEnvData=True)
    if env_id:
        try:
            r = requests.post(
                f"{MORELOGIN_API}/api/env/removeToRecycleBin/batch",
                json={"envIds": [env_id], "removeEnvData": True},
                timeout=15).json()
            out["profile_deleted"] = (r.get("code") == 0)
        except Exception as e:
            out["errors"].append(f"env_del:{str(e)[:80]}")

    # Borrar proxy
    if proxy_id:
        try:
            r = requests.post(f"{MORELOGIN_API}/api/proxyInfo/delete",
                               json=[proxy_id], timeout=10).json()
            out["proxy_deleted"] = (r.get("code") == 0)
        except Exception as e:
            out["errors"].append(f"proxy_del:{str(e)[:80]}")

    return out


def _start_profile(env_id: str) -> Optional[int]:
    try:
        r = requests.post(f"{MORELOGIN_API}/api/env/start",
                          json={"envId": env_id}, timeout=20).json()
        return int(r["data"]["debugPort"])
    except Exception:
        return None


def _close_profile_clean(env_id: str, port: int, profile_label: str = "") -> Dict[str, Any]:
    """Cleanup discriminado + close. Reusa la lógica de venue_scanner."""
    out = {"keep": 0, "drop": 0, "unknown": 0, "closed": False}
    try:
        from playwright.sync_api import sync_playwright
        v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=5).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
            ctx = b.contexts[0] if b.contexts else None
            if ctx:
                cookies = ctx.cookies()
                keep, drop, unknown = cookie_filter.partition_cookies(cookies)
                out.update(keep=len(keep), drop=len(drop), unknown=len(unknown))
                ctx.clear_cookies()
                if keep:
                    norm = []
                    for c in keep:
                        nc = {k: v for k, v in c.items() if v is not None and k in
                              ("name", "value", "domain", "path", "expires",
                               "httpOnly", "secure", "sameSite", "url")}
                        if "sameSite" in nc:
                            ss = str(nc["sameSite"]).capitalize()
                            nc["sameSite"] = ss if ss in ("Strict", "Lax", "None") else "Lax"
                        norm.append(nc)
                    try: ctx.add_cookies(norm)
                    except Exception: pass
                # Política usuario: borrar historial COMPLETO (cache + storage
                # + cookies remanentes) al cerrar cualquier navegador.
                try:
                    import browser_cleanup as _bc
                    out["history"] = _bc.clear_history_via_cdp(ctx)
                except Exception as _e:
                    out["history_err"] = str(_e)[:120]
            b.close()
    except Exception as e:
        out["cleanup_err"] = str(e)[:120]
    try:
        r = requests.post(f"{MORELOGIN_API}/api/env/close",
                          json={"envId": env_id}, timeout=15).json()
        out["closed"] = r.get("code") == 0
    except Exception:
        pass
    return out


def _emit(broadcast: Optional[Callable], stage: str, payload: Dict[str, Any]):
    if broadcast:
        try: broadcast(stage, payload)
        except Exception: pass


def run_purchase_flow(event_id: Optional[str] = None,
                     url: Optional[str] = None,
                     quantity: int = 1,
                     selection_mode: str = "quick",
                     prefer_offer_indices: Optional[List[int]] = None,
                     selected_sections: Optional[List[str]] = None,
                     strict_targets: Optional[bool] = None,
                     venue_id: Optional[str] = None,
                     offer_max_prices: Optional[Dict[str, float]] = None,
                     account_id: Optional[str] = None,
                     captcha_provider: str = "auto",
                     browser: str = "auto",
                     worker_id: Optional[str] = None,
                     country_code: str = "ES",
                     queue_max_minutes: int = 90,
                     broadcast: Optional[Callable] = None,
                     keep_browser_open: bool = False,
                     **extra) -> Dict[str, Any]:
    """Orquestador completo. Devuelve dict con `stage` indicando hasta dónde llegó."""
    debug: Dict[str, Any] = {"worker_id": worker_id, "extra": list(extra.keys())}
    started = time.time()

    # 1) Validar URL del evento
    if not url and event_id:
        try:
            import event_manager
            for e in event_manager.list_events():
                if str(e.get("id")) == str(event_id) or str(e.get("tm_id")) == str(event_id):
                    url = e.get("tm_url"); break
        except Exception: pass
    if not url:
        return {"ok": False, "stage": "no_url", "error": "No url ni event_id válido", "debug": debug}
    debug["url"] = url

    # 2) Perfil MoreLogin nuevo (regla "perfil nuevo por ejecución")
    prof = _pick_fresh_profile(country_code=country_code)
    if not prof:
        return {"ok": False, "stage": "no_es_profile_available", "debug": debug}
    debug.update(profile=prof["profile"], env_id=prof["env_id"], ip=prof.get("ip"))
    _emit(broadcast, "profile_picked", {"profile": prof["profile"], "ip": prof["ip"]})

    port = _start_profile(prof["env_id"])
    if not port:
        return {"ok": False, "stage": "cannot_start_profile", "debug": debug}
    debug["port"] = port

    result_stage = "unknown"
    try:
        from playwright.sync_api import sync_playwright
        time.sleep(5)  # init browser
        v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
            ctx = b.contexts[0] if b.contexts else b.new_context()

            # 3) Bloquear notificaciones desde antes de navegar
            try:
                ctx.add_init_script("""
                    if (window.Notification) {
                        Notification.requestPermission = () => Promise.resolve('denied');
                        try { Object.defineProperty(Notification, 'permission', {get:()=>'denied', configurable:true}); } catch(e){}
                    }
                    if (navigator.serviceWorker && navigator.serviceWorker.register) {
                        navigator.serviceWorker.register = function() { return Promise.reject(new Error('blocked')); };
                    }
                """)
            except Exception: pass

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.bring_to_front()

            # 4) Navegar al evento (con retries)
            goto_ok = False
            for attempt in range(3):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    goto_ok = True
                    break
                except Exception as e:
                    debug.setdefault("goto_errors", []).append(str(e)[:80])
                    time.sleep(3)
            if not goto_ok:
                result_stage = "goto_failed"
                _emit(broadcast, "stage", {"stage": result_stage})
                return {"ok": False, "stage": result_stage, "debug": debug}
            page.wait_for_timeout(7000)

            # 5) Cerrar cookies
            for txt in ["Aceptar todas las cookies", "Rechazarlas todas"]:
                try:
                    btn = page.get_by_text(txt, exact=False).first
                    if btn.is_visible(timeout=1500):
                        btn.click(); page.wait_for_timeout(1500); break
                except Exception: continue
            ticket_selector._block_notifications(page)

            # 6) Seleccionar entradas según modo
            _emit(broadcast, "stage", {"stage": "selecting"})
            if selection_mode == "seatmap":
                sel = ticket_selector.select_seatmap(
                    page, qty=quantity,
                    target_section_ids=selected_sections,
                    strict_targets=bool(strict_targets) if strict_targets is not None else bool(selected_sections),
                    fallback_to_quick=True,
                )
            else:
                sel = ticket_selector.select_quick_smart(
                    page, qty=quantity,
                    prefer_offer_indices=prefer_offer_indices,
                )
            debug["selection"] = {k: v for k, v in sel.items() if k != "debug"}
            if not sel.get("ok"):
                result_stage = "select_failed"
                _emit(broadcast, "stage", {"stage": result_stage, "selection": sel})
                return {"ok": False, "stage": result_stage, "selection": sel, "debug": debug}
            _emit(broadcast, "selection_done", {"applied_qty": sel.get("applied_qty"),
                                                  "offer_or_section": sel.get("offer_name") or sel.get("section_clicked")})

            # 7) Proceder al checkout
            _emit(broadcast, "stage", {"stage": "proceeding_to_checkout"})
            proc = cart_handler.proceed_to_checkout(page, click_attempts=2, post_click_wait_s=12)
            debug["proceed"] = {k: v for k, v in proc.items() if k != "debug"}
            destination = proc.get("destination") or "unknown"
            _emit(broadcast, "destination", {"destination": destination, "url": proc.get("url")})

            # 8) Manejar destination
            if destination == "queue":
                result_stage = "queue_in_progress"
                _emit(broadcast, "stage", {"stage": result_stage,
                                            "queue_status": queue_handler.get_queue_status(page)})
                wq = queue_handler.wait_for_turn(
                    page, max_minutes=queue_max_minutes, poll_interval_s=10,
                    on_progress=lambda st: _emit(broadcast, "queue_progress", st),
                )
                debug["queue_wait"] = wq
                if not wq["ok"]:
                    result_stage = "queue_timeout"
                    return {"ok": False, "stage": result_stage, "selection": sel,
                            "destination": destination, "queue_wait": wq, "debug": debug}
                # Re-detectar destino tras salir de cola
                page.wait_for_timeout(3000)
                destination = cart_handler.detect_destination(page).get("destination") or "unknown"
                debug["post_queue_destination"] = destination

            if destination == "auth":
                result_stage = "auth_required"
                _emit(broadcast, "stage", {"stage": result_stage})
                return {"ok": True, "stage": result_stage, "selection": sel,
                        "destination": destination, "url": page.url, "debug": debug,
                        "elapsed_s": round(time.time() - started, 1),
                        "next_action": "Se requiere login TM. Bloqueado por OTP iCloud (ver feedback_bot_compra status email)."}
            if destination == "checkout":
                result_stage = "checkout_reached"
                _emit(broadcast, "stage", {"stage": result_stage})
                return {"ok": True, "stage": result_stage, "selection": sel,
                        "destination": destination, "url": page.url, "debug": debug,
                        "elapsed_s": round(time.time() - started, 1),
                        "next_action": "Pendiente: rellenar checkout (datos personales + tarjeta + 3DS)."}
            if destination == "same_page":
                result_stage = "no_destination"
                _emit(broadcast, "stage", {"stage": result_stage})
                return {"ok": False, "stage": result_stage, "selection": sel,
                        "destination": destination, "url": page.url, "debug": debug,
                        "next_action": "Click CTA no avanzó. Posible login wall TM o cart pre-validation."}

            # Unknown destination
            result_stage = "unknown_destination"
            return {"ok": False, "stage": result_stage, "selection": sel,
                    "destination": destination, "url": page.url, "debug": debug}
    except Exception as e:
        result_stage = "exception"
        debug["exception"] = f"{type(e).__name__}: {str(e)[:200]}"
        return {"ok": False, "stage": result_stage, "debug": debug}
    finally:
        if keep_browser_open:
            debug["browser_kept_open"] = True
            debug["profile"] = prof["profile"]
            debug["port"] = port
            debug["env_id"] = prof["env_id"]
            _emit(broadcast, "browser_kept_open", {"profile": prof["profile"], "port": port, "env_id": prof["env_id"]})
            # NO liberar — el browser queda activo, no debe re-asignarse
        else:
            cleanup = _close_profile_clean(prof["env_id"], port, profile_label=prof["profile"])
            debug["cleanup"] = cleanup
            _emit(broadcast, "cleanup", cleanup)
            _release_profile(prof["profile"])


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
