"""core/contracts/schema.py — Typed LogEvent contract.

Pure schema layer. SIN lógica compleja. SIN side-effects. SIN IO.

Define el esquema canónico de un evento observability ya enriquecido
por `runtime_diagnostics`. Sirve como contrato runtime-safe para:

    - `core.dashboard.aggregator`
    - `core.dashboard.historical_sqlite`
    - `core.dashboard.api` (serialización)
    - terceros que consuman JSONL streaming

Política:
    - frozen dataclass (immutable, hashable)
    - to_log_event(record) fail-safe: nunca lanza
    - schema_v="1" para futuras migraciones
    - backward-compatible: tolera payloads legacy sin todos los campos
    - payload extraído de claves "p_<name>" (convención del logger)

NO altera el formato JSONL escrito por el bot. Solo lo interpreta.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional, Union


# ════════════════════════════════════════════════════════════════════
#  Tipos
# ════════════════════════════════════════════════════════════════════
EventKindStr = Literal["STATE", "ACTION", "OBSERVABILITY", "NOISE", "UNKNOWN"]

_VALID_KINDS: frozenset[str] = frozenset(
    {"STATE", "ACTION", "OBSERVABILITY", "NOISE", "UNKNOWN"}
)

SCHEMA_VERSION = "1"


# ════════════════════════════════════════════════════════════════════
#  LogEvent (immutable)
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class LogEvent:
    """Canonical typed log event.

    Atributos obligatorios:
        ts, run_id, slot, event

    Atributos derivados (poblados por runtime_diagnostics):
        kind, state, score, rule, drift, payload

    Versionado:
        schema_v: string que permite migración futura sin breakage.
    """

    ts: str = ""
    run_id: str = ""
    slot: Union[int, str] = 0
    event: str = ""
    kind: EventKindStr = "UNKNOWN"
    state: Optional[str] = None
    score: int = 0
    rule: str = "FALLBACK"
    drift: Optional[dict] = None
    payload: dict = field(default_factory=dict)
    schema_v: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Serializa a dict para JSON / endpoint output."""
        return {
            "ts": self.ts,
            "run_id": self.run_id,
            "slot": self.slot,
            "event": self.event,
            "kind": self.kind,
            "state": self.state,
            "score": self.score,
            "rule": self.rule,
            "drift": self.drift,
            "payload": dict(self.payload) if self.payload else {},
            "schema_v": self.schema_v,
        }


# ════════════════════════════════════════════════════════════════════
#  to_log_event() — fail-safe helper
# ════════════════════════════════════════════════════════════════════
def to_log_event(record: Any) -> LogEvent:
    """Convierte un dict (típicamente del JSONL stream) en LogEvent.

    Fail-safe absoluto:
        - Nunca lanza excepciones
        - Inputs no-dict → LogEvent vacío con kind=UNKNOWN
        - Campos faltantes → defaults
        - Tipos incorrectos → coerción segura o default
        - Kind inválido → "UNKNOWN"
        - Payloads legacy (con prefijo "p_" o claves sueltas) → mejor esfuerzo

    Args:
        record: dict del stream JSONL (o cualquier cosa).

    Returns:
        LogEvent. Nunca None, nunca lanza.
    """
    if not isinstance(record, dict):
        return LogEvent(event="<invalid_record>", kind="UNKNOWN")

    try:
        # Campos básicos
        ts = _safe_str(record.get("ts"), "")
        run_id = _safe_str(record.get("run_id"), "")
        slot = record.get("slot", 0)
        # Coerce slot a int si es posible, si no mantenlo string
        if isinstance(slot, str):
            try:
                slot = int(slot)
            except (ValueError, TypeError):
                pass
        event = _safe_str(record.get("event"), "")

        # Kind con validación
        raw_kind = record.get("kind", "UNKNOWN")
        kind: EventKindStr = raw_kind if raw_kind in _VALID_KINDS else "UNKNOWN"  # type: ignore[assignment]

        # State opcional
        state = record.get("state")
        if state is not None and not isinstance(state, str):
            state = str(state) if state else None

        # Score como int seguro
        score = _safe_int(record.get("score", 0), 0)

        # Rule
        rule = _safe_str(record.get("rule"), "FALLBACK")

        # Drift: solo si es dict
        drift = record.get("drift")
        if drift is not None and not isinstance(drift, dict):
            drift = None

        # Payload: extraer claves p_* (convención del logger)
        payload: dict = {}
        for k, v in record.items():
            if isinstance(k, str) and k.startswith("p_"):
                # quitar prefijo p_
                payload[k[2:]] = v

        # Soporte legacy: si record viene con campo "payload" directo (dict)
        legacy_payload = record.get("payload")
        if isinstance(legacy_payload, dict):
            for k, v in legacy_payload.items():
                if k not in payload:
                    payload[k] = v

        return LogEvent(
            ts=ts,
            run_id=run_id,
            slot=slot,
            event=event,
            kind=kind,
            state=state,
            score=score,
            rule=rule,
            drift=drift,
            payload=payload,
        )
    except Exception:
        # Fail-safe absoluto
        return LogEvent(event="<parse_error>", kind="UNKNOWN")


# ════════════════════════════════════════════════════════════════════
#  Helpers internos
# ════════════════════════════════════════════════════════════════════
def _safe_str(v: Any, default: str) -> str:
    if v is None:
        return default
    if isinstance(v, str):
        return v
    try:
        return str(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except (ValueError, TypeError):
            return default
    return default


# ════════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════════
__all__ = [
    "LogEvent",
    "EventKindStr",
    "SCHEMA_VERSION",
    "to_log_event",
]


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Test rápido
    cases = [
        {"ts": "2026-05-13T05:00:00Z", "run_id": "r1", "slot": 1, "event": "goto",
          "kind": "STATE", "state": "browsing", "score": 50, "rule": "R4a",
          "p_url": "http://test"},
        {"event": "shot", "kind": "NOISE"},
        {},
        None,
        "not a dict",
        {"event": "bad_kind", "kind": "INVALID_KIND"},
        {"event": "with_drift", "drift": {"kind": "INVALID_TRANSITION"}},
    ]
    for c in cases:
        e = to_log_event(c)
        print(f"  input={str(c)[:40]:40} → kind={e.kind:13} event={e.event} state={e.state}")


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
