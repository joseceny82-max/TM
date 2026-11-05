"""core/reliability/ — Reliability primitives (STEP 8+).

Circuit breakers, recovery controllers, backoff policies.

OPT-IN: estos primitivos NO afectan al bot legacy a menos que se
invoquen explícitamente. Pure additive.
"""
from core.reliability.circuit_breakers import (
    DEFAULT_BACKOFF_BASE_SEC,
    DEFAULT_BACKOFF_MAX_SEC,
    DEFAULT_BACKOFF_MULTIPLIER,
    DEFAULT_COOLDOWN_SEC,
    DEFAULT_FAILURE_THRESHOLD,
    RESOURCE_ACCOUNT,
    RESOURCE_EVENT,
    RESOURCE_SUBNET,
    VALID_RESOURCE_TYPES,
    BackoffPolicy,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
    RecoveryController,
    get_default_registry,
    reset_default_registry,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitState",
    "BackoffPolicy",
    "RecoveryController",
    "get_default_registry",
    "reset_default_registry",
    "RESOURCE_ACCOUNT",
    "RESOURCE_SUBNET",
    "RESOURCE_EVENT",
    "VALID_RESOURCE_TYPES",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_COOLDOWN_SEC",
    "DEFAULT_BACKOFF_BASE_SEC",
    "DEFAULT_BACKOFF_MAX_SEC",
    "DEFAULT_BACKOFF_MULTIPLIER",
]
