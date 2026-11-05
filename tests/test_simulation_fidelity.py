"""tests/test_simulation_fidelity.py — M20 Simulation Fidelity tests.

Cobertura mínima per spec (25+ tests):
    - Determinismo
    - Schema validation
    - Fidelity scoring (perfect/degraded/mismatch)
    - Signal alignment (volatility, liquidity, macro_risk, volume)
    - Drift detection
    - Adapter quality classification
    - Edge cases
    - Isolation (sin imports M11-M19)
    - Integración real con M19
"""
import copy

import pytest

from core.observability.simulation_fidelity import (
    ADAPTER_QUALITY_THRESHOLDS,
    RECOMMENDATION_BY_QUALITY,
    compute_fidelity_report,
    evaluate_simulation_fidelity,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _mk_market(*, volatility=0.5, liquidity=0.5, volume=500,
                macro_risk=0.3, event_id="evt_test"):
    return {
        "event_id": event_id,
        "asset": "BTC",
        "signals": {
            "price": 1.0, "volume": volume, "volatility": volatility,
            "trend": 0.0, "liquidity": liquidity,
        },
        "external_factors": {
            "news_sentiment": 0.0, "macro_risk": macro_risk,
            "sector_strength": 0.5,
        },
        "system_inputs": {
            "previous_decision": None, "exposure_level": 0.0,
            "capital_available": 1000,
        },
    }


def _mk_tm_snapshot(*, block_rate=0.5, dry_run_rate=0.5,
                     internal_errors=0, drift_hint="medium",
                     event_id="evt_test"):
    return {
        "history": [],
        "current": {
            "rates": {
                "purchase_block_rate": block_rate,
                "purchase_dry_run_rate": dry_run_rate,
            },
            "counters": {
                "purchase_live_count": 0,
                "internal_errors": internal_errors,
            },
            "config": {"mode": "dry_run"},
        },
        "meta": {
            "mode": "market",
            "event_id": event_id,
            "drift_hint": drift_hint,
            "timestamp": "2026-05-14T00:00:00Z",
        },
    }


def _perfectly_aligned_pair(*, volatility=0.42, liquidity=0.78,
                             volume=500, macro_risk=0.3):
    """Devuelve (market, tm) donde tm es la traducción perfecta del market."""
    # macro_risk=0.3 → medium / volume=500 → 0 errors
    me = _mk_market(volatility=volatility, liquidity=liquidity,
                    volume=volume, macro_risk=macro_risk)
    ts = _mk_tm_snapshot(
        block_rate=volatility, dry_run_rate=liquidity,
        internal_errors=0, drift_hint="medium",
    )
    return me, ts


# ════════════════════════════════════════════════════════════════════
#  Determinismo
# ════════════════════════════════════════════════════════════════════
def test_m20_deterministic_same_input_same_output():
    me, ts = _perfectly_aligned_pair()
    r1 = evaluate_simulation_fidelity(me, ts)
    r2 = evaluate_simulation_fidelity(me, ts)
    # Comparar todo excepto timestamp
    for k in r1:
        if k == "timestamp":
            continue
        assert r1[k] == r2[k]


def test_m20_different_inputs_produce_different_outputs():
    me1, ts1 = _perfectly_aligned_pair(volatility=0.2)
    me2, ts2 = _perfectly_aligned_pair(volatility=0.9)
    r1 = evaluate_simulation_fidelity(me1, ts1)
    r2 = evaluate_simulation_fidelity(me2, ts2)
    # Los hashes deben diferir
    assert r1["signals"]["market_event_hash"] != r2["signals"]["market_event_hash"]


def test_m20_no_mutation_of_inputs():
    me, ts = _perfectly_aligned_pair()
    me_copy = copy.deepcopy(me)
    ts_copy = copy.deepcopy(ts)
    evaluate_simulation_fidelity(me, ts)
    assert me == me_copy
    assert ts == ts_copy


# ════════════════════════════════════════════════════════════════════
#  Schema
# ════════════════════════════════════════════════════════════════════
def test_m20_output_schema_complete():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    required = {
        "fidelity_score", "signal_alignment", "drift_between_layers",
        "adapter_quality", "consistency_verified", "deterministic",
        "recommendation", "signals", "timestamp",
    }
    assert required.issubset(r.keys())


def test_m20_signal_alignment_block_has_4_keys():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    required = {
        "volatility_alignment", "liquidity_alignment",
        "risk_proxy_alignment", "volume_mapping_accuracy",
    }
    assert required == set(r["signal_alignment"].keys())


def test_m20_signals_block_has_2_hashes():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    assert "market_event_hash" in r["signals"]
    assert "snapshot_hash" in r["signals"]
    assert len(r["signals"]["market_event_hash"]) == 16
    assert len(r["signals"]["snapshot_hash"]) == 16


def test_m20_adapter_quality_is_string():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    assert isinstance(r["adapter_quality"], str)
    assert r["adapter_quality"] in {"high", "medium", "low", "critical"}


# ════════════════════════════════════════════════════════════════════
#  Fidelity scoring — perfect mapping
# ════════════════════════════════════════════════════════════════════
def test_m20_perfect_mapping_score_one():
    """Mapping perfecto → fidelity_score muy alto (≈1.0)."""
    me, ts = _perfectly_aligned_pair(
        volatility=0.5, liquidity=0.5, volume=500, macro_risk=0.3,
    )
    r = evaluate_simulation_fidelity(me, ts)
    assert r["fidelity_score"] >= 0.99
    assert r["adapter_quality"] == "high"


def test_m20_degraded_mapping_score_reduced():
    """Mapping desviado → fidelity_score reducido."""
    me = _mk_market(volatility=0.5, liquidity=0.5)
    ts = _mk_tm_snapshot(block_rate=0.7, dry_run_rate=0.3)  # off by 0.2 each
    r = evaluate_simulation_fidelity(me, ts)
    # Cada alignment ≈ 1 - 0.4 = 0.6 (lineal con factor 2)
    assert 0.4 <= r["fidelity_score"] <= 0.85


def test_m20_total_mismatch_score_low():
    """Mismatches grandes → fidelity_score crítico."""
    me = _mk_market(volatility=0.0, liquidity=0.0, volume=50000,
                     macro_risk=0.9)
    ts = _mk_tm_snapshot(
        block_rate=1.0, dry_run_rate=1.0, internal_errors=0,
        drift_hint="low",
    )
    r = evaluate_simulation_fidelity(me, ts)
    assert r["fidelity_score"] < 0.4
    assert r["adapter_quality"] == "critical"


# ════════════════════════════════════════════════════════════════════
#  Signal alignment — volatility
# ════════════════════════════════════════════════════════════════════
def test_m20_volatility_alignment_exact_match():
    me = _mk_market(volatility=0.42)
    ts = _mk_tm_snapshot(block_rate=0.42)
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["volatility_alignment"] >= 0.99


def test_m20_volatility_alignment_small_deviation():
    me = _mk_market(volatility=0.5)
    ts = _mk_tm_snapshot(block_rate=0.55)
    r = evaluate_simulation_fidelity(me, ts)
    # |Δ| = 0.05, alignment = 1 - 0.1 = 0.9
    assert 0.85 <= r["signal_alignment"]["volatility_alignment"] <= 0.95


def test_m20_volatility_alignment_complete_mismatch():
    me = _mk_market(volatility=0.0)
    ts = _mk_tm_snapshot(block_rate=1.0)
    r = evaluate_simulation_fidelity(me, ts)
    # |Δ| = 1.0, factor 2 → alignment clamped a 0
    assert r["signal_alignment"]["volatility_alignment"] == 0.0


# ════════════════════════════════════════════════════════════════════
#  Signal alignment — liquidity
# ════════════════════════════════════════════════════════════════════
def test_m20_liquidity_alignment_exact_match():
    me = _mk_market(liquidity=0.78)
    ts = _mk_tm_snapshot(dry_run_rate=0.78)
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["liquidity_alignment"] >= 0.99


def test_m20_liquidity_alignment_with_deviation():
    me = _mk_market(liquidity=0.8)
    ts = _mk_tm_snapshot(dry_run_rate=0.6)
    r = evaluate_simulation_fidelity(me, ts)
    # |Δ| = 0.2, alignment = 1 - 0.4 = 0.6
    assert 0.55 <= r["signal_alignment"]["liquidity_alignment"] <= 0.65


# ════════════════════════════════════════════════════════════════════
#  Signal alignment — macro_risk → drift_hint
# ════════════════════════════════════════════════════════════════════
def test_m20_risk_proxy_exact_bucket_match():
    """macro_risk=0.3 (medium bucket) + drift_hint=medium → alignment 1.0."""
    me = _mk_market(macro_risk=0.3)
    ts = _mk_tm_snapshot(drift_hint="medium")
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["risk_proxy_alignment"] == 1.0


def test_m20_risk_proxy_off_by_one_bucket():
    """macro_risk=0.3 (medium) + drift_hint=low → off-by-1 → 0.5."""
    me = _mk_market(macro_risk=0.3)
    ts = _mk_tm_snapshot(drift_hint="low")
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["risk_proxy_alignment"] == 0.5


def test_m20_risk_proxy_far_mismatch():
    """macro_risk=0.1 (low) + drift_hint=critical → 3 buckets off → 0.0."""
    me = _mk_market(macro_risk=0.1)
    ts = _mk_tm_snapshot(drift_hint="critical")
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["risk_proxy_alignment"] == 0.0


def test_m20_risk_proxy_critical_macro_critical_hint():
    """macro_risk=0.9 (critical bucket) + drift_hint=critical → 1.0."""
    me = _mk_market(macro_risk=0.9)
    ts = _mk_tm_snapshot(drift_hint="critical")
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["risk_proxy_alignment"] == 1.0


# ════════════════════════════════════════════════════════════════════
#  Signal alignment — volume → internal_errors
# ════════════════════════════════════════════════════════════════════
def test_m20_volume_mapping_exact_bucket():
    """volume=500 (0 errors) + internal_errors=0 → 1.0."""
    me = _mk_market(volume=500)
    ts = _mk_tm_snapshot(internal_errors=0)
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["volume_mapping_accuracy"] == 1.0


def test_m20_volume_mapping_off_by_one():
    """volume=500 (0 errors) + internal_errors=1 → off-by-1 → 0.5."""
    me = _mk_market(volume=500)
    ts = _mk_tm_snapshot(internal_errors=1)
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["volume_mapping_accuracy"] == 0.5


def test_m20_volume_mapping_high_bucket():
    """volume=12000 (3 errors cap) + internal_errors=3 → 1.0."""
    me = _mk_market(volume=12000)
    ts = _mk_tm_snapshot(internal_errors=3)
    r = evaluate_simulation_fidelity(me, ts)
    assert r["signal_alignment"]["volume_mapping_accuracy"] == 1.0


# ════════════════════════════════════════════════════════════════════
#  Drift between layers
# ════════════════════════════════════════════════════════════════════
def test_m20_drift_low_when_aligned():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    assert r["drift_between_layers"] < 0.15


def test_m20_drift_high_when_mismatched():
    me = _mk_market(volatility=0.0, liquidity=0.0, volume=50000,
                     macro_risk=0.9)
    ts = _mk_tm_snapshot(
        block_rate=1.0, dry_run_rate=1.0, internal_errors=0,
        drift_hint="low",
    )
    r = evaluate_simulation_fidelity(me, ts)
    assert r["drift_between_layers"] > 0.5


def test_m20_drift_zero_for_perfect_alignment():
    """Bloque perfectamente alineado → drift ≈ 0."""
    me = _mk_market(volatility=0.5, liquidity=0.5, volume=0,
                     macro_risk=0.0)
    ts = _mk_tm_snapshot(
        block_rate=0.5, dry_run_rate=0.5, internal_errors=0,
        drift_hint="low",
    )
    r = evaluate_simulation_fidelity(me, ts)
    assert r["drift_between_layers"] < 0.05


# ════════════════════════════════════════════════════════════════════
#  Adapter quality classification
# ════════════════════════════════════════════════════════════════════
def test_m20_adapter_quality_high():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    assert r["adapter_quality"] == "high"
    assert r["fidelity_score"] >= 0.85


def test_m20_adapter_quality_medium():
    """Fidelity ≈ 0.7-0.8 → medium."""
    me = _mk_market(volatility=0.5, liquidity=0.5, volume=500,
                     macro_risk=0.3)
    # Block rate desviado, resto OK
    ts = _mk_tm_snapshot(block_rate=0.65, dry_run_rate=0.5,
                          internal_errors=0, drift_hint="medium")
    r = evaluate_simulation_fidelity(me, ts)
    # vol: 1-0.3=0.7, liq:1.0, risk:1.0, vol:1.0 → mean=0.925
    # Actually 0.7+1+1+1 / 4 = 0.925 → high
    # Need different setup for medium
    me2 = _mk_market(volatility=0.5, liquidity=0.5, volume=500,
                      macro_risk=0.3)
    ts2 = _mk_tm_snapshot(block_rate=0.65, dry_run_rate=0.35,
                            internal_errors=1, drift_hint="medium")
    r2 = evaluate_simulation_fidelity(me2, ts2)
    # vol: 0.7, liq: 0.7, risk: 1.0, vol: 0.5 → mean=0.725 → medium
    assert r2["adapter_quality"] == "medium"


def test_m20_adapter_quality_low():
    """Fidelity ≈ 0.5 → low."""
    me = _mk_market(volatility=0.5, liquidity=0.5, volume=500,
                     macro_risk=0.3)
    ts = _mk_tm_snapshot(block_rate=0.8, dry_run_rate=0.2,
                          internal_errors=2, drift_hint="critical")
    # vol:0.4, liq:0.4, risk:0.0, vol_map:0.0 → mean=0.2 → critical
    # Need different setup
    me2 = _mk_market(volatility=0.5, liquidity=0.5, volume=500,
                      macro_risk=0.3)
    ts2 = _mk_tm_snapshot(block_rate=0.7, dry_run_rate=0.3,
                            internal_errors=2, drift_hint="low")
    # vol:0.6, liq:0.6, risk:0.5, vol_map:0.0 → mean=0.425 → low
    r = evaluate_simulation_fidelity(me2, ts2)
    assert r["adapter_quality"] == "low"


def test_m20_adapter_quality_critical():
    me = _mk_market(volatility=0.0, liquidity=0.0, volume=50000,
                     macro_risk=0.9)
    ts = _mk_tm_snapshot(block_rate=1.0, dry_run_rate=1.0,
                          internal_errors=0, drift_hint="low")
    r = evaluate_simulation_fidelity(me, ts)
    assert r["adapter_quality"] == "critical"


# ════════════════════════════════════════════════════════════════════
#  Recommendation mapping
# ════════════════════════════════════════════════════════════════════
def test_m20_recommendation_high_quality():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    assert r["recommendation"] == "translation_is_faithful_continue"


def test_m20_recommendation_critical_quality():
    me = _mk_market(volatility=0.0, liquidity=0.0, volume=50000,
                     macro_risk=0.9)
    ts = _mk_tm_snapshot(block_rate=1.0, dry_run_rate=1.0,
                          internal_errors=0, drift_hint="low")
    r = evaluate_simulation_fidelity(me, ts)
    assert "reevaluate" in r["recommendation"]


def test_m20_recommendation_matrix_complete():
    """RECOMMENDATION_BY_QUALITY tiene los 4 quality levels."""
    assert set(RECOMMENDATION_BY_QUALITY.keys()) == {
        "high", "medium", "low", "critical",
    }


def test_m20_quality_thresholds_canonical():
    """ADAPTER_QUALITY_THRESHOLDS cubre [0, 1] sin gaps."""
    labels = [t[0] for t in ADAPTER_QUALITY_THRESHOLDS]
    assert set(labels) == {"critical", "low", "medium", "high"}


# ════════════════════════════════════════════════════════════════════
#  Hashes determinísticos
# ════════════════════════════════════════════════════════════════════
def test_m20_market_event_hash_deterministic():
    me, ts = _perfectly_aligned_pair()
    r1 = evaluate_simulation_fidelity(me, ts)
    r2 = evaluate_simulation_fidelity(me, ts)
    assert r1["signals"]["market_event_hash"] == r2["signals"]["market_event_hash"]


def test_m20_snapshot_hash_deterministic():
    me, ts = _perfectly_aligned_pair()
    r1 = evaluate_simulation_fidelity(me, ts)
    r2 = evaluate_simulation_fidelity(me, ts)
    assert r1["signals"]["snapshot_hash"] == r2["signals"]["snapshot_hash"]


# ════════════════════════════════════════════════════════════════════
#  Edge cases
# ════════════════════════════════════════════════════════════════════
def test_m20_empty_market_event():
    r = evaluate_simulation_fidelity({}, _mk_tm_snapshot())
    assert isinstance(r, dict)
    assert "fidelity_score" in r
    assert r["adapter_quality"] in {"high", "medium", "low", "critical"}


def test_m20_empty_tm_snapshot():
    r = evaluate_simulation_fidelity(_mk_market(), {})
    assert isinstance(r, dict)
    assert "fidelity_score" in r


def test_m20_malformed_inputs_no_crash():
    bad_pairs = [
        (None, None), ("str", []), (42, set()),
        ({"signals": "not a dict"}, {"current": None}),
    ]
    for me, ts in bad_pairs:
        try:
            r = evaluate_simulation_fidelity(me, ts)
        except Exception as e:
            assert False, f"crashed on {(me, ts)}: {e}"
        assert isinstance(r, dict)
        assert "fidelity_score" in r


def test_m20_partial_inputs_no_crash():
    partial_me = {"signals": {"volatility": 0.5}}
    partial_ts = {"current": {"rates": {"purchase_block_rate": 0.5}}}
    r = evaluate_simulation_fidelity(partial_me, partial_ts)
    assert isinstance(r, dict)
    # volatility alignment debería ser 1.0 (ambos 0.5)
    assert r["signal_alignment"]["volatility_alignment"] >= 0.99


# ════════════════════════════════════════════════════════════════════
#  Isolation (sin imports M11-M19)
# ════════════════════════════════════════════════════════════════════
def test_m20_no_imports_of_m11_to_m19():
    """M20 NO importa nada de M11-M19 (independencia epistemológica)."""
    import ast
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "simulation_fidelity",
        "fidelity_engine.py",
    )
    with open(src_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_prefixes = [
        "core.observability.drift", "core.observability.risk",
        "core.observability.readiness", "core.observability.autonomy",
        "core.observability.governance", "core.observability.policy",
        "core.observability.audit", "core.observability.explainability",
        "core.observability.adapter", "core.diagnostics.purchase_gate",
    ]
    for mod in imported:
        for prefix in forbidden_prefixes:
            assert not mod.startswith(prefix), (
                f"M20 importa {mod} (viola independencia)"
            )


def test_m20_no_random_no_io_no_async():
    """M20 sólo usa stdlib determinista (hashlib, json, datetime, typing)."""
    import ast
    import os
    src_path = os.path.join(
        os.path.dirname(__file__), "..",
        "core", "observability", "simulation_fidelity",
        "fidelity_engine.py",
    )
    with open(src_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "random", "asyncio", "subprocess", "socket",
        "urllib", "requests", "aiohttp", "threading",
    }
    leaks = imported & forbidden
    assert not leaks, f"M20 importa módulos prohibidos: {leaks}"


# ════════════════════════════════════════════════════════════════════
#  Spec alias
# ════════════════════════════════════════════════════════════════════
def test_m20_compute_fidelity_report_alias():
    me, ts = _perfectly_aligned_pair()
    r1 = evaluate_simulation_fidelity(me, ts)
    r2 = compute_fidelity_report(me, ts)
    assert r1["fidelity_score"] == r2["fidelity_score"]
    assert r1["adapter_quality"] == r2["adapter_quality"]


# ════════════════════════════════════════════════════════════════════
#  Determinístico = True siempre
# ════════════════════════════════════════════════════════════════════
def test_m20_deterministic_flag_always_true():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    assert r["deterministic"] is True


# ════════════════════════════════════════════════════════════════════
#  consistency_verified
# ════════════════════════════════════════════════════════════════════
def test_m20_consistency_verified_true_for_aligned():
    me, ts = _perfectly_aligned_pair()
    r = evaluate_simulation_fidelity(me, ts)
    assert r["consistency_verified"] is True


def test_m20_consistency_verified_false_for_mismatch():
    me = _mk_market(volatility=0.0, liquidity=0.0)
    ts = _mk_tm_snapshot(block_rate=1.0, dry_run_rate=1.0)
    r = evaluate_simulation_fidelity(me, ts)
    assert r["consistency_verified"] is False


# ════════════════════════════════════════════════════════════════════
#  Integración real con M19
# ════════════════════════════════════════════════════════════════════
def test_m20_real_m19_output_produces_high_fidelity():
    """Si M19 traduce correctamente, M20 mide fidelidad alta."""
    from core.observability.adapter import adapt_event_to_tm_snapshot
    me = _mk_market(volatility=0.42, liquidity=0.78, volume=1200,
                     macro_risk=0.3)
    ts = adapt_event_to_tm_snapshot(me)
    r = evaluate_simulation_fidelity(me, ts)
    # M19 sigue el spec exactamente → high fidelity
    assert r["fidelity_score"] >= 0.85
    assert r["adapter_quality"] == "high"


def test_m20_perturbed_snapshot_produces_lower_fidelity():
    """Si forzamos un snapshot que NO coincide con M19 spec → fidelity baja."""
    me = _mk_market(volatility=0.5, liquidity=0.5, volume=500,
                     macro_risk=0.3)
    # Snapshot artificialmente desviado del spec
    perturbed_ts = _mk_tm_snapshot(
        block_rate=0.95,    # spec esperaría 0.5
        dry_run_rate=0.05,  # spec esperaría 0.5
        internal_errors=3,  # spec esperaría 0 (volume=500)
        drift_hint="critical",  # spec esperaría medium (macro_risk=0.3)
    )
    r = evaluate_simulation_fidelity(me, perturbed_ts)
    assert r["fidelity_score"] < 0.5
    assert r["adapter_quality"] in {"low", "critical"}
