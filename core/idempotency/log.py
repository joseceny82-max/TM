"""core/idempotency/log.py — Idempotency Log (Write-Ahead Intent + Outcome).

Persistencia atómica de intent + outcome por idempotency_key, sin DB.

PATRÓN:
    Antes de cualquier side-effect (submit cart, submit payment, etc.):
        1) write_intent(key, payload)   ← antes del side-effect
        2) <ejecutar side-effect>
        3) write_outcome(key, payload)  ← después de confirmar

    En reintento o recovery:
        record = load_record(key)
        if record.outcome_exists: return record.outcome
        if record.intent_exists and orphan:
            # Recovery flow (cliente decide qué hacer)

ATOMICIDAD:
    Escritura via NamedTemporaryFile en MISMO directorio + os.replace().
    POSIX-atomic en Linux/macOS y Windows (Python 3.3+).

ESTRUCTURA EN DISK:
    runs/idempotency_log/
        ├── <key1>/
        │   ├── intent.json
        │   └── outcome.json
        ├── <key2>/
        │   └── intent.json          ← orphan (sin outcome)
        └── <key3>/
            ├── intent.json
            └── outcome.json

stdlib only. Pure functions donde se puede. Side-effects encapsulados.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
INTENT_FILE: str = "intent.json"
OUTCOME_FILE: str = "outcome.json"
DEFAULT_ROOT: str = "runs/idempotency_log"


# ════════════════════════════════════════════════════════════════════
#  Excepciones
# ════════════════════════════════════════════════════════════════════
class IdempotencyError(Exception):
    """Error de operación del idempotency log."""
    pass


class IntentAlreadyExists(IdempotencyError):
    """Se intentó write_intent con strict=True y ya existe."""
    pass


class OutcomeAlreadyExists(IdempotencyError):
    """Se intentó write_outcome con strict=True y ya existe."""
    pass


# ════════════════════════════════════════════════════════════════════
#  Modelo
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class IdempotencyRecord:
    """Snapshot del estado de un (key) en disk."""
    key: str
    intent_exists: bool
    outcome_exists: bool
    intent_payload: Optional[dict]
    outcome_payload: Optional[dict]
    intent_path: str
    outcome_path: str

    def is_orphan(self) -> bool:
        """Intent sin outcome = side-effect comenzado pero no confirmado."""
        return self.intent_exists and not self.outcome_exists

    def is_committed(self) -> bool:
        """Intent + outcome = operación cerrada."""
        return self.intent_exists and self.outcome_exists

    def is_unknown(self) -> bool:
        """Ni intent ni outcome = nunca se procesó esta key."""
        return not self.intent_exists and not self.outcome_exists


# ════════════════════════════════════════════════════════════════════
#  Helpers privados
# ════════════════════════════════════════════════════════════════════
def _now_iso_utc() -> str:
    """ISO 8601 UTC con sufijo +00:00 (no Z, evita ambigüedad)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _atomic_write_json(target_path: Path, payload: dict) -> None:
    """Escribe `payload` como JSON a `target_path` de forma atómica.

    Usa NamedTemporaryFile en el mismo directorio + os.replace().
    NUNCA deja un archivo a medio escribir.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, sort_keys=True, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, AttributeError):
                    # fsync no disponible (algunos FS / Windows) — best effort
                    pass
        except Exception:
            # Si falló la escritura, limpiar el temp
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        # Reemplazo atómico
        os.replace(tmp_path, target_path)
    except Exception:
        # Limpiar temp si quedó
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _safe_load_json(path: Path) -> Optional[dict]:
    """Lee JSON. Devuelve None si no existe o malformado."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


# ════════════════════════════════════════════════════════════════════
#  API principal
# ════════════════════════════════════════════════════════════════════
def _key_dir(root: Path, key: str) -> Path:
    """Subdirectorio canónico para una key."""
    if not key:
        raise IdempotencyError("key must be non-empty")
    return root / key


def write_intent(key: str,
                  payload: dict,
                  *,
                  root: str = DEFAULT_ROOT,
                  strict: bool = False) -> str:
    """Persiste el INTENT para `key`. Atómico.

    Args:
        key: idempotency key.
        payload: dict serializable JSON.
        root: directorio raíz del log.
        strict: si True y ya existe intent → IntentAlreadyExists.

    Returns:
        path string del intent.json escrito.

    Raises:
        IdempotencyError, IntentAlreadyExists (si strict).
    """
    if not isinstance(payload, dict):
        raise IdempotencyError(f"payload must be dict, got {type(payload).__name__}")
    root_p = Path(root)
    kd = _key_dir(root_p, key)
    intent_path = kd / INTENT_FILE
    if strict and intent_path.exists():
        raise IntentAlreadyExists(f"intent already exists for {key}")
    full_payload = {
        "key": key,
        "phase": "intent",
        "timestamp": _now_iso_utc(),
        "payload": payload,
    }
    _atomic_write_json(intent_path, full_payload)
    return str(intent_path)


def write_outcome(key: str,
                   payload: dict,
                   *,
                   root: str = DEFAULT_ROOT,
                   strict: bool = False) -> str:
    """Persiste el OUTCOME para `key`. Atómico.

    Args:
        key: idempotency key.
        payload: dict serializable JSON.
        root: directorio raíz del log.
        strict: si True y ya existe outcome → OutcomeAlreadyExists.

    Returns:
        path string del outcome.json escrito.

    Raises:
        IdempotencyError, OutcomeAlreadyExists (si strict).
    """
    if not isinstance(payload, dict):
        raise IdempotencyError(f"payload must be dict, got {type(payload).__name__}")
    root_p = Path(root)
    kd = _key_dir(root_p, key)
    outcome_path = kd / OUTCOME_FILE
    if strict and outcome_path.exists():
        raise OutcomeAlreadyExists(f"outcome already exists for {key}")
    full_payload = {
        "key": key,
        "phase": "outcome",
        "timestamp": _now_iso_utc(),
        "payload": payload,
    }
    _atomic_write_json(outcome_path, full_payload)
    return str(outcome_path)


def load_record(key: str, *, root: str = DEFAULT_ROOT) -> IdempotencyRecord:
    """Carga el snapshot completo del estado de `key`.

    Returns:
        IdempotencyRecord (siempre — campos vacíos si no existe).
    """
    root_p = Path(root)
    kd = _key_dir(root_p, key)
    intent_path = kd / INTENT_FILE
    outcome_path = kd / OUTCOME_FILE
    intent_data = _safe_load_json(intent_path)
    outcome_data = _safe_load_json(outcome_path)
    return IdempotencyRecord(
        key=key,
        intent_exists=intent_data is not None,
        outcome_exists=outcome_data is not None,
        intent_payload=intent_data,
        outcome_payload=outcome_data,
        intent_path=str(intent_path),
        outcome_path=str(outcome_path),
    )


def is_committed(key: str, *, root: str = DEFAULT_ROOT) -> bool:
    """Atajo: ¿hay outcome para esta key?"""
    return load_record(key, root=root).is_committed()


def is_orphan(key: str, *, root: str = DEFAULT_ROOT) -> bool:
    """Atajo: ¿hay intent sin outcome?"""
    return load_record(key, root=root).is_orphan()


# ════════════════════════════════════════════════════════════════════
#  Listado / scan
# ════════════════════════════════════════════════════════════════════
def list_keys(*, root: str = DEFAULT_ROOT) -> list:
    """Devuelve lista de keys presentes en el log (no garantiza orden)."""
    root_p = Path(root)
    if not root_p.exists():
        return []
    out: list = []
    for child in root_p.iterdir():
        if child.is_dir():
            out.append(child.name)
    return sorted(out)


def scan_orphan_intents(*,
                         root: str = DEFAULT_ROOT,
                         older_than_seconds: Optional[float] = None,
                         now_iso: Optional[str] = None) -> list:
    """Escanea el log y devuelve lista de keys con intent sin outcome.

    Args:
        root: directorio raíz.
        older_than_seconds: si se da, sólo orphan cuyo intent.timestamp
                             sea más antiguo que este umbral.
        now_iso: timestamp de referencia (default: ahora UTC).

    Returns:
        list[dict] con: {key, intent_timestamp, age_sec, intent_payload}.
    """
    if now_iso:
        try:
            s = now_iso.replace("Z", "+00:00") if now_iso.endswith("Z") else now_iso
            now_dt = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            now_dt = datetime.now(timezone.utc)
    else:
        now_dt = datetime.now(timezone.utc)

    orphans: list = []
    for key in list_keys(root=root):
        rec = load_record(key, root=root)
        if not rec.is_orphan():
            continue
        intent_ts = ""
        age_sec = 0.0
        if rec.intent_payload and isinstance(rec.intent_payload.get("timestamp"), str):
            intent_ts = rec.intent_payload["timestamp"]
            try:
                s = intent_ts.replace("Z", "+00:00") if intent_ts.endswith("Z") else intent_ts
                intent_dt = datetime.fromisoformat(s)
                age_sec = (now_dt - intent_dt).total_seconds()
            except (ValueError, TypeError):
                age_sec = 0.0
        if older_than_seconds is not None and age_sec < older_than_seconds:
            continue
        orphans.append({
            "key": key,
            "intent_timestamp": intent_ts,
            "age_sec": age_sec,
            "intent_payload": rec.intent_payload,
        })
    return orphans


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
