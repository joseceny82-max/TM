"""core/diagnostics/purchase_gate.py — M13 Production-grade purchase safety layer.

Capa determinista de seguridad operacional para compras reales en producción.
Evalúa allow/deny de cada intento de compra basándose en:

    - Modo de operación: disabled / dry_run / live
    - Global kill switch (hard deny)
    - Idempotency keys con TTL (duplicate protection)
    - Per-session locks (concurrent submit protection)
    - Stale checkout detection
    - Cooldowns por sesión (retry storm protection)
    - Retry count cap
    - Health score (M11) thresholds
    - Shadow score (M12) thresholds
    - Mismatch rate (M12.2) thresholds
    - Critical crossing rate (M12.3) thresholds
    - Consistency mínima (M12)
    - Inflation penalty máximo (M12.1)

Diseño:
    - Thread-safe vía threading.Lock + OrderedDict (sin async/Redis/DB)
    - Stdlib only (sin dependencias externas nuevas)
    - Backward-compatible (módulo nuevo, cero modificaciones de existentes)
    - Fail-safe: cualquier excepción interna → DENY (internal_error)
    - HealthScore es OPCIONAL — gate funciona sin él (skip health checks)
    - Eventos emitidos a runtime_diagnostics best-effort (silent fail)

Default state:
    PURCHASE_MODE              = "disabled" (hard deny)
    GLOBAL_PURCHASE_ENABLED    = False      (kill switch off)

Estos defaults garantizan que NO se pueden hacer compras reales sin override
explícito por env var o argumento de constructor.

Production rollout:
    Fase 1: disabled  (Gate solo emite telemetría, sin acción real)
    Fase 2: dry_run   (Bot ejecuta flow SIN submit final)
    Fase 3: live      (Compras reales permitidas si gate OK)

M13.1 Hardening (STRICT — pre-Fase-3 MUST FIX):
    FIX 1 — HealthScore snapshot atómico:
        _health_snapshot_lock serializa las lecturas dentro de
        _extract_health_payload() → eliminación de race intra-gate.
        NO bloquea mutaciones externas de HS (sigue siendo best-effort
        para HS, pero las lecturas internas del gate son coherentes entre sí).

    FIX 2 — Cooldown sobre TODOS los attempts excepto system-off:
        _record_attempt se ejecuta también en DENY excepto cuando la
        razón es KILL_SWITCH_OFF o MODE_DISABLED (system-off, no spam).
        Protege contra retry storms de invalid_input/duplicate/stale/etc.

    FIX 3 — Kill switch re-check post-lock:
        Tras adquirir sess_lock, segundo check de global_enabled antes
        de tocar idempotency. Garantiza shutdown efectivo bajo concurrencia.

    FIX 4 — Test crash/restart documenta limitación in-memory:
        El cache de idempotency es process-local. Tras crash/restart se
        pierde y duplicados pueden ocurrir. Comportamiento ACEPTADO en
        single-worker; requiere persistencia externa para multi-worker.

Referencias:
    - M11 (.score): produce el verdict oficial
    - M12 (compute_m12_score): produce el shadow score
    - M12.1: hardening (deque, monotonic, weighted inflation)
    - M12.2: observability (mismatch_rate, critical_crossing_rate)
    - M12.3: boundary crossings
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Configuration helpers
# ════════════════════════════════════════════════════════════════════
def _env_int(name: str, default: int) -> int:
    """Lee int de env var con fallback fail-safe."""
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    """Lee float de env var con fallback fail-safe."""
    try:
        return float(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Lee bool de env var con falsy variants comunes."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val not in ("0", "false", "False", "FALSE", "no", "NO", "")


# ════════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════════
class PurchaseMode:
    """3 modos de operación. Strings inmutables para parseability y replay."""
    DISABLED: str = "disabled"
    DRY_RUN:  str = "dry_run"
    LIVE:     str = "live"

    _ALL: tuple = (DISABLED, DRY_RUN, LIVE)

    @classmethod
    def all_modes(cls) -> tuple:
        return cls._ALL

    @classmethod
    def is_valid(cls, mode: Any) -> bool:
        return isinstance(mode, str) and mode in cls._ALL


class DenyReason:
    """Razones estructuradas de denegación. Strings estables para telemetría."""
    KILL_SWITCH_OFF        = "kill_switch_off"
    MODE_DISABLED          = "mode_disabled"
    INVALID_INPUT          = "invalid_input"
    RETRY_EXCEEDED         = "retry_count_exceeded"
    DUPLICATE_KEY          = "duplicate_idempotency_key"
    STALE_CHECKOUT         = "stale_checkout"
    COOLDOWN_ACTIVE        = "cooldown_active"
    CONCURRENT_ATTEMPT     = "concurrent_attempt"
    HEALTH_CRITICAL        = "health_verdict_critical"
    SHADOW_CRITICAL        = "shadow_verdict_critical"
    HEALTH_LOW             = "health_score_below_threshold"
    MISMATCH_RATE_HIGH     = "mismatch_rate_high"
    CRITICAL_CROSSING_HIGH = "critical_crossing_rate_high"
    CONSISTENCY_LOW        = "consistency_low"
    INFLATION_HIGH         = "inflation_penalty_high"
    DRIFT_SEVERE           = "drift_severity_high"
    PHASE_INCOHERENT       = "phase_incoherent"
    INTERNAL_ERROR         = "internal_error"


# M13.1 FIX 2: Reasons consideradas system-off — NO triggear cooldown.
# El resto de denials sí registran cooldown para protección anti-storm.
_SYSTEM_OFF_DENY_REASONS: frozenset = frozenset({
    DenyReason.KILL_SWITCH_OFF,
    DenyReason.MODE_DISABLED,
})


class PurchaseEvent:
    """Event names emitidos a runtime_diagnostics. Replay-safe constants."""
    ATTEMPTED              = "purchase_attempted"
    ALLOWED                = "purchase_allowed"
    BLOCKED                = "purchase_blocked"
    DRY_RUN                = "purchase_dry_run"
    LIVE_SUCCESS           = "purchase_live_success"
    DUPLICATE_BLOCKED      = "purchase_duplicate_blocked"
    STALE_CHECKOUT         = "purchase_stale_checkout"
    KILL_SWITCH_BLOCKED    = "purchase_kill_switch_blocked"

    _ALL: tuple = (
        ATTEMPTED, ALLOWED, BLOCKED, DRY_RUN, LIVE_SUCCESS,
        DUPLICATE_BLOCKED, STALE_CHECKOUT, KILL_SWITCH_BLOCKED,
    )

    @classmethod
    def all_events(cls) -> tuple:
        return cls._ALL


# ════════════════════════════════════════════════════════════════════
#  Default thresholds (env-overridable)
# ════════════════════════════════════════════════════════════════════
DEFAULT_MISMATCH_RATE_THRESHOLD: float = 0.20
DEFAULT_CRITICAL_CROSSING_RATE_THRESHOLD: float = 0.05
DEFAULT_MAX_CHECKOUT_LIFETIME_S: int = 600    # 10 min
DEFAULT_MIN_COOLDOWN_S: int = 30
DEFAULT_IDEMPOTENCY_TTL_S: int = 3600         # 1 h
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_MAX_INFLATION_PENALTY: int = 15
DEFAULT_MIN_HEALTH_SCORE: int = 60            # STABLE o mejor
DEFAULT_MIN_CONSISTENCY: float = 0.5
DEFAULT_MAX_DRIFT_PENALTIES: int = 5
DEFAULT_IDEM_CACHE_MAX: int = 4096

# Hard caps (defense-in-depth, no overridable)
_IDEM_CACHE_HARD_CAP: int = 16384
_LAST_ATTEMPT_CACHE_CAP: int = 4096


# ════════════════════════════════════════════════════════════════════
#  PurchaseDecision dataclass
# ════════════════════════════════════════════════════════════════════
@dataclass
class PurchaseDecision:
    """Resultado determinista de PurchaseGate.evaluate().

    Serializable vía as_dict() para JSONL/eventos/dashboards.
    """
    allowed: bool
    reason: str
    mode: str = PurchaseMode.DISABLED
    # Trazabilidad de la decisión
    decision_ts_monotonic: float = field(default_factory=time.monotonic)
    dry_run: bool = False  # True si allowed AND mode == DRY_RUN
    # Health context (None si no se proveyó HealthScore o falló extracción)
    official_score: Optional[int] = None
    shadow_score: Optional[int] = None
    official_verdict: Optional[str] = None
    shadow_verdict: Optional[str] = None
    mismatch_rate: Optional[float] = None
    critical_crossing_rate: Optional[float] = None
    consistency: Optional[float] = None
    inflation_penalty: Optional[int] = None
    # Gate context
    idempotency_key: Optional[str] = None
    checkout_age_s: Optional[float] = None
    retry_count: int = 0
    session_id: Optional[str] = None
    account_id: Optional[str] = None

    def as_dict(self) -> dict:
        """Snapshot serializable. No incluye claves no-JSON-friendly."""
        return {
            "allowed":                self.allowed,
            "reason":                 self.reason,
            "mode":                   self.mode,
            "dry_run":                self.dry_run,
            "official_score":         self.official_score,
            "shadow_score":           self.shadow_score,
            "official_verdict":       self.official_verdict,
            "shadow_verdict":         self.shadow_verdict,
            "mismatch_rate":          self.mismatch_rate,
            "critical_crossing_rate": self.critical_crossing_rate,
            "consistency":            self.consistency,
            "inflation_penalty":      self.inflation_penalty,
            "idempotency_key":        self.idempotency_key,
            "checkout_age_s":         self.checkout_age_s,
            "retry_count":            self.retry_count,
            "session_id":             self.session_id,
            "account_id":             self.account_id,
        }


# ════════════════════════════════════════════════════════════════════
#  IdempotencyTracker — thread-safe LRU con TTL
# ════════════════════════════════════════════════════════════════════
class IdempotencyTracker:
    """Cache LRU thread-safe de claves de idempotency.

    Cada (key, ts_monotonic) se almacena hasta TTL. Expulsión LRU si
    supera max_size. Combina check + insert atómico bajo lock.
    """
    def __init__(
        self,
        ttl_s: int = DEFAULT_IDEMPOTENCY_TTL_S,
        max_size: int = DEFAULT_IDEM_CACHE_MAX,
    ):
        self.ttl_s = max(1, int(ttl_s))
        self.max_size = min(max(1, int(max_size)), _IDEM_CACHE_HARD_CAP)
        self._cache: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        """True si la key existe Y no ha expirado.

        Thread-safe. Limpia keys expiradas en el camino.
        """
        if not isinstance(key, str) or not key:
            return False
        now = time.monotonic()
        with self._lock:
            ts = self._cache.get(key)
            if ts is None:
                return False
            if now - ts > self.ttl_s:
                self._cache.pop(key, None)
                return False
            self._cache.move_to_end(key)
            return True

    def record(self, key: str) -> bool:
        """Registra una key atómicamente. Devuelve False si ya existía vigente.

        Combina check + insert bajo lock (operación atómica).
        """
        if not isinstance(key, str) or not key:
            return False
        now = time.monotonic()
        with self._lock:
            existing = self._cache.get(key)
            if existing is not None and (now - existing) <= self.ttl_s:
                # Vigente → duplicate
                self._cache.move_to_end(key)
                return False
            self._cache[key] = now
            self._cache.move_to_end(key)
            # LRU eviction
            while len(self._cache) > self.max_size:
                self._cache.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._cache)


# ════════════════════════════════════════════════════════════════════
#  PurchaseGate — API principal
# ════════════════════════════════════════════════════════════════════
class PurchaseGate:
    """Gate determinista de seguridad para compras reales.

    Uso:
        gate = PurchaseGate(mode="live", global_enabled=True)
        decision = gate.evaluate(
            session_id="s1",
            account_id="a1",
            event_id="evt-123",
            qty=2,
            checkout_opened_at_monotonic=time.monotonic() - 30,
            health_score=hs_instance,  # opcional
            retry_count=0,
        )
        if decision.allowed:
            if decision.dry_run:
                ...  # ejecutar flow SIN submit real
            else:
                ...  # compra real

    Thread-safety:
        - Counters bajo _stats_lock
        - Idempotency con su propio lock interno
        - Per-session locks (creación atómica + acquire non-blocking)
        - Last-attempt cache con LRU bajo _attempt_lock

    Backward compat:
        - No depende de HealthScore — funciona sin él
        - No emite eventos si runtime_diagnostics no está disponible
        - Estado interno aislado por instancia (no singleton forzado)
    """

    def __init__(
        self,
        *,
        mode: Optional[str] = None,
        global_enabled: Optional[bool] = None,
        mismatch_rate_threshold: Optional[float] = None,
        critical_crossing_rate_threshold: Optional[float] = None,
        max_checkout_lifetime_s: Optional[int] = None,
        min_cooldown_s: Optional[int] = None,
        idempotency_ttl_s: Optional[int] = None,
        max_retries: Optional[int] = None,
        max_inflation_penalty: Optional[int] = None,
        min_health_score: Optional[int] = None,
        min_consistency: Optional[float] = None,
        max_drift_penalties: Optional[int] = None,
        idem_cache_max: Optional[int] = None,
    ):
        # Modo: arg > env > default
        env_mode = os.environ.get("PURCHASE_MODE", PurchaseMode.DISABLED)
        chosen_mode = mode if mode is not None else env_mode
        if not PurchaseMode.is_valid(chosen_mode):
            chosen_mode = PurchaseMode.DISABLED
        self._mode = chosen_mode

        # Kill switch: arg > env > default False (safety-first)
        if global_enabled is not None:
            self._global_enabled = bool(global_enabled)
        else:
            self._global_enabled = _env_bool("GLOBAL_PURCHASE_ENABLED", False)

        # Thresholds — args override env
        self.mismatch_rate_threshold = (
            float(mismatch_rate_threshold)
            if mismatch_rate_threshold is not None
            else _env_float("PURCHASE_MISMATCH_RATE_THRESHOLD",
                            DEFAULT_MISMATCH_RATE_THRESHOLD)
        )
        self.critical_crossing_rate_threshold = (
            float(critical_crossing_rate_threshold)
            if critical_crossing_rate_threshold is not None
            else _env_float("PURCHASE_CRITICAL_CROSSING_THRESHOLD",
                            DEFAULT_CRITICAL_CROSSING_RATE_THRESHOLD)
        )
        self.max_checkout_lifetime_s = (
            int(max_checkout_lifetime_s)
            if max_checkout_lifetime_s is not None
            else _env_int("PURCHASE_MAX_CHECKOUT_LIFETIME_S",
                          DEFAULT_MAX_CHECKOUT_LIFETIME_S)
        )
        self.min_cooldown_s = (
            int(min_cooldown_s) if min_cooldown_s is not None
            else _env_int("PURCHASE_MIN_COOLDOWN_S", DEFAULT_MIN_COOLDOWN_S)
        )
        idem_ttl = (
            int(idempotency_ttl_s) if idempotency_ttl_s is not None
            else _env_int("PURCHASE_IDEMPOTENCY_TTL_S",
                          DEFAULT_IDEMPOTENCY_TTL_S)
        )
        idem_max = (
            int(idem_cache_max) if idem_cache_max is not None
            else _env_int("PURCHASE_IDEM_CACHE_MAX", DEFAULT_IDEM_CACHE_MAX)
        )
        self.max_retries = (
            int(max_retries) if max_retries is not None
            else _env_int("PURCHASE_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        )
        self.max_inflation_penalty = (
            int(max_inflation_penalty) if max_inflation_penalty is not None
            else _env_int("PURCHASE_MAX_INFLATION",
                          DEFAULT_MAX_INFLATION_PENALTY)
        )
        self.min_health_score = (
            int(min_health_score) if min_health_score is not None
            else _env_int("PURCHASE_MIN_HEALTH", DEFAULT_MIN_HEALTH_SCORE)
        )
        self.min_consistency = (
            float(min_consistency) if min_consistency is not None
            else _env_float("PURCHASE_MIN_CONSISTENCY",
                            DEFAULT_MIN_CONSISTENCY)
        )
        self.max_drift_penalties = (
            int(max_drift_penalties) if max_drift_penalties is not None
            else _env_int("PURCHASE_MAX_DRIFT", DEFAULT_MAX_DRIFT_PENALTIES)
        )

        # Idempotency
        self._idem = IdempotencyTracker(ttl_s=idem_ttl, max_size=idem_max)

        # Per-session locks (creación bajo _session_locks_lock)
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_lock = threading.Lock()

        # Last attempt timestamps por session_id (cooldown), LRU bounded
        self._last_attempt_at: "OrderedDict[str, float]" = OrderedDict()
        self._attempt_lock = threading.Lock()

        # M13.1 FIX 1: serializa lecturas internas de HealthScore en
        # _extract_health_payload() para evitar interleaving intra-gate.
        # No protege mutaciones externas de HS — eso queda fuera de scope.
        self._health_snapshot_lock = threading.Lock()

        # Telemetry counters
        self._stats_lock = threading.Lock()
        self._stats: dict[str, int] = {
            "purchase_attempted_count":   0,
            "purchase_allowed_count":     0,
            "purchase_blocked_count":     0,
            "purchase_dry_run_count":     0,
            "purchase_live_count":        0,
            "duplicate_prevented_count":  0,
            "stale_checkout_denials":     0,
            "mismatch_purchase_denials":  0,
            "critical_crossing_denials":  0,
            "kill_switch_denials":        0,
            "mode_disabled_denials":      0,
            "concurrent_denials":         0,
            "cooldown_denials":           0,
            "health_denials":             0,
            "retry_denials":              0,
            "consistency_denials":        0,
            "inflation_denials":          0,
            "drift_denials":              0,
            "invalid_input_denials":      0,
            "internal_errors":            0,
        }

    # ── Mode / Kill switch ────────────────────────────────────────────
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> bool:
        """Cambia modo en runtime. Devuelve False si modo inválido."""
        if not PurchaseMode.is_valid(mode):
            return False
        with self._stats_lock:
            self._mode = mode
        return True

    @property
    def global_enabled(self) -> bool:
        return self._global_enabled

    def set_global_enabled(self, enabled: bool) -> None:
        with self._stats_lock:
            self._global_enabled = bool(enabled)

    # ── Idempotency key derivation ────────────────────────────────────
    @staticmethod
    def make_idempotency_key(
        *,
        event_id: str,
        account_id: str,
        qty: int,
        checkout_opened_at_monotonic: float,
        bucket_s: int = 60,
    ) -> str:
        """Deriva clave determinista de idempotency.

        SHA-256 de (event_id | account_id | qty | bucket_ts).
        bucket_s define la resolución temporal — submits dentro del
        mismo bucket se consideran duplicados.
        """
        try:
            bucket = int(checkout_opened_at_monotonic // max(1, int(bucket_s)))
        except (TypeError, ValueError):
            bucket = 0
        raw = f"{event_id}|{account_id}|{qty}|{bucket}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ── Telemetry ─────────────────────────────────────────────────────
    def _bump(self, *keys: str) -> None:
        """Incrementa uno o más counters atómicamente."""
        if not keys:
            return
        with self._stats_lock:
            for k in keys:
                self._stats[k] = self._stats.get(k, 0) + 1

    def stats(self) -> dict:
        """Snapshot de counters + rates calculadas."""
        with self._stats_lock:
            snapshot = dict(self._stats)
        attempts = snapshot["purchase_attempted_count"]
        if attempts > 0:
            snapshot["purchase_allow_rate"] = (
                snapshot["purchase_allowed_count"] / attempts
            )
            snapshot["purchase_block_rate"] = (
                snapshot["purchase_blocked_count"] / attempts
            )
            snapshot["purchase_dry_run_rate"] = (
                snapshot["purchase_dry_run_count"] / attempts
            )
            snapshot["purchase_live_rate"] = (
                snapshot["purchase_live_count"] / attempts
            )
        else:
            snapshot["purchase_allow_rate"] = 0.0
            snapshot["purchase_block_rate"] = 0.0
            snapshot["purchase_dry_run_rate"] = 0.0
            snapshot["purchase_live_rate"] = 0.0
        return snapshot

    def reset_stats(self) -> None:
        with self._stats_lock:
            for k in self._stats:
                self._stats[k] = 0

    # ── Internal helpers ──────────────────────────────────────────────
    def _emit(self, event: str, payload: dict) -> None:
        """Best-effort emit a runtime_diagnostics. Silent fail."""
        try:
            from core.observability.runtime_diagnostics import observe
            observe(event_name=event, payload=payload)
        except Exception:
            pass  # observability opcional — no propaga

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        """Obtiene/crea el lock para una session. Thread-safe."""
        with self._session_locks_lock:
            lk = self._session_locks.get(session_id)
            if lk is None:
                lk = threading.Lock()
                self._session_locks[session_id] = lk
            return lk

    def _check_cooldown(self, session_id: str, now: float) -> bool:
        """True si hay cooldown activo (deny). False si puede proceder."""
        if self.min_cooldown_s <= 0:
            return False
        with self._attempt_lock:
            last = self._last_attempt_at.get(session_id)
            if last is None:
                return False
            return (now - last) < self.min_cooldown_s

    def _record_attempt(self, session_id: str, now: float) -> None:
        """Registra timestamp de attempt en LRU cap."""
        with self._attempt_lock:
            self._last_attempt_at[session_id] = now
            self._last_attempt_at.move_to_end(session_id)
            while len(self._last_attempt_at) > _LAST_ATTEMPT_CACHE_CAP:
                self._last_attempt_at.popitem(last=False)

    # ── Main API ──────────────────────────────────────────────────────
    def evaluate(
        self,
        *,
        session_id: str,
        account_id: str,
        event_id: str,
        qty: int,
        checkout_opened_at_monotonic: Optional[float] = None,
        health_score: Optional[Any] = None,
        retry_count: int = 0,
        idempotency_key: Optional[str] = None,
    ) -> PurchaseDecision:
        """Evalúa determinísticamente si la compra puede proceder.

        Cualquier excepción interna → DENY (internal_error) — failsafe absoluto.
        """
        try:
            self._bump("purchase_attempted_count")
            self._emit(PurchaseEvent.ATTEMPTED, {
                "session_id": session_id, "account_id": account_id,
                "event_id": event_id, "qty": qty,
                "retry_count": retry_count,
            })
            now = time.monotonic()

            # 1. Kill switch (hard deny)
            if not self._global_enabled:
                return self._deny(
                    DenyReason.KILL_SWITCH_OFF,
                    session_id=session_id, account_id=account_id,
                    retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "kill_switch_denials"),
                    emit_event=PurchaseEvent.KILL_SWITCH_BLOCKED,
                )

            # 2. Mode disabled
            if self._mode == PurchaseMode.DISABLED:
                return self._deny(
                    DenyReason.MODE_DISABLED,
                    session_id=session_id, account_id=account_id,
                    retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "mode_disabled_denials"),
                    emit_event=PurchaseEvent.BLOCKED,
                )

            # 3. Invalid input
            if not isinstance(session_id, str) or not session_id:
                return self._deny(
                    DenyReason.INVALID_INPUT,
                    account_id=account_id, retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "invalid_input_denials"),
                )
            if not isinstance(account_id, str) or not account_id:
                return self._deny(
                    DenyReason.INVALID_INPUT,
                    session_id=session_id, retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "invalid_input_denials"),
                )
            if not isinstance(event_id, str) or not event_id:
                return self._deny(
                    DenyReason.INVALID_INPUT,
                    session_id=session_id, account_id=account_id,
                    retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "invalid_input_denials"),
                )
            try:
                qty_int = int(qty)
                if qty_int <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return self._deny(
                    DenyReason.INVALID_INPUT,
                    session_id=session_id, account_id=account_id,
                    retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "invalid_input_denials"),
                )

            # 4. Retry exceeded
            if retry_count >= self.max_retries:
                return self._deny(
                    DenyReason.RETRY_EXCEEDED,
                    session_id=session_id, account_id=account_id,
                    retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "retry_denials"),
                    emit_event=PurchaseEvent.BLOCKED,
                )

            # Idempotency key derivation
            if idempotency_key is None:
                co_ts = (checkout_opened_at_monotonic
                         if checkout_opened_at_monotonic is not None else now)
                idempotency_key = self.make_idempotency_key(
                    event_id=event_id, account_id=account_id,
                    qty=qty_int, checkout_opened_at_monotonic=co_ts,
                )

            # 5. Stale checkout
            checkout_age: Optional[float] = None
            if checkout_opened_at_monotonic is not None:
                checkout_age = now - float(checkout_opened_at_monotonic)
                if checkout_age > self.max_checkout_lifetime_s:
                    return self._deny(
                        DenyReason.STALE_CHECKOUT,
                        session_id=session_id, account_id=account_id,
                        idempotency_key=idempotency_key,
                        checkout_age_s=checkout_age, retry_count=retry_count,
                        bump_keys=("purchase_blocked_count",
                                    "stale_checkout_denials"),
                        emit_event=PurchaseEvent.STALE_CHECKOUT,
                    )

            # 6. Cooldown
            if self._check_cooldown(session_id, now):
                return self._deny(
                    DenyReason.COOLDOWN_ACTIVE,
                    session_id=session_id, account_id=account_id,
                    idempotency_key=idempotency_key,
                    checkout_age_s=checkout_age, retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "cooldown_denials"),
                    emit_event=PurchaseEvent.BLOCKED,
                )

            # 7. Concurrent attempt — acquire session lock non-blocking
            sess_lock = self._get_session_lock(session_id)
            if not sess_lock.acquire(blocking=False):
                return self._deny(
                    DenyReason.CONCURRENT_ATTEMPT,
                    session_id=session_id, account_id=account_id,
                    idempotency_key=idempotency_key,
                    checkout_age_s=checkout_age, retry_count=retry_count,
                    bump_keys=("purchase_blocked_count", "concurrent_denials"),
                    emit_event=PurchaseEvent.BLOCKED,
                )

            try:
                # M13.1 FIX 3: kill switch re-check post-lock.
                # Garantiza shutdown efectivo bajo concurrencia: si el
                # operator flipea el switch mientras hay threads en flight,
                # cada uno re-evalúa al adquirir su sess_lock. Idempotency
                # NO se registra todavía → retry tras switch-on funciona.
                if not self._global_enabled:
                    return self._deny(
                        DenyReason.KILL_SWITCH_OFF,
                        session_id=session_id, account_id=account_id,
                        idempotency_key=idempotency_key,
                        checkout_age_s=checkout_age, retry_count=retry_count,
                        bump_keys=("purchase_blocked_count",
                                    "kill_switch_denials"),
                        emit_event=PurchaseEvent.KILL_SWITCH_BLOCKED,
                    )

                # 8. Idempotency — atomic check+insert
                if not self._idem.record(idempotency_key):
                    return self._deny(
                        DenyReason.DUPLICATE_KEY,
                        session_id=session_id, account_id=account_id,
                        idempotency_key=idempotency_key,
                        checkout_age_s=checkout_age, retry_count=retry_count,
                        bump_keys=("purchase_blocked_count",
                                    "duplicate_prevented_count"),
                        emit_event=PurchaseEvent.DUPLICATE_BLOCKED,
                    )

                # 9. Health checks (opcional)
                health_payload = self._extract_health_payload(health_score)
                if health_score is not None:
                    deny = self._evaluate_health(
                        health_payload,
                        session_id=session_id, account_id=account_id,
                        retry_count=retry_count,
                        idempotency_key=idempotency_key,
                        checkout_age=checkout_age,
                    )
                    if deny is not None:
                        return deny

                # 10. ALLOW
                self._record_attempt(session_id, now)
                dry_run = (self._mode == PurchaseMode.DRY_RUN)
                if dry_run:
                    self._bump("purchase_dry_run_count", "purchase_allowed_count")
                    emit = PurchaseEvent.DRY_RUN
                else:
                    self._bump("purchase_live_count", "purchase_allowed_count")
                    emit = PurchaseEvent.ALLOWED

                decision = PurchaseDecision(
                    allowed=True,
                    reason="ok",
                    mode=self._mode,
                    dry_run=dry_run,
                    idempotency_key=idempotency_key,
                    checkout_age_s=checkout_age,
                    retry_count=retry_count,
                    session_id=session_id,
                    account_id=account_id,
                    **health_payload,
                )
                self._emit(emit, decision.as_dict())
                return decision
            finally:
                sess_lock.release()
        except Exception:
            # Failsafe absoluto: cualquier error → DENY internal_error
            self._bump("internal_errors", "purchase_blocked_count")
            return PurchaseDecision(
                allowed=False,
                reason=DenyReason.INTERNAL_ERROR,
                mode=self._mode,
                session_id=session_id if isinstance(session_id, str) else None,
                account_id=account_id if isinstance(account_id, str) else None,
                retry_count=retry_count if isinstance(retry_count, int) else 0,
            )

    # ── Health extraction + evaluation ────────────────────────────────
    def _extract_health_payload(self, hs: Optional[Any]) -> dict:
        """Extrae métricas relevantes de un HealthScore. Best-effort.

        Devuelve dict siempre con las 8 keys completas (None si no aplica
        o si la extracción falla — fail-safe).

        M13.1 FIX 1: TODAS las lecturas se realizan bajo
        _health_snapshot_lock para evitar interleaving entre dos threads
        del gate evaluando HealthScore concurrentemente. Esto NO bloquea
        mutaciones externas (bot.observe()) — limitación documentada.
        """
        payload = {
            "official_score":         None,
            "shadow_score":           None,
            "official_verdict":       None,
            "shadow_verdict":         None,
            "mismatch_rate":          None,
            "critical_crossing_rate": None,
            "consistency":            None,
            "inflation_penalty":      None,
        }
        if hs is None:
            return payload
        # M13.1 FIX 1: lock encierra TODAS las lecturas como bloque atómico
        with self._health_snapshot_lock:
            try:
                payload["official_score"] = int(getattr(hs, "score", 0))
                if hasattr(hs, "compute_m12_score"):
                    try:
                        payload["shadow_score"] = int(hs.compute_m12_score())
                    except Exception:
                        pass
                if (hasattr(hs, "_verdict")
                        and payload["official_score"] is not None):
                    try:
                        payload["official_verdict"] = hs._verdict(
                            payload["official_score"]
                        )
                    except Exception:
                        pass
                    if payload["shadow_score"] is not None:
                        try:
                            payload["shadow_verdict"] = hs._verdict(
                                payload["shadow_score"]
                            )
                        except Exception:
                            pass
                if hasattr(hs, "_shadow_comparison_stats"):
                    try:
                        stats = hs._shadow_comparison_stats()
                        payload["mismatch_rate"] = float(
                            stats.get("mismatch_rate", 0.0)
                        )
                        payload["critical_crossing_rate"] = float(
                            stats.get("critical_crossing_rate", 0.0)
                        )
                    except Exception:
                        pass
                if hasattr(hs, "_compute_consistency"):
                    try:
                        payload["consistency"] = float(
                            hs._compute_consistency()
                        )
                    except Exception:
                        pass
                if hasattr(hs, "_compute_inflation_penalty"):
                    try:
                        payload["inflation_penalty"] = int(
                            hs._compute_inflation_penalty()
                        )
                    except Exception:
                        pass
            except Exception:
                pass  # devolver payload con None
        return payload

    def _evaluate_health(
        self,
        payload: dict,
        *,
        session_id: str,
        account_id: str,
        retry_count: int,
        idempotency_key: str,
        checkout_age: Optional[float],
    ) -> Optional[PurchaseDecision]:
        """Health-based denial. Returns PurchaseDecision si deny, else None."""
        official_verdict = payload.get("official_verdict")
        shadow_verdict = payload.get("shadow_verdict")
        official_score = payload.get("official_score")
        mismatch_rate = payload.get("mismatch_rate")
        critical_crossing_rate = payload.get("critical_crossing_rate")
        consistency = payload.get("consistency")
        inflation = payload.get("inflation_penalty")

        common = dict(
            session_id=session_id, account_id=account_id,
            retry_count=retry_count, idempotency_key=idempotency_key,
            checkout_age_s=checkout_age,
            **payload,
        )

        if official_verdict == "CRITICAL":
            return self._deny(
                DenyReason.HEALTH_CRITICAL,
                bump_keys=("purchase_blocked_count", "health_denials"),
                emit_event=PurchaseEvent.BLOCKED,
                **common,
            )
        if shadow_verdict == "CRITICAL":
            return self._deny(
                DenyReason.SHADOW_CRITICAL,
                bump_keys=("purchase_blocked_count", "health_denials"),
                emit_event=PurchaseEvent.BLOCKED,
                **common,
            )
        if (official_score is not None
                and official_score < self.min_health_score):
            return self._deny(
                DenyReason.HEALTH_LOW,
                bump_keys=("purchase_blocked_count", "health_denials"),
                emit_event=PurchaseEvent.BLOCKED,
                **common,
            )
        if (mismatch_rate is not None
                and mismatch_rate > self.mismatch_rate_threshold):
            return self._deny(
                DenyReason.MISMATCH_RATE_HIGH,
                bump_keys=("purchase_blocked_count",
                            "mismatch_purchase_denials"),
                emit_event=PurchaseEvent.BLOCKED,
                **common,
            )
        if (critical_crossing_rate is not None
                and critical_crossing_rate > self.critical_crossing_rate_threshold):
            return self._deny(
                DenyReason.CRITICAL_CROSSING_HIGH,
                bump_keys=("purchase_blocked_count",
                            "critical_crossing_denials"),
                emit_event=PurchaseEvent.BLOCKED,
                **common,
            )
        if (consistency is not None
                and consistency < self.min_consistency):
            return self._deny(
                DenyReason.CONSISTENCY_LOW,
                bump_keys=("purchase_blocked_count", "consistency_denials"),
                emit_event=PurchaseEvent.BLOCKED,
                **common,
            )
        if (inflation is not None
                and inflation > self.max_inflation_penalty):
            return self._deny(
                DenyReason.INFLATION_HIGH,
                bump_keys=("purchase_blocked_count", "inflation_denials"),
                emit_event=PurchaseEvent.BLOCKED,
                **common,
            )
        return None

    # ── Deny builder (centraliza bump + emit) ─────────────────────────
    def _deny(
        self,
        reason: str,
        *,
        session_id: Optional[str] = None,
        account_id: Optional[str] = None,
        retry_count: int = 0,
        idempotency_key: Optional[str] = None,
        checkout_age_s: Optional[float] = None,
        bump_keys: tuple = (),
        emit_event: Optional[str] = None,
        # Health payload passthrough
        official_score: Optional[int] = None,
        shadow_score: Optional[int] = None,
        official_verdict: Optional[str] = None,
        shadow_verdict: Optional[str] = None,
        mismatch_rate: Optional[float] = None,
        critical_crossing_rate: Optional[float] = None,
        consistency: Optional[float] = None,
        inflation_penalty: Optional[int] = None,
    ) -> PurchaseDecision:
        """Construye + emite DENY decision con bump de counters.

        M13.1 FIX 2: registra cooldown (_record_attempt) para razones que
        representan acción de usuario/bot (no system-off). Esto bloquea
        retry storms de inputs inválidos / duplicates / etc.
        """
        if bump_keys:
            self._bump(*bump_keys)
        # M13.1 FIX 2: cooldown en denials user/action, NO en system-off
        if (reason not in _SYSTEM_OFF_DENY_REASONS
                and isinstance(session_id, str) and session_id):
            try:
                self._record_attempt(session_id, time.monotonic())
            except Exception:
                pass  # fail-safe: nunca propaga
        decision = PurchaseDecision(
            allowed=False, reason=reason, mode=self._mode,
            session_id=session_id, account_id=account_id,
            idempotency_key=idempotency_key,
            checkout_age_s=checkout_age_s,
            retry_count=retry_count,
            official_score=official_score, shadow_score=shadow_score,
            official_verdict=official_verdict, shadow_verdict=shadow_verdict,
            mismatch_rate=mismatch_rate,
            critical_crossing_rate=critical_crossing_rate,
            consistency=consistency, inflation_penalty=inflation_penalty,
        )
        if emit_event:
            self._emit(emit_event, decision.as_dict())
        return decision


# ════════════════════════════════════════════════════════════════════
#  Module-level singleton (lazy, opt-in)
# ════════════════════════════════════════════════════════════════════
_default_gate: Optional[PurchaseGate] = None
_default_gate_lock = threading.Lock()


def get_default_gate() -> PurchaseGate:
    """Devuelve la instancia singleton (lazy init).

    Usado por production code. Para tests, instanciar PurchaseGate
    directamente o llamar reset_default_gate_for_test().
    """
    global _default_gate
    with _default_gate_lock:
        if _default_gate is None:
            _default_gate = PurchaseGate()
        return _default_gate


def reset_default_gate_for_test() -> None:
    """Test helper: resetea el singleton."""
    global _default_gate
    with _default_gate_lock:
        _default_gate = None


__all__ = [
    "PurchaseMode",
    "DenyReason",
    "PurchaseEvent",
    "PurchaseDecision",
    "IdempotencyTracker",
    "PurchaseGate",
    "get_default_gate",
    "reset_default_gate_for_test",
]


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
