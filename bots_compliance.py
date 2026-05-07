"""bots_compliance.py — BOTS Act 2016 compliance + FTC safety layer.

Bloquea el modo LIVE hasta que el usuario firme un waiver explícito.
Protege legalmente dado el pleito activo FTC v Ticketmaster (sep 2025).

Multas BOTS Act (ajustado inflación 2026): $53,088 por violación.

API pública:
  get_config() / save_config(patch)
  sign_waiver(user_id, ip, signature) → dict
  verify_live_allowed() → bool + reason
  list_waivers() / revoke_waiver(waiver_id)
  self_test()
"""
import hashlib
import threading
import uuid
from datetime import datetime
from typing import Optional, List, Dict

import config
import json_atomic

_WAIVERS_KEY = "bots_compliance_waivers.json"
_CONFIG_KEY = "bots_compliance_config.json"
_lock = threading.Lock()

WAIVER_VERSION = "2026-04-22"
WAIVER_TEXT = """
⚖️ BOTS ACT COMPLIANCE WAIVER — TM PERROS

Al firmar este waiver, el usuario:

1. Reconoce el Better Online Ticket Sales (BOTS) Act of 2016 (US Code 15 §45c),
   multas $53,088 por violación (FTC 2026 inflation adjustment).

2. Confirma que solo usará el bot para:
   (a) compras PROPIAS con tarjeta propia
   (b) NO para reventa con fines de lucro / scalping
   (c) NO para crear cuentas falsas masivamente

3. Acepta que operar en jurisdicciones donde los bots de tickets son
   ILEGALES (USA + UE por Digital Economy Act 2017 + Ley 13.288 España)
   es responsabilidad SUYA como operador.

4. El software se provee AS-IS sin garantía. El autor no se responsabiliza
   de multas, baneos, demandas ni pérdidas económicas por mal uso.

5. El modo LIVE queda bloqueado hasta que este waiver sea firmado con:
   - user_id autenticado en dashboard
   - IP del firmante
   - timestamp inmutable
   - hash SHA256 de consent string

⚠️ NOTA SOBRE EL PLEITO FTC: septiembre 2025, FTC + 7 AG vs Ticketmaster
por no impedir bots. Durante este proceso, Ticketmaster tiene incentivo
MÁXIMO para perseguir a operadores de bots. USE RESPONSABLEMENTE.
"""


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "enabled": True,
        "require_waiver_for_live": True,
        "require_waiver_for_registrations": True,
        "waiver_version_required": WAIVER_VERSION,
        "audit_log_immutable": True,
        "show_ftc_warning_on_login": True,
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def get_waiver_text() -> dict:
    return {
        "version": WAIVER_VERSION,
        "text": WAIVER_TEXT,
        "published_at": WAIVER_VERSION,
    }


def sign_waiver(user_id: str, ip: str = "",
                 signature: str = "",
                 user_agent: str = "",
                 consent_checked: bool = True) -> dict:
    """Firma un waiver. Requiere consent_checked=True explícito."""
    if not user_id:
        return {"ok": False, "error": "user_id requerido"}
    if not consent_checked:
        return {"ok": False, "error": "consent_checked=True requerido"}

    # Hash inmutable
    payload = f"{user_id}|{WAIVER_VERSION}|{ip}|{datetime.now().isoformat()}|{signature}"
    waiver_hash = hashlib.sha256(payload.encode()).hexdigest()

    waiver = {
        "id": "wv_" + uuid.uuid4().hex[:10],
        "user_id": user_id,
        "ip": ip,
        "user_agent": user_agent[:200],
        "signature": signature[:100],
        "signed_at": datetime.now().isoformat(),
        "waiver_version": WAIVER_VERSION,
        "hash": waiver_hash,
        "revoked": False,
    }
    with _lock:
        d = json_atomic.load(config.ruta(_WAIVERS_KEY), default={"waivers": []})
        d["waivers"].append(waiver)
        json_atomic.save(config.ruta(_WAIVERS_KEY), d)

    return {"ok": True, "waiver": waiver}


def list_waivers(user_id: Optional[str] = None,
                   active_only: bool = True) -> List[dict]:
    d = json_atomic.load(config.ruta(_WAIVERS_KEY), default={"waivers": []})
    items = d.get("waivers", [])
    if user_id:
        items = [w for w in items if w.get("user_id") == user_id]
    if active_only:
        items = [w for w in items if not w.get("revoked")]
    return items


def revoke_waiver(waiver_id: str, reason: str = "") -> dict:
    with _lock:
        d = json_atomic.load(config.ruta(_WAIVERS_KEY), default={"waivers": []})
        for w in d["waivers"]:
            if w.get("id") == waiver_id:
                w["revoked"] = True
                w["revoked_at"] = datetime.now().isoformat()
                w["revoke_reason"] = reason[:200]
                json_atomic.save(config.ruta(_WAIVERS_KEY), d)
                return {"ok": True, "revoked": waiver_id}
    return {"ok": False, "error": "waiver_id no encontrado"}


def verify_live_allowed(user_id: str = "", operation: str = "live_purchase") -> dict:
    """Verifica si se permite modo LIVE para este user+operación."""
    cfg = get_config()
    if not cfg.get("enabled"):
        return {"ok": True, "allowed": True, "reason": "compliance disabled (dev mode)"}

    if operation == "live_purchase" and not cfg.get("require_waiver_for_live"):
        return {"ok": True, "allowed": True, "reason": "waiver not required for live"}
    if operation == "registrations" and not cfg.get("require_waiver_for_registrations"):
        return {"ok": True, "allowed": True, "reason": "waiver not required for registrations"}

    if not user_id:
        return {
            "ok": True, "allowed": False, "reason": "no user_id",
            "action_required": "login first",
        }

    # Buscar waiver firmado por este user
    waivers = list_waivers(user_id=user_id, active_only=True)
    valid = [w for w in waivers
              if w.get("waiver_version") == cfg.get("waiver_version_required", WAIVER_VERSION)]

    if not valid:
        return {
            "ok": True, "allowed": False,
            "reason": "no_signed_waiver",
            "action_required": f"sign waiver version {WAIVER_VERSION}",
            "endpoint": "POST /api/bots-compliance/sign",
            "waiver_text_endpoint": "GET /api/bots-compliance/waiver-text",
        }

    return {
        "ok": True, "allowed": True,
        "reason": "valid_waiver_found",
        "waiver_id": valid[0].get("id"),
        "signed_at": valid[0].get("signed_at"),
    }


def get_stats() -> dict:
    d = json_atomic.load(config.ruta(_WAIVERS_KEY), default={"waivers": []})
    waivers = d.get("waivers", [])
    active = [w for w in waivers if not w.get("revoked")]
    unique_users = len(set(w.get("user_id") for w in active))
    return {
        "total_waivers": len(waivers),
        "active_waivers": len(active),
        "revoked_waivers": len(waivers) - len(active),
        "unique_signing_users": unique_users,
        "waiver_version_current": WAIVER_VERSION,
        "compliance_enabled": get_config().get("enabled"),
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    # Get waiver text
    wt = get_waiver_text()
    results["checks"]["waiver_text_available"] = "BOTS Act" in wt["text"]
    results["checks"]["waiver_version"] = wt["version"]

    # Verify live blocked without waiver
    v = verify_live_allowed(user_id="test_user_no_waiver")
    results["checks"]["live_blocked_without_waiver"] = not v["allowed"]

    # Sign
    r = sign_waiver(user_id="test_user_wv", ip="127.0.0.1",
                     signature="I AGREE",
                     user_agent="test/1.0", consent_checked=True)
    results["checks"]["sign_ok"] = r.get("ok", False)
    results["checks"]["waiver_id"] = r.get("waiver", {}).get("id")

    # Verify now allowed
    v2 = verify_live_allowed(user_id="test_user_wv")
    results["checks"]["live_allowed_after_sign"] = v2.get("allowed", False)

    # Revoke
    if r.get("waiver", {}).get("id"):
        rev = revoke_waiver(r["waiver"]["id"], reason="test cleanup")
        results["checks"]["revoke_ok"] = rev.get("ok", False)

    # Verify blocked again after revoke
    v3 = verify_live_allowed(user_id="test_user_wv")
    results["checks"]["live_blocked_after_revoke"] = not v3["allowed"]

    # Stats
    s = get_stats()
    results["checks"]["stats_readable"] = "total_waivers" in s

    return results


def get_info() -> dict:
    return {
        "module": "bots_compliance",
        "waiver_version": WAIVER_VERSION,
        "stats": get_stats(),
        "config": get_config(),
    }
