"""ja4_session_continuity.py — JA4+ inter-request continuity per session (SOTA P0-2).

Cloudflare publicó (Mar 2026) que ya correlacionan JA4 entre requests de la
MISMA sesión: si JA4 cambia mid-flow (queue → cart → purchase) = bot.

`ja4_pinning.py` ya pin (account_id, ip) → JA4. Pero esta capa NO garantiza
que el JA4 se mantenga consistente a lo largo del flujo COMPLETO.

Este módulo añade:
  - **Session-scoped JA4 lock**: cada session_id tiene un único JA4 inmutable
  - **Cross-request validation**: cada request observa JA4 → valida vs lock
  - **Drift detection**: si JA4 cambia → marca incident + intenta forzar el JA4 original
  - **Per-domain enforcement**: queue / cart / payment usan EL MISMO JA4
  - **Replay verifier**: valida historial de JA4 observados en una sesión
  - **Auto-bind on first request**: si no hay lock, bind con primer JA4 observado

Diferencia vs ja4_pinning.py:
  - Aquel pin (account, ip) — no maneja sesiones de duración variable
  - Este pin (session_id) durante TODA la session, incluso si IP cambia
    (ej. proxy rotation con sticky)

API:
  bind_session_ja4(session_id, ja4_fp, lock_until_ts=None) → ok
  validate_request(session_id, observed_ja4, request_url=None) → {ok, drift, ja4_locked}
  get_session_ja4(session_id) → ja4_fp | None
  get_session_history(session_id) → list of observations
  release_session(session_id) → ok
  detect_drift_pattern(session_id) → {drifted, drift_count, ...}
  force_replay_ja4(session_id, target_session) → ok
  get_metrics() → dict
  self_test()
"""
from __future__ import annotations
import os, time, json, hashlib, threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

try:
    import ja4_pinning
    PINNING_AVAILABLE = True
except Exception:
    PINNING_AVAILABLE = False


_CONFIG_KEY = "ja4_continuity_config.json"
_LOG_KEY = "ja4_continuity_log.json"
_lock = threading.RLock()
_session_locks: Dict[str, Dict[str, Any]] = {}
_session_observations: Dict[str, List[Dict[str, Any]]] = {}
_drift_incidents: List[Dict[str, Any]] = []
_metrics = {
    "sessions_bound": 0,
    "validations_passed": 0,
    "validations_drifted": 0,
    "auto_binds": 0,
    "force_replays": 0,
    "incidents_logged": 0,
}


def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "default_lock_ttl_s": 1800,           # 30 min
        "max_observations_per_session": 50,
        "drift_threshold": 1,                  # cualquier drift es incident
        "auto_bind_on_first_observation": True,
        "log_max_entries": 500,
        "incidents_max": 200,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# SESSION JA4 LOCK
# ═══════════════════════════════════════════════════════════════
def bind_session_ja4(session_id: str, ja4_fp: str = "",
                      lock_ttl_s: Optional[int] = None,
                      country: str = "us",
                      browser_hint: str = "") -> Dict[str, Any]:
    """Pin JA4 para session_id durante lock_ttl_s segundos.

    Si no se proporciona ja4_fp, usa get_recommended_ja4 si ja4_pinning disponible.
    Si ya existía un lock para session_id, retorna error (usar release_session primero).
    """
    if not session_id: return {"ok": False, "error": "session_id_required"}

    cfg = get_config()
    if lock_ttl_s is None: lock_ttl_s = cfg["default_lock_ttl_s"]

    # Auto-pick si no hay JA4
    if not ja4_fp:
        if PINNING_AVAILABLE:
            try: ja4_fp = ja4_pinning.get_recommended_ja4(country=country,
                                                            browser_hint=browser_hint)
            except Exception: pass
        if not ja4_fp:
            ja4_fp = "t13d1516h2_8daaf6152771_b1ff8ab2d16f"  # chrome131_win default

    with _lock:
        if session_id in _session_locks:
            existing = _session_locks[session_id]
            if time.time() < existing["expires_at"]:
                return {"ok": False, "error": "session_already_bound",
                        "current_ja4": existing["ja4_fp"][:30] + "..."}
            # Expirado → permitir re-bind
        _session_locks[session_id] = {
            "session_id": session_id,
            "ja4_fp": ja4_fp,
            "country": country,
            "browser_hint": browser_hint or "auto",
            "bound_at": datetime.now().isoformat(),
            "bound_ts": time.time(),
            "expires_at": time.time() + lock_ttl_s,
            "validation_count": 0,
            "drift_count": 0,
        }
        _session_observations[session_id] = []
        _metrics["sessions_bound"] += 1

    return {"ok": True, "session_id": session_id,
            "ja4_locked": ja4_fp[:30] + "...",
            "ttl_s": lock_ttl_s,
            "expires_at": datetime.fromtimestamp(time.time() + lock_ttl_s).isoformat()}


def get_session_ja4(session_id: str) -> Optional[str]:
    with _lock:
        s = _session_locks.get(session_id)
        if not s: return None
        if time.time() > s["expires_at"]:
            return None
        return s["ja4_fp"]


def get_session_lock_info(session_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        s = _session_locks.get(session_id)
        if not s: return None
        return {**s, "ttl_remaining_s": int(s["expires_at"] - time.time())}


def release_session(session_id: str) -> Dict[str, Any]:
    with _lock:
        was = _session_locks.pop(session_id, None)
        _session_observations.pop(session_id, None)
    return {"ok": was is not None, "session_id": session_id}


def list_active_sessions() -> List[Dict[str, Any]]:
    now = time.time()
    with _lock:
        return [
            {"session_id": sid, "ja4_short": s["ja4_fp"][:20] + "...",
              "country": s["country"], "ttl_remaining_s": int(s["expires_at"] - now),
              "validation_count": s["validation_count"],
              "drift_count": s["drift_count"]}
            for sid, s in _session_locks.items()
            if now < s["expires_at"]
        ]


# ═══════════════════════════════════════════════════════════════
# CROSS-REQUEST VALIDATION
# ═══════════════════════════════════════════════════════════════
def validate_request(session_id: str, observed_ja4: str,
                      request_url: str = "",
                      request_phase: str = "") -> Dict[str, Any]:
    """Valida que el JA4 observado en este request coincide con el lock.

    Si NO hay lock y `auto_bind_on_first_observation=True` → auto-bind.
    Si HAY lock y no coincide → drift incident.

    request_phase: "queue" | "warm" | "cart" | "checkout" | "" (free-form)
    """
    cfg = get_config()
    if not observed_ja4: return {"ok": False, "error": "observed_ja4_empty"}

    with _lock:
        s = _session_locks.get(session_id)
        # Auto-bind path
        if not s:
            if cfg["auto_bind_on_first_observation"]:
                # Liberar primero para evitar conflict (s puede ser expirado)
                _session_locks.pop(session_id, None)
                _session_observations.pop(session_id, None)
            else:
                return {"ok": False, "error": "session_not_bound",
                        "hint": "call bind_session_ja4 first"}

    # Si auto-bind, hacerlo ahora (fuera del lock)
    if not s and cfg["auto_bind_on_first_observation"]:
        bind_session_ja4(session_id, ja4_fp=observed_ja4)
        with _lock:
            _metrics["auto_binds"] += 1
            s = _session_locks.get(session_id)

    if not s: return {"ok": False, "error": "auto_bind_failed"}

    # TTL check
    if time.time() > s["expires_at"]:
        return {"ok": False, "error": "lock_expired",
                "expired_at": datetime.fromtimestamp(s["expires_at"]).isoformat()}

    # Comparar
    locked_ja4 = s["ja4_fp"]
    drifted = locked_ja4 != observed_ja4

    obs = {
        "ts": datetime.now().isoformat(),
        "request_url_short": (request_url or "")[:80],
        "request_phase": request_phase,
        "observed_ja4": observed_ja4,
        "locked_ja4": locked_ja4,
        "drifted": drifted,
    }

    with _lock:
        s["validation_count"] += 1
        if drifted:
            s["drift_count"] += 1
            _metrics["validations_drifted"] += 1
            # Log incident
            incident = {
                **obs,
                "session_id": session_id,
                "drift_count": s["drift_count"],
            }
            _drift_incidents.insert(0, incident)
            if len(_drift_incidents) > cfg["incidents_max"]:
                _drift_incidents.pop()
            _metrics["incidents_logged"] += 1
        else:
            _metrics["validations_passed"] += 1
        # Append observation
        obs_list = _session_observations.setdefault(session_id, [])
        obs_list.append(obs)
        if len(obs_list) > cfg["max_observations_per_session"]:
            obs_list.pop(0)

    return {
        "ok": True,
        "drift": drifted,
        "ja4_locked": locked_ja4[:30] + "...",
        "ja4_observed": observed_ja4[:30] + "...",
        "session_id": session_id,
        "phase": request_phase,
        "validation_count": s["validation_count"],
        "drift_count": s["drift_count"],
    }


def get_session_history(session_id: str,
                        limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        return list(_session_observations.get(session_id, []))[-limit:]


def detect_drift_pattern(session_id: str) -> Dict[str, Any]:
    """Analiza el historial para clasificar tipo de drift.

    Returns:
      {drifted, drift_count, drift_phases: [...],
       multi_browser_signals: bool}
    """
    hist = get_session_history(session_id)
    if not hist:
        return {"drifted": False, "no_observations": True}

    drifts = [o for o in hist if o.get("drifted")]
    if not drifts:
        return {"drifted": False, "drift_count": 0,
                 "total_observations": len(hist)}

    # Phases en las que ocurrió drift
    phases = list({o.get("request_phase") or "?" for o in drifts})
    # Distintos JA4 observados
    distinct_ja4s = list({o["observed_ja4"] for o in drifts})

    return {
        "drifted": True,
        "drift_count": len(drifts),
        "total_observations": len(hist),
        "drift_phases": phases,
        "distinct_observed_ja4s": len(distinct_ja4s),
        "multi_browser_signals": len(distinct_ja4s) >= 3,
    }


# ═══════════════════════════════════════════════════════════════
# FORCE REPLAY (re-bind con JA4 de otra session)
# ═══════════════════════════════════════════════════════════════
def force_replay_ja4(session_id: str, source_session_id: str) -> Dict[str, Any]:
    """Cuando un session drifteó, re-bindea con JA4 de otra session conocida.

    Útil cuando proxy_rotator cambia exit IP y pierde JA4 implícito.
    """
    src_ja4 = get_session_ja4(source_session_id)
    if not src_ja4:
        return {"ok": False, "error": "source_session_not_bound_or_expired"}

    # Release target y re-bind con src
    release_session(session_id)
    r = bind_session_ja4(session_id, ja4_fp=src_ja4)
    with _lock:
        _metrics["force_replays"] += 1
    return {**r, "replayed_from": source_session_id}


# ═══════════════════════════════════════════════════════════════
# INCIDENTS / METRICS
# ═══════════════════════════════════════════════════════════════
def get_drift_incidents(limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        return list(_drift_incidents[:limit])


def clear_drift_incidents() -> Dict[str, Any]:
    with _lock:
        n = len(_drift_incidents)
        _drift_incidents.clear()
    return {"ok": True, "cleared": n}


def get_metrics() -> Dict[str, Any]:
    with _lock:
        active_sessions = sum(1 for s in _session_locks.values()
                                if time.time() < s["expires_at"])
        total_validations = (_metrics["validations_passed"]
                              + _metrics["validations_drifted"])
        drift_pct = round(
            100.0 * _metrics["validations_drifted"] / max(1, total_validations), 2
        )
        return {
            **_metrics,
            "active_sessions": active_sessions,
            "total_validations": total_validations,
            "drift_pct": drift_pct,
            "incidents_in_memory": len(_drift_incidents),
            "ja4_pinning_available": PINNING_AVAILABLE,
        }


def reset_metrics():
    with _lock:
        for k in _metrics:
            _metrics[k] = 0


def cleanup_expired() -> Dict[str, Any]:
    """Elimina sessions expiradas."""
    now = time.time()
    removed = 0
    with _lock:
        for sid in list(_session_locks.keys()):
            if now > _session_locks[sid]["expires_at"]:
                _session_locks.pop(sid)
                _session_observations.pop(sid, None)
                removed += 1
    return {"ok": True, "removed_count": removed}


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_has_ttl"] = "default_lock_ttl_s" in cfg
    checks["config_auto_bind"] = cfg["auto_bind_on_first_observation"] is True

    # 2. Bind session
    sid = "ja4cont_test_001"
    r = bind_session_ja4(sid, ja4_fp="t13d1516h2_TEST_ja4")
    checks["bind_ok"] = r.get("ok") is True

    # 3. Bind duplicate fails
    r = bind_session_ja4(sid, ja4_fp="other_ja4")
    checks["bind_duplicate_fails"] = r.get("ok") is False

    # 4. Get session JA4
    ja4 = get_session_ja4(sid)
    checks["get_ja4_works"] = ja4 == "t13d1516h2_TEST_ja4"

    # 5. Get lock info
    info = get_session_lock_info(sid)
    checks["get_lock_info_works"] = info is not None
    checks["lock_info_has_ttl"] = info and "ttl_remaining_s" in info

    # 6. Validate matching JA4 → no drift
    r = validate_request(sid, "t13d1516h2_TEST_ja4",
                          request_url="https://www.ticketmaster.com/queue",
                          request_phase="queue")
    checks["validate_match_ok"] = r.get("ok") is True
    checks["validate_no_drift"] = r.get("drift") is False

    # 7. Validate wrong JA4 → drift
    r = validate_request(sid, "t13d_DIFFERENT_ja4",
                          request_url="https://www.ticketmaster.com/cart",
                          request_phase="cart")
    checks["validate_drift_detected"] = r.get("drift") is True

    # 8. Drift incidents recorded
    incidents = get_drift_incidents(limit=10)
    checks["incidents_logged"] = len(incidents) >= 1

    # 9. Drift pattern analysis
    p = detect_drift_pattern(sid)
    checks["pattern_drifted"] = p.get("drifted") is True
    checks["pattern_drift_count"] = p.get("drift_count") >= 1

    # 10. History
    hist = get_session_history(sid)
    checks["history_returns_list"] = isinstance(hist, list)
    checks["history_count"] = len(hist) >= 2

    # 11. Auto-bind on first observation (sin previo bind)
    sid_auto = "ja4cont_auto_001"
    r = validate_request(sid_auto, "t13d_auto_bound_ja4",
                          request_phase="warm")
    checks["auto_bind_works"] = r.get("ok") is True
    # Después de auto-bind, validation con mismo JA4 → no drift
    r = validate_request(sid_auto, "t13d_auto_bound_ja4",
                          request_phase="cart")
    checks["auto_bind_same_no_drift"] = r.get("drift") is False

    # 12. Force replay
    r = force_replay_ja4(sid, source_session_id=sid_auto)
    checks["force_replay_ok"] = r.get("ok") is True
    new_ja4 = get_session_ja4(sid)
    checks["replay_changed_ja4"] = new_ja4 == "t13d_auto_bound_ja4"

    # 13. List active
    active = list_active_sessions()
    checks["list_active_returns_list"] = isinstance(active, list)
    checks["list_active_count"] = len(active) >= 2

    # 14. Release
    r = release_session(sid)
    checks["release_ok"] = r.get("ok") is True
    checks["released_session_no_ja4"] = get_session_ja4(sid) is None

    # 15. Metrics
    m = get_metrics()
    checks["metrics_sessions_bound"] = m["sessions_bound"] >= 2
    checks["metrics_validations"] = m["total_validations"] >= 3
    checks["metrics_drift_pct"] = "drift_pct" in m
    checks["metrics_auto_binds"] = m["auto_binds"] >= 1

    # 16. Cleanup expired
    r = cleanup_expired()
    checks["cleanup_ok"] = r.get("ok") is True

    # Cleanup
    release_session(sid_auto)

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "ja4_session_continuity",
        "version": 1,
        "metrics": get_metrics(),
        "purpose": "JA4+ inter-request continuity per session (SOTA P0-2)",
    }
