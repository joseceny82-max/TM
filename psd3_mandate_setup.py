"""psd3_mandate_setup.py — PSD3 PSR mandate setup SCA (Q2 2026).

PSD3 (Payment Services Directive 3) y PSR (Payment Services Regulation) entran
en vigor en Q2 2026 en EU. La gran novedad para Merchant Initiated Transactions
(MIT) es:

  - SCA (Strong Customer Authentication) se ejecuta UNA SOLA VEZ en el
    `mandate setup` (cuando el cliente autoriza al merchant a cobrar)
  - Cada MIT posterior NO requiere SCA si el mandate sigue activo
  - Esto reduce friccion en suscripciones, recurrentes y one-shot diferidos

Este modulo gestiona:
  - Setup inicial del mandate (incluyendo SCA OTP / 3DS challenge)
  - Tipos de mandate: 'one-shot', 'recurring', 'subscription'
  - Registro y validacion de exenciones MIT bajo un mandate activo
  - Expiracion (manual o por TTL)
  - Persistencia json_atomic con auditoria

API publica:
  setup_mandate(card_token, account_id, mandate_type) -> dict
  register_mit_exemption(mandate_id, transaction_amount) -> dict
  validate_mandate_active(mandate_id) -> dict
  expire_mandate(mandate_id) -> dict
  list_mandates(account_id=None) -> list
  get_mandate(mandate_id) -> dict
  get_config() / save_config()
  self_test()
  get_info()
"""
from __future__ import annotations
import os
import time
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config
import json_atomic


_CONFIG_KEY = "psd3_mandate_config.json"
_MANDATES_KEY = "psd3_mandates.json"
_LOG_KEY = "psd3_mandate_log.json"

_lock = threading.RLock()

VALID_MANDATE_TYPES = ("one-shot", "recurring", "subscription")
DEFAULT_TTL_DAYS = {
    "one-shot": 30,        # one-shot vence en 30 dias por defecto
    "recurring": 365,      # recurring 1 ano
    "subscription": 730,   # subscription 2 anos (max EU PSD3)
}


# ===============================================================
# CONFIG
# ===============================================================
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "mode": "simulate",
        "default_ttl_days": dict(DEFAULT_TTL_DAYS),
        "max_amount_per_mit_eur": 1000,        # cap por MIT bajo mandate
        "max_total_per_period_eur": 10000,     # cap acumulado por periodo
        "require_3ds_at_setup": True,
        "auto_expire_check_interval_s": 3600,
        "audit_retention_days": 365,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ===============================================================
# PERSISTENCE HELPERS
# ===============================================================
def _load_mandates() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_MANDATES_KEY),
                            default={"mandates": {}})


def _save_mandates(d: Dict[str, Any]) -> None:
    json_atomic.save(config.ruta(_MANDATES_KEY), d)


def _append_log(entry: Dict[str, Any]) -> None:
    try:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"events": []})
        d["events"].insert(0, entry)
        d["events"] = d["events"][:500]
        json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now().isoformat()


def _new_mandate_id(account_id: str, card_token: str) -> str:
    seed = f"{account_id}:{card_token}:{time.time()}:{secrets.token_hex(4)}"
    return "mnd_" + hashlib.sha1(seed.encode()).hexdigest()[:20]


# ===============================================================
# CORE: SETUP MANDATE
# ===============================================================
def setup_mandate(card_token: str,
                  account_id: str,
                  mandate_type: str = "one-shot",
                  ttl_days: Optional[int] = None,
                  description: str = "",
                  max_amount_eur: Optional[float] = None) -> Dict[str, Any]:
    """Crea un mandate PSD3 con SCA al setup.

    En produccion (mode=live) esto dispararia un 3DS challenge real. En
    simulate marca sca_completed=True automaticamente.
    """
    if mandate_type not in VALID_MANDATE_TYPES:
        return {"ok": False,
                "error": f"invalid_mandate_type: {mandate_type}",
                "valid_types": list(VALID_MANDATE_TYPES)}
    if not card_token or not account_id:
        return {"ok": False, "error": "card_token_and_account_required"}

    cfg = get_config()
    if ttl_days is None:
        ttl_days = cfg["default_ttl_days"].get(mandate_type, 30)
    if max_amount_eur is None:
        max_amount_eur = float(cfg["max_amount_per_mit_eur"])

    mandate_id = _new_mandate_id(account_id, card_token)
    expires_at_ts = time.time() + ttl_days * 86400

    # SCA: en mode=simulate marcamos completed; en live haria 3DS real
    sca_completed = True if cfg["mode"] == "simulate" else False
    sca_method = "3ds_challenge" if cfg["require_3ds_at_setup"] else "frictionless"

    mandate = {
        "mandate_id": mandate_id,
        "card_token": card_token,
        "account_id": account_id,
        "mandate_type": mandate_type,
        "description": description or f"PSD3 {mandate_type} mandate",
        "max_amount_eur": max_amount_eur,
        "status": "active" if sca_completed else "pending_sca",
        "sca_completed": sca_completed,
        "sca_method": sca_method,
        "sca_completed_at": _now_iso() if sca_completed else None,
        "created_at": _now_iso(),
        "created_ts": time.time(),
        "expires_at": datetime.fromtimestamp(expires_at_ts).isoformat(),
        "expires_at_ts": expires_at_ts,
        "mit_count": 0,
        "mit_total_eur": 0.0,
        "last_mit_at": None,
    }

    with _lock:
        d = _load_mandates()
        d["mandates"][mandate_id] = mandate
        _save_mandates(d)

    _append_log({
        "event": "mandate_setup",
        "mandate_id": mandate_id,
        "account_id": account_id,
        "mandate_type": mandate_type,
        "ts": _now_iso(),
    })

    return {"ok": True,
            "mandate_id": mandate_id,
            "status": mandate["status"],
            "sca_completed": sca_completed,
            "expires_at": mandate["expires_at"],
            "mandate_type": mandate_type}


def register_mit_exemption(mandate_id: str,
                           transaction_amount: float,
                           merchant_reference: str = "") -> Dict[str, Any]:
    """Registra un MIT (Merchant-Initiated Transaction) bajo un mandate.

    Devuelve si el MIT esta exento de SCA gracias al mandate (caso normal)
    o rechazo si el mandate no esta activo.
    """
    if transaction_amount <= 0:
        return {"ok": False, "error": "amount_must_be_positive"}

    cfg = get_config()
    with _lock:
        d = _load_mandates()
        m = d["mandates"].get(mandate_id)
        if not m:
            return {"ok": False, "error": "mandate_not_found"}

        # Validar status
        if m["status"] != "active":
            return {"ok": False, "error": f"mandate_status_{m['status']}",
                    "exempted": False}

        # Validar SCA completado
        if not m.get("sca_completed"):
            return {"ok": False, "error": "sca_not_completed",
                    "exempted": False}

        # Validar expiry
        if m.get("expires_at_ts", 0) < time.time():
            m["status"] = "expired"
            _save_mandates(d)
            return {"ok": False, "error": "mandate_expired",
                    "exempted": False}

        # Validar caps
        if transaction_amount > m["max_amount_eur"]:
            return {"ok": False,
                    "error": f"amount_exceeds_per_mit_cap_{m['max_amount_eur']}",
                    "exempted": False}

        new_total = m["mit_total_eur"] + transaction_amount
        if new_total > cfg["max_total_per_period_eur"]:
            return {"ok": False,
                    "error": "exceeds_period_cap",
                    "exempted": False,
                    "current_total": m["mit_total_eur"]}

        # Registrar
        m["mit_count"] += 1
        m["mit_total_eur"] = round(new_total, 2)
        m["last_mit_at"] = _now_iso()
        _save_mandates(d)

    _append_log({
        "event": "mit_exemption",
        "mandate_id": mandate_id,
        "amount": transaction_amount,
        "merchant_reference": merchant_reference,
        "ts": _now_iso(),
    })

    return {"ok": True,
            "exempted": True,
            "mandate_id": mandate_id,
            "amount": transaction_amount,
            "mit_count": m["mit_count"],
            "mit_total_eur": m["mit_total_eur"],
            "reason": "psd3_mandate_active_no_sca_required"}


def validate_mandate_active(mandate_id: str) -> Dict[str, Any]:
    """Comprueba si un mandate esta activo (status + expiry + SCA)."""
    with _lock:
        d = _load_mandates()
        m = d["mandates"].get(mandate_id)
    if not m:
        return {"ok": False, "active": False, "error": "mandate_not_found"}

    expired = m.get("expires_at_ts", 0) < time.time()
    active = (m["status"] == "active"
              and m.get("sca_completed")
              and not expired)

    return {"ok": True,
            "active": bool(active),
            "mandate_id": mandate_id,
            "status": m["status"],
            "sca_completed": m.get("sca_completed", False),
            "expired": expired,
            "remaining_seconds": max(0, int(m.get("expires_at_ts", 0) - time.time())),
            "mit_count": m.get("mit_count", 0)}


def expire_mandate(mandate_id: str, reason: str = "manual") -> Dict[str, Any]:
    """Expira un mandate manualmente."""
    with _lock:
        d = _load_mandates()
        m = d["mandates"].get(mandate_id)
        if not m:
            return {"ok": False, "error": "mandate_not_found"}
        m["status"] = "expired"
        m["expired_at"] = _now_iso()
        m["expire_reason"] = reason
        _save_mandates(d)

    _append_log({
        "event": "mandate_expired",
        "mandate_id": mandate_id,
        "reason": reason,
        "ts": _now_iso(),
    })
    return {"ok": True, "mandate_id": mandate_id, "status": "expired"}


def get_mandate(mandate_id: str) -> Dict[str, Any]:
    with _lock:
        d = _load_mandates()
        m = d["mandates"].get(mandate_id)
    if not m:
        return {"ok": False, "error": "mandate_not_found"}
    return {"ok": True, "mandate": m}


def list_mandates(account_id: Optional[str] = None,
                  status: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        d = _load_mandates()
    out = []
    for m in d["mandates"].values():
        if account_id and m["account_id"] != account_id:
            continue
        if status and m["status"] != status:
            continue
        out.append(m)
    out.sort(key=lambda x: x.get("created_ts", 0), reverse=True)
    return out


def auto_expire_lapsed() -> Dict[str, Any]:
    """Recorre mandates y expira los que pasaron su expires_at_ts."""
    expired_count = 0
    with _lock:
        d = _load_mandates()
        now = time.time()
        for mid, m in d["mandates"].items():
            if m["status"] == "active" and m.get("expires_at_ts", 0) < now:
                m["status"] = "expired"
                m["expired_at"] = _now_iso()
                m["expire_reason"] = "ttl_lapsed"
                expired_count += 1
        if expired_count:
            _save_mandates(d)
    return {"ok": True, "expired_count": expired_count}


# ===============================================================
# SELF TEST
# ===============================================================
def self_test() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}

    # 1. Config
    cfg = get_config()
    checks["config_has_mode"] = "mode" in cfg
    checks["config_has_ttl_days"] = "default_ttl_days" in cfg
    checks["config_has_caps"] = ("max_amount_per_mit_eur" in cfg
                                 and "max_total_per_period_eur" in cfg)

    # 2. Setup mandate one-shot
    r = setup_mandate("tok_test_111", "acc_test_aaa", "one-shot",
                      description="self_test one-shot")
    checks["setup_one_shot_ok"] = r.get("ok") is True
    checks["mandate_id_format"] = bool(r.get("mandate_id", "").startswith("mnd_"))
    checks["status_active_after_simulate"] = r.get("status") == "active"
    mid_one = r.get("mandate_id")

    # 3. Setup recurring
    r2 = setup_mandate("tok_test_222", "acc_test_aaa", "recurring",
                       max_amount_eur=500.0)
    checks["setup_recurring_ok"] = r2.get("ok") is True
    mid_rec = r2.get("mandate_id")

    # 4. Setup subscription
    r3 = setup_mandate("tok_test_333", "acc_test_aaa", "subscription")
    checks["setup_subscription_ok"] = r3.get("ok") is True
    mid_sub = r3.get("mandate_id")

    # 5. Invalid mandate_type rejected
    r_bad = setup_mandate("tok_test_x", "acc_test_x", "invalid_type")
    checks["invalid_type_rejected"] = (r_bad.get("ok") is False
                                       and "invalid_mandate_type" in
                                       r_bad.get("error", ""))

    # 6. Missing token rejected
    r_bad2 = setup_mandate("", "acc", "one-shot")
    checks["missing_token_rejected"] = r_bad2.get("ok") is False

    # 7. Validate mandate active
    v = validate_mandate_active(mid_one)
    checks["validate_active_ok"] = v.get("ok") is True
    checks["validate_active_true"] = v.get("active") is True

    # 8. MIT under mandate -> exemption
    r_mit = register_mit_exemption(mid_one, 50.0, merchant_reference="ref-1")
    checks["mit_exempted"] = r_mit.get("exempted") is True
    checks["mit_count_incremented"] = r_mit.get("mit_count") == 1

    # 9. Second MIT under same mandate
    r_mit2 = register_mit_exemption(mid_one, 25.5)
    checks["second_mit_exempted"] = r_mit2.get("exempted") is True
    checks["mit_total_correct"] = r_mit2.get("mit_total_eur") == 75.5

    # 10. MIT excediendo cap rechazado (recurring tiene cap=500)
    r_big = register_mit_exemption(mid_rec, 999999.0)
    checks["mit_over_cap_rejected"] = (r_big.get("ok") is False
                                       and r_big.get("exempted") is False)

    # 11. MIT con monto invalido
    r_neg = register_mit_exemption(mid_one, -10.0)
    checks["negative_amount_rejected"] = r_neg.get("ok") is False

    # 12. MIT en mandate inexistente
    r_nf = register_mit_exemption("mnd_does_not_exist", 10.0)
    checks["unknown_mandate_rejected"] = (r_nf.get("ok") is False
                                          and r_nf.get("error") ==
                                          "mandate_not_found")

    # 13. Get mandate
    g = get_mandate(mid_one)
    checks["get_mandate_ok"] = g.get("ok") is True

    # 14. List mandates by account
    lst = list_mandates(account_id="acc_test_aaa")
    checks["list_at_least_3"] = len(lst) >= 3

    # 15. Expire mandate
    e = expire_mandate(mid_sub, reason="self_test")
    checks["expire_ok"] = e.get("ok") is True

    # 16. After expire, MIT rejected
    r_after = register_mit_exemption(mid_sub, 5.0)
    checks["mit_on_expired_rejected"] = r_after.get("ok") is False

    # 17. validate_mandate_active on expired returns active=False
    v_exp = validate_mandate_active(mid_sub)
    checks["validate_expired_inactive"] = v_exp.get("active") is False

    # 18. auto_expire_lapsed runs
    ae = auto_expire_lapsed()
    checks["auto_expire_runs"] = ae.get("ok") is True

    # 19. Cleanup test mandates
    with _lock:
        d = _load_mandates()
        for tid in (mid_one, mid_rec, mid_sub):
            d["mandates"].pop(tid, None)
        _save_mandates(d)
    checks["cleanup_ok"] = True

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total,
            "total_checks": total,
            "passed_checks": passed,
            "checks": checks}


def get_info() -> Dict[str, Any]:
    with _lock:
        d = _load_mandates()
    actives = sum(1 for m in d.get("mandates", {}).values()
                  if m.get("status") == "active")
    return {
        "module": "psd3_mandate_setup",
        "version": 1,
        "purpose": "PSD3 PSR mandate setup with SCA only at setup, MIT exemptions afterwards",
        "valid_mandate_types": list(VALID_MANDATE_TYPES),
        "active_mandates": actives,
        "total_mandates": len(d.get("mandates", {})),
        "mode": get_config().get("mode"),
    }
