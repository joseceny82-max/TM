"""tests/test_dashboard_runtime.py — Dashboard observability tests.

Target: ~80-120 tests covering:
    1. dead_code_detector             (12 tests)
    2. slo_engine                      (12 tests)
    3. anomaly detectors individuales  (15 tests)
    4. scan_for_anomalies              (5 tests)
    5. canary decision engine          (15 tests)
    6. legacy readiness score          (8 tests)
    7. runtime_health                  (10 tests)
    8. health_aggregator               (5 tests)
    9. build_system_snapshot           (10 tests)
   10. Simulation: anomaly storms       (5 tests)

stdlib + pytest only.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.dashboard.anomaly_detection import (
    Alert,
    AlertSeverity,
    AnomalyConfig,
    detect_antibot_surge,
    detect_circuit_breaker_storm,
    detect_divergence_spike,
    detect_orphan_wal_growth,
    detect_payment_anomaly,
    detect_shadow_crash_storm,
    detect_success_rate_drop,
    scan_for_anomalies,
    write_alerts,
)
from core.dashboard.canary_decision_engine import (
    CanaryDecision,
    compute_legacy_readiness_score,
    decide,
)
from core.dashboard.dashboard_state import build_system_snapshot
from core.dashboard.dead_code_detector import (
    build_dead_components_report,
    find_unemitted_event_codes,
    find_unused_feature_flags,
    find_unused_modules,
    find_zero_traffic_metrics,
)
from core.dashboard.health_aggregator import aggregate_health
from core.dashboard.runtime_health import (
    compute_cutover_stats,
    compute_runtime_health,
    compute_shadow_stats,
    compute_step_success_rates,
    compute_top_failures,
)
from core.dashboard.slo_engine import (
    SLOStatus,
    SLOTargets,
    evaluate_slos,
)


BASE = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)


def _ev(code, category="flow", *,
         severity="recoverable", context="",
         payload=None, ts_offset=0):
    return {
        "code": code, "category": category, "context": context,
        "severity": severity,
        "payload": payload or {},
        "timestamp": (BASE + timedelta(seconds=ts_offset)).isoformat(),
        "cart_id": "", "idempotency_key": "",
        "message": "", "exception_class": "",
    }


def _write_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


# ════════════════════════════════════════════════════════════════════
#  1. DEAD CODE DETECTOR
# ════════════════════════════════════════════════════════════════════
class TestDeadCodeDetector:

    def test_find_unused_modules_returns_structure(self, tmp_path):
        # Crear estructura mínima
        core_dir = tmp_path / "core" / "x"
        core_dir.mkdir(parents=True)
        (core_dir / "foo.py").write_text("def f(): pass")
        (tmp_path / "main.py").write_text("from core.x.foo import f")
        r = find_unused_modules(project_root=tmp_path)
        assert set(r.keys()) == {
            "total_modules", "imported_modules", "unused", "details",
        }
        # foo.py importada → no debe estar unused
        assert "core.x.foo" not in r["unused"]

    def test_find_unused_modules_detects_orphan(self, tmp_path):
        core_dir = tmp_path / "core" / "x"
        core_dir.mkdir(parents=True)
        (core_dir / "orphan.py").write_text("def orphan(): pass")
        (core_dir / "used.py").write_text("def used(): pass")
        (tmp_path / "main.py").write_text("from core.x.used import used")
        r = find_unused_modules(project_root=tmp_path)
        assert "core.x.orphan" in r["unused"]
        assert "core.x.used" not in r["unused"]

    def test_find_unused_modules_excludes_init(self, tmp_path):
        core_dir = tmp_path / "core" / "y"
        core_dir.mkdir(parents=True)
        (core_dir / "__init__.py").write_text("")
        (tmp_path / "main.py").write_text("# nothing")
        r = find_unused_modules(project_root=tmp_path)
        # __init__.py NO debe contar como unused
        assert not any("__init__" in k for k in r["unused"])

    def test_find_unemitted_codes_with_real_catalog(self):
        # Sin eventos → todos los códigos del catálogo son unemitted
        r = find_unemitted_event_codes(events_source=[])
        assert r["total_unemitted"] > 0
        assert r["coverage_rate"] == 0.0

    def test_find_unemitted_codes_partial_coverage(self):
        events = [
            {"code": "card_declined", "category": "payment"},
            {"code": "timeout", "category": "network"},
        ]
        r = find_unemitted_event_codes(events_source=events)
        assert "card_declined" not in r["unemitted_per_category"]["payment"]
        # Otros códigos siguen unemitted
        assert "3ds_required" in r["unemitted_per_category"]["payment"]

    def test_find_unemitted_codes_full_coverage(self):
        # Imposible alcanzar 100% sin emitir TODOS los códigos
        from core.observability.error_classification import ERROR_CODES
        events = []
        for cat, codes in ERROR_CODES.items():
            for c in codes:
                events.append({"code": c, "category": cat.value})
        r = find_unemitted_event_codes(events_source=events)
        assert r["coverage_rate"] == 1.0
        assert r["total_unemitted"] == 0

    def test_find_unused_feature_flags_default(self):
        r = find_unused_feature_flags(project_root=PROJ_ROOT)
        assert "total_flags" in r
        assert "unused" in r
        assert "usage_per_flag" in r

    def test_find_unused_flags_with_explicit_list(self):
        r = find_unused_feature_flags(
            project_root=PROJ_ROOT,
            flag_names=["DEFINITELY_UNUSED_FLAG_XYZ"],
        )
        assert "DEFINITELY_UNUSED_FLAG_XYZ" in r["unused"]

    def test_find_zero_traffic_metrics_no_events(self):
        r = find_zero_traffic_metrics(events_source=[])
        assert r["total_events"] == 0
        assert r["diagnosis"] == "insufficient_data"
        # Todas las productivas a cero
        assert "payment" in r["productive_categories_with_zero_traffic"]

    def test_find_zero_traffic_with_partial_events(self):
        events = [{"category": "payment", "code": "card_declined"}
                   for _ in range(15)]
        r = find_zero_traffic_metrics(events_source=events)
        # >10 events → ya no es insufficient_data
        # payment tiene tráfico, pero anti_bot/network/etc. siguen a cero
        assert "anti_bot" in r["productive_categories_with_zero_traffic"]
        assert r["diagnosis"] == "possible_broken_hooks"

    def test_build_dead_components_report_structure(self, tmp_path):
        r = build_dead_components_report(
            project_root=tmp_path,
            events_source=[],
        )
        assert set(r.keys()) >= {
            "generated_at", "unused_modules", "unemitted_codes",
            "unused_flags", "zero_traffic_metrics",
        }

    def test_build_dead_report_persists(self, tmp_path):
        report_path = tmp_path / "report.json"
        build_dead_components_report(
            project_root=tmp_path, events_source=[],
            output_path=report_path,
        )
        assert report_path.exists()
        data = json.loads(report_path.read_text())
        assert "unused_modules" in data


# ════════════════════════════════════════════════════════════════════
#  2. SLO ENGINE
# ════════════════════════════════════════════════════════════════════
class TestSLOEngine:

    def test_insufficient_data_below_threshold(self):
        m = {"events_in_window": 5}
        r = evaluate_slos(m)
        assert r["overall_status"] == SLOStatus.INSUFFICIENT_DATA.value
        assert r["data_sufficient"] is False

    def test_passing_when_all_targets_met(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.95},
            "latency": {"p95_ms": 2000, "p99_ms": 4000},
            "idempotency": {"orphan_rate": 0.0001},
            "avg_divergence_score": 5.0,
        }
        r = evaluate_slos(m)
        assert r["overall_status"] == SLOStatus.PASSING.value

    def test_violating_on_low_success_rate(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.40},   # << 0.90
        }
        r = evaluate_slos(m)
        assert r["overall_status"] == SLOStatus.VIOLATING.value
        assert "success_rate" in r["violations"]

    def test_burning_on_marginal_drop(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.88},  # entre 95% target y violator
        }
        r = evaluate_slos(m)
        assert r["overall_status"] in {
            SLOStatus.BURNING.value, SLOStatus.VIOLATING.value,
        }

    def test_violating_on_high_latency(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.95},
            "latency": {"p95_ms": 20000, "p99_ms": 30000},
        }
        r = evaluate_slos(m)
        assert r["overall_status"] == SLOStatus.VIOLATING.value
        assert "p95_latency_ms" in r["violations"]

    def test_violating_on_orphan_rate(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.95},
            "idempotency": {"orphan_rate": 0.05},   # >> 0.001 max
        }
        r = evaluate_slos(m)
        assert "orphan_rate" in r["violations"]

    def test_violating_on_divergence(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.95},
            "avg_divergence_score": 50.0,    # > 10 max
        }
        r = evaluate_slos(m)
        assert "divergence_score" in r["violations"]

    def test_custom_targets(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.70},
        }
        targets = SLOTargets(success_rate_min=0.50)
        r = evaluate_slos(m, targets=targets)
        # 0.70 > 0.50 → passing
        assert "success_rate" not in r["violations"]

    def test_evaluations_have_canonical_fields(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.95},
        }
        r = evaluate_slos(m)
        for e in r["evaluations"]:
            assert set(e.keys()) == {
                "name", "target", "actual", "status",
                "burn_rate", "kind",
            }

    def test_burn_rate_calculation(self):
        m = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.85},   # below 0.90 target
        }
        r = evaluate_slos(m)
        sr = next(e for e in r["evaluations"] if e["name"] == "success_rate")
        # Quemamos parte del margen 1.0-0.90 = 0.10. gap = 0.05 → burn = 0.5
        assert sr["burn_rate"] > 0

    def test_slo_targets_to_dict(self):
        t = SLOTargets()
        d = t.to_dict()
        assert "success_rate_min" in d

    def test_empty_metrics_safe(self):
        r = evaluate_slos({})
        assert r["overall_status"] == SLOStatus.INSUFFICIENT_DATA.value


# ════════════════════════════════════════════════════════════════════
#  3. ANOMALY DETECTORS (individuales)
# ════════════════════════════════════════════════════════════════════
class TestAnomalyDetectors:

    def test_success_rate_drop_warning(self):
        cur = {"success_rate_24h": {"rate": 0.85}}
        base = {"success_rate_24h": {"rate": 0.92}}
        a = detect_success_rate_drop(cur, base)
        assert a is not None
        assert a.severity == AlertSeverity.WARNING.value

    def test_success_rate_drop_critical(self):
        cur = {"success_rate_24h": {"rate": 0.78}}
        base = {"success_rate_24h": {"rate": 0.92}}
        a = detect_success_rate_drop(cur, base)
        assert a.severity == AlertSeverity.CRITICAL.value

    def test_success_rate_drop_emergency(self):
        cur = {"success_rate_24h": {"rate": 0.60}}
        base = {"success_rate_24h": {"rate": 0.92}}
        a = detect_success_rate_drop(cur, base)
        assert a.severity == AlertSeverity.EMERGENCY.value

    def test_success_rate_no_drop_no_alert(self):
        cur = {"success_rate_24h": {"rate": 0.91}}
        base = {"success_rate_24h": {"rate": 0.92}}
        assert detect_success_rate_drop(cur, base) is None

    def test_divergence_spike_warning(self):
        a = detect_divergence_spike(25.0)
        assert a is not None
        assert a.severity == AlertSeverity.WARNING.value

    def test_divergence_spike_emergency(self):
        a = detect_divergence_spike(85.0)
        assert a.severity == AlertSeverity.EMERGENCY.value

    def test_divergence_no_alert_when_low(self):
        assert detect_divergence_spike(5.0) is None

    def test_payment_anomaly_detected(self):
        cur = {"success_rate_24h": {"rate": 0.80}}
        base = {"success_rate_24h": {"rate": 0.92}}
        a = detect_payment_anomaly(cur, base)
        assert a is not None

    def test_orphan_wal_growth_warning(self):
        cur = {"idempotency": {"orphan_rate": 0.08}}
        a = detect_orphan_wal_growth(cur)
        assert a is not None
        assert a.severity == AlertSeverity.WARNING.value

    def test_orphan_wal_growth_critical(self):
        cur = {"idempotency": {"orphan_rate": 0.20}}
        a = detect_orphan_wal_growth(cur)
        assert a.severity == AlertSeverity.CRITICAL.value

    def test_circuit_breaker_storm_warning(self):
        a = detect_circuit_breaker_storm(8)
        assert a is not None
        assert a.severity == AlertSeverity.WARNING.value

    def test_circuit_breaker_storm_critical(self):
        a = detect_circuit_breaker_storm(50)
        assert a.severity == AlertSeverity.CRITICAL.value

    def test_antibot_surge_warning(self):
        a = detect_antibot_surge(15)
        assert a is not None

    def test_shadow_crash_storm_high_rate(self):
        a = detect_shadow_crash_storm(60, 100)
        assert a is not None
        assert a.severity == AlertSeverity.CRITICAL.value

    def test_shadow_crash_storm_no_runs(self):
        # 0 runs → None
        assert detect_shadow_crash_storm(0, 0) is None


# ════════════════════════════════════════════════════════════════════
#  4. scan_for_anomalies
# ════════════════════════════════════════════════════════════════════
class TestScanForAnomalies:

    def test_no_anomalies_in_healthy_state(self):
        cur = {"success_rate_24h": {"rate": 0.92},
                "idempotency": {"orphan_rate": 0.0001}}
        base = {"success_rate_24h": {"rate": 0.92}}
        alerts = scan_for_anomalies(
            current_metrics=cur, baseline_metrics=base,
            avg_divergence_score=2.0, open_circuit_breakers=0,
            antibot_events=0, shadow_crashes=0, shadow_runs=10,
        )
        assert alerts == []

    def test_multiple_alerts_in_storm(self):
        cur = {"success_rate_24h": {"rate": 0.40},
                "idempotency": {"orphan_rate": 0.20}}
        base = {"success_rate_24h": {"rate": 0.92}}
        alerts = scan_for_anomalies(
            current_metrics=cur, baseline_metrics=base,
            avg_divergence_score=80.0, open_circuit_breakers=30,
            antibot_events=60, shadow_crashes=80, shadow_runs=100,
        )
        codes = {a["code"] for a in alerts}
        # Múltiples categorías afectadas
        assert "success_rate_drop" in codes
        assert "divergence_spike" in codes
        assert len(alerts) >= 4

    def test_write_alerts_persists(self, tmp_path):
        alerts = [{"code": "x", "severity": "info", "message": "test",
                    "actual": 1.0, "threshold": 0.5, "delta": 0.5,
                    "timestamp": "2026-05-14T10:00:00+00:00", "payload": {}}]
        path = tmp_path / "alerts.jsonl"
        ok = write_alerts(alerts, path=path)
        assert ok
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_write_alerts_empty_safe(self, tmp_path):
        path = tmp_path / "alerts.jsonl"
        assert write_alerts([], path=path) is True

    def test_write_alerts_invalid_path_safe(self):
        # Path imposible → no debe propagar
        assert write_alerts(
            [{"code": "x"}],
            path="/invalid/blocking/path/alerts.jsonl"
        ) in {True, False}   # any return is fine, no raise


# ════════════════════════════════════════════════════════════════════
#  5. CANARY DECISION ENGINE
# ════════════════════════════════════════════════════════════════════
class TestCanaryDecisionEngine:

    def test_insufficient_data_when_no_events(self):
        r = decide(current_metrics={}, prior_metrics={})
        assert r.decision == CanaryDecision.INSUFFICIENT_DATA.value

    def test_insufficient_data_few_events(self):
        cur = {"events_in_window": 10}
        r = decide(current_metrics=cur, prior_metrics={})
        assert r.decision == CanaryDecision.INSUFFICIENT_DATA.value

    def test_rollback_on_emergency(self):
        cur = {"events_in_window": 100,
                "success_rate_24h": {"rate": 0.92}}
        alerts = [{"code": "x", "severity": "emergency"}]
        r = decide(current_metrics=cur, prior_metrics=cur, alerts=alerts)
        assert r.decision == CanaryDecision.ROLLBACK.value
        assert r.risk_level == "critical"

    def test_rollback_on_slo_violation(self):
        cur = {"events_in_window": 100,
                "success_rate_24h": {"rate": 0.30}}
        r = decide(current_metrics=cur, prior_metrics=cur)
        assert r.decision == CanaryDecision.ROLLBACK.value

    def test_freeze_on_critical_alert(self):
        cur = {"events_in_window": 100,
                "success_rate_24h": {"rate": 0.92}}
        alerts = [{"code": "x", "severity": "critical"}]
        r = decide(current_metrics=cur, prior_metrics=cur, alerts=alerts)
        assert r.decision == CanaryDecision.FREEZE.value

    def test_hold_when_gates_fail(self):
        cur = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.92},
            "avg_divergence_score": 50,   # gate fail
        }
        prior = {"success_rate_24h": {"rate": 0.92}}
        r = decide(current_metrics=cur, prior_metrics=prior)
        # Divergence elevated triggers critical alert OR gate fail
        assert r.decision in {
            CanaryDecision.HOLD.value, CanaryDecision.ROLLBACK.value,
            CanaryDecision.FREEZE.value,
        }

    def test_proceed_when_all_green_and_enough_days(self):
        cur = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.95},
            "idempotency": {"orphan_rate": 0.0},
            "avg_divergence_score": 2,
            "latency": {"p95_ms": 2000, "p99_ms": 4000},
        }
        prior = cur
        r = decide(
            current_metrics=cur, prior_metrics=prior,
            alerts=[], days_of_clean_data=5,
            current_canary_rate=0.01,
        )
        assert r.decision == CanaryDecision.PROCEED.value

    def test_hold_when_not_enough_days(self):
        cur = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.95},
            "idempotency": {"orphan_rate": 0.0},
            "avg_divergence_score": 2,
            "latency": {"p95_ms": 2000, "p99_ms": 4000},
        }
        r = decide(
            current_metrics=cur, prior_metrics=cur,
            days_of_clean_data=1,
        )
        assert r.decision == CanaryDecision.HOLD.value

    def test_cutover_ready_after_14_days_full_canary(self):
        cur = {
            "events_in_window": 1000,
            "success_rate_24h": {"rate": 0.95},
            "idempotency": {"orphan_rate": 0.0},
            "avg_divergence_score": 2,
            "latency": {"p95_ms": 2000, "p99_ms": 4000},
        }
        r = decide(
            current_metrics=cur, prior_metrics=cur,
            days_of_clean_data=15,
            current_canary_rate=1.0,
        )
        assert r.decision == CanaryDecision.CUTOVER_READY.value

    def test_result_has_canonical_fields(self):
        r = decide(current_metrics={})
        d = r.to_dict()
        assert set(d.keys()) == {
            "decision", "confidence", "reasons", "risk_level",
            "gates_passed", "slo_status", "data_sufficient",
        }

    def test_confidence_is_float(self):
        r = decide(current_metrics={})
        assert isinstance(r.confidence, float)

    def test_reasons_are_tuple(self):
        r = decide(current_metrics={})
        assert isinstance(r.reasons, tuple)


# ════════════════════════════════════════════════════════════════════
#  6. LEGACY READINESS SCORE
# ════════════════════════════════════════════════════════════════════
class TestLegacyReadiness:

    def test_zero_when_no_data(self):
        r = compute_legacy_readiness_score(
            current_canary_rate=0.0, days_of_clean_data=0,
            fallback_count_24h=0, divergence_avg=0.0,
            slo_passing=False, orphan_rate=0.0,
            events_observed=0,
        )
        assert r["score"] == 0
        assert r["step14_unlocked"] is False
        assert "INSUFFICIENT_PRODUCTION_DATA" in r["blockers"]

    def test_zero_until_minimum_events(self):
        r = compute_legacy_readiness_score(
            current_canary_rate=1.0, days_of_clean_data=14,
            fallback_count_24h=0, divergence_avg=0.0,
            slo_passing=True, orphan_rate=0.0,
            events_observed=50,    # < 100 minimum
        )
        assert r["score"] == 0

    def test_full_score_after_14_days_clean_full_canary(self):
        r = compute_legacy_readiness_score(
            current_canary_rate=1.0, days_of_clean_data=14,
            fallback_count_24h=0, divergence_avg=0.5,
            slo_passing=True, orphan_rate=0.0001,
            events_observed=1000,
        )
        assert r["score"] >= 95
        assert r["step14_unlocked"] is True

    def test_partial_canary_no_unlock(self):
        r = compute_legacy_readiness_score(
            current_canary_rate=0.5, days_of_clean_data=14,
            fallback_count_24h=0, divergence_avg=0.5,
            slo_passing=True, orphan_rate=0.0001,
            events_observed=1000,
        )
        assert r["step14_unlocked"] is False

    def test_fallback_count_blocks_unlock(self):
        r = compute_legacy_readiness_score(
            current_canary_rate=1.0, days_of_clean_data=14,
            fallback_count_24h=5,    # >0
            divergence_avg=0.5, slo_passing=True, orphan_rate=0.0,
            events_observed=1000,
        )
        assert r["step14_unlocked"] is False
        assert any("fallback" in b for b in r["blockers"])

    def test_blockers_listed(self):
        r = compute_legacy_readiness_score(
            current_canary_rate=0.1, days_of_clean_data=3,
            fallback_count_24h=2, divergence_avg=5.0,
            slo_passing=False, orphan_rate=0.05,
            events_observed=500,
        )
        # Múltiples blockers presentes
        assert len(r["blockers"]) >= 2

    def test_high_divergence_blocks(self):
        r = compute_legacy_readiness_score(
            current_canary_rate=1.0, days_of_clean_data=14,
            fallback_count_24h=0, divergence_avg=15.0,   # > 1.0
            slo_passing=True, orphan_rate=0.0,
            events_observed=1000,
        )
        assert r["step14_unlocked"] is False

    def test_score_capped_at_100(self):
        r = compute_legacy_readiness_score(
            current_canary_rate=1.0, days_of_clean_data=30,   # > 14
            fallback_count_24h=0, divergence_avg=0.1,
            slo_passing=True, orphan_rate=0.0,
            events_observed=10000,
        )
        assert r["score"] == 100


# ════════════════════════════════════════════════════════════════════
#  7. RUNTIME HEALTH
# ════════════════════════════════════════════════════════════════════
class TestRuntimeHealth:

    def test_empty_sink_data_insufficient(self, tmp_path):
        r = compute_runtime_health(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            now=BASE,
        )
        assert r["data_sufficient"] is False
        assert r["events_observed"] == 0

    def test_step_success_rates(self):
        events = [
            _ev("outcome_recorded", context="attach_cart",
                 payload={"status": "paid"}),
            _ev("outcome_recorded", context="attach_cart",
                 payload={"status": "paid"}),
            _ev("outcome_recorded", context="attach_cart",
                 payload={"status": "payment_failed"}),
        ]
        r = compute_step_success_rates(events)
        assert r["attach_cart"] == pytest.approx(2/3, abs=1e-3)

    def test_top_failures(self):
        events = [
            _ev("card_declined", "payment", severity="fatal"),
            _ev("card_declined", "payment", severity="fatal"),
            _ev("timeout", "network", severity="retryable"),
        ]
        r = compute_top_failures(events)
        # card_declined es el top
        assert r[0]["key"] == "payment/card_declined"
        assert r[0]["count"] == 2

    def test_top_failures_excludes_operational(self):
        events = [
            _ev("flow_started", "flow", severity="recoverable"),
            _ev("phase_transition", "flow", severity="recoverable"),
            _ev("card_declined", "payment", severity="fatal"),
        ]
        r = compute_top_failures(events)
        codes = {e["key"] for e in r}
        # flow operationals excluidos
        assert "flow/flow_started" not in codes
        assert "payment/card_declined" in codes

    def test_shadow_stats_no_shadow(self):
        r = compute_shadow_stats([])
        assert r["shadow_compares"] == 0
        assert r["divergence_avg"] == 0.0

    def test_shadow_stats_with_data(self):
        events = [
            _ev("canary_compare", "flow",
                 payload={"divergence_score": 5}),
            _ev("canary_compare", "flow",
                 payload={"divergence_score": 15}),
            _ev("shadow_core_crash", "flow"),
        ]
        r = compute_shadow_stats(events)
        assert r["shadow_compares"] == 2
        assert r["shadow_crashes"] == 1
        assert r["divergence_avg"] == 10.0

    def test_cutover_stats(self):
        events = [
            _ev("cutover_event", "flow",
                 payload={"used_path": "legacy"}),
            _ev("cutover_event", "flow",
                 payload={"used_path": "flowcore"}),
            _ev("cutover_event", "flow",
                 payload={"used_path": "legacy_fallback",
                          "fallback_triggered": True}),
        ]
        r = compute_cutover_stats(events)
        assert r["total_routings"] == 3
        assert r["fallback_count"] == 1

    def test_compute_runtime_health_full_pipeline(self, tmp_path):
        events = [
            _ev("flow_started", "flow", ts_offset=0),
            _ev("outcome_recorded", "flow", ts_offset=2,
                 payload={"status": "paid"}),
            _ev("canary_compare", "flow", ts_offset=2,
                 payload={"divergence_score": 5}),
        ] * 5
        sink = tmp_path / "structured_events_pid1.jsonl"
        _write_events(sink, events)
        r = compute_runtime_health(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            now=BASE + timedelta(hours=1),
        )
        assert r["events_observed"] == 15
        assert r["data_sufficient"] is True
        assert r["shadow_stats"]["shadow_compares"] == 5

    def test_data_sufficient_threshold(self, tmp_path):
        # 5 events → data_sufficient = False
        events = [_ev("flow_started", ts_offset=i) for i in range(5)]
        sink = tmp_path / "structured_events_pid1.jsonl"
        _write_events(sink, events)
        r = compute_runtime_health(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            now=BASE + timedelta(hours=1),
        )
        assert r["data_sufficient"] is False

    def test_window_filter(self, tmp_path):
        old = _ev("flow_started", ts_offset=-7200)  # 2h ago
        new = _ev("flow_started", ts_offset=0)
        sink = tmp_path / "structured_events_pid1.jsonl"
        _write_events(sink, [old, new])
        r = compute_runtime_health(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            window_hours=1.0,
            now=BASE,
        )
        # Only the recent one
        assert r["events_observed"] == 1


# ════════════════════════════════════════════════════════════════════
#  8. HEALTH AGGREGATOR
# ════════════════════════════════════════════════════════════════════
class TestHealthAggregator:

    def test_aggregate_empty_returns_structure(self, tmp_path):
        r = aggregate_health(
            sink_dir=str(tmp_path),
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            now=BASE,
        )
        assert "metrics" in r
        assert "runtime" in r
        assert "slo" in r
        assert "circuit_breakers" in r
        assert "cutover" in r

    def test_aggregate_with_data(self, tmp_path):
        events = [
            _ev("flow_started", "flow", ts_offset=0),
            _ev("outcome_recorded", "flow",
                 payload={"status": "paid"}, ts_offset=2),
        ] * 50
        sink = tmp_path / "structured_events_pid1.jsonl"
        _write_events(sink, events)
        r = aggregate_health(
            sink_dir=str(tmp_path),
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            now=BASE + timedelta(hours=1),
        )
        assert r["runtime"]["events_observed"] == 100
        assert r["runtime"]["data_sufficient"] is True

    def test_aggregate_handles_missing_directories(self, tmp_path):
        # Directorio que no existe
        r = aggregate_health(
            sink_dir=str(tmp_path / "missing"),
            events_glob=str(tmp_path / "missing/structured_events_*.jsonl"),
            idem_root=str(tmp_path / "missing/idem"),
        )
        # No raise; estructura siempre presente
        assert "metrics" in r

    def test_aggregate_returns_divergence_avg(self, tmp_path):
        events = [
            _ev("canary_compare", "flow",
                 payload={"divergence_score": 10})
        ] * 12
        sink = tmp_path / "structured_events_pid1.jsonl"
        _write_events(sink, events)
        r = aggregate_health(
            sink_dir=str(tmp_path),
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            now=BASE + timedelta(hours=1),
        )
        assert r["divergence_avg"] == 10.0


# ════════════════════════════════════════════════════════════════════
#  9. build_system_snapshot
# ════════════════════════════════════════════════════════════════════
class TestBuildSystemSnapshot:

    def test_snapshot_no_data_honest(self, tmp_path):
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
            now=BASE,
        )
        assert r["honest_assessment"]["summary"] == "INSUFFICIENT_DATA"
        assert "INSUFFICIENT_PRODUCTION_DATA" in r["honest_assessment"]["blocking_flags"]
        assert r["legacy_readiness"]["score"] == 0
        assert r["step14_unlock"] is False

    def test_snapshot_top_level_keys(self, tmp_path):
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
            now=BASE,
        )
        required = {
            "as_of", "system_state", "flow_mode", "canary_rate",
            "kill_switch", "health", "shadow_health", "cutover_stats",
            "circuit_breakers", "slo", "alerts", "alert_summary",
            "canary_decision", "legacy_readiness", "step14_unlock",
            "top_failures", "dead_components", "risk_level",
            "recommended_action", "honest_assessment",
        }
        missing = required - set(r.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_snapshot_with_synthetic_data(self, tmp_path):
        events = []
        for i in range(60):
            events.append(_ev("flow_started", "flow", ts_offset=i))
            events.append(_ev(
                "outcome_recorded", "flow",
                payload={"status": "paid"}, ts_offset=i+1,
            ))
        sink = tmp_path / "structured_events_pid1.jsonl"
        _write_events(sink, events)
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
            now=BASE + timedelta(hours=1),
        )
        assert r["health"]["data_sufficient"] is True

    def test_snapshot_default_flow_mode_is_legacy(self, tmp_path):
        # Reset cutover state + P2 flag file (cross-process kill switch)
        from core.migration import reset_state
        from pathlib import Path
        flag = Path("runs/_kill_switch.flag")
        if flag.exists():
            try: flag.unlink()
            except: pass
        reset_state()
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
        )
        assert r["flow_mode"] == "legacy"
        assert r["kill_switch"] is False

    def test_snapshot_recommended_action_is_string(self, tmp_path):
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
        )
        assert isinstance(r["recommended_action"], str)
        assert r["recommended_action"]

    def test_snapshot_json_serializable(self, tmp_path):
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
        )
        s = json.dumps(r, default=str)
        assert isinstance(s, str)

    def test_snapshot_with_dead_code_scan(self, tmp_path):
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=True,
            project_root=str(tmp_path),  # tmp empty → trivial scan
        )
        assert r["dead_components"] is not None

    def test_snapshot_alert_summary(self, tmp_path):
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
        )
        assert "total" in r["alert_summary"]
        assert "by_severity" in r["alert_summary"]

    def test_snapshot_honest_flags_completeness(self, tmp_path):
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
        )
        flags = r["honest_assessment"]["blocking_flags"]
        # En estado vacío esperamos múltiples flags
        assert "INSUFFICIENT_PRODUCTION_DATA" in flags
        assert "LEGACY_STILL_PRIMARY_ALL_TRAFFIC" in flags
        assert "SHADOW_NEVER_RAN" in flags

    def test_snapshot_does_not_crash_on_corrupted_events(self, tmp_path):
        sink = tmp_path / "structured_events_pid1.jsonl"
        sink.write_text("{ corrupt json\nrandom garbage\n{ also corrupt\n",
                          encoding="utf-8")
        # No debe propagar
        r = build_system_snapshot(
            events_glob=str(tmp_path / "structured_events_*.jsonl"),
            idem_root=str(tmp_path / "idem"),
            include_dead_code_scan=False,
        )
        assert isinstance(r, dict)


# ════════════════════════════════════════════════════════════════════
#  10. SIMULATION: ANOMALY STORMS
# ════════════════════════════════════════════════════════════════════
class TestAnomalyStorms:

    def test_payment_outage_simulation(self):
        """Simula caída de pagos masiva."""
        cur = {"events_in_window": 200,
                "success_rate_24h": {"rate": 0.20}}  # collapse
        base = {"success_rate_24h": {"rate": 0.92}}
        alerts = scan_for_anomalies(
            current_metrics=cur, baseline_metrics=base,
            avg_divergence_score=0,
        )
        # Debe detectar emergency
        emergency = [a for a in alerts if a["severity"] == "emergency"]
        assert len(emergency) >= 1

    def test_antibot_wave_simulation(self):
        alerts = scan_for_anomalies(
            current_metrics={"events_in_window": 100,
                              "success_rate_24h": {"rate": 0.92}},
            baseline_metrics={"success_rate_24h": {"rate": 0.92}},
            antibot_events=100,
        )
        codes = {a["code"] for a in alerts}
        assert "antibot_surge" in codes

    def test_circuit_breaker_storm_simulation(self):
        alerts = scan_for_anomalies(
            current_metrics={"events_in_window": 100,
                              "success_rate_24h": {"rate": 0.92}},
            baseline_metrics={"success_rate_24h": {"rate": 0.92}},
            open_circuit_breakers=50,
        )
        codes = {a["code"] for a in alerts}
        assert "circuit_breaker_storm" in codes

    def test_divergence_storm_blocks_proceed(self):
        cur = {
            "events_in_window": 100,
            "success_rate_24h": {"rate": 0.95},
            "avg_divergence_score": 80,
        }
        alerts = scan_for_anomalies(
            current_metrics=cur, baseline_metrics=cur,
            avg_divergence_score=80,
        )
        # Emergency alert → decision rollback
        r = decide(current_metrics=cur, prior_metrics=cur,
                    alerts=alerts, days_of_clean_data=5,
                    current_canary_rate=0.1)
        assert r.decision in {
            CanaryDecision.ROLLBACK.value, CanaryDecision.FREEZE.value,
        }

    def test_orphan_growth_triggers_alert(self):
        cur = {"events_in_window": 100,
                "success_rate_24h": {"rate": 0.92},
                "idempotency": {"orphan_rate": 0.25}}
        alerts = scan_for_anomalies(
            current_metrics=cur,
            baseline_metrics={"success_rate_24h": {"rate": 0.92}},
        )
        codes = {a["code"] for a in alerts}
        assert "orphan_wal_growth" in codes
