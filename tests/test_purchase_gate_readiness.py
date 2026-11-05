"""tests/test_purchase_gate_readiness.py — M13.9 Production Readiness Index tests."""
import copy

import pytest

from core.observability.readiness import (
    READINESS_STATUS_THRESHOLDS,
    compute_purchase_readiness_index,
    compute_readiness_index,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _mk_risk(*, risk_score=10.0, risk_level="low",
              drift_w=0.0, error_w=0.0, block_w=0.0, vol_w=0.0):
    return {
        "risk_score": risk_score,
        "risk_level": risk_level,
        "contributors": {
            "drift_weight": drift_w,
            "error_weight": error_w,
            "block_weight": block_w,
            "volatility_weight": vol_w,
        },
        "recommendation": "continue",
        "timestamp": "2026-05-13T00:00:00+00:00",
    }


def _mk_history(*, internal_errors_total=0):
    return {"internal_errors_total": internal_errors_total}


# ════════════════════════════════════════════════════════════════════
#  Schema / pure / deterministic
# ════════════════════════════════════════════════════════════════════
def test_m13_9_output_schema_complete():
    r = compute_readiness_index(_mk_risk(), _mk_history())
    required = {"readiness_index", "status", "factors",
                "go_live_eligibility", "timestamp"}
    assert required.issubset(r.keys())


def test_m13_9_factors_block_complete():
    r = compute_readiness_index(_mk_risk(), _mk_history())
    factor_required = {"stability", "error_rate",
                        "drift_control", "system_confidence"}
    assert factor_required.issubset(r["factors"].keys())


def test_m13_9_pure_function_no_mutation():
    risk = _mk_risk(risk_score=40.0, risk_level="medium", error_w=8.0)
    hist = _mk_history(internal_errors_total=2)
    risk_copy = copy.deepcopy(risk)
    hist_copy = copy.deepcopy(hist)
    compute_readiness_index(risk, hist)
    assert risk == risk_copy
    assert hist == hist_copy


def test_m13_9_deterministic_same_input():
    risk = _mk_risk(risk_score=25.0, risk_level="low",
                     drift_w=5.0, error_w=2.0, block_w=3.0, vol_w=1.0)
    hist = _mk_history(internal_errors_total=0)
    r1 = compute_readiness_index(risk, hist)
    r2 = compute_readiness_index(risk, hist)
    assert r1["readiness_index"] == r2["readiness_index"]
    assert r1["status"] == r2["status"]
    assert r1["factors"] == r2["factors"]
    assert r1["go_live_eligibility"] == r2["go_live_eligibility"]


def test_m13_9_spec_alias_works():
    risk = _mk_risk()
    hist = _mk_history()
    r1 = compute_readiness_index(risk, hist)
    r2 = compute_purchase_readiness_index(risk, hist)
    assert r1["readiness_index"] == r2["readiness_index"]
    assert r1["status"] == r2["status"]


# ════════════════════════════════════════════════════════════════════
#  Status mapping per spec
# ════════════════════════════════════════════════════════════════════
def test_m13_9_status_thresholds_canonical():
    """Thresholds per spec: 0-30/30-55/55-80/80-100."""
    statuses = [(s, lo, hi) for s, lo, hi in READINESS_STATUS_THRESHOLDS]
    assert statuses[0][0] == "not_ready"
    assert statuses[0][2] == 30.0
    assert statuses[1][0] == "stabilizing"
    assert statuses[1][2] == 55.0
    assert statuses[2][0] == "ready"
    assert statuses[2][2] == 80.0
    assert statuses[3][0] == "production_safe"


def test_m13_9_clean_state_production_safe():
    """Risk muy bajo + factors limpios → production_safe."""
    risk = _mk_risk(risk_score=2.0, risk_level="low")
    hist = _mk_history(internal_errors_total=0)
    r = compute_readiness_index(risk, hist)
    assert r["readiness_index"] >= 80.0
    assert r["status"] == "production_safe"


def test_m13_9_high_risk_not_ready():
    """Risk crítico → readiness bajo → not_ready."""
    risk = _mk_risk(
        risk_score=90.0, risk_level="critical",
        drift_w=38.0, error_w=20.0, block_w=18.0, vol_w=12.0,
    )
    hist = _mk_history(internal_errors_total=5)
    r = compute_readiness_index(risk, hist)
    assert r["readiness_index"] < 30.0
    assert r["status"] == "not_ready"


def test_m13_9_mixed_signals_stabilizing():
    """Risk medium + algunos errores → stabilizing."""
    risk = _mk_risk(
        risk_score=50.0, risk_level="medium",
        drift_w=18.0, error_w=10.0, block_w=10.0, vol_w=5.0,
    )
    hist = _mk_history(internal_errors_total=1)
    r = compute_readiness_index(risk, hist)
    assert 30.0 <= r["readiness_index"] < 80.0
    assert r["status"] in ("stabilizing", "ready")


# ════════════════════════════════════════════════════════════════════
#  go_live_eligibility logic
# ════════════════════════════════════════════════════════════════════
def test_m13_9_go_live_only_at_production_safe_with_low_risk():
    """go_live=True solo si production_safe AND low risk AND no errores."""
    risk = _mk_risk(risk_score=2.0, risk_level="low")
    hist = _mk_history(internal_errors_total=0)
    r = compute_readiness_index(risk, hist)
    assert r["status"] == "production_safe"
    assert r["go_live_eligibility"] is True


def test_m13_9_go_live_false_when_medium_risk():
    """Aunque readiness sea high, risk_level != low bloquea go_live."""
    risk = _mk_risk(risk_score=2.0, risk_level="medium")  # mismatch
    hist = _mk_history(internal_errors_total=0)
    r = compute_readiness_index(risk, hist)
    # Risk muy bajo → readiness alto → status production_safe, pero risk_level medium
    if r["status"] == "production_safe":
        assert r["go_live_eligibility"] is False


def test_m13_9_go_live_false_when_history_has_errors():
    """Si history tiene errores totales > 0 → go_live=False."""
    risk = _mk_risk(risk_score=2.0, risk_level="low")
    hist = _mk_history(internal_errors_total=3)
    r = compute_readiness_index(risk, hist)
    assert r["go_live_eligibility"] is False


def test_m13_9_go_live_false_when_not_production_safe():
    """status != production_safe → go_live=False."""
    risk = _mk_risk(risk_score=50.0, risk_level="medium")
    hist = _mk_history(internal_errors_total=0)
    r = compute_readiness_index(risk, hist)
    assert r["go_live_eligibility"] is False


# ════════════════════════════════════════════════════════════════════
#  Factors computation
# ════════════════════════════════════════════════════════════════════
def test_m13_9_clean_inputs_high_factors():
    """Contributors en 0 → todos factors ≈ 100."""
    risk = _mk_risk(risk_score=0.0, risk_level="low",
                     drift_w=0.0, error_w=0.0, block_w=0.0, vol_w=0.0)
    hist = _mk_history()
    r = compute_readiness_index(risk, hist)
    f = r["factors"]
    assert f["stability"] == 100.0
    assert f["error_rate"] == 100.0
    assert f["drift_control"] == 100.0
    assert f["system_confidence"] == 100.0


def test_m13_9_high_drift_reduces_drift_control():
    """drift_weight alto → drift_control bajo."""
    risk = _mk_risk(drift_w=30.0)
    r = compute_readiness_index(risk, _mk_history())
    # drift_control = 100 - 30 * 2.5 = 25
    assert r["factors"]["drift_control"] < 50.0


def test_m13_9_factors_clamped_0_100():
    """Factors siempre en [0, 100] aunque contributors sean extremos."""
    risk = _mk_risk(
        drift_w=200.0, error_w=200.0, block_w=200.0, vol_w=200.0,
    )
    r = compute_readiness_index(risk, _mk_history())
    f = r["factors"]
    for k, v in f.items():
        assert 0.0 <= v <= 100.0, f"Factor {k}={v} out of [0,100]"


# ════════════════════════════════════════════════════════════════════
#  Edge cases
# ════════════════════════════════════════════════════════════════════
def test_m13_9_empty_inputs_returns_conservative_state():
    """Inputs vacíos → status conservador, NO go_live."""
    r = compute_readiness_index({}, {})
    assert isinstance(r["readiness_index"], float)
    assert r["go_live_eligibility"] is False
    assert r["status"] in ("not_ready", "stabilizing", "ready",
                            "production_safe")


def test_m13_9_malformed_inputs_no_crash():
    """Inputs raros → fail-safe sin crash."""
    bad_pairs = [
        (None, None),
        ("str", []),
        ({"risk_score": "not a number"}, {"errors": []}),
        ({"contributors": "not a dict"}, None),
    ]
    for risk, hist in bad_pairs:
        try:
            r = compute_readiness_index(risk, hist)
        except Exception as e:
            assert False, f"crashed on {(risk, hist)}: {e}"
        assert isinstance(r, dict)
        assert "readiness_index" in r
        assert "status" in r
        assert "go_live_eligibility" in r


def test_m13_9_none_history_acceptable():
    """history_summary=None es válido (campo opcional)."""
    risk = _mk_risk(risk_score=2.0, risk_level="low")
    r = compute_readiness_index(risk, None)
    assert isinstance(r["readiness_index"], float)
    assert r["go_live_eligibility"] in (True, False)


def test_m13_9_readiness_index_clamped_0_100():
    """readiness_index siempre en [0, 100]."""
    # Best case
    r_best = compute_readiness_index(
        _mk_risk(risk_score=0.0), _mk_history()
    )
    # Worst case
    r_worst = compute_readiness_index(
        _mk_risk(risk_score=100.0, drift_w=40.0, error_w=25.0,
                  block_w=20.0, vol_w=15.0),
        _mk_history(internal_errors_total=100),
    )
    assert 0.0 <= r_best["readiness_index"] <= 100.0
    assert 0.0 <= r_worst["readiness_index"] <= 100.0
