"""hyper_sdk_chain.py — Cadena multi-vendor Akamai → Kasada → DataDome.

Wrapper que orquesta llamadas al `hyper_sdk_client.py` ya existente con
cadena de fallback. Si el primer vendor falla (red, 5xx, timeout, sensor
inválido), prueba el siguiente. Esto maximiza la tasa de éxito cuando
el target está usando MULTIPLE protections (e.g., TM en checkout usa
Akamai en el front y DataDome en pagos).

Política de cadena:
  - Default order: ['akamai', 'kasada', 'datadome']
  - Si vendor=N → intenta N primero, luego los demás
  - Backoff exponencial 0.5s → 1s → 2s entre intentos
  - Stats por vendor: success_count, fail_count, avg_latency_ms

Modos:
  simulate (default) — retorna mocks deterministas de hyper_sdk_client
  live — llama al cliente real (requiere api_key configurada)

API:
  get_config() / save_config()
  solve_with_chain(target='akamai', payload_data={...}) → dict
  get_chain_stats()
  set_chain_order(['akamai','kasada','datadome'])
  get_chain_order()
  reset_stats()
  test_chain_simulate(target='akamai')
  self_test()
"""
from __future__ import annotations
import os, time, json, threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

try:
    import hyper_sdk_client
    HYPER_AVAILABLE = True
except Exception as e:
    HYPER_AVAILABLE = False
    HYPER_IMPORT_ERROR = str(e)


_CONFIG_KEY = "hyper_sdk_chain_config.json"
_STATS_KEY = "hyper_sdk_chain_stats.json"
_LOG_KEY = "hyper_sdk_chain_log.json"
_lock = threading.RLock()

_VALID_TARGETS = ("akamai", "kasada", "datadome")

_stats: Dict[str, Dict[str, Any]] = {
    "akamai":   {"success": 0, "fail": 0, "total_ms": 0.0, "calls": 0},
    "kasada":   {"success": 0, "fail": 0, "total_ms": 0.0, "calls": 0},
    "datadome": {"success": 0, "fail": 0, "total_ms": 0.0, "calls": 0},
}


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "mode": "simulate",                    # simulate | live
        "chain_order": ["akamai", "kasada", "datadome"],
        "max_attempts_per_vendor": 1,
        "backoff_s": [0.5, 1.0, 2.0],
        "halt_on_first_success": True,
        "fail_on_all_vendors_fail": True,
        "log_each_call": True,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def _log(event: Dict[str, Any]) -> None:
    try:
        event["ts"] = datetime.now().isoformat()
        with _lock:
            d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
            d["log"].insert(0, event)
            d["log"] = d["log"][:200]
            json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


def _save_stats() -> None:
    try:
        with _lock:
            json_atomic.save(config.ruta(_STATS_KEY), {
                **_stats, "updated_at": datetime.now().isoformat()
            })
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# CHAIN ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════════════
def set_chain_order(order: List[str]) -> Dict[str, Any]:
    """Define el orden de la cadena. Solo valid targets aceptados."""
    if not isinstance(order, list) or not order:
        return {"ok": False, "error": "order must be non-empty list"}
    cleaned = [v.lower() for v in order if v.lower() in _VALID_TARGETS]
    if not cleaned:
        return {"ok": False, "error": "no valid targets in order",
                "valid": list(_VALID_TARGETS)}
    save_config({"chain_order": cleaned})
    return {"ok": True, "chain_order": cleaned, "removed_invalid": len(order) - len(cleaned)}


def get_chain_order() -> List[str]:
    return list(get_config().get("chain_order", ["akamai", "kasada", "datadome"]))


# ═══════════════════════════════════════════════════════════════
# VENDOR DISPATCH (mode-aware)
# ═══════════════════════════════════════════════════════════════
def _call_vendor(vendor: str, payload: Dict[str, Any],
                  mode: str) -> Tuple[bool, Dict[str, Any]]:
    """Llama al cliente real (live) o devuelve mock (simulate). Retorna (ok, dict)."""
    payload = payload or {}

    if mode == "simulate":
        # Mock determinista usando el patrón del cliente real
        if vendor == "akamai":
            r = {"ok": True, "mode": "simulate", "vendor": "akamai",
                 "sensor_data": "3;680;abc123def456;mock_payload_b64;sig_xyz;dyn_token",
                 "sensor_length": 64,
                 "cookies": {"_abck": "0~mock~-1~-1~-1",
                             "bm_sz": "mock_bmsz_value"},
                 "user_agent": payload.get("user_agent", "Mozilla/5.0 (mock)")}
            return True, r
        if vendor == "kasada":
            r = {"ok": True, "mode": "simulate", "vendor": "kasada",
                 "x_kpsdk_ct": "kpsdk_ct_" + "a" * 32,
                 "x_kpsdk_cd": "kpsdk_cd_" + "b" * 16}
            return True, r
        if vendor == "datadome":
            r = {"ok": True, "mode": "simulate", "vendor": "datadome",
                 "datadome_cookie": "datadome=" + "c" * 64,
                 "payload": "d" * 128}
            return True, r
        return False, {"ok": False, "error": f"unknown vendor: {vendor}"}

    # LIVE
    if not HYPER_AVAILABLE:
        return False, {"ok": False, "error": "hyper_sdk_client not importable",
                       "import_error": HYPER_IMPORT_ERROR}
    try:
        if vendor == "akamai":
            r = hyper_sdk_client.generate_akamai_sensor(
                abck=payload.get("abck", ""), bmsz=payload.get("bmsz", ""),
                user_agent=payload.get("user_agent", ""),
                page_url=payload.get("page_url", ""))
            return r.get("ok", False), r
        if vendor == "kasada":
            r = hyper_sdk_client.generate_kasada_payload(
                url=payload.get("page_url", ""))
            return r.get("ok", False), r
        if vendor == "datadome":
            r = hyper_sdk_client.generate_datadome(
                target_url=payload.get("page_url", ""),
                cookie=payload.get("cookie", ""),
                user_agent=payload.get("user_agent", ""))
            return r.get("ok", False), r
    except Exception as e:
        return False, {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return False, {"ok": False, "error": "vendor_not_dispatched"}


def _record_stat(vendor: str, success: bool, elapsed_ms: float) -> None:
    with _lock:
        if vendor not in _stats:
            _stats[vendor] = {"success": 0, "fail": 0, "total_ms": 0.0, "calls": 0}
        _stats[vendor]["calls"] += 1
        _stats[vendor]["total_ms"] += elapsed_ms
        if success:
            _stats[vendor]["success"] += 1
        else:
            _stats[vendor]["fail"] += 1
    _save_stats()


# ═══════════════════════════════════════════════════════════════
# CHAIN SOLVER
# ═══════════════════════════════════════════════════════════════
def solve_with_chain(target: str = "akamai",
                      payload_data: Optional[Dict[str, Any]] = None,
                      override_order: Optional[List[str]] = None,
                      mode_override: Optional[str] = None) -> Dict[str, Any]:
    """Ejecuta la cadena: intenta `target` primero, luego los demás del config.

    payload_data: dict con campos específicos (page_url, user_agent, abck, ...).
    override_order: si pasado, ignora config.chain_order.
    mode_override: 'simulate' o 'live' para forzar (override config.mode).
    """
    cfg = get_config()
    mode = mode_override or cfg.get("mode", "simulate")
    target = (target or "akamai").lower()
    if target not in _VALID_TARGETS:
        return {"ok": False, "error": f"invalid target: {target}",
                "valid": list(_VALID_TARGETS)}

    # Construir orden: target primero, luego el resto del config (sin duplicar)
    base_order = override_order if override_order else cfg.get("chain_order", list(_VALID_TARGETS))
    chain = [target]
    for v in base_order:
        if v not in chain and v in _VALID_TARGETS:
            chain.append(v)

    backoffs = cfg.get("backoff_s", [0.5, 1.0, 2.0])
    halt_on_success = cfg.get("halt_on_first_success", True)

    attempts: List[Dict[str, Any]] = []
    final_result: Optional[Dict[str, Any]] = None
    success_vendor: Optional[str] = None

    for idx, vendor in enumerate(chain):
        t0 = time.time()
        ok, resp = _call_vendor(vendor, payload_data or {}, mode)
        elapsed_ms = (time.time() - t0) * 1000
        _record_stat(vendor, ok, elapsed_ms)

        attempt = {
            "attempt_index": idx,
            "vendor": vendor,
            "ok": ok,
            "elapsed_ms": round(elapsed_ms, 1),
            "mode": mode,
            "error": resp.get("error") if not ok else None,
        }
        attempts.append(attempt)

        if cfg.get("log_each_call"):
            _log({"op": "chain_attempt", **attempt})

        if ok:
            final_result = resp
            success_vendor = vendor
            if halt_on_success:
                break
        else:
            # Backoff antes de siguiente vendor
            if idx + 1 < len(chain):
                wait = backoffs[min(idx, len(backoffs) - 1)]
                # Respetar wait pero acotar a 0.05s en simulate (tests)
                if mode == "simulate":
                    wait = min(wait, 0.01)
                time.sleep(wait)

    if final_result is None:
        # Todos fallaron
        return {
            "ok": False,
            "error": "all_vendors_failed" if cfg.get("fail_on_all_vendors_fail") else None,
            "attempts": attempts,
            "vendors_tried": [a["vendor"] for a in attempts],
            "chain_order_used": chain,
            "mode": mode,
        }

    return {
        "ok": True,
        "vendor_succeeded": success_vendor,
        "result": final_result,
        "attempts": attempts,
        "vendors_tried": [a["vendor"] for a in attempts],
        "chain_order_used": chain,
        "mode": mode,
    }


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════
def get_chain_stats() -> Dict[str, Any]:
    with _lock:
        out: Dict[str, Any] = {}
        for vendor, s in _stats.items():
            avg = (s["total_ms"] / s["calls"]) if s["calls"] > 0 else 0
            success_rate = (s["success"] / s["calls"] * 100) if s["calls"] > 0 else 0
            out[vendor] = {
                **s,
                "avg_latency_ms": round(avg, 1),
                "success_rate_pct": round(success_rate, 1),
            }
        total_calls = sum(s["calls"] for s in _stats.values())
        total_success = sum(s["success"] for s in _stats.values())
        return {
            "ok": True,
            "by_vendor": out,
            "total_calls": total_calls,
            "total_success": total_success,
            "total_success_rate_pct": round(
                (total_success / total_calls * 100) if total_calls > 0 else 0, 1),
            "current_chain_order": get_chain_order(),
            "current_mode": get_config().get("mode"),
            "hyper_sdk_client_available": HYPER_AVAILABLE,
        }


def reset_stats() -> Dict[str, Any]:
    """Resetea contadores (para tests o nuevo período)."""
    with _lock:
        for vendor in _stats:
            _stats[vendor] = {"success": 0, "fail": 0, "total_ms": 0.0, "calls": 0}
    _save_stats()
    return {"ok": True, "reset_at": datetime.now().isoformat()}


def test_chain_simulate(target: str = "akamai") -> Dict[str, Any]:
    """Helper: ejecuta la cadena en modo simulate (no toca live)."""
    return solve_with_chain(
        target=target,
        payload_data={"page_url": "https://www.ticketmaster.com/test",
                       "user_agent": "Mozilla/5.0 test"},
        mode_override="simulate",
    )


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}

    # Asegurar simulate mode + reset stats limpio
    save_config({"mode": "simulate"})
    reset_stats()

    # 1. Config
    cfg = get_config()
    checks["config_mode_simulate"] = cfg["mode"] == "simulate"
    checks["config_has_chain_order"] = "chain_order" in cfg
    checks["config_chain_default_3"] = len(cfg["chain_order"]) == 3

    # 2. Chain order get/set
    r = set_chain_order(["datadome", "akamai", "kasada"])
    checks["set_order_ok"] = r.get("ok") is True
    checks["set_order_persists"] = get_chain_order() == ["datadome", "akamai", "kasada"]

    # Set order inválido
    r = set_chain_order(["nonsense_vendor"])
    checks["set_order_rejects_invalid"] = r.get("ok") is False

    # Restaurar default
    set_chain_order(["akamai", "kasada", "datadome"])

    # 3. solve_with_chain target=akamai
    r = solve_with_chain(target="akamai",
                          payload_data={"page_url": "https://test.com",
                                        "user_agent": "Mozilla/5.0"})
    checks["solve_akamai_ok"] = r.get("ok") is True
    checks["solve_akamai_succeeded_vendor"] = r.get("vendor_succeeded") == "akamai"
    checks["solve_akamai_has_attempts"] = len(r.get("attempts", [])) >= 1
    checks["solve_akamai_first_attempt_ok"] = r["attempts"][0]["ok"] is True
    checks["solve_akamai_result_has_sensor"] = (
        "sensor_data" in (r.get("result") or {})
    )

    # 4. solve target=kasada
    r = solve_with_chain(target="kasada", payload_data={"page_url": "https://k.com"})
    checks["solve_kasada_ok"] = r.get("ok") is True
    checks["solve_kasada_first_vendor_kasada"] = (
        r["attempts"][0]["vendor"] == "kasada"
    )
    checks["solve_kasada_result_has_kpsdk"] = (
        "x_kpsdk_ct" in (r.get("result") or {})
    )

    # 5. solve target=datadome
    r = solve_with_chain(target="datadome", payload_data={"page_url": "https://d.com"})
    checks["solve_datadome_ok"] = r.get("ok") is True
    checks["solve_datadome_result_has_cookie"] = (
        "datadome_cookie" in (r.get("result") or {})
    )

    # 6. invalid target
    r = solve_with_chain(target="nonsense")
    checks["invalid_target_fails"] = r.get("ok") is False

    # 7. override_order
    r = solve_with_chain(target="akamai",
                          override_order=["kasada", "datadome"],
                          payload_data={"page_url": "https://o.com"})
    # chain construido: [akamai] + ["kasada","datadome"] → 3 vendors si todos fallaran
    checks["override_chain_starts_with_target"] = (
        r["chain_order_used"][0] == "akamai"
    )

    # 8. stats updated
    s = get_chain_stats()
    checks["stats_total_calls_ge_4"] = s["total_calls"] >= 4
    checks["stats_total_success_ge_4"] = s["total_success"] >= 4
    checks["stats_akamai_calls_ge_1"] = s["by_vendor"]["akamai"]["calls"] >= 1

    # 9. simulate mode never goes to live
    checks["mode_remained_simulate"] = get_config()["mode"] == "simulate"

    # 10. test_chain_simulate helper
    r = test_chain_simulate("akamai")
    checks["test_chain_simulate_ok"] = r.get("ok") is True
    checks["test_chain_simulate_mode"] = r.get("mode") == "simulate"

    # 11. reset stats
    reset_stats()
    s = get_chain_stats()
    checks["reset_clears_calls"] = s["total_calls"] == 0

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "hyper_sdk_chain",
        "version": 1,
        "purpose": "Multi-vendor fallback chain wrapping hyper_sdk_client",
        "valid_targets": list(_VALID_TARGETS),
        "current_chain_order": get_chain_order(),
        "current_mode": get_config().get("mode"),
        "hyper_sdk_client_available": HYPER_AVAILABLE,
        "stats": get_chain_stats(),
    }
