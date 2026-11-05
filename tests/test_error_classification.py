"""tests/test_error_classification.py — Test suite for STEP 3.

Cubre el módulo de clasificación + emitter estructurado.

Categorías:
    1. Taxonomía completa             (5 tests)
    2. ErrorEvent construcción         (6 tests)
    3. Heurístico classify_exception   (12 tests)
    4. emit_event / emit_from_exception (8 tests)
    5. Fail-safe sink                   (5 tests)
    6. JSONL persistence                (6 tests)
    7. Thread safety / concurrent emit  (3 tests)
    8. Extra sinks (subscribers)        (4 tests)
    9. Integration with CartState + idem (3 tests)
   10. Public API                       (3 tests)

stdlib + pytest only. Aislado en tmp_path (no toca runs/ real).
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.observability.error_classification import (
    ERROR_CODES,
    ErrorCategory,
    ErrorEvent,
    ErrorSeverity,
    all_categories,
    classify_exception,
    is_known_code,
    list_codes,
    make_event,
)
from core.observability.event_emitter import (
    add_sink,
    clear_sinks,
    emit,
    emit_event,
    emit_from_exception,
    get_sink_path,
    read_events,
    set_sink_path,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolated_sink(tmp_path):
    """Cada test usa su propio sink path + extra sinks limpios."""
    sink = tmp_path / "events.jsonl"
    set_sink_path(sink)
    clear_sinks()
    yield sink
    clear_sinks()
    set_sink_path(None)


# ════════════════════════════════════════════════════════════════════
#  1. TAXONOMÍA COMPLETA
# ════════════════════════════════════════════════════════════════════
class TestTaxonomy:

    def test_all_seven_categories_present(self):
        cats = {c.value for c in all_categories()}
        assert cats == {
            "flow", "network", "anti_bot", "cart_lifecycle",
            "inventory", "queue", "payment",
        }

    def test_every_category_has_codes(self):
        for cat in all_categories():
            codes = list_codes(cat)
            assert isinstance(codes, frozenset)
            assert len(codes) > 0, f"Empty codes for {cat.value}"

    def test_known_code_lookup(self):
        assert is_known_code(ErrorCategory.PAYMENT, "card_declined")
        assert is_known_code(ErrorCategory.NETWORK, "timeout")
        assert not is_known_code(ErrorCategory.PAYMENT, "nonexistent_code")

    def test_severity_levels(self):
        assert ErrorSeverity.RECOVERABLE.value == "recoverable"
        assert ErrorSeverity.RETRYABLE.value == "retryable"
        assert ErrorSeverity.FATAL.value == "fatal"
        assert ErrorSeverity.NON_RECOVERABLE.value == "non_recoverable"

    def test_codes_are_unique_within_category(self):
        for cat, codes in ERROR_CODES.items():
            assert len(codes) == len({c for c in codes})


# ════════════════════════════════════════════════════════════════════
#  2. ErrorEvent CONSTRUCCIÓN
# ════════════════════════════════════════════════════════════════════
class TestErrorEventConstruction:

    def test_make_event_minimal(self):
        ev = make_event(
            category=ErrorCategory.NETWORK,
            code="timeout",
        )
        assert ev.category == ErrorCategory.NETWORK
        assert ev.code == "timeout"
        assert ev.severity == ErrorSeverity.RECOVERABLE
        assert ev.timestamp_iso  # autogenerado

    def test_make_event_with_full_fields(self):
        ev = make_event(
            category=ErrorCategory.PAYMENT,
            code="card_declined",
            severity=ErrorSeverity.FATAL,
            message="card refused",
            context="submit_payment",
            cart_id="CART-1",
            idempotency_key="ik-abc",
            payload={"decline_code": "DO_NOT_HONOR"},
        )
        assert ev.message == "card refused"
        assert ev.context == "submit_payment"
        assert ev.cart_id == "CART-1"
        assert ev.idempotency_key == "ik-abc"
        assert ev.payload == {"decline_code": "DO_NOT_HONOR"}

    def test_event_is_frozen(self):
        ev = make_event(category=ErrorCategory.FLOW, code="step_failed")
        with pytest.raises((AttributeError, Exception)):
            ev.code = "tampered"

    def test_event_to_dict_has_all_keys(self):
        ev = make_event(category=ErrorCategory.NETWORK, code="timeout")
        d = ev.to_dict()
        assert set(d.keys()) >= {
            "category", "code", "severity", "timestamp",
            "message", "context", "payload", "exception_class",
            "cart_id", "idempotency_key",
        }

    def test_event_to_dict_json_serializable(self):
        ev = make_event(category=ErrorCategory.PAYMENT, code="3ds_required",
                         payload={"nested": {"x": [1, 2, 3]}})
        s = json.dumps(ev.to_dict())
        assert isinstance(s, str) and len(s) > 0

    def test_message_truncated_at_500_chars(self):
        long_msg = "x" * 1000
        ev = make_event(category=ErrorCategory.FLOW, code="step_failed",
                         message=long_msg)
        assert len(ev.message) == 500


# ════════════════════════════════════════════════════════════════════
#  3. HEURÍSTICO classify_exception
# ════════════════════════════════════════════════════════════════════
class TestClassifyException:

    def test_connection_error_classified_network(self):
        ev = classify_exception(ConnectionError("connection refused by proxy"))
        assert ev.category == ErrorCategory.NETWORK
        assert ev.code in {"proxy_dead", "connection_refused", "connection_reset"}
        assert ev.severity == ErrorSeverity.RETRYABLE

    def test_timeout_classified_network_timeout(self):
        ev = classify_exception(TimeoutError("operation timeout"))
        assert ev.category == ErrorCategory.NETWORK
        assert ev.code == "timeout"

    def test_session_suspended_classified_anti_bot(self):
        ev = classify_exception(message="Su sesión se ha suspendido")
        assert ev.category == ErrorCategory.ANTI_BOT
        assert ev.code == "bot_block_detected"
        assert ev.severity == ErrorSeverity.NON_RECOVERABLE

    def test_akamai_classified_anti_bot(self):
        ev = classify_exception(message="Browsing Activity Has Been Paused")
        assert ev.category == ErrorCategory.ANTI_BOT
        assert ev.code == "akamai_block"

    def test_captcha_classified_anti_bot(self):
        ev = classify_exception(message="CAPTCHA required to continue")
        assert ev.category == ErrorCategory.ANTI_BOT
        assert ev.code == "captcha_required"

    def test_cart_expired_classified_cart_lifecycle(self):
        ev = classify_exception(message="Your cart expired")
        assert ev.category == ErrorCategory.CART_LIFECYCLE
        assert ev.code == "cart_expired"

    def test_sold_out_classified_inventory(self):
        ev = classify_exception(message="Tickets sold out")
        assert ev.category == ErrorCategory.INVENTORY
        assert ev.code == "seat_sold_out"

    def test_queue_reassign_classified_queue(self):
        ev = classify_exception(message="queue_reassigned position=5000")
        assert ev.category == ErrorCategory.QUEUE
        assert ev.code == "queue_reassigned"

    def test_card_declined_classified_payment(self):
        ev = classify_exception(message="card declined - INSUFFICIENT_FUNDS")
        assert ev.category == ErrorCategory.PAYMENT
        # "insufficient funds" es más específico que "declined"
        assert ev.code in {"insufficient_funds", "card_declined"}

    def test_3ds_required_classified_payment(self):
        ev = classify_exception(message="3DS authentication required")
        assert ev.category == ErrorCategory.PAYMENT
        assert ev.code in {"3ds_required", "3ds_failed"}

    def test_unknown_falls_back_flow(self):
        ev = classify_exception(message="totally random gibberish xyzqwe")
        assert ev.category == ErrorCategory.FLOW
        assert ev.code == "step_failed"
        assert ev.severity == ErrorSeverity.RECOVERABLE

    def test_classify_preserves_trace_ids(self):
        ev = classify_exception(
            ConnectionError("proxy"),
            context="attach_cart",
            cart_id="CART-X",
            idempotency_key="ik-1234",
            payload={"step": 3},
        )
        assert ev.context == "attach_cart"
        assert ev.cart_id == "CART-X"
        assert ev.idempotency_key == "ik-1234"
        assert ev.payload == {"step": 3}
        assert ev.exception_class == "ConnectionError"


# ════════════════════════════════════════════════════════════════════
#  4. emit_event / emit_from_exception
# ════════════════════════════════════════════════════════════════════
class TestEmit:

    def test_emit_event_returns_event(self):
        ev = emit_event(ErrorCategory.PAYMENT, "card_declined")
        assert isinstance(ev, ErrorEvent)
        assert ev.code == "card_declined"

    def test_emit_event_accepts_string_category(self):
        ev = emit_event("network", "timeout")
        assert ev is not None
        assert ev.category == ErrorCategory.NETWORK

    def test_emit_event_invalid_category_falls_back_to_flow(self):
        ev = emit_event("invented_category", "x")
        assert ev.category == ErrorCategory.FLOW

    def test_emit_event_invalid_severity_falls_back(self):
        ev = emit_event(ErrorCategory.PAYMENT, "card_declined",
                         severity="weird_severity")
        assert ev.severity == ErrorSeverity.RECOVERABLE

    def test_emit_event_writes_to_sink(self, _isolated_sink):
        emit_event(ErrorCategory.NETWORK, "timeout", context="step_X")
        events = read_events()
        assert len(events) == 1
        assert events[0]["category"] == "network"
        assert events[0]["code"] == "timeout"
        assert events[0]["context"] == "step_X"

    def test_emit_from_exception_classifies_and_emits(self, _isolated_sink):
        try:
            raise ConnectionError("proxy refused")
        except ConnectionError as e:
            ev = emit_from_exception(e, context="attach_cart")
        events = read_events()
        assert len(events) == 1
        assert events[0]["category"] == "network"
        assert events[0]["exception_class"] == "ConnectionError"

    def test_emit_returns_none_for_invalid_event(self):
        # emit() acepta solo ErrorEvent
        assert emit("not an event") is False

    def test_emit_event_None_payload_safe(self):
        ev = emit_event(ErrorCategory.FLOW, "step_failed", None)
        assert ev is not None
        assert ev.payload == {}


# ════════════════════════════════════════════════════════════════════
#  5. FAIL-SAFE — nunca propagar
# ════════════════════════════════════════════════════════════════════
class TestFailSafe:

    def test_emit_event_never_raises_with_garbage_payload(self):
        # No raise aunque payload sea raro
        assert emit_event(ErrorCategory.FLOW, "step_failed",
                           payload={"obj": object()}) is not None

    def test_emit_event_with_non_serializable_payload_returns_event(self, _isolated_sink):
        class WeirdObj:
            pass
        ev = emit_event(ErrorCategory.FLOW, "step_failed",
                         payload={"weird": WeirdObj()})
        # Event se construye, pero la escritura al sink puede fallar silenciosamente.
        # Lo importante: no raise.
        assert ev is not None

    def test_emit_from_exception_with_None_exc(self):
        ev = emit_from_exception(exc=None, message="empty error")
        assert ev is not None
        assert ev.category == ErrorCategory.FLOW

    def test_classify_with_exception_that_str_raises(self):
        class BadStr(Exception):
            def __str__(self):
                raise RuntimeError("can't stringify me")
        ev = classify_exception(BadStr())
        # No raise — caemos a FLOW/step_failed
        assert isinstance(ev, ErrorEvent)

    def test_emit_to_non_writable_sink_returns_false(self, tmp_path):
        # Apuntar a un path que no se puede crear (file existente como dir parent)
        bad_file = tmp_path / "blocker"
        bad_file.write_text("not a directory")
        set_sink_path(bad_file / "events.jsonl")
        ev = make_event(category=ErrorCategory.FLOW, code="step_failed")
        # emit no debe explotar
        result = emit(ev)
        # Puede ser True o False según OS, lo importante: NO raise


# ════════════════════════════════════════════════════════════════════
#  6. JSONL PERSISTENCE
# ════════════════════════════════════════════════════════════════════
class TestJSONLPersistence:

    def test_each_event_is_one_line(self, _isolated_sink):
        for i in range(5):
            emit_event(ErrorCategory.NETWORK, "timeout", message=f"#{i}")
        lines = _isolated_sink.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 5
        for line in lines:
            d = json.loads(line)
            assert d["category"] == "network"

    def test_append_only_does_not_overwrite(self, _isolated_sink):
        emit_event(ErrorCategory.NETWORK, "timeout", message="first")
        emit_event(ErrorCategory.PAYMENT, "card_declined", message="second")
        events = read_events()
        assert len(events) == 2
        assert events[0]["message"] == "first"
        assert events[1]["message"] == "second"

    def test_read_events_skips_corrupt_lines(self, _isolated_sink):
        # Escribimos manualmente una línea válida y una corrupta
        emit_event(ErrorCategory.NETWORK, "timeout")
        with _isolated_sink.open("a", encoding="utf-8") as f:
            f.write("{ not valid json\n")
        emit_event(ErrorCategory.PAYMENT, "card_declined")
        events = read_events()
        # Sólo 2 válidos (corrupto ignorado)
        assert len(events) == 2

    def test_read_events_empty_when_no_file(self, tmp_path):
        set_sink_path(tmp_path / "does_not_exist.jsonl")
        assert read_events() == []

    def test_events_keys_sorted_in_output(self, _isolated_sink):
        emit_event(ErrorCategory.PAYMENT, "card_declined")
        line = _isolated_sink.read_text(encoding="utf-8").strip()
        d = json.loads(line)
        # Verificar serialización estable (sort_keys=True)
        # → si volcamos de nuevo con sort_keys=True, debe ser idéntico
        assert json.dumps(d, sort_keys=True) == line

    def test_timestamps_present_and_iso8601(self, _isolated_sink):
        emit_event(ErrorCategory.NETWORK, "timeout")
        events = read_events()
        ts = events[0]["timestamp"]
        # ISO 8601 contiene T y +00:00 (UTC)
        assert "T" in ts
        assert "+" in ts or "Z" in ts


# ════════════════════════════════════════════════════════════════════
#  7. THREAD SAFETY
# ════════════════════════════════════════════════════════════════════
class TestThreadSafety:

    def test_concurrent_emits_all_landed(self, _isolated_sink):
        N = 50

        def _emit_many(idx):
            for i in range(10):
                emit_event(ErrorCategory.NETWORK, "timeout",
                            message=f"thread{idx}_msg{i}")

        threads = [threading.Thread(target=_emit_many, args=(i,))
                    for i in range(N // 10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = read_events()
        assert len(events) == N  # 5 threads × 10 emits = 50

    def test_no_partial_lines_under_concurrency(self, _isolated_sink):
        N = 100

        def _emit():
            emit_event(ErrorCategory.PAYMENT, "card_declined",
                        message="x" * 200)  # 200-char message

        threads = [threading.Thread(target=_emit) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Cada línea debe ser JSON válido
        lines = _isolated_sink.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == N
        for line in lines:
            json.loads(line)  # no raise = OK

    def test_emit_does_not_block_extra_sink_failures(self, _isolated_sink):
        def bad_sink(_):
            raise RuntimeError("bad sink")
        add_sink(bad_sink)
        # No debe propagar la exception
        ev = emit_event(ErrorCategory.NETWORK, "timeout")
        assert ev is not None


# ════════════════════════════════════════════════════════════════════
#  8. EXTRA SINKS (subscribers)
# ════════════════════════════════════════════════════════════════════
class TestExtraSinks:

    def test_add_sink_receives_event(self, _isolated_sink):
        received = []
        add_sink(lambda ev: received.append(ev))
        emit_event(ErrorCategory.NETWORK, "timeout")
        assert len(received) == 1
        assert received[0]["category"] == "network"

    def test_clear_sinks_removes_subscribers(self, _isolated_sink):
        received = []
        add_sink(lambda ev: received.append(ev))
        clear_sinks()
        emit_event(ErrorCategory.NETWORK, "timeout")
        assert received == []

    def test_multiple_sinks_all_receive(self, _isolated_sink):
        a = []
        b = []
        add_sink(lambda ev: a.append(ev))
        add_sink(lambda ev: b.append(ev))
        emit_event(ErrorCategory.PAYMENT, "card_declined")
        assert len(a) == 1
        assert len(b) == 1

    def test_non_callable_sink_silently_ignored(self, _isolated_sink):
        add_sink("not callable")  # noop
        # No raise
        emit_event(ErrorCategory.NETWORK, "timeout")


# ════════════════════════════════════════════════════════════════════
#  9. INTEGRACIÓN con CartState + idempotency_log
# ════════════════════════════════════════════════════════════════════
class TestIntegration:

    def test_event_carries_idempotency_key(self, _isolated_sink):
        from core.idempotency import make_idempotency_key
        ik = make_idempotency_key(
            event_id="E1", seats=["S1"], account_id="A1",
            timestamp_iso="2026-05-14T10:00:00+00:00",
        )
        ev = emit_event(
            ErrorCategory.PAYMENT, "card_declined",
            idempotency_key=ik, cart_id="CART-1",
        )
        events = read_events()
        assert events[0]["idempotency_key"] == ik

    def test_event_carries_cart_state_phase(self, _isolated_sink):
        from core.purchase_state import CartPhase
        ev = emit_event(
            ErrorCategory.CART_LIFECYCLE, "cart_expired",
            payload={"phase": CartPhase.CHECKING_OUT.value},
            cart_id="CART-9",
        )
        events = read_events()
        assert events[0]["payload"]["phase"] == "checking_out"
        assert events[0]["cart_id"] == "CART-9"

    def test_classify_illegal_cart_transition_as_flow(self):
        from core.purchase_state import IllegalCartTransition
        ev = classify_exception(IllegalCartTransition("paid -> attached"))
        assert ev.category == ErrorCategory.FLOW
        assert ev.code == "transition_invalid"


# ════════════════════════════════════════════════════════════════════
#  10. PUBLIC API
# ════════════════════════════════════════════════════════════════════
class TestPublicAPI:

    def test_classification_exports(self):
        import core.observability.error_classification as ec
        for name in ("ErrorCategory", "ErrorSeverity", "ErrorEvent",
                      "classify_exception", "make_event",
                      "is_known_code", "list_codes", "all_categories",
                      "ERROR_CODES"):
            assert hasattr(ec, name)

    def test_emitter_exports(self):
        import core.observability.event_emitter as ee
        for name in ("emit", "emit_event", "emit_from_exception",
                      "add_sink", "clear_sinks", "set_sink_path",
                      "get_sink_path", "read_events"):
            assert hasattr(ee, name)

    def test_sink_path_resettable(self, tmp_path):
        custom = tmp_path / "custom.jsonl"
        set_sink_path(custom)
        assert get_sink_path() == custom
        set_sink_path(None)
        # Tras reset, get_sink_path crea un default (no None)
        assert get_sink_path() is not None
