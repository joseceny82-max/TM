"""tests/test_step13_cutover_router.py — Kill switch + cutover routing.

Categorías:
    1. Defaults safe (legacy)              (4 tests)
    2. set_flow_mode runtime               (4 tests)
    3. kill_switch override                (4 tests)
    4. decide_path (auto)                  (4 tests)
    5. execute_purchase happy paths        (5 tests)
    6. Auto-fallback                        (4 tests)
    7. CUTOVER_EVENT emission              (3 tests)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.migration import (
    DEFAULT_CANARY_RATE,
    DEFAULT_FLOW_MODE,
    FLOW_MODE_AUTO,
    FLOW_MODE_FLOWCORE,
    FLOW_MODE_LEGACY,
    decide_path,
    execute_purchase,
    get_canary_rate,
    get_flow_mode,
    get_kill_switch,
    reset_state,
    set_canary_rate,
    set_flow_mode,
    set_kill_switch,
    snapshot,
)
from core.observability.event_emitter import (
    clear_sinks,
    read_events,
    set_sink_path,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Cada test parte de estado limpio + env vars del cutover desactivadas."""
    # Limpiar env vars relevantes
    for var in ("FLOW_MODE", "KILL_SWITCH", "CANARY_RATE"):
        monkeypatch.delenv(var, raising=False)
    # P2 audit: limpiar flag file cross-process
    from pathlib import Path
    flag = Path("runs/_kill_switch.flag")
    if flag.exists():
        try: flag.unlink()
        except: pass
    reset_state()
    set_sink_path(tmp_path / "events.jsonl")
    clear_sinks()
    yield
    clear_sinks()
    set_sink_path(None)
    reset_state()
    if flag.exists():
        try: flag.unlink()
        except: pass


def _legacy(ctx):
    return {"path": "legacy", "ok": True}


def _flowcore(ctx):
    return {"path": "flowcore", "ok": True}


def _flowcore_crash(ctx):
    raise ConnectionError("flowcore down")


# ════════════════════════════════════════════════════════════════════
#  1. DEFAULTS SAFE
# ════════════════════════════════════════════════════════════════════
class TestDefaultsSafe:

    def test_default_flow_mode_is_legacy(self):
        assert get_flow_mode() == FLOW_MODE_LEGACY

    def test_default_kill_switch_is_false(self):
        assert get_kill_switch() is False

    def test_default_canary_rate_is_zero(self):
        assert get_canary_rate() == 0.0

    def test_snapshot_indicates_safe_default(self):
        s = snapshot()
        assert s["is_safe_default"] is True


# ════════════════════════════════════════════════════════════════════
#  2. set_flow_mode runtime
# ════════════════════════════════════════════════════════════════════
class TestSetFlowMode:

    def test_set_to_flowcore(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        assert get_flow_mode() == FLOW_MODE_FLOWCORE

    def test_set_to_auto(self):
        set_flow_mode(FLOW_MODE_AUTO)
        assert get_flow_mode() == FLOW_MODE_AUTO

    def test_invalid_mode_falls_back_to_legacy(self):
        set_flow_mode("nonsense")
        assert get_flow_mode() == FLOW_MODE_LEGACY

    def test_back_to_legacy(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        set_flow_mode(FLOW_MODE_LEGACY)
        assert get_flow_mode() == FLOW_MODE_LEGACY


# ════════════════════════════════════════════════════════════════════
#  3. KILL SWITCH OVERRIDE
# ════════════════════════════════════════════════════════════════════
class TestKillSwitchOverride:

    def test_kill_switch_forces_legacy_when_mode_flowcore(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        set_kill_switch(True)
        assert decide_path() == FLOW_MODE_LEGACY

    def test_kill_switch_forces_legacy_when_mode_auto(self):
        set_flow_mode(FLOW_MODE_AUTO)
        set_canary_rate(1.0)
        set_kill_switch(True)
        assert decide_path(canary_identity="X") == FLOW_MODE_LEGACY

    def test_kill_switch_off_respects_mode(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        set_kill_switch(False)
        assert decide_path() == FLOW_MODE_FLOWCORE

    def test_kill_switch_emits_event_on_activation(self):
        set_kill_switch(True)
        events = read_events()
        codes = [e.get("code") for e in events]
        assert "kill_switch_activated" in codes


# ════════════════════════════════════════════════════════════════════
#  4. decide_path
# ════════════════════════════════════════════════════════════════════
class TestDecidePath:

    def test_legacy_mode_returns_legacy(self):
        set_flow_mode(FLOW_MODE_LEGACY)
        assert decide_path() == FLOW_MODE_LEGACY

    def test_flowcore_mode_returns_flowcore(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        assert decide_path() == FLOW_MODE_FLOWCORE

    def test_auto_with_canary_zero_returns_legacy(self):
        set_flow_mode(FLOW_MODE_AUTO)
        set_canary_rate(0.0)
        assert decide_path(canary_identity="X") == FLOW_MODE_LEGACY

    def test_auto_with_canary_one_returns_flowcore(self):
        set_flow_mode(FLOW_MODE_AUTO)
        set_canary_rate(1.0)
        assert decide_path(canary_identity="X") == FLOW_MODE_FLOWCORE


# ════════════════════════════════════════════════════════════════════
#  5. execute_purchase
# ════════════════════════════════════════════════════════════════════
class TestExecutePurchase:

    def test_default_routes_to_legacy(self):
        r = execute_purchase(
            {}, legacy_callable=_legacy, flowcore_callable=_flowcore,
        )
        assert r["path_used"] == "legacy"
        assert r["result"]["path"] == "legacy"

    def test_flowcore_mode_routes_to_flowcore(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        r = execute_purchase(
            {}, legacy_callable=_legacy, flowcore_callable=_flowcore,
        )
        assert r["path_used"] == "flowcore"

    def test_no_flowcore_callable_falls_back_to_legacy(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        r = execute_purchase(
            {}, legacy_callable=_legacy, flowcore_callable=None,
        )
        assert r["path_used"] == "legacy"

    def test_kill_switch_overrides_to_legacy(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        set_kill_switch(True)
        r = execute_purchase(
            {}, legacy_callable=_legacy, flowcore_callable=_flowcore,
        )
        assert r["result"]["path"] == "legacy"

    def test_latency_recorded(self):
        r = execute_purchase({}, legacy_callable=_legacy)
        assert r["latency_ms"] >= 0


# ════════════════════════════════════════════════════════════════════
#  6. AUTO-FALLBACK
# ════════════════════════════════════════════════════════════════════
class TestAutoFallback:

    def test_flowcore_crash_falls_back_to_legacy(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        r = execute_purchase(
            {}, legacy_callable=_legacy,
            flowcore_callable=_flowcore_crash,
        )
        assert r["path_used"] == "legacy_fallback"
        assert r["fallback_triggered"] is True

    def test_fallback_returns_legacy_result(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        r = execute_purchase(
            {}, legacy_callable=_legacy,
            flowcore_callable=_flowcore_crash,
        )
        assert r["result"]["path"] == "legacy"

    def test_disabled_fallback_propagates(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        with pytest.raises(ConnectionError):
            execute_purchase(
                {}, legacy_callable=_legacy,
                flowcore_callable=_flowcore_crash,
                auto_fallback=False,
            )

    def test_fallback_error_recorded(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        r = execute_purchase(
            {}, legacy_callable=_legacy,
            flowcore_callable=_flowcore_crash,
        )
        assert "ConnectionError" in r["error"]


# ════════════════════════════════════════════════════════════════════
#  7. CUTOVER_EVENT EMISSION
# ════════════════════════════════════════════════════════════════════
class TestCutoverEvents:

    def test_cutover_event_emitted_for_each_call(self):
        execute_purchase({}, legacy_callable=_legacy)
        events = read_events()
        codes = [e.get("code") for e in events]
        assert "cutover_event" in codes

    def test_cutover_event_records_path(self):
        execute_purchase({}, legacy_callable=_legacy)
        events = read_events()
        cutover_events = [e for e in events
                           if e.get("code") == "cutover_event"]
        assert cutover_events[0]["payload"]["used_path"] == "legacy"

    def test_cutover_event_records_fallback(self):
        set_flow_mode(FLOW_MODE_FLOWCORE)
        execute_purchase(
            {}, legacy_callable=_legacy,
            flowcore_callable=_flowcore_crash,
        )
        events = read_events()
        cutover_events = [e for e in events
                           if e.get("code") == "cutover_event"]
        assert cutover_events[0]["payload"]["fallback_triggered"] is True
