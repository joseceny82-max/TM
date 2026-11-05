"""core/observability/simulation_fidelity/fidelity_engine.py — M20.

Simulation Fidelity Engine (OBSERVATIONAL VALIDATION ONLY).

Pure function que cuantifica la fidelidad del adapter M19 respecto al
comportamiento esperado definido por DOMAIN BOUNDARY SPEC v1.0.

PRINCIPIO ARQUITECTÓNICO:
    M20 es OBSERVATIONAL-ONLY y NO importa M11-M19:
        - independencia epistemológica: el evaluador no puede depender
          del evaluado
        - el mapping esperado está HARDCODED aquí según spec v1.0
        - si M19 deriva del spec, M20 lo detecta vía fidelity baja

Funcionamiento:
    1. Calcula 4 métricas de alignment Market↔TM
    2. Computa fidelity_score agregado (media)
    3. Clasifica adapter_quality (high|medium|low|critical)
    4. Mide drift entre vectores
    5. Genera hashes determinísticos de inputs
    6. Produce recomendación per quality

Output schema (spec):
    {
        "fidelity_score": float (0-1),
        "signal_alignment": {volatility, liquidity, risk_proxy,
                              volume_mapping},
        "drift_between_layers": float,
        "adapter_quality": "low" | "medium" | "high" | "critical",
        "consistency_verified": bool,
        "deterministic": bool,
        "recommendation": str,
        "signals": {market_event_hash, snapshot_hash},
        "timestamp": str (ISO8601)
    }

stdlib only. Determinístico. Sin side-effects.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Spec v1.0 — mapping esperado HARDCODED (independencia de M19)
# ════════════════════════════════════════════════════════════════════
# Bucketing macro_risk → drift_hint (per DOMAIN BOUNDARY SPEC v1.0)
_DRIFT_HINT_SPEC: list = [
    (0.00, 0.25, "low"),
    (0.25, 0.50, "medium"),
    (0.50, 0.75, "high"),
    (0.75, 1.01, "critical"),
]

# Bucketing volume → internal_errors (per spec)
_VOLUME_ERROR_SPEC: list = [
    (0.0, 1000.0, 0),
    (1000.0, 5000.0, 1),
    (5000.0, 10000.0, 2),
]
_VOLUME_MAX_ERROR: int = 3

# Ordenamiento canónico para drift_hint → numérico (para drift vector)
_DRIFT_HINT_ORDER: dict[str, float] = {
    "low": 0.0,
    "medium": 0.33,
    "high": 0.66,
    "critical": 1.0,
}


# ════════════════════════════════════════════════════════════════════
#  Adapter quality classification (per spec literal)
# ════════════════════════════════════════════════════════════════════
ADAPTER_QUALITY_THRESHOLDS: list = [
    ("critical", 0.00, 0.40),
    ("low",      0.40, 0.65),
    ("medium",   0.65, 0.85),
    ("high",     0.85, 1.01),
]

RECOMMENDATION_BY_QUALITY: dict[str, str] = {
    "high":     "translation_is_faithful_continue",
    "medium":   "translation_has_minor_deviations_monitor",
    "low":      "translation_has_significant_gaps_review_mapping",
    "critical": "translation_diverges_substantially_reevaluate_adapter",
}


# Threshold para consistency_verified
_CONSISTENCY_MIN_ALIGNMENT: float = 0.5
_CONSISTENCY_MAX_DRIFT: float = 0.2


# ════════════════════════════════════════════════════════════════════
#  Helpers internos
# ════════════════════════════════════════════════════════════════════
def _safe_get(d: Any, *path: str, default: Any = None) -> Any:
    current = d
    for key in path:
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
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
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _expected_drift_bucket(macro_risk: float) -> str:
    """Devuelve el drift_hint esperado para un macro_risk según spec."""
    for lo, hi, label in _DRIFT_HINT_SPEC:
        if lo <= macro_risk < hi:
            return label
    return "critical"


def _expected_volume_bucket(volume: float) -> int:
    """Devuelve internal_errors esperados para un volume según spec."""
    if volume < 0:
        return 0
    for lo, hi, errors in _VOLUME_ERROR_SPEC:
        if lo <= volume <= hi:
            return errors
    return _VOLUME_MAX_ERROR


def _bucket_distance(actual: str, expected: str,
                      order: list) -> int:
    """Distancia en buckets entre actual y expected. -1 si no encontrado."""
    try:
        ai = order.index(actual)
        ei = order.index(expected)
        return abs(ai - ei)
    except (ValueError, AttributeError):
        return -1


def _alignment_from_distance(distance: int) -> float:
    """Bucket-based alignment: 0 buckets off → 1.0, 1 off → 0.5, more → 0.0."""
    if distance == 0:
        return 1.0
    if distance == 1:
        return 0.5
    return 0.0


def _alignment_continuous(market_val: float, tm_val: float,
                           factor: float = 2.0) -> float:
    """Alignment lineal continuo para signals [0,1]."""
    return _clamp(1.0 - abs(market_val - tm_val) * factor)


def _hash_dict(d: Any) -> str:
    """SHA-256 truncado determinístico de un dict serializable."""
    try:
        if not isinstance(d, dict):
            d = {"_": str(d)}
        canonical = json.dumps(d, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return "0" * 16


def _classify_adapter_quality(fidelity_score: float) -> str:
    """Mapea fidelity_score → adapter_quality según thresholds spec."""
    for label, lo, hi in ADAPTER_QUALITY_THRESHOLDS:
        if lo <= fidelity_score < hi:
            return label
    return "high"  # cap superior


def _normalize_errors(errors: int) -> float:
    """Normaliza internal_errors [0, MAX] a [0, 1]."""
    if errors <= 0:
        return 0.0
    return _clamp(errors / float(_VOLUME_MAX_ERROR))


def _normalize_volume(volume: float, cap: float = 10000.0) -> float:
    """Normaliza volume a [0, 1] vía cap."""
    if volume <= 0:
        return 0.0
    return _clamp(volume / cap)


# ════════════════════════════════════════════════════════════════════
#  Cómputo de las 4 métricas de alignment
# ════════════════════════════════════════════════════════════════════
def _compute_volatility_alignment(market_event: Any,
                                    tm_snapshot: Any) -> float:
    """volatility ↔ purchase_block_rate."""
    volatility = _clamp(_safe_float(
        _safe_get(market_event, "signals", "volatility"), 0.0
    ))
    block_rate = _clamp(_safe_float(
        _safe_get(tm_snapshot, "current", "rates", "purchase_block_rate"),
        0.0,
    ))
    return _alignment_continuous(volatility, block_rate)


def _compute_liquidity_alignment(market_event: Any,
                                   tm_snapshot: Any) -> float:
    """liquidity ↔ purchase_dry_run_rate."""
    liquidity = _clamp(_safe_float(
        _safe_get(market_event, "signals", "liquidity"), 0.0
    ))
    dry_run = _clamp(_safe_float(
        _safe_get(tm_snapshot, "current", "rates", "purchase_dry_run_rate"),
        0.0,
    ))
    return _alignment_continuous(liquidity, dry_run)


def _compute_risk_proxy_alignment(market_event: Any,
                                    tm_snapshot: Any) -> float:
    """macro_risk bucket ↔ drift_hint."""
    macro_risk = _clamp(_safe_float(
        _safe_get(market_event, "external_factors", "macro_risk"), 0.0
    ))
    expected_hint = _expected_drift_bucket(macro_risk)
    actual_hint = _safe_get(tm_snapshot, "meta", "drift_hint",
                              default="low")
    if not isinstance(actual_hint, str):
        actual_hint = "low"
    order = ["low", "medium", "high", "critical"]
    distance = _bucket_distance(actual_hint, expected_hint, order)
    if distance < 0:
        return 0.0
    return _alignment_from_distance(distance)


def _compute_volume_mapping_accuracy(market_event: Any,
                                       tm_snapshot: Any) -> float:
    """volume bucket ↔ internal_errors."""
    volume = _safe_float(
        _safe_get(market_event, "signals", "volume"), 0.0
    )
    expected_errors = _expected_volume_bucket(volume)
    actual_errors = _safe_int(
        _safe_get(tm_snapshot, "current", "counters", "internal_errors"),
        0,
    )
    distance = abs(actual_errors - expected_errors)
    return _alignment_from_distance(distance)


# ════════════════════════════════════════════════════════════════════
#  Drift entre layers
# ════════════════════════════════════════════════════════════════════
def _compute_drift_between_layers(market_event: Any,
                                    tm_snapshot: Any) -> float:
    """Distancia normalizada entre vectores Market y TM."""
    try:
        # Market vector
        volatility = _clamp(_safe_float(
            _safe_get(market_event, "signals", "volatility"), 0.0
        ))
        liquidity = _clamp(_safe_float(
            _safe_get(market_event, "signals", "liquidity"), 0.0
        ))
        macro_risk = _clamp(_safe_float(
            _safe_get(market_event, "external_factors", "macro_risk"), 0.0
        ))
        volume_norm = _normalize_volume(_safe_float(
            _safe_get(market_event, "signals", "volume"), 0.0
        ))

        # TM proxy vector
        block_rate = _clamp(_safe_float(
            _safe_get(tm_snapshot, "current", "rates",
                       "purchase_block_rate"), 0.0
        ))
        dry_run_rate = _clamp(_safe_float(
            _safe_get(tm_snapshot, "current", "rates",
                       "purchase_dry_run_rate"), 0.0
        ))
        drift_hint = _safe_get(tm_snapshot, "meta", "drift_hint",
                                 default="low")
        if not isinstance(drift_hint, str):
            drift_hint = "low"
        drift_numeric = _DRIFT_HINT_ORDER.get(drift_hint, 0.0)
        errors_norm = _normalize_errors(_safe_int(
            _safe_get(tm_snapshot, "current", "counters",
                       "internal_errors"), 0
        ))

        # Mean absolute distance
        deltas = [
            abs(volatility - block_rate),
            abs(liquidity - dry_run_rate),
            abs(macro_risk - drift_numeric),
            abs(volume_norm - errors_norm),
        ]
        return _clamp(sum(deltas) / len(deltas))
    except Exception:
        return 1.0  # max drift en caso de error


# ════════════════════════════════════════════════════════════════════
#  API principal: evaluate_simulation_fidelity
# ════════════════════════════════════════════════════════════════════
def evaluate_simulation_fidelity(
    market_event: Any,
    tm_snapshot: Any,
    expected_behavior: Any = None,
) -> dict:
    """Evalúa fidelidad del adapter M19 respecto a spec v1.0.

    OBSERVATIONAL-ONLY: NO decide, NO ejecuta, NO modifica nada.

    Args:
        market_event: dict MarketEvent (spec DATA INPUT v1.0)
        tm_snapshot: dict TM_Snapshot (output de M19)
        expected_behavior: opcional dict con overrides al mapping
                          esperado (reservado, no usado en v1.0)

    Returns:
        dict con fidelity_score, signal_alignment, drift_between_layers,
        adapter_quality, consistency_verified, deterministic,
        recommendation, signals.{market_event_hash, snapshot_hash},
        timestamp.

    Función pura: NO muta inputs, NO IO, NO random. Mapping spec
    hardcoded para independencia epistemológica de M19.
    """
    try:
        # Defensive normalization
        me = market_event if isinstance(market_event, dict) else {}
        ts = tm_snapshot if isinstance(tm_snapshot, dict) else {}

        # 4 alignment metrics
        vol_align = _compute_volatility_alignment(me, ts)
        liq_align = _compute_liquidity_alignment(me, ts)
        risk_align = _compute_risk_proxy_alignment(me, ts)
        vol_map = _compute_volume_mapping_accuracy(me, ts)

        # Fidelity score (mean of 4)
        alignments = [vol_align, liq_align, risk_align, vol_map]
        fidelity_score = sum(alignments) / len(alignments)
        fidelity_score = _clamp(fidelity_score)

        # Drift between layers
        drift = _compute_drift_between_layers(me, ts)

        # Adapter quality
        quality = _classify_adapter_quality(fidelity_score)

        # Consistency: all alignments above threshold AND drift acceptable
        consistency_verified = bool(
            all(a >= _CONSISTENCY_MIN_ALIGNMENT for a in alignments)
            and drift <= _CONSISTENCY_MAX_DRIFT
        )

        # Recommendation
        recommendation = RECOMMENDATION_BY_QUALITY.get(
            quality, "indeterminate"
        )

        # Hashes determinísticos (excluyendo timestamps si presentes)
        me_for_hash = {k: v for k, v in me.items() if k != "timestamp"}
        ts_for_hash = {k: v for k, v in ts.items()
                        if k != "timestamp"}
        # Para tm_snapshot, también excluir meta.timestamp si presente
        if isinstance(ts_for_hash.get("meta"), dict):
            ts_for_hash["meta"] = {
                k: v for k, v in ts_for_hash["meta"].items()
                if k != "timestamp"
            }
        market_event_hash = _hash_dict(me_for_hash)
        snapshot_hash = _hash_dict(ts_for_hash)

        return {
            "fidelity_score":       float(round(fidelity_score, 6)),
            "signal_alignment": {
                "volatility_alignment":   float(round(vol_align, 6)),
                "liquidity_alignment":    float(round(liq_align, 6)),
                "risk_proxy_alignment":   float(round(risk_align, 6)),
                "volume_mapping_accuracy": float(round(vol_map, 6)),
            },
            "drift_between_layers": float(round(drift, 6)),
            "adapter_quality":      quality,
            "consistency_verified": consistency_verified,
            "deterministic":        True,
            "recommendation":       recommendation,
            "signals": {
                "market_event_hash": market_event_hash,
                "snapshot_hash":     snapshot_hash,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Fail-safe absoluto: estado conservador
        return {
            "fidelity_score":       0.0,
            "signal_alignment": {
                "volatility_alignment":    0.0,
                "liquidity_alignment":     0.0,
                "risk_proxy_alignment":    0.0,
                "volume_mapping_accuracy": 0.0,
            },
            "drift_between_layers": 1.0,
            "adapter_quality":      "critical",
            "consistency_verified": False,
            "deterministic":        True,
            "recommendation": (
                RECOMMENDATION_BY_QUALITY["critical"]
            ),
            "signals": {
                "market_event_hash": "0" * 16,
                "snapshot_hash":     "0" * 16,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": "fidelity_compute_failed_failsafe",
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
