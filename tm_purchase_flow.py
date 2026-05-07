"""tm_purchase_flow.py — Orquestador del flujo de compra TM.

Integra TODOS los módulos reales disponibles:
- ja4_spoof      → TLS fingerprint Chrome
- cookie_warmer  → warming + AcpAT pool
- proxy_health   → pick best proxy vivo
- captcha_manager → solver real (CapSolver/2Captcha)
- purchases_manager → registra éxito
- error_manager  → registra fallos
- problem_center → crea problemas automáticos
- notifications  → webhook Discord/Telegram al completar

⚠️ AVISO LEGAL (lee antes de usar):
- Este módulo orquesta peticiones HTTP a Ticketmaster.
- Usuario es responsable del cumplimiento de TOS (sus cuentas, sus datos, sus tarjetas).
- Por defecto corre en modo SIMULATE para testing seguro.
- Los pasos "live" están claramente marcados y requieren configuración explícita.

Flujo completo de compra (live mode):
  1. pick_proxy         — elige proxy saludable (opcional geo match)
  2. warm_session       — GET home TM, captura cookies
  3. login              — POST /auth con email+password
  4. wait_for_onsale    — espera hasta sale_date
  5. enter_queue        — handle Queue-it (usa AcpAT pool si hay)
  6. find_tickets       — GET event, busca availability
  7. select_seats       — selecciona según estrategia
  8. add_to_cart        — POST add
  9. solve_captcha_if_needed → usa captcha_manager
 10. checkout           — POST pago con tarjeta
 11. confirm            — verifica confirmación TM
 12. register_purchase  — guarda éxito en purchases_manager
 13. notify             — webhook Discord

Cada paso devuelve dict {step, ok, elapsed_ms, error?, data?} y el flujo
se detiene en el primer fallo irrecuperable.
"""
import time
import random
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import json_atomic
import compliance_gate


FLOW_LOG_KEY = "purchase_flows.json"

# Estrategias de selección de asientos
STRATEGIES = {
    "best_available": "Mejor disponible (TM default)",
    "cheapest":       "Más barato primero",
    "fastest":        "Checkout más rápido (cualquier asiento)",
    "sniper":         "Sección/fila exacta",
    "floor_only":     "Solo pista / floor",
    "vip_first":      "VIP primero, luego normal",
    "distributed":    "Multi-dominio en paralelo",
    "polling_loop":   "Ciclo entre N eventos",
}

_lock = threading.Lock()


def _log_path():
    return config.ruta(FLOW_LOG_KEY)


def _append_log(flow_id: str, entry: dict):
    with _lock:
        d = json_atomic.load(_log_path(), default={"flows": {}})
        f = d["flows"].setdefault(flow_id, {"steps": [], "started_at": datetime.now().isoformat()})
        f["steps"].append(entry)
        f["steps"] = f["steps"][-100:]  # cap
        json_atomic.save(_log_path(), d)


def get_log(flow_id: Optional[str] = None, limit: int = 20) -> Dict:
    d = json_atomic.load(_log_path(), default={"flows": {}})
    if flow_id:
        return {"ok": True, "flow": d["flows"].get(flow_id, {})}
    flows = list(d.get("flows", {}).items())
    flows.sort(key=lambda x: x[1].get("started_at", ""), reverse=True)
    return {"ok": True, "flows": [{"id": fid, **f} for fid, f in flows[:limit]]}


# ═══════════════════════════════════════════
#  STEPS (cada uno autónomo, ok/error)
# ═══════════════════════════════════════════
def step_pick_proxy(ctx: dict) -> dict:
    """Paso 1: elige mejor proxy vivo para el país del evento."""
    started = time.time()
    try:
        import proxy_health
        geo = ctx.get("country")
        best = proxy_health.pick_best(geo=geo, min_score=50)
        if not best:
            # fallback sin geo
            best = proxy_health.pick_best(min_score=30)
        ctx["proxy"] = best
        return {"step": "pick_proxy", "ok": bool(best),
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"proxy_id": best.get("id") if best else None,
                         "status": best.get("status") if best else None,
                         "score": best.get("score") if best else 0} if best else None,
                "error": None if best else "no hay proxies alive"}
    except Exception as e:
        return {"step": "pick_proxy", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_pin_proxy_session(ctx: dict) -> dict:
    """Paso 1b: pinea el proxy elegido para que se use la misma IP
    durante warm → queue → checkout. Evita invalidación de AcpAT."""
    started = time.time()
    try:
        import proxy_session_pin as psp
        proxy = ctx.get("proxy") or {}
        if not proxy.get("id"):
            return {"step": "pin_proxy_session", "ok": True,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "data": {"skipped": "no_proxy"}}
        sid = f"flow_{ctx.get('flow_id','?')}"
        r = psp.pin_session(
            session_id=sid, proxy_id=proxy["id"],
            geo=ctx.get("country", "us"),
        )
        ctx["pin_session_id"] = sid
        return {"step": "pin_proxy_session", "ok": bool(r.get("ok", True)),
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"session_id": sid, "proxy_id": proxy["id"]}}
    except Exception as e:
        return {"step": "pin_proxy_session", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_warm_session(ctx: dict) -> dict:
    """Paso 2: warming del dominio TM con curl_cffi (Chrome 120 impersonate)
    + intento de obtener `_abck`/`bm_sz` válidos via hyper_sdk_chain.

    Estrategia:
      1. `warm_with_akamai_sensor` (chain de vendors: akamai → kasada → datadome).
         Si está en `mode='live'` con api_key Hyper Solutions/TakionAPI
         devuelve cookies REALES; si está en simulate devuelve mocks.
      2. Fallback: `warm_cookies(client='curl_cffi')` para warming HTTP/2 + TLS
         Chrome real → 200 OK + cookies orgánicas TM.

    El paso es OK si AL MENOS uno de los dos da cookies.
    """
    started = time.time()
    try:
        import cookie_warmer
        country = ctx.get("country", "us")
        event_url = (ctx.get("event") or {}).get("tm_url", "")
        cookies_total: dict = {}
        sensor_data = {}

        # 1) Akamai sensor via hyper_sdk_chain
        try:
            sr = cookie_warmer.warm_with_akamai_sensor(
                domain_id=country, page_url=event_url,
                user_agent=ctx.get("warm_ua", ""))
            if sr.get("success"):
                sensor_data = sr
                cookies_total.update(sr.get("cookies_dict") or {})
                ctx["warm_ua"] = sr.get("user_agent") or ctx.get("warm_ua")
        except Exception as e:
            sensor_data = {"error": f"{type(e).__name__}: {str(e)[:80]}"}

        # 2) Warming HTTP estándar via curl_cffi (Chrome 120 impersonate)
        r = cookie_warmer.warm_cookies(domain_id=country, pages=3, timeout=10,
                                        client="curl_cffi")
        ctx["warm_cookies"] = r.get("cookie_names", [])
        ctx["warm_ua"] = ctx.get("warm_ua") or r.get("user_agent")
        if r.get("success"):
            cookies_total.update({n: "" for n in r.get("cookie_names") or []})

        ok = bool(cookies_total)
        return {"step": "warm_session", "ok": ok,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"cookies": len(cookies_total),
                         "pages": r.get("pages_visited", 0),
                         "client_used": r.get("client_used"),
                         "akamai_sensor": {
                             "success": sensor_data.get("success", False),
                             "vendor": sensor_data.get("vendor"),
                             "mode": sensor_data.get("mode"),
                             "sensor_length": sensor_data.get("sensor_length"),
                         }},
                "error": r.get("error") or sensor_data.get("error")}
    except Exception as e:
        return {"step": "warm_session", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_get_session(ctx: dict) -> dict:
    """Paso 3: obtiene session HTTP con JA4+ spoof.

    En modo live con browser_pool habilitado, además adquiere un contexto
    Playwright pre-warmed (con cookies PX ya inyectadas) para los pasos
    de cart/checkout → ahorra ~3-5 s por compra.
    """
    started = time.time()
    try:
        import ja4_spoof
        session = ja4_spoof.get_session(profile=ctx.get("ja4_profile"))
        ctx["session"] = session

        # En modo live, intenta adquirir contexto pre-warmed del browser pool
        pool_data = None
        if ctx.get("mode") == "live":
            try:
                import browser_pool
                h = browser_pool.acquire(timeout_s=3)
                if h and h.get("ok"):
                    ctx["browser_handle_id"] = h.get("hid")
                    pool_data = {"hid": h.get("hid"),
                                 "px_cookies": h.get("px_cookies_count", 0),
                                 "hit_from_pool": h.get("from_pool", False)}
            except Exception:
                pass  # pool opcional

        return {"step": "get_session", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"ja4_profile": getattr(session, "_ja4_profile", "?"),
                         "browser_pool": pool_data}}
    except ImportError:
        return {"step": "get_session", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": "tls-client no instalado"}
    except Exception as e:
        return {"step": "get_session", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_inject_acpat(ctx: dict) -> dict:
    """Paso 4: inyecta token AcpAT.

    Estrategia:
      1. PRIMERO: <code>queue_manager.claim_slot_for_purchase</code> — si hay una
         sesión `accepted` para ese event_id, reusa su <code>accepted_token</code>
         + <code>queueit_session_xt</code>. Esto es el path "swarm" óptimo.
      2. FALLBACK: <code>cookie_warmer.inject_acpat</code> (pool genérico).
    """
    started = time.time()
    tm_id = (ctx.get("event") or {}).get("tm_id") or ctx.get("event_id", "")
    try:
        import queue_manager
        r = queue_manager.claim_slot_for_purchase(
            event_id=str(tm_id), country=ctx.get("country", "us"))
        if r.get("ok"):
            ctx["acpat_token"] = r.get("accepted_token")
            ctx["queueit_session_xt"] = r.get("queueit_session_xt", "")
            ctx["acpat_source"] = r.get("from") or "queue_manager"
            return {"step": "inject_acpat", "ok": True,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "data": {"source": ctx["acpat_source"],
                             "session_id": r.get("session_id")}}
    except Exception as e:
        ctx["queue_manager_err"] = f"{type(e).__name__}: {str(e)[:80]}"
    try:
        import cookie_warmer
        r = cookie_warmer.inject_acpat(domain_id=ctx.get("country", "us"),
                                         event_id=ctx.get("event_id", ""))
        if r.get("ok"):
            ctx["acpat_token"] = r.get("token")
            ctx["acpat_used_count"] = r.get("entry", {}).get("used_count", 0)
            ctx["acpat_source"] = "cookie_warmer_pool"
        return {"step": "inject_acpat", "ok": r.get("ok", False),
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"source": "cookie_warmer_pool",
                         "used_count": r.get("entry", {}).get("used_count")} if r.get("ok") else None,
                "error": None if r.get("ok") else r.get("error", "sin tokens disponibles")}
    except Exception as e:
        return {"step": "inject_acpat", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_find_event(ctx: dict) -> dict:
    """Paso 5: verifica que el evento existe y obtiene info."""
    started = time.time()
    try:
        import event_manager
        evs = event_manager.list_events()
        e = next((x for x in evs if x.get("id") == ctx.get("event_id")), None)
        if not e:
            return {"step": "find_event", "ok": False,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "error": f"evento {ctx.get('event_id')} no encontrado"}
        ctx["event"] = e
        return {"step": "find_event", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"name": e.get("name"), "venue": e.get("venue"),
                         "sale_date": e.get("sale_date")}}
    except Exception as e:
        return {"step": "find_event", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_check_onsale_window(ctx: dict) -> dict:
    """Paso 6: valida que el evento está en ventana de venta.

    En modo simulate NO bloquea (útil para testing).
    En modo live rechaza si sale_date > 60s en el futuro.
    """
    started = time.time()
    ev = ctx.get("event", {})
    sale = ev.get("sale_date")
    mode = ctx.get("mode", "simulate")
    if not sale:
        return {"step": "check_onsale_window", "ok": mode == "simulate",
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"sale_status": "unknown", "mode": mode},
                "error": None if mode == "simulate" else "sale_date no definida"}
    try:
        sale_dt = datetime.fromisoformat(sale)
        now = datetime.now()
        diff = (sale_dt - now).total_seconds()
        if mode == "simulate":
            # simulate: siempre OK pero reporta el estado real
            return {"step": "check_onsale_window", "ok": True,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "data": {"sale_status": "live" if diff <= 0 else "future",
                             "opens_in_s": max(0, int(diff)), "mode": "simulate"}}
        if diff > 60:
            return {"step": "check_onsale_window", "ok": False,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "error": f"sale aún no abierta (en {int(diff)}s)",
                    "data": {"opens_in_s": int(diff)}}
        return {"step": "check_onsale_window", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"sale_status": "live" if diff <= 0 else "opening_soon"}}
    except Exception as e:
        return {"step": "check_onsale_window", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_try_presale_code(ctx: dict) -> dict:
    """Paso 6.5: si hay presale codes para este evento, prueba el mejor por tier.

    Llama a presale_farming.try_codes_for_event con un tester real (live mode)
    o simulado. Si encuentra un code válido lo persiste en ctx['presale_code']
    para que step_simulate_cart lo use. Skipea silenciosamente si no hay codes.
    """
    started = time.time()
    try:
        import presale_farming
    except ImportError:
        return {"step": "try_presale_code", "ok": True, "skipped": True,
                "reason": "module_not_available",
                "elapsed_ms": int((time.time()-started)*1000)}

    event_id = ctx.get("event_id") or ""
    artist = ctx.get("artist") or ""
    if not event_id:
        return {"step": "try_presale_code", "ok": True, "skipped": True,
                "reason": "no_event_id",
                "elapsed_ms": int((time.time()-started)*1000)}

    ranked = presale_farming.codes_for_event_ranked(event_id, artist=artist)
    if not ranked:
        return {"step": "try_presale_code", "ok": True, "skipped": True,
                "reason": "no_codes_in_db",
                "elapsed_ms": int((time.time()-started)*1000)}

    # Tester: en live delega a tm_browser_engine, en simulate marca el primero válido
    if ctx.get("mode") == "live":
        try:
            import tm_browser_engine
            page = ctx.get("page")
            if not page or not hasattr(tm_browser_engine, "try_presale_code"):
                # Fallback: marca el primero como tentativo, dejar al bot real probarlo
                top = ranked[0]
                ctx["presale_code"] = top["code"]
                ctx["presale_code_id"] = top["id"]
                ctx["presale_codes_pool"] = [{"code": c["code"], "id": c["id"], "tier": c.get("_tier")} for c in ranked[:5]]
                return {"step": "try_presale_code", "ok": True,
                        "data": {"code": top["code"], "tier": top.get("_tier"),
                                  "pool_size": len(ranked)},
                        "note": "browser_tester_unavailable_passing_pool",
                        "elapsed_ms": int((time.time()-started)*1000)}
            res = presale_farming.try_codes_for_event(
                event_id=event_id, artist=artist,
                tester_callback=lambda code: tm_browser_engine.try_presale_code(page, code),
                max_tries=int(ctx.get("presale_max_tries") or 5))
            if res.get("ok"):
                ctx["presale_code"] = res["valid_code"]
                ctx["presale_code_id"] = res["code_id"]
            return {"step": "try_presale_code",
                    "ok": res.get("ok", False),
                    "data": res,
                    "elapsed_ms": int((time.time()-started)*1000)}
        except Exception as e:
            return {"step": "try_presale_code", "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "retriable": False,
                    "elapsed_ms": int((time.time()-started)*1000)}

    # Modo simulate: pasamos el code top sin probar
    top = ranked[0]
    ctx["presale_code"] = top["code"]
    ctx["presale_code_id"] = top["id"]
    return {"step": "try_presale_code", "ok": True, "simulated": True,
            "data": {"code": top["code"], "tier": top.get("_tier"),
                      "pool_size": len(ranked)},
            "elapsed_ms": int((time.time()-started)*1000)}


def step_simulate_cart(ctx: dict) -> dict:
    """Paso 7: añadir al carrito.

    En `mode='live'` delega a `tm_real_purchase.run_real_purchase` que arranca
    `_compra_parallel.py` (browser real, login, sectores, captcha, cart, checkout).
    El wrapper cubre los pasos 7+8+9 en una sola llamada y deja `ctx['order_id']`
    si compra OK; los pasos posteriores leen ese flag para no duplicar.

    En `mode='simulate'` mantiene el random para test del orquestador sin browser.
    """
    started = time.time()
    if ctx.get("mode") == "live":
        try:
            import tm_real_purchase
            r = tm_real_purchase.run_real_purchase(ctx)
            if r.get("ok") and ctx.get("order_id"):
                ctx["_real_purchase_done"] = True
            return {"step": "simulate_cart", **{k: v for k, v in r.items() if k != "step"}}
        except Exception as e:
            return {"step": "simulate_cart", "ok": False,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "error": f"{type(e).__name__}: {e}",
                    "error_type": "real_wrapper_fail", "retriable": False}

    time.sleep(random.uniform(0.3, 0.9))
    roll = random.random()
    if roll < 0.60:
        ctx["cart_id"] = f"cart_{random.randint(10000, 99999)}"
        return {"step": "simulate_cart", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"cart_id": ctx["cart_id"], "seats": ctx.get("quantity", 2)}}
    elif roll < 0.80:
        return {"step": "simulate_cart", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": "captcha requerido", "error_type": "captcha",
                "retriable": True}
    elif roll < 0.92:
        return {"step": "simulate_cart", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": "sold out", "error_type": "sold_out"}
    else:
        return {"step": "simulate_cart", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": "queue-it 429", "error_type": "queue", "retriable": True}


def step_solve_captcha(ctx: dict) -> dict:
    """Paso 8: resuelve CAPTCHA si el anterior lo requirió.

    Guard: si `_real_purchase_done` (paso 7 ya hizo todo via _compra_parallel),
    devuelve no-op OK — el captcha real ya lo gestionó el wrapper.
    """
    started = time.time()
    if ctx.get("_real_purchase_done"):
        return {"step": "solve_captcha", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"via": "real_purchase_fwd",
                         "note": "captcha gestionado por _compra_parallel"}}
    try:
        import captcha_manager
        best = captcha_manager.get_best_method()
        if best == "manual":
            return {"step": "solve_captcha", "ok": False,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "error": "ningún solver configurado"}
        # En simulate sólo comprobamos conectividad
        if ctx.get("mode", "simulate") == "simulate":
            bal = captcha_manager.check_balance(best)
            return {"step": "solve_captcha", "ok": bal.get("ok", False),
                    "elapsed_ms": int((time.time()-started)*1000),
                    "data": {"service": best, "balance": bal.get("balance")},
                    "error": bal.get("error")}
        # LIVE: sitekey y URL del evento
        sitekey = ctx.get("captcha_sitekey")
        url = (ctx.get("event") or {}).get("tm_url", "")
        if not sitekey or not url:
            return {"step": "solve_captcha", "ok": False,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "error": "falta sitekey o url para solver"}
        captcha_type = ctx.get("captcha_type", "recaptcha_v2")
        # Turnstile: si hay page Playwright en ctx, delegar al solver
        # especializado (incluye solve+inject+callback fire). Si no, fallback
        # al genérico que sólo devuelve token (otra capa lo inyectará).
        if captcha_type == "turnstile":
            page = ctx.get("page")
            if page is not None:
                try:
                    import turnstile_solver
                    ts_r = turnstile_solver.solve_and_inject(
                        page, url=url, max_wait_render_s=8)
                    if ts_r.get("ok"):
                        ctx["captcha_token"] = ts_r.get("token")
                        return {"step": "solve_captcha", "ok": True,
                                "elapsed_ms": int((time.time()-started)*1000),
                                "data": {"service": ts_r.get("service"),
                                          "via": "turnstile_solver",
                                          "fired_callback": ts_r.get("fired_callback"),
                                          "elapsed_solve_s": ts_r.get("elapsed_solve_s")}}
                    # Fallback al genérico si solve_and_inject falló
                except ImportError:
                    pass
            r = captcha_manager.solve_turnstile(sitekey, url,
                                                    event_id=ctx.get("event_id"))
        elif captcha_type == "hcaptcha":
            r = captcha_manager.solve_hcaptcha(sitekey, url,
                                                  event_id=ctx.get("event_id"))
        elif captcha_type == "recaptcha_v3":
            r = captcha_manager.solve_recaptcha_v3(sitekey, url,
                                                      event_id=ctx.get("event_id"))
        else:
            r = captcha_manager.solve_recaptcha_v2(sitekey, url,
                                                      event_id=ctx.get("event_id"))
        if r.get("ok"):
            ctx["captcha_token"] = r.get("token")
        return {"step": "solve_captcha", "ok": r.get("ok", False),
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"service": r.get("service"), "has_token": bool(r.get("token"))},
                "error": r.get("error")}
    except Exception as e:
        return {"step": "solve_captcha", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_pick_card(ctx: dict) -> dict:
    """Paso 8b: elige tarjeta disponible (rotación + cooldowns)."""
    started = time.time()
    try:
        import card_rotation
        account_id = ctx.get("account_id", "")
        card = card_rotation.pick_card(account_id=account_id)
        if not card:
            # si no hay tarjetas registradas, en modo simulate seguimos; en live aborta
            if ctx.get("mode", "simulate") == "simulate":
                return {"step": "pick_card", "ok": True,
                        "elapsed_ms": int((time.time()-started)*1000),
                        "data": {"skipped": "no_cards_registered_simulate_mode"}}
            return {"step": "pick_card", "ok": False,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "error": "sin tarjetas disponibles (todas en cooldown)"}
        ctx["card"] = card
        return {"step": "pick_card", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"card_id": card.get("id"),
                         "brand": card.get("brand"),
                         "masked": card.get("masked")}}
    except Exception as e:
        return {"step": "pick_card", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_tokenize_card(ctx: dict) -> dict:
    """Paso 8c: pre-tokeniza la tarjeta con Adyen para checkout < 2s."""
    started = time.time()
    try:
        import adyen_tokenizer
        card = ctx.get("card") or {}
        if not card.get("number"):
            # simulate-safe: sin PAN, generamos fake token
            return {"step": "tokenize_card", "ok": True,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "data": {"skipped": "no_card_number (simulate)"}}
        r = adyen_tokenizer.tokenize_card(card_data={
            "number": card.get("number"),
            "expiry_month": card.get("expiry_month", card.get("exp_month", "")),
            "expiry_year":  card.get("expiry_year",  card.get("exp_year", "")),
            "cvc":          card.get("cvc", ""),
            "holder_name":  card.get("holder", card.get("holder_name", "")),
        }, shopper_reference=ctx.get("account_id") or f"flow-{ctx.get('flow_id','?')}")
        if r.get("ok"):
            ctx["adyen_token"] = r.get("token_id")
        return {"step": "tokenize_card", "ok": bool(r.get("ok")),
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"token_id": r.get("token_id"), "mode": r.get("mode")},
                "error": r.get("error")}
    except Exception as e:
        return {"step": "tokenize_card", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_await_3ds_otp(ctx: dict) -> dict:
    """Paso 9b: espera y autorellena código 3DS desde IMAP/SMS."""
    started = time.time()
    try:
        import otp_3ds
        cfg = otp_3ds.get_config()
        if not cfg.get("enabled"):
            return {"step": "await_3ds_otp", "ok": False,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "data": {"skipped": "otp_3ds no habilitado"},
                    "error": "autofill 3DS deshabilitado — código manual requerido"}
        # otp_3ds expone wait_for_otp (no wait_for_code) — el segundo es legacy
        if hasattr(otp_3ds, "wait_for_otp"):
            r = otp_3ds.wait_for_otp(timeout=60)
        elif hasattr(otp_3ds, "wait_for_code"):
            r = otp_3ds.wait_for_code(timeout=60)
        else:
            r = otp_3ds.check_inbox()
        code = r.get("code") if isinstance(r, dict) else None
        if code:
            ctx["otp_3ds_code"] = code
        return {"step": "await_3ds_otp", "ok": bool(code),
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"code_len": len(code) if code else 0,
                         "sender": (r or {}).get("sender", "?")} if code else None,
                "error": None if code else "no se recibió código 3DS"}
    except Exception as e:
        return {"step": "await_3ds_otp", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_simulate_checkout(ctx: dict) -> dict:
    """Paso 9: checkout final.

    Si `_real_purchase_done` está marcado (paso 7 ya hizo la compra real vía
    `tm_real_purchase`), este step se vuelve no-op y reporta éxito con el
    order_id ya generado. En `mode='simulate'` simula random.
    """
    started = time.time()
    if ctx.get("_real_purchase_done") and ctx.get("order_id"):
        return {"step": "simulate_checkout", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"order_id": ctx["order_id"], "via": "real_purchase_fwd"}}
    if ctx.get("mode") == "live":
        return {"step": "simulate_checkout", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": "no real_purchase_done — paso 7 falló o no ejecutó",
                "error_type": "missing_real_purchase"}

    time.sleep(random.uniform(0.8, 2.0))
    roll = random.random()
    if roll < 0.70:
        order_id = f"ORD-{random.randint(10000000, 99999999)}"
        ctx["order_id"] = order_id
        return {"step": "simulate_checkout", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"order_id": order_id, "amount": (ctx.get("event") or {}).get("max_price", 100)}}
    elif roll < 0.85:
        return {"step": "simulate_checkout", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": "pago rechazado (3DS)", "error_type": "3ds"}
    else:
        return {"step": "simulate_checkout", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": "carrito expirado", "error_type": "cart_timeout"}


def step_record_card_usage(ctx: dict, outcome: str) -> dict:
    """Paso 9c: registra el uso de la tarjeta en card_rotation (aplica cooldown automático)."""
    started = time.time()
    try:
        import card_rotation
        card = ctx.get("card") or {}
        if not card.get("id"):
            return {"step": "record_card_usage", "ok": True,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "data": {"skipped": "no_card"}}
        entry = card_rotation.record_usage(
            card_id=card["id"], outcome=outcome,
            account_id=ctx.get("account_id", ""),
            purchase_id=ctx.get("order_id", ""),
            details=f"flow={ctx.get('flow_id')}",
        )
        return {"step": "record_card_usage", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"outcome": outcome, "entry_id": entry.get("id")}}
    except Exception as e:
        return {"step": "record_card_usage", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_register_purchase(ctx: dict) -> dict:
    """Paso 10: registra la compra en purchases_manager si hubo éxito.
    Propaga image_url, tm_url, venue, city, event_date y otros campos del
    evento para que la vista 'Mis Entradas' muestre toda la info rica.
    """
    started = time.time()
    try:
        import purchases_manager
        event = ctx.get("event") or {}
        purchase = {
            "event_id": event.get("id"),
            "event_name": event.get("name"),
            "venue": event.get("venue", ""),
            "city": event.get("city", ""),
            "country": event.get("country", ""),
            "domain_id": event.get("country"),
            "event_date": event.get("date", ""),
            "order_id": ctx.get("order_id"),
            "confirmation_number": ctx.get("confirmation_number", ""),
            "cart_id": ctx.get("cart_id"),
            "quantity": ctx.get("quantity", event.get("quantity", 2)),
            "unit_price": ctx.get("unit_price", event.get("max_price", 0)),
            "fees": ctx.get("fees", 0),
            "currency": event.get("currency", "EUR"),
            "seat_category": ctx.get("seat_category", "Reserved"),
            "section": ctx.get("section", ""),
            "row": ctx.get("row", ""),
            "seats": ctx.get("seats", []),
            "payment_method": ctx.get("payment_method", ""),
            "delivery_method": ctx.get("delivery_method", "mobile"),
            "account_id": ctx.get("account_id", ""),
            "account_email": ctx.get("account_email", ""),
            "browser_engine": ctx.get("browser", ""),
            "proxy_used": ctx.get("proxy", ""),
            "mode": ctx.get("mode", "simulate"),
            "flow_id": ctx.get("flow_id"),
            "checkout_ms": int((time.time() - ctx.get("_start_ts", time.time())) * 1000),
            # IMAGEN del evento — lo que muestra 'Mis Entradas'
            "image_url": event.get("image_url", ""),
            "tm_url": event.get("tm_url", ""),
            "tags": ["bot"] + (["live"] if ctx.get("mode") == "live" else ["simulate"]),
        }
        p = purchases_manager.add_purchase(purchase)
        return {"step": "register_purchase", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"purchase_id": p.get("id") if isinstance(p, dict) else None,
                         "has_image": bool(event.get("image_url"))}}
    except Exception as e:
        return {"step": "register_purchase", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


def step_notify(ctx: dict) -> dict:
    """Paso 11: webhook Discord/Telegram del resultado."""
    started = time.time()
    try:
        import notifications
        ev = ctx.get("event") or {}
        r = notifications.notify(
            event_type="purchase_success",
            title=f"✅ Compra confirmada: {ev.get('name','(evento)')}",
            message=(f"Evento: {ev.get('name')}\n"
                     f"Venue: {ev.get('venue')}\n"
                     f"Order: {ctx.get('order_id')}\n"
                     f"Amount: {ev.get('max_price')}\n"
                     f"Mode: {ctx.get('mode', 'simulate')}"),
            level="success",
        )
        return {"step": "notify", "ok": True,
                "elapsed_ms": int((time.time()-started)*1000),
                "data": {"sent_to": (r or {}).get("sent_to", [])}}
    except Exception as e:
        return {"step": "notify", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════════
def step_dfa_compliance(ctx: dict) -> dict:
    """Step legal: registra consent del usuario para (account, event) y valida
    que la operación no infringe DFA (Digital Fairness Act EU Q3 2026).

    - Si el evento está en EU: registra consent inmutable en hash chain.
    - Si hay listing_value > face_value y país EU: bloquea por violación legal.
    - Si país no-EU: pasa de largo (no aplica).
    """
    started = time.time()
    country = (ctx.get("country") or "").lower()
    account_id = ctx.get("account_id") or ctx.get("account") or "anonymous"
    event_id = ctx.get("event_id") or ""

    # log consent (append-only con hash chain)
    consent_res = compliance_gate.log_consent(
        account_id=account_id, event_id=event_id, country=country,
        meta={"flow_id": ctx.get("flow_id"), "mode": ctx.get("mode"),
              "quantity": ctx.get("quantity")},
    )

    # face-value check con auto-fill desde event_offer_scanner / ticket_selector
    ev = ctx.get("event") or {}
    face = float(ev.get("face_value") or ev.get("price_min") or 0)
    listing = float(ctx.get("listing_value") or ev.get("price_max") or 0)

    # Auto-detect: si no hay listing en ctx, intenta extraer del scanner / selector
    if not listing or not face:
        try:
            import event_offer_scanner as _eos
            for fn_name in ("get_offers_for", "scan_event", "get_event_offers"):
                fn = getattr(_eos, fn_name, None)
                if callable(fn):
                    try:
                        offers = fn(ctx.get("event_id")) or {}
                    except TypeError:
                        offers = fn(event_id=ctx.get("event_id")) or {}
                    if isinstance(offers, dict):
                        face = face or float(offers.get("face_value") or offers.get("price_min") or 0)
                        listing = listing or float(offers.get("listing_value") or offers.get("price_max") or 0)
                    break
        except Exception:
            pass
    if not listing and face:
        listing = face  # fallback: si solo conocemos face, asumimos venta a face

    resale = {"ok": True, "regulated": compliance_gate.is_eu_country(country)}
    if face > 0 and listing > 0:
        resale = compliance_gate.validate_resale(country, face, listing)

    elapsed_ms = int((time.time() - started) * 1000)
    if resale.get("violation"):
        return {
            "step": "dfa_compliance", "ok": False,
            "elapsed_ms": elapsed_ms,
            "error": f"DFA violation: listing {listing} > {resale.get('max_allowed')} ({country.upper()})",
            "data": {"consent_hash": consent_res.get("entry_hash"),
                     "resale": resale},
        }
    return {
        "step": "dfa_compliance", "ok": True,
        "elapsed_ms": elapsed_ms,
        "data": {
            "consent_hash": consent_res.get("entry_hash"),
            "is_eu": consent_res.get("is_eu"),
            "resale": resale,
        },
    }


def run_flow(event_id: str, quantity: int = 2, mode: str = "simulate",
             strategy: str = "best_available", broadcast: Optional[Callable] = None,
             ja4_profile: Optional[str] = None) -> dict:
    """Ejecuta el flujo completo de compra para un evento."""
    # Validación de entrada: mode + quantity + strategy (whitelist)
    if mode not in ("simulate", "live"):
        mode = "simulate"  # default seguro ante valores extraños
    try:
        quantity = max(1, min(int(quantity), 8))
    except (TypeError, ValueError):
        quantity = 2
    if strategy not in STRATEGIES:
        strategy = "best_available"
    flow_id = f"flow_{int(time.time())}_{random.randint(1000, 9999)}"
    ctx = {
        "flow_id": flow_id,
        "event_id": event_id,
        "quantity": quantity,
        "mode": mode,
        "strategy": strategy,
        "ja4_profile": ja4_profile,
    }
    started_all = time.time()
    result = {"flow_id": flow_id, "ok": False, "mode": mode, "steps": [],
              "event_id": event_id, "started_at": datetime.now().isoformat()}

    # 1. find_event PRIMERO para obtener country
    r = step_find_event(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast:
        broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})
    if not r["ok"]:
        result["error"] = r.get("error")
        result["elapsed_ms"] = int((time.time()-started_all)*1000)
        _report_problem(flow_id, "Bot de compra", "find_event falló", r.get("error"))
        return result
    ctx["country"] = ctx["event"].get("country", "us")

    # 1b. DFA Compliance Gate (EU Q3 2026) — registra consent + verifica face-value
    r = step_dfa_compliance(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})
    if not r["ok"]:
        # En EU sin consent o resale > face value: abortar legalmente
        result["error"] = r.get("error")
        result["elapsed_ms"] = int((time.time()-started_all)*1000)
        _report_problem(flow_id, "DFA Compliance",
                        "Bloqueo legal EU DFA", r.get("error"))
        return result

    # 2. pick_proxy (opcional — sigue sin proxy si no hay)
    r = step_pick_proxy(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 2b. pin_proxy_session (misma IP warm→queue→checkout)
    r = step_pin_proxy_session(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 3. warm_session
    r = step_warm_session(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 4. get_session (JA4+)
    r = step_get_session(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 5. inject_acpat (opcional)
    r = step_inject_acpat(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 6. check_onsale_window
    r = step_check_onsale_window(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})
    if not r["ok"]:
        result["error"] = r.get("error")
        result["elapsed_ms"] = int((time.time()-started_all)*1000)
        return result

    # 6b. try_presale_code (best-effort, no aborta si falla)
    r = step_try_presale_code(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 7. simulate_cart (en live: real POST a cart)
    r = step_simulate_cart(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 8. solve_captcha si lo pide
    if not r["ok"] and r.get("error_type") == "captcha":
        cap = step_solve_captcha(ctx)
        result["steps"].append(cap)
        _append_log(flow_id, cap)
        if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": cap})
        if cap["ok"]:
            # Reintento cart
            r = step_simulate_cart(ctx)
            result["steps"].append(r)
            _append_log(flow_id, r)
            if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    if not r["ok"]:
        result["error"] = r.get("error")
        result["elapsed_ms"] = int((time.time()-started_all)*1000)
        _report_problem(flow_id, "Bot de compra", "cart falló", r.get("error"))
        return result

    # 8b. pick_card (rotación multi-tarjeta con cooldowns)
    r = step_pick_card(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})
    if not r["ok"] and ctx.get("mode") != "simulate":
        result["error"] = r.get("error")
        result["elapsed_ms"] = int((time.time()-started_all)*1000)
        _report_problem(flow_id, "Bot de compra", "sin tarjetas disponibles", r.get("error"))
        return result

    # 8c. tokenize_card (Adyen pre-checkout)
    r = step_tokenize_card(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 9. simulate_checkout
    r = step_simulate_checkout(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 9b. Si falla con 3DS → await_3ds_otp + retry checkout
    if not r["ok"] and r.get("error_type") == "3ds":
        otp = step_await_3ds_otp(ctx)
        result["steps"].append(otp)
        _append_log(flow_id, otp)
        if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": otp})
        if otp["ok"]:
            r = step_simulate_checkout(ctx)
            result["steps"].append(r)
            _append_log(flow_id, r)
            if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    if not r["ok"]:
        # registra outcome de la tarjeta según el tipo de fallo
        err_type = r.get("error_type", "declined")
        out_map = {"3ds": "3ds_failed", "declined": "declined",
                   "cart_timeout": "timeout"}
        rec = step_record_card_usage(ctx, out_map.get(err_type, "declined"))
        result["steps"].append(rec)
        _append_log(flow_id, rec)
        result["error"] = r.get("error")
        result["elapsed_ms"] = int((time.time()-started_all)*1000)
        _report_problem(flow_id, "Bot de compra", "checkout falló", r.get("error"))
        return result

    # 9c. checkout OK → registra éxito en card_rotation (cooldown 24h)
    rec = step_record_card_usage(ctx, "success")
    result["steps"].append(rec)
    _append_log(flow_id, rec)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": rec})

    # 10. register_purchase
    r = step_register_purchase(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # 11. notify
    r = step_notify(ctx)
    result["steps"].append(r)
    _append_log(flow_id, r)
    if broadcast: broadcast("purchase_flow_step", {"flow_id": flow_id, "step": r})

    # Release browser context si se adquirió del pool
    try:
        hid = ctx.get("browser_handle_id")
        if hid:
            import browser_pool
            browser_pool.release(hid)
    except Exception:
        pass

    # Release del pin de proxy (libera para otros flujos)
    try:
        sid = ctx.get("pin_session_id")
        if sid:
            import proxy_session_pin
            proxy_session_pin.release(sid)
    except Exception:
        pass

    result["ok"] = True
    result["order_id"] = ctx.get("order_id")
    result["elapsed_ms"] = int((time.time()-started_all)*1000)
    if broadcast:
        broadcast("purchase_flow_done", {"flow_id": flow_id, "ok": True,
                                          "order_id": ctx.get("order_id")})
    return result


def _report_problem(flow_id: str, category: str, title: str, message: str):
    """Reporta fallo al problem_center."""
    try:
        import problem_center
        problem_center.report_problem(
            category=category, title=title,
            message=f"{message} (flow_id: {flow_id})",
            severity="high",
            meta={"flow_id": flow_id},
        )
    except Exception:
        pass


def run_many(event_id: str, count: int = 3, mode: str = "simulate",
             quantity: int = 2, strategy: str = "best_available",
             broadcast: Optional[Callable] = None) -> dict:
    """Ejecuta N flujos en paralelo (distintos workers)."""
    # Validación de entrada
    try:
        count = max(1, min(int(count), 10))
    except (TypeError, ValueError):
        count = 3
    results = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=min(count, 10)) as ex:
        futures = [ex.submit(run_flow, event_id, quantity, mode, strategy, broadcast)
                   for _ in range(count)]
        for fut in as_completed(futures):
            results.append(fut.result())
    ok = sum(1 for r in results if r.get("ok"))
    return {
        "ok": True,
        "total": count,
        "success": ok,
        "failed": count - ok,
        "success_rate": round(ok / count * 100, 1) if count else 0,
        "elapsed_s": round(time.time() - started, 1),
        "results": results,
    }


def get_strategies() -> List[dict]:
    return [{"id": k, "name": v} for k, v in STRATEGIES.items()]
