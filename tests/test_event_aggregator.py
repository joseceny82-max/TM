"""tests/test_event_aggregator.py — Test suite for STEP 4.

Cubre las primitivas de agregación + correlación.

Categorías:
    1. read_event_stream             (5 tests)
    2. group_by / count_by            (4 tests)
    3. failure_rate_by_category       (5 tests)
    4. step_failure_distribution      (4 tests)
    5. cart_lifecycle_heatmap         (4 tests)
    6. correlation                    (4 tests)
    7. severity_breakdown             (3 tests)
    8. aggregate_all                  (3 tests)
    9. Determinism + fail-safe        (4 tests)

stdlib + pytest only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.observability.event_aggregator import (
    FAILURE_CATEGORIES,
    OPERATIONAL_CODES,
    aggregate_all,
    cart_lifecycle_heatmap,
    correlate_by_cart_id,
    correlate_by_idempotency_key,
    count_by,
    failure_rate_by_category,
    group_by,
    read_event_stream,
    severity_breakdown,
    step_failure_distribution,
)
from core.observability.event_schema import (
    AggregatedMetric,
    EventGroup,
    UNKNOWN_BUCKET,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
def _ev(category: str, code: str, *, context: str = "",
        cart_id: str = "", idem_key: str = "",
        severity: str = "recoverable",
        phase: str = "") -> dict:
    return {
        "category": category,
        "code": code,
        "context": context,
        "cart_id": cart_id,
        "idempotency_key": idem_key,
        "severity": severity,
        "payload": {"cart_phase": phase} if phase else {},
        "timestamp": "2026-05-14T10:00:00+00:00",
        "message": "",
        "exception_class": "",
    }


def _failure_set() -> list:
    return [
        _ev("payment", "card_declined", context="submit_payment",
            cart_id="C1", idem_key="ik-1", severity="fatal",
            phase="checking_out"),
        _ev("payment", "card_declined", context="submit_payment",
            cart_id="C2", idem_key="ik-2", severity="fatal",
            phase="checking_out"),
        _ev("network", "timeout", context="attach_cart",
            cart_id="C3", idem_key="ik-3", severity="retryable"),
        _ev("anti_bot", "akamai_block", context="boot",
            cart_id="", idem_key="ik-4", severity="non_recoverable",
            phase="init"),
        _ev("flow", "flow_started", context="start_flow",
            cart_id="", idem_key="ik-5", severity="recoverable"),
        _ev("flow", "phase_transition", context="transition_phase",
            cart_id="C1", idem_key="ik-1", severity="recoverable"),
    ]


# ════════════════════════════════════════════════════════════════════
#  1. read_event_stream
# ════════════════════════════════════════════════════════════════════
class TestReadEventStream:

    def test_reads_jsonl_file(self, tmp_path):
        p = tmp_path / "events.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for ev in _failure_set():
                f.write(json.dumps(ev) + "\n")
        events = read_event_stream(p)
        assert len(events) == 6

    def test_skips_corrupt_lines(self, tmp_path):
        p = tmp_path / "events.jsonl"
        with p.open("w", encoding="utf-8") as f:
            f.write(json.dumps(_ev("network", "timeout")) + "\n")
            f.write("{ not valid json\n")
            f.write(json.dumps(_ev("payment", "card_declined")) + "\n")
        events = read_event_stream(p)
        assert len(events) == 2

    def test_nonexistent_path_returns_empty(self, tmp_path):
        assert read_event_stream(tmp_path / "missing.jsonl") == []

    def test_accepts_iterable_directly(self):
        events = read_event_stream(_failure_set())
        assert len(events) == 6

    def test_filters_non_event_dicts(self):
        mixed = [_ev("network", "timeout"), {"random": "garbage"},
                  "string", 42, None]
        events = read_event_stream(mixed)
        assert len(events) == 1


# ════════════════════════════════════════════════════════════════════
#  2. group_by / count_by
# ════════════════════════════════════════════════════════════════════
class TestGroupAndCount:

    def test_group_by_category(self):
        evs = _failure_set()
        grouped = group_by(evs, lambda e: e["category"])
        assert "payment" in grouped
        assert len(grouped["payment"]) == 2

    def test_count_by_code(self):
        evs = _failure_set()
        counts = count_by(evs, lambda e: e["code"])
        assert counts["card_declined"] == 2
        assert counts["timeout"] == 1

    def test_group_by_handles_missing_keys(self):
        evs = [{"category": "x", "code": "y"}, {"category": "x", "code": "z"}]
        grouped = group_by(evs, lambda e: e.get("missing", "default"))
        assert "default" in grouped

    def test_group_by_key_fn_raises_safely(self):
        evs = _failure_set()
        # key_fn que lanza para algunos eventos
        def bad_key(e):
            raise RuntimeError("boom")
        grouped = group_by(evs, bad_key)
        # Todos van a "unknown"
        assert UNKNOWN_BUCKET in grouped


# ════════════════════════════════════════════════════════════════════
#  3. failure_rate_by_category
# ════════════════════════════════════════════════════════════════════
class TestFailureRate:

    def test_counts_correctly(self):
        m = failure_rate_by_category(_failure_set())
        # 4 failures (2 payment + 1 network + 1 anti_bot, flow excluded)
        assert m.data["_total_failures"] == 4
        assert m.data["payment"]["count"] == 2
        assert m.data["network"]["count"] == 1
        assert m.data["anti_bot"]["count"] == 1

    def test_rates_sum_to_one(self):
        m = failure_rate_by_category(_failure_set())
        rates = [v["rate"] for k, v in m.data.items()
                  if isinstance(v, dict)]
        assert abs(sum(rates) - 1.0) < 1e-6

    def test_excludes_operational_events(self):
        m = failure_rate_by_category(_failure_set())
        # flow/flow_started y flow/phase_transition NO cuentan
        assert "flow" not in m.data

    def test_empty_input(self):
        m = failure_rate_by_category([])
        assert m.data["_total_failures"] == 0

    def test_metric_name_canonical(self):
        m = failure_rate_by_category(_failure_set())
        assert m.name == "failure_rate_by_category"


# ════════════════════════════════════════════════════════════════════
#  4. step_failure_distribution
# ════════════════════════════════════════════════════════════════════
class TestStepDistribution:

    def test_groups_by_context(self):
        m = step_failure_distribution(_failure_set())
        assert "submit_payment" in m.data
        assert "attach_cart" in m.data

    def test_counts_per_context(self):
        m = step_failure_distribution(_failure_set())
        assert m.data["submit_payment"]["_total"] == 2
        assert m.data["submit_payment"]["payment"] == 2

    def test_empty_context_uses_unknown(self):
        evs = [_ev("network", "timeout", context="")]
        m = step_failure_distribution(evs)
        assert UNKNOWN_BUCKET in m.data

    def test_excludes_operational(self):
        m = step_failure_distribution(_failure_set())
        # context "start_flow" sólo contiene operacional → no aparece
        assert "start_flow" not in m.data


# ════════════════════════════════════════════════════════════════════
#  5. cart_lifecycle_heatmap
# ════════════════════════════════════════════════════════════════════
class TestCartLifecycleHeatmap:

    def test_groups_by_phase(self):
        m = cart_lifecycle_heatmap(_failure_set())
        assert "checking_out" in m.data
        assert "init" in m.data

    def test_phase_x_category(self):
        m = cart_lifecycle_heatmap(_failure_set())
        # checking_out tuvo 2 payment failures
        assert m.data["checking_out"]["payment"] == 2

    def test_missing_phase_falls_to_unknown(self):
        evs = [_ev("network", "timeout")]  # sin phase
        m = cart_lifecycle_heatmap(evs)
        assert UNKNOWN_BUCKET in m.data

    def test_phase_field_supports_both_names(self):
        # payload.phase y payload.cart_phase deben ser equivalentes
        ev1 = _ev("payment", "card_declined", phase="paid")
        ev2 = {**_ev("payment", "card_declined"),
                "payload": {"phase": "paid"}}
        m1 = cart_lifecycle_heatmap([ev1])
        m2 = cart_lifecycle_heatmap([ev2])
        assert "paid" in m1.data
        assert "paid" in m2.data


# ════════════════════════════════════════════════════════════════════
#  6. correlation
# ════════════════════════════════════════════════════════════════════
class TestCorrelation:

    def test_correlate_by_cart_id(self):
        m = correlate_by_cart_id(_failure_set())
        # C1, C2, C3 + empty (unknown) = 4 únicos
        assert m.meta["unique_cart_ids"] == 4
        assert "C1" in m.data

    def test_cart_id_group_contains_all_events(self):
        m = correlate_by_cart_id(_failure_set())
        # C1 tuvo 2 eventos (1 payment + 1 phase_transition)
        assert m.data["C1"]["count"] == 2

    def test_correlate_by_idem_key(self):
        m = correlate_by_idempotency_key(_failure_set())
        # ik-1 a ik-5 = 5 únicos
        assert m.meta["unique_idem_keys"] == 5

    def test_idem_key_groups_categories(self):
        m = correlate_by_idempotency_key(_failure_set())
        # ik-1 tuvo payment + flow → 2 categorías
        assert set(m.data["ik-1"]["categories"]) == {"payment", "flow"}


# ════════════════════════════════════════════════════════════════════
#  7. severity_breakdown
# ════════════════════════════════════════════════════════════════════
class TestSeverityBreakdown:

    def test_counts_all_severities(self):
        m = severity_breakdown(_failure_set())
        # 2 fatal + 1 retryable + 1 non_recoverable + 2 recoverable
        assert m.data["fatal"] == 2
        assert m.data["retryable"] == 1
        assert m.data["non_recoverable"] == 1
        assert m.data["recoverable"] == 2

    def test_total_matches(self):
        evs = _failure_set()
        m = severity_breakdown(evs)
        assert sum(m.data.values()) == len(evs)

    def test_empty_input(self):
        m = severity_breakdown([])
        assert m.data == {}


# ════════════════════════════════════════════════════════════════════
#  8. aggregate_all
# ════════════════════════════════════════════════════════════════════
class TestAggregateAll:

    def test_contains_all_six_metrics(self):
        r = aggregate_all(_failure_set())
        assert set(r.keys()) == {
            "failure_rate_by_category",
            "step_failure_distribution",
            "cart_lifecycle_heatmap",
            "severity_breakdown",
            "correlation_by_cart_id",
            "correlation_by_idem_key",
        }

    def test_all_metrics_json_serializable(self):
        r = aggregate_all(_failure_set())
        s = json.dumps(r)
        assert isinstance(s, str) and len(s) > 0

    def test_aggregate_all_from_file(self, tmp_path):
        p = tmp_path / "ev.jsonl"
        with p.open("w") as f:
            for e in _failure_set():
                f.write(json.dumps(e) + "\n")
        r = aggregate_all(p)
        assert r["failure_rate_by_category"]["data"]["_total_failures"] == 4


# ════════════════════════════════════════════════════════════════════
#  9. DETERMINISM + FAIL-SAFE
# ════════════════════════════════════════════════════════════════════
class TestDeterminismFailSafe:

    def test_same_input_same_output(self):
        evs = _failure_set()
        r1 = aggregate_all(evs)
        r2 = aggregate_all(evs)
        assert r1 == r2

    def test_malformed_events_ignored(self):
        evs = _failure_set() + [{"invalid": "structure"}, None, "string", 42]
        m = failure_rate_by_category(evs)
        # Mismo count que sin la basura
        assert m.data["_total_failures"] == 4

    def test_failure_categories_constant(self):
        assert FAILURE_CATEGORIES == frozenset({
            "network", "anti_bot", "cart_lifecycle", "inventory",
            "queue", "payment",
        })

    def test_aggregated_metric_to_dict(self):
        m = failure_rate_by_category(_failure_set())
        d = m.to_dict()
        assert set(d.keys()) == {"name", "total_events", "data", "meta"}
        assert isinstance(d["total_events"], int)
