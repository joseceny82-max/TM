"""core/backtesting/m23/ — M23 Backtesting Engine.

Evaluador determinista de estrategias sobre datos históricos
simulados. Orquesta el pipeline M19 → M13–M18 → M21 → M22.

PRINCIPIO ARQUITECTÓNICO:
    M23 es ORCHESTRATOR-ONLY:
        - NUNCA ejecuta órdenes reales
        - NUNCA decide (strategy_callable se inyecta)
        - NUNCA modifica capas previas
        - NO contiene lógica de mercado
        - NO contiene aprendizaje automático

    Garantías:
        B1: Determinismo absoluto — mismo dataset + mismo callable → mismo output
        B2: Stateless — sin estado persistente entre llamadas
        B3: Fail-safe — input malformado → empty backtest report válido
        B4: Additive — coexiste con M19/M20/M21/M22 sin perturbar contratos
        B5: stdlib only — sin dependencias externas
        B6: Pipeline integrity — win_rate + loss_rate == 1.0 (invariante)

Public API:
    run_backtest(historical_dataset, strategy_callable, *,
                  adapter_callable, executor_callable, reporter_callable,
                  mode="simulation") -> dict
        — orquestador puro con dependency injection.

    run_default_backtest(historical_dataset, strategy_callable, *,
                          mode="simulation") -> dict
        — wiring de conveniencia que cablea M19/M21/M22 public API.

Output schema:
    {
        "backtest_summary": {
            "total_pnl", "avg_pnl_per_step", "max_drawdown",
            "win_rate", "loss_rate",
        },
        "execution_stats": {
            "total_steps", "total_orders",
            "avg_fill_rate", "avg_slippage",
        },
        "risk_metrics": {
            "volatility", "sharpe_ratio", "efficiency_score",
        },
        "equity_curve": list[float],
    }

stdlib only. Determinístico. Sin side-effects.
"""
from core.backtesting.m23.backtest_engine import (
    DEFAULT_MODE,
    INITIAL_EQUITY,
    run_backtest,
)
from core.backtesting.m23.default_pipeline import (
    default_pipeline_callables,
    run_default_backtest,
)
from core.backtesting.m23.metrics_compute import (
    max_drawdown,
    mean,
    sharpe_ratio,
    stddev,
    win_loss_rates,
)

__all__ = [
    # Entry points
    "run_backtest",
    "run_default_backtest",
    # Helpers
    "default_pipeline_callables",
    # Metrics primitives
    "max_drawdown",
    "sharpe_ratio",
    "win_loss_rates",
    "mean",
    "stddev",
    # Constants
    "DEFAULT_MODE",
    "INITIAL_EQUITY",
]
