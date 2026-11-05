"""tests/test_purchase_audit_traceability.py — M17 Audit & Traceability tests.

Cobertura mínima per spec (25+ tests):
    - schema validation
    - deterministic behavior
    - no mutation of inputs
    - advisory-only verification
    - integrity check validation (4 checks)
    - lineage validation
    - trace_id deterministic
    - inconsistent path
    - review_required path
    - governed path
    - verified path
    - malformed input safe handling
    - alias verification
"""
import copy

import pytest

from core.observability.audit import (
    AUDIT_STATES,
    INTEGRITY_CHECK_NAMES,
    build_audit_trace,
    compute_decision_traceability_report,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers — construcción de reports sintéticos coherentes
# ════════════════════════════════════════════════════════════════════
def _mk_drift(severity="low"):
    return {
        "drift_detected": severity != "low",
        "severity": severity,
        "signals": [],
        "baseline": {},
    }


def _mk_risk(level="low"):
    return {
        "risk_score": 10.0,
        "risk_level": level,
        "contributors": {
            "drift_weight": 0.0,
            "error_weight": 0.0,
            "block_weight": 0.0,
            "volatility_weight": 0.0,
        },
        "recommendation": "continue",
    }


def _mk_readiness(status="production_safe", go_live=True):
    return {
        "readiness_index": 90.0,
        "status": status,
        "go_live_eligibility": go_live,
        "factors": {},
    }


def _mk_autonomy(decision="allow_live"):
    return {
        "decision": decision,
        "confidence": 90.0,
        "autonomy_level": "candidate_autonomous",
        "operator_action": "continue_monitoring",
        "signals": {"risk_level": "low"},
    }


def _mk_governance(state="production_governed"):
    return {
        "governance_state": state,
        "approval_required": False,
        "recommended_action": "maintain_production_controls",
        "release_recommendation": "approved_candidate",
        "escalation_level": "none",
        "signals": {},
    }


def _mk_policy(state="compliant"):
    return {
        "policy_state": state,
        "compliance_level": "full",
        "deployment_authorization": state == "compliant",
        "recommended_policy_action": "maintain_compliance",
        "operator_review_required": state != "compliant",
        "signals": {},
    }


def _full_stack_coherent(label="clean"):
    """Devuelve los 6 reports en estado coherente según label.

    label="clean"      → todos limpios, audit verified
    label="critical"   → todos críticos, audit review_required
    label="approval"   → governance approval_candidate, policy controlled
    """
    if label == "clean":
        return (
            _mk_drift("low"),
            _mk_risk("low"),
            _mk_readiness("production_safe", True),
            _mk_autonomy("allow_live"),
            _mk_governance("production_governed"),
            _mk_policy("compliant"),
        )
    if label == "critical":
        return (
            _mk_drift("critical"),
            _mk_risk("critical"),
            _mk_readiness("not_ready", False),
            _mk_autonomy("rollback_required"),
            _mk_governance("manual_review_required"),
            _mk_policy("blocked"),
        )
    if label == "approval":
        return (
            _mk_drift("low"),
            _mk_risk("low"),
            _mk_readiness("production_safe", True),
            _mk_autonomy("allow_live"),
            _mk_governance("approval_candidate"),
            _mk_policy("controlled"),
        )
    if label == "restricted":
        return (
            _mk_drift("medium"),
            _mk_risk("medium"),
            _mk_readiness("stabilizing", False),
            _mk_autonomy("hold_dry_run"),
            _mk_governance("observation_only"),
            _mk_policy("restricted"),
        )
    raise ValueError(label)


# ════════════════════════════════════════════════════════════════════
#  Schema / pure / determinismo / alias
# ════════════════════════════════════════════════════════════════════
def test_m17_output_schema_complete():
    """Output contiene las 10 claves obligatorias."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    required = {
        "trace_id", "audit_state", "decision_lineage",
        "decision_consistency", "human_review_required",
        "deployment_authorized", "signals", "reasoning_chain",
        "integrity_checks", "timestamp",
    }
    assert required.issubset(r.keys())


def test_m17_signals_block_complete():
    """Signals contiene las 6 claves obligatorias."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    required = {
        "drift_severity", "risk_level", "readiness_status",
        "autonomy_decision", "governance_state", "policy_state",
    }
    assert required.issubset(r["signals"].keys())


def test_m17_integrity_checks_block_complete():
    """integrity_checks contiene las 4 verificaciones."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    required = {
        "layer_order_valid", "policy_matches_governance",
        "governance_matches_autonomy", "risk_matches_drift",
    }
    assert required == set(r["integrity_checks"].keys())


def test_m17_pure_function_no_mutation():
    """build_audit_trace NO modifica los inputs."""
    inputs = _full_stack_coherent("clean")
    copies = tuple(copy.deepcopy(i) for i in inputs)
    build_audit_trace(*inputs)
    for original, copy_ref in zip(inputs, copies):
        assert original == copy_ref


def test_m17_deterministic_same_input():
    """Mismo input → mismo output (timestamp aparte)."""
    inputs = _full_stack_coherent("clean")
    r1 = build_audit_trace(*inputs)
    r2 = build_audit_trace(*inputs)
    assert r1["trace_id"] == r2["trace_id"]
    assert r1["audit_state"] == r2["audit_state"]
    assert r1["decision_lineage"] == r2["decision_lineage"]
    assert r1["integrity_checks"] == r2["integrity_checks"]
    assert r1["signals"] == r2["signals"]


def test_m17_spec_alias_works():
    """compute_decision_traceability_report es alias funcional."""
    inputs = _full_stack_coherent("clean")
    r1 = build_audit_trace(*inputs)
    r2 = compute_decision_traceability_report(*inputs)
    assert r1["trace_id"] == r2["trace_id"]
    assert r1["audit_state"] == r2["audit_state"]


# ════════════════════════════════════════════════════════════════════
#  Trace ID determinístico
# ════════════════════════════════════════════════════════════════════
def test_m17_trace_id_deterministic():
    """Mismos inputs canónicos → mismo trace_id."""
    inputs = _full_stack_coherent("clean")
    r1 = build_audit_trace(*inputs)
    r2 = build_audit_trace(*inputs)
    assert r1["trace_id"] == r2["trace_id"]


def test_m17_trace_id_changes_with_inputs():
    """Inputs distintos → trace_id distinto."""
    r_clean = build_audit_trace(*_full_stack_coherent("clean"))
    r_crit = build_audit_trace(*_full_stack_coherent("critical"))
    assert r_clean["trace_id"] != r_crit["trace_id"]


def test_m17_trace_id_format_is_hex_16_chars():
    """trace_id es exactamente 16 caracteres hex."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    tid = r["trace_id"]
    assert isinstance(tid, str)
    assert len(tid) == 16
    int(tid, 16)  # valida que sean hex chars (raises si no)


# ════════════════════════════════════════════════════════════════════
#  Integrity check: risk_matches_drift
# ════════════════════════════════════════════════════════════════════
def test_m17_risk_matches_drift_true_for_aligned_critical():
    """drift=critical + risk=critical → check pasa."""
    r = build_audit_trace(*_full_stack_coherent("critical"))
    assert r["integrity_checks"]["risk_matches_drift"] is True


def test_m17_risk_matches_drift_false_for_mismatched():
    """drift=critical + risk=low → check falla."""
    r = build_audit_trace(
        _mk_drift("critical"),
        _mk_risk("low"),  # mismatch — debería ser high o critical
        _mk_readiness(),
        _mk_autonomy("hold_dry_run"),
        _mk_governance("manual_review_required"),
        _mk_policy("blocked"),
    )
    assert r["integrity_checks"]["risk_matches_drift"] is False


# ════════════════════════════════════════════════════════════════════
#  Integrity check: governance_matches_autonomy
# ════════════════════════════════════════════════════════════════════
def test_m17_governance_matches_autonomy_true_aligned():
    """allow_live + production_governed → check pasa."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    assert r["integrity_checks"]["governance_matches_autonomy"] is True


def test_m17_governance_matches_autonomy_false_when_mismatched():
    """rollback_required + observation_only → check falla."""
    r = build_audit_trace(
        _mk_drift("low"),
        _mk_risk("low"),
        _mk_readiness(),
        _mk_autonomy("rollback_required"),
        _mk_governance("observation_only"),  # mismatch — debería ser manual_review
        _mk_policy("restricted"),
    )
    assert r["integrity_checks"]["governance_matches_autonomy"] is False


# ════════════════════════════════════════════════════════════════════
#  Integrity check: policy_matches_governance
# ════════════════════════════════════════════════════════════════════
def test_m17_policy_matches_governance_true_aligned():
    """production_governed + compliant → check pasa."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    assert r["integrity_checks"]["policy_matches_governance"] is True


def test_m17_policy_matches_governance_false_when_mismatched():
    """manual_review_required + compliant → check falla (debería ser blocked)."""
    r = build_audit_trace(
        _mk_drift("low"),
        _mk_risk("low"),
        _mk_readiness(),
        _mk_autonomy("rollback_required"),
        _mk_governance("manual_review_required"),
        _mk_policy("compliant"),  # mismatch — debería ser blocked
    )
    assert r["integrity_checks"]["policy_matches_governance"] is False


# ════════════════════════════════════════════════════════════════════
#  Integrity check: layer_order_valid (agregado)
# ════════════════════════════════════════════════════════════════════
def test_m17_layer_order_valid_when_all_pass():
    """Stack coherente → layer_order_valid = True."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    assert r["integrity_checks"]["layer_order_valid"] is True


def test_m17_layer_order_valid_false_when_any_check_fails():
    """Si un sub-check falla → layer_order_valid = False."""
    r = build_audit_trace(
        _mk_drift("critical"),
        _mk_risk("low"),  # forza risk_matches_drift=False
        _mk_readiness(),
        _mk_autonomy("rollback_required"),
        _mk_governance("manual_review_required"),
        _mk_policy("blocked"),
    )
    assert r["integrity_checks"]["risk_matches_drift"] is False
    assert r["integrity_checks"]["layer_order_valid"] is False


# ════════════════════════════════════════════════════════════════════
#  decision_consistency
# ════════════════════════════════════════════════════════════════════
def test_m17_decision_consistency_true_when_all_checks_pass():
    r = build_audit_trace(*_full_stack_coherent("clean"))
    assert r["decision_consistency"] is True


def test_m17_decision_consistency_false_when_any_check_fails():
    r = build_audit_trace(
        _mk_drift("critical"),
        _mk_risk("low"),
        _mk_readiness(),
        _mk_autonomy("rollback_required"),
        _mk_governance("manual_review_required"),
        _mk_policy("blocked"),
    )
    assert r["decision_consistency"] is False


# ════════════════════════════════════════════════════════════════════
#  Audit state paths
# ════════════════════════════════════════════════════════════════════
def test_m17_inconsistent_path_overrides_policy():
    """Inconsistency tiene precedencia sobre policy_state."""
    r = build_audit_trace(
        _mk_drift("critical"),
        _mk_risk("low"),  # mismatch → inconsistent
        _mk_readiness(),
        _mk_autonomy("rollback_required"),
        _mk_governance("manual_review_required"),
        _mk_policy("blocked"),
    )
    assert r["audit_state"] == "inconsistent"
    assert r["deployment_authorized"] is False
    assert r["human_review_required"] is True


def test_m17_review_required_path_blocked():
    """policy_state=blocked + consistency → review_required."""
    r = build_audit_trace(*_full_stack_coherent("critical"))
    # Critical scenario es coherente → no inconsistent
    assert r["audit_state"] == "review_required"
    assert r["deployment_authorized"] is False


def test_m17_review_required_path_restricted():
    """policy_state=restricted + consistency → review_required."""
    r = build_audit_trace(*_full_stack_coherent("restricted"))
    assert r["audit_state"] == "review_required"


def test_m17_governed_path_controlled():
    """policy_state=controlled + consistency → governed."""
    r = build_audit_trace(*_full_stack_coherent("approval"))
    assert r["audit_state"] == "governed"
    assert r["deployment_authorized"] is False
    assert r["human_review_required"] is True


def test_m17_verified_path_when_compliant_and_consistent():
    """policy=compliant + consistency → verified + deploy authorized."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    assert r["audit_state"] == "verified"
    assert r["deployment_authorized"] is True
    assert r["human_review_required"] is False


def test_m17_unknown_policy_state_triggers_inconsistent():
    """policy_state desconocido rompe policy_matches_governance →
    integrity check falla → audit_state=inconsistent (precedencia sobre
    default safe). Comportamiento correcto by design: cualquier policy
    no canónica es inconsistencia, no estado default.
    """
    r = build_audit_trace(
        _mk_drift("low"),
        _mk_risk("low"),
        _mk_readiness("production_safe", True),
        _mk_autonomy("allow_live"),
        _mk_governance("production_governed"),
        {"policy_state": "unknown_state_xyz"},
    )
    # policy_matches_governance falla porque governance=production_governed
    # exige policy=compliant. Cualquier otro valor → inconsistencia.
    assert r["integrity_checks"]["policy_matches_governance"] is False
    assert r["audit_state"] == "inconsistent"
    assert r["deployment_authorized"] is False


# ════════════════════════════════════════════════════════════════════
#  Reasoning chain (lineage)
# ════════════════════════════════════════════════════════════════════
def test_m17_reasoning_chain_length_seven():
    """reasoning_chain refleja las 7 capas: drift→risk→readiness→autonomy→governance→policy→audit."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    assert len(r["reasoning_chain"]) == 7
    assert len(r["decision_lineage"]) == 7


def test_m17_reasoning_chain_reflects_layers():
    """reasoning_chain menciona explícitamente cada capa en orden."""
    r = build_audit_trace(*_full_stack_coherent("clean"))
    chain = r["reasoning_chain"]
    # En orden: drift, risk, readiness, autonomy, governance, policy, audit
    assert "drift" in chain[0]
    assert "risk" in chain[1]
    assert "readiness" in chain[2]
    assert "autonomy" in chain[3]
    assert "governance" in chain[4]
    assert "policy" in chain[5]
    assert "audit" in chain[6]


# ════════════════════════════════════════════════════════════════════
#  Deployment authorization mapping
# ════════════════════════════════════════════════════════════════════
def test_m17_deployment_authorized_only_in_verified():
    """deployment_authorized=True solo cuando audit_state=verified."""
    for label in ("critical", "restricted", "approval"):
        r = build_audit_trace(*_full_stack_coherent(label))
        assert r["deployment_authorized"] is False, (
            f"label={label} not expected to authorize deployment"
        )
    # Solo clean autoriza
    r_clean = build_audit_trace(*_full_stack_coherent("clean"))
    assert r_clean["audit_state"] == "verified"
    assert r_clean["deployment_authorized"] is True


def test_m17_human_review_required_inverse_of_verified():
    """human_review_required=False solo cuando audit_state=verified+consistent."""
    r_clean = build_audit_trace(*_full_stack_coherent("clean"))
    assert r_clean["human_review_required"] is False
    for label in ("critical", "restricted", "approval"):
        r = build_audit_trace(*_full_stack_coherent(label))
        assert r["human_review_required"] is True, (
            f"label={label} should require review"
        )


# ════════════════════════════════════════════════════════════════════
#  Constantes públicas
# ════════════════════════════════════════════════════════════════════
def test_m17_audit_states_canonical():
    """AUDIT_STATES contiene los 4 valores del spec."""
    assert set(AUDIT_STATES) == {
        "inconsistent", "review_required", "governed", "verified",
    }


def test_m17_integrity_check_names_canonical():
    """INTEGRITY_CHECK_NAMES contiene los 4 nombres del spec."""
    assert set(INTEGRITY_CHECK_NAMES) == {
        "layer_order_valid", "policy_matches_governance",
        "governance_matches_autonomy", "risk_matches_drift",
    }


# ════════════════════════════════════════════════════════════════════
#  Edge cases / fail-safe
# ════════════════════════════════════════════════════════════════════
def test_m17_empty_inputs_safe_default():
    """Inputs vacíos → audit_state seguro, sin autorización."""
    r = build_audit_trace({}, {}, {}, {}, {}, {})
    assert r["audit_state"] in AUDIT_STATES
    assert r["deployment_authorized"] is False
    assert r["human_review_required"] is True


def test_m17_malformed_inputs_no_crash():
    """Inputs raros → fail-safe sin crash."""
    bad_tuples = [
        (None, None, None, None, None, None),
        ("str", [], 42, {}, None, ()),
        ({"severity": []}, {"risk_level": None}, {"status": False},
         {"decision": 0}, {"governance_state": []}, {"policy_state": None}),
    ]
    for inputs in bad_tuples:
        try:
            r = build_audit_trace(*inputs)
        except Exception as e:
            assert False, f"crashed on {inputs}: {e}"
        assert isinstance(r, dict)
        assert "audit_state" in r
        assert r["audit_state"] in AUDIT_STATES
        # Fail-safe nunca autoriza
        assert r["deployment_authorized"] is False


def test_m17_signals_block_reflects_inputs():
    """Signals contiene los valores literales de los inputs."""
    r = build_audit_trace(
        _mk_drift("medium"),
        _mk_risk("medium"),
        _mk_readiness("stabilizing", False),
        _mk_autonomy("hold_dry_run"),
        _mk_governance("observation_only"),
        _mk_policy("restricted"),
    )
    s = r["signals"]
    assert s["drift_severity"] == "medium"
    assert s["risk_level"] == "medium"
    assert s["readiness_status"] == "stabilizing"
    assert s["autonomy_decision"] == "hold_dry_run"
    assert s["governance_state"] == "observation_only"
    assert s["policy_state"] == "restricted"


def test_m17_advisory_only_no_execution_imports():
    """M17 NO importa módulos de ejecución, core ni networking."""
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "audit",
        "purchase_audit_traceability.py",
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
        assert token not in src, f"M17 contiene token prohibido: {token}"
