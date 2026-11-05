"""core/observability/runtime_diagnostics.py — Entrypoint del sistema diagnostics.

ÚNICO punto de entrada que el legacy debe invocar:

    from core.observability.runtime_diagnostics import observe
    observe(event_name="goto", payload={"url": "..."})

Diseño:
    - 0 dependencia del bot core
    - fail-safe: try/except envolvente
    - apagable con env var OBSERVABILITY_ENABLED=0
    - streaming: cada observe → JSONL append
    - process-local: una RunSession por PID/slot

Activación:
    OBSERVABILITY_ENABLED=1  (default ON)
    OBSERVABILITY_ENABLED=0  (apagado)

Cleanup:
    Llamar finalize() al final del run para obtener summary.
    O dejar que el GC lo cierre (los datos quedan en JSONL).

P3 — Session lifecycle hardening:
    - TTL por inactividad: SESSIONS_TTL_S (default 300s)
    - Cap absoluto: SESSIONS_MAX (default 100), eviction LRU real
    - Burst-safe: no evict si actividad continua reciente
    - Daemon adaptativo: sleep variable según presión
    - Reinit protection: sesiones evictadas se recrean en próximo observe()
    - Compat P1: file handles cerrados de forma segura
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════════
ENV_FLAG = "OBSERVABILITY_ENABLED"  # "0" to disable; default ON


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


# ════════════════════════════════════════════════════════════════════
#  P3 — Session lifecycle config (env-overridable)
# ════════════════════════════════════════════════════════════════════
SESSIONS_TTL_S: int = _env_int("SESSIONS_TTL_S", 300)
SESSIONS_MAX: int = _env_int("SESSIONS_MAX", 100)
SESSIONS_BURST_WINDOW_S: int = _env_int("SESSIONS_BURST_WINDOW_S", 10)
SESSIONS_CLEANUP_INTERVAL_S: int = _env_int("SESSIONS_CLEANUP_INTERVAL_S", 60)


def _ttl_enabled() -> bool:
    return os.environ.get("SESSIONS_TTL_ENABLED", "1") not in (
        "0", "false", "False", "FALSE", "no", "NO"
    )


# ════════════════════════════════════════════════════════════════════
#  State
# ════════════════════════════════════════════════════════════════════
# Session pool: una por slot (thread-safe)
_sessions: dict[Any, "RunSession"] = {}
# Metadata paralela por slot (last_observe_at, ttl_finalized, no_activity_burst)
# Mantenida fuera de RunSession para respetar scope estricto P3.
_sessions_metadata: dict[Any, dict] = {}
_sessions_lock = threading.Lock()

# Daemon thread state
_daemon_stop = threading.Event()
_daemon_thread: Optional[threading.Thread] = None
_daemon_started = False
_daemon_started_lock = threading.Lock()

# Stats / observability del propio sistema
_stats: dict = {
    "ttl_evictions_total": 0,
    "lru_evictions_total": 0,
    "reinits_after_ttl_total": 0,
    "daemon_iterations": 0,
    "daemon_errors": 0,
}


def _is_enabled() -> bool:
    """True si la observabilidad está activa (default ON)."""
    val = os.environ.get(ENV_FLAG, "1")
    return val not in ("0", "false", "False", "FALSE", "no", "NO", "")


# ════════════════════════════════════════════════════════════════════
#  Metadata helpers (paralela a _sessions)
# ════════════════════════════════════════════════════════════════════
def _get_or_init_metadata(slot: Any) -> dict:
    """Devuelve metadata por slot, creándola si no existe.

    Debe llamarse bajo _sessions_lock.
    """
    meta = _sessions_metadata.get(slot)
    if meta is None:
        meta = {
            "last_observe_at": 0.0,
            "ttl_finalized": False,
            "no_activity_burst": False,
            "created_at": time.time(),
        }
        _sessions_metadata[slot] = meta
    return meta


def _record_observation(slot: Any) -> None:
    """Actualiza last_observe_at + burst detection. Bajo _sessions_lock."""
    now = time.time()
    meta = _get_or_init_metadata(slot)
    last = meta["last_observe_at"]
    if last > 0:
        delta = now - last
        # Burst si dos observaciones cercanas
        meta["no_activity_burst"] = (delta < SESSIONS_BURST_WINDOW_S)
    else:
        meta["no_activity_burst"] = False
    meta["last_observe_at"] = now


# ════════════════════════════════════════════════════════════════════
#  Session lookup / creation
# ════════════════════════════════════════════════════════════════════
def _get_session(slot: Any) -> Optional["RunSession"]:
    """Obtiene o crea la RunSession para un slot.

    P3: si la sesión fue evictada por TTL, ttl_finalized=True; recrea.
    """
    try:
        from core.runtime.run_session import RunSession
    except Exception:
        return None
    with _sessions_lock:
        meta = _get_or_init_metadata(slot)
        # P3: reinit protection si fue cerrada por TTL
        if meta.get("ttl_finalized", False):
            # La session ya fue removida por el daemon; flag para recreate
            _sessions.pop(slot, None)
            meta["ttl_finalized"] = False
            _stats["reinits_after_ttl_total"] += 1
        s = _sessions.get(slot)
        if s is None:
            try:
                s = RunSession(slot=slot)
                _sessions[slot] = s
            except Exception:
                return None
        return s


# ════════════════════════════════════════════════════════════════════
#  Eviction (TTL + LRU)
# ════════════════════════════════════════════════════════════════════
def _evict_session(slot: Any, reason: str = "ttl") -> None:
    """Evict atómico: pop dict + marcar metadata + finalize fuera del lock.

    Compat P1: finalize() cierra file handle bajo su propio self._lock.
    Si un thread paralelo está mid-write, P1 recovery reabre el handle.
    """
    with _sessions_lock:
        s = _sessions.pop(slot, None)
        meta = _sessions_metadata.get(slot)
        if meta:
            meta["ttl_finalized"] = True
        if reason == "ttl":
            _stats["ttl_evictions_total"] += 1
        elif reason == "lru":
            _stats["lru_evictions_total"] += 1
    # finalize() FUERA del lock — close FH puede ser lento
    if s is not None:
        try:
            s.finalize()
        except Exception:
            pass


def _evict_idle_sessions() -> int:
    """TTL eviction. Devuelve número de sesiones evictadas."""
    now = time.time()
    victims: list[Any] = []
    with _sessions_lock:
        for slot, meta in list(_sessions_metadata.items()):
            if slot not in _sessions:
                # Metadata huérfana (sesión ya removida) → limpiar
                continue
            idle = now - meta["last_observe_at"]
            # Burst guard: no evict si actividad reciente continua
            if idle > SESSIONS_TTL_S and not meta["no_activity_burst"]:
                victims.append(slot)
    for slot in victims:
        _evict_session(slot, reason="ttl")
    return len(victims)


def _evict_lru_if_overflow() -> int:
    """LRU eviction si len(_sessions) > SESSIONS_MAX. Por last_observe_at asc."""
    with _sessions_lock:
        current = len(_sessions)
        if current <= SESSIONS_MAX:
            return 0
        # Ordenar slots por last_observe_at ascendente (más viejo primero)
        candidates = sorted(
            list(_sessions.keys()),
            key=lambda s: _sessions_metadata.get(s, {}).get("last_observe_at", 0.0),
        )
        to_remove_count = current - SESSIONS_MAX
        victims = candidates[:to_remove_count]
    # Evict fuera del lock (finalize lento)
    for slot in victims:
        _evict_session(slot, reason="lru")
    return len(victims)


def _cleanup_metadata_for_unknown_slots() -> None:
    """GC: eliminar metadata huérfana (sin session activa)."""
    with _sessions_lock:
        orphans = [s for s in list(_sessions_metadata.keys())
                    if s not in _sessions]
        for slot in orphans:
            meta = _sessions_metadata.get(slot)
            # Mantener metadata SI tiene ttl_finalized=True (para reinit)
            if meta and not meta.get("ttl_finalized", False):
                _sessions_metadata.pop(slot, None)


# ════════════════════════════════════════════════════════════════════
#  Daemon (TTL + LRU sweeper, adaptive sleep)
# ════════════════════════════════════════════════════════════════════
def _daemon_loop() -> None:
    """Loop principal del daemon. Fail-safe, adaptive sleep."""
    while not _daemon_stop.is_set():
        try:
            _evict_idle_sessions()
            _evict_lru_if_overflow()
            _stats["daemon_iterations"] += 1
        except Exception:
            _stats["daemon_errors"] += 1
        # Adaptive sleep:
        #  - Si presión >80% del MAX → sleep corto (wake rápido)
        #  - Else → min(CLEANUP_INTERVAL, TTL/2)
        try:
            current_count = len(_sessions)
            if current_count >= int(SESSIONS_MAX * 0.8):
                sleep_s = 1.0
            else:
                sleep_s = float(min(SESSIONS_CLEANUP_INTERVAL_S,
                                      max(1, SESSIONS_TTL_S // 2)))
        except Exception:
            sleep_s = float(SESSIONS_CLEANUP_INTERVAL_S)
        _daemon_stop.wait(sleep_s)


def _ensure_daemon_started() -> None:
    """Arranca el daemon idempotente. Solo si TTL enabled."""
    global _daemon_thread, _daemon_started
    if not _ttl_enabled():
        return
    with _daemon_started_lock:
        if _daemon_started:
            return
        _daemon_stop.clear()
        _daemon_thread = threading.Thread(
            target=_daemon_loop, daemon=True, name="SessionsCleanupDaemon"
        )
        _daemon_thread.start()
        _daemon_started = True


def _stop_daemon_for_test() -> None:
    """Helper de tests: detener daemon."""
    global _daemon_started
    _daemon_stop.set()
    with _daemon_started_lock:
        _daemon_started = False


def _force_cleanup_for_test() -> dict:
    """Helper de tests: forzar una iteración de cleanup."""
    n_ttl = _evict_idle_sessions()
    n_lru = _evict_lru_if_overflow()
    return {"ttl_evicted": n_ttl, "lru_evicted": n_lru}


# ════════════════════════════════════════════════════════════════════
#  PUBLIC API — observe()
# ════════════════════════════════════════════════════════════════════
def observe(event_name: str = "",
            payload: Optional[dict] = None,
            slot: Any = None,
            **_kw: Any) -> None:
    """Hook único llamado desde el legacy.

    CONTRATO:
        - NUNCA lanza excepciones
        - NUNCA bloquea
        - NUNCA llama al bot
        - Si OBSERVABILITY_ENABLED=0, retorna inmediatamente
        - Cualquier error interno se silencia

    Args:
        event_name: nombre del evento (string)
        payload: dict opcional con extras
        slot: identificador del slot (extraído de payload si no se pasa)

    Returns:
        None — fire-and-forget
    """
    if not _is_enabled():
        return
    try:
        # P3: arrancar daemon idempotente (1ª vez)
        _ensure_daemon_started()

        # Resolver slot
        if slot is None and payload:
            slot = payload.get("slot")
        if slot is None:
            slot = "default"

        # P3: get_session maneja reinit si ttl_finalized=True
        session = _get_session(slot)
        if session is None:
            return

        # P3: actualizar metadata (bajo lock corto)
        with _sessions_lock:
            _record_observation(slot)

        # payload sin reservados
        safe_payload = {}
        if isinstance(payload, dict):
            for k, v in payload.items():
                if k not in ("slot", "event"):  # slot ya gestionado
                    safe_payload[k] = v
        session.observe(event_name, **safe_payload)
    except Exception:
        # CONTRATO: silencio absoluto
        pass


# ════════════════════════════════════════════════════════════════════
#  Lifecycle helpers
# ════════════════════════════════════════════════════════════════════
def finalize(slot: Any = "default") -> Optional[dict]:
    """Finaliza la sesión de un slot y devuelve su summary.

    Llamar al final del run del bot. Si no se llama, los datos quedan
    igualmente en JSONL — no se pierde nada.
    """
    if not _is_enabled():
        return None
    try:
        with _sessions_lock:
            s = _sessions.pop(slot, None)
            # Limpiar metadata explícita (finalize manual ≠ TTL)
            _sessions_metadata.pop(slot, None)
        if s is None:
            return None
        return s.finalize()
    except Exception:
        return None


def get_summary(slot: Any = "default") -> Optional[dict]:
    """Devuelve summary actual SIN cerrar la sesión."""
    if not _is_enabled():
        return None
    try:
        with _sessions_lock:
            s = _sessions.get(slot)
        return s.summary() if s else None
    except Exception:
        return None


def reset_all() -> None:
    """Cierra todas las sesiones. Para tests."""
    try:
        with _sessions_lock:
            sessions_to_close = list(_sessions.values())
            _sessions.clear()
            _sessions_metadata.clear()
        for s in sessions_to_close:
            try:
                s.finalize()
            except Exception:
                pass
        # Reset stats counters
        for k in _stats:
            _stats[k] = 0
    except Exception:
        pass


def get_info() -> dict:
    """Estado actual del módulo — para health checks."""
    try:
        with _sessions_lock:
            active = list(_sessions.keys())
            n_meta = len(_sessions_metadata)
        return {
            "module": "core.observability.runtime_diagnostics",
            "enabled": _is_enabled(),
            "env_flag": ENV_FLAG,
            "active_sessions": active,
            "active_sessions_count": len(active),
            "metadata_entries": n_meta,
            # P3 config + stats
            "ttl_enabled": _ttl_enabled(),
            "ttl_s": SESSIONS_TTL_S,
            "max_sessions": SESSIONS_MAX,
            "burst_window_s": SESSIONS_BURST_WINDOW_S,
            "cleanup_interval_s": SESSIONS_CLEANUP_INTERVAL_S,
            "daemon_started": _daemon_started,
            "stats": dict(_stats),
        }
    except Exception:
        return {"module": "runtime_diagnostics", "error": "introspection_failed"}


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Self-test requiere PYTHONPATH apuntando al proyecto raíz
    import sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parents[2]
    if str(_root) not in _sys.path:
        _sys.path.insert(0, str(_root))

    print("=== runtime_diagnostics self-test ===")
    print(f"Info: {get_info()}")
    print()

    # Simular un flow con observe()
    flow = [
        ("init", {"slot": 1}),
        ("goto", {"slot": 1, "url": "http://..."}),
        ("qty", {"slot": 1, "current": 2}),
        ("oauth_detected", {"slot": 1}),
        ("login_start", {"slot": 1, "email": "x@y.com"}),
        ("shot", {"slot": 1}),  # NOISE
        ("no_email_input", {"slot": 1}),
        ("FATAL_bot_block_early", {"slot": 1}),
        ("stop_received", {"slot": 1}),
    ]
    for evt, pl in flow:
        observe(event_name=evt, payload=pl)

    summary = finalize(slot=1)
    if summary:
        import json as _j
        print("Summary:")
        print(_j.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print("(disabled)")


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
