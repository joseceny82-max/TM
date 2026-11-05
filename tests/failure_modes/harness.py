"""tests/failure_modes/harness.py — Mock backend + reference flow driver.

Componentes:
    - MockBackend       : simula respuestas tipo TM con inyección de fallos
    - ReferenceFlowDriver: consume CartState + idempotency_log como
                          ejemplo ejecutable del flow refactorizado

Diseño:
    Pure Python, stdlib only. Sin tocar producción.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.idempotency import (
    load_record,
    make_idempotency_key,
    scan_orphan_intents,
    write_intent,
    write_outcome,
)
from core.purchase_state import (
    CartPhase,
    CartState,
    SeatLock,
    new_cart_state,
)


# ════════════════════════════════════════════════════════════════════
#  MockBackend — simulador de TM con inyección de fallos
# ════════════════════════════════════════════════════════════════════
class MockBackend:
    """Simula un backend tipo Ticketmaster con failure injection.

    Modos de fallo soportados por step (via `inject(step, ...)`):
        attach_cart:
            - fail=True                                   → respuesta error genérico
            - inventory_changed=True                      → seats ya no disponibles
            - ttl=<int seconds>                           → expiración corta
        submit_payment:
            - crash_before_response=True                  → raise sin response
            - cart_expired=True                           → response con "cart_expired"
            - queue_reassign=True                         → response con queue change
            - declined=True                               → pago rechazado
            - amount=<float>                              → personalizar amount

    Estado interno:
        - now: datetime UTC simulado (avance manual con `advance_time`)
        - call_log: lista de tuplas (step, args) — para assertions
        - committed_orders: dict cart_id → order_id (idempotencia backend-side)
    """

    def __init__(self,
                 now_iso: str = "2026-05-14T10:00:00+00:00",
                 default_cart_ttl_sec: int = 600):
        self._now = self._parse_iso(now_iso)
        self.default_cart_ttl_sec = default_cart_ttl_sec
        self.injected_failures: dict = {}     # step → kwargs
        self.call_log: list = []
        self.committed_orders: dict = {}      # cart_id → order_id
        self.cart_expiries: dict = {}          # cart_id → expires_at datetime

    # ────── Time control ─────────────────────────────────────────
    @staticmethod
    def _parse_iso(s: str) -> datetime:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)

    def now_iso(self) -> str:
        return self._now.isoformat()

    def advance_time(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=int(seconds))

    # ────── Failure injection ───────────────────────────────────
    def inject(self, step: str, **kwargs) -> None:
        """Inyecta un fallo para la PRÓXIMA llamada a `step`.
        Se consume on first call (single-shot)."""
        self.injected_failures[step] = kwargs

    # ────── Endpoint: attach_cart ────────────────────────────────
    def attach_cart(self, *, event_id: str, seats: list,
                    account_id: str) -> dict:
        self.call_log.append(("attach_cart",
                              {"event_id": event_id, "seats": list(seats),
                               "account_id": account_id}))
        inject = self.injected_failures.pop("attach_cart", {})
        if inject.get("fail"):
            return {"ok": False, "error": inject.get("error", "attach_failed")}
        if inject.get("inventory_changed"):
            return {
                "ok": False,
                "error": "inventory_changed",
                "available_seats": inject.get("now_available", []),
            }
        # Éxito
        cart_id = inject.get("cart_id",
                              f"CART-{event_id}-{int(self._now.timestamp())}")
        ttl = int(inject.get("ttl", self.default_cart_ttl_sec))
        expires_dt = self._now + timedelta(seconds=ttl)
        self.cart_expiries[cart_id] = expires_dt
        return {
            "ok": True,
            "cart_id": cart_id,
            "expires_at": expires_dt.isoformat(),
            "seats": list(seats),
        }

    # ────── Endpoint: submit_payment ─────────────────────────────
    def submit_payment(self, *, cart_id: str, card_token: str) -> dict:
        self.call_log.append(("submit_payment",
                              {"cart_id": cart_id, "card_token": card_token}))
        inject = self.injected_failures.pop("submit_payment", {})

        # Simular delay de procesamiento (avanza el reloj del backend)
        delay = int(inject.get("delay_seconds", 0))
        if delay > 0:
            self.advance_time(delay)

        if inject.get("crash_before_response"):
            exc_cls = inject.get("exception_class", RuntimeError)
            msg = inject.get("exception_msg", "simulated network crash")
            raise exc_cls(msg)

        # Verificar idempotencia backend-side: si ya hay order para este cart, devolverla
        if cart_id in self.committed_orders:
            return {
                "ok": True,
                "order_id": self.committed_orders[cart_id],
                "amount": inject.get("amount", 100.0),
                "duplicate": True,
            }

        if inject.get("cart_expired"):
            return {"ok": False, "error": "cart_expired"}

        # Detección real de expiración por tiempo
        exp = self.cart_expiries.get(cart_id)
        if exp is not None and self._now > exp:
            return {"ok": False, "error": "cart_expired"}

        if inject.get("queue_reassign"):
            return {
                "ok": False,
                "error": "queue_reassigned",
                "new_position": inject.get("new_position", 9999),
            }

        if inject.get("declined"):
            return {
                "ok": False,
                "error": "payment_declined",
                "decline_code": inject.get("decline_code", "INSUFFICIENT_FUNDS"),
            }

        # Éxito
        order_id = inject.get("order_id", f"ORD-{cart_id}")
        self.committed_orders[cart_id] = order_id
        return {
            "ok": True,
            "order_id": order_id,
            "amount": inject.get("amount", 100.0),
        }


# ════════════════════════════════════════════════════════════════════
#  ReferenceFlowDriver — flow usando CartState + idempotency_log
# ════════════════════════════════════════════════════════════════════
@dataclass
class FlowResult:
    """Resultado estructurado del flow."""
    status: str
    cart_phase: str
    revision: int
    idempotency_state: str  # "committed" | "orphan" | "fresh"
    outcome: Optional[dict] = None
    error: str = ""
    error_category: str = ""    # cart_lifecycle / payment / network / inventory / queue
    cart_id: str = ""


class ReferenceFlowDriver:
    """Reference flow que consume CartState + idempotency_log.

    NO es producción — es referencia ejecutable que demuestra cómo el
    flow refactorizado debería componer las primitivas.
    """

    def __init__(self, *,
                 backend: MockBackend,
                 idem_root: str,
                 event_id: str,
                 account_id: str,
                 seats: list,
                 card_token: str = "TOK-X"):
        self.backend = backend
        self.idem_root = idem_root
        self.event_id = event_id
        self.account_id = account_id
        self.seats = list(seats)
        self.card_token = card_token
        self.idem_key = make_idempotency_key(
            event_id=event_id,
            seats=seats,
            account_id=account_id,
            timestamp_iso=backend.now_iso(),
        )

    # ────── Helpers ─────────────────────────────────────────────
    def _build_locks(self) -> tuple:
        return tuple(SeatLock(seat_id=s) for s in self.seats)

    def _outcome(self, status: str, cart: CartState, *,
                  outcome: Optional[dict] = None,
                  error: str = "",
                  category: str = "",
                  idem_state: str = "fresh") -> FlowResult:
        return FlowResult(
            status=status,
            cart_phase=cart.phase.value,
            revision=cart.revision,
            idempotency_state=idem_state,
            outcome=outcome,
            error=error,
            error_category=category,
            cart_id=cart.cart_id,
        )

    # ────── Run ─────────────────────────────────────────────────
    def run(self) -> FlowResult:
        """Ejecuta el flow completo con todas las validaciones.

        Returns FlowResult con status explícito.
        """
        cart = new_cart_state(
            cart_id="", event_id=self.event_id, account_id=self.account_id,
        )

        # ─── 0. Idempotency pre-check ─────────────────────────────
        rec = load_record(self.idem_key, root=self.idem_root)
        if rec.is_committed():
            return self._outcome(
                "skipped_already_committed", cart,
                outcome=(rec.outcome_payload or {}).get("payload", {}),
                idem_state="committed",
            )
        if rec.is_orphan():
            # Intent pasado sin outcome → flow seguro: NO continuar
            return self._outcome(
                "aborted_orphan_intent", cart,
                error="prior intent without outcome",
                category="cart_lifecycle",
                idem_state="orphan",
            )

        # ─── 1. Intent + attach_cart ──────────────────────────────
        write_intent(
            self.idem_key,
            {"step": "attach_cart",
             "event_id": self.event_id,
             "seats": list(self.seats),
             "account_id": self.account_id},
            root=self.idem_root,
        )

        try:
            r = self.backend.attach_cart(
                event_id=self.event_id,
                seats=self.seats,
                account_id=self.account_id,
            )
        except Exception as e:
            # Excepción en el side-effect → intent queda orphan,
            # flow se aborta limpiamente (recovery decide luego)
            return self._outcome(
                "exception_during_attach", cart,
                error=str(e), category="network",
                idem_state="orphan",
            )

        if not r.get("ok"):
            err = r.get("error", "attach_failed")
            cart = cart.with_phase(CartPhase.FAILED,
                                     validated_at_iso=self.backend.now_iso())
            category = "inventory" if "inventory" in err else "cart_lifecycle"
            write_outcome(self.idem_key, {
                "status": "attach_failed", "error": err,
            }, root=self.idem_root)
            return self._outcome(
                "attach_failed", cart, error=err, category=category,
                idem_state="committed",
            )

        # ─── 2. Construir CartState ATTACHED ─────────────────────
        cart = cart.with_seats(self._build_locks(),
                                validated_at_iso=self.backend.now_iso())
        cart = cart.with_expiration(r["expires_at"],
                                      validated_at_iso=self.backend.now_iso())
        cart = cart.with_phase(CartPhase.ATTACHED,
                                validated_at_iso=self.backend.now_iso())
        cart = replace(cart, cart_id=r["cart_id"])

        # ─── 3. Pre-checkout validation ──────────────────────────
        ok, why = cart.can_proceed_to_checkout(now_iso=self.backend.now_iso())
        if not ok:
            cart = cart.with_phase(
                CartPhase.EXPIRED if "expired" in why else CartPhase.FAILED,
                validated_at_iso=self.backend.now_iso(),
            )
            write_outcome(self.idem_key, {
                "status": "precheckout_failed", "error": why,
            }, root=self.idem_root)
            return self._outcome(
                "precheckout_failed", cart, error=why,
                category="cart_lifecycle", idem_state="committed",
            )

        # ─── 4. Transition CHECKING_OUT ──────────────────────────
        cart = cart.with_phase(CartPhase.CHECKING_OUT,
                                validated_at_iso=self.backend.now_iso())

        # ─── 5. Submit payment ───────────────────────────────────
        try:
            pay = self.backend.submit_payment(
                cart_id=cart.cart_id, card_token=self.card_token,
            )
        except Exception as e:
            # CRÍTICO: crash entre intent y outcome.
            # NO escribimos outcome — el intent queda orphan.
            # Recovery posterior decidirá. Estado del carrito local:
            # se queda en CHECKING_OUT (no transitamos sin info).
            return self._outcome(
                "crash_during_payment", cart,
                error=str(e), category="network",
                idem_state="orphan",
            )

        if not pay.get("ok"):
            err = pay.get("error", "payment_failed")
            if "expired" in err.lower():
                cart = cart.with_phase(CartPhase.EXPIRED,
                                         validated_at_iso=self.backend.now_iso())
                category = "cart_lifecycle"
            elif "queue" in err.lower():
                cart = cart.with_phase(CartPhase.FAILED,
                                         validated_at_iso=self.backend.now_iso())
                category = "queue"
            else:
                cart = cart.with_phase(CartPhase.FAILED,
                                         validated_at_iso=self.backend.now_iso())
                category = "payment"
            write_outcome(self.idem_key, {
                "status": "payment_failed",
                "error": err,
                "details": {k: v for k, v in pay.items() if k != "ok"},
            }, root=self.idem_root)
            return self._outcome(
                "payment_failed", cart, error=err, category=category,
                idem_state="committed",
            )

        # ─── 6. PAID ─────────────────────────────────────────────
        cart = cart.with_phase(CartPhase.PAID,
                                validated_at_iso=self.backend.now_iso())
        outcome_payload = {
            "status": "paid",
            "order_id": pay["order_id"],
            "amount": pay.get("amount", 0),
            "duplicate": pay.get("duplicate", False),
        }
        write_outcome(self.idem_key, outcome_payload, root=self.idem_root)
        return self._outcome(
            "success", cart, outcome=outcome_payload,
            idem_state="committed",
        )


# ════════════════════════════════════════════════════════════════════
#  Recovery helper — qué hacer con orphans
# ════════════════════════════════════════════════════════════════════
def recover_orphan(idem_key: str, *, idem_root: str,
                    backend: MockBackend) -> dict:
    """Cliente típico: ¿esta orphan key tiene order en backend?

    Estrategia conservadora:
        - Si backend tiene order para algún cart_id que coincida con el
          intent.payload, sellar outcome con éxito.
        - Si no, sellar outcome con status=unknown.

    Devuelve dict descriptor del resultado de recovery.
    """
    rec = load_record(idem_key, root=idem_root)
    if not rec.is_orphan():
        return {"recovered": False, "reason": "not_orphan"}

    intent_payload = (rec.intent_payload or {}).get("payload", {})
    # En el harness no tenemos cart_id en el intent (se sabría tras attach).
    # Estrategia simple: marcar como unknown — el operador decide.
    write_outcome(idem_key, {
        "status": "unknown_after_orphan_recovery",
        "intent_payload": intent_payload,
        "note": "no backend evidence to confirm; manual reconciliation required",
    }, root=idem_root)
    return {"recovered": True, "status": "unknown"}
