# SPLIT-TRAFFIC A/B MIGRATION PLAN

## Why not shadow

Confirmed architecturally impossible for this bot:
- OTP IMAP one-shot — can't be replayed
- Cart attach mutates remote TM inventory
- account_manager consumes real accounts
- Profile pool burns 2× under parallel runs
- Anti-bot detects duplicates

## Strategy: SPLIT-TRAFFIC A/B (not parallel)

For each purchase request, route to EXACTLY ONE path:
```
deterministic_route(account_email) →
    if hash(account_email) % 100 < CANARY_PCT:
        FlowCore_path   (calls wrapped legacy functions)
    else:
        Legacy_path     (calls _compra_parallel.main directly)
```

Both paths share the SAME side-effect generators:
- Same browser engine (Playwright / Camoufox)
- Same account_manager
- Same bot_orchestrator (MoreLogin)
- Same IMAP OTP
- Same proxy infrastructure

The ONLY difference is orchestration:
- **Legacy**: flat 3868 LOC script with implicit state
- **FlowCore**: same logic decomposed into named steps with CartState + WAL hooks

## Where to inject the routing decision

Production launcher chain:
```
_compra_with_swap.py  ──→  subprocess  ──→  _compra_parallel.py:main()
```

**Routing happens at the launcher level** (`_compra_with_swap.py`),
not inside `_compra_parallel.py`. Because:
1. The decision needs account_email, which is known BEFORE subprocess launch
2. The legacy main() is too entangled to bifurcate internally
3. The launcher already has subprocess control — perfect routing point

### Concrete proposal (minimal change to launcher)

```python
# Inside _compra_with_swap.py, before subprocess.run(...):

def _should_use_flowcore(args) -> bool:
    """Deterministic hash routing based on account_email pre-pick.
    Returns False until split-traffic infrastructure is ready."""
    try:
        from core.migration import get_kill_switch, get_flow_mode, get_canary_rate
        if get_kill_switch():
            return False   # kill switch forces legacy
        mode = get_flow_mode()
        if mode == "legacy":
            return False
        if mode == "flowcore":
            return True
        # mode == "auto" → canary by hash
        # NOTE: we don't have account_email yet (picked inside subprocess)
        # so route by slot for now (less ideal, but no choice)
        rate = get_canary_rate()
        import hashlib
        h = int(hashlib.sha256(str(args.slot).encode()).hexdigest()[:8], 16)
        return (h % 10000) < (rate * 10000)
    except Exception:
        return False   # safe default: legacy


# Then in main loop:
if _should_use_flowcore(args):
    SCRIPT = "_compra_flowcore.py"  # NEW launcher (Phase C deliverable)
else:
    SCRIPT = "_compra_parallel.py"
```

## What FlowCore needs to be (Phase C)

NOT a rewrite of the bot. Just a new launcher script `_compra_flowcore.py`
that imports the legacy bot's functions and orchestrates them differently:

```python
# _compra_flowcore.py (NEW, ~150 LOC, NOT 3868)

from _compra_parallel import (
    parse_args, write_outcome, pick_morelogin_profile, pick_account,
    login_full, fill_card, login_with_account_swap,
    # ... all the heavy lifting functions from legacy
)

def main_flowcore():
    args = parse_args()
    # Same Step 11 hooks as legacy
    ctx = start_flow(slot=args.slot, ...)

    # Explicit phases, calling legacy functions:
    profile = attach_profile_step(args, ctx)  # wraps pick_morelogin_profile
    account, card = attach_account_step(args, ctx, profile)
    login_ok = login_step(args, ctx, page, account, card)
    preflight_ok = preflight_step(args, ctx, page)
    cart_ok = attach_cart_step(args, ctx, page)
    checkout_ok = checkout_step(args, ctx, page, card)
    payment_ok = payment_step(args, ctx, page)
    finalize_step(args, ctx)
```

Each `*_step` is a 5-20 LOC wrapper that:
1. Records intent: `record_intent(ctx, step="attach_profile", ...)`
2. Calls the LEGACY function: `result = pick_morelogin_profile(slot)`
3. Records outcome / handles errors

**No legacy logic is rewritten**. The "step callables" are just
named wrappers that add WAL + observability per phase, then delegate
to the legacy functions.

## Migration sequence (no surprises)

```
Day 0   : This plan documented. canary_rate = 0.0
Day 0+  : Implement _compra_flowcore.py wrapper (Phase C, ~150 LOC)
Day 1   : Run _compra_flowcore.py once manually against safe URL
          Verify same outcome as legacy
Day 2-3 : Activate FLOW_MODE=auto + CANARY_RATE=0.01 (1%)
          Monitor success_rate_flowcore vs success_rate_legacy
Day 4-7 : If parity > 95%, ramp to 0.10 (10%)
Day 8-14: Ramp 0.50, 1.00
Day 14+ : If 14 days clean, consider FLOW_MODE=flowcore
Day 28+ : Consider eliminating legacy

ROLLBACK at any time: set_kill_switch(True) → next subprocess uses legacy
```

## Success criteria (real)

- `success_rate_flowcore >= success_rate_legacy - 0.005` over 24h windows
- `idempotency_collisions == 0` for both paths
- No fallback activations during canary period
- Same payment_failure distribution

## What does NOT exist yet

- `_compra_flowcore.py` — Phase C deliverable
- Modified `_compra_with_swap.py` routing — small change required
- Real account-based hashing (currently slot-based as fallback)

## What is ALREADY ready

- Kill switch cross-process (P2 audit) ✅
- WAL writes (P3 audit) ✅
- Source tagging (P2 prior audit) ✅
- Step 11 hooks in main() ✅
- Dashboard with health states ✅
- Flow mode + canary rate config in cutover_router ✅
- 9 critical operational exits instrumented ✅

## Risk acceptance

Split-traffic A/B has different risks than shadow:
- A failed FlowCore run **affects real users** (one purchase per routed request)
- Hence: canary starts at 1% MAX, ramps only on data
- Failed FlowCore run can fall back to legacy IF wrapped correctly
- Recovery: kill_switch + WAL orphan detection → manual reconciliation
