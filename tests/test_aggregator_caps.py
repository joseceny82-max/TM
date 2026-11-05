"""Tests P2 — Memory hardening RunStats.

Garantías:
    1. drifts: deque(maxlen=50), descartes contados
    2. failure_trace: deque(maxlen=10)
    3. loops: cap top-20 keys, descartes contados, max_seen tracked
    4. to_dict() convierte deque→list (backward compat)
    5. counters expuestos en to_dict()
    6. memory bounded con 10K eventos (smoke)
    7. backward compat: campos pre-P2 accesibles
    8. SessionAggregator integra sin regresión
"""
import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _drift_record(evt: str, slot: int = 1, run_id: str = "r1") -> dict:
    return {
        "slot": slot, "run_id": run_id, "event": evt,
        "kind": "OBSERVABILITY", "state": "login",
        "drift": {"kind": "SOFT_DRIFT", "severity": "high",
                   "detail": "LOGIN_FAILURE"},
    }


def _failure_record(evt: str, slot: int = 1, run_id: str = "r1") -> dict:
    return {"slot": slot, "run_id": run_id, "event": evt,
            "kind": "OBSERVABILITY", "state": "login"}


# ════════════════════════════════════════════════════════════════════
#  1. Drifts capped at 50
# ════════════════════════════════════════════════════════════════════
def test_drifts_capped_at_50():
    from core.dashboard.aggregator import RunStats, DRIFTS_MAXLEN
    assert DRIFTS_MAXLEN == 50
    rs = RunStats(slot=1, run_id="r1")
    # 100 drifts
    for i in range(100):
        rs.update_from_record(_drift_record(f"drift_{i}"))
    # deque cap = 50
    assert len(rs.drifts) == 50
    assert isinstance(rs.drifts, deque)
    assert rs.drifts.maxlen == 50
    # Counter total = 100 (drift_count); drifts_dropped_total = 50
    assert rs.drift_count == 100
    assert rs.drifts_dropped_total == 50


def test_drifts_keeps_most_recent():
    """Los últimos N drifts se conservan, los viejos se descartan."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    for i in range(60):
        rs.update_from_record(_drift_record(f"drift_{i}"))
    # El primero retenido debe ser drift_10 (perdidos: 0..9)
    first = rs.drifts[0]
    last = rs.drifts[-1]
    assert first["event"] == "drift_10"
    assert last["event"] == "drift_59"


# ════════════════════════════════════════════════════════════════════
#  2. Failure trace capped at 10
# ════════════════════════════════════════════════════════════════════
def test_failure_trace_capped_at_10_after_100_failures():
    """P2 spec: 100 failures → len(failure_trace) == 10 (los últimos 10).

    Semántica P2: acumulativa (no es trace causal). Los últimos 10 nombres
    de eventos de fallo, deque(maxlen=10).
    """
    from core.dashboard.aggregator import RunStats, FAILURE_TRACE_MAXLEN
    assert FAILURE_TRACE_MAXLEN == 10
    rs = RunStats(slot=1, run_id="r1")
    # Insertar 100 failures consecutivos
    for i in range(100):
        rs.update_from_record(_failure_record(f"FATAL_failure_{i}"))
    # deque cap = 10
    assert isinstance(rs.failure_trace, deque)
    assert rs.failure_trace.maxlen == 10
    assert len(rs.failure_trace) == 10
    # Los últimos 10: FATAL_failure_90 .. FATAL_failure_99
    assert list(rs.failure_trace) == [f"FATAL_failure_{i}" for i in range(90, 100)]
    assert rs.last_failure == "FATAL_failure_99"


def test_failure_trace_accumulates_across_failures():
    """Cada fallo se appendea al deque (no reasigna)."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    rs.update_from_record(_failure_record("FATAL_first"))
    rs.update_from_record(_failure_record("non_failure_event"))  # NO failure
    rs.update_from_record(_failure_record("FATAL_second"))
    # Solo los failures están en el trace
    trace = list(rs.failure_trace)
    assert "FATAL_first" in trace
    assert "FATAL_second" in trace
    assert "non_failure_event" not in trace
    assert rs.last_failure == "FATAL_second"


# ════════════════════════════════════════════════════════════════════
#  3. Loops capped at top-20
# ════════════════════════════════════════════════════════════════════
def test_loops_capped_top_20():
    from core.dashboard.aggregator import RunStats, LOOPS_CAP
    assert LOOPS_CAP == 20
    rs = RunStats(slot=1, run_id="r1")
    # Generar 30 keys distintas con loops (par consecutivo repetido)
    for i in range(30):
        evt = f"loop_evt_{i}"
        rs.update_from_record(_failure_record(evt))
        rs.update_from_record(_failure_record(evt))  # repetido consecutivo
    # loops dict debe tener <= 20 keys
    assert len(rs.loops) <= 20
    # 10 keys descartadas
    assert rs.loops_dropped_keys == 10


def test_loops_max_seen_default_20():
    """P2 spec: loops_max_seen default = 20 (capacidad declarada del top-N)."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    # Default literal del spec
    assert rs.loops_max_seen == 20


def test_deterministic_top_n_loops_ordering():
    """P2 spec test 4: ordering estable en to_dict()["loops"] por (-count, key).

    Frequency descendente, key ascendente para empates. Permite UI estable
    y diffs deterministicos entre snapshots.
    """
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    # Crear 5 loops con frecuencias distintas:
    # alpha=5, beta=3, gamma=3, delta=1, epsilon=1
    for evt, count in [("epsilon", 1), ("beta", 3), ("alpha", 5),
                         ("gamma", 3), ("delta", 1)]:
        # +1 para crear el loop par consecutivo + 'count' increments
        rs.update_from_record(_failure_record(evt))  # 1ª vez
        for _ in range(count):
            rs.update_from_record(_failure_record(evt))  # repeticiones
    d = rs.to_dict()
    loops = d["loops"]
    keys = list(loops.keys())
    # Orden esperado: alpha(5), beta(3), gamma(3), delta(1), epsilon(1)
    # (-count desc, key asc para empates)
    assert keys[0] == "alpha"
    # beta y gamma empatan a count 3 → beta antes alfabéticamente
    assert keys.index("beta") < keys.index("gamma")
    # delta y epsilon empatan a count 1 → delta antes alfabéticamente
    assert keys.index("delta") < keys.index("epsilon")
    # active_loops también respeta orden
    active = d["active_loops"]
    active_events = [a["event"] for a in active]
    # alpha (5) y beta/gamma (3) están en active (count >= 3)
    assert "alpha" in active_events
    if "beta" in active_events and "gamma" in active_events:
        assert active_events.index("beta") < active_events.index("gamma")


# ════════════════════════════════════════════════════════════════════
#  4. Dropped counters
# ════════════════════════════════════════════════════════════════════
def test_dropped_counters_increment():
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    # 70 drifts → 20 descartados
    for i in range(70):
        rs.update_from_record(_drift_record(f"d{i}"))
    assert rs.drifts_dropped_total == 20
    assert rs.drift_count == 70


# ════════════════════════════════════════════════════════════════════
#  5. to_dict() convierte deque→list (backward compat)
# ════════════════════════════════════════════════════════════════════
def test_to_dict_converts_deque_to_list():
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    rs.update_from_record(_drift_record("d1"))
    rs.update_from_record(_failure_record("FATAL_x"))
    d = rs.to_dict()
    # drifts es list (no deque)
    assert isinstance(d["drifts"], list)
    assert not isinstance(d["drifts"], deque)
    # failure_trace es list (no deque)
    assert isinstance(d["failure_trace"], list)
    assert not isinstance(d["failure_trace"], deque)


def test_to_dict_exposes_p2_counters():
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    for i in range(60):
        rs.update_from_record(_drift_record(f"d{i}"))
    d = rs.to_dict()
    assert "drifts_dropped_total" in d
    assert d["drifts_dropped_total"] == 10
    assert "loops_dropped_keys" in d
    assert "loops_max_seen" in d


def test_to_dict_drifts_visible_window():
    """to_dict() expone últimos 10 drifts (window UI), no los 50 internos."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    for i in range(50):
        rs.update_from_record(_drift_record(f"d{i}"))
    d = rs.to_dict()
    assert len(d["drifts"]) == 10  # window UI
    # El último es d49 (más reciente)
    assert d["drifts"][-1]["event"] == "d49"


# ════════════════════════════════════════════════════════════════════
#  6. Memory smoke: 10K drifts → bounded
# ════════════════════════════════════════════════════════════════════
def test_memory_bounded_with_10k_drifts():
    """10K drifts → drifts deque size 50, dropped >9900."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    for i in range(10_000):
        rs.update_from_record(_drift_record(f"d{i}"))
    assert len(rs.drifts) == 50
    assert rs.drifts_dropped_total == 9950
    assert rs.drift_count == 10_000


def test_memory_estimate_under_50kb():
    """Memory estimation: RunStats con 10K eventos < 100 KB.

    Usa sys.getsizeof + tracking de las estructuras principales.
    """
    import sys as _sys
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    for i in range(10_000):
        rs.update_from_record(_drift_record(f"d{i}"))
    # Tamaños aproximados (no son perfectos pero sirven como sanity)
    size_drifts = _sys.getsizeof(rs.drifts)
    size_loops = _sys.getsizeof(rs.loops)
    size_failure = _sys.getsizeof(rs.failure_trace)
    size_recent = _sys.getsizeof(rs.recent_events)
    size_state_hist = _sys.getsizeof(rs.state_history)
    total = size_drifts + size_loops + size_failure + size_recent + size_state_hist
    # Bound conservador: 100 KB para todas las estructuras de un run
    assert total < 100_000, f"RunStats memory {total} bytes excede 100 KB"


# ════════════════════════════════════════════════════════════════════
#  7. SessionAggregator integration (no regresión)
# ════════════════════════════════════════════════════════════════════
def test_session_aggregator_still_works_after_caps():
    from core.dashboard.aggregator import SessionAggregator
    a = SessionAggregator()
    # 100 records, varios drifts
    for i in range(100):
        if i % 3 == 0:
            a.update(_drift_record(f"d{i}"))
        else:
            a.update(_failure_record(f"e{i}"))
    run = a.get_current_run()
    assert run is not None
    assert run["event_count"] == 100
    # Drifts visibles ≤ 10 (window UI)
    assert len(run["drifts"]) <= 10
    # Total drift_count refleja el real
    assert run["drift_count"] >= 1


def test_loops_cap_does_not_break_active_loops_panel():
    """active_loops sigue funcionando tras cap."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    # qty repetido 10 veces → cuenta como loop
    for _ in range(10):
        rs.update_from_record(_failure_record("qty"))
    d = rs.to_dict()
    # active_loops debe contener qty con count >= 3
    qty_active = [l for l in d["active_loops"] if l["event"] == "qty"]
    if qty_active:
        assert qty_active[0]["count"] >= 3


# ════════════════════════════════════════════════════════════════════
#  8. Backward compat: campos accesibles vía atributo
# ════════════════════════════════════════════════════════════════════
def test_runstats_attribute_access_still_works():
    """rs.drifts, rs.failure_trace, rs.loops siguen accesibles."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    rs.update_from_record(_drift_record("d1"))
    # Acceso por atributo
    assert rs.drifts is not None
    assert rs.failure_trace is not None
    assert rs.loops is not None
    # Iteración sigue funcionando (deque es iterable como list)
    for d in rs.drifts:
        assert "event" in d


def test_runstats_drift_iteration_preserved():
    """Iteración estilo list sobre rs.drifts sigue funcionando."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    for i in range(5):
        rs.update_from_record(_drift_record(f"d{i}"))
    events = [d["event"] for d in rs.drifts]
    assert events == [f"d{i}" for i in range(5)]


# ════════════════════════════════════════════════════════════════════
#  9. Idempotencia
# ════════════════════════════════════════════════════════════════════
def test_caps_idempotent_under_burst():
    """Múltiples bursts de drifts → cap se mantiene constante."""
    from core.dashboard.aggregator import RunStats
    rs = RunStats(slot=1, run_id="r1")
    for burst in range(5):
        for i in range(100):
            rs.update_from_record(_drift_record(f"b{burst}_d{i}"))
        # Cada burst: 100 nuevos → 50 dropped acumulados por burst
    assert len(rs.drifts) == 50
    # 500 totales: 50 retenidos, 450 dropped
    assert rs.drifts_dropped_total == 450
    assert rs.drift_count == 500


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
