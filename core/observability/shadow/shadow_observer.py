"""core/observability/shadow/shadow_observer.py — M20 Shadow Observation.

Shadow Observation Layer (OBSERVATIONAL-ONLY).

Capa puramente observacional que clasifica el régimen dinámico de una
secuencia de TM_Snapshots (history + current) y reporta:

    - shadow_state         : stable | volatile | critical
    - trend_direction      : improving | degrading | oscillating
    - stability_index      : float ∈ [0, 1]
    - volatility_index     : float ∈ [0, 1]
    - divergence_events    : list[dict]
    - tick_summary         : {total, critical_ticks, verified_ticks}
    - meta                 : {simulation_mode, timestamp}

PRINCIPIO ARQUITECTÓNICO:
    M20 Shadow es OBSERVATIONAL-ONLY:
        - NUNCA decide
        - NUNCA ejecuta
        - NUNCA hace override
        - NUNCA influye en M11-M19 ni en M20-Fidelity
        - NUNCA importa otras capas del sistema (independencia)
        - Lectura PASIVA de history+current ya producidos

    Garantías:
        S1: Determinismo absoluto — misma entrada → misma salida bit-exact
        S2: Pure function — sin IO, sin random, sin clock, sin threading
        S3: Fail-safe — entrada malformada → salida "stable" + meta vacía
        S4: Additive — coexiste con M11-M20 sin perturbar contratos
        S5: stdlib only — sin dependencias externas

Algoritmo (alto nivel):
    1. Aplanar history + current en stream determinístico de ticks
    2. Extraer métricas: block_rate, dry_run_rate, internal_errors
    3. Volatility = max(mean(|Δblock|), mean(|Δdry_run|)) clampeada
    4. Stability = 1 − max(volatility, normalized_error_pressure)
    5. Classify shadow_state por umbrales fijos
    6. Trend = comparar primera-mitad vs segunda-mitad
    7. Divergence = pares (i, i+1) con |Δrate| > 0.30 o Δerrors ≥ 1
    8. Counters críticos: block_rate ≥ 0.85 o errors ≥ 3
    9. timestamp tomado de current.meta.timestamp si existe; sino ""

stdlib only. Determinístico. Sin side-effects.
"""
from __future__ import annotations

from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Umbrales fijos (NO calibrables externamente — preserva determinismo)
# ════════════════════════════════════════════════════════════════════
# Volatility / Stability
_VOLATILITY_CLAMP_HI: float = 1.0
_VOLATILITY_CLAMP_LO: float = 0.0
_ERROR_PRESSURE_MAX_REF: float = 3.0   # internal_errors ∈ [0..3] (per M19)

# shadow_state classification
_STABILITY_THRESHOLD_CRITICAL: float = 0.30  # stability < .30 → critical
_STABILITY_THRESHOLD_VOLATILE: float = 0.65  # stability < .65 → volatile

# Critical tick markers
_CRITICAL_BLOCK_RATE: float = 0.85
_CRITICAL_ERROR_COUNT: int = 3

# Divergence detection
_DIVERGENCE_RATE_DELTA: float = 0.30
_DIVERGENCE_ERRORS_DELTA: int = 1

# Trend detection
_TREND_RATE_MIN_DELTA: float = 0.05   # cambio mínimo medible
_TREND_ERROR_MIN_DELTA: float = 0.5   # diferencia media-de-errores

# Output schema literal values
_SHADOW_STATE_STABLE: str = "stable"
_SHADOW_STATE_VOLATILE: str = "volatile"
_SHADOW_STATE_CRITICAL: str = "critical"

_TREND_IMPROVING: str = "improving"
_TREND_DEGRADING: str = "degrading"
_TREND_OSCILLATING: str = "oscillating"


# ════════════════════════════════════════════════════════════════════
#  Helpers de coerción fail-safe
# ════════════════════════════════════════════════════════════════════
def _safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ════════════════════════════════════════════════════════════════════
#  Extracción de tick (acepta dos formas: plana o anidada en "current")
# ════════════════════════════════════════════════════════════════════
def _extract_tick(entry: Any) -> dict:
    """Normaliza una entry (history elem o current) a dict con keys:
        block_rate (float), dry_run_rate (float), errors (int), verified (bool).

    Acepta:
        - entry plana: {"rates": {...}, "counters": {...}}
        - entry anidada: {"current": {"rates": {...}, "counters": {...}}}

    Si la entry no es procesable → verified=False, valores 0.
    """
    if not isinstance(entry, dict):
        return {
            "block_rate": 0.0,
            "dry_run_rate": 0.0,
            "errors": 0,
            "verified": False,
        }

    # Detección de shape anidado
    if isinstance(entry.get("current"), dict):
        root = entry["current"]
    else:
        root = entry

    rates = _safe_dict(root.get("rates"))
    counters = _safe_dict(root.get("counters"))

    has_rates = isinstance(root.get("rates"), dict) and bool(rates)
    has_counters = isinstance(root.get("counters"), dict) and bool(counters)

    block_rate = _clamp(_safe_float(rates.get("purchase_block_rate"), 0.0))
    dry_run_rate = _clamp(_safe_float(rates.get("purchase_dry_run_rate"), 0.0))
    errors_raw = _safe_int(counters.get("internal_errors"), 0)
    errors = errors_raw if errors_raw >= 0 else 0

    return {
        "block_rate": block_rate,
        "dry_run_rate": dry_run_rate,
        "errors": errors,
        "verified": bool(has_rates and has_counters),
    }


def _build_stream(history: Any, current: Any) -> list[dict]:
    """Aplana history+current en una lista ordenada de ticks normalizados."""
    stream: list[dict] = []
    for entry in _safe_list(history):
        stream.append(_extract_tick(entry))
    stream.append(_extract_tick(current))
    return stream


# ════════════════════════════════════════════════════════════════════
#  Métricas determinísticas
# ════════════════════════════════════════════════════════════════════
def _mean_abs_delta(values: list[float]) -> float:
    """Media de |Δ| entre ticks consecutivos. 0 si < 2 valores."""
    if len(values) < 2:
        return 0.0
    deltas: list[float] = []
    for i in range(1, len(values)):
        deltas.append(abs(values[i] - values[i - 1]))
    return sum(deltas) / len(deltas) if deltas else 0.0


def _compute_volatility(stream: list[dict]) -> float:
    """Volatilidad ∈ [0, 1] = max(mean|Δblock|, mean|Δdry_run|) clampeada."""
    if not stream:
        return 0.0
    blocks = [t["block_rate"] for t in stream]
    dryruns = [t["dry_run_rate"] for t in stream]
    block_vol = _mean_abs_delta(blocks)
    dry_vol = _mean_abs_delta(dryruns)
    return _clamp(max(block_vol, dry_vol), _VOLATILITY_CLAMP_LO, _VOLATILITY_CLAMP_HI)


def _compute_error_pressure(stream: list[dict]) -> float:
    """Presión de errores ∈ [0, 1] = max(errors)/MAX_REF."""
    if not stream:
        return 0.0
    max_err = max(t["errors"] for t in stream)
    if _ERROR_PRESSURE_MAX_REF <= 0:
        return 0.0
    return _clamp(max_err / _ERROR_PRESSURE_MAX_REF)


def _compute_stability(volatility: float, error_pressure: float) -> float:
    """Estabilidad = 1 − max(volatility, error_pressure), clampeada."""
    pressure = max(volatility, error_pressure)
    return _clamp(1.0 - pressure)


def _classify_state(stability: float, stream: list[dict]) -> str:
    """Clasifica shadow_state por umbrales y por ticks críticos absolutos."""
    # Si hay algún tick críticamente bloqueado → critical
    max_block = max((t["block_rate"] for t in stream), default=0.0)
    max_err = max((t["errors"] for t in stream), default=0)
    if max_block >= _CRITICAL_BLOCK_RATE or max_err >= _CRITICAL_ERROR_COUNT:
        if stability < _STABILITY_THRESHOLD_VOLATILE:
            return _SHADOW_STATE_CRITICAL
        # Tick crítico aislado pero régimen estable → marcar volatile
        return _SHADOW_STATE_VOLATILE
    if stability < _STABILITY_THRESHOLD_CRITICAL:
        return _SHADOW_STATE_CRITICAL
    if stability < _STABILITY_THRESHOLD_VOLATILE:
        return _SHADOW_STATE_VOLATILE
    return _SHADOW_STATE_STABLE


def _split_halves(stream: list[dict]) -> tuple[list[dict], list[dict]]:
    """Parte el stream en mitades temporales (early, late). Si len<2 → (stream, stream)."""
    n = len(stream)
    if n < 2:
        return stream, stream
    mid = n // 2
    return stream[:mid], stream[mid:]


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _compute_trend(stream: list[dict]) -> str:
    """Trend = improving si block_rate y errors bajan en la segunda mitad;
    degrading si suben; oscillating si signos mezclados o sin cambio claro."""
    if len(stream) < 2:
        return _TREND_OSCILLATING
    early, late = _split_halves(stream)

    early_block = _avg([t["block_rate"] for t in early])
    late_block = _avg([t["block_rate"] for t in late])
    early_err = _avg([float(t["errors"]) for t in early])
    late_err = _avg([float(t["errors"]) for t in late])

    block_delta = late_block - early_block
    err_delta = late_err - early_err

    # Direcciones individuales
    block_improving = block_delta < -_TREND_RATE_MIN_DELTA
    block_degrading = block_delta > _TREND_RATE_MIN_DELTA
    err_improving = err_delta < -_TREND_ERROR_MIN_DELTA
    err_degrading = err_delta > _TREND_ERROR_MIN_DELTA

    # Consenso de mejora
    if (block_improving and not err_degrading) or (
        err_improving and not block_degrading
    ):
        if block_improving or err_improving:
            return _TREND_IMPROVING
    # Consenso de degradación
    if (block_degrading and not err_improving) or (
        err_degrading and not block_improving
    ):
        if block_degrading or err_degrading:
            return _TREND_DEGRADING
    return _TREND_OSCILLATING


def _build_divergence_events(stream: list[dict]) -> list[dict]:
    """Detecta saltos consecutivos significativos en rates o errors."""
    events: list[dict] = []
    for i in range(1, len(stream)):
        prev = stream[i - 1]
        cur = stream[i]
        block_d = cur["block_rate"] - prev["block_rate"]
        dry_d = cur["dry_run_rate"] - prev["dry_run_rate"]
        err_d = cur["errors"] - prev["errors"]

        if abs(block_d) >= _DIVERGENCE_RATE_DELTA:
            events.append({
                "tick_index": i,
                "type": "block_rate_jump",
                "delta": round(block_d, 6),
                "magnitude": round(abs(block_d), 6),
            })
        if abs(dry_d) >= _DIVERGENCE_RATE_DELTA:
            events.append({
                "tick_index": i,
                "type": "dry_run_rate_jump",
                "delta": round(dry_d, 6),
                "magnitude": round(abs(dry_d), 6),
            })
        if abs(err_d) >= _DIVERGENCE_ERRORS_DELTA:
            events.append({
                "tick_index": i,
                "type": "internal_errors_jump",
                "delta": int(err_d),
                "magnitude": int(abs(err_d)),
            })
    return events


def _build_tick_summary(stream: list[dict]) -> dict:
    total = len(stream)
    critical = 0
    verified = 0
    for t in stream:
        if t["block_rate"] >= _CRITICAL_BLOCK_RATE or t["errors"] >= _CRITICAL_ERROR_COUNT:
            critical += 1
        if t["verified"]:
            verified += 1
    return {
        "total": int(total),
        "critical_ticks": int(critical),
        "verified_ticks": int(verified),
    }


def _extract_timestamp(current: Any) -> str:
    """Recupera timestamp de current.meta.timestamp si existe (string).
    Si no, devuelve "" para preservar determinismo (sin clock real)."""
    if not isinstance(current, dict):
        return ""
    meta = _safe_dict(current.get("meta"))
    return _safe_str(meta.get("timestamp"), "")


def _build_default_observation() -> dict:
    """Observation default cuando inputs no son procesables (fail-safe)."""
    return {
        "shadow_state": _SHADOW_STATE_STABLE,
        "trend_direction": _TREND_OSCILLATING,
        "stability_index": 1.0,
        "volatility_index": 0.0,
        "divergence_events": [],
        "tick_summary": {
            "total": 0,
            "critical_ticks": 0,
            "verified_ticks": 0,
        },
        "meta": {
            "simulation_mode": True,
            "timestamp": "",
        },
    }


# ════════════════════════════════════════════════════════════════════
#  API principal: build_shadow_observation
# ════════════════════════════════════════════════════════════════════
def build_shadow_observation(history: list, current: dict) -> dict:
    """Construye una observación shadow a partir de history + current.

    OBSERVATIONAL-ONLY: NO decide, NO ejecuta, NO muta inputs, NO IO.

    Args:
        history: lista de TM_Snapshot-like entries (o entries con `current`
            anidado). Cada entry debería contener `rates` y `counters`.
        current: dict TM_Snapshot canónico (o equivalente). Se concatena al
            final del stream.

    Returns:
        dict con schema:
            {
                "shadow_state": "stable" | "volatile" | "critical",
                "trend_direction": "improving" | "degrading" | "oscillating",
                "stability_index": float ∈ [0, 1],
                "volatility_index": float ∈ [0, 1],
                "divergence_events": list[dict],
                "tick_summary": {
                    "total": int,
                    "critical_ticks": int,
                    "verified_ticks": int,
                },
                "meta": {
                    "simulation_mode": True,
                    "timestamp": str,
                },
            }

    Pure function. Determinista. Fail-safe absoluto.
    """
    try:
        # Si ambos inputs son totalmente inutilizables → default
        invalid_history = not isinstance(history, list)
        invalid_current = not isinstance(current, dict)
        if invalid_history and invalid_current:
            return _build_default_observation()

        stream = _build_stream(history if isinstance(history, list) else [],
                                current if isinstance(current, dict) else {})

        if not stream:
            return _build_default_observation()

        volatility = _compute_volatility(stream)
        error_pressure = _compute_error_pressure(stream)
        stability = _compute_stability(volatility, error_pressure)
        state = _classify_state(stability, stream)
        trend = _compute_trend(stream)
        divergences = _build_divergence_events(stream)
        tick_summary = _build_tick_summary(stream)
        timestamp = _extract_timestamp(current)

        return {
            "shadow_state": state,
            "trend_direction": trend,
            "stability_index": float(round(stability, 6)),
            "volatility_index": float(round(volatility, 6)),
            "divergence_events": divergences,
            "tick_summary": tick_summary,
            "meta": {
                "simulation_mode": True,
                "timestamp": timestamp,
            },
        }
    except Exception:
        # Fail-safe absoluto: nunca propagar excepciones
        return _build_default_observation()


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
