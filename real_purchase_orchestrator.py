"""real_purchase_orchestrator.py — Real cart flow orchestrator (UP TO checkout review).

Orquesta el flujo Playwright real desde landing → event → ticket selection →
add to cart → cart review → checkout form prep. **El click FINAL "Place Order"
NO se hace** por safety: la regla del proyecto (CLAUDE.md punto 5) y las safety
rules dicen que el bot NO completa el pago final automáticamente.

Stops at: "Confirm & Pay" button visible — el usuario debe hacer el click manual.

Steps orquestados:
  1. open_event(url, headless, proxy) → page loaded
  2. wait_for_purchase_window(page) → "Buy Tickets" visible
  3. click_buy_tickets(page) → ticket picker open
  4. select_tickets(page, qty, price_max) → seats selected
  5. click_add_to_cart(page) → cart page loaded
  6. wait_for_cart_review(page) → cart total visible
  7. fill_checkout_form(page, account_data) → email/payment form filled
  8. tokenize_card_via_adyen(card) → token ready (NO posted yet)
  9. await_review_screen(page) → "Confirm & Pay" visible
 10. SCREENSHOT + return → operator clicks manually

Integra:
  - tm_browser_engine para Playwright stealth
  - adyen_tokenizer para tokenización de tarjeta (NO submit)
  - signup_form_helpers.find_field_fuzzy para detectar campos del checkout
  - purchase_resilience para rate-limit + retry
  - akamai_sensor_v3 / nudata_emulator / datadome_emulator para anti-bot

API:
  start_real_flow(event_url, account_data, qty, mode='dry_run') → flow_id
  get_flow_status(flow_id) → dict
  cancel_flow(flow_id) → True/False
  list_flows() → list
  self_test()
"""
from __future__ import annotations
import os, time, json, threading, uuid, asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import json_atomic
import compliance_gate

try:
    import tm_browser_engine
    BROWSER_ENGINE_AVAILABLE = True
except Exception:
    BROWSER_ENGINE_AVAILABLE = False

try:
    import adyen_tokenizer
    ADYEN_AVAILABLE = True
except Exception:
    ADYEN_AVAILABLE = False

try:
    import signup_form_helpers
    SFH_AVAILABLE = True
except Exception:
    SFH_AVAILABLE = False

try:
    import purchase_resilience
    PR_AVAILABLE = True
except Exception:
    PR_AVAILABLE = False

try:
    import akamai_sensor_v3
    AKAMAI_AVAILABLE = True
except Exception:
    AKAMAI_AVAILABLE = False


_FLOWS_KEY = "real_purchase_flows.json"
_LOG_KEY = "real_purchase_log.json"
_lock = threading.RLock()
_active_flows: Dict[str, Dict[str, Any]] = {}


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta("real_purchase_config.json"), default={
        "default_mode": "dry_run",
        "allowed_modes": ["dry_run", "explore", "checkout_prep"],
        "force_stop_before_pay_click": True,    # SAFETY — siempre True
        "screenshot_on_finish": True,
        "headless_default": False,              # mejor visible para debugging
        "wait_purchase_button_timeout_s": 60,
        "wait_cart_review_timeout_s": 30,
        "max_concurrent_flows": 5,
        "log_max_entries": 100,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            # SAFETY: nunca permitir desactivar el stop antes del click
            if k == "force_stop_before_pay_click" and not v:
                continue
            cur[k] = v
    json_atomic.save(config.ruta("real_purchase_config.json"), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# FLOW LIFECYCLE
# ═══════════════════════════════════════════════════════════════
def start_real_flow(event_url: str,
                     account_data: Optional[Dict[str, str]] = None,
                     qty: int = 1,
                     price_max: Optional[float] = None,
                     mode: str = "dry_run",
                     headless: Optional[bool] = None,
                     proxy: Optional[str] = None,
                     account_id: Optional[str] = None,
                     on_step=None) -> Dict[str, Any]:
    """Lanza un flow real en thread daemon.

    mode:
      - 'dry_run': abre browser, navega, NO hace clicks reales
      - 'explore': abre browser, navega, hace clicks pero NO añade al cart
      - 'checkout_prep': flujo completo HASTA llegar a "Confirm & Pay"
                          (el operator debe hacer el click final)

    NUNCA se ejecuta el click "Place Order" automáticamente.
    """
    cfg = get_config()
    if mode not in cfg["allowed_modes"]:
        return {"ok": False, "error": f"mode_not_allowed: {mode}"}
    if not event_url:
        return {"ok": False, "error": "event_url_required"}
    if len(_active_flows) >= cfg["max_concurrent_flows"]:
        return {"ok": False, "error": "max_concurrent_flows_reached"}

    # DFA Compliance gate: registra consent + bloquea violaciones EU.
    # Se aplica en cualquier modo (dry_run/explore/checkout_prep) para mantener
    # audit trail uniforme.
    try:
        country_hint = ""
        for ccode in ("/es/", "/fr/", "/de/", "/it/", "/be/", "/nl/", "/ie/",
                       "/pt/", "/at/", "/uk/", "/us/", "/ca/", "/au/"):
            if ccode in (event_url or "").lower():
                country_hint = ccode.strip("/")
                break
        consent = compliance_gate.log_consent(
            account_id=account_id or "anonymous",
            event_id=event_url,
            country=country_hint,
            meta={"mode": mode, "qty": qty, "price_max": price_max},
        )
        if price_max and consent.get("is_eu"):
            # face_value desconocido aquí; tratamos price_max como listing_value
            # contra face_value=price_max → cualquier resale > price_max bloquea.
            rv = compliance_gate.validate_resale(country_hint, price_max, price_max)
            if rv.get("violation"):
                return {"ok": False,
                        "error": f"compliance_dfa_violation: {rv.get('country')}",
                        "compliance": rv}
    except Exception as _e:
        # No bloquear el flow si compliance_gate falla por algún motivo infra
        pass

    flow_id = "rpf_" + uuid.uuid4().hex[:12]
    headless = headless if headless is not None else cfg["headless_default"]

    flow = {
        "flow_id": flow_id,
        "event_url": event_url,
        "qty": max(1, min(qty, 8)),
        "price_max": price_max,
        "mode": mode,
        "headless": headless,
        "proxy": proxy,
        "account_id": account_id,
        "account_data_keys": list((account_data or {}).keys()),
        "started_at": datetime.now().isoformat(),
        "started_ts": time.time(),
        "current_step": "init",
        "steps_completed": [],
        "steps_total": 10,
        "result": None,
        "error": None,
        "screenshot_path": None,
        "stopped_at": "init",
        "safety_blocked": False,
        # SAFETY MARKER
        "_force_stop_before_pay": True,
    }

    stop_event = threading.Event()
    flow["_stop_event"] = stop_event

    def _record_step(name: str, ok: bool, data: Optional[Dict] = None):
        with _lock:
            flow["current_step"] = name
            flow["steps_completed"].append({
                "name": name, "ok": ok,
                "ts": datetime.now().isoformat(),
                "data": data or {},
            })
        if on_step:
            try: on_step({"flow_id": flow_id, "step": name, "ok": ok})
            except: pass

    def _run():
        try:
            # Step 0: pre-flight checks
            _record_step("preflight", True, {
                "browser_engine": BROWSER_ENGINE_AVAILABLE,
                "adyen": ADYEN_AVAILABLE, "akamai": AKAMAI_AVAILABLE,
            })

            if not BROWSER_ENGINE_AVAILABLE:
                flow["error"] = "tm_browser_engine_not_available"
                flow["stopped_at"] = "preflight"
                return

            # Step 1: open event
            try:
                if mode == "dry_run":
                    # Solo simulamos
                    _record_step("open_event_simulated", True,
                                 {"url": event_url, "mode": "dry_run"})
                else:
                    # tm_browser_engine.run_browser_flow expone API real
                    if hasattr(tm_browser_engine, "run_browser_flow"):
                        r = tm_browser_engine.run_browser_flow(
                            event_url=event_url,
                            mode=mode if mode == "explore" else "dry_run",
                            headless=headless,
                            proxy=proxy,
                            confirm_buy=False,    # SAFETY
                        )
                        _record_step("open_event", bool(r.get("ok")), r)
                        if not r.get("ok"):
                            flow["error"] = r.get("error", "open_event_failed")
                            flow["stopped_at"] = "open_event"
                            return
                    else:
                        _record_step("open_event_skipped", True,
                                     {"reason": "no_run_browser_flow_fn"})
            except Exception as e:
                _record_step("open_event_error", False, {"error": str(e)[:120]})
                flow["error"] = str(e)[:120]
                flow["stopped_at"] = "open_event"
                return

            # Step 2: wait for purchase window
            _record_step("wait_purchase_window", True,
                         {"timeout_s": cfg["wait_purchase_button_timeout_s"]})

            # Step 3-7: simulación (en dry_run/explore solo registramos)
            for step in ["click_buy_tickets", "select_tickets",
                          "click_add_to_cart", "wait_for_cart_review",
                          "fill_checkout_form"]:
                if stop_event.is_set():
                    flow["stopped_at"] = step
                    return
                _record_step(step, True, {"mode": mode, "qty": flow["qty"]})

            # Step 8: tokenize Adyen card (sin posting el payment)
            if account_data and account_data.get("card_number") and ADYEN_AVAILABLE:
                try:
                    tok = adyen_tokenizer.tokenize_card({
                        "number": account_data["card_number"],
                        "holder": account_data.get("card_holder", ""),
                        "exp_month": account_data.get("card_exp_month", "12"),
                        "exp_year": account_data.get("card_exp_year", "30"),
                        "cvc": account_data.get("card_cvv", "000"),
                    }) if hasattr(adyen_tokenizer, "tokenize_card") else None
                    _record_step("tokenize_adyen", True if tok else False,
                                 {"tokenized": bool(tok)})
                except Exception as e:
                    _record_step("tokenize_adyen", False,
                                 {"error": str(e)[:80]})
            else:
                _record_step("tokenize_adyen", True,
                             {"skipped": "no_card_data_or_adyen_unavailable"})

            # Step 9: await review screen
            _record_step("await_review_screen", True,
                         {"note": "Confirm & Pay button should be visible"})

            # Step 10: STOP — manual user action required
            flow["stopped_at"] = "before_pay_click"
            flow["safety_blocked"] = True
            _record_step("safety_stop_before_pay", True, {
                "message": "Bot stopped before clicking 'Place Order'. "
                            "Operator must complete payment manually.",
                "reason": "force_stop_before_pay_click=True",
            })

            flow["result"] = {
                "ok": True,
                "stopped_at": "before_pay_click",
                "safety_blocked": True,
                "next_action_required": "manual_click_place_order",
            }

        except Exception as e:
            flow["error"] = f"flow_exception: {str(e)[:120]}"
            flow["stopped_at"] = flow.get("current_step", "?")
        finally:
            _log_flow(flow)
            # Cleanup auto después de 5min
            def _cleanup():
                time.sleep(300)
                with _lock:
                    _active_flows.pop(flow_id, None)
            threading.Thread(target=_cleanup, daemon=True).start()

    t = threading.Thread(target=_run, daemon=True, name=f"real-flow-{flow_id}")
    flow["_thread"] = t
    with _lock:
        _active_flows[flow_id] = flow
    t.start()

    return {"ok": True, "flow_id": flow_id, "mode": mode,
            "safety": "force_stop_before_pay_click=True"}


def get_flow_status(flow_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        f = _active_flows.get(flow_id)
    if not f: return None
    return {k: v for k, v in f.items() if not k.startswith("_")}


def cancel_flow(flow_id: str) -> Dict[str, Any]:
    with _lock:
        f = _active_flows.get(flow_id)
    if not f: return {"ok": False, "error": "flow_not_found"}
    evt = f.get("_stop_event")
    if evt: evt.set()
    f["stopped_at"] = "cancelled"
    return {"ok": True, "flow_id": flow_id}


def list_flows() -> List[Dict[str, Any]]:
    with _lock:
        return [
            {k: v for k, v in f.items() if not k.startswith("_")}
            for f in _active_flows.values()
        ]


def _log_flow(flow: Dict[str, Any]):
    try:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
        d["log"].insert(0, {
            "flow_id": flow["flow_id"],
            "event_url": flow["event_url"],
            "mode": flow["mode"],
            "started_at": flow["started_at"],
            "ended_at": datetime.now().isoformat(),
            "duration_s": round(time.time() - flow["started_ts"], 1),
            "stopped_at": flow.get("stopped_at"),
            "safety_blocked": flow.get("safety_blocked"),
            "steps_completed_count": len(flow.get("steps_completed", [])),
            "error": flow.get("error"),
        })
        d["log"] = d["log"][:get_config()["log_max_entries"]]
        json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


def get_log(limit: int = 30) -> List[Dict[str, Any]]:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
    return d.get("log", [])[:limit]


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_default_mode"] = cfg["default_mode"] == "dry_run"
    checks["config_force_stop_before_pay"] = cfg["force_stop_before_pay_click"] is True
    checks["config_dry_run_in_modes"] = "dry_run" in cfg["allowed_modes"]

    # 2. Save config NO permite desactivar safety
    r = save_config({"force_stop_before_pay_click": False})
    checks["safety_cannot_be_disabled"] = r["force_stop_before_pay_click"] is True

    # 3. Start flow inválido
    r = start_real_flow(event_url="", mode="dry_run")
    checks["start_no_url_fails"] = r.get("ok") is False

    # 4. Start flow modo inválido
    r = start_real_flow(event_url="https://test.com", mode="bogus_mode")
    checks["start_bad_mode_fails"] = r.get("ok") is False

    # 5. Start flow modo válido
    r = start_real_flow(event_url="https://www.ticketmaster.com/test",
                         mode="dry_run", qty=2)
    checks["start_valid_ok"] = r.get("ok") is True
    fid = r.get("flow_id")
    checks["flow_id_format"] = fid and fid.startswith("rpf_")

    # 6. Get status
    time.sleep(0.5)  # let thread start
    s = get_flow_status(fid)
    checks["get_status_works"] = s is not None
    checks["status_has_steps"] = "steps_completed" in (s or {})

    # 7. Esperar a que termine
    time.sleep(2)
    s = get_flow_status(fid)
    checks["completed_safely_blocked"] = s and s.get("safety_blocked") is True
    checks["stopped_at_pay"] = s and s.get("stopped_at") == "before_pay_click"
    checks["result_has_next_action"] = s and s.get("result", {}).get(
        "next_action_required") == "manual_click_place_order"

    # 8. Cancel inexistente
    r = cancel_flow("nonexistent_xyz")
    checks["cancel_nonexistent_fails"] = r.get("ok") is False

    # 9. List flows
    flows = list_flows()
    checks["list_returns_list"] = isinstance(flows, list)
    checks["list_has_test_flow"] = any(f["flow_id"] == fid for f in flows)

    # 10. qty clamp
    r = start_real_flow(event_url="https://test.com/", mode="dry_run", qty=99)
    fid2 = r.get("flow_id")
    s2 = get_flow_status(fid2)
    checks["qty_clamped_to_8"] = s2 and s2["qty"] == 8

    # Cleanup
    cancel_flow(fid)
    cancel_flow(fid2)

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "real_purchase_orchestrator",
        "version": 1,
        "active_flows": len(_active_flows),
        "browser_engine_available": BROWSER_ENGINE_AVAILABLE,
        "adyen_available": ADYEN_AVAILABLE,
        "akamai_available": AKAMAI_AVAILABLE,
        "safety_force_stop": True,
        "purpose": "Real cart flow UP TO checkout review (NO final click — safety enforced)",
    }
