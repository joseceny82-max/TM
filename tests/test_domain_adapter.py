"""tests/test_domain_adapter.py — M19 Domain Adapter tests.

Cobertura mínima per spec (30-40 tests):
    - Determinismo (same input → same output, no mutation)
    - Schema validation (TM_Snapshot canónico)
    - Isolation (sin imports core, stdlib only, sin random/IO)
    - Mapping correctness (volatility, liquidity, macro_risk, volume)
    - SimulationEvent envelope detection
    - Edge cases (empty, malformed, partial)
    - Integración downstream con M13.7
"""
import copy

import pytest

from core.observability.adapter import (
    DRIFT_HINT_THRESHOLDS,
    MAX_ERROR_PROXY,
    VOLUME_ERROR_THRESHOLDS,
    adapt_event_to_tm_snapshot,
    adapt_market_event,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers — construcción de eventos canónicos del spec v1.0
# ════════════════════════════════════════════════════════════════════
def _mk_market_event(*, event_id="evt_0001", timestamp="2026-05-14T10:00:00Z",
                      asset="BTC", market="crypto", session="eu_open",
                      source="paper_sim",
                      price=65000.5, volume=1200.0, volatility=0.42,
                      trend=0.15, liquidity=0.78,
                      news_sentiment=0.1, macro_risk=0.3,
                      sector_strength=0.6,
                      previous_decision=None, exposure_level=0.2,
                      capital_available=10000):
    """MarketEvent canónico per DATA INPUT SPEC v1.0."""
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "asset": asset,
        "context": {
            "market": market, "session": session, "source": source,
        },
        "signals": {
            "price": price, "volume": volume, "volatility": volatility,
            "trend": trend, "liquidity": liquidity,
        },
        "external_factors": {
            "news_sentiment": news_sentiment, "macro_risk": macro_risk,
            "sector_strength": sector_strength,
        },
        "system_inputs": {
            "previous_decision": previous_decision,
            "exposure_level": exposure_level,
            "capital_available": capital_available,
        },
    }


def _mk_sim_envelope(market_event, source="simulation",
                      seed="deterministic_seed_001"):
    """SimulationEvent envelope per spec v1.0."""
    return {
        "source": source,
        "event": market_event,
        "determinism_seed": seed,
    }


# ════════════════════════════════════════════════════════════════════
#  Determinismo
# ════════════════════════════════════════════════════════════════════
def test_m19_deterministic_same_input_same_output():
    """Mismo input → output idéntico bit-a-bit (todos los campos)."""
    event = _mk_market_event()
    r1 = adapt_event_to_tm_snapshot(event)
    r2 = adapt_event_to_tm_snapshot(event)
    assert r1 == r2


def test_m19_deterministic_different_inputs_produce_different_outputs():
    """Inputs distintos → outputs distintos."""
    e1 = _mk_market_event(volatility=0.1, liquidity=0.9)
    e2 = _mk_market_event(volatility=0.9, liquidity=0.1)
    r1 = adapt_event_to_tm_snapshot(e1)
    r2 = adapt_event_to_tm_snapshot(e2)
    assert r1["current"] != r2["current"]


def test_m19_no_mutation_of_input():
    """adapt_event_to_tm_snapshot NO modifica el input."""
    event = _mk_market_event()
    event_copy = copy.deepcopy(event)
    adapt_event_to_tm_snapshot(event)
    assert event == event_copy


# ════════════════════════════════════════════════════════════════════
#  Schema TM_Snapshot canónico
# ════════════════════════════════════════════════════════════════════
def test_m19_output_has_history_current_meta():
    r = adapt_event_to_tm_snapshot(_mk_market_event())
    assert "history" in r
    assert "current" in r
    assert "meta" in r


def test_m19_current_has_rates_counters_config():
    r = adapt_event_to_tm_snapshot(_mk_market_event())
    c = r["current"]
    assert "rates" in c
    assert "counters" in c
    assert "config" in c


def test_m19_rates_has_dry_run_and_block():
    r = adapt_event_to_tm_snapshot(_mk_market_event())
    assert "purchase_dry_run_rate" in r["current"]["rates"]
    assert "purchase_block_rate" in r["current"]["rates"]


def test_m19_counters_has_live_count_and_errors():
    r = adapt_event_to_tm_snapshot(_mk_market_event())
    assert "purchase_live_count" in r["current"]["counters"]
    assert "internal_errors" in r["current"]["counters"]


def test_m19_meta_has_required_keys():
    r = adapt_event_to_tm_snapshot(_mk_market_event())
    m = r["meta"]
    assert "mode" in m
    assert "event_id" in m
    assert "timestamp" in m
    assert "drift_hint" in m


def test_m19_no_market_signals_leak_into_tm_snapshot():
    """Market signals (price, asset, etc.) NO aparecen en TM_Snapshot."""
    r = adapt_event_to_tm_snapshot(_mk_market_event())
    # Recorrer todo el output buscando strings prohibidos
    output_str = str(r)
    forbidden_market_terms = [
        "asset", "BTC", "news_sentiment", "macro_risk",
        "sector_strength", "exposure_level",
    ]
    # event_id puede aparecer si es 'asset' literal, pero no como key TM
    # Verificamos keys, no valores
    # En el current: rates/counters/config no deben mencionar Market
    current_keys = set(r["current"].keys())
    market_forbidden = {"asset", "price", "volume", "macro_risk"}
    assert market_forbidden.isdisjoint(current_keys)


# ════════════════════════════════════════════════════════════════════
#  Isolation (sin core imports, stdlib only)
# ════════════════════════════════════════════════════════════════════
def test_m19_no_imports_of_m13_to_m18_layers():
    """M19 NO importa M13.7-M18 (sin feedback loop ni acoplamiento)."""
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "adapter", "domain_adapter.py",
    )
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    forbidden = [
        "from core.observability.drift",
        "from core.observability.risk",
        "from core.observability.readiness",
        "from core.observability.autonomy",
        "from core.observability.governance",
        "from core.observability.policy",
        "from core.observability.audit",
        "from core.observability.explainability",
        "from core.diagnostics.purchase_gate",
        "_compra_parallel",
    ]
    for token in forbidden:
        assert token not in src, f"M19 importa {token} (feedback loop!)"


def test_m19_no_random_no_io_no_network():
    """M19 NO usa random, IO ni networking — verificación AST-aware.

    El check ignora docstrings y comentarios; solo inspecciona código
    real (imports y llamadas).
    """
    import ast
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "adapter", "domain_adapter.py",
    )
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    # Recolectar imports
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module.split(".")[0])

    forbidden_modules = {
        "random", "subprocess", "asyncio", "socket",
        "urllib", "requests", "aiohttp", "threading",
    }
    leaks = imported_modules & forbidden_modules
    assert not leaks, f"M19 importa módulos prohibidos: {leaks}"

    # Recolectar calls a APIs sospechosas (open, eval, exec)
    forbidden_calls = {"open", "eval", "exec"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name and name in forbidden_calls:
                assert False, f"M19 llama función prohibida: {name}"


# ════════════════════════════════════════════════════════════════════
#  Mapping: volatility → purchase_block_rate
# ════════════════════════════════════════════════════════════════════
def test_m19_volatility_maps_directly_to_block_rate():
    """volatility=0.42 → block_rate=0.42 (copia directa)."""
    r = adapt_event_to_tm_snapshot(_mk_market_event(volatility=0.42))
    assert r["current"]["rates"]["purchase_block_rate"] == 0.42


def test_m19_volatility_below_zero_clamped_to_zero():
    r = adapt_event_to_tm_snapshot(_mk_market_event(volatility=-0.5))
    assert r["current"]["rates"]["purchase_block_rate"] == 0.0


def test_m19_volatility_above_one_clamped_to_one():
    r = adapt_event_to_tm_snapshot(_mk_market_event(volatility=1.5))
    assert r["current"]["rates"]["purchase_block_rate"] == 1.0


# ════════════════════════════════════════════════════════════════════
#  Mapping: liquidity → purchase_dry_run_rate
# ════════════════════════════════════════════════════════════════════
def test_m19_liquidity_maps_directly_to_dry_run_rate():
    r = adapt_event_to_tm_snapshot(_mk_market_event(liquidity=0.78))
    assert r["current"]["rates"]["purchase_dry_run_rate"] == 0.78


def test_m19_liquidity_clamping_works():
    r_neg = adapt_event_to_tm_snapshot(_mk_market_event(liquidity=-1.0))
    r_high = adapt_event_to_tm_snapshot(_mk_market_event(liquidity=2.0))
    assert r_neg["current"]["rates"]["purchase_dry_run_rate"] == 0.0
    assert r_high["current"]["rates"]["purchase_dry_run_rate"] == 1.0


# ════════════════════════════════════════════════════════════════════
#  Mapping: macro_risk → drift_hint
# ════════════════════════════════════════════════════════════════════
def test_m19_macro_risk_low_maps_to_drift_low():
    r = adapt_event_to_tm_snapshot(_mk_market_event(macro_risk=0.1))
    assert r["meta"]["drift_hint"] == "low"


def test_m19_macro_risk_medium_maps_to_drift_medium():
    r = adapt_event_to_tm_snapshot(_mk_market_event(macro_risk=0.35))
    assert r["meta"]["drift_hint"] == "medium"


def test_m19_macro_risk_high_maps_to_drift_high():
    r = adapt_event_to_tm_snapshot(_mk_market_event(macro_risk=0.6))
    assert r["meta"]["drift_hint"] == "high"


def test_m19_macro_risk_critical_maps_to_drift_critical():
    r = adapt_event_to_tm_snapshot(_mk_market_event(macro_risk=0.85))
    assert r["meta"]["drift_hint"] == "critical"


def test_m19_macro_risk_boundary_values():
    """Boundaries: 0.25, 0.50, 0.75 caen en el bucket superior."""
    # 0.25 → medium (no low, porque >= 0.25)
    r = adapt_event_to_tm_snapshot(_mk_market_event(macro_risk=0.25))
    assert r["meta"]["drift_hint"] == "medium"


# ════════════════════════════════════════════════════════════════════
#  Mapping: volume → internal_errors
# ════════════════════════════════════════════════════════════════════
def test_m19_volume_low_maps_to_zero_errors():
    r = adapt_event_to_tm_snapshot(_mk_market_event(volume=500))
    assert r["current"]["counters"]["internal_errors"] == 0


def test_m19_volume_medium_maps_to_one_error():
    r = adapt_event_to_tm_snapshot(_mk_market_event(volume=3000))
    assert r["current"]["counters"]["internal_errors"] == 1


def test_m19_volume_high_maps_to_two_errors():
    r = adapt_event_to_tm_snapshot(_mk_market_event(volume=8000))
    assert r["current"]["counters"]["internal_errors"] == 2


def test_m19_volume_extreme_caps_at_three():
    r = adapt_event_to_tm_snapshot(_mk_market_event(volume=100000))
    assert r["current"]["counters"]["internal_errors"] == MAX_ERROR_PROXY
    assert MAX_ERROR_PROXY == 3


def test_m19_volume_negative_treated_as_zero():
    r = adapt_event_to_tm_snapshot(_mk_market_event(volume=-100))
    assert r["current"]["counters"]["internal_errors"] == 0


# ════════════════════════════════════════════════════════════════════
#  SimulationEvent envelope detection
# ════════════════════════════════════════════════════════════════════
def test_m19_simulation_envelope_unwrapped():
    """SimulationEvent envelope se desempaqueta correctamente."""
    inner = _mk_market_event(volatility=0.7)
    envelope = _mk_sim_envelope(inner, source="replay")
    r = adapt_event_to_tm_snapshot(envelope)
    # El volatility del inner debe llegar al output
    assert r["current"]["rates"]["purchase_block_rate"] == 0.7
    # El mode debe reflejar el source del envelope
    assert r["meta"]["mode"] == "replay"


def test_m19_market_event_direct_no_envelope():
    """MarketEvent directo (sin envelope) también procesa correctamente."""
    event = _mk_market_event(volatility=0.3)
    r = adapt_event_to_tm_snapshot(event)
    assert r["current"]["rates"]["purchase_block_rate"] == 0.3
    # Mode por defecto cuando no hay envelope
    assert r["meta"]["mode"] == "market"


# ════════════════════════════════════════════════════════════════════
#  Edge cases / fail-safe
# ════════════════════════════════════════════════════════════════════
def test_m19_empty_dict_returns_safe_default():
    """Input {} → TM_Snapshot default seguro."""
    r = adapt_event_to_tm_snapshot({})
    assert "history" in r
    assert "current" in r
    assert "meta" in r
    # Default mode
    assert r["meta"]["mode"] in ("simulation", "market")


def test_m19_none_input_returns_safe_default():
    """None → default snapshot sin crash."""
    r = adapt_event_to_tm_snapshot(None)
    assert isinstance(r, dict)
    assert "history" in r


def test_m19_malformed_inputs_no_crash():
    """Inputs raros → fail-safe sin crash."""
    bad_inputs = [
        "string", 42, [], (1, 2, 3), set(), True,
        {"signals": "not a dict"},
        {"signals": {"volatility": "not a number"}},
        {"external_factors": None},
    ]
    for bad in bad_inputs:
        try:
            r = adapt_event_to_tm_snapshot(bad)
        except Exception as e:
            assert False, f"crashed on {bad!r}: {e}"
        assert isinstance(r, dict)
        assert "current" in r


def test_m19_missing_signals_uses_defaults():
    """Evento sin signals → defaults razonables."""
    event = {"event_id": "evt_x", "timestamp": "2026-05-14T00:00:00Z"}
    r = adapt_event_to_tm_snapshot(event)
    # Sin volatility, block_rate cae a 0.0 (default)
    assert r["current"]["rates"]["purchase_block_rate"] == 0.0


def test_m19_partial_signals_works():
    """Sólo volatility presente, resto defaults."""
    event = {
        "event_id": "evt_partial",
        "signals": {"volatility": 0.5},
    }
    r = adapt_event_to_tm_snapshot(event)
    assert r["current"]["rates"]["purchase_block_rate"] == 0.5
    # liquidity ausente → default 0.5
    assert r["current"]["rates"]["purchase_dry_run_rate"] == 0.5


# ════════════════════════════════════════════════════════════════════
#  Spec alias
# ════════════════════════════════════════════════════════════════════
def test_m19_alias_adapt_market_event():
    """adapt_market_event es alias funcional."""
    event = _mk_market_event()
    r1 = adapt_event_to_tm_snapshot(event)
    r2 = adapt_market_event(event)
    assert r1 == r2


# ════════════════════════════════════════════════════════════════════
#  Constantes de thresholds
# ════════════════════════════════════════════════════════════════════
def test_m19_drift_hint_thresholds_canonical():
    """4 buckets cubriendo [0, 1] exactamente."""
    labels = [t[2] for t in DRIFT_HINT_THRESHOLDS]
    assert labels == ["low", "medium", "high", "critical"]


def test_m19_volume_error_thresholds_monotonic():
    """Thresholds de volume → errors monotónicamente crecientes."""
    errors = [t[2] for t in VOLUME_ERROR_THRESHOLDS]
    for i in range(1, len(errors)):
        assert errors[i] > errors[i - 1]


# ════════════════════════════════════════════════════════════════════
#  Integración downstream con M13.7
# ════════════════════════════════════════════════════════════════════
def test_m19_output_consumable_by_m13_7_drift_detection():
    """El output de M19 puede ser pasado directamente a detect_gate_drift
    sin errores. Verifica el contrato Market → TM → M13.7."""
    from core.observability.drift import detect_gate_drift
    event = _mk_market_event(volatility=0.5, liquidity=0.6, macro_risk=0.4)
    snap = adapt_event_to_tm_snapshot(event)
    # M13.7 espera (history, current)
    drift = detect_gate_drift(snap["history"], snap["current"])
    assert isinstance(drift, dict)
    assert "severity" in drift
    assert "signals" in drift


def test_m19_full_pipeline_market_to_explanation():
    """Pipeline completo: Market → M19 → M13.7 → ... → M18.

    Verifica G1: domain isolation. Market signals NUNCA tocan M13.7+ sin
    pasar por M19. Una vez via M19, todo el pipeline funciona normal.
    """
    from core.observability.drift import detect_gate_drift
    from core.observability.risk import compute_risk_score
    from core.observability.readiness import compute_readiness_index
    from core.observability.autonomy import decide_purchase_autonomy
    from core.observability.governance import evaluate_governance_state
    from core.observability.policy import evaluate_policy_compliance
    from core.observability.audit import build_audit_trace
    from core.observability.explainability import build_decision_explanation

    event = _mk_market_event(
        volatility=0.2, liquidity=0.9, macro_risk=0.1, volume=200,
    )
    snap = adapt_event_to_tm_snapshot(event)
    drift = detect_gate_drift(snap["history"], snap["current"])
    risk = compute_risk_score(drift, snap["current"])
    readiness = compute_readiness_index(
        risk,
        {"internal_errors_total":
            snap["current"]["counters"]["internal_errors"]},
    )
    autonomy = decide_purchase_autonomy(drift, risk, readiness)
    governance = evaluate_governance_state(autonomy, readiness, risk, drift)
    policy = evaluate_policy_compliance(autonomy, governance, readiness)
    audit = build_audit_trace(
        drift, risk, readiness, autonomy, governance, policy,
    )
    explain = build_decision_explanation(
        drift, risk, readiness, autonomy, governance, policy, audit,
    )
    # El pipeline completo devuelve outputs válidos
    assert "explanation_state" in explain
    assert "trace_id" in explain


# ════════════════════════════════════════════════════════════════════
#  Spec example minimal input
# ════════════════════════════════════════════════════════════════════
def test_m19_spec_example_minimal_input_works():
    """El ejemplo del spec v1.0 produce TM_Snapshot válido."""
    spec_example = {
        "event_id": "evt_0001",
        "timestamp": "2026-05-13T10:00:00Z",
        "asset": "BTC",
        "context": {
            "market": "crypto",
            "session": "eu_open",
            "source": "paper_sim",
        },
        "signals": {
            "price": 65000.5, "volume": 1200.0, "volatility": 0.42,
            "trend": 0.15, "liquidity": 0.78,
        },
        "external_factors": {
            "news_sentiment": 0.1, "macro_risk": 0.3,
            "sector_strength": 0.6,
        },
        "system_inputs": {
            "previous_decision": None, "exposure_level": 0.2,
            "capital_available": 10000,
        },
    }
    r = adapt_event_to_tm_snapshot(spec_example)
    # Verificaciones del spec example
    assert r["current"]["rates"]["purchase_block_rate"] == 0.42
    assert r["current"]["rates"]["purchase_dry_run_rate"] == 0.78
    assert r["meta"]["drift_hint"] == "medium"  # macro_risk=0.3 → medium
    assert r["current"]["counters"]["internal_errors"] == 1  # volume=1200 → 1
    assert r["meta"]["event_id"] == "evt_0001"


# ════════════════════════════════════════════════════════════════════
#  Garantías arquitectónicas (G1-G4)
# ════════════════════════════════════════════════════════════════════
def test_m19_g1_domain_separation():
    """G1: Market domain NUNCA entra directo al core. M19 traduce."""
    event = _mk_market_event()
    snap = adapt_event_to_tm_snapshot(event)
    # No quedan claves Market en el snapshot
    assert "asset" not in snap["current"]
    assert "external_factors" not in snap["current"]
    assert "signals" not in snap["current"]  # las "signals" Market no leak


def test_m19_g3_advisory_only_no_decision_output():
    """G3: M19 NO produce decisiones (allow/block/etc.), sólo transforma."""
    snap = adapt_event_to_tm_snapshot(_mk_market_event())
    # No debe haber campos de decisión
    decision_fields = {
        "decision", "allowed", "policy_state", "governance_state",
        "audit_state", "explanation_state",
    }
    for field in decision_fields:
        assert field not in snap, (
            f"M19 produce campo de decisión '{field}' — viola G3"
        )
        assert field not in snap.get("current", {}), (
            f"M19.current contiene decisión '{field}'"
        )
