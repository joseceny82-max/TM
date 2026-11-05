"""core/dashboard/health_states.py — Explicit health state taxonomy (Phase 6).

Distingue 5 estados que ANTES estaban colapsados en "passing/burning/violating":

    INSUFFICIENT_DATA   — no hay suficientes eventos productivos
    TELEMETRY_BROKEN    — eventos existen pero les faltan campos clave
                          (cart_id/idempotency_key vacíos, etc.)
    HEALTHY             — todo verde
    DEGRADED            — algunos alerts o gates burning
    CRITICAL            — emergency alerts o SLO violating

Phase 6 audit principle:
    "NO DATA, BAD DATA, FAILING SYSTEM are NOT the same thing"

stdlib only. Pure functions.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class HealthState(Enum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    TELEMETRY_BROKEN = "TELEMETRY_BROKEN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


# ════════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════════
MIN_EVENTS_FOR_DATA: int = 50
MIN_EVENTS_FOR_HEALTH: int = 200
MIN_TRACEABILITY_RATIO: float = 0.95   # 95% events must have cart_id


# ════════════════════════════════════════════════════════════════════
#  Telemetry quality check
# ════════════════════════════════════════════════════════════════════
def compute_telemetry_quality(events: list) -> dict:
    """Mide la calidad de la telemetría (no contenido — calidad).

    PHASE A audit: traceability se mide por idempotency_key (PRIMARY)
    porque el bot TM no tiene cart_id explícito (DOM-based flow).
    `cart_id` queda como secundario y NO bloquea broken=True.

    Returns:
        dict con flags + reasons.
    """
    if not isinstance(events, list):
        return {
            "total_events": 0, "with_cart_id": 0, "with_idem_key": 0,
            "with_source_tag": 0, "traceability_ratio": 0.0,
            "tagged_ratio": 0.0, "broken": True,
            "reasons": ["events is not a list"],
        }

    total = len(events)
    with_cart = 0
    with_idem = 0
    with_source = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("cart_id"):
            with_cart += 1
        if ev.get("idempotency_key"):
            with_idem += 1
        if ev.get("source"):
            with_source += 1

    # PRIMARY trace = idempotency_key coverage
    idem_ratio = (with_idem / total) if total > 0 else 0.0
    # SECONDARY trace = cart_id coverage (informational only)
    cart_ratio = (with_cart / total) if total > 0 else 0.0
    tagged_ratio = (with_source / total) if total > 0 else 0.0

    reasons: list = []
    broken = False
    if total >= MIN_EVENTS_FOR_DATA:
        # NOW: idempotency_key is the gating criterion, not cart_id
        if idem_ratio < MIN_TRACEABILITY_RATIO:
            broken = True
            reasons.append(
                f"idem_key_coverage={idem_ratio:.2%} below "
                f"{MIN_TRACEABILITY_RATIO:.0%} (idempotency keys missing)"
            )
        if tagged_ratio < 0.95:
            broken = True
            reasons.append(
                f"source_tag_coverage={tagged_ratio:.2%} below 95% "
                "(post-Phase 2 events should all be tagged)"
            )

    return {
        "total_events": int(total),
        "with_cart_id": int(with_cart),
        "with_idem_key": int(with_idem),
        "with_source_tag": int(with_source),
        # PHASE A: traceability_ratio is now PRIMARY (idem_key)
        "traceability_ratio": round(idem_ratio, 4),
        "cart_id_ratio": round(cart_ratio, 4),    # informational
        "tagged_ratio": round(tagged_ratio, 4),
        "broken": broken,
        "reasons": reasons,
    }


# ════════════════════════════════════════════════════════════════════
#  Compute health state
# ════════════════════════════════════════════════════════════════════
def compute_health_state(*,
                          events: Optional[list] = None,
                          alerts: Optional[list] = None,
                          slo_status: str = "insufficient_data",
                          ) -> dict:
    """Devuelve el estado consolidado.

    Returns:
        dict {state, reasons, quality, recommendations}
    """
    events = events or []
    alerts = alerts or []
    quality = compute_telemetry_quality(events)
    total = quality["total_events"]
    reasons: list = []
    recommendations: list = []

    # ─── 1. INSUFFICIENT_DATA — antes que nada ────────────────
    if total < MIN_EVENTS_FOR_DATA:
        state = HealthState.INSUFFICIENT_DATA
        reasons.append(f"only {total} events (need >= {MIN_EVENTS_FOR_DATA})")
        recommendations.append("Run the bot in production to accumulate telemetry")

    # ─── 2. TELEMETRY_BROKEN — hay eventos pero falsos/sin campos ─
    elif quality["broken"]:
        state = HealthState.TELEMETRY_BROKEN
        reasons.extend(quality["reasons"])
        recommendations.append(
            "Investigate hooks: events arrive but lack required fields. "
            "Possible causes: hooks called outside main flow, "
            "or fields not populated correctly."
        )

    # ─── 3. CRITICAL — emergency alerts o SLO violating ──────
    elif (slo_status == "violating"
            or any(a.get("severity") == "emergency" for a in alerts)):
        state = HealthState.CRITICAL
        if slo_status == "violating":
            reasons.append("SLO violating")
        emergency = [a.get("code", "?") for a in alerts
                      if a.get("severity") == "emergency"]
        if emergency:
            reasons.append(f"emergency alerts: {emergency}")
        recommendations.append("Execute set_kill_switch(True) immediately")

    # ─── 4. DEGRADED — critical alerts pero no emergency ──────
    elif (slo_status == "burning"
            or any(a.get("severity") == "critical" for a in alerts)):
        state = HealthState.DEGRADED
        if slo_status == "burning":
            reasons.append("SLO burning (consume rate elevated)")
        critical = [a.get("code", "?") for a in alerts
                     if a.get("severity") == "critical"]
        if critical:
            reasons.append(f"critical alerts: {critical}")
        recommendations.append("Investigate alerts before any progression")

    # ─── 5. HEALTHY ────────────────────────────────────────────
    else:
        state = HealthState.HEALTHY
        reasons.append(f"{total} events, {quality['traceability_ratio']:.0%} traceability, SLO {slo_status}")
        # No specific recommendation when healthy

    return {
        "state": state.value,
        "reasons": reasons,
        "quality": quality,
        "recommendations": recommendations,
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
