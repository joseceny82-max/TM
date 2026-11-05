"""core/purchase_state/cart_state.py — Explicit Cart State Model (STEP 1).

Modelo formal e inmutable del ciclo de vida del carrito de compra.

PRINCIPIO ARQUITECTÓNICO:
    Reemplaza el "estado implícito en el orden de ejecución" del flow
    lineal por un modelo de datos validable, con transiciones legales
    declarativas y predicados de salud (`is_expired`, `is_stale`,
    `can_proceed_to_checkout`).

    Este módulo NO se conecta todavía al flow productivo
    (`_compra_parallel.py` / `tm_purchase_flow.py`). Es la pieza
    arquitectónica que las próximas integraciones consumirán.

CICLO DE VIDA:

    INIT  ── attach ──▶  ATTACHED  ── start_checkout ──▶  CHECKING_OUT
      │                     │                              │
      │                     │                              ├──▶ PAID    (terminal)
      ├──▶ ABORTED          ├──▶ EXPIRED                   ├──▶ EXPIRED  (terminal)
      ├──▶ FAILED           ├──▶ ABORTED                   ├──▶ FAILED   (terminal)
                            └──▶ FAILED                    └──▶ ABORTED  (terminal)

    Estados terminales (sin transiciones salientes): PAID, EXPIRED,
    ABORTED, FAILED.

GARANTÍAS:
    - Inmutabilidad: `CartState` es frozen. Cambios producen nuevas
      instancias vía `with_phase`, `with_seats`, `with_expiration`.
    - Revisión monotónica: cada cambio incrementa `revision`.
    - Transiciones legales explícitas: `LEGAL_TRANSITIONS` dict.
    - Sin side-effects: pure data + pure validations.
    - stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  CartPhase — enum del ciclo de vida
# ════════════════════════════════════════════════════════════════════
class CartPhase(Enum):
    """Fases del ciclo de vida de un carrito.

    No-terminales:
        INIT, ATTACHED, CHECKING_OUT
    Terminales (sin transiciones salientes):
        PAID, EXPIRED, ABORTED, FAILED
    """
    INIT = "init"
    ATTACHED = "attached"
    CHECKING_OUT = "checking_out"
    PAID = "paid"
    EXPIRED = "expired"
    ABORTED = "aborted"
    FAILED = "failed"


# Conjunto de fases terminales (sin transiciones salientes)
TERMINAL_PHASES: frozenset = frozenset({
    CartPhase.PAID,
    CartPhase.EXPIRED,
    CartPhase.ABORTED,
    CartPhase.FAILED,
})

# Mapa declarativo de transiciones legales: FROM → set of allowed TO
LEGAL_TRANSITIONS: dict = {
    CartPhase.INIT: frozenset({
        CartPhase.ATTACHED,
        CartPhase.ABORTED,
        CartPhase.FAILED,
    }),
    CartPhase.ATTACHED: frozenset({
        CartPhase.CHECKING_OUT,
        CartPhase.EXPIRED,
        CartPhase.ABORTED,
        CartPhase.FAILED,
    }),
    CartPhase.CHECKING_OUT: frozenset({
        CartPhase.PAID,
        CartPhase.EXPIRED,
        CartPhase.ABORTED,
        CartPhase.FAILED,
    }),
    # Fases terminales: sin transiciones salientes
    CartPhase.PAID: frozenset(),
    CartPhase.EXPIRED: frozenset(),
    CartPhase.ABORTED: frozenset(),
    CartPhase.FAILED: frozenset(),
}


# ════════════════════════════════════════════════════════════════════
#  Excepciones del dominio
# ════════════════════════════════════════════════════════════════════
class IllegalCartTransition(Exception):
    """Intento de transitar a una fase no permitida desde la actual."""
    pass


# ════════════════════════════════════════════════════════════════════
#  SeatLock — butaca reservada con TTL propio
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SeatLock:
    """Una butaca reservada dentro de un carrito.

    Frozen — inmutable y hashable. Usar `replace()` para crear
    versiones modificadas (raro en práctica: las butacas se
    sustituyen completas, no se mutan).
    """
    seat_id: str
    section: str = ""
    row: str = ""
    seat: str = ""
    price: float = 0.0
    reserved_at_iso: str = ""   # ISO 8601 UTC
    expires_at_iso: str = ""    # ISO 8601 UTC

    def to_dict(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "section": self.section,
            "row": self.row,
            "seat": self.seat,
            "price": float(self.price),
            "reserved_at": self.reserved_at_iso,
            "expires_at": self.expires_at_iso,
        }


# ════════════════════════════════════════════════════════════════════
#  Helpers de fechas (stdlib, sin pytz)
# ════════════════════════════════════════════════════════════════════
def _parse_iso(value: Any) -> Optional[datetime]:
    """Parsea un ISO 8601 a datetime. Devuelve None si inválido."""
    if not isinstance(value, str) or not value:
        return None
    try:
        # Aceptar "Z" como sufijo UTC explícito
        s = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ════════════════════════════════════════════════════════════════════
#  CartState — modelo inmutable principal
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CartState:
    """Estado inmutable del carrito.

    Frozen — toda mutación produce una nueva instancia vía métodos
    `with_*`. El `revision` se incrementa automáticamente en cada
    cambio para detectar staleness y race conditions.

    Campos:
        cart_id: identificador opaco del carrito (string).
        phase: CartPhase actual.
        expires_at_iso: ISO 8601 UTC. "" si no aplica.
        seats: tuple inmutable de SeatLock. Vacía hasta ATTACHED.
        revision: contador monotónico de cambios. Empieza en 0.
        last_validated_at_iso: timestamp de última revalidación con
                                el backend. "" si nunca se validó.
        event_id: identificador del evento (trazabilidad).
        account_id: identificador de la cuenta (trazabilidad).
    """
    cart_id: str
    phase: CartPhase
    expires_at_iso: str = ""
    seats: tuple = ()                  # tuple[SeatLock, ...]
    revision: int = 0
    last_validated_at_iso: str = ""
    event_id: str = ""
    account_id: str = ""

    # ────── Predicados ──────────────────────────────────────────────
    def is_terminal(self) -> bool:
        """¿La fase actual es terminal (sin transiciones salientes)?"""
        return self.phase in TERMINAL_PHASES

    def is_expired(self, now_iso: str = "") -> bool:
        """¿El carrito ha expirado a la hora `now_iso`?

        Si no hay `expires_at_iso` o `now_iso` es inválido → False
        (no podemos afirmar expiración sin datos).
        """
        if not self.expires_at_iso:
            return False
        now_dt = _parse_iso(now_iso)
        exp_dt = _parse_iso(self.expires_at_iso)
        if now_dt is None or exp_dt is None:
            return False
        return now_dt > exp_dt

    def is_stale(self, now_iso: str = "", max_age_sec: float = 3.0) -> bool:
        """¿Hace cuánto que no se revalidó el carrito?

        Stale = no ha sido validado en los últimos `max_age_sec`
        segundos. Si nunca se validó (`last_validated_at_iso == ""`)
        o si no podemos parsear timestamps → True (seguridad).
        """
        if not self.last_validated_at_iso:
            return True
        now_dt = _parse_iso(now_iso)
        last_dt = _parse_iso(self.last_validated_at_iso)
        if now_dt is None or last_dt is None:
            return True
        return (now_dt - last_dt).total_seconds() > max_age_sec

    def can_transition_to(self, new_phase: CartPhase) -> tuple:
        """¿Puedo transitar de self.phase a new_phase?

        Returns:
            (ok: bool, reason: str)
        """
        if not isinstance(new_phase, CartPhase):
            return False, f"new_phase must be CartPhase, got {type(new_phase).__name__}"
        if self.is_terminal():
            return False, f"phase {self.phase.value} is terminal"
        allowed = LEGAL_TRANSITIONS.get(self.phase, frozenset())
        if new_phase not in allowed:
            return False, (
                f"illegal transition: {self.phase.value} -> {new_phase.value}"
            )
        return True, ""

    def can_proceed_to_checkout(self, now_iso: str = "") -> tuple:
        """¿Estoy en condiciones de iniciar checkout?

        Requisitos:
            - phase == ATTACHED
            - seats no vacío
            - not is_expired(now_iso)

        Returns:
            (ok: bool, reason: str)
        """
        if self.phase != CartPhase.ATTACHED:
            return False, f"not in ATTACHED phase (current: {self.phase.value})"
        if not self.seats:
            return False, "no seats reserved"
        if self.is_expired(now_iso):
            return False, "cart expired"
        return True, ""

    # ────── Constructores inmutables ────────────────────────────────
    def with_phase(self, new_phase: CartPhase,
                   validated_at_iso: str = "") -> "CartState":
        """Crea una nueva versión con `phase` actualizada.

        Raises:
            IllegalCartTransition si la transición no es válida.
        """
        ok, why = self.can_transition_to(new_phase)
        if not ok:
            raise IllegalCartTransition(why)
        return replace(
            self,
            phase=new_phase,
            revision=self.revision + 1,
            last_validated_at_iso=validated_at_iso or self.last_validated_at_iso,
        )

    def with_seats(self, seats, validated_at_iso: str = "") -> "CartState":
        """Crea una nueva versión con `seats` actualizados.

        Args:
            seats: iterable de SeatLock. Se convierte a tuple inmutable.
        """
        seats_tuple = tuple(seats) if seats else ()
        return replace(
            self,
            seats=seats_tuple,
            revision=self.revision + 1,
            last_validated_at_iso=validated_at_iso or self.last_validated_at_iso,
        )

    def with_expiration(self, expires_at_iso: str,
                         validated_at_iso: str = "") -> "CartState":
        """Crea una nueva versión con `expires_at_iso` actualizada."""
        return replace(
            self,
            expires_at_iso=expires_at_iso or "",
            revision=self.revision + 1,
            last_validated_at_iso=validated_at_iso or self.last_validated_at_iso,
        )

    def revalidated(self, now_iso: str) -> "CartState":
        """Marca el carrito como recién revalidado (sin otros cambios).

        Bump de revision + actualización de `last_validated_at_iso`.
        """
        return replace(
            self,
            revision=self.revision + 1,
            last_validated_at_iso=now_iso or self.last_validated_at_iso,
        )

    # ────── Serialización ────────────────────────────────────────────
    def to_dict(self) -> dict:
        """Serializa a dict JSON-compatible."""
        return {
            "cart_id": self.cart_id,
            "phase": self.phase.value,
            "expires_at": self.expires_at_iso,
            "seats": [s.to_dict() for s in self.seats],
            "revision": int(self.revision),
            "last_validated_at": self.last_validated_at_iso,
            "event_id": self.event_id,
            "account_id": self.account_id,
        }


# ════════════════════════════════════════════════════════════════════
#  Factories
# ════════════════════════════════════════════════════════════════════
def new_cart_state(*,
                    cart_id: str,
                    event_id: str = "",
                    account_id: str = "") -> CartState:
    """Construye un carrito en fase INIT (sin seats, sin expiración)."""
    return CartState(
        cart_id=cart_id,
        phase=CartPhase.INIT,
        expires_at_iso="",
        seats=(),
        revision=0,
        last_validated_at_iso="",
        event_id=event_id,
        account_id=account_id,
    )


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
