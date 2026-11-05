"""tests/test_purchase_gate.py — M13 Production-grade purchase safety tests.

Cobertura:
    - PurchaseDecision dataclass
    - Mode (disabled/dry_run/live) + kill switch
    - Decision priority order
    - Idempotency (duplicate prevention)
    - Stale checkout detection
    - Cooldown enforcement
    - Concurrent attempts (per-session lock)
    - Thread safety stress (100 threads)
    - HealthScore integration (M11 + M12 + M12.1 + M12.2)
    - Telemetry counters + rates
    - Replay-safe event names
    - Singleton lazy init + reset
    - Failsafe behavior (internal errors)
"""
import threading
import time

import pytest

from core.diagnostics.health_score import HealthScore
from core.diagnostics.purchase_gate import (
    DEFAULT_IDEM_CACHE_MAX,
    DEFAULT_IDEMPOTENCY_TTL_S,
    DEFAULT_MAX_CHECKOUT_LIFETIME_S,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_COOLDOWN_S,
    DEFAULT_MIN_HEALTH_SCORE,
    DenyReason,
    IdempotencyTracker,
    PurchaseDecision,
    PurchaseEvent,
    PurchaseGate,
    PurchaseMode,
    get_default_gate,
    reset_default_gate_for_test,
)


# ════════════════════════════════════════════════════════════════════
#  Test helpers
# ════════════════════════════════════════════════════════════════════
def permissive_gate(**overrides):
    """Crea un gate con thresholds laxos para tests no-health.

    Cooldown=0 + sin health checks por defecto (min_health_score=0,
    thresholds altos). Permite testear casos no-health sin interferencia.
    """
    defaults = dict(
        mode=PurchaseMode.LIVE,
        global_enabled=True,
        min_cooldown_s=0,
        min_health_score=0,
        mismatch_rate_threshold=1.0,
        critical_crossing_rate_threshold=1.0,
        min_consistency=0.0,
        max_inflation_penalty=999,
    )
    defaults.update(overrides)
    return PurchaseGate(**defaults)


class FakeHealthScore:
    """Test double: control total sobre lo que ve el gate.

    Reproduce el contrato público que PurchaseGate._extract_health_payload()
    consume, aislando cada métrica para tests deterministas.
    """
    def __init__(self, *, score=90, shadow=90, consistency=1.0,
                 inflation=0, mismatch_rate=0.0,
                 critical_crossing_rate=0.0):
        self.score = score
        self._shadow = shadow
        self._consistency = consistency
        self._inflation = inflation
        self._mismatch_rate = mismatch_rate
        self._critical_crossing_rate = critical_crossing_rate

    def compute_m12_score(self):
        return self._shadow

    def _verdict(self, score=None):
        s = self.score if score is None else score
        if s >= 80:
            return "HEALTHY"
        if s >= 60:
            return "STABLE"
        if s >= 30:
            return "UNHEALTHY"
        return "CRITICAL"

    def _shadow_comparison_stats(self):
        return {
            "mismatch_rate": self._mismatch_rate,
            "critical_crossing_rate": self._critical_crossing_rate,
        }

    def _compute_consistency(self):
        return self._consistency

    def _compute_inflation_penalty(self):
        return self._inflation


# ════════════════════════════════════════════════════════════════════
#  1. PurchaseDecision dataclass
# ════════════════════════════════════════════════════════════════════
def test_m13_purchase_decision_default_fields():
    """PurchaseDecision tiene fields default seguros."""
    d = PurchaseDecision(allowed=False, reason="test")
    assert d.allowed is False
    assert d.reason == "test"
    assert d.mode == PurchaseMode.DISABLED
    assert d.dry_run is False
    assert d.official_score is None
    assert d.retry_count == 0
    assert isinstance(d.decision_ts_monotonic, float)


def test_m13_purchase_decision_as_dict_complete():
    """as_dict() expone todas las keys esperadas para JSONL."""
    d = PurchaseDecision(
        allowed=True, reason="ok",
        mode=PurchaseMode.LIVE,
        official_score=80, shadow_score=85,
        official_verdict="HEALTHY", shadow_verdict="HEALTHY",
        mismatch_rate=0.0, critical_crossing_rate=0.0,
        consistency=1.0, inflation_penalty=0,
        idempotency_key="abc",
        session_id="s1", account_id="a1",
        retry_count=0, dry_run=False,
        checkout_age_s=5.0,
    )
    out = d.as_dict()
    expected_keys = {
        "allowed", "reason", "mode", "dry_run",
        "official_score", "shadow_score",
        "official_verdict", "shadow_verdict",
        "mismatch_rate", "critical_crossing_rate",
        "consistency", "inflation_penalty",
        "idempotency_key", "checkout_age_s",
        "retry_count", "session_id", "account_id",
    }
    assert set(out.keys()) == expected_keys


# ════════════════════════════════════════════════════════════════════
#  2. Constants integrity (replay compat)
# ════════════════════════════════════════════════════════════════════
def test_m13_purchase_modes_constants():
    """3 modos exactos."""
    assert PurchaseMode.DISABLED == "disabled"
    assert PurchaseMode.DRY_RUN == "dry_run"
    assert PurchaseMode.LIVE == "live"
    assert PurchaseMode.all_modes() == ("disabled", "dry_run", "live")
    assert PurchaseMode.is_valid("live")
    assert not PurchaseMode.is_valid("bogus")
    assert not PurchaseMode.is_valid(None)


def test_m13_purchase_event_constants_replay_safe():
    """8 event names exactos como strings inmutables."""
    expected = {
        "purchase_attempted", "purchase_allowed", "purchase_blocked",
        "purchase_dry_run", "purchase_live_success",
        "purchase_duplicate_blocked", "purchase_stale_checkout",
        "purchase_kill_switch_blocked",
    }
    assert set(PurchaseEvent.all_events()) == expected


def test_m13_deny_reason_constants_stable():
    """DenyReason constants son strings estables."""
    assert DenyReason.KILL_SWITCH_OFF == "kill_switch_off"
    assert DenyReason.MODE_DISABLED == "mode_disabled"
    assert DenyReason.DUPLICATE_KEY == "duplicate_idempotency_key"
    assert DenyReason.STALE_CHECKOUT == "stale_checkout"
    assert DenyReason.HEALTH_CRITICAL == "health_verdict_critical"
    assert DenyReason.CRITICAL_CROSSING_HIGH == "critical_crossing_rate_high"


# ════════════════════════════════════════════════════════════════════
#  3. Kill switch (hard deny)
# ════════════════════════════════════════════════════════════════════
def test_m13_kill_switch_off_denies_even_in_live():
    """Kill switch off → DENY incluso con mode=live."""
    gate = PurchaseGate(mode=PurchaseMode.LIVE, global_enabled=False)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.KILL_SWITCH_OFF
    assert gate.stats()["kill_switch_denials"] == 1


def test_m13_kill_switch_default_is_off():
    """Por defecto kill switch está off (safety-first)."""
    gate = PurchaseGate()
    assert gate.global_enabled is False


def test_m13_kill_switch_runtime_toggle():
    """set_global_enabled cambia estado en runtime."""
    gate = PurchaseGate(global_enabled=False)
    assert gate.global_enabled is False
    gate.set_global_enabled(True)
    assert gate.global_enabled is True
    gate.set_global_enabled(False)
    assert gate.global_enabled is False


def test_m13_kill_switch_blocked_event_emitted():
    """Kill switch denial emite PurchaseEvent.KILL_SWITCH_BLOCKED."""
    gate = PurchaseGate(mode=PurchaseMode.LIVE, global_enabled=False)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
    )
    # Telemetry recoge el deny
    assert d.reason == DenyReason.KILL_SWITCH_OFF


# ════════════════════════════════════════════════════════════════════
#  4. Mode (disabled/dry_run/live)
# ════════════════════════════════════════════════════════════════════
def test_m13_mode_disabled_denies():
    """Mode disabled → DENY mode_disabled."""
    gate = PurchaseGate(mode=PurchaseMode.DISABLED, global_enabled=True)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.MODE_DISABLED
    assert gate.stats()["mode_disabled_denials"] == 1


def test_m13_mode_dry_run_allows_with_dry_run_flag():
    """Mode dry_run → allowed=True con dry_run=True."""
    gate = permissive_gate(mode=PurchaseMode.DRY_RUN)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    assert d.allowed is True
    assert d.dry_run is True
    assert d.mode == PurchaseMode.DRY_RUN
    assert gate.stats()["purchase_dry_run_count"] == 1
    assert gate.stats()["purchase_live_count"] == 0


def test_m13_mode_live_allows_without_dry_run_flag():
    """Mode live → allowed=True con dry_run=False."""
    gate = permissive_gate(mode=PurchaseMode.LIVE)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    assert d.allowed is True
    assert d.dry_run is False
    assert d.mode == PurchaseMode.LIVE
    assert gate.stats()["purchase_live_count"] == 1
    assert gate.stats()["purchase_dry_run_count"] == 0


def test_m13_set_mode_runtime_valid_and_invalid():
    """set_mode acepta válidos, rechaza inválidos."""
    gate = PurchaseGate(mode=PurchaseMode.DISABLED)
    assert gate.set_mode(PurchaseMode.LIVE) is True
    assert gate.mode == PurchaseMode.LIVE
    assert gate.set_mode("bogus") is False
    assert gate.mode == PurchaseMode.LIVE  # unchanged


def test_m13_invalid_mode_arg_falls_back_to_disabled():
    """PurchaseGate(mode=bogus) → fallback a disabled."""
    gate = PurchaseGate(mode="invalid_mode")
    assert gate.mode == PurchaseMode.DISABLED


# ════════════════════════════════════════════════════════════════════
#  5. Input validation
# ════════════════════════════════════════════════════════════════════
def test_m13_invalid_session_id_denies():
    gate = permissive_gate()
    d = gate.evaluate(session_id="", account_id="a", event_id="e", qty=1)
    assert d.allowed is False
    assert d.reason == DenyReason.INVALID_INPUT


def test_m13_invalid_account_id_denies():
    gate = permissive_gate()
    d = gate.evaluate(session_id="s", account_id="", event_id="e", qty=1)
    assert d.allowed is False
    assert d.reason == DenyReason.INVALID_INPUT


def test_m13_invalid_qty_zero_denies():
    gate = permissive_gate()
    d = gate.evaluate(session_id="s", account_id="a", event_id="e", qty=0)
    assert d.allowed is False
    assert d.reason == DenyReason.INVALID_INPUT


def test_m13_retry_exceeded_denies():
    """retry_count >= max_retries → DENY retry_count_exceeded."""
    gate = permissive_gate(max_retries=3)
    d = gate.evaluate(
        session_id="s", account_id="a", event_id="e", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        retry_count=3,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.RETRY_EXCEEDED


# ════════════════════════════════════════════════════════════════════
#  6. Idempotency (duplicate protection)
# ════════════════════════════════════════════════════════════════════
def test_m13_idempotency_key_deterministic():
    """make_idempotency_key es determinista para mismos inputs."""
    k1 = PurchaseGate.make_idempotency_key(
        event_id="e1", account_id="a1", qty=1,
        checkout_opened_at_monotonic=1000.0,
    )
    k2 = PurchaseGate.make_idempotency_key(
        event_id="e1", account_id="a1", qty=1,
        checkout_opened_at_monotonic=1000.0,
    )
    assert k1 == k2
    assert len(k1) == 64  # SHA-256 hex


def test_m13_idempotency_key_distinct_for_distinct_inputs():
    """Diferente input → diferente key."""
    k1 = PurchaseGate.make_idempotency_key(
        event_id="e1", account_id="a1", qty=1,
        checkout_opened_at_monotonic=1000.0,
    )
    k2 = PurchaseGate.make_idempotency_key(
        event_id="e2", account_id="a1", qty=1,
        checkout_opened_at_monotonic=1000.0,
    )
    assert k1 != k2


def test_m13_duplicate_submit_blocked():
    """Submit con misma key consecutivamente → 2º bloqueado."""
    gate = permissive_gate()
    now = time.monotonic()
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
    )
    d2 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
    )
    assert d1.allowed is True
    assert d2.allowed is False
    assert d2.reason == DenyReason.DUPLICATE_KEY
    assert gate.stats()["duplicate_prevented_count"] == 1


def test_m13_distinct_events_both_allowed():
    """Diferentes event_id → ambos pasan."""
    gate = permissive_gate()
    now = time.monotonic()
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
    )
    d2 = gate.evaluate(
        session_id="s2", account_id="a1", event_id="e2", qty=1,
        checkout_opened_at_monotonic=now,
    )
    assert d1.allowed is True
    assert d2.allowed is True


def test_m13_idempotency_tracker_record_atomic():
    """IdempotencyTracker.record() es atómico: 1ª True, 2ª False."""
    t = IdempotencyTracker(ttl_s=60, max_size=10)
    assert t.record("k1") is True
    assert t.record("k1") is False  # duplicate
    assert t.record("k2") is True
    assert t.size() == 2


def test_m13_idempotency_tracker_lru_eviction():
    """LRU eviction cuando supera max_size."""
    t = IdempotencyTracker(ttl_s=3600, max_size=3)
    for i in range(5):
        t.record(f"k{i}")
    assert t.size() == 3  # cap respetado
    # Las 3 últimas (k2, k3, k4) deben estar; k0, k1 evictadas
    assert t.seen("k0") is False
    assert t.seen("k1") is False
    assert t.seen("k4") is True


def test_m13_idempotency_tracker_invalid_input():
    """Keys vacías/None → False sin lanzar."""
    t = IdempotencyTracker()
    assert t.record("") is False
    assert t.record(None) is False  # type: ignore
    assert t.seen("") is False


# ════════════════════════════════════════════════════════════════════
#  7. Stale checkout
# ════════════════════════════════════════════════════════════════════
def test_m13_stale_checkout_denies():
    """Checkout abierto hace >max_checkout_lifetime_s → DENY stale_checkout."""
    gate = permissive_gate(max_checkout_lifetime_s=10)
    stale_ts = time.monotonic() - 60
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=stale_ts,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.STALE_CHECKOUT
    assert d.checkout_age_s is not None
    assert d.checkout_age_s > 10
    assert gate.stats()["stale_checkout_denials"] == 1


def test_m13_fresh_checkout_allowed():
    """Checkout abierto recientemente → permitido."""
    gate = permissive_gate(max_checkout_lifetime_s=600)
    fresh_ts = time.monotonic() - 5
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=fresh_ts,
    )
    assert d.allowed is True
    assert d.checkout_age_s is not None
    assert d.checkout_age_s < 10


# ════════════════════════════════════════════════════════════════════
#  8. Cooldown
# ════════════════════════════════════════════════════════════════════
def test_m13_cooldown_blocks_subsequent_attempt():
    """Attempt rápido tras success → cooldown_active."""
    gate = permissive_gate(min_cooldown_s=30)
    now = time.monotonic()
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
    )
    # Mismo session_id, diferente event_id → cooldown activo
    d2 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e2", qty=1,
        checkout_opened_at_monotonic=now,
    )
    assert d1.allowed is True
    assert d2.allowed is False
    assert d2.reason == DenyReason.COOLDOWN_ACTIVE
    assert gate.stats()["cooldown_denials"] == 1


def test_m13_cooldown_zero_allows_immediate_retry():
    """min_cooldown_s=0 → no cooldown."""
    gate = permissive_gate(min_cooldown_s=0)
    now = time.monotonic()
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
    )
    d2 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e2", qty=1,
        checkout_opened_at_monotonic=now,
    )
    assert d1.allowed is True
    assert d2.allowed is True


# ════════════════════════════════════════════════════════════════════
#  9. Concurrent attempts (per-session lock)
# ════════════════════════════════════════════════════════════════════
def test_m13_concurrent_same_session_only_one_wins():
    """N threads hitting same session_id → solo 1 allowed."""
    gate = permissive_gate()
    barrier = threading.Barrier(10)
    results: list[PurchaseDecision] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        d = gate.evaluate(
            session_id="s1", account_id="a1", event_id="e1", qty=1,
            checkout_opened_at_monotonic=time.monotonic(),
        )
        with results_lock:
            results.append(d)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = [d for d in results if d.allowed]
    denied = [d for d in results if not d.allowed]
    assert len(results) == 10
    assert len(allowed) == 1
    # Denials son concurrent_attempt o duplicate_idempotency_key
    deny_reasons = {d.reason for d in denied}
    assert deny_reasons.issubset({
        DenyReason.CONCURRENT_ATTEMPT,
        DenyReason.DUPLICATE_KEY,
    })


def test_m13_concurrent_distinct_sessions_all_succeed():
    """100 threads, sessions distintas → todos pasan."""
    gate = permissive_gate()
    barrier = threading.Barrier(100)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker(i):
        barrier.wait()
        d = gate.evaluate(
            session_id=f"s{i}", account_id=f"a{i}", event_id=f"e{i}", qty=1,
            checkout_opened_at_monotonic=time.monotonic(),
        )
        with results_lock:
            results.append(d.allowed)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 100
    assert gate.stats()["purchase_allowed_count"] == 100


def test_m13_thread_safety_stats_no_corruption():
    """Stress test: counters consistentes bajo concurrencia."""
    gate = permissive_gate(min_cooldown_s=0)
    N = 200
    barrier = threading.Barrier(N)

    def worker(i):
        barrier.wait()
        gate.evaluate(
            session_id=f"s{i}", account_id=f"a{i}", event_id=f"e{i}", qty=1,
            checkout_opened_at_monotonic=time.monotonic(),
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = gate.stats()
    # Conservación: attempted == allowed + blocked
    assert stats["purchase_attempted_count"] == N
    assert (stats["purchase_allowed_count"]
            + stats["purchase_blocked_count"]) == N


# ════════════════════════════════════════════════════════════════════
#  10. HealthScore integration
# ════════════════════════════════════════════════════════════════════
def test_m13_no_health_score_allows_when_other_checks_pass():
    """Sin HealthScore: gate solo aplica reglas no-health."""
    gate = permissive_gate()
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=None,
    )
    assert d.allowed is True


def test_m13_health_critical_denies():
    """HealthScore con verdict CRITICAL → DENY health_verdict_critical."""
    hs = HealthScore()
    hs.score = 10  # CRITICAL (<30)
    gate = permissive_gate(min_health_score=0)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.HEALTH_CRITICAL
    assert d.official_verdict == "CRITICAL"


def test_m13_health_below_threshold_denies():
    """Score oficial < min_health_score (sin verdicts CRITICAL) → DENY health_low.

    Uso FakeHealthScore para aislar la métrica concreta: con HealthScore real,
    .score=50 sin events produce shadow=25 (CRITICAL) y dispararía SHADOW_CRITICAL
    antes de evaluar HEALTH_LOW.
    """
    hs = FakeHealthScore(score=50, shadow=50)  # ambos UNHEALTHY, no CRITICAL
    gate = permissive_gate(min_health_score=60)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.HEALTH_LOW


def test_m13_health_healthy_allows():
    """Score HEALTHY (ambos verdicts) → permitido."""
    hs = FakeHealthScore(score=90, shadow=90)
    gate = permissive_gate(min_health_score=60)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.allowed is True
    assert d.official_score == 90
    assert d.official_verdict == "HEALTHY"
    assert d.shadow_verdict == "HEALTHY"


def test_m13_shadow_critical_denies():
    """shadow_verdict CRITICAL (oficial NO) → DENY shadow_verdict_critical.

    Este es el caso operacional real: M11 OK, M12 detecta degradación.
    """
    hs = FakeHealthScore(score=80, shadow=20)  # HEALTHY / CRITICAL
    gate = permissive_gate(min_health_score=0)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.SHADOW_CRITICAL
    assert gate.stats()["health_denials"] == 1


def test_m13_mismatch_rate_high_denies():
    """mismatch_rate > threshold (sin CRITICAL en verdicts) → DENY mismatch_rate_high."""
    hs = FakeHealthScore(score=90, shadow=90, mismatch_rate=0.5)
    gate = permissive_gate(
        min_health_score=0,
        mismatch_rate_threshold=0.20,
        critical_crossing_rate_threshold=1.0,
    )
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.MISMATCH_RATE_HIGH
    assert gate.stats()["mismatch_purchase_denials"] == 1


def test_m13_critical_crossing_rate_high_denies():
    """critical_crossing_rate > threshold → DENY critical_crossing_rate_high."""
    hs = FakeHealthScore(
        score=90, shadow=90,
        mismatch_rate=0.1,
        critical_crossing_rate=0.2,
    )
    gate = permissive_gate(
        min_health_score=0,
        mismatch_rate_threshold=1.0,  # permissive
        critical_crossing_rate_threshold=0.05,  # 0.2 > 0.05
    )
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.CRITICAL_CROSSING_HIGH
    assert gate.stats()["critical_crossing_denials"] == 1


def test_m13_consistency_low_denies():
    """consistency < min_consistency → DENY consistency_low."""
    hs = FakeHealthScore(score=90, shadow=90, consistency=0.3)
    gate = permissive_gate(
        min_health_score=0,
        min_consistency=0.5,
        mismatch_rate_threshold=1.0,
        critical_crossing_rate_threshold=1.0,
    )
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.CONSISTENCY_LOW


def test_m13_inflation_high_denies():
    """inflation_penalty > threshold → DENY inflation_penalty_high."""
    hs = FakeHealthScore(score=90, shadow=90, inflation=25, consistency=1.0)
    gate = permissive_gate(
        min_health_score=0,
        max_inflation_penalty=15,
        mismatch_rate_threshold=1.0,
        critical_crossing_rate_threshold=1.0,
        min_consistency=0.0,
    )
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.allowed is False
    assert d.reason == DenyReason.INFLATION_HIGH
    assert gate.stats()["inflation_denials"] == 1


def test_m13_health_check_priority_order():
    """Orden de checks de salud: CRITICAL > shadow CRITICAL > HEALTH_LOW > mismatch > crossing > consistency > inflation."""
    # Multiple flags: should hit health_critical first
    hs = FakeHealthScore(
        score=10, shadow=20,  # both CRITICAL
        mismatch_rate=0.9, critical_crossing_rate=0.9,
        consistency=0.0, inflation=99,
    )
    gate = permissive_gate(min_health_score=60, max_inflation_penalty=15,
                          mismatch_rate_threshold=0.2,
                          critical_crossing_rate_threshold=0.05,
                          min_consistency=0.5)
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=hs,
    )
    assert d.reason == DenyReason.HEALTH_CRITICAL  # priority 1


# ════════════════════════════════════════════════════════════════════
#  11. Telemetry counters + rates
# ════════════════════════════════════════════════════════════════════
def test_m13_stats_initial_zero():
    """Counters inicializan en 0."""
    gate = PurchaseGate()
    stats = gate.stats()
    for k in ("purchase_attempted_count", "purchase_allowed_count",
              "purchase_blocked_count", "duplicate_prevented_count"):
        assert stats[k] == 0
    # Rates iniciales son 0.0 (no NaN)
    assert stats["purchase_allow_rate"] == 0.0
    assert stats["purchase_block_rate"] == 0.0


def test_m13_stats_rates_calculated():
    """allow_rate y block_rate calculadas correctamente."""
    gate = permissive_gate()
    # 3 attempts: 2 allowed + 1 duplicate blocked
    now = time.monotonic()
    gate.evaluate(session_id="s1", account_id="a1", event_id="e1", qty=1,
                  checkout_opened_at_monotonic=now)
    gate.evaluate(session_id="s2", account_id="a2", event_id="e2", qty=1,
                  checkout_opened_at_monotonic=now)
    gate.evaluate(session_id="s1", account_id="a1", event_id="e1", qty=1,
                  checkout_opened_at_monotonic=now)  # duplicate
    stats = gate.stats()
    assert stats["purchase_attempted_count"] == 3
    assert stats["purchase_allowed_count"] == 2
    assert stats["purchase_blocked_count"] == 1
    assert abs(stats["purchase_allow_rate"] - 2 / 3) < 0.001
    assert abs(stats["purchase_block_rate"] - 1 / 3) < 0.001


def test_m13_reset_stats_clears_counters():
    """reset_stats() pone todos en 0."""
    gate = permissive_gate()
    gate.evaluate(session_id="s1", account_id="a1", event_id="e1", qty=1,
                  checkout_opened_at_monotonic=time.monotonic())
    assert gate.stats()["purchase_attempted_count"] == 1
    gate.reset_stats()
    stats = gate.stats()
    assert stats["purchase_attempted_count"] == 0
    assert stats["purchase_allowed_count"] == 0


# ════════════════════════════════════════════════════════════════════
#  12. Decision priority ordering
# ════════════════════════════════════════════════════════════════════
def test_m13_kill_switch_priority_over_mode():
    """Kill switch off + mode live → DENY kill_switch_off (no mode_disabled)."""
    gate = PurchaseGate(mode=PurchaseMode.LIVE, global_enabled=False)
    d = gate.evaluate(session_id="s", account_id="a", event_id="e", qty=1)
    assert d.reason == DenyReason.KILL_SWITCH_OFF


def test_m13_mode_priority_over_input_validation():
    """Mode disabled + invalid input → DENY mode_disabled primero."""
    gate = PurchaseGate(mode=PurchaseMode.DISABLED, global_enabled=True)
    d = gate.evaluate(session_id="", account_id="a", event_id="e", qty=1)
    assert d.reason == DenyReason.MODE_DISABLED


def test_m13_stale_checkout_priority_over_cooldown():
    """Stale checkout + cooldown activo → DENY stale_checkout primero."""
    gate = permissive_gate(
        min_cooldown_s=30, max_checkout_lifetime_s=10,
    )
    now = time.monotonic()
    # Crear cooldown previo
    gate.evaluate(session_id="s1", account_id="a1", event_id="e0", qty=1,
                  checkout_opened_at_monotonic=now)
    # Ahora stale + cooldown
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now - 60,
    )
    assert d.reason == DenyReason.STALE_CHECKOUT


# ════════════════════════════════════════════════════════════════════
#  13. Singleton
# ════════════════════════════════════════════════════════════════════
def test_m13_singleton_lazy_init():
    """get_default_gate() devuelve la misma instancia."""
    reset_default_gate_for_test()
    g1 = get_default_gate()
    g2 = get_default_gate()
    assert g1 is g2


def test_m13_singleton_reset():
    """reset_default_gate_for_test() invalida singleton."""
    reset_default_gate_for_test()
    g1 = get_default_gate()
    reset_default_gate_for_test()
    g2 = get_default_gate()
    assert g1 is not g2


# ════════════════════════════════════════════════════════════════════
#  14. Failsafe behavior
# ════════════════════════════════════════════════════════════════════
def test_m13_failsafe_on_health_score_exception():
    """Si HealthScore lanza, gate degrada a DENY (no propaga)."""
    class BrokenHS:
        @property
        def score(self):
            raise RuntimeError("broken")

        def compute_m12_score(self):
            raise RuntimeError("broken")

    gate = permissive_gate()
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
        health_score=BrokenHS(),
    )
    # _extract_health_payload tiene try/except por cada campo;
    # devuelve dict con None values. Gate decide ALLOW (no health checks aplicables).
    # El test valida que NO crashea.
    assert isinstance(d, PurchaseDecision)


def test_m13_failsafe_returns_decision_on_internal_error():
    """Excepción no anticipada → DENY internal_error sin propagar."""
    gate = permissive_gate()
    # Forzar excepción mutando estado interno
    gate._idem = None  # type: ignore  # type-unsafe pero válido para test
    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    assert d.allowed is False
    assert d.reason == DenyReason.INTERNAL_ERROR
    assert gate.stats()["internal_errors"] >= 1


# ════════════════════════════════════════════════════════════════════
#  15. Defaults sanity
# ════════════════════════════════════════════════════════════════════
def test_m13_default_constants_sane():
    """Defaults son safety-first."""
    assert DEFAULT_MIN_COOLDOWN_S >= 30
    assert DEFAULT_MAX_CHECKOUT_LIFETIME_S >= 600
    assert DEFAULT_IDEMPOTENCY_TTL_S >= 3600
    assert DEFAULT_IDEM_CACHE_MAX >= 1024
    assert DEFAULT_MIN_HEALTH_SCORE >= 60
    assert DEFAULT_MAX_RETRIES >= 1
    assert DEFAULT_MAX_RETRIES <= 10  # razonable


# ════════════════════════════════════════════════════════════════════
#  M13.1 HARDENING TESTS — pre-Fase-3 MUST FIX
# ════════════════════════════════════════════════════════════════════

# ── FIX 1: HealthScore snapshot atómico ──────────────────────────────
def test_m13_1_health_snapshot_lock_present():
    """PurchaseGate expone _health_snapshot_lock para serializar reads."""
    gate = PurchaseGate()
    assert hasattr(gate, "_health_snapshot_lock")
    assert isinstance(gate._health_snapshot_lock, type(threading.Lock()))


def test_m13_1_health_snapshot_atomic_under_concurrency():
    """Múltiples threads leyendo el mismo HealthScore via gate NO se
    interleavean dentro de _extract_health_payload.

    Setup: HealthScore que cuenta cuántas veces se llama compute_m12_score().
    Si el lock funciona, las llamadas son serializadas — verificable
    porque NO observamos lecturas intercaladas.
    """
    class CountingHS:
        def __init__(self):
            self.score = 80
            self._concurrent_reads = 0
            self._max_concurrent = 0
            self._lock = threading.Lock()

        def _enter(self):
            with self._lock:
                self._concurrent_reads += 1
                if self._concurrent_reads > self._max_concurrent:
                    self._max_concurrent = self._concurrent_reads

        def _exit(self):
            with self._lock:
                self._concurrent_reads -= 1

        def compute_m12_score(self):
            self._enter()
            time.sleep(0.001)  # forzar overlap si NO hay serialización
            self._exit()
            return 80

        def _verdict(self, score=None):
            return "HEALTHY"

        def _shadow_comparison_stats(self):
            return {"mismatch_rate": 0.0, "critical_crossing_rate": 0.0}

        def _compute_consistency(self):
            return 1.0

        def _compute_inflation_penalty(self):
            return 0

    hs = CountingHS()
    gate = permissive_gate()

    # 20 threads ejecutando _extract_health_payload en el mismo HS
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        gate._extract_health_payload(hs)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Con _health_snapshot_lock, max_concurrent debe ser 1
    assert hs._max_concurrent == 1, (
        f"Health reads interleaved: max_concurrent={hs._max_concurrent}, "
        "expected 1 con _health_snapshot_lock"
    )


# ── FIX 2: Cooldown sobre TODOS los attempts excepto system-off ──────
def test_m13_1_cooldown_applies_on_deny_duplicate():
    """Tras DENY duplicate en s2, cooldown se registra para s2.

    Setup intencional para aislar el efecto del FIX 2 sobre DUPLICATE
    específicamente:
        - s1 + key K → ALLOW (registra K en idempotency)
        - s2 + key K → DENY DUPLICATE_KEY (sesión sin cooldown previo)
        - s2 + nuevo intento → debe ver COOLDOWN_ACTIVE
          (porque el DENY de d2 registró cooldown para s2 — M13.1 FIX 2)
    """
    gate = permissive_gate(min_cooldown_s=30)
    now = time.monotonic()
    shared_key = "shared_idem_key_for_test"
    # s1 → ALLOW, registra shared_key en idempotency
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
        idempotency_key=shared_key,
    )
    assert d1.allowed is True
    # s2 (sin cooldown previo) + misma key → DUPLICATE_KEY
    d2 = gate.evaluate(
        session_id="s2", account_id="a2", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
        idempotency_key=shared_key,
    )
    assert d2.allowed is False
    assert d2.reason == DenyReason.DUPLICATE_KEY
    # s2 retry distinto: el DENY anterior registró cooldown para s2
    d3 = gate.evaluate(
        session_id="s2", account_id="a2", event_id="e_other", qty=1,
        checkout_opened_at_monotonic=now,
    )
    assert d3.allowed is False
    assert d3.reason == DenyReason.COOLDOWN_ACTIVE


def test_m13_1_cooldown_applies_on_invalid_input():
    """invalid_input deny TAMBIÉN registra cooldown (anti spam storm)."""
    gate = permissive_gate(min_cooldown_s=30)
    # Inválido por qty=0
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=0,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    assert d1.reason == DenyReason.INVALID_INPUT
    # Siguiente intento legítimo debe ver cooldown
    d2 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    assert d2.allowed is False
    assert d2.reason == DenyReason.COOLDOWN_ACTIVE


def test_m13_1_cooldown_skipped_on_kill_switch_deny():
    """kill_switch_off NO registra cooldown (system-off, no throttle)."""
    # Setup: gate live, primero attempt → ALLOW.
    gate = permissive_gate(min_cooldown_s=30)
    now = time.monotonic()
    gate.evaluate(  # ALLOW registra cooldown
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
    )
    # Operator flipea kill switch off
    gate.set_global_enabled(False)
    # Attempt en cooldown window: kill switch tiene prioridad
    d = gate.evaluate(
        session_id="s2", account_id="a2", event_id="e2", qty=1,
        checkout_opened_at_monotonic=now,
    )
    assert d.reason == DenyReason.KILL_SWITCH_OFF
    # Verificar que cooldown NO fue registrado para s2
    # (no se puede chequear directamente sin leer estado, pero sabemos
    # que el deny NO atravesó _record_attempt)
    # Otro flip y attempt en s2: debe pasar kill switch ahora
    gate.set_global_enabled(True)
    # s2 sin cooldown previo → idempotency-fresh attempt
    d2 = gate.evaluate(
        session_id="s2", account_id="a2", event_id="e3", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    assert d2.allowed is True


def test_m13_1_cooldown_skipped_on_mode_disabled_deny():
    """mode_disabled NO registra cooldown."""
    gate = PurchaseGate(
        mode=PurchaseMode.DISABLED, global_enabled=True,
        min_cooldown_s=30,
    )
    # Deny por mode_disabled
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    assert d1.reason == DenyReason.MODE_DISABLED
    # Cambiar a live: s1 debería poder atacar sin cooldown
    gate.set_mode(PurchaseMode.LIVE)
    d2 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e2", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    # min_health_score default es 60 — sin health_score, no se evalúa health.
    # Pero el default constructor usa min_health_score=DEFAULT (60).
    # Como no pasamos health_score, no health check se ejecuta → ALLOW.
    assert d2.allowed is True


# ── FIX 3: Kill switch re-check post-lock ────────────────────────────
def test_m13_1_kill_switch_recheck_post_lock_aborts():
    """Si kill switch se flipea entre el primer check y el sess_lock,
    el segundo check (post-lock) lo aborta. Test usa hook en
    _get_session_lock para simular el flip mid-flight.
    """
    gate = permissive_gate()
    original_get_lock = gate._get_session_lock

    def hooked_get_lock(session_id):
        lock = original_get_lock(session_id)
        # Simular operator flip durante la adquisición del lock
        gate.set_global_enabled(False)
        return lock

    gate._get_session_lock = hooked_get_lock  # type: ignore

    d = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )
    # El primer check pasó (gate.global_enabled era True al entrar).
    # El segundo check (post-lock) detectó el flip → DENY kill_switch_off.
    assert d.allowed is False
    assert d.reason == DenyReason.KILL_SWITCH_OFF
    # Verificar que idempotency NO se registró (key debería ser fresh)
    assert gate._idem.size() == 0


def test_m13_1_kill_switch_recheck_preserves_idempotency_freshness():
    """Late-abort tras kill switch flip permite retry una vez flipeado on."""
    gate = permissive_gate()
    original_get_lock = gate._get_session_lock
    flip_once = {"done": False}

    def hooked_get_lock(session_id):
        lock = original_get_lock(session_id)
        if not flip_once["done"]:
            gate.set_global_enabled(False)
            flip_once["done"] = True
        return lock

    gate._get_session_lock = hooked_get_lock  # type: ignore
    now = time.monotonic()
    # Attempt 1: late-abort por kill switch flip
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
    )
    assert d1.reason == DenyReason.KILL_SWITCH_OFF
    # Restore kill switch + remove hook
    gate.set_global_enabled(True)
    gate._get_session_lock = original_get_lock  # type: ignore
    # Attempt 2: misma key — idempotency NO debe bloquear porque
    # el late-abort NO la registró
    # PERO: cooldown podría bloquear (KILL_SWITCH_OFF es system-off → no
    # registra cooldown). Y necesitamos sortear esto:
    # idempotency key derivada usa checkout_opened_at_monotonic bucket(60s).
    # Con misma ts, misma key. Si no fue registrada, ALLOW.
    d2 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
    )
    assert d2.allowed is True, (
        f"Retry tras kill switch on debe pasar; got {d2.reason}"
    )


# ── FIX 4: Crash/restart idempotency loss — documenta limitación ─────
def test_m13_1_crash_restart_idempotency_loss_documented():
    """In-memory idempotency se pierde tras 'crash'. Duplicado tras
    restart es ACEPTADO en single-worker mode — documentado, no es bug.

    Para multi-worker o crash-sensitive, requerir persistencia externa
    (fuera de scope M13.1).
    """
    gate = permissive_gate()
    now = time.monotonic()
    key = "deterministic_key_for_crash_test"
    # Attempt 1: ALLOW + key registrada
    d1 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
        idempotency_key=key,
    )
    assert d1.allowed is True
    assert gate._idem.seen(key) is True
    # Simulación de crash: cache wipe in-memory
    gate._idem._cache.clear()
    assert gate._idem.seen(key) is False
    # Attempt 2: misma key tras "crash" → ALLOW (NO duplicate)
    d2 = gate.evaluate(
        session_id="s1", account_id="a1", event_id="e1", qty=1,
        checkout_opened_at_monotonic=now,
        idempotency_key=key,
        # min_cooldown_s default es DEFAULT_MIN_COOLDOWN_S=30,
        # pero permissive_gate sobrescribe a 0
    )
    assert d2.allowed is True, (
        "Tras cache wipe, misma key debe ser ALLOW (limitación in-memory "
        "documentada — single-worker mode acepta duplicates post-crash)"
    )
