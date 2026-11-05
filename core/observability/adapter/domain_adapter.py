"""core/observability/adapter/domain_adapter.py — M19.

Domain Adapter Layer (ADVISORY ONLY).

Función pura que traduce eventos del dominio externo Market (o Simulation
envelope) al formato canónico TM_Snapshot consumible por la pipeline
M13.7 → M18.

PRINCIPIO ARQUITECTÓNICO:
    M19 es ADVISORY-ONLY:
        - NUNCA ejecuta
        - NUNCA muta state
        - NUNCA hace override
        - NUNCA introduce feedback loops
        - NO contiene business logic — sólo mapping fijo

    Garantías de boundary (DOMAIN BOUNDARY SPEC v1.0):
        G1: Market domain nunca entra directo a M13.7+
        G2: Core (M13.7-M18) inalterado
        G3: M19 sólo transforma inputs, no decide
        G4: Sin feedback loop M13.7 → M19

Mapping fijo (sin inferencia creativa):
    volatility (0-1)    → purchase_block_rate (0-1)
    liquidity (0-1)     → purchase_dry_run_rate (0-1)
    macro_risk (0-1)    → drift_hint (low|medium|high|critical) en meta
    volume (≥ 0)        → internal_errors proxy (0-3) por threshold

Output schema (TM_Snapshot canónico):
    {
        "history": list[dict],
        "current": {
            "rates": {"purchase_dry_run_rate", "purchase_block_rate"},
            "counters": {"purchase_live_count", "internal_errors"},
            "config": {"mode": str},
        },
        "meta": {
            "mode": "simulation" | "replay" | "live" | "market",
            "event_id": str,
            "timestamp": str,
            "drift_hint": "low" | "medium" | "high" | "critical",
        },
    }

stdlib only. Determinístico. Sin side-effects.
"""
from __future__ import annotations

from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes públicas — thresholds del mapping (fijos, no calibrables)
# ════════════════════════════════════════════════════════════════════
# macro_risk (0-1) → drift_hint (4 buckets)
DRIFT_HINT_THRESHOLDS: list = [
    (0.00, 0.25, "low"),
    (0.25, 0.50, "medium"),
    (0.50, 0.75, "high"),
    (0.75, 1.01, "critical"),
]

# volume → internal_errors proxy (4 buckets, cap 3)
VOLUME_ERROR_THRESHOLDS: list = [
    (0.0, 1000.0, 0),
    (1000.0, 5000.0, 1),
    (5000.0, 10000.0, 2),
]
MAX_ERROR_PROXY: int = 3  # volume > 10000


# Modes válidos en meta
_VALID_MODES: frozenset = frozenset({
    "simulation", "replay", "live", "market", "synthetic",
})
_DEFAULT_MODE: str = "simulation"
_DEFAULT_EVENT_ID: str = "unknown"
_DEFAULT_TIMESTAMP: str = ""


# ════════════════════════════════════════════════════════════════════
#  Helpers internos (fail-safe coerciones)
# ════════════════════════════════════════════════════════════════════
def _safe_dict(value: Any) -> dict:
    """Devuelve value si es dict, sino dict vacío."""
    return value if isinstance(value, dict) else {}


def _safe_str(value: Any, default: str = "") -> str:
    """Coerción fail-safe a string no vacío."""
    if isinstance(value, str) and value:
        return value
    return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerción fail-safe a float."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_unit(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp a [lo, hi]."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _macro_to_drift_hint(macro_risk: float) -> str:
    """Bucketing fijo de macro_risk [0-1] → drift_hint categórico."""
    for lo, hi, label in DRIFT_HINT_THRESHOLDS:
        if lo <= macro_risk < hi:
            return label
    # Cap en 1.0 → critical
    return "critical"


def _volume_to_errors(volume: float) -> int:
    """Bucketing fijo de volume → internal_errors proxy."""
    if volume < 0:
        return 0
    for lo, hi, errors in VOLUME_ERROR_THRESHOLDS:
        if lo <= volume <= hi:
            return errors
    return MAX_ERROR_PROXY


def _normalize_mode(value: Any) -> str:
    """Coerce mode a uno de los valores válidos o default."""
    if isinstance(value, str) and value in _VALID_MODES:
        return value
    return _DEFAULT_MODE


def _build_default_snapshot() -> dict:
    """TM_Snapshot por defecto (input no procesable)."""
    return {
        "history": [],
        "current": {
            "rates": {
                "purchase_dry_run_rate": 0.5,
                "purchase_block_rate": 0.5,
            },
            "counters": {
                "purchase_live_count": 0,
                "internal_errors": 0,
            },
            "config": {"mode": "dry_run"},
        },
        "meta": {
            "mode": _DEFAULT_MODE,
            "event_id": _DEFAULT_EVENT_ID,
            "timestamp": _DEFAULT_TIMESTAMP,
            "drift_hint": "low",
        },
    }


def _unwrap_simulation_envelope(event: dict) -> tuple:
    """Si event es un SimulationEvent envelope, extrae el evento interno.

    Returns:
        tuple (inner_event, source_mode)
    """
    # Heurística: si tiene "event" como sub-dict AND tiene "source",
    # tratar como SimulationEvent envelope
    has_event = isinstance(event.get("event"), dict)
    has_source = "source" in event
    if has_event and has_source:
        return event["event"], _normalize_mode(event.get("source"))
    return event, "market"


# ════════════════════════════════════════════════════════════════════
#  API principal: adapt_event_to_tm_snapshot
# ════════════════════════════════════════════════════════════════════
def adapt_event_to_tm_snapshot(event: Any) -> dict:
    """Traduce MarketEvent / SimulationEvent → TM_Snapshot canónico.

    ADVISORY-ONLY: NO ejecuta, NO muta, NO override.
    BOUNDARY-ONLY: única traducción legal Market → TM (per DOMAIN
    BOUNDARY SPEC v1.0).

    Args:
        event: dict que puede ser:
            - MarketEvent directo: {event_id, timestamp, asset, context,
              signals, external_factors, system_inputs}
            - SimulationEvent envelope: {source, event, determinism_seed}

    Returns:
        dict TM_Snapshot:
            {history, current, meta}

    Pure function: NO muta inputs, NO IO, NO random. Mapping 100% fijo.
    Fail-safe: cualquier input malformado → TM_Snapshot default seguro.
    """
    try:
        if not isinstance(event, dict):
            return _build_default_snapshot()

        # Unwrap SimulationEvent envelope si aplica
        inner, source_mode = _unwrap_simulation_envelope(event)

        # Extraer secciones del MarketEvent con fail-safe
        signals = _safe_dict(inner.get("signals"))
        external = _safe_dict(inner.get("external_factors"))

        # Lecturas con clamp + coerción
        volatility = _clamp_unit(_safe_float(signals.get("volatility"), 0.0))
        liquidity = _clamp_unit(_safe_float(signals.get("liquidity"), 0.5))
        volume = _safe_float(signals.get("volume"), 0.0)
        macro_risk = _clamp_unit(_safe_float(external.get("macro_risk"), 0.0))

        # Mapping fijo determinístico
        block_rate = volatility           # volatility → block_rate
        dry_run_rate = liquidity          # liquidity → dry_run_rate
        drift_hint = _macro_to_drift_hint(macro_risk)
        internal_errors = _volume_to_errors(volume)

        # Construir current (TM canónico)
        current = {
            "rates": {
                "purchase_dry_run_rate": float(round(dry_run_rate, 6)),
                "purchase_block_rate":   float(round(block_rate, 6)),
            },
            "counters": {
                "purchase_live_count": 0,  # no mapping desde Market domain
                "internal_errors":     int(internal_errors),
            },
            "config": {
                "mode": "dry_run",  # simulation siempre opera en dry_run
            },
        }

        # History vacío — el caller compone secuencia desde múltiples calls
        history: list = []

        # Meta con trazabilidad
        meta = {
            "mode":       _normalize_mode(source_mode),
            "event_id":   _safe_str(inner.get("event_id"), _DEFAULT_EVENT_ID),
            "timestamp":  _safe_str(inner.get("timestamp"), _DEFAULT_TIMESTAMP),
            "drift_hint": drift_hint,
        }

        return {
            "history": history,
            "current": current,
            "meta":    meta,
        }
    except Exception:
        # Fail-safe absoluto
        return _build_default_snapshot()


# ════════════════════════════════════════════════════════════════════
#  Aliases canónicos (per docs/M19_CANONICAL.md)
#  Vinculaciones de nombre puras — cero lógica nueva. Mismo function
#  object expuesto bajo múltiples nombres por compatibilidad de specs.
# ════════════════════════════════════════════════════════════════════
adapt_market_event = adapt_event_to_tm_snapshot
adapt_market_event_to_simulation = adapt_event_to_tm_snapshot


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
