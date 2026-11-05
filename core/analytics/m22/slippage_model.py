"""core/analytics/m22/slippage_model.py — Slippage Model.

Modelo de slippage DETERMINISTA, sin random, sin clock, sin IO,
sin acceso a market externo.

Fórmula base (per spec M22):

    slippage_per_share = latency_ms * SLIPPAGE_LATENCY_COEFF * volatility_proxy

Donde:
    - SLIPPAGE_LATENCY_COEFF = 0.001 (constante literal del spec)
    - volatility_proxy ∈ [0, 1] derivado de la dispersión interna
      del execution_result (NO mercado externo)

Comportamiento por tipo de orden:
    - El execution_result de M21 NO incluye order_type, por lo que
      este módulo computa slippage uniforme por orden a partir de su
      `latency_ms`. La diferenciación market vs limit del spec se
      preserva como helpers expuestos (`apply_market_multiplier`,
      `apply_limit_multiplier`) que el caller puede invocar si
      dispone del tipo desde otra fuente.

stdlib only. Pure functions.
"""
from __future__ import annotations

from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes literales (spec M22)
# ════════════════════════════════════════════════════════════════════
SLIPPAGE_LATENCY_COEFF: float = 0.001

# Multiplicadores por tipo de orden (helpers — no aplicados por defecto
# porque execution_result carece de order_type per contrato M21)
MARKET_ORDER_MULTIPLIER: float = 1.5
LIMIT_ORDER_MULTIPLIER: float = 0.0   # "limit orders: menor o cero" (spec)

# Defaults / clamps
VOLATILITY_PROXY_DEFAULT: float = 0.5
VOLATILITY_PROXY_MIN: float = 0.0
VOLATILITY_PROXY_MAX: float = 1.0
CV_SCALE: float = 10.0  # escala el coef. de variación a banda [0, 1]

ROUND_NDIGITS: int = 8


# ════════════════════════════════════════════════════════════════════
#  Helpers fail-safe (coerciones puras)
# ════════════════════════════════════════════════════════════════════
def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ════════════════════════════════════════════════════════════════════
#  Volatility proxy — dispersión interna del execution_result
# ════════════════════════════════════════════════════════════════════
def compute_volatility_proxy(execution_result: Any) -> float:
    """Deriva volatility_proxy ∈ [0, 1] desde la dispersión de
    `avg_price` entre las órdenes con status="filled".

    Determinístico. Si hay < 2 fills, devuelve VOLATILITY_PROXY_DEFAULT.

    Args:
        execution_result: dict M21-shaped (o similar).

    Returns:
        float ∈ [0, 1]
    """
    results = _safe_list(_safe_dict(execution_result).get("results"))
    fills: list = []
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("status") != "filled":
            continue
        fills.append(_safe_float(r.get("avg_price"), 0.0))

    if len(fills) < 2:
        return VOLATILITY_PROXY_DEFAULT

    mean = sum(fills) / len(fills)
    if mean <= 0:
        return VOLATILITY_PROXY_DEFAULT

    variance = sum((p - mean) ** 2 for p in fills) / len(fills)
    std_dev = variance ** 0.5
    cv = std_dev / mean  # coeficiente de variación
    scaled = cv * CV_SCALE
    return _clamp(scaled, VOLATILITY_PROXY_MIN, VOLATILITY_PROXY_MAX)


# ════════════════════════════════════════════════════════════════════
#  Slippage por orden — fórmula base spec
# ════════════════════════════════════════════════════════════════════
def per_order_slippage(latency_ms: Any, volatility_proxy: float) -> float:
    """slippage = latency_ms × 0.001 × volatility_proxy.

    Pure function. Determinística.
    """
    lat = _safe_float(latency_ms, 0.0)
    if lat < 0:
        lat = 0.0
    vp = _clamp(_safe_float(volatility_proxy, VOLATILITY_PROXY_DEFAULT),
                VOLATILITY_PROXY_MIN, VOLATILITY_PROXY_MAX)
    return round(lat * SLIPPAGE_LATENCY_COEFF * vp, ROUND_NDIGITS)


# ════════════════════════════════════════════════════════════════════
#  Helpers de multiplicador por tipo (opt-in)
# ════════════════════════════════════════════════════════════════════
def apply_market_multiplier(slippage: float) -> float:
    """Aplica el multiplicador para market orders. Helper opt-in."""
    return round(_safe_float(slippage, 0.0) * MARKET_ORDER_MULTIPLIER,
                  ROUND_NDIGITS)


def apply_limit_multiplier(slippage: float) -> float:
    """Aplica el multiplicador para limit orders (per spec: menor o cero)."""
    return round(_safe_float(slippage, 0.0) * LIMIT_ORDER_MULTIPLIER,
                  ROUND_NDIGITS)


# ════════════════════════════════════════════════════════════════════
#  Compute slippage per order id (consumido por report_generator)
# ════════════════════════════════════════════════════════════════════
def compute_slippage_per_order(execution_result: Any) -> dict:
    """Para cada result en execution_result.results, calcula slippage.

    Returns:
        dict {order_id: slippage_per_share}
    """
    vol_proxy = compute_volatility_proxy(execution_result)
    out: dict = {}
    results = _safe_list(_safe_dict(execution_result).get("results"))
    for r in results:
        if not isinstance(r, dict):
            continue
        oid = r.get("order_id", "")
        if not isinstance(oid, str):
            oid = str(oid)
        lat = r.get("latency_ms", 0)
        # Slippage sólo es económicamente relevante en fills
        if r.get("status") == "filled":
            out[oid] = per_order_slippage(lat, vol_proxy)
        else:
            out[oid] = 0.0
    return out


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
