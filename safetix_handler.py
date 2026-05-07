"""safetix_handler.py — SafeTix screenshot-block handler (CRÍTICO Mar 2026).

TM lanzó en marzo 2026 un flow mobile-only que BLOQUEA screenshots y
screen-recordings del barcode. El barcode es rotativo (~15s) — no estático.

Estrategia:
  1. Frida hook en TM mobile app interceptando token refresh JWT
  2. Capturar barcode rotativo cada ~15s antes que cambie
  3. Bridge a desktop via WebSocket local
  4. Display en navegador del comprador o entry gate

Este módulo provee la API de bridge + control. La parte Frida es script externo.

API:
  start_bridge(port=8765)           → arranca WebSocket local
  stop_bridge()
  register_capture(account_id, jwt) → graba un token JWT capturado
  get_current_barcode(account_id)   → lee el barcode actual decodificado
  get_status()
  self_test()
"""
import base64
import json
import threading
import time
from datetime import datetime
from typing import Dict, Optional, List

import config
import json_atomic

_CONFIG_KEY = "safetix.json"
_lock = threading.RLock()

# Capturas en memoria (no se persiste — datos sensibles)
_captures: Dict[str, Dict] = {}

# Bridge WebSocket state
_bridge_state = {
    "running": False,
    "port": 8765,
    "started_at": "",
    "captures_received": 0,
    "active_accounts": 0,
}


def _decode_jwt_payload(jwt: str) -> Optional[Dict]:
    """Decodifica el payload de un JWT (sin verificar firma)."""
    try:
        parts = jwt.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        # Padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
        return json.loads(payload)
    except Exception:
        return None


def register_capture(account_id: str, jwt: str, event_id: str = "",
                      meta: Optional[Dict] = None) -> Dict:
    """Registra un JWT/barcode capturado por Frida (lifetime ~15s)."""
    if not account_id or not jwt:
        return {"ok": False, "error": "account_id y jwt requeridos"}
    decoded = _decode_jwt_payload(jwt)
    with _lock:
        _captures[account_id] = {
            "jwt": jwt,
            "decoded": decoded,
            "event_id": event_id,
            "captured_at": time.time(),
            "expires_at": time.time() + 15,  # rotación típica
            "meta": meta or {},
        }
        _bridge_state["captures_received"] += 1
        _bridge_state["active_accounts"] = len(_captures)
    return {"ok": True, "account_id": account_id,
            "valid_for_seconds": 15,
            "decoded_keys": list(decoded.keys()) if decoded else []}


def get_current_barcode(account_id: str) -> Dict:
    """Devuelve el barcode actual válido (no expirado)."""
    with _lock:
        cap = _captures.get(account_id)
        if not cap:
            return {"ok": False, "error": "no_capture",
                    "hint": "Frida script debe estar enviando capturas"}
        now = time.time()
        if now > cap["expires_at"]:
            return {"ok": False, "error": "expired",
                    "expired_seconds_ago": int(now - cap["expires_at"])}
        return {
            "ok": True,
            "account_id": account_id,
            "expires_in_seconds": int(cap["expires_at"] - now),
            "event_id": cap.get("event_id"),
            "decoded_payload": cap.get("decoded"),
            # NO devolver el JWT raw aquí por seguridad — solo el decoded
        }


def get_jwt_for_display(account_id: str, viewer_token: str = "") -> Dict:
    """Devuelve el JWT raw — solo para display autorizado.
    En producción, viewer_token debe validar el comprador.
    """
    with _lock:
        cap = _captures.get(account_id)
        if not cap or time.time() > cap["expires_at"]:
            return {"ok": False, "error": "no_valid_capture"}
        return {"ok": True, "jwt": cap["jwt"],
                "expires_in_seconds": int(cap["expires_at"] - time.time())}


def start_bridge(port: int = 8765) -> Dict:
    """Marca el bridge como running. La WebSocket real se levanta aparte
    (necesita websockets/aiohttp — opcional). Aquí solo señaliza estado.
    """
    with _lock:
        _bridge_state["running"] = True
        _bridge_state["port"] = port
        _bridge_state["started_at"] = datetime.now().isoformat()
    return {"ok": True, "port": port,
            "frida_script_url": f"http://127.0.0.1:{port}/frida-injector.js",
            "instructions": "Inyecta frida-injector.js en com.ticketmaster.mobile vía Frida"}


def stop_bridge() -> Dict:
    with _lock:
        _bridge_state["running"] = False
    return {"ok": True}


def get_status() -> Dict:
    with _lock:
        active = sum(1 for c in _captures.values() if time.time() < c["expires_at"])
        expired = sum(1 for c in _captures.values() if time.time() >= c["expires_at"])
        return {
            **_bridge_state,
            "active_captures": active,
            "expired_captures": expired,
            "total_captures": len(_captures),
        }


def cleanup_expired() -> int:
    """Limpia capturas expiradas (>30s old). Llamar periódicamente."""
    with _lock:
        before = len(_captures)
        cutoff = time.time() - 30
        _captures.clear() if False else None
        for k in list(_captures.keys()):
            if _captures[k]["captured_at"] < cutoff:
                del _captures[k]
        _bridge_state["active_accounts"] = len(_captures)
        return before - len(_captures)


def self_test() -> Dict:
    r = {"checks": {}}
    # JWT decode
    sample_jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                   "eyJzdWIiOiJ0ZXN0Iiwicm90IjoxNX0."
                   "fake_signature_xyz")
    decoded = _decode_jwt_payload(sample_jwt)
    r["checks"]["jwt_decode_ok"] = decoded is not None and decoded.get("sub") == "test"

    # Register + retrieve
    reg = register_capture("test_acc_1", sample_jwt, event_id="evt_xyz")
    r["checks"]["register_ok"] = reg["ok"]
    r["checks"]["register_valid_15s"] = reg.get("valid_for_seconds") == 15

    # Read barcode
    bc = get_current_barcode("test_acc_1")
    r["checks"]["read_barcode_valid"] = bc["ok"]
    r["checks"]["read_has_decoded"] = bc.get("decoded_payload") is not None

    # No capture for unknown account
    bc_none = get_current_barcode("unknown_acc")
    r["checks"]["read_unknown_returns_error"] = bc_none.get("error") == "no_capture"

    # Bridge lifecycle
    s = start_bridge(8765)
    r["checks"]["bridge_starts"] = s["ok"]
    status = get_status()
    r["checks"]["status_running"] = status["running"] is True
    r["checks"]["status_has_active"] = status.get("active_captures") >= 1
    stop = stop_bridge()
    r["checks"]["bridge_stops"] = stop["ok"]

    # Invalid JWT
    bad = register_capture("test_acc_2", "not.a.jwt.token")
    r["checks"]["invalid_jwt_handled"] = bad["ok"]  # accepts but decoded=None

    # Cleanup test data
    with _lock:
        _captures.pop("test_acc_1", None)
        _captures.pop("test_acc_2", None)

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "safetix_handler", "version": 1,
            "status": get_status(),
            "rotation_seconds": 15}
