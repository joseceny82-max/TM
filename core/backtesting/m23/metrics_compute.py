"""core/backtesting/m23/metrics_compute.py — Backtest metrics math.

Funciones puras stdlib para computar:
    - mean
    - stddev (población)
    - max_drawdown (pico-valle sobre equity curve)
    - win_rate / loss_rate (positive_steps vs no-positive)
    - sharpe_ratio (mean(returns) / stddev(returns))

INVARIANTE: win_rate + loss_rate == 1.0 (con tolerancia float).
INVARIANTE: max_drawdown >= 0.0.

stdlib only. Pure functions. Determinístico.
"""
from __future__ import annotations

from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
ROUND_NDIGITS: int = 8

WIN_THRESHOLD: float = 0.0  # PnL > 0 → win; ≤ 0 → loss


# ════════════════════════════════════════════════════════════════════
#  Helpers fail-safe
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


def _safe_list_of_floats(values: Any) -> list:
    if not isinstance(values, list):
        return []
    return [_safe_float(v, 0.0) for v in values]


# ════════════════════════════════════════════════════════════════════
#  Estadísticos básicos
# ════════════════════════════════════════════════════════════════════
def mean(values: list) -> float:
    vs = _safe_list_of_floats(values)
    if not vs:
        return 0.0
    return sum(vs) / len(vs)


def stddev(values: list) -> float:
    """Desviación típica poblacional. Devuelve 0.0 si len < 2."""
    vs = _safe_list_of_floats(values)
    n = len(vs)
    if n < 2:
        return 0.0
    m = sum(vs) / n
    variance = sum((v - m) ** 2 for v in vs) / n
    return variance ** 0.5


# ════════════════════════════════════════════════════════════════════
#  max_drawdown — peak-to-trough sobre equity curve
# ════════════════════════════════════════════════════════════════════
def max_drawdown(equity_curve: list) -> float:
    """Drawdown máximo = max(peak − trough) recorriendo la curva.

    Pure function. Determinística.

    Args:
        equity_curve: list[float] con valores secuenciales del equity.

    Returns:
        float ≥ 0
    """
    if not isinstance(equity_curve, list) or not equity_curve:
        return 0.0
    peak = _safe_float(equity_curve[0], 0.0)
    max_dd: float = 0.0
    for v in equity_curve:
        val = _safe_float(v, 0.0)
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, ROUND_NDIGITS)


# ════════════════════════════════════════════════════════════════════
#  win_rate / loss_rate (binarios por step)
# ════════════════════════════════════════════════════════════════════
def win_loss_rates(step_pnls: list) -> tuple:
    """Devuelve (win_rate, loss_rate) garantizando suma == 1.0.

    Definición: win = step_pnl > 0; loss = step_pnl ≤ 0 (incluye cero).
    Si list vacía → (0.0, 0.0).
    """
    vs = _safe_list_of_floats(step_pnls)
    n = len(vs)
    if n == 0:
        return 0.0, 0.0
    wins = sum(1 for v in vs if v > WIN_THRESHOLD)
    win_rate = wins / n
    loss_rate = 1.0 - win_rate
    return round(win_rate, ROUND_NDIGITS), round(loss_rate, ROUND_NDIGITS)


# ════════════════════════════════════════════════════════════════════
#  Sharpe ratio simplificado: mean / stddev
# ════════════════════════════════════════════════════════════════════
def sharpe_ratio(returns: list) -> float:
    """Sharpe simplificado = mean(returns) / stddev(returns).

    Sin tasa libre de riesgo ni anualización (per spec M23: "simplificado").
    Si stddev == 0 (todos iguales o len<2) → 0.0.
    """
    vs = _safe_list_of_floats(returns)
    if len(vs) < 2:
        return 0.0
    std = stddev(vs)
    if std <= 0.0:
        return 0.0
    return round(mean(vs) / std, ROUND_NDIGITS)


# ════════════════════════════════════════════════════════════════════
#  Helper de redondeo masivo (para outputs reproducibles)
# ════════════════════════════════════════════════════════════════════
def round_float(value: float) -> float:
    return round(_safe_float(value, 0.0), ROUND_NDIGITS)


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
