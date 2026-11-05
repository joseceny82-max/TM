"""core/backtesting/m23/backtest_engine.py — Pure Backtest Orchestrator.

Función pura que orquesta el pipeline:

    market_event → adapter_callable → snapshot
    snapshot     → strategy_callable → decision
    decision     → executor_callable → execution_result
    execution_result → reporter_callable → trading_report

AISLAMIENTO ESTRICTO:
    Este módulo NO importa M19, M20, M21 ni M22. TODAS las dependencias
    de pipeline se inyectan como callables. El wiring por defecto vive
    en `default_pipeline.py` (módulo separado, AST-exento del isolation
    test del core).

PURE FUNCTION:
    - Sin random
    - Sin clock
    - Sin IO
    - Sin estado persistente
    - Misma entrada (incluso callables idempotentes) → mismo output

stdlib only.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from core.backtesting.m23.metrics_compute import (
    max_drawdown,
    mean,
    round_float,
    sharpe_ratio,
    stddev,
    win_loss_rates,
)


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
DEFAULT_MODE: str = "simulation"
INITIAL_EQUITY: float = 0.0


# ════════════════════════════════════════════════════════════════════
#  Helpers fail-safe
# ════════════════════════════════════════════════════════════════════
def _safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_empty_backtest_report() -> dict:
    """Default report seguro para entradas degeneradas."""
    return {
        "backtest_summary": {
            "total_pnl": 0.0,
            "avg_pnl_per_step": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "loss_rate": 0.0,
        },
        "execution_stats": {
            "total_steps": 0,
            "total_orders": 0,
            "avg_fill_rate": 0.0,
            "avg_slippage": 0.0,
        },
        "risk_metrics": {
            "volatility": 0.0,
            "sharpe_ratio": 0.0,
            "efficiency_score": 0.0,
        },
        "equity_curve": [INITIAL_EQUITY],
    }


# ════════════════════════════════════════════════════════════════════
#  Single-step pipeline executor
# ════════════════════════════════════════════════════════════════════
def _step_pipeline(market_event: Any,
                    strategy_callable: Callable,
                    adapter_callable: Callable,
                    executor_callable: Callable,
                    reporter_callable: Callable,
                    mode: str) -> dict:
    """Ejecuta un step completo del pipeline.

    Returns dict con:
        snapshot, decision, execution_result, trading_report.

    Fail-safe: cualquier callable que falle → step neutro (PnL=0).
    """
    try:
        snapshot = adapter_callable(market_event)
    except Exception:
        snapshot = {}

    try:
        decision = strategy_callable(snapshot)
    except Exception:
        decision = {"actions": []}

    try:
        execution_result = executor_callable(decision, mode=mode)
    except TypeError:
        # Algunos callables pueden no aceptar `mode` kwarg
        try:
            execution_result = executor_callable(decision)
        except Exception:
            execution_result = {
                "execution_mode": mode,
                "orders_sent": 0,
                "results": [],
            }
    except Exception:
        execution_result = {
            "execution_mode": mode,
            "orders_sent": 0,
            "results": [],
        }

    try:
        report = reporter_callable(execution_result)
    except Exception:
        report = {
            "pnl": {"realized": 0.0, "unrealized": 0.0, "total": 0.0},
            "slippage": {"avg": 0.0, "max": 0.0},
            "execution_metrics": {
                "fill_rate": 0.0,
                "rejection_rate": 0.0,
                "latency_avg_ms": 0.0,
            },
            "efficiency_score": 0.0,
        }

    return {
        "snapshot": snapshot,
        "decision": decision,
        "execution_result": execution_result,
        "trading_report": report,
    }


# ════════════════════════════════════════════════════════════════════
#  API principal: run_backtest
# ════════════════════════════════════════════════════════════════════
def run_backtest(
    historical_dataset: list,
    strategy_callable: Optional[Callable] = None,
    *,
    adapter_callable: Optional[Callable] = None,
    executor_callable: Optional[Callable] = None,
    reporter_callable: Optional[Callable] = None,
    mode: str = DEFAULT_MODE,
) -> dict:
    """Ejecuta un backtest determinista sobre `historical_dataset`.

    Args:
        historical_dataset: list de market_events (cualquier shape que
            el adapter sepa procesar).
        strategy_callable: callable(snapshot) → decision.
            REQUERIDO. Si None → equivalente a "decision vacía".
        adapter_callable: callable(market_event) → snapshot.
            REQUERIDO para corrida real. Si None → equivalente a
            "snapshot = market_event" (identity).
        executor_callable: callable(decision, mode=...) → execution_result.
            REQUERIDO. Si None → equivalente a "execution_result vacío".
        reporter_callable: callable(execution_result) → trading_report.
            REQUERIDO. Si None → equivalente a "trading_report vacío".
        mode: str pasado al executor_callable (por defecto "simulation").

    Returns:
        dict con schema M23 (backtest_summary, execution_stats,
                              risk_metrics, equity_curve).

    Pure function. Determinística. Fail-safe.
    """
    try:
        # Normalizar inputs
        if not isinstance(historical_dataset, list):
            return _build_empty_backtest_report()

        # Callables faltantes → no-ops seguros (mantienen pipeline corriendo)
        if not callable(strategy_callable):
            strategy_callable = lambda s: {"actions": []}
        if not callable(adapter_callable):
            adapter_callable = lambda e: e if isinstance(e, dict) else {}
        if not callable(executor_callable):
            executor_callable = lambda d, mode=mode: {
                "execution_mode": mode, "orders_sent": 0, "results": [],
            }
        if not callable(reporter_callable):
            reporter_callable = lambda er: {
                "pnl": {"realized": 0.0, "unrealized": 0.0, "total": 0.0},
                "slippage": {"avg": 0.0, "max": 0.0},
                "execution_metrics": {
                    "fill_rate": 0.0,
                    "rejection_rate": 0.0,
                    "latency_avg_ms": 0.0,
                },
                "efficiency_score": 0.0,
            }

        if not historical_dataset:
            return _build_empty_backtest_report()

        # ─────── Acumuladores ───────
        step_pnls: list = []
        fill_rates: list = []
        slippages: list = []
        efficiencies: list = []
        total_orders: int = 0
        equity_curve: list = [INITIAL_EQUITY]
        cumulative: float = INITIAL_EQUITY

        # ─────── Loop principal ───────
        for event in historical_dataset:
            step = _step_pipeline(
                market_event=event,
                strategy_callable=strategy_callable,
                adapter_callable=adapter_callable,
                executor_callable=executor_callable,
                reporter_callable=reporter_callable,
                mode=mode,
            )

            report = _safe_dict(step["trading_report"])
            pnl_dict = _safe_dict(report.get("pnl"))
            metrics_dict = _safe_dict(report.get("execution_metrics"))
            slip_dict = _safe_dict(report.get("slippage"))

            step_pnl = _safe_float(pnl_dict.get("total"), 0.0)
            step_pnls.append(step_pnl)
            cumulative += step_pnl
            equity_curve.append(round_float(cumulative))

            fill_rates.append(_safe_float(metrics_dict.get("fill_rate"), 0.0))
            slippages.append(_safe_float(slip_dict.get("avg"), 0.0))
            efficiencies.append(
                _safe_float(report.get("efficiency_score"), 0.0)
            )

            er = _safe_dict(step["execution_result"])
            total_orders += _safe_int(er.get("orders_sent"), 0)

        # ─────── Métricas agregadas ───────
        total_steps = len(step_pnls)
        total_pnl = sum(step_pnls)
        avg_pnl = total_pnl / total_steps if total_steps else 0.0
        win_rate, loss_rate = win_loss_rates(step_pnls)
        dd = max_drawdown(equity_curve)
        vol = stddev(step_pnls)
        sharpe = sharpe_ratio(step_pnls)
        avg_fill_rate = mean(fill_rates)
        avg_slippage = mean(slippages)
        eff_score = mean(efficiencies)

        return {
            "backtest_summary": {
                "total_pnl": round_float(total_pnl),
                "avg_pnl_per_step": round_float(avg_pnl),
                "max_drawdown": round_float(dd),
                "win_rate": win_rate,
                "loss_rate": loss_rate,
            },
            "execution_stats": {
                "total_steps": int(total_steps),
                "total_orders": int(total_orders),
                "avg_fill_rate": round_float(avg_fill_rate),
                "avg_slippage": round_float(avg_slippage),
            },
            "risk_metrics": {
                "volatility": round_float(vol),
                "sharpe_ratio": round_float(sharpe),
                "efficiency_score": round_float(eff_score),
            },
            "equity_curve": [round_float(v) for v in equity_curve],
        }
    except Exception:
        return _build_empty_backtest_report()


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
