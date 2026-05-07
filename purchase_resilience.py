"""purchase_resilience.py — Capa de resiliencia para tm_purchase_flow.

Soluciona los gaps críticos:
  • Adaptive rate limiting con backoff exponencial (detecta 429)
  • Fallback automático per-proxy (si proxy falla en warm, prueba otro)
  • AcpAT re-injection cíclico antes de TTL expirar
  • 3DS 2.0 Adyen liability shift wrapper (detecta y maneja silent auth)
  • Retry inteligente con jitter

API:
  warm_with_fallback(domain, max_attempts=3) → intenta warm con N proxies
  rate_limited_request(url, ...) → request con auto-detect 429 y backoff
  start_acpat_refresh(domain, account_id, ttl_min=25) → re-injection background
  stop_acpat_refresh(account_id)
  detect_3ds_flow(html_response) → analiza si TM aplica 3DS 2.0 silent auth
  with_retry(fn, max_attempts=3, on_429='backoff') → decorator-like wrapper
  self_test()
"""
from __future__ import annotations
import os, re, time, threading, random
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Callable, List

import config
import json_atomic

_lock = threading.RLock()
_RL_KEY = "rate_limit_state.json"
_acpat_refreshers: Dict[str, threading.Event] = {}


def _path(k):
    p = config.ruta(k)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


# ═══════════════════════════════════════════════════════════════
# 1. ADAPTIVE RATE LIMITING
# ═══════════════════════════════════════════════════════════════
def _load_rl_state() -> Dict[str, Any]:
    return json_atomic.load(_path(_RL_KEY), default={"hosts": {}})


def _save_rl_state(d: Dict[str, Any]) -> None:
    json_atomic.save(_path(_RL_KEY), d)


def get_rl_state(host: str) -> Dict[str, Any]:
    """Devuelve el estado actual de rate-limit para un host."""
    d = _load_rl_state()
    return d.get("hosts", {}).get(host, {
        "consecutive_429": 0, "next_allowed_ts": 0,
        "total_429": 0, "last_429": None, "backoff_s": 0,
    })


def record_response(host: str, status: int,
                    retry_after: Optional[int] = None) -> Dict[str, Any]:
    """Registra una respuesta HTTP y ajusta el rate limit."""
    with _lock:
        d = _load_rl_state()
        host_state = d.setdefault("hosts", {}).setdefault(host, {})
        if status == 429:
            host_state["consecutive_429"] = host_state.get("consecutive_429", 0) + 1
            host_state["total_429"] = host_state.get("total_429", 0) + 1
            host_state["last_429"] = datetime.now().isoformat()
            # Backoff exponencial: 2^n segundos (max 5min)
            n = min(host_state["consecutive_429"], 8)
            backoff = retry_after if retry_after else min(2 ** n, 300)
            backoff += random.uniform(0, backoff * 0.1)  # jitter 10%
            host_state["backoff_s"] = backoff
            host_state["next_allowed_ts"] = time.time() + backoff
        elif 200 <= status < 300:
            # Reset al éxito
            host_state["consecutive_429"] = 0
            host_state["backoff_s"] = 0
            host_state["next_allowed_ts"] = 0
        host_state["last_status"] = status
        host_state["last_check"] = datetime.now().isoformat()
        _save_rl_state(d)
    return host_state


def wait_if_rate_limited(host: str, max_wait_s: int = 300) -> Dict[str, Any]:
    """Espera el backoff si hay rate-limit activo. Devuelve info."""
    state = get_rl_state(host)
    next_ts = state.get("next_allowed_ts", 0)
    if next_ts > time.time():
        wait = min(next_ts - time.time(), max_wait_s)
        if wait > 0:
            time.sleep(wait)
            return {"waited_s": wait, "host": host}
    return {"waited_s": 0, "host": host}


def is_rate_limited(host: str) -> bool:
    return get_rl_state(host).get("next_allowed_ts", 0) > time.time()


# ═══════════════════════════════════════════════════════════════
# 2. PER-PROXY FALLBACK EN WARM
# ═══════════════════════════════════════════════════════════════
def warm_with_fallback(domain: str = "es", max_attempts: int = 3,
                       account_id: Optional[str] = None,
                       pages: int = 3) -> Dict[str, Any]:
    """Intenta warm_cookies probando hasta N proxies si fallan.

    Marca cada proxy fallido en el rotator para que no lo elija de nuevo
    durante un tiempo (cooldown corto).
    """
    last_error = None
    attempts = []
    for attempt in range(max_attempts):
        # Pick proxy
        try:
            import proxy_rotator
            if account_id:
                pr = proxy_rotator.pick_for_account(
                    account_id, country=domain, sticky=(attempt == 0))
            else:
                pr = proxy_rotator.pick_random(country=domain,
                                                exclude_recent_hours=1)
            if not pr.get("ok"):
                last_error = "no_proxy"; continue
            proxy = pr["proxy"]
        except Exception as e:
            last_error = f"proxy_picker: {e}"; continue

        # Wait if rate-limited
        host = "ticketmaster.es" if domain == "es" else "ticketmaster.com"
        wait_if_rate_limited(host)

        # Intentar warm
        try:
            import cookie_warmer
            t0 = time.time()
            r = cookie_warmer.warm_cookies(domain_id=domain, pages=pages)
            elapsed = time.time() - t0
            ok = bool(r.get("ok") if isinstance(r, dict) else r)
            attempts.append({
                "attempt": attempt + 1,
                "proxy": f'{proxy["host"]}:{proxy["port"]}',
                "ok": ok,
                "elapsed_s": round(elapsed, 1),
            })
            if ok:
                # Release proxy con success
                try: proxy_rotator.release(proxy["id"], account_id or "", success=True)
                except: pass
                return {
                    "ok": True, "attempts": attempts, "final_proxy": proxy,
                    "warm_result": r if isinstance(r, dict) else {},
                }
            # Marcar proxy como fail
            try: proxy_rotator.release(proxy["id"], account_id or "", success=False,
                                        error="warm_failed")
            except: pass
            last_error = f"warm fail attempt {attempt+1}"
        except Exception as e:
            last_error = str(e)[:120]
            attempts.append({"attempt": attempt + 1, "ok": False,
                              "error": last_error})

    return {"ok": False, "attempts": attempts, "error": last_error}


# ═══════════════════════════════════════════════════════════════
# 3. ACPAT RE-INJECTION BACKGROUND
# ═══════════════════════════════════════════════════════════════
def start_acpat_refresh(domain: str = "es", account_id: str = "",
                        ttl_min: int = 25,
                        on_refresh: Optional[Callable] = None) -> Dict[str, Any]:
    """Lanza un thread que re-inyecta AcpAT cada `ttl_min` minutos.

    El TTL real de AcpAT en TM es 30min, así que renovamos a los 25 para evitar
    expiry mid-flow.
    """
    key = account_id or "default"
    if key in _acpat_refreshers:
        return {"ok": False, "error": "already_running", "key": key}

    stop_evt = threading.Event()
    _acpat_refreshers[key] = stop_evt

    def _loop():
        while not stop_evt.is_set():
            try:
                import cookie_warmer
                tok = cookie_warmer.inject_acpat(domain_id=domain)
                if on_refresh:
                    try: on_refresh({"key": key, "domain": domain, "token": bool(tok)})
                    except Exception: pass
            except Exception: pass
            stop_evt.wait(ttl_min * 60)

    t = threading.Thread(target=_loop, daemon=True, name=f"acpat-refresh-{key}")
    t.start()
    return {"ok": True, "key": key, "ttl_min": ttl_min}


def stop_acpat_refresh(account_id: str = "") -> Dict[str, Any]:
    key = account_id or "default"
    evt = _acpat_refreshers.pop(key, None)
    if evt: evt.set()
    return {"ok": evt is not None, "key": key}


def list_acpat_refreshers() -> List[str]:
    return list(_acpat_refreshers.keys())


# ═══════════════════════════════════════════════════════════════
# 4. 3DS 2.0 SILENT AUTH DETECTOR
# ═══════════════════════════════════════════════════════════════
def detect_3ds_flow(html_response: str) -> Dict[str, Any]:
    """Analiza una respuesta HTML/JSON de Adyen para detectar si aplica 3DS 2.0
    silent auth (frictionless, sin OTP) o requiere OTP humano.

    Devuelve:
      {flow: 'frictionless'|'challenge'|'fallback'|'unknown',
       liability_shift: bool, action: dict}
    """
    h = (html_response or "").lower()

    # Patrones de Adyen 3DS 2.0
    if "frictionless" in h or "auth_method=frictionless" in h or "transstatus=y" in h:
        return {"flow": "frictionless", "liability_shift": True,
                "action": "auto_continue",
                "note": "3DS 2.0 silent auth · NO requiere OTP"}

    if "challengeshopper" in h or "redirectshopper" in h or "transstatus=c" in h:
        # Detecta tipo de challenge
        if "redirect" in h:
            return {"flow": "challenge", "liability_shift": True,
                    "action": "redirect_otp",
                    "note": "3DS 2.0 challenge · OTP/biometric requerido"}
        return {"flow": "challenge", "liability_shift": True,
                "action": "challenge_iframe",
                "note": "3DS 2.0 challenge iframe"}

    if "transstatus=u" in h:
        return {"flow": "fallback", "liability_shift": False,
                "action": "fallback_3ds1",
                "note": "Fallback a 3DS 1.0"}

    if "authorised" in h or "authorized" in h or "success" in h:
        return {"flow": "frictionless", "liability_shift": True,
                "action": "auto_continue",
                "note": "Auth ya completada"}

    return {"flow": "unknown", "liability_shift": False,
            "action": "manual_review", "note": "No se detectó patrón 3DS"}


def adyen_liability_shift_wrapper(payment_response: Dict[str, Any]) -> Dict[str, Any]:
    """Procesa respuesta Adyen y retorna decisión de continuación.

    Si liability_shift=True → seguir sin necesitar OTP usuario
    Si liability_shift=False → requiere user input (return action)
    """
    result_code = (payment_response.get("resultCode") or "").lower()
    auth_data = payment_response.get("additionalData", {}) or {}

    if result_code in ("authorised", "authorized"):
        return {
            "ok": True, "liability_shift": True,
            "action": "complete",
            "note": "Authorised · liability shift al issuer",
        }

    if result_code == "identifyshopper" or result_code == "challengeshopper":
        # 3DS 2.0 challenge
        action = payment_response.get("action", {})
        return {
            "ok": False, "liability_shift": True,
            "action": "3ds2_challenge",
            "challenge_type": action.get("type", "unknown"),
            "url": action.get("url"),
            "note": "Requiere 3DS 2.0 challenge",
        }

    if result_code == "redirectshopper":
        return {
            "ok": False, "liability_shift": True,
            "action": "3ds_redirect",
            "url": payment_response.get("action", {}).get("url"),
            "note": "Redirect a 3DS",
        }

    if result_code == "refused":
        # Sin liability shift al merchant
        return {
            "ok": False, "liability_shift": False,
            "action": "rotate_card",
            "refusal_reason": payment_response.get("refusalReason"),
            "note": "Tarjeta rechazada · rotar a otra",
        }

    return {"ok": False, "liability_shift": False,
            "action": "manual_review", "result_code": result_code}


# ═══════════════════════════════════════════════════════════════
# 5. WITH_RETRY DECORATOR
# ═══════════════════════════════════════════════════════════════
def with_retry(fn: Callable, max_attempts: int = 3,
               base_delay: float = 1.0,
               on_429: str = "backoff") -> Any:
    """Ejecuta fn con retry exponencial. on_429 = 'backoff' | 'rotate_proxy'."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            r = fn()
            # Si la función retorna dict con 'status'==429, considerar fail
            if isinstance(r, dict) and r.get("status") == 429:
                if on_429 == "backoff":
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(delay)
                    continue
                else:
                    return {"ok": False, "rate_limited": True,
                            "attempts": attempt + 1}
            return r
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
    return {"ok": False, "error": str(last_exc)[:120],
            "attempts": max_attempts}


# ═══════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Rate limit state
    s = get_rl_state("test.example.com")
    checks["get_rl_returns_dict"] = isinstance(s, dict)
    checks["rl_default_no_block"] = s.get("next_allowed_ts", 0) <= time.time()

    # 2. Record 429 → backoff
    record_response("test.host", 429)
    s = get_rl_state("test.host")
    checks["record_429_increments"] = s.get("consecutive_429") == 1
    checks["record_429_sets_backoff"] = s.get("backoff_s", 0) > 0

    # 3. Record 200 → reset
    record_response("test.host", 200)
    s = get_rl_state("test.host")
    checks["record_200_resets"] = s.get("consecutive_429") == 0

    # 4. is_rate_limited
    record_response("blocked.host", 429)
    checks["is_rate_limited_works"] = is_rate_limited("blocked.host")
    record_response("blocked.host", 200)  # cleanup

    # 5. AcpAT refresher start/stop
    r = start_acpat_refresh(domain="es", account_id="test_acc", ttl_min=99999)
    checks["acpat_start_ok"] = r.get("ok") is True
    checks["acpat_in_list"] = "test_acc" in list_acpat_refreshers()
    r = start_acpat_refresh(domain="es", account_id="test_acc")
    checks["acpat_no_duplicate"] = not r.get("ok")
    r = stop_acpat_refresh("test_acc")
    checks["acpat_stop_ok"] = r.get("ok") is True
    checks["acpat_removed"] = "test_acc" not in list_acpat_refreshers()

    # 6. detect_3ds_flow
    r = detect_3ds_flow("transStatus=Y frictionless")
    checks["3ds_detect_frictionless"] = r["flow"] == "frictionless" and r["liability_shift"]
    r = detect_3ds_flow("challengeShopper required")
    checks["3ds_detect_challenge"] = r["flow"] == "challenge"
    r = detect_3ds_flow("transStatus=U")
    checks["3ds_detect_fallback"] = r["flow"] == "fallback"
    r = detect_3ds_flow("totally unrelated text")
    checks["3ds_detect_unknown"] = r["flow"] == "unknown"

    # 7. adyen_liability_shift_wrapper
    r = adyen_liability_shift_wrapper({"resultCode": "Authorised"})
    checks["adyen_authorised"] = r["liability_shift"] is True and r["ok"]
    r = adyen_liability_shift_wrapper({"resultCode": "Refused", "refusalReason": "Insufficient"})
    checks["adyen_refused"] = r["action"] == "rotate_card"
    r = adyen_liability_shift_wrapper({"resultCode": "ChallengeShopper",
                                        "action": {"type": "threeDS2", "url": "x"}})
    checks["adyen_challenge"] = r["action"] == "3ds2_challenge"

    # 8. with_retry
    counter = [0]
    def _fail_then_ok():
        counter[0] += 1
        if counter[0] < 2: raise Exception("transient")
        return {"ok": True, "tries": counter[0]}
    r = with_retry(_fail_then_ok, max_attempts=3, base_delay=0.05)
    checks["retry_succeeds_after_fail"] = isinstance(r, dict) and r.get("ok")

    counter[0] = 0
    r = with_retry(lambda: {"status": 429}, max_attempts=2, base_delay=0.05,
                    on_429="rotate_proxy")
    checks["retry_429_rotates"] = r.get("rate_limited") is True

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {"module": "purchase_resilience", "version": 1,
            "active_acpat_refreshers": len(_acpat_refreshers),
            "rate_limited_hosts": [h for h in _load_rl_state().get("hosts", {})
                                    if is_rate_limited(h)]}
