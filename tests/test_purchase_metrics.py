"""tests/test_purchase_metrics.py — Test suite for STEP 5.

Cubre el módulo de métricas + el blueprint Flask.

Categorías:
    1. Loaders                       (5 tests)
    2. Percentile helper             (4 tests)
    3. success_rate                  (4 tests)
    4. latency_percentiles           (4 tests)
    5. cart_lifetime_distribution    (3 tests)
    6. idempotency_collision_rate    (4 tests)
    7. burn_rate_per_day             (4 tests)
    8. compute_purchase_metrics      (5 tests)
    9. Flask blueprint               (3 tests)

stdlib + pytest only. Aislado en tmp_path.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.observability.metrics import (
    SINK_GLOB,
    burn_rate_per_day,
    cart_lifetime_distribution,
    compute_purchase_metrics,
    idempotency_collision_rate,
    latency_percentiles,
    list_idempotency_keys,
    load_all_events,
    step_latencies,
    success_rate,
    _percentile,
)
from core.observability.metrics_api import (
    create_metrics_blueprint,
    is_flask_available,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
BASE_TS = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)


def _ev(code: str, cart_id: str, *,
        ts_offset_sec: int = 0,
        category: str = "flow",
        status: str = "",
        context: str = "",
        severity: str = "recoverable",
        idem: str = "") -> dict:
    ts = BASE_TS + timedelta(seconds=ts_offset_sec)
    payload = {"status": status} if status else {}
    return {
        "category": category,
        "code": code,
        "context": context,
        "cart_id": cart_id,
        "idempotency_key": idem or f"ik-{cart_id}",
        "timestamp": ts.isoformat(),
        "payload": payload,
        "severity": severity,
        "message": "",
        "exception_class": "",
    }


@pytest.fixture
def sink_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def idem_root(tmp_path):
    d = tmp_path / "idem"
    d.mkdir()
    return d


def _write_sink(sink_dir: Path, events: list, pid: int = 1) -> Path:
    p = sink_dir / f"structured_events_pid{pid}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return p


def _make_idem(root: Path, key: str, *, intent: bool = True,
                outcome: bool = True) -> None:
    d = root / key
    d.mkdir(parents=True, exist_ok=True)
    if intent:
        (d / "intent.json").write_text("{}", encoding="utf-8")
    if outcome:
        (d / "outcome.json").write_text("{}", encoding="utf-8")


# ════════════════════════════════════════════════════════════════════
#  1. LOADERS
# ════════════════════════════════════════════════════════════════════
class TestLoaders:

    def test_load_all_events_from_single_file(self, sink_dir):
        _write_sink(sink_dir, [_ev("flow_started", "C1")])
        evs = load_all_events(sink_dir)
        assert len(evs) == 1

    def test_load_all_events_multiple_files(self, sink_dir):
        _write_sink(sink_dir, [_ev("flow_started", "C1")], pid=1)
        _write_sink(sink_dir, [_ev("flow_started", "C2")], pid=2)
        evs = load_all_events(sink_dir)
        assert len(evs) == 2

    def test_load_all_events_empty_dir(self, sink_dir):
        assert load_all_events(sink_dir) == []

    def test_load_all_events_filters_by_window(self, sink_dir):
        events = [
            _ev("flow_started", f"C{i}", ts_offset_sec=i * 3600)
            for i in range(5)
        ]
        _write_sink(sink_dir, events)
        # Ventana: últimas 2 horas desde BASE_TS + 5h
        since = BASE_TS + timedelta(hours=3)
        evs = load_all_events(sink_dir, since=since)
        # Eventos con ts_offset_sec >= 3h (10800s)
        assert len(evs) == 2  # i=3 y i=4

    def test_list_idempotency_keys(self, idem_root):
        _make_idem(idem_root, "ik-a")
        _make_idem(idem_root, "ik-b")
        assert sorted(list_idempotency_keys(idem_root)) == ["ik-a", "ik-b"]


# ════════════════════════════════════════════════════════════════════
#  2. PERCENTILE HELPER
# ════════════════════════════════════════════════════════════════════
class TestPercentile:

    def test_empty_list(self):
        assert _percentile([], 50) == 0.0

    def test_single_value(self):
        assert _percentile([42.0], 95) == 42.0

    def test_p50_of_sorted_range(self):
        assert _percentile([1, 2, 3, 4, 5], 50) == 3.0

    def test_p95_interpolates(self):
        # 100 valores 0..99 → p95 ≈ 94.05
        sorted_vals = list(range(100))
        p95 = _percentile([float(v) for v in sorted_vals], 95.0)
        assert 94.0 <= p95 <= 95.0


# ════════════════════════════════════════════════════════════════════
#  3. success_rate
# ════════════════════════════════════════════════════════════════════
class TestSuccessRate:

    def test_all_paid_rate_one(self):
        evs = [_ev("outcome_recorded", f"C{i}", status="paid")
                for i in range(5)]
        r = success_rate(evs)
        assert r["rate"] == 1.0
        assert r["paid"] == 5
        assert r["total_outcomes"] == 5

    def test_half_paid(self):
        evs = [
            _ev("outcome_recorded", "C1", status="paid"),
            _ev("outcome_recorded", "C2", status="payment_failed"),
        ]
        r = success_rate(evs)
        assert r["rate"] == 0.5

    def test_no_outcomes(self):
        r = success_rate([])
        assert r["rate"] == 0.0
        assert r["total_outcomes"] == 0

    def test_ignores_non_outcome_events(self):
        evs = [
            _ev("outcome_recorded", "C1", status="paid"),
            _ev("flow_started", "C2"),
            _ev("phase_transition", "C3"),
        ]
        r = success_rate(evs)
        assert r["total_outcomes"] == 1


# ════════════════════════════════════════════════════════════════════
#  4. latency_percentiles
# ════════════════════════════════════════════════════════════════════
class TestLatencyPercentiles:

    def test_pairs_start_and_outcome(self):
        evs = [
            _ev("flow_started", "C1", ts_offset_sec=0, idem="ik-1"),
            _ev("outcome_recorded", "C1", ts_offset_sec=2,
                idem="ik-1"),
        ]
        r = latency_percentiles(evs)
        assert r["count"] == 1
        assert r["min_ms"] == 2000.0

    def test_no_pairs_returns_zeros(self):
        evs = [_ev("flow_started", "C1")]
        r = latency_percentiles(evs)
        assert r["count"] == 0
        assert r["p50_ms"] == 0.0

    def test_multiple_latencies_sorted_correctly(self):
        evs = []
        for i, delta in enumerate([1, 5, 10, 20]):
            evs.append(_ev("flow_started", f"C{i}",
                            ts_offset_sec=i * 100, idem=f"ik-{i}"))
            evs.append(_ev("outcome_recorded", f"C{i}",
                            ts_offset_sec=i * 100 + delta,
                            idem=f"ik-{i}"))
        r = latency_percentiles(evs)
        assert r["count"] == 4
        assert r["min_ms"] == 1000.0
        assert r["max_ms"] == 20000.0

    def test_step_latencies_returns_list(self):
        evs = [
            _ev("flow_started", "C1", ts_offset_sec=0, idem="ik-1"),
            _ev("outcome_recorded", "C1", ts_offset_sec=3,
                idem="ik-1"),
        ]
        lats = step_latencies(evs)
        assert lats == [3000.0]


# ════════════════════════════════════════════════════════════════════
#  5. cart_lifetime_distribution
# ════════════════════════════════════════════════════════════════════
class TestCartLifetime:

    def test_returns_seconds_not_ms(self):
        evs = [
            _ev("flow_started", "C1", ts_offset_sec=0, idem="ik-1"),
            _ev("outcome_recorded", "C1", ts_offset_sec=10,
                idem="ik-1"),
        ]
        r = cart_lifetime_distribution(evs)
        assert r["min_sec"] == 10.0

    def test_empty(self):
        r = cart_lifetime_distribution([])
        assert r["count"] == 0

    def test_p50_and_p95_keys(self):
        evs = [
            _ev("flow_started", "C1", ts_offset_sec=0, idem="ik-1"),
            _ev("outcome_recorded", "C1", ts_offset_sec=5,
                idem="ik-1"),
        ]
        r = cart_lifetime_distribution(evs)
        assert "p50_sec" in r
        assert "p95_sec" in r


# ════════════════════════════════════════════════════════════════════
#  6. idempotency_collision_rate
# ════════════════════════════════════════════════════════════════════
class TestIdempotencyCollisionRate:

    def test_empty_root_zero_rate(self, idem_root):
        r = idempotency_collision_rate(idem_root)
        assert r["total"] == 0
        assert r["orphan_rate"] == 0.0

    def test_all_committed_zero_orphan(self, idem_root):
        for k in ("ik-a", "ik-b", "ik-c"):
            _make_idem(idem_root, k, intent=True, outcome=True)
        r = idempotency_collision_rate(idem_root)
        assert r["total"] == 3
        assert r["committed"] == 3
        assert r["orphan"] == 0

    def test_orphan_intent_detected(self, idem_root):
        _make_idem(idem_root, "ik-orph", intent=True, outcome=False)
        _make_idem(idem_root, "ik-done", intent=True, outcome=True)
        r = idempotency_collision_rate(idem_root)
        assert r["orphan"] == 1
        assert r["committed"] == 1
        assert r["orphan_rate"] == 0.5

    def test_nonexistent_root_safe(self, tmp_path):
        r = idempotency_collision_rate(tmp_path / "missing")
        assert r["total"] == 0


# ════════════════════════════════════════════════════════════════════
#  7. burn_rate_per_day
# ════════════════════════════════════════════════════════════════════
class TestBurnRate:

    def test_nonexistent_file_zero(self, tmp_path):
        r = burn_rate_per_day(tmp_path / "missing.json")
        assert r["total"] == 0
        assert r["per_day"] is None

    def test_list_format_counts_total_only(self, tmp_path):
        p = tmp_path / "burned.json"
        p.write_text(json.dumps(["a", "b", "c"]))
        r = burn_rate_per_day(p)
        assert r["total"] == 3
        assert r["per_day"] is None

    def test_dict_format_with_timestamps_computes_rate(self, tmp_path):
        p = tmp_path / "burned.json"
        # 5 accounts burned over 2 days
        p.write_text(json.dumps({
            "acc1": {"burned_at": "2026-05-12T10:00:00+00:00"},
            "acc2": {"burned_at": "2026-05-12T11:00:00+00:00"},
            "acc3": {"burned_at": "2026-05-13T10:00:00+00:00"},
            "acc4": {"burned_at": "2026-05-13T15:00:00+00:00"},
            "acc5": {"burned_at": "2026-05-14T09:00:00+00:00"},
        }))
        now = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
        r = burn_rate_per_day(p, now=now)
        assert r["total"] == 5
        # window_days = 2 (de 2026-05-12T10:00 a 2026-05-14T10:00)
        assert 1.9 < r["window_days"] < 2.1
        # per_day = 5 / 2 = 2.5
        assert 2.0 < r["per_day"] < 3.0

    def test_corrupt_json_safe(self, tmp_path):
        p = tmp_path / "burned.json"
        p.write_text("{ not json")
        r = burn_rate_per_day(p)
        assert r["total"] == 0


# ════════════════════════════════════════════════════════════════════
#  8. compute_purchase_metrics (top-level)
# ════════════════════════════════════════════════════════════════════
class TestComputeMetrics:

    def test_returns_canonical_schema(self, sink_dir, idem_root):
        r = compute_purchase_metrics(
            sink_dir=sink_dir, idem_root=idem_root,
            window_hours=None,
            burned_accounts_file=sink_dir / "missing1.json",
            burned_subnets_file=sink_dir / "missing2.json",
        )
        assert set(r.keys()) >= {
            "window_hours", "as_of", "events_in_window",
            "success_rate_24h", "failure_pareto", "latency",
            "cart_lifecycle", "idempotency",
            "account_burn_rate_per_day", "subnet_burn_rate_per_day",
        }

    def test_window_filter_applied(self, sink_dir, idem_root):
        old = _ev("outcome_recorded", "C1", ts_offset_sec=0, status="paid")
        new = _ev("outcome_recorded", "C2",
                   ts_offset_sec=3600 * 2, status="paid")
        _write_sink(sink_dir, [old, new])
        # Ventana 1h desde BASE_TS + 2.5h → solo `new`
        now = BASE_TS + timedelta(hours=2, minutes=30)
        r = compute_purchase_metrics(
            sink_dir=sink_dir, idem_root=idem_root,
            window_hours=1.0, now=now,
            burned_accounts_file=sink_dir / "x1.json",
            burned_subnets_file=sink_dir / "x2.json",
        )
        # Only "new" event survives the window
        assert r["events_in_window"] == 1

    def test_with_real_events_and_idem_log(self, sink_dir, idem_root):
        events = []
        for i in range(3):
            events.append(_ev("flow_started", f"C{i}",
                               ts_offset_sec=i * 60, idem=f"ik-{i}"))
            events.append(_ev("outcome_recorded", f"C{i}",
                               ts_offset_sec=i * 60 + 5,
                               status="paid", idem=f"ik-{i}"))
        _write_sink(sink_dir, events)
        for i in range(3):
            _make_idem(idem_root, f"ik-{i}")
        r = compute_purchase_metrics(
            sink_dir=sink_dir, idem_root=idem_root,
            window_hours=None,
            burned_accounts_file=sink_dir / "x1.json",
            burned_subnets_file=sink_dir / "x2.json",
        )
        assert r["success_rate_24h"]["rate"] == 1.0
        assert r["latency"]["count"] == 3
        assert r["idempotency"]["total"] == 3

    def test_json_serializable(self, sink_dir, idem_root):
        r = compute_purchase_metrics(
            sink_dir=sink_dir, idem_root=idem_root,
            burned_accounts_file=sink_dir / "x1.json",
            burned_subnets_file=sink_dir / "x2.json",
        )
        s = json.dumps(r)
        assert isinstance(s, str) and len(s) > 0

    def test_deterministic(self, sink_dir, idem_root):
        events = [
            _ev("flow_started", "C1", ts_offset_sec=0, idem="ik-1"),
            _ev("outcome_recorded", "C1", ts_offset_sec=3,
                status="paid", idem="ik-1"),
        ]
        _write_sink(sink_dir, events)
        _make_idem(idem_root, "ik-1")
        now = BASE_TS + timedelta(hours=1)
        r1 = compute_purchase_metrics(
            sink_dir=sink_dir, idem_root=idem_root,
            window_hours=None, now=now,
            burned_accounts_file=sink_dir / "x1.json",
            burned_subnets_file=sink_dir / "x2.json",
        )
        r2 = compute_purchase_metrics(
            sink_dir=sink_dir, idem_root=idem_root,
            window_hours=None, now=now,
            burned_accounts_file=sink_dir / "x1.json",
            burned_subnets_file=sink_dir / "x2.json",
        )
        assert r1 == r2


# ════════════════════════════════════════════════════════════════════
#  9. FLASK BLUEPRINT
# ════════════════════════════════════════════════════════════════════
class TestFlaskBlueprint:

    @pytest.mark.skipif(not is_flask_available(),
                         reason="flask not available")
    def test_create_blueprint_returns_object(self):
        bp = create_metrics_blueprint()
        assert bp is not None

    @pytest.mark.skipif(not is_flask_available(),
                         reason="flask not available")
    def test_blueprint_responds_to_health(self):
        from flask import Flask
        app = Flask(__name__)
        bp = create_metrics_blueprint()
        app.register_blueprint(bp)
        with app.test_client() as client:
            r = client.get("/api/purchase/metrics/healthz")
            assert r.status_code == 200
            data = r.get_json()
            assert data["ok"] is True

    @pytest.mark.skipif(not is_flask_available(),
                         reason="flask not available")
    def test_blueprint_metrics_endpoint_returns_json(self):
        from flask import Flask
        app = Flask(__name__)
        bp = create_metrics_blueprint()
        app.register_blueprint(bp)
        with app.test_client() as client:
            r = client.get("/api/purchase/metrics")
            assert r.status_code == 200
            data = r.get_json()
            assert "success_rate_24h" in data
            assert "failure_pareto" in data
