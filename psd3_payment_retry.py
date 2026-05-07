"""psd3_payment_retry.py — PSD3 TRA-aware payment retry policy (2026).

PSD3 (2026) tightens TRA fraud-rate thresholds. Adyen authentication engine
analiza 100+ data points y mass-retry on SCA failure tankea auth rates fast.

Estrategia inteligente:
  - Detect TRA-exemption denial vs hard-fail por refusalReason
  - Backoff exponencial 60-120s (NO instant retry)
  - Switch a fallback card si 2 retries fallan
  - Track TRA denial rate per card → auto-disable si > 20%

API:
  classify_failure(refusal_code, reason)  → tipo de fallo (retry/skip/disable_card)
  next_retry_delay(attempt)               → segundos a esperar
  should_disable_card(card_id)            → True si denial rate > 20%
  record_attempt(card_id, success, refusal_code)
  get_card_health(card_id)
  self_test()
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List

import config
import json_atomic

_CONFIG_KEY = "psd3_retry.json"
_lock = threading.RLock()

# Adyen refusalReason codes mapping (de docs.adyen.com)
TRA_EXEMPTION_DENIAL = {
    "Authentication Required",      # SCA mandatorio
    "RReq",                         # Acquirer requested re-authentication
    "Issuer Suspected Fraud",       # tipo TRA
    "Refused",                       # genérico — depende del context
}

HARD_FAIL_CODES = {
    "Insufficient Funds", "Expired Card", "Invalid Card Number",
    "Stolen Card", "Pickup Card", "Closed Account",
    "Restricted Card", "Withdrawal amount exceeded",
}

RETRY_OK_CODES = {
    "Issuer Unavailable", "Acquirer Error", "Network Error",
    "Timeout", "Try Again Later",
}

# Thresholds
DENIAL_RATE_DISABLE_THRESHOLD = 0.20  # 20%
MIN_ATTEMPTS_FOR_DISABLE = 5


def _load() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "card_history": {},  # {card_id: [{ts, success, refusal_code}]}
        "stats": {"total_retries": 0, "cards_disabled": 0,
                   "tra_denials": 0, "hard_fails": 0},
    })


def _save(d: dict):
    json_atomic.save(config.ruta(_CONFIG_KEY), d)


def classify_failure(refusal_code: str, reason: str = "") -> Dict:
    """Clasifica un fallo de pago y decide la acción."""
    code = (refusal_code or "").strip()
    if not code:
        return {"action": "retry", "type": "unknown", "delay_s": 30}

    # TRA / SCA exemption denial → backoff + mantener card
    if any(t.lower() in code.lower() for t in TRA_EXEMPTION_DENIAL):
        return {"action": "retry_with_sca", "type": "tra_denial",
                "delay_s": 60, "needs_3ds": True,
                "msg": "PSD3 SCA required — usar 3DS challenge en próximo intento"}

    # Hard fail → no retry, disable card
    if any(h.lower() in code.lower() for h in HARD_FAIL_CODES):
        return {"action": "skip_card", "type": "hard_fail",
                "delay_s": 0, "disable_card": True,
                "msg": "Fallo permanente — usar otra tarjeta"}

    # Retry OK (network, etc.)
    if any(r.lower() in code.lower() for r in RETRY_OK_CODES):
        return {"action": "retry", "type": "transient",
                "delay_s": 30, "msg": "Error transitorio"}

    # Default: retry conservador
    return {"action": "retry", "type": "unclassified",
            "delay_s": 90, "msg": f"Refusal desconocido: {code}"}


def next_retry_delay(attempt: int, base_s: int = 60) -> int:
    """Backoff exponencial con jitter. attempt=1 → ~60s, =2 → ~120s, =3 → ~240s."""
    import random
    delay = base_s * (2 ** (attempt - 1))
    jitter = random.uniform(-0.15, 0.15) * delay
    return max(30, min(600, int(delay + jitter)))


def record_attempt(card_id: str, success: bool, refusal_code: str = "") -> Dict:
    """Registra un intento de pago."""
    if not card_id:
        return {"ok": False, "error": "card_id requerido"}
    with _lock:
        d = _load()
        history = d["card_history"].setdefault(card_id, [])
        history.append({
            "ts": datetime.now().isoformat(),
            "success": success,
            "refusal_code": refusal_code,
        })
        # Limita a 100 últimos
        d["card_history"][card_id] = history[-100:]
        # Stats
        if not success:
            classification = classify_failure(refusal_code)
            if classification["type"] == "tra_denial":
                d["stats"]["tra_denials"] = d["stats"].get("tra_denials", 0) + 1
            elif classification["type"] == "hard_fail":
                d["stats"]["hard_fails"] = d["stats"].get("hard_fails", 0) + 1
        _save(d)
        return {"ok": True, "card_id": card_id,
                "history_size": len(history)}


def should_disable_card(card_id: str) -> Dict:
    """Calcula si la tarjeta debe deshabilitarse por denial rate alto."""
    d = _load()
    history = d["card_history"].get(card_id, [])
    if len(history) < MIN_ATTEMPTS_FOR_DISABLE:
        return {"disable": False, "reason": "insufficient_data",
                "attempts": len(history)}
    fails = sum(1 for h in history if not h["success"])
    denial_rate = fails / len(history)
    should = denial_rate >= DENIAL_RATE_DISABLE_THRESHOLD
    return {
        "disable": should,
        "denial_rate": round(denial_rate, 3),
        "threshold": DENIAL_RATE_DISABLE_THRESHOLD,
        "attempts": len(history),
        "failures": fails,
        "reason": f"denial rate {round(denial_rate*100,1)}% >= {int(DENIAL_RATE_DISABLE_THRESHOLD*100)}%"
                  if should else "ok",
    }


def get_card_health(card_id: str) -> Dict:
    """Health check completo de una tarjeta."""
    d = _load()
    history = d["card_history"].get(card_id, [])
    if not history:
        return {"card_id": card_id, "status": "unknown",
                "attempts": 0, "msg": "Sin historial"}
    fails = sum(1 for h in history if not h["success"])
    last_24h = [h for h in history if datetime.fromisoformat(h["ts"]) >
                  datetime.now() - timedelta(hours=24)]
    last_24h_fails = sum(1 for h in last_24h if not h["success"])
    disable = should_disable_card(card_id)
    return {
        "card_id": card_id,
        "status": "disabled" if disable["disable"] else "ok",
        "attempts_total": len(history),
        "attempts_24h": len(last_24h),
        "failures_total": fails,
        "failures_24h": last_24h_fails,
        "denial_rate": disable.get("denial_rate", 0),
        "should_disable": disable["disable"],
        "last_attempt": history[-1] if history else None,
    }


def get_stats() -> Dict:
    d = _load()
    cards = d.get("card_history", {})
    return {
        **d.get("stats", {}),
        "cards_tracked": len(cards),
        "cards_with_failures": sum(1 for h in cards.values()
                                     if any(not a["success"] for a in h)),
    }


def self_test() -> Dict:
    r = {"checks": {}}
    # Classification
    tra = classify_failure("Authentication Required")
    r["checks"]["tra_denial_classified"] = tra["type"] == "tra_denial"
    r["checks"]["tra_needs_3ds"] = tra.get("needs_3ds") is True

    hard = classify_failure("Insufficient Funds")
    r["checks"]["hard_fail_skip_card"] = hard["action"] == "skip_card"

    transient = classify_failure("Issuer Unavailable")
    r["checks"]["transient_retry"] = transient["action"] == "retry"

    # Backoff
    d1 = next_retry_delay(1)
    d2 = next_retry_delay(2)
    d3 = next_retry_delay(3)
    r["checks"]["backoff_grows"] = d1 < d2 < d3
    r["checks"]["backoff_min_30"] = d1 >= 30
    r["checks"]["backoff_max_600"] = d3 <= 600

    # Record attempts
    record_attempt("test_card_1", success=True)
    record_attempt("test_card_1", success=False, refusal_code="Authentication Required")
    record_attempt("test_card_1", success=True)
    h = get_card_health("test_card_1")
    r["checks"]["health_returns_data"] = h["attempts_total"] == 3
    r["checks"]["health_status_ok"] = h["status"] == "ok"  # 33% < 20% threshold... wait
    # 1 fail / 3 = 33% > 20% → debería disable. Pero MIN=5 → no aún.
    r["checks"]["below_min_attempts_no_disable"] = not h["should_disable"]

    # Force disable: 5 attempts, 2 fail = 40%
    for _ in range(2):
        record_attempt("test_card_2", success=True)
    for _ in range(2):
        record_attempt("test_card_2", success=False, refusal_code="Refused")
    record_attempt("test_card_2", success=True)
    disable = should_disable_card("test_card_2")
    r["checks"]["disable_calc_correct"] = disable.get("denial_rate") == 0.4
    r["checks"]["disable_triggers_above_20"] = disable["disable"] is True

    # Stats
    s = get_stats()
    r["checks"]["stats_tracks_cards"] = s.get("cards_tracked", 0) >= 2

    # Cleanup
    with _lock:
        d = _load()
        d["card_history"].pop("test_card_1", None)
        d["card_history"].pop("test_card_2", None)
        _save(d)

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "psd3_payment_retry", "version": 1, "stats": get_stats()}
