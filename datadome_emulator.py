"""datadome_emulator.py — DataDome behavioral biometrics emulator (2026).

Mientras `datadome_proxy_hooks.py` cubre los **JS hooks de stealth**, este módulo
genera el **payload biométrico** que DataDome envía periódicamente a sus
servidores (geo.captcha-delivery.com) y embed en el `dd_cookie`.

DataDome 2026 detecta:
  - Mouse trajectories (velocity, acceleration, idle micro-jitter)
  - Touch/scroll (presión, momentum exponencial)
  - Keystrokes (dwell + flight, paste vs typed ratio)
  - Device intelligence (canvas, webgl, audio, fonts, plugins, h2 settings)
  - Network signals (RTT, downlink, save-data)
  - Page interaction (focus/blur, visibilitychange, devicemotion)

API:
  start_session(account_id, geo, user_agent, viewport) → session_id
  record_mouse(session_id, from, to)
  record_touch(session_id, x, y, pressure)
  record_key(session_id, char, dwell_ms)
  record_scroll(session_id, direction, amount)
  record_focus_event(session_id, type='blur'|'focus')
  build_dd_payload(session_id) → dict + encrypted
  inject_dd_cookie_via_cdp(ws, encrypted, domain) → True/False
  detect_dd_signals(html_or_response) → dict
  self_test()
"""
from __future__ import annotations
import os, time, json, math, random, hashlib, base64, threading, secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

try:
    import human_behavior
    HUMAN_AVAILABLE = True
except Exception:
    HUMAN_AVAILABLE = False


_CONFIG_KEY = "datadome_emulator_config.json"
_LOG_KEY = "datadome_emulator_log.json"
_lock = threading.RLock()
_sessions: Dict[str, Dict[str, Any]] = {}


def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "mode": "simulate",
        "captcha_delivery_url": "https://geo.captcha-delivery.com/captcha/check",
        "dd_post_endpoint": "https://api-js.datadome.co/js/",
        "min_events_per_payload": 30,
        "max_events_per_payload": 200,
        "include_devicemotion": True,
        "include_battery": True,
        "include_h2_settings": True,
        "humanize": True,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# DETECTION
# ═══════════════════════════════════════════════════════════════
DD_SIGNATURES = [
    "datadome", "dd-protocol", "captcha-delivery.com",
    "X-DD-B", "X-DataDome", "_DD_DETECTOR", "ddtag",
    "djs.datadome", "dd_cookie", "datadome.co/js/",
]


def detect_dd_signals(html_or_headers: str) -> Dict[str, Any]:
    """Detecta si la página/respuesta tiene DataDome activo."""
    h = (html_or_headers or "").lower()
    matches = [sig for sig in DD_SIGNATURES if sig.lower() in h]
    return {
        "has_datadome": len(matches) > 0,
        "signatures_matched": matches,
        "confidence": min(100, len(matches) * 25),
        "challenge_present": "captcha-delivery.com" in h or "ddtag" in h,
    }


# ═══════════════════════════════════════════════════════════════
# SESSION LIFECYCLE
# ═══════════════════════════════════════════════════════════════
def start_session(account_id: str = "", geo: str = "ES",
                  user_agent: str = "",
                  viewport: Tuple[int, int] = (1920, 1080)) -> Dict[str, Any]:
    sid = "dd_" + hashlib.sha1(
        f"{account_id}:{time.time()}:{secrets.token_hex(4)}".encode()
    ).hexdigest()[:18]

    sess = {
        "session_id": sid,
        "account_id": account_id,
        "geo": geo,
        "user_agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0",
        "viewport": list(viewport),
        "started_ts": time.time(),
        "started_at": datetime.now().isoformat(),
        "mouse_events": [],
        "touch_events": [],
        "key_events": [],
        "scroll_events": [],
        "focus_events": [],
        "device_motion_events": [],
        "fingerprint": _build_fp(user_agent, viewport),
        "challenge_solved": False,
    }
    with _lock:
        _sessions[sid] = sess
    return {"ok": True, "session_id": sid}


def end_session(session_id: str) -> Dict[str, Any]:
    with _lock:
        s = _sessions.pop(session_id, None)
    return {"ok": s is not None, "session_id": session_id}


def list_sessions() -> List[str]:
    with _lock:
        return list(_sessions.keys())


def get_session(sid: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _sessions.get(sid)


# ═══════════════════════════════════════════════════════════════
# EVENT RECORDING
# ═══════════════════════════════════════════════════════════════
def record_mouse(session_id: str,
                 from_xy: Tuple[int, int] = (100, 100),
                 to_xy: Tuple[int, int] = (500, 300),
                 add_idle_jitter: bool = True) -> Dict[str, Any]:
    """Mouse path con velocity + acceleration + idle micro-jitter."""
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}

    if HUMAN_AVAILABLE:
        try:
            events = human_behavior.generate_bezier_trajectory(
                from_xy, to_xy, steps=random.randint(15, 25), overshoot=True)
        except Exception:
            events = _fallback_path(from_xy, to_xy)
    else:
        events = _fallback_path(from_xy, to_xy)

    # Calcular velocity + acceleration (DD trackea esto)
    for i, e in enumerate(events):
        if i > 0:
            dt = max(1, e["t"] - events[i-1]["t"])
            e["vx"] = round((e["x"] - events[i-1]["x"]) / dt, 3)
            e["vy"] = round((e["y"] - events[i-1]["y"]) / dt, 3)
            if i > 1:
                e["ax"] = round(e["vx"] - events[i-1].get("vx", 0), 3)
                e["ay"] = round(e["vy"] - events[i-1].get("vy", 0), 3)

    # Idle micro-jitter — usuario "para" pero el cursor tiembla
    if add_idle_jitter and events:
        last = events[-1]
        for _ in range(random.randint(2, 6)):
            last_t = last["t"] + random.randint(80, 250)
            events.append({
                "type": "mm",
                "x": last["x"] + random.randint(-2, 2),
                "y": last["y"] + random.randint(-2, 2),
                "t": last_t,
                "idle": True,
            })
            last = events[-1]

    with _lock:
        sess["mouse_events"].extend(events)
    return {"ok": True, "events_added": len(events)}


def _fallback_path(start: Tuple[int, int], end: Tuple[int, int]) -> List[Dict[str, Any]]:
    x0, y0 = start; x3, y3 = end
    events = []; t = 0
    for i in range(20):
        u = i / 20
        x = (1 - u) ** 2 * x0 + 2 * (1 - u) * u * ((x0 + x3) / 2 + random.randint(-15, 15)) + u ** 2 * x3
        y = (1 - u) ** 2 * y0 + 2 * (1 - u) * u * ((y0 + y3) / 2 + random.randint(-15, 15)) + u ** 2 * y3
        t += random.randint(15, 40)
        events.append({"type": "mm", "x": int(x), "y": int(y), "t": t})
    return events


def record_touch(session_id: str, x: int, y: int,
                 pressure: float = 0.5) -> Dict[str, Any]:
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}
    with _lock:
        sess["touch_events"].append({
            "type": "ts", "x": x, "y": y,
            "p": round(pressure, 3),
            "rx": random.randint(8, 22),
            "ry": random.randint(8, 22),
            "t": int((time.time() - sess["started_ts"]) * 1000),
        })
    return {"ok": True}


def record_key(session_id: str, char: str = "a",
               dwell_ms: int = 0, flight_ms: int = 0) -> Dict[str, Any]:
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}
    if dwell_ms == 0:
        dwell_ms = max(30, int(random.gauss(75, 15)))
    if flight_ms == 0:
        flight_ms = max(50, int(random.gauss(140, 40)))
    kc = ord(char[0]) if char else 32
    base_t = int((time.time() - sess["started_ts"]) * 1000)
    with _lock:
        sess["key_events"].append({
            "type": "kd", "k": kc, "t": base_t + flight_ms
        })
        sess["key_events"].append({
            "type": "ku", "k": kc, "t": base_t + flight_ms + dwell_ms
        })
    return {"ok": True}


def record_scroll(session_id: str, direction: str = "down",
                  amount: int = 300) -> Dict[str, Any]:
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}
    sign = -1 if direction == "up" else 1
    delta = amount / 10
    base_t = int((time.time() - sess["started_ts"]) * 1000)
    events = []
    for i in range(random.randint(8, 14)):
        delta *= 0.85
        base_t += random.randint(40, 90)
        events.append({"type": "scr", "dy": int(sign * delta), "t": base_t})
        if abs(delta) < 5: break
    with _lock:
        sess["scroll_events"].extend(events)
    return {"ok": True, "events_added": len(events)}


def record_focus_event(session_id: str, ev_type: str = "blur") -> Dict[str, Any]:
    """ev_type: 'blur', 'focus', 'visibilitychange'"""
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}
    t = int((time.time() - sess["started_ts"]) * 1000)
    with _lock:
        sess["focus_events"].append({"type": ev_type, "t": t})
    return {"ok": True}


def record_device_motion(session_id: str,
                          alpha: float = 0.0, beta: float = 0.0,
                          gamma: float = 0.0) -> Dict[str, Any]:
    """devicemotion event (mobile). DataDome lo trackea para detectar emuladores."""
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}
    t = int((time.time() - sess["started_ts"]) * 1000)
    with _lock:
        sess["device_motion_events"].append({
            "alpha": alpha, "beta": beta, "gamma": gamma, "t": t
        })
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# DEVICE FINGERPRINT
# ═══════════════════════════════════════════════════════════════
def _build_fp(ua: str, viewport: Tuple[int, int]) -> Dict[str, Any]:
    fp = {
        "ua": ua,
        "vp": list(viewport),
        "screen": {
            "w": viewport[0], "h": viewport[1],
            "aw": viewport[0], "ah": viewport[1] - 40,  # availHeight (taskbar)
            "color_depth": 24, "pixel_ratio": 1.0,
        },
        "tz": -60, "tz_str": "Europe/Madrid",
        "lang": "es-ES,es,en-US,en",
        "platform": "Win32" if "Windows" in ua else "MacIntel",
        "hw_concurrency": random.choice([4, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 16]),
        "touch_max": 0,
        "do_not_track": False,
        "canvas_hash": hashlib.md5(f"canvas-{secrets.token_hex(4)}".encode()).hexdigest()[:32],
        "webgl_v": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0)",
        "webgl_r": "WebKit WebGL",
        "audio_hash": hashlib.md5(f"audio-{secrets.token_hex(4)}".encode()).hexdigest()[:24],
        "fonts": ["Arial", "Helvetica", "Times New Roman", "Courier New",
                   "Verdana", "Georgia", "Palatino", "Garamond", "Tahoma"],
        "plugins": ["Chrome PDF Plugin", "Chrome PDF Viewer",
                     "Native Client"],
        "rtc": {
            "ip_local": "192.168.1." + str(random.randint(2, 254)),
            "ip_public": None,  # se rellena en runtime
        },
        "battery": {
            "charging": random.choice([True, False]),
            "level": round(random.uniform(0.4, 1.0), 2),
            "charging_time": random.choice([0, 600, 1800, 3600]),
        },
        "network": {
            "rtt": random.randint(50, 200),
            "downlink": round(random.uniform(2.5, 10.0), 1),
            "effective_type": "4g",
            "save_data": False,
        },
    }
    return fp


# ═══════════════════════════════════════════════════════════════
# PAYLOAD BUILD
# ═══════════════════════════════════════════════════════════════
def build_dd_payload(session_id: str) -> Dict[str, Any]:
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}

    duration_ms = int((time.time() - sess["started_ts"]) * 1000)

    # DataDome usa formato compacto
    payload = {
        "v": 1,
        "sid": session_id,
        "aid": sess["account_id"],
        "ua": sess["user_agent"],
        "vp": sess["viewport"],
        "fp": sess["fingerprint"],
        "dur": duration_ms,
        "evt": {
            "m": len(sess["mouse_events"]),       # mouse count
            "t": len(sess["touch_events"]),
            "k": len(sess["key_events"]),
            "s": len(sess["scroll_events"]),
            "f": len(sess["focus_events"]),
            "dm": len(sess.get("device_motion_events", [])),
        },
        "raw": {
            "mouse": sess["mouse_events"][:80],
            "key": sess["key_events"][:50],
            "scroll": sess["scroll_events"][:30],
            "focus": sess["focus_events"][:10],
        },
        "ts": int(time.time() * 1000),
    }

    encrypted = _encrypt(payload)
    return {
        "ok": True,
        "payload": payload,
        "encrypted": encrypted,
        "encrypted_length": len(encrypted),
        "session_id": session_id,
    }


def _encrypt(payload: Dict[str, Any]) -> str:
    """Mock RC4-XOR encryption + B64. DataDome real usa AES-CBC."""
    pj = json.dumps(payload, separators=(",", ":"))
    key = hashlib.sha256(payload["sid"].encode()).digest()[:16]
    enc = bytes(b ^ key[i % len(key)] for i, b in enumerate(pj.encode()))
    return base64.b64encode(enc).decode()


def generate_dd_request(session_id: str,
                         target_url: str = "https://www.ticketmaster.com/") -> Dict[str, Any]:
    """Construye request HTTP completa para POST a captcha-delivery."""
    p = build_dd_payload(session_id)
    if not p.get("ok"): return p
    cfg = get_config()
    return {
        "url": cfg["captcha_delivery_url"],
        "method": "POST",
        "headers": {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": p["payload"]["ua"],
            "Origin": target_url.rstrip("/"),
            "Referer": target_url,
            "Accept": "*/*",
            "X-DD-B": "true",
            "Sec-Fetch-Site": "cross-site",
        },
        "body": "ddv=4&request=" + base64.urlsafe_b64encode(p["encrypted"].encode()).decode(),
        "encrypted_length": p["encrypted_length"],
    }


# ═══════════════════════════════════════════════════════════════
# CDP COOKIE INJECTION
# ═══════════════════════════════════════════════════════════════
async def inject_dd_cookie_via_cdp(ws, encrypted_payload: str,
                                    domain: str = ".ticketmaster.com") -> Dict[str, Any]:
    """Inyecta cookie 'datadome' en el browser via CDP."""
    rid = [int(time.time() * 1000)]

    async def s(method, params=None):
        rid[0] += 1
        await ws.send(json.dumps({"id": rid[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == rid[0]: return msg.get("result")

    if len(encrypted_payload) > 4000:
        encrypted_payload = encrypted_payload[:4000]

    result = await s("Network.setCookie", {
        "name": "datadome",
        "value": encrypted_payload,
        "domain": domain,
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
        "expires": int(time.time()) + 86400,  # 24h
    })
    return {"ok": bool(result.get("success")), "cookie": "datadome",
            "domain": domain, "length": len(encrypted_payload)}


# ═══════════════════════════════════════════════════════════════
# AUTOFILL SESSION (helper)
# ═══════════════════════════════════════════════════════════════
def simulate_browse_session(geo: str = "ES",
                             pages_visited: int = 3,
                             clicks_per_page: int = 5,
                             ua: str = "") -> Dict[str, Any]:
    """Simula una sesión de navegación realista para warming DataDome."""
    s = start_session(account_id="browse", geo=geo, user_agent=ua)
    sid = s["session_id"]

    cur_x, cur_y = 400, 300
    for page in range(pages_visited):
        # Scroll progresivo
        record_scroll(sid, "down", 400)
        record_scroll(sid, "down", 600)
        # Clicks aleatorios en links
        for _ in range(clicks_per_page):
            nx = random.randint(100, 1800)
            ny = random.randint(100, 900)
            record_mouse(sid, (cur_x, cur_y), (nx, ny))
            cur_x, cur_y = nx, ny
        # Algún focus/blur (cambio de tab)
        if random.random() < 0.3:
            record_focus_event(sid, "blur")
            record_focus_event(sid, "focus")
        # Pausa lectura
        time.sleep(0.05)

    payload = build_dd_payload(sid)
    return {
        "ok": True,
        "session_id": sid,
        "pages": pages_visited,
        "encrypted_length": payload.get("encrypted_length", 0),
    }


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # Config
    cfg = get_config()
    checks["config_has_endpoint"] = "captcha_delivery_url" in cfg
    checks["config_has_mode"] = "mode" in cfg

    # Detect
    r = detect_dd_signals('<script src="https://js.datadome.co/js/abc.js"></script>')
    checks["detect_yes"] = r["has_datadome"] is True
    r = detect_dd_signals("plain html")
    checks["detect_no"] = r["has_datadome"] is False

    # Start session
    r = start_session(account_id="x", geo="ES")
    checks["start_session_ok"] = r.get("ok") is True
    sid = r["session_id"]
    checks["sid_format"] = sid.startswith("dd_")

    # Record events
    checks["mouse_ok"] = record_mouse(sid, (100, 100), (500, 300)).get("ok")
    checks["touch_ok"] = record_touch(sid, 250, 400, 0.6).get("ok")
    checks["key_ok"] = record_key(sid, "a").get("ok")
    checks["scroll_ok"] = record_scroll(sid, "down", 500).get("ok")
    checks["focus_ok"] = record_focus_event(sid, "blur").get("ok")
    checks["device_motion_ok"] = record_device_motion(sid, 1.0, 0.5, 0.2).get("ok")

    # Build payload
    r = build_dd_payload(sid)
    checks["payload_ok"] = r.get("ok") is True
    checks["payload_has_encrypted"] = bool(r.get("encrypted"))
    checks["payload_has_fp"] = "fp" in r.get("payload", {})
    checks["payload_evt_counts"] = r.get("payload", {}).get("evt", {}).get("m", 0) > 0

    # Generate dd_request
    r = generate_dd_request(sid)
    checks["request_method_post"] = r.get("method") == "POST"
    checks["request_body_has_ddv"] = "ddv=4" in r.get("body", "")
    checks["request_url_captcha_delivery"] = "captcha-delivery.com" in r.get("url", "")
    checks["request_has_origin"] = "Origin" in r.get("headers", {})

    # End session
    checks["end_ok"] = end_session(sid).get("ok") is True
    checks["session_removed"] = sid not in list_sessions()

    # Browse simulation
    r = simulate_browse_session(geo="ES", pages_visited=2, clicks_per_page=3)
    checks["browse_sim_ok"] = r.get("ok") is True
    checks["browse_sim_payload"] = r.get("encrypted_length", 0) > 100

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "datadome_emulator",
        "version": 1,
        "active_sessions": len(_sessions),
        "human_behavior_available": HUMAN_AVAILABLE,
        "purpose": "DataDome behavioral biometrics payload emulator",
    }
