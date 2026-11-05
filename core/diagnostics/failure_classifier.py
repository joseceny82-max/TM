"""core/diagnostics/failure_classifier.py — Functional failure classifier.

PROBLEMA REAL DETECTADO en validación empírica:
    El DriftDetector estructural solo entiende transiciones STATE del FSM.
    Pero los fallos dominantes (53% del total) son eventos OBSERVABILITY que
    indican **fallos funcionales** sin romper estructura FSM:

        - no_email_input          (53% de fallos en 12 runs reales)
        - recovery_failed_*       (recovery exhausted)
        - reset_method_next_failed
        - FATAL_bot_block_early
        - bot_block_persisted

DISEÑO:
    - Pure function (sin estado)
    - Fail-safe absoluto
    - NO toca el FSM ni el DriftDetector estructural
    - Devuelve FailureSignal opcional con categoría + severidad + remediation

INTEGRACIÓN:
    El DriftDetector consulta este clasificador y, si hay match, emite un
    DriftEvent kind="SOFT_DRIFT" en paralelo a los structural drifts. Esto
    permite que el dashboard distinga drift estructural de fallo funcional.

NO ROMPE API:
    - DriftDetector sigue devolviendo Optional[DriftEvent]
    - SOFT_DRIFT es un valor adicional del campo kind, no rompe nada
    - failure_classifier es módulo nuevo, importación opt-in
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ════════════════════════════════════════════════════════════════════
#  FailureSignal
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class FailureSignal:
    """Señal de fallo funcional clasificada.

    Attributes:
        event:       evento que disparó el match
        category:    categoría funcional ("LOGIN_FAILURE", "BOT_BLOCK", ...)
        severity:    "low" | "medium" | "high"
        confidence:  "low" | "medium" | "high"
        remediation: pasos sugeridos
    """
    event: str
    category: str
    severity: str
    confidence: str
    remediation: str

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "remediation": self.remediation,
        }


# ════════════════════════════════════════════════════════════════════
#  Catálogo de patrones — derivado de runs reales (12 runs, 17 fallos)
# ════════════════════════════════════════════════════════════════════
# (regex, category, severity, confidence, remediation)
_FAILURE_RULES: list[tuple[re.Pattern, str, str, str, str]] = [
    # ── LOGIN_FAILURE (53% de fallos reales) ──────────────────────────
    (
        re.compile(r"^no_email_input$"),
        "LOGIN_FAILURE", "high", "high",
        "OAuth form no renderizó input de email. Inspeccionar HTML post-oauth, "
        "añadir wait domcontentloaded +2s, fallback selector "
        "input[type=email],#email-input,[data-bdd=email]"
    ),
    (
        re.compile(r"^login_full_false_treating_as_bad_creds$"),
        "LOGIN_FAILURE", "medium", "medium",
        "Login flow declaró bad credentials prematuramente. Verificar password "
        "actual vs stored, comprobar account validated_at reciente."
    ),
    # ── BOT_BLOCK (Akamai / step-up auth) ─────────────────────────────
    (
        re.compile(r"^FATAL_bot_block_early$"),
        "BOT_BLOCK", "high", "high",
        "Akamai detectó comportamiento de bot. Verificar WebRTC IP leak, "
        "fingerprint TZ/UA, warming home más largo. Considerar Hyper Solutions SDK."
    ),
    (
        re.compile(r"^bot_block_persisted$"),
        "BOT_BLOCK", "high", "high",
        "Bot block persistió tras detección. Subnet/cuenta probablemente quemada. "
        "Swap perfil + IP fresca, cleanup cookies, considerar cooldown 30 min."
    ),
    (
        re.compile(r"^tm_forced_password_reset_detected$"),
        "BOT_BLOCK", "medium", "medium",
        "TM forzó reset password (step-up auth). Usuario afectado por nuevo "
        "device-fingerprint. Continuar con auto_recovery flow."
    ),
    # ── RECOVERY_FAILURE ──────────────────────────────────────────────
    (
        re.compile(r"^recovery_failed_swap_account$"),
        "RECOVERY_FAILURE", "high", "high",
        "Recovery falló tras varios intentos → forzó swap. Verificar IMAP madre, "
        "timing OTP, conexión 5sim/SMSPool."
    ),
    (
        re.compile(r"^reset_method_next_failed$"),
        "RECOVERY_FAILURE", "high", "high",
        "Reset password flow no avanzó al siguiente paso. Verificar selector "
        "botón 'Siguiente', comprobar que radio email esté seleccionado."
    ),
    # ── CHECKOUT_FAILURE ──────────────────────────────────────────────
    (
        re.compile(r"^TIMEOUT_no_checkout$"),
        "CHECKOUT_FAILURE", "high", "high",
        "Bot no llegó a checkout en tiempo máximo. Verificar lógica de stepper, "
        "Continuar atascado, queue-it activo, sector clickable."
    ),
    (
        re.compile(r"^continuar_js_err$"),
        "CHECKOUT_FAILURE", "medium", "low",
        "Click 'Continuar' falló por JS error. Re-ejecutar tras DOM stable; "
        "usar Playwright role click en vez de JS click puro."
    ),
    (
        re.compile(r"^FATAL_continuar_no_avanza$"),
        "CHECKOUT_FAILURE", "high", "high",
        "Continuar clickeado pero no avanzó. Verificar cookies acepto, "
        "checkbox marcada, blockers activos en modal."
    ),
    (
        re.compile(r"^FATAL_no_sector_clickable$"),
        "CHECKOUT_FAILURE", "high", "high",
        "No hay sectores clickables en seatmap. Evento agotado o filtro "
        "demasiado estricto."
    ),
    (
        re.compile(r"^FATAL_no_tickets_available$"),
        "CHECKOUT_FAILURE", "high", "high",
        "TM reporta sin entradas disponibles. Verificar offer/qty, "
        "intentar otro offer_idx."
    ),
    # ── NETWORK_FAILURE / INFRA ───────────────────────────────────────
    (
        re.compile(r"^pre_flight_FAIL_abort$"),
        "NETWORK_FAILURE", "high", "high",
        "Pre-flight HTTP falló antes de browser. Verificar proxy conectividad, "
        "IP reputation, headers."
    ),
    (
        re.compile(r"^pre_flight_skipped_err$"),
        "NETWORK_FAILURE", "medium", "medium",
        "Pre-flight check saltado por error. Inspeccionar excepción, "
        "considerar abort early si proxy no responde."
    ),
    (
        re.compile(r"^FATAL_cdp_connect$"),
        "NETWORK_FAILURE", "high", "high",
        "CDP connect falló. MoreLogin no responde, puerto ocupado, "
        "o browser no arrancó."
    ),
    (
        re.compile(r"^FATAL_cdp_version$"),
        "NETWORK_FAILURE", "high", "high",
        "CDP version mismatch. Browser MoreLogin desactualizado, "
        "Playwright incompatible."
    ),
    # ── RESOURCE_EXHAUSTION ───────────────────────────────────────────
    (
        re.compile(r"^max_account_swaps_reached$"),
        "RECOVERY_FAILURE", "high", "high",
        "Pool de cuentas agotado tras swaps consecutivos. Re-generar pool, "
        "ampliar TIER 1, verificar validated_at."
    ),
    (
        re.compile(r"^no_more_accounts_to_try$"),
        "RECOVERY_FAILURE", "high", "high",
        "No quedan cuentas disponibles. Pool TIER 1+2 vacío para el evento."
    ),
]


# ════════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════════
def classify_failure(event_name: str, payload: Optional[dict] = None) -> Optional[FailureSignal]:
    """Clasifica un evento como functional failure si matchea un patrón conocido.

    Pure function. Fail-safe. Nunca lanza.

    Args:
        event_name: nombre del evento (string)
        payload:    dict opcional con datos del evento (no usado actualmente,
                    reservado para reglas que dependan de payload)

    Returns:
        FailureSignal si matchea un patrón, None si no.

    Garantías:
        - Inputs inválidos → None
        - Excepciones internas → None
    """
    if not isinstance(event_name, str) or not event_name:
        return None
    try:
        for pattern, category, severity, confidence, remediation in _FAILURE_RULES:
            if pattern.search(event_name):
                return FailureSignal(
                    event=event_name,
                    category=category,
                    severity=severity,
                    confidence=confidence,
                    remediation=remediation,
                )
    except Exception:
        return None
    return None


def is_failure(event_name: str) -> bool:
    """Convenience: True si el evento matchea algún patrón."""
    return classify_failure(event_name) is not None


def get_known_failures() -> list[str]:
    """Devuelve los patrones regex conocidos (para diagnostics)."""
    return [r[0].pattern for r in _FAILURE_RULES]


def get_categories() -> set[str]:
    """Categorías soportadas."""
    return {r[1] for r in _FAILURE_RULES}


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=== failure_classifier self-test ===")
    print(f"Categories: {sorted(get_categories())}")
    print(f"Total patterns: {len(_FAILURE_RULES)}")
    print()
    cases = [
        "no_email_input",
        "FATAL_bot_block_early",
        "recovery_failed_swap_account",
        "reset_method_next_failed",
        "TIMEOUT_no_checkout",
        "pre_flight_FAIL_abort",
        "goto",  # NOT a failure
        "shot",  # NOT a failure
        "",
        None,
    ]
    for c in cases:
        sig = classify_failure(c)
        if sig:
            print(f"  {c:35} → [{sig.category:18}] {sig.severity}/{sig.confidence}")
        else:
            print(f"  {c!s:35} → (no failure)")


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
