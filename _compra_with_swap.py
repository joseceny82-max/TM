"""Wrapper de _compra_parallel.py con swap-on-bot-block.

Cuando _compra_parallel.py detecta Akamai bot-block (Su sesión se ha
suspendido), retorna exit code 99 y persiste profile + subnet /24 en
burned files. Este wrapper:

1. Lanza _compra_parallel.py con los args originales
2. Si exit code = 99 → cooldown configurable + relanza (max MAX_SWAPS=3)
3. Si exit code = 0 (éxito) o cualquier otro → exit con ese mismo code

Uso (drop-in replacement):
    python _compra_with_swap.py --slot 1 --event-url <URL> --offer-idx 3 --qty 2 --mode quick

El profile_id + ip quemados se persisten automáticamente vía
`_compra_parallel._persist_bot_block()` antes del exit 99. La siguiente
iteración llama a `pick_morelogin_profile` que excluye subnets /24
quemadas → nueva combinación profile + IP.

Salida estándar: el wrapper imprime [SWAP] markers entre reintentos.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Forzar stdout/stderr a UTF-8 en Windows (default cp1252 rompe ═, ✅, etc.)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

RC_BOT_BLOCK = 99
MAX_SWAPS = int(os.environ.get('COMPRA_MAX_SWAPS', '3'))
COOLDOWN_S = int(os.environ.get('COMPRA_SWAP_COOLDOWN_S', '60'))
SCRIPT_LEGACY = str(Path(__file__).parent / '_compra_parallel.py')
SCRIPT_FLOWCORE = str(Path(__file__).parent / '_compra_flowcore.py')


def _log(msg: str) -> None:
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[swap {ts}] {msg}', flush=True)


def _pick_script(slot: int) -> tuple[str, str]:
    """Split-traffic routing decision: legacy vs flowcore.

    Delegates to `core.migration.decide_path()` which already implements:
        1. kill_switch=True            → legacy (override absoluto)
        2. FLOW_MODE=legacy            → legacy
        3. FLOW_MODE=flowcore          → flowcore
        4. FLOW_MODE=auto + canary_rate → hash(slot) % bucket
        5. FLOW_MODE=simulated + canary_rate → hash(slot) % bucket (no execution)

    Safety:
        - Any exception → legacy (safe default)
        - SCRIPT_FLOWCORE missing on disk → legacy (defensive)

    Returns:
        (script_path, runner_tag)  — runner_tag for logging only.
    """
    try:
        from core.migration import decide_path, FLOW_MODE_FLOWCORE
        chosen = decide_path(canary_identity=str(slot))
        if chosen == FLOW_MODE_FLOWCORE and Path(SCRIPT_FLOWCORE).exists():
            return SCRIPT_FLOWCORE, 'flowcore'
    except Exception:
        pass
    return SCRIPT_LEGACY, 'legacy'


# ════════════════════════════════════════════════════════════════════
#  Simulation mode — PHASE 1 gate before real execution
# ════════════════════════════════════════════════════════════════════
SIM_EVENTS_FILE = Path(__file__).parent / 'runs' / 'structured_events_simulated.jsonl'
SIM_WAL_DIR = Path(__file__).parent / 'runs' / 'idempotency_log'


def _is_simulation() -> bool:
    """True when FLOW_MODE=simulated. Wrapper must NOT spawn subprocess."""
    try:
        from core.migration import is_simulation_mode
        return is_simulation_mode()
    except Exception:
        return False


def _arg(argv: list[str], name: str, default: str = '') -> str:
    """Extract --name=val or --name val from argv. Returns default if absent."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + '='):
            return a.split('=', 1)[1]
    return default


def _execute_simulated(slot: int, script: str, runner: str,
                        argv: list[str]) -> int:
    """Simulated dispatch: writes WAL + structured events, returns rc=0.

    HARD GUARANTEES:
        - NO subprocess.run / Popen / os.exec*
        - NO browser launch
        - NO MoreLogin profile mutation
        - NO account consumption
        - NO TM endpoint hit
        - NO network I/O of any kind

    Side effects (controlled, test-only):
        - Append 4 JSON lines to runs/structured_events_simulated.jsonl
        - Write 2 files to runs/idempotency_log/<key>/
    """
    import json
    import time
    import uuid as _uuid
    from datetime import datetime, timezone

    event_url = _arg(argv, '--event-url')
    qty = _arg(argv, '--qty', '?')
    offer_idx = _arg(argv, '--offer-idx', '?')
    mode = _arg(argv, '--mode', '?')

    # Deterministic-ish key for traceability
    key = f"sim-{slot}-{int(time.time() * 1000)}-{_uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── WAL: intent.json ───────────────────────────────────────────
    wal_dir = SIM_WAL_DIR / key
    try:
        wal_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _log(f'  ⚠ sim wal mkdir failed: {e}')

    intent = {
        "simulated": True,
        "idempotency_key": key,
        "slot": slot,
        "runner": runner,
        "script": Path(script).name,
        "step": "bot_main_entry_simulated",
        "event_url": event_url[:120],
        "qty": qty,
        "offer_idx": offer_idx,
        "mode": mode,
        "timestamp": now_iso,
        "source": "simulated",
    }
    try:
        (wal_dir / 'intent.json').write_text(
            json.dumps(intent, indent=2), encoding='utf-8')
    except Exception as e:
        _log(f'  ⚠ sim intent write failed: {e}')

    # ── Structured events (4 lines, full simulated lifecycle) ──────
    events = [
        {"source": "simulated", "runner": runner, "event": "start_flow",
         "idempotency_key": key, "slot": slot,
         "decision": "route_ok", "script": Path(script).name,
         "timestamp": now_iso},
        {"source": "simulated", "runner": runner, "event": "record_intent",
         "idempotency_key": key, "step": "bot_main_entry_simulated",
         "event_url": event_url[:120], "slot": slot, "qty": qty,
         "offer_idx": offer_idx, "mode": mode,
         "timestamp": now_iso},
        {"source": "simulated", "runner": runner, "event": "account_picked",
         "idempotency_key": key, "slot": slot,
         "account_email": f"sim-account-slot{slot}@simulated.local",
         "account_id": f"sim-{slot}-{key[-8:]}",
         "timestamp": now_iso},
        {"source": "simulated", "runner": runner, "event": "record_outcome",
         "idempotency_key": key, "slot": slot,
         "status": "simulated_ok", "rc": 0,
         "timestamp": now_iso},
    ]
    try:
        SIM_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SIM_EVENTS_FILE.open('a', encoding='utf-8') as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
    except Exception as e:
        _log(f'  ⚠ sim events write failed: {e}')

    # ── WAL: outcome.json ──────────────────────────────────────────
    outcome = {
        "simulated": True,
        "idempotency_key": key,
        "slot": slot,
        "runner": runner,
        "script": Path(script).name,
        "status": "simulated_ok",
        "rc": 0,
        "phases_extracted": 2 if runner == 'flowcore' else 0,
        "phases_total": 10,
        "timestamp": now_iso,
        "source": "simulated",
    }
    try:
        (wal_dir / 'outcome.json').write_text(
            json.dumps(outcome, indent=2), encoding='utf-8')
    except Exception as e:
        _log(f'  ⚠ sim outcome write failed: {e}')

    _log(f'  ✓ simulated dispatch · key={key} · WAL={wal_dir.name}')
    return 0


def _execute_simulated_kill(slot: int, runner: str, argv: list[str]) -> int:
    """Simulated kill-switch abort: writes a single kill_switch_aborted event."""
    import json
    from datetime import datetime, timezone
    try:
        SIM_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SIM_EVENTS_FILE.open('a', encoding='utf-8') as f:
            f.write(json.dumps({
                "source": "simulated", "runner": runner,
                "event": "kill_switch_aborted",
                "slot": slot,
                "rc": 99,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
    except Exception:
        pass
    _log(f'  🛑 simulated kill_switch_aborted · slot={slot}')
    return 99


def _slot_from_args(argv: list[str]) -> int:
    try:
        for i, a in enumerate(argv):
            if a == '--slot' and i + 1 < len(argv):
                return int(argv[i + 1])
            if a.startswith('--slot='):
                return int(a.split('=', 1)[1])
    except Exception:
        pass
    return 1


def _read_outcome(slot: int) -> dict:
    path = Path(__file__).parent / f'_compra_parallel_slot{slot}.outcome.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def main() -> int:
    argv = sys.argv[1:]
    slot = _slot_from_args(argv)
    _log(f'iniciando swap-wrapper · slot={slot} · max_swaps={MAX_SWAPS} '
         f'· cooldown={COOLDOWN_S}s')

    # ─── PHASE 1 simulation short-circuit ──────────────────────────
    # If FLOW_MODE=simulated, routing logic still runs (_pick_script,
    # decide_path, canary, kill switch) but NO subprocess is spawned.
    # Instead we write WAL + structured events with source=simulated.
    if _is_simulation():
        script, runner = _pick_script(slot)
        _log(f'═══ SIMULATED · runner={runner} · '
             f'script={Path(script).name} ═══')
        # Kill switch in simulation still aborts (records sim event rc=99)
        try:
            from core.migration import get_kill_switch
            if get_kill_switch():
                return _execute_simulated_kill(slot, runner, argv)
        except Exception:
            pass
        return _execute_simulated(slot, script, runner, argv)

    for attempt in range(1, MAX_SWAPS + 1):
        _log(f'═══ ATTEMPT {attempt}/{MAX_SWAPS} ═══')
        script, runner = _pick_script(slot)
        _log(f'  runner={runner} · script={Path(script).name}')
        t0 = time.time()
        try:
            # FIX 2026-05-15: timeout 10 min hard limit (warmup human_journey
            # toma 2-5 min, + flow checkout 2-3 min, total ~8 min en peor caso).
            r = subprocess.run([sys.executable, script] + argv,
                                check=False, timeout=600)
            rc = r.returncode
        except subprocess.TimeoutExpired as _toe:
            _log(f'subprocess TIMEOUT 600s: killed forcefully')
            rc = 99   # treat as bot-block para que el wrapper haga swap
        except Exception as e:
            _log(f'subprocess error: {type(e).__name__}: {e}')
            return 2
        elapsed = int(time.time() - t0)
        outcome = _read_outcome(slot)
        st = outcome.get('status', '?')
        _log(f'attempt {attempt} terminó rc={rc} status={st} '
             f'elapsed={elapsed}s runner={runner}')

        if rc == RC_BOT_BLOCK:
            burned = outcome.get('burned_profile') or '?'
            subnet = outcome.get('burned_subnet') or '?'
            _log(f'  🚫 bot-block detectado · profile={burned} '
                 f'subnet={subnet}')
            if attempt >= MAX_SWAPS:
                _log(f'  ⛔ max_swaps={MAX_SWAPS} alcanzado — abort')
                return RC_BOT_BLOCK
            _log(f'  ⏳ cooldown {COOLDOWN_S}s antes de swap...')
            time.sleep(COOLDOWN_S)
            _log('  🔄 reintentando con profile/IP nuevos '
                 '(burned se excluyen automáticamente)')
            continue

        # Cualquier otro exit code = no retry (éxito o error no recuperable)
        if rc == 0:
            _log(f'✅ ÉXITO en attempt {attempt}')
        else:
            _log(f'❌ exit code {rc} no recuperable (no es bot-block) — abort')
        return rc

    _log(f'⛔ agotados {MAX_SWAPS} intentos sin éxito')
    return RC_BOT_BLOCK


if __name__ == '__main__':
    sys.exit(main())
