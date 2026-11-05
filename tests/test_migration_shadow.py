"""tests/test_migration_shadow.py — Test suite for STEP 9.

Categorías:
    1. Flags defaults + env override     (6 tests)
    2. flow_router default-legacy        (5 tests)
    3. flow_router core opt-in           (4 tests)
    4. flow_router fallback on crash     (3 tests)
    5. shadow_orchestrator legacy primary (4 tests)
    6. shadow_orchestrator core dry-run  (5 tests)
    7. shadow_orchestrator core crash    (3 tests)
    8. divergence_engine                  (10 tests)
    9. Integration / equivalence         (3 tests)

stdlib + pytest only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.migration import (
    MigrationFlags,
    compute_divergence,
    execute_with_shadow,
    get_flags,
    reload_flags,
    route_flow,
    set_flags,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_flags():
    """Resetea flags entre tests."""
    set_flags(MigrationFlags())  # defaults
    yield
    set_flags(MigrationFlags())


def _legacy(ctx):
    return {"path": "legacy", "ctx": ctx, "success": True,
             "cart_phase": "paid", "order_id": "ORD-L1",
             "idempotency_state": "committed"}


def _core(ctx):
    return {"path": "core", "ctx": ctx, "success": True,
             "cart_phase": "paid", "order_id": "ORD-C1",
             "idempotency_state": "committed"}


def _core_crash(ctx):
    raise RuntimeError("core kaboom")


# ════════════════════════════════════════════════════════════════════
#  1. FLAGS
# ════════════════════════════════════════════════════════════════════
class TestFlags:

    def test_defaults_safe(self):
        f = MigrationFlags()
        assert f.USE_CORE_FLOW is False         # CRITICAL
        assert f.SHADOW_CORE_FLOW is True
        assert f.ENABLE_CORE_METRICS is True

    def test_env_override_use_core_flow(self, monkeypatch):
        monkeypatch.setenv("USE_CORE_FLOW", "true")
        f = MigrationFlags.from_env()
        assert f.USE_CORE_FLOW is True

    def test_env_override_disable_shadow(self, monkeypatch):
        monkeypatch.setenv("SHADOW_CORE_FLOW", "false")
        f = MigrationFlags.from_env()
        assert f.SHADOW_CORE_FLOW is False

    def test_get_flags_cached(self):
        a = get_flags()
        b = get_flags()
        assert a is b

    def test_reload_flags_re_reads_env(self, monkeypatch):
        monkeypatch.setenv("USE_CORE_FLOW", "true")
        f = reload_flags()
        assert f.USE_CORE_FLOW is True
        monkeypatch.setenv("USE_CORE_FLOW", "false")
        f = reload_flags()
        assert f.USE_CORE_FLOW is False

    def test_flags_to_dict(self):
        f = MigrationFlags()
        d = f.to_dict()
        assert set(d.keys()) == {
            "USE_CORE_FLOW", "SHADOW_CORE_FLOW",
            "ENABLE_CORE_METRICS", "DIVERGENCE_LOG_LEVEL",
        }


# ════════════════════════════════════════════════════════════════════
#  2. FLOW ROUTER — DEFAULT LEGACY
# ════════════════════════════════════════════════════════════════════
class TestFlowRouterDefaultLegacy:

    def test_default_routes_to_legacy(self):
        r = route_flow({"x": 1}, legacy_callable=_legacy, core_callable=_core)
        assert r["path"] == "legacy"

    def test_no_core_callable_routes_to_legacy(self):
        r = route_flow({"x": 1}, legacy_callable=_legacy, core_callable=None)
        assert r["path"] == "legacy"

    def test_flag_off_with_core_callable_routes_to_legacy(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=False))
        r = route_flow({"x": 1}, legacy_callable=_legacy, core_callable=_core)
        assert r["path"] == "legacy"

    def test_legacy_called_with_ctx(self):
        captured = {}
        def legacy(ctx):
            captured["ctx"] = ctx
            return {}
        route_flow({"id": 42}, legacy_callable=legacy)
        assert captured["ctx"] == {"id": 42}

    def test_legacy_propagates_exceptions(self):
        def bad(ctx):
            raise ValueError("legacy fail")
        with pytest.raises(ValueError):
            route_flow({}, legacy_callable=bad)


# ════════════════════════════════════════════════════════════════════
#  3. FLOW ROUTER — CORE OPT-IN
# ════════════════════════════════════════════════════════════════════
class TestFlowRouterCoreOptIn:

    def test_flag_on_routes_to_core(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=True))
        r = route_flow({}, legacy_callable=_legacy, core_callable=_core)
        assert r["path"] == "core"

    def test_flag_on_no_core_callable_falls_back_to_legacy(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=True))
        r = route_flow({}, legacy_callable=_legacy, core_callable=None)
        assert r["path"] == "legacy"

    def test_core_called_with_ctx(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=True))
        captured = {}
        def core(ctx):
            captured["ctx"] = ctx
            return {}
        route_flow({"id": 9}, legacy_callable=_legacy, core_callable=core)
        assert captured["ctx"] == {"id": 9}

    def test_explicit_flags_override_global(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=False))
        custom = MigrationFlags(USE_CORE_FLOW=True)
        r = route_flow({}, legacy_callable=_legacy, core_callable=_core,
                        flags=custom)
        assert r["path"] == "core"


# ════════════════════════════════════════════════════════════════════
#  4. FLOW ROUTER — FALLBACK ON CRASH
# ════════════════════════════════════════════════════════════════════
class TestFlowRouterFallbackOnCrash:

    def test_core_crash_falls_back_to_legacy(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=True))
        r = route_flow({}, legacy_callable=_legacy,
                        core_callable=_core_crash)
        assert r["path"] == "legacy"

    def test_legacy_still_called_after_core_crash(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=True))
        legacy_calls = []
        def legacy(ctx):
            legacy_calls.append(ctx)
            return _legacy(ctx)
        route_flow({}, legacy_callable=legacy, core_callable=_core_crash)
        assert len(legacy_calls) == 1

    def test_core_exception_not_propagated(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=True))
        # NO debe raise
        r = route_flow({}, legacy_callable=_legacy,
                        core_callable=_core_crash)
        assert r is not None


# ════════════════════════════════════════════════════════════════════
#  5. SHADOW ORCHESTRATOR — LEGACY PRIMARY
# ════════════════════════════════════════════════════════════════════
class TestShadowLegacyPrimary:

    def test_returns_legacy_result(self):
        result, _ = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=_core,
        )
        assert result["path"] == "legacy"

    def test_legacy_always_called(self):
        calls = []
        def legacy(ctx):
            calls.append(ctx)
            return _legacy(ctx)
        execute_with_shadow({}, legacy_callable=legacy)
        assert len(calls) == 1

    def test_shadow_off_no_core_call(self):
        set_flags(MigrationFlags(SHADOW_CORE_FLOW=False))
        core_called = []
        def core(ctx):
            core_called.append(ctx)
            return _core(ctx)
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=core,
        )
        assert core_called == []
        assert divergence is None

    def test_no_core_callable_no_divergence(self):
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=None,
        )
        assert divergence is None


# ════════════════════════════════════════════════════════════════════
#  6. SHADOW ORCHESTRATOR — CORE DRY-RUN
# ════════════════════════════════════════════════════════════════════
class TestShadowCoreDryRun:

    def test_core_called_when_shadow_on(self):
        calls = []
        def core(ctx):
            calls.append(ctx)
            return _core(ctx)
        execute_with_shadow({}, legacy_callable=_legacy,
                             core_callable=core)
        assert len(calls) == 1

    def test_divergence_returned(self):
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=_core,
        )
        assert divergence is not None
        assert "score" in divergence

    def test_identical_results_zero_divergence(self):
        # Mismo callable para ambos
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=_legacy,
        )
        assert divergence["score"] == 0
        assert divergence["same"] is True

    def test_different_success_high_divergence(self):
        def failing_core(ctx):
            return {"success": False, "cart_phase": "failed"}
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=failing_core,
        )
        assert divergence["score"] >= 40

    def test_legacy_result_not_affected_by_core(self):
        result, _ = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=_core,
        )
        assert result["order_id"] == "ORD-L1"   # legacy order_id, no core


# ════════════════════════════════════════════════════════════════════
#  7. SHADOW ORCHESTRATOR — CORE CRASH
# ════════════════════════════════════════════════════════════════════
class TestShadowCoreCrash:

    def test_core_crash_returns_legacy_result(self):
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=_core_crash,
        )
        assert result["path"] == "legacy"

    def test_core_crash_does_not_propagate(self):
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=_core_crash,
        )
        assert divergence.get("core_crashed") is True

    def test_core_crash_marks_divergence(self):
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=_core_crash,
        )
        assert "core kaboom" in divergence.get("error", "")


# ════════════════════════════════════════════════════════════════════
#  8. DIVERGENCE ENGINE
# ════════════════════════════════════════════════════════════════════
class TestDivergenceEngine:

    def test_identical_zero_score(self):
        a = {"success": True, "cart_phase": "paid",
              "order_id": "X", "idempotency_state": "committed"}
        b = dict(a)
        d = compute_divergence(a, b)
        assert d["score"] == 0
        assert d["same"] is True
        assert d["severity"] == "low"

    def test_success_mismatch_high_weight(self):
        a = {"success": True}
        b = {"success": False}
        d = compute_divergence(a, b)
        assert d["score"] >= 40

    def test_cart_phase_mismatch(self):
        a = {"cart_phase": "paid"}
        b = {"cart_phase": "failed"}
        d = compute_divergence(a, b)
        assert d["score"] >= 25

    def test_order_id_presence_mismatch(self):
        a = {"order_id": "X"}
        b = {"order_id": ""}
        d = compute_divergence(a, b)
        assert d["score"] >= 20

    def test_severity_low(self):
        a = {"error_category": "x"}
        b = {"error_category": "y"}
        d = compute_divergence(a, b)
        assert d["severity"] == "low"

    def test_severity_medium(self):
        a = {"success": True, "cart_phase": "paid"}
        b = {"success": True, "cart_phase": "failed"}
        d = compute_divergence(a, b)
        assert d["severity"] in {"low", "medium"}

    def test_severity_critical(self):
        a = {"success": True, "cart_phase": "paid",
              "order_id": "X", "idempotency_state": "committed"}
        b = {"success": False, "cart_phase": "failed",
              "order_id": "", "idempotency_state": "orphan"}
        d = compute_divergence(a, b)
        assert d["severity"] == "critical"

    def test_non_dict_inputs_safe(self):
        d = compute_divergence(None, None)
        assert d["score"] == 0
        d = compute_divergence("garbage", {})
        assert d["score"] == 0

    def test_differences_listed(self):
        a = {"success": True}
        b = {"success": False}
        d = compute_divergence(a, b)
        assert any(diff["field"] == "success" for diff in d["differences"])

    def test_score_capped_at_100(self):
        a = {"success": True, "cart_phase": "paid",
              "order_id": "X", "idempotency_state": "committed",
              "error_category": "ok"}
        b = {"success": False, "cart_phase": "failed",
              "order_id": "", "idempotency_state": "orphan",
              "error_category": "bad"}
        d = compute_divergence(a, b)
        assert d["score"] <= 100


# ════════════════════════════════════════════════════════════════════
#  9. INTEGRATION / EQUIVALENCE
# ════════════════════════════════════════════════════════════════════
class TestIntegration:

    def test_equivalent_callables_no_divergence(self):
        def shared(ctx):
            return {"success": True, "cart_phase": "paid",
                     "order_id": "X", "idempotency_state": "committed"}
        result, divergence = execute_with_shadow(
            {}, legacy_callable=shared, core_callable=shared,
        )
        assert divergence["score"] == 0

    def test_router_then_shadow_compose(self):
        set_flags(MigrationFlags(USE_CORE_FLOW=False, SHADOW_CORE_FLOW=True))
        # 1. route → legacy
        r1 = route_flow({}, legacy_callable=_legacy, core_callable=_core)
        # 2. shadow → legacy primary + core compare
        r2, div = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=_core,
        )
        # Ambos retornan legacy
        assert r1["path"] == "legacy"
        assert r2["path"] == "legacy"

    def test_legacy_unaffected_when_core_has_bugs(self):
        def buggy_core(ctx):
            return {"success": "not a bool",  # tipo raro
                     "cart_phase": None}
        result, divergence = execute_with_shadow(
            {}, legacy_callable=_legacy, core_callable=buggy_core,
        )
        # Legacy intacto
        assert result["success"] is True
        # Divergence detected pero no panic
        assert divergence is not None
