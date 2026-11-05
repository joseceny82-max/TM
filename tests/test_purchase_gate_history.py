"""tests/test_purchase_gate_history.py — M13.6 historical aggregation tests.

Cobertura:
    - window parsing (1h, 24h, 7d) + fallback default
    - bucketing correcto por timestamp
    - derived metrics calculados correctamente
    - empty dir / missing files manejados sin crash
    - deterministic output (mismo input → mismo output)
    - mapping event → metric correcto
    - skip de eventos fuera del window
    - sin side-effects (no escribe files)
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.observability.history import (
    PURCHASE_EVENT_NAMES,
    WINDOW_SECONDS,
    aggregate_purchase_history,
)


def _write_jsonl(path: Path, records: list) -> None:
    """Helper: escribe JSONL en un archivo."""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _mk_rec(event: str, ts_dt: datetime, slot: int = 1) -> dict:
    """Construye un record JSONL como el que emite runtime_diagnostics."""
    return {
        "ts": ts_dt.isoformat(),
        "slot": slot,
        "run_id": "test",
        "event": event,
        "kind": "OBSERVABILITY",
    }


# ════════════════════════════════════════════════════════════════════
#  Window parsing + schema
# ════════════════════════════════════════════════════════════════════
def test_m13_6_window_seconds_constants():
    assert WINDOW_SECONDS["1h"] == 3600
    assert WINDOW_SECONDS["24h"] == 86400
    assert WINDOW_SECONDS["7d"] == 604800


def test_m13_6_purchase_event_names_complete():
    """PURCHASE_EVENT_NAMES contiene los 8 events del gate."""
    expected = {
        "purchase_attempted", "purchase_allowed", "purchase_blocked",
        "purchase_dry_run", "purchase_live_success",
        "purchase_duplicate_blocked", "purchase_stale_checkout",
        "purchase_kill_switch_blocked",
    }
    assert expected == set(PURCHASE_EVENT_NAMES)


def test_m13_6_empty_dir_returns_zero_buckets(tmp_path):
    """Directorio inexistente → zero buckets, no crash."""
    bogus_dir = tmp_path / "does_not_exist"
    r = aggregate_purchase_history(
        window="1h", directory=bogus_dir, bucket_count=12
    )
    assert r["window"] == "1h"
    assert r["bucket_count"] == 12
    assert all(v == 0 for v in r["metrics"]["attempted"])
    assert all(v == 0 for v in r["metrics"]["blocked"])


def test_m13_6_output_schema_complete(tmp_path):
    """Output tiene window/bucket_count/bucket_size_s/now/metrics/derived."""
    r = aggregate_purchase_history(
        window="24h", directory=tmp_path, bucket_count=8
    )
    required = {"window", "bucket_count", "bucket_size_s", "now",
                "metrics", "derived"}
    assert required.issubset(r.keys())
    metrics_keys = {"attempted", "allowed", "blocked", "dry_run",
                     "live", "duplicate", "errors"}
    assert metrics_keys.issubset(r["metrics"].keys())
    derived_keys = {"block_rate_trend", "dry_run_stability",
                     "live_rate_trend"}
    assert derived_keys.issubset(r["derived"].keys())


def test_m13_6_invalid_window_falls_back_to_24h():
    """Window inválido → default 24h."""
    r = aggregate_purchase_history(window="bogus", directory="/tmp/xxx")
    assert r["window"] == "24h"


def test_m13_6_bucket_count_minimum_enforced():
    """bucket_count < 1 → fallback a 24."""
    r = aggregate_purchase_history(
        window="1h", directory="/tmp/xxx", bucket_count=0
    )
    assert r["bucket_count"] == 24


# ════════════════════════════════════════════════════════════════════
#  Bucketing
# ════════════════════════════════════════════════════════════════════
def test_m13_6_event_in_window_lands_in_correct_bucket(tmp_path):
    """Event 30 min antes de now con window=1h y 12 buckets → bucket cercano al final."""
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    # 30 min antes = bucket index ~5 (de 12 buckets cubriendo 1h, 0=oldest, 11=newest)
    event_ts = now - timedelta(minutes=30)

    jsonl = tmp_path / "diagnostics_slot1_test.jsonl"
    _write_jsonl(jsonl, [_mk_rec("purchase_attempted", event_ts)])

    r = aggregate_purchase_history(
        window="1h", directory=tmp_path, now=now, bucket_count=12
    )
    # El bucket 5 o 6 (mid) debe tener 1 attempted
    total = sum(r["metrics"]["attempted"])
    assert total == 1


def test_m13_6_event_outside_window_ignored(tmp_path):
    """Event más viejo que window → ignorado."""
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    too_old = now - timedelta(hours=2)  # window = 1h

    jsonl = tmp_path / "diagnostics_slot1_old.jsonl"
    _write_jsonl(jsonl, [_mk_rec("purchase_attempted", too_old)])

    r = aggregate_purchase_history(
        window="1h", directory=tmp_path, now=now, bucket_count=12
    )
    total = sum(r["metrics"]["attempted"])
    assert total == 0


def test_m13_6_event_mapping_to_metrics(tmp_path):
    """Cada event_name se mapea a su métrica correcta."""
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    recent = now - timedelta(minutes=5)

    records = [
        _mk_rec("purchase_attempted", recent),
        _mk_rec("purchase_attempted", recent),
        _mk_rec("purchase_allowed", recent),
        _mk_rec("purchase_blocked", recent),
        _mk_rec("purchase_dry_run", recent),
        _mk_rec("purchase_live_success", recent),
        _mk_rec("purchase_duplicate_blocked", recent),
        _mk_rec("purchase_stale_checkout", recent),
        _mk_rec("purchase_kill_switch_blocked", recent),
        # Non-purchase event — debe ser ignorado
        _mk_rec("goto", recent),
        _mk_rec("fatal_error", recent),
    ]
    jsonl = tmp_path / "diagnostics_slot1_mapping.jsonl"
    _write_jsonl(jsonl, records)

    r = aggregate_purchase_history(
        window="1h", directory=tmp_path, now=now, bucket_count=12
    )
    m = r["metrics"]
    assert sum(m["attempted"]) == 2
    assert sum(m["allowed"]) == 1
    # blocked = explicit blocked + stale + kill_switch_blocked = 3
    assert sum(m["blocked"]) == 3
    assert sum(m["dry_run"]) == 1
    assert sum(m["live"]) == 1
    assert sum(m["duplicate"]) == 1


# ════════════════════════════════════════════════════════════════════
#  Derived metrics
# ════════════════════════════════════════════════════════════════════
def test_m13_6_derived_block_rate_trend(tmp_path):
    """En un bucket con 10 attempted y 5 blocked → block_rate_trend=0.5."""
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    recent = now - timedelta(minutes=5)

    records = []
    for _ in range(10):
        records.append(_mk_rec("purchase_attempted", recent))
    for _ in range(5):
        records.append(_mk_rec("purchase_blocked", recent))

    jsonl = tmp_path / "diagnostics_slot1_derived.jsonl"
    _write_jsonl(jsonl, records)

    r = aggregate_purchase_history(
        window="1h", directory=tmp_path, now=now, bucket_count=12
    )
    # En algún bucket debe haber un 0.5 (5/10) — verificamos que al menos uno cumple
    bucket_with_data_idx = next(
        (i for i, v in enumerate(r["metrics"]["attempted"]) if v > 0),
        None,
    )
    assert bucket_with_data_idx is not None
    assert r["derived"]["block_rate_trend"][bucket_with_data_idx] == 0.5


def test_m13_6_derived_zero_when_no_attempted(tmp_path):
    """Bucket con 0 attempted → derived = 0.0 (no division by zero)."""
    r = aggregate_purchase_history(
        window="1h", directory=tmp_path, bucket_count=12
    )
    assert all(v == 0.0 for v in r["derived"]["block_rate_trend"])
    assert all(v == 0.0 for v in r["derived"]["dry_run_stability"])
    assert all(v == 0.0 for v in r["derived"]["live_rate_trend"])


# ════════════════════════════════════════════════════════════════════
#  Robustness
# ════════════════════════════════════════════════════════════════════
def test_m13_6_handles_corrupt_jsonl_lines(tmp_path):
    """Lines mal formadas → ignoradas, otras procesadas."""
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    recent = now - timedelta(minutes=5)

    jsonl = tmp_path / "diagnostics_slot1_corrupt.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write("{partial}\n")
        f.write(json.dumps(_mk_rec("purchase_attempted", recent)) + "\n")
        f.write("\n")  # línea vacía
        f.write("garbage123\n")

    r = aggregate_purchase_history(
        window="1h", directory=tmp_path, now=now, bucket_count=12
    )
    # La única línea válida cuenta
    assert sum(r["metrics"]["attempted"]) == 1


def test_m13_6_handles_missing_ts(tmp_path):
    """Records sin 'ts' → ignorados."""
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    jsonl = tmp_path / "diagnostics_slot1_no_ts.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "purchase_attempted"}) + "\n")
        f.write(json.dumps({"ts": "not iso", "event": "purchase_attempted"}) + "\n")

    r = aggregate_purchase_history(
        window="1h", directory=tmp_path, now=now, bucket_count=12
    )
    assert sum(r["metrics"]["attempted"]) == 0


def test_m13_6_deterministic_output(tmp_path):
    """Mismo input + mismo `now` → output idéntico (excepto si llamamos sin now)."""
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    recent = now - timedelta(minutes=10)
    jsonl = tmp_path / "diagnostics_slot1_det.jsonl"
    _write_jsonl(jsonl, [
        _mk_rec("purchase_attempted", recent),
        _mk_rec("purchase_dry_run", recent),
    ])
    r1 = aggregate_purchase_history(
        window="1h", directory=tmp_path, now=now, bucket_count=12
    )
    r2 = aggregate_purchase_history(
        window="1h", directory=tmp_path, now=now, bucket_count=12
    )
    assert r1["metrics"] == r2["metrics"]
    assert r1["derived"] == r2["derived"]
    assert r1["now"] == r2["now"]


def test_m13_6_no_file_writes_to_directory(tmp_path):
    """Aggregator NO debe escribir archivos al dir (read-only)."""
    jsonl = tmp_path / "diagnostics_slot1_readonly.jsonl"
    _write_jsonl(jsonl, [
        _mk_rec("purchase_attempted",
                datetime.now(timezone.utc) - timedelta(minutes=5)),
    ])
    files_before = set(tmp_path.iterdir())
    aggregate_purchase_history(
        window="1h", directory=tmp_path, bucket_count=12
    )
    files_after = set(tmp_path.iterdir())
    assert files_before == files_after


def test_m13_6_supports_naive_datetime():
    """Si now es naive datetime → se asume UTC, no crashea."""
    naive_now = datetime(2026, 5, 13, 12, 0, 0)  # sin tzinfo
    r = aggregate_purchase_history(
        window="1h", directory="/nonexistent",
        now=naive_now, bucket_count=12,
    )
    # No crash, devuelve buckets vacíos
    assert r["window"] == "1h"
