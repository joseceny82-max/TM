"""_compra_flowcore.py — Step-based launcher for split-traffic A/B migration.

PHASE C of SRE migration plan (see SPLIT_TRAFFIC_PLAN.md).

This is NOT a rewrite of the bot. It is a NAMED-STEP WRAPPER around the
legacy bot's functions in `_compra_parallel.py`. Each step:
    1. Records intent to WAL
    2. Calls the LEGACY function unchanged
    3. Records outcome / classifies errors

Production routing (decided in `_compra_with_swap.py` based on
FLOW_MODE + CANARY_RATE):
    - FLOW_MODE=legacy or canary fail → `_compra_parallel.py`
    - FLOW_MODE=flowcore or canary hit → `_compra_flowcore.py` (this file)

Both paths share IDENTICAL side effects:
    - Same browser engine (Playwright/Camoufox)
    - Same account_manager, bot_orchestrator
    - Same IMAP OTP
    - Same proxy infrastructure

The ONLY difference: this file decomposes the flow into named phases
with explicit WAL hooks per phase. Useful for:
    - Per-phase success rate metrics
    - Phase-level orphan recovery
    - Future split-by-step routing (Phase D+)

CRITICAL: this file is INACTIVE in production until
`_compra_with_swap.py` is updated to route to it (Phase B step).

stdlib + legacy bot only. NO new abstractions.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Import everything from legacy — we WRAP, we don't reimplement.
import _compra_parallel as legacy  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#  Phase wrappers — each wraps a legacy function with WAL + observability
# ════════════════════════════════════════════════════════════════════
def _emit_step_intent(ctx, step: str, **payload) -> None:
    """Fail-safe intent recording for a step. Never raises."""
    if ctx is None:
        return
    try:
        from core.observability.bridge import record_intent
        record_intent(ctx, step=step, **payload)
    except Exception:
        pass


def _emit_step_outcome(slot: int, step: str, status: str,
                        rc: int = 0, **payload) -> None:
    """Fail-safe outcome recording via the write_outcome tap.

    This is what produces the structured_events_pid*.jsonl entries
    that the dashboard reads.
    """
    try:
        legacy.write_outcome(slot, {
            "status": status,
            "step_reached": step,
            "rc": rc,
            **payload,
        })
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
#  Step 1: profile_pick
# ────────────────────────────────────────────────────────────────────
def attach_profile_step(args, ctx) -> Optional[dict]:
    """Wraps legacy.pick_morelogin_profile + Camoufox fallback.

    Returns the prof dict, or None if no profile available.
    """
    _emit_step_intent(ctx, step="attach_profile",
                       engine=getattr(args, 'engine', 'morelogin'))
    slot = args.slot
    engine = getattr(args, 'engine', 'morelogin')
    prof = None
    if engine == 'morelogin':
        for retry in range(legacy.RETRY_PROFILE_PICK):
            if not legacy._acquire_pick_mutex(timeout=30):
                legacy.log(slot, "pick_mutex_timeout")
                time.sleep(3)
                continue
            try:
                prof = legacy.pick_morelogin_profile(slot)
            finally:
                legacy._release_pick_mutex()
            if prof:
                break
            legacy.log(slot, "pick_retry", retry=retry+1)
            time.sleep(3)
        if not prof:
            _emit_step_outcome(slot, "attach_profile",
                                "fatal_no_profile", rc=1)
            return None
    else:
        import random
        sessid = random.randint(100000, 999999)
        prof = {
            "profile": f"camoufox-{sessid}",
            "env_id": f"camoufox-{sessid}",
            "ip": "",
            "proxy_country": "ES",
        }
    legacy.log(slot, "profile_picked", **prof)
    _emit_step_outcome(slot, "attach_profile",
                        "profile_attached",
                        profile=prof.get('profile', ''))
    return prof


# ────────────────────────────────────────────────────────────────────
#  Step 2: account_pick
# ────────────────────────────────────────────────────────────────────
def attach_account_step(args, ctx) -> tuple:
    """Wraps legacy.pick_account. Returns (account, card) or (None, None)."""
    _emit_step_intent(ctx, step="attach_account")
    slot = args.slot
    used_lock_path = os.path.join(
        legacy.PROYECTO, "_compra_parallel_used_accounts.lock")
    used_ids: set = set()
    try:
        if os.path.exists(used_lock_path):
            with open(used_lock_path, encoding='utf-8') as f:
                import json as _j
                used_ids = set(_j.load(f))
    except Exception:
        pass

    account, card = legacy.pick_account(slot, used_ids)
    if not account:
        _emit_step_outcome(slot, "attach_account",
                            "fatal_no_account", rc=1)
        return None, None
    _emit_step_outcome(slot, "attach_account",
                        "account_picked",
                        account_email=account.get('tm_email', ''),
                        account_id=str(account.get('id', '')))
    return account, card


# ────────────────────────────────────────────────────────────────────
#  main_flowcore — orchestrator
# ────────────────────────────────────────────────────────────────────
def main_flowcore() -> int:
    """Step-based equivalent of legacy.main(). Calls into legacy for
    everything beyond steps 1-2; legacy handles the deep complexity
    of login/cart/checkout/payment until we extract more steps.

    Phase C delivers steps 1-2 as named wrappers. Steps 3+ delegate
    to legacy.main() for now — they'll be extracted incrementally.
    """
    args = legacy.parse_args()

    # ─── Kill switch cross-process check (P2) ────────────────
    try:
        if Path("runs/_kill_switch.flag").exists():
            legacy.log(args.slot, "kill_switch_active_flowcore_aborting")
            _emit_step_outcome(args.slot, "startup",
                                "kill_switch_aborted", rc=99)
            return 99
    except Exception:
        pass

    # ─── Init bridge ctx ─────────────────────────────────────
    ctx = None
    try:
        from core.observability.bridge import start_flow, record_intent
        ctx = start_flow(
            slot=args.slot,
            event_id=(args.event_url or "")[:120],
            account_id="",
            seats=[],
        )
        record_intent(ctx, step="bot_main_entry_flowcore",
                       event_url=(args.event_url or "")[:120],
                       slot=args.slot,
                       qty=getattr(args, 'qty', None),
                       offer_idx=getattr(args, 'offer_idx', None),
                       runner="flowcore")
    except Exception:
        ctx = None

    # ─── Phase 1: profile attach ─────────────────────────────
    prof = attach_profile_step(args, ctx)
    if not prof:
        return 1

    # ─── Phase 2: account attach ─────────────────────────────
    account, card = attach_account_step(args, ctx)
    if not account:
        return 1

    # ─── Phase 3+: delegate to legacy ────────────────────────
    # For everything past account_pick (browser start, login, cart,
    # checkout, payment), we call into legacy.main() with the prof
    # and account already picked. Since legacy.main() picks its own,
    # this is currently a clean re-pick (some waste but correct).
    #
    # FUTURE: extract more steps progressively. For now, we proven
    # the wrapping pattern works for steps 1-2 and let legacy handle
    # the rest. This is INTENTIONAL — we want minimal risk per audit.
    legacy.log(args.slot, "flowcore_delegating_to_legacy",
                phases_extracted=2, phases_total=10)
    rc = legacy.main()

    if ctx is not None:
        try:
            from core.observability.bridge import record_outcome
            record_outcome(ctx, status="flowcore_completed", rc=rc,
                            runner="flowcore", phases_extracted=2)
        except Exception:
            pass
    return rc


if __name__ == '__main__':
    _slot = None
    try:
        for i, a in enumerate(sys.argv[1:], 1):
            if a == "--slot" and i < len(sys.argv) - 1:
                try:
                    _slot = int(sys.argv[i+1])
                except (ValueError, TypeError):
                    pass
                break
        rc = main_flowcore()
    except SystemExit:
        raise
    except Exception as e:
        try:
            from core.observability.bridge import record_exception
            record_exception(None, e, context="flowcore_main_topfatal",
                              payload={"slot": _slot})
        except Exception:
            pass
        if _slot is not None:
            try:
                legacy.write_outcome(_slot, {
                    "status": "fatal_exception_flowcore",
                    "step_reached": "unknown",
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
                    "rc": 99,
                })
            except Exception:
                pass
        sys.exit(99)
    sys.exit(rc)
