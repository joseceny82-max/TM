"""continuous_biometrics.py - Continuous behavioral biometrics post-login (SOTA P1-9).

NuData + DataDome 2026 ya no se conforman con el "snapshot" del signup; ambas
plataformas ahora corren modelos transformer-based con ventana deslizante
post-login que evaluan continuamente:

  - Scroll velocity profile (pixels/s sobre el tiempo)
  - Focus / blur timing entre elementos
  - Idle micro-jitters (movimientos sub-pixel sin proposito)
  - Lectura realista (pause en bloques de texto)
  - Mouse hover sobre elementos antes de click

Esta capa se ejecuta DESPUES de nudata_emulator (que cubre el signup) y
extiende human_behavior.py con sesiones continuas.

API publica:
  start_continuous_session(account_id, geo)            -> dict {session_id}
  inject_idle_jitter(session_id, duration_s)           -> dict {events_count}
  simulate_reading_pattern(session_id, text_length)    -> dict {duration_ms}
  track_focus_pattern(session_id, elements)            -> dict {focus_events}
  record_scroll_profile(session_id, total_px)          -> dict {events_count}
  record_hover(session_id, target, dwell_ms)           -> dict {ok}
  build_continuous_payload(session_id, format='full')  -> dict
  score_session_humanness(session_id)                  -> dict {score}
  end_continuous_session(session_id)                   -> dict
  self_test()
"""
from __future__ import annotations
import os
import time
import json
import math
import random
import hashlib
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

try:
    import human_behavior
    HUMAN_AVAILABLE = True
except Exception:
    HUMAN_AVAILABLE = False


_CONFIG_KEY = "continuous_biometrics_config.json"
_LOG_KEY = "continuous_biometrics_log.json"
_SESSIONS_KEY = "continuous_biometrics_sessions.json"
_lock = threading.RLock()
_sessions: Dict[str, Dict[str, Any]] = {}


# =====================================================================
# CONFIG
# =====================================================================
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "min_session_duration_s": 15,
        "max_session_duration_s": 1800,
        "idle_jitter_amplitude_px": 3,
        "idle_jitter_min_interval_ms": 800,
        "idle_jitter_max_interval_ms": 4500,
        "reading_wpm_low": 180,
        "reading_wpm_avg": 250,
        "reading_wpm_high": 360,
        "scroll_velocity_min_pps": 200,    # pixels per second
        "scroll_velocity_max_pps": 1400,
        "focus_dwell_min_ms": 250,
        "focus_dwell_max_ms": 4500,
        "hover_min_ms": 80,
        "hover_max_ms": 1500,
        "humanness_pass_threshold": 0.65,
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
# SESSION LIFECYCLE
# =====================================================================
def start_continuous_session(account_id: str = "",
                             geo: str = "ES",
                             user_agent: str = "",
                             viewport: Tuple[int, int] = (1920, 1080)) -> Dict[str, Any]:
    """Arranca una sesion continua post-login."""
    sid = "cb_" + hashlib.sha1(
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
        "user_agent": user_agent,
        "viewport": list(viewport),
        "started_ts": time.time(),
        "started_at": datetime.now().isoformat(),
        "idle_events": [],
        "scroll_events": [],
        "focus_events": [],
        "hover_events": [],
        "reading_events": [],
        "last_activity_ts": time.time(),
    }
    with _lock:
        _sessions[sid] = sess
    return {"ok": True, "session_id": sid, "personality": personality}


def end_continuous_session(session_id: str) -> Dict[str, Any]:
    with _lock:
        sess = _sessions.pop(session_id, None)
    if not sess:
        return {"ok": False, "error": "session_not_found"}
    duration = round(time.time() - sess["started_ts"], 1)
    try:
        d = json_atomic.load(config.ruta(_SESSIONS_KEY), default={"sessions": []})
        d["sessions"].insert(0, {
            "session_id": session_id,
            "account_id": sess.get("account_id"),
            "duration_s": duration,
            "idle_count": len(sess.get("idle_events", [])),
            "scroll_count": len(sess.get("scroll_events", [])),
            "focus_count": len(sess.get("focus_events", [])),
            "ended_at": datetime.now().isoformat(),
        })
        d["sessions"] = d["sessions"][:200]
        json_atomic.save(config.ruta(_SESSIONS_KEY), d)
    except Exception:
        pass
    return {"ok": True, "session_id": session_id, "duration_s": duration}


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _sessions.get(session_id)


def list_continuous_sessions() -> List[str]:
    with _lock:
        return list(_sessions.keys())


# =====================================================================
# IDLE JITTER (movimientos sub-pixel mientras "no haces nada")
# =====================================================================
def inject_idle_jitter(session_id: str, duration_s: float = 5.0) -> Dict[str, Any]:
    """Genera micro-jitters durante un periodo idle.

    NuData ML detecta ausencia COMPLETA de movimiento como bot. Un humano,
    incluso "quieto", genera jitters sub-pixel cada 1-4s.
    """
    sess = get_session(session_id)
    if not sess:
        return {"ok": False, "error": "session_not_found"}
    cfg = get_config()
    amp = cfg["idle_jitter_amplitude_px"]
    events = []
    elapsed = 0.0
    base_x, base_y = sess["viewport"][0] // 2, sess["viewport"][1] // 2
    while elapsed < duration_s:
        gap_ms = random.randint(cfg["idle_jitter_min_interval_ms"],
                                cfg["idle_jitter_max_interval_ms"])
        elapsed += gap_ms / 1000.0
        if elapsed > duration_s:
            break
        ev = {
            "type": "idle_jitter",
            "x": base_x + random.randint(-amp, amp),
            "y": base_y + random.randint(-amp, amp),
            "t_offset_ms": int(elapsed * 1000),
        }
        events.append(ev)
    with _lock:
        sess["idle_events"].extend(events)
        sess["last_activity_ts"] = time.time()
    return {"ok": True, "events_count": len(events),
            "duration_s": duration_s, "amplitude_px": amp}


# =====================================================================
# READING PATTERN (pause realista al leer)
# =====================================================================
def simulate_reading_pattern(session_id: str, text_length: int) -> Dict[str, Any]:
    """Simula el tiempo que tarda un humano en leer un bloque de texto.

    text_length = caracteres aprox.
    Convierte a palabras (~5 chars/word) y pausa segun WPM personality.
    """
    sess = get_session(session_id)
    if not sess:
        return {"ok": False, "error": "session_not_found"}
    if text_length <= 0:
        return {"ok": False, "error": "empty_text"}
    cfg = get_config()
    pers = sess.get("personality", "casual")
    wpm = {
        "fast": cfg["reading_wpm_high"],
        "power_user": cfg["reading_wpm_high"],
        "casual": cfg["reading_wpm_avg"],
        "slow": cfg["reading_wpm_low"],
        "elderly": cfg["reading_wpm_low"] - 50,
    }.get(pers, cfg["reading_wpm_avg"])
    words = max(1, text_length // 5)
    base_seconds = (words / wpm) * 60.0
    # Anyade variabilidad humana +/- 25%
    jitter = base_seconds * random.uniform(-0.25, 0.25)
    duration_ms = int(max(300, (base_seconds + jitter) * 1000))
    ev = {
        "type": "reading",
        "text_length": text_length,
        "words_estimated": words,
        "wpm": wpm,
        "duration_ms": duration_ms,
        "ts": time.time(),
    }
    with _lock:
        sess["reading_events"].append(ev)
        sess["last_activity_ts"] = time.time()
    return {"ok": True, "duration_ms": duration_ms, "words_estimated": words,
            "wpm_used": wpm}


# =====================================================================
# FOCUS / BLUR TRACKING
# =====================================================================
def track_focus_pattern(session_id: str,
                        elements: Optional[List[str]] = None,
                        sequence_len: int = 5) -> Dict[str, Any]:
    """Genera secuencia realista de focus/blur entre elementos.

    Humanos hacen focus -> dwell -> blur al pasar de campo a campo.
    Bots tipicamente saltan instantaneamente o no hacen blur.
    """
    sess = get_session(session_id)
    if not sess:
        return {"ok": False, "error": "session_not_found"}
    cfg = get_config()
    if not elements:
        elements = [f"el_{i}" for i in range(sequence_len)]
    events = []
    t = 0
    for el in elements:
        dwell = random.randint(cfg["focus_dwell_min_ms"],
                                cfg["focus_dwell_max_ms"])
        events.append({"type": "focus", "el": el, "t_offset_ms": t})
        t += dwell
        events.append({"type": "blur", "el": el, "t_offset_ms": t,
                       "dwell_ms": dwell})
        # gap entre elements
        gap = random.randint(60, 380)
        t += gap
    with _lock:
        sess["focus_events"].extend(events)
        sess["last_activity_ts"] = time.time()
    return {"ok": True, "events_count": len(events),
            "elements": elements, "total_duration_ms": t}


# =====================================================================
# SCROLL PROFILE
# =====================================================================
def record_scroll_profile(session_id: str, total_px: int = 1500,
                          direction: str = "down") -> Dict[str, Any]:
    """Genera scroll con perfil de velocidad humano.

    Humanos aceleran al inicio, decay al final, micro-pausas a mitad.
    """
    sess = get_session(session_id)
    if not sess:
        return {"ok": False, "error": "session_not_found"}
    cfg = get_config()
    sign = -1 if direction == "up" else 1
    target = abs(total_px)
    accumulated = 0
    events = []
    t = 0
    velocity_pps = cfg["scroll_velocity_min_pps"]
    accel_phase_done = False
    while accumulated < target:
        # accel/decel
        if not accel_phase_done and velocity_pps < cfg["scroll_velocity_max_pps"] * 0.7:
            velocity_pps += random.randint(40, 120)
        else:
            accel_phase_done = True
            if accumulated > target * 0.7:
                velocity_pps = max(cfg["scroll_velocity_min_pps"],
                                    velocity_pps - random.randint(50, 180))
        gap_ms = random.randint(35, 90)
        t += gap_ms
        chunk = int(velocity_pps * gap_ms / 1000)
        chunk = max(8, min(chunk, target - accumulated))
        events.append({"type": "scroll", "dy": sign * chunk,
                       "t_offset_ms": t, "v_pps": velocity_pps})
        accumulated += chunk
        # micro pause aleatoria
        if random.random() < 0.08:
            t += random.randint(120, 450)
        if len(events) > 60:
            break
    with _lock:
        sess["scroll_events"].extend(events)
        sess["last_activity_ts"] = time.time()
    return {"ok": True, "events_count": len(events),
            "total_px": accumulated, "direction": direction}


# =====================================================================
# HOVER (humanos hacen hover sobre boton antes de click)
# =====================================================================
def record_hover(session_id: str, target: str = "button",
                 dwell_ms: Optional[int] = None) -> Dict[str, Any]:
    sess = get_session(session_id)
    if not sess:
        return {"ok": False, "error": "session_not_found"}
    cfg = get_config()
    dw = dwell_ms or random.randint(cfg["hover_min_ms"], cfg["hover_max_ms"])
    ev = {"type": "hover", "target": target, "dwell_ms": dw, "ts": time.time()}
    with _lock:
        sess["hover_events"].append(ev)
        sess["last_activity_ts"] = time.time()
    return {"ok": True, "target": target, "dwell_ms": dw}


# =====================================================================
# PAYLOAD BUILDING
# =====================================================================
def build_continuous_payload(session_id: str,
                             format: str = "full") -> Dict[str, Any]:
    """Construye el payload tipo `continuous_biometrics` (NuData v3 / DataDome 2026)."""
    sess = get_session(session_id)
    if not sess:
        return {"ok": False, "error": "session_not_found"}
    duration_ms = int((time.time() - sess["started_ts"]) * 1000)
    payload = {
        "v": "cb-1.0",
        "sid": session_id,
        "aid": sess.get("account_id", ""),
        "geo": sess.get("geo"),
        "ua": sess.get("user_agent", ""),
        "vp": sess["viewport"],
        "personality": sess["personality"],
        "duration_ms": duration_ms,
        "summary": {
            "idle_events": len(sess["idle_events"]),
            "scroll_events": len(sess["scroll_events"]),
            "focus_events": len(sess["focus_events"]),
            "hover_events": len(sess["hover_events"]),
            "reading_events": len(sess["reading_events"]),
        },
        "ts": int(time.time() * 1000),
    }
    if format == "full":
        payload["raw"] = {
            "idle": sess["idle_events"][-200:],
            "scroll": sess["scroll_events"][-200:],
            "focus": sess["focus_events"][-200:],
            "hover": sess["hover_events"][-100:],
            "reading": sess["reading_events"][-50:],
        }
    encoded = _encode_payload(payload)
    return {"ok": True, "payload": payload,
            "encoded": encoded, "encoded_length": len(encoded)}


def _encode_payload(payload: Dict[str, Any]) -> str:
    """Encoding simple (XOR + b64) - el real usa AES-GCM derivado del cookie."""
    import base64
    pj = json.dumps(payload, separators=(",", ":"))
    key = hashlib.sha256(payload["sid"].encode()).digest()[:16]
    enc = bytes(b ^ key[i % len(key)] for i, b in enumerate(pj.encode()))
    return base64.b64encode(enc).decode()


# =====================================================================
# HUMANNESS SCORE
# =====================================================================
def score_session_humanness(session_id: str) -> Dict[str, Any]:
    """Devuelve score 0-1 de "que tan humano parece" segun heuristicas.

    Reglas:
      - Idle events presentes (no flat 0): +0.2
      - Scroll velocity con varianza (no constante): +0.2
      - Focus events con dwell > 250ms y < 5000ms: +0.2
      - Reading events presentes: +0.15
      - Hover events presentes: +0.15
      - Duration > 15s: +0.10
    """
    sess = get_session(session_id)
    if not sess:
        return {"ok": False, "error": "session_not_found"}
    score = 0.0
    reasons = []
    if sess["idle_events"]:
        score += 0.20
        reasons.append("idle_present")
    sc = sess["scroll_events"]
    if sc:
        velocities = [abs(e.get("v_pps", 0)) for e in sc if isinstance(e, dict)]
        if velocities and len(set(velocities)) > 2:
            score += 0.20
            reasons.append("scroll_variable")
    cfg = get_config()
    fe = sess["focus_events"]
    if fe:
        dwells = [e.get("dwell_ms", 0) for e in fe if e.get("type") == "blur"]
        if dwells and all(cfg["focus_dwell_min_ms"] <= d <= cfg["focus_dwell_max_ms"] + 500 for d in dwells):
            score += 0.20
            reasons.append("focus_dwell_human")
    if sess["reading_events"]:
        score += 0.15
        reasons.append("reading_present")
    if sess["hover_events"]:
        score += 0.15
        reasons.append("hover_present")
    if (time.time() - sess["started_ts"]) >= cfg["min_session_duration_s"]:
        score += 0.10
        reasons.append("min_duration")
    score = round(min(1.0, score), 3)
    return {
        "ok": True,
        "session_id": session_id,
        "score": score,
        "passes_threshold": score >= cfg["humanness_pass_threshold"],
        "threshold": cfg["humanness_pass_threshold"],
        "reasons_matched": reasons,
    }


# =====================================================================
# SELF TEST
# =====================================================================
def self_test() -> Dict[str, Any]:
    checks = {}
    cfg = get_config()
    checks["config_has_min_dur"] = "min_session_duration_s" in cfg
    checks["config_threshold_065"] = abs(cfg["humanness_pass_threshold"] - 0.65) < 0.001

    # 1. Start session
    r = start_continuous_session(account_id="cb@test.com", geo="ES")
    checks["start_session_ok"] = r.get("ok") is True
    sid = r["session_id"]
    checks["session_id_prefix"] = sid.startswith("cb_")

    # 2. Idle jitter
    r = inject_idle_jitter(sid, duration_s=4.0)
    checks["idle_jitter_ok"] = r.get("ok") is True
    checks["idle_jitter_events"] = r.get("events_count", 0) >= 1

    # 3. Reading pattern
    r = simulate_reading_pattern(sid, text_length=400)
    checks["reading_ok"] = r.get("ok") is True
    checks["reading_duration_positive"] = r.get("duration_ms", 0) > 0
    checks["reading_words_estimated"] = r.get("words_estimated") == 80

    # 4. Reading rejects empty
    r_bad = simulate_reading_pattern(sid, text_length=0)
    checks["reading_rejects_zero"] = r_bad.get("ok") is False

    # 5. Focus pattern
    r = track_focus_pattern(sid, ["email", "password", "submit"])
    checks["focus_ok"] = r.get("ok") is True
    checks["focus_event_count"] = r.get("events_count", 0) == 6  # 3 focus + 3 blur

    # 6. Scroll profile
    r = record_scroll_profile(sid, total_px=2000, direction="down")
    checks["scroll_ok"] = r.get("ok") is True
    checks["scroll_has_events"] = r.get("events_count", 0) > 0

    # 7. Scroll up
    r_up = record_scroll_profile(sid, total_px=400, direction="up")
    checks["scroll_up_ok"] = r_up.get("ok") is True

    # 8. Hover
    r = record_hover(sid, target="cta_button")
    checks["hover_ok"] = r.get("ok") is True
    checks["hover_dwell_in_range"] = cfg["hover_min_ms"] <= r.get("dwell_ms", 0) <= cfg["hover_max_ms"]

    # 9. Build payload
    r = build_continuous_payload(sid, format="full")
    checks["payload_ok"] = r.get("ok") is True
    checks["payload_has_encoded"] = bool(r.get("encoded"))
    checks["payload_summary_present"] = "summary" in r.get("payload", {})
    checks["payload_raw_present"] = "raw" in r.get("payload", {})

    # 10. Build minimal payload
    r_min = build_continuous_payload(sid, format="minimal")
    checks["payload_minimal_ok"] = r_min.get("ok") is True
    checks["payload_minimal_no_raw"] = "raw" not in r_min.get("payload", {})

    # 11. Humanness score
    sc = score_session_humanness(sid)
    checks["score_ok"] = sc.get("ok") is True
    checks["score_in_range"] = 0.0 <= sc.get("score", -1) <= 1.0
    checks["score_passes_threshold"] = sc.get("passes_threshold") is True

    # 12. List sessions
    lst = list_continuous_sessions()
    checks["session_listed"] = sid in lst

    # 13. Session found by id
    s = get_session(sid)
    checks["get_session_returns_dict"] = isinstance(s, dict)

    # 14. End session
    r = end_continuous_session(sid)
    checks["end_session_ok"] = r.get("ok") is True
    checks["session_removed"] = sid not in list_continuous_sessions()

    # 15. Operations on dead session fail
    r_dead = inject_idle_jitter(sid, 1.0)
    checks["dead_session_rejected"] = r_dead.get("ok") is False

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "continuous_biometrics",
        "version": 1,
        "active_sessions": len(_sessions),
        "human_behavior_available": HUMAN_AVAILABLE,
        "purpose": "Continuous behavioral biometrics post-login (NuData v3 / DataDome 2026)",
    }
