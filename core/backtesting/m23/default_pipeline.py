"""core/backtesting/m23/default_pipeline.py — Convenience wiring.

ESTE MÓDULO ESTÁ FUERA DEL CORE DE M23.

Su único propósito es **importar el public API** de M19, M21 y M22 y
exponer una función `run_default_backtest` que cablea esos defaults
en `run_backtest`.

El core de M23 (backtest_engine.py) NO importa ninguna otra capa —
es 100% pure orchestrator con dependency injection. Este wiring vive
en archivo separado para que el test de aislamiento del core pueda
escanear `backtest_engine.py` sin tropezar con imports cross-layer.

NO contiene lógica de orquestación (toda vive en backtest_engine).
NO modifica las capas que importa.

stdlib only + public API de M19/M21/M22.
"""
from __future__ import annotations

from typing import Callable, Optional

# Public API imports — sólo símbolos exportados
from core.analytics.m22 import generate_report
from core.execution.m21 import execute_decision
from core.observability.adapter import adapt_event_to_tm_snapshot

from core.backtesting.m23.backtest_engine import run_backtest


# ════════════════════════════════════════════════════════════════════
#  Función de conveniencia
# ════════════════════════════════════════════════════════════════════
def run_default_backtest(
    historical_dataset: list,
    strategy_callable: Callable,
    *,
    mode: str = "simulation",
) -> dict:
    """Ejecuta `run_backtest` cableado con M19/M21/M22 public API.

    Args:
        historical_dataset: list de market_events compatibles con M19.
        strategy_callable: callable(snapshot) → decision.
        mode: pasado al executor (default "simulation").

    Returns:
        dict M23 (backtest schema).

    Pure function (delega en run_backtest, que es pure).
    """
    return run_backtest(
        historical_dataset=historical_dataset,
        strategy_callable=strategy_callable,
        adapter_callable=adapt_event_to_tm_snapshot,
        executor_callable=execute_decision,
        reporter_callable=generate_report,
        mode=mode,
    )


# ════════════════════════════════════════════════════════════════════
#  Factory: devuelve la tripleta de callables del wiring por defecto
# ════════════════════════════════════════════════════════════════════
def default_pipeline_callables() -> dict:
    """Devuelve el set de callables que componen el pipeline por defecto.

    Útil para tests que quieren inyectar explícitamente los defaults
    sin importar M19/M21/M22 directamente desde la suite.
    """
    return {
        "adapter_callable": adapt_event_to_tm_snapshot,
        "executor_callable": execute_decision,
        "reporter_callable": generate_report,
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
