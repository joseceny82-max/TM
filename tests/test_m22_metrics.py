"""tests/test_m22_metrics.py — Test suite for M22 Trading Metrics Layer.

Cubre las 6 categorías mandatadas por spec M22:

    1. Determinism            — mismo execution_result → mismo report
    2. PnL correctness        — filled orders → PnL coherente
    3. Slippage consistency   — latency × vol_proxy → slippage estable
    4. Metrics validity       — fill_rate + rejection_rate == 1.0
    5. Edge cases             — empty / all rejected / missing fields / zero
    6. Isolation              — M22 NO importa M19, M20 ni M21

Extras:
    7. Schema validation
    8. Trade reconstruction
    9. Public API surface

NO importa M21 — todos los execution_result dicts se construyen
manualmente. Esto valida que M22 es un consumidor de SHAPE, no de TIPO.

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


from core.analytics.m22 import (
    EFFICIENCY_SLIPPAGE_PENALTY,
    LATENCY_REFERENCE_MS,
    LIMIT_ORDER_MULTIPLIER,
    MARKET_ORDER_MULTIPLIER,
    OUTCOME_EXECUTED,
    OUTCOME_NOT_EXECUTED,
    SLIPPAGE_LATENCY_COEFF,
    VOLATILITY_PROXY_DEFAULT,
    apply_limit_multiplier,
    apply_market_multiplier,
    build_trading_report,
    compute_metrics,
    compute_pnl,
    compute_slippage_per_order,
    compute_volatility_proxy,
    generate_report,
    per_order_slippage,
    reconstruct_trades,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures helpers — construyen execution_result M21-shaped sin importar M21
# ────────────────────────────────────────────────────────────────────
def _result(order_id: str, status: str, qty: float = 0.0,
            price: float = 0.0, latency: int = 0,
            error=None) -> dict:
    return {
        "order_id": order_id,
        "status": status,
        "filled_quantity": float(qty),
        "avg_price": float(price),
        "latency_ms": int(latency),
        "error": error,
    }


def _exec(*results: dict, mode: str = "simulation") -> dict:
    return {
        "execution_mode": mode,
        "orders_sent": len(results),
        "results": list(results),
    }


# ════════════════════════════════════════════════════════════════════
#  1. DETERMINISM
# ════════════════════════════════════════════════════════════════════
class TestDeterminism:

    def test_same_execution_same_report(self):
        er = _exec(
            _result("o1", "filled", qty=10.0, price=100.0, latency=12),
            _result("o2", "rejected", error="invalid_quantity"),
        )
        r1 = generate_report(er)
        r2 = generate_report(er)
        assert r1 == r2

    def test_repeated_calls_identical(self):
        er = _exec(
            _result("a", "filled", qty=5.0, price=50.0, latency=15),
            _result("b", "filled", qty=2.0, price=55.0, latency=22),
            _result("c", "simulated", error="no_fill_simulated"),
        )
        reports = [generate_report(er) for _ in range(10)]
        assert all(r == reports[0] for r in reports)

    def test_json_roundtrip_stable(self):
        er = _exec(
            _result("x", "filled", qty=1.0, price=10.0, latency=5),
        )
        r = generate_report(er)
        s = json.dumps(r, sort_keys=True)
        r2 = json.loads(s)
        assert r["pnl"]["total"] == r2["pnl"]["total"]
        assert r["slippage"]["avg"] == r2["slippage"]["avg"]


# ════════════════════════════════════════════════════════════════════
#  2. PnL CORRECTNESS
# ════════════════════════════════════════════════════════════════════
class TestPnLCorrectness:

    def test_no_filled_orders_zero_pnl(self):
        er = _exec(
            _result("o1", "rejected", error="invalid_quantity"),
            _result("o2", "simulated", error="no_fill_simulated"),
        )
        r = generate_report(er)
        assert r["pnl"]["realized"] == 0.0
        assert r["pnl"]["unrealized"] == 0.0
        assert r["pnl"]["total"] == 0.0

    def test_filled_orders_produce_negative_pnl_when_slippage(self):
        # 2 fills con dispersión de precios → vol_proxy > 0 → slippage > 0
        # → PnL = -slippage*qty → negativo
        er = _exec(
            _result("o1", "filled", qty=10.0, price=100.0, latency=20),
            _result("o2", "filled", qty=5.0, price=120.0, latency=25),
        )
        r = generate_report(er)
        assert r["pnl"]["realized"] <= 0.0  # implementation shortfall

    def test_zero_latency_zero_slippage_zero_pnl(self):
        # Latency=0 → slippage=0 → PnL=0
        er = _exec(
            _result("o1", "filled", qty=10.0, price=100.0, latency=0),
            _result("o2", "filled", qty=5.0, price=110.0, latency=0),
        )
        r = generate_report(er)
        assert r["pnl"]["realized"] == 0.0

    def test_total_equals_realized_plus_unrealized(self):
        er = _exec(
            _result("o1", "filled", qty=1.0, price=10.0, latency=10),
            _result("o2", "filled", qty=2.0, price=20.0, latency=15),
        )
        r = generate_report(er)
        assert r["pnl"]["total"] == round(
            r["pnl"]["realized"] + r["pnl"]["unrealized"], 8
        )

    def test_per_order_pnl_via_engine(self):
        er = _exec(
            _result("a", "filled", qty=10.0, price=100.0, latency=10),
            _result("b", "filled", qty=10.0, price=110.0, latency=10),
            _result("c", "rejected"),
        )
        slip = compute_slippage_per_order(er)
        pnl = compute_pnl(er, slip)
        assert "a" in pnl["per_order"]
        assert "b" in pnl["per_order"]
        # c rejected → PnL = 0
        assert pnl["per_order"]["c"] == 0.0

    def test_unrealized_is_always_zero_stateless(self):
        er = _exec(
            _result("o1", "filled", qty=100.0, price=50.0, latency=50),
        )
        r = generate_report(er)
        assert r["pnl"]["unrealized"] == 0.0


# ════════════════════════════════════════════════════════════════════
#  3. SLIPPAGE CONSISTENCY
# ════════════════════════════════════════════════════════════════════
class TestSlippageConsistency:

    def test_zero_latency_zero_slippage(self):
        assert per_order_slippage(0, 0.5) == 0.0
        assert per_order_slippage(0, 1.0) == 0.0

    def test_higher_latency_higher_slippage(self):
        s1 = per_order_slippage(10, 0.5)
        s2 = per_order_slippage(100, 0.5)
        assert s2 > s1

    def test_higher_volatility_higher_slippage(self):
        s_low = per_order_slippage(50, 0.1)
        s_high = per_order_slippage(50, 0.9)
        assert s_high > s_low

    def test_slippage_formula_literal(self):
        # spec literal: slippage = latency_ms × 0.001 × volatility_proxy
        s = per_order_slippage(20, 0.5)
        expected = round(20 * SLIPPAGE_LATENCY_COEFF * 0.5, 8)
        assert s == expected

    def test_volatility_proxy_default_when_lt_2_fills(self):
        er = _exec(
            _result("o1", "filled", qty=1.0, price=100.0, latency=10),
        )
        vp = compute_volatility_proxy(er)
        assert vp == VOLATILITY_PROXY_DEFAULT

    def test_volatility_proxy_zero_when_flat_prices(self):
        er = _exec(
            _result("o1", "filled", qty=1.0, price=100.0, latency=10),
            _result("o2", "filled", qty=2.0, price=100.0, latency=15),
            _result("o3", "filled", qty=3.0, price=100.0, latency=20),
        )
        vp = compute_volatility_proxy(er)
        assert vp == 0.0  # no dispersion

    def test_volatility_proxy_bounded_unit(self):
        er = _exec(
            _result("o1", "filled", qty=1.0, price=10.0, latency=10),
            _result("o2", "filled", qty=2.0, price=1000.0, latency=15),
        )
        vp = compute_volatility_proxy(er)
        assert 0.0 <= vp <= 1.0

    def test_slippage_per_order_only_for_fills(self):
        er = _exec(
            _result("filled1", "filled", qty=1.0, price=100.0, latency=10),
            _result("rejected1", "rejected", error="invalid_quantity"),
            _result("simulated1", "simulated"),
            _result("blocked1", "blocked", error="live_mode_disabled"),
        )
        slip = compute_slippage_per_order(er)
        # Non-fills → 0 slippage
        assert slip["rejected1"] == 0.0
        assert slip["simulated1"] == 0.0
        assert slip["blocked1"] == 0.0

    def test_market_multiplier_increases_slippage(self):
        base = 0.05
        assert apply_market_multiplier(base) == round(
            base * MARKET_ORDER_MULTIPLIER, 8
        )

    def test_limit_multiplier_zero_per_spec(self):
        # Per spec "limit orders: menor o cero" — implementación es 0
        assert apply_limit_multiplier(0.05) == 0.0
        assert LIMIT_ORDER_MULTIPLIER == 0.0


# ════════════════════════════════════════════════════════════════════
#  4. METRICS VALIDITY — fill_rate + rejection_rate == 1.0
# ════════════════════════════════════════════════════════════════════
class TestMetricsValidity:

    def test_fill_plus_rejection_equals_one_mixed(self):
        er = _exec(
            _result("o1", "filled", qty=1.0, price=10.0, latency=5),
            _result("o2", "rejected", error="invalid_quantity"),
            _result("o3", "simulated"),
        )
        m = compute_metrics(er)
        assert m["fill_rate"] + m["rejection_rate"] == pytest.approx(1.0, abs=1e-9)

    def test_fill_plus_rejection_one_when_all_filled(self):
        er = _exec(
            _result("o1", "filled", qty=1.0, price=10.0, latency=5),
            _result("o2", "filled", qty=2.0, price=20.0, latency=10),
        )
        m = compute_metrics(er)
        assert m["fill_rate"] == 1.0
        assert m["rejection_rate"] == 0.0

    def test_fill_plus_rejection_one_when_all_rejected(self):
        er = _exec(
            _result("o1", "rejected", error="invalid_quantity"),
            _result("o2", "rejected", error="invalid_quantity"),
        )
        m = compute_metrics(er)
        assert m["fill_rate"] == 0.0
        assert m["rejection_rate"] == 1.0

    def test_latency_avg_correct(self):
        er = _exec(
            _result("o1", "filled", qty=1.0, price=10.0, latency=10),
            _result("o2", "filled", qty=1.0, price=10.0, latency=20),
            _result("o3", "filled", qty=1.0, price=10.0, latency=30),
        )
        m = compute_metrics(er)
        assert m["latency_avg_ms"] == 20.0

    def test_execution_efficiency_in_unit_interval(self):
        er = _exec(
            _result("o1", "filled", qty=1.0, price=10.0, latency=5),
        )
        m = compute_metrics(er)
        assert 0.0 <= m["execution_efficiency"] <= 1.0

    def test_efficiency_higher_when_lower_latency(self):
        er_fast = _exec(
            _result("o1", "filled", qty=1.0, price=100.0, latency=5),
        )
        er_slow = _exec(
            _result("o1", "filled", qty=1.0, price=100.0, latency=80),
        )
        m_fast = compute_metrics(er_fast)
        m_slow = compute_metrics(er_slow)
        assert m_fast["execution_efficiency"] >= m_slow["execution_efficiency"]


# ════════════════════════════════════════════════════════════════════
#  5. EDGE CASES
# ════════════════════════════════════════════════════════════════════
class TestEdgeCases:

    def test_empty_execution(self):
        r = generate_report({"execution_mode": "simulation", "orders_sent": 0, "results": []})
        assert r["pnl"]["total"] == 0.0
        assert r["slippage"]["avg"] == 0.0
        assert r["execution_metrics"]["fill_rate"] == 0.0
        assert r["execution_metrics"]["rejection_rate"] == 0.0

    def test_none_input(self):
        r = generate_report(None)
        # Fail-safe → empty report shape
        assert r["pnl"]["total"] == 0.0
        assert r["execution_metrics"]["fill_rate"] == 0.0

    def test_non_dict_input(self):
        r = generate_report("garbage")
        assert r["pnl"]["total"] == 0.0

    def test_all_rejected(self):
        er = _exec(
            _result("o1", "rejected", error="invalid_quantity"),
            _result("o2", "rejected", error="invalid_quantity"),
            _result("o3", "rejected", error="invalid_order_type:??"),
        )
        r = generate_report(er)
        assert r["pnl"]["total"] == 0.0
        assert r["execution_metrics"]["fill_rate"] == 0.0
        assert r["execution_metrics"]["rejection_rate"] == 1.0

    def test_missing_results_key(self):
        r = generate_report({"execution_mode": "simulation", "orders_sent": 5})
        # No results key → empty list internally
        assert r["execution_metrics"]["fill_rate"] == 0.0

    def test_results_not_a_list(self):
        r = generate_report({"results": "not-a-list"})
        assert r["pnl"]["total"] == 0.0

    def test_result_entries_with_missing_fields(self):
        er = {"results": [
            {"order_id": "o1", "status": "filled"},  # missing qty, price, latency
            {"status": "rejected"},                   # missing order_id
            {},                                       # empty
        ]}
        r = generate_report(er)
        # No debe explotar
        assert isinstance(r, dict)
        assert r["execution_metrics"]["fill_rate"] + \
               r["execution_metrics"]["rejection_rate"] == pytest.approx(1.0, abs=1e-9)

    def test_zero_quantity_filled(self):
        # Quantity 0 con status "filled" — caso degenerado
        er = _exec(
            _result("o1", "filled", qty=0.0, price=100.0, latency=10),
        )
        r = generate_report(er)
        # PnL = -slip × 0 = 0
        assert r["pnl"]["realized"] == 0.0

    def test_negative_latency_treated_as_zero(self):
        # Defensive: negative latency no debe romper
        er = _exec(
            _result("o1", "filled", qty=1.0, price=100.0, latency=-50),
        )
        r = generate_report(er)
        # No crash, slippage clamped
        assert isinstance(r, dict)
        assert r["slippage"]["max"] >= 0.0

    def test_non_dict_entry_in_results(self):
        er = {"results": ["string", 42, None,
                          _result("ok", "filled", qty=1.0, price=10.0, latency=5)]}
        r = generate_report(er)
        # 4 results: 3 malformed + 1 filled
        # Invariant must still hold
        assert r["execution_metrics"]["fill_rate"] + \
               r["execution_metrics"]["rejection_rate"] == pytest.approx(1.0, abs=1e-9)


# ════════════════════════════════════════════════════════════════════
#  6. ISOLATION — M22 NO importa M19, M20 ni M21 (AST scan)
# ════════════════════════════════════════════════════════════════════
class TestIsolation:

    M22_DIR = PROJ_ROOT / "core" / "analytics" / "m22"

    FORBIDDEN_PREFIXES = (
        "core.observability",   # M19, M20
        "core.execution",       # M21
    )

    FORBIDDEN_STDLIB = {
        "random", "subprocess", "socket", "threading",
        "multiprocessing", "asyncio",
    }

    def _all_m22_files(self):
        return sorted(self.M22_DIR.glob("*.py"))

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

    def test_no_m22_file_imports_observability_or_execution(self):
        for path in self._all_m22_files():
            imports = self._module_imports(path)
            for imp in imports:
                for forbidden in self.FORBIDDEN_PREFIXES:
                    assert not imp.startswith(forbidden), (
                        f"{path.name} importa {imp} — prohibido por contrato M22"
                    )

    def test_no_m22_file_imports_forbidden_stdlib(self):
        for path in self._all_m22_files():
            imports = self._module_imports(path)
            forbidden_seen = imports & self.FORBIDDEN_STDLIB
            assert not forbidden_seen, (
                f"{path.name} importa stdlib prohibido: {forbidden_seen}"
            )

    def test_m22_only_imports_within_m22_or_safe_stdlib(self):
        allowed_prefixes = ("core.analytics.m22",)
        allowed_stdlib = {
            "typing", "__future__", "dataclasses",
        }
        for path in self._all_m22_files():
            imports = self._module_imports(path)
            for imp in imports:
                if imp in allowed_stdlib:
                    continue
                if imp.split(".")[0] in allowed_stdlib:
                    continue
                ok = any(imp.startswith(p) for p in allowed_prefixes)
                assert ok, f"{path.name} importa módulo no permitido: {imp}"

    def test_test_file_itself_does_not_import_m21(self):
        """El propio test de M22 NO importa M21 — demuestra el contrato:
        M22 consume dict shape, no objetos M21. AST-based para evitar
        falsos positivos por menciones en docstrings/comentarios."""
        own_path = Path(__file__)
        own_tree = ast.parse(own_path.read_text(encoding="utf-8"))
        for node in ast.walk(own_tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    assert not n.name.startswith("core.execution.m21"), (
                        f"Test file importa {n.name} — prohibido"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert not node.module.startswith("core.execution.m21"), (
                        f"Test file importa from {node.module} — prohibido"
                    )


# ════════════════════════════════════════════════════════════════════
#  7. SCHEMA VALIDATION
# ════════════════════════════════════════════════════════════════════
class TestSchemaValidation:

    EXPECTED_TOP_KEYS = {"pnl", "slippage", "execution_metrics", "efficiency_score"}
    EXPECTED_PNL_KEYS = {"realized", "unrealized", "total"}
    EXPECTED_SLIPPAGE_KEYS = {"avg", "max"}
    EXPECTED_METRICS_KEYS = {"fill_rate", "rejection_rate", "latency_avg_ms"}

    def test_top_level_keys(self):
        r = generate_report(_exec(_result("o1", "filled", qty=1.0, price=10.0, latency=5)))
        assert set(r.keys()) == self.EXPECTED_TOP_KEYS

    def test_pnl_keys(self):
        r = generate_report(_exec(_result("o1", "filled", qty=1.0, price=10.0, latency=5)))
        assert set(r["pnl"].keys()) == self.EXPECTED_PNL_KEYS

    def test_slippage_keys(self):
        r = generate_report(_exec(_result("o1", "filled", qty=1.0, price=10.0, latency=5)))
        assert set(r["slippage"].keys()) == self.EXPECTED_SLIPPAGE_KEYS

    def test_execution_metrics_keys(self):
        r = generate_report(_exec(_result("o1", "filled", qty=1.0, price=10.0, latency=5)))
        assert set(r["execution_metrics"].keys()) == self.EXPECTED_METRICS_KEYS

    def test_all_numeric_values_are_floats(self):
        r = generate_report(_exec(
            _result("o1", "filled", qty=10.0, price=100.0, latency=15),
            _result("o2", "filled", qty=5.0, price=110.0, latency=20),
        ))
        assert isinstance(r["pnl"]["realized"], float)
        assert isinstance(r["pnl"]["unrealized"], float)
        assert isinstance(r["pnl"]["total"], float)
        assert isinstance(r["slippage"]["avg"], float)
        assert isinstance(r["slippage"]["max"], float)
        assert isinstance(r["execution_metrics"]["fill_rate"], float)
        assert isinstance(r["execution_metrics"]["rejection_rate"], float)
        assert isinstance(r["execution_metrics"]["latency_avg_ms"], float)
        assert isinstance(r["efficiency_score"], float)

    def test_efficiency_score_in_unit_interval(self):
        r = generate_report(_exec(_result("o1", "filled", qty=1.0, price=10.0, latency=5)))
        assert 0.0 <= r["efficiency_score"] <= 1.0

    def test_output_is_json_serializable(self):
        r = generate_report(_exec(
            _result("o1", "filled", qty=10.0, price=100.0, latency=15),
        ))
        s = json.dumps(r)
        assert isinstance(s, str) and len(s) > 0


# ════════════════════════════════════════════════════════════════════
#  8. TRADE RECONSTRUCTION
# ════════════════════════════════════════════════════════════════════
class TestTradeReconstruction:

    def test_reconstruct_one_trade_per_result(self):
        er = _exec(
            _result("o1", "filled", qty=10.0, price=100.0, latency=12),
            _result("o2", "rejected", error="invalid_quantity"),
            _result("o3", "simulated"),
        )
        trades = reconstruct_trades(er)
        assert len(trades) == 3

    def test_filled_has_outcome_executed(self):
        er = _exec(_result("o1", "filled", qty=10.0, price=100.0, latency=12))
        trades = reconstruct_trades(er)
        assert trades[0]["outcome"] == OUTCOME_EXECUTED

    def test_non_filled_has_outcome_not_executed(self):
        er = _exec(
            _result("o1", "rejected"),
            _result("o2", "simulated"),
            _result("o3", "blocked"),
        )
        trades = reconstruct_trades(er)
        for t in trades:
            assert t["outcome"] == OUTCOME_NOT_EXECUTED

    def test_intent_map_attached_when_provided(self):
        er = _exec(
            _result("o1", "filled", qty=10.0, price=100.0, latency=12),
            _result("o2", "rejected"),
        )
        intent = {"o1": {"reason": "signal_X"}, "o2": {"reason": "stop_loss"}}
        trades = reconstruct_trades(er, intent_map=intent)
        assert trades[0]["intent"] == {"reason": "signal_X"}
        assert trades[1]["intent"] == {"reason": "stop_loss"}

    def test_intent_omitted_when_no_map(self):
        er = _exec(_result("o1", "filled", qty=10.0, price=100.0, latency=12))
        trades = reconstruct_trades(er)
        assert "intent" not in trades[0]


# ════════════════════════════════════════════════════════════════════
#  9. PUBLIC API SURFACE
# ════════════════════════════════════════════════════════════════════
class TestPublicAPI:

    def test_generate_report_callable(self):
        assert callable(generate_report)

    def test_build_trading_report_alias(self):
        assert build_trading_report is generate_report

    def test_all_constants_exported(self):
        assert SLIPPAGE_LATENCY_COEFF == 0.001
        assert isinstance(VOLATILITY_PROXY_DEFAULT, float)
        assert isinstance(LATENCY_REFERENCE_MS, float)
        assert isinstance(EFFICIENCY_SLIPPAGE_PENALTY, float)
        assert MARKET_ORDER_MULTIPLIER >= 1.0
        assert LIMIT_ORDER_MULTIPLIER == 0.0
        assert OUTCOME_EXECUTED == "executed"
        assert OUTCOME_NOT_EXECUTED == "not_executed"

    def test_subengines_individually_callable(self):
        er = _exec(_result("o1", "filled", qty=1.0, price=10.0, latency=5))
        assert isinstance(compute_metrics(er), dict)
        assert isinstance(compute_pnl(er), dict)
        assert isinstance(compute_slippage_per_order(er), dict)
        assert isinstance(compute_volatility_proxy(er), float)
        assert isinstance(reconstruct_trades(er), list)
