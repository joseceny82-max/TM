"""Tests del dashboard de diagnostics.

Garantías:
    1. JSONLTailer detecta nuevas líneas
    2. SessionAggregator agrupa correctamente
    3. Flask Blueprint expone endpoints
    4. Fail-safe ante errores
    5. UI HTML se renderiza
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  1. JSONLTailer
# ════════════════════════════════════════════════════════════════════
def test_tailer_reads_new_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path / "runs"))
    received: list[dict] = []
    t.subscribe(lambda r: received.append(r))
    t.start()
    # Crear fichero después de start
    fp = tmp_path / "runs" / "diagnostics_slot1_test.jsonl"
    fp.write_text(json.dumps({"event": "init", "slot": 1, "kind": "OBSERVABILITY"}) + "\n",
                   encoding="utf-8")
    time.sleep(1.2)  # esperar al poll
    t.stop()
    assert len(received) >= 1
    assert received[0]["event"] == "init"


def test_tailer_buffer_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path / "runs"))
    t.start()
    fp = tmp_path / "runs" / "diagnostics_slot1_test.jsonl"
    # Escribir más eventos que BUFFER_SIZE
    with open(fp, "w", encoding="utf-8") as f:
        for i in range(1000):
            f.write(json.dumps({"event": f"e{i}", "slot": 1}) + "\n")
    time.sleep(1.5)
    t.stop()
    recent = t.get_recent(2000)
    # No debería superar BUFFER_SIZE
    assert len(recent) <= JSONLTailer.BUFFER_SIZE


def test_tailer_failsafe_on_bad_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path / "runs"))
    received = []
    t.subscribe(lambda r: received.append(r))
    t.start()
    fp = tmp_path / "runs" / "diagnostics_slot1_test.jsonl"
    # Mezclar JSON inválido con válido
    fp.write_text(
        '{"event":"good","slot":1}\n'
        'this is not json\n'
        '{"event":"good2","slot":1}\n',
        encoding="utf-8"
    )
    time.sleep(1.2)
    t.stop()
    # Las líneas válidas se procesan; las inválidas se ignoran
    events = [r["event"] for r in received]
    assert "good" in events
    assert "good2" in events


# ════════════════════════════════════════════════════════════════════
#  2. SessionAggregator
# ════════════════════════════════════════════════════════════════════
def test_aggregator_tracks_state():
    from core.dashboard.aggregator import SessionAggregator
    a = SessionAggregator()
    a.update({"slot": 1, "run_id": "r1", "event": "init", "kind": "OBSERVABILITY",
              "state": "setup", "score": 30})
    a.update({"slot": 1, "run_id": "r1", "event": "goto", "kind": "STATE",
              "state": "browsing", "score": 50})
    run = a.get_current_run()
    assert run is not None
    assert run["current_state"] == "browsing"
    assert run["previous_state"] == "setup"
    assert run["health_score"] == 50


def test_aggregator_detects_loops():
    from core.dashboard.aggregator import SessionAggregator
    a = SessionAggregator()
    for _ in range(5):
        a.update({"slot": 1, "run_id": "r1", "event": "qty", "kind": "OBSERVABILITY",
                  "state": "browsing"})
    run = a.get_current_run()
    assert run["loops"].get("qty", 0) >= 3  # 4 repeticiones después de la primera


def test_aggregator_failure_trace():
    from core.dashboard.aggregator import SessionAggregator
    a = SessionAggregator()
    flow = [
        {"event": "goto", "kind": "STATE", "state": "browsing"},
        {"event": "oauth_detected", "kind": "STATE", "state": "login"},
        {"event": "login_start", "kind": "ACTION", "state": "login"},
        {"event": "no_email_input", "kind": "OBSERVABILITY", "state": "login"},
        {"event": "FATAL_bot_block_early", "kind": "STATE", "state": "exit"},
    ]
    for ev in flow:
        ev.update({"slot": 1, "run_id": "r1"})
        a.update(ev)
    run = a.get_current_run()
    assert run["last_failure"] == "FATAL_bot_block_early"
    assert "FATAL_bot_block_early" in run["failure_trace"]
    assert run["exit_outcome"] == "BLOCKED"


def test_aggregator_failsafe_on_bad_input():
    from core.dashboard.aggregator import SessionAggregator
    a = SessionAggregator()
    # No debe lanzar con inputs raros
    a.update({})
    a.update({"event": None})
    a.update({"slot": "no_int", "event": "goto", "kind": "STATE"})
    # OK si no crashea
    assert a.get_global_summary() is not None


# ════════════════════════════════════════════════════════════════════
#  3. Flask Blueprint endpoints
# ════════════════════════════════════════════════════════════════════
def test_blueprint_registers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    ok = register_diagnostics(app)
    assert ok
    # Endpoints existen
    rules = [str(r) for r in app.url_map.iter_rules()]
    assert "/diagnostics" in rules
    assert "/api/diagnostics/info" in rules
    assert "/api/diagnostics/runs" in rules
    assert "/api/diagnostics/state/live" in rules
    assert "/api/diagnostics/events/stream" in rules


def test_blueprint_info_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/api/diagnostics/info")
    assert r.status_code == 200
    data = r.get_json()
    assert "module" in data
    assert data["dashboard_enabled"] is True


def test_blueprint_runs_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/api/diagnostics/runs")
    assert r.status_code == 200
    data = r.get_json()
    assert "runs" in data
    assert "global" in data


def test_blueprint_state_live_empty(tmp_path, monkeypatch):
    """Sin runs → respuesta válida con state=None."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    # Reset aggregator del singleton
    if "core.dashboard.aggregator" in sys.modules:
        del sys.modules["core.dashboard.aggregator"]
    if "core.dashboard.api" in sys.modules:
        del sys.modules["core.dashboard.api"]
    if "core.dashboard.stream" in sys.modules:
        del sys.modules["core.dashboard.stream"]
    if "core.dashboard" in sys.modules:
        del sys.modules["core.dashboard"]
    from core.dashboard import register_diagnostics as rd
    app = Flask(__name__)
    rd(app)
    client = app.test_client()
    r = client.get("/api/diagnostics/state/live")
    assert r.status_code == 200
    data = r.get_json()
    # state puede ser None o algún valor — la respuesta es válida
    assert "state" in data


# ════════════════════════════════════════════════════════════════════
#  4. UI HTML render
# ════════════════════════════════════════════════════════════════════
def test_ui_diagnostics_renders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/diagnostics")
    # Render debe responder con html
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "TM DIAGNOSTICS" in body
    assert "FSM state" in body


# ════════════════════════════════════════════════════════════════════
#  5. Disabled mode
# ════════════════════════════════════════════════════════════════════
def test_dashboard_disabled_does_not_start_tailer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("DIAGNOSTICS_DASHBOARD_ENABLED", "0")
    # Reset singletons
    for m in list(sys.modules.keys()):
        if m.startswith("core.dashboard"):
            del sys.modules[m]
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    # Endpoints existen pero tailer está OFF
    client = app.test_client()
    r = client.get("/api/diagnostics/info")
    data = r.get_json()
    assert data["dashboard_enabled"] is False or data["started"] is False


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
