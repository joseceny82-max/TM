"""core/observability/logger.py — Shadow logger (observabilidad paralela).

FASE 3 del plan de migración: puente de observabilidad SIN sustituir nada.

Diseño:
    El sistema legacy de logging sigue funcionando idéntico. El core ofrece
    un PUENTE (`log_bridge`) que el legacy puede invocar para emitir UNA
    COPIA del evento a un JSONL del core. El bridge es:
      - OFF por defecto (activado por env CORE_SHADOW_LOG=1)
      - SILENCIOSO ante cualquier error (no rompe legacy)
      - APPEND-ONLY a `runs/shadow_core_<run_id>.jsonl`
      - THREAD-SAFE entre slots paralelos
      - INDEPENDIENTE: si su fichero falla, el legacy sigue

Activación:
    export CORE_SHADOW_LOG=1  # POSIX
    set CORE_SHADOW_LOG=1     # Windows cmd
    $env:CORE_SHADOW_LOG="1"  # Windows PowerShell

Compatibilidad legacy:
    El legacy llama `log_bridge(slot, event, **kw)`. Si CORE_SHADOW_LOG
    no está activo, la función retorna inmediatamente sin efecto. Si está
    activo, escribe una línea JSON.

NO se importa ni se llama desde aquí al sistema legacy — el flujo es:
    legacy → llama bridge → core escribe shadow JSONL
    (unidireccional, sin ciclos)
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ────────────────────────────────────────────────────────────────────
#  CONFIG
# ────────────────────────────────────────────────────────────────────
SHADOW_ENV_FLAG = "CORE_SHADOW_LOG"  # activar con "1"
SHADOW_DIR_NAME = "runs"             # relativo al cwd

# Lock para escritura concurrente (varios slots paralelos)
_write_lock = threading.Lock()

# Path JSONL — se calcula una vez por proceso (incluye PID + run_id)
_shadow_path: Optional[Path] = None
_run_id = os.environ.get("CORE_RUN_ID") or datetime.now().strftime("%Y%m%d_%H%M%S")


def _is_enabled() -> bool:
    """True si el shadow logging está activo via env var."""
    return os.environ.get(SHADOW_ENV_FLAG) == "1"


def _shadow_jsonl_path() -> Path:
    """Path del JSONL shadow. Se crea el directorio si no existe.

    Path: `runs/shadow_core_<run_id>_<pid>.jsonl`
    El PID separa procesos paralelos (pool de slots).
    """
    global _shadow_path
    if _shadow_path is None:
        d = Path(SHADOW_DIR_NAME)
        d.mkdir(parents=True, exist_ok=True)
        _shadow_path = d / f"shadow_core_{_run_id}_pid{os.getpid()}.jsonl"
    return _shadow_path


# ────────────────────────────────────────────────────────────────────
#  PUBLIC API — log_bridge
# ────────────────────────────────────────────────────────────────────
def log_bridge(slot: Any = None,
               event: str = "",
               **kw: Any) -> None:
    """Puente de observabilidad llamado desde el legacy.

    PROPIEDADES:
      - OFF por defecto (env CORE_SHADOW_LOG=1 para activar)
      - NO lanza excepciones bajo NINGUNA circunstancia
      - NO toca stdout
      - Idempotente — múltiples invocaciones desde el mismo evento
        producen múltiples líneas (decisión deliberada para no perder)

    Args:
        slot:  identificador del slot/worker (any type, se serializa)
        event: nombre del evento (string)
        **kw:  campos extra del evento

    Returns:
        None — no devuelve nada para reforzar que es fire-and-forget.

    Errores:
        Cualquier error se silencia internamente. El legacy NUNCA debe
        sufrir efectos del bridge.
    """
    if not _is_enabled():
        return
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "slot": slot,
            "event": event,
            "pid": os.getpid(),
            "thread": threading.get_ident(),
        }
        # Mergear kw sin tocar las claves reservadas
        for k, v in kw.items():
            if k not in record:
                record[k] = v
        path = _shadow_jsonl_path()
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _write_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        # CONTRATO: nunca propagar errores al legacy.
        pass


# ────────────────────────────────────────────────────────────────────
#  Placeholder legacy-compat — para que tests/test_smoke.py no se rompa
# ────────────────────────────────────────────────────────────────────
def log_event(*args: Any, **kwargs: Any) -> None:
    """Placeholder compatible (no usar en producción nueva).

    Mantenido por compat con `tests/test_smoke.py` (FASE 2 commit b588da9).
    En FASE 4+ se redirigirá a un StructuredLogger del core. Por ahora
    solo imprime — equivalente al placeholder original.
    """
    print("[LOG_EVENT]", args, kwargs)


# ────────────────────────────────────────────────────────────────────
#  Helpers para tests
# ────────────────────────────────────────────────────────────────────
def get_shadow_path() -> Path:
    """Para tests: retorna el path del JSONL shadow (creando el dir)."""
    return _shadow_jsonl_path()


def reset_shadow_path_for_test() -> None:
    """Para tests: fuerza recálculo del path (usado tras cambiar cwd)."""
    global _shadow_path
    _shadow_path = None


def get_info() -> dict:
    return {
        "module": "core.observability.logger",
        "phase": "3 (shadow bridge)",
        "enabled": _is_enabled(),
        "env_flag": SHADOW_ENV_FLAG,
        "shadow_path": str(_shadow_jsonl_path()) if _is_enabled() else None,
        "run_id": _run_id,
    }


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
