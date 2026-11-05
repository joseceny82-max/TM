"""tests/test_circuit_breakers.py — Test suite for STEP 8.

Categorías:
    1. CircuitBreaker dataclass         (4 tests)
    2. CircuitBreakerRegistry CLOSED    (4 tests)
    3. Threshold opens circuit          (4 tests)
    4. Cooldown half-open transition    (4 tests)
    5. HALF_OPEN behavior               (4 tests)
    6. Recovery success closes circuit  (3 tests)
    7. Per-resource isolation           (4 tests)
    8. Persistence                       (3 tests)
    9. BackoffPolicy                     (5 tests)
   10. RecoveryController                (4 tests)
   11. Thread safety                     (2 tests)

stdlib + pytest only.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.reliability import (
    DEFAULT_FAILURE_THRESHOLD,
    RESOURCE_ACCOUNT,
    RESOURCE_EVENT,
    RESOURCE_SUBNET,
    BackoffPolicy,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
    RecoveryController,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


BASE_TS = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)


# ════════════════════════════════════════════════════════════════════
#  1. CircuitBreaker dataclass
# ════════════════════════════════════════════════════════════════════
class TestCircuitBreakerModel:

    def test_default_state_closed(self):
        br = CircuitBreaker(resource_type="account", resource_id="A1")
        assert br.state == CircuitState.CLOSED

    def test_to_dict_has_canonical_keys(self):
        br = CircuitBreaker(resource_type="account", resource_id="A1")
        d = br.to_dict()
        assert set(d.keys()) == {
            "resource_type", "resource_id", "state",
            "failure_count", "success_count_half_open",
            "opened_at", "last_failure_at", "last_success_at",
        }

    def test_frozen(self):
        br = CircuitBreaker(resource_type="account", resource_id="A1")
        with pytest.raises((AttributeError, Exception)):
            br.state = CircuitState.OPEN

    def test_state_values(self):
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"


# ════════════════════════════════════════════════════════════════════
#  2. Registry CLOSED default
# ════════════════════════════════════════════════════════════════════
class TestRegistryClosed:

    def test_can_proceed_when_no_state(self):
        r = CircuitBreakerRegistry()
        assert r.can_proceed(RESOURCE_ACCOUNT, "A1") is True

    def test_get_creates_closed_breaker(self):
        r = CircuitBreakerRegistry()
        br = r.get(RESOURCE_ACCOUNT, "A1")
        assert br.state == CircuitState.CLOSED
        assert br.failure_count == 0

    def test_invalid_resource_type_returns_safe_default(self):
        r = CircuitBreakerRegistry()
        # Tipo inválido → fail-open
        assert r.can_proceed("unknown_type", "X") is True

    def test_single_success_no_change(self):
        r = CircuitBreakerRegistry()
        r.record_success(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        br = r.get(RESOURCE_ACCOUNT, "A1")
        assert br.state == CircuitState.CLOSED


# ════════════════════════════════════════════════════════════════════
#  3. Threshold opens circuit
# ════════════════════════════════════════════════════════════════════
class TestThresholdOpensCircuit:

    def test_single_failure_does_not_open(self):
        r = CircuitBreakerRegistry(threshold=3)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.CLOSED

    def test_threshold_failures_open_circuit(self):
        r = CircuitBreakerRegistry(threshold=3)
        for i in range(3):
            r.record_failure(RESOURCE_ACCOUNT, "A1",
                              now_iso=_iso(BASE_TS + timedelta(seconds=i)))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.OPEN

    def test_open_blocks_proceed(self):
        r = CircuitBreakerRegistry(threshold=2, cooldown_sec=600)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        r.record_failure(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=1)))
        # Pasaron < cooldown → debe seguir OPEN
        assert r.can_proceed(
            RESOURCE_ACCOUNT, "A1",
            now_iso=_iso(BASE_TS + timedelta(seconds=2)),
        ) is False

    def test_open_records_opened_at(self):
        r = CircuitBreakerRegistry(threshold=1)
        ts = _iso(BASE_TS)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=ts)
        br = r.get(RESOURCE_ACCOUNT, "A1")
        assert br.opened_at_iso == ts


# ════════════════════════════════════════════════════════════════════
#  4. Cooldown half-open transition
# ════════════════════════════════════════════════════════════════════
class TestCooldownTransition:

    def test_cooldown_transitions_to_half_open(self):
        r = CircuitBreakerRegistry(threshold=1, cooldown_sec=60.0)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        # 70s después
        after = _iso(BASE_TS + timedelta(seconds=70))
        assert r.can_proceed(RESOURCE_ACCOUNT, "A1", now_iso=after) is True
        br = r.get(RESOURCE_ACCOUNT, "A1")
        assert br.state == CircuitState.HALF_OPEN

    def test_cooldown_not_yet_expired_still_open(self):
        r = CircuitBreakerRegistry(threshold=1, cooldown_sec=60.0)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        # 30s después
        after = _iso(BASE_TS + timedelta(seconds=30))
        assert r.can_proceed(RESOURCE_ACCOUNT, "A1", now_iso=after) is False

    def test_cooldown_zero_immediately_half_open(self):
        r = CircuitBreakerRegistry(threshold=1, cooldown_sec=0.0)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        # Mismo instante
        after = _iso(BASE_TS + timedelta(seconds=1))
        assert r.can_proceed(RESOURCE_ACCOUNT, "A1", now_iso=after) is True

    def test_malformed_timestamp_keeps_open(self):
        r = CircuitBreakerRegistry(threshold=1, cooldown_sec=60.0)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        # ISO inválido en `now` → no se puede comparar → mantiene OPEN
        assert r.can_proceed(RESOURCE_ACCOUNT, "A1", now_iso="not-a-date") is False


# ════════════════════════════════════════════════════════════════════
#  5. HALF_OPEN behavior
# ════════════════════════════════════════════════════════════════════
class TestHalfOpenBehavior:

    def _open_and_advance(self, threshold=1, cooldown=60.0):
        r = CircuitBreakerRegistry(threshold=threshold, cooldown_sec=cooldown)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        # Cooldown pasa
        r.can_proceed(RESOURCE_ACCOUNT, "A1",
                       now_iso=_iso(BASE_TS + timedelta(seconds=cooldown + 1)))
        return r

    def test_half_open_failure_immediately_reopens(self):
        r = self._open_and_advance()
        r.record_failure(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=120)))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.OPEN

    def test_half_open_failure_resets_cooldown(self):
        r = self._open_and_advance()
        new_open_ts = _iso(BASE_TS + timedelta(seconds=120))
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=new_open_ts)
        br = r.get(RESOURCE_ACCOUNT, "A1")
        # opened_at_iso debe ser el nuevo timestamp
        assert br.opened_at_iso == new_open_ts

    def test_half_open_success_closes(self):
        r = self._open_and_advance()
        r.record_success(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=120)))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.CLOSED

    def test_half_open_with_multiple_success_target(self):
        r = CircuitBreakerRegistry(threshold=1, cooldown_sec=60.0,
                                     half_open_success_target=3)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        r.can_proceed(RESOURCE_ACCOUNT, "A1",
                       now_iso=_iso(BASE_TS + timedelta(seconds=70)))
        # 1 success — sigue HALF_OPEN
        r.record_success(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=75)))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.HALF_OPEN
        # 3 successes — debe cerrarse
        r.record_success(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=80)))
        r.record_success(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=85)))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.CLOSED


# ════════════════════════════════════════════════════════════════════
#  6. Recovery success closes circuit
# ════════════════════════════════════════════════════════════════════
class TestRecoverySuccessCloses:

    def test_closed_success_resets_failure_count(self):
        r = CircuitBreakerRegistry(threshold=3)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        r.record_failure(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=1)))
        # 2 fails. Ahora un success → reset
        r.record_success(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=2)))
        br = r.get(RESOURCE_ACCOUNT, "A1")
        assert br.failure_count == 0
        assert br.state == CircuitState.CLOSED

    def test_reset_explicit(self):
        r = CircuitBreakerRegistry(threshold=1, cooldown_sec=600)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.OPEN
        r.reset(RESOURCE_ACCOUNT, "A1")
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.CLOSED

    def test_full_cycle_closed_open_half_open_closed(self):
        r = CircuitBreakerRegistry(threshold=2, cooldown_sec=30.0)
        # CLOSED → 2 fails → OPEN
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        r.record_failure(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=1)))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.OPEN
        # Cooldown → HALF_OPEN
        r.can_proceed(RESOURCE_ACCOUNT, "A1",
                       now_iso=_iso(BASE_TS + timedelta(seconds=35)))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.HALF_OPEN
        # Success → CLOSED
        r.record_success(RESOURCE_ACCOUNT, "A1",
                          now_iso=_iso(BASE_TS + timedelta(seconds=40)))
        assert r.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.CLOSED


# ════════════════════════════════════════════════════════════════════
#  7. Per-resource isolation
# ════════════════════════════════════════════════════════════════════
class TestPerResourceIsolation:

    def test_account_a_does_not_affect_account_b(self):
        r = CircuitBreakerRegistry(threshold=1)
        r.record_failure(RESOURCE_ACCOUNT, "A", now_iso=_iso(BASE_TS))
        assert r.can_proceed(RESOURCE_ACCOUNT, "A",
                              now_iso=_iso(BASE_TS)) is False
        assert r.can_proceed(RESOURCE_ACCOUNT, "B",
                              now_iso=_iso(BASE_TS)) is True

    def test_account_does_not_affect_subnet_same_id(self):
        r = CircuitBreakerRegistry(threshold=1)
        r.record_failure(RESOURCE_ACCOUNT, "X", now_iso=_iso(BASE_TS))
        assert r.can_proceed(RESOURCE_SUBNET, "X",
                              now_iso=_iso(BASE_TS)) is True

    def test_each_resource_type_independent(self):
        r = CircuitBreakerRegistry(threshold=1, cooldown_sec=600)
        r.record_failure(RESOURCE_ACCOUNT, "X", now_iso=_iso(BASE_TS))
        r.record_failure(RESOURCE_SUBNET, "X",
                          now_iso=_iso(BASE_TS + timedelta(seconds=1)))
        r.record_failure(RESOURCE_EVENT, "X",
                          now_iso=_iso(BASE_TS + timedelta(seconds=2)))
        # Los 3 están OPEN, pero pertenecen a tipos distintos
        for rtype in (RESOURCE_ACCOUNT, RESOURCE_SUBNET, RESOURCE_EVENT):
            assert r.get(rtype, "X").state == CircuitState.OPEN

    def test_list_open_includes_all(self):
        r = CircuitBreakerRegistry(threshold=1, cooldown_sec=600)
        r.record_failure(RESOURCE_ACCOUNT, "A", now_iso=_iso(BASE_TS))
        r.record_failure(RESOURCE_SUBNET, "B", now_iso=_iso(BASE_TS))
        opens = r.list_open()
        assert len(opens) == 2


# ════════════════════════════════════════════════════════════════════
#  8. Persistence
# ════════════════════════════════════════════════════════════════════
class TestPersistence:

    def test_state_survives_reload(self, tmp_path):
        p = str(tmp_path / "breakers.json")
        r1 = CircuitBreakerRegistry(threshold=1, persistence_path=p)
        r1.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        # Nueva instancia carga desde disco
        r2 = CircuitBreakerRegistry(threshold=1, persistence_path=p)
        assert r2.get(RESOURCE_ACCOUNT, "A1").state == CircuitState.OPEN

    def test_no_persistence_when_path_none(self):
        r = CircuitBreakerRegistry(threshold=1, persistence_path=None)
        r.record_failure(RESOURCE_ACCOUNT, "A1", now_iso=_iso(BASE_TS))
        # No raise (best-effort)

    def test_corrupted_persistence_file_safe(self, tmp_path):
        p = tmp_path / "breakers.json"
        p.write_text("{ not valid json", encoding="utf-8")
        r = CircuitBreakerRegistry(threshold=1, persistence_path=str(p))
        # No raise, comportamiento normal
        assert r.can_proceed(RESOURCE_ACCOUNT, "A1") is True


# ════════════════════════════════════════════════════════════════════
#  9. BackoffPolicy
# ════════════════════════════════════════════════════════════════════
class TestBackoffPolicy:

    def test_first_attempt_returns_base(self):
        bp = BackoffPolicy(base_sec=1.0, multiplier=2.0)
        assert bp.compute(0) == 1.0

    def test_exponential_growth(self):
        bp = BackoffPolicy(base_sec=1.0, multiplier=2.0)
        assert bp.compute(1) == 2.0
        assert bp.compute(2) == 4.0
        assert bp.compute(3) == 8.0

    def test_capped_at_max(self):
        bp = BackoffPolicy(base_sec=1.0, multiplier=2.0, max_sec=10.0)
        assert bp.compute(20) == 10.0

    def test_jitter_deterministic_per_seed(self):
        bp = BackoffPolicy(base_sec=10.0, multiplier=2.0,
                            jitter_fraction=0.3)
        a = bp.compute(2, seed="A1")
        b = bp.compute(2, seed="A1")
        assert a == b   # determinístico

    def test_jitter_differs_per_seed(self):
        bp = BackoffPolicy(base_sec=10.0, multiplier=2.0,
                            jitter_fraction=0.3)
        a = bp.compute(2, seed="A1")
        b = bp.compute(2, seed="B2")
        # Diferentes seeds → muy probablemente diferente jitter
        assert a != b


# ════════════════════════════════════════════════════════════════════
#  10. RecoveryController
# ════════════════════════════════════════════════════════════════════
class TestRecoveryController:

    def test_allows_when_all_closed(self):
        registry = CircuitBreakerRegistry()
        rc = RecoveryController(registry=registry)
        ok, reason = rc.should_allow_request(
            account_id="A1", subnet="10.0.0.0", event_id="E1",
        )
        assert ok is True
        assert reason == "ok"

    def test_blocks_when_account_open(self):
        registry = CircuitBreakerRegistry(threshold=1, cooldown_sec=600)
        registry.record_failure(RESOURCE_ACCOUNT, "A1",
                                 now_iso=_iso(BASE_TS))
        rc = RecoveryController(registry=registry)
        ok, reason = rc.should_allow_request(
            account_id="A1",
            now_iso=_iso(BASE_TS + timedelta(seconds=1)),
        )
        assert ok is False
        assert "account_open" in reason

    def test_record_outcome_failure_opens_all_provided(self):
        registry = CircuitBreakerRegistry(threshold=1, cooldown_sec=600)
        rc = RecoveryController(registry=registry)
        rc.record_outcome(
            success=False,
            account_id="A1", subnet="S1", event_id="E1",
            now_iso=_iso(BASE_TS),
        )
        for rtype, rid in (
            (RESOURCE_ACCOUNT, "A1"),
            (RESOURCE_SUBNET, "S1"),
            (RESOURCE_EVENT, "E1"),
        ):
            assert registry.get(rtype, rid).state == CircuitState.OPEN

    def test_compute_retry_wait_delegates(self):
        registry = CircuitBreakerRegistry()
        bp = BackoffPolicy(base_sec=2.0, multiplier=3.0)
        rc = RecoveryController(registry=registry, backoff=bp)
        assert rc.compute_retry_wait(2) == 18.0


# ════════════════════════════════════════════════════════════════════
#  11. Thread safety
# ════════════════════════════════════════════════════════════════════
class TestThreadSafety:

    def test_concurrent_failures_count_correctly(self):
        r = CircuitBreakerRegistry(threshold=1000)
        N = 100

        def _spam():
            for _ in range(10):
                r.record_failure(RESOURCE_ACCOUNT, "A1")

        threads = [threading.Thread(target=_spam) for _ in range(N // 10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 10 threads × 10 = 100 failures
        assert r.get(RESOURCE_ACCOUNT, "A1").failure_count == 100

    def test_concurrent_can_proceed_does_not_crash(self):
        r = CircuitBreakerRegistry(threshold=5)
        r.record_failure(RESOURCE_ACCOUNT, "A1")

        def _check():
            for _ in range(50):
                r.can_proceed(RESOURCE_ACCOUNT, "A1")

        threads = [threading.Thread(target=_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No crash = OK
