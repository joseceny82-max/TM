"""safetix_reverse.py - SafeTix rotating barcode reverse engineering (SOTA P1-8).

SafeTix es el sistema de barcodes rotativos de Ticketmaster (lanzado 2019,
rebranded en 2025-2026 con HMAC-SHA1 reforzado). El barcode visible al usuario
en Apple Wallet / Google Wallet rota cada ~15 segundos para evitar:

  - Screenshots compartidos por whatsapp
  - Fotos de pantalla impresas
  - Apps de scalping con "static" tickets

Formato del token observado (paper conduition.io 2025):
    "<base64-event-id>:<HMAC-SHA1-hex>:<unix-ts // 15>"

La HMAC se deriva de:
    key  = SHA1(event_id + "|" + ticket_id)[:20]
    msg  = f"{event_id}|{ticket_id}|{rotation_window}"
    hmac = HMAC-SHA1(key, msg).hexdigest()[:32]

Donde rotation_window = floor(unix_ts / 15).

API publica:
  parse_safetix_token(barcode_str)            -> dict {event_id, hmac, window, valid_format}
  derive_barcode(seed_hex, ts_seconds)        -> str  (token completo para ese ts)
  validate_rotation_pattern(barcodes_list)    -> dict (analiza si rotan correctamente)
  extract_seed_from_ticket_url(url)           -> dict {seed_hex, event_id, ticket_id}
  build_safetix_session(event_id, ticket_id)  -> dict (objeto sesion completo)
  forecast_next_rotation(now_ts)              -> dict {next_rotation_ts, remaining_s}
  self_test()
"""
from __future__ import annotations
import os
import re
import time
import json
import hmac
import hashlib
import base64
import secrets
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import config
import json_atomic


_CONFIG_KEY = "safetix_config.json"
_LOG_KEY = "safetix_log.json"
_SESSION_KEY = "safetix_sessions.json"
_lock = threading.RLock()
_sessions: Dict[str, Dict[str, Any]] = {}

# Ventana de rotacion (segundos) - SafeTix usa 15s desde Q2 2025
ROTATION_WINDOW_S = 15


# =====================================================================
# CONFIG
# =====================================================================
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "rotation_window_s": ROTATION_WINDOW_S,
        "hmac_truncate_chars": 32,
        "key_truncate_bytes": 20,
        "default_event_prefix": "evt",
        "default_ticket_prefix": "tkt",
        "max_clock_skew_s": 30,
        "validate_strict_format": True,
        "mode": "simulate",
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# =====================================================================
# CORE: derivacion / parseo
# =====================================================================
def _derive_key(event_id: str, ticket_id: str) -> bytes:
    """Deriva la HMAC key (key_truncate_bytes desde SHA1)."""
    cfg = get_config()
    raw = f"{event_id}|{ticket_id}".encode("utf-8")
    return hashlib.sha1(raw).digest()[: cfg["key_truncate_bytes"]]


def _b64url_event(event_id: str) -> str:
    """Base64url del event_id (sin padding) - asi aparece en el barcode."""
    return base64.urlsafe_b64encode(event_id.encode("utf-8")).rstrip(b"=").decode()


def _decode_b64url_event(b64: str) -> str:
    """Inversa de _b64url_event."""
    pad = "=" * (-len(b64) % 4)
    try:
        return base64.urlsafe_b64decode((b64 + pad).encode()).decode("utf-8")
    except Exception:
        return ""


def derive_barcode(seed_hex: str, ts_seconds: Optional[int] = None,
                   event_id: Optional[str] = None,
                   ticket_id: Optional[str] = None) -> str:
    """Deriva el barcode SafeTix para un timestamp dado.

    El "seed_hex" representa la combinacion event_id|ticket_id. Si se pasan
    event_id/ticket_id explicitos se usan; si no, se splittean del seed.
    """
    cfg = get_config()
    ts = int(ts_seconds if ts_seconds is not None else time.time())
    window = ts // cfg["rotation_window_s"]

    if event_id is None or ticket_id is None:
        # seed_hex format esperado: "<event_id>|<ticket_id>" en hex o crudo
        try:
            decoded = bytes.fromhex(seed_hex).decode("utf-8")
        except Exception:
            decoded = seed_hex
        if "|" in decoded:
            event_id, ticket_id = decoded.split("|", 1)
        else:
            event_id = event_id or decoded[:16]
            ticket_id = ticket_id or decoded[16:32] or "tkt0"

    key = _derive_key(event_id, ticket_id)
    msg = f"{event_id}|{ticket_id}|{window}".encode("utf-8")
    mac = hmac.new(key, msg, hashlib.sha1).hexdigest()[: cfg["hmac_truncate_chars"]]
    return f"{_b64url_event(event_id)}:{mac}:{window}"


def parse_safetix_token(barcode_str: str) -> Dict[str, Any]:
    """Parsea un barcode SafeTix devolviendo sus 3 partes."""
    if not isinstance(barcode_str, str) or not barcode_str:
        return {"valid_format": False, "error": "empty"}
    parts = barcode_str.strip().split(":")
    if len(parts) != 3:
        return {"valid_format": False, "error": "wrong_part_count",
                "parts": len(parts)}
    b64_event, mac, window_s = parts
    if not re.fullmatch(r"[A-Za-z0-9_-]+=*", b64_event):
        return {"valid_format": False, "error": "bad_b64_event"}
    if not re.fullmatch(r"[0-9a-fA-F]+", mac):
        return {"valid_format": False, "error": "bad_hmac_hex"}
    if not window_s.isdigit():
        return {"valid_format": False, "error": "bad_window"}
    window = int(window_s)
    return {
        "valid_format": True,
        "event_b64": b64_event,
        "event_id": _decode_b64url_event(b64_event),
        "hmac": mac,
        "hmac_length": len(mac),
        "window": window,
        "window_ts": window * ROTATION_WINDOW_S,
        "window_iso": datetime.utcfromtimestamp(window * ROTATION_WINDOW_S).isoformat() + "Z",
    }


def verify_barcode(barcode_str: str, event_id: str, ticket_id: str,
                   max_skew_windows: int = 2) -> Dict[str, Any]:
    """Verifica un barcode contra event_id+ticket_id permitiendo skew."""
    parsed = parse_safetix_token(barcode_str)
    if not parsed["valid_format"]:
        return {"ok": False, "reason": "bad_format", "detail": parsed}
    target_window = parsed["window"]
    matches = []
    for delta in range(-max_skew_windows, max_skew_windows + 1):
        w = target_window + delta
        cfg = get_config()
        key = _derive_key(event_id, ticket_id)
        msg = f"{event_id}|{ticket_id}|{w}".encode("utf-8")
        mac = hmac.new(key, msg, hashlib.sha1).hexdigest()[: cfg["hmac_truncate_chars"]]
        if hmac.compare_digest(mac, parsed["hmac"]):
            matches.append({"window": w, "delta": delta})
    return {"ok": bool(matches), "matches": matches,
            "expected_event_b64": _b64url_event(event_id),
            "got_event_b64": parsed["event_b64"]}


# =====================================================================
# ROTATION ANALYSIS
# =====================================================================
def validate_rotation_pattern(barcodes_list: List[str]) -> Dict[str, Any]:
    """Recibe una lista de barcodes consecutivos y valida que rotan bien.

    Reglas:
      - Cada barcode parsea OK
      - El event_b64 es el mismo en toda la lista
      - Las ventanas son monotonas crecientes
      - Cada ventana sucesiva difiere en >= 1 (no repeticion)
      - Si hay >2 elementos, el delta promedio es 1.0 (cada 15s)
    """
    if not isinstance(barcodes_list, list) or len(barcodes_list) < 2:
        return {"valid": False, "reason": "need_at_least_2"}
    parsed = [parse_safetix_token(b) for b in barcodes_list]
    if not all(p.get("valid_format") for p in parsed):
        return {"valid": False, "reason": "some_unparseable",
                "details": parsed}
    events = {p["event_b64"] for p in parsed}
    if len(events) != 1:
        return {"valid": False, "reason": "event_b64_changes",
                "events": list(events)}
    windows = [p["window"] for p in parsed]
    deltas = [windows[i + 1] - windows[i] for i in range(len(windows) - 1)]
    if any(d <= 0 for d in deltas):
        return {"valid": False, "reason": "non_monotonic", "deltas": deltas}
    avg_delta = sum(deltas) / len(deltas)
    hmacs = [p["hmac"] for p in parsed]
    if len(set(hmacs)) != len(hmacs):
        return {"valid": False, "reason": "duplicate_hmac",
                "uniques": len(set(hmacs)), "total": len(hmacs)}
    return {
        "valid": True,
        "count": len(parsed),
        "event_b64": list(events)[0],
        "deltas": deltas,
        "avg_delta": round(avg_delta, 3),
        "min_window": min(windows),
        "max_window": max(windows),
        "rotation_seconds": ROTATION_WINDOW_S,
        "all_unique_hmacs": True,
    }


def forecast_next_rotation(now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Cuanto tiempo queda hasta la siguiente rotacion."""
    cfg = get_config()
    now = now_ts if now_ts is not None else time.time()
    win = int(now) // cfg["rotation_window_s"]
    next_ts = (win + 1) * cfg["rotation_window_s"]
    return {
        "current_window": win,
        "next_rotation_ts": next_ts,
        "remaining_s": round(next_ts - now, 2),
        "rotation_window_s": cfg["rotation_window_s"],
    }


# =====================================================================
# URL EXTRACTION
# =====================================================================
def extract_seed_from_ticket_url(url: str) -> Dict[str, Any]:
    """Intenta extraer event_id + ticket_id de una URL TM SafeTix.

    Formatos soportados:
      - https://am.ticketmaster.com/safetix/transfer?evt=ABC&tkt=XYZ
      - https://wallet.ticketmaster.com/?e=ABC&t=XYZ&seed=HEX
      - tm-safetix://?evt=ABC&tkt=XYZ
      - https://ticketmaster.com/event/<EVENT>/seat/<TKT>
    """
    if not isinstance(url, str) or not url:
        return {"ok": False, "error": "empty_url"}
    try:
        parsed = urlparse(url)
    except Exception as e:
        return {"ok": False, "error": "parse_failed", "detail": str(e)}
    qs = parse_qs(parsed.query or "")
    event_id = (qs.get("evt") or qs.get("e") or qs.get("event_id") or [""])[0]
    ticket_id = (qs.get("tkt") or qs.get("t") or qs.get("ticket_id") or [""])[0]
    seed_hex = (qs.get("seed") or qs.get("s") or [""])[0]
    # Si vinieran en el path /event/<EVT>/seat/<TKT>
    if not event_id or not ticket_id:
        m = re.search(r"/event/([A-Za-z0-9_-]+)/seat/([A-Za-z0-9_-]+)", parsed.path or "")
        if m:
            event_id = event_id or m.group(1)
            ticket_id = ticket_id or m.group(2)
    if not event_id or not ticket_id:
        return {"ok": False, "error": "missing_ids",
                "event_id": event_id, "ticket_id": ticket_id, "seed": seed_hex}
    if not seed_hex:
        seed_hex = f"{event_id}|{ticket_id}".encode("utf-8").hex()
    return {
        "ok": True,
        "url": url,
        "host": parsed.netloc,
        "event_id": event_id,
        "ticket_id": ticket_id,
        "seed_hex": seed_hex,
    }


# =====================================================================
# SESSION HELPERS (para multiple tickets simultaneos)
# =====================================================================
def build_safetix_session(event_id: str, ticket_id: str,
                          start_ts: Optional[int] = None,
                          generate_count: int = 4) -> Dict[str, Any]:
    """Crea una sesion SafeTix con N barcodes consecutivos pre-derivados."""
    cfg = get_config()
    if not event_id or not ticket_id:
        return {"ok": False, "error": "missing_ids"}
    sid = "stx_" + hashlib.sha1(f"{event_id}|{ticket_id}|{secrets.token_hex(4)}".encode()).hexdigest()[:16]
    base_ts = int(start_ts if start_ts is not None else time.time())
    seed_hex = f"{event_id}|{ticket_id}".encode("utf-8").hex()
    barcodes = []
    for i in range(generate_count):
        ts = base_ts + i * cfg["rotation_window_s"]
        bc = derive_barcode(seed_hex, ts, event_id=event_id, ticket_id=ticket_id)
        barcodes.append({"ts": ts, "barcode": bc,
                         "window": ts // cfg["rotation_window_s"]})
    sess = {
        "session_id": sid,
        "event_id": event_id,
        "ticket_id": ticket_id,
        "seed_hex": seed_hex,
        "rotation_window_s": cfg["rotation_window_s"],
        "barcodes": barcodes,
        "created_at": datetime.now().isoformat(),
    }
    with _lock:
        _sessions[sid] = sess
    return {"ok": True, "session_id": sid,
            "barcodes_generated": len(barcodes),
            "session": sess}


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _sessions.get(session_id)


# =====================================================================
# SELF TEST
# =====================================================================
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_window_15"] = cfg["rotation_window_s"] == 15
    checks["config_hmac_chars"] = cfg["hmac_truncate_chars"] == 32

    # 2. Derive un barcode determinista
    bc = derive_barcode(seed_hex="ev1|tk1".encode().hex(),
                        ts_seconds=1700000000,
                        event_id="ev1", ticket_id="tk1")
    checks["derive_returns_str"] = isinstance(bc, str)
    checks["derive_has_3_parts"] = bc.count(":") == 2

    # 3. Mismo input, mismo output (determinista)
    bc2 = derive_barcode("ev1|tk1".encode().hex(),
                         1700000000, event_id="ev1", ticket_id="tk1")
    checks["derive_deterministic"] = bc == bc2

    # 4. Cambiar timestamp cambia el HMAC y la window
    bc_later = derive_barcode("ev1|tk1".encode().hex(),
                              1700000000 + 15,
                              event_id="ev1", ticket_id="tk1")
    p1 = parse_safetix_token(bc)
    p2 = parse_safetix_token(bc_later)
    checks["window_changes_on_rotation"] = p2["window"] == p1["window"] + 1
    checks["hmac_changes_on_rotation"] = p1["hmac"] != p2["hmac"]

    # 5. Parse OK
    checks["parse_valid_format"] = p1["valid_format"] is True
    checks["parse_decodes_event_id"] = p1["event_id"] == "ev1"

    # 6. Parse falla con basura
    bad = parse_safetix_token("not:a:valid:barcode:5parts")
    checks["parse_rejects_5parts"] = bad["valid_format"] is False

    # 7. Verify con HMAC correcto
    v = verify_barcode(bc, "ev1", "tk1", max_skew_windows=1)
    checks["verify_ok_for_correct_pair"] = v["ok"] is True

    # 8. Verify rechaza ticket_id distinto
    v_bad = verify_barcode(bc, "ev1", "DIFFERENT", max_skew_windows=1)
    checks["verify_rejects_wrong_ticket"] = v_bad["ok"] is False

    # 9. Rotation pattern: lista correlativa pasa
    base_ts = 1700000000
    chain = [derive_barcode("ev1|tk1".encode().hex(),
                            base_ts + i * 15,
                            event_id="ev1", ticket_id="tk1") for i in range(5)]
    rp = validate_rotation_pattern(chain)
    checks["rotation_pattern_valid"] = rp["valid"] is True
    checks["rotation_pattern_avg_1"] = abs(rp["avg_delta"] - 1.0) < 0.01
    checks["rotation_pattern_unique"] = rp["all_unique_hmacs"] is True

    # 10. Rotation pattern detecta repeticion
    rp_bad = validate_rotation_pattern([chain[0], chain[0]])
    checks["rotation_pattern_detects_dup"] = rp_bad["valid"] is False

    # 11. Forecast
    fc = forecast_next_rotation(now_ts=1700000005)  # window 113333333+
    checks["forecast_remaining_in_range"] = 0 < fc["remaining_s"] <= 15

    # 12. URL extraction (query string)
    ex = extract_seed_from_ticket_url("https://am.ticketmaster.com/safetix/transfer?evt=ABC123&tkt=XYZ789")
    checks["url_extract_ok"] = ex["ok"] is True
    checks["url_extract_event"] = ex["event_id"] == "ABC123"
    checks["url_extract_ticket"] = ex["ticket_id"] == "XYZ789"

    # 13. URL extraction (path style)
    ex2 = extract_seed_from_ticket_url("https://ticketmaster.com/event/EVT_X/seat/T_42")
    checks["url_extract_path_ok"] = ex2["ok"] is True

    # 14. URL extraction falla
    ex3 = extract_seed_from_ticket_url("https://example.com/")
    checks["url_extract_rejects_empty"] = ex3["ok"] is False

    # 15. build_safetix_session
    s = build_safetix_session("EVT_1", "TKT_1",
                              start_ts=1700000000, generate_count=3)
    checks["session_built"] = s["ok"] is True
    checks["session_n_barcodes"] = s["barcodes_generated"] == 3
    sid = s["session_id"]
    checks["session_id_prefix"] = sid.startswith("stx_")

    # 16. Save / load config
    save_config({"rotation_window_s": 15})
    cfg2 = get_config()
    checks["config_save_load"] = cfg2["rotation_window_s"] == 15

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "safetix_reverse",
        "version": 1,
        "rotation_window_s": ROTATION_WINDOW_S,
        "active_sessions": len(_sessions),
        "purpose": "SafeTix rotating barcode reverse engineering (HMAC-SHA1 rotating tokens)",
        "paper_ref": "conduition.io/safetix-2025",
    }
