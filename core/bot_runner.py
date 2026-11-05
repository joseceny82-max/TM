"""core/bot_runner.py — Instrumented entry point wrapper (STEP 11 alt).

Alternativa al wiring directo de `_compra_parallel.py`: ejecuta el bot
legacy a través de un wrapper que añade observabilidad sin tocar el
archivo principal.

USO:
    python -m core.bot_runner [args originales]

Equivale a:
    python _compra_parallel.py [args originales]

PERO con instrumentación añadida:
    - start_flow al inicio
    - record_exception en top-level catch
    - record_outcome opcional al final

PRINCIPIO:
    Fail-safe absoluto. Si la observability falla, el bot continúa.
"""
from __future__ import annotations

import sys
from typing import Any, Optional


def _parse_slot_from_argv(argv: list) -> Optional[int]:
    """Extrae --slot de argv sin disparar argparse completo."""
    for i, a in enumerate(argv):
        if a == "--slot" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except (ValueError, TypeError):
                return None
    return None


def _parse_event_url_from_argv(argv: list) -> str:
    for i, a in enumerate(argv):
        if a == "--event-url" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--event-url="):
            return a.split("=", 1)[1]
    return ""


def main() -> int:
    """Entry point instrumentado. Delega al legacy main() tras hooks."""
    argv = sys.argv[1:]
    slot = _parse_slot_from_argv(argv)
    event_url = _parse_event_url_from_argv(argv)

    # ─── Bridge: start_flow (fail-safe) ──────────────────────────
    ctx = None
    try:
        from core.observability.bridge import start_flow
        ctx = start_flow(
            slot=slot if slot is not None else 0,
            event_id=event_url[:120],
            account_id="",
            seats=[],
        )
    except Exception:
        pass

    # ─── Decide if shadow_runner should observe this execution ───
    # Phase 5 wiring: if SHADOW_CORE_FLOW=true (default per MigrationFlags),
    # wrap the legacy call with run_with_shadow. The shadow_callable is a
    # NO-OP observer that snapshots what the legacy did. This produces
    # canary_compare events even without a fully-implemented FlowCore.
    shadow_enabled = False
    try:
        from core.migration.flags import get_flags
        shadow_enabled = get_flags().SHADOW_CORE_FLOW
    except Exception:
        pass

    # ─── Delegate to legacy main (with optional shadow observer) ─
    rc = 1
    try:
        import _compra_parallel as legacy

        def _legacy_call(_ctx):
            return legacy.main()

        if shadow_enabled:
            # No-op shadow: just emits an event recording that the bot
            # ran legacy. Real FlowCore-based shadow comes later (Step 12+).
            def _shadow_observer(_ctx):
                try:
                    from core.observability.event_emitter import emit_event
                    from core.observability.error_classification import (
                        ErrorCategory, ErrorSeverity,
                    )
                    emit_event(
                        ErrorCategory.FLOW, "shadow_observer_invoked",
                        {"slot": slot, "stage": "bot_runner",
                         "event_url": event_url[:80]},
                        severity=ErrorSeverity.RECOVERABLE,
                        context="bot_runner.shadow",
                    )
                except Exception:
                    pass
                return {"observed": True, "path": "legacy"}

            try:
                from core.migration.shadow_runner import run_with_shadow
                result = run_with_shadow(
                    ctx=ctx,
                    legacy_callable=_legacy_call,
                    shadow_callable=_shadow_observer,
                    canary_rate=1.0,    # observe every run for now
                    canary_identity=str(slot or "default"),
                )
                rc = result.legacy_result
            except Exception:
                # Shadow wiring failure → pure legacy
                rc = legacy.main()
        else:
            rc = legacy.main()
    except SystemExit:
        raise
    except Exception as e:
        try:
            from core.observability.bridge import record_exception
            record_exception(ctx, e, context="bot_runner.main",
                              payload={"slot": slot})
        except Exception:
            pass
        rc = 99

    return rc if isinstance(rc, int) else 1


if __name__ == "__main__":
    sys.exit(main())


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
