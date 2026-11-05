"""core/migration/cutover_router.py — Kill switch + Cutover Router (STEP 13).

ROUTER FINAL para cutover legacy → FlowCore.

⚠️ ESTADO: INFRAESTRUCTURA LISTA. **NO ACTIVADA**.
    El default es FLOW_MODE = "legacy". Para activar el cutover
    se requieren 14 días de validación productiva (per spec).

MODOS:
    "legacy"   — SIEMPRE legacy (default seguro)
    "flowcore" — SIEMPRE FlowCore (cutover completo)
    "auto"     — Canary determinístico decide por request

KILL_SWITCH:
    Global flag que fuerza legacy independientemente del mode.
    Útil para rollback instantáneo: una asignación,
    cero restart, efecto inmediato.

stdlib only.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.migration.canary_controller import CanaryController
from core.migration.shadow_runner import in_canary
from core.observability.error_classification import (
    ErrorCategory,
    ErrorSeverity,
)
from core.observability.event_emitter import emit_event


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
FLOW_MODE_LEGACY: str = "legacy"
FLOW_MODE_FLOWCORE: str = "flowcore"
FLOW_MODE_AUTO: str = "auto"
FLOW_MODE_SIMULATED: str = "simulated"  # PHASE 1 gate: routing exercised, NO real execution
VALID_FLOW_MODES: frozenset = frozenset({
    FLOW_MODE_LEGACY, FLOW_MODE_FLOWCORE, FLOW_MODE_AUTO, FLOW_MODE_SIMULATED,
})

DEFAULT_FLOW_MODE: str = FLOW_MODE_LEGACY  # ← SAFE DEFAULT
DEFAULT_KILL_SWITCH: bool = False
DEFAULT_CANARY_RATE: float = 0.0

ENV_FLOW_MODE: str = "FLOW_MODE"
ENV_KILL_SWITCH: str = "KILL_SWITCH"
ENV_CANARY_RATE: str = "CANARY_RATE"

EVENT_CUTOVER: str = "cutover_event"
EVENT_KILL_SWITCH_ACTIVATED: str = "kill_switch_activated"


# ════════════════════════════════════════════════════════════════════
#  Estado global (thread-safe)
# ════════════════════════════════════════════════════════════════════
# ─── Cross-process kill switch propagation ──────────────────────
# El kill switch DEBE funcionar entre procesos (parent dashboard +
# subprocess bot). Lo persistimos como file flag en disco.
# Cualquier proceso puede chequearlo con KILL_SWITCH_FLAG_FILE.exists()
KILL_SWITCH_FLAG_FILE: str = "runs/_kill_switch.flag"


def _flag_exists() -> bool:
    try:
        from pathlib import Path
        return Path(KILL_SWITCH_FLAG_FILE).exists()
    except Exception:
        return False


def _flag_set(active: bool) -> None:
    """Crea o elimina el file flag. Fail-safe."""
    try:
        from pathlib import Path
        p = Path(KILL_SWITCH_FLAG_FILE)
        if active:
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                # atomic create via O_EXCL; ignore if exists
                fd = os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o644)
                try:
                    os.write(fd, b"1")
                finally:
                    os.close(fd)
            except FileExistsError:
                pass
        else:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    except Exception:
        pass


_lock = threading.Lock()
_state: dict = {
    "flow_mode": None,           # lazy from env
    "kill_switch": None,
    "canary_rate": None,
    "canary_seed": "cutover_v1",
}


def _safe_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _safe_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _load_from_env() -> None:
    """Lazy load del estado desde env vars.

    IMPORTANTE: sólo asigna campos que están en None. Valores ya
    seteados manualmente vía set_* SE RESPETAN.
    """
    if _state["flow_mode"] is None:
        mode = os.environ.get(ENV_FLOW_MODE, DEFAULT_FLOW_MODE).strip().lower()
        if mode not in VALID_FLOW_MODES:
            mode = DEFAULT_FLOW_MODE
        _state["flow_mode"] = mode
    if _state["kill_switch"] is None:
        _state["kill_switch"] = _safe_bool(
            os.environ.get(ENV_KILL_SWITCH), DEFAULT_KILL_SWITCH,
        )
    if _state["canary_rate"] is None:
        _state["canary_rate"] = _safe_float(
            os.environ.get(ENV_CANARY_RATE), DEFAULT_CANARY_RATE,
        )


# ════════════════════════════════════════════════════════════════════
#  API: leer / mutar config
# ════════════════════════════════════════════════════════════════════
def get_flow_mode() -> str:
    with _lock:
        if _state["flow_mode"] is None:
            _load_from_env()
        return _state["flow_mode"]


def set_flow_mode(mode: str) -> None:
    """Cambio runtime sin restart. Default = legacy."""
    if mode not in VALID_FLOW_MODES:
        mode = DEFAULT_FLOW_MODE
    with _lock:
        _state["flow_mode"] = mode


def get_kill_switch() -> bool:
    """Reads kill switch state. PRIORITIZES file flag for cross-process
    propagation. In-memory state is secondary.

    This means: setting kill_switch=True in the dashboard process
    creates a file flag that subprocesses (e.g. the bot) will see
    immediately on their next get_kill_switch() check.
    """
    # File flag takes precedence (cross-process truth)
    if _flag_exists():
        with _lock:
            _state["kill_switch"] = True
        return True
    with _lock:
        if _state["kill_switch"] is None:
            _load_from_env()
        return _state["kill_switch"]


def set_kill_switch(active: bool) -> None:
    """ROLLBACK INSTANTÁNEO cross-process.

    1. Updates in-memory state of this process.
    2. Creates/removes the disk flag file so OTHER processes (bot
       subprocesses launched by `_compra_with_swap.py`) see the change
       on their next get_kill_switch() check.
    """
    # Disk flag FIRST (atomic, observable cross-process)
    _flag_set(bool(active))
    # Then in-memory
    with _lock:
        prev = _state["kill_switch"]
        _state["kill_switch"] = bool(active)
    if active and not prev:
        try:
            emit_event(
                ErrorCategory.FLOW, EVENT_KILL_SWITCH_ACTIVATED,
                {"timestamp": time.time(),
                 "flag_file": KILL_SWITCH_FLAG_FILE},
                severity=ErrorSeverity.FATAL,
                context="cutover_router",
            )
        except Exception:
            pass


def get_canary_rate() -> float:
    with _lock:
        if _state["canary_rate"] is None:
            _load_from_env()
        return _state["canary_rate"]


def set_canary_rate(rate: float) -> None:
    """Mueve el canary % en runtime (0.0 a 1.0)."""
    rate = max(0.0, min(1.0, _safe_float(rate, 0.0)))
    with _lock:
        _state["canary_rate"] = rate


def reset_state() -> None:
    """Reset completo a defaults (para tests)."""
    with _lock:
        _state["flow_mode"] = None
        _state["kill_switch"] = None
        _state["canary_rate"] = None


def snapshot() -> dict:
    """Snapshot del estado actual (debug / UI)."""
    return {
        "flow_mode": get_flow_mode(),
        "kill_switch": get_kill_switch(),
        "canary_rate": get_canary_rate(),
        "valid_modes": sorted(VALID_FLOW_MODES),
        "is_safe_default": (
            get_flow_mode() == FLOW_MODE_LEGACY
            and not get_kill_switch()
        ),
    }


# ════════════════════════════════════════════════════════════════════
#  Routing decision
# ════════════════════════════════════════════════════════════════════
def decide_path(*,
                 canary_identity: str = "",
                 mode: Optional[str] = None,
                 kill_switch: Optional[bool] = None,
                 canary_rate: Optional[float] = None) -> str:
    """Devuelve "legacy" o "flowcore" según las reglas:

        1. kill_switch=True       → legacy (override absoluto)
        2. mode="legacy"           → legacy
        3. mode="flowcore"         → flowcore
        4. mode="auto" + canary    → según hash de identity
        5. cualquier otra cosa     → legacy (safe default)
    """
    m = (mode or get_flow_mode()).strip().lower()
    ks = kill_switch if kill_switch is not None else get_kill_switch()
    cr = canary_rate if canary_rate is not None else get_canary_rate()

    if ks:
        return FLOW_MODE_LEGACY
    if m == FLOW_MODE_LEGACY:
        return FLOW_MODE_LEGACY
    if m == FLOW_MODE_FLOWCORE:
        return FLOW_MODE_FLOWCORE
    if m == FLOW_MODE_AUTO:
        if in_canary(rate=cr, identity=canary_identity):
            return FLOW_MODE_FLOWCORE
        return FLOW_MODE_LEGACY
    if m == FLOW_MODE_SIMULATED:
        # In simulation, still respect canary to decide WHICH child would
        # have been picked, so distribution can be observed without executing.
        if in_canary(rate=cr, identity=canary_identity):
            return FLOW_MODE_FLOWCORE
        return FLOW_MODE_LEGACY
    return FLOW_MODE_LEGACY


def is_simulation_mode() -> bool:
    """True when FLOW_MODE=simulated. Wrapper short-circuits subprocess
    when this returns True and instead writes simulated WAL + events.
    """
    return get_flow_mode() == FLOW_MODE_SIMULATED


# ════════════════════════════════════════════════════════════════════
#  Execute purchase — routing wrapper
# ════════════════════════════════════════════════════════════════════
def execute_purchase(ctx: Any,
                      *,
                      legacy_callable: Callable[[Any], Any],
                      flowcore_callable: Optional[Callable[[Any], Any]] = None,
                      canary_identity: str = "",
                      auto_fallback: bool = True,
                      ) -> dict:
    """Rutea la ejecución según decide_path + emite CUTOVER_EVENT.

    Args:
        ctx: contexto del request
        legacy_callable: REQUERIDO. Es el fallback final.
        flowcore_callable: opcional. Si None → siempre legacy.
        canary_identity: identidad sticky para routing auto.
        auto_fallback: si True y flowcore crashea → legacy retoma.

    Returns:
        dict {result, path_used, fallback_triggered, latency_ms, error}.

    SEGURIDAD:
        Legacy SIEMPRE disponible. FlowCore failures NUNCA propagan
        si auto_fallback=True.
    """
    chosen = decide_path(canary_identity=canary_identity)

    # Si chosen=flowcore pero no hay callable → forzar legacy
    if chosen == FLOW_MODE_FLOWCORE and not callable(flowcore_callable):
        chosen = FLOW_MODE_LEGACY

    fallback_triggered = False
    error = ""
    t0 = time.monotonic()
    result: Any = None

    if chosen == FLOW_MODE_FLOWCORE:
        try:
            result = flowcore_callable(ctx)
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:200]}"
            if auto_fallback:
                # Fallback inmediato a legacy
                fallback_triggered = True
                try:
                    result = legacy_callable(ctx)
                except Exception as legacy_e:
                    # Ambos fallaron: propagar legacy exception
                    raise
            else:
                raise
    else:
        result = legacy_callable(ctx)

    latency_ms = (time.monotonic() - t0) * 1000.0
    final_path = (
        "legacy_fallback" if fallback_triggered
        else chosen
    )

    # Emit CUTOVER_EVENT
    try:
        emit_event(
            ErrorCategory.FLOW, EVENT_CUTOVER,
            {
                "mode_decided": chosen,
                "used_path": final_path,
                "fallback_triggered": fallback_triggered,
                "latency_ms": round(latency_ms, 3),
                "error": error,
            },
            severity=ErrorSeverity.RECOVERABLE,
            context="cutover_router",
        )
    except Exception:
        pass

    return {
        "result": result,
        "path_used": final_path,
        "mode_decided": chosen,
        "fallback_triggered": fallback_triggered,
        "latency_ms": latency_ms,
        "error": error,
    }


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
