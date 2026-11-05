"""tests/test_purchase_governance.py — M15 Governance & Approval tests.

Cobertura mínima per spec (20+ tests):
    - schema validation
    - deterministic behavior
    - no mutation of inputs
    - every governance path (6)
    - escalation precedence
    - release recommendation correctness
    - malformed inputs fail-safe
    - spec alias
    - advisory-only guarantee
"""
import copy

import pytest

from core.observability.governance import (
    APPROVAL_REQUIRED_BY_STATE,
    ESCALATION_LEVELS,
    GOVERNANCE_STATES,
    OPERATOR_ACTIONS,
    RELEASE_RECOMMENDATIONS,
    compute_purchase_governance_state,
    evaluate_governance_state,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _mk_autonomy(decision="allow_live"):
    return {
        "decision": decision,
        "confidence": 90.0,
        "autonomy_level": "candidate_autonomous",
        "operator_action": "continue_monitoring",
        "signals": {},
    }


def _mk_readiness(status="production_safe", go_live=True):
    return {
        "readiness_index": 90.0,
        "status": status,
        "go_live_eligibility": go_live,
        "factors": {},
    }


def _mk_risk(level="low", error_weight=0.0):
    return {
        "risk_score": 10.0,
        "risk_level": level,
        "contributors": {
            "drift_weight": 0.0,
            "error_weight": error_weight,
            "block_weight": 0.0,
            "volatility_weight": 0.0,
        },
    }


def _mk_drift(severity="low", detected=False, signal_types=None):
    signal_types = signal_types or []
    return {
        "drift_detected": detected,
        "severity": severity,
        "signals": [{"type": t} for t in signal_types],
    }


# ════════════════════════════════════════════════════════════════════
#  Schema / pure / deterministic / alias
# ════════════════════════════════════════════════════════════════════
def test_m15_output_schema_complete():
    r = evaluate_governance_state(
        _mk_autonomy(), _mk_readiness(), _mk_risk(), _mk_drift(),
    )
    required = {
        "governance_state", "approval_required", "recommended_action",
        "release_recommendation", "escalation_level",
        "signals", "reasons", "timestamp",
    }
    assert required.issubset(r.keys())


def test_m15_signals_block_complete():
    r = evaluate_governance_state(
        _mk_autonomy(), _mk_readiness(), _mk_risk(), _mk_drift(),
    )
    required = {"autonomy_decision", "risk_level",
                "readiness_status", "drift_severity"}
    assert required.issubset(r["signals"].keys())


def test_m15_pure_function_no_mutation():
    a = _mk_autonomy("hold_dry_run")
    rd = _mk_readiness("stabilizing", go_live=False)
    rk = _mk_risk("medium", error_weight=5.0)
    dr = _mk_drift("medium", detected=True, signal_types=["a"])
    a_c, rd_c, rk_c, dr_c = (copy.deepcopy(a), copy.deepcopy(rd),
                              copy.deepcopy(rk), copy.deepcopy(dr))
    evaluate_governance_state(a, rd, rk, dr)
    assert a == a_c
    assert rd == rd_c
    assert rk == rk_c
    assert dr == dr_c


def test_m15_deterministic_same_input():
    a = _mk_autonomy("pause_system")
    rd = _mk_readiness("not_ready", go_live=False)
    rk = _mk_risk("high")
    dr = _mk_drift("medium", detected=True)
    r1 = evaluate_governance_state(a, rd, rk, dr)
    r2 = evaluate_governance_state(a, rd, rk, dr)
    assert r1["governance_state"] == r2["governance_state"]
    assert r1["escalation_level"] == r2["escalation_level"]
    assert r1["release_recommendation"] == r2["release_recommendation"]
    assert r1["recommended_action"] == r2["recommended_action"]
    assert r1["signals"] == r2["signals"]


def test_m15_spec_alias_works():
    a = _mk_autonomy()
    rd = _mk_readiness()
    rk = _mk_risk()
    dr = _mk_drift()
    r1 = evaluate_governance_state(a, rd, rk, dr)
    r2 = compute_purchase_governance_state(a, rd, rk, dr)
    assert r1["governance_state"] == r2["governance_state"]
    assert r1["escalation_level"] == r2["escalation_level"]


# ════════════════════════════════════════════════════════════════════
#  Governance path 1: EMERGENCY
# ════════════════════════════════════════════════════════════════════
def test_m15_emergency_path_autonomy_rollback():
    r = evaluate_governance_state(
        _mk_autonomy("rollback_required"),
        _mk_readiness("not_ready", go_live=False),
        _mk_risk("high"),
        _mk_drift("high", detected=True),
    )
    assert r["governance_state"] == "manual_review_required"
    assert r["escalation_level"] == "emergency"
    assert r["release_recommendation"] == "deny"
    assert r["recommended_action"] == "request_operator_review"


def test_m15_emergency_path_critical_risk():
    r = evaluate_governance_state(
        _mk_autonomy("pause_system"),
        _mk_readiness("not_ready", go_live=False),
        _mk_risk("critical"),
        _mk_drift("medium"),
    )
    assert r["governance_state"] == "manual_review_required"
    assert r["escalation_level"] == "emergency"
    assert r["release_recommendation"] == "deny"


# ════════════════════════════════════════════════════════════════════
#  Governance path 2: HIGH RISK
# ════════════════════════════════════════════════════════════════════
def test_m15_high_risk_pause_system():
    r = evaluate_governance_state(
        _mk_autonomy("pause_system"),
        _mk_readiness("ready", go_live=False),
        _mk_risk("high"),
        _mk_drift("medium"),
    )
    assert r["governance_state"] == "manual_review_required"
    assert r["escalation_level"] == "senior_operator"
    assert r["release_recommendation"] == "hold"
    assert r["recommended_action"] == "request_operator_review"


def test_m15_high_risk_high_drift():
    r = evaluate_governance_state(
        _mk_autonomy("hold_dry_run"),
        _mk_readiness("stabilizing", go_live=False),
        _mk_risk("medium"),
        _mk_drift("high", detected=True),
    )
    assert r["governance_state"] == "manual_review_required"
    assert r["escalation_level"] == "senior_operator"


# ════════════════════════════════════════════════════════════════════
#  Governance path 3: STABLE DRY-RUN
# ════════════════════════════════════════════════════════════════════
def test_m15_stable_dry_run_stabilizing():
    r = evaluate_governance_state(
        _mk_autonomy("hold_dry_run"),
        _mk_readiness("stabilizing", go_live=False),
        _mk_risk("low"),
        _mk_drift("low"),
    )
    assert r["governance_state"] == "observation_only"
    assert r["escalation_level"] == "operator"
    assert r["release_recommendation"] == "hold"
    assert r["recommended_action"] == "continue_observation"


def test_m15_stable_dry_run_ready():
    r = evaluate_governance_state(
        _mk_autonomy("hold_dry_run"),
        _mk_readiness("ready", go_live=False),
        _mk_risk("low"),
        _mk_drift("low"),
    )
    assert r["governance_state"] == "observation_only"
    assert r["escalation_level"] == "operator"


# ════════════════════════════════════════════════════════════════════
#  Governance path 4: GO-LIVE CANDIDATE
# ════════════════════════════════════════════════════════════════════
def test_m15_go_live_candidate_with_drift():
    """allow_live + production_safe + low risk BUT drift detected → candidate."""
    r = evaluate_governance_state(
        _mk_autonomy("allow_live"),
        _mk_readiness("production_safe", go_live=True),
        _mk_risk("low"),
        _mk_drift("low", detected=True),  # drift detected blocks full governed
    )
    assert r["governance_state"] == "approval_candidate"
    assert r["escalation_level"] == "operator"
    assert r["release_recommendation"] == "candidate"
    assert r["recommended_action"] == "prepare_go_live_review"


def test_m15_go_live_candidate_with_errors():
    """allow_live + clean signals BUT internal errors → candidate."""
    r = evaluate_governance_state(
        _mk_autonomy("allow_live"),
        _mk_readiness("production_safe", go_live=True),
        _mk_risk("low", error_weight=5.0),  # error weight > 0 blocks full
        _mk_drift("low", detected=False),
    )
    assert r["governance_state"] == "approval_candidate"
    assert r["release_recommendation"] == "candidate"


# ════════════════════════════════════════════════════════════════════
#  Governance path 5: FULL GOVERNED
# ════════════════════════════════════════════════════════════════════
def test_m15_full_governed_all_clean():
    """allow_live + production_safe + low risk + no drift + no errors."""
    r = evaluate_governance_state(
        _mk_autonomy("allow_live"),
        _mk_readiness("production_safe", go_live=True),
        _mk_risk("low", error_weight=0.0),
        _mk_drift("low", detected=False),
    )
    assert r["governance_state"] == "production_governed"
    assert r["escalation_level"] == "none"
    assert r["release_recommendation"] == "approved_candidate"
    assert r["recommended_action"] == "maintain_production_controls"


# ════════════════════════════════════════════════════════════════════
#  Precedence
# ════════════════════════════════════════════════════════════════════
def test_m15_emergency_beats_high_risk():
    """Critical risk + high drift → emergency wins."""
    r = evaluate_governance_state(
        _mk_autonomy("rollback_required"),
        _mk_readiness("not_ready", go_live=False),
        _mk_risk("critical"),
        _mk_drift("high", detected=True),
    )
    assert r["escalation_level"] == "emergency"


def test_m15_high_risk_beats_stable():
    """High drift + hold_dry_run → high risk (manual review)."""
    r = evaluate_governance_state(
        _mk_autonomy("hold_dry_run"),
        _mk_readiness("ready", go_live=False),
        _mk_risk("low"),
        _mk_drift("high", detected=True),
    )
    assert r["governance_state"] == "manual_review_required"
    assert r["escalation_level"] == "senior_operator"


def test_m15_stable_beats_default():
    """hold_dry_run + ready → observation_only, not default."""
    r = evaluate_governance_state(
        _mk_autonomy("hold_dry_run"),
        _mk_readiness("ready", go_live=False),
        _mk_risk("low"),
        _mk_drift("low"),
    )
    assert r["governance_state"] == "observation_only"


# ════════════════════════════════════════════════════════════════════
#  approval_required mapping
# ════════════════════════════════════════════════════════════════════
def test_m15_approval_required_for_manual_review():
    """manual_review_required → approval_required=True."""
    r = evaluate_governance_state(
        _mk_autonomy("rollback_required"),
        _mk_readiness(), _mk_risk("critical"), _mk_drift(),
    )
    assert r["approval_required"] is True


def test_m15_approval_required_for_candidate():
    """approval_candidate → approval_required=True."""
    r = evaluate_governance_state(
        _mk_autonomy("allow_live"),
        _mk_readiness("production_safe", go_live=True),
        _mk_risk("low"),
        _mk_drift("low", detected=True),  # forces candidate, not governed
    )
    assert r["governance_state"] == "approval_candidate"
    assert r["approval_required"] is True


def test_m15_no_approval_for_observation_only():
    """observation_only → approval_required=False."""
    r = evaluate_governance_state(
        _mk_autonomy("hold_dry_run"),
        _mk_readiness("ready", go_live=False),
        _mk_risk("low"),
        _mk_drift("low"),
    )
    assert r["governance_state"] == "observation_only"
    assert r["approval_required"] is False


def test_m15_no_approval_for_production_governed():
    """production_governed → approval_required=False (ya aprobado)."""
    r = evaluate_governance_state(
        _mk_autonomy("allow_live"),
        _mk_readiness("production_safe", go_live=True),
        _mk_risk("low"),
        _mk_drift("low", detected=False),
    )
    assert r["governance_state"] == "production_governed"
    assert r["approval_required"] is False


# ════════════════════════════════════════════════════════════════════
#  Mapping consistency
# ════════════════════════════════════════════════════════════════════
def test_m15_approval_required_matrix_complete():
    """APPROVAL_REQUIRED_BY_STATE cubre los 4 estados."""
    assert set(APPROVAL_REQUIRED_BY_STATE.keys()) == set(GOVERNANCE_STATES)


def test_m15_constants_match_spec_literal():
    """Las constantes públicas son strings inmutables del spec."""
    assert "observation_only" in GOVERNANCE_STATES
    assert "manual_review_required" in GOVERNANCE_STATES
    assert "approval_candidate" in GOVERNANCE_STATES
    assert "production_governed" in GOVERNANCE_STATES
    assert "emergency" in ESCALATION_LEVELS
    assert "deny" in RELEASE_RECOMMENDATIONS
    assert "approved_candidate" in RELEASE_RECOMMENDATIONS
    assert "request_operator_review" in OPERATOR_ACTIONS
    assert "maintain_production_controls" in OPERATOR_ACTIONS


# ════════════════════════════════════════════════════════════════════
#  Reasons block
# ════════════════════════════════════════════════════════════════════
def test_m15_reasons_emergency_includes_trigger():
    r = evaluate_governance_state(
        _mk_autonomy("rollback_required"),
        _mk_readiness(), _mk_risk("critical"), _mk_drift(),
    )
    reasons_text = " ".join(r["reasons"])
    assert ("rollback" in reasons_text or "critical" in reasons_text)


def test_m15_reasons_full_governed_states_clean():
    r = evaluate_governance_state(
        _mk_autonomy("allow_live"),
        _mk_readiness("production_safe", go_live=True),
        _mk_risk("low"),
        _mk_drift("low", detected=False),
    )
    reasons_text = " ".join(r["reasons"])
    assert "clean" in reasons_text or "full_governance" in reasons_text


# ════════════════════════════════════════════════════════════════════
#  Edge cases / fail-safe
# ════════════════════════════════════════════════════════════════════
def test_m15_empty_inputs_safe_default():
    """Inputs vacíos → fail-safe conservador."""
    r = evaluate_governance_state({}, {}, {}, {})
    # Default state es conservador (manual_review_required o observation_only)
    assert r["governance_state"] in GOVERNANCE_STATES
    assert isinstance(r["approval_required"], bool)


def test_m15_malformed_inputs_no_crash():
    """Inputs raros → fail-safe sin crash."""
    bad_tuples = [
        (None, None, None, None),
        ("str", [], 42, {}),
        ({"decision": []}, {"status": None}, {"risk_level": 0},
         {"severity": False}),
    ]
    for a, rd, rk, dr in bad_tuples:
        try:
            r = evaluate_governance_state(a, rd, rk, dr)
        except Exception as e:
            assert False, f"crashed on {(a, rd, rk, dr)}: {e}"
        assert isinstance(r, dict)
        assert "governance_state" in r
        assert r["governance_state"] in GOVERNANCE_STATES


def test_m15_signals_block_reflects_inputs():
    r = evaluate_governance_state(
        _mk_autonomy("hold_dry_run"),
        _mk_readiness("stabilizing", go_live=False),
        _mk_risk("medium"),
        _mk_drift("medium", detected=True),
    )
    s = r["signals"]
    assert s["autonomy_decision"] == "hold_dry_run"
    assert s["risk_level"] == "medium"
    assert s["readiness_status"] == "stabilizing"
    assert s["drift_severity"] == "medium"


def test_m15_advisory_only_no_execution_imports():
    """M15 NO importa subprocess, exec, ni componentes core."""
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "governance",
        "purchase_governance.py",
    )
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    assert "subprocess" not in src
    assert "os.system" not in src
    assert "core.diagnostics.purchase_gate" not in src
    assert "_compra_parallel" not in src
    assert "import asyncio" not in src
