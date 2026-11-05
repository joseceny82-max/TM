"""tests/test_purchase_policy_compliance.py — M16 Policy & Compliance tests.

Cobertura mínima per spec (20+ tests):
    - schema validation
    - deterministic behavior
    - no mutation of inputs
    - advisory-only verification
    - blocked path
    - restricted path
    - controlled path
    - compliant path
    - precedence validation
    - malformed input safe handling
    - alias verification
    - compliance matrix integrity
"""
import copy

import pytest

from core.observability.policy import (
    COMPLIANCE_LEVELS,
    POLICY_ACTIONS,
    POLICY_STATES,
    POLICY_STATE_TO_ACTION,
    POLICY_STATE_TO_AUTHORIZATION,
    POLICY_STATE_TO_COMPLIANCE,
    POLICY_STATE_TO_REVIEW_REQUIRED,
    compute_policy_compliance_state,
    evaluate_policy_compliance,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers para construir reports sintéticos
# ════════════════════════════════════════════════════════════════════
def _mk_autonomy(*, decision="allow_live", risk_level="low"):
    return {
        "decision": decision,
        "confidence": 90.0,
        "autonomy_level": "candidate_autonomous",
        "operator_action": "continue_monitoring",
        "signals": {
            "drift_severity": "low",
            "risk_level": risk_level,
            "readiness_status": "production_safe",
            "go_live_eligibility": True,
        },
    }


def _mk_governance(*, state="production_governed"):
    return {
        "governance_state": state,
        "approval_required": False,
        "recommended_action": "maintain_production_controls",
        "release_recommendation": "approved_candidate",
        "escalation_level": "none",
        "signals": {},
        "reasons": [],
    }


def _mk_readiness(*, status="production_safe", go_live=True):
    return {
        "readiness_index": 90.0,
        "status": status,
        "go_live_eligibility": go_live,
        "factors": {},
    }


# ════════════════════════════════════════════════════════════════════
#  Schema / pure / determinismo / alias
# ════════════════════════════════════════════════════════════════════
def test_m16_output_schema_complete():
    """Output contiene las 8 claves obligatorias."""
    r = evaluate_policy_compliance(
        _mk_autonomy(), _mk_governance(), _mk_readiness(),
    )
    required = {
        "policy_state", "compliance_level", "deployment_authorization",
        "recommended_policy_action", "operator_review_required",
        "signals", "reasons", "timestamp",
    }
    assert required.issubset(r.keys())


def test_m16_signals_block_complete():
    """Signals contiene las 4 claves obligatorias."""
    r = evaluate_policy_compliance(
        _mk_autonomy(), _mk_governance(), _mk_readiness(),
    )
    required = {"governance_state", "autonomy_decision",
                "risk_level", "readiness_status"}
    assert required.issubset(r["signals"].keys())


def test_m16_pure_function_no_mutation():
    """evaluate_policy_compliance NO modifica los inputs."""
    a = _mk_autonomy(decision="pause_system")
    g = _mk_governance(state="manual_review_required")
    rd = _mk_readiness(status="not_ready", go_live=False)
    a_c, g_c, rd_c = copy.deepcopy(a), copy.deepcopy(g), copy.deepcopy(rd)
    evaluate_policy_compliance(a, g, rd)
    assert a == a_c
    assert g == g_c
    assert rd == rd_c


def test_m16_deterministic_same_input():
    """Mismo input → misma decisión (timestamp aparte)."""
    a = _mk_autonomy(decision="hold_dry_run")
    g = _mk_governance(state="observation_only")
    rd = _mk_readiness(status="ready", go_live=False)
    r1 = evaluate_policy_compliance(a, g, rd)
    r2 = evaluate_policy_compliance(a, g, rd)
    assert r1["policy_state"] == r2["policy_state"]
    assert r1["compliance_level"] == r2["compliance_level"]
    assert r1["deployment_authorization"] == r2["deployment_authorization"]
    assert r1["recommended_policy_action"] == r2["recommended_policy_action"]
    assert r1["operator_review_required"] == r2["operator_review_required"]
    assert r1["signals"] == r2["signals"]


def test_m16_spec_alias_works():
    """compute_policy_compliance_state es alias funcional."""
    a, g, rd = _mk_autonomy(), _mk_governance(), _mk_readiness()
    r1 = evaluate_policy_compliance(a, g, rd)
    r2 = compute_policy_compliance_state(a, g, rd)
    assert r1["policy_state"] == r2["policy_state"]
    assert r1["compliance_level"] == r2["compliance_level"]


# ════════════════════════════════════════════════════════════════════
#  Ruta 1: BLOCKED
# ════════════════════════════════════════════════════════════════════
def test_m16_blocked_via_manual_review_governance():
    """governance=manual_review_required → blocked."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="pause_system"),
        _mk_governance(state="manual_review_required"),
        _mk_readiness(status="not_ready", go_live=False),
    )
    assert r["policy_state"] == "blocked"
    assert r["compliance_level"] == "non_compliant"
    assert r["deployment_authorization"] is False
    assert r["operator_review_required"] is True
    assert r["recommended_policy_action"] == "halt_and_review"


def test_m16_blocked_via_rollback_autonomy():
    """autonomy=rollback_required → blocked (incluso si governance no es manual_review)."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="rollback_required"),
        _mk_governance(state="observation_only"),
        _mk_readiness(status="ready", go_live=False),
    )
    assert r["policy_state"] == "blocked"
    assert r["deployment_authorization"] is False


# ════════════════════════════════════════════════════════════════════
#  Ruta 2: RESTRICTED
# ════════════════════════════════════════════════════════════════════
def test_m16_restricted_via_observation_only():
    """governance=observation_only (sin blocked) → restricted."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="hold_dry_run"),
        _mk_governance(state="observation_only"),
        _mk_readiness(status="ready", go_live=False),
    )
    assert r["policy_state"] == "restricted"
    assert r["compliance_level"] == "limited"
    assert r["deployment_authorization"] is False
    assert r["operator_review_required"] is True
    assert r["recommended_policy_action"] == "request_authorization"


def test_m16_restricted_via_pause_autonomy():
    """autonomy=pause_system (sin blocked) → restricted."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="pause_system"),
        _mk_governance(state="approval_candidate"),
        _mk_readiness(status="ready", go_live=False),
    )
    assert r["policy_state"] == "restricted"
    assert r["deployment_authorization"] is False


# ════════════════════════════════════════════════════════════════════
#  Ruta 3: CONTROLLED
# ════════════════════════════════════════════════════════════════════
def test_m16_controlled_when_approval_candidate_ready():
    """governance=approval_candidate AND readiness=ready → controlled."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="allow_live"),
        _mk_governance(state="approval_candidate"),
        _mk_readiness(status="ready", go_live=False),
    )
    assert r["policy_state"] == "controlled"
    assert r["compliance_level"] == "supervised"
    assert r["deployment_authorization"] is False
    assert r["operator_review_required"] is True
    assert r["recommended_policy_action"] == "await_approval"


def test_m16_controlled_when_approval_candidate_production_safe():
    """governance=approval_candidate AND readiness=production_safe → controlled."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="allow_live"),
        _mk_governance(state="approval_candidate"),
        _mk_readiness(status="production_safe", go_live=True),
    )
    assert r["policy_state"] == "controlled"
    assert r["compliance_level"] == "supervised"


def test_m16_not_controlled_when_approval_candidate_not_ready():
    """approval_candidate + readiness=stabilizing → cae a default restricted."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="allow_live"),
        _mk_governance(state="approval_candidate"),
        _mk_readiness(status="stabilizing", go_live=False),
    )
    # Cae al default safe (restricted)
    assert r["policy_state"] == "restricted"


# ════════════════════════════════════════════════════════════════════
#  Ruta 4: COMPLIANT
# ════════════════════════════════════════════════════════════════════
def test_m16_compliant_all_three_conditions():
    """governance=production_governed + readiness=production_safe + autonomy=allow_live → compliant."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="allow_live"),
        _mk_governance(state="production_governed"),
        _mk_readiness(status="production_safe", go_live=True),
    )
    assert r["policy_state"] == "compliant"
    assert r["compliance_level"] == "full"
    assert r["deployment_authorization"] is True
    assert r["operator_review_required"] is False
    assert r["recommended_policy_action"] == "maintain_compliance"


def test_m16_not_compliant_when_governance_not_production_governed():
    """Si governance != production_governed → no compliant."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="allow_live"),
        _mk_governance(state="approval_candidate"),
        _mk_readiness(status="production_safe", go_live=True),
    )
    assert r["policy_state"] != "compliant"
    assert r["deployment_authorization"] is False


def test_m16_not_compliant_when_autonomy_not_allow_live():
    """Si autonomy != allow_live → no compliant."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="hold_dry_run"),
        _mk_governance(state="production_governed"),
        _mk_readiness(status="production_safe", go_live=True),
    )
    assert r["policy_state"] != "compliant"
    assert r["deployment_authorization"] is False


# ════════════════════════════════════════════════════════════════════
#  Ruta 5: DEFAULT SAFE
# ════════════════════════════════════════════════════════════════════
def test_m16_default_safe_for_unmapped_combination():
    """Combinación que no encaja en 1-4 → restricted (default safe)."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="hold_dry_run"),
        _mk_governance(state="approval_candidate"),
        _mk_readiness(status="stabilizing", go_live=False),
    )
    assert r["policy_state"] == "restricted"
    assert r["deployment_authorization"] is False


# ════════════════════════════════════════════════════════════════════
#  Precedencia estricta
# ════════════════════════════════════════════════════════════════════
def test_m16_blocked_beats_restricted():
    """rollback (blocked) + pause (restricted) → blocked gana."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="rollback_required"),
        _mk_governance(state="observation_only"),
        _mk_readiness(),
    )
    assert r["policy_state"] == "blocked"


def test_m16_restricted_beats_controlled():
    """pause (restricted) + approval_candidate (controlled potencial) → restricted gana."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="pause_system"),
        _mk_governance(state="approval_candidate"),
        _mk_readiness(status="production_safe", go_live=True),
    )
    assert r["policy_state"] == "restricted"


# ════════════════════════════════════════════════════════════════════
#  Mapping de campos por policy_state
# ════════════════════════════════════════════════════════════════════
def test_m16_deployment_authorization_only_for_compliant():
    """deployment_authorization=True solo para policy_state=compliant."""
    # blocked, restricted, controlled → False
    for state in ("blocked", "restricted", "controlled"):
        assert POLICY_STATE_TO_AUTHORIZATION[state] is False
    # compliant → True
    assert POLICY_STATE_TO_AUTHORIZATION["compliant"] is True


def test_m16_operator_review_required_inverse_of_compliant():
    """operator_review_required=False solo para compliant."""
    assert POLICY_STATE_TO_REVIEW_REQUIRED["blocked"] is True
    assert POLICY_STATE_TO_REVIEW_REQUIRED["restricted"] is True
    assert POLICY_STATE_TO_REVIEW_REQUIRED["controlled"] is True
    assert POLICY_STATE_TO_REVIEW_REQUIRED["compliant"] is False


def test_m16_action_mapping_canonical():
    """recommended_policy_action mapea según spec."""
    assert POLICY_STATE_TO_ACTION["blocked"] == "halt_and_review"
    assert POLICY_STATE_TO_ACTION["restricted"] == "request_authorization"
    assert POLICY_STATE_TO_ACTION["controlled"] == "await_approval"
    assert POLICY_STATE_TO_ACTION["compliant"] == "maintain_compliance"


def test_m16_compliance_level_mapping_canonical():
    """compliance_level mapea según spec."""
    assert POLICY_STATE_TO_COMPLIANCE["blocked"] == "non_compliant"
    assert POLICY_STATE_TO_COMPLIANCE["restricted"] == "limited"
    assert POLICY_STATE_TO_COMPLIANCE["controlled"] == "supervised"
    assert POLICY_STATE_TO_COMPLIANCE["compliant"] == "full"


def test_m16_constants_match_spec():
    """Las 3 tuplas de constantes cubren los valores literales del spec."""
    assert set(POLICY_STATES) == {
        "blocked", "restricted", "controlled", "compliant",
    }
    assert set(COMPLIANCE_LEVELS) == {
        "non_compliant", "limited", "supervised", "full",
    }
    assert set(POLICY_ACTIONS) == {
        "halt_and_review", "request_authorization",
        "await_approval", "maintain_compliance",
    }


# ════════════════════════════════════════════════════════════════════
#  Reasons block
# ════════════════════════════════════════════════════════════════════
def test_m16_reasons_blocked_includes_trigger():
    """Cuando blocked, reasons indica la causa (rollback o manual_review)."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="rollback_required"),
        _mk_governance(state="manual_review_required"),
        _mk_readiness(),
    )
    text = " ".join(r["reasons"])
    assert "rollback" in text or "manual_review" in text


def test_m16_reasons_compliant_includes_clean_state():
    """Cuando compliant, reasons indica todos los signals alineados."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="allow_live"),
        _mk_governance(state="production_governed"),
        _mk_readiness(status="production_safe", go_live=True),
    )
    text = " ".join(r["reasons"])
    assert "compliance" in text or "aligned" in text or "allow_live" in text


# ════════════════════════════════════════════════════════════════════
#  Edge cases / fail-safe
# ════════════════════════════════════════════════════════════════════
def test_m16_empty_inputs_safe_default():
    """Inputs vacíos → policy_state seguro (restricted o blocked)."""
    r = evaluate_policy_compliance({}, {}, {})
    assert r["policy_state"] in POLICY_STATES
    # Default seguro: NO autoriza deployment
    assert r["deployment_authorization"] is False
    assert r["operator_review_required"] is True


def test_m16_malformed_inputs_no_crash():
    """Inputs raros → fail-safe sin crash."""
    bad_tuples = [
        (None, None, None),
        ("str", [], 42),
        ({"decision": []}, {"governance_state": None},
         {"status": False}),
    ]
    for a, g, rd in bad_tuples:
        try:
            r = evaluate_policy_compliance(a, g, rd)
        except Exception as e:
            assert False, f"crashed on {(a, g, rd)}: {e}"
        assert isinstance(r, dict)
        assert "policy_state" in r
        assert r["policy_state"] in POLICY_STATES
        # Fail-safe nunca autoriza
        assert r["deployment_authorization"] is False


def test_m16_signals_block_reflects_inputs():
    """El bloque signals refleja los inputs exactamente."""
    r = evaluate_policy_compliance(
        _mk_autonomy(decision="hold_dry_run", risk_level="medium"),
        _mk_governance(state="approval_candidate"),
        _mk_readiness(status="stabilizing", go_live=False),
    )
    s = r["signals"]
    assert s["governance_state"] == "approval_candidate"
    assert s["autonomy_decision"] == "hold_dry_run"
    assert s["risk_level"] == "medium"
    assert s["readiness_status"] == "stabilizing"


def test_m16_advisory_only_no_execution_imports():
    """M16 NO importa módulos de ejecución, core ni networking."""
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "policy",
        "purchase_policy_compliance.py",
    )
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    forbidden = [
        "subprocess", "os.system", "import asyncio",
        "core.diagnostics.purchase_gate",
        "_compra_parallel",
        "import socket", "urllib", "requests.",
    ]
    for token in forbidden:
        assert token not in src, f"M16 contiene token prohibido: {token}"
