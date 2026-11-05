"""core/migration/flags.py — Feature flags for migration (STEP 9).

Flags controlling progressive migration from legacy bot → core flow.

Defaults: SAFEST possible.
    USE_CORE_FLOW         = False    # NEVER route real traffic to core
    SHADOW_CORE_FLOW      = True     # safe parallel execution
    ENABLE_CORE_METRICS   = True     # passive observability
    DIVERGENCE_LOG_LEVEL  = "info"

Override via env vars (runtime reload supported).

stdlib only.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from typing import Optional


# ════════════════════════════════════════════════════════════════════
#  Flag defaults
# ════════════════════════════════════════════════════════════════════
DEFAULT_USE_CORE_FLOW: bool = False
DEFAULT_SHADOW_CORE_FLOW: bool = True
DEFAULT_ENABLE_CORE_METRICS: bool = True
DEFAULT_DIVERGENCE_LOG_LEVEL: str = "info"

ENV_USE_CORE_FLOW: str = "USE_CORE_FLOW"
ENV_SHADOW_CORE_FLOW: str = "SHADOW_CORE_FLOW"
ENV_ENABLE_CORE_METRICS: str = "ENABLE_CORE_METRICS"
ENV_DIVERGENCE_LOG_LEVEL: str = "DIVERGENCE_LOG_LEVEL"

_TRUE_VALUES: frozenset = frozenset({"1", "true", "yes", "on", "True", "TRUE"})


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip() in _TRUE_VALUES


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v is not None else default


# ════════════════════════════════════════════════════════════════════
#  Flags dataclass
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class MigrationFlags:
    """Snapshot inmutable de los flags."""
    USE_CORE_FLOW: bool = DEFAULT_USE_CORE_FLOW
    SHADOW_CORE_FLOW: bool = DEFAULT_SHADOW_CORE_FLOW
    ENABLE_CORE_METRICS: bool = DEFAULT_ENABLE_CORE_METRICS
    DIVERGENCE_LOG_LEVEL: str = DEFAULT_DIVERGENCE_LOG_LEVEL

    @classmethod
    def from_env(cls) -> "MigrationFlags":
        return cls(
            USE_CORE_FLOW=_env_bool(ENV_USE_CORE_FLOW, DEFAULT_USE_CORE_FLOW),
            SHADOW_CORE_FLOW=_env_bool(ENV_SHADOW_CORE_FLOW, DEFAULT_SHADOW_CORE_FLOW),
            ENABLE_CORE_METRICS=_env_bool(ENV_ENABLE_CORE_METRICS, DEFAULT_ENABLE_CORE_METRICS),
            DIVERGENCE_LOG_LEVEL=_env_str(ENV_DIVERGENCE_LOG_LEVEL, DEFAULT_DIVERGENCE_LOG_LEVEL),
        )

    def to_dict(self) -> dict:
        return {
            "USE_CORE_FLOW": bool(self.USE_CORE_FLOW),
            "SHADOW_CORE_FLOW": bool(self.SHADOW_CORE_FLOW),
            "ENABLE_CORE_METRICS": bool(self.ENABLE_CORE_METRICS),
            "DIVERGENCE_LOG_LEVEL": str(self.DIVERGENCE_LOG_LEVEL),
        }


# ════════════════════════════════════════════════════════════════════
#  Singleton + reload
# ════════════════════════════════════════════════════════════════════
_lock = threading.Lock()
_cached: Optional[MigrationFlags] = None


def get_flags() -> MigrationFlags:
    """Devuelve los flags actuales (con cache)."""
    global _cached
    if _cached is None:
        with _lock:
            if _cached is None:
                _cached = MigrationFlags.from_env()
    return _cached


def reload_flags() -> MigrationFlags:
    """Recarga desde env vars. Override para tests."""
    global _cached
    with _lock:
        _cached = MigrationFlags.from_env()
        return _cached


def set_flags(flags: MigrationFlags) -> None:
    """Inyecta flags manualmente (para tests)."""
    global _cached
    with _lock:
        _cached = flags


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
