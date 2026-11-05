"""tests/test_m19_namespace_consistency.py — Enforces M19 canonical contract.

Verifica los 4 puntos del contrato (core/observability/M19_CANONICAL.md):

    1. IDENTITY        — namespaces canónico y shim apuntan al MISMO objeto
    2. NO-DIVERGENCE   — el shim no contiene lógica propia (AST)
    3. MODE PRESERVATION — meta.mode se preserva para los 5 valores válidos
    4. SCHEMA INVARIANCE — output respeta {history, current, meta},
                            sin mirrors `history_snapshot` / `current_snapshot`

Cero dependencias externas. stdlib + pytest.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


# ════════════════════════════════════════════════════════════════════
#  1. IDENTITY — un solo function object detrás de ambos namespaces
# ════════════════════════════════════════════════════════════════════
class TestIdentity:

    def test_canonical_and_shim_are_same_function_object(self):
        from core.observability.adapter import adapt_event_to_tm_snapshot as canonical
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot as shim
        assert canonical is shim

    def test_all_three_aliases_same_function_object(self):
        """adapt_event_to_tm_snapshot, adapt_market_event y
        adapt_market_event_to_simulation deben ser el MISMO objeto."""
        from core.observability.adapter import (
            adapt_event_to_tm_snapshot,
            adapt_market_event,
            adapt_market_event_to_simulation,
        )
        assert adapt_market_event is adapt_event_to_tm_snapshot
        assert adapt_market_event_to_simulation is adapt_event_to_tm_snapshot
        assert adapt_market_event_to_simulation is adapt_market_event

    def test_shim_exports_spec_required_names(self):
        """El shim debe exponer exactamente los nombres del spec EXACT."""
        from core.observability.domain_adapter import (
            adapt_event_to_tm_snapshot,
            adapt_market_event_to_simulation,
        )
        # Ambos deben ser el mismo function object
        assert adapt_market_event_to_simulation is adapt_event_to_tm_snapshot

    def test_shim_all_matches_spec(self):
        """__all__ del shim contiene los nombres del spec EXACT."""
        from core.observability import domain_adapter as shim
        assert set(shim.__all__) == {
            "adapt_event_to_tm_snapshot",
            "adapt_market_event_to_simulation",
        }

    def test_shim_imports_from_canonical_module(self):
        """Verifica que el shim importa de core.observability.adapter.domain_adapter
        (módulo) y no de cualquier otro path. Identidad con la fuente canónica."""
        from core.observability.adapter.domain_adapter import (
            adapt_event_to_tm_snapshot as canonical_func,
        )
        from core.observability.domain_adapter import (
            adapt_event_to_tm_snapshot as shim_func,
        )
        assert canonical_func is shim_func

    def test_output_byte_identical_across_namespaces(self):
        from core.observability.adapter import adapt_event_to_tm_snapshot as f1
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot as f2
        event = {
            "event_id": "evt-canon-1",
            "timestamp": "2026-05-14T00:00:00Z",
            "signals": {"volatility": 0.42, "liquidity": 0.7, "volume": 2500},
            "external_factors": {"macro_risk": 0.55},
        }
        r1 = f1(event)
        r2 = f2(event)
        assert r1 == r2


# ════════════════════════════════════════════════════════════════════
#  2. NO-DIVERGENCE — shim no contiene lógica (AST inspection)
# ════════════════════════════════════════════════════════════════════
class TestNoDivergence:

    SHIM_PATH = (
        PROJ_ROOT / "core" / "observability" / "domain_adapter" / "__init__.py"
    )

    def _shim_ast(self):
        return ast.parse(self.SHIM_PATH.read_text(encoding="utf-8"))

    def test_shim_only_imports_assigns_and_docstring(self):
        """Top-level del shim sólo puede contener: docstring + ImportFrom + Assign(__all__).
        Cualquier FunctionDef / ClassDef / If / Call indica lógica → divergencia potencial."""
        tree = self._shim_ast()
        for i, node in enumerate(tree.body):
            if i == 0 and isinstance(node, ast.Expr) and isinstance(
                node.value, ast.Constant
            ) and isinstance(node.value.value, str):
                continue  # docstring
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Assign):
                # Sólo permitir asignación a __all__
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                assert targets == ["__all__"], (
                    f"Shim contiene asignación distinta a __all__: {targets}"
                )
                continue
            pytest.fail(
                f"Shim contiene nodo no permitido (potencial divergencia): "
                f"{type(node).__name__}"
            )

    def test_shim_no_function_or_class_defs(self):
        tree = self._shim_ast()
        for node in ast.walk(tree):
            assert not isinstance(node, ast.FunctionDef), (
                "Shim NO debe definir funciones — re-exporta sólo"
            )
            assert not isinstance(node, ast.AsyncFunctionDef), (
                "Shim NO debe definir funciones async"
            )
            assert not isinstance(node, ast.ClassDef), (
                "Shim NO debe definir clases"
            )

    def test_shim_no_calls_at_top_level(self):
        tree = self._shim_ast()
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                pytest.fail("Shim contiene llamada a función en top-level")


# ════════════════════════════════════════════════════════════════════
#  3. MODE PRESERVATION — los 5 modos válidos se preservan en meta.mode
# ════════════════════════════════════════════════════════════════════
class TestModePreservation:

    VALID_MODES = ["simulation", "replay", "live", "market", "synthetic"]

    @pytest.mark.parametrize("mode", VALID_MODES)
    def test_envelope_mode_propagates_to_meta(self, mode):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        envelope = {
            "source": mode,
            "event": {
                "event_id": "evt-mode-test",
                "timestamp": "2026-05-14T01:00:00Z",
                "signals": {"volatility": 0.1, "liquidity": 0.5, "volume": 100},
                "external_factors": {"macro_risk": 0.1},
            },
            "determinism_seed": 42,
        }
        snap = adapt_event_to_tm_snapshot(envelope)
        assert snap["meta"]["mode"] == mode

    def test_flat_event_defaults_to_market_mode(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        event = {
            "event_id": "evt-flat",
            "timestamp": "2026-05-14T02:00:00Z",
            "signals": {"volatility": 0.0, "liquidity": 0.5, "volume": 0},
            "external_factors": {"macro_risk": 0.0},
        }
        snap = adapt_event_to_tm_snapshot(event)
        assert snap["meta"]["mode"] == "market"

    def test_invalid_mode_falls_back_to_simulation_default(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        envelope = {
            "source": "INVALID_MODE_XYZ",
            "event": {
                "event_id": "evt-invalid-mode",
                "timestamp": "",
                "signals": {"volatility": 0.0, "liquidity": 0.5, "volume": 0},
                "external_factors": {"macro_risk": 0.0},
            },
        }
        snap = adapt_event_to_tm_snapshot(envelope)
        # Per _normalize_mode: invalid → _DEFAULT_MODE ("simulation")
        assert snap["meta"]["mode"] == "simulation"


# ════════════════════════════════════════════════════════════════════
#  4. SCHEMA INVARIANCE — sólo {history, current, meta}, sin mirrors
# ════════════════════════════════════════════════════════════════════
class TestSchemaInvariance:

    SAMPLE_EVENT = {
        "event_id": "evt-schema",
        "timestamp": "2026-05-14T03:00:00Z",
        "signals": {"volatility": 0.35, "liquidity": 0.65, "volume": 4200},
        "external_factors": {"macro_risk": 0.42},
    }

    def test_top_level_keys_are_exactly_history_current_meta(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        snap = adapt_event_to_tm_snapshot(self.SAMPLE_EVENT)
        assert set(snap.keys()) == {"history", "current", "meta"}

    def test_no_history_snapshot_mirror_key(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        snap = adapt_event_to_tm_snapshot(self.SAMPLE_EVENT)
        assert "history_snapshot" not in snap
        assert "current_snapshot" not in snap

    def test_current_has_rates_counters_config_only(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        snap = adapt_event_to_tm_snapshot(self.SAMPLE_EVENT)
        assert set(snap["current"].keys()) == {"rates", "counters", "config"}

    def test_meta_has_canonical_keys_only(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        snap = adapt_event_to_tm_snapshot(self.SAMPLE_EVENT)
        meta = snap["meta"]
        assert set(meta.keys()) == {"mode", "event_id", "timestamp", "drift_hint"}
        # Forbidden enrichment fields
        for forbidden in ("exposure_level", "news_sentiment", "extensions"):
            assert forbidden not in meta

    def test_rates_within_unit_interval(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        snap = adapt_event_to_tm_snapshot(self.SAMPLE_EVENT)
        r = snap["current"]["rates"]
        assert 0.0 <= r["purchase_block_rate"] <= 1.0
        assert 0.0 <= r["purchase_dry_run_rate"] <= 1.0

    def test_internal_errors_bounded_zero_to_three(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        # Volume alto fuerza max bucket
        event = dict(self.SAMPLE_EVENT)
        event["signals"] = dict(event["signals"])
        event["signals"]["volume"] = 999_999
        snap = adapt_event_to_tm_snapshot(event)
        errs = snap["current"]["counters"]["internal_errors"]
        assert 0 <= errs <= 3

    def test_drift_hint_in_canonical_buckets(self):
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        snap = adapt_event_to_tm_snapshot(self.SAMPLE_EVENT)
        assert snap["meta"]["drift_hint"] in {"low", "medium", "high", "critical"}


# ════════════════════════════════════════════════════════════════════
#  5. CONSUMER COMPATIBILITY — Fidelity y Shadow consumen el shim igual
# ════════════════════════════════════════════════════════════════════
class TestConsumerCompatibility:

    def test_fidelity_works_on_shim_output(self):
        """M20 Fidelity acepta TM_Snapshot generado por el shim."""
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        from core.observability.simulation_fidelity import evaluate_simulation_fidelity

        event = {
            "event_id": "evt-fidelity-consumer",
            "timestamp": "2026-05-14T04:00:00Z",
            "signals": {"volatility": 0.3, "liquidity": 0.7, "volume": 1500},
            "external_factors": {"macro_risk": 0.4},
        }
        snap = adapt_event_to_tm_snapshot(event)
        report = evaluate_simulation_fidelity(event, snap)
        assert "fidelity_score" in report
        assert isinstance(report["fidelity_score"], float)

    def test_shadow_works_on_shim_output(self):
        """M20 Shadow acepta TM_Snapshot generado por el shim."""
        from core.observability.domain_adapter import adapt_event_to_tm_snapshot
        from core.observability.shadow import build_shadow_observation

        events = [
            {
                "event_id": f"evt-{i}",
                "timestamp": f"2026-05-14T05:00:{i:02d}Z",
                "signals": {"volatility": 0.1 + 0.1 * i, "liquidity": 0.5, "volume": 500},
                "external_factors": {"macro_risk": 0.1},
            }
            for i in range(3)
        ]
        snapshots = [adapt_event_to_tm_snapshot(e) for e in events]
        history = [s["current"] for s in snapshots[:-1]]
        current = snapshots[-1]
        obs = build_shadow_observation(history, current)
        assert obs["shadow_state"] in {"stable", "volatile", "critical"}


# ════════════════════════════════════════════════════════════════════
#  6. CONTRACT FILE EXISTS
# ════════════════════════════════════════════════════════════════════
class TestNoDuplicateImplementations:
    """Garantía explícita: no existe implementación paralela de M19."""

    OBSERVABILITY_ROOT = PROJ_ROOT / "core" / "observability"

    def test_no_adapter_v2_directory(self):
        forbidden = self.OBSERVABILITY_ROOT / "adapter_v2"
        assert not forbidden.exists(), (
            "Forbidden by M19_CANONICAL.md: adapter_v2/ no debe existir"
        )

    def test_no_alternative_domain_adapter_module(self):
        """El namespace shim debe contener SOLO __init__.py, no un domain_adapter.py
        con lógica propia que duplique el canónico."""
        shim_dir = self.OBSERVABILITY_ROOT / "domain_adapter"
        py_files = sorted(p.name for p in shim_dir.glob("*.py"))
        # Sólo __init__.py permitido; ningún módulo con lógica
        assert py_files == ["__init__.py"], (
            f"domain_adapter/ contiene archivos inesperados: {py_files}"
        )

    def test_only_one_domain_adapter_py_in_tree(self):
        """Sólo puede existir un archivo `domain_adapter.py` con lógica
        en todo el árbol core/observability/."""
        matches = list(self.OBSERVABILITY_ROOT.rglob("domain_adapter.py"))
        # Excluir caches (.pyc no entran en rglob *.py)
        matches = [m for m in matches if "__pycache__" not in m.parts]
        assert len(matches) == 1, (
            f"Se esperaba 1 domain_adapter.py canónico, se encontraron {len(matches)}: "
            f"{[str(m) for m in matches]}"
        )
        # El único debe ser el canónico
        assert matches[0] == self.OBSERVABILITY_ROOT / "adapter" / "domain_adapter.py"


class TestContractDocument:

    CONTRACT_PATH = (
        PROJ_ROOT / "core" / "observability" / "M19_CANONICAL.md"
    )

    def test_canonical_contract_file_exists(self):
        assert self.CONTRACT_PATH.exists(), (
            "core/observability/M19_CANONICAL.md ausente — contrato sin documentar"
        )

    def test_canonical_contract_declares_single_truth(self):
        text = self.CONTRACT_PATH.read_text(encoding="utf-8")
        # Statements esenciales del contrato
        assert "one and only one" in text or "single source of truth" in text
        assert "adapt_event_to_tm_snapshot" in text
        assert "no M19A / M19B" in text.lower() or "no m19a" in text.lower()

    def test_contract_declares_canonical_namespace(self):
        text = self.CONTRACT_PATH.read_text(encoding="utf-8")
        assert "core.observability.adapter" in text
        assert "domain_adapter" in text  # menciona el shim

    def test_contract_declares_forbidden_patterns(self):
        text = self.CONTRACT_PATH.read_text(encoding="utf-8")
        # adapter_v2 debe estar mencionado como prohibido
        assert "adapter_v2" in text
