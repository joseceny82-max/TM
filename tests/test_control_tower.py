"""Tests del Control Tower FastAPI (las 4 fases).

Garantías:
    1. FastAPI app importa y arranca
    2. FASE 1: /, /api/v1/state, /api/v1/health funcionan
    3. FASE 2: /realtime, /api/v2/* funcionan
    4. FASE 3: /control, /api/v3/* (analytics + root cause) funcionan
    5. FASE 4: /tower, /api/v4/* (multi-run + alerts + history) funcionan
    6. Módulos analytics/alerts/historical son pure functions
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  1. Imports + app
# ════════════════════════════════════════════════════════════════════
def test_app_imports():
    from core.control_tower.server import app
    assert app is not None
    routes = [r.path for r in app.routes]
    # FASE 1
    assert "/" in routes
    assert "/api/v1/state" in routes
    assert "/api/v1/health" in routes
    assert "/api/v1/stream" in routes
    # FASE 2
    assert "/realtime" in routes
    assert "/api/v2/state/live" in routes
    assert "/api/v2/drift/current" in routes
    assert "/api/v2/loops/current" in routes
    assert "/api/v2/health/current" in routes
    # FASE 3
    assert "/control" in routes
    assert "/api/v3/run/{slot}/analytics" in routes
    assert "/api/v3/run/{slot}/root-cause" in routes
    assert "/api/v3/events/recent" in routes
    # FASE 4
    assert "/tower" in routes
    assert "/api/v4/runs/all" in routes
    assert "/api/v4/drift/global" in routes
    assert "/api/v4/alerts" in routes
    assert "/api/v4/history" in routes


# ════════════════════════════════════════════════════════════════════
#  2. analytics.py — pure function
# ════════════════════════════════════════════════════════════════════
def test_analytics_compute():
    from core.control_tower.analytics import compute_run_analytics
    run = {
        "elapsed_s": 30,
        "event_count": 60,
        "current_state": "exit",
        "state_history": [
            {"from": "setup", "to": "browsing"},
            {"from": "browsing", "to": "login"},
            {"from": "login", "to": "exit"},
        ],
        "exit_outcome": "BLOCKED",
        "kind_counts": {"STATE": 4, "ACTION": 20, "OBSERVABILITY": 30, "NOISE": 6, "UNKNOWN": 0},
        "drift_count": 2,
        "health_score": 25,
        "health_verdict": "CRITICAL",
        "last_failure": "FATAL_bot_block_early",
    }
    a = compute_run_analytics(run)
    assert a["status"] == "blocked"
    assert a["states_count"] >= 3
    assert a["fsm_coverage_pct"] > 0
    assert a["event_throughput"] == 2.0
    assert a["severity"] == "high"
    assert a["has_failures"]


def test_analytics_running_status():
    from core.control_tower.analytics import compute_run_analytics
    run = {"elapsed_s": 5, "event_count": 10, "current_state": "browsing",
           "health_score": 75, "kind_counts": {}}
    a = compute_run_analytics(run)
    assert a["status"] == "running"
    assert a["severity"] in ("info", "low")


def test_analytics_empty():
    from core.control_tower.analytics import compute_run_analytics
    # Empty dict → devuelve error
    a = compute_run_analytics({})
    assert "error" in a
    # None → devuelve error
    a2 = compute_run_analytics(None)
    assert "error" in a2


# ════════════════════════════════════════════════════════════════════
#  3. Root cause detection
# ════════════════════════════════════════════════════════════════════
def test_root_cause_no_email_input():
    from core.control_tower.analytics import detect_root_cause
    rc = detect_root_cause({
        "last_failure": "no_email_input",
        "failure_trace": ["post_continuar_url", "oauth_detected", "login_start", "no_email_input"],
        "current_state": "login",
    })
    assert rc is not None
    assert rc["root_cause"] == "OAUTH_NO_EMAIL_INPUT"
    assert rc["confidence"] == "high"
    assert rc["category"] == "login_failure"


def test_root_cause_bot_block():
    from core.control_tower.analytics import detect_root_cause
    rc = detect_root_cause({
        "last_failure": "FATAL_bot_block_early",
        "failure_trace": ["pwd_typed", "shot", "FATAL_bot_block_early"],
        "current_state": "exit",
    })
    assert rc is not None
    assert "BOT_BLOCK" in rc["root_cause"]


def test_root_cause_none_when_no_failure():
    from core.control_tower.analytics import detect_root_cause
    assert detect_root_cause({}) is None
    assert detect_root_cause({"last_failure": None}) is None


def test_root_cause_unknown_fallback():
    from core.control_tower.analytics import detect_root_cause
    rc = detect_root_cause({"last_failure": "FATAL_weird_new_thing",
                              "failure_trace": ["x", "y", "z"]})
    assert rc is not None
    assert rc["confidence"] == "low"


# ════════════════════════════════════════════════════════════════════
#  4. Event filtering
# ════════════════════════════════════════════════════════════════════
def test_filter_events():
    from core.control_tower.analytics import filter_events
    events = [
        {"event": "goto",        "kind": "STATE",         "state": "browsing"},
        {"event": "shot",        "kind": "NOISE",         "state": "browsing"},
        {"event": "qty",         "kind": "OBSERVABILITY", "state": "browsing"},
        {"event": "login_start", "kind": "ACTION",        "state": "login"},
    ]
    # Solo STATE
    r = filter_events(events, kinds={"STATE"})
    assert len(r) == 1
    assert r[0]["event"] == "goto"
    # Por state
    r = filter_events(events, state="login")
    assert len(r) == 1
    # Search
    r = filter_events(events, search="qt")
    assert len(r) == 1
    assert r[0]["event"] == "qty"


# ════════════════════════════════════════════════════════════════════
#  5. alerts.py
# ════════════════════════════════════════════════════════════════════
def test_alerts_no_runs():
    from core.control_tower.alerts import evaluate_alerts
    assert evaluate_alerts([], {}) == []


def test_alerts_drift_spike(monkeypatch):
    monkeypatch.setenv("CT_ALERT_DRIFT_SPIKE", "3")
    # Reset import cache to pick up env
    if "core.control_tower.alerts" in sys.modules:
        del sys.modules["core.control_tower.alerts"]
    from core.control_tower.alerts import evaluate_alerts
    runs = [
        {"slot": 1, "drift_count": 4, "exit_outcome": None,
         "state_history": [], "current_state": "login", "health_score": 60},
    ]
    alerts = evaluate_alerts(runs, {"avg_health_score": 60})
    assert any(a["kind"] == "DRIFT_SPIKE" for a in alerts)


def test_alerts_bot_block_burst():
    from core.control_tower.alerts import evaluate_alerts
    runs = [
        {"slot": 1, "exit_outcome": "BLOCKED", "state_history": [],
         "current_state": "exit", "health_score": 25, "drift_count": 0,
         "last_failure": "bot_block"},
        {"slot": 2, "exit_outcome": "BLOCKED", "state_history": [],
         "current_state": "exit", "health_score": 30, "drift_count": 0,
         "last_failure": "bot_block"},
    ]
    alerts = evaluate_alerts(runs, {"avg_health_score": 27})
    assert any(a["kind"] == "BOT_BLOCK_BURST" for a in alerts)


# ════════════════════════════════════════════════════════════════════
#  6. historical.py
# ════════════════════════════════════════════════════════════════════
def test_historical_index_creates(tmp_path):
    import json
    from core.control_tower.historical import HistoricalIndex
    rdir = tmp_path / "runs"
    rdir.mkdir()
    # Crear un JSONL de test
    fp = rdir / "diagnostics_slot7_20260513_123456.jsonl"
    lines = [
        {"ts": "2026-05-13T12:00:00Z", "event": "init", "kind": "OBSERVABILITY", "state": "setup", "score": 30},
        {"ts": "2026-05-13T12:00:05Z", "event": "goto", "kind": "STATE", "state": "browsing", "score": 50},
        {"ts": "2026-05-13T12:00:10Z", "event": "FATAL_bot_block_early", "kind": "STATE", "state": "exit", "score": 25,
         "drift": {"kind": "INVALID_TRANSITION"}},
    ]
    fp.write_text("\n".join(json.dumps(r) for r in lines), encoding="utf-8")
    idx = HistoricalIndex(directory=str(rdir))
    runs = idx.get_runs(force_refresh=True)
    assert len(runs) == 1
    r = runs[0]
    assert r["slot"] == 7
    assert r["event_count"] == 3
    assert r["drift_count"] == 1
    assert r["exit_outcome"] == "BLOCKED"
    assert r["final_state"] == "exit"


def test_historical_index_stats(tmp_path):
    import json
    from core.control_tower.historical import HistoricalIndex
    rdir = tmp_path / "runs"
    rdir.mkdir()
    fp = rdir / "diagnostics_slot1_20260513_120000.jsonl"
    fp.write_text(json.dumps({"ts": "t", "event": "init", "kind": "OBSERVABILITY",
                                "state": "setup", "score": 30}) + "\n", encoding="utf-8")
    idx = HistoricalIndex(directory=str(rdir))
    stats = idx.stats()
    assert stats["total_runs"] == 1


# ════════════════════════════════════════════════════════════════════
#  7. Endpoints via TestClient (FastAPI)
# ════════════════════════════════════════════════════════════════════
def test_endpoint_state_v1():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    r = client.get("/api/v1/state")
    assert r.status_code == 200
    assert "state" in r.json()


def test_endpoint_health_v1():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    data = r.json()
    assert "score" in data and "verdict" in data


def test_endpoint_v2_drift():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    r = client.get("/api/v2/drift/current")
    assert r.status_code == 200
    assert "drifts" in r.json()


def test_endpoint_v3_events():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    r = client.get("/api/v3/events/recent?n=10")
    assert r.status_code == 200
    assert "events" in r.json()


def test_endpoint_v4_runs_all():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    r = client.get("/api/v4/runs/all")
    assert r.status_code == 200
    data = r.json()
    assert "runs" in data
    assert "global" in data


def test_endpoint_v4_alerts():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    r = client.get("/api/v4/alerts")
    assert r.status_code == 200
    data = r.json()
    assert "alerts" in data
    assert "thresholds" in data


def test_endpoint_v4_history():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    r = client.get("/api/v4/history")
    assert r.status_code == 200
    assert "runs" in r.json()


def test_ui_routes_render():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    for path in ["/", "/realtime", "/control", "/tower"]:
        r = client.get(path)
        assert r.status_code == 200
        body = r.text
        assert "TM Control Tower" in body


def test_healthz():
    from fastapi.testclient import TestClient
    from core.control_tower.server import app
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
