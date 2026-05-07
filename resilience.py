"""resilience.py — Retry, circuit breaker, timeout, graceful shutdown.

Decoradores que hacen el bot/dashboard tolerantes a fallos:

  @retry(max=3, backoff=2.0)         ← reintenta exponencialmente
  @circuit_breaker(fail_threshold=5) ← abre circuito tras 5 fallos seguidos
  @with_timeout(seconds=10)          ← mata la función tras N segundos

También:
  register_shutdown_handler(fn)  ← callback en SIGTERM/SIGINT
  is_shutting_down()             ← para bucles de workers
"""
import time
import signal
import threading
from functools import wraps
from typing import Callable, Optional
from datetime import datetime, timedelta


# ═══════════════════════════════════════════
#  RETRY
# ═══════════════════════════════════════════
def retry(max: int = 3, backoff: float = 2.0, exceptions=(Exception,),
          on_retry: Optional[Callable] = None):
    """Reintenta con exponential backoff. Reraises tras `max` intentos."""
    def deco(fn):
        @wraps(fn)
        def inner(*a, **kw):
            delay = 0.5
            last_exc = None
            for attempt in range(1, max + 1):
                try:
                    return fn(*a, **kw)
                except exceptions as e:
                    last_exc = e
                    if on_retry:
                        try:
                            on_retry(attempt, e)
                        except Exception:
                            pass
                    if attempt < max:
                        time.sleep(delay)
                        delay *= backoff
            raise last_exc
        return inner
    return deco


# ═══════════════════════════════════════════
#  CIRCUIT BREAKER
# ═══════════════════════════════════════════
class CircuitBreaker:
    """Circuit breaker simple: después de N fallos seguidos, abre el
    circuito por `timeout_s` segundos. Durante ese tiempo, todas las
    llamadas fallan rápido sin ejecutar la función.
    """
    def __init__(self, fail_threshold: int = 5, timeout_s: int = 60):
        self.fail_threshold = fail_threshold
        self.timeout_s = timeout_s
        self._fails = 0
        self._opened_at: Optional[datetime] = None
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if datetime.now() - self._opened_at > timedelta(seconds=self.timeout_s):
                # auto-reset a half-open
                self._opened_at = None
                self._fails = 0
                return False
            return True

    def record_success(self):
        with self._lock:
            self._fails = 0
            self._opened_at = None

    def record_failure(self):
        with self._lock:
            self._fails += 1
            if self._fails >= self.fail_threshold:
                self._opened_at = datetime.now()

    def state(self) -> dict:
        with self._lock:
            return {
                "open": self._opened_at is not None,
                "fails": self._fails,
                "threshold": self.fail_threshold,
                "opened_at": self._opened_at.isoformat() if self._opened_at else None,
            }


_BREAKERS = {}

def circuit_breaker(name: str, fail_threshold: int = 5, timeout_s: int = 60):
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(fail_threshold, timeout_s)
    cb = _BREAKERS[name]
    def deco(fn):
        @wraps(fn)
        def inner(*a, **kw):
            if cb.is_open():
                raise RuntimeError(f"circuit breaker '{name}' is OPEN")
            try:
                result = fn(*a, **kw)
                cb.record_success()
                return result
            except Exception as e:
                cb.record_failure()
                raise
        return inner
    return deco


def get_breakers_state() -> dict:
    return {name: cb.state() for name, cb in _BREAKERS.items()}


# ═══════════════════════════════════════════
#  TIMEOUT (thread-based, Windows-friendly)
# ═══════════════════════════════════════════
def with_timeout(seconds: float):
    """Ejecuta la función en thread, mata si excede timeout.
    Nota: no interrumpe código sync — marca para que el caller sepa.
    """
    def deco(fn):
        @wraps(fn)
        def inner(*a, **kw):
            result = [TimeoutError(f"{fn.__name__} excedió {seconds}s")]
            def runner():
                try:
                    result[0] = fn(*a, **kw)
                except Exception as e:
                    result[0] = e
            th = threading.Thread(target=runner, daemon=True)
            th.start()
            th.join(seconds)
            if th.is_alive():
                raise TimeoutError(f"{fn.__name__} timeout {seconds}s")
            if isinstance(result[0], Exception):
                raise result[0]
            return result[0]
        return inner
    return deco


# ═══════════════════════════════════════════
#  GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════
_shutdown_handlers = []
_shutting_down = False


def register_shutdown_handler(fn: Callable):
    _shutdown_handlers.append(fn)


def is_shutting_down() -> bool:
    return _shutting_down


def _handle_signal(signum, frame):
    global _shutting_down
    _shutting_down = True
    print(f"\n[resilience] Señal {signum} recibida, shutdown graceful...", flush=True)
    for h in _shutdown_handlers:
        try:
            h()
        except Exception as e:
            print(f"[resilience] handler error: {e}", flush=True)


def install_signal_handlers():
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
    except (ValueError, AttributeError):
        # no en main thread o windows limitations
        pass


# ═══════════════════════════════════════════
#  HEALTH STATE
# ═══════════════════════════════════════════
def get_state() -> dict:
    return {
        "shutting_down": _shutting_down,
        "shutdown_handlers": len(_shutdown_handlers),
        "circuit_breakers": get_breakers_state(),
    }
