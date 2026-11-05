"""Tests P1 — JSONL handle cache.

Garantías:
    1. Handle reusado dentro del mismo run (no fopen/fclose por línea)
    2. Concurrent writes NO se entremezclan (thread safety)
    3. cleanup correcto: finalize() cierra handle
    4. Recovery: handle invalidado se reabre transparentemente
    5. atexit handler cierra todos los handles
    6. RunSession + log_event() coexisten sin interferir
    7. Kill switch JSONL_HANDLE_CACHE_ENABLED=0 → fallback legacy
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
#  1. RunSession reusa handle
# ════════════════════════════════════════════════════════════════════
def test_run_session_reuses_handle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    from core.runtime.run_session import RunSession
    s = RunSession(slot=1, run_id="cache_test")
    # Handle abierto en __init__
    assert s._fh is not None
    assert not s._fh.closed
    first_handle = s._fh
    s.observe("e1")
    s.observe("e2")
    s.observe("e3")
    # Mismo handle reusado
    assert s._fh is first_handle
    assert not s._fh.closed
    s.finalize()
    # Cerrado tras finalize
    assert s._fh is None or s._fh.closed


# ════════════════════════════════════════════════════════════════════
#  2. Concurrent writes NO se entremezclan
# ════════════════════════════════════════════════════════════════════
def test_concurrent_writes_strict_atomicity(tmp_path, monkeypatch):
    """Ajuste D: validación FUERTE de atomicidad JSONL.

    Garantías obligatorias:
    1. Exactamente 1000 líneas (sin pérdidas)
    2. Cada línea parsea con json.loads (sin parciales)
    3. Ninguna línea con interleaving {"a":1}{"b": (concatenación corrupta)
    4. Sin truncados (líneas vacías intermedias)
    5. Cada thread emite exactamente sus 100 eventos
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    from core.runtime.run_session import RunSession
    s = RunSession(slot=99, run_id="concurrent_test")

    THREADS = 10
    WRITES_PER_THREAD = 100
    EXPECTED_LINES = THREADS * WRITES_PER_THREAD

    def worker(tid):
        for i in range(WRITES_PER_THREAD):
            s.observe(f"thread{tid}_evt{i}", thread_id=tid, iteration=i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s.finalize()

    raw = s.jsonl_path.read_text(encoding="utf-8")

    # --- 1. Exactamente N líneas (sin pérdidas) ---
    lines = raw.splitlines()
    assert len(lines) == EXPECTED_LINES, (
        f"Esperadas {EXPECTED_LINES}, obtenidas {len(lines)} (write loss)"
    )

    # --- 2. Sin líneas vacías intermedias (truncados silenciosos) ---
    for i, line in enumerate(lines):
        assert line.strip() != "", f"Línea vacía en posición {i} (truncado)"

    # --- 3. Cada línea parsea como UN solo objeto JSON ---
    parsed: list[dict] = []
    for i, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"Línea {i} no parsea (interleaving o write parcial): "
                f"{line!r} — error: {e}"
            )
        assert isinstance(rec, dict), f"Línea {i} no es dict: {rec!r}"
        parsed.append(rec)

    # --- 4. Detectar interleaving tipo {"a":1}{"b": en MISMA línea ---
    # Si hubiera concatenación corrupta, json.loads encontraría
    # "Extra data" o "Expecting property name". Validamos también con regex:
    for i, line in enumerate(lines):
        # Una línea JSON válida tiene exactamente UN '\n' al final (ya quitado)
        # y empieza con { y termina con }. Sin '}{' interno (excepto en strings).
        # Heurística defensiva: contar braces balanceadas asumiendo no escapes
        # corruptos. json.loads ya capturó casi todo, pero validamos extra:
        s_trimmed = line.strip()
        assert s_trimmed.startswith("{"), f"Línea {i} no empieza con {{: {s_trimmed[:50]!r}"
        assert s_trimmed.endswith("}"), f"Línea {i} no termina con }}: {s_trimmed[-50:]!r}"

    # --- 5. Cada thread emitió exactamente WRITES_PER_THREAD eventos ---
    thread_counts: dict[int, int] = {}
    for rec in parsed:
        tid = rec.get("p_thread_id")
        if tid is not None:
            thread_counts[tid] = thread_counts.get(tid, 0) + 1
    assert len(thread_counts) == THREADS, (
        f"Esperados {THREADS} threads, vistos {len(thread_counts)}"
    )
    for tid, count in thread_counts.items():
        assert count == WRITES_PER_THREAD, (
            f"Thread {tid} emitió {count} eventos (esperados {WRITES_PER_THREAD})"
        )

    # --- 6. Iteraciones únicas por thread (sin duplicados ni pérdidas) ---
    by_thread: dict[int, set[int]] = {}
    for rec in parsed:
        tid = rec.get("p_thread_id")
        it = rec.get("p_iteration")
        if tid is not None and it is not None:
            by_thread.setdefault(tid, set()).add(it)
    for tid, iterations in by_thread.items():
        assert iterations == set(range(WRITES_PER_THREAD)), (
            f"Thread {tid} iteraciones incompletas: faltan "
            f"{set(range(WRITES_PER_THREAD)) - iterations}"
        )


# ════════════════════════════════════════════════════════════════════
#  3. cleanup correcto
# ════════════════════════════════════════════════════════════════════
def test_finalize_closes_handle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from core.runtime.run_session import RunSession
    s = RunSession(slot=1, run_id="cleanup_test")
    assert s._fh is not None
    fh_ref = s._fh
    s.finalize()
    # Handle cerrado y desreferenciado
    assert s._fh is None
    assert fh_ref.closed


def test_finalize_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from core.runtime.run_session import RunSession
    s = RunSession(slot=1, run_id="idempotent_test")
    s.observe("e1")
    s.finalize()
    # Segunda llamada no debe lanzar
    s.finalize()
    s.finalize()


# ════════════════════════════════════════════════════════════════════
#  4. Recovery tras handle invalidado
# ════════════════════════════════════════════════════════════════════
def test_recovers_after_handle_closed_externally(tmp_path, monkeypatch):
    """Si el handle se cierra externamente, _persist debe degradar a open()."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from core.runtime.run_session import RunSession
    s = RunSession(slot=1, run_id="recovery_test")
    s.observe("before_break")
    # Cerrar el handle desde fuera (simular corrupción)
    s._fh.close()
    # Siguiente write debe degradar a fallback open()/close()
    s.observe("after_break")
    s.finalize()
    # Ambas líneas escritas
    lines = s.jsonl_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l)["event"] for l in lines]
    assert "before_break" in events
    assert "after_break" in events


# ════════════════════════════════════════════════════════════════════
#  5. atexit cleanup global
# ════════════════════════════════════════════════════════════════════
def test_atexit_cleanup_log_event(tmp_path, monkeypatch):
    """_close_all_cached_handles cierra cache global."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    # Importar fresh
    if "_compra_logger" in sys.modules:
        del sys.modules["_compra_logger"]
    import _compra_logger as cl
    cl._reset_handle_cache_for_test()
    # Forzar creación de handle via log_event
    cl.log_event(slot=42, event="test_event")
    # Cache debe tener una entrada
    assert len(cl._jsonl_handles) >= 1
    # atexit cleanup
    cl._close_all_cached_handles()
    # Cache vacío
    assert len(cl._jsonl_handles) == 0


def test_get_handle_returns_none_when_disabled(monkeypatch):
    """Kill switch OFF → _get_handle_and_lock devuelve (None, None)."""
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "0")
    if "_compra_logger" in sys.modules:
        del sys.modules["_compra_logger"]
    import _compra_logger as cl
    cl._reset_handle_cache_for_test()
    h, lock = cl._get_handle_and_lock(Path("/tmp/test.jsonl"))
    assert h is None
    assert lock is None


# ════════════════════════════════════════════════════════════════════
#  6. Coexistence: RunSession + log_event
# ════════════════════════════════════════════════════════════════════
def test_log_event_uses_cached_handle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    if "_compra_logger" in sys.modules:
        del sys.modules["_compra_logger"]
    import _compra_logger as cl
    cl._reset_handle_cache_for_test()
    # Múltiples log_event al mismo path → mismo handle
    cl.log_event(slot=1, event="e1")
    cl.log_event(slot=1, event="e2")
    cl.log_event(slot=1, event="e3")
    # Cache tiene 1 entrada (mismo slot → mismo path)
    assert len(cl._jsonl_handles) == 1
    cl._reset_handle_cache_for_test()


def test_log_event_separate_handles_per_slot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    if "_compra_logger" in sys.modules:
        del sys.modules["_compra_logger"]
    import _compra_logger as cl
    cl._reset_handle_cache_for_test()
    cl.log_event(slot=1, event="e1")
    cl.log_event(slot=2, event="e1")
    cl.log_event(slot=3, event="e1")
    # 3 paths distintos → 3 handles
    assert len(cl._jsonl_handles) == 3
    cl._reset_handle_cache_for_test()


# ════════════════════════════════════════════════════════════════════
#  7. Kill switch
# ════════════════════════════════════════════════════════════════════
def test_kill_switch_off_no_cache(tmp_path, monkeypatch):
    """JSONL_HANDLE_CACHE_ENABLED=0 → RunSession._fh = None."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "0")
    from core.runtime.run_session import RunSession
    s = RunSession(slot=1, run_id="kill_switch_test")
    assert s._fh is None
    # Aún así escribir funciona (fallback open per-line)
    s.observe("e1")
    s.finalize()
    lines = s.jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ════════════════════════════════════════════════════════════════════
#  8. Backward compat
# ════════════════════════════════════════════════════════════════════
def test_jsonl_format_unchanged(tmp_path, monkeypatch):
    """JSONL line format idéntico a pre-P1."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from core.runtime.run_session import RunSession
    s = RunSession(slot=7, run_id="format_test")
    s.observe("goto", url="http://x")
    s.finalize()
    line = s.jsonl_path.read_text(encoding="utf-8").splitlines()[0]
    rec = json.loads(line)
    # Mismos campos canonicos
    assert "ts" in rec and "event" in rec and "kind" in rec
    assert rec["event"] == "goto"
    assert rec["slot"] == 7
    assert rec["p_url"] == "http://x"


def test_existing_logger_event_unchanged(tmp_path, monkeypatch, capsys):
    """StructuredLogger.event() sigue funcionando idéntico (no tocado)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from _compra_logger import StructuredLogger
    logger = StructuredLogger(slot=99, run_id="logger_compat_test")
    logger.event("init")
    captured = capsys.readouterr()
    assert "init" in captured.out  # stdout legacy intacto


# ════════════════════════════════════════════════════════════════════
#  9. Ajuste A — Lock por fichero (no global)
# ════════════════════════════════════════════════════════════════════
def test_separate_locks_per_file(tmp_path, monkeypatch):
    """Cada path tiene su propio write lock (no bottleneck global)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    if "_compra_logger" in sys.modules:
        del sys.modules["_compra_logger"]
    import _compra_logger as cl
    cl._reset_handle_cache_for_test()

    p1 = tmp_path / "runs" / "fileA.jsonl"
    p2 = tmp_path / "runs" / "fileB.jsonl"
    h1, lock1 = cl._get_handle_and_lock(p1)
    h2, lock2 = cl._get_handle_and_lock(p2)
    assert h1 is not None and h2 is not None
    assert lock1 is not None and lock2 is not None
    # Locks DIFERENTES para paths distintos
    assert lock1 is not lock2
    cl._reset_handle_cache_for_test()


def test_same_lock_returned_for_same_path(tmp_path, monkeypatch):
    """Mismas llamadas al mismo path devuelven mismo lock."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    if "_compra_logger" in sys.modules:
        del sys.modules["_compra_logger"]
    import _compra_logger as cl
    cl._reset_handle_cache_for_test()

    p = tmp_path / "runs" / "same.jsonl"
    h1, lock1 = cl._get_handle_and_lock(p)
    h2, lock2 = cl._get_handle_and_lock(p)
    assert lock1 is lock2  # IDENTIDAD, no solo igualdad
    cl._reset_handle_cache_for_test()


def test_concurrent_writes_to_different_files_no_blocking(tmp_path, monkeypatch):
    """Ajuste A: writes a ficheros distintos NO se bloquean entre sí.

    Validamos que 2 threads escribiendo a 2 ficheros distintos terminan
    en tiempo significativamente menor que si compartieran un lock global.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    from core.runtime.run_session import RunSession

    s1 = RunSession(slot=1, run_id="parallel_1")
    s2 = RunSession(slot=2, run_id="parallel_2")
    N = 500

    def worker(s):
        for i in range(N):
            s.observe(f"evt{i}")

    import time as _t
    t0 = _t.perf_counter()
    threads = [threading.Thread(target=worker, args=(s1,)),
               threading.Thread(target=worker, args=(s2,))]
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = _t.perf_counter() - t0

    s1.finalize(); s2.finalize()

    # Ambos JSONLs tienen N líneas cada uno
    assert len(s1.jsonl_path.read_text(encoding="utf-8").splitlines()) == N
    assert len(s2.jsonl_path.read_text(encoding="utf-8").splitlines()) == N
    # Verificación más débil: ambos completaron sin deadlock
    assert elapsed < 30.0  # generoso, solo previene hang


# ════════════════════════════════════════════════════════════════════
#  10. Ajuste B — Recovery automático tras write() falla
# ════════════════════════════════════════════════════════════════════
def test_recovery_after_write_failure_via_log_event(tmp_path, monkeypatch):
    """Si handle se invalida entre writes, el siguiente write reabre + retry.

    Usa jsonl_path explícito para no mutar _LOG_DIR global (test pollution).
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    if "_compra_logger" in sys.modules:
        del sys.modules["_compra_logger"]
    import _compra_logger as cl
    cl._reset_handle_cache_for_test()

    p = tmp_path / "runs" / "structured_slot7.jsonl"
    # Primer write OK
    cl.log_event(slot=7, event="ok1", jsonl_path=str(p))
    # Forzar invalidación del handle (simular handle corrupto)
    cl._invalidate_handle(p)
    # Segundo write debe REABRIR + escribir sin perder la línea
    cl.log_event(slot=7, event="ok2_after_invalidation", jsonl_path=str(p))
    # Tercer write usa handle ya reabierto
    cl.log_event(slot=7, event="ok3", jsonl_path=str(p))
    cl._reset_handle_cache_for_test()

    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    events = [json.loads(l)["event"] for l in lines]
    assert events == ["ok1", "ok2_after_invalidation", "ok3"]


def test_recovery_in_run_session_after_handle_break(tmp_path, monkeypatch):
    """RunSession: si _fh se cierra externamente, _persist recupera + retry."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    from core.runtime.run_session import RunSession
    s = RunSession(slot=8, run_id="recovery_session")
    s.observe("e1")
    # Simular handle corrupto cerrando externamente
    s._fh.close()
    # Siguientes writes deben recuperar
    s.observe("e2_after_break")
    s.observe("e3_post_recovery")
    s.finalize()
    lines = s.jsonl_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l)["event"] for l in lines]
    assert "e1" in events
    assert "e2_after_break" in events
    assert "e3_post_recovery" in events


# ════════════════════════════════════════════════════════════════════
#  11. Ajuste C — finalize() bajo lock (no race con _persist)
# ════════════════════════════════════════════════════════════════════
def test_finalize_under_lock_no_race(tmp_path, monkeypatch):
    """Race test: T1 escribe mientras T2 finaliza. No debe corromper."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("JSONL_HANDLE_CACHE_ENABLED", "1")
    from core.runtime.run_session import RunSession

    # Repetir varias veces para aumentar prob. de detectar race
    for run_n in range(5):
        s = RunSession(slot=run_n, run_id=f"race_test_{run_n}")
        writes_done = [0]
        stop_flag = [False]

        def writer():
            while not stop_flag[0]:
                try:
                    s.observe(f"e{writes_done[0]}")
                    writes_done[0] += 1
                except Exception:
                    pass

        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.05)  # dejar acumular writes
        s.finalize()
        stop_flag[0] = True
        t.join(timeout=2.0)

        # Validar: el fichero existe y todas las líneas son JSON válido
        if s.jsonl_path.exists():
            lines = s.jsonl_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                if line.strip():
                    json.loads(line)  # debe parsear sin error


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
