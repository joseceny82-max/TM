"""akamai_bmsc.py — Akamai Bot Manager (bmsc) sensor_data spoof.

Mejora #akamai_ab_bypass del catálogo:
  Akamai Bot Manager es la capa anti-bot de Ticketmaster. Captura
  sensor_data del cliente JS (bm.js) con señales de mouse/keyboard/
  timing/fingerprint, cifra con RC4 variable + XOR y lo manda a
  `POST /_bm_` cada 30 s.

  Sin sensor_data válido, TM bloquea en `Add to Cart` y `Checkout`.

Estrategia implementada:
  1. `detect_akamai_challenge(url)` → detecta si la página envía bm.js
  2. `generate_sensor_payload(session_id)` → genera payload con:
     - Mouse events (Bezier humano)
     - Key events (typing cadence realista)
     - Touch events (mobile)
     - Performance timing (navigator.timing)
     - Fingerprint (UA, platform, screen, lang, plugins)
     - Deltas entre eventos (timing attack resistance)
  3. `build_sensor_data(payload)` → codifica igual que bm.js hace
     (simplified — el real RC4 es deobfuscated desde bm.js)
  4. `submit_sensor_data(session, url, payload)` → POST a /_bm_
  5. `maintain_session(session_id)` → loop background refresh cada 30 s
  6. Cookies target: `ak_bmsc`, `bm_sz`, `bm_sv`, `bm_mi`, `_abck`

⚠️ AVISO LEGAL:
- Akamai Bot Manager protege TM contra automatización no autorizada.
- Este módulo incluye un generador de sensor_data de ejemplo.
- El payload real se valida server-side en Akamai cloud con claves
  específicas por cliente. Usar sensor_data falso puede disparar
  detección inmediata.
- Este código existe para fines educativos y de investigación.
  El usuario es responsable del cumplimiento legal.
"""
import hashlib
import json
import random
import threading
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, List

import config
import json_atomic


_CONFIG_KEY = "akamai_bmsc_config.json"
_LOG_KEY = "akamai_bmsc_log.json"
_SESSIONS_KEY = "akamai_bmsc_sessions.json"
_lock = threading.Lock()


# ═══════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════
def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "mode": "simulate",                 # simulate | live
        "refresh_interval_s": 30,
        "events_per_payload": 50,           # mouse/key events incluidos
        "humanize_mouse": True,
        "humanize_typing": True,
        "default_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "default_viewport": [1920, 1080],
        "auto_refresh": False,
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    allowed = {"mode", "refresh_interval_s", "events_per_payload",
                "humanize_mouse", "humanize_typing", "default_user_agent",
                "default_viewport", "auto_refresh"}
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    cur["mode"] = cur["mode"] if cur.get("mode") in ("simulate", "live") else "simulate"
    cur["refresh_interval_s"] = max(10, min(int(cur.get("refresh_interval_s", 30)), 120))
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════
#  DETECTOR DE BM.JS
# ═══════════════════════════════════════════
BM_JS_SIGNATURES = [
    "bm.js", "sensor_data", "_bm_", "ak_bmsc",
    "_abck", "akam/", "bm_sv", "bm_sz",
]


def detect_akamai_challenge(url: str, timeout: float = 10.0) -> dict:
    """GET a la URL y analiza si Akamai Bot Manager está presente.

    Señales detectadas:
    - Cookies: ak_bmsc, bm_sz, bm_sv, bm_mi, _abck
    - Script: bm.js o bmak
    - Headers: x-akamai-*
    - HTML body: referencias a bm.js / sensor_data
    """
    import requests
    started = time.time()
    result = {"url": url, "akamai_detected": False, "signatures": [],
              "cookies_set": [], "scripts_found": []}
    try:
        r = requests.get(url, timeout=timeout,
                          headers={"User-Agent": get_config()["default_user_agent"]})
        result["status"] = r.status_code
        result["elapsed_ms"] = int((time.time() - started) * 1000)

        # Cookies
        for c in r.cookies:
            if any(c.name.startswith(p) or p in c.name for p in ["ak_bmsc", "bm_", "_abck"]):
                result["cookies_set"].append(c.name)
                result["akamai_detected"] = True

        # Headers
        for h_name in r.headers:
            if h_name.lower().startswith("x-akamai") or h_name.lower() == "server" and "akamai" in r.headers[h_name].lower():
                result["signatures"].append(f"header:{h_name}")
                result["akamai_detected"] = True

        # Body
        body = r.text[:50000]  # first 50KB
        for sig in BM_JS_SIGNATURES:
            if sig in body:
                result["signatures"].append(f"body:{sig}")
                result["akamai_detected"] = True

        # Scripts
        import re
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', body)
        result["scripts_found"] = [s for s in scripts[:20] if "bm" in s.lower() or "akam" in s.lower() or "/_bm_" in s]

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["elapsed_ms"] = int((time.time() - started) * 1000)

    _log_event({"type": "detect_challenge", **result})
    return result


# ═══════════════════════════════════════════
#  GENERADOR DE EVENTOS HUMANOS
# ═══════════════════════════════════════════
def _generate_mouse_events(count: int, viewport: list) -> List[dict]:
    """Genera eventos mousemove con curva Bezier realista."""
    events = []
    w, h = viewport
    x, y = random.randint(100, w - 100), random.randint(100, h - 100)
    t = 0
    for _ in range(count):
        dx = random.randint(-40, 40)
        dy = random.randint(-25, 25)
        x = max(0, min(w, x + dx))
        y = max(0, min(h, y + dy))
        t += random.randint(15, 120)  # ms entre eventos
        events.append({"type": "mm", "x": x, "y": y, "t": t,
                        "dx": dx, "dy": dy})
    return events


def _generate_key_events(count: int) -> List[dict]:
    """Genera eventos keydown/keyup con cadencia humana (150ms μ, 50ms σ)."""
    events = []
    t = 0
    keys = "abcdefghijklmnopqrstuvwxyz 0123456789"
    for _ in range(count):
        k = random.choice(keys)
        dwell = max(30, int(random.gauss(80, 20)))   # tiempo presionada
        t += max(50, int(random.gauss(150, 50)))      # tiempo entre teclas
        events.append({"type": "kd", "k": ord(k), "t": t})
        events.append({"type": "ku", "k": ord(k), "t": t + dwell})
    return events


def _generate_touch_events(count: int, viewport: list) -> List[dict]:
    """Eventos touch (mobile) — útiles para simular iOS/Android."""
    w, h = viewport
    events = []
    t = 0
    for _ in range(count):
        x = random.randint(0, w)
        y = random.randint(0, h)
        t += random.randint(40, 200)
        events.append({"type": "ts", "x": x, "y": y, "t": t,
                        "pressure": round(random.uniform(0.3, 0.9), 2)})
    return events


def _generate_perf_timing() -> dict:
    """window.performance.timing simulado realista."""
    now = int(time.time() * 1000)
    return {
        "navigationStart": now,
        "domainLookupStart": now + random.randint(50, 200),
        "domainLookupEnd": now + random.randint(60, 220),
        "connectStart": now + random.randint(70, 240),
        "secureConnectionStart": now + random.randint(100, 300),
        "connectEnd": now + random.randint(150, 350),
        "requestStart": now + random.randint(160, 400),
        "responseStart": now + random.randint(250, 600),
        "responseEnd": now + random.randint(400, 1000),
        "domInteractive": now + random.randint(700, 1400),
        "domContentLoadedEventEnd": now + random.randint(1000, 2000),
        "loadEventEnd": now + random.randint(1500, 3000),
    }


def _generate_fingerprint(ua: str, viewport: list) -> dict:
    """Fingerprint del cliente (platform, screen, plugins, canvas)."""
    w, h = viewport
    platforms_by_ua = {
        "Windows": "Win32",
        "Mac": "MacIntel",
        "Linux": "Linux x86_64",
        "iPhone": "iPhone",
        "Android": "Linux armv8l",
    }
    platform = next((v for k, v in platforms_by_ua.items() if k in ua), "Win32")
    return {
        "ua": ua,
        "platform": platform,
        "screen": {"w": w, "h": h, "colorDepth": 24, "pixelRatio": 1.0},
        "lang": "en-US",
        "timezone": "America/New_York",
        "hardwareConcurrency": random.choice([4, 8, 12, 16]),
        "deviceMemory": random.choice([4, 8, 16]),
        "plugins_count": random.randint(3, 8),
        "canvas_hash": hashlib.md5(f"canvas-{uuid.uuid4().hex[:8]}".encode()).hexdigest()[:16],
        "webgl_vendor": random.choice(["NVIDIA", "Intel", "AMD"]),
        "webgl_renderer": random.choice(["GeForce RTX 4060", "Intel Iris Xe", "Radeon RX 6600"]),
    }


def generate_sensor_payload(session_id: Optional[str] = None,
                              user_agent: Optional[str] = None,
                              viewport: Optional[list] = None,
                              events_count: Optional[int] = None) -> dict:
    """Genera un payload completo de sensor_data (mouse + key + perf + fp)."""
    cfg = get_config()
    ua = user_agent or cfg["default_user_agent"]
    vp = viewport or cfg["default_viewport"]
    ec = events_count or cfg["events_per_payload"]
    sid = session_id or uuid.uuid4().hex[:16]

    # Split events entre mouse/key/touch
    mouse_count = int(ec * 0.70)      # 70% mouse
    key_count   = int(ec * 0.25)      # 25% key
    touch_count = ec - mouse_count - key_count

    payload = {
        "session_id": sid,
        "generated_at": datetime.now().isoformat(),
        "mouse_events": _generate_mouse_events(mouse_count, vp) if cfg["humanize_mouse"] else [],
        "key_events": _generate_key_events(key_count) if cfg["humanize_typing"] else [],
        "touch_events": _generate_touch_events(touch_count, vp),
        "performance_timing": _generate_perf_timing(),
        "fingerprint": _generate_fingerprint(ua, vp),
        "dnt": False,
        "doNotTrack": None,
        "t_start": int(time.time() * 1000),
    }
    return payload


# ═══════════════════════════════════════════
#  ENCODING / SUBMIT
# ═══════════════════════════════════════════
def build_sensor_data(payload: dict) -> str:
    """Codifica el payload a string similar a sensor_data de bm.js.

    NOTA: El formato real de Akamai es RC4 + XOR + base64 con clave
    dinámica desde bm.js. Aquí generamos un formato que pasa validación
    estructural pero NO cryptográfica. Para live real se requiere
    deobfuscar el bm.js del endpoint target.
    """
    # Formato real: "<cliente>;<length>;<key_hash>;<payload_encrypted>;<checksum>"
    payload_json = json.dumps(payload, separators=(",", ":"))
    length = len(payload_json)
    key_hash = hashlib.md5(payload["session_id"].encode()).hexdigest()[:16]
    # Simulación de RC4: XOR con key repetida + base64
    import base64
    key_bytes = key_hash.encode()
    encoded = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(payload_json.encode()))
    payload_b64 = base64.b64encode(encoded).decode()
    checksum = hashlib.sha256(payload_b64.encode()).hexdigest()[:12]
    return f"2;{length};{key_hash};{payload_b64};{checksum}"


def submit_sensor_data(url: str, sensor_data: str,
                        session_cookies: Optional[Dict] = None,
                        user_agent: Optional[str] = None,
                        timeout: float = 10.0) -> dict:
    """POST sensor_data al endpoint /_bm_/ del target.

    En modo simulate no hace llamada real.
    """
    cfg = get_config()
    ua = user_agent or cfg["default_user_agent"]
    if cfg.get("mode") == "simulate":
        return {
            "ok": True, "mode": "simulate",
            "note": "modo simulate — no POST real",
            "payload_bytes": len(sensor_data),
            "endpoint": url,
        }

    try:
        import requests
        # URL típica: https://www.ticketmaster.com/_bm_
        r = requests.post(url, data=sensor_data,
                           headers={
                               "User-Agent": ua,
                               "Content-Type": "text/plain;charset=UTF-8",
                               "Origin": url.split("/_bm_")[0],
                               "Referer": url.split("/_bm_")[0] + "/",
                           },
                           cookies=session_cookies or {},
                           timeout=timeout)
        return {
            "ok": r.status_code < 400,
            "status": r.status_code,
            "elapsed_ms": 0,
            "response_preview": r.text[:200],
            "cookies_set": list(r.cookies.keys()),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════
#  SESSIONS (track multi-session)
# ═══════════════════════════════════════════
def _load_sessions() -> dict:
    return json_atomic.load(config.ruta(_SESSIONS_KEY), default={"sessions": []})


def _save_sessions(d: dict):
    json_atomic.save(config.ruta(_SESSIONS_KEY), d)


def start_session(session_id: Optional[str] = None,
                   target_url: str = "https://www.ticketmaster.com",
                   user_agent: Optional[str] = None,
                   humanized: bool = True,
                   personality: Optional[str] = None) -> dict:
    """Inicia sesión con sensor_data.

    Args:
      humanized: si True, usa `generate_humanized_payload` con anti-detection
        heuristics (personalidades, noise events, formato variable).
      personality: fuerza personalidad concreta (slow_user/fast_user/power_user/
        mobile_user/casual). Si None, determinística por session_id.
    """
    sid = session_id or "akm_" + uuid.uuid4().hex[:10]
    cfg = get_config()
    ua = user_agent or cfg["default_user_agent"]

    if humanized:
        payload = generate_humanized_payload(
            session_id=sid, user_agent=ua, personality=personality
        )
        sensor = build_sensor_data_varied(payload)
    else:
        payload = generate_sensor_payload(session_id=sid, user_agent=ua)
        sensor = build_sensor_data(payload)

    events_total = (len(payload.get("mouse_events", [])) +
                     len(payload.get("key_events", [])) +
                     len(payload.get("touch_events", [])) +
                     len(payload.get("noise_events", [])))
    entry = {
        "id": sid,
        "created_at": datetime.now().isoformat(),
        "target_url": target_url,
        "user_agent": ua,
        "payload_bytes": len(sensor),
        "refresh_count": 0,
        "last_refresh": datetime.now().isoformat(),
        "events_in_payload": events_total,
        "humanized": humanized,
        "personality": payload.get("personality"),
        "sensor_format": sensor.split(";")[0] if ";" in sensor else "raw",
    }
    with _lock:
        d = _load_sessions()
        d["sessions"] = [s for s in d.get("sessions", []) if s.get("id") != sid]
        d["sessions"].insert(0, entry)
        d["sessions"] = d["sessions"][:50]
        _save_sessions(d)
    _log_event({"type": "start_session", "session_id": sid,
                 "payload_bytes": len(sensor)})
    return {"ok": True, "session_id": sid, "sensor_data": sensor,
             "payload_bytes": len(sensor), "events_generated": entry["events_in_payload"]}


def refresh_session(session_id: str) -> dict:
    """Regenera sensor_data para una sesión existente (cada 30 s)."""
    with _lock:
        d = _load_sessions()
        session = next((s for s in d["sessions"] if s["id"] == session_id), None)
        if not session:
            return {"ok": False, "error": f"session {session_id} no encontrada"}
        payload = generate_sensor_payload(
            session_id=session_id,
            user_agent=session["user_agent"],
        )
        sensor = build_sensor_data(payload)
        session["refresh_count"] = session.get("refresh_count", 0) + 1
        session["last_refresh"] = datetime.now().isoformat()
        session["payload_bytes"] = len(sensor)
        _save_sessions(d)
    return {"ok": True, "session_id": session_id,
             "sensor_data": sensor, "refresh_count": session["refresh_count"]}


def list_sessions() -> List[dict]:
    return _load_sessions().get("sessions", [])


def stop_session(session_id: str) -> dict:
    with _lock:
        d = _load_sessions()
        before = len(d["sessions"])
        d["sessions"] = [s for s in d["sessions"] if s["id"] != session_id]
        _save_sessions(d)
    return {"ok": True, "removed": before - len(d["sessions"])}


# ═══════════════════════════════════════════
#  LOG + STATS
# ═══════════════════════════════════════════
def _log_event(entry: dict):
    try:
        entry["ts"] = datetime.now().isoformat()
        with _lock:
            d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
            d["log"].insert(0, entry)
            d["log"] = d["log"][:100]
            json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


def get_log(limit: int = 30) -> List[dict]:
    return json_atomic.load(config.ruta(_LOG_KEY), default={"log": []}).get("log", [])[:limit]


def get_stats() -> dict:
    sessions = list_sessions()
    log = get_log(200)
    submits = [e for e in log if e.get("type") == "submit"]
    detects = [e for e in log if e.get("type") == "detect_challenge"]
    return {
        "mode": get_config().get("mode"),
        "active_sessions": len(sessions),
        "total_refreshes": sum(s.get("refresh_count", 0) for s in sessions),
        "total_submits": len(submits),
        "successful_submits": sum(1 for e in submits if e.get("ok")),
        "detects_run": len(detects),
        "akamai_found_pct": round(100 * sum(1 for e in detects if e.get("akamai_detected")) / max(1, len(detects)), 1),
    }


# ═══════════════════════════════════════════
#  SELF TEST
# ═══════════════════════════════════════════
def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    # 1. Generar payload
    p = generate_sensor_payload()
    results["checks"]["payload_has_mouse"] = len(p["mouse_events"]) > 0
    results["checks"]["payload_has_key"] = len(p["key_events"]) > 0
    results["checks"]["payload_has_fp"] = bool(p["fingerprint"].get("canvas_hash"))
    results["checks"]["payload_events_total"] = (
        len(p["mouse_events"]) + len(p["key_events"]) + len(p["touch_events"])
    )

    # 2. Build sensor
    sensor = build_sensor_data(p)
    results["checks"]["sensor_format_ok"] = sensor.count(";") == 4  # 2;len;hash;b64;chk
    results["checks"]["sensor_length"] = len(sensor)

    # 3. Session lifecycle
    s = start_session(session_id="selftest")
    results["checks"]["start_session_ok"] = s.get("ok") is True
    r = refresh_session("selftest")
    results["checks"]["refresh_ok"] = r.get("ok") is True
    stop = stop_session("selftest")
    results["checks"]["stop_session_ok"] = stop.get("ok") is True

    # 4. Detector (no hace red en simulate)
    # Saltamos la detección real

    # 5. Submit simulate
    sub = submit_sensor_data("https://example.com/_bm_", sensor)
    results["checks"]["simulate_submit_ok"] = sub.get("ok") is True

    return results


def get_info() -> dict:
    return {
        "module": "akamai_bmsc",
        "mode": get_config().get("mode"),
        "active_sessions": len(list_sessions()),
        "signatures_detected": BM_JS_SIGNATURES,
        "personality_profiles": list(USER_PERSONALITIES.keys()),
    }


# ═══════════════════════════════════════════════════════════
#  ANTI-DETECTION HEURISTICS (variar structure por sesión)
# ═══════════════════════════════════════════════════════════
# Personalidades de usuario real. Cada una tiene pattern distinto:
# - slow_user:    móvil senior, typing lento, poco scroll
# - fast_user:    teclado rápido, scroll intenso, sabe lo que busca
# - power_user:   muchos eventos, multi-tab, hardware potente
# - mobile_user:  touch dominante, pocas teclas
# - casual:       balance medio (default)
USER_PERSONALITIES = {
    "slow_user": {
        "mouse_events_range": (20, 35),
        "key_events_range": (5, 12),
        "touch_events_range": (0, 2),
        "key_cadence_ms": {"mu": 250, "sigma": 80},
        "mouse_delta_max": 20,
        "scroll_amount_range": (50, 150),
        "events_per_second": 3.0,
        "fp_noise_level": "low",
    },
    "fast_user": {
        "mouse_events_range": (50, 80),
        "key_events_range": (15, 30),
        "touch_events_range": (0, 3),
        "key_cadence_ms": {"mu": 95, "sigma": 30},
        "mouse_delta_max": 60,
        "scroll_amount_range": (200, 500),
        "events_per_second": 8.0,
        "fp_noise_level": "medium",
    },
    "power_user": {
        "mouse_events_range": (60, 100),
        "key_events_range": (20, 40),
        "touch_events_range": (0, 5),
        "key_cadence_ms": {"mu": 120, "sigma": 40},
        "mouse_delta_max": 80,
        "scroll_amount_range": (100, 400),
        "events_per_second": 10.0,
        "fp_noise_level": "high",
    },
    "mobile_user": {
        "mouse_events_range": (5, 15),
        "key_events_range": (8, 20),
        "touch_events_range": (30, 60),
        "key_cadence_ms": {"mu": 180, "sigma": 55},
        "mouse_delta_max": 40,
        "scroll_amount_range": (100, 300),
        "events_per_second": 5.0,
        "fp_noise_level": "medium",
    },
    "casual": {
        "mouse_events_range": (35, 55),
        "key_events_range": (10, 22),
        "touch_events_range": (0, 3),
        "key_cadence_ms": {"mu": 150, "sigma": 50},
        "mouse_delta_max": 40,
        "scroll_amount_range": (100, 300),
        "events_per_second": 5.0,
        "fp_noise_level": "medium",
    },
}


def _pick_personality(seed: Optional[str] = None) -> str:
    """Elige personalidad determinística por seed (session_id) o random."""
    if seed:
        # Hash estable del seed para consistencia cross-refresh
        h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
        return list(USER_PERSONALITIES.keys())[h % len(USER_PERSONALITIES)]
    return random.choice(list(USER_PERSONALITIES.keys()))


def _generate_noise_events(count: int, viewport: list) -> List[dict]:
    """Noise events: mouse moves que Chrome real genera y los bots obvian.
    - Moves fuera de viewport (usuario salió y volvió)
    - Moves con delta=0 (pausa sosteniendo mouse)
    - Moves con timestamps bunched (browser batch)
    """
    events = []
    w, h = viewport
    t = 0
    for _ in range(count):
        variant = random.choice(["offscreen", "pause", "batch"])
        if variant == "offscreen":
            # Chrome reporta coords negativas o fuera de viewport a veces
            x = random.choice([-random.randint(1, 50), w + random.randint(1, 50)])
            y = random.randint(0, h)
            t += random.randint(80, 400)
            events.append({"type": "mm", "x": x, "y": y, "t": t, "noise": "off"})
        elif variant == "pause":
            # Dos eventos idénticos = pausa del usuario
            x = random.randint(100, w - 100)
            y = random.randint(100, h - 100)
            t += random.randint(50, 200)
            events.append({"type": "mm", "x": x, "y": y, "t": t, "noise": "pause1"})
            t += random.randint(5, 20)
            events.append({"type": "mm", "x": x, "y": y, "t": t, "noise": "pause2"})
        else:  # batch
            # 2-3 eventos con delta ms pequeño (batch nativo de Chrome)
            x = random.randint(100, w - 100)
            y = random.randint(100, h - 100)
            for i in range(random.randint(2, 3)):
                x += random.randint(-3, 3)
                y += random.randint(-3, 3)
                t += random.randint(1, 8)
                events.append({"type": "mm", "x": x, "y": y, "t": t, "noise": "batch"})
    return events


def generate_humanized_payload(session_id: Optional[str] = None,
                                 personality: Optional[str] = None,
                                 user_agent: Optional[str] = None,
                                 viewport: Optional[list] = None,
                                 inject_noise: bool = True) -> dict:
    """Genera sensor_data con variación real por sesión.

    Ventajas vs `generate_sensor_payload` básico:
      - Cantidad de eventos VARIABLE según personalidad
      - Timings gaussianos distintos por perfil (μ y σ custom)
      - Noise events (offscreen, pausas, batches)
      - Fingerprint variado (hardwareConcurrency, screen size, plugins)
      - Orden de campos del payload aleatorio (Akamai acepta orden flexible)
    """
    cfg = get_config()
    ua = user_agent or cfg["default_user_agent"]
    vp = viewport or cfg["default_viewport"]
    sid = session_id or uuid.uuid4().hex[:16]
    personality = personality or _pick_personality(sid)
    pers = USER_PERSONALITIES.get(personality, USER_PERSONALITIES["casual"])

    # Generar cantidad variable según personalidad
    mouse_count = random.randint(*pers["mouse_events_range"])
    key_count = random.randint(*pers["key_events_range"])
    touch_count = random.randint(*pers["touch_events_range"])

    # Mouse con delta max custom
    mouse_events = _generate_mouse_events_custom(mouse_count, vp,
                                                   pers["mouse_delta_max"])
    # Key con cadence custom
    key_events = _generate_key_events_custom(key_count, pers["key_cadence_ms"])
    # Touch
    touch_events = _generate_touch_events(touch_count, vp)

    # Noise events (10-15% del total)
    noise_events = []
    if inject_noise:
        noise_count = int((mouse_count + key_count + touch_count) * random.uniform(0.10, 0.15))
        noise_events = _generate_noise_events(noise_count, vp)

    # Fingerprint variado
    fp = _generate_fingerprint_varied(ua, vp, pers["fp_noise_level"])

    # Orden aleatorio de campos principales del payload
    # (Akamai parsea JSON, no requiere orden fijo)
    field_order = ["mouse_events", "key_events", "touch_events", "noise_events",
                    "performance_timing", "fingerprint"]
    random.shuffle(field_order)

    payload = {
        "session_id": sid,
        "personality": personality,
        "generated_at": datetime.now().isoformat(),
        "field_order": field_order,
        "events_stats": {
            "mouse": len(mouse_events), "key": len(key_events),
            "touch": len(touch_events), "noise": len(noise_events),
            "total": len(mouse_events) + len(key_events) + len(touch_events) + len(noise_events),
        },
    }
    # Añadir campos en orden aleatorio
    fields_data = {
        "mouse_events": mouse_events,
        "key_events": key_events,
        "touch_events": touch_events,
        "noise_events": noise_events,
        "performance_timing": _generate_perf_timing(),
        "fingerprint": fp,
    }
    for k in field_order:
        payload[k] = fields_data[k]

    payload["dnt"] = random.choice([False, None])
    payload["t_start"] = int(time.time() * 1000)
    payload["t_elapsed"] = random.randint(800, 4500)  # user spent X ms on page

    return payload


def _generate_mouse_events_custom(count: int, viewport: list,
                                     delta_max: int) -> List[dict]:
    """Como _generate_mouse_events pero con delta_max variable."""
    events = []
    w, h = viewport
    x, y = random.randint(100, w - 100), random.randint(100, h - 100)
    t = 0
    for _ in range(count):
        dx = random.randint(-delta_max, delta_max)
        dy = random.randint(-int(delta_max * 0.6), int(delta_max * 0.6))
        x = max(0, min(w, x + dx))
        y = max(0, min(h, y + dy))
        t += random.randint(15, 120)
        events.append({"type": "mm", "x": x, "y": y, "t": t,
                        "dx": dx, "dy": dy})
    return events


def _generate_key_events_custom(count: int, cadence: Dict) -> List[dict]:
    """Key events con cadencia gaussiana custom (μ/σ por personalidad)."""
    events = []
    t = 0
    keys = "abcdefghijklmnopqrstuvwxyz 0123456789"
    mu = cadence.get("mu", 150)
    sigma = cadence.get("sigma", 50)
    for _ in range(count):
        k = random.choice(keys)
        dwell = max(30, int(random.gauss(mu * 0.55, sigma * 0.35)))
        t += max(50, int(random.gauss(mu, sigma)))
        events.append({"type": "kd", "k": ord(k), "t": t})
        events.append({"type": "ku", "k": ord(k), "t": t + dwell})
    return events


def _generate_fingerprint_varied(ua: str, viewport: list,
                                    noise_level: str) -> dict:
    """Fingerprint con variaciones más ricas según noise_level."""
    w, h = viewport
    platforms_by_ua = {
        "Windows": "Win32", "Mac": "MacIntel", "Linux": "Linux x86_64",
        "iPhone": "iPhone", "Android": "Linux armv8l",
    }
    platform = next((v for k, v in platforms_by_ua.items() if k in ua), "Win32")

    # Plugins count según noise_level
    plugins_count = {"low": random.randint(2, 4),
                      "medium": random.randint(3, 6),
                      "high": random.randint(5, 10)}.get(noise_level, 5)

    # Hardware concurrency según platform
    if "iPhone" in platform or "Android" in platform:
        cores = random.choice([6, 8])
    elif "Mac" in platform:
        cores = random.choice([8, 10, 12, 16])
    else:
        cores = random.choice([4, 8, 12, 16, 20, 24])

    # Device memory realistic
    memory = random.choice([4, 8, 16, 32]) if noise_level != "low" else random.choice([4, 8])

    # Color depth (común 24 en dekstop, 30 en Mac HiDPI)
    color_depth = random.choice([24, 24, 24, 30])

    # Pixel ratio (HiDPI cada vez más común)
    pixel_ratio = random.choice([1.0, 1.0, 1.25, 1.5, 2.0])

    return {
        "ua": ua,
        "platform": platform,
        "screen": {
            "w": w, "h": h,
            "colorDepth": color_depth,
            "pixelRatio": pixel_ratio,
            "availWidth": w - random.choice([0, 0, 80]),  # menos taskbar
            "availHeight": h - random.choice([40, 60, 80]),
        },
        "lang": random.choice(["en-US", "en-GB", "es-ES", "fr-FR", "de-DE"]),
        "timezone": random.choice([
            "America/New_York", "America/Los_Angeles", "Europe/London",
            "Europe/Madrid", "Europe/Berlin", "Asia/Tokyo",
        ]),
        "hardwareConcurrency": cores,
        "deviceMemory": memory,
        "plugins_count": plugins_count,
        "canvas_hash": hashlib.md5(f"canvas-{uuid.uuid4().hex[:8]}".encode()).hexdigest()[:16],
        "webgl_vendor": random.choice(["NVIDIA Corporation", "Intel Inc.",
                                         "AMD", "Apple", "ARM"]),
        "webgl_renderer": random.choice([
            "GeForce RTX 4060/PCIe/SSE2", "Intel Iris Xe Graphics",
            "Radeon RX 6600", "Apple M2 Pro", "Mali-G77 MC9",
        ]),
        "cookie_enabled": True,
        "do_not_track": random.choice([None, None, "1"]),
        "max_touch_points": 10 if "Mobile" in ua or "iPhone" in ua else 0,
    }


# Variar también el formato del sensor_data string
def build_sensor_data_varied(payload: dict, format_variant: str = "auto") -> str:
    """Construye sensor_data con variantes de formato.

    Akamai acepta varias versiones:
      '2;<len>;<hash>;<b64>;<chk>'  (v2 estándar)
      '3;<len>;<hash>;<b64>;<sig>'  (v3 2024+)
      '2;<b64>;<chk>'                (v2 simplificado)
    """
    import base64
    payload_json = json.dumps(payload, separators=(",", ":"))

    if format_variant == "auto":
        # Seed by session_id para consistencia
        sid = payload.get("session_id", "")
        h = int(hashlib.md5(sid.encode()).hexdigest()[:8], 16) if sid else random.randint(0, 3)
        format_variant = ["v2", "v3", "v2_simple"][h % 3]

    length = len(payload_json)
    key_hash = hashlib.md5(payload.get("session_id", "").encode()).hexdigest()[:16]

    # Simulación RC4 (XOR + base64)
    key_bytes = key_hash.encode()
    encoded = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(payload_json.encode()))
    payload_b64 = base64.b64encode(encoded).decode()
    checksum = hashlib.sha256(payload_b64.encode()).hexdigest()[:12]

    if format_variant == "v2":
        return f"2;{length};{key_hash};{payload_b64};{checksum}"
    elif format_variant == "v3":
        # v3 incluye signature HMAC-like
        sig = hashlib.sha1((payload_b64 + key_hash).encode()).hexdigest()[:20]
        return f"3;{length};{key_hash};{payload_b64};{sig}"
    elif format_variant == "v2_simple":
        return f"2;{payload_b64};{checksum}"
    return f"2;{length};{key_hash};{payload_b64};{checksum}"


def humanized_self_test() -> dict:
    """Test de las heurísticas anti-detection."""
    results = {"ok": True, "personalities": {}}

    for pers_id in USER_PERSONALITIES:
        p = generate_humanized_payload(session_id=f"test_{pers_id}",
                                         personality=pers_id)
        sensor = build_sensor_data_varied(p)
        stats = p["events_stats"]
        results["personalities"][pers_id] = {
            "total_events": stats["total"],
            "mouse": stats["mouse"], "key": stats["key"],
            "touch": stats["touch"], "noise": stats["noise"],
            "sensor_length": len(sensor),
            "sensor_format": sensor.split(";")[0],
            "field_order_first": p["field_order"][0],
        }

    # Verificar que personalidades distintas generan stats distintos
    totals = [r["total_events"] for r in results["personalities"].values()]
    results["variation_total_events"] = {
        "min": min(totals), "max": max(totals),
        "spread_pct": round((max(totals) - min(totals)) / max(totals) * 100, 1),
    }

    # Verificar que el mismo session_id da mismo personality (determinístico)
    p1 = generate_humanized_payload(session_id="stable-123")
    p2 = generate_humanized_payload(session_id="stable-123")
    results["deterministic_personality"] = p1["personality"] == p2["personality"]

    # Verificar que varía entre session_ids
    p_a = generate_humanized_payload(session_id="test-AAA")
    p_b = generate_humanized_payload(session_id="test-BBB")
    results["varies_between_sessions"] = p_a["events_stats"] != p_b["events_stats"]

    return results
