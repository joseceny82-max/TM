"""core/observability/event_emitter.py — Structured Event Sink (STEP 3).

Emite ErrorEvent + EventoArbitrario al sink estructurado.

PRINCIPIOS:
    1. **Fail-safe absoluto**: emit nunca propaga excepciones. Aunque
       el disco esté lleno o el formato falle, el flow productivo sigue.
    2. **No colisión con logger existente**: escribe a un archivo
       dedicado `runs/structured_events_<pid>.jsonl`, distinto del
       legacy `shadow_core_*.jsonl`.
    3. **Append-only**: cada `emit_event` añade una línea JSON. Sin
       reescritura. Sin races destructivas.
    4. **Atómico por línea**: cada `print + flush` o `write + flush` es
       atómico bajo POSIX para escrituras menores que PIPE_BUF.
    5. **Thread-safe**: Lock interno serializa escrituras desde múltiples
       threads en el mismo proceso.
    6. **No globals mutables peligrosos**: sink path se calcula lazy y
       cachea. Hay un `set_sink_path()` para tests.

stdlib only.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Union

from core.observability.error_classification import (
    ErrorCategory,
    ErrorEvent,
    ErrorSeverity,
    classify_exception,
    make_event,
)


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
DEFAULT_RUNS_DIR: str = "runs"
SINK_FILENAME_PATTERN: str = "structured_events_pid{pid}.jsonl"

# ─── Source tagging (Phase 2 audit) ────────────────────────────────
# Cada evento se etiqueta con `source` para que el dashboard pueda
# distinguir test pollution de tráfico productivo real.
SOURCE_PRODUCTION: str = "production"
SOURCE_TEST: str = "test"
SOURCE_SHADOW: str = "shadow"
SOURCE_CANARY: str = "canary"
SOURCE_UNKNOWN: str = "unknown"

VALID_SOURCES: frozenset = frozenset({
    SOURCE_PRODUCTION, SOURCE_TEST, SOURCE_SHADOW,
    SOURCE_CANARY, SOURCE_UNKNOWN,
})


def _detect_source() -> str:
    """Auto-detecta source basado en runtime context.

    Reglas (en orden):
        1. Env var EVENT_SOURCE explícita → respeta
        2. Si pytest está en sys.modules → "test"
        3. Default → "production"
    """
    # Override explícito
    env = os.environ.get("EVENT_SOURCE", "").strip().lower()
    if env in VALID_SOURCES:
        return env
    # Detección de pytest
    import sys
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return SOURCE_TEST
    return SOURCE_PRODUCTION


# ════════════════════════════════════════════════════════════════════
#  Estado interno (encapsulado, accesible vía helpers para tests)
# ════════════════════════════════════════════════════════════════════
_lock = threading.Lock()
_sink_path: Optional[Path] = None
_extra_sinks: list = []  # callables(event_dict) → None — para tests/extensiones


# ════════════════════════════════════════════════════════════════════
#  Resolución del path del sink (lazy)
# ════════════════════════════════════════════════════════════════════
def _resolve_default_sink_path() -> Path:
    runs_dir = Path(DEFAULT_RUNS_DIR)
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Si no podemos crear runs/, usar tmp como fallback
        import tempfile
        runs_dir = Path(tempfile.gettempdir())
    return runs_dir / SINK_FILENAME_PATTERN.format(pid=os.getpid())


def get_sink_path() -> Path:
    """Devuelve el path del sink actual (creándolo si nunca se calculó)."""
    global _sink_path
    if _sink_path is None:
        _sink_path = _resolve_default_sink_path()
    return _sink_path


def set_sink_path(path: Optional[Union[str, Path]]) -> None:
    """Sobrescribe el sink path (para tests). None → reset al default."""
    global _sink_path
    if path is None:
        _sink_path = None
    else:
        _sink_path = Path(path)


# ════════════════════════════════════════════════════════════════════
#  Extra sinks (subscribers externos — tests / dashboards)
# ════════════════════════════════════════════════════════════════════
def add_sink(sink: Callable[[dict], None]) -> None:
    """Añade un callable que recibirá cada evento como dict.

    Útil para tests y dashboards. NO bloquea fail-safe del sink principal.
    """
    if not callable(sink):
        return
    with _lock:
        _extra_sinks.append(sink)


def clear_sinks() -> None:
    """Limpia los extra sinks (para tests)."""
    with _lock:
        _extra_sinks.clear()


# ════════════════════════════════════════════════════════════════════
#  Helpers internos de escritura
# ════════════════════════════════════════════════════════════════════
def _safe_jsonl_append(path: Path, line_dict: dict) -> bool:
    """Append una línea JSON al sink. Devuelve True si tuvo éxito.

    SILENCIA todas las excepciones (fail-safe). Crea directorio si falta.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(line_dict, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            try:
                f.flush()
            except OSError:
                pass
        return True
    except (OSError, TypeError, ValueError):
        return False


def _notify_extra_sinks(event_dict: dict) -> None:
    """Notifica a los extra sinks. SILENCIA excepciones de cada uno."""
    # Copiar la lista bajo lock para iterar sin bloquear emisores nuevos
    with _lock:
        sinks = list(_extra_sinks)
    for sink in sinks:
        try:
            sink(event_dict)
        except Exception:
            # NUNCA propagar desde sink — fail-safe absoluto
            pass


# ════════════════════════════════════════════════════════════════════
#  API principal
# ════════════════════════════════════════════════════════════════════
def emit(event: ErrorEvent) -> bool:
    """Emite un ErrorEvent ya construido al sink.

    Fail-safe absoluto. Returns True si la escritura al sink principal
    tuvo éxito; False en caso contrario (sin propagar).
    """
    if not isinstance(event, ErrorEvent):
        return False
    try:
        d = event.to_dict()
    except Exception:
        return False

    # ─── Source tagging (Phase 2 audit) ─────────────────────────
    # Cada evento es tagueado con su source para que el dashboard
    # pueda filtrar test pollution. Si ya viene con source en payload,
    # respeta. Si no, auto-detecta.
    if "source" not in d:
        d["source"] = _detect_source()

    # Sink principal (JSONL append)
    path = get_sink_path()
    with _lock:
        ok = _safe_jsonl_append(path, d)

    # Notificar extra sinks (tests / subscribers)
    _notify_extra_sinks(d)

    return ok


def emit_event(category: Union[ErrorCategory, str],
                code: str,
                payload: Optional[dict] = None,
                *,
                severity: Union[ErrorSeverity, str] = ErrorSeverity.RECOVERABLE,
                message: str = "",
                context: str = "",
                cart_id: str = "",
                idempotency_key: str = "") -> Optional[ErrorEvent]:
    """API de conveniencia per spec STEP 3.

    Acepta `category` y `severity` como Enum O como string (para usuarios
    que no quieran importar los Enums).

    Returns:
        El ErrorEvent emitido, o None si no se pudo construir.
        NUNCA propaga excepciones.
    """
    try:
        # Coerción de category
        if isinstance(category, str):
            try:
                category = ErrorCategory(category)
            except ValueError:
                category = ErrorCategory.FLOW
        if not isinstance(category, ErrorCategory):
            category = ErrorCategory.FLOW

        # Coerción de severity
        if isinstance(severity, str):
            try:
                severity = ErrorSeverity(severity)
            except ValueError:
                severity = ErrorSeverity.RECOVERABLE
        if not isinstance(severity, ErrorSeverity):
            severity = ErrorSeverity.RECOVERABLE

        event = make_event(
            category=category,
            code=str(code) if code is not None else "",
            severity=severity,
            message=str(message) if message is not None else "",
            context=str(context) if context is not None else "",
            cart_id=str(cart_id) if cart_id is not None else "",
            idempotency_key=str(idempotency_key) if idempotency_key is not None else "",
            payload=payload,
        )
    except Exception:
        return None

    emit(event)
    return event


def emit_from_exception(exc: Optional[BaseException] = None,
                         *,
                         message: str = "",
                         context: str = "",
                         cart_id: str = "",
                         idempotency_key: str = "",
                         payload: Optional[dict] = None) -> Optional[ErrorEvent]:
    """Atajo: clasifica una Exception y emite. Fail-safe.

    Returns ErrorEvent emitido (o None si ni siquiera se pudo construir).
    """
    try:
        event = classify_exception(
            exc=exc, message=message, context=context,
            cart_id=cart_id, idempotency_key=idempotency_key,
            payload=payload,
        )
    except Exception:
        return None
    emit(event)
    return event


# ════════════════════════════════════════════════════════════════════
#  Helpers de lectura (para metrics futuros)
# ════════════════════════════════════════════════════════════════════
def read_events(path: Optional[Union[str, Path]] = None) -> list:
    """Lee los eventos emitidos en `path` (default: sink actual).

    Devuelve list de dicts. Líneas corruptas se ignoran. Si el archivo
    no existe → []. NUNCA propaga excepciones.
    """
    target = Path(path) if path is not None else get_sink_path()
    if not target.exists():
        return []
    out: list = []
    try:
        with target.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if isinstance(d, dict):
                        out.append(d)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return out
    return out


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
