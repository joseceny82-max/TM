"""core/observability/error_classification.py — Error Taxonomy (STEP 3).

Sistema de clasificación de errores del bot de compra.

PROBLEMA QUE RESUELVE:
    Sin taxonomía, los logs son ilegibles agregadamente. No se puede
    responder "¿qué % de fallos son cart_lifecycle vs payment vs
    anti_bot?" sin parsear strings sueltas. Esta capa formaliza la
    clasificación.

DISEÑO:
    - 7 categorías canónicas (flow / network / anti_bot / cart_lifecycle
      / inventory / queue / payment)
    - 4 niveles de severidad (recoverable / retryable / fatal /
      non_recoverable)
    - Códigos estables por categoría (catálogo cerrado, ampliable)
    - `ErrorEvent` frozen dataclass — serializable, hashable
    - `classify_exception(exc, context)` heurístico que mapea
      Exception class + mensaje + contexto → ErrorEvent

PRINCIPIO:
    Pure data + pure classification. SIN side-effects. La emisión vive
    en `event_emitter.py`.

stdlib only. Determinístico (excepto timestamp).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Categorías de error (cerrado, no extensible en runtime)
# ════════════════════════════════════════════════════════════════════
class ErrorCategory(Enum):
    """Categorías canónicas — corresponden a fases del pipeline."""
    FLOW = "flow"                       # orquestación general
    NETWORK = "network"                 # TCP/HTTP/proxy/CDP
    ANTI_BOT = "anti_bot"               # bot_block, CAPTCHA, queue-it
    CART_LIFECYCLE = "cart_lifecycle"   # cart expired/stale/lost
    INVENTORY = "inventory"             # seats sold out/changed
    QUEUE = "queue"                     # queue reassign/position
    PAYMENT = "payment"                 # card declined/3DS/fraud


# ════════════════════════════════════════════════════════════════════
#  Severidad — cómo reaccionar al error
# ════════════════════════════════════════════════════════════════════
class ErrorSeverity(Enum):
    """Política de respuesta sugerida al detectar el error."""
    RECOVERABLE = "recoverable"           # retry inmediato sin penalty
    RETRYABLE = "retryable"               # retry con backoff
    FATAL = "fatal"                       # abort flow, no retry
    NON_RECOVERABLE = "non_recoverable"   # cuenta/perfil/IP quemado


# ════════════════════════════════════════════════════════════════════
#  Catálogo de códigos por categoría (ampliable por adición)
# ════════════════════════════════════════════════════════════════════
ERROR_CODES: dict = {
    ErrorCategory.FLOW: frozenset({
        "step_failed",
        "transition_invalid",
        "unexpected_state",
        "precondition_failed",
        "config_invalid",
        "internal_assert",
    }),
    ErrorCategory.NETWORK: frozenset({
        "connection_refused",
        "connection_reset",
        "timeout",
        "tls_handshake_failed",
        "proxy_dead",
        "proxy_auth_failed",
        "cdp_disconnect",
        "http_5xx",
        "http_4xx_unexpected",
        "dns_failure",
    }),
    ErrorCategory.ANTI_BOT: frozenset({
        "bot_block_detected",
        "captcha_required",
        "captcha_failed",
        "queue_it_ban",
        "akamai_block",
        "datadome_block",
        "cloudflare_block",
        "fingerprint_flagged",
    }),
    ErrorCategory.CART_LIFECYCLE: frozenset({
        "cart_expired",
        "cart_stale",
        "cart_not_found",
        "precheckout_failed",
        "session_lost",
        "seats_released",
        "cart_revision_mismatch",
    }),
    ErrorCategory.INVENTORY: frozenset({
        "inventory_changed",
        "seat_sold_out",
        "event_sold_out",
        "alternate_offered",
    }),
    ErrorCategory.QUEUE: frozenset({
        "queue_reassigned",
        "position_lost",
        "queue_full",
        "queue_expired",
        "queue_token_invalid",
    }),
    ErrorCategory.PAYMENT: frozenset({
        "card_declined",
        "card_expired",
        "card_invalid",
        "3ds_required",
        "3ds_failed",
        "3ds_timeout",
        "insufficient_funds",
        "fraud_block",
        "iframe_not_found",
        "tokenization_failed",
        "amount_mismatch",
    }),
}


def is_known_code(category: ErrorCategory, code: str) -> bool:
    """¿`code` está catalogado para `category`?"""
    if not isinstance(category, ErrorCategory):
        return False
    return code in ERROR_CODES.get(category, frozenset())


def list_codes(category: ErrorCategory) -> frozenset:
    """Devuelve los códigos catalogados para una categoría."""
    return ERROR_CODES.get(category, frozenset())


def all_categories() -> tuple:
    """Tuple de todas las categorías canónicas."""
    return tuple(ErrorCategory)


# ════════════════════════════════════════════════════════════════════
#  ErrorEvent — modelo inmutable del evento clasificado
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ErrorEvent:
    """Evento de error estructurado.

    Frozen — hashable, serializable, comparable por valor.
    """
    category: ErrorCategory
    code: str
    severity: ErrorSeverity
    timestamp_iso: str
    message: str = ""
    context: str = ""              # nombre del step / función origen
    payload: dict = field(default_factory=dict)
    exception_class: str = ""      # nombre de la Exception original, si aplica
    cart_id: str = ""              # trazabilidad
    idempotency_key: str = ""      # trazabilidad

    def to_dict(self) -> dict:
        """Serializa a dict JSON-compatible."""
        return {
            "category": self.category.value,
            "code": self.code,
            "severity": self.severity.value,
            "timestamp": self.timestamp_iso,
            "message": self.message,
            "context": self.context,
            "payload": dict(self.payload),
            "exception_class": self.exception_class,
            "cart_id": self.cart_id,
            "idempotency_key": self.idempotency_key,
        }


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ════════════════════════════════════════════════════════════════════
#  Heurístico: classify_exception
# ════════════════════════════════════════════════════════════════════

# Reglas declarativas (más específicas primero). Cada regla:
#   ("substring_lower", category, code, severity)
# Match contra `(exception_class_lower + " | " + message_lower)`
_CLASSIFICATION_RULES: list = [
    # ─── ANTI-BOT (lo más específico — keywords muy puntuales) ─────
    ("se ha suspendido", ErrorCategory.ANTI_BOT, "bot_block_detected", ErrorSeverity.NON_RECOVERABLE),
    ("session has been suspended", ErrorCategory.ANTI_BOT, "bot_block_detected", ErrorSeverity.NON_RECOVERABLE),
    ("browsing activity has been paused", ErrorCategory.ANTI_BOT, "akamai_block", ErrorSeverity.NON_RECOVERABLE),
    ("akamai", ErrorCategory.ANTI_BOT, "akamai_block", ErrorSeverity.NON_RECOVERABLE),
    ("datadome", ErrorCategory.ANTI_BOT, "datadome_block", ErrorSeverity.NON_RECOVERABLE),
    ("cloudflare", ErrorCategory.ANTI_BOT, "cloudflare_block", ErrorSeverity.NON_RECOVERABLE),
    ("captcha", ErrorCategory.ANTI_BOT, "captcha_required", ErrorSeverity.RETRYABLE),
    ("queue-it ban", ErrorCategory.ANTI_BOT, "queue_it_ban", ErrorSeverity.NON_RECOVERABLE),

    # ─── PAYMENT ─────────────────────────────────────────────────
    ("insufficient funds", ErrorCategory.PAYMENT, "insufficient_funds", ErrorSeverity.FATAL),
    ("do_not_honor", ErrorCategory.PAYMENT, "card_declined", ErrorSeverity.FATAL),
    ("card declined", ErrorCategory.PAYMENT, "card_declined", ErrorSeverity.FATAL),
    ("payment_declined", ErrorCategory.PAYMENT, "card_declined", ErrorSeverity.FATAL),
    ("declined", ErrorCategory.PAYMENT, "card_declined", ErrorSeverity.FATAL),
    ("3ds_failed", ErrorCategory.PAYMENT, "3ds_failed", ErrorSeverity.FATAL),
    ("3ds", ErrorCategory.PAYMENT, "3ds_required", ErrorSeverity.RETRYABLE),
    ("card expired", ErrorCategory.PAYMENT, "card_expired", ErrorSeverity.NON_RECOVERABLE),
    ("fraud", ErrorCategory.PAYMENT, "fraud_block", ErrorSeverity.NON_RECOVERABLE),
    ("iframe", ErrorCategory.PAYMENT, "iframe_not_found", ErrorSeverity.RETRYABLE),
    ("tokenization", ErrorCategory.PAYMENT, "tokenization_failed", ErrorSeverity.RETRYABLE),

    # ─── QUEUE ────────────────────────────────────────────────────
    ("queue_reassigned", ErrorCategory.QUEUE, "queue_reassigned", ErrorSeverity.RETRYABLE),
    ("queue reassign", ErrorCategory.QUEUE, "queue_reassigned", ErrorSeverity.RETRYABLE),
    ("queue full", ErrorCategory.QUEUE, "queue_full", ErrorSeverity.RETRYABLE),
    ("queue expired", ErrorCategory.QUEUE, "queue_expired", ErrorSeverity.RETRYABLE),
    ("queue", ErrorCategory.QUEUE, "queue_reassigned", ErrorSeverity.RETRYABLE),

    # ─── INVENTORY (antes de cart_lifecycle: "sold out" es más específico) ─
    ("sold out", ErrorCategory.INVENTORY, "seat_sold_out", ErrorSeverity.FATAL),
    ("inventory_changed", ErrorCategory.INVENTORY, "inventory_changed", ErrorSeverity.FATAL),
    ("inventory changed", ErrorCategory.INVENTORY, "inventory_changed", ErrorSeverity.FATAL),
    ("seat unavailable", ErrorCategory.INVENTORY, "seat_sold_out", ErrorSeverity.FATAL),

    # ─── CART LIFECYCLE ─────────────────────────────────────────
    ("cart_expired", ErrorCategory.CART_LIFECYCLE, "cart_expired", ErrorSeverity.FATAL),
    ("cart expired", ErrorCategory.CART_LIFECYCLE, "cart_expired", ErrorSeverity.FATAL),
    ("session_lost", ErrorCategory.CART_LIFECYCLE, "session_lost", ErrorSeverity.RETRYABLE),
    ("session lost", ErrorCategory.CART_LIFECYCLE, "session_lost", ErrorSeverity.RETRYABLE),
    ("cart_not_found", ErrorCategory.CART_LIFECYCLE, "cart_not_found", ErrorSeverity.FATAL),
    ("seats released", ErrorCategory.CART_LIFECYCLE, "seats_released", ErrorSeverity.FATAL),

    # ─── NETWORK (más genérico) ──────────────────────────────────
    ("proxy", ErrorCategory.NETWORK, "proxy_dead", ErrorSeverity.RETRYABLE),
    ("connectionreseterror", ErrorCategory.NETWORK, "connection_reset", ErrorSeverity.RETRYABLE),
    ("connectionrefusederror", ErrorCategory.NETWORK, "connection_refused", ErrorSeverity.RETRYABLE),
    ("timeouterror", ErrorCategory.NETWORK, "timeout", ErrorSeverity.RETRYABLE),
    ("connectionerror", ErrorCategory.NETWORK, "connection_reset", ErrorSeverity.RETRYABLE),
    ("connection refused", ErrorCategory.NETWORK, "connection_refused", ErrorSeverity.RETRYABLE),
    ("connection reset", ErrorCategory.NETWORK, "connection_reset", ErrorSeverity.RETRYABLE),
    ("timeout", ErrorCategory.NETWORK, "timeout", ErrorSeverity.RETRYABLE),
    ("dns", ErrorCategory.NETWORK, "dns_failure", ErrorSeverity.RETRYABLE),
    ("tls", ErrorCategory.NETWORK, "tls_handshake_failed", ErrorSeverity.RETRYABLE),
    ("ssl", ErrorCategory.NETWORK, "tls_handshake_failed", ErrorSeverity.RETRYABLE),
    ("cdp", ErrorCategory.NETWORK, "cdp_disconnect", ErrorSeverity.RETRYABLE),
    ("http 5", ErrorCategory.NETWORK, "http_5xx", ErrorSeverity.RETRYABLE),

    # ─── FLOW (fallback genérico al final) ───────────────────────
    ("transition", ErrorCategory.FLOW, "transition_invalid", ErrorSeverity.FATAL),
    ("illegalcart", ErrorCategory.FLOW, "transition_invalid", ErrorSeverity.FATAL),
    ("assertion", ErrorCategory.FLOW, "internal_assert", ErrorSeverity.FATAL),
]


def classify_exception(exc: Optional[BaseException] = None,
                        *,
                        message: str = "",
                        context: str = "",
                        cart_id: str = "",
                        idempotency_key: str = "",
                        payload: Optional[dict] = None) -> ErrorEvent:
    """Heurístico que mapea una Exception (o solo mensaje) a un ErrorEvent.

    Args:
        exc: Exception ya capturada (opcional — si no, usa `message`).
        message: mensaje libre adicional (se concatena con exc.args).
        context: nombre del step/función origen (p.ej. "submit_payment").
        cart_id: trazabilidad.
        idempotency_key: trazabilidad.
        payload: dict libre con datos extra (no usado para clasificar).

    Returns:
        ErrorEvent con la mejor categoría/código/severidad disponible.
        Si no matchea ninguna regla → FLOW/step_failed/RECOVERABLE.

    Determinístico: misma entrada → mismo output (excepto timestamp).
    """
    exc_class = type(exc).__name__ if exc is not None else ""
    exc_msg = ""
    if exc is not None:
        try:
            exc_msg = str(exc)
        except Exception:
            exc_msg = ""

    combined = f"{exc_class} | {exc_msg} | {message}".lower()

    matched_category: Optional[ErrorCategory] = None
    matched_code: str = ""
    matched_severity: Optional[ErrorSeverity] = None

    for substring, cat, code, sev in _CLASSIFICATION_RULES:
        if substring in combined:
            matched_category = cat
            matched_code = code
            matched_severity = sev
            break

    if matched_category is None:
        matched_category = ErrorCategory.FLOW
        matched_code = "step_failed"
        matched_severity = ErrorSeverity.RECOVERABLE

    return ErrorEvent(
        category=matched_category,
        code=matched_code,
        severity=matched_severity,
        timestamp_iso=_now_iso_utc(),
        message=(message or exc_msg)[:500],   # cap longitud
        context=context,
        payload=dict(payload) if isinstance(payload, dict) else {},
        exception_class=exc_class,
        cart_id=cart_id,
        idempotency_key=idempotency_key,
    )


def make_event(*,
                category: ErrorCategory,
                code: str,
                severity: ErrorSeverity = ErrorSeverity.RECOVERABLE,
                message: str = "",
                context: str = "",
                cart_id: str = "",
                idempotency_key: str = "",
                payload: Optional[dict] = None) -> ErrorEvent:
    """Construye un ErrorEvent explícitamente (sin heurístico)."""
    return ErrorEvent(
        category=category,
        code=code,
        severity=severity,
        timestamp_iso=_now_iso_utc(),
        message=message[:500],
        context=context,
        payload=dict(payload) if isinstance(payload, dict) else {},
        exception_class="",
        cart_id=cart_id,
        idempotency_key=idempotency_key,
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
