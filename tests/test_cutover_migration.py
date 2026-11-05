"""tests/test_cutover_migration.py — Test suite for STEP 10.

Categorías:
    1. CanaryController routing         (8 tests)
    2. CutoverState transitions          (10 tests)
    3. Rollback paths                    (4 tests)
    4. CutoverConfig per state           (4 tests)
    5. ValidationGates                   (10 tests)
    6. execute_with_fallback             (8 tests)
    7. End-to-end migration scenarios    (4 tests)

stdlib + pytest only.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.migration import (
    CanaryController,
    CutoverConfig,
    CutoverState,
    FallbackResult,
    GateConfig,
    GateResult,
    IllegalCutoverTransition,
    can_progress,
    config_for,
    emergency_rollback,
    execute_with_fallback,
    is_legal_transition,
    legal_transitions,
    rollback_state,
    transition,
)


# ════════════════════════════════════════════════════════════════════
#  1. CANARY ROUTING
# ════════════════════════════════════════════════════════════════════
class TestCanaryRouting:

    def test_zero_pct_never_routes_to_core(self):
        c = CanaryController(percentage=0.0)
        for i in range(50):
            assert c.should_route_to_core(account_id=f"A{i}") is False

    def test_hundred_pct_always_routes_to_core(self):
        c = CanaryController(percentage=100.0)
        for i in range(50):
            assert c.should_route_to_core(account_id=f"A{i}") is True

    def test_deterministic_routing(self):
        c = CanaryController(percentage=50.0)
        a = c.should_route_to_core(account_id="A1")
        b = c.should_route_to_core(account_id="A1")
        assert a == b

    def test_sticky_session_across_calls(self):
        c = CanaryController(percentage=50.0)
        # Misma identidad, mismas dimensiones → mismo bucket
        for _ in range(20):
            r = c.should_route_to_core(account_id="STICKY", subnet="10.0.0.0")
            r2 = c.should_route_to_core(account_id="STICKY", subnet="10.0.0.0")
            assert r == r2

    def test_different_identities_independent(self):
        c = CanaryController(percentage=50.0)
        # Aproximadamente 50% deberían rutarse a core
        results = [c.should_route_to_core(account_id=f"A{i}")
                    for i in range(1000)]
        true_count = sum(results)
        # Tolerancia ±10% sobre 50%
        assert 400 <= true_count <= 600

    def test_increasing_pct_only_adds_routes(self):
        """Si A está en canary al 10%, también lo está al 50% (monotonía)."""
        c10 = CanaryController(percentage=10.0)
        c50 = CanaryController(percentage=50.0)
        in_10 = []
        for i in range(200):
            aid = f"A{i}"
            if c10.should_route_to_core(account_id=aid):
                in_10.append(aid)
                # Si está en 10%, debe estar también en 50%
                assert c50.should_route_to_core(account_id=aid) is True

    def test_with_percentage_factory(self):
        c = CanaryController(percentage=10.0)
        c2 = c.with_percentage(50.0)
        assert c2.percentage == 50.0
        assert c.percentage == 10.0  # original intact

    def test_anonymous_routing_stable(self):
        c = CanaryController(percentage=30.0)
        a = c.should_route_to_core()
        b = c.should_route_to_core()
        assert a == b  # determinístico aún sin identidad


# ════════════════════════════════════════════════════════════════════
#  2. CUTOVER STATE TRANSITIONS
# ════════════════════════════════════════════════════════════════════
class TestCutoverTransitions:

    def test_legacy_to_shadow_legal(self):
        assert is_legal_transition(CutoverState.LEGACY_ONLY,
                                      CutoverState.SHADOW)

    def test_shadow_to_canary_1_legal(self):
        assert is_legal_transition(CutoverState.SHADOW,
                                      CutoverState.CANARY_1)

    def test_canary_1_to_10_legal(self):
        assert is_legal_transition(CutoverState.CANARY_1,
                                      CutoverState.CANARY_10)

    def test_canary_10_to_50_legal(self):
        assert is_legal_transition(CutoverState.CANARY_10,
                                      CutoverState.CANARY_50)

    def test_canary_50_to_100_legal(self):
        assert is_legal_transition(CutoverState.CANARY_50,
                                      CutoverState.CANARY_100)

    def test_canary_100_to_full_core_legal(self):
        assert is_legal_transition(CutoverState.CANARY_100,
                                      CutoverState.FULL_CORE)

    def test_legacy_to_canary_50_illegal_skip(self):
        # No se puede saltar escalones
        assert is_legal_transition(CutoverState.LEGACY_ONLY,
                                      CutoverState.CANARY_50) is False

    def test_full_core_to_canary_50_illegal_skip(self):
        # Rollback debe ser un step a la vez
        assert is_legal_transition(CutoverState.FULL_CORE,
                                      CutoverState.CANARY_50) is False

    def test_transition_raises_for_illegal(self):
        with pytest.raises(IllegalCutoverTransition):
            transition(CutoverState.LEGACY_ONLY, CutoverState.FULL_CORE)

    def test_idempotent_transition_allowed(self):
        # Same state → same state: no-op legal
        assert is_legal_transition(CutoverState.SHADOW,
                                      CutoverState.SHADOW)


# ════════════════════════════════════════════════════════════════════
#  3. ROLLBACK PATHS
# ════════════════════════════════════════════════════════════════════
class TestRollbackPaths:

    def test_any_state_can_rollback_to_legacy(self):
        for state in CutoverState:
            if state == CutoverState.LEGACY_ONLY:
                continue
            assert is_legal_transition(state, CutoverState.LEGACY_ONLY)

    def test_canary_can_rollback_one_step(self):
        assert is_legal_transition(CutoverState.CANARY_10,
                                      CutoverState.CANARY_1)
        assert is_legal_transition(CutoverState.CANARY_50,
                                      CutoverState.CANARY_10)

    def test_rollback_state_returns_previous(self):
        assert rollback_state(CutoverState.CANARY_50) == CutoverState.CANARY_10
        assert rollback_state(CutoverState.SHADOW) == CutoverState.LEGACY_ONLY
        assert rollback_state(CutoverState.LEGACY_ONLY) == CutoverState.LEGACY_ONLY

    def test_emergency_rollback_returns_legacy(self):
        assert emergency_rollback() == CutoverState.LEGACY_ONLY


# ════════════════════════════════════════════════════════════════════
#  4. CUTOVER CONFIG
# ════════════════════════════════════════════════════════════════════
class TestCutoverConfig:

    def test_legacy_only_disables_everything(self):
        c = config_for(CutoverState.LEGACY_ONLY)
        assert c.use_core_flow is False
        assert c.shadow_core_flow is False
        assert c.canary_percentage == 0.0

    def test_shadow_enables_only_shadow(self):
        c = config_for(CutoverState.SHADOW)
        assert c.use_core_flow is False
        assert c.shadow_core_flow is True
        assert c.canary_percentage == 0.0

    def test_canary_50_uses_50_pct(self):
        c = config_for(CutoverState.CANARY_50)
        assert c.canary_percentage == 50.0

    def test_full_core_uses_core(self):
        c = config_for(CutoverState.FULL_CORE)
        assert c.use_core_flow is True


# ════════════════════════════════════════════════════════════════════
#  5. VALIDATION GATES
# ════════════════════════════════════════════════════════════════════
class TestValidationGates:

    def test_empty_metrics_pass(self):
        r = can_progress(current_metrics={}, prior_metrics={})
        assert r.passed is True

    def test_success_rate_stable_passes(self):
        cur = {"success_rate": 0.85, "idempotency_collision_rate": 0.0}
        pri = {"success_rate": 0.85}
        r = can_progress(current_metrics=cur, prior_metrics=pri)
        assert r.passed is True

    def test_success_rate_drop_fails(self):
        cur = {"success_rate": 0.50}
        pri = {"success_rate": 0.85}
        r = can_progress(current_metrics=cur, prior_metrics=pri)
        assert r.passed is False
        assert "success_rate_unstable" in r.failed_gates

    def test_small_success_delta_within_tolerance(self):
        cur = {"success_rate": 0.852}
        pri = {"success_rate": 0.850}
        r = can_progress(current_metrics=cur, prior_metrics=pri)
        # 0.002 delta < default 0.005
        assert "success_rate_unstable" not in r.failed_gates

    def test_idempotency_collision_blocks(self):
        cur = {"idempotency_collision_rate": 0.1}
        r = can_progress(current_metrics=cur, prior_metrics={})
        assert r.passed is False
        assert "idempotency_collisions" in r.failed_gates

    def test_divergence_spike_blocks(self):
        cur = {"avg_divergence_score": 80}
        r = can_progress(current_metrics=cur, prior_metrics={})
        assert "divergence_spike" in r.failed_gates

    def test_payment_failure_increase_blocks(self):
        cur = {"payment_failure_rate": 0.10}
        pri = {"payment_failure_rate": 0.02}
        r = can_progress(current_metrics=cur, prior_metrics=pri)
        # 0.08 > default 0.02
        assert "payment_failure_increase" in r.failed_gates

    def test_gate_result_to_dict(self):
        r = can_progress(current_metrics={}, prior_metrics={})
        d = r.to_dict()
        assert set(d.keys()) == {"passed", "failed_gates", "details"}

    def test_custom_gate_config(self):
        cur = {"success_rate": 0.70}
        pri = {"success_rate": 0.85}
        cfg = GateConfig(success_rate_delta=0.20)
        r = can_progress(current_metrics=cur, prior_metrics=pri, config=cfg)
        # 0.15 < 0.20 → passes
        assert "success_rate_unstable" not in r.failed_gates

    def test_multiple_gates_fail(self):
        cur = {"success_rate": 0.50, "idempotency_collision_rate": 0.1,
                "avg_divergence_score": 90, "payment_failure_rate": 0.5}
        pri = {"success_rate": 0.85, "payment_failure_rate": 0.02}
        r = can_progress(current_metrics=cur, prior_metrics=pri)
        assert len(r.failed_gates) >= 3


# ════════════════════════════════════════════════════════════════════
#  6. execute_with_fallback
# ════════════════════════════════════════════════════════════════════
class TestExecuteWithFallback:

    def test_core_success_returns_core_result(self):
        r = execute_with_fallback(
            {}, core_callable=lambda c: {"path": "core_ok"},
            legacy_callable=lambda c: {"path": "legacy"},
        )
        assert r.path == "core"
        assert r.result["path"] == "core_ok"

    def test_core_exception_falls_back_to_legacy(self):
        def core_crash(c):
            raise ConnectionError("network broke")
        r = execute_with_fallback(
            {}, core_callable=core_crash,
            legacy_callable=lambda c: {"path": "legacy"},
        )
        assert r.path == "legacy_fallback"
        assert "ConnectionError" in r.fallback_reason

    def test_legacy_called_only_on_fallback(self):
        legacy_calls = []
        def legacy(c):
            legacy_calls.append(c)
            return {}
        # Core OK → legacy NO se llama
        execute_with_fallback(
            {}, core_callable=lambda c: {"ok": True},
            legacy_callable=legacy,
        )
        assert legacy_calls == []

    def test_legacy_called_when_core_crashes(self):
        legacy_calls = []
        def legacy(c):
            legacy_calls.append(c)
            return {"ok": True}
        execute_with_fallback(
            {}, core_callable=lambda c: 1/0,   # ZeroDivisionError
            legacy_callable=legacy,
        )
        assert len(legacy_calls) == 1

    def test_timeout_triggers_fallback(self):
        def slow_core(c):
            time.sleep(2.0)
            return {"ok": True}
        r = execute_with_fallback(
            {}, core_callable=slow_core,
            legacy_callable=lambda c: {"path": "legacy"},
            timeout_sec=0.1,
        )
        assert r.path == "legacy_fallback"
        assert "timeout" in r.fallback_reason

    def test_divergence_check_falls_back_when_high(self):
        def core(c):
            return {"success": False, "cart_phase": "failed",
                     "order_id": "", "idempotency_state": "orphan"}
        def legacy(c):
            return {"success": True, "cart_phase": "paid",
                     "order_id": "X", "idempotency_state": "committed"}
        r = execute_with_fallback(
            {}, core_callable=core, legacy_callable=legacy,
            check_divergence_with_legacy=True,
            divergence_threshold=50,
        )
        assert r.path == "legacy_fallback"

    def test_divergence_check_keeps_core_when_low(self):
        def shared(c):
            return {"success": True, "cart_phase": "paid"}
        r = execute_with_fallback(
            {}, core_callable=shared, legacy_callable=shared,
            check_divergence_with_legacy=True,
            divergence_threshold=50,
        )
        assert r.path == "core"

    def test_legacy_crash_with_divergence_check_returns_core(self):
        def legacy_crash(c):
            raise RuntimeError("legacy down")
        r = execute_with_fallback(
            {}, core_callable=lambda c: {"ok": True},
            legacy_callable=legacy_crash,
            check_divergence_with_legacy=True,
        )
        assert r.path == "core"


# ════════════════════════════════════════════════════════════════════
#  7. END-TO-END MIGRATION SCENARIOS
# ════════════════════════════════════════════════════════════════════
class TestE2EMigration:

    def test_full_progression_legacy_to_full_core(self):
        """Avanza paso a paso por toda la cadena."""
        current = CutoverState.LEGACY_ONLY
        chain = [
            CutoverState.SHADOW,
            CutoverState.CANARY_1,
            CutoverState.CANARY_10,
            CutoverState.CANARY_50,
            CutoverState.CANARY_100,
            CutoverState.FULL_CORE,
        ]
        for next_state in chain:
            cfg = transition(current, next_state)
            assert cfg.state == next_state
            current = next_state

    def test_emergency_rollback_from_any_state(self):
        for state in CutoverState:
            cfg = transition(state, emergency_rollback())
            assert cfg.state == CutoverState.LEGACY_ONLY

    def test_canary_progression_with_gates(self):
        """Simulate: SHADOW → CANARY_1 con gates verdes."""
        # Estado actual: SHADOW. Métricas estables.
        cur = {"success_rate": 0.92, "idempotency_collision_rate": 0.0,
                "avg_divergence_score": 5, "payment_failure_rate": 0.05}
        pri = {"success_rate": 0.92, "payment_failure_rate": 0.05}
        gate = can_progress(current_metrics=cur, prior_metrics=pri)
        assert gate.passed is True
        # Promoción permitida
        cfg = transition(CutoverState.SHADOW, CutoverState.CANARY_1)
        assert cfg.canary_percentage == 1.0

    def test_canary_blocked_when_gates_fail(self):
        # Métricas deterioradas
        cur = {"success_rate": 0.40, "idempotency_collision_rate": 0.0}
        pri = {"success_rate": 0.92}
        gate = can_progress(current_metrics=cur, prior_metrics=pri)
        # Gates falla → caller decide rollback
        assert gate.passed is False
        # En la práctica, el flow controller invocaría rollback_state()
        rb = rollback_state(CutoverState.CANARY_10)
        assert rb == CutoverState.CANARY_1
