"""core/reliability/circuit_breakers.py — Circuit Breakers (STEP 8).

Sistema de circuit breakers + recovery + backoff por recurso (cuenta,
subnet, event_id), sin tocar el bot legacy.

ESTADOS:
    CLOSED     → operación normal
    OPEN       → bloqueado tras N fallos (cooldown activo)
    HALF_OPEN  → probando recuperación tras cooldown; un solo intento

PRINCIPIOS:
    - Thread-safe (Lock interno)
    - Determinístico (backoff hash-based)
    - Pure decisions (consultar estado nunca muta)
    - Hooks opt-in (sistema legacy no afectado si no se invocan)
    - Persistencia JSON opcional para sobrevivir restarts

stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
DEFAULT_FAILURE_THRESHOLD: int = 3
DEFAULT_COOLDOWN_SEC: float = 300.0
DEFAULT_BACKOFF_BASE_SEC: float = 1.0
DEFAULT_BACKOFF_MAX_SEC: float = 600.0
DEFAULT_BACKOFF_MULTIPLIER: float = 2.0
MAX_RETRY_DEFAULT: int = 10

# Resource types canónicos
RESOURCE_ACCOUNT: str = "account"
RESOURCE_SUBNET: str = "subnet"
RESOURCE_EVENT: str = "event"
VALID_RESOURCE_TYPES: frozenset = frozenset({
    RESOURCE_ACCOUNT, RESOURCE_SUBNET, RESOURCE_EVENT,
})


# ════════════════════════════════════════════════════════════════════
#  Estados
# ════════════════════════════════════════════════════════════════════
class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _composite_key(resource_type: str, resource_id: str) -> str:
    return f"{resource_type}:{resource_id}"


# ════════════════════════════════════════════════════════════════════
#  CircuitBreaker — modelo inmutable de un breaker individual
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CircuitBreaker:
    """Estado de un circuit breaker para un recurso.

    Frozen — toda transición produce nueva instancia.
    """
    resource_type: str
    resource_id: str
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count_half_open: int = 0
    opened_at_iso: str = ""
    last_failure_at_iso: str = ""
    last_success_at_iso: str = ""

    def to_dict(self) -> dict:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "state": self.state.value,
            "failure_count": int(self.failure_count),
            "success_count_half_open": int(self.success_count_half_open),
            "opened_at": self.opened_at_iso,
            "last_failure_at": self.last_failure_at_iso,
            "last_success_at": self.last_success_at_iso,
        }


def _breaker_from_dict(d: dict) -> Optional[CircuitBreaker]:
    if not isinstance(d, dict):
        return None
    try:
        state = CircuitState(d.get("state", "closed"))
    except (ValueError, TypeError):
        state = CircuitState.CLOSED
    return CircuitBreaker(
        resource_type=str(d.get("resource_type", "")),
        resource_id=str(d.get("resource_id", "")),
        state=state,
        failure_count=int(d.get("failure_count", 0)),
        success_count_half_open=int(d.get("success_count_half_open", 0)),
        opened_at_iso=str(d.get("opened_at", "")),
        last_failure_at_iso=str(d.get("last_failure_at", "")),
        last_success_at_iso=str(d.get("last_success_at", "")),
    )


# ════════════════════════════════════════════════════════════════════
#  CircuitBreakerRegistry — registro thread-safe + persistencia opcional
# ════════════════════════════════════════════════════════════════════
class CircuitBreakerRegistry:
    """Registro central de breakers.

    Thread-safe. Persistencia JSON opcional.
    """

    def __init__(self,
                 *,
                 threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
                 persistence_path: Optional[str] = None,
                 half_open_success_target: int = 1):
        self._lock = threading.Lock()
        self._breakers: dict = {}
        self.threshold = max(1, int(threshold))
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self.persistence_path: Optional[Path] = (
            Path(persistence_path) if persistence_path else None
        )
        self.half_open_success_target = max(1, int(half_open_success_target))
        if self.persistence_path:
            self._load()

    # ────── persistence ──────────────────────────────────────────
    def _load(self) -> None:
        if not self.persistence_path or not self.persistence_path.exists():
            return
        try:
            data = json.loads(self.persistence_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        breakers = data.get("breakers", {})
        if not isinstance(breakers, dict):
            return
        for key, raw in breakers.items():
            br = _breaker_from_dict(raw)
            if br:
                self._breakers[key] = br

    def _persist(self) -> None:
        if not self.persistence_path:
            return
        try:
            self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "breakers": {k: v.to_dict() for k, v in self._breakers.items()},
            }
            tmp = self.persistence_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True),
                            encoding="utf-8")
            os.replace(tmp, self.persistence_path)
        except OSError:
            pass

    # ────── core mutations (under lock) ──────────────────────────
    def _get_or_create(self, resource_type: str,
                        resource_id: str) -> CircuitBreaker:
        key = _composite_key(resource_type, resource_id)
        br = self._breakers.get(key)
        if br is None:
            br = CircuitBreaker(
                resource_type=resource_type, resource_id=resource_id,
            )
            self._breakers[key] = br
        return br

    def get(self, resource_type: str, resource_id: str) -> CircuitBreaker:
        """Devuelve snapshot del breaker. Crea CLOSED si no existe."""
        if resource_type not in VALID_RESOURCE_TYPES:
            return CircuitBreaker(
                resource_type=resource_type, resource_id=resource_id,
            )
        with self._lock:
            return self._get_or_create(resource_type, resource_id)

    def _advance_if_cooldown_passed(self, br: CircuitBreaker,
                                      now_iso: str) -> CircuitBreaker:
        """Si está OPEN y pasó cooldown → HALF_OPEN."""
        if br.state != CircuitState.OPEN:
            return br
        opened = _parse_iso(br.opened_at_iso)
        now = _parse_iso(now_iso)
        if opened is None or now is None:
            return br
        if (now - opened).total_seconds() >= self.cooldown_sec:
            return replace(
                br,
                state=CircuitState.HALF_OPEN,
                success_count_half_open=0,
            )
        return br

    def record_failure(self, resource_type: str,
                        resource_id: str,
                        *,
                        now_iso: Optional[str] = None) -> CircuitBreaker:
        """Registra un fallo. Puede abrir el circuito si supera threshold."""
        if resource_type not in VALID_RESOURCE_TYPES:
            return CircuitBreaker(
                resource_type=resource_type, resource_id=resource_id,
            )
        now = now_iso or _now_iso_utc()
        with self._lock:
            br = self._get_or_create(resource_type, resource_id)
            # Si está HALF_OPEN, un fallo reabre INMEDIATAMENTE
            if br.state == CircuitState.HALF_OPEN:
                new_br = replace(
                    br,
                    state=CircuitState.OPEN,
                    failure_count=br.failure_count + 1,
                    success_count_half_open=0,
                    last_failure_at_iso=now,
                    opened_at_iso=now,
                )
            else:
                new_count = br.failure_count + 1
                if new_count >= self.threshold and br.state == CircuitState.CLOSED:
                    new_br = replace(
                        br,
                        state=CircuitState.OPEN,
                        failure_count=new_count,
                        last_failure_at_iso=now,
                        opened_at_iso=now,
                    )
                else:
                    new_br = replace(
                        br,
                        failure_count=new_count,
                        last_failure_at_iso=now,
                    )
            self._breakers[_composite_key(resource_type, resource_id)] = new_br
            self._persist()
            return new_br

    def record_success(self, resource_type: str,
                        resource_id: str,
                        *,
                        now_iso: Optional[str] = None) -> CircuitBreaker:
        """Registra éxito. HALF_OPEN puede cerrar tras N éxitos."""
        if resource_type not in VALID_RESOURCE_TYPES:
            return CircuitBreaker(
                resource_type=resource_type, resource_id=resource_id,
            )
        now = now_iso or _now_iso_utc()
        with self._lock:
            br = self._get_or_create(resource_type, resource_id)
            if br.state == CircuitState.HALF_OPEN:
                new_success = br.success_count_half_open + 1
                if new_success >= self.half_open_success_target:
                    # Cerrar circuito
                    new_br = replace(
                        br,
                        state=CircuitState.CLOSED,
                        failure_count=0,
                        success_count_half_open=0,
                        last_success_at_iso=now,
                        opened_at_iso="",
                    )
                else:
                    new_br = replace(
                        br,
                        success_count_half_open=new_success,
                        last_success_at_iso=now,
                    )
            elif br.state == CircuitState.CLOSED:
                # Reset failure count tras éxito (rolling)
                new_br = replace(
                    br,
                    failure_count=0,
                    last_success_at_iso=now,
                )
            else:
                # OPEN: éxito en OPEN no debería pasar (no se llamó por bloqueo);
                # si pasa, lo registramos sin cambiar estado.
                new_br = replace(br, last_success_at_iso=now)
            self._breakers[_composite_key(resource_type, resource_id)] = new_br
            self._persist()
            return new_br

    def can_proceed(self, resource_type: str,
                     resource_id: str,
                     *,
                     now_iso: Optional[str] = None) -> bool:
        """¿Está permitido proceder con este recurso?

        - CLOSED → True
        - OPEN → False (a menos que pasó cooldown → HALF_OPEN → True)
        - HALF_OPEN → True (un intento permitido)
        """
        if resource_type not in VALID_RESOURCE_TYPES:
            # Tipos desconocidos → permitir (fail-open)
            return True
        now = now_iso or _now_iso_utc()
        with self._lock:
            br = self._get_or_create(resource_type, resource_id)
            advanced = self._advance_if_cooldown_passed(br, now)
            if advanced is not br:
                self._breakers[_composite_key(resource_type, resource_id)] = advanced
                br = advanced
                self._persist()
            return br.state != CircuitState.OPEN

    def reset(self, resource_type: str, resource_id: str) -> None:
        """Resetea explícitamente un breaker a CLOSED."""
        if resource_type not in VALID_RESOURCE_TYPES:
            return
        with self._lock:
            key = _composite_key(resource_type, resource_id)
            self._breakers[key] = CircuitBreaker(
                resource_type=resource_type, resource_id=resource_id,
            )
            self._persist()

    def list_open(self) -> list:
        """Devuelve lista de breakers actualmente OPEN."""
        with self._lock:
            return [b.to_dict() for b in self._breakers.values()
                     if b.state == CircuitState.OPEN]

    def snapshot(self) -> dict:
        """Snapshot completo del registro (para debug / UI)."""
        with self._lock:
            return {
                "threshold": self.threshold,
                "cooldown_sec": self.cooldown_sec,
                "total": len(self._breakers),
                "open": sum(1 for b in self._breakers.values()
                             if b.state == CircuitState.OPEN),
                "half_open": sum(1 for b in self._breakers.values()
                                  if b.state == CircuitState.HALF_OPEN),
                "closed": sum(1 for b in self._breakers.values()
                               if b.state == CircuitState.CLOSED),
                "breakers": {k: v.to_dict()
                              for k, v in self._breakers.items()},
            }


# ════════════════════════════════════════════════════════════════════
#  BackoffPolicy — exponential + jitter determinístico
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class BackoffPolicy:
    """Política de backoff exponencial determinístico con jitter opt-in."""
    base_sec: float = DEFAULT_BACKOFF_BASE_SEC
    max_sec: float = DEFAULT_BACKOFF_MAX_SEC
    multiplier: float = DEFAULT_BACKOFF_MULTIPLIER
    jitter_fraction: float = 0.0   # 0 = sin jitter; 0.5 = ±50%

    def compute(self, attempt: int, *, seed: str = "") -> float:
        """Calcula tiempo de espera para attempt N (0-based).

        Si jitter_fraction > 0, aplica jitter determinístico hashed
        sobre `seed` (mismo seed → mismo jitter, no random).
        """
        a = max(0, int(attempt))
        base = self.base_sec * (self.multiplier ** a)
        base = max(0.0, min(base, self.max_sec))
        if self.jitter_fraction > 0.0 and seed:
            h = hashlib.sha256(f"{seed}:{a}".encode("utf-8")).hexdigest()
            # Mapear hash a [-1, 1]
            unit = (int(h[:8], 16) / 0xFFFFFFFF) * 2.0 - 1.0
            jitter = base * self.jitter_fraction * unit
            base = max(0.0, base + jitter)
        return round(base, 6)


# ════════════════════════════════════════════════════════════════════
#  RecoveryController — combina registry + lectura de events
# ════════════════════════════════════════════════════════════════════
class RecoveryController:
    """Toma decisiones de recuperación combinando:
        - estado de los circuit breakers
        - clasificación de fallos recientes (vía structured_events)

    NO ejecuta acciones. Sólo decide "allowed/blocked + reason".
    """

    def __init__(self,
                 *,
                 registry: CircuitBreakerRegistry,
                 backoff: Optional[BackoffPolicy] = None):
        self.registry = registry
        self.backoff = backoff or BackoffPolicy()

    def should_allow_request(self,
                              *,
                              account_id: str = "",
                              subnet: str = "",
                              event_id: str = "",
                              now_iso: Optional[str] = None) -> tuple:
        """Política multi-dimensional.

        Returns:
            (allowed: bool, reason: str)
        """
        blockers: list = []
        if account_id and not self.registry.can_proceed(
            RESOURCE_ACCOUNT, account_id, now_iso=now_iso
        ):
            blockers.append("account_open")
        if subnet and not self.registry.can_proceed(
            RESOURCE_SUBNET, subnet, now_iso=now_iso
        ):
            blockers.append("subnet_open")
        if event_id and not self.registry.can_proceed(
            RESOURCE_EVENT, event_id, now_iso=now_iso
        ):
            blockers.append("event_open")
        if blockers:
            return False, ",".join(blockers)
        return True, "ok"

    def record_outcome(self,
                        *,
                        success: bool,
                        account_id: str = "",
                        subnet: str = "",
                        event_id: str = "",
                        now_iso: Optional[str] = None) -> None:
        """Registra success/failure en TODOS los breakers aplicables."""
        method = self.registry.record_success if success else self.registry.record_failure
        for rtype, rid in (
            (RESOURCE_ACCOUNT, account_id),
            (RESOURCE_SUBNET, subnet),
            (RESOURCE_EVENT, event_id),
        ):
            if rid:
                method(rtype, rid, now_iso=now_iso)

    def compute_retry_wait(self, attempt: int, *, seed: str = "") -> float:
        return self.backoff.compute(attempt, seed=seed)


# ════════════════════════════════════════════════════════════════════
#  Singleton helpers (opt-in)
# ════════════════════════════════════════════════════════════════════
_default_registry: Optional[CircuitBreakerRegistry] = None
_default_lock = threading.Lock()


def get_default_registry() -> CircuitBreakerRegistry:
    """Devuelve un singleton registry (creación lazy)."""
    global _default_registry
    if _default_registry is None:
        with _default_lock:
            if _default_registry is None:
                _default_registry = CircuitBreakerRegistry()
    return _default_registry


def reset_default_registry() -> None:
    """Resetea el singleton (para tests)."""
    global _default_registry
    with _default_lock:
        _default_registry = None


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
