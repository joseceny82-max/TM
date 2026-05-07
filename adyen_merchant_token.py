"""adyen_merchant_token.py — SOTA P2-25.

Adyen Apple Pay / Google Pay merchant token (DPAN persistente).

Stripe / Adyen 2026 docs confirman que merchant tokens (network tokens DPAN)
sobreviven a recurring + cross-device + cross-merchant cuando se registran
correctamente. TM acepta merchant token en checkout para reducir friction.

Diferencia vs `adyen_tokenizer.py` (que tokeniza una tarjeta single-use):
  - merchant_token = network token persistente (DPAN), revocable
  - se asocia a un recipient_id (account_id en TM)
  - puede ser usado en N transacciones futuras
  - lifecycle: create → validate → revoke

API:
  create_merchant_token(card_token, recipient_id, ...) → dict
  revoke_merchant_token(token_id) → dict
  list_merchant_tokens(account_id=None) → list[dict]
  validate_token_active(token_id) → dict
  get_token(token_id) → dict | None
  cleanup_expired() → dict
  self_test()

NO ejecuta payments reales; persiste estado de tokens simulados con json_atomic.
"""
from __future__ import annotations
import os, time, json, hashlib, threading, secrets, random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config
import json_atomic


_CONFIG_KEY = "adyen_merchant_token_config.json"
_TOKENS_KEY = "adyen_merchant_tokens.json"
_LOG_KEY = "adyen_merchant_token_log.json"
_lock = threading.RLock()


def _path(key: str) -> str:
    p = config.ruta(key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
def get_config() -> Dict[str, Any]:
    return json_atomic.load(_path(_CONFIG_KEY), default={
        "mode": "simulate",                  # simulate | live
        "endpoint": "https://checkout-test.adyen.com/v71/paymentMethods/balance",
        "default_ttl_days": 365,             # network tokens DPAN duran ~1y
        "default_currency": "EUR",
        "max_tokens_per_account": 10,
        "auto_revoke_inactive_days": 540,
        "wallet_types_supported": ["applepay", "googlepay", "scheme"],
        "merchant_account": "TicketmasterCOM",
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(_path(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# STORAGE
# ═══════════════════════════════════════════════════════════════
def _load_tokens() -> Dict[str, Any]:
    return json_atomic.load(_path(_TOKENS_KEY), default={"tokens": {}})


def _save_tokens(d: Dict[str, Any]) -> None:
    json_atomic.save(_path(_TOKENS_KEY), d)


def _log_event(event: str, data: Dict[str, Any]) -> None:
    try:
        log = json_atomic.load(_path(_LOG_KEY), default={"events": []})
        log["events"].insert(0, {
            "ts": datetime.now().isoformat(),
            "event": event,
            "data": data,
        })
        log["events"] = log["events"][:500]
        json_atomic.save(_path(_LOG_KEY), log)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# TOKEN OPERATIONS
# ═══════════════════════════════════════════════════════════════
def _gen_token_id() -> str:
    """Genera un token id estilo Adyen network token (DPAN reference)."""
    seed = f"mt_{time.time()}_{secrets.token_hex(8)}"
    return "MT-" + hashlib.sha256(seed.encode()).hexdigest()[:24].upper()


def _gen_dpan_last4() -> str:
    """Network token dpan last4 (artificial)."""
    return "".join(secrets.choice("0123456789") for _ in range(4))


def create_merchant_token(card_token: str,
                          recipient_id: str,
                          wallet_type: str = "scheme",
                          metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Crea un merchant_token (network DPAN) para `recipient_id`.

    `card_token` debería ser un id ya tokenizado por `adyen_tokenizer`,
    pero aceptamos también texto libre. NO ejecuta payments reales.
    """
    if not card_token or not recipient_id:
        return {"ok": False, "error": "missing_card_or_recipient"}

    cfg = get_config()
    if wallet_type not in cfg["wallet_types_supported"]:
        return {"ok": False, "error": "unsupported_wallet_type",
                "supported": cfg["wallet_types_supported"]}

    with _lock:
        d = _load_tokens()
        # max por cuenta
        active_for_acc = [t for t in d["tokens"].values()
                          if t.get("recipient_id") == recipient_id
                          and t.get("status") == "active"]
        if len(active_for_acc) >= cfg["max_tokens_per_account"]:
            return {"ok": False, "error": "max_tokens_per_account",
                    "current": len(active_for_acc)}

        tid = _gen_token_id()
        now = datetime.now()
        token = {
            "token_id": tid,
            "card_token_ref": card_token,
            "recipient_id": recipient_id,
            "wallet_type": wallet_type,
            "dpan_last4": _gen_dpan_last4(),
            "currency": (metadata or {}).get("currency") or cfg["default_currency"],
            "merchant_account": cfg["merchant_account"],
            "status": "active",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=cfg["default_ttl_days"])).isoformat(),
            "last_used_at": None,
            "uses_count": 0,
            "metadata": metadata or {},
        }
        d["tokens"][tid] = token
        _save_tokens(d)
    _log_event("create", {"token_id": tid, "recipient_id": recipient_id,
                          "wallet_type": wallet_type})
    return {"ok": True, "token_id": tid, "dpan_last4": token["dpan_last4"],
            "expires_at": token["expires_at"], "wallet_type": wallet_type}


def revoke_merchant_token(token_id: str,
                          reason: str = "user_request") -> Dict[str, Any]:
    """Revoca un merchant_token (no se borra, queda con status='revoked')."""
    with _lock:
        d = _load_tokens()
        tk = d["tokens"].get(token_id)
        if not tk:
            return {"ok": False, "error": "not_found", "token_id": token_id}
        if tk["status"] == "revoked":
            return {"ok": True, "already_revoked": True, "token_id": token_id}
        tk["status"] = "revoked"
        tk["revoked_at"] = datetime.now().isoformat()
        tk["revoke_reason"] = reason
        _save_tokens(d)
    _log_event("revoke", {"token_id": token_id, "reason": reason})
    return {"ok": True, "token_id": token_id, "status": "revoked"}


def list_merchant_tokens(account_id: Optional[str] = None,
                          include_revoked: bool = False) -> List[Dict[str, Any]]:
    """Lista tokens por cuenta. Si account_id=None, devuelve todos."""
    d = _load_tokens()
    out = []
    for tk in d.get("tokens", {}).values():
        if account_id and tk.get("recipient_id") != account_id:
            continue
        if not include_revoked and tk.get("status") == "revoked":
            continue
        out.append(tk)
    out.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return out


def validate_token_active(token_id: str) -> Dict[str, Any]:
    """Comprueba si un token sigue activo (status + expiry)."""
    d = _load_tokens()
    tk = d.get("tokens", {}).get(token_id)
    if not tk:
        return {"ok": False, "active": False, "error": "not_found"}
    if tk.get("status") != "active":
        return {"ok": True, "active": False, "status": tk.get("status"),
                "reason": "non_active_status"}
    try:
        exp = datetime.fromisoformat(tk.get("expires_at", ""))
        if exp < datetime.now():
            return {"ok": True, "active": False, "status": "expired",
                    "expires_at": tk.get("expires_at")}
    except Exception:
        pass
    return {"ok": True, "active": True, "token_id": token_id,
            "wallet_type": tk.get("wallet_type"),
            "dpan_last4": tk.get("dpan_last4"),
            "uses_count": tk.get("uses_count", 0)}


def get_token(token_id: str) -> Optional[Dict[str, Any]]:
    d = _load_tokens()
    return d.get("tokens", {}).get(token_id)


def mark_used(token_id: str) -> Dict[str, Any]:
    """Incrementa el contador de uso de un token (registro local)."""
    with _lock:
        d = _load_tokens()
        tk = d["tokens"].get(token_id)
        if not tk:
            return {"ok": False, "error": "not_found"}
        tk["uses_count"] = tk.get("uses_count", 0) + 1
        tk["last_used_at"] = datetime.now().isoformat()
        _save_tokens(d)
    return {"ok": True, "token_id": token_id, "uses_count": tk["uses_count"]}


def cleanup_expired() -> Dict[str, Any]:
    """Marca como `expired` todos los tokens cuyo expires_at ya pasó."""
    n_expired = 0
    with _lock:
        d = _load_tokens()
        now = datetime.now()
        for tk in d["tokens"].values():
            if tk.get("status") != "active":
                continue
            try:
                exp = datetime.fromisoformat(tk.get("expires_at", ""))
                if exp < now:
                    tk["status"] = "expired"
                    tk["expired_at"] = now.isoformat()
                    n_expired += 1
            except Exception:
                continue
        if n_expired:
            _save_tokens(d)
    return {"ok": True, "expired_count": n_expired}


def stats() -> Dict[str, Any]:
    d = _load_tokens()
    counts = {"active": 0, "revoked": 0, "expired": 0, "total": 0}
    by_wallet: Dict[str, int] = {}
    for tk in d["tokens"].values():
        counts["total"] += 1
        st = tk.get("status", "active")
        counts[st] = counts.get(st, 0) + 1
        wt = tk.get("wallet_type", "unknown")
        by_wallet[wt] = by_wallet.get(wt, 0) + 1
    return {"counts": counts, "by_wallet": by_wallet}


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. config
    cfg = get_config()
    checks["config_has_mode"] = cfg.get("mode") in ("simulate", "live")
    checks["config_has_wallet_types"] = isinstance(cfg.get("wallet_types_supported"), list) and len(cfg["wallet_types_supported"]) >= 3

    # 2. save_config patch
    cur = save_config({"default_currency": "EUR"})
    checks["save_config_keeps_keys"] = "endpoint" in cur

    # 3. create with missing args
    r = create_merchant_token("", "")
    checks["create_rejects_empty"] = r.get("ok") is False

    # 4. create with bad wallet_type
    r = create_merchant_token("card_xx", "acc_st_x", wallet_type="bitcoin")
    checks["create_rejects_bad_wallet"] = r.get("ok") is False

    # 5. create OK applepay
    r = create_merchant_token("card_apl", "acc_st_apl", wallet_type="applepay")
    checks["create_applepay_ok"] = r.get("ok") is True
    tid_a = r.get("token_id")
    checks["token_id_format"] = isinstance(tid_a, str) and tid_a.startswith("MT-")
    checks["dpan_last4_4digits"] = len(r.get("dpan_last4", "")) == 4

    # 6. create OK googlepay
    r = create_merchant_token("card_g", "acc_st_g", wallet_type="googlepay")
    tid_g = r.get("token_id")
    checks["create_googlepay_ok"] = r.get("ok") is True

    # 7. validate_token_active
    v = validate_token_active(tid_a)
    checks["validate_active_true"] = v.get("active") is True
    v = validate_token_active("MT-DOES-NOT-EXIST")
    checks["validate_unknown_inactive"] = v.get("active") is False

    # 8. list filtering
    lst = list_merchant_tokens(account_id="acc_st_apl")
    checks["list_by_account_filters"] = all(t["recipient_id"] == "acc_st_apl" for t in lst)
    checks["list_has_at_least_one"] = len(lst) >= 1

    # 9. mark_used increments
    r1 = mark_used(tid_a)
    r2 = mark_used(tid_a)
    checks["mark_used_increments"] = r2.get("uses_count", 0) >= 2

    # 10. revoke
    r = revoke_merchant_token(tid_g, reason="self_test")
    checks["revoke_ok"] = r.get("ok") is True
    v = validate_token_active(tid_g)
    checks["revoked_not_active"] = v.get("active") is False

    # 11. revoke unknown
    r = revoke_merchant_token("MT-NOT-REAL")
    checks["revoke_unknown_fails"] = r.get("ok") is False

    # 12. revoke twice idempotent
    r = revoke_merchant_token(tid_g)
    checks["revoke_twice_idempotent"] = r.get("ok") is True

    # 13. list excludes revoked by default
    lst = list_merchant_tokens(account_id="acc_st_g")
    checks["list_excludes_revoked"] = all(t["status"] != "revoked" for t in lst)
    lst_full = list_merchant_tokens(account_id="acc_st_g", include_revoked=True)
    checks["list_includes_when_flag"] = any(t["status"] == "revoked" for t in lst_full)

    # 14. cleanup expired (force expired token to test path)
    with _lock:
        d = _load_tokens()
        for tk in d["tokens"].values():
            if tk["token_id"] == tid_a:
                tk["expires_at"] = (datetime.now() - timedelta(days=1)).isoformat()
        _save_tokens(d)
    r = cleanup_expired()
    checks["cleanup_runs"] = r.get("ok") is True
    checks["cleanup_marked_one"] = r.get("expired_count", 0) >= 1
    v = validate_token_active(tid_a)
    checks["expired_not_active"] = v.get("active") is False

    # 15. stats
    s = stats()
    checks["stats_has_counts"] = "counts" in s and s["counts"].get("total", 0) >= 2
    checks["stats_has_wallets"] = "by_wallet" in s

    # 16. get_token returns dict or None
    checks["get_token_dict"] = isinstance(get_token(tid_a), dict)
    checks["get_token_none_unknown"] = get_token("nope") is None

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    s = stats()
    return {
        "module": "adyen_merchant_token",
        "version": 1,
        "purpose": "Adyen Apple Pay / Google Pay merchant token (DPAN persistente)",
        "tokens_total": s["counts"].get("total", 0),
        "tokens_active": s["counts"].get("active", 0),
        "by_wallet": s["by_wallet"],
        "mode": get_config().get("mode"),
    }
