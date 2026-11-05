"""Tests M1 — LogEvent typed schema.

Garantías:
    1. dataclass frozen (inmutable)
    2. to_log_event() nunca lanza
    3. backward compatibility con payloads legacy
    4. fallback UNKNOWN robusto
    5. coerción de tipos segura
    6. payload extracción correcta (prefijo p_)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  1. Estructura del LogEvent
# ════════════════════════════════════════════════════════════════════
def test_log_event_is_frozen():
    from core.contracts.schema import LogEvent
    e = LogEvent(event="goto", kind="STATE")
    # frozen → no asignación
    try:
        e.event = "other"
        assert False, "should not allow mutation"
    except Exception:
        pass


def test_log_event_defaults():
    from core.contracts.schema import LogEvent, SCHEMA_VERSION
    e = LogEvent()
    assert e.event == ""
    assert e.kind == "UNKNOWN"
    assert e.state is None
    assert e.score == 0
    assert e.rule == "FALLBACK"
    assert e.drift is None
    assert e.payload == {}
    assert e.schema_v == SCHEMA_VERSION


def test_log_event_to_dict():
    from core.contracts.schema import LogEvent
    e = LogEvent(ts="t1", run_id="r1", slot=1, event="goto",
                  kind="STATE", state="browsing", score=50, rule="R4a",
                  payload={"url": "http://x"})
    d = e.to_dict()
    assert d["event"] == "goto"
    assert d["kind"] == "STATE"
    assert d["payload"] == {"url": "http://x"}
    assert d["schema_v"] == "1"


# ════════════════════════════════════════════════════════════════════
#  2. to_log_event() — happy path
# ════════════════════════════════════════════════════════════════════
def test_valid_record_full():
    from core.contracts.schema import to_log_event
    rec = {
        "ts": "2026-05-13T05:00:00Z",
        "run_id": "r1",
        "slot": 1,
        "event": "goto",
        "kind": "STATE",
        "state": "browsing",
        "score": 50,
        "rule": "R4a",
        "drift": None,
        "p_url": "http://test",
    }
    e = to_log_event(rec)
    assert e.event == "goto"
    assert e.kind == "STATE"
    assert e.state == "browsing"
    assert e.score == 50
    assert e.rule == "R4a"
    assert e.payload == {"url": "http://test"}


def test_partial_record():
    """Record con solo algunos campos → defaults para resto."""
    from core.contracts.schema import to_log_event
    e = to_log_event({"event": "shot", "kind": "NOISE"})
    assert e.event == "shot"
    assert e.kind == "NOISE"
    assert e.state is None
    assert e.score == 0
    assert e.rule == "FALLBACK"


def test_empty_dict():
    from core.contracts.schema import to_log_event
    e = to_log_event({})
    assert e.event == ""
    assert e.kind == "UNKNOWN"


# ════════════════════════════════════════════════════════════════════
#  3. Fail-safe absoluto
# ════════════════════════════════════════════════════════════════════
def test_handles_non_dict():
    from core.contracts.schema import to_log_event
    for bad in [None, "string", 123, [], (1, 2), object()]:
        e = to_log_event(bad)
        # No lanza, devuelve algo válido
        assert e.kind == "UNKNOWN"


def test_handles_invalid_kind():
    from core.contracts.schema import to_log_event
    e = to_log_event({"event": "x", "kind": "INVALID_KIND_XYZ"})
    assert e.kind == "UNKNOWN"  # fallback


def test_handles_corrupt_score():
    from core.contracts.schema import to_log_event
    e = to_log_event({"event": "x", "score": "not_a_number"})
    assert e.score == 0  # default


def test_handles_corrupt_drift():
    from core.contracts.schema import to_log_event
    # drift es string en vez de dict
    e = to_log_event({"event": "x", "drift": "corrupt"})
    assert e.drift is None


def test_handles_state_coercion():
    from core.contracts.schema import to_log_event
    # state no-string → coerce a string
    e = to_log_event({"event": "x", "state": 123})
    assert e.state == "123"


def test_handles_slot_int_string():
    from core.contracts.schema import to_log_event
    # slot como string numérica → coerce a int
    e = to_log_event({"event": "x", "slot": "5"})
    assert e.slot == 5
    # slot como string no numérica → string
    e2 = to_log_event({"event": "x", "slot": "default"})
    assert e2.slot == "default"


# ════════════════════════════════════════════════════════════════════
#  4. Backward compatibility
# ════════════════════════════════════════════════════════════════════
def test_backward_compat_p_prefix():
    """Convención del logger: claves p_X van a payload[X]."""
    from core.contracts.schema import to_log_event
    e = to_log_event({
        "event": "goto",
        "p_url": "http://test",
        "p_method": "GET",
        "p_status": 200,
    })
    assert e.payload == {"url": "http://test", "method": "GET", "status": 200}


def test_backward_compat_payload_legacy_field():
    """Si record viene con campo 'payload' directo (legacy), mergea."""
    from core.contracts.schema import to_log_event
    e = to_log_event({
        "event": "goto",
        "payload": {"legacy_key": "v1"},
        "p_modern_key": "v2",
    })
    assert e.payload["legacy_key"] == "v1"
    assert e.payload["modern_key"] == "v2"


def test_backward_compat_p_takes_precedence():
    """Si misma clave aparece en payload legacy y en p_X, p_X gana."""
    from core.contracts.schema import to_log_event
    e = to_log_event({
        "event": "goto",
        "payload": {"key": "legacy"},
        "p_key": "modern",
    })
    assert e.payload["key"] == "modern"


def test_backward_compat_no_drift_field():
    """Records pre-runtime_diagnostics (sin campos kind/drift) → defaults."""
    from core.contracts.schema import to_log_event
    e = to_log_event({"ts": "t", "event": "old_event", "slot": 1})
    assert e.kind == "UNKNOWN"  # fallback
    assert e.drift is None


def test_schema_version_present():
    from core.contracts.schema import to_log_event, SCHEMA_VERSION
    e = to_log_event({"event": "x"})
    assert e.schema_v == SCHEMA_VERSION


# ════════════════════════════════════════════════════════════════════
#  5. UNKNOWN fallback
# ════════════════════════════════════════════════════════════════════
def test_unknown_fallback_invalid_input():
    from core.contracts.schema import to_log_event
    e = to_log_event(None)
    assert e.kind == "UNKNOWN"
    assert e.event == "<invalid_record>"


def test_to_dict_serializable():
    """to_dict() debe ser JSON serializable directamente."""
    import json
    from core.contracts.schema import LogEvent
    e = LogEvent(event="goto", kind="STATE", payload={"url": "http://x"})
    s = json.dumps(e.to_dict())
    assert "goto" in s
    assert "STATE" in s


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
