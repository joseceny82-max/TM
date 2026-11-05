"""core/analytics/m22/ — M22 Trading Metrics Layer.

Capa observacional económica que convierte execution_result de M21
en métricas reproducibles y deterministas.

PRINCIPIO ARQUITECTÓNICO:
    M22 es OBSERVATIONAL ECONOMIC ONLY:
        - NUNCA ejecuta
        - NUNCA decide
        - NUNCA ordena
        - NUNCA importa M19, M20 ni M21
        - SOLO mide

    Garantías:
        T1: Determinismo absoluto — misma entrada → mismo output bit-exact
        T2: Stateless funcionalmente — sin estado persistente
        T3: Fail-safe — input malformado → report con ceros (válido)
        T4: Additive — coexiste con M19/M20/M21 sin perturbar contratos
        T5: stdlib only — sin dependencias externas
        T6: Invariante metric — fill_rate + rejection_rate == 1.0

Public API:
    generate_report(execution_result: dict) -> dict
    compute_pnl(execution_result, slippage_per_order=None) -> dict
    compute_metrics(execution_result, slippage_per_order=None) -> dict
    compute_slippage_per_order(execution_result) -> dict
    compute_volatility_proxy(execution_result) -> float
    reconstruct_trades(execution_result, intent_map=None) -> list[dict]

Output schema final:
    {
        "pnl": {"realized", "unrealized", "total"},
        "slippage": {"avg", "max"},
        "execution_metrics": {"fill_rate", "rejection_rate", "latency_avg_ms"},
        "efficiency_score": float,
    }

stdlib only. Determinístico. Sin side-effects.
"""
from core.analytics.m22.metrics_engine import (
    LATENCY_REFERENCE_MS,
    compute_metrics,
)
from core.analytics.m22.pnl_engine import compute_pnl
from core.analytics.m22.report_generator import (
    EFFICIENCY_SLIPPAGE_PENALTY,
    generate_report,
)
from core.analytics.m22.slippage_model import (
    LIMIT_ORDER_MULTIPLIER,
    MARKET_ORDER_MULTIPLIER,
    SLIPPAGE_LATENCY_COEFF,
    VOLATILITY_PROXY_DEFAULT,
    apply_limit_multiplier,
    apply_market_multiplier,
    compute_slippage_per_order,
    compute_volatility_proxy,
    per_order_slippage,
)
from core.analytics.m22.trade_reconstruction import (
    OUTCOME_EXECUTED,
    OUTCOME_NOT_EXECUTED,
    reconstruct_trades,
)

# Alias semántico — algunos specs invocan ambos nombres
build_trading_report = generate_report

__all__ = [
    # Entry points
    "generate_report",
    "build_trading_report",
    # Sub-engines
    "compute_pnl",
    "compute_metrics",
    "compute_slippage_per_order",
    "compute_volatility_proxy",
    "per_order_slippage",
    "reconstruct_trades",
    # Helpers de multiplicador por tipo (opt-in)
    "apply_market_multiplier",
    "apply_limit_multiplier",
    # Constants
    "SLIPPAGE_LATENCY_COEFF",
    "MARKET_ORDER_MULTIPLIER",
    "LIMIT_ORDER_MULTIPLIER",
    "VOLATILITY_PROXY_DEFAULT",
    "LATENCY_REFERENCE_MS",
    "EFFICIENCY_SLIPPAGE_PENALTY",
    "OUTCOME_EXECUTED",
    "OUTCOME_NOT_EXECUTED",
]
