"""account_validator.py — Verifica cuentas TM haciendo login real.

Detecta el mensaje "No podemos encontrar una cuenta con el correo y contraseña
indicados" y marca la cuenta como `invalid_credentials`. Las cuentas inválidas
se devuelven a `tm_registrations` con status=pending para reintentar registro.

Uso:
    validate_account(account_id) -> dict
    validate_batch(account_ids, max_parallel=2) -> dict
    get_job(job_id) -> dict (estado del batch en curso)

Cada validación abre UN browser MoreLogin headless (cierra al terminar) y
prueba el login real en my.ticketmaster.es.
"""
from __future__ import annotations
import os, time, json, threading, uuid, re
from datetime import datetime
from typing import Dict, Any, List, Optional
import requests

import account_manager as am
import bot_orchestrator as bo

PROYECTO = os.path.dirname(os.path.abspath(__file__))
_JOBS: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_JOBS_PATH = os.path.join(PROYECTO, "datos", "account_validator_jobs.json")
_LOG_PATH = os.path.join(PROYECTO, "datos", "account_validator.log")
_FRESH_HISTORY_PATH = os.path.join(PROYECTO, "datos",
                                    "account_validator_fresh_history.json")
_ACTIVE_IPS_PATH = os.path.join(PROYECTO, "datos",
                                  "account_validator_active_ips.json")
os.makedirs(os.path.dirname(_JOBS_PATH), exist_ok=True)


# ─── IP LOCK GLOBAL: delegado a ip_lock module (cross-process, todo el bot) ───
import ip_lock as _ip_lock


def _load_active_ips() -> set:
    """Compat shim — delega a ip_lock global."""
    return set(it["ip"] for it in _ip_lock.list_active())


def _claim_ip(ip: str) -> bool:
    return _ip_lock.claim_ip(ip, owner="account_validator")


def _release_ip(ip: str):
    _ip_lock.release_ip(ip, mark_recent=True)


def reset_active_ips():
    _ip_lock.reset_active()


def is_ip_globally_available(ip: str, recent_hours: float = 2.0):
    return _ip_lock.is_ip_available(ip, recent_hours=recent_hours)


# ═══════════════════════════════════════════════════════════
#  GUARDAS DE SEGURIDAD ANTI-BLOQUEO TM
# ═══════════════════════════════════════════════════════════
# TM bloquea cuentas tras: 3 fallos consecutivos, login+logout repetido en
# corto tiempo, demasiados intentos por IP. Estas guardas evitan que el
# validador queme cuentas inadvertidamente.

# Cooldown mínimo entre 2 tests FRESH login para LA MISMA cuenta.
# 6h es conservador — TM resetea su contador de intentos en ~1-2h.
_FRESH_COOLDOWN_S = 6 * 3600

# Si una cuenta fue marcada credentials_verified=True hace <24h,
# saltar tests redundantes (storage_state ya garantiza que es válida).
_VERIFIED_RECENT_S = 24 * 3600

# Si en un batch >X% de cuentas dan bad_credentials en serie, ABORTAR.
# Patrón típico de IP/perfil quemado — seguir gastaría más cuentas.
_BATCH_FAIL_RATIO_ABORT = 0.5
_BATCH_MIN_FAILS_FOR_ABORT = 3


def _load_fresh_history() -> dict:
    """Histórico de tests FRESH login por cuenta: {account_id: {last_test_at,
    last_result, fail_count}}."""
    try:
        if os.path.exists(_FRESH_HISTORY_PATH):
            with open(_FRESH_HISTORY_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception: pass
    return {}


def _save_fresh_history(data: dict):
    try:
        with open(_FRESH_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception: pass


def _can_do_fresh_test(account_id: str) -> tuple:
    """¿Puede esta cuenta someterse a un FRESH login ahora?
    Devuelve (allowed: bool, reason: str). Aplica:
      - cooldown 6h entre tests FRESH consecutivos.
      - hard-skip si la cuenta acumula 3+ fails (probablemente bloqueada).
    """
    h = _load_fresh_history()
    entry = h.get(account_id) or {}
    last_at = entry.get("last_test_at")
    fail_count = int(entry.get("fail_count") or 0)
    if fail_count >= 3:
        return False, f"safety_too_many_fails_{fail_count}"
    if last_at:
        try:
            dt = datetime.fromisoformat(last_at)
            elapsed = (datetime.now() - dt).total_seconds()
            if elapsed < _FRESH_COOLDOWN_S:
                hours_left = (_FRESH_COOLDOWN_S - elapsed) / 3600
                return False, f"safety_cooldown_active_{hours_left:.1f}h_left"
        except Exception: pass
    return True, "ok"


def _record_fresh_test(account_id: str, valid: bool, reason: str):
    h = _load_fresh_history()
    entry = h.get(account_id) or {"fail_count": 0}
    entry["last_test_at"] = datetime.now().isoformat()
    entry["last_result"] = {"valid": valid, "reason": reason}
    if not valid and reason == "bad_credentials":
        entry["fail_count"] = int(entry.get("fail_count") or 0) + 1
    elif valid:
        # Reset fail_count si finalmente loguea OK
        entry["fail_count"] = 0
    h[account_id] = entry
    _save_fresh_history(h)


def _is_recently_verified(account: dict) -> bool:
    """¿La cuenta fue verificada via storage_state en las últimas 24h?
    Si sí, saltarse los tests redundantes (storage_state ya garantiza la
    cuenta opera funcionalmente)."""
    if not account.get("credentials_verified"):
        return False
    va = account.get("validated_at")
    if not va:
        return False
    try:
        dt = datetime.fromisoformat(va)
        return (datetime.now() - dt).total_seconds() < _VERIFIED_RECENT_S
    except Exception:
        return False


def _persist_jobs():
    """Guarda _JOBS a disco para sobrevivir restarts del dashboard."""
    try:
        with _jobs_lock:
            data = {jid: {k: v for k, v in j.items()
                          if k != "results" or len(v) <= 100}
                    for jid, j in _JOBS.items()}
        with open(_JOBS_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, default=str, ensure_ascii=False)
    except Exception:
        pass


def _restore_jobs():
    """Carga jobs persistidos al iniciar."""
    try:
        if not os.path.exists(_JOBS_PATH): return
        with open(_JOBS_PATH, encoding='utf-8') as f:
            data = json.load(f)
        with _jobs_lock:
            for jid, j in data.items():
                # Marcar como interrupted los que no terminaron
                if j.get("status") not in ("done", "interrupted", "error"):
                    j["status"] = "interrupted"
                    j["interrupted_at"] = datetime.now().isoformat()
                _JOBS[jid] = j
    except Exception:
        pass


_restore_jobs()


def _log(slot, msg, **kw):
    line = f"[validator{slot}][{time.strftime('%H:%M:%S')}] {msg}"
    if kw:
        try: line += " " + json.dumps(kw, default=str, ensure_ascii=False)[:300]
        except Exception: pass
    print(line, flush=True)
    # También persistir a archivo para auditar runs históricos
    try:
        with open(_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception: pass


def _detect_bad_creds(page) -> bool:
    try:
        return page.evaluate(r"""() => {
            const txt = (document.body && document.body.innerText || '').toLowerCase();
            return /no podemos encontrar una cuenta|cuenta con el correo y contrase|contrase[ñn]a indicados|incorrect.*password|invalid.*credentials/i.test(txt);
        }""")
    except Exception:
        return False


def _detect_logged_in(page) -> bool:
    try:
        u = page.url.lower()
        if any(k in u for k in ('my.ticketmaster', 'account', 'profile', 'orders')):
            return True
        return page.evaluate(r"""() => {
            const txt = (document.body && document.body.innerText || '').toLowerCase();
            return /cerrar sesi|sign\s*out|log\s*out|mi cuenta|my account/i.test(txt);
        }""")
    except Exception:
        return False


def _verify_account_identity(page, expected_email: str, slot: int = 0) -> Dict[str, Any]:
    """Verifica que la sesión logueada PERTENECE a la cuenta esperada.
    Visita el perfil y extrae el email visible. Returns {confirmed, found_email,
    found_name, page_url, reason}."""
    out = {"confirmed": False, "found_email": "", "found_name": "",
           "page_url": "", "reason": ""}
    try:
        # Navegar a la página de perfil/settings
        for url in ("https://my.ticketmaster.es/settings",
                    "https://my.ticketmaster.es/account",
                    "https://my.ticketmaster.es/"):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2500)
                out["page_url"] = page.url
                # Si TM redirige a login → sesión no válida
                if any(k in page.url.lower() for k in ("/login", "/signin",
                                                          "auth.ticketmaster")):
                    out["reason"] = "redirected_to_login_session_invalid"
                    return out
                break
            except Exception: continue

        # Extraer email visible en la página
        result = page.evaluate(r"""() => {
            const txt = (document.body && document.body.innerText || '');
            const html = (document.body && document.body.innerHTML || '');
            // Email visible: regex sobre innerText
            const emailMatches = txt.match(/[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g) || [];
            // Buscar campos input con value email
            const inputs = Array.from(document.querySelectorAll('input[type="email"], input[name*="email" i]'));
            const inputEmails = inputs.map(i => i.value).filter(Boolean);
            // Nombre visible: heurística simple — buscar en h1/h2 saludo "Hola, X"
            const greetingMatch = txt.match(/(?:hola|hi|welcome|bienvenido)[,\s]+([A-Z][a-zA-Záéíóú]+(?:\s+[A-Z][a-zA-Záéíóú]+)?)/i);
            return {
                emails_in_text: emailMatches.slice(0, 5),
                emails_in_inputs: inputEmails,
                greeting_name: greetingMatch ? greetingMatch[1] : "",
                title: document.title,
                has_logout: /cerrar sesi[oó]n|sign\s*out|log\s*out/i.test(txt),
            };
        }""")
        all_emails = list(set((result.get("emails_in_text") or []) +
                                (result.get("emails_in_inputs") or [])))
        out["found_name"] = result.get("greeting_name", "")
        out["page_title"] = result.get("title", "")[:60]
        out["has_logout"] = result.get("has_logout", False)
        # Buscar el email esperado entre los visibles (case-insensitive)
        expected_low = expected_email.lower().strip()
        match = next((e for e in all_emails
                       if e.lower() == expected_low), None)
        out["found_email"] = match or (all_emails[0] if all_emails else "")
        out["all_emails_seen"] = all_emails[:8]
        if match:
            out["confirmed"] = True
            out["reason"] = "account_email_confirmed_in_profile_page"
        elif all_emails:
            out["reason"] = "different_email_in_session_account_mismatch"
        elif out.get("has_logout"):
            out["confirmed"] = True   # logueado pero sin email visible
            out["reason"] = "logged_but_email_not_extractable_from_page"
        else:
            out["reason"] = "no_login_indicators_found"
        return out
    except Exception as e:
        out["reason"] = f"verify_err: {type(e).__name__}: {str(e)[:80]}"
        return out


def _pick_morelogin_profile(used_ips_in_job: Optional[set] = None,
                              used_profiles_in_job: Optional[set] = None):
    """Pick perfil ES disponible cuya IP+profile NUNCA se hayan usado en este
    job. Garantiza que cada browser abierto en un mismo job tenga IP+perfil
    completamente distintos (no solo concurrentemente, sino históricamente
    en este job).

    Args:
        used_ips_in_job: set de IPs ya usadas en este job (no liberar entre slots).
        used_profiles_in_job: set de profiles ya usados en este job.
    """
    used_ips_in_job = used_ips_in_job or set()
    used_profiles_in_job = used_profiles_in_job or set()
    active_ips = _load_active_ips()
    skipped_ip_collision = 0
    skipped_profile_already_used = 0
    # Buscar hasta 20 páginas (2000 perfiles) — pool grande para asegurar IPs únicas.
    for page_no in range(1, 21):
        try:
            r = requests.post(f'{bo.MORELOGIN_API}/api/env/page',
                              json={'pageNo': page_no, 'pageSize': 100},
                              timeout=10).json()
        except Exception:
            return None
        items = (r.get('data') or {}).get('dataList') or []
        if not items: break
        for it in items:
            p = it.get('proxy') or {}
            name = it.get('envName') or ''
            if not name or name in bo._active_profiles:
                continue
            # Skip si profile YA fue usado en este job
            if name in used_profiles_in_job:
                skipped_profile_already_used += 1
                continue
            cc = (p.get('countryCode') or '').upper()
            user = p.get('username') or ''
            is_es = (cc == 'ES') or ('cc-ES' in user) or ('-ES-' in user)
            if not is_es:
                continue
            if p.get('proxyCheckStatus') == 2:
                continue
            ip = p.get('exportIp', '') or ''
            # Skip si IP ya usada en este job (cross-slot, histórico)
            if ip and ip in used_ips_in_job:
                skipped_ip_collision += 1
                continue
            # Skip si IP NO disponible globalmente (otro proceso del bot
            # la está usando, O fue usada en las últimas 2h por cualquier
            # proceso — registros, compras, filas, validaciones).
            allowed, _reason = _ip_lock.is_ip_available(ip, recent_hours=2.0)
            if not allowed:
                skipped_ip_collision += 1
                continue
            # Reserva atómica global
            if not _claim_ip(ip):
                skipped_ip_collision += 1
                continue
            bo._active_profiles.add(name)
            return {'profile': name, 'env_id': it['id'],
                    'ip': ip,
                    'skipped_ip_collisions': skipped_ip_collision,
                    'skipped_profiles_already_used': skipped_profile_already_used}
    return None


def _send_to_registrations(account):
    """Devuelve la cuenta a tm_registrations con status pending (para re-registrar).
    Crea un nuevo item con misma fila plantilla pero status=pending."""
    try:
        import tm_registrations as tr
        with tr._lock:
            d = tr._load()
            items = d.setdefault('items', [])
            # Si ya existe con este email → solo cambiar status
            em_low = (account.get('tm_email') or '').lower()
            existing = next((it for it in items
                              if (it.get('email') or '').lower() == em_low), None)
            now = datetime.now().isoformat()
            if existing:
                existing.update({
                    'status': tr.STATUS_PENDING,
                    'invalidated_at': now,
                    'invalidated_reason': 'bad_credentials_found_in_validator',
                })
            else:
                items.append({
                    'id': uuid.uuid4().hex[:10],
                    'email': account.get('tm_email', ''),
                    'first_name': account.get('first_name', ''),
                    'last_name': account.get('last_name', ''),
                    'country': 'es',
                    'status': tr.STATUS_PENDING,
                    'created_at': now,
                    'invalidated_at': now,
                    'invalidated_reason': 'bad_credentials_found_in_validator',
                    'source': 'account_validator',
                })
            tr._save(d)
            return True
    except Exception as e:
        _log("send_reg", "err", err=str(e)[:120])
        return False


_INVALID_REASONS_PERSIST = (
    "bad_credentials",
    "session_invalid_redirected_to_login",
    "account_mismatch",
    "fresh_login_form_not_reachable",
    "no_password_form",
    "no_login_form_visible",
    "session_persistent_unable_to_force_fresh",
)


def _apply_persistence(account: Dict[str, Any], out: Dict[str, Any],
                        opt_bump: bool, opt_mark_for_review: bool,
                        started: Optional[float] = None) -> None:
    """Persiste el resultado de validate_account en accounts.json.

    Idempotente: ignora si ya se persistió en esta llamada (out["_persisted"]).
    Llamada desde TODOS los return paths (early returns + final).
    Si `started` se pasa, también setea `out["elapsed_s"]`.
    """
    if started is not None:
        try: out["elapsed_s"] = round(time.time() - started, 1)
        except Exception: pass
    if out.get("_persisted"):
        return
    aid = account.get("id", "")
    if not aid:
        out["_persisted"] = True
        return
    try:
        valid = out.get("valid")
        reason = out.get("reason") or ""
        if valid is True:
            am.account_manager.update_account(aid, {
                "validated_at": datetime.now().isoformat(),
                "validation_reason": reason,
                "credentials_verified": True,
                "pending_user_review": False,
            })
        elif valid is False and reason in _INVALID_REASONS_PERSIST:
            patch = {
                "validated_at": datetime.now().isoformat(),
                "validation_reason": reason,
                "credentials_verified": False,
            }
            if opt_mark_for_review:
                # NO mover, NO cambiar status: solo marcar para revisión.
                patch["pending_user_review"] = True
            else:
                patch["status"] = "invalid_credentials"
            am.account_manager.update_account(aid, patch)
            if opt_bump and not opt_mark_for_review:
                try:
                    _send_to_registrations(account)
                    out["sent_to_registrations"] = True
                except Exception as e:
                    out["bump_err"] = str(e)[:80]
            else:
                out["sent_to_registrations"] = False
                if opt_mark_for_review:
                    out["bump_skipped"] = "marked_for_review"
                elif not opt_bump:
                    out["bump_skipped"] = "opt_bump=False"
        out["_persisted"] = True
    except Exception as e:
        out["persist_err"] = f"{type(e).__name__}: {str(e)[:80]}"
        out["_persisted"] = True


def validate_account(account: Dict[str, Any], slot: int = 0,
                      force_password_test: bool = False,
                      options: Optional[Dict[str, Any]] = None,
                      used_ips_in_job: Optional[set] = None,
                      used_profiles_in_job: Optional[set] = None) -> Dict[str, Any]:
    """Valida UNA cuenta con opciones modulares del menú UI.
    options dict:
      - verifySession (bool): comprobar sesión via storage_state
      - verifyPassword (bool): forzar login fresh con password
      - bumpToRegistrations (bool): mover cuentas inválidas a Registros TM
      - clearHistory (bool): limpieza CDP cookies/cache/storage al cerrar
      - updateStorage (bool): si login OK, refrescar storage_state_file
    """
    options = options or {}
    opt_session = options.get("verifySession", True)
    opt_password = options.get("verifyPassword", force_password_test)
    opt_bump = options.get("bumpToRegistrations", True)
    opt_clear = options.get("clearHistory", True)
    opt_update_storage = options.get("updateStorage", False)
    opt_skip_recent = options.get("skipRecentlyVerified", True)
    opt_mark_for_review = options.get("markForReview", False)
    # force_password_test queda mapeado al flag del menú
    force_password_test = force_password_test or opt_password
    aid = account.get("id", "")

    # ─── GUARDA #1: Skip si ya verificada recientemente (storage_state) ───
    # Ahorra cuentas + reduce riesgo de bloqueo TM por intentos repetidos.
    if opt_skip_recent and not force_password_test and _is_recently_verified(account):
        return {
            "account_id": aid,
            "tm_email": account.get('tm_email'),
            "valid": True,
            "reason": "skip_already_verified_recently",
            "elapsed_s": 0,
            "skipped": True,
            "previous_validated_at": account.get("validated_at"),
        }

    # ─── GUARDA #2: Cooldown FRESH login por cuenta ───
    # TM bloquea 3+ failed logins consecutivos. Forzamos espera 6h entre
    # tests FRESH para la misma cuenta.
    if force_password_test:
        allowed, reason_skip = _can_do_fresh_test(aid)
        if not allowed:
            return {
                "account_id": aid,
                "tm_email": account.get('tm_email'),
                "valid": None,
                "reason": reason_skip,
                "elapsed_s": 0,
                "skipped": True,
                "note": "saltado por seguridad anti-bloqueo TM",
            }
    aid = account.get('id', '')
    started = time.time()
    out: Dict[str, Any] = {
        "account_id": aid,
        "tm_email": account.get('tm_email'),
        "valid": False,
        "reason": "",
        "elapsed_s": 0,
    }
    prof = _pick_morelogin_profile(used_ips_in_job=used_ips_in_job,
                                     used_profiles_in_job=used_profiles_in_job)
    if not prof:
        out["reason"] = "no_morelogin_profile_with_unique_ip"
        return out
    _log(slot, "profile_picked", **prof)
    # Marcar IP+profile como usados en este job (cross-slot)
    if used_ips_in_job is not None and prof.get("ip"):
        used_ips_in_job.add(prof["ip"])
    if used_profiles_in_job is not None and prof.get("profile"):
        used_profiles_in_job.add(prof["profile"])
    try:
        # Start profile
        r = requests.post(f'{bo.MORELOGIN_API}/api/env/start',
                          json={'envId': prof['env_id']}, timeout=20).json()
        port = int(r['data']['debugPort'])
        time.sleep(4)
        from playwright.sync_api import sync_playwright
        v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
            ctx = b.contexts[0] if b.contexts else b.new_context()
            # CRÍTICO: cerrar TODAS las páginas previas (tabs que MoreLogin
            # restauró del cierre anterior) y crear UNA pestaña limpia.
            # Sin esto, al abrir el browser aparecen tabs antiguos cargados
            # con cookies/contenido del run anterior.
            try:
                old_pages = list(ctx.pages)
                page = ctx.new_page()   # crea una NUEVA en blanco
                for op in old_pages:
                    try: op.close()
                    except Exception: pass
                _log(slot, "fresh_page_created_old_closed",
                     old_count=len(old_pages))
            except Exception as e:
                _log(slot, "page_setup_err", err=str(e)[:80])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try: page.bring_to_front()
            except Exception: pass

            # Cargar storage_state si existe (saltado si force_password_test)
            ssf = account.get('storage_state_file')
            if force_password_test:
                # ESTRATEGIA ROBUSTA FRESH LOGIN — 4 fases:
                #   (1) Goto my.ticketmaster.es para tener referencia.
                #   (2) Si logueado → buscar link "Cerrar sesión" + click ⇒ logout
                #       SERVER-SIDE (TM invalida la sesión en su back).
                #   (3) Verificar que tras logout NO está logueado (recarga +
                #       check). Si aún logueado, aplicar nuclear: clear todos
                #       los cookies/storage CDP en TODOS los origins TM.
                #   (4) Goto signin y proceder con login fresh email+password.
                _log(slot, "fresh_login_start", strategy="server_side_logout")
                # (1) Ir a my.tick para detectar estado
                try:
                    page.goto("https://my.ticketmaster.es/",
                               wait_until="domcontentloaded", timeout=25000)
                    page.wait_for_timeout(3500)
                except Exception as e:
                    _log(slot, "fresh_goto_my_err", err=str(e)[:80])
                # Cookies banner
                for txt in ("Aceptar todas las cookies", "Aceptar", "Accept"):
                    try:
                        el = page.get_by_text(txt, exact=False).first
                        if el.is_visible(timeout=1500):
                            el.click(); page.wait_for_timeout(1500); break
                    except Exception: continue
                # (2) Si logueado → buscar y click "Cerrar sesión"
                logout_done = False
                if _detect_logged_in(page):
                    _log(slot, "fresh_login_currently_logged",
                         note="intentando logout server-side")
                    # Buscar link/botón de logout
                    logout_found = False
                    try:
                        # Estrategia A: visit logout URL directo
                        for logout_url in ("https://my.ticketmaster.es/logout",
                                            "https://www.ticketmaster.es/logout",
                                            "https://my.ticketmaster.es/account/sign-out",
                                            "https://identity.ticketmaster.com/logout"):
                            try:
                                page.goto(logout_url, wait_until="domcontentloaded",
                                           timeout=15000)
                                page.wait_for_timeout(2500)
                                logout_found = True
                                _log(slot, "fresh_logout_url_visited", url=logout_url)
                                break
                            except Exception:
                                continue
                    except Exception as e:
                        _log(slot, "fresh_logout_url_err", err=str(e)[:80])
                    # Estrategia B: click botón si las URLs no funcionan
                    if not logout_found:
                        try:
                            clicked = page.evaluate(r"""() => {
                                const links = Array.from(document.querySelectorAll('a, button'));
                                const lo = links.find(el => /cerrar sesi[oó]n|sign\s*out|log\s*out|cerrar cuenta/i.test(el.innerText || el.textContent || ''));
                                if (lo) {
                                    if (lo.tagName === 'A' && lo.href) { window.location.href = lo.href; return {ok:true, href:lo.href}; }
                                    lo.click();
                                    return {ok:true, clicked:true};
                                }
                                return {ok:false};
                            }""")
                            if clicked.get("ok"):
                                _log(slot, "fresh_logout_button_clicked", **clicked)
                                page.wait_for_timeout(4000)
                        except Exception: pass
                    # (3) Verificar logout efectivo
                    try:
                        page.goto("https://my.ticketmaster.es/",
                                   wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(3000)
                    except Exception: pass
                    if not _detect_logged_in(page):
                        logout_done = True
                        _log(slot, "fresh_logout_confirmed",
                             note="server-side logout exitoso")
                    else:
                        # FALLBACK NUCLEAR: limpiar TODO cookies+storage TM origins
                        try:
                            cdp = ctx.new_cdp_session(page)
                            cdp.send("Network.clearBrowserCache")
                            cdp.send("Network.clearBrowserCookies")
                            for origin in ("https://www.ticketmaster.es",
                                            "https://my.ticketmaster.es",
                                            "https://auth.ticketmaster.com",
                                            "https://identity.ticketmaster.com",
                                            "https://www.ticketmaster.com"):
                                try:
                                    cdp.send("Storage.clearDataForOrigin",
                                             {"origin": origin,
                                              "storageTypes": "all"})
                                except Exception: pass
                            ctx.clear_cookies()
                            _log(slot, "fresh_logout_nuclear_clear",
                                 note="limpieza CDP brutal en 5 origins TM")
                        except Exception as e:
                            _log(slot, "fresh_nuclear_err", err=str(e)[:80])
                        # Refresh y verificar última vez
                        try:
                            page.goto("https://my.ticketmaster.es/",
                                       wait_until="domcontentloaded", timeout=15000)
                            page.wait_for_timeout(2500)
                        except Exception: pass
                        logout_done = not _detect_logged_in(page)
                else:
                    logout_done = True
                    _log(slot, "fresh_login_not_logged_already",
                         note="cookies inválidas o caducadas — direct login fresh")

                if not logout_done:
                    # Imposible forzar fresh login con esta cuenta — la sesión
                    # persiste pese a logout y nuclear clear. Reportar honesto.
                    out["valid"] = None
                    out["reason"] = "session_persistent_unable_to_force_fresh"
                    out["note"] = "TM mantiene sesión activa; storage_state es válido implícitamente"
                    if opt_clear:
                        try:
                            import browser_cleanup as bc
                            bc.clear_history_via_cdp(ctx)
                        except Exception: pass
                    b.close()
                    _apply_persistence(account, out, opt_bump, opt_mark_for_review, started=started)
                    return out

                # (4) Goto signin form para login fresh email+password
                signin_urls = [
                    "https://my.ticketmaster.es/login",
                    "https://www.ticketmaster.es/login",
                    "https://identity.ticketmaster.com/login",
                ]
                form_visible = False
                for u in signin_urls:
                    try:
                        page.goto(u, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(3500)
                        try:
                            page.wait_for_selector('input[type="email"]', timeout=5000)
                            form_visible = True
                            _log(slot, "fresh_signin_form_visible", url=u[:80])
                            break
                        except Exception: continue
                    except Exception: continue
                if not form_visible:
                    out["valid"] = False
                    out["reason"] = "fresh_login_form_not_reachable"
                    if opt_clear:
                        try:
                            import browser_cleanup as bc
                            bc.clear_history_via_cdp(ctx)
                        except Exception: pass
                    b.close()
                    _apply_persistence(account, out, opt_bump, opt_mark_for_review, started=started)
                    return out
            elif ssf and os.path.exists(ssf):
                try:
                    with open(ssf, encoding='utf-8') as f:
                        st = json.load(f)
                    ctx.clear_cookies()
                    if st.get('cookies'):
                        ctx.add_cookies(st['cookies'])
                    _log(slot, "storage_state_loaded",
                         cookies=len(st.get('cookies') or []))
                except Exception as e:
                    _log(slot, "storage_state_err", err=str(e)[:80])

            # Goto login page (saltado si ya navegamos al signin con prompt=login)
            if not force_password_test:
                page.goto("https://my.ticketmaster.es/",
                           wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4000)

            # Si ya está logueado por storage_state → válida (NO en force_password_test:
            # ahí el form de email DEBE estar visible obligatoriamente).
            if not force_password_test and _detect_logged_in(page):
                out["valid"] = True
                out["reason"] = "storage_state_active_session"
                _log(slot, "valid_via_storage_state", email=account.get('tm_email'))
                # ─── NUEVO: verificar que la cuenta REGISTRADA coincide ───
                # Visita /settings y comprueba que el email del perfil TM
                # coincide con el del JSON local. Si no coincide → la sesión
                # storage_state es de OTRA cuenta (account_mismatch).
                identity = _verify_account_identity(
                    page, account.get("tm_email", ""), slot=slot)
                out["identity"] = identity
                _log(slot, "identity_verified", **{k: v for k, v in identity.items()
                                                     if k in ("confirmed",
                                                               "found_email",
                                                               "reason")})
                if identity.get("confirmed"):
                    out["reason"] = "account_registered_and_session_active"
                    out["account_registered_in_tm"] = True
                else:
                    # La sesión existe pero el email visible no coincide → MISMATCH.
                    # Esto puede indicar storage_state corrupto o cuenta cambiada en TM.
                    if identity.get("reason") == "different_email_in_session_account_mismatch":
                        out["valid"] = False
                        out["reason"] = "account_mismatch"
                        out["account_registered_in_tm"] = "unknown_session_belongs_to_other_account"
                    elif identity.get("reason") == "redirected_to_login_session_invalid":
                        out["valid"] = False
                        out["reason"] = "session_invalid_redirected_to_login"
                        out["account_registered_in_tm"] = False
                    else:
                        # No pudimos extraer email pero sí hay logout button → asumir registrada
                        out["account_registered_in_tm"] = "probably"
                # Refrescar storage_state si flag activo
                if opt_update_storage and ssf:
                    try:
                        state = ctx.storage_state()
                        with open(ssf, "w", encoding="utf-8") as f:
                            json.dump(state, f, indent=2, ensure_ascii=False)
                        out["storage_updated"] = True
                        _log(slot, "storage_state_refreshed",
                             path=os.path.basename(ssf))
                    except Exception as e:
                        out["storage_update_err"] = str(e)[:80]
                # cleanup historial solo si flag activo
                if opt_clear:
                    try:
                        import browser_cleanup as bc
                        bc.clear_history_via_cdp(ctx)
                        _log(slot, "history_cleaned_early_return")
                    except Exception: pass
                # Cerrar todas las pages antes del browser.close()
                try:
                    for p in list(ctx.pages):
                        try: p.close()
                        except Exception: pass
                except Exception: pass
                b.close()
                _apply_persistence(account, out, opt_bump, opt_mark_for_review, started=started)
                return out

            # Cookies banner
            for txt in ("Aceptar todas las cookies", "Aceptar", "Accept"):
                try:
                    el = page.get_by_text(txt, exact=False).first
                    if el.is_visible(timeout=1500):
                        el.click(); page.wait_for_timeout(1500); break
                except Exception: continue

            # Form login
            try:
                page.wait_for_selector('input[type="email"]', timeout=15000)
            except Exception:
                # Posible page no requiere login → asumir válida
                if _detect_logged_in(page):
                    out["valid"] = True
                    out["reason"] = "already_logged"
                else:
                    out["reason"] = "no_login_form_visible"
                try:
                    import browser_cleanup as bc
                    bc.clear_history_via_cdp(ctx)
                except Exception: pass
                b.close()
                _apply_persistence(account, out, opt_bump, opt_mark_for_review, started=started)
                return out

            # Pacing humano: pausas variables para evitar fingerprinting bot
            import random as _rnd
            page.wait_for_timeout(int(800 + _rnd.random() * 1200))
            page.click('input[type="email"]')
            page.wait_for_timeout(int(400 + _rnd.random() * 600))
            page.type('input[type="email"]', account['tm_email'],
                       delay=int(70 + _rnd.random() * 60))
            page.wait_for_timeout(int(1500 + _rnd.random() * 1500))
            for nxt in ("Continuar", "Continue", "Siguiente", "Next"):
                try:
                    btn = page.get_by_role("button",
                                            name=re.compile(nxt, re.I)).first
                    if btn.is_visible(timeout=1500):
                        btn.click(force=True); break
                except Exception: continue
            page.wait_for_timeout(3500)
            try:
                page.wait_for_selector('input[type="password"]', timeout=15000)
            except Exception:
                out["reason"] = "no_password_form"
                try:
                    import browser_cleanup as bc
                    bc.clear_history_via_cdp(ctx)
                except Exception: pass
                b.close()
                _apply_persistence(account, out, opt_bump, opt_mark_for_review, started=started)
                return out
            page.wait_for_timeout(int(900 + _rnd.random() * 1200))
            page.click('input[type="password"]')
            page.wait_for_timeout(int(400 + _rnd.random() * 600))
            page.type('input[type="password"]', account.get('tm_password', ''),
                      delay=int(70 + _rnd.random() * 60))
            page.wait_for_timeout(int(1500 + _rnd.random() * 1500))
            for nxt in ("Iniciar sesión", "Sign In", "Login", "Continuar"):
                try:
                    btn = page.get_by_role("button",
                                            name=re.compile(nxt, re.I)).first
                    if btn.is_visible(timeout=1500):
                        btn.click(force=True); break
                except Exception: continue
            page.wait_for_timeout(7000)

            if _detect_bad_creds(page):
                out["valid"] = False
                out["reason"] = "bad_credentials"
                _log(slot, "BAD_CREDS", email=account.get('tm_email'))
            elif _detect_logged_in(page):
                out["valid"] = True
                out["reason"] = "login_ok"
                _log(slot, "VALID", email=account.get('tm_email'))
            else:
                # Posible OTP → no la marcamos como inválida (puede ser válida pero
                # con 2FA), reportamos otp_required.
                try:
                    otp = page.locator('input[name*="code" i], input[autocomplete*="one-time" i]').first
                    if otp.is_visible(timeout=2000):
                        out["valid"] = True   # asumir válida con OTP (TM no rechazó password)
                        out["reason"] = "otp_required_password_ok"
                        _log(slot, "VALID_with_otp", email=account.get('tm_email'))
                    else:
                        out["valid"] = False
                        out["reason"] = "unknown_state"
                except Exception:
                    out["valid"] = False
                    out["reason"] = "unknown_state"

            # Refrescar storage_state si login OK y opt_update_storage activado
            if opt_update_storage and out["valid"] and ssf:
                try:
                    state = ctx.storage_state()
                    with open(ssf, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2, ensure_ascii=False)
                    out["storage_updated"] = True
                    _log(slot, "storage_state_refreshed", path=os.path.basename(ssf))
                except Exception as e:
                    out["storage_update_err"] = str(e)[:80]

            if opt_clear:
                try:
                    import browser_cleanup as bc
                    bc.clear_history_via_cdp(ctx)
                except Exception: pass
            # Cerrar TODAS las pages antes del browser.close() para que la
            # próxima vez que MoreLogin arranque NO restaure estas tabs.
            try:
                for p in list(ctx.pages):
                    try: p.close()
                    except Exception: pass
            except Exception: pass
            b.close()
    except Exception as e:
        out["reason"] = f"error: {type(e).__name__}: {str(e)[:120]}"
        _log(slot, "ERR", **out)
    finally:
        # GARANTÍA TRIPLE de cierre — el browser MoreLogin SIEMPRE debe cerrarse
        # para no acumular ventanas huérfanas.
        # Intento 1: API close oficial
        closed_ok = False
        try:
            r = requests.post(f'{bo.MORELOGIN_API}/api/env/close',
                               json={'envId': prof['env_id']},
                               timeout=15).json()
            closed_ok = (r.get("code") == 0)
        except Exception:
            pass
        # Intento 2: si falló, esperar 2s y reintentar (a veces MoreLogin
        # necesita un momento)
        if not closed_ok:
            try:
                time.sleep(2)
                requests.post(f'{bo.MORELOGIN_API}/api/env/close',
                               json={'envId': prof['env_id']},
                               timeout=15)
            except Exception: pass
        # Liberar lock del profile + IP claim
        bo._active_profiles.discard(prof['profile'])
        _release_ip(prof.get('ip', ''))
        _log(slot, "browser_closed_finally",
             env_id=prof.get('env_id'), profile=prof.get('profile'),
             ip_released=prof.get('ip', ''))

    out["elapsed_s"] = round(time.time() - started, 1)

    # Persistir histórico FRESH login (cooldown + fail counter)
    if force_password_test and not out.get("skipped"):
        try:
            _record_fresh_test(aid, bool(out.get("valid")),
                                str(out.get("reason", "")))
        except Exception: pass

    # ─── AUTO-RETRY de errores transitorios ───
    # Si la cuenta dio TimeoutError/ERR_ABORTED/ReadTimeout/TargetClosed,
    # reintentar UNA vez (perfil distinto) antes de marcarla. Las cuentas
    # válidas no deben quedar como "error" por timeouts de red.
    transient_errors = ("TimeoutError", "ReadTimeout", "ERR_ABORTED",
                          "TargetClosed", "ConnectionError", "net::ERR")
    reason_str = str(out.get("reason", ""))
    is_transient = any(e in reason_str for e in transient_errors)
    retry_count = (account.get("_retry_count") or 0)
    if is_transient and retry_count < 1 and not out.get("skipped"):
        _log(slot, "auto_retry_transient",
             reason=reason_str[:80], retry_count=retry_count + 1)
        # Marcar internamente y reintentar
        account_retry = dict(account)
        account_retry["_retry_count"] = retry_count + 1
        time.sleep(3)  # pacing
        return validate_account(account_retry, slot=slot,
                                  force_password_test=force_password_test,
                                  options=options,
                                  used_ips_in_job=used_ips_in_job,
                                  used_profiles_in_job=used_profiles_in_job)

    # Aplicar resultado: persistencia centralizada (idempotente).
    # Cubre los paths NO-early. Los early returns ya llamaron a _apply_persistence.
    _apply_persistence(account, out, opt_bump, opt_mark_for_review, started=started)
    return out


def _run_job(job_id: str, account_ids: List[str], max_parallel: int):
    """Worker que corre el batch usando ThreadPoolExecutor con timeout.
    Robustez total: cualquier excepción/cuelgue en una cuenta NO afecta al resto.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
    accs = am.account_manager.list_accounts()
    by_id = {a.get('id'): a for a in accs}
    targets = [by_id[aid] for aid in account_ids if aid in by_id]
    with _jobs_lock:
        _JOBS[job_id]["total"] = len(targets)
        _JOBS[job_id]["status"] = "running"
        force_pw = bool(_JOBS[job_id].get("force_password_test"))
        opts = dict(_JOBS[job_id].get("options") or {})

    abort_flag = {"abort": False, "reason": ""}
    job_used_ips = set()
    job_used_profiles = set()
    apply_fail_rate_guard = bool(force_pw)
    PER_ACCOUNT_TIMEOUT_S = 180   # 3 min hard timeout per account

    def _validate_one_safe(acc, slot):
        """Wrapper SIEMPRE devuelve un dict result. Excepciones capturadas."""
        try:
            if abort_flag["abort"]:
                return {"account_id": acc.get("id"),
                        "tm_email": acc.get("tm_email"),
                        "valid": None,
                        "reason": f"batch_aborted: {abort_flag['reason']}",
                        "skipped": True}
            return validate_account(acc, slot=slot,
                                     force_password_test=force_pw,
                                     options=opts,
                                     used_ips_in_job=job_used_ips,
                                     used_profiles_in_job=job_used_profiles)
        except Exception as e:
            _log(slot, "validate_one_uncaught_err",
                 acc=acc.get("id"), err=f"{type(e).__name__}: {str(e)[:100]}")
            return {"account_id": acc.get("id"),
                    "tm_email": acc.get("tm_email"),
                    "valid": False,
                    "reason": f"runner_error: {type(e).__name__}",
                    "elapsed_s": 0}

    def _record_result(res):
        """Actualiza contadores+abort logic. Thread-safe."""
        with _jobs_lock:
            _JOBS[job_id]["results"].append(res)
            _JOBS[job_id]["done"] += 1
            if res.get("valid"):
                _JOBS[job_id]["valid_count"] += 1
            else:
                _JOBS[job_id]["invalid_count"] += 1
            if _JOBS[job_id]["done"] % 5 == 0:
                try: _persist_jobs()
                except Exception: pass
            if apply_fail_rate_guard:
                done = _JOBS[job_id]["done"]
                fails = _JOBS[job_id]["invalid_count"]
                if (fails >= _BATCH_MIN_FAILS_FOR_ABORT and
                        fails / max(1, done) >= _BATCH_FAIL_RATIO_ABORT):
                    abort_flag["abort"] = True
                    abort_flag["reason"] = (f"high_fail_rate "
                                              f"{fails}/{done}={fails*100/done:.0f}%")
                    _JOBS[job_id]["aborted"] = True
                    _JOBS[job_id]["abort_reason"] = abort_flag["reason"]

    # ThreadPoolExecutor + timeout por future = ROBUSTO contra cuelgues.
    try:
        with ThreadPoolExecutor(max_workers=max_parallel,
                                  thread_name_prefix=f"val_{job_id}") as ex:
            future_to_slot = {}
            for i, acc in enumerate(targets):
                slot = i + 1
                fut = ex.submit(_validate_one_safe, acc, slot)
                future_to_slot[fut] = (slot, acc)
            _log("runner", "all_submitted", total=len(future_to_slot),
                 job_id=job_id)
            for fut in as_completed(future_to_slot):
                slot, acc = future_to_slot[fut]
                try:
                    res = fut.result(timeout=PER_ACCOUNT_TIMEOUT_S)
                except FuturesTimeout:
                    _log(slot, "FUTURE_TIMEOUT",
                         account_id=acc.get("id"),
                         tm_email=acc.get("tm_email"),
                         timeout_s=PER_ACCOUNT_TIMEOUT_S)
                    res = {"account_id": acc.get("id"),
                           "tm_email": acc.get("tm_email"),
                           "valid": False,
                           "reason": f"hard_timeout_{PER_ACCOUNT_TIMEOUT_S}s",
                           "elapsed_s": PER_ACCOUNT_TIMEOUT_S}
                except Exception as e:
                    _log(slot, "FUTURE_EXCEPTION", err=str(e)[:100])
                    res = {"account_id": acc.get("id"),
                           "tm_email": acc.get("tm_email"),
                           "valid": False,
                           "reason": f"future_error: {type(e).__name__}"}
                _record_result(res)
    except Exception as e:
        _log("runner", "RUNNER_FATAL", err=f"{type(e).__name__}: {str(e)[:120]}")
        with _jobs_lock:
            _JOBS[job_id]["status"] = "error"
            _JOBS[job_id]["error"] = str(e)[:200]
        _persist_jobs()
        return

    with _jobs_lock:
        _JOBS[job_id]["status"] = "done"
        _JOBS[job_id]["finished_at"] = datetime.now().isoformat()
    _persist_jobs()

    # WATCHDOG: tras cada job, asegurar que NO queda ningún browser MoreLogin
    # huérfano abierto. Limpieza automática para evitar acumulación.
    try:
        cleanup = close_all_running_browsers()
        if cleanup.get("closed_count", 0) > 0:
            _log("watchdog", "post_job_cleanup",
                 closed=cleanup.get("closed_count"),
                 job_id=job_id)
    except Exception:
        pass


def validate_batch(account_ids: List[str], max_parallel: int = 2,
                   force_password_test: bool = False,
                   options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Lanza job en background. Devuelve {job_id, total}.
    options dict con flags del menú UI:
      - verifySession (bool, default True)
      - verifyPassword (bool, default = force_password_test)
      - bumpToRegistrations (bool, default True)
      - clearHistory (bool, default True)
      - updateStorage (bool, default False)
    """
    if not account_ids:
        return {"ok": False, "error": "account_ids required"}
    options = dict(options or {})
    options.setdefault("verifySession", True)
    options.setdefault("verifyPassword", force_password_test)
    options.setdefault("bumpToRegistrations", True)
    options.setdefault("clearHistory", True)
    options.setdefault("updateStorage", False)
    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _JOBS[job_id] = {
            "id": job_id,
            "total": len(account_ids),
            "done": 0,
            "valid_count": 0,
            "invalid_count": 0,
            "status": "starting",
            "started_at": datetime.now().isoformat(),
            "results": [],
            "max_parallel": max_parallel,
            "force_password_test": force_password_test,
            "options": options,
        }
    _persist_jobs()
    th = threading.Thread(target=_run_job,
                           args=(job_id, account_ids, max_parallel),
                           daemon=True)
    th.start()
    return {"ok": True, "job_id": job_id, "total": len(account_ids)}


def delete_job(job_id: str) -> Dict[str, Any]:
    """Elimina un job del registro (típicamente para limpiar histórico)."""
    with _jobs_lock:
        removed = _JOBS.pop(job_id, None) is not None
    if removed: _persist_jobs()
    return {"ok": removed, "job_id": job_id}


def clear_done_jobs() -> Dict[str, Any]:
    """Borra todos los jobs en status=done (útil para no acumular histórico)."""
    with _jobs_lock:
        before = len(_JOBS)
        for jid in list(_JOBS.keys()):
            if _JOBS[jid].get("status") in ("done", "interrupted", "error"):
                del _JOBS[jid]
        removed = before - len(_JOBS)
    _persist_jobs()
    return {"ok": True, "removed": removed}


def get_job(job_id: str) -> Dict[str, Any]:
    with _jobs_lock:
        job = _JOBS.get(job_id)
        return dict(job) if job else {"ok": False, "error": "job_not_found"}


def list_jobs() -> List[Dict[str, Any]]:
    with _jobs_lock:
        return [dict(j) for j in _JOBS.values()]


def close_all_running_browsers() -> Dict[str, Any]:
    """Cierra TODOS los browsers MoreLogin actualmente abiertos (huérfanos
    incluidos). Útil tras crash o para limpieza periódica.
    Devuelve {ok, closed_count, failed_count, env_ids_closed}."""
    closed = []
    failed = []
    try:
        r = requests.post(f"{bo.MORELOGIN_API}/api/env/page",
                           json={"pageNo": 1, "pageSize": 200},
                           timeout=10).json()
        items = (r.get("data") or {}).get("dataList") or []
        running = [it for it in items if it.get("runStatus") == 1]
        for it in running:
            env_id = it.get("id")
            if not env_id: continue
            try:
                cr = requests.post(f"{bo.MORELOGIN_API}/api/env/close",
                                    json={"envId": env_id},
                                    timeout=10).json()
                if cr.get("code") == 0:
                    closed.append(env_id)
                    bo._active_profiles.discard(it.get("envName") or "")
                else:
                    failed.append({"env_id": env_id,
                                    "code": cr.get("code"),
                                    "msg": cr.get("msg", "")[:80]})
            except Exception as e:
                failed.append({"env_id": env_id,
                                "err": str(e)[:80]})
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": True, "closed_count": len(closed),
            "failed_count": len(failed),
            "env_ids_closed": closed[:20], "failed": failed[:5]}


def bulk_mark_for_review(account_ids: List[str],
                           reason: str = "deferred_for_user_review",
                           dry_run: bool = False) -> Dict[str, Any]:
    """Marca cuentas con flag `pending_user_review=true` SIN moverlas a
    Registros TM. Quedan visibles en la tabla con badge naranja para que
    el usuario las revise/decida manualmente cuándo registrar de nuevo."""
    accs = am.account_manager.list_accounts()
    by_id = {a.get("id"): a for a in accs}
    marked = []
    not_found = []
    for aid in account_ids:
        a = by_id.get(aid)
        if not a:
            not_found.append(aid); continue
        if dry_run:
            marked.append({"id": aid, "email": a.get("tm_email")})
            continue
        try:
            am.account_manager.update_account(aid, {
                "pending_user_review": True,
                "review_reason": reason,
                "review_marked_at": datetime.now().isoformat(),
            })
            marked.append({"id": aid, "email": a.get("tm_email")})
        except Exception: pass
    return {"ok": True, "dry_run": dry_run,
            "marked_count": len(marked), "not_found_count": len(not_found)}


def bulk_clear_review_flag(account_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Limpia el flag pending_user_review (cuando el user ya las revisó)."""
    accs = am.account_manager.list_accounts()
    if account_ids is None:
        targets = [a for a in accs if a.get("pending_user_review")]
    else:
        targets = [a for a in accs if a.get("id") in set(account_ids)]
    cleared = 0
    for a in targets:
        try:
            am.account_manager.update_account(a.get("id"), {
                "pending_user_review": False,
                "review_cleared_at": datetime.now().isoformat(),
            })
            cleared += 1
        except Exception: pass
    return {"ok": True, "cleared": cleared}


def revert_bulk_move(account_ids: List[str]) -> Dict[str, Any]:
    """Revierte un bulk_move_to_registrations: restaura status='active' y
    elimina la entry de tm_registrations.json para esas cuentas."""
    accs = am.account_manager.list_accounts()
    by_id = {a.get("id"): a for a in accs}
    reverted = 0
    # 1) Restaurar status active en accounts.json
    for aid in account_ids:
        a = by_id.get(aid)
        if not a: continue
        try:
            am.account_manager.update_account(aid, {
                "status": "active",
                "validation_reason": "",
                "bulk_moved": False,
                "reverted_at": datetime.now().isoformat(),
            })
            reverted += 1
        except Exception: pass
    # 2) Limpiar entries de tm_registrations.json
    try:
        import tm_registrations as tr
        emails_to_remove = set((by_id.get(aid, {}).get("tm_email") or "").lower()
                                for aid in account_ids if by_id.get(aid))
        with tr._lock:
            d = tr._load()
            items = d.get("items", [])
            kept = []
            removed = 0
            for it in items:
                em_low = (it.get("email") or "").lower()
                if em_low in emails_to_remove and \
                        it.get("invalidated_reason") == "bad_credentials_found_in_validator":
                    removed += 1
                    continue
                kept.append(it)
            d["items"] = kept
            tr._save(d)
    except Exception as e:
        return {"ok": False, "reverted_accounts": reverted, "err": str(e)[:120]}
    return {"ok": True, "reverted_accounts": reverted,
            "removed_from_registrations": removed}


def bulk_move_to_registrations(account_ids: List[str],
                                 reason: str = "manual_bulk_move",
                                 dry_run: bool = False) -> Dict[str, Any]:
    """Mueve directamente N cuentas a Registros TM con status=pending.
    NO requiere validación previa. Útil para sacar cuentas del pool sin
    pasarlas por el validador (ahorra tiempo y evita carga sobre TM)."""
    accs = am.account_manager.list_accounts()
    by_id = {a.get("id"): a for a in accs}
    moved = []
    not_found = []
    for aid in account_ids:
        a = by_id.get(aid)
        if not a:
            not_found.append(aid); continue
        if dry_run:
            moved.append({"id": aid, "email": a.get("tm_email")})
            continue
        try:
            am.account_manager.update_account(aid, {
                "status": "invalid_credentials",
                "validated_at": datetime.now().isoformat(),
                "validation_reason": reason,
                "bulk_moved": True,
            })
            _send_to_registrations(a)
            moved.append({"id": aid, "email": a.get("tm_email")})
        except Exception as e:
            _log("bulk_move", "err", aid=aid, err=str(e)[:100])
    return {"ok": True, "dry_run": dry_run,
            "moved_count": len(moved), "not_found_count": len(not_found),
            "moved": moved[:50], "moved_truncated": len(moved) > 50}


def audit_and_purge_invalid(reasons_to_purge: Optional[List[str]] = None,
                             dry_run: bool = False) -> Dict[str, Any]:
    """Recorre todas las cuentas activas, identifica las inválidas según el
    último resultado de validación y las mueve a tm_registrations status=pending.

    Cuentas se consideran inválidas si su validation_reason ∈ reasons_to_purge.
    Default: ['bad_credentials', 'session_invalid_redirected_to_login',
             'account_mismatch'].

    Si dry_run=True, solo lista las que se purgarían sin tocarlas.
    """
    if reasons_to_purge is None:
        reasons_to_purge = [
            "bad_credentials",
            "session_invalid_redirected_to_login",
            "account_mismatch",
        ]
    accs = am.account_manager.list_accounts()
    purged = []
    skipped = []
    for a in accs:
        reason = a.get("validation_reason") or ""
        # También cuentas que ya tienen status invalid_credentials
        invalid = (reason in reasons_to_purge or
                   a.get("status") == "invalid_credentials")
        if not invalid:
            skipped.append(a.get("id"))
            continue
        purged.append({
            "id": a.get("id"),
            "email": a.get("tm_email"),
            "reason": reason or a.get("status"),
        })
        if not dry_run:
            try:
                am.account_manager.update_account(a.get("id"), {
                    "status": "invalid_credentials",
                    "purged_at": datetime.now().isoformat(),
                })
                _send_to_registrations(a)
            except Exception:
                pass
    return {
        "ok": True,
        "dry_run": dry_run,
        "total_accounts": len(accs),
        "purged_count": len(purged),
        "skipped_count": len(skipped),
        "purged_accounts": purged[:30],   # cap output
        "purged_truncated": len(purged) > 30,
    }
