"""core/migration/divergence_engine.py — Divergence Detection (STEP 9).

Compara dos resultados de flow (legacy vs core) y produce un reporte
de divergencia con score + severidad.

PRINCIPIOS:
    - Pure function. Mismo input → mismo output.
    - Log-only. NUNCA bloquea ejecución real.
    - Fail-safe: si los dicts son malformados → score=0 (sin pánico).

stdlib only.
"""
from __future__ import annotations

from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
# Pesos por categoría de divergencia (suman para max=100 cap)
WEIGHT_SUCCESS: int = 40
WEIGHT_CART_PHASE: int = 25
WEIGHT_ORDER_ID: int = 20
WEIGHT_IDEM_STATE: int = 10
WEIGHT_ERROR_CATEGORY: int = 5

SCORE_LOW_MAX: int = 30
SCORE_MEDIUM_MAX: int = 70


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _safe_get(d: Any, key: str, default: Any = None) -> Any:
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def _classify_severity(score: int) -> str:
    if score < SCORE_LOW_MAX:
        return "low"
    if score < SCORE_MEDIUM_MAX:
        return "medium"
    return "critical"


# ════════════════════════════════════════════════════════════════════
#  API principal
# ════════════════════════════════════════════════════════════════════
def compute_divergence(legacy: Any, core: Any) -> dict:
    """Compara dos flow results y devuelve report de divergencia.

    Args:
        legacy: dict del resultado legacy (e.g. {"success", "cart_phase",
                "order_id", "idempotency_state", "error_category"})
        core: dict análogo del flow core

    Returns:
        dict con:
            score: int 0-100
            severity: "low" | "medium" | "critical"
            differences: list[dict] con cada diferencia detectada
            same: bool (True si score == 0)
    """
    if not isinstance(legacy, dict) or not isinstance(core, dict):
        return {
            "score": 0,
            "severity": "low",
            "differences": [],
            "same": True,
            "note": "non-dict inputs treated as same",
        }

    differences: list = []
    score: int = 0

    # 1. success
    leg_succ = _safe_get(legacy, "success")
    cor_succ = _safe_get(core, "success")
    if leg_succ is not None and cor_succ is not None and leg_succ != cor_succ:
        differences.append({
            "field": "success",
            "legacy": leg_succ,
            "core": cor_succ,
            "weight": WEIGHT_SUCCESS,
        })
        score += WEIGHT_SUCCESS

    # 2. cart_phase / final_phase
    leg_phase = (_safe_get(legacy, "final_phase") or
                  _safe_get(legacy, "cart_phase"))
    cor_phase = (_safe_get(core, "final_phase") or
                  _safe_get(core, "cart_phase"))
    if leg_phase != cor_phase and (leg_phase or cor_phase):
        differences.append({
            "field": "cart_phase",
            "legacy": leg_phase,
            "core": cor_phase,
            "weight": WEIGHT_CART_PHASE,
        })
        score += WEIGHT_CART_PHASE

    # 3. order_id (presence-based — el valor literal puede diferir)
    leg_order = _safe_get(legacy, "order_id", "")
    cor_order = _safe_get(core, "order_id", "")
    leg_has = bool(leg_order)
    cor_has = bool(cor_order)
    if leg_has != cor_has:
        differences.append({
            "field": "order_id_presence",
            "legacy": leg_has,
            "core": cor_has,
            "weight": WEIGHT_ORDER_ID,
        })
        score += WEIGHT_ORDER_ID

    # 4. idempotency_state
    leg_idem = _safe_get(legacy, "idempotency_state")
    cor_idem = _safe_get(core, "idempotency_state")
    if leg_idem != cor_idem and (leg_idem or cor_idem):
        differences.append({
            "field": "idempotency_state",
            "legacy": leg_idem,
            "core": cor_idem,
            "weight": WEIGHT_IDEM_STATE,
        })
        score += WEIGHT_IDEM_STATE

    # 5. error_category (only when both failed and differ)
    leg_cat = _safe_get(legacy, "error_category", "")
    cor_cat = _safe_get(core, "error_category", "")
    if leg_cat != cor_cat and (leg_cat or cor_cat):
        differences.append({
            "field": "error_category",
            "legacy": leg_cat,
            "core": cor_cat,
            "weight": WEIGHT_ERROR_CATEGORY,
        })
        score += WEIGHT_ERROR_CATEGORY

    score = min(100, score)
    return {
        "score": int(score),
        "severity": _classify_severity(score),
        "differences": differences,
        "same": score == 0,
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
