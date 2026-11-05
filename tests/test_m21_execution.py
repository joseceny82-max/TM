"""tests/test_m21_execution.py — Test suite for M21 Execution Layer.

Cubre las 7 categorías mandatadas por el spec M21:

    1. Determinism            — mismo input → mismo output bit-exact
    2. Translation correctness— decision.actions → Orders correctos
    3. Simulation vs Live iso — live nunca ejecuta lógica real
    4. Safety                 — live broker SIEMPRE bloqueado
    5. Schema validation      — output siempre válido
    6. Isolation              — M21 NO importa M19 ni M20 (AST)
    7. Edge cases             — empty / invalid qty / missing fields

Extras:
    8. Fill-rate sanity       — agregado ≈ 95%/70% over many orders
    9. Public API surface     — todos los símbolos importables

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


from core.execution.m21 import (
    DEFAULT_EXECUTION_MODE,
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_SIMULATION,
    LIMIT_FILL_THRESHOLD,
    LIVE_MODE_DISABLED_ERROR,
    MARKET_FILL_THRESHOLD,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    STATUS_BLOCKED,
    STATUS_FILLED,
    STATUS_REJECTED,
    STATUS_SIMULATED,
    VALID_STATUSES,
    ExecutionEngine,
    ExecutionResult,
    LiveBroker,
    Order,
    SimulatedBroker,
    execute_decision,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
def _action(symbol: str = "TST", side: str = "buy", qty: float = 1.0,
            order_type: str = "market", price=None,
            timestamp: str = "") -> dict:
    a = {
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "order_type": order_type,
        "timestamp": timestamp,
    }
    if price is not None:
        a["price"] = price
    return a


def _decision(*actions: dict) -> dict:
    return {"actions": list(actions)}


# ════════════════════════════════════════════════════════════════════
#  1. DETERMINISM
# ════════════════════════════════════════════════════════════════════
class TestDeterminism:

    def test_same_input_same_output(self):
        d = _decision(_action("AAPL", "buy", 10.0, "market"))
        r1 = execute_decision(d)
        r2 = execute_decision(d)
        assert r1 == r2

    def test_repeated_calls_identical(self):
        d = _decision(
            _action("X", "buy", 5.0, "market"),
            _action("Y", "sell", 3.0, "limit", price=200.0),
        )
        results = [execute_decision(d) for _ in range(8)]
        assert all(r == results[0] for r in results)

    def test_order_id_deterministic(self):
        d = _decision(_action("Z", "buy", 7.0, "market"))
        r1 = execute_decision(d)
        r2 = execute_decision(d)
        assert r1["results"][0]["order_id"] == r2["results"][0]["order_id"]

    def test_latency_deterministic(self):
        d = _decision(_action("LAT", "buy", 1.0, "market"))
        r1 = execute_decision(d)
        r2 = execute_decision(d)
        assert r1["results"][0]["latency_ms"] == r2["results"][0]["latency_ms"]


# ════════════════════════════════════════════════════════════════════
#  2. TRANSLATION CORRECTNESS
# ════════════════════════════════════════════════════════════════════
class TestTranslationCorrectness:

    def test_action_count_equals_orders_sent(self):
        d = _decision(
            _action("A", "buy", 1.0, "market"),
            _action("B", "sell", 2.0, "limit", price=100.0),
            _action("C", "buy", 3.0, "market"),
        )
        r = execute_decision(d)
        assert r["orders_sent"] == 3
        assert len(r["results"]) == 3

    def test_unique_order_ids_per_action(self):
        # Distintos índices → distintos order_ids aún con misma acción
        d = _decision(
            _action("S", "buy", 1.0, "market"),
            _action("S", "buy", 1.0, "market"),
        )
        r = execute_decision(d)
        ids = [x["order_id"] for x in r["results"]]
        assert len(set(ids)) == 2

    def test_explicit_price_preserved_for_limit_fill(self):
        d = _decision(_action("L", "buy", 1.0, "limit", price=137.42))
        r = execute_decision(d)
        # Si fill llega, avg_price debe ser exactamente 137.42
        if r["results"][0]["status"] == STATUS_FILLED:
            assert r["results"][0]["avg_price"] == 137.42

    def test_filled_quantity_matches_order_quantity_when_filled(self):
        d = _decision(_action("Q", "buy", 42.0, "market"))
        r = execute_decision(d)
        if r["results"][0]["status"] == STATUS_FILLED:
            assert r["results"][0]["filled_quantity"] == 42.0

    def test_invalid_quantity_zero_rejected(self):
        d = _decision(_action("ZERO", "buy", 0.0, "market"))
        r = execute_decision(d)
        assert r["results"][0]["status"] == STATUS_REJECTED
        assert r["results"][0]["error"] == "invalid_quantity"

    def test_invalid_quantity_negative_rejected(self):
        d = _decision(_action("NEG", "buy", -5.0, "market"))
        r = execute_decision(d)
        assert r["results"][0]["status"] == STATUS_REJECTED


# ════════════════════════════════════════════════════════════════════
#  3. SIMULATION vs LIVE ISOLATION
# ════════════════════════════════════════════════════════════════════
class TestSimulationLiveIsolation:

    def test_simulation_mode_uses_simulated_broker(self):
        eng = ExecutionEngine(mode="simulation")
        assert isinstance(eng.broker, SimulatedBroker)

    def test_live_mode_uses_live_broker(self):
        eng = ExecutionEngine(mode="live")
        assert isinstance(eng.broker, LiveBroker)

    def test_simulation_and_live_produce_different_statuses(self):
        d = _decision(_action("X", "buy", 1.0, "market"))
        sim = execute_decision(d, mode="simulation")
        live = execute_decision(d, mode="live")
        sim_status = sim["results"][0]["status"]
        live_status = live["results"][0]["status"]
        assert sim_status != STATUS_BLOCKED
        assert live_status == STATUS_BLOCKED

    def test_invalid_mode_falls_back_to_simulation(self):
        d = _decision(_action("X", "buy", 1.0, "market"))
        r = execute_decision(d, mode="INVALID_NONSENSE")
        assert r["execution_mode"] == DEFAULT_EXECUTION_MODE


# ════════════════════════════════════════════════════════════════════
#  4. SAFETY — Live broker SIEMPRE bloqueado
# ════════════════════════════════════════════════════════════════════
class TestSafetyLiveBlocked:

    def test_live_broker_returns_blocked_status(self):
        broker = LiveBroker()
        order = Order(
            order_id="ord-1", symbol="X", side="buy",
            quantity=1.0, order_type="market",
        )
        r = broker.submit_order(order)
        assert r["status"] == STATUS_BLOCKED

    def test_live_broker_error_is_live_mode_disabled(self):
        broker = LiveBroker()
        order = Order(
            order_id="ord-2", symbol="Y", side="sell",
            quantity=10.0, order_type="limit", price=100.0,
        )
        r = broker.submit_order(order)
        assert r["error"] == LIVE_MODE_DISABLED_ERROR

    def test_live_broker_filled_quantity_zero(self):
        broker = LiveBroker()
        order = Order(
            order_id="ord-3", symbol="Z", side="buy",
            quantity=999.0, order_type="market",
        )
        r = broker.submit_order(order)
        assert r["filled_quantity"] == 0.0
        assert r["avg_price"] == 0.0

    def test_live_mode_all_orders_blocked(self):
        d = _decision(
            _action("A", "buy", 1.0, "market"),
            _action("B", "sell", 2.0, "limit", price=50.0),
            _action("C", "buy", 100.0, "market"),
        )
        r = execute_decision(d, mode="live")
        assert all(x["status"] == STATUS_BLOCKED for x in r["results"])
        assert all(x["error"] == LIVE_MODE_DISABLED_ERROR for x in r["results"])

    def test_live_broker_returns_defensive_copy(self):
        broker = LiveBroker()
        order = Order(
            order_id="ord-4", symbol="W", side="buy",
            quantity=1.0, order_type="market",
        )
        r1 = broker.submit_order(order)
        r1["error"] = "TAMPERED"  # mutate the returned dict
        r2 = broker.submit_order(order)
        assert r2["error"] == LIVE_MODE_DISABLED_ERROR  # not tampered


# ════════════════════════════════════════════════════════════════════
#  5. SCHEMA VALIDATION
# ════════════════════════════════════════════════════════════════════
class TestSchemaValidation:

    REQUIRED_TOP_KEYS = {"execution_mode", "orders_sent", "results"}
    REQUIRED_RESULT_KEYS = {
        "order_id", "status", "filled_quantity", "avg_price",
        "latency_ms", "error",
    }

    def test_output_has_required_top_keys(self):
        r = execute_decision(_decision(_action()))
        assert set(r.keys()) == self.REQUIRED_TOP_KEYS

    def test_execution_mode_is_string(self):
        r = execute_decision(_decision(_action()))
        assert isinstance(r["execution_mode"], str)

    def test_orders_sent_is_int(self):
        r = execute_decision(_decision(_action()))
        assert isinstance(r["orders_sent"], int)

    def test_results_is_list(self):
        r = execute_decision(_decision(_action()))
        assert isinstance(r["results"], list)

    def test_result_entries_have_required_keys(self):
        d = _decision(_action(), _action("X", "sell", 2.0, "limit", price=50.0))
        r = execute_decision(d)
        for entry in r["results"]:
            assert set(entry.keys()) == self.REQUIRED_RESULT_KEYS

    def test_status_in_valid_set(self):
        d = _decision(
            _action("F1", "buy", 1.0, "market"),
            _action("F2", "buy", 0.0, "market"),     # rejected
        )
        r_sim = execute_decision(d, mode="simulation")
        r_live = execute_decision(d, mode="live")
        for x in r_sim["results"] + r_live["results"]:
            assert x["status"] in VALID_STATUSES

    def test_filled_quantity_and_avg_price_are_floats(self):
        r = execute_decision(_decision(_action()))
        x = r["results"][0]
        assert isinstance(x["filled_quantity"], float)
        assert isinstance(x["avg_price"], float)

    def test_latency_ms_is_int(self):
        r = execute_decision(_decision(_action()))
        assert isinstance(r["results"][0]["latency_ms"], int)

    def test_output_is_json_serializable(self):
        r = execute_decision(_decision(_action(), _action("Y", "sell", 5.0)))
        # No debe explotar al serializar
        s = json.dumps(r)
        assert isinstance(s, str) and len(s) > 0


# ════════════════════════════════════════════════════════════════════
#  6. ISOLATION — M21 NO importa M19 ni M20 (AST scan)
# ════════════════════════════════════════════════════════════════════
class TestIsolation:

    M21_DIR = PROJ_ROOT / "core" / "execution" / "m21"

    FORBIDDEN_TOP_MODULES = {
        "core.observability.adapter",
        "core.observability.adapter.domain_adapter",
        "core.observability.domain_adapter",
        "core.observability.simulation_fidelity",
        "core.observability.shadow",
        "core.observability.shadow.shadow_observer",
        "core.observability.drift",
        "core.observability.risk",
        "core.observability.readiness",
        "core.observability.autonomy",
        "core.observability.governance",
        "core.observability.policy",
        "core.observability.audit",
        "core.observability.explainability",
    }

    FORBIDDEN_STDLIB = {
        "random", "subprocess", "socket", "threading",
        "multiprocessing", "asyncio",
    }

    def _all_m21_files(self):
        return sorted(self.M21_DIR.glob("*.py"))

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

    def test_no_m21_file_imports_observability(self):
        for path in self._all_m21_files():
            imports = self._module_imports(path)
            for imp in imports:
                for forbidden in self.FORBIDDEN_TOP_MODULES:
                    assert not imp.startswith(forbidden), (
                        f"{path.name} importa observability ({imp}) — "
                        f"prohibido por contrato M21"
                    )

    def test_no_m21_file_imports_random_or_async(self):
        for path in self._all_m21_files():
            imports = self._module_imports(path)
            forbidden_seen = imports & self.FORBIDDEN_STDLIB
            assert not forbidden_seen, (
                f"{path.name} importa stdlib prohibido: {forbidden_seen}"
            )

    def test_no_m21_file_imports_M13_M18_layers(self):
        # M13-M18 viven dentro de core.observability.* — ya cubierto arriba.
        # Aquí verificamos también que NO se importa `core.observability`
        # como módulo padre.
        for path in self._all_m21_files():
            imports = self._module_imports(path)
            assert "core.observability" not in imports, (
                f"{path.name} importa core.observability directo"
            )

    def test_m21_modules_only_import_within_m21_or_stdlib(self):
        allowed_prefixes = (
            "core.execution.m21",  # self
        )
        allowed_stdlib = {
            "hashlib", "typing", "dataclasses", "__future__",
        }
        for path in self._all_m21_files():
            imports = self._module_imports(path)
            for imp in imports:
                if imp in allowed_stdlib:
                    continue
                if imp.split(".")[0] in allowed_stdlib:
                    continue
                # core.execution.m21.* permitido
                ok = any(imp.startswith(p) for p in allowed_prefixes)
                assert ok, (
                    f"{path.name} importa módulo no permitido: {imp}"
                )


# ════════════════════════════════════════════════════════════════════
#  7. EDGE CASES
# ════════════════════════════════════════════════════════════════════
class TestEdgeCases:

    def test_empty_decision_returns_empty_results(self):
        r = execute_decision({})
        assert r["orders_sent"] == 0
        assert r["results"] == []

    def test_decision_none_returns_empty(self):
        r = execute_decision(None)
        assert r["orders_sent"] == 0
        assert r["results"] == []

    def test_actions_none_returns_empty(self):
        r = execute_decision({"actions": None})
        assert r["orders_sent"] == 0
        assert r["results"] == []

    def test_actions_not_a_list(self):
        r = execute_decision({"actions": "not-a-list"})
        assert r["orders_sent"] == 0

    def test_action_with_missing_fields(self):
        # No symbol, side, qty, order_type — sólo {}
        r = execute_decision({"actions": [{}]})
        assert r["orders_sent"] == 1
        # qty default 0.0 → rejected
        assert r["results"][0]["status"] == STATUS_REJECTED

    def test_action_with_invalid_side(self):
        # Side inválido → cae a default "buy"
        r = execute_decision({"actions": [
            {"symbol": "X", "side": "INVALID", "quantity": 1.0,
             "order_type": "market"},
        ]})
        # No debe explotar — orden se procesa con side=buy
        assert r["orders_sent"] == 1
        assert r["results"][0]["status"] != STATUS_REJECTED or \
               r["results"][0]["error"] != "invalid_quantity"

    def test_action_with_invalid_order_type(self):
        # Order type inválido → cae a default "market"
        r = execute_decision({"actions": [
            {"symbol": "X", "side": "buy", "quantity": 1.0,
             "order_type": "WEIRD_TYPE"},
        ]})
        # Normalizado a market, procesado normalmente
        assert r["orders_sent"] == 1

    def test_action_with_string_quantity(self):
        r = execute_decision({"actions": [
            {"symbol": "X", "side": "buy", "quantity": "5.5",
             "order_type": "market"},
        ]})
        assert r["orders_sent"] == 1
        # "5.5" coerce a 5.5 → no rejected por qty
        assert r["results"][0]["status"] != STATUS_REJECTED or \
               r["results"][0]["error"] != "invalid_quantity"

    def test_action_non_dict_entry(self):
        # Una entry de la lista no es dict → procesar como vacía
        r = execute_decision({"actions": [None, "string", 42]})
        assert r["orders_sent"] == 3
        # Todas rejected por qty=0
        for x in r["results"]:
            assert x["status"] == STATUS_REJECTED

    def test_boolean_quantity_treated_as_invalid(self):
        r = execute_decision({"actions": [
            {"symbol": "X", "side": "buy", "quantity": True,
             "order_type": "market"},
        ]})
        # bool → default 0.0 → rejected
        assert r["results"][0]["status"] == STATUS_REJECTED


# ════════════════════════════════════════════════════════════════════
#  8. FILL-RATE SANITY (statistical — large N → ~target)
# ════════════════════════════════════════════════════════════════════
class TestFillRateSanity:

    def test_market_fill_rate_near_95_over_many_orders(self):
        N = 500
        actions = [
            _action(f"SYM{i}", "buy", 1.0, "market") for i in range(N)
        ]
        r = execute_decision({"actions": actions})
        filled = sum(1 for x in r["results"] if x["status"] == STATUS_FILLED)
        ratio = filled / N
        # Tolerancia ±7% sobre 95% target
        assert 0.88 <= ratio <= 1.0, (
            f"Market fill ratio {ratio:.3f} fuera de banda razonable. "
            f"Esperado ~{MARKET_FILL_THRESHOLD/100:.2f}"
        )

    def test_limit_fill_rate_near_70_over_many_orders(self):
        N = 500
        actions = [
            _action(f"SYM{i}", "buy", 1.0, "limit", price=100.0)
            for i in range(N)
        ]
        r = execute_decision({"actions": actions})
        filled = sum(1 for x in r["results"] if x["status"] == STATUS_FILLED)
        ratio = filled / N
        # Tolerancia ±8% sobre 70% target
        assert 0.62 <= ratio <= 0.78, (
            f"Limit fill ratio {ratio:.3f} fuera de banda razonable. "
            f"Esperado ~{LIMIT_FILL_THRESHOLD/100:.2f}"
        )


# ════════════════════════════════════════════════════════════════════
#  9. PUBLIC API SURFACE
# ════════════════════════════════════════════════════════════════════
class TestPublicAPI:

    def test_execute_decision_is_callable(self):
        assert callable(execute_decision)

    def test_order_is_frozen_dataclass(self):
        o = Order(
            order_id="x", symbol="A", side="buy",
            quantity=1.0, order_type="market",
        )
        with pytest.raises((AttributeError, Exception)):
            o.order_id = "tampered"  # frozen → no debe permitir

    def test_execution_result_is_frozen_dataclass(self):
        e = ExecutionResult(
            order_id="x", status="filled",
            filled_quantity=1.0, avg_price=100.0, latency_ms=10,
        )
        with pytest.raises((AttributeError, Exception)):
            e.status = "tampered"

    def test_constants_available(self):
        assert STATUS_FILLED == "filled"
        assert STATUS_REJECTED == "rejected"
        assert STATUS_SIMULATED == "simulated"
        assert STATUS_BLOCKED == "blocked"
        assert ORDER_TYPE_MARKET == "market"
        assert ORDER_TYPE_LIMIT == "limit"
        assert EXECUTION_MODE_SIMULATION == "simulation"
        assert EXECUTION_MODE_LIVE == "live"

    def test_default_mode_is_simulation(self):
        d = _decision(_action())
        r = execute_decision(d)  # sin mode
        assert r["execution_mode"] == "simulation"
