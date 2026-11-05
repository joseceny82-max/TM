"""tests/test_purchase_gate_drift.py — M13.7 Drift Detection Engine tests.

Cobertura:
    - Schema y output completos
    - Pure function (no mutation, deterministic)
    - Baseline computation correcta
    - 5 signals: cada uno trigger / silent
    - Severity precedence
    - Edge cases (empty history, malformed inputs)
"""
import pytest

from core.observability.drift import (
    BLOCK_RATE_SPIKE_FACTOR,
    BLOCK_RATE_SPIKE_MIN_ABSOLUTE,
    DRY_RUN_DEVIATION_THRESHOLD,
    MIN_HISTORY_FOR_DEVIATION,
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    compute_baseline,
    detect_gate_drift,
    detect_purchase_gate_drift,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _mk_snapshot(*, mode="dry_run", enabled=True,
                  attempted=10, dry_run=8, blocked=2, live=0,
                  internal_errors=0,
                  dry_run_rate=0.8, block_rate=0.2, live_rate=0.0):
    return {
        "config": {"mode": mode, "global_enabled": enabled, "thresholds": {}},
        "counters": {
            "purchase_attempted_count": attempted,
            "purchase_allowed_count": dry_run + (live if mode == "live" else 0),
            "purchase_blocked_count": blocked,
            "purchase_dry_run_count": dry_run,
            "purchase_live_count": live,
            "internal_errors": internal_errors,
        },
        "rates": {
            "purchase_allow_rate": (dry_run + live) / max(1, attempted),
            "purchase_block_rate": block_rate,
            "purchase_dry_run_rate": dry_run_rate,
            "purchase_live_rate": live_rate,
        },
        "denial_breakdown": {},
        "cache_state": {"idempotency_size": 0, "session_locks_count": 0},
    }


def _stable_history(n=5, dry_run_rate=0.8, block_rate=0.1):
    """Returns n stable snapshots (dry_run mode, healthy)."""
    return [
        _mk_snapshot(
            attempted=50, dry_run=40, blocked=5,
            dry_run_rate=dry_run_rate, block_rate=block_rate,
        )
        for _ in range(n)
    ]


# ════════════════════════════════════════════════════════════════════
#  Schema / pure function
# ════════════════════════════════════════════════════════════════════
def test_m13_7_output_schema_complete():
    """Output tiene las 5 keys obligatorias."""
    r = detect_gate_drift([], _mk_snapshot())
    required = {"drift_detected", "severity", "signals", "baseline",
                "timestamp"}
    assert required.issubset(r.keys())


def test_m13_7_severity_levels_match_canonical():
    assert SEVERITY_LEVELS == ("low", "medium", "high", "critical")
    assert SEVERITY_ORDER["critical"] > SEVERITY_ORDER["high"]
    assert SEVERITY_ORDER["high"] > SEVERITY_ORDER["medium"]
    assert SEVERITY_ORDER["medium"] > SEVERITY_ORDER["low"]


def test_m13_7_no_mutation_of_inputs():
    """detect_gate_drift NO modifica history ni current."""
    history = _stable_history(5)
    current = _mk_snapshot(dry_run_rate=0.5)
    # Snapshot deep equivalence
    import copy
    history_copy = copy.deepcopy(history)
    current_copy = copy.deepcopy(current)

    detect_gate_drift(history, current)
    assert history == history_copy
    assert current == current_copy


def test_m13_7_deterministic_output_same_inputs():
    """Mismo (history, current) → mismas signals (timestamp excluded)."""
    history = _stable_history(4)
    current = _mk_snapshot(internal_errors=2)
    r1 = detect_gate_drift(history, current)
    r2 = detect_gate_drift(history, current)
    assert r1["drift_detected"] == r2["drift_detected"]
    assert r1["severity"] == r2["severity"]
    # Comparar signals como sets/lists (algunos pueden re-orderarse, pero aquí no)
    assert r1["signals"] == r2["signals"]
    assert r1["baseline"] == r2["baseline"]


def test_m13_7_spec_alias_works():
    """detect_purchase_gate_drift (spec alias) llama a detect_gate_drift."""
    r1 = detect_gate_drift([], _mk_snapshot())
    r2 = detect_purchase_gate_drift([], _mk_snapshot())
    assert r1["drift_detected"] == r2["drift_detected"]
    assert r1["severity"] == r2["severity"]


# ════════════════════════════════════════════════════════════════════
#  Baseline computation
# ════════════════════════════════════════════════════════════════════
def test_m13_7_baseline_empty_history():
    """compute_baseline con [] devuelve defaults."""
    b = compute_baseline([])
    assert b["samples"] == 0
    assert b["dry_run_rate_mean"] is None
    assert b["block_rate_mean"] is None
    assert b["live_count_max"] == 0
    assert b["internal_errors_max"] == 0


def test_m13_7_baseline_valid_history():
    """Baseline con history válida computa medias correctas."""
    history = _stable_history(4, dry_run_rate=0.9, block_rate=0.05)
    b = compute_baseline(history)
    assert b["samples"] == 4
    assert abs(b["dry_run_rate_mean"] - 0.9) < 0.001
    assert abs(b["block_rate_mean"] - 0.05) < 0.001


def test_m13_7_baseline_malformed_history_skipped():
    """Snapshots no-dict skipped, válidos contados."""
    bad_history = [None, "not a dict", [], 42, _mk_snapshot()]
    b = compute_baseline(bad_history)
    assert b["samples"] == 1


def test_m13_7_baseline_max_live_and_errors():
    """live_count_max y internal_errors_max correctos."""
    history = [
        _mk_snapshot(live=0, internal_errors=0),
        _mk_snapshot(live=3, internal_errors=1),
        _mk_snapshot(live=2, internal_errors=5),
    ]
    b = compute_baseline(history)
    assert b["live_count_max"] == 3
    assert b["internal_errors_max"] == 5


# ════════════════════════════════════════════════════════════════════
#  Signal 1: dry_run_rate deviation
# ════════════════════════════════════════════════════════════════════
def test_m13_7_signal_dry_run_deviation_triggers():
    """Stable history 0.9, current 0.4 → deviation 0.5 > 0.20 → drift medium."""
    history = _stable_history(5, dry_run_rate=0.9)
    current = _mk_snapshot(dry_run_rate=0.4)
    r = detect_gate_drift(history, current)
    types = [s["type"] for s in r["signals"]]
    assert "dry_run_rate_deviation" in types
    # Si solo dispara este, severity = medium
    if len(r["signals"]) == 1:
        assert r["severity"] == "medium"


def test_m13_7_signal_dry_run_stable_no_trigger():
    """Stable history + similar current → no deviation triggered."""
    history = _stable_history(5, dry_run_rate=0.9)
    current = _mk_snapshot(dry_run_rate=0.85)  # within 0.20
    r = detect_gate_drift(history, current)
    types = [s["type"] for s in r["signals"]]
    assert "dry_run_rate_deviation" not in types


def test_m13_7_signal_dry_run_needs_minimum_history():
    """Con history < MIN_HISTORY_FOR_DEVIATION, no se dispara la signal."""
    short_history = _stable_history(MIN_HISTORY_FOR_DEVIATION - 1,
                                       dry_run_rate=0.9)
    current = _mk_snapshot(dry_run_rate=0.1)  # huge deviation
    r = detect_gate_drift(short_history, current)
    types = [s["type"] for s in r["signals"]]
    # Sin suficiente baseline, NO dispara dry_run deviation
    assert "dry_run_rate_deviation" not in types


# ════════════════════════════════════════════════════════════════════
#  Signal 2: block_rate spike
# ════════════════════════════════════════════════════════════════════
def test_m13_7_signal_block_rate_spike_triggers():
    """Baseline ~0.1, current 0.6 → spike threshold superado → high."""
    history = _stable_history(5, block_rate=0.1)
    current = _mk_snapshot(block_rate=0.6)
    r = detect_gate_drift(history, current)
    types = [s["type"] for s in r["signals"]]
    assert "block_rate_spike" in types


def test_m13_7_signal_block_rate_normal_no_trigger():
    """Baseline ~0.1, current 0.15 → variance normal, no spike."""
    history = _stable_history(5, block_rate=0.1)
    current = _mk_snapshot(block_rate=0.15)
    r = detect_gate_drift(history, current)
    types = [s["type"] for s in r["signals"]]
    assert "block_rate_spike" not in types


# ════════════════════════════════════════════════════════════════════
#  Signal 3: internal_error anomaly
# ════════════════════════════════════════════════════════════════════
def test_m13_7_signal_internal_error_triggers_critical():
    """internal_errors > 0 y > baseline → critical drift."""
    history = _stable_history(3)  # internal_errors=0 by default
    current = _mk_snapshot(internal_errors=1)
    r = detect_gate_drift(history, current)
    assert r["drift_detected"] is True
    assert r["severity"] == "critical"
    types = [s["type"] for s in r["signals"]]
    assert "internal_error_anomaly" in types


def test_m13_7_signal_internal_error_zero_no_trigger():
    history = _stable_history(3)
    current = _mk_snapshot(internal_errors=0)
    r = detect_gate_drift(history, current)
    types = [s["type"] for s in r["signals"]]
    assert "internal_error_anomaly" not in types


# ════════════════════════════════════════════════════════════════════
#  Signal 4/5: live_count
# ════════════════════════════════════════════════════════════════════
def test_m13_7_signal_live_count_in_dry_run_critical():
    """live > 0 con mode != live → safety violation critical."""
    history = _stable_history(3)
    current = _mk_snapshot(mode="dry_run", live=1)
    r = detect_gate_drift(history, current)
    assert r["drift_detected"] is True
    assert r["severity"] == "critical"
    types = [s["type"] for s in r["signals"]]
    assert "live_count_unexpected_mode" in types


def test_m13_7_signal_live_count_first_emergence_in_live_mode():
    """live > 0 con mode=live tras baseline live=0 → high (review)."""
    history = _stable_history(5)  # live=0 baseline
    current = _mk_snapshot(mode="live", live=2)
    r = detect_gate_drift(history, current)
    types = [s["type"] for s in r["signals"]]
    assert "live_count_first_emergence" in types
    # No critical (es esperable en modo live), pero high
    assert "live_count_unexpected_mode" not in types


def test_m13_7_signal_live_count_zero_no_trigger():
    history = _stable_history(3)
    current = _mk_snapshot(live=0)
    r = detect_gate_drift(history, current)
    types = [s["type"] for s in r["signals"]]
    assert "live_count_unexpected_mode" not in types
    assert "live_count_first_emergence" not in types


# ════════════════════════════════════════════════════════════════════
#  Severity precedence
# ════════════════════════════════════════════════════════════════════
def test_m13_7_severity_critical_wins_over_high():
    """Cuando hay critical + high → severity = critical."""
    history = _stable_history(5, block_rate=0.05)
    current = _mk_snapshot(
        block_rate=0.7,         # high spike
        internal_errors=1,       # critical
    )
    r = detect_gate_drift(history, current)
    assert r["severity"] == "critical"
    types = [s["type"] for s in r["signals"]]
    assert "block_rate_spike" in types
    assert "internal_error_anomaly" in types


# ════════════════════════════════════════════════════════════════════
#  Edge cases
# ════════════════════════════════════════════════════════════════════
def test_m13_7_empty_history_with_clean_current():
    """No history + clean current → no drift detected."""
    r = detect_gate_drift([], _mk_snapshot())
    assert r["drift_detected"] is False
    assert r["severity"] == "low"


def test_m13_7_malformed_inputs_no_crash():
    """Inputs raros → no crash, safe result."""
    bad_pairs = [
        (None, None),
        ("not a list", "not a dict"),
        ([1, 2, 3], {"random": "stuff"}),
        ({}, _mk_snapshot()),  # history es dict, no list
    ]
    for history, current in bad_pairs:
        try:
            r = detect_gate_drift(history, current)
        except Exception as e:
            assert False, f"crashed on {(history, current)}: {e}"
        assert isinstance(r, dict)
        assert "drift_detected" in r
        assert "severity" in r


def test_m13_7_compute_baseline_pure():
    """compute_baseline es pure — no muta input."""
    import copy
    history = _stable_history(3)
    history_copy = copy.deepcopy(history)
    compute_baseline(history)
    assert history == history_copy


def test_m13_7_compute_baseline_with_malformed_input_no_crash():
    """compute_baseline(None) / int / str → safe defaults."""
    for bad in [None, 42, "str", {}]:
        b = compute_baseline(bad)
        assert b["samples"] == 0
        assert b["live_count_max"] == 0
