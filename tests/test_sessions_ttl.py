"""Tests P3 — Session lifecycle hardening (TTL + LRU + burst-safe).

Garantías obligatorias (spec):
    1. test_session_ttl_eviction_after_idle
    2. test_max_sessions_lru_eviction
    3. test_no_eviction_during_burst_activity
    4. test_reinit_after_ttl_finalize
    5. test_no_file_handle_double_write
    6. test_sessions_dont_leak_memory_10k_events
    7. test_daemon_cpu_stability

Stress:
    - 200 sessions burst → max 100 active
    - 10K event load → no growth infinito
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


def _fresh_module(monkeypatch, **env):
    """Re-import runtime_diagnostics con env nuevo + sin daemon iniciado."""
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    # Reset modules
    for mod in list(sys.modules.keys()):
        if "runtime_diagnostics" in mod or "run_session" in mod:
            del sys.modules[mod]
    import core.observability.runtime_diagnostics as rd
    rd.reset_all()
    rd._stop_daemon_for_test()
    return rd


# ════════════════════════════════════════════════════════════════════
#  1. TTL eviction after idle
# ════════════════════════════════════════════════════════════════════
def test_session_ttl_eviction_after_idle(tmp_path, monkeypatch):
    """Sesión sin observe() > TTL → daemon evicta."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=1,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=0,  # sin burst guard
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    rd.observe(event_name="init", payload={"slot": 99})
    info = rd.get_info()
    assert 99 in info["active_sessions"]
    # Esperar > TTL
    time.sleep(1.5)
    # Forzar cleanup manual (no esperar daemon)
    rd._force_cleanup_for_test()
    info = rd.get_info()
    assert 99 not in info["active_sessions"]
    assert info["stats"]["ttl_evictions_total"] >= 1


def test_session_not_evicted_within_ttl(tmp_path, monkeypatch):
    """Sesión activa < TTL → NO se evicta."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=5,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    rd.observe(event_name="init", payload={"slot": 1})
    time.sleep(0.5)  # < TTL
    rd._force_cleanup_for_test()
    info = rd.get_info()
    assert 1 in info["active_sessions"]
    assert info["stats"]["ttl_evictions_total"] == 0


# ════════════════════════════════════════════════════════════════════
#  2. MAX_SESSIONS LRU eviction
# ════════════════════════════════════════════════════════════════════
def test_max_sessions_lru_eviction(tmp_path, monkeypatch):
    """Crear > MAX_SESSIONS → LRU evicta los más viejos por last_observe_at."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=3600,  # TTL alto, no interferir
                       SESSIONS_MAX=5,
                       SESSIONS_BURST_WINDOW_S=0)
    # Crear 10 sesiones con last_observe_at distinto (slot=i creado en orden i)
    for i in range(10):
        rd.observe(event_name="init", payload={"slot": i})
        time.sleep(0.01)  # asegurar last_observe_at distinto
    rd._force_cleanup_for_test()
    info = rd.get_info()
    # Solo deben quedar las 5 más recientes (slots 5..9)
    active = set(info["active_sessions"])
    assert len(active) == 5
    # Los más viejos (0..4) deben haber sido evictados
    for old in range(5):
        assert old not in active
    # Los más nuevos (5..9) deben estar
    for new in range(5, 10):
        assert new in active
    assert info["stats"]["lru_evictions_total"] >= 5


# ════════════════════════════════════════════════════════════════════
#  3. Burst guard: no evict durante actividad continua
# ════════════════════════════════════════════════════════════════════
def test_no_eviction_during_burst_activity(tmp_path, monkeypatch):
    """Si llegan observes seguidos (delta < BURST_WINDOW), NO evictar.

    Aunque el último observe haya sido > TTL atrás, si la actividad fue
    continua hasta hace poco (no_activity_burst=True), no se evicta.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=1,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=10,  # ventana burst larga
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    # Burst: 3 observes muy seguidos (delta << burst window)
    rd.observe(event_name="e1", payload={"slot": 7})
    rd.observe(event_name="e2", payload={"slot": 7})
    rd.observe(event_name="e3", payload={"slot": 7})
    # Inmediato cleanup (TTL puede haberse cumplido para algunos, pero burst guarda)
    # Para forzar idle > TTL, esperamos un poco
    time.sleep(1.2)
    rd._force_cleanup_for_test()
    info = rd.get_info()
    # Burst guard activo → NO evict
    assert 7 in info["active_sessions"]


def test_eviction_after_burst_ends(tmp_path, monkeypatch):
    """Si tras burst hay idle prolongado, sí se evicta (burst se desactiva)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=1,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=1,  # burst corto
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    # Inicial: 1 observe
    rd.observe(event_name="e1", payload={"slot": 8})
    # Esperar > burst_window (burst se rompe)
    time.sleep(1.2)
    # Segundo observe: delta = 1.2s > burst (1s), no_activity_burst=False
    rd.observe(event_name="e2", payload={"slot": 8})
    # Esperar > TTL
    time.sleep(1.5)
    rd._force_cleanup_for_test()
    info = rd.get_info()
    # Burst no protegió → evictado
    assert 8 not in info["active_sessions"]


# ════════════════════════════════════════════════════════════════════
#  4. Reinit after TTL finalize
# ════════════════════════════════════════════════════════════════════
def test_reinit_after_ttl_finalize(tmp_path, monkeypatch):
    """Si sesión es evictada por TTL, próximo observe() la recrea."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=1,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    rd.observe(event_name="init", payload={"slot": 42})
    # Forzar eviction TTL
    time.sleep(1.2)
    rd._force_cleanup_for_test()
    info = rd.get_info()
    assert 42 not in info["active_sessions"]
    # Nuevo observe debe recrear
    rd.observe(event_name="re_init", payload={"slot": 42})
    info = rd.get_info()
    assert 42 in info["active_sessions"]
    assert info["stats"]["reinits_after_ttl_total"] >= 1


# ════════════════════════════════════════════════════════════════════
#  5. No file handle double write tras eviction (P1 compat)
# ════════════════════════════════════════════════════════════════════
def test_no_file_handle_double_write(tmp_path, monkeypatch):
    """Tras eviction TTL, el JSONL no tiene escrituras corruptas.

    Compat P1: RunSession.finalize() cierra handle bajo lock; siguiente
    observe() recrea sesión + nuevo handle. JSONL del nuevo run separa.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=1,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    # 5 observes pre-eviction
    for i in range(5):
        rd.observe(event_name=f"pre_{i}", payload={"slot": 1})
    time.sleep(1.2)
    rd._force_cleanup_for_test()
    # 5 observes post-reinit
    for i in range(5):
        rd.observe(event_name=f"post_{i}", payload={"slot": 1})
    # Buscar JSONL del slot 1 (puede ser uno o varios según run_id)
    files = list((tmp_path / "runs").glob("diagnostics_slot1_*.jsonl"))
    assert len(files) >= 1
    total_lines: list[str] = []
    for fp in files:
        total_lines.extend(fp.read_text(encoding="utf-8").splitlines())
    # Cada línea es JSON válido (no corruption)
    for line in total_lines:
        if line.strip():
            rec = json.loads(line)  # debe parsear
            assert "event" in rec
    # Al menos 10 eventos (5 pre + 5 post)
    events = [json.loads(l)["event"] for l in total_lines if l.strip()]
    pre_count = sum(1 for e in events if e.startswith("pre_"))
    post_count = sum(1 for e in events if e.startswith("post_"))
    assert pre_count == 5
    assert post_count == 5


# ════════════════════════════════════════════════════════════════════
#  6. Sessions don't leak memory 10K events
# ════════════════════════════════════════════════════════════════════
def test_sessions_dont_leak_memory_10k_events(tmp_path, monkeypatch):
    """10K observes a 50 slots rotativos → activos ≤ MAX, no leak en metadata."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=3600,
                       SESSIONS_MAX=10,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    # 10K observes round-robin a 50 slots distintos
    for i in range(10_000):
        rd.observe(event_name="e", payload={"slot": i % 50})
        if i % 100 == 0:
            rd._force_cleanup_for_test()
    rd._force_cleanup_for_test()
    info = rd.get_info()
    # No más de MAX sesiones activas
    assert info["active_sessions_count"] <= 10
    # Metadata bounded (no growth infinito)
    assert info["metadata_entries"] <= 50  # 50 unique slots


# ════════════════════════════════════════════════════════════════════
#  7. Daemon CPU stability (smoke)
# ════════════════════════════════════════════════════════════════════
def test_daemon_cpu_stability(tmp_path, monkeypatch):
    """Daemon corre 2s sin causar crash. Adaptive sleep funciona."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=10,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=1,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    rd.observe(event_name="init", payload={"slot": 1})
    # Daemon ya arrancó (ensure_daemon_started en primer observe)
    time.sleep(2.5)  # 2-3 iteraciones del daemon
    info = rd.get_info()
    assert info["daemon_started"] is True
    # Sin errores acumulados
    assert info["stats"]["daemon_errors"] == 0
    # Sesión sigue activa (no idle > TTL)
    assert 1 in info["active_sessions"]
    rd._stop_daemon_for_test()


# ════════════════════════════════════════════════════════════════════
#  Stress: 1000 sessions burst → max 100 activas (spec literal)
# ════════════════════════════════════════════════════════════════════
def test_stress_1000_sessions_burst(tmp_path, monkeypatch):
    """1000 sesiones creadas rápido → cap 100 vía LRU. No crash."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=3600,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    for i in range(1000):
        rd.observe(event_name="init", payload={"slot": i})
        # Force cleanup intermedio cada 100 para evitar runaway intermedio
        if i % 100 == 99:
            rd._force_cleanup_for_test()
    rd._force_cleanup_for_test()
    info = rd.get_info()
    # Después de las evictions, max 100 activas
    assert info["active_sessions_count"] <= 100
    # Al menos 900 evictions (1000 creados - 100 retenidos)
    assert info["stats"]["lru_evictions_total"] >= 900


def test_stress_200_sessions_burst(tmp_path, monkeypatch):
    """Caso intermedio: 200 sesiones rápido → cap 100 vía LRU."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=3600,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    for i in range(200):
        rd.observe(event_name="init", payload={"slot": i})
    rd._force_cleanup_for_test()
    info = rd.get_info()
    assert info["active_sessions_count"] <= 100
    assert info["stats"]["lru_evictions_total"] >= 100


def test_daemon_idempotent_and_restart_safe(tmp_path, monkeypatch):
    """Daemon: arrancar 2 veces NO crea 2 threads; tras stop+restart, funciona.

    Spec: "test daemon idempotent + restart safe".
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=2,
                       SESSIONS_MAX=100,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    # 1ª llamada: arranca daemon
    rd._ensure_daemon_started()
    first_thread = rd._daemon_thread
    assert first_thread is not None
    assert first_thread.is_alive()
    # 2ª llamada: idempotente — mismo thread
    rd._ensure_daemon_started()
    rd._ensure_daemon_started()
    assert rd._daemon_thread is first_thread
    # Stop + restart safe
    rd._stop_daemon_for_test()
    assert rd._daemon_started is False
    # Volver a arrancar
    rd._ensure_daemon_started()
    second_thread = rd._daemon_thread
    assert second_thread is not None
    # Tras stop, el _ensure crea un nuevo thread (no reusable el viejo)
    assert second_thread.is_alive()
    # Funcionalidad post-restart: TTL eviction sigue operando
    rd.observe(event_name="init", payload={"slot": 99})
    time.sleep(2.2)
    rd._force_cleanup_for_test()
    info = rd.get_info()
    assert 99 not in info["active_sessions"]
    rd._stop_daemon_for_test()


def test_stress_10k_events_no_crash(tmp_path, monkeypatch):
    """10K eventos sobre 20 slots, sin crash, sin growth infinito."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=3600,
                       SESSIONS_MAX=20,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    for i in range(10_000):
        rd.observe(event_name="evt", payload={"slot": i % 20})
    info = rd.get_info()
    assert info["active_sessions_count"] <= 20
    assert info["stats"]["daemon_errors"] == 0


# ════════════════════════════════════════════════════════════════════
#  Compat tests
# ════════════════════════════════════════════════════════════════════
def test_ttl_disabled_no_eviction(tmp_path, monkeypatch):
    """SESSIONS_TTL_ENABLED=0 → daemon NO arranca, no eviction."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_ENABLED=0,
                       SESSIONS_TTL_S=1)
    rd.observe(event_name="init", payload={"slot": 1})
    time.sleep(1.5)
    info = rd.get_info()
    assert info["daemon_started"] is False
    # La sesión sigue activa (no daemon → no eviction)
    assert 1 in info["active_sessions"]


def test_finalize_explicit_works(tmp_path, monkeypatch):
    """finalize() explícito sigue funcionando como antes."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch)
    rd.observe(event_name="init", payload={"slot": 99})
    summary = rd.finalize(slot=99)
    assert summary is not None
    info = rd.get_info()
    assert 99 not in info["active_sessions"]


def test_get_info_exposes_p3_config(tmp_path, monkeypatch):
    """get_info() incluye config P3 + stats."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch)
    info = rd.get_info()
    assert "ttl_s" in info
    assert "max_sessions" in info
    assert "burst_window_s" in info
    assert "stats" in info
    assert "ttl_evictions_total" in info["stats"]
    assert "lru_evictions_total" in info["stats"]


def test_observe_fail_safe_under_eviction(tmp_path, monkeypatch):
    """observe() no lanza ante eviciones concurrentes."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    rd = _fresh_module(monkeypatch,
                       SESSIONS_TTL_S=1,
                       SESSIONS_MAX=5,
                       SESSIONS_BURST_WINDOW_S=0,
                       SESSIONS_CLEANUP_INTERVAL_S=1)
    errors: list[Exception] = []

    def worker():
        try:
            for i in range(500):
                rd.observe(event_name=f"e{i}", payload={"slot": i % 10})
                if i % 50 == 0:
                    rd._force_cleanup_for_test()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []
    info = rd.get_info()
    assert info["stats"]["daemon_errors"] == 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
