"""akamai_sensor_v3.py — Akamai Bot Manager v3.1 sensor_data custom builder.

Implementa la generación de payload `sensor_data` con el formato real v3 de
Akamai (2024-2026), evolucionado de bm.js / akam.js.

Diferencias vs `akamai_bmsc.py` (que apunta v2):
  - Header: "3;NUM;HASH;DATA;SIG" en vez de "2;..."
  - Soporta v3.1 con bp_score (browser proxy detection)
  - 32 campos extra obligatorios (audio_fp, webgl_renderer_v2, dynamic_props)
  - HTTP/2 SETTINGS frame fingerprint incluido
  - Telemetría incluye sensor_id rotativo (cada 60s no cada 30s)

Patrón inspirado en bm.js v3.7 deobfuscated del CDN Akamai (2025-Q1).

API:
  build_v3_payload(session_id, ua, viewport, ...) → dict completo
  encode_v3_sensor(payload) → str listo para POST a /_bm_/...
  detect_v3(html) → bool (heuristics: bm.js v3 vs v2)
  rotate_sensor_id(session_id) → str (genera nuevo cada ttl)
  generate_telemetry_post(sensor_str) → dict {url, body, headers}
  validate_structure(sensor_str) → bool
  self_test()
"""
from __future__ import annotations
import os, time, json, math, random, hashlib, base64, threading, struct, secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

try:
    import human_behavior
    HUMAN_AVAILABLE = True
except Exception:
    HUMAN_AVAILABLE = False


_CONFIG_KEY = "akamai_v3_config.json"
_LOG_KEY = "akamai_v3_log.json"
_SESSION_KEY = "akamai_v3_sessions.json"
_lock = threading.RLock()
_sensor_cache: Dict[str, Dict[str, Any]] = {}


def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "version": "3.1",
        "events_per_payload": 60,         # más que v2 (era 50)
        "rotation_interval_s": 60,        # v3 rota cada 60s (v2 era 30s)
        # UA Chrome 146 — sincronizado con MoreLogin env-kit/Core/chrome_64_146.0
        # para que el sensor_data POST coincida con el browser real cuando se active live mode.
        "default_ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "default_viewport": [1920, 1080],
        "include_bp_signals": True,       # browser proxy detection (v3.1)
        "include_h2_fingerprint": True,
        "humanize": True,
        "mode": "simulate",               # simulate | live
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# DETECTION DE BM.JS V3 (vs V2)
# ═══════════════════════════════════════════════════════════════
V3_SIGNATURES = [
    "_bm_/v=3", "bmak.startTracking", "bmak.sensor_data",
    "bm-verify", "akam/13/", "_abck=3~",
    "akamai-bot-manager-v3", "bm.flnpd",   # campo nuevo v3.1
]

V2_SIGNATURES = ["_bm_/v=2", "akam/11/", "_abck=2~", "akam/12/"]


def detect_v3(html_or_response: str) -> Dict[str, Any]:
    """Detecta si la página usa Akamai v3 (vs v2 vs no Akamai)."""
    h = (html_or_response or "").lower()
    v3_hits = sum(1 for sig in V3_SIGNATURES if sig.lower() in h)
    v2_hits = sum(1 for sig in V2_SIGNATURES if sig.lower() in h)
    return {
        "version": "v3" if v3_hits >= 1 else ("v2" if v2_hits >= 1 else "none"),
        "v3_signatures_matched": v3_hits,
        "v2_signatures_matched": v2_hits,
        "confidence": min(100, v3_hits * 30 + v2_hits * 20),
    }


# ═══════════════════════════════════════════════════════════════
# EVENT GENERATORS (más rica que v2)
# ═══════════════════════════════════════════════════════════════
def _gen_mouse_events_v3(count: int, viewport: Tuple[int, int]) -> List[Dict[str, Any]]:
    """v3 incluye velocity + acceleration por evento."""
    if HUMAN_AVAILABLE:
        try:
            x0, y0 = random.randint(100, viewport[0] - 100), random.randint(100, viewport[1] - 100)
            x1, y1 = random.randint(100, viewport[0] - 100), random.randint(100, viewport[1] - 100)
            ev = human_behavior.generate_bezier_trajectory((x0, y0), (x1, y1),
                                                            steps=min(count, 30))
            # Add velocity/acceleration per event
            for i, e in enumerate(ev):
                if i > 0:
                    dt = max(1, e["t"] - ev[i-1]["t"])
                    e["vx"] = (e["x"] - ev[i-1]["x"]) / dt
                    e["vy"] = (e["y"] - ev[i-1]["y"]) / dt
                else:
                    e["vx"] = e["vy"] = 0
            return ev
        except Exception:
            pass
    # fallback
    events = []
    w, h = viewport
    x, y = random.randint(100, w - 100), random.randint(100, h - 100)
    t = 0
    for _ in range(count):
        dx, dy = random.randint(-40, 40), random.randint(-25, 25)
        x = max(0, min(w, x + dx)); y = max(0, min(h, y + dy))
        t += random.randint(15, 120)
        events.append({"type": "mm", "x": x, "y": y, "t": t,
                        "vx": dx / 50.0, "vy": dy / 50.0})
    return events


def _gen_key_events_v3(count: int) -> List[Dict[str, Any]]:
    """v3 incluye keyCode + which + repeat flag."""
    events = []; t = 0
    keys = list("abcdefghijklmnopqrstuvwxyz0123456789 ")
    for _ in range(count):
        k = random.choice(keys); kc = ord(k)
        dwell = max(30, int(random.gauss(80, 20)))
        t += max(50, int(random.gauss(150, 50)))
        events.append({"type": "kd", "k": kc, "kc": kc, "wh": kc, "rp": False, "t": t})
        events.append({"type": "ku", "k": kc, "kc": kc, "wh": kc, "t": t + dwell})
    return events


def _gen_touch_events_v3(count: int, viewport: Tuple[int, int]) -> List[Dict[str, Any]]:
    """Touch events con pressure + radius (v3 tracking mobile)."""
    w, h = viewport; events = []; t = 0
    for _ in range(count):
        x, y = random.randint(0, w), random.randint(0, h)
        t += random.randint(40, 200)
        events.append({"type": "ts", "x": x, "y": y, "t": t,
                        "p": round(random.uniform(0.3, 0.9), 2),
                        "rx": random.randint(8, 22), "ry": random.randint(8, 22)})
    return events


def _gen_h2_fingerprint() -> Dict[str, Any]:
    """HTTP/2 SETTINGS frame fingerprint (v3.1 only)."""
    return {
        "settings": {
            "HEADER_TABLE_SIZE": 65536,
            "ENABLE_PUSH": 0,
            "MAX_CONCURRENT_STREAMS": 1000,
            "INITIAL_WINDOW_SIZE": 6291456,
            "MAX_FRAME_SIZE": 16384,
            "MAX_HEADER_LIST_SIZE": 262144,
        },
        "window_update": 15663105,
        "priorities": [
            {"stream_id": 3, "weight": 201, "depends_on": 0},
            {"stream_id": 5, "weight": 101, "depends_on": 0},
            {"stream_id": 7, "weight": 1, "depends_on": 0},
        ],
        "pseudo_header_order": ":method:authority:scheme:path",
    }


def _gen_browser_proxy_signals() -> Dict[str, Any]:
    """v3.1 introduce signals para detectar Patchright/Playwright stealth."""
    return {
        "navigator_webdriver": False,                  # debe ser False
        "navigator_languages_count": random.choice([1, 2, 3]),
        "permissions_query_works": True,
        "chrome_runtime_present": True,
        "plugins_array_consistent": True,
        "iframe_contentwindow_chrome": True,
        "broken_image_height": 16,                     # debe ser 16 en Chromium real
        "broken_image_width": 16,
        "rtt_ms": random.randint(50, 200),
        "downlink_mbps": round(random.uniform(2.5, 10.0), 1),
        "battery_charging": random.choice([True, False]),
        "battery_level": round(random.uniform(0.4, 1.0), 2),
        "memory_estimate_mb": random.choice([4096, 8192, 16384]),
        "function_to_string_native_chrome": True,      # Function.prototype.toString() consistency
    }


def _gen_perf_v3() -> Dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "navigationStart": now,
        "fetchStart": now + random.randint(5, 30),
        "domainLookupStart": now + random.randint(50, 200),
        "domainLookupEnd": now + random.randint(60, 220),
        "connectStart": now + random.randint(70, 240),
        "secureConnectionStart": now + random.randint(100, 300),
        "connectEnd": now + random.randint(150, 350),
        "requestStart": now + random.randint(160, 400),
        "responseStart": now + random.randint(250, 600),
        "responseEnd": now + random.randint(400, 1000),
        "domLoading": now + random.randint(450, 1100),
        "domInteractive": now + random.randint(700, 1400),
        "domContentLoadedEventStart": now + random.randint(900, 1700),
        "domContentLoadedEventEnd": now + random.randint(1000, 2000),
        "loadEventStart": now + random.randint(1500, 2900),
        "loadEventEnd": now + random.randint(1500, 3000),
    }


def _gen_dynamic_props() -> Dict[str, Any]:
    """v3.1 challenge: dynamic JS props que cambian cada call."""
    return {
        "dyn_a": secrets.token_hex(8),
        "dyn_b": int(time.time() * 1000) ^ random.randint(10000, 99999),
        "dyn_c": hashlib.md5(str(random.random()).encode()).hexdigest()[:24],
    }


# ═══════════════════════════════════════════════════════════════
# PAYLOAD BUILD (v3.1)
# ═══════════════════════════════════════════════════════════════
def build_v3_payload(session_id: Optional[str] = None,
                      user_agent: Optional[str] = None,
                      viewport: Optional[Tuple[int, int]] = None,
                      events_count: Optional[int] = None,
                      include_h2: bool = True,
                      include_bp: bool = True) -> Dict[str, Any]:
    """Genera payload v3.1 completo."""
    cfg = get_config()
    ua = user_agent or cfg["default_ua"]
    vp = viewport or tuple(cfg["default_viewport"])
    ec = events_count or cfg["events_per_payload"]
    sid = session_id or "ak3_" + secrets.token_hex(8)

    mouse_n = int(ec * 0.65)
    key_n = int(ec * 0.20)
    touch_n = ec - mouse_n - key_n

    payload = {
        "v": "3.1",
        "sid": sid,
        "ua": ua,
        "vp": list(vp),
        "ts": int(time.time() * 1000),
        "tz_offset": -60,
        "lang": "es-ES,es",
        "scr_dpr": 1.0,
        "color_depth": 24,
        "platform": "Win32" if "Windows" in ua else "MacIntel",
        "hw_concurrency": random.choice([4, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 16]),
        "mouse_events": _gen_mouse_events_v3(mouse_n, vp),
        "key_events": _gen_key_events_v3(key_n),
        "touch_events": _gen_touch_events_v3(touch_n, vp),
        "perf": _gen_perf_v3(),
        "fp": {
            "canvas": hashlib.md5(f"canvas-{random.random()}".encode()).hexdigest()[:24],
            "webgl_v": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0)",
            "webgl_r": "WebKit WebGL",
            "audio": hashlib.md5(f"audio-{random.random()}".encode()).hexdigest()[:24],
            "fonts_n": random.randint(45, 80),
            "plugins_n": random.choice([3, 4, 5]),
        },
        "dyn": _gen_dynamic_props(),
    }

    if include_h2 and cfg.get("include_h2_fingerprint"):
        payload["h2"] = _gen_h2_fingerprint()
    if include_bp and cfg.get("include_bp_signals"):
        payload["bp"] = _gen_browser_proxy_signals()

    with _lock:
        _sensor_cache[sid] = {
            "payload": payload,
            "created_ts": time.time(),
            "rotation_count": 0,
        }
    return payload


# ═══════════════════════════════════════════════════════════════
# ENCODING (formato v3 string)
# ═══════════════════════════════════════════════════════════════
def encode_v3_sensor(payload: Dict[str, Any]) -> str:
    """Codifica el payload al formato v3:
        "3;<length>;<key_hash>;<encrypted_b64>;<sig>;<dyn_token>"

    Diferencias vs v2:
      - Versión "3" en vez de "2"
      - Token dinámico al final (rota cada llamada)
      - Encryption: simulación XOR+B64. Real Akamai usa AES-128-CBC.
    """
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    length = len(payload_json)
    key_hash = hashlib.sha1(payload["sid"].encode()).hexdigest()[:20]
    key_bytes = bytes.fromhex(key_hash[:16])
    encrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(payload_json.encode()))
    payload_b64 = base64.b64encode(encrypted).decode()
    sig = hashlib.sha256((payload_b64 + key_hash).encode()).hexdigest()[:24]
    dyn_token = secrets.token_hex(8)
    return f"3;{length};{key_hash};{payload_b64};{sig};{dyn_token}"


def validate_structure(sensor_str: str) -> Dict[str, Any]:
    """Valida estructura del sensor_data (sin decryption real)."""
    parts = (sensor_str or "").split(";")
    return {
        "valid": len(parts) >= 5 and parts[0] in ("2", "3"),
        "version": parts[0] if parts else "?",
        "length_field": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
        "key_hash_present": len(parts) > 2 and len(parts[2]) >= 16,
        "payload_present": len(parts) > 3 and len(parts[3]) > 100,
        "sig_present": len(parts) > 4 and len(parts[4]) > 0,
        "parts_count": len(parts),
    }


# ═══════════════════════════════════════════════════════════════
# ROTATION (v3 rota cada 60s; el sensor_id se mantiene pero token rota)
# ═══════════════════════════════════════════════════════════════
def rotate_sensor_id(session_id: str) -> Dict[str, Any]:
    """Rota el sensor (mismo session_id, nuevo dyn_token + nuevo timestamp)."""
    with _lock:
        cached = _sensor_cache.get(session_id)
    if not cached:
        return {"ok": False, "error": "session_not_in_cache"}

    cfg = get_config()
    now = time.time()
    if (now - cached["created_ts"]) < cfg["rotation_interval_s"] - 5:
        # Demasiado pronto
        return {"ok": False, "error": "rotation_too_soon",
                "remaining_s": int(cfg["rotation_interval_s"] - (now - cached["created_ts"]))}

    payload = cached["payload"]
    payload["ts"] = int(now * 1000)
    payload["dyn"] = _gen_dynamic_props()
    new_sensor = encode_v3_sensor(payload)

    with _lock:
        cached["created_ts"] = now
        cached["rotation_count"] += 1

    return {"ok": True, "session_id": session_id,
            "rotation_count": cached["rotation_count"],
            "sensor_data": new_sensor,
            "sensor_length": len(new_sensor)}


def list_active_sensors() -> List[Dict[str, Any]]:
    with _lock:
        return [{
            "session_id": sid,
            "age_s": int(time.time() - c["created_ts"]),
            "rotation_count": c["rotation_count"],
        } for sid, c in _sensor_cache.items()]


def clear_sensor(session_id: str) -> Dict[str, Any]:
    with _lock:
        was = _sensor_cache.pop(session_id, None)
    return {"ok": was is not None, "session_id": session_id}


# ═══════════════════════════════════════════════════════════════
# TELEMETRY POST (genera body + headers para POST a /_bm_/...)
# ═══════════════════════════════════════════════════════════════
def generate_telemetry_post(sensor_str: str,
                             page_url: str = "https://www.ticketmaster.com/",
                             user_agent: Optional[str] = None) -> Dict[str, Any]:
    """Construye request HTTP completa para enviar telemetry a Akamai."""
    cfg = get_config()
    ua = user_agent or cfg["default_ua"]
    return {
        "url": page_url.rstrip("/") + "/akam/13/" + secrets.token_hex(4),
        "method": "POST",
        "headers": {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": ua,
            "Accept": "*/*",
            "Origin": page_url.rstrip("/"),
            "Referer": page_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Akamai-BM-Telemetry": "1",
        },
        "body": "sensor_data=" + sensor_str,
    }


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_version_31"] = cfg["version"] == "3.1"
    checks["config_rotation_60s"] = cfg["rotation_interval_s"] == 60

    # 2. Detect
    r = detect_v3('<script src="/_bm_/v=3/akam/13/abc.js"></script>')
    checks["detect_v3_yes"] = r["version"] == "v3"
    r = detect_v3('<script src="/_bm_/v=2/akam/11/abc.js"></script>')
    checks["detect_v2_yes"] = r["version"] == "v2"
    r = detect_v3("plain html no akamai")
    checks["detect_none"] = r["version"] == "none"

    # 3. Build payload
    p = build_v3_payload(events_count=30)
    checks["payload_has_v_31"] = p["v"] == "3.1"
    checks["payload_has_sid"] = "sid" in p
    checks["payload_has_mouse"] = len(p["mouse_events"]) > 0
    checks["payload_has_key"] = len(p["key_events"]) > 0
    checks["payload_has_h2"] = "h2" in p
    checks["payload_has_bp"] = "bp" in p
    checks["payload_has_dyn"] = "dyn" in p

    # 4. Encode
    sensor = encode_v3_sensor(p)
    checks["encode_starts_with_3"] = sensor.startswith("3;")
    checks["encode_has_6_parts"] = len(sensor.split(";")) == 6

    # 5. Validate
    v = validate_structure(sensor)
    checks["validate_valid"] = v["valid"] is True
    checks["validate_version_3"] = v["version"] == "3"

    # 6. Rotation (necesita esperar pero le decimos al cache que pasó tiempo)
    sid = p["sid"]
    with _lock:
        if sid in _sensor_cache:
            _sensor_cache[sid]["created_ts"] = time.time() - 999
    r = rotate_sensor_id(sid)
    checks["rotation_ok"] = r.get("ok") is True
    checks["rotation_count_incremented"] = r.get("rotation_count") == 1

    # 7. Active sensors
    active = list_active_sensors()
    checks["active_sensors_listed"] = len(active) >= 1

    # 8. Clear
    r = clear_sensor(sid)
    checks["clear_ok"] = r.get("ok") is True

    # 9. Generate telemetry POST
    p2 = build_v3_payload(events_count=10)
    s2 = encode_v3_sensor(p2)
    tp = generate_telemetry_post(s2, "https://www.ticketmaster.es/")
    checks["telemetry_url_includes_akam"] = "akam" in tp["url"]
    checks["telemetry_method_post"] = tp["method"] == "POST"
    checks["telemetry_body_has_sensor"] = "sensor_data=" in tp["body"]
    checks["telemetry_has_origin"] = "Origin" in tp["headers"]

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "akamai_sensor_v3",
        "version": 1,
        "config_version": get_config()["version"],
        "active_sensors": len(_sensor_cache),
        "human_behavior_available": HUMAN_AVAILABLE,
        "purpose": "Akamai Bot Manager v3.1 sensor_data builder (custom)",
    }
