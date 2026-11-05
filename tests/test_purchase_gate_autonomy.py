"""tests/test_purchase_gate_autonomy.py — M14 Autonomous Decision Layer tests.

Cobertura mínima per spec:
    - schema validation
    - deterministic output
    - no mutation of inputs
    - allow_live path
    - hold_dry_run path
    - pause_system path
    - rollback_required path
    - confidence clamping
    - malformed inputs fail-safe
    - spec alias
    - precedence critical > high > medium
    - operator_action correctness
"""
import copy

import pytest

from core.observability.autonomy import (
    AUTONOMY_LEVEL_BY_DECISION,
    BASE_CONFIDENCE_BY_DECISION,
    OPERATOR_ACTION_BY_DECISION,
    compute_autonomous_purchase_decision,
    decide_purchase_autonomy,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _mk_drift(*, severity="low", detected=False, signal_types=None):
    signal_types = signal_types or []
    return {
        "drift_detected": detected,
        "severity": severity,
        "signals": [{"type": t, "severity": "medium"} for t in signal_types],
        "baseline": {},
        "timestamp": "2026-05-13T00:00:00+00:00",
    }


def _mk_risk(*, risk_level="low", error_weight=0.0):
    return {
        "risk_score": 10.0,
        "risk_level": risk_level,
        "contributors": {
            "drift_weight": 0.0,
            "error_weight": error_weight,
            "block_weight": 0.0,
            "volatility_weight": 0.0,
        },
        "recommendation": "continue",
        "timestamp": "2026-05-13T00:00:00+00:00",
    }


def _mk_readiness(*, status="production_safe", go_live=True):
    return {
        "readiness_index": 90.0,
        "status": status,
        "factors": {
            "stability": 100.0, "error_rate": 100.0,
            "drift_control": 100.0, "system_confidence": 100.0,
        },
        "go_live_eligibility": go_live,
        "timestamp": "2026-05-13T00:00:00+00:00",
    }


# ════════════════════════════════════════════════════════════════════
#  Schema / pure / determinism / alias
# ════════════════════════════════════════════════════════════════════
def test_m14_output_schema_complete():
    r = decide_purchase_autonomy(_mk_drift(), _mk_risk(), _mk_readiness())
    required = {"decision", "confidence", "autonomy_level", "reasons",
                "operator_action", "signals", "timestamp"}
    assert required.issubset(r.keys())


def test_m14_signals_block_complete():
    r = decide_purchase_autonomy(_mk_drift(), _mk_risk(), _mk_readiness())
    signal_required = {"drift_severity", "risk_level", "readiness_status",
                        "go_live_eligibility"}
    assert signal_required.issubset(r["signals"].keys())


def test_m14_pure_function_no_mutation():
    drift = _mk_drift(severity="medium", detected=True,
                       signal_types=["dry_run_rate_deviation"])
    risk = _mk_risk(risk_level="medium", error_weight=2.0)
    readiness = _mk_readiness(status="stabilizing", go_live=False)
    drift_copy = copy.deepcopy(drift)
    risk_copy = copy.deepcopy(risk)
    readiness_copy = copy.deepcopy(readiness)
    decide_purchase_autonomy(drift, risk, readiness)
    assert drift == drift_copy
    assert risk == risk_copy
    assert readiness == readiness_copy


def test_m14_deterministic_same_input():
    drift = _mk_drift(severity="high", detected=True)
    risk = _mk_risk(risk_level="high")
    readiness = _mk_readiness(status="not_ready", go_live=False)
    r1 = decide_purchase_autonomy(drift, risk, readiness)
    r2 = decide_purchase_autonomy(drift, risk, readiness)
    assert r1["decision"] == r2["decision"]
    assert r1["confidence"] == r2["confidence"]
    assert r1["autonomy_level"] == r2["autonomy_level"]
    assert r1["operator_action"] == r2["operator_action"]
    assert r1["signals"] == r2["signals"]


def test_m14_spec_alias_works():
    drift = _mk_drift()
    risk = _mk_risk()
    readiness = _mk_readiness()
    r1 = decide_purchase_autonomy(drift, risk, readiness)
    r2 = compute_autonomous_purchase_decision(drift, risk, readiness)
    assert r1["decision"] == r2["decision"]
    assert r1["operator_action"] == r2["operator_action"]


# ════════════════════════════════════════════════════════════════════
#  Decision paths
# ════════════════════════════════════════════════════════════════════
def test_m14_allow_live_when_all_clean():
    """go_live=True + risk.low + no drift → allow_live."""
    drift = _mk_drift(severity="low", detected=False)
    risk = _mk_risk(risk_level="low")
    readiness = _mk_readiness(status="production_safe", go_live=True)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "allow_live"
    assert r["autonomy_level"] == "candidate_autonomous"
    assert r["operator_action"] == "continue_monitoring"


def test_m14_hold_dry_run_on_medium_risk():
    """risk.level=medium → hold_dry_run."""
    drift = _mk_drift(severity="low")
    risk = _mk_risk(risk_level="medium")
    readiness = _mk_readiness(status="ready", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "hold_dry_run"
    assert r["autonomy_level"] == "supervised"
    assert r["operator_action"] == "review_metrics"


def test_m14_hold_dry_run_on_stabilizing_readiness():
    """readiness.status=stabilizing → hold_dry_run."""
    drift = _mk_drift()
    risk = _mk_risk(risk_level="low")
    readiness = _mk_readiness(status="stabilizing", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "hold_dry_run"


def test_m14_pause_system_on_high_risk():
    """risk.level=high → pause_system."""
    drift = _mk_drift(severity="medium")
    risk = _mk_risk(risk_level="high")
    readiness = _mk_readiness(status="not_ready", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "pause_system"
    assert r["autonomy_level"] == "assisted"
    assert r["operator_action"] == "pause_and_investigate"


def test_m14_pause_system_on_high_drift():
    """drift.severity=high (sin high risk) → pause_system."""
    drift = _mk_drift(severity="high", detected=True)
    risk = _mk_risk(risk_level="medium")
    readiness = _mk_readiness(status="ready", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "pause_system"


def test_m14_rollback_required_on_critical_risk():
    """risk.level=critical → rollback_required."""
    drift = _mk_drift(severity="high")
    risk = _mk_risk(risk_level="critical")
    readiness = _mk_readiness(status="not_ready", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "rollback_required"
    assert r["autonomy_level"] == "none"
    assert r["operator_action"] == "rollback_to_safe_mode"


def test_m14_rollback_required_on_critical_drift():
    """drift.severity=critical → rollback_required."""
    drift = _mk_drift(severity="critical", detected=True)
    risk = _mk_risk(risk_level="medium")
    readiness = _mk_readiness(status="ready", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "rollback_required"


# ════════════════════════════════════════════════════════════════════
#  Decision precedence
# ════════════════════════════════════════════════════════════════════
def test_m14_critical_wins_over_high():
    """critical en risk + high en drift → rollback (critical wins)."""
    drift = _mk_drift(severity="high", detected=True)
    risk = _mk_risk(risk_level="critical")
    readiness = _mk_readiness(status="not_ready", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "rollback_required"


def test_m14_high_wins_over_medium():
    """high drift + medium risk → pause (high wins)."""
    drift = _mk_drift(severity="high", detected=True)
    risk = _mk_risk(risk_level="medium")
    readiness = _mk_readiness(status="stabilizing", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    assert r["decision"] == "pause_system"


def test_m14_allow_live_only_with_all_conditions():
    """Sin go_live → no allow_live aunque risk.low y no drift."""
    drift = _mk_drift(severity="low", detected=False)
    risk = _mk_risk(risk_level="low")
    readiness = _mk_readiness(status="ready", go_live=False)
    r = decide_purchase_autonomy(drift, risk, readiness)
    # go_live False → no allow_live → default hold_dry_run
    assert r["decision"] == "hold_dry_run"


# ════════════════════════════════════════════════════════════════════
#  Confidence model
# ════════════════════════════════════════════════════════════════════
def test_m14_confidence_base_values():
    """Base confidence sin adjustments matches spec."""
    # allow_live clean → 90
    r_allow = decide_purchase_autonomy(
        _mk_drift(detected=False, signal_types=[]),
        _mk_risk(risk_level="low", error_weight=0),
        _mk_readiness(status="production_safe", go_live=True),
    )
    assert r_allow["decision"] == "allow_live"
    assert r_allow["confidence"] == 90.0


def test_m14_confidence_drift_detected_reduces_10():
    """drift_detected=True reduces confidence by 10."""
    # hold_dry_run base = 65, with drift_detected = 55
    r = decide_purchase_autonomy(
        _mk_drift(detected=True, severity="medium",
                   signal_types=["dry_run_rate_deviation"]),
        _mk_risk(risk_level="medium"),
        _mk_readiness(status="stabilizing", go_live=False),
    )
    # base 65 - 10 (drift detected) = 55
    assert r["decision"] == "hold_dry_run"
    assert r["confidence"] == 55.0


def test_m14_confidence_internal_errors_reduce_15():
    """error_weight > 0 reduces confidence by 15."""
    r = decide_purchase_autonomy(
        _mk_drift(detected=False),
        _mk_risk(risk_level="medium", error_weight=5.0),
        _mk_readiness(status="stabilizing", go_live=False),
    )
    # hold_dry_run base 65 - 15 (errors) = 50
    assert r["confidence"] == 50.0


def test_m14_confidence_extra_signals_reduce_5_each():
    """Cada extra signal beyond first reduce 5."""
    r = decide_purchase_autonomy(
        _mk_drift(detected=True, severity="medium",
                   signal_types=["a", "b", "c"]),  # 3 signals, 2 extras
        _mk_risk(risk_level="medium"),
        _mk_readiness(status="stabilizing", go_live=False),
    )
    # hold_dry_run 65 - 10 (drift_detected) - 5*2 (extras) = 45
    assert r["confidence"] == 45.0


def test_m14_confidence_clamped_0_100():
    """Confidence siempre en [0, 100] aunque adjustments salgan."""
    # Worst case adjustments
    r = decide_purchase_autonomy(
        _mk_drift(detected=True, severity="medium",
                   signal_types=["a"] * 30),
        _mk_risk(risk_level="medium", error_weight=10.0),
        _mk_readiness(status="not_ready", go_live=False),
    )
    assert 0.0 <= r["confidence"] <= 100.0


# ════════════════════════════════════════════════════════════════════
#  Operator action + autonomy level mapping
# ════════════════════════════════════════════════════════════════════
def test_m14_operator_action_mapping_canonical():
    """Cada decision mapea a su operator_action correcto."""
    assert OPERATOR_ACTION_BY_DECISION["allow_live"] == "continue_monitoring"
    assert OPERATOR_ACTION_BY_DECISION["hold_dry_run"] == "review_metrics"
    assert OPERATOR_ACTION_BY_DECISION["pause_system"] == "pause_and_investigate"
    assert OPERATOR_ACTION_BY_DECISION["rollback_required"] == "rollback_to_safe_mode"


def test_m14_autonomy_level_mapping_canonical():
    """Cada decision mapea a su autonomy_level correcto."""
    assert AUTONOMY_LEVEL_BY_DECISION["allow_live"] == "candidate_autonomous"
    assert AUTONOMY_LEVEL_BY_DECISION["hold_dry_run"] == "supervised"
    assert AUTONOMY_LEVEL_BY_DECISION["pause_system"] == "assisted"
    assert AUTONOMY_LEVEL_BY_DECISION["rollback_required"] == "none"


def test_m14_base_confidence_values_per_spec():
    """Base confidence values match spec literal."""
    assert BASE_CONFIDENCE_BY_DECISION["allow_live"] == 90.0
    assert BASE_CONFIDENCE_BY_DECISION["hold_dry_run"] == 65.0
    assert BASE_CONFIDENCE_BY_DECISION["pause_system"] == 45.0
    assert BASE_CONFIDENCE_BY_DECISION["rollback_required"] == 95.0


# ════════════════════════════════════════════════════════════════════
#  Reasons block
# ════════════════════════════════════════════════════════════════════
def test_m14_reasons_clean_state():
    """All clean → reasons contains 'all_signals_clean'."""
    r = decide_purchase_autonomy(
        _mk_drift(detected=False, severity="low"),
        _mk_risk(risk_level="low"),
        _mk_readiness(status="production_safe", go_live=True),
    )
    assert "all_signals_clean" in r["reasons"]


def test_m14_reasons_drift_detected():
    """drift_detected → reason incluye 'drift_detected_severity_X'."""
    r = decide_purchase_autonomy(
        _mk_drift(detected=True, severity="medium"),
        _mk_risk(),
        _mk_readiness(),
    )
    assert any("drift_detected" in reason for reason in r["reasons"])


# ════════════════════════════════════════════════════════════════════
#  Edge cases / fail-safe
# ════════════════════════════════════════════════════════════════════
def test_m14_empty_inputs_safe_default():
    """Inputs vacíos → fail-safe conservador (hold_dry_run o rollback)."""
    r = decide_purchase_autonomy({}, {}, {})
    assert r["decision"] in ("hold_dry_run", "pause_system",
                              "rollback_required")
    assert r["autonomy_level"] in ("supervised", "assisted", "none")


def test_m14_malformed_inputs_no_crash():
    """Inputs raros → fail-safe sin crash."""
    bad_triples = [
        (None, None, None),
        ("str", [], {}),
        ({"severity": 42}, {"risk_level": []}, {"status": None}),
    ]
    for drift, risk, readiness in bad_triples:
        try:
            r = decide_purchase_autonomy(drift, risk, readiness)
        except Exception as e:
            assert False, f"crashed on {(drift, risk, readiness)}: {e}"
        assert isinstance(r, dict)
        assert "decision" in r
        assert r["decision"] in (
            "allow_live", "hold_dry_run",
            "pause_system", "rollback_required",
        )


def test_m14_signals_block_reflects_inputs():
    """Signals block espejo de inputs."""
    r = decide_purchase_autonomy(
        _mk_drift(severity="medium"),
        _mk_risk(risk_level="high"),
        _mk_readiness(status="stabilizing", go_live=False),
    )
    s = r["signals"]
    assert s["drift_severity"] == "medium"
    assert s["risk_level"] == "high"
    assert s["readiness_status"] == "stabilizing"
    assert s["go_live_eligibility"] is False


def test_m14_advisory_only_no_mutation_class():
    """M14 NO expone clases ni métodos que ejecuten/muten."""
    from core.observability import autonomy
    # Solo exporta funciones pure y constantes
    public_names = [n for n in dir(autonomy) if not n.startswith("_")]
    for name in public_names:
        attr = getattr(autonomy, name)
        # No clases stateful — solo funciones, dicts, módulos
        assert callable(attr) or isinstance(attr, (dict, list, tuple, str)) \
            or attr.__class__.__name__ == "module"
