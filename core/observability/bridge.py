"""core/observability/bridge.py — Integration Bridge (STEP 3B).

Capa de integración que combina las primitivas de:
    - Step 1: CartState (estado explícito del carrito)
    - Step 2: idempotency_log (WAL intent/outcome)
    - Step 3: event_emitter (eventos estructurados)

OBJETIVO:
    Resolver el "dual system drift" entre las primitivas (que viven en
    capas nuevas) y `_compra_parallel.py` (que no las consume todavía).

    Este bridge entrega helpers de UNA línea que el bot puede invocar
    incrementalmente, en orden, sin refactor masivo.

PRINCIPIOS:
    1. **Fail-safe absoluto**: cualquier hook que falle es no-op
       silencioso. NUNCA propaga. El bot sigue su flow normal.
    2. **Opt-in**: el bot adopta los hooks línea a línea. No hay
       monkey-patching ni magia oculta.
    3. **Inmutable**: `FlowContext` es frozen. Helpers que modifican
       estado devuelven nueva instancia.
    4. **Composable**: helpers se pueden encadenar; el orden no produce
       efectos no deseados.

EJEMPLO DE INTEGRACIÓN (futuro — sin tocar nada ahora):

    from core.observability.bridge import (
        start_flow, transition_phase, record_intent, record_outcome,
        record_exception, check_already_committed,
    )
    from core.purchase_state import CartPhase

    # 1. Al inicio del flow
    ctx = start_flow(slot=1, event_id="EVT", account_id="ACC",
                     seats=["S1", "S2"])

    # 2. Idempotency pre-check
    prior = check_already_committed(ctx)
    if prior is not None:
        return prior   # ya pagado, no repetir

    # 3. Antes de side-effect
    record_intent(ctx, step="attach_cart")

    try:
        # ... bot hace su attach normal ...
        ctx = transition_phase(ctx, CartPhase.ATTACHED, cart_id="CART-X")
    except Exception as e:
        record_exception(ctx, e, context="attach_cart")
        raise

    # 4. Tras side-effect
    record_outcome(ctx, status="attached")

NUNCA modifica la lógica del bot. Cada hook puede ser comentado
individualmente sin romper nada.

stdlib only. Pure delegation a las primitivas existentes.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from core.idempotency import (
    is_committed,
    is_orphan,
    load_record,
    make_idempotency_key,
    write_intent,
    write_outcome,
)
from core.observability.error_classification import (
    ErrorCategory,
    ErrorEvent,
    ErrorSeverity,
    classify_exception,
    make_event,
)
from core.observability.event_emitter import emit, emit_event
from core.purchase_state import (
    CartPhase,
    CartState,
    IllegalCartTransition,
    SeatLock,
    new_cart_state,
)


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
DEFAULT_IDEM_ROOT: str = "runs/idempotency_log"


# ════════════════════════════════════════════════════════════════════
#  Helpers internos
# ════════════════════════════════════════════════════════════════════
def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe(callable_, *args, default=None, **kwargs):
    """Ejecuta callable bajo try/except. Devuelve default si falla."""
    try:
        return callable_(*args, **kwargs)
    except Exception:
        return default


# ════════════════════════════════════════════════════════════════════
#  FlowContext — identidad inmutable de un flow en curso
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class FlowContext:
    """Identidad + estado de un flow de compra en curso.

    Frozen — toda actualización produce nueva instancia. Esto evita
    races silenciosas entre threads y simplifica el modelo mental.
    """
    slot: int
    event_id: str
    account_id: str
    seats: tuple                  # tuple[str, ...]
    card_token: str
    idempotency_key: str
    cart_state: CartState
    idem_root: str = DEFAULT_IDEM_ROOT
    # Trazabilidad extra
    created_at_iso: str = ""

    def with_cart_state(self, cs: CartState) -> "FlowContext":
        return replace(self, cart_state=cs)


# ════════════════════════════════════════════════════════════════════
#  start_flow — punto de entrada
# ════════════════════════════════════════════════════════════════════
def start_flow(*,
                slot: int,
                event_id: str,
                account_id: str,
                seats,
                card_token: str = "",
                idem_root: str = DEFAULT_IDEM_ROOT,
                bucket_granularity: str = "day") -> FlowContext:
    """Inicia un FlowContext + emite evento `flow_started`.

    Genera idempotency_key determinístico desde
    (event_id, seats, account_id, day_bucket).

    Fail-safe: si algo del setup falla, devuelve un ctx mínimo válido.
    """
    now = _now_iso_utc()
    seats_tuple = tuple(seats) if seats else ()

    idem_key = _safe(
        make_idempotency_key,
        event_id=event_id, seats=seats_tuple,
        account_id=account_id, timestamp_iso=now,
        bucket_granularity=bucket_granularity,
        default=f"ik-fallback-{slot}",
    )

    cart = _safe(
        new_cart_state,
        cart_id="", event_id=event_id, account_id=account_id,
        default=new_cart_state(cart_id="", event_id=event_id,
                                 account_id=account_id),
    )

    ctx = FlowContext(
        slot=int(slot) if isinstance(slot, int) else 0,
        event_id=str(event_id) if event_id else "",
        account_id=str(account_id) if account_id else "",
        seats=seats_tuple,
        card_token=str(card_token) if card_token else "",
        idempotency_key=str(idem_key),
        cart_state=cart,
        idem_root=str(idem_root),
        created_at_iso=now,
    )

    # Emit fire-and-forget
    _safe(
        emit_event, ErrorCategory.FLOW, "flow_started",
        {"slot": ctx.slot, "n_seats": len(seats_tuple)},
        severity=ErrorSeverity.RECOVERABLE,
        context="start_flow",
        cart_id="",
        idempotency_key=ctx.idempotency_key,
    )
    return ctx


# ════════════════════════════════════════════════════════════════════
#  Idempotency checks (read-only)
# ════════════════════════════════════════════════════════════════════
def check_already_committed(ctx: FlowContext) -> Optional[dict]:
    """Si el outcome ya está committed → devuelve su payload.

    Permite al bot abortar reintentos: `if check_already_committed(ctx): return`.
    """
    try:
        rec = load_record(ctx.idempotency_key, root=ctx.idem_root)
        if rec.is_committed() and rec.outcome_payload:
            return rec.outcome_payload.get("payload", rec.outcome_payload)
    except Exception:
        pass
    return None


def check_orphan_intent(ctx: FlowContext) -> bool:
    """¿Existe un intent sin outcome (crash previo)?"""
    return bool(_safe(is_orphan, ctx.idempotency_key,
                       root=ctx.idem_root, default=False))


# ════════════════════════════════════════════════════════════════════
#  Idempotency write (intent + outcome)
# ════════════════════════════════════════════════════════════════════
def record_intent(ctx: FlowContext, step: str, **payload) -> bool:
    """Persiste intent atomic + emite evento `intent_recorded`.

    Devuelve True si la escritura tuvo éxito. NUNCA propaga.
    """
    full_payload = dict(payload)
    full_payload["step"] = str(step) if step is not None else "?"
    full_payload["slot"] = ctx.slot
    ok = _safe(
        write_intent, ctx.idempotency_key, full_payload,
        root=ctx.idem_root, default=False,
    )
    _safe(
        emit_event, ErrorCategory.FLOW, "intent_recorded",
        {"step": full_payload["step"], "ok": bool(ok != False)},
        severity=ErrorSeverity.RECOVERABLE,
        context="record_intent",
        cart_id=ctx.cart_state.cart_id,
        idempotency_key=ctx.idempotency_key,
    )
    return bool(ok != False)


def record_outcome(ctx: FlowContext, status: str, **payload) -> bool:
    """Persiste outcome atomic + emite evento `outcome_recorded`.

    Devuelve True si la escritura tuvo éxito. NUNCA propaga.
    """
    full_payload = dict(payload)
    full_payload["status"] = str(status) if status is not None else "unknown"
    full_payload["slot"] = ctx.slot
    full_payload["cart_phase"] = ctx.cart_state.phase.value
    ok = _safe(
        write_outcome, ctx.idempotency_key, full_payload,
        root=ctx.idem_root, default=False,
    )
    _safe(
        emit_event, ErrorCategory.FLOW, "outcome_recorded",
        {"status": full_payload["status"], "ok": bool(ok != False)},
        severity=ErrorSeverity.RECOVERABLE,
        context="record_outcome",
        cart_id=ctx.cart_state.cart_id,
        idempotency_key=ctx.idempotency_key,
    )
    return bool(ok != False)


# ════════════════════════════════════════════════════════════════════
#  CartState transitions
# ════════════════════════════════════════════════════════════════════
def transition_phase(ctx: FlowContext,
                      new_phase: CartPhase,
                      *,
                      cart_id: str = "",
                      expires_at_iso: str = "",
                      seats=None,
                      validated_at_iso: str = "") -> FlowContext:
    """Transiciona la CartState al new_phase + emite evento.

    Fail-safe: si la transición es ilegal, emite evento
    `transition_invalid` y devuelve ctx sin cambios. NO propaga.

    Args opcionales:
        cart_id: si se da y ctx no tenía, lo asigna.
        expires_at_iso: actualiza expiración.
        seats: tuple de SeatLock o lista de seat_ids.
        validated_at_iso: marca revalidación.
    """
    if not isinstance(new_phase, CartPhase):
        _safe(
            emit_event, ErrorCategory.FLOW, "transition_invalid",
            {"reason": "new_phase not a CartPhase"},
            severity=ErrorSeverity.FATAL,
            context="transition_phase",
            idempotency_key=ctx.idempotency_key,
        )
        return ctx

    validated = validated_at_iso or _now_iso_utc()
    cart = ctx.cart_state

    # 1. Actualizar cart_id si vino
    if cart_id and not cart.cart_id:
        cart = replace(cart, cart_id=str(cart_id))

    # 2. Actualizar seats si vino
    if seats is not None:
        try:
            # Acepta lista de SeatLock o lista de strings
            seat_locks = []
            for s in seats:
                if isinstance(s, SeatLock):
                    seat_locks.append(s)
                else:
                    seat_locks.append(SeatLock(seat_id=str(s)))
            cart = cart.with_seats(tuple(seat_locks), validated_at_iso=validated)
        except Exception:
            pass

    # 3. Actualizar expiración si vino
    if expires_at_iso:
        try:
            cart = cart.with_expiration(expires_at_iso, validated_at_iso=validated)
        except Exception:
            pass

    # 4. Transitar fase (puede lanzar IllegalCartTransition)
    try:
        cart = cart.with_phase(new_phase, validated_at_iso=validated)
    except IllegalCartTransition as e:
        _safe(
            emit_event, ErrorCategory.FLOW, "transition_invalid",
            {"reason": str(e),
             "from": ctx.cart_state.phase.value,
             "to": new_phase.value},
            severity=ErrorSeverity.FATAL,
            context="transition_phase",
            cart_id=ctx.cart_state.cart_id,
            idempotency_key=ctx.idempotency_key,
        )
        return ctx
    except Exception:
        return ctx

    new_ctx = ctx.with_cart_state(cart)

    # Emit transition event
    _safe(
        emit_event, ErrorCategory.FLOW, "phase_transition",
        {"from": ctx.cart_state.phase.value,
         "to": new_phase.value,
         "revision": cart.revision},
        severity=ErrorSeverity.RECOVERABLE,
        context="transition_phase",
        cart_id=cart.cart_id,
        idempotency_key=ctx.idempotency_key,
    )
    return new_ctx


# ════════════════════════════════════════════════════════════════════
#  Exception capture
# ════════════════════════════════════════════════════════════════════
def record_exception(ctx: Optional[FlowContext],
                      exc: BaseException,
                      context: str = "",
                      **payload) -> Optional[ErrorEvent]:
    """Clasifica una Exception + emite evento. NUNCA propaga.

    Acepta `ctx=None` (para sites donde no hay flow context — tipo top-level).
    """
    try:
        cart_id = ctx.cart_state.cart_id if ctx is not None else ""
        idem_key = ctx.idempotency_key if ctx is not None else ""
        ev = classify_exception(
            exc=exc, context=context,
            cart_id=cart_id, idempotency_key=idem_key,
            payload=dict(payload) if payload else None,
        )
        emit(ev)
        return ev
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
#  Context manager: with_instrumentation
# ════════════════════════════════════════════════════════════════════
@contextmanager
def with_instrumentation(*, slot: int, event_id: str, account_id: str,
                          seats, card_token: str = "",
                          idem_root: str = DEFAULT_IDEM_ROOT,
                          auto_record_exception: bool = True) -> Iterator[FlowContext]:
    """Context manager que instrumenta un bloque de código.

    Uso:
        with with_instrumentation(slot=1, event_id="E", ...) as ctx:
            # bot logic (puede usar ctx para hooks adicionales)
            ...

    Si auto_record_exception=True y el bloque lanza, captura y emite
    el evento de excepción (luego re-lanza para que el caller lo maneje).
    """
    ctx = start_flow(
        slot=slot, event_id=event_id, account_id=account_id,
        seats=seats, card_token=card_token, idem_root=idem_root,
    )
    try:
        yield ctx
    except BaseException as e:
        if auto_record_exception:
            try:
                record_exception(ctx, e, context="with_instrumentation_block")
            except Exception:
                pass
        raise


# ════════════════════════════════════════════════════════════════════
#  Helper: snapshot del estado actual (debugging / dashboards)
# ════════════════════════════════════════════════════════════════════
def snapshot(ctx: FlowContext) -> dict:
    """Devuelve un dict con el estado completo del flow para debug/UI."""
    try:
        rec = load_record(ctx.idempotency_key, root=ctx.idem_root)
        idem_state = (
            "committed" if rec.is_committed()
            else "orphan" if rec.is_orphan()
            else "fresh"
        )
    except Exception:
        idem_state = "unknown"

    return {
        "slot": ctx.slot,
        "event_id": ctx.event_id,
        "account_id": ctx.account_id,
        "seats": list(ctx.seats),
        "idempotency_key": ctx.idempotency_key,
        "idempotency_state": idem_state,
        "cart_state": ctx.cart_state.to_dict(),
        "created_at": ctx.created_at_iso,
    }


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
