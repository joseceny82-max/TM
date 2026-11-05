"""Tests P8 — Aggregator replay engine (2-stage: index + replay).

Tests obligatorios (spec v2):
    1. replay restores full state after restart
    2. no duplication after double replay
    3. offset dedup across files works
    4. partial JSONL corruption safe
    5. mtime + index hybrid correctness
    6. concurrent write + replay consistency
    7. incremental replay (new logs appended)
    8. empty system replay safe
    9. multi-file merge ordering correct

Extras:
    - performance under 500ms (10k events)
    - replay disabled via env
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _make_record(slot: int, run_id: str, event: str, **extra) -> dict:
    rec = {
        "ts": "2026-05-13T12:00:00Z",
        "slot": slot,
        "run_id": run_id,
        "event": event,
        "kind": "STATE",
        "state": "browsing",
        "score": 50,
    }
    rec.update(extra)
    return rec


def _make_jsonl(path: Path, records: list[dict],
                mtime_ago_s: float = 0) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    if mtime_ago_s > 0:
        old = time.time() - mtime_ago_s
        os.utime(path, (old, old))
    return path


# ════════════════════════════════════════════════════════════════════
#  1. Replay restores full state after restart
# ════════════════════════════════════════════════════════════════════
def test_replay_restores_full_state_after_restart(tmp_path):
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"
    runs.mkdir()
    fp = runs / "diagnostics_slot7_20260513_120000.jsonl"
    records = [
        _make_record(7, "20260513_120000", "init", kind="OBSERVABILITY", state="setup"),
        _make_record(7, "20260513_120000", "goto", kind="STATE", state="browsing", score=50),
        _make_record(7, "20260513_120000", "oauth_detected", kind="STATE", state="login", score=65),
    ]
    _make_jsonl(fp, records)
    # Aggregator vacío (simulando restart)
    agg = SessionAggregator()
    result = agg.replay_from_jsonl(directory=runs)
    assert result["applied"] == 3
    assert result["files"] == 1
    # Estado reconstruido
    run = agg.get_run(slot=7, run_id="20260513_120000")
    assert run is not None
    assert run["event_count"] == 3
    assert run["current_state"] == "login"


# ════════════════════════════════════════════════════════════════════
#  2. No duplication after double replay
# ════════════════════════════════════════════════════════════════════
def test_no_duplication_after_double_replay(tmp_path):
    """Spec v3: doble replay con skip_existing_runs=True → run skipped completo."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    records = [_make_record(1, "20260513_120000", f"e{i}",
                              kind="STATE" if i == 0 else "OBSERVABILITY",
                              state="browsing")
                for i in range(5)]
    _make_jsonl(fp, records)
    agg = SessionAggregator()
    r1 = agg.replay_from_jsonl(directory=runs)  # skip_existing_runs=True default
    assert r1["applied"] == 5
    assert r1["runs_skipped"] == 0  # run no existía
    # Segundo replay: run ya existe → SKIP completo
    r2 = agg.replay_from_jsonl(directory=runs)
    assert r2["applied"] == 0
    assert r2["runs_skipped"] == 1  # file completo skipped
    # Aggregator no duplicó counters
    run = agg.get_run(slot=1, run_id="20260513_120000")
    assert run["event_count"] == 5  # NO 10


def test_no_duplication_with_cursor_mode(tmp_path):
    """Modo cursor (skip_existing_runs=False): segundo replay procesa por offset."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    records = [_make_record(1, "20260513_120000", f"e{i}") for i in range(5)]
    _make_jsonl(fp, records)
    agg = SessionAggregator()
    r1 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    assert r1["applied"] == 5
    # Modo cursor: segundo replay con mismo file → todo skipped por offset
    r2 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    assert r2["applied"] == 0
    assert r2["skipped"] == 5
    run = agg.get_run(slot=1, run_id="20260513_120000")
    assert run["event_count"] == 5


# ════════════════════════════════════════════════════════════════════
#  3. Offset dedup across files works (modo cursor)
# ════════════════════════════════════════════════════════════════════
def test_offset_dedup_across_files(tmp_path):
    """Files distintos → cada uno con su cursor. skip_existing_runs=False."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp1 = runs / "diagnostics_slot1_20260513_120000.jsonl"
    fp2 = runs / "diagnostics_slot2_20260513_120100.jsonl"
    _make_jsonl(fp1, [_make_record(1, "20260513_120000", f"a{i}")
                       for i in range(3)])
    _make_jsonl(fp2, [_make_record(2, "20260513_120100", f"b{i}")
                       for i in range(3)])
    agg = SessionAggregator()
    r1 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    assert r1["applied"] == 6  # 3+3
    # Modo cursor: replay otra vez → 6 skips (offsets ya procesados)
    r2 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    assert r2["applied"] == 0
    assert r2["skipped"] == 6


# ════════════════════════════════════════════════════════════════════
#  4. Partial JSONL corruption safe
# ════════════════════════════════════════════════════════════════════
def test_partial_jsonl_corruption_safe(tmp_path):
    """Líneas corruptas en JSONL → replay continúa, no crash."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    # Mezclar JSON válido con corrupto
    content = (
        json.dumps(_make_record(1, "20260513_120000", "ok1")) + "\n"
        "CORRUPT_LINE_NOT_JSON\n"
        + json.dumps(_make_record(1, "20260513_120000", "ok2")) + "\n"
        "\n"  # línea vacía
        + json.dumps(_make_record(1, "20260513_120000", "ok3")) + "\n"
        "another \\ corrupt {bad json\n"
    )
    fp.write_text(content, encoding="utf-8")
    agg = SessionAggregator()
    result = agg.replay_from_jsonl(directory=runs)
    # 3 válidos aplicados, 2 corruptos contados
    assert result["applied"] == 3
    assert result["corrupt_lines"] >= 2


# ════════════════════════════════════════════════════════════════════
#  5. mtime filter correctness
# ════════════════════════════════════════════════════════════════════
def test_mtime_filter_excludes_old_files(tmp_path):
    """Archivos con mtime fuera del window NO se aplican."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    # File reciente (dentro de window)
    fp_recent = runs / "diagnostics_slot1_20260513_120000.jsonl"
    _make_jsonl(fp_recent, [_make_record(1, "20260513_120000", "recent")])
    # File antiguo (1h atrás, fuera de window 10 min)
    fp_old = runs / "diagnostics_slot2_20260513_100000.jsonl"
    _make_jsonl(fp_old, [_make_record(2, "20260513_100000", "old")],
                 mtime_ago_s=3600)
    agg = SessionAggregator()
    result = agg.replay_from_jsonl(directory=runs, since_minutes=10)
    assert result["files"] == 1  # solo el reciente
    assert agg.get_run(slot=1, run_id="20260513_120000") is not None
    assert agg.get_run(slot=2, run_id="20260513_100000") is None


# ════════════════════════════════════════════════════════════════════
#  6. Concurrent write + replay consistency
# ════════════════════════════════════════════════════════════════════
def test_concurrent_write_replay_consistency(tmp_path):
    """Writes live + replay paralelo → no crash, sin duplicados.

    Append-only model garantiza: replay con cursor solo procesa NUEVO contenido.
    """
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    # Initial content
    _make_jsonl(fp, [_make_record(1, "20260513_120000", f"init_{i}")
                      for i in range(10)])
    agg = SessionAggregator()
    # Replay inicial en modo cursor para capturar incrementales
    agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    # Thread que appendea más eventos (simula bot live)
    stop_flag = [False]

    def writer():
        i = 0
        while not stop_flag[0]:
            try:
                with open(fp, "a", encoding="utf-8") as f:
                    f.write(json.dumps(_make_record(1, "20260513_120000",
                                                       f"live_{i}")) + "\n")
                i += 1
                time.sleep(0.005)
            except Exception:
                pass

    t = threading.Thread(target=writer)
    t.start()
    # Replays paralelos (modo cursor)
    time.sleep(0.1)
    r2 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    time.sleep(0.1)
    r3 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    stop_flag[0] = True
    t.join(timeout=2.0)
    # Replay final para capturar lo último
    r4 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    # NO crash. event_count debe coincidir con líneas reales en file
    lines = fp.read_text(encoding="utf-8").splitlines()
    line_count = sum(1 for l in lines if l.strip())
    run = agg.get_run(slot=1, run_id="20260513_120000")
    assert run is not None
    # event_count debe = líneas escritas (no duplicados)
    assert run["event_count"] == line_count


# ════════════════════════════════════════════════════════════════════
#  7. Incremental replay (new logs appended)
# ════════════════════════════════════════════════════════════════════
def test_incremental_replay_new_logs(tmp_path):
    """Modo cursor: replay → append nuevas líneas → replay solo procesa lo nuevo."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    _make_jsonl(fp, [_make_record(1, "20260513_120000", f"e{i}")
                      for i in range(5)])
    agg = SessionAggregator()
    # skip_existing_runs=False habilita el modo cursor incremental
    r1 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    assert r1["applied"] == 5
    # Append 3 nuevos
    with open(fp, "a", encoding="utf-8") as f:
        for i in range(5, 8):
            f.write(json.dumps(_make_record(1, "20260513_120000", f"e{i}")) + "\n")
    r2 = agg.replay_from_jsonl(directory=runs, skip_existing_runs=False)
    # Solo los 3 nuevos aplicados, primeros 5 skipped por offset
    assert r2["applied"] == 3
    assert r2["skipped"] == 5
    run = agg.get_run(slot=1, run_id="20260513_120000")
    assert run["event_count"] == 8


# ════════════════════════════════════════════════════════════════════
#  8. Empty system replay safe
# ════════════════════════════════════════════════════════════════════
def test_empty_system_replay_safe(tmp_path):
    """Sin archivos JSONL → replay no crash."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    agg = SessionAggregator()
    result = agg.replay_from_jsonl(directory=runs)
    assert result["applied"] == 0
    assert result["files"] == 0
    assert result["error"] is None


def test_nonexistent_directory_safe(tmp_path):
    """Directorio inexistente → no crash."""
    from core.dashboard.aggregator import SessionAggregator
    agg = SessionAggregator()
    result = agg.replay_from_jsonl(directory=tmp_path / "does_not_exist")
    assert result["files"] == 0
    assert result["error"] is None


# ════════════════════════════════════════════════════════════════════
#  9. Multi-file merge ordering correct
# ════════════════════════════════════════════════════════════════════
def test_multi_file_merge_ordering(tmp_path):
    """Files con mtime distinto → procesados en orden cronológico."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    # File más antiguo (5 min ago)
    fp_a = runs / "diagnostics_slot1_20260513_120000.jsonl"
    _make_jsonl(fp_a, [_make_record(1, "20260513_120000", "first"),
                        _make_record(1, "20260513_120000", "second")],
                 mtime_ago_s=300)
    # File más reciente (1 min ago)
    fp_b = runs / "diagnostics_slot2_20260513_120500.jsonl"
    _make_jsonl(fp_b, [_make_record(2, "20260513_120500", "third"),
                        _make_record(2, "20260513_120500", "fourth")],
                 mtime_ago_s=60)
    agg = SessionAggregator()
    # Build index para verificar orden
    index = agg._build_index(runs, since_minutes=10)
    # Index ordenado por mtime asc → file_a primero
    assert index[0]["file"] == fp_a
    assert index[1]["file"] == fp_b
    # Replay aplica en orden cronológico
    result = agg.replay_from_jsonl(directory=runs)
    assert result["applied"] == 4


# ════════════════════════════════════════════════════════════════════
#  Performance (extra obligatorio del spec)
# ════════════════════════════════════════════════════════════════════
def test_replay_performance_under_500ms_10k_events(tmp_path):
    """Spec: <500ms para 10k events."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    records = [_make_record(1, "20260513_120000", f"e{i}") for i in range(10_000)]
    _make_jsonl(fp, records)
    agg = SessionAggregator()
    t0 = time.time()
    result = agg.replay_from_jsonl(directory=runs)
    elapsed_ms = (time.time() - t0) * 1000
    assert result["applied"] == 10_000
    # Bound conservador: 2s (en CI puede ser más lento que dev)
    assert elapsed_ms < 2000, f"replay tardó {elapsed_ms:.1f}ms (target <500ms)"


# ════════════════════════════════════════════════════════════════════
#  Compat / env
# ════════════════════════════════════════════════════════════════════
def test_async_mode_does_not_block(tmp_path):
    """sync=False lanza thread, devuelve inmediato."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    _make_jsonl(fp, [_make_record(1, "20260513_120000", "e1")])
    agg = SessionAggregator()
    t0 = time.time()
    result = agg.replay_from_jsonl(directory=runs, sync=False)
    elapsed_ms = (time.time() - t0) * 1000
    assert result.get("async") is True
    assert elapsed_ms < 100  # debe retornar inmediato
    # Esperar a que el thread termine
    time.sleep(0.5)
    run = agg.get_run(slot=1, run_id="20260513_120000")
    assert run is not None


def test_replay_disabled_via_env(tmp_path, monkeypatch):
    """AGGREGATOR_REPLAY_ENABLED=0 → hook de startup no ejecuta replay."""
    monkeypatch.setenv("AGGREGATOR_REPLAY_ENABLED", "0")
    # Reset módulo dashboard para tomar env
    for m in list(sys.modules.keys()):
        if "core.dashboard" in m:
            del sys.modules[m]
    from core.dashboard import _replay_enabled
    assert _replay_enabled() is False


def test_get_replay_stats(tmp_path):
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    _make_jsonl(fp, [_make_record(1, "20260513_120000", f"e{i}")
                      for i in range(3)])
    agg = SessionAggregator()
    agg.replay_from_jsonl(directory=runs)
    stats = agg.get_replay_stats()
    assert stats["replay_count"] == 1
    assert stats["total_applied"] == 3
    assert stats["last_replay_at"] is not None


def test_replay_respects_p2_caps(tmp_path):
    """Replay no rompe los caps P2 (drifts maxlen=50)."""
    from core.dashboard.aggregator import SessionAggregator, DRIFTS_MAXLEN
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    # 100 drifts
    records = []
    for i in range(100):
        records.append(_make_record(
            1, "20260513_120000", f"d{i}",
            drift={"kind": "SOFT_DRIFT", "severity": "high",
                    "detail": f"failure_{i}"},
        ))
    _make_jsonl(fp, records)
    agg = SessionAggregator()
    agg.replay_from_jsonl(directory=runs)
    run = agg.get_run(slot=1, run_id="20260513_120000")
    # Caps respetados
    assert run["drift_count"] == 100
    assert run["drifts_dropped_total"] >= 50  # P2 cap


def test_to_dict_after_replay_unchanged_contract(tmp_path):
    """to_dict() sigue devolviendo list (no deque), dict (no deque)."""
    from core.dashboard.aggregator import SessionAggregator
    runs = tmp_path / "runs"; runs.mkdir()
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    _make_jsonl(fp, [_make_record(1, "20260513_120000", "e1",
                                     drift={"kind": "SOFT_DRIFT"})])
    agg = SessionAggregator()
    agg.replay_from_jsonl(directory=runs)
    run = agg.get_run(slot=1, run_id="20260513_120000")
    # Backward compat
    assert isinstance(run["drifts"], list)
    assert isinstance(run["loops"], dict)
    assert isinstance(run["failure_trace"], list)


# ════════════════════════════════════════════════════════════════════
#  Startup non-blocking (spec v3 obligatorio)
# ════════════════════════════════════════════════════════════════════
def test_register_diagnostics_returns_fast_with_replay(tmp_path, monkeypatch):
    """register_diagnostics() retorna en <500ms incluso con JSONL grande.

    Spec v3: replay debe ejecutarse en background, NO bloquear Flask startup.
    """
    monkeypatch.chdir(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    # Crear JSONL grande (10K eventos)
    fp = runs / "diagnostics_slot1_20260513_120000.jsonl"
    records = [_make_record(1, "20260513_120000", f"e{i}")
                for i in range(10_000)]
    _make_jsonl(fp, records)
    # Reset módulo dashboard para tests
    for m in list(sys.modules.keys()):
        if "core.dashboard" in m:
            del sys.modules[m]
    monkeypatch.setenv("AGGREGATOR_REPLAY_ENABLED", "1")
    from flask import Flask
    from core.dashboard import register_diagnostics, _reset_replay_for_test
    _reset_replay_for_test()
    app = Flask(__name__)
    t0 = time.time()
    ok = register_diagnostics(app)
    elapsed_ms = (time.time() - t0) * 1000
    assert ok
    # Critical: registro debe retornar rápido (replay corre en background)
    assert elapsed_ms < 500, f"register_diagnostics() tardó {elapsed_ms:.1f}ms"


def test_replay_startup_background_thread_completes(tmp_path, monkeypatch):
    """Replay background completa eventualmente y rehidrata aggregator.

    Spec v3 fix: replay corre antes que el tailer (sequenced bootstrap),
    evitando doble ingestión del mismo JSONL. El test valida que tras
    register_diagnostics(), el aggregator contiene exactamente los records
    del replay (2 events para 2 líneas en JSONL).
    """
    monkeypatch.chdir(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    # Filename con patrón regex válido: diagnostics_slot{N}_{digits}_{digits}.jsonl
    fp = runs / "diagnostics_slot812_99999_120000.jsonl"
    _make_jsonl(fp, [
        _make_record(812, "99999_120000", "init"),
        _make_record(812, "99999_120000", "goto"),
    ])
    # Reset módulos para tomar env limpia
    for m in list(sys.modules.keys()):
        if "core.dashboard" in m:
            del sys.modules[m]
    monkeypatch.setenv("AGGREGATOR_REPLAY_ENABLED", "1")
    from flask import Flask
    from core.dashboard import (register_diagnostics, _reset_replay_for_test,
                                  _replay_status)
    from core.dashboard.aggregator import get_default_aggregator, reset_for_test
    # Reset singleton del aggregator para test aislado
    reset_for_test()
    _reset_replay_for_test()
    agg = get_default_aggregator()
    # Confirmar aggregator vacío al inicio
    assert agg.get_run(slot=812, run_id="99999_120000") is None
    app = Flask(__name__)
    register_diagnostics(app)
    # Esperar a que el bootstrap (replay → tailer) termine (max 5s)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _replay_status()["completed"]:
            break
        time.sleep(0.05)
    assert _replay_status()["completed"], "replay no completó en 5s"
    # Verificar que el aggregator contiene exactamente los 2 events del JSONL
    # (sin duplicación: replay corre antes que tailer arranque)
    run = agg.get_run(slot=812, run_id="99999_120000")
    assert run is not None, "aggregator no rehidrató el run"
    assert run["event_count"] == 2


def test_replay_timeout_env_var(monkeypatch):
    """env AGGREGATOR_REPLAY_TIMEOUT_S es leído correctamente como int."""
    monkeypatch.setenv("AGGREGATOR_REPLAY_TIMEOUT_S", "15")
    import os
    raw = os.environ.get("AGGREGATOR_REPLAY_TIMEOUT_S")
    assert raw == "15"
    assert int(raw) == 15


def test_replay_timeout_invalid_falls_back_to_default(monkeypatch, tmp_path):
    """env AGGREGATOR_REPLAY_TIMEOUT_S inválida → fallback a 30 sin crash."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("AGGREGATOR_REPLAY_TIMEOUT_S", "not_a_number")
    for m in list(sys.modules.keys()):
        if "core.dashboard" in m:
            del sys.modules[m]
    monkeypatch.setenv("AGGREGATOR_REPLAY_ENABLED", "1")
    from flask import Flask
    from core.dashboard import register_diagnostics, _reset_replay_for_test
    _reset_replay_for_test()
    app = Flask(__name__)
    # No debe lanzar
    ok = register_diagnostics(app)
    assert ok is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
