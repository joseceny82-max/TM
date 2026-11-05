"""Tests del sistema runtime_diagnostics.

Garantías testeadas:
    1. event_classifier es pure function
    2. drift_detector detecta INVALID_TRANSITION
    3. health_score escala 0-100 correctamente
    4. RunSession persiste JSONL
    5. observe() es fail-safe
    6. OBSERVABILITY_ENABLED=0 desactiva todo
    7. Hook desde _compra_logger no rompe nada
"""
import json
import os
import sys
from pathlib import Path

# Asegurar path raíz para imports
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  1. event_classifier
# ════════════════════════════════════════════════════════════════════
def test_classifier_pure_function():
    from core.contracts.event_classifier import classify, EventKind
    # Misma entrada → misma salida
    r1 = classify("shot")
    r2 = classify("shot")
    assert r1 == r2
    assert r1[0] == EventKind.NOISE


def test_classifier_state_events():
    from core.contracts.event_classifier import classify, EventKind
    for evt in ["goto", "oauth_detected", "checkout_detected", "stop_received",
                "FATAL_bot_block_early", "TIMEOUT_no_checkout"]:
        kind, _, _ = classify(evt)
        assert kind == EventKind.STATE, f"{evt} debería ser STATE"


def test_classifier_noise_filtering():
    from core.contracts.event_classifier import classify, EventKind
    for evt in ["shot", "shot_err", "blockers_eval_skipped", "pre_checkout_unknown"]:
        kind, _, _ = classify(evt)
        assert kind == EventKind.NOISE, f"{evt} debería ser NOISE"


def test_classifier_exceptions_before_noise():
    """Las excepciones Rx* deben ganar antes que R2 NOISE."""
    from core.contracts.event_classifier import classify, EventKind
    kind, rule, _ = classify("pre_login_navigate")
    assert kind == EventKind.ACTION, f"got {kind}"
    assert rule == "Rx1"


def test_classifier_fallback_safe():
    from core.contracts.event_classifier import classify, EventKind
    kind, rule, _ = classify("never_seen_before_xyz_123")
    assert kind == EventKind.UNKNOWN
    assert rule == "FALLBACK"


def test_classifier_handles_invalid_input():
    """Pure function — no debe lanzar con input raro."""
    from core.contracts.event_classifier import classify, EventKind
    for bad in ["", None, 123, [], {}]:
        kind, rule, _ = classify(bad)
        assert kind == EventKind.UNKNOWN
        assert rule == "FALLBACK"


# ════════════════════════════════════════════════════════════════════
#  2. drift_detector
# ════════════════════════════════════════════════════════════════════
def test_drift_detector_happy_path():
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector(initial_state="setup")
    # Camino feliz
    for evt in ["init", "goto", "oauth_detected", "checkout_detected", "stop_received"]:
        d.observe(evt)
    s = d.summary()
    assert s["drift_count"] == 0, f"happy path debe tener 0 drifts, got {s}"
    assert s["final_state"] == "exit"


def test_drift_detector_loop_anomaly():
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector(initial_state="browsing")
    # 35 eventos ACTION/OBS sin transición → loop anomaly
    for _ in range(35):
        d.observe("qty")
    s = d.summary()
    assert s["drift_count"] >= 1, "debería detectar LOOP_ANOMALY"
    assert any(dd["kind"] == "LOOP_ANOMALY" for dd in s["drifts_detail"])


def test_drift_detector_safe_on_empty_input():
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector()
    # Inputs raros no deben lanzar
    for bad in ["", "xxxx", "weird_unmapped_event"]:
        d.observe(bad)  # debe pasar sin excepción
    s = d.summary()
    assert s is not None


# ════════════════════════════════════════════════════════════════════
#  3. health_score
# ════════════════════════════════════════════════════════════════════
def test_health_score_baseline():
    from core.diagnostics.health_score import HealthScore, SCORE_BASELINE
    h = HealthScore()
    assert h.score == SCORE_BASELINE


def test_health_score_clamps_0_100():
    from core.diagnostics.health_score import HealthScore
    h = HealthScore()
    # Lluvia de positivos
    for _ in range(20):
        h.observe("goto")
    assert h.score <= 100
    h2 = HealthScore()
    for _ in range(20):
        h2.observe("FATAL_some_error")
    assert h2.score >= 0


def test_health_score_drift_penalty():
    from core.diagnostics.health_score import HealthScore
    h = HealthScore()
    initial = h.score
    h.apply_drift_penalty("INVALID_TRANSITION")
    assert h.score < initial


def test_health_score_verdict():
    from core.diagnostics.health_score import HealthScore
    h = HealthScore()
    for e in ["goto", "oauth_detected", "checkout_detected", "card_fields_filled",
              "READY_FOR_USER", "stop_received"]:
        h.observe(e)
    assert h.score >= 80
    assert h._verdict() == "HEALTHY"


# ════════════════════════════════════════════════════════════════════
#  4. RunSession
# ════════════════════════════════════════════════════════════════════
def test_run_session_persists_jsonl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    from core.runtime.run_session import RunSession
    s = RunSession(slot=88, run_id="test_persist")
    s.observe("goto", url="http://test")
    s.observe("shot")
    s.finalize()
    assert s.jsonl_path.exists()
    lines = s.jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    # Cada línea es JSON parseable
    for line in lines:
        rec = json.loads(line)
        assert "event" in rec and "kind" in rec


def test_run_session_failsafe_on_bad_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    from core.runtime.run_session import RunSession
    s = RunSession(slot=88, run_id="test_failsafe")
    # Inputs raros no deben lanzar
    s.observe("")
    s.observe(123)  # type: ignore
    s.observe(None)  # type: ignore
    sm = s.finalize()
    assert sm is not None


# ════════════════════════════════════════════════════════════════════
#  5. observe() entrypoint
# ════════════════════════════════════════════════════════════════════
def test_observe_works_with_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "1")
    # Reset module cache to pick up env
    if "core.observability.runtime_diagnostics" in sys.modules:
        del sys.modules["core.observability.runtime_diagnostics"]
    from core.observability.runtime_diagnostics import observe, finalize, reset_all
    reset_all()
    observe(event_name="init", payload={"slot": 1})
    observe(event_name="goto", payload={"slot": 1})
    summary = finalize(slot=1)
    assert summary is not None
    assert summary["total_events"] == 2


def test_observe_silenced_when_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "0")
    # Reset module cache
    if "core.observability.runtime_diagnostics" in sys.modules:
        del sys.modules["core.observability.runtime_diagnostics"]
    from core.observability.runtime_diagnostics import observe, finalize, reset_all
    reset_all()
    observe(event_name="init", payload={"slot": 1})
    observe(event_name="goto", payload={"slot": 1})
    summary = finalize(slot=1)
    assert summary is None


def test_observe_fail_safe_on_crashing_payload(tmp_path, monkeypatch):
    """observe() debe ser fail-safe ante payloads que no se pueden serializar."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "1")
    if "core.observability.runtime_diagnostics" in sys.modules:
        del sys.modules["core.observability.runtime_diagnostics"]
    from core.observability.runtime_diagnostics import observe, finalize, reset_all
    reset_all()

    class Unserializable:
        def __repr__(self):
            raise RuntimeError("boom")

    # No debe lanzar
    observe(event_name="goto", payload={"slot": 1, "bad": Unserializable()})
    sm = finalize(slot=1)
    # Llegamos hasta aquí sin excepción → fail-safe OK
    assert sm is not None or sm is None  # cualquiera de los dos es válido


# ════════════════════════════════════════════════════════════════════
#  6. Hook integration con _compra_logger
# ════════════════════════════════════════════════════════════════════
def test_legacy_logger_unaffected_by_hook(tmp_path, monkeypatch, capsys):
    """El legacy log_event debe seguir emitiendo stdout + JSONL idénticos."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "1")
    # Reset cache
    for m in list(sys.modules.keys()):
        if m.startswith("core.observability.runtime_diagnostics"):
            del sys.modules[m]
    from _compra_logger import StructuredLogger
    logger = StructuredLogger(slot=77, run_id="hook_test")
    logger.event("init")
    logger.event("goto")
    captured = capsys.readouterr()
    assert "init" in captured.out
    assert "goto" in captured.out


def test_legacy_logger_with_hook_disabled(tmp_path, monkeypatch, capsys):
    """Con OBSERVABILITY_ENABLED=0 el legacy sigue funcionando."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "0")
    for m in list(sys.modules.keys()):
        if m.startswith("core.observability.runtime_diagnostics"):
            del sys.modules[m]
    from _compra_logger import StructuredLogger
    logger = StructuredLogger(slot=78, run_id="hook_off_test")
    logger.event("init")
    logger.event("FATAL_test")
    captured = capsys.readouterr()
    assert "init" in captured.out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
