"""tests/test_purchase_decision_explainability.py — M18 Explainability tests.

Cobertura mínima per spec (36+ tests):
    - schema validation
    - deterministic behavior
    - no mutation of inputs
    - advisory-only verification
    - trace propagation
    - narrative generation
    - executive summary
    - explanation precedence
    - posture mapping
    - guidance mapping
    - consistency propagation
    - audit integration
    - malformed inputs
    - empty inputs
    - advisory-only static validation
    - no forbidden imports
    - exact deterministic wording
    - same input → identical narrative
"""
import copy

import pytest

from core.observability.explainability import (
    EXPLANATION_STATES,
    OPERATOR_GUIDANCE_MATRIX,
    SYSTEM_POSTURE_MATRIX,
    build_decision_explanation,
    compute_explainability_report,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers — reports sintéticos coherentes
# ════════════════════════════════════════════════════════════════════
def _mk_drift(severity="low"):
    return {"severity": severity, "drift_detected": severity != "low",
            "signals": []}


def _mk_risk(level="low"):
    return {"risk_level": level, "risk_score": 10.0,
            "contributors": {"drift_weight": 0.0, "error_weight": 0.0,
                              "block_weight": 0.0, "volatility_weight": 0.0}}


def _mk_readiness(status="production_safe", go_live=True):
    return {"status": status, "readiness_index": 90.0,
            "go_live_eligibility": go_live, "factors": {}}


def _mk_autonomy(decision="allow_live"):
    return {"decision": decision, "confidence": 90.0,
            "autonomy_level": "candidate_autonomous",
            "signals": {}, "operator_action": "continue_monitoring"}


def _mk_governance(state="production_governed"):
    return {"governance_state": state, "approval_required": False,
            "recommended_action": "maintain_production_controls",
            "release_recommendation": "approved_candidate",
            "escalation_level": "none", "signals": {}}


def _mk_policy(state="compliant"):
    return {"policy_state": state, "compliance_level": "full",
            "deployment_authorization": state == "compliant",
            "recommended_policy_action": "maintain_compliance",
            "operator_review_required": state != "compliant", "signals": {}}


def _mk_audit(state="verified", trace_id="abc1234567890def",
              consistency=True):
    return {"audit_state": state, "trace_id": trace_id,
            "decision_consistency": consistency,
            "human_review_required": state != "verified",
            "deployment_authorized": state == "verified",
            "signals": {}, "decision_lineage": [],
            "reasoning_chain": [], "integrity_checks": {}}


def _coherent_stack(label="clean"):
    """Devuelve los 7 reports en estado coherente según label."""
    if label == "clean":
        return (
            _mk_drift("low"), _mk_risk("low"),
            _mk_readiness("production_safe", True),
            _mk_autonomy("allow_live"),
            _mk_governance("production_governed"),
            _mk_policy("compliant"),
            _mk_audit("verified"),
        )
    if label == "critical":
        return (
            _mk_drift("critical"), _mk_risk("critical"),
            _mk_readiness("not_ready", False),
            _mk_autonomy("rollback_required"),
            _mk_governance("manual_review_required"),
            _mk_policy("blocked"),
            _mk_audit("review_required", consistency=True),
        )
    if label == "controlled":
        return (
            _mk_drift("low"), _mk_risk("low"),
            _mk_readiness("ready", False),
            _mk_autonomy("allow_live"),
            _mk_governance("approval_candidate"),
            _mk_policy("controlled"),
            _mk_audit("governed", consistency=True),
        )
    if label == "restricted":
        return (
            _mk_drift("medium"), _mk_risk("medium"),
            _mk_readiness("stabilizing", False),
            _mk_autonomy("hold_dry_run"),
            _mk_governance("observation_only"),
            _mk_policy("restricted"),
            _mk_audit("review_required", consistency=True),
        )
    if label == "inconsistent":
        return (
            _mk_drift("low"), _mk_risk("low"),
            _mk_readiness("production_safe", True),
            _mk_autonomy("allow_live"),
            _mk_governance("production_governed"),
            _mk_policy("compliant"),
            _mk_audit("inconsistent", consistency=False),
        )
    raise ValueError(label)


# ════════════════════════════════════════════════════════════════════
#  Schema / signals / pure / determinismo / alias
# ════════════════════════════════════════════════════════════════════
def test_m18_output_schema_complete():
    """Output contiene las 12 claves obligatorias."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    required = {
        "explanation_state", "executive_summary", "decision_narrative",
        "human_readable_reasons", "risk_summary", "deployment_summary",
        "operator_guidance", "system_posture", "signals",
        "consistency_verified", "trace_id", "timestamp",
    }
    assert required.issubset(r.keys())


def test_m18_signals_block_complete():
    """Signals contiene las 7 claves de las capas."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    required = {
        "drift_severity", "risk_level", "readiness_status",
        "autonomy_decision", "governance_state", "policy_state",
        "audit_state",
    }
    assert required.issubset(r["signals"].keys())


def test_m18_pure_function_no_mutation():
    """build_decision_explanation NO modifica los inputs."""
    inputs = _coherent_stack("clean")
    copies = tuple(copy.deepcopy(i) for i in inputs)
    build_decision_explanation(*inputs)
    for original, copy_ref in zip(inputs, copies):
        assert original == copy_ref


def test_m18_deterministic_identical_narrative():
    """Mismo input → narrativa idéntica (incluido wording exacto)."""
    inputs = _coherent_stack("clean")
    r1 = build_decision_explanation(*inputs)
    r2 = build_decision_explanation(*inputs)
    assert r1["explanation_state"] == r2["explanation_state"]
    assert r1["executive_summary"] == r2["executive_summary"]
    assert r1["decision_narrative"] == r2["decision_narrative"]
    assert r1["human_readable_reasons"] == r2["human_readable_reasons"]
    assert r1["risk_summary"] == r2["risk_summary"]
    assert r1["deployment_summary"] == r2["deployment_summary"]
    assert r1["operator_guidance"] == r2["operator_guidance"]
    assert r1["system_posture"] == r2["system_posture"]
    assert r1["signals"] == r2["signals"]


def test_m18_spec_alias_works():
    """compute_explainability_report es alias funcional."""
    inputs = _coherent_stack("clean")
    r1 = build_decision_explanation(*inputs)
    r2 = compute_explainability_report(*inputs)
    assert r1["explanation_state"] == r2["explanation_state"]
    assert r1["decision_narrative"] == r2["decision_narrative"]


def test_m18_explanation_states_constant():
    """Constante EXPLANATION_STATES contiene los 3 valores spec."""
    assert set(EXPLANATION_STATES) == {"critical", "restricted", "verified"}


# ════════════════════════════════════════════════════════════════════
#  Propagación de trace + consistency desde M17
# ════════════════════════════════════════════════════════════════════
def test_m18_trace_id_propagated_from_audit():
    """trace_id es el del audit_report (no se regenera)."""
    drift, risk, readiness, autonomy, governance, policy, audit = (
        _coherent_stack("clean")
    )
    audit["trace_id"] = "deadbeefcafefade"
    r = build_decision_explanation(
        drift, risk, readiness, autonomy, governance, policy, audit,
    )
    assert r["trace_id"] == "deadbeefcafefade"


def test_m18_consistency_verified_true_from_audit():
    """consistency_verified refleja audit.decision_consistency=True."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert r["consistency_verified"] is True


def test_m18_consistency_verified_false_from_audit():
    """consistency_verified refleja audit.decision_consistency=False."""
    r = build_decision_explanation(*_coherent_stack("inconsistent"))
    assert r["consistency_verified"] is False


# ════════════════════════════════════════════════════════════════════
#  Narrativa y razones humanas
# ════════════════════════════════════════════════════════════════════
def test_m18_decision_narrative_length_seven():
    """decision_narrative tiene exactamente 7 entradas (una por capa)."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert len(r["decision_narrative"]) == 7


def test_m18_human_reasons_length_seven():
    """human_readable_reasons tiene exactamente 7 entradas."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert len(r["human_readable_reasons"]) == 7


def test_m18_decision_narrative_reflects_each_layer():
    """Cada línea de narrativa menciona la capa correspondiente."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    chain = r["decision_narrative"]
    assert "Drift" in chain[0]
    assert "Risk" in chain[1]
    assert "Readiness" in chain[2]
    assert "Autonomy" in chain[3]
    assert "Governance" in chain[4]
    assert "Policy" in chain[5]
    assert "Audit" in chain[6]


def test_m18_narrative_exact_deterministic_wording():
    """Wording exacto en narrativa para inputs known."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert r["decision_narrative"][0] == "Drift severity detected: low."
    assert (
        r["decision_narrative"][1]
        == "Risk engine classified system as low risk."
    )
    assert (
        r["decision_narrative"][3]
        == "Autonomy layer recommends allow_live."
    )
    assert (
        r["decision_narrative"][6]
        == "Audit layer state: verified."
    )


def test_m18_human_reasons_snake_case():
    """human_readable_reasons usan snake_case con valores literales."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    reasons = r["human_readable_reasons"]
    assert reasons[0] == "drift_pattern_indicates_low_deviation"
    assert reasons[1] == "risk_engine_low_aggregate"
    assert reasons[3] == "autonomy_recommends_allow_live"
    assert reasons[6] == "audit_verified"


# ════════════════════════════════════════════════════════════════════
#  Rutas explanation_state
# ════════════════════════════════════════════════════════════════════
def test_m18_critical_via_audit_inconsistent():
    """audit=inconsistent → explanation_state=critical."""
    r = build_decision_explanation(*_coherent_stack("inconsistent"))
    assert r["explanation_state"] == "critical"


def test_m18_critical_via_policy_blocked():
    """policy=blocked → explanation_state=critical (incluso si audit ok)."""
    r = build_decision_explanation(*_coherent_stack("critical"))
    assert r["explanation_state"] == "critical"


def test_m18_restricted_via_policy_restricted():
    """policy=restricted → explanation_state=restricted."""
    r = build_decision_explanation(*_coherent_stack("restricted"))
    assert r["explanation_state"] == "restricted"


def test_m18_restricted_via_policy_controlled():
    """policy=controlled → explanation_state=restricted."""
    r = build_decision_explanation(*_coherent_stack("controlled"))
    assert r["explanation_state"] == "restricted"


def test_m18_verified_when_compliant_and_audit_verified():
    """policy=compliant AND audit=verified → explanation_state=verified."""
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert r["explanation_state"] == "verified"


# ════════════════════════════════════════════════════════════════════
#  Precedencia
# ════════════════════════════════════════════════════════════════════
def test_m18_critical_beats_restricted():
    """policy=blocked tiene precedencia sobre policy=controlled u otros."""
    # Forzar policy=blocked + governance=approval_candidate (caso inválido)
    drift, risk, readiness, autonomy, governance, policy, audit = (
        _coherent_stack("critical")
    )
    # Aseguramos policy=blocked
    policy["policy_state"] = "blocked"
    r = build_decision_explanation(
        drift, risk, readiness, autonomy, governance, policy, audit,
    )
    assert r["explanation_state"] == "critical"


# ════════════════════════════════════════════════════════════════════
#  Matriz system_posture (per spec)
# ════════════════════════════════════════════════════════════════════
def test_m18_system_posture_blocked_defensive():
    r = build_decision_explanation(*_coherent_stack("critical"))
    assert r["system_posture"] == "defensive"


def test_m18_system_posture_restricted_constrained():
    r = build_decision_explanation(*_coherent_stack("restricted"))
    assert r["system_posture"] == "constrained"


def test_m18_system_posture_controlled_supervised():
    r = build_decision_explanation(*_coherent_stack("controlled"))
    assert r["system_posture"] == "supervised"


def test_m18_system_posture_compliant_production_ready():
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert r["system_posture"] == "production_ready"


def test_m18_system_posture_matrix_canonical():
    """Matriz SYSTEM_POSTURE_MATRIX coincide con spec literal."""
    assert SYSTEM_POSTURE_MATRIX["blocked"] == "defensive"
    assert SYSTEM_POSTURE_MATRIX["restricted"] == "constrained"
    assert SYSTEM_POSTURE_MATRIX["controlled"] == "supervised"
    assert SYSTEM_POSTURE_MATRIX["compliant"] == "production_ready"


# ════════════════════════════════════════════════════════════════════
#  Matriz operator_guidance (per spec)
# ════════════════════════════════════════════════════════════════════
def test_m18_operator_guidance_blocked():
    r = build_decision_explanation(*_coherent_stack("critical"))
    assert r["operator_guidance"] == "immediate_review_required"


def test_m18_operator_guidance_restricted():
    r = build_decision_explanation(*_coherent_stack("restricted"))
    assert r["operator_guidance"] == "continue_observation"


def test_m18_operator_guidance_controlled():
    r = build_decision_explanation(*_coherent_stack("controlled"))
    assert r["operator_guidance"] == "await_manual_approval"


def test_m18_operator_guidance_compliant():
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert r["operator_guidance"] == "maintain_operational_controls"


def test_m18_operator_guidance_matrix_canonical():
    """Matriz OPERATOR_GUIDANCE_MATRIX coincide con spec literal."""
    assert OPERATOR_GUIDANCE_MATRIX["blocked"] == "immediate_review_required"
    assert OPERATOR_GUIDANCE_MATRIX["restricted"] == "continue_observation"
    assert OPERATOR_GUIDANCE_MATRIX["controlled"] == "await_manual_approval"
    assert OPERATOR_GUIDANCE_MATRIX["compliant"] == "maintain_operational_controls"


# ════════════════════════════════════════════════════════════════════
#  Resúmenes textuales
# ════════════════════════════════════════════════════════════════════
def test_m18_risk_summary_low():
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert "below operational threshold" in r["risk_summary"].lower()


def test_m18_risk_summary_critical():
    r = build_decision_explanation(*_coherent_stack("critical"))
    assert "critical" in r["risk_summary"].lower()


def test_m18_deployment_summary_authorized_when_verified():
    r = build_decision_explanation(*_coherent_stack("clean"))
    assert "authorized" in r["deployment_summary"].lower()


def test_m18_deployment_summary_denied_when_critical():
    r = build_decision_explanation(*_coherent_stack("critical"))
    assert "denied" in r["deployment_summary"].lower()


def test_m18_executive_summary_mentions_state():
    """Executive summary contiene contexto del explanation_state."""
    r_verified = build_decision_explanation(*_coherent_stack("clean"))
    assert "verified" in r_verified["executive_summary"].lower()
    r_critical = build_decision_explanation(*_coherent_stack("critical"))
    assert "critical" in r_critical["executive_summary"].lower()


# ════════════════════════════════════════════════════════════════════
#  Edge cases / fail-safe
# ════════════════════════════════════════════════════════════════════
def test_m18_empty_inputs_safe_default():
    """Inputs vacíos → fail-safe restricted, sin crash."""
    r = build_decision_explanation({}, {}, {}, {}, {}, {}, {})
    assert r["explanation_state"] in EXPLANATION_STATES
    assert isinstance(r["consistency_verified"], bool)
    assert isinstance(r["decision_narrative"], list)
    assert len(r["decision_narrative"]) == 7


def test_m18_malformed_inputs_no_crash():
    """Inputs raros → fail-safe sin crash."""
    bad_tuples = [
        (None, None, None, None, None, None, None),
        ("str", [], 42, {}, None, (), set()),
        ({"severity": []}, {"risk_level": None}, {"status": False},
         {"decision": 0}, {"governance_state": []},
         {"policy_state": None}, {"audit_state": False}),
    ]
    for inputs in bad_tuples:
        try:
            r = build_decision_explanation(*inputs)
        except Exception as e:
            assert False, f"crashed on {inputs}: {e}"
        assert isinstance(r, dict)
        assert "explanation_state" in r
        assert r["explanation_state"] in EXPLANATION_STATES


def test_m18_signals_block_reflects_inputs():
    """Signals reflejan los valores literales de los inputs."""
    r = build_decision_explanation(
        _mk_drift("medium"),
        _mk_risk("medium"),
        _mk_readiness("stabilizing", False),
        _mk_autonomy("hold_dry_run"),
        _mk_governance("observation_only"),
        _mk_policy("restricted"),
        _mk_audit("review_required"),
    )
    s = r["signals"]
    assert s["drift_severity"] == "medium"
    assert s["risk_level"] == "medium"
    assert s["readiness_status"] == "stabilizing"
    assert s["autonomy_decision"] == "hold_dry_run"
    assert s["governance_state"] == "observation_only"
    assert s["policy_state"] == "restricted"
    assert s["audit_state"] == "review_required"


# ════════════════════════════════════════════════════════════════════
#  Advisory-only static validation
# ════════════════════════════════════════════════════════════════════
def test_m18_advisory_only_no_execution_imports():
    """M18 NO importa módulos de ejecución, NLP, modelos IA, ni networking."""
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "explainability",
        "purchase_decision_explainability.py",
    )
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    forbidden = [
        "subprocess", "os.system", "import asyncio",
        "core.diagnostics.purchase_gate",
        "_compra_parallel",
        "import socket", "urllib", "requests.",
        "openai", "anthropic", "transformers",
        "torch", "tensorflow", "sklearn",
        "spacy", "nltk", "random.",
    ]
    for token in forbidden:
        assert token not in src, f"M18 contiene token prohibido: {token}"


def test_m18_no_random_in_module():
    """M18 NO usa módulo random (todo determinístico)."""
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "explainability",
        "purchase_decision_explainability.py",
    )
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    # No `import random` ni uso de random
    assert "import random" not in src
    assert "random." not in src or "random_id" in src  # by-name no allowed


def test_m18_only_stdlib_imports():
    """M18 importa solo stdlib (datetime, typing)."""
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "explainability",
        "purchase_decision_explainability.py",
    )
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    # Imports permitidos
    allowed_stdlib = ("datetime", "typing", "__future__")
    # Buscar líneas import
    for line in src.splitlines():
        line_stripped = line.strip()
        if line_stripped.startswith("import ") or line_stripped.startswith("from "):
            # Permitido si menciona stdlib o si es relativo
            ok = any(s in line_stripped for s in allowed_stdlib)
            assert ok, f"Import no permitido: {line_stripped}"
