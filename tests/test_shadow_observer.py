"""tests/test_shadow_observer.py — Test suite for M20 Shadow Observation.

Categorías:
    1. Determinism (3)         — misma entrada → misma salida bit-exact
    2. Schema validity (5)     — claves, tipos, rangos
    3. Stability index (5)     — comportamiento ante series planas / variadas
    4. Volatility detection (5)— oscilaciones detectadas / no
    5. Divergence detection (5)— saltos block / dry_run / errors / múltiples
    6. Edge cases (5)          — None, malformed, deeply nested, mixed
    7. Isolation (2)           — AST: no random, no IO, no subprocess
    8. Shadow state + trend (extras) — clasificación

Total ≥ 30 tests. Cero dependencias externas.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest


# Path injection para ejecutar standalone
PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.observability.shadow import (
    build_shadow_observation,
    compute_shadow_report,
)


# ────────────────────────────────────────────────────────────────────
#  Helpers de fixtures determinísticos
# ────────────────────────────────────────────────────────────────────
def _mk_snapshot(block: float = 0.0, dry: float = 0.5, errors: int = 0,
                  *, with_meta: bool = True, timestamp: str = "") -> dict:
    """Construye un TM_Snapshot canónico (compatible con M19)."""
    out: dict = {
        "history": [],
        "current": {
            "rates": {
                "purchase_dry_run_rate": float(dry),
                "purchase_block_rate": float(block),
            },
            "counters": {
                "purchase_live_count": 0,
                "internal_errors": int(errors),
            },
            "config": {"mode": "dry_run"},
        },
    }
    if with_meta:
        out["meta"] = {
            "mode": "simulation",
            "event_id": "fx-1",
            "timestamp": timestamp,
            "drift_hint": "low",
        }
    return out


def _mk_tick(block: float, dry: float = 0.5, errors: int = 0) -> dict:
    """Tick mínimo plano (sin nesting current/) para uso en history."""
    return {
        "rates": {
            "purchase_dry_run_rate": float(dry),
            "purchase_block_rate": float(block),
        },
        "counters": {
            "purchase_live_count": 0,
            "internal_errors": int(errors),
        },
    }


# ════════════════════════════════════════════════════════════════════
#  1. DETERMINISM
# ════════════════════════════════════════════════════════════════════
class TestDeterminism:

    def test_same_input_same_output(self):
        history = [_mk_tick(0.1), _mk_tick(0.15), _mk_tick(0.2)]
        current = _mk_snapshot(block=0.25)
        r1 = build_shadow_observation(history, current)
        r2 = build_shadow_observation(history, current)
        assert r1 == r2

    def test_repeated_invocations_stable(self):
        history = [_mk_tick(0.5, dry=0.5, errors=1)]
        current = _mk_snapshot(block=0.6, dry=0.4)
        results = [build_shadow_observation(history, current) for _ in range(10)]
        assert all(r == results[0] for r in results)

    def test_json_roundtrip_preserves_output(self):
        history = [_mk_tick(0.1), _mk_tick(0.3)]
        current = _mk_snapshot(block=0.5, errors=1)
        r1 = build_shadow_observation(history, current)
        # JSON roundtrip — preserva orden de keys y valores
        s = json.dumps(r1, sort_keys=True)
        r2 = json.loads(s)
        # Comparar via dict equality
        assert r1["shadow_state"] == r2["shadow_state"]
        assert r1["trend_direction"] == r2["trend_direction"]
        assert r1["stability_index"] == r2["stability_index"]
        assert r1["volatility_index"] == r2["volatility_index"]


# ════════════════════════════════════════════════════════════════════
#  2. SCHEMA VALIDITY
# ════════════════════════════════════════════════════════════════════
class TestSchemaValidity:

    def test_required_top_level_keys_present(self):
        r = build_shadow_observation([], _mk_snapshot())
        required = {
            "shadow_state", "trend_direction", "stability_index",
            "volatility_index", "divergence_events", "tick_summary", "meta",
        }
        assert required.issubset(set(r.keys()))

    def test_value_types_correct(self):
        r = build_shadow_observation([_mk_tick(0.2)], _mk_snapshot(block=0.3))
        assert isinstance(r["shadow_state"], str)
        assert isinstance(r["trend_direction"], str)
        assert isinstance(r["stability_index"], float)
        assert isinstance(r["volatility_index"], float)
        assert isinstance(r["divergence_events"], list)
        assert isinstance(r["tick_summary"], dict)
        assert isinstance(r["meta"], dict)

    def test_ranges_within_unit_interval(self):
        r = build_shadow_observation(
            [_mk_tick(0.0), _mk_tick(1.0), _mk_tick(0.0)],
            _mk_snapshot(block=1.0),
        )
        assert 0.0 <= r["stability_index"] <= 1.0
        assert 0.0 <= r["volatility_index"] <= 1.0

    def test_tick_summary_schema(self):
        r = build_shadow_observation([_mk_tick(0.5)], _mk_snapshot(block=0.5))
        ts = r["tick_summary"]
        assert set(ts.keys()) == {"total", "critical_ticks", "verified_ticks"}
        assert isinstance(ts["total"], int)
        assert isinstance(ts["critical_ticks"], int)
        assert isinstance(ts["verified_ticks"], int)

    def test_meta_schema(self):
        r = build_shadow_observation([], _mk_snapshot(timestamp="2026-05-13T10:00:00"))
        meta = r["meta"]
        assert meta["simulation_mode"] is True
        assert meta["timestamp"] == "2026-05-13T10:00:00"

    def test_shadow_state_uses_canonical_labels(self):
        r = build_shadow_observation([], _mk_snapshot())
        assert r["shadow_state"] in {"stable", "volatile", "critical"}

    def test_trend_direction_uses_canonical_labels(self):
        r = build_shadow_observation([], _mk_snapshot())
        assert r["trend_direction"] in {"improving", "degrading", "oscillating"}


# ════════════════════════════════════════════════════════════════════
#  3. STABILITY INDEX
# ════════════════════════════════════════════════════════════════════
class TestStabilityIndex:

    def test_empty_history_with_zero_current_is_stable(self):
        r = build_shadow_observation([], _mk_snapshot(block=0.0, dry=0.0, errors=0))
        assert r["stability_index"] == 1.0
        assert r["shadow_state"] == "stable"

    def test_flat_history_high_stability(self):
        history = [_mk_tick(0.2, dry=0.5) for _ in range(8)]
        current = _mk_snapshot(block=0.2, dry=0.5)
        r = build_shadow_observation(history, current)
        assert r["stability_index"] >= 0.95

    def test_alternating_rates_low_stability(self):
        # Oscilación máxima 0 ↔ 1
        history = [_mk_tick(0.0 if i % 2 == 0 else 1.0) for i in range(8)]
        current = _mk_snapshot(block=0.0)
        r = build_shadow_observation(history, current)
        assert r["stability_index"] < 0.3

    def test_high_errors_lowers_stability(self):
        # rates planas pero errores altos → stability sigue baja
        history = [_mk_tick(0.1, errors=0) for _ in range(3)]
        current = _mk_snapshot(block=0.1, errors=3)
        r = build_shadow_observation(history, current)
        assert r["stability_index"] <= 0.0001  # 1 - max(0, 3/3) = 0

    def test_stability_volatility_complementary(self):
        history = [_mk_tick(0.1), _mk_tick(0.5), _mk_tick(0.1)]
        current = _mk_snapshot(block=0.5)
        r = build_shadow_observation(history, current)
        # stability ≤ 1 - volatility (cuando error_pressure = 0)
        assert r["stability_index"] <= 1.0 - r["volatility_index"] + 1e-9


# ════════════════════════════════════════════════════════════════════
#  4. VOLATILITY DETECTION
# ════════════════════════════════════════════════════════════════════
class TestVolatilityDetection:

    def test_no_variance_zero_volatility(self):
        history = [_mk_tick(0.3, dry=0.6) for _ in range(5)]
        current = _mk_snapshot(block=0.3, dry=0.6)
        r = build_shadow_observation(history, current)
        assert r["volatility_index"] == 0.0

    def test_full_oscillation_high_volatility(self):
        history = [_mk_tick(0.0 if i % 2 == 0 else 1.0) for i in range(6)]
        current = _mk_snapshot(block=1.0)
        r = build_shadow_observation(history, current)
        assert r["volatility_index"] >= 0.8

    def test_gradual_drift_low_volatility(self):
        # 0.10 → 0.12 → 0.14 ... pequeños deltas (mean abs delta ~0.02)
        history = [_mk_tick(0.10 + 0.02 * i) for i in range(5)]
        current = _mk_snapshot(block=0.20)
        r = build_shadow_observation(history, current)
        assert r["volatility_index"] < 0.05

    def test_volatility_clamped_at_one(self):
        # rate_delta nunca debería exceder 1.0
        history = [_mk_tick(0.0), _mk_tick(1.0), _mk_tick(0.0), _mk_tick(1.0)]
        current = _mk_snapshot(block=0.0)
        r = build_shadow_observation(history, current)
        assert r["volatility_index"] <= 1.0

    def test_single_history_entry_no_delta(self):
        history = [_mk_tick(0.5)]
        current = _mk_snapshot(block=0.5)
        r = build_shadow_observation(history, current)
        assert r["volatility_index"] == 0.0


# ════════════════════════════════════════════════════════════════════
#  5. DIVERGENCE DETECTION
# ════════════════════════════════════════════════════════════════════
class TestDivergenceDetection:

    def test_no_divergence_when_flat(self):
        history = [_mk_tick(0.1) for _ in range(4)]
        current = _mk_snapshot(block=0.1)
        r = build_shadow_observation(history, current)
        assert r["divergence_events"] == []

    def test_block_rate_jump_detected(self):
        history = [_mk_tick(0.0), _mk_tick(0.0)]
        current = _mk_snapshot(block=0.5)  # salto 0.0 → 0.5
        r = build_shadow_observation(history, current)
        types = {e["type"] for e in r["divergence_events"]}
        assert "block_rate_jump" in types

    def test_dry_run_rate_jump_detected(self):
        history = [_mk_tick(0.0, dry=0.1), _mk_tick(0.0, dry=0.1)]
        current = _mk_snapshot(block=0.0, dry=0.9)  # salto dry 0.1 → 0.9
        r = build_shadow_observation(history, current)
        types = {e["type"] for e in r["divergence_events"]}
        assert "dry_run_rate_jump" in types

    def test_internal_errors_jump_detected(self):
        history = [_mk_tick(0.0, errors=0), _mk_tick(0.0, errors=0)]
        current = _mk_snapshot(errors=2)
        r = build_shadow_observation(history, current)
        types = {e["type"] for e in r["divergence_events"]}
        assert "internal_errors_jump" in types

    def test_multiple_divergences_accumulate(self):
        history = [_mk_tick(0.0, dry=0.0, errors=0), _mk_tick(0.0, dry=0.0, errors=0)]
        current = _mk_snapshot(block=0.6, dry=0.6, errors=2)  # tres saltos
        r = build_shadow_observation(history, current)
        types = {e["type"] for e in r["divergence_events"]}
        assert "block_rate_jump" in types
        assert "dry_run_rate_jump" in types
        assert "internal_errors_jump" in types

    def test_divergence_below_threshold_ignored(self):
        # Delta 0.1 < threshold 0.3 → ignorado
        history = [_mk_tick(0.1)]
        current = _mk_snapshot(block=0.2)
        r = build_shadow_observation(history, current)
        assert r["divergence_events"] == []

    def test_divergence_event_has_tick_index_and_magnitude(self):
        history = [_mk_tick(0.0), _mk_tick(0.0)]
        current = _mk_snapshot(block=0.7)
        r = build_shadow_observation(history, current)
        assert len(r["divergence_events"]) >= 1
        for e in r["divergence_events"]:
            assert "tick_index" in e
            assert "magnitude" in e
            assert isinstance(e["tick_index"], int)


# ════════════════════════════════════════════════════════════════════
#  6. EDGE CASES
# ════════════════════════════════════════════════════════════════════
class TestEdgeCases:

    def test_none_inputs_returns_default(self):
        r = build_shadow_observation(None, None)
        assert r["shadow_state"] == "stable"
        assert r["tick_summary"]["total"] == 0

    def test_history_none_current_valid(self):
        r = build_shadow_observation(None, _mk_snapshot(block=0.5))
        # current solo → 1 tick
        assert r["tick_summary"]["total"] == 1

    def test_history_valid_current_none(self):
        r = build_shadow_observation([_mk_tick(0.3)], None)
        # history + 1 tick default = 2 ticks
        assert r["tick_summary"]["total"] == 2

    def test_malformed_history_entries_handled(self):
        history = [None, "garbage", 123, _mk_tick(0.2)]
        current = _mk_snapshot(block=0.2)
        # No debe lanzar — ticks malformados cuentan pero unverified
        r = build_shadow_observation(history, current)
        assert isinstance(r, dict)
        assert r["tick_summary"]["total"] == 5  # 4 history + 1 current

    def test_verified_ticks_counts_only_well_formed(self):
        history = [None, "garbage", _mk_tick(0.3), _mk_tick(0.3)]
        current = _mk_snapshot(block=0.3)
        r = build_shadow_observation(history, current)
        # 3 verified: 2 history + 1 current (current via _mk_snapshot tiene rates+counters anidados)
        assert r["tick_summary"]["verified_ticks"] >= 3

    def test_empty_dict_inputs(self):
        r = build_shadow_observation([], {})
        assert isinstance(r, dict)
        # current vacío + history vacío = 1 tick default
        assert r["tick_summary"]["total"] == 1

    def test_negative_errors_clamped_to_zero(self):
        history = [_mk_tick(0.0, errors=0)]
        current = {
            "current": {
                "rates": {"purchase_block_rate": 0.0, "purchase_dry_run_rate": 0.5},
                "counters": {"internal_errors": -5},
            },
        }
        r = build_shadow_observation(history, current)
        # No debe afectar critical_ticks
        assert r["tick_summary"]["critical_ticks"] == 0

    def test_string_numeric_inputs_coerced(self):
        history = [{
            "rates": {"purchase_block_rate": "0.4", "purchase_dry_run_rate": "0.5"},
            "counters": {"internal_errors": "1"},
        }]
        current = _mk_snapshot(block=0.4)
        r = build_shadow_observation(history, current)
        # No debe explotar
        assert isinstance(r, dict)


# ════════════════════════════════════════════════════════════════════
#  7. ISOLATION (AST-based — sin random, no IO, no subprocess)
# ════════════════════════════════════════════════════════════════════
class TestIsolation:

    SHADOW_SRC = (
        PROJ_ROOT / "core" / "observability" / "shadow" / "shadow_observer.py"
    )

    def _module_ast(self):
        return ast.parse(self.SHADOW_SRC.read_text(encoding="utf-8"))

    def test_no_forbidden_imports(self):
        forbidden = {
            "random", "os", "sys", "subprocess", "socket", "threading",
            "multiprocessing", "requests", "urllib", "asyncio", "time",
            "datetime",
        }
        tree = self._module_ast()
        seen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    seen.add(n.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    seen.add(node.module.split(".")[0])
        assert seen.isdisjoint(forbidden), (
            f"Imports prohibidos en shadow_observer: {seen & forbidden}"
        )

    def test_no_random_no_io_calls(self):
        tree = self._module_ast()
        forbidden_names = {
            "open", "input", "print", "random", "uniform", "randint",
            "shuffle", "choice", "exec", "eval", "__import__",
        }
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
        # Permitir helpers internos como `set`, `max`, etc. — sólo verificamos forbidden
        assert called.isdisjoint(forbidden_names), (
            f"Llamadas prohibidas en shadow_observer: {called & forbidden_names}"
        )

    def test_no_module_level_side_effects(self):
        """Top-level statements deben ser: imports, asignaciones constantes,
        defs de función. No llamadas, no I/O."""
        tree = self._module_ast()
        for node in tree.body:
            assert isinstance(node, (
                ast.Import, ast.ImportFrom,
                ast.Assign, ast.AnnAssign,
                ast.FunctionDef, ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Expr,  # docstrings o constantes — verificamos abajo
            )), f"Top-level inesperado: {ast.dump(node)[:120]}"
            if isinstance(node, ast.Expr):
                # Solo permitir docstring (Constant str)
                assert isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ), "Top-level expression no docstring"


# ════════════════════════════════════════════════════════════════════
#  8. SHADOW STATE CLASSIFICATION
# ════════════════════════════════════════════════════════════════════
class TestShadowStateClassification:

    def test_critical_state_on_extreme_block_rate(self):
        history = [_mk_tick(0.9, errors=2) for _ in range(3)]
        current = _mk_snapshot(block=0.95, errors=3)
        r = build_shadow_observation(history, current)
        assert r["shadow_state"] == "critical"

    def test_volatile_state_on_moderate_oscillation(self):
        # Oscilación 0.1 ↔ 0.6 (mean delta ~0.5) → estabilidad ~0.5 < 0.65
        # y max_block (0.6) por debajo del umbral crítico (0.85) → "volatile"
        history = [_mk_tick(0.1 if i % 2 == 0 else 0.6) for i in range(6)]
        current = _mk_snapshot(block=0.1)
        r = build_shadow_observation(history, current)
        assert r["shadow_state"] == "volatile"

    def test_stable_state_on_flat_history(self):
        history = [_mk_tick(0.1) for _ in range(5)]
        current = _mk_snapshot(block=0.1)
        r = build_shadow_observation(history, current)
        assert r["shadow_state"] == "stable"


# ════════════════════════════════════════════════════════════════════
#  9. TREND DIRECTION
# ════════════════════════════════════════════════════════════════════
class TestTrendDirection:

    def test_improving_trend_when_block_rate_falls(self):
        # primera mitad alta, segunda mitad baja
        history = [_mk_tick(0.8), _mk_tick(0.8), _mk_tick(0.1), _mk_tick(0.1)]
        current = _mk_snapshot(block=0.1)
        r = build_shadow_observation(history, current)
        assert r["trend_direction"] == "improving"

    def test_degrading_trend_when_errors_grow(self):
        history = [
            _mk_tick(0.1, errors=0), _mk_tick(0.1, errors=0),
            _mk_tick(0.1, errors=2), _mk_tick(0.1, errors=3),
        ]
        current = _mk_snapshot(block=0.1, errors=3)
        r = build_shadow_observation(history, current)
        assert r["trend_direction"] == "degrading"

    def test_oscillating_trend_when_no_clear_direction(self):
        history = [
            _mk_tick(0.5), _mk_tick(0.5), _mk_tick(0.5), _mk_tick(0.5),
        ]
        current = _mk_snapshot(block=0.5)
        r = build_shadow_observation(history, current)
        assert r["trend_direction"] == "oscillating"


# ════════════════════════════════════════════════════════════════════
#  10. PUBLIC API / ALIASES
# ════════════════════════════════════════════════════════════════════
class TestPublicAPI:

    def test_compute_shadow_report_is_alias(self):
        assert compute_shadow_report is build_shadow_observation

    def test_module_exports(self):
        from core.observability import shadow
        assert "build_shadow_observation" in shadow.__all__
        assert "compute_shadow_report" in shadow.__all__
