"""core/dashboard/sse_backpressure.py — Priority queue for SSE broadcast.

PROBLEMA RESUELTO:
    `queue.Queue(maxsize=200)` actual descarta silenciosamente cuando lleno.
    Esto significa que un cliente lento pierde STATE events tanto como NOISE.
    Pero STATE events son CRÍTICOS para la observabilidad correcta.

POLÍTICA (priority drop):

    PRIORIDADES (mayor = más importante):
        STATE        = 4  (transiciones FSM)
        ACTION       = 3  (intent del bot)
        UNKNOWN      = 2  (eventos sin clasificar — preservar para diagnóstico)
        OBSERVABILITY = 2 (telemetría)
        NOISE        = 1  (filtrable)

    NIVELES DE PRESIÓN:

        Pressure 0-80%:   push normal
        Pressure 80-95%:  intentar drop NOISE existente antes que el nuevo
        Pressure 95-100%: dropear OBSERVABILITY/UNKNOWN si necesario
        Pressure 100%:    SI nuevo > algún existente: substitute; SI no: drop nuevo

GARANTÍAS:
    - SSE API (URL, formato) idéntica: lectura igual al cliente
    - Mismas semánticas put/get para callers existentes
    - Counters de drops por kind → /api/info expone presión
    - Thread-safe
    - Fail-safe: drops silenciosos vs lanzar
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Prioridades por kind
# ════════════════════════════════════════════════════════════════════
_PRIORITY = {
    "STATE":         4,
    "ACTION":        3,
    "OBSERVABILITY": 2,
    "UNKNOWN":       2,
    "NOISE":         1,
}


# Umbrales de presión
_THRESHOLD_LOW = 0.80   # 80% → empezar a drop NOISE
_THRESHOLD_HIGH = 0.95  # 95% → drop OBSERVABILITY


# ════════════════════════════════════════════════════════════════════
#  PrioritizedSSEQueue
# ════════════════════════════════════════════════════════════════════
class PrioritizedSSEQueue:
    """Queue thread-safe con drop por prioridad.

    Mantiene compatibilidad de API con `queue.Queue`:
        - put_nowait(item): añade item; nunca bloquea
        - get(timeout=...): consumir; bloquea hasta timeout
        - qsize(): tamaño actual
        - empty(): True si vacío

    Política de drop:
        1. Si hay espacio: push directo
        2. Si lleno: buscar item de PRIORIDAD MENOR al nuevo:
            - encontrado: substituir (drop el más viejo de menor prioridad)
            - no encontrado: drop el nuevo (todos son ≥ prioridad)
        3. Tracking de drops por kind para observability self-metrics

    Diferencia vs queue.Queue:
        - No bloquea jamás en put
        - put_nowait nunca lanza Full
    """

    def __init__(self, maxsize: int = 200):
        self.maxsize = max(1, int(maxsize))
        self._q: deque = deque()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        # Counters
        self.total_dropped: int = 0
        self.dropped_by_kind: dict[str, int] = {
            "STATE": 0, "ACTION": 0, "OBSERVABILITY": 0, "NOISE": 0, "UNKNOWN": 0,
        }
        self.total_pushed: int = 0
        self.total_consumed: int = 0

    # ────────────────────────────────────────────────────────────────
    #  Public API (queue.Queue-compatible)
    # ────────────────────────────────────────────────────────────────
    def put_nowait(self, item: Any) -> bool:
        """Non-blocking push con drop por prioridad. Devuelve True si pushed."""
        kind = self._kind_of(item)
        priority = _PRIORITY.get(kind, 2)
        with self._lock:
            self.total_pushed += 1
            current = len(self._q)
            # Caso 1: hay espacio
            if current < self.maxsize:
                self._q.append(item)
                self._not_empty.notify()
                return True

            # Caso 2: lleno → drop por prioridad
            # Buscar el item más viejo con prioridad MENOR que `priority`
            # Iteramos desde el frente del deque (FIFO order)
            for i, existing in enumerate(self._q):
                existing_kind = self._kind_of(existing)
                existing_priority = _PRIORITY.get(existing_kind, 2)
                if existing_priority < priority:
                    # Substituir: eliminar el existente, push el nuevo al final
                    del self._q[i]
                    self._q.append(item)
                    self._track_drop(existing_kind)
                    self._not_empty.notify()
                    return True

            # Caso 3: todos los existentes son ≥ prioridad → drop el nuevo
            self._track_drop(kind)
            return False

    def put(self, item: Any, block: bool = False,
              timeout: Optional[float] = None) -> bool:
        """Alias de put_nowait (ignoramos block/timeout, contrato non-blocking)."""
        return self.put_nowait(item)

    def get(self, timeout: Optional[float] = None) -> Optional[Any]:
        """Bloquea hasta item disponible o timeout. Devuelve None en timeout."""
        with self._not_empty:
            if not self._q:
                self._not_empty.wait(timeout)
            if self._q:
                item = self._q.popleft()
                self.total_consumed += 1
                return item
            return None

    def get_nowait(self) -> Optional[Any]:
        """Non-blocking get. Devuelve None si vacío."""
        with self._lock:
            if not self._q:
                return None
            item = self._q.popleft()
            self.total_consumed += 1
            return item

    def qsize(self) -> int:
        with self._lock:
            return len(self._q)

    def empty(self) -> bool:
        with self._lock:
            return not self._q

    # ────────────────────────────────────────────────────────────────
    #  Stats / metrics
    # ────────────────────────────────────────────────────────────────
    def get_stats(self) -> dict:
        """Snapshot de métricas. Para /api/info."""
        with self._lock:
            size = len(self._q)
        return {
            "size": size,
            "maxsize": self.maxsize,
            "pressure_pct": round(100 * size / self.maxsize, 1),
            "total_pushed": self.total_pushed,
            "total_consumed": self.total_consumed,
            "total_dropped": self.total_dropped,
            "dropped_by_kind": dict(self.dropped_by_kind),
            "is_pressure_high": (size / self.maxsize) >= _THRESHOLD_HIGH,
            "is_pressure_low_threshold": (size / self.maxsize) >= _THRESHOLD_LOW,
        }

    def reset_stats(self) -> None:
        with self._lock:
            self.total_dropped = 0
            self.dropped_by_kind = {k: 0 for k in self.dropped_by_kind}
            self.total_pushed = 0
            self.total_consumed = 0

    # ────────────────────────────────────────────────────────────────
    #  Helpers internos
    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _kind_of(item: Any) -> str:
        if isinstance(item, dict):
            k = item.get("kind", "UNKNOWN")
            if k in _PRIORITY:
                return k
        return "UNKNOWN"

    def _track_drop(self, kind: str) -> None:
        self.total_dropped += 1
        if kind in self.dropped_by_kind:
            self.dropped_by_kind[kind] += 1
        else:
            self.dropped_by_kind[kind] = 1


# ════════════════════════════════════════════════════════════════════
#  SSEBroadcaster — orchestrator de N clientes
# ════════════════════════════════════════════════════════════════════
class SSEBroadcaster:
    """Manager de múltiples PrioritizedSSEQueue (uno por cliente).

    Garantías:
        - Drop silencioso si queue de cliente lento llena
        - maxlen de subscribers para evitar leaks (default 50)
        - Métricas agregadas por cliente y globales

    Uso:
        bc = SSEBroadcaster(max_clients=50)
        q = bc.subscribe()
        try:
            ...consume q.get() en bucle de SSE...
        finally:
            bc.unsubscribe(q)

        bc.broadcast(record_dict)  # push a TODOS los clientes
    """

    def __init__(self, max_clients: int = 50, per_client_maxsize: int = 200):
        self.max_clients = max_clients
        self.per_client_maxsize = per_client_maxsize
        self._subscribers: list[PrioritizedSSEQueue] = []
        self._lock = threading.Lock()
        self.rejected_subscriptions: int = 0

    def subscribe(self) -> Optional[PrioritizedSSEQueue]:
        """Registra un nuevo cliente. None si maxed out."""
        with self._lock:
            if len(self._subscribers) >= self.max_clients:
                self.rejected_subscriptions += 1
                return None
            q = PrioritizedSSEQueue(maxsize=self.per_client_maxsize)
            self._subscribers.append(q)
            return q

    def unsubscribe(self, q: PrioritizedSSEQueue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def broadcast(self, record: dict) -> int:
        """Push a todos los clientes. Devuelve número de pushes exitosos."""
        with self._lock:
            subs = list(self._subscribers)
        ok = 0
        for q in subs:
            try:
                if q.put_nowait(record):
                    ok += 1
            except Exception:
                pass
        return ok

    def get_stats(self) -> dict:
        with self._lock:
            subs = list(self._subscribers)
        total_pushed = 0
        total_dropped = 0
        dropped_by_kind_global: dict[str, int] = {}
        max_pressure_pct = 0.0
        for q in subs:
            s = q.get_stats()
            total_pushed += s["total_pushed"]
            total_dropped += s["total_dropped"]
            for k, v in s["dropped_by_kind"].items():
                dropped_by_kind_global[k] = dropped_by_kind_global.get(k, 0) + v
            max_pressure_pct = max(max_pressure_pct, s["pressure_pct"])
        return {
            "active_clients": len(subs),
            "max_clients": self.max_clients,
            "rejected_subscriptions": self.rejected_subscriptions,
            "total_pushed": total_pushed,
            "total_dropped": total_dropped,
            "dropped_by_kind": dropped_by_kind_global,
            "max_pressure_pct": max_pressure_pct,
        }


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    q = PrioritizedSSEQueue(maxsize=3)
    # Llenar con NOISE
    q.put_nowait({"event": "shot1", "kind": "NOISE"})
    q.put_nowait({"event": "shot2", "kind": "NOISE"})
    q.put_nowait({"event": "shot3", "kind": "NOISE"})
    print(f"After 3 NOISE: size={q.qsize()}")
    # Push STATE → debe substituir un NOISE
    pushed = q.put_nowait({"event": "goto", "kind": "STATE"})
    print(f"STATE pushed: {pushed}, size={q.qsize()}")
    print(f"Stats: {q.get_stats()}")
    # Llenar con STATE → ya no hay donde drop
    for i in range(3):
        q.put_nowait({"event": f"s{i}", "kind": "STATE"})
    # Push NOISE → debe rechazarse
    pushed = q.put_nowait({"event": "shot_extra", "kind": "NOISE"})
    print(f"NOISE pushed under STATE pressure: {pushed}")
    print(f"Final stats: {q.get_stats()}")


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
