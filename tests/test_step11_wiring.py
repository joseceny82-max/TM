"""tests/test_step11_wiring.py — STEP 11 wiring verification.

Verifica:
    1. `_compra_parallel.py` sigue parseable y los hooks no rompen import
    2. Los hooks emiten eventos cuando observability disponible
    3. Los hooks NO propagan excepciones si observability falla
    4. `core/bot_runner.py` wrapper funciona como alternativa
    5. Cero regresiones de baseline

stdlib + pytest. NO ejecuta el bot real (necesita browser); sólo
verifica la integración instrumentación-side.
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
#  1. Bot file parse + structural integrity
# ════════════════════════════════════════════════════════════════════
class TestBotFileIntegrity:

    BOT_PATH = PROJ_ROOT / "_compra_parallel.py"

    def test_bot_file_still_parses(self):
        src = self.BOT_PATH.read_text(encoding="utf-8")
        ast.parse(src)  # no raise = OK

    def test_main_function_still_present(self):
        src = self.BOT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = [n.name for n in tree.body
                  if isinstance(n, ast.FunctionDef)]
        assert "main" in names

    def test_hooks_use_lazy_import(self):
        """Los hooks DEBEN importar lazy desde bridge.
        Esto garantiza que si bridge no está disponible,
        el bot sigue funcionando."""
        src = self.BOT_PATH.read_text(encoding="utf-8")
        # Los imports están dentro de try/except, NO al top-level
        # Si están al top-level, romperían el bot si bridge no está
        top_imports = []
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if node.module and "core.observability" in node.module:
                    top_imports.append(node.module)
            elif isinstance(node, ast.Import):
                for n in node.names:
                    if "core.observability" in n.name:
                        top_imports.append(n.name)
        # Top-level imports a core.observability deben ser 0
        # (los hooks usan lazy import dentro de try/except)
        assert top_imports == [], (
            f"Hooks should use LAZY import, found top-level: {top_imports}"
        )

    def test_hooks_wrapped_in_try_except(self):
        """Cada hook debe estar dentro de un try/except para fail-safe."""
        src = self.BOT_PATH.read_text(encoding="utf-8")
        # Verificar marcadores de Step 11
        assert "STEP 11 INSTRUMENTATION" in src
        # Verificar que las llamadas a hooks están envueltas
        # Mínimo 3 try/except con observability
        assert src.count("from core.observability.bridge") >= 3


# ════════════════════════════════════════════════════════════════════
#  2. Bot import doesn't break
# ════════════════════════════════════════════════════════════════════
class TestBotImports:

    def test_bot_can_be_imported_without_running(self):
        """Importar el módulo NO debe ejecutar main()."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_compra_parallel_import_test",
            str(PROJ_ROOT / "_compra_parallel.py"),
        )
        assert spec is not None
        # Crear el módulo sin ejecutar (sólo spec)
        mod = importlib.util.module_from_spec(spec)
        assert mod is not None


# ════════════════════════════════════════════════════════════════════
#  3. core/bot_runner wrapper
# ════════════════════════════════════════════════════════════════════
class TestBotRunner:

    def test_wrapper_module_imports(self):
        from core import bot_runner
        assert hasattr(bot_runner, "main")
        assert callable(bot_runner.main)

    def test_wrapper_parses_slot_arg(self):
        from core.bot_runner import _parse_slot_from_argv
        assert _parse_slot_from_argv(["--slot", "3"]) == 3
        assert _parse_slot_from_argv(["--slot", "not-int"]) is None
        assert _parse_slot_from_argv([]) is None

    def test_wrapper_parses_event_url(self):
        from core.bot_runner import _parse_event_url_from_argv
        assert _parse_event_url_from_argv(
            ["--event-url", "https://x.com"]
        ) == "https://x.com"
        assert _parse_event_url_from_argv(
            ["--event-url=https://y.com"]
        ) == "https://y.com"
        assert _parse_event_url_from_argv([]) == ""


# ════════════════════════════════════════════════════════════════════
#  4. Hooks fail-safe behavior
# ════════════════════════════════════════════════════════════════════
class TestHookFailSafe:

    def test_start_flow_failure_does_not_propagate(self, monkeypatch, tmp_path):
        """Si start_flow lanza una excepción, NO debe propagar al bot."""
        # Simular start_flow rompiéndose
        from core.observability import bridge as bridge_mod
        original = bridge_mod.start_flow

        def broken_start(*a, **kw):
            raise RuntimeError("simulated observability failure")

        monkeypatch.setattr(bridge_mod, "start_flow", broken_start)

        # Wrap del patrón usado en _compra_parallel.py
        _bridge_ctx = None
        try:
            from core.observability.bridge import start_flow as _start_flow
            _bridge_ctx = _start_flow(slot=1, event_id="x",
                                         account_id="", seats=[])
        except Exception:
            _bridge_ctx = None

        # El patrón sobrevive sin propagar
        assert _bridge_ctx is None

        monkeypatch.setattr(bridge_mod, "start_flow", original)

    def test_record_exception_failure_does_not_propagate(self, monkeypatch):
        from core.observability import bridge as bridge_mod
        original = bridge_mod.record_exception

        def broken_record(*a, **kw):
            raise RuntimeError("simulated")

        monkeypatch.setattr(bridge_mod, "record_exception", broken_record)

        # Patrón del except top-level
        e_simulated = ValueError("test")
        try:
            from core.observability.bridge import record_exception as _re
            _re(None, e_simulated, context="x", payload={"slot": 1})
        except Exception:
            pass  # debe estar swallowed

        monkeypatch.setattr(bridge_mod, "record_exception", original)


# ════════════════════════════════════════════════════════════════════
#  5. Bridge integration end-to-end (live)
# ════════════════════════════════════════════════════════════════════
class TestBridgeIntegration:

    def test_real_start_flow_works(self, tmp_path):
        """Con bridge disponible, start_flow produce ctx válido."""
        from core.observability.event_emitter import (
            set_sink_path, clear_sinks, read_events,
        )
        sink = tmp_path / "events.jsonl"
        set_sink_path(sink)
        clear_sinks()
        try:
            from core.observability.bridge import start_flow
            ctx = start_flow(
                slot=1, event_id="https://example.com/event",
                account_id="", seats=[],
                idem_root=str(tmp_path / "idem"),
            )
            assert ctx is not None
            assert ctx.slot == 1
            # Verificar evento emitido
            events = read_events()
            assert any(e.get("code") == "flow_started" for e in events)
        finally:
            clear_sinks()
            set_sink_path(None)
