"""tests/test_purchase_gate_alerts.py — M13.5 alerting rules tests.

Cobertura:
    - cada una de las 4 reglas dispara correctamente
    - healthy state → no alerts
    - severity precedence (critical > high > medium > low)
    - pure function (mismo input → mismo output excepto timestamp)
    - edge cases (snapshot mal formado, missing keys, types raros)
    - schema de output
"""
import time

from core.observability.alerts import (
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    evaluate_rules,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _mk_snapshot(*, mode="dry_run", enabled=True,
                 attempted=10, allowed=8, blocked=2,
                 dry_run=8, live=0, internal_errors=0,
                 allow_rate=0.8, block_rate=0.2,
                 dry_run_rate=0.8, live_rate=0.0,
                 **denials):
    """Construye un snapshot mínimo del schema M13.3."""
    denial_breakdown = {
        "kill_switch_denials": 0,
        "mode_disabled_denials": 0,
        "duplicate_prevented_count": 0,
        "stale_checkout_denials": 0,
        "cooldown_denials": 0,
        "concurrent_denials": 0,
        "health_denials": 0,
        "mismatch_purchase_denials": 0,
        "critical_crossing_denials": 0,
        "consistency_denials": 0,
        "inflation_denials": 0,
        "retry_denials": 0,
        "invalid_input_denials": 0,
    }
    denial_breakdown.update(denials)
    return {
        "module": "purchase_gate",
        "timestamp": "2026-05-13T00:00:00+00:00",
        "config": {"mode": mode, "global_enabled": enabled,
                    "thresholds": {}},
        "counters": {
            "purchase_attempted_count": attempted,
            "purchase_allowed_count": allowed,
            "purchase_blocked_count": blocked,
            "purchase_dry_run_count": dry_run,
            "purchase_live_count": live,
            "internal_errors": internal_errors,
        },
        "rates": {
            "purchase_allow_rate": allow_rate,
            "purchase_block_rate": block_rate,
            "purchase_dry_run_rate": dry_run_rate,
            "purchase_live_rate": live_rate,
        },
        "denial_breakdown": denial_breakdown,
        "cache_state": {"idempotency_size": 0, "session_locks_count": 0},
    }


# ════════════════════════════════════════════════════════════════════
#  Schema + pure function
# ════════════════════════════════════════════════════════════════════
def test_m13_5_output_schema_complete():
    """Output tiene los 4 keys obligatorios."""
    r = evaluate_rules(_mk_snapshot())
    required = {"alert_active", "severity", "triggered_rules", "timestamp"}
    assert required.issubset(r.keys())


def test_m13_5_severity_levels_ordered():
    """SEVERITY_ORDER: low < medium < high < critical."""
    assert SEVERITY_ORDER["low"] < SEVERITY_ORDER["medium"]
    assert SEVERITY_ORDER["medium"] < SEVERITY_ORDER["high"]
    assert SEVERITY_ORDER["high"] < SEVERITY_ORDER["critical"]
    assert SEVERITY_LEVELS == ("low", "medium", "high", "critical")


def test_m13_5_pure_function_same_input_same_output_minus_timestamp():
    """Llamadas consecutivas con mismo input → mismas keys (excepto timestamp)."""
    snap = _mk_snapshot(internal_errors=5)
    r1 = evaluate_rules(snap)
    r2 = evaluate_rules(snap)
    assert r1["alert_active"] == r2["alert_active"]
    assert r1["severity"] == r2["severity"]
    assert r1["triggered_rules"] == r2["triggered_rules"]
    # Timestamps pueden diferir (legítimo)


# ════════════════════════════════════════════════════════════════════
#  Rule R1 — Internal error spike
# ════════════════════════════════════════════════════════════════════
def test_m13_5_rule_1_internal_error_triggers_critical():
    r = evaluate_rules(_mk_snapshot(internal_errors=1))
    assert r["alert_active"] is True
    assert r["severity"] == "critical"
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "internal_error_spike" in rules


def test_m13_5_rule_1_silent_when_zero_errors():
    r = evaluate_rules(_mk_snapshot(internal_errors=0))
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "internal_error_spike" not in rules


# ════════════════════════════════════════════════════════════════════
#  Rule R2 — Block rate degradation
# ════════════════════════════════════════════════════════════════════
def test_m13_5_rule_2_block_rate_high_triggers_high():
    r = evaluate_rules(_mk_snapshot(block_rate=0.9))
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "block_rate_degradation" in rules
    # Severity: high (no critical)
    assert r["severity"] == "high"


def test_m13_5_rule_2_block_rate_at_threshold_does_not_trigger():
    """Estrictamente > 0.8 — exactly 0.8 NO dispara."""
    r = evaluate_rules(_mk_snapshot(block_rate=0.8))
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "block_rate_degradation" not in rules


# ════════════════════════════════════════════════════════════════════
#  Rule R3 — Dry-run failure anomaly
# ════════════════════════════════════════════════════════════════════
def test_m13_5_rule_3_dry_run_anomaly_triggers_medium():
    r = evaluate_rules(_mk_snapshot(
        attempted=30, dry_run_rate=0.2,
    ))
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "dry_run_failure_anomaly" in rules


def test_m13_5_rule_3_silent_when_low_attempted():
    """Aunque dry_run_rate < 0.5, si attempted ≤ 20, no dispara (insuficiente)."""
    r = evaluate_rules(_mk_snapshot(
        attempted=10, dry_run_rate=0.0,
    ))
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "dry_run_failure_anomaly" not in rules


# ════════════════════════════════════════════════════════════════════
#  Rule R4 — Live execution safety violation
# ════════════════════════════════════════════════════════════════════
def test_m13_5_rule_4_live_count_in_dry_run_triggers_critical():
    r = evaluate_rules(_mk_snapshot(mode="dry_run", live=1))
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "live_execution_safety_violation" in rules
    assert r["severity"] == "critical"


def test_m13_5_rule_4_silent_when_mode_is_live():
    r = evaluate_rules(_mk_snapshot(mode="live", live=5))
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "live_execution_safety_violation" not in rules


def test_m13_5_rule_4_silent_when_no_live_purchases():
    r = evaluate_rules(_mk_snapshot(mode="dry_run", live=0))
    rules = [t["rule"] for t in r["triggered_rules"]]
    assert "live_execution_safety_violation" not in rules


# ════════════════════════════════════════════════════════════════════
#  Healthy / no alerts
# ════════════════════════════════════════════════════════════════════
def test_m13_5_healthy_state_no_alerts():
    """Snapshot saludable → alert_active=False, severity=low."""
    r = evaluate_rules(_mk_snapshot(
        mode="dry_run", attempted=100, dry_run=95, blocked=5,
        dry_run_rate=0.95, block_rate=0.05,
        live=0, internal_errors=0,
    ))
    assert r["alert_active"] is False
    assert r["severity"] == "low"
    assert r["triggered_rules"] == []


# ════════════════════════════════════════════════════════════════════
#  Severity precedence
# ════════════════════════════════════════════════════════════════════
def test_m13_5_severity_precedence_critical_wins():
    """Cuando hay critical + high simultáneos → severity = critical."""
    r = evaluate_rules(_mk_snapshot(
        internal_errors=2,     # critical
        block_rate=0.95,       # high
    ))
    assert r["severity"] == "critical"
    assert len(r["triggered_rules"]) == 2


def test_m13_5_severity_precedence_medium_when_only_medium():
    """Si solo medium dispara → severity = medium."""
    r = evaluate_rules(_mk_snapshot(
        attempted=30, dry_run_rate=0.2,
    ))
    assert r["severity"] == "medium"


# ════════════════════════════════════════════════════════════════════
#  Edge cases
# ════════════════════════════════════════════════════════════════════
def test_m13_5_empty_snapshot_returns_safe_state():
    """Snapshot vacío → no alerts (defaults seguros)."""
    r = evaluate_rules({})
    assert r["alert_active"] is False
    assert r["severity"] == "low"


def test_m13_5_malformed_snapshot_no_crash():
    """Snapshot con types raros (None, list) → no crash, no alert."""
    bad_snapshots = [
        None,
        [],
        "not a dict",
        {"counters": "not a dict"},
        {"counters": {"internal_errors": "not an int"}},
    ]
    for bad in bad_snapshots:
        try:
            r = evaluate_rules(bad)
        except Exception:
            assert False, f"evaluate_rules crashed on {bad!r}"
        # Must return some sane dict
        assert isinstance(r, dict)
        assert "alert_active" in r


def test_m13_5_missing_keys_treated_as_defaults():
    """Si snapshot omite counters/rates, evaluate trata como 0/0.0."""
    r = evaluate_rules({"module": "purchase_gate"})
    # Nothing to alert on (all defaults are 0)
    assert r["alert_active"] is False


def test_m13_5_no_mutation_of_input_snapshot():
    """evaluate_rules NO modifica el snapshot original."""
    snap = _mk_snapshot(internal_errors=3)
    snap_copy = {
        "counters": dict(snap["counters"]),
        "rates": dict(snap["rates"]),
        "denial_breakdown": dict(snap["denial_breakdown"]),
        "config": dict(snap["config"]),
    }
    evaluate_rules(snap)
    # El input no debe haber sido tocado
    assert snap["counters"] == snap_copy["counters"]
    assert snap["rates"] == snap_copy["rates"]
