"""tests/test_purchase_gate_risk.py — M13.8 Risk Scoring Engine tests."""
import copy

import pytest

from core.observability.risk import (
    RECOMMENDATION_BY_LEVEL,
    RISK_LEVEL_THRESHOLDS,
    SEVERITY_TO_BASE_SCORE,
    WEIGHTS,
    compute_purchase_risk_score,
    compute_risk_score,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _mk_drift(*, severity="low", signal_types=None):
    signal_types = signal_types or []
    return {
        "drift_detected": severity != "low",
        "severity": severity,
        "signals": [{"type": t, "severity": "medium"} for t in signal_types],
        "baseline": {},
        "timestamp": "2026-05-13T00:00:00+00:00",
    }


def _mk_metrics(*, internal_errors=0, block_rate=0.0, mode="dry_run"):
    return {
        "config": {"mode": mode, "global_enabled": True, "thresholds": {}},
        "counters": {
            "purchase_attempted_count": 100,
            "purchase_allowed_count": 80,
            "purchase_blocked_count": int(block_rate * 100),
            "purchase_dry_run_count": 80,
            "purchase_live_count": 0,
            "internal_errors": internal_errors,
        },
        "rates": {
            "purchase_allow_rate": 0.8,
            "purchase_block_rate": block_rate,
            "purchase_dry_run_rate": 0.8,
            "purchase_live_rate": 0.0,
        },
        "denial_breakdown": {},
        "cache_state": {},
    }


# ════════════════════════════════════════════════════════════════════
#  Schema / pure / deterministic
# ════════════════════════════════════════════════════════════════════
def test_m13_8_output_schema_complete():
    r = compute_risk_score(_mk_drift(), _mk_metrics())
    required = {"risk_score", "risk_level", "contributors",
                "recommendation", "timestamp"}
    assert required.issubset(r.keys())


def test_m13_8_contributors_block_complete():
    r = compute_risk_score(_mk_drift(), _mk_metrics())
    contrib_required = {"drift_weight", "error_weight",
                         "block_weight", "volatility_weight"}
    assert contrib_required.issubset(r["contributors"].keys())


def test_m13_8_weights_sum_to_one():
    """Los 4 pesos del modelo suman 1.0."""
    total = sum(WEIGHTS.values())
    assert abs(total - 1.0) < 0.0001


def test_m13_8_pure_function_no_mutation():
    drift = _mk_drift(severity="medium", signal_types=["block_rate_spike"])
    metrics = _mk_metrics(internal_errors=2, block_rate=0.5)
    drift_copy = copy.deepcopy(drift)
    metrics_copy = copy.deepcopy(metrics)
    compute_risk_score(drift, metrics)
    assert drift == drift_copy
    assert metrics == metrics_copy


def test_m13_8_deterministic_same_input():
    drift = _mk_drift(severity="high",
                       signal_types=["dry_run_rate_deviation"])
    metrics = _mk_metrics(internal_errors=1, block_rate=0.3)
    r1 = compute_risk_score(drift, metrics)
    r2 = compute_risk_score(drift, metrics)
    # Mismas keys excepto timestamp
    assert r1["risk_score"] == r2["risk_score"]
    assert r1["risk_level"] == r2["risk_level"]
    assert r1["contributors"] == r2["contributors"]
    assert r1["recommendation"] == r2["recommendation"]


def test_m13_8_spec_alias_works():
    drift = _mk_drift()
    metrics = _mk_metrics()
    r1 = compute_risk_score(drift, metrics)
    r2 = compute_purchase_risk_score(drift, metrics)
    assert r1["risk_score"] == r2["risk_score"]
    assert r1["risk_level"] == r2["risk_level"]


# ════════════════════════════════════════════════════════════════════
#  Severity → score mapping
# ════════════════════════════════════════════════════════════════════
def test_m13_8_severity_to_base_score_monotonic():
    """SEVERITY_TO_BASE_SCORE es monotónicamente creciente."""
    levels = ["none", "low", "medium", "high", "critical"]
    scores = [SEVERITY_TO_BASE_SCORE[level] for level in levels]
    for i in range(1, len(scores)):
        assert scores[i] > scores[i - 1], (
            f"Severity score not monotonic: {scores}"
        )


def test_m13_8_clean_system_low_risk():
    """Drift low + cero errores + cero block → risk low + recommendation continue."""
    r = compute_risk_score(_mk_drift(severity="low"), _mk_metrics())
    assert r["risk_score"] < 30.0
    assert r["risk_level"] == "low"
    assert r["recommendation"] == "continue"


def test_m13_8_critical_drift_drives_high_risk():
    """Drift critical → risk_score elevado independiente de otros componentes."""
    r = compute_risk_score(
        _mk_drift(severity="critical"),
        _mk_metrics(),
    )
    # critical base = 95, drift weight = 0.40, contribución ≈ 38
    assert r["contributors"]["drift_weight"] > 30.0
    # Sin otros componentes, risk_score ≈ 38 (medium)
    assert r["risk_level"] in ("medium", "high")


# ════════════════════════════════════════════════════════════════════
#  Component scoring
# ════════════════════════════════════════════════════════════════════
def test_m13_8_internal_errors_escalate_risk():
    """5+ internal_errors → error_score satura cap → error_weight elevado."""
    r_clean = compute_risk_score(_mk_drift(), _mk_metrics(internal_errors=0))
    r_dirty = compute_risk_score(_mk_drift(), _mk_metrics(internal_errors=5))
    assert r_dirty["contributors"]["error_weight"] > r_clean["contributors"]["error_weight"]
    assert r_dirty["risk_score"] > r_clean["risk_score"]


def test_m13_8_high_block_rate_escalates_risk():
    """block_rate=0.9 → block_score=90 → block_weight ≈ 18."""
    r = compute_risk_score(_mk_drift(), _mk_metrics(block_rate=0.9))
    assert r["contributors"]["block_weight"] >= 15.0


def test_m13_8_volatility_signals_contribute():
    """Múltiples signals de volatilidad → volatility_weight > 0."""
    drift = _mk_drift(
        severity="medium",
        signal_types=["dry_run_rate_deviation", "block_rate_spike"],
    )
    r = compute_risk_score(drift, _mk_metrics())
    assert r["contributors"]["volatility_weight"] > 0


def test_m13_8_no_volatility_signals_zero_weight():
    """Sin signals de volatilidad → volatility_weight = 0."""
    drift = _mk_drift(severity="low", signal_types=[])
    r = compute_risk_score(drift, _mk_metrics())
    assert r["contributors"]["volatility_weight"] == 0.0


# ════════════════════════════════════════════════════════════════════
#  Risk level + recommendation mapping
# ════════════════════════════════════════════════════════════════════
def test_m13_8_extreme_metrics_drives_critical_risk():
    """Critical drift + 10 errors + 95% block → risk crítico."""
    drift = _mk_drift(
        severity="critical",
        signal_types=["dry_run_rate_deviation", "block_rate_spike"],
    )
    metrics = _mk_metrics(internal_errors=10, block_rate=0.95)
    r = compute_risk_score(drift, metrics)
    assert r["risk_score"] >= 55.0  # at least high
    assert r["risk_level"] in ("high", "critical")
    assert r["recommendation"] in ("pause", "rollback")


def test_m13_8_recommendation_mapping_per_level():
    """Cada level mapea a su recomendación correcta."""
    assert RECOMMENDATION_BY_LEVEL["low"] == "continue"
    assert RECOMMENDATION_BY_LEVEL["medium"] == "monitor"
    assert RECOMMENDATION_BY_LEVEL["high"] == "pause"
    assert RECOMMENDATION_BY_LEVEL["critical"] == "rollback"


def test_m13_8_risk_level_thresholds_canonical():
    """Thresholds per spec: 0-30/30-55/55-80/80-100."""
    levels = [(l, lo, hi) for l, lo, hi in RISK_LEVEL_THRESHOLDS]
    # low: 0-30
    assert levels[0][0] == "low"
    assert levels[0][1] == 0.0
    assert levels[0][2] == 30.0
    # medium: 30-55
    assert levels[1][0] == "medium"
    assert levels[1][2] == 55.0
    # high: 55-80
    assert levels[2][0] == "high"
    assert levels[2][2] == 80.0


# ════════════════════════════════════════════════════════════════════
#  Edge cases
# ════════════════════════════════════════════════════════════════════
def test_m13_8_empty_inputs_safe_default():
    """Inputs vacíos → risk_score bajo, recomendación continue."""
    r = compute_risk_score({}, {})
    assert isinstance(r["risk_score"], float)
    assert r["risk_score"] <= 30.0
    assert r["risk_level"] == "low"
    assert r["recommendation"] == "continue"


def test_m13_8_malformed_inputs_no_crash():
    """Inputs raros → fail-safe sin crash."""
    bad_pairs = [
        (None, None),
        ([], "string"),
        ({"severity": 42}, {"counters": "not a dict"}),
        ({"signals": "not a list"}, {"rates": None}),
    ]
    for drift, metrics in bad_pairs:
        try:
            r = compute_risk_score(drift, metrics)
        except Exception as e:
            assert False, f"crashed on {(drift, metrics)}: {e}"
        assert isinstance(r, dict)
        assert "risk_score" in r
        assert "risk_level" in r
        assert "recommendation" in r


def test_m13_8_risk_score_clamped_0_100():
    """Risk score nunca fuera de [0, 100]."""
    # Worst case
    drift = _mk_drift(
        severity="critical",
        signal_types=["dry_run_rate_deviation", "block_rate_spike",
                       "dry_run_rate_deviation"],  # 3 vol signals
    )
    metrics = _mk_metrics(internal_errors=100, block_rate=1.0)
    r = compute_risk_score(drift, metrics)
    assert 0.0 <= r["risk_score"] <= 100.0


def test_m13_8_contributors_sum_close_to_risk_score():
    """Suma de contributors ≈ risk_score (excepto clamp)."""
    drift = _mk_drift(severity="medium")
    metrics = _mk_metrics(internal_errors=2, block_rate=0.3)
    r = compute_risk_score(drift, metrics)
    contribs_sum = sum(r["contributors"].values())
    # Permitimos pequeña tolerancia por rounding
    assert abs(contribs_sum - r["risk_score"]) <= 0.5
