"""core/idempotency/keys.py — Idempotency key generation (STEP 2).

Genera claves determinísticas para identificar UNÍVOCAMENTE una
intención de compra. Dos intentos con la misma intención (mismo
evento, mismas seats, misma cuenta, mismo bucket temporal) producen
la MISMA clave → reintento seguro.

PRINCIPIO:
    La clave debe ser INSENSIBLE a:
        - orden de seats en el input (canonicalizamos sort)
        - whitespace en strings
    La clave debe ser SENSIBLE a:
        - event_id
        - lista de seat_ids (set semantics)
        - account_id
        - day_bucket (granularidad temporal configurable)

stdlib only (hashlib + datetime). Determinístico. Sin random.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
KEY_LENGTH: int = 32  # hex chars del SHA-256 truncado (128 bits)
DEFAULT_BUCKET_GRANULARITY: str = "day"  # "day" | "hour" | "minute" | "none"
KEY_PREFIX: str = "ik"   # prefijo legible


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _canonical_seats(seats: Any) -> str:
    """Canonicaliza una lista de seats a una representación estable.

    Acepta: list[str], list[dict] (con clave "seat_id"), tuple, set.
    Devuelve: "seat1|seat2|seat3" ordenados alfabéticamente.
    """
    if seats is None:
        return ""
    if isinstance(seats, (str, bytes)):
        # Un string solo → tratar como un único seat
        return _safe_str(seats)
    if not hasattr(seats, "__iter__"):
        return _safe_str(seats)

    ids: list = []
    for s in seats:
        if isinstance(s, dict):
            sid = s.get("seat_id") or s.get("id") or ""
            ids.append(_safe_str(sid))
        else:
            ids.append(_safe_str(s))
    # Eliminar vacíos y ordenar para canonicalidad
    ids = sorted(set(x for x in ids if x))
    return "|".join(ids)


def _bucket_of(ts_iso: str, granularity: str) -> str:
    """Devuelve el bucket temporal de un timestamp ISO.

    granularity:
        "day"     → "YYYY-MM-DD"
        "hour"    → "YYYY-MM-DDTHH"
        "minute"  → "YYYY-MM-DDTHH:MM"
        "none"    → ""

    Si ts_iso es inválido o vacío → "" (no bucket).
    """
    if granularity == "none":
        return ""
    if not ts_iso:
        return ""
    try:
        s = ts_iso.replace("Z", "+00:00") if ts_iso.endswith("Z") else ts_iso
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return ""

    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "hour":
        return dt.strftime("%Y-%m-%dT%H")
    if granularity == "minute":
        return dt.strftime("%Y-%m-%dT%H:%M")
    return ""


# ════════════════════════════════════════════════════════════════════
#  API principal: make_idempotency_key
# ════════════════════════════════════════════════════════════════════
def make_idempotency_key(*,
                          event_id: Any,
                          seats: Any,
                          account_id: Any,
                          timestamp_iso: str = "",
                          bucket_granularity: str = DEFAULT_BUCKET_GRANULARITY,
                          extra: Any = None) -> str:
    """Genera una clave de idempotencia determinística.

    Args:
        event_id: identificador del evento.
        seats: lista/tuple/set de seat_ids (str) o seat dicts con
               campo "seat_id". Se canonicaliza por sort+set.
        account_id: identificador de la cuenta.
        timestamp_iso: ISO 8601 UTC. Usado para derivar bucket temporal.
        bucket_granularity: "day"|"hour"|"minute"|"none". Default "day".
        extra: cualquier objeto serializable extra para diferenciar
               intentos (p.ej. tipo de tarjeta). Opcional.

    Returns:
        str: clave de la forma "ik-<32 hex chars>".

    Determinístico: misma intención → misma clave bit-exact.
    """
    bucket = _bucket_of(_safe_str(timestamp_iso), bucket_granularity)
    parts = [
        _safe_str(event_id),
        _canonical_seats(seats),
        _safe_str(account_id),
        bucket,
        _safe_str(extra) if extra is not None else "",
    ]
    blob = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    return f"{KEY_PREFIX}-{digest[:KEY_LENGTH]}"


def is_valid_key(key: Any) -> bool:
    """¿`key` tiene el formato esperado?"""
    if not isinstance(key, str):
        return False
    if not key.startswith(f"{KEY_PREFIX}-"):
        return False
    rest = key[len(KEY_PREFIX) + 1:]
    if len(rest) != KEY_LENGTH:
        return False
    try:
        int(rest, 16)
        return True
    except ValueError:
        return False


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
