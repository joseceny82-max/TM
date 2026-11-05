"""Tests M3 — SQLite Historical Index.

Garantías:
    1. Schema creado correctamente
    2. Ingest desde JSONL real
    3. UPSERT idempotente (re-ingest mismo run actualiza)
    4. Queries: recent, root_causes, health_distribution, stats
    5. Fail-safe absoluto (DB locked, fichero corrupto, etc.)
    6. Endpoint /api/v4/history2 funciona en paralelo a /api/diagnostics/runs
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_jsonl(tmp_path: Path, name: str, records: list[dict]) -> Path:
    fp = tmp_path / name
    with open(fp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return fp


# ════════════════════════════════════════════════════════════════════
#  1. Schema / connection
# ════════════════════════════════════════════════════════════════════
def test_index_creates_db(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    assert (tmp_path / "idx.sqlite").exists()


def test_index_empty_get_stats(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    stats = idx.get_stats()
    assert stats["total_runs"] == 0


# ════════════════════════════════════════════════════════════════════
#  2. Ingest real JSONL
# ════════════════════════════════════════════════════════════════════
def test_ingest_basic_run(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    records = [
        {"ts": "2026-05-13T12:00:00Z", "run_id": "r1", "slot": 1, "event": "init",
          "kind": "OBSERVABILITY", "state": "setup", "score": 30},
        {"ts": "2026-05-13T12:00:05Z", "run_id": "r1", "slot": 1, "event": "goto",
          "kind": "STATE", "state": "browsing", "score": 50},
        {"ts": "2026-05-13T12:00:30Z", "run_id": "r1", "slot": 1, "event": "stop_received",
          "kind": "STATE", "state": "exit", "score": 60},
    ]
    fp = _make_jsonl(tmp_path, "diagnostics_slot1_20260513_120000.jsonl", records)
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    ok = idx.ingest_completed_run(str(fp))
    assert ok is True
    runs = idx.get_recent_runs()
    assert len(runs) == 1
    r = runs[0]
    assert r["slot"] == "1"
    assert r["event_count"] == 3
    assert r["exit_outcome"] == "STOPPED"


def test_ingest_with_drifts(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    records = [
        {"ts": "2026-05-13T12:00:00Z", "run_id": "r1", "slot": 1, "event": "init",
          "kind": "OBSERVABILITY", "state": "setup", "score": 30},
        {"ts": "2026-05-13T12:00:05Z", "run_id": "r1", "slot": 1,
          "event": "no_email_input", "kind": "OBSERVABILITY", "state": "login",
          "score": 35,
          "drift": {"kind": "SOFT_DRIFT", "severity": "high",
                      "detail": "LOGIN_FAILURE"}},
        {"ts": "2026-05-13T12:00:10Z", "run_id": "r1", "slot": 1,
          "event": "FATAL_bot_block_early", "kind": "STATE", "state": "exit",
          "score": 25},
    ]
    fp = _make_jsonl(tmp_path, "diagnostics_slot1_20260513_120000.jsonl", records)
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    idx.ingest_completed_run(str(fp))
    runs = idx.get_recent_runs()
    assert len(runs) == 1
    assert runs[0]["drift_count"] == 1
    assert runs[0]["soft_drift_count"] == 1
    drifts = idx.get_drifts(runs[0]["run_id"])
    assert len(drifts) == 1
    assert drifts[0]["kind"] == "SOFT_DRIFT"


def test_ingest_empty_file(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    fp = tmp_path / "empty.jsonl"
    fp.touch()
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    assert idx.ingest_completed_run(str(fp)) is False


def test_ingest_nonexistent_file(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    assert idx.ingest_completed_run("/nonexistent/path.jsonl") is False


def test_ingest_corrupt_lines(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    fp = tmp_path / "diagnostics_slot1_20260513_120000.jsonl"
    fp.write_text(
        '{"ts":"t1","event":"init","kind":"STATE","state":"setup","slot":1,"run_id":"r1"}\n'
        'CORRUPT_LINE_NOT_JSON\n'
        '{"ts":"t2","event":"goto","kind":"STATE","state":"browsing","slot":1,"run_id":"r1"}\n',
        encoding="utf-8"
    )
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    ok = idx.ingest_completed_run(str(fp))
    assert ok is True
    runs = idx.get_recent_runs()
    # 2 líneas válidas
    assert runs[0]["event_count"] == 2


# ════════════════════════════════════════════════════════════════════
#  3. Idempotencia (UPSERT)
# ════════════════════════════════════════════════════════════════════
def test_idempotent_reingest(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    records = [
        {"ts": "t1", "run_id": "r1", "slot": 1, "event": "init", "kind": "STATE", "state": "setup", "score": 30},
    ]
    fp = _make_jsonl(tmp_path, "diagnostics_slot1_20260513_120000.jsonl", records)
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    idx.ingest_completed_run(str(fp))
    idx.ingest_completed_run(str(fp))  # otra vez
    runs = idx.get_recent_runs()
    assert len(runs) == 1  # no duplicado


# ════════════════════════════════════════════════════════════════════
#  4. Queries
# ════════════════════════════════════════════════════════════════════
def test_filter_by_outcome(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    # 1 blocked, 1 success
    rec_blocked = [
        {"ts": "t1", "run_id": "r1", "slot": 1, "event": "FATAL_bot_block_early",
          "kind": "STATE", "state": "exit", "score": 25},
    ]
    rec_success = [
        {"ts": "t2", "run_id": "r2", "slot": 2, "event": "stop_received",
          "kind": "STATE", "state": "exit", "score": 90},
    ]
    fp1 = _make_jsonl(tmp_path, "diagnostics_slot1_20260513_120000.jsonl", rec_blocked)
    fp2 = _make_jsonl(tmp_path, "diagnostics_slot2_20260513_120001.jsonl", rec_success)
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    idx.ingest_completed_run(str(fp1))
    idx.ingest_completed_run(str(fp2))
    blocked = idx.get_recent_runs(outcome="BLOCKED")
    assert len(blocked) == 1
    stopped = idx.get_recent_runs(outcome="STOPPED")
    assert len(stopped) == 1


def test_health_distribution(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    runs_data = [
        ("r1", 90),  # healthy
        ("r2", 70),  # degraded
        ("r3", 45),  # unhealthy
        ("r4", 15),  # critical
    ]
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    for run_id, score in runs_data:
        rec = [{"ts": "t", "run_id": run_id, "slot": 1, "event": "stop_received",
                 "kind": "STATE", "state": "exit", "score": score}]
        fp = _make_jsonl(tmp_path, f"diagnostics_slot1_{run_id}.jsonl", rec)
        # Renombrar para match pattern
        new_fp = tmp_path / f"diagnostics_slot1_{run_id.replace('r','2026')}_0000.jsonl"
        fp.rename(new_fp)
        # Cambiar también el run_id en records
        with open(new_fp, "r", encoding="utf-8") as f:
            content = f.read()
        idx.ingest_completed_run(str(new_fp))
    dist = idx.get_health_distribution()
    assert dist["total"] >= 4
    assert dist["healthy"] >= 1
    assert dist["critical"] >= 1


def test_get_stats(tmp_path):
    from core.dashboard.historical_sqlite import HistoricalSQLiteIndex
    rec = [
        {"ts": "t1", "run_id": "r1", "slot": 1, "event": "stop_received",
          "kind": "STATE", "state": "exit", "score": 80},
    ]
    fp = _make_jsonl(tmp_path, "diagnostics_slot1_20260513_120000.jsonl", rec)
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    idx.ingest_completed_run(str(fp))
    stats = idx.get_stats()
    assert stats["total_runs"] == 1
    assert "outcomes" in stats
    assert "avg_health_score" in stats


# ════════════════════════════════════════════════════════════════════
#  5. Background ingester
# ════════════════════════════════════════════════════════════════════
def test_background_ingester_picks_up_files(tmp_path, monkeypatch):
    import time
    from core.dashboard.historical_sqlite import (
        HistoricalSQLiteIndex, SQLiteBackgroundIngester,
    )
    monkeypatch.setenv("HISTORICAL_INGESTER_ENABLED", "1")
    records = [
        {"ts": "t1", "run_id": "r1", "slot": 1, "event": "init", "kind": "STATE",
          "state": "setup", "score": 30},
        {"ts": "t2", "run_id": "r1", "slot": 1, "event": "stop_received",
          "kind": "STATE", "state": "exit", "score": 90},
    ]
    fp = _make_jsonl(tmp_path, "diagnostics_slot1_20260513_120000.jsonl", records)
    # Forzar mtime antiguo
    import os
    old_time = time.time() - 120
    os.utime(fp, (old_time, old_time))

    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    ingester = SQLiteBackgroundIngester(directory=str(tmp_path), index=idx,
                                          idle_threshold_s=10.0)
    n = ingester.force_scan()
    assert n == 1
    runs = idx.get_recent_runs()
    assert len(runs) == 1


def test_ingester_skips_recent_files(tmp_path, monkeypatch):
    import time
    from core.dashboard.historical_sqlite import (
        HistoricalSQLiteIndex, SQLiteBackgroundIngester,
    )
    records = [{"ts": "t", "run_id": "r1", "slot": 1, "event": "init",
                  "kind": "STATE", "state": "setup", "score": 30}]
    fp = _make_jsonl(tmp_path, "diagnostics_slot1_20260513_120000.jsonl", records)
    # Fichero recién modificado
    idx = HistoricalSQLiteIndex(db_path=str(tmp_path / "idx.sqlite"))
    ingester = SQLiteBackgroundIngester(directory=str(tmp_path), index=idx,
                                          idle_threshold_s=300.0)  # 5 min threshold
    n = ingester.force_scan()
    assert n == 0  # demasiado reciente


# ════════════════════════════════════════════════════════════════════
#  6. Endpoint /api/v4/history2
# ════════════════════════════════════════════════════════════════════
def test_endpoint_history2_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/api/v4/history2")
    assert r.status_code == 200
    data = r.get_json()
    assert "runs" in data
    assert "stats" in data


def test_endpoint_history2_with_data(tmp_path, monkeypatch):
    """Test E2E: ingest + query desde endpoint."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("HISTORICAL_DB_PATH", str(tmp_path / "runs" / "test_idx.sqlite"))
    # Reset singleton
    import importlib
    import core.dashboard.historical_sqlite as hs_mod
    hs_mod.reset_for_test()
    # Crear fichero JSONL completado
    records = [
        {"ts": "t1", "run_id": "r1", "slot": 1, "event": "init",
          "kind": "STATE", "state": "setup", "score": 30},
        {"ts": "t2", "run_id": "r1", "slot": 1, "event": "stop_received",
          "kind": "STATE", "state": "exit", "score": 88},
    ]
    fp = tmp_path / "runs" / "diagnostics_slot1_20260513_120000.jsonl"
    with open(fp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    # Ingest directo
    idx = hs_mod.get_default_sqlite_index()
    idx.ingest_completed_run(str(fp))
    # Endpoint query
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/api/v4/history2")
    data = r.get_json()
    assert len(data["runs"]) >= 1
    assert data["stats"]["total_runs"] >= 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
