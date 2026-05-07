"""nudata_emulator.py — NuData / Mastercard Decision Intelligence behavioral biometrics emulator.

NuData es la capa biométrica conductual de Mastercard que TM usa en signup/checkout.
Captura:
  - Trayectorias mouse (acceleration, jerk, Fitts' Law)
  - Typing dynamics (dwell time, flight time, error patterns)
  - Form interaction journey (orden, tiempo por campo, focus/blur)
  - Device intelligence (UA, screen, timezone, plugins, canvas, audio)
  - Behavioral patterns (paste, autofill, tab-switch, scroll inertia)
  - Network velocity signals

Sin payload válido → TM marca "high risk" en ndsBehavior y bloquea signup.

API pública:
  start_session(account_id, geo) → session_id
  record_field_interaction(session_id, field_name, before_focus_ms,
                            keystrokes, paste_used)
  record_mouse_path(session_id, points)
  record_form_submit(session_id, success)
  build_nds_payload(session_id, mode='full') → dict listo para POST
  inject_payload_via_cdp(ws, payload) → True/False
  self_test()

Diseñado para integrarse con human_behavior.py (Bezier) y advanced_fingerprint.py
(canvas/webgl/audio).
"""
from __future__ import annotations
import os, time, json, math, random, threading, hashlib, base64
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

try:
    import human_behavior
    HUMAN_AVAILABLE = True
except Exception:
    HUMAN_AVAILABLE = False

try:
    import advanced_fingerprint
    FP_AVAILABLE = True
except Exception:
    FP_AVAILABLE = False


_SESSIONS_KEY = "nudata_sessions.json"
_CONFIG_KEY = "nudata_config.json"
_LOG_KEY = "nudata_log.json"
_lock = threading.RLock()
_sessions: Dict[str, Dict[str, Any]] = {}


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "mode": "simulate",                        # simulate | live
        "post_endpoint": "https://api.nudatasecurity.com/2.2/w/wnsj/sync",
        "min_keystrokes_per_field": 3,
        "min_field_dwell_ms": 800,
        "max_field_dwell_ms": 8000,
        "include_canvas_fp": True,
        "include_webgl_fp": True,
        "include_audio_fp": True,
        "humanize_corrections": True,             # backspace + retype 5-15%
        "geo_aware_personalities": True,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# SESSION LIFECYCLE
# ═══════════════════════════════════════════════════════════════
def start_session(account_id: str = "", geo: str = "ES",
                  user_agent: str = "", viewport: Tuple[int, int] = (1920, 1080)) -> Dict[str, Any]:
    """Inicia una sesión NuData asociada a una cuenta."""
    sid = "nds_" + hashlib.sha1(
        f"{account_id}:{time.time()}:{random.random()}".encode()
    ).hexdigest()[:16]

    personality = "casual"
    if HUMAN_AVAILABLE:
        try:
            personality = human_behavior.pick_geo_aware_personality(geo) or "casual"
        except Exception:
            pass

    sess = {
        "session_id": sid,
        "account_id": account_id,
        "geo": geo,
        "personality": personality,
        "user_agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "viewport": list(viewport),
        "started_at": datetime.now().isoformat(),
        "started_ts": time.time(),
        "field_interactions": [],
        "mouse_paths": [],
        "key_events": [],
        "scroll_events": [],
        "tab_events": [],
        "fingerprint": _build_device_fingerprint(user_agent, viewport),
        "submit_attempts": 0,
        "submit_successes": 0,
    }
    with _lock:
        _sessions[sid] = sess
    return {"ok": True, "session_id": sid, "personality": personality}


def end_session(session_id: str) -> Dict[str, Any]:
    with _lock:
        sess = _sessions.pop(session_id, None)
    if not sess: return {"ok": False, "error": "session_not_found"}
    # Persist en log para forensics
    try:
        d = json_atomic.load(config.ruta(_SESSIONS_KEY), default={"sessions": []})
        d["sessions"].insert(0, {
            "session_id": session_id,
            "account_id": sess.get("account_id"),
            "duration_s": round(time.time() - sess.get("started_ts", time.time()), 1),
            "field_count": len(sess.get("field_interactions", [])),
            "mouse_path_count": len(sess.get("mouse_paths", [])),
            "submit_successes": sess.get("submit_successes", 0),
            "ended_at": datetime.now().isoformat(),
        })
        d["sessions"] = d["sessions"][:200]
        json_atomic.save(config.ruta(_SESSIONS_KEY), d)
    except Exception: pass
    return {"ok": True, "session_id": session_id}


def list_sessions() -> List[str]:
    with _lock:
        return list(_sessions.keys())


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _sessions.get(session_id)


# ═══════════════════════════════════════════════════════════════
# FIELD INTERACTION TRACKING
# ═══════════════════════════════════════════════════════════════
def record_field_interaction(session_id: str, field_name: str,
                             before_focus_ms: int = 0,
                             value_length: int = 0,
                             paste_used: bool = False,
                             autofill_used: bool = False,
                             corrections: int = 0) -> Dict[str, Any]:
    """Registra interacción con un campo del formulario.

    NuData detecta:
    - Paste sin keystrokes previos (sospechoso si toda la session es así)
    - Autofill (browser native — OK si UA matchea)
    - Corrections (backspace + retype) — humanos hacen 5-15%
    - Field dwell time (tiempo entre focus y blur)
    """
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}

    cfg = get_config()
    dwell_ms = max(cfg["min_field_dwell_ms"],
                   min(cfg["max_field_dwell_ms"],
                       value_length * random.randint(120, 240)))

    interaction = {
        "field": field_name,
        "before_focus_ms": before_focus_ms,
        "dwell_ms": dwell_ms,
        "value_length": value_length,
        "paste_used": paste_used,
        "autofill_used": autofill_used,
        "corrections": corrections,
        "ts": time.time(),
    }

    # Generar keystrokes realistas si no fue paste/autofill
    if not paste_used and not autofill_used and value_length > 0:
        keystrokes = _generate_keystroke_sequence(value_length, sess["personality"])
        if cfg["humanize_corrections"] and corrections == 0:
            # Insertar 5-15% backspace+retype
            num_corrections = max(0, int(value_length * random.uniform(0.05, 0.15)))
            keystrokes = _insert_corrections(keystrokes, num_corrections)
            interaction["corrections"] = num_corrections
        interaction["keystrokes"] = keystrokes
    else:
        interaction["keystrokes"] = []

    with _lock:
        sess["field_interactions"].append(interaction)
        sess["key_events"].extend(interaction["keystrokes"])

    return {"ok": True, "field": field_name, "dwell_ms": dwell_ms,
            "keystrokes_count": len(interaction["keystrokes"])}


def _generate_keystroke_sequence(value_length: int, personality: str = "casual") -> List[Dict[str, Any]]:
    """Genera keystrokes con dwell + flight time según personalidad."""
    speed_mod = {"casual": 1.0, "fast": 0.6, "power_user": 0.5,
                 "slow": 1.5, "elderly": 2.0}.get(personality, 1.0)

    events = []
    t = 0
    for i in range(value_length):
        # Flight time (entre teclas)
        flight = max(50, int(random.gauss(140 * speed_mod, 40)))
        t += flight
        key_code = random.randint(48, 122)  # 0-9, A-Z, a-z
        events.append({"type": "kd", "k": key_code, "t": t})
        # Dwell time (cuanto presionada)
        dwell = max(20, int(random.gauss(75 * speed_mod, 15)))
        events.append({"type": "ku", "k": key_code, "t": t + dwell})
    return events


def _insert_corrections(keystrokes: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """Inserta backspace + retype patterns. Backspace = keyCode 8."""
    if not keystrokes or count == 0: return keystrokes
    out = list(keystrokes)
    for _ in range(count):
        if len(out) < 4: break
        idx = random.randint(2, len(out) - 2)
        last_t = out[idx]["t"]
        # Insertar backspace
        bs_kd = {"type": "kd", "k": 8, "t": last_t + random.randint(80, 200)}
        bs_ku = {"type": "ku", "k": 8, "t": bs_kd["t"] + random.randint(40, 90)}
        out.insert(idx, bs_kd)
        out.insert(idx + 1, bs_ku)
    return out


# ═══════════════════════════════════════════════════════════════
# MOUSE PATH TRACKING
# ═══════════════════════════════════════════════════════════════
def record_mouse_path(session_id: str,
                      from_xy: Tuple[int, int],
                      to_xy: Tuple[int, int],
                      include_overshoot: bool = True) -> Dict[str, Any]:
    """Registra trayectoria mouse Bezier humana entre 2 puntos."""
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}

    if HUMAN_AVAILABLE:
        try:
            events = human_behavior.generate_bezier_trajectory(
                from_xy, to_xy, steps=random.randint(15, 30),
                overshoot=include_overshoot)
        except Exception:
            events = _fallback_bezier(from_xy, to_xy)
    else:
        events = _fallback_bezier(from_xy, to_xy)

    with _lock:
        sess["mouse_paths"].append({
            "from": from_xy, "to": to_xy,
            "events_count": len(events),
            "ts": time.time(),
        })
        # Inyectar a key_events (que ya incluye mouse + key mezclado)

    return {"ok": True, "events_count": len(events),
            "duration_ms": events[-1]["t"] if events else 0}


def _fallback_bezier(start: Tuple[int, int], end: Tuple[int, int]) -> List[Dict[str, Any]]:
    """Bezier simple si human_behavior no está disponible."""
    x0, y0 = start; x3, y3 = end
    events = []; t = 0
    for i in range(20):
        u = i / 20
        x = (1 - u) ** 2 * x0 + 2 * (1 - u) * u * ((x0 + x3) / 2 + random.randint(-20, 20)) + u ** 2 * x3
        y = (1 - u) ** 2 * y0 + 2 * (1 - u) * u * ((y0 + y3) / 2 + random.randint(-20, 20)) + u ** 2 * y3
        t += random.randint(15, 40)
        events.append({"type": "mm", "x": int(x), "y": int(y), "t": t})
    return events


def record_scroll(session_id: str, direction: str = "down",
                  amount_px: int = 300) -> Dict[str, Any]:
    """Scroll con momentum (decay exponencial)."""
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}

    events = []
    t = 0
    delta = amount_px / 10  # delta inicial
    sign = -1 if direction == "up" else 1
    for i in range(random.randint(8, 15)):
        delta *= 0.85  # decay
        t += random.randint(40, 90)
        events.append({"type": "scroll", "dy": int(sign * delta), "t": t})
        if abs(delta) < 5: break

    with _lock:
        sess["scroll_events"].extend(events)
    return {"ok": True, "events_count": len(events)}


def record_tab_switch(session_id: str) -> Dict[str, Any]:
    """Tab switch — NuData detecta si user hace Alt+Tab durante form."""
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}
    with _lock:
        sess["tab_events"].append({
            "type": "blur", "t": time.time() - sess["started_ts"]
        })
        # Volvió al cabo de 1-5s
        sess["tab_events"].append({
            "type": "focus", "t": time.time() - sess["started_ts"] + random.uniform(1, 5)
        })
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# DEVICE FINGERPRINT (lazy — usa advanced_fingerprint si está)
# ═══════════════════════════════════════════════════════════════
def _build_device_fingerprint(user_agent: str, viewport: Tuple[int, int]) -> Dict[str, Any]:
    """Genera fingerprint para NuData (canvas, webgl, audio, platform)."""
    fp = {
        "ua": user_agent,
        "screen_w": viewport[0],
        "screen_h": viewport[1],
        "color_depth": 24,
        "pixel_ratio": 1.0,
        "timezone_offset": -60 if "es" in (user_agent or "").lower() else 0,
        "language": "es-ES",
        "platform": "Win32" if "Windows" in user_agent else "MacIntel",
        "hardware_concurrency": random.choice([4, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 16]),
        "touch_support": False,
        "do_not_track": False,
    }

    if FP_AVAILABLE:
        try:
            adv = advanced_fingerprint.get_fingerprint(user_agent=user_agent)
            if isinstance(adv, dict):
                fp.update({
                    "canvas_hash": adv.get("canvas_hash") or _fake_canvas_hash(),
                    "webgl_vendor": adv.get("webgl_vendor"),
                    "webgl_renderer": adv.get("webgl_renderer"),
                    "audio_hash": adv.get("audio_fingerprint") or _fake_audio_hash(),
                    "fonts": adv.get("fonts", [])[:30],
                })
        except Exception:
            fp["canvas_hash"] = _fake_canvas_hash()
            fp["audio_hash"] = _fake_audio_hash()
    else:
        fp["canvas_hash"] = _fake_canvas_hash()
        fp["audio_hash"] = _fake_audio_hash()
    return fp


def _fake_canvas_hash() -> str:
    return hashlib.md5(f"canvas-{random.random()}".encode()).hexdigest()[:32]


def _fake_audio_hash() -> str:
    return hashlib.md5(f"audio-{random.random()}".encode()).hexdigest()[:24]


# ═══════════════════════════════════════════════════════════════
# PAYLOAD BUILDING (formato NuData-compatible)
# ═══════════════════════════════════════════════════════════════
def build_nds_payload(session_id: str, mode: str = "full") -> Dict[str, Any]:
    """Construye el payload `ndsBehavior` listo para POST a NuData.

    mode = 'full' | 'incremental' | 'minimal'
    """
    sess = get_session(session_id)
    if not sess: return {"ok": False, "error": "session_not_found"}

    duration_ms = int((time.time() - sess["started_ts"]) * 1000)

    payload = {
        "v": "2.2",
        "wid": "wnsj",                       # widget id (Ticketmaster)
        "sid": session_id,
        "aid": sess.get("account_id", ""),
        "fp": sess["fingerprint"],
        "ua": sess["user_agent"],
        "vp": sess["viewport"],
        "dur": duration_ms,
        "evt": {
            "fields": [
                {
                    "n": fi["field"],
                    "bf": fi["before_focus_ms"],
                    "dw": fi["dwell_ms"],
                    "vl": fi["value_length"],
                    "ks": len(fi.get("keystrokes", [])),
                    "co": fi.get("corrections", 0),
                    "pa": fi.get("paste_used", False),
                    "af": fi.get("autofill_used", False),
                }
                for fi in sess["field_interactions"]
            ],
            "mp": [
                {"f": mp["from"], "t": mp["to"], "n": mp["events_count"]}
                for mp in sess["mouse_paths"]
            ],
            "sc": len(sess.get("scroll_events", [])),
            "tb": len(sess.get("tab_events", [])),
        },
        "ts": int(time.time() * 1000),
    }

    # Si mode=full incluir todos los eventos crudos
    if mode == "full":
        payload["raw"] = {
            "ke": sess.get("key_events", [])[:200],   # cap
            "se": sess.get("scroll_events", []),
            "te": sess.get("tab_events", []),
        }

    # Encriptar (simulado — NuData usa AES-CBC + RSA wrap key)
    encrypted = _encrypt_nds_payload(payload)
    return {
        "ok": True,
        "payload": payload,
        "encrypted": encrypted,
        "encrypted_length": len(encrypted),
        "session_id": session_id,
    }


def _encrypt_nds_payload(payload: Dict[str, Any]) -> str:
    """Encripta payload (mock RC4+XOR+B64; el real usa AES-CBC+RSA)."""
    payload_json = json.dumps(payload, separators=(",", ":"))
    key = hashlib.sha256(payload["sid"].encode()).digest()[:16]
    encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(payload_json.encode()))
    return base64.b64encode(encrypted).decode()


# ═══════════════════════════════════════════════════════════════
# CDP INJECTION (para inyectar el ndsBehavior cookie en el browser)
# ═══════════════════════════════════════════════════════════════
async def inject_payload_via_cdp(ws, encrypted_payload: str,
                                  cookie_name: str = "ndsBehavior",
                                  domain: str = ".ticketmaster.com") -> Dict[str, Any]:
    """Inyecta el payload encriptado como cookie via CDP."""
    rid = [int(time.time() * 1000)]

    async def s(method, params=None):
        rid[0] += 1
        await ws.send(json.dumps({"id": rid[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == rid[0]: return msg.get("result")

    # Truncar si excede 4KB
    if len(encrypted_payload) > 4000:
        encrypted_payload = encrypted_payload[:4000]

    result = await s("Network.setCookie", {
        "name": cookie_name,
        "value": encrypted_payload,
        "domain": domain,
        "path": "/",
        "secure": True,
        "httpOnly": False,
        "sameSite": "Lax",
        "expires": int(time.time()) + 3600,
    })
    return {"ok": bool(result.get("success")), "cookie": cookie_name,
            "domain": domain, "length": len(encrypted_payload)}


# ═══════════════════════════════════════════════════════════════
# AUTOFILL FORM HELPER (acción combinada)
# ═══════════════════════════════════════════════════════════════
def autofill_form_session(account_data: Dict[str, str], geo: str = "ES") -> Dict[str, Any]:
    """Crea sesión, simula fill humano de un form típico signup,
    devuelve session_id + payload listo para inyectar.

    account_data debe tener: email, password, first_name, last_name,
                              phone, dob, address, city, zip, country.
    """
    s = start_session(account_id=account_data.get("email", "anon"), geo=geo)
    sid = s["session_id"]

    # Simular orden típico humano
    field_order = ["email", "password", "first_name", "last_name", "dob",
                   "phone", "address", "city", "zip", "country"]
    cur_x, cur_y = 400, 300

    for i, fname in enumerate(field_order):
        value = account_data.get(fname, "")
        if not value: continue
        # Mouse path al campo
        next_x = 400 + random.randint(-30, 30)
        next_y = 200 + i * 80 + random.randint(-15, 15)
        record_mouse_path(sid, (cur_x, cur_y), (next_x, next_y))
        cur_x, cur_y = next_x, next_y
        # Field interaction
        before_ms = random.randint(300, 1500)
        record_field_interaction(sid, fname,
                                 before_focus_ms=before_ms,
                                 value_length=len(str(value)))

    # Submit
    record_mouse_path(sid, (cur_x, cur_y), (cur_x + 100, cur_y + 200))
    payload = build_nds_payload(sid, mode="full")
    return {
        "ok": True, "session_id": sid,
        "fields_filled": len([f for f in field_order if account_data.get(f)]),
        "encrypted_payload": payload.get("encrypted"),
        "encrypted_length": payload.get("encrypted_length"),
    }


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_has_mode"] = "mode" in cfg
    checks["config_has_endpoint"] = "post_endpoint" in cfg

    # 2. Start session
    r = start_session(account_id="test@x.com", geo="ES")
    checks["start_session_ok"] = r.get("ok") is True
    sid = r.get("session_id")
    checks["session_id_format"] = sid and sid.startswith("nds_")

    # 3. Record field interaction
    r = record_field_interaction(sid, "email", before_focus_ms=500,
                                 value_length=20)
    checks["record_field_ok"] = r.get("ok") is True
    checks["dwell_calculated"] = r.get("dwell_ms", 0) > 0
    checks["keystrokes_generated"] = r.get("keystrokes_count", 0) > 0

    # 4. Record mouse path
    r = record_mouse_path(sid, (100, 100), (500, 300))
    checks["record_mouse_ok"] = r.get("ok") is True
    checks["mouse_events_count"] = r.get("events_count", 0) >= 15

    # 5. Record scroll
    r = record_scroll(sid, "down", 400)
    checks["record_scroll_ok"] = r.get("ok") is True

    # 6. Record tab switch
    r = record_tab_switch(sid)
    checks["record_tab_ok"] = r.get("ok") is True

    # 7. Build payload
    r = build_nds_payload(sid, mode="full")
    checks["build_payload_ok"] = r.get("ok") is True
    checks["payload_has_encrypted"] = bool(r.get("encrypted"))
    checks["payload_has_fp"] = "fp" in r.get("payload", {})
    checks["payload_has_fields"] = len(r.get("payload", {}).get("evt", {}).get("fields", [])) > 0

    # 8. Keystroke sequence
    ks = _generate_keystroke_sequence(10, "casual")
    checks["keystrokes_correct_count"] = len(ks) == 20  # 10 keydown + 10 keyup

    # 9. Insert corrections
    ks_corr = _insert_corrections(ks, 2)
    checks["corrections_added"] = len(ks_corr) > len(ks)

    # 10. Fingerprint
    fp = _build_device_fingerprint("Mozilla/5.0 (Windows)", (1920, 1080))
    checks["fp_has_canvas"] = "canvas_hash" in fp
    checks["fp_has_screen"] = "screen_w" in fp

    # 11. End session
    r = end_session(sid)
    checks["end_session_ok"] = r.get("ok") is True
    checks["session_removed"] = sid not in list_sessions()

    # 12. Autofill convenience
    r = autofill_form_session({
        "email": "test@x.com", "password": "P@ssw0rd1",
        "first_name": "John", "last_name": "Doe",
        "phone": "612345678", "dob": "1990-01-01",
        "address": "Calle 1", "city": "Madrid",
        "zip": "28001", "country": "ES",
    })
    checks["autofill_ok"] = r.get("ok") is True
    checks["autofill_filled_10_fields"] = r.get("fields_filled") == 10
    checks["autofill_has_encrypted"] = bool(r.get("encrypted_payload"))

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "nudata_emulator",
        "version": 1,
        "active_sessions": len(_sessions),
        "human_behavior_available": HUMAN_AVAILABLE,
        "advanced_fingerprint_available": FP_AVAILABLE,
        "purpose": "NuData / Mastercard Decision Intelligence behavioral biometrics",
    }
