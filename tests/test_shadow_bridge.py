"""tests/test_shadow_bridge.py — Validación del puente shadow (FASE 3).

Garantiza:
    - log_bridge OFF por defecto: cero efectos secundarios.
    - log_bridge ON: escribe línea JSON en `runs/shadow_core_*.jsonl`.
    - log_bridge NUNCA lanza excepciones (contrato silencioso).
    - Legacy log() sigue funcionando idéntico (stdout intacto).
    - El bridge no interfiere con el JSONL legacy.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_shadow_state(monkeypatch, tmp_path):
    """Cada test arranca en un cwd temporal con state limpio del shadow."""
    monkeypatch.chdir(tmp_path)
    # Asegurar runs/ existe en el tmp_path (el legacy lo crea solo al import,
    # que ya pasó en cwd original — replicarlo aquí para que log_event escriba).
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    # Forzar recálculo del path interno al cambiar cwd
    from core.observability.logger import reset_shadow_path_for_test
    reset_shadow_path_for_test()
    yield
    reset_shadow_path_for_test()


# ────────────────────────────────────────────────────────────────────
#  1. OFF por defecto
# ────────────────────────────────────────────────────────────────────
def test_bridge_off_by_default(monkeypatch):
    """Sin env var, el bridge NO escribe nada."""
    monkeypatch.delenv("CORE_SHADOW_LOG", raising=False)

    from core.observability.logger import log_bridge

    log_bridge(slot=1, event="test_event", foo="bar")

    # No debe existir el JSONL
    assert not list(Path("runs").glob("shadow_core_*.jsonl"))


# ────────────────────────────────────────────────────────────────────
#  2. ON: escribe JSONL
# ────────────────────────────────────────────────────────────────────
def test_bridge_on_writes_jsonl(monkeypatch):
    """Con CORE_SHADOW_LOG=1, escribe una línea JSON."""
    monkeypatch.setenv("CORE_SHADOW_LOG", "1")

    from core.observability.logger import log_bridge, reset_shadow_path_for_test
    reset_shadow_path_for_test()  # recalcular path con env activo

    log_bridge(slot=1, event="test_event", foo="bar", count=42)

    files = list(Path("runs").glob("shadow_core_*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "test_event"
    assert record["slot"] == 1
    assert record["foo"] == "bar"
    assert record["count"] == 42
    assert "ts" in record
    assert "pid" in record


# ────────────────────────────────────────────────────────────────────
#  3. Bridge NUNCA lanza excepciones
# ────────────────────────────────────────────────────────────────────
def test_bridge_silent_on_failure(monkeypatch):
    """Aunque el JSON dump falle (objeto no serializable), no lanza."""
    monkeypatch.setenv("CORE_SHADOW_LOG", "1")

    from core.observability.logger import log_bridge

    # Pasar un objeto que normalmente no serializa
    class Weird:
        def __repr__(self): raise RuntimeError("boom")

    # No debe lanzar — bridge silencia
    log_bridge(slot=1, event="weird", obj=Weird())
    # Si llegamos aquí, el bridge fue silencioso ✅


def test_bridge_non_serializable_no_crash(monkeypatch):
    """Pasar un lambda u objeto raro no rompe nada."""
    monkeypatch.setenv("CORE_SHADOW_LOG", "1")

    from core.observability.logger import log_bridge

    log_bridge(slot=2, event="weird_lambda", fn=lambda x: x)
    # Sin assert — basta con que no lance


# ────────────────────────────────────────────────────────────────────
#  4. Legacy log_event sigue funcionando + bridge en paralelo
# ────────────────────────────────────────────────────────────────────
def test_legacy_log_event_with_bridge_on(monkeypatch, capsys):
    """Cuando CORE_SHADOW_LOG=1, el legacy log_event escribe:
        - stdout legacy `[slot1][HH:MM:SS] event {...}`
        - JSONL legacy `runs/structured_slot1.jsonl`
        - JSONL shadow `runs/shadow_core_*.jsonl`
    """
    monkeypatch.setenv("CORE_SHADOW_LOG", "1")

    from core.observability.logger import reset_shadow_path_for_test
    reset_shadow_path_for_test()
    from _compra_logger import log_event

    log_event(slot=1, event="oauth_detected", url="https://example.com")

    out = capsys.readouterr().out
    assert "[slot1]" in out
    assert "oauth_detected" in out

    # Legacy JSONL existe
    legacy_path = Path("runs") / "structured_slot1.jsonl"
    assert legacy_path.exists()
    legacy_record = json.loads(legacy_path.read_text(encoding="utf-8").strip())
    assert legacy_record["event"] == "oauth_detected"

    # Shadow JSONL existe y tiene el mismo evento
    shadow_files = list(Path("runs").glob("shadow_core_*.jsonl"))
    assert len(shadow_files) == 1
    shadow_record = json.loads(shadow_files[0].read_text(encoding="utf-8").strip())
    assert shadow_record["event"] == "oauth_detected"
    assert shadow_record["slot"] == 1
    assert shadow_record["url"] == "https://example.com"


def test_legacy_log_event_with_bridge_off(monkeypatch, capsys):
    """Sin CORE_SHADOW_LOG, el legacy sigue funcionando pero el shadow no escribe."""
    monkeypatch.delenv("CORE_SHADOW_LOG", raising=False)

    from _compra_logger import log_event

    log_event(slot=1, event="oauth_detected", url="https://example.com")

    out = capsys.readouterr().out
    assert "[slot1]" in out  # stdout legacy intacto
    assert "oauth_detected" in out

    # Legacy JSONL existe
    legacy_path = Path("runs") / "structured_slot1.jsonl"
    assert legacy_path.exists()

    # Shadow JSONL NO existe
    shadow_files = list(Path("runs").glob("shadow_core_*.jsonl"))
    assert len(shadow_files) == 0


# ────────────────────────────────────────────────────────────────────
#  5. Múltiples eventos: shadow acumula en append
# ────────────────────────────────────────────────────────────────────
def test_bridge_appends_multiple_events(monkeypatch):
    monkeypatch.setenv("CORE_SHADOW_LOG", "1")

    from core.observability.logger import log_bridge, reset_shadow_path_for_test
    reset_shadow_path_for_test()

    for i in range(5):
        log_bridge(slot=1, event="iter", n=i)

    files = list(Path("runs").glob("shadow_core_*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5
    for i, line in enumerate(lines):
        r = json.loads(line)
        assert r["n"] == i


# ────────────────────────────────────────────────────────────────────
#  6. Bridge NO afecta stdout del legacy
# ────────────────────────────────────────────────────────────────────
def test_bridge_does_not_pollute_stdout(monkeypatch, capsys):
    """El shadow escribe a fichero, nunca a stdout."""
    monkeypatch.setenv("CORE_SHADOW_LOG", "1")

    from core.observability.logger import log_bridge, reset_shadow_path_for_test
    reset_shadow_path_for_test()

    log_bridge(slot=1, event="silent_event", any="value")

    out = capsys.readouterr().out
    assert out == ""  # cero output a stdout
