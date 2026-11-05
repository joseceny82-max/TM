"""tests/test_m23_backtest.py — Test suite for M23 Backtesting Engine.

Cubre las 7 categorías mandatadas por spec M23:

    1. Determinism            — mismo dataset → mismo backtest result
    2. Pipeline integrity     — M19→M22 ejecuta sin romper schema
    3. No leakage / isolation — core engine NO importa M19/M21/M22
    4. Metrics correctness    — PnL acumulado, equity curve coherente
    5. Edge cases             — empty / single / all-loss / all-win
    6. Statistical invariants — win_rate + loss_rate == 1.0
    7. Isolation (pure)       — sin IO, sin randomness, sin state

Extras:
    8. Schema validation
    9. Default pipeline integration (con M19/M21/M22 reales)
   10. Statelessness explícita

Cero dependencias externas. stdlib + pytest.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.backtesting.m23 import (
    DEFAULT_MODE,
    INITIAL_EQUITY,
    default_pipeline_callables,
    max_drawdown,
    mean,
    run_backtest,
    run_default_backtest,
    sharpe_ratio,
    stddev,
    win_loss_rates,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures — mocks que NO importan M19/M21/M22
# ────────────────────────────────────────────────────────────────────
def _mock_adapter(event: dict) -> dict:
    """Adapter mock — devuelve un snapshot mínimo determinístico."""
    if not isinstance(event, dict):
        event = {}
    return {
        "history": [],
        "current": {
            "rates": {
                "purchase_block_rate": float(event.get("block", 0.0)),
                "purchase_dry_run_rate": 0.5,
            },
            "counters": {"purchase_live_count": 0, "internal_errors": 0},
            "config": {"mode": "dry_run"},
        },
        "meta": {"mode": "simulation", "event_id": event.get("id", ""),
                  "timestamp": "", "drift_hint": "low"},
    }


def _mock_strategy_always_buy(snapshot: dict) -> dict:
    return {
        "actions": [
            {"symbol": "X", "side": "buy", "quantity": 1.0,
             "order_type": "market"},
        ],
    }


def _mock_strategy_no_action(snapshot: dict) -> dict:
    return {"actions": []}


def _mock_executor_always_fill(decision: dict, mode: str = "simulation") -> dict:
    """Mock executor: cada action → filled order."""
    actions = decision.get("actions", []) if isinstance(decision, dict) else []
    results = []
    for i, a in enumerate(actions):
        qty = float(a.get("quantity", 1.0)) if isinstance(a, dict) else 1.0
        results.append({
            "order_id": f"mock-{i}",
            "status": "filled",
            "filled_quantity": qty,
            "avg_price": 100.0,
            "latency_ms": 10,
            "error": None,
        })
    return {
        "execution_mode": mode,
        "orders_sent": len(results),
        "results": results,
    }


def _mock_executor_always_reject(decision: dict, mode: str = "simulation") -> dict:
    actions = decision.get("actions", []) if isinstance(decision, dict) else []
    results = []
    for i, a in enumerate(actions):
        results.append({
            "order_id": f"mock-{i}",
            "status": "rejected",
            "filled_quantity": 0.0,
            "avg_price": 0.0,
            "latency_ms": 0,
            "error": "mock_rejected",
        })
    return {
        "execution_mode": mode,
        "orders_sent": len(results),
        "results": results,
    }


def _mock_reporter_with_pnl(pnl_per_step: float):
    """Factory: devuelve un reporter que asigna PnL fijo por step."""
    def _reporter(execution_result: dict) -> dict:
        results = execution_result.get("results", []) if isinstance(execution_result, dict) else []
        filled = sum(1 for r in results if r.get("status") == "filled")
        total = len(results) if results else 0
        fr = (filled / total) if total else 0.0
        return {
            "pnl": {
                "realized": pnl_per_step,
                "unrealized": 0.0,
                "total": pnl_per_step,
            },
            "slippage": {"avg": 0.001, "max": 0.001},
            "execution_metrics": {
                "fill_rate": fr,
                "rejection_rate": 1.0 - fr,
                "latency_avg_ms": 10.0,
            },
            "efficiency_score": 0.8,
        }
    return _reporter


def _basic_dataset(n: int = 5) -> list:
    return [{"id": f"e{i}", "block": 0.0 + 0.1 * i} for i in range(n)]


def _pipeline_mocks(pnl: float = 0.0) -> dict:
    return {
        "adapter_callable": _mock_adapter,
        "executor_callable": _mock_executor_always_fill,
        "reporter_callable": _mock_reporter_with_pnl(pnl),
    }


# ════════════════════════════════════════════════════════════════════
#  1. DETERMINISM
# ════════════════════════════════════════════════════════════════════
class TestDeterminism:

    def test_same_dataset_same_output(self):
        ds = _basic_dataset(5)
        r1 = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.5))
        r2 = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.5))
        assert r1 == r2

    def test_repeated_calls_identical(self):
        ds = _basic_dataset(10)
        reports = [
            run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(-0.2))
            for _ in range(5)
        ]
        assert all(r == reports[0] for r in reports)

    def test_json_roundtrip_stable(self):
        ds = _basic_dataset(3)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.1))
        s = json.dumps(r, sort_keys=True)
        r2 = json.loads(s)
        assert r["backtest_summary"]["total_pnl"] == r2["backtest_summary"]["total_pnl"]


# ════════════════════════════════════════════════════════════════════
#  2. PIPELINE INTEGRITY
# ════════════════════════════════════════════════════════════════════
class TestPipelineIntegrity:

    def test_each_step_invokes_full_pipeline(self):
        """Verifica que cada step pasa por adapter → strategy → executor → reporter."""
        calls = {"adapter": 0, "strategy": 0, "executor": 0, "reporter": 0}

        def adapter(ev):
            calls["adapter"] += 1
            return _mock_adapter(ev)
        def strategy(snap):
            calls["strategy"] += 1
            return _mock_strategy_always_buy(snap)
        def executor(dec, mode="simulation"):
            calls["executor"] += 1
            return _mock_executor_always_fill(dec, mode)
        def reporter(er):
            calls["reporter"] += 1
            return _mock_reporter_with_pnl(0.0)(er)

        ds = _basic_dataset(7)
        run_backtest(ds, strategy,
                      adapter_callable=adapter,
                      executor_callable=executor,
                      reporter_callable=reporter)
        assert calls["adapter"] == 7
        assert calls["strategy"] == 7
        assert calls["executor"] == 7
        assert calls["reporter"] == 7

    def test_step_count_equals_dataset_length(self):
        ds = _basic_dataset(12)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.0))
        assert r["execution_stats"]["total_steps"] == 12

    def test_equity_curve_length_equals_steps_plus_one(self):
        """Equity curve incluye initial equity + 1 entry por step."""
        ds = _basic_dataset(8)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.1))
        assert len(r["equity_curve"]) == 8 + 1

    def test_failing_strategy_does_not_crash(self):
        def bad_strategy(snap):
            raise RuntimeError("strategy crashed")
        ds = _basic_dataset(3)
        r = run_backtest(ds, bad_strategy, **_pipeline_mocks(0.0))
        # No debe propagar — step queda neutro
        assert r["execution_stats"]["total_steps"] == 3


# ════════════════════════════════════════════════════════════════════
#  3. ISOLATION — engine + metrics_compute sin imports cross-layer
# ════════════════════════════════════════════════════════════════════
class TestIsolation:

    CORE_FILES = [
        PROJ_ROOT / "core" / "backtesting" / "m23" / "backtest_engine.py",
        PROJ_ROOT / "core" / "backtesting" / "m23" / "metrics_compute.py",
    ]

    FORBIDDEN_PREFIXES = (
        "core.observability",   # M19, M20
        "core.execution",       # M21
        "core.analytics",       # M22
    )

    FORBIDDEN_STDLIB = {
        "random", "subprocess", "socket", "threading",
        "multiprocessing", "asyncio",
    }

    def _module_imports(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    seen.add(n.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    seen.add(node.module)
        return seen

    def test_core_engine_no_cross_layer_imports(self):
        for path in self.CORE_FILES:
            imports = self._module_imports(path)
            for imp in imports:
                for forbidden in self.FORBIDDEN_PREFIXES:
                    assert not imp.startswith(forbidden), (
                        f"{path.name} importa {imp} — prohibido en core M23"
                    )

    def test_core_engine_no_forbidden_stdlib(self):
        for path in self.CORE_FILES:
            imports = self._module_imports(path)
            forbidden_seen = imports & self.FORBIDDEN_STDLIB
            assert not forbidden_seen, (
                f"{path.name} importa stdlib prohibido: {forbidden_seen}"
            )

    def test_core_engine_only_imports_within_m23_or_safe_stdlib(self):
        allowed_prefixes = ("core.backtesting.m23",)
        allowed_stdlib = {"typing", "__future__"}
        for path in self.CORE_FILES:
            imports = self._module_imports(path)
            for imp in imports:
                if imp in allowed_stdlib:
                    continue
                if imp.split(".")[0] in allowed_stdlib:
                    continue
                ok = any(imp.startswith(p) for p in allowed_prefixes)
                assert ok, (
                    f"{path.name} importa módulo no permitido: {imp}"
                )

    def test_default_pipeline_imports_only_public_api(self):
        """default_pipeline.py es el ÚNICO archivo M23 autorizado a
        importar M19/M21/M22, y SÓLO el public API (no submódulos
        internos como adapter.domain_adapter)."""
        path = PROJ_ROOT / "core" / "backtesting" / "m23" / "default_pipeline.py"
        imports = self._module_imports(path)
        forbidden_internals = {
            "core.observability.adapter.domain_adapter",
            "core.execution.m21.simulator",
            "core.execution.m21.execution_engine",
            "core.analytics.m22.report_generator",
            "core.analytics.m22.pnl_engine",
        }
        leaked = imports & forbidden_internals
        assert not leaked, f"default_pipeline.py importa internals: {leaked}"


# ════════════════════════════════════════════════════════════════════
#  4. METRICS CORRECTNESS
# ════════════════════════════════════════════════════════════════════
class TestMetricsCorrectness:

    def test_total_pnl_is_sum_of_step_pnls(self):
        ds = _basic_dataset(4)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.5))
        # 4 steps × 0.5 = 2.0
        assert r["backtest_summary"]["total_pnl"] == 2.0

    def test_equity_curve_cumulative(self):
        ds = _basic_dataset(3)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(1.0))
        # equity: 0 → 1 → 2 → 3
        assert r["equity_curve"] == [0.0, 1.0, 2.0, 3.0]

    def test_avg_pnl_per_step_correct(self):
        ds = _basic_dataset(5)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(2.0))
        # avg = 2.0
        assert r["backtest_summary"]["avg_pnl_per_step"] == 2.0

    def test_max_drawdown_correct(self):
        # Equity: 0 → 5 → 5 → 2 → 2 → 4
        # peaks: 0, 5, 5, 5, 5, 5
        # dd:    0, 0, 0, 3, 3, 1
        # max_dd = 3
        equity = [0.0, 5.0, 5.0, 2.0, 2.0, 4.0]
        assert max_drawdown(equity) == 3.0

    def test_max_drawdown_zero_for_monotonic_growth(self):
        assert max_drawdown([0.0, 1.0, 2.0, 3.0]) == 0.0

    def test_win_loss_rates_correct(self):
        pnls = [1.0, -2.0, 3.0, -1.0, 5.0]  # 3 wins, 2 losses
        win, loss = win_loss_rates(pnls)
        assert win == 0.6
        assert loss == 0.4

    def test_sharpe_zero_when_no_variance(self):
        assert sharpe_ratio([1.0, 1.0, 1.0]) == 0.0

    def test_volatility_zero_when_flat(self):
        ds = _basic_dataset(5)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.0))
        assert r["risk_metrics"]["volatility"] == 0.0


# ════════════════════════════════════════════════════════════════════
#  5. EDGE CASES
# ════════════════════════════════════════════════════════════════════
class TestEdgeCases:

    def test_empty_dataset_returns_default(self):
        r = run_backtest([], _mock_strategy_always_buy, **_pipeline_mocks(0.0))
        assert r["execution_stats"]["total_steps"] == 0
        assert r["backtest_summary"]["total_pnl"] == 0.0
        assert r["equity_curve"] == [0.0]

    def test_none_dataset_returns_default(self):
        r = run_backtest(None, _mock_strategy_always_buy, **_pipeline_mocks(0.0))
        assert r["execution_stats"]["total_steps"] == 0

    def test_single_step_dataset(self):
        ds = [{"id": "single"}]
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(1.5))
        assert r["execution_stats"]["total_steps"] == 1
        assert r["backtest_summary"]["total_pnl"] == 1.5
        # Sharpe = 0 cuando n < 2
        assert r["risk_metrics"]["sharpe_ratio"] == 0.0

    def test_all_wins(self):
        ds = _basic_dataset(5)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(1.0))
        assert r["backtest_summary"]["win_rate"] == 1.0
        assert r["backtest_summary"]["loss_rate"] == 0.0
        assert r["backtest_summary"]["max_drawdown"] == 0.0

    def test_all_losses(self):
        ds = _basic_dataset(5)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(-1.0))
        assert r["backtest_summary"]["win_rate"] == 0.0
        assert r["backtest_summary"]["loss_rate"] == 1.0
        # Drawdown debe acumular: 0,-1,-2,-3,-4,-5 → peak=0, max_dd=5
        assert r["backtest_summary"]["max_drawdown"] == 5.0

    def test_all_zero_pnl(self):
        ds = _basic_dataset(5)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.0))
        # PnL=0 cuenta como loss (porque > 0 es la condición de win)
        assert r["backtest_summary"]["win_rate"] == 0.0
        assert r["backtest_summary"]["loss_rate"] == 1.0
        assert r["backtest_summary"]["total_pnl"] == 0.0

    def test_strategy_callable_none_uses_noop(self):
        ds = _basic_dataset(3)
        r = run_backtest(ds, None, **_pipeline_mocks(0.0))
        # No callable → fallback no-op, pero pipeline corre
        assert r["execution_stats"]["total_steps"] == 3

    def test_all_callables_none_uses_noops(self):
        ds = _basic_dataset(2)
        r = run_backtest(ds, None,
                          adapter_callable=None,
                          executor_callable=None,
                          reporter_callable=None)
        assert isinstance(r, dict)
        assert r["execution_stats"]["total_steps"] == 2

    def test_non_list_dataset_returns_default(self):
        r = run_backtest("not-a-list", _mock_strategy_always_buy, **_pipeline_mocks(0.0))
        assert r["execution_stats"]["total_steps"] == 0


# ════════════════════════════════════════════════════════════════════
#  6. STATISTICAL INVARIANTS
# ════════════════════════════════════════════════════════════════════
class TestStatisticalInvariants:

    def test_win_plus_loss_equals_one_mixed(self):
        ds = _basic_dataset(7)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.3))
        s = r["backtest_summary"]
        assert s["win_rate"] + s["loss_rate"] == pytest.approx(1.0, abs=1e-9)

    def test_win_plus_loss_equals_one_all_wins(self):
        ds = _basic_dataset(3)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(1.0))
        s = r["backtest_summary"]
        assert s["win_rate"] + s["loss_rate"] == pytest.approx(1.0, abs=1e-9)

    def test_win_plus_loss_zero_when_dataset_empty(self):
        r = run_backtest([], _mock_strategy_always_buy, **_pipeline_mocks(0.0))
        s = r["backtest_summary"]
        # Empty caso especial: ambos 0
        assert s["win_rate"] == 0.0
        assert s["loss_rate"] == 0.0

    def test_drawdown_non_negative(self):
        ds = _basic_dataset(10)
        for pnl in [-2.0, -1.0, 0.0, 0.5, 1.0]:
            r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(pnl))
            assert r["backtest_summary"]["max_drawdown"] >= 0.0

    def test_total_pnl_matches_equity_final_minus_initial(self):
        ds = _basic_dataset(6)
        r = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.7))
        diff = r["equity_curve"][-1] - r["equity_curve"][0]
        assert r["backtest_summary"]["total_pnl"] == pytest.approx(diff, abs=1e-9)


# ════════════════════════════════════════════════════════════════════
#  7. PURE FUNCTION / STATELESSNESS
# ════════════════════════════════════════════════════════════════════
class TestPurity:

    def test_two_separate_invocations_dont_share_state(self):
        ds = _basic_dataset(4)
        r1 = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(1.0))
        r2 = run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(-1.0))
        # Independientes
        assert r1["backtest_summary"]["total_pnl"] == 4.0
        assert r2["backtest_summary"]["total_pnl"] == -4.0

    def test_dataset_not_mutated(self):
        ds = _basic_dataset(3)
        ds_snapshot = json.dumps(ds, sort_keys=True)
        run_backtest(ds, _mock_strategy_always_buy, **_pipeline_mocks(0.5))
        assert json.dumps(ds, sort_keys=True) == ds_snapshot


# ════════════════════════════════════════════════════════════════════
#  8. SCHEMA VALIDATION
# ════════════════════════════════════════════════════════════════════
class TestSchemaValidation:

    EXPECTED_TOP = {"backtest_summary", "execution_stats", "risk_metrics", "equity_curve"}
    EXPECTED_SUMMARY = {"total_pnl", "avg_pnl_per_step", "max_drawdown",
                         "win_rate", "loss_rate"}
    EXPECTED_STATS = {"total_steps", "total_orders", "avg_fill_rate",
                       "avg_slippage"}
    EXPECTED_RISK = {"volatility", "sharpe_ratio", "efficiency_score"}

    def _basic_run(self):
        return run_backtest(_basic_dataset(3), _mock_strategy_always_buy,
                              **_pipeline_mocks(0.5))

    def test_top_level_keys(self):
        r = self._basic_run()
        assert set(r.keys()) == self.EXPECTED_TOP

    def test_backtest_summary_keys(self):
        r = self._basic_run()
        assert set(r["backtest_summary"].keys()) == self.EXPECTED_SUMMARY

    def test_execution_stats_keys(self):
        r = self._basic_run()
        assert set(r["execution_stats"].keys()) == self.EXPECTED_STATS

    def test_risk_metrics_keys(self):
        r = self._basic_run()
        assert set(r["risk_metrics"].keys()) == self.EXPECTED_RISK

    def test_equity_curve_is_list_of_floats(self):
        r = self._basic_run()
        assert isinstance(r["equity_curve"], list)
        for v in r["equity_curve"]:
            assert isinstance(v, float)

    def test_total_steps_is_int(self):
        r = self._basic_run()
        assert isinstance(r["execution_stats"]["total_steps"], int)
        assert isinstance(r["execution_stats"]["total_orders"], int)

    def test_numeric_metrics_are_floats(self):
        r = self._basic_run()
        for key in self.EXPECTED_SUMMARY:
            assert isinstance(r["backtest_summary"][key], float)
        for key in self.EXPECTED_RISK:
            assert isinstance(r["risk_metrics"][key], float)

    def test_json_serializable(self):
        r = self._basic_run()
        s = json.dumps(r)
        assert isinstance(s, str) and len(s) > 0


# ════════════════════════════════════════════════════════════════════
#  9. DEFAULT PIPELINE INTEGRATION (real M19+M21+M22)
# ════════════════════════════════════════════════════════════════════
class TestDefaultPipelineIntegration:
    """Tests end-to-end con el wiring real de M19/M21/M22. Estos tests
    NO violan el aislamiento del CORE de M23 — usan el módulo
    `default_pipeline.py` que es el único autorizado a importarlos."""

    def _real_dataset(self, n: int = 5) -> list:
        return [
            {
                "event_id": f"evt-{i}",
                "timestamp": f"2026-05-14T00:{i:02d}:00Z",
                "signals": {
                    "volatility": 0.1 + 0.05 * i,
                    "liquidity": 0.6,
                    "volume": 1000 + 100 * i,
                },
                "external_factors": {"macro_risk": 0.2},
            }
            for i in range(n)
        ]

    def _simple_real_strategy(self, snapshot: dict) -> dict:
        cur = (snapshot or {}).get("current", {})
        rates = cur.get("rates", {})
        block = rates.get("purchase_block_rate", 0.5)
        if block < 0.5:
            return {"actions": [
                {"symbol": "X", "side": "buy", "quantity": 1.0,
                 "order_type": "market"},
            ]}
        return {"actions": []}

    def test_default_pipeline_runs_end_to_end(self):
        ds = self._real_dataset(5)
        r = run_default_backtest(ds, self._simple_real_strategy)
        assert r["execution_stats"]["total_steps"] == 5

    def test_default_pipeline_schema_intact(self):
        ds = self._real_dataset(3)
        r = run_default_backtest(ds, self._simple_real_strategy)
        assert set(r.keys()) == {
            "backtest_summary", "execution_stats", "risk_metrics", "equity_curve",
        }

    def test_default_pipeline_invariant_holds(self):
        ds = self._real_dataset(10)
        r = run_default_backtest(ds, self._simple_real_strategy)
        s = r["backtest_summary"]
        assert s["win_rate"] + s["loss_rate"] == pytest.approx(1.0, abs=1e-9)

    def test_default_pipeline_deterministic(self):
        ds = self._real_dataset(7)
        r1 = run_default_backtest(ds, self._simple_real_strategy)
        r2 = run_default_backtest(ds, self._simple_real_strategy)
        assert r1 == r2

    def test_default_pipeline_callables_factory(self):
        defaults = default_pipeline_callables()
        assert "adapter_callable" in defaults
        assert "executor_callable" in defaults
        assert "reporter_callable" in defaults
        assert all(callable(v) for v in defaults.values())


# ════════════════════════════════════════════════════════════════════
#  10. PUBLIC API
# ════════════════════════════════════════════════════════════════════
class TestPublicAPI:

    def test_run_backtest_callable(self):
        assert callable(run_backtest)

    def test_run_default_backtest_callable(self):
        assert callable(run_default_backtest)

    def test_metrics_primitives_callable(self):
        assert callable(max_drawdown)
        assert callable(sharpe_ratio)
        assert callable(win_loss_rates)
        assert callable(mean)
        assert callable(stddev)

    def test_default_mode_is_simulation(self):
        assert DEFAULT_MODE == "simulation"
        assert INITIAL_EQUITY == 0.0
