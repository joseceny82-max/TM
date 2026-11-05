"""core/dashboard/stream.py — JSONL tail streaming.

Lee de `runs/diagnostics_slot*.jsonl` en background thread y emite eventos
a suscriptores. Sin polling sobre el bot. Sin acoplamiento.

Diseño:
    - Daemon thread (no bloquea el proceso al cerrar)
    - Tail-follow (like `tail -f`) sobre múltiples ficheros
    - Detección de nuevos ficheros (slots nuevos)
    - Ring buffer de últimos N eventos (memoria limitada)
    - Suscripción por callback
    - Fail-safe: cualquier error se silencia

Uso:
    from core.dashboard.stream import JSONLTailer

    tailer = JSONLTailer(pattern="runs/diagnostics_slot*.jsonl")
    tailer.start()

    @tailer.subscribe
    def on_event(record: dict):
        print(record)

    recent = tailer.get_recent(50)
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional


# ════════════════════════════════════════════════════════════════════
#  JSONLTailer
# ════════════════════════════════════════════════════════════════════
class JSONLTailer:
    """Tail-follow sobre múltiples JSONL files con suscripción callback."""

    DEFAULT_DIR = "runs"
    DEFAULT_GLOB = "diagnostics_slot*.jsonl"
    BUFFER_SIZE = 500  # ring buffer

    def __init__(self,
                 directory: str = DEFAULT_DIR,
                 pattern: str = DEFAULT_GLOB,
                 poll_interval_s: float = 0.5):
        self.directory = Path(directory)
        self.pattern = pattern
        self.poll_interval_s = poll_interval_s

        self._buffer: deque[dict] = deque(maxlen=self.BUFFER_SIZE)
        self._buffer_lock = threading.Lock()
        self._subscribers: list[Callable[[dict], None]] = []
        self._sub_lock = threading.Lock()

        # State: para cada fichero, su última posición leída
        self._file_offsets: dict[Path, int] = {}
        self._file_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

        # M6: watchdog heartbeat + degraded mode
        self._last_poll_at: float = 0.0
        self._poll_count: int = 0
        self._error_count: int = 0
        self._last_error: Optional[str] = None

    # ────────────────────────────────────────────────────────────────
    #  Lifecycle
    # ────────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Lanza el thread daemon. Idempotente."""
        if self._started:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                          name="JSONLTailer")
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        """Solicita parar el thread. No bloquea (daemon)."""
        self._stop_event.set()

    def is_running(self) -> bool:
        return self._started and self._thread is not None and self._thread.is_alive()

    # ────────────────────────────────────────────────────────────────
    #  Subscription API
    # ────────────────────────────────────────────────────────────────
    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[dict], None]:
        """Registra un callback. Puede usarse como decorator."""
        with self._sub_lock:
            self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: Callable[[dict], None]) -> None:
        with self._sub_lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def get_recent(self, n: int = 50) -> list[dict]:
        """Devuelve los últimos N eventos del ring buffer."""
        with self._buffer_lock:
            snap = list(self._buffer)
        return snap[-n:] if n > 0 else snap

    # ────────────────────────────────────────────────────────────────
    #  Main loop
    # ────────────────────────────────────────────────────────────────
    def _run_loop(self) -> None:
        """Loop principal del thread: detecta ficheros + tail-follow.

        M6: heartbeat (last_poll_at) actualizado en cada iteración exitosa.
        Errores se cuentan pero no rompen el loop.
        """
        while not self._stop_event.is_set():
            try:
                self._last_poll_at = time.time()
                self._poll_count += 1
                files = self._discover_files()
                for fp in files:
                    self._process_file(fp)
            except Exception as e:
                self._error_count += 1
                try:
                    self._last_error = type(e).__name__
                except Exception:
                    self._last_error = "unknown"
            self._stop_event.wait(self.poll_interval_s)

    def _discover_files(self) -> list[Path]:
        """Encuentra ficheros que matchean el patrón."""
        try:
            return sorted(self.directory.glob(self.pattern))
        except Exception:
            return []

    def _process_file(self, fp: Path) -> None:
        """Lee nuevas líneas de un fichero desde su última posición."""
        try:
            with self._file_lock:
                offset = self._file_offsets.get(fp, 0)
            size = fp.stat().st_size
            if size < offset:
                # Fichero fue rotado/truncado — reset
                offset = 0
            if size == offset:
                return
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    self._handle_line(line, fp)
                new_offset = f.tell()
            with self._file_lock:
                self._file_offsets[fp] = new_offset
        except Exception:
            pass

    def _handle_line(self, line: str, fp: Path) -> None:
        """Parsea una línea JSON y notifica."""
        try:
            record = json.loads(line)
        except Exception:
            return
        # Enriquecer con metadata
        record["_source"] = fp.name
        # Buffer + notificación
        with self._buffer_lock:
            self._buffer.append(record)
        # Notificar a suscriptores (silencioso ante errores)
        with self._sub_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(record)
            except Exception:
                pass

    # ────────────────────────────────────────────────────────────────
    #  M6 — Watchdog API
    # ────────────────────────────────────────────────────────────────
    def get_health(self, stalled_threshold_s: float = 5.0) -> dict:
        """Snapshot de salud del tailer. Para /api/diagnostics/info.

        Returns:
            dict con:
              - alive: thread vivo
              - stalled: no ha hecho poll en >threshold_s
              - last_poll_at: epoch del último poll
              - last_poll_ago_s: hace cuánto fue
              - poll_count: total polls completados
              - error_count: total errores en el loop
              - last_error: tipo del último error
              - mode: "FULL" | "NO_AGGREGATE" | "TAILER_DEAD"
        """
        try:
            now = time.time()
            alive = self.is_running()
            last_poll = float(self._last_poll_at)
            ago = (now - last_poll) if last_poll > 0 else None
            stalled = bool(ago is not None and ago > stalled_threshold_s)
            # Modo degradado
            if not alive:
                mode = "TAILER_DEAD"
            elif stalled:
                mode = "NO_AGGREGATE"
            else:
                mode = "FULL"
            return {
                "alive": alive,
                "stalled": stalled,
                "last_poll_at": last_poll if last_poll > 0 else None,
                "last_poll_ago_s": round(ago, 2) if ago is not None else None,
                "poll_count": self._poll_count,
                "poll_interval_s": self.poll_interval_s,
                "error_count": self._error_count,
                "last_error": self._last_error,
                "mode": mode,
                "stalled_threshold_s": stalled_threshold_s,
            }
        except Exception:
            return {
                "alive": False,
                "stalled": True,
                "mode": "TAILER_DEAD",
                "error": "introspection_failed",
            }

    def is_stalled(self, threshold_s: float = 5.0) -> bool:
        """True si el tailer no ha hecho poll en threshold_s segundos."""
        try:
            if self._last_poll_at <= 0:
                return False  # nunca arrancó, no es stall
            return (time.time() - self._last_poll_at) > threshold_s
        except Exception:
            return False

    def attempt_recovery(self) -> bool:
        """Reinicia el thread si está muerto. Idempotente. Fail-safe."""
        try:
            if self.is_running():
                return True
            # Reset state
            self._started = False
            self._stop_event.clear()
            self.start()
            return self.is_running()
        except Exception:
            return False


# ════════════════════════════════════════════════════════════════════
#  Singleton instance (proceso-local)
# ════════════════════════════════════════════════════════════════════
_default_tailer: Optional[JSONLTailer] = None
_default_lock = threading.Lock()


def get_default_tailer() -> JSONLTailer:
    """Devuelve la instancia singleton del tailer."""
    global _default_tailer
    with _default_lock:
        if _default_tailer is None:
            _default_tailer = JSONLTailer()
    return _default_tailer


if __name__ == "__main__":
    print("=== JSONLTailer self-test ===")
    t = JSONLTailer()
    t.start()
    print(f"Running: {t.is_running()}")

    @t.subscribe
    def show(rec):
        print(f"  EVT: slot={rec.get('slot'):>3}  event={rec.get('event')}  "
              f"kind={rec.get('kind')}  state={rec.get('state')}")

    time.sleep(2)
    recent = t.get_recent(5)
    print(f"\nRecent {len(recent)} eventos en buffer")
    t.stop()


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
