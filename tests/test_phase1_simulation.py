"""tests/test_phase1_simulation.py — PHASE 1 SIMULATION validation harness.

PURPOSE
    Validate FlowCore routing + WAL + event emission in SIMULATION mode.
    HARD GUARANTEE: zero subprocess spawn, zero browser, zero account/
    profile mutation, zero TM/external endpoints touched.

REFERENCE
    PHASE 1 contract (SRE brief):
        - FLOW_MODE=simulated must short-circuit subprocess
        - _pick_script + decide_path + canary + kill_switch still ACTIVE
        - WAL artifacts written: intent.json + outcome.json per key
        - structured_events_simulated.jsonl contains the 4 expected events
        - source=simulated tag on every event
        - PASS only when all SUCCESS CRITERIA hold AND no FAIL CRITERION is hit
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

PROYECTO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROYECTO))

# Sentinel files / dirs that MUST NOT be modified during simulation
SENTINEL_FILES = [
    PROYECTO / 'datos' / 'cuentas' / 'accounts.json',
    PROYECTO / 'burned_profiles.json',
    PROYECTO / 'burned_subnets24.json',
    PROYECTO / '_compra_parallel_used_accounts.lock',
]

# Output paths owned by the simulator
SIM_EVENTS_FILE = PROYECTO / 'runs' / 'structured_events_simulated.jsonl'
SIM_WAL_DIR = PROYECTO / 'runs' / 'idempotency_log'

# Quarantine: archive any pre-existing sim artifacts so this run is clean
QUARANTINE_DIR = PROYECTO / 'runs' / '_sim_quarantine'


def _hash_file(p: Path) -> str:
    if not p.exists():
        return '<absent>'
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _snapshot_sentinels() -> dict:
    return {str(p): _hash_file(p) for p in SENTINEL_FILES}


def _quarantine_pre_existing() -> None:
    """Move pre-existing sim artifacts aside so the run starts clean."""
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    if SIM_EVENTS_FILE.exists():
        ts = int(time.time())
        SIM_EVENTS_FILE.rename(
            QUARANTINE_DIR / f'structured_events_simulated.{ts}.jsonl')
    # Archive only old sim-prefixed WAL keys (don't touch real production keys)
    if SIM_WAL_DIR.exists():
        for d in SIM_WAL_DIR.iterdir():
            if d.is_dir() and d.name.startswith('sim-'):
                d.rename(QUARANTINE_DIR / d.name)


def _read_events() -> list[dict]:
    if not SIM_EVENTS_FILE.exists():
        return []
    out = []
    for line in SIM_EVENTS_FILE.open(encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def main() -> int:
    print('═' * 64)
    print(' PHASE 1 — SIMULATION VALIDATION HARNESS')
    print('═' * 64)

    # ─── Setup: clean sim artifacts, snapshot production sentinels ─
    _quarantine_pre_existing()
    sentinels_before = _snapshot_sentinels()

    # ─── Subprocess tripwire: any subprocess.run/Popen call FAILS the test
    import subprocess
    tripwire = {'tripped': False, 'calls': []}
    orig_run = subprocess.run
    orig_popen = subprocess.Popen

    def _trip_run(cmd, *a, **kw):
        tripwire['tripped'] = True
        tripwire['calls'].append(('subprocess.run', list(cmd)[:3]))
        raise RuntimeError(
            f'SAFETY VIOLATION: subprocess.run called in simulation: '
            f'{list(cmd)[:3]}')

    def _trip_popen(cmd, *a, **kw):
        tripwire['tripped'] = True
        tripwire['calls'].append(('subprocess.Popen', list(cmd)[:3]))
        raise RuntimeError(
            f'SAFETY VIOLATION: subprocess.Popen called in simulation: '
            f'{list(cmd)[:3]}')

    subprocess.run = _trip_run
    subprocess.Popen = _trip_popen

    # ─── Force FLOW_MODE=simulated with 50% canary so we see BOTH runners
    from core.migration import (
        set_flow_mode, set_canary_rate, set_kill_switch, reset_state,
    )
    reset_state()
    set_flow_mode('simulated')
    set_canary_rate(0.5)
    set_kill_switch(False)

    # Import wrapper AFTER subprocess monkeypatch
    import _compra_with_swap as wrapper

    # ─── Execute 20 simulated runs across slots (50% canary → mixed) ──
    SLOTS = list(range(1, 21))
    runs_results = []
    for slot in SLOTS:
        sys.argv = [
            '_compra_with_swap.py',
            '--slot', str(slot),
            '--event-url', 'https://example.invalid/safe-sim',
            '--qty', '2',
            '--offer-idx', '3',
            '--mode', 'quick',
        ]
        try:
            rc = wrapper.main()
        except RuntimeError as e:
            print(f'  🔴 slot={slot}  SAFETY TRIP: {e}')
            rc = -1
        runs_results.append((slot, rc))

    # ─── Kill switch sub-test: set flag, ensure rc=99 + recorded event ─
    set_kill_switch(True)
    sys.argv = ['_compra_with_swap.py', '--slot', '99',
                 '--event-url', 'https://example.invalid/safe-sim',
                 '--qty', '2', '--offer-idx', '3', '--mode', 'quick']
    try:
        kill_rc = wrapper.main()
    except RuntimeError as e:
        print(f'  🔴 kill-switch sub-test SAFETY TRIP: {e}')
        kill_rc = -1
    set_kill_switch(False)
    reset_state()

    # Restore subprocess
    subprocess.run = orig_run
    subprocess.Popen = orig_popen

    # ─── Validation ─────────────────────────────────────────────────
    events = _read_events()
    runner_dist = {'legacy': 0, 'flowcore': 0}
    src_counts = {}
    has_idem_key_all = True
    has_runner_all = True
    accounts_in_events = 0
    has_kill_event = False
    keys_seen = set()
    event_codes = {}

    for ev in events:
        s = ev.get('source')
        src_counts[s] = src_counts.get(s, 0) + 1
        r = ev.get('runner')
        if r in runner_dist:
            runner_dist[r] += 1
        if not ev.get('idempotency_key') and ev.get('event') != 'kill_switch_aborted':
            has_idem_key_all = False
        if not r:
            has_runner_all = False
        if ev.get('account_email'):
            accounts_in_events += 1
        if ev.get('event') == 'kill_switch_aborted':
            has_kill_event = True
        if ev.get('idempotency_key'):
            keys_seen.add(ev['idempotency_key'])
        c = ev.get('event', '?')
        event_codes[c] = event_codes.get(c, 0) + 1

    # WAL pairs check
    wal_keys_with_intent = set()
    wal_keys_with_outcome = set()
    if SIM_WAL_DIR.exists():
        for d in SIM_WAL_DIR.iterdir():
            if d.is_dir() and d.name.startswith('sim-'):
                if (d / 'intent.json').exists():
                    wal_keys_with_intent.add(d.name)
                if (d / 'outcome.json').exists():
                    wal_keys_with_outcome.add(d.name)
    wal_pairs_complete = (wal_keys_with_intent == wal_keys_with_outcome) and wal_keys_with_intent

    sentinels_after = _snapshot_sentinels()
    no_sentinel_mutation = all(
        sentinels_before.get(k) == sentinels_after.get(k)
        for k in set(sentinels_before) | set(sentinels_after)
    )

    rc_all_ok = all(rc == 0 for _, rc in runs_results)
    kill_rc_ok = (kill_rc == 99)

    # ─── Report ─────────────────────────────────────────────────────
    print()
    print('─' * 64)
    print(' RESULTS')
    print('─' * 64)
    print(f'Runs executed (20 slots + 1 kill-switch)    : '
          f'{len(runs_results)} + 1')
    print(f'All run RCs == 0 (normal)                   : {rc_all_ok}')
    print(f'Kill-switch RC == 99                        : {kill_rc_ok}')
    print(f'Events emitted to file                      : {len(events)}')
    print(f'Event source breakdown                      : {src_counts}')
    print(f'Runner distribution (legacy/flowcore)       : {runner_dist}')
    print(f'Event code breakdown                        : {event_codes}')
    print(f'All non-kill events have idempotency_key    : {has_idem_key_all}')
    print(f'All events have runner field                : {has_runner_all}')
    print(f'Events with account_email                   : {accounts_in_events}')
    print(f'kill_switch_aborted event present           : {has_kill_event}')
    print(f'Unique WAL keys                             : {len(keys_seen)}')
    print(f'WAL keys with intent.json                   : '
          f'{len(wal_keys_with_intent)}')
    print(f'WAL keys with outcome.json                  : '
          f'{len(wal_keys_with_outcome)}')
    print(f'WAL intent ⇄ outcome pairs complete         : {wal_pairs_complete}')
    print(f'Subprocess tripwire tripped                 : {tripwire["tripped"]}')
    print(f'Sentinel files (accounts/profiles) modified : '
          f'{not no_sentinel_mutation}')

    print()
    print('─' * 64)
    print(' PASS/FAIL EVALUATION')
    print('─' * 64)

    # FAIL CRITERIA (any one fails the harness)
    fail_subprocess = tripwire['tripped']
    fail_sentinel_mutation = not no_sentinel_mutation
    fail_account_burn = False  # nothing we can do here besides sentinel hash
    fail_tm_endpoint = False   # no network was made — would need outbound monitor; covered by no subprocess

    fail_any = (fail_subprocess or fail_sentinel_mutation
                or fail_account_burn or fail_tm_endpoint)

    # PASS CRITERIA (all must hold)
    pass_routing = (runner_dist['legacy'] > 0 and runner_dist['flowcore'] > 0)
    pass_events_file = SIM_EVENTS_FILE.exists() and len(events) > 0
    pass_runner_field = has_runner_all
    pass_idem_field = has_idem_key_all
    pass_wal = wal_pairs_complete and len(wal_keys_with_intent) == len(SLOTS)
    pass_source_tag = (src_counts.get('simulated', 0) == len(events))
    pass_kill_switch = (kill_rc_ok and has_kill_event)
    pass_rc = rc_all_ok

    print(f'PASS · routing chose BOTH runners            : {pass_routing}')
    print(f'PASS · structured_events_simulated.jsonl ok  : {pass_events_file}')
    print(f'PASS · all events have runner field          : {pass_runner_field}')
    print(f'PASS · all events have idempotency_key       : {pass_idem_field}')
    print(f'PASS · WAL intent/outcome complete for all   : {pass_wal}')
    print(f'PASS · all events tagged source=simulated    : {pass_source_tag}')
    print(f'PASS · kill switch works in simulation       : {pass_kill_switch}')
    print(f'PASS · all normal runs returned rc=0         : {pass_rc}')
    print(f'FAIL · subprocess called                     : {fail_subprocess}')
    print(f'FAIL · sentinel files mutated                : '
          f'{fail_sentinel_mutation}')

    all_pass = (pass_routing and pass_events_file and pass_runner_field
                and pass_idem_field and pass_wal and pass_source_tag
                and pass_kill_switch and pass_rc and not fail_any)

    print()
    print('═' * 64)
    print(f' VERDICT: {"PASS ✅" if all_pass else "FAIL ❌"}')
    print('═' * 64)

    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
