"""Tests M5 — SSE priority backpressure.

Garantías:
    1. PrioritizedSSEQueue respeta orden FIFO
    2. NOISE se descarta antes que ACTION/STATE/OBSERVABILITY
    3. OBSERVABILITY se descarta antes que STATE
    4. STATE solo se descarta si todos los items son STATE
    5. Métricas counters correctas (total_dropped, dropped_by_kind)
    6. Thread-safe bajo concurrencia
    7. SSEBroadcaster maneja múltiples clientes con drop independiente
    8. SSEBroadcaster rechaza si max_clients alcanzado
"""
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  1. Comportamiento básico FIFO
# ════════════════════════════════════════════════════════════════════
def test_empty_queue():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=5)
    assert q.qsize() == 0
    assert q.empty()
    assert q.get_nowait() is None


def test_push_under_capacity():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=10)
    for i in range(5):
        ok = q.put_nowait({"event": f"e{i}", "kind": "STATE"})
        assert ok is True
    assert q.qsize() == 5


def test_fifo_order():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=10)
    q.put_nowait({"event": "first", "kind": "STATE"})
    q.put_nowait({"event": "second", "kind": "STATE"})
    q.put_nowait({"event": "third", "kind": "STATE"})
    assert q.get_nowait()["event"] == "first"
    assert q.get_nowait()["event"] == "second"
    assert q.get_nowait()["event"] == "third"


# ════════════════════════════════════════════════════════════════════
#  2. Drop por prioridad
# ════════════════════════════════════════════════════════════════════
def test_noise_dropped_for_state():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=3)
    # Llenar con NOISE
    for i in range(3):
        q.put_nowait({"event": f"shot{i}", "kind": "NOISE"})
    # Push STATE → NOISE más viejo dropped, STATE encolado
    ok = q.put_nowait({"event": "goto", "kind": "STATE"})
    assert ok is True
    assert q.qsize() == 3
    # Stats: 1 NOISE dropped
    stats = q.get_stats()
    assert stats["dropped_by_kind"]["NOISE"] == 1


def test_observability_dropped_for_state():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=3)
    for i in range(3):
        q.put_nowait({"event": f"obs{i}", "kind": "OBSERVABILITY"})
    ok = q.put_nowait({"event": "goto", "kind": "STATE"})
    assert ok is True
    stats = q.get_stats()
    assert stats["dropped_by_kind"]["OBSERVABILITY"] == 1


def test_noise_dropped_for_action():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=3)
    for i in range(3):
        q.put_nowait({"event": f"shot{i}", "kind": "NOISE"})
    ok = q.put_nowait({"event": "click_x", "kind": "ACTION"})
    assert ok is True
    stats = q.get_stats()
    assert stats["dropped_by_kind"]["NOISE"] == 1


def test_state_dropped_only_when_all_state():
    """Si la queue está llena de STATE, un STATE nuevo se descarta (FIFO no aplica)."""
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=2)
    q.put_nowait({"event": "s1", "kind": "STATE"})
    q.put_nowait({"event": "s2", "kind": "STATE"})
    ok = q.put_nowait({"event": "s3", "kind": "STATE"})
    assert ok is False  # rechazado (no hay nada menor)
    stats = q.get_stats()
    assert stats["dropped_by_kind"]["STATE"] == 1


def test_observability_dropped_for_action():
    """OBSERVABILITY prioridad 2, ACTION prioridad 3."""
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=2)
    q.put_nowait({"event": "obs1", "kind": "OBSERVABILITY"})
    q.put_nowait({"event": "obs2", "kind": "OBSERVABILITY"})
    ok = q.put_nowait({"event": "act", "kind": "ACTION"})
    assert ok is True
    stats = q.get_stats()
    assert stats["dropped_by_kind"]["OBSERVABILITY"] == 1


def test_state_kept_priority_unchanged_by_obs():
    """STATE en la queue NO debe substituir por OBSERVABILITY (priority lower)."""
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=2)
    q.put_nowait({"event": "state", "kind": "STATE"})
    q.put_nowait({"event": "state2", "kind": "STATE"})
    ok = q.put_nowait({"event": "obs", "kind": "OBSERVABILITY"})
    assert ok is False  # OBSERVABILITY rechazado


# ════════════════════════════════════════════════════════════════════
#  3. Stats / métricas
# ════════════════════════════════════════════════════════════════════
def test_stats_pressure_pct():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=10)
    for i in range(5):
        q.put_nowait({"event": "e", "kind": "STATE"})
    s = q.get_stats()
    assert s["pressure_pct"] == 50.0
    assert s["size"] == 5
    assert s["maxsize"] == 10


def test_stats_high_pressure_flag():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=10)
    for i in range(10):
        q.put_nowait({"event": "e", "kind": "STATE"})
    s = q.get_stats()
    assert s["is_pressure_high"] is True


def test_stats_counters():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=3)
    for i in range(3):
        q.put_nowait({"event": "x", "kind": "NOISE"})
    for i in range(3):
        q.put_nowait({"event": "x", "kind": "STATE"})
    stats = q.get_stats()
    assert stats["total_pushed"] == 6
    assert stats["total_dropped"] == 3


def test_reset_stats():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=2)
    q.put_nowait({"event": "x", "kind": "NOISE"})
    q.put_nowait({"event": "x", "kind": "NOISE"})
    q.put_nowait({"event": "x", "kind": "STATE"})  # drop NOISE
    assert q.get_stats()["total_dropped"] >= 1
    q.reset_stats()
    assert q.get_stats()["total_dropped"] == 0


# ════════════════════════════════════════════════════════════════════
#  4. Get behavior
# ════════════════════════════════════════════════════════════════════
def test_get_timeout():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=5)
    item = q.get(timeout=0.05)
    assert item is None


def test_get_nowait_empty():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=5)
    assert q.get_nowait() is None


# ════════════════════════════════════════════════════════════════════
#  5. Thread safety
# ════════════════════════════════════════════════════════════════════
def test_concurrent_push_does_not_corrupt():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=100)

    def pusher():
        for i in range(50):
            q.put_nowait({"event": f"e{i}", "kind": "STATE"})

    threads = [threading.Thread(target=pusher) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 200 pushes total con queue maxsize=100 → 100 dropped
    stats = q.get_stats()
    assert stats["total_pushed"] == 200
    assert stats["size"] == 100


# ════════════════════════════════════════════════════════════════════
#  6. SSEBroadcaster
# ════════════════════════════════════════════════════════════════════
def test_broadcaster_subscribe_unsubscribe():
    from core.dashboard.sse_backpressure import SSEBroadcaster
    bc = SSEBroadcaster(max_clients=5, per_client_maxsize=10)
    q1 = bc.subscribe()
    assert q1 is not None
    q2 = bc.subscribe()
    assert q2 is not None
    bc.unsubscribe(q1)
    assert bc.get_stats()["active_clients"] == 1


def test_broadcaster_rejects_when_full():
    from core.dashboard.sse_backpressure import SSEBroadcaster
    bc = SSEBroadcaster(max_clients=2, per_client_maxsize=10)
    bc.subscribe(); bc.subscribe()
    q3 = bc.subscribe()
    assert q3 is None
    assert bc.get_stats()["rejected_subscriptions"] == 1


def test_broadcaster_pushes_to_all_subscribers():
    from core.dashboard.sse_backpressure import SSEBroadcaster
    bc = SSEBroadcaster(max_clients=3, per_client_maxsize=10)
    q1 = bc.subscribe()
    q2 = bc.subscribe()
    n = bc.broadcast({"event": "goto", "kind": "STATE"})
    assert n == 2
    assert q1.get_nowait()["event"] == "goto"
    assert q2.get_nowait()["event"] == "goto"


def test_broadcaster_stats_aggregate():
    from core.dashboard.sse_backpressure import SSEBroadcaster
    bc = SSEBroadcaster(max_clients=3, per_client_maxsize=2)
    bc.subscribe(); bc.subscribe()
    # Llenar y forzar drops
    for i in range(5):
        bc.broadcast({"event": f"e{i}", "kind": "NOISE"})
    stats = bc.get_stats()
    assert stats["active_clients"] == 2
    assert stats["total_pushed"] >= 2  # cada cliente recibió al menos 2


# ════════════════════════════════════════════════════════════════════
#  7. Edge cases
# ════════════════════════════════════════════════════════════════════
def test_handles_record_without_kind():
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=3)
    # Sin kind → UNKNOWN priority
    ok = q.put_nowait({"event": "x"})
    assert ok is True


def test_handles_non_dict_item():
    """Items no dict tratados como UNKNOWN."""
    from core.dashboard.sse_backpressure import PrioritizedSSEQueue
    q = PrioritizedSSEQueue(maxsize=3)
    ok = q.put_nowait("string item")
    assert ok is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
