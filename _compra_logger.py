"""_compra_logger.py — Logging estructurado para el bot de compra.

Diseño:
    - Wrapper sobre el `log()` legacy que SIGUE imprimiendo el formato actual a
      stdout (compat 100% con consumidores existentes: tail / grep / wrapper).
    - **Adicionalmente** escribe cada evento como JSON Line en
      `runs/structured_<ts>.jsonl` para análisis programático.
    - Esquema fijo: timestamp ISO, event, level, stage, slot, account_id,
      profile_id, sessionid, elapsed_ms, error_type, kw...

Compatibilidad:
    El `log()` actual de `_compra_parallel.py` puede migrar progresivamente a
    `log_event()`. Mientras tanto AMBOS coexisten — no se rompe nada.

Uso recomendado:

    from _compra_logger import StructuredLogger

    logger = StructuredLogger(slot=1, run_id="abc123")
    logger.set_context(account_id="0ed0ed39", profile_id="P-172112",
                        sessionid="305025")
    logger.event("profile_started", port=54191)
    logger.event("oauth_detected", level="info")
    logger.event("BAD_CREDENTIALS_detected", level="error",
                  error_type="bad_creds", email="x@y.com")

    # Timing helper
    with logger.time("checkout_fill_card"):
        ...  # fill_card_adyen(...)
    # → emite "checkout_fill_card_done" con elapsed_ms

Truncado:
    Mensajes largos se truncan a TRUNC_LOG_ERR_LONG (200 chars) por defecto.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Reuso de constantes centralizadas (truncado de errores)
try:
    from _compra_constants import (TRUNC_LOG_ERR_LONG, TRUNC_LOG_ERR_NORMAL,
                                    TRUNC_LOG_ERR_SHORT)
except ImportError:
    TRUNC_LOG_ERR_SHORT = 80
    TRUNC_LOG_ERR_NORMAL = 120
    TRUNC_LOG_ERR_LONG = 200


# ────────────────────────────────────────────────────────────────────
#  CONFIG
# ────────────────────────────────────────────────────────────────────
_LOG_DIR = Path("runs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    """ISO-8601 UTC con ms (terminado en 'Z')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _short_hms() -> str:
    """Para compat con formato legacy `[HH:MM:SS]`."""
    return time.strftime("%H:%M:%S")


def truncate(value: Any, max_len: int = TRUNC_LOG_ERR_NORMAL) -> Any:
    """Trunca strings largos para evitar logs gigantes. Otros tipos sin tocar."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "..."
    return value


# ────────────────────────────────────────────────────────────────────
#  P1: JSONL handle cache (perf hardening)
#  ─ Elimina fopen+fclose per-event del hot path
#  ─ Kill switch: JSONL_HANDLE_CACHE_ENABLED=0 → fallback legacy
#  ─ Line-buffering (buffering=1): flush automático por '\n'
#  ─ atexit cleanup: cerrar handles abiertos al shutdown
#
#  THREAD SAFETY (Ajuste A):
#    _jsonl_handles_lock     → protege SOLO el dict (cache + per-file locks)
#    _jsonl_write_locks[p]   → lock por fichero, atomicidad write por path
#    Sin lock global de writes → no bottleneck entre slots distintos.
# ────────────────────────────────────────────────────────────────────
_jsonl_handles: Dict[Path, Any] = {}
_jsonl_write_locks: Dict[Path, "threading.Lock"] = {}
_jsonl_handles_lock = threading.Lock()


def _jsonl_cache_enabled() -> bool:
    """Kill switch: env JSONL_HANDLE_CACHE_ENABLED=0 → desactiva cache."""
    return os.environ.get("JSONL_HANDLE_CACHE_ENABLED", "1") not in (
        "0", "false", "False", "FALSE", "no", "NO"
    )


def _get_handle_and_lock(path: Path):
    """Obtiene (handle, write_lock) para un path. Fail-safe.

    Returns:
        (TextIO, threading.Lock) o (None, None) si cache off / open falla.
        Si None, el caller usa fallback open()/close() per-line.

    Ajuste A: lock por fichero para atomicidad write SIN bottleneck global.
    """
    if not _jsonl_cache_enabled():
        return None, None
    try:
        # Path absoluto = clave canónica del cache (evita colisiones cwd).
        path = Path(path).resolve()
        with _jsonl_handles_lock:
            h = _jsonl_handles.get(path)
            wlock = _jsonl_write_locks.get(path)
            if h is not None and not h.closed and wlock is not None:
                return h, wlock
            # Necesita (re)abrir handle
            try:
                h = open(path, "a", encoding="utf-8", buffering=1)
                _jsonl_handles[path] = h
                if wlock is None:
                    wlock = threading.Lock()
                    _jsonl_write_locks[path] = wlock
                return h, wlock
            except Exception:
                return None, None
    except Exception:
        return None, None


def _invalidate_handle(path: Path) -> None:
    """Cierra y elimina handle inválido del cache. Preserva el write_lock.

    Llamado tras un write() fallido. El lock se conserva para que un
    posterior _get_handle_and_lock devuelva el MISMO lock al reabrir.
    """
    try:
        path = Path(path).resolve()
        with _jsonl_handles_lock:
            h = _jsonl_handles.pop(path, None)
            # NO eliminar el write_lock — futuro reopen reusa
        if h is not None:
            try:
                h.close()
            except Exception:
                pass
    except Exception:
        pass


def _close_cached_handle(path: Path) -> None:
    """Cierra handle + elimina del cache. Mantiene write_lock (idempotencia).

    Útil para shutdown explícito / atexit / tests.
    """
    _invalidate_handle(path)


@atexit.register
def _close_all_cached_handles() -> None:
    """atexit: cierra todos los handles JSONL al shutdown del proceso."""
    try:
        with _jsonl_handles_lock:
            handles = list(_jsonl_handles.values())
            _jsonl_handles.clear()
            _jsonl_write_locks.clear()
        for h in handles:
            try:
                h.close()
            except Exception:
                pass
    except Exception:
        pass


def _reset_handle_cache_for_test() -> None:
    """Solo para tests: limpia el cache + cierra handles + locks."""
    _close_all_cached_handles()


# ────────────────────────────────────────────────────────────────────
#  StructuredLogger
# ────────────────────────────────────────────────────────────────────
class StructuredLogger:
    """Logger estructurado con contexto persistente entre eventos.

    Conserva en `self._ctx` campos comunes que se rellenan una vez (account_id,
    profile_id, sessionid) y se incluyen automáticamente en cada `event()`.

    Args:
        slot: número de slot del bot (1, 2, ...).
        run_id: identificador del run (UUID o timestamp).
        jsonl_path: ruta del JSON Lines (por defecto `runs/structured_<ts>.jsonl`).
        stdout_legacy: si True (default), también imprime el formato legacy a stdout
                       para compatibilidad con consumidores existentes (wrapper, tail).
    """

    def __init__(self,
                 slot: int = 1,
                 run_id: Optional[str] = None,
                 jsonl_path: Optional[Path] = None,
                 stdout_legacy: bool = True):
        self.slot = slot
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.stdout_legacy = stdout_legacy
        self._ctx: Dict[str, Any] = {"slot": slot, "run_id": self.run_id}
        if jsonl_path is None:
            jsonl_path = _LOG_DIR / f"structured_slot{slot}_{self.run_id}.jsonl"
        self.jsonl_path = Path(jsonl_path)
        self._t_start = time.time()
        # Abrir append-mode (resiliente a múltiples instancias)
        try:
            self._fh = open(self.jsonl_path, "a", encoding="utf-8")
        except Exception:
            self._fh = None  # degradación: solo stdout

    def __del__(self):
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass

    # ── Context API ──────────────────────────────────────────────────
    def set_context(self, **fields: Any) -> None:
        """Persiste campos en TODOS los eventos siguientes.

        Ej.: logger.set_context(account_id="abc", profile_id="P-1")
        """
        for k, v in fields.items():
            if v is None:
                # None se interpreta como "borrar el campo"
                self._ctx.pop(k, None)
            else:
                self._ctx[k] = v

    # ── Event API ────────────────────────────────────────────────────
    def event(self,
              name: str,
              level: str = "info",
              error_type: Optional[str] = None,
              elapsed_ms: Optional[float] = None,
              **kw: Any) -> Dict[str, Any]:
        """Emite un evento estructurado.

        Args:
            name: nombre del evento (snake_case). Ej.: "profile_started", "oauth_detected".
            level: "debug" | "info" | "warn" | "error" | "fatal"
            error_type: categoría del error si aplica (ej. "bad_creds", "timeout").
            elapsed_ms: tiempo transcurrido en ms (si la operación midió duración).
            **kw: campos adicionales libres (cookies_count=14, url="https://...").

        Returns:
            dict con el record emitido (útil para tests).
        """
        record: Dict[str, Any] = {
            "ts": _now_iso(),
            "event": name,
            "level": level,
            **self._ctx,
        }
        if error_type is not None:
            record["error_type"] = error_type
        if elapsed_ms is not None:
            record["elapsed_ms"] = round(float(elapsed_ms), 1)
        # Trunca valores string largos automáticamente
        for k, v in kw.items():
            record[k] = truncate(v, TRUNC_LOG_ERR_LONG)

        # 1) JSON line a fichero
        if self._fh:
            try:
                self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                self._fh.flush()
            except Exception:
                pass  # cleanup, no podemos hacer mucho

        # 2) Legacy stdout (compat con grep/tail/wrapper)
        if self.stdout_legacy:
            msg = f"[slot{self.slot}][{_short_hms()}] {name}"
            extra = {k: v for k, v in record.items()
                     if k not in ("ts", "event", "level", "slot", "run_id")}
            if extra:
                msg += " " + json.dumps(extra, ensure_ascii=False, default=str)[:500]
            print(msg, flush=True)

        # 3) SHADOW BRIDGE (FASE 3) — observabilidad paralela OFF por defecto.
        # Activable con CORE_SHADOW_LOG=1. Contrato: nunca rompe legacy.
        try:
            from core.observability.logger import log_bridge as _core_bridge
            _bridge_kw = {k: v for k, v in record.items()
                          if k not in ("ts", "event", "level", "slot")}
            _core_bridge(slot=self.slot, event=name, level=level, **_bridge_kw)
        except Exception:
            pass

        # 4) RUNTIME DIAGNOSTICS (FASE 4) — self-diagnosing layer.
        # Apagable con OBSERVABILITY_ENABLED=0. Contrato: nunca rompe legacy.
        try:
            from core.observability.runtime_diagnostics import observe as _observe
            _observe(event_name=name, payload=record)
        except Exception:
            pass

        return record

    # ── Timing helper ────────────────────────────────────────────────
    @contextmanager
    def time(self, name: str, level: str = "info", **start_kw: Any):
        """Context manager que mide elapsed_ms y emite N_start + N_done.

        Uso:
            with logger.time("fill_card", method="adyen_iframe"):
                ...
            # emite "fill_card_start" al entrar y "fill_card_done" al salir
            # (o "fill_card_err" si hay exception).
        """
        t0 = time.time()
        self.event(f"{name}_start", level=level, **start_kw)
        try:
            yield
            elapsed_ms = (time.time() - t0) * 1000.0
            self.event(f"{name}_done", level=level, elapsed_ms=elapsed_ms)
        except Exception as e:
            elapsed_ms = (time.time() - t0) * 1000.0
            self.event(f"{name}_err", level="error",
                       error_type=type(e).__name__,
                       elapsed_ms=elapsed_ms,
                       err=str(e))
            raise

    # ── Conveniencias ────────────────────────────────────────────────
    def info(self, name: str, **kw: Any) -> None:
        self.event(name, level="info", **kw)

    def warn(self, name: str, **kw: Any) -> None:
        self.event(name, level="warn", **kw)

    def error(self, name: str, error_type: Optional[str] = None, **kw: Any) -> None:
        self.event(name, level="error", error_type=error_type, **kw)

    def fatal(self, name: str, error_type: Optional[str] = None, **kw: Any) -> None:
        self.event(name, level="fatal", error_type=error_type, **kw)


# ────────────────────────────────────────────────────────────────────
#  Helper standalone — para módulos que no quieren instanciar StructuredLogger
# ────────────────────────────────────────────────────────────────────
def log_event(slot: int,
              event: str,
              level: str = "info",
              jsonl_path: Optional[Path] = None,
              **kw: Any) -> None:
    """Función equivalente al `log()` legacy pero con esquema estructurado.

    NO mantiene contexto entre llamadas (usar StructuredLogger para eso).
    Reescritura mínima del log() legacy:

        # ANTES
        log(slot, "oauth_detected", url=u)

        # DESPUÉS (equivalente, JSON Lines + stdout)
        log_event(slot, "oauth_detected", url=u)
    """
    record: Dict[str, Any] = {
        "ts": _now_iso(),
        "event": event,
        "level": level,
        "slot": slot,
    }
    for k, v in kw.items():
        record[k] = truncate(v, TRUNC_LOG_ERR_LONG)

    if jsonl_path is None:
        jsonl_path = _LOG_DIR / f"structured_slot{slot}.jsonl"
    # P1: handle cache reusable + atomicidad por fichero + recovery auto.
    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        h, wlock = _get_handle_and_lock(jsonl_path)
        if h is not None and wlock is not None:
            # Ajuste A: lock por fichero (no global) → concurrencia entre slots
            with wlock:
                try:
                    h.write(line)
                except Exception:
                    # Ajuste B: recovery automático
                    # 1) invalidar handle inválido
                    _invalidate_handle(jsonl_path)
                    # 2) reabrir + retry 1 vez
                    try:
                        h2, _ = _get_handle_and_lock(jsonl_path)
                        if h2 is not None:
                            h2.write(line)
                        else:
                            # 3) si reopen falla: fallback legacy open()
                            with open(jsonl_path, "a", encoding="utf-8") as f:
                                f.write(line)
                    except Exception:
                        # 4) si retry también falla: swallow (fail-safe)
                        pass
        else:
            # Cache off o falla open → fallback legacy
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass

    # Stdout legacy
    msg = f"[slot{slot}][{_short_hms()}] {event}"
    extra = {k: v for k, v in record.items()
             if k not in ("ts", "event", "level", "slot")}
    if extra:
        msg += " " + json.dumps(extra, ensure_ascii=False, default=str)[:500]
    print(msg, flush=True)

    # ── SHADOW BRIDGE (FASE 3) ─────────────────────────────────────────
    # Observabilidad paralela OFF por defecto. Activable con CORE_SHADOW_LOG=1.
    # Contrato: el bridge NUNCA debe romper el legacy. Try/except defensivo.
    try:
        from core.observability.logger import log_bridge as _core_bridge
        _core_bridge(slot=slot, event=event, level=level, **kw)
    except Exception:
        pass

    # ── RUNTIME DIAGNOSTICS (FASE 4) ───────────────────────────────────
    # Self-diagnosing layer ON por defecto. Apagable con OBSERVABILITY_ENABLED=0.
    # Contrato: nunca rompe legacy.
    try:
        from core.observability.runtime_diagnostics import observe as _observe
        _observe(event_name=event, payload=record)
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
#  Self-test
# ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== StructuredLogger self-test ===")
    logger = StructuredLogger(slot=99, run_id="selftest")
    logger.set_context(account_id="test123", profile_id="P-selftest",
                       sessionid="999000")
    logger.event("startup", browser="chrome/146")
    logger.warn("flaky_op", retry=2)
    logger.error("login_failed", error_type="bad_creds", email="x@y.com")

    # Timing
    with logger.time("dummy_op"):
        time.sleep(0.05)

    # Long string truncation
    logger.event("long_msg", details="X" * 1000)

    print(f"\n[OK] JSONL written to: {logger.jsonl_path}")
    print(f"  Records:")
    with open(logger.jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            print(f"    {r['ts']} [{r['level']:5}] {r['event']}")
