"""tests/test_step12_shadow_canary.py — Shadow runner + canary tests.

Categorías:
    1. in_canary determinismo + sticky      (5 tests)
    2. run_with_shadow legacy primary       (4 tests)
    3. Shadow execution + comparison        (4 tests)
    4. Shadow crash isolation                (3 tests)
    5. Canary rate respect                   (3 tests)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.migration import (
    ShadowResult,
    in_canary,
    run_with_shadow,
)
from core.observability.event_emitter import (
    clear_sinks,
    read_events,
    set_sink_path,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    set_sink_path(tmp_path / "events.jsonl")
    clear_sinks()
    yield
    clear_sinks()
    set_sink_path(None)


def _legacy(ctx):
    return {"path": "legacy", "success": True, "cart_phase": "paid",
             "order_id": "ORD-L"}


def _shadow(ctx):
    return {"path": "shadow", "success": True, "cart_phase": "paid",
             "order_id": "ORD-S"}


def _shadow_crash(ctx):
    raise RuntimeError("shadow boom")


# ════════════════════════════════════════════════════════════════════
#  1. CANARY DETERMINISMO + STICKY
# ════════════════════════════════════════════════════════════════════
class TestCanaryDeterministic:

    def test_zero_rate_never_in_canary(self):
        for i in range(100):
            assert in_canary(rate=0.0, identity=f"A{i}") is False

    def test_one_rate_always_in_canary(self):
        for i in range(100):
            assert in_canary(rate=1.0, identity=f"A{i}") is True

    def test_sticky_same_identity_same_result(self):
        a = in_canary(rate=0.5, identity="STICKY")
        b = in_canary(rate=0.5, identity="STICKY")
        c = in_canary(rate=0.5, identity="STICKY")
        assert a == b == c

    def test_different_identities_distribution(self):
        results = [in_canary(rate=0.5, identity=f"A{i}") for i in range(1000)]
        true_count = sum(results)
        # ±10% sobre 50% (estadística)
        assert 400 <= true_count <= 600

    def test_anonymous_identity_stable(self):
        a = in_canary(rate=0.5)
        b = in_canary(rate=0.5)
        assert a == b


# ════════════════════════════════════════════════════════════════════
#  2. run_with_shadow — LEGACY PRIMARY
# ════════════════════════════════════════════════════════════════════
class TestLegacyPrimary:

    def test_legacy_always_called(self):
        legacy_calls = []
        def legacy(ctx):
            legacy_calls.append(ctx)
            return _legacy(ctx)
        run_with_shadow(
            ctx={}, legacy_callable=legacy, shadow_callable=_shadow,
            canary_rate=0.0,
        )
        assert len(legacy_calls) == 1

    def test_returns_legacy_result(self):
        r = run_with_shadow(
            ctx={}, legacy_callable=_legacy, shadow_callable=_shadow,
            canary_rate=1.0,
        )
        assert r.legacy_result["order_id"] == "ORD-L"

    def test_canary_rate_zero_no_shadow(self):
        shadow_calls = []
        def shadow(ctx):
            shadow_calls.append(ctx)
            return _shadow(ctx)
        run_with_shadow(
            ctx={}, legacy_callable=_legacy, shadow_callable=shadow,
            canary_rate=0.0,
        )
        assert shadow_calls == []

    def test_no_shadow_callable_no_call(self):
        r = run_with_shadow(
            ctx={}, legacy_callable=_legacy, shadow_callable=None,
            canary_rate=1.0,
        )
        assert r.shadow_executed is False
        assert r.legacy_result is not None


# ════════════════════════════════════════════════════════════════════
#  3. SHADOW EXECUTION + COMPARISON
# ════════════════════════════════════════════════════════════════════
class TestShadowExecution:

    def test_shadow_executed_when_in_canary(self):
        shadow_calls = []
        def shadow(ctx):
            shadow_calls.append(ctx)
            return _shadow(ctx)
        run_with_shadow(
            ctx={"key": "v"}, legacy_callable=_legacy,
            shadow_callable=shadow, canary_rate=1.0,
        )
        assert len(shadow_calls) == 1
        assert shadow_calls[0] == {"key": "v"}

    def test_divergence_computed(self):
        r = run_with_shadow(
            ctx={}, legacy_callable=_legacy, shadow_callable=_shadow,
            canary_rate=1.0,
        )
        assert r.divergence is not None
        assert "score" in r.divergence

    def test_compare_event_emitted(self):
        run_with_shadow(
            ctx={}, legacy_callable=_legacy, shadow_callable=_shadow,
            canary_rate=1.0,
        )
        events = read_events()
        codes = [e.get("code") for e in events]
        assert "canary_compare" in codes

    def test_latency_recorded(self):
        r = run_with_shadow(
            ctx={}, legacy_callable=_legacy, shadow_callable=_shadow,
            canary_rate=1.0,
        )
        assert r.legacy_latency_ms >= 0
        assert r.shadow_latency_ms >= 0


# ════════════════════════════════════════════════════════════════════
#  4. SHADOW CRASH ISOLATION
# ════════════════════════════════════════════════════════════════════
class TestShadowCrashIsolation:

    def test_shadow_crash_does_not_affect_legacy(self):
        r = run_with_shadow(
            ctx={}, legacy_callable=_legacy,
            shadow_callable=_shadow_crash, canary_rate=1.0,
        )
        assert r.legacy_result["order_id"] == "ORD-L"
        assert r.shadow_executed is False
        assert "boom" in r.shadow_error

    def test_legacy_propagates_normally(self):
        def bad_legacy(ctx):
            raise ValueError("legacy fail")
        with pytest.raises(ValueError):
            run_with_shadow(
                ctx={}, legacy_callable=bad_legacy,
                shadow_callable=_shadow, canary_rate=1.0,
            )

    def test_shadow_crash_does_not_raise(self):
        # No debe propagar
        r = run_with_shadow(
            ctx={}, legacy_callable=_legacy,
            shadow_callable=_shadow_crash, canary_rate=1.0,
        )
        assert isinstance(r, ShadowResult)


# ════════════════════════════════════════════════════════════════════
#  5. CANARY RATE RESPECT
# ════════════════════════════════════════════════════════════════════
class TestCanaryRateRespect:

    def test_rate_50_pct_approximate(self):
        executed = 0
        for i in range(200):
            r = run_with_shadow(
                ctx={}, legacy_callable=_legacy,
                shadow_callable=_shadow,
                canary_rate=0.5, canary_identity=f"A{i}",
            )
            if r.in_canary:
                executed += 1
        # ±15% tolerancia sobre 50%
        assert 70 <= executed <= 130

    def test_rate_1_pct_minimal(self):
        executed = 0
        for i in range(500):
            r = run_with_shadow(
                ctx={}, legacy_callable=_legacy,
                shadow_callable=_shadow,
                canary_rate=0.01, canary_identity=f"A{i}",
            )
            if r.in_canary:
                executed += 1
        # 1% de 500 = 5, ±10
        assert executed <= 25

    def test_in_canary_status_returned(self):
        r = run_with_shadow(
            ctx={}, legacy_callable=_legacy, shadow_callable=_shadow,
            canary_rate=0.0,
        )
        assert r.in_canary is False
        r2 = run_with_shadow(
            ctx={}, legacy_callable=_legacy, shadow_callable=_shadow,
            canary_rate=1.0,
        )
        assert r2.in_canary is True
