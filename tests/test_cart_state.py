"""tests/test_cart_state.py — Test suite for CartState model (STEP 1).

Cubre los predicados de salud + transiciones + inmutabilidad + serialización.

Categorías:
    1. Construction & defaults    (5 tests)
    2. Legal transitions           (7 tests)
    3. Illegal transitions         (6 tests)
    4. Terminal phases             (4 tests)
    5. Immutability & revisions    (5 tests)
    6. Expiration detection        (5 tests)
    7. Staleness detection         (5 tests)
    8. Checkout readiness          (5 tests)
    9. Serialization               (3 tests)
   10. Public API surface          (3 tests)

stdlib + pytest only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.purchase_state import (
    LEGAL_TRANSITIONS,
    TERMINAL_PHASES,
    CartPhase,
    CartState,
    IllegalCartTransition,
    SeatLock,
    new_cart_state,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
def _seat(seat_id: str = "s1", section: str = "A", row: str = "5",
          seat: str = "10", price: float = 100.0,
          expires_at_iso: str = "") -> SeatLock:
    return SeatLock(
        seat_id=seat_id, section=section, row=row, seat=seat,
        price=price, reserved_at_iso="", expires_at_iso=expires_at_iso,
    )


def _new(*, cart_id: str = "cart-x", event_id: str = "evt-x",
         account_id: str = "acc-x") -> CartState:
    return new_cart_state(cart_id=cart_id, event_id=event_id,
                           account_id=account_id)


# ════════════════════════════════════════════════════════════════════
#  1. CONSTRUCTION & DEFAULTS
# ════════════════════════════════════════════════════════════════════
class TestConstruction:

    def test_factory_creates_init_phase(self):
        c = _new()
        assert c.phase == CartPhase.INIT

    def test_factory_zero_revision(self):
        c = _new()
        assert c.revision == 0

    def test_factory_empty_seats(self):
        c = _new()
        assert c.seats == ()
        assert isinstance(c.seats, tuple)

    def test_factory_preserves_trace_ids(self):
        c = new_cart_state(cart_id="C1", event_id="E1", account_id="A1")
        assert c.cart_id == "C1"
        assert c.event_id == "E1"
        assert c.account_id == "A1"

    def test_factory_no_expiration_by_default(self):
        c = _new()
        assert c.expires_at_iso == ""
        assert c.last_validated_at_iso == ""


# ════════════════════════════════════════════════════════════════════
#  2. LEGAL TRANSITIONS
# ════════════════════════════════════════════════════════════════════
class TestLegalTransitions:

    def test_init_to_attached(self):
        c = _new().with_phase(CartPhase.ATTACHED)
        assert c.phase == CartPhase.ATTACHED

    def test_init_to_aborted(self):
        c = _new().with_phase(CartPhase.ABORTED)
        assert c.phase == CartPhase.ABORTED

    def test_init_to_failed(self):
        c = _new().with_phase(CartPhase.FAILED)
        assert c.phase == CartPhase.FAILED

    def test_attached_to_checking_out(self):
        c = _new().with_phase(CartPhase.ATTACHED).with_phase(CartPhase.CHECKING_OUT)
        assert c.phase == CartPhase.CHECKING_OUT

    def test_attached_to_expired(self):
        c = _new().with_phase(CartPhase.ATTACHED).with_phase(CartPhase.EXPIRED)
        assert c.phase == CartPhase.EXPIRED

    def test_checking_out_to_paid(self):
        c = (_new()
             .with_phase(CartPhase.ATTACHED)
             .with_phase(CartPhase.CHECKING_OUT)
             .with_phase(CartPhase.PAID))
        assert c.phase == CartPhase.PAID

    def test_checking_out_to_failed(self):
        c = (_new()
             .with_phase(CartPhase.ATTACHED)
             .with_phase(CartPhase.CHECKING_OUT)
             .with_phase(CartPhase.FAILED))
        assert c.phase == CartPhase.FAILED


# ════════════════════════════════════════════════════════════════════
#  3. ILLEGAL TRANSITIONS
# ════════════════════════════════════════════════════════════════════
class TestIllegalTransitions:

    def test_init_to_paid_blocked(self):
        c = _new()
        with pytest.raises(IllegalCartTransition):
            c.with_phase(CartPhase.PAID)

    def test_init_to_checking_out_blocked(self):
        c = _new()
        with pytest.raises(IllegalCartTransition):
            c.with_phase(CartPhase.CHECKING_OUT)

    def test_init_to_expired_blocked(self):
        c = _new()
        with pytest.raises(IllegalCartTransition):
            c.with_phase(CartPhase.EXPIRED)

    def test_paid_to_anything_blocked(self):
        paid = (_new()
                .with_phase(CartPhase.ATTACHED)
                .with_phase(CartPhase.CHECKING_OUT)
                .with_phase(CartPhase.PAID))
        for phase in (CartPhase.INIT, CartPhase.ATTACHED,
                       CartPhase.CHECKING_OUT, CartPhase.EXPIRED,
                       CartPhase.ABORTED, CartPhase.FAILED):
            with pytest.raises(IllegalCartTransition):
                paid.with_phase(phase)

    def test_expired_to_attached_blocked(self):
        expired = _new().with_phase(CartPhase.ATTACHED).with_phase(CartPhase.EXPIRED)
        with pytest.raises(IllegalCartTransition):
            expired.with_phase(CartPhase.ATTACHED)

    def test_non_cartphase_argument_raises(self):
        c = _new()
        ok, why = c.can_transition_to("attached")  # not a CartPhase
        assert ok is False
        assert "CartPhase" in why


# ════════════════════════════════════════════════════════════════════
#  4. TERMINAL PHASES
# ════════════════════════════════════════════════════════════════════
class TestTerminalPhases:

    def test_paid_is_terminal(self):
        c = (_new()
             .with_phase(CartPhase.ATTACHED)
             .with_phase(CartPhase.CHECKING_OUT)
             .with_phase(CartPhase.PAID))
        assert c.is_terminal()

    def test_expired_is_terminal(self):
        c = _new().with_phase(CartPhase.ATTACHED).with_phase(CartPhase.EXPIRED)
        assert c.is_terminal()

    def test_aborted_is_terminal(self):
        c = _new().with_phase(CartPhase.ABORTED)
        assert c.is_terminal()

    def test_failed_is_terminal(self):
        c = _new().with_phase(CartPhase.FAILED)
        assert c.is_terminal()

    def test_non_terminals_are_not_terminal(self):
        assert _new().is_terminal() is False  # INIT
        assert _new().with_phase(CartPhase.ATTACHED).is_terminal() is False
        c2 = _new().with_phase(CartPhase.ATTACHED).with_phase(CartPhase.CHECKING_OUT)
        assert c2.is_terminal() is False

    def test_terminal_phases_constant_is_correct(self):
        assert TERMINAL_PHASES == frozenset({
            CartPhase.PAID, CartPhase.EXPIRED,
            CartPhase.ABORTED, CartPhase.FAILED,
        })

    def test_legal_transitions_terminal_phases_have_empty_outgoing(self):
        for phase in TERMINAL_PHASES:
            assert LEGAL_TRANSITIONS[phase] == frozenset()


# ════════════════════════════════════════════════════════════════════
#  5. IMMUTABILITY & REVISIONS
# ════════════════════════════════════════════════════════════════════
class TestImmutability:

    def test_cannot_set_attribute_directly(self):
        c = _new()
        with pytest.raises((AttributeError, Exception)):
            c.phase = CartPhase.ATTACHED  # frozen

    def test_with_phase_returns_new_instance(self):
        c1 = _new()
        c2 = c1.with_phase(CartPhase.ATTACHED)
        assert c1 is not c2
        assert c1.phase == CartPhase.INIT  # original intact
        assert c2.phase == CartPhase.ATTACHED

    def test_revision_increments_on_phase_change(self):
        c = _new()
        assert c.revision == 0
        c = c.with_phase(CartPhase.ATTACHED)
        assert c.revision == 1
        c = c.with_phase(CartPhase.CHECKING_OUT)
        assert c.revision == 2

    def test_revision_increments_on_seats_change(self):
        c = _new()
        c = c.with_seats((_seat("s1"), _seat("s2")))
        assert c.revision == 1
        c = c.with_seats((_seat("s3"),))
        assert c.revision == 2

    def test_revalidated_only_bumps_revision_and_timestamp(self):
        c = _new().with_phase(CartPhase.ATTACHED)
        c2 = c.revalidated("2026-05-14T12:00:00+00:00")
        assert c2.phase == c.phase
        assert c2.revision == c.revision + 1
        assert c2.last_validated_at_iso == "2026-05-14T12:00:00+00:00"


# ════════════════════════════════════════════════════════════════════
#  6. EXPIRATION DETECTION
# ════════════════════════════════════════════════════════════════════
class TestExpiration:

    def test_no_expires_at_returns_false(self):
        c = _new()
        assert c.is_expired(now_iso="2026-05-14T10:00:00+00:00") is False

    def test_now_before_expiration(self):
        c = (_new()
             .with_phase(CartPhase.ATTACHED)
             .with_expiration("2026-05-14T10:10:00+00:00"))
        assert c.is_expired(now_iso="2026-05-14T10:05:00+00:00") is False

    def test_now_after_expiration(self):
        c = (_new()
             .with_phase(CartPhase.ATTACHED)
             .with_expiration("2026-05-14T10:00:00+00:00"))
        assert c.is_expired(now_iso="2026-05-14T10:01:00+00:00") is True

    def test_now_exactly_at_expiration(self):
        """Exactly at expires_at → NOT expired (strict >)."""
        c = (_new()
             .with_phase(CartPhase.ATTACHED)
             .with_expiration("2026-05-14T10:00:00+00:00"))
        assert c.is_expired(now_iso="2026-05-14T10:00:00+00:00") is False

    def test_malformed_now_returns_false(self):
        c = (_new()
             .with_phase(CartPhase.ATTACHED)
             .with_expiration("2026-05-14T10:00:00+00:00"))
        assert c.is_expired(now_iso="not-a-date") is False
        assert c.is_expired(now_iso="") is False


# ════════════════════════════════════════════════════════════════════
#  7. STALENESS DETECTION
# ════════════════════════════════════════════════════════════════════
class TestStaleness:

    def test_no_validation_history_is_stale(self):
        c = _new()
        assert c.is_stale(now_iso="2026-05-14T10:00:00+00:00") is True

    def test_recent_validation_not_stale(self):
        c = _new().with_phase(CartPhase.ATTACHED,
                                validated_at_iso="2026-05-14T10:00:00+00:00")
        # 2 seconds later, default max_age 3.0 → not stale
        assert c.is_stale(now_iso="2026-05-14T10:00:02+00:00") is False

    def test_old_validation_is_stale(self):
        c = _new().with_phase(CartPhase.ATTACHED,
                                validated_at_iso="2026-05-14T10:00:00+00:00")
        # 10 seconds later → stale (max_age default 3.0)
        assert c.is_stale(now_iso="2026-05-14T10:00:10+00:00") is True

    def test_custom_max_age(self):
        c = _new().with_phase(CartPhase.ATTACHED,
                                validated_at_iso="2026-05-14T10:00:00+00:00")
        # 5 seconds later, max_age 10.0 → not stale
        assert c.is_stale(now_iso="2026-05-14T10:00:05+00:00",
                           max_age_sec=10.0) is False
        # Same 5 seconds, max_age 4.0 → stale
        assert c.is_stale(now_iso="2026-05-14T10:00:05+00:00",
                           max_age_sec=4.0) is True

    def test_malformed_timestamps_are_stale_for_safety(self):
        c = _new().with_phase(CartPhase.ATTACHED,
                                validated_at_iso="not-a-date")
        assert c.is_stale(now_iso="2026-05-14T10:00:00+00:00") is True


# ════════════════════════════════════════════════════════════════════
#  8. CHECKOUT READINESS
# ════════════════════════════════════════════════════════════════════
class TestCheckoutReadiness:

    def test_init_not_ready(self):
        c = _new()
        ok, why = c.can_proceed_to_checkout()
        assert ok is False
        assert "ATTACHED" in why

    def test_attached_no_seats_not_ready(self):
        c = _new().with_phase(CartPhase.ATTACHED)
        ok, why = c.can_proceed_to_checkout()
        assert ok is False
        assert "seats" in why.lower()

    def test_attached_with_seats_ready(self):
        c = (_new()
             .with_seats((_seat("s1"),))
             .with_phase(CartPhase.ATTACHED))
        ok, why = c.can_proceed_to_checkout(now_iso="2026-05-14T10:00:00+00:00")
        assert ok is True

    def test_expired_cart_not_ready(self):
        c = (_new()
             .with_seats((_seat("s1"),))
             .with_phase(CartPhase.ATTACHED)
             .with_expiration("2026-05-14T10:00:00+00:00"))
        ok, why = c.can_proceed_to_checkout(now_iso="2026-05-14T10:05:00+00:00")
        assert ok is False
        assert "expired" in why.lower()

    def test_checking_out_already_not_ready(self):
        c = (_new()
             .with_seats((_seat("s1"),))
             .with_phase(CartPhase.ATTACHED)
             .with_phase(CartPhase.CHECKING_OUT))
        ok, why = c.can_proceed_to_checkout()
        assert ok is False


# ════════════════════════════════════════════════════════════════════
#  9. SERIALIZATION
# ════════════════════════════════════════════════════════════════════
class TestSerialization:

    def test_to_dict_contains_all_keys(self):
        c = (_new()
             .with_seats((_seat("s1"),))
             .with_phase(CartPhase.ATTACHED,
                         validated_at_iso="2026-05-14T10:00:00+00:00"))
        d = c.to_dict()
        assert set(d.keys()) == {
            "cart_id", "phase", "expires_at", "seats", "revision",
            "last_validated_at", "event_id", "account_id",
        }

    def test_to_dict_phase_is_string(self):
        c = _new()
        d = c.to_dict()
        assert d["phase"] == "init"

    def test_to_dict_is_json_serializable(self):
        c = (_new()
             .with_seats((_seat("s1", price=125.50),))
             .with_phase(CartPhase.ATTACHED))
        s = json.dumps(c.to_dict())
        assert isinstance(s, str) and len(s) > 0


# ════════════════════════════════════════════════════════════════════
#  10. PUBLIC API SURFACE
# ════════════════════════════════════════════════════════════════════
class TestPublicAPI:

    def test_all_exported_symbols(self):
        import core.purchase_state as ps
        for name in ("CartState", "CartPhase", "SeatLock",
                      "IllegalCartTransition", "new_cart_state",
                      "LEGAL_TRANSITIONS", "TERMINAL_PHASES"):
            assert hasattr(ps, name), f"Missing public export: {name}"

    def test_seat_lock_to_dict(self):
        s = _seat("X", section="B", row="2", seat="3", price=99.99)
        d = s.to_dict()
        assert d["seat_id"] == "X"
        assert d["price"] == 99.99

    def test_legal_transitions_covers_all_phases(self):
        for phase in CartPhase:
            assert phase in LEGAL_TRANSITIONS
