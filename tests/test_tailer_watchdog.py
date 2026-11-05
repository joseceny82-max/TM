"""Tests M6 — JSONLTailer watchdog.

Garantías:
    1. get_health() reporta alive + last_poll_at + mode
    2. is_stalled() detecta gap >threshold
    3. attempt_recovery() reinicia thread muerto
    4. Endpoint /api/diagnostics/info incluye watchdog
    5. Endpoint /api/diagnostics/tailer/health
    6. Endpoint /api/diagnostics/tailer/recover
    7. Modos degradados (FULL / NO_AGGREGATE / TAILER_DEAD)
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  1. get_health()
# ════════════════════════════════════════════════════════════════════
def test_health_initial_state(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path))
    h = t.get_health()
    # No arrancado todavía
    assert h["alive"] is False
    assert h["poll_count"] == 0
    assert h["mode"] in ("TAILER_DEAD",)


def test_health_after_start(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.3)  # esperar 1-2 polls
    h = t.get_health()
    assert h["alive"] is True
    assert h["poll_count"] >= 1
    assert h["last_poll_at"] is not None
    assert h["mode"] == "FULL"
    t.stop()


def test_health_last_poll_recent(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.3)
    h = t.get_health()
    # last_poll_ago_s debe ser pequeño
    assert h["last_poll_ago_s"] is not None
    assert h["last_poll_ago_s"] < 2.0
    t.stop()


# ════════════════════════════════════════════════════════════════════
#  2. is_stalled()
# ════════════════════════════════════════════════════════════════════
def test_is_stalled_when_never_polled(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path))
    # Sin arrancar, _last_poll_at = 0 → no stalled (no falso positivo)
    assert t.is_stalled() is False


def test_is_stalled_after_long_gap(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.2)
    # Manipular manualmente last_poll_at para simular gap
    t._last_poll_at = time.time() - 10.0
    assert t.is_stalled(threshold_s=5.0) is True
    t.stop()


def test_is_stalled_not_stalled(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.2)
    assert t.is_stalled(threshold_s=5.0) is False
    t.stop()


# ════════════════════════════════════════════════════════════════════
#  3. Mode detection
# ════════════════════════════════════════════════════════════════════
def test_mode_full_when_healthy(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.3)
    assert t.get_health()["mode"] == "FULL"
    t.stop()


def test_mode_no_aggregate_when_stalled(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.2)
    # Simular stall
    t._last_poll_at = time.time() - 10.0
    h = t.get_health()
    assert h["mode"] == "NO_AGGREGATE"
    t.stop()


def test_mode_tailer_dead(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path))
    # No start
    assert t.get_health()["mode"] == "TAILER_DEAD"


# ════════════════════════════════════════════════════════════════════
#  4. attempt_recovery()
# ════════════════════════════════════════════════════════════════════
def test_recovery_on_dead_tailer(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    assert not t.is_running()
    ok = t.attempt_recovery()
    assert ok is True
    assert t.is_running()
    t.stop()


def test_recovery_idempotent(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.1)
    # Llamar attempt_recovery cuando ya está vivo → no efecto
    ok = t.attempt_recovery()
    assert ok is True
    t.stop()


# ════════════════════════════════════════════════════════════════════
#  5. Endpoint /api/diagnostics/info incluye watchdog
# ════════════════════════════════════════════════════════════════════
def test_endpoint_info_has_tailer_health(tmp_path, monkeypatch):
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
    assert "tailer_health" in data
    assert "current_mode" in data


def test_endpoint_tailer_health(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/api/diagnostics/tailer/health")
    assert r.status_code == 200
    data = r.get_json()
    assert "alive" in data
    assert "mode" in data


def test_endpoint_tailer_recover(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/api/diagnostics/tailer/recover")
    assert r.status_code == 200
    data = r.get_json()
    assert "ok" in data


# ════════════════════════════════════════════════════════════════════
#  6. Counter de polls + errors
# ════════════════════════════════════════════════════════════════════
def test_poll_count_increases(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.5)
    h = t.get_health()
    assert h["poll_count"] >= 3
    t.stop()


def test_health_error_safe(tmp_path):
    """get_health() debe ser fail-safe ante errores internos."""
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path))
    h = t.get_health()
    # Tiene que devolver dict, no lanzar
    assert isinstance(h, dict)


# ════════════════════════════════════════════════════════════════════
#  7. Heartbeat updates on each poll
# ════════════════════════════════════════════════════════════════════
def test_heartbeat_advances(tmp_path):
    from core.dashboard.stream import JSONLTailer
    t = JSONLTailer(directory=str(tmp_path), poll_interval_s=0.1)
    t.start()
    time.sleep(0.2)
    h1 = t.get_health()
    time.sleep(0.3)
    h2 = t.get_health()
    # last_poll_at debe haber avanzado
    assert h2["last_poll_at"] > h1["last_poll_at"]
    assert h2["poll_count"] > h1["poll_count"]
    t.stop()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
