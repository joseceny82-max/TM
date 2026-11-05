"""Gap analysis: 231 eventos vs FSM final de 5 estados."""
import re
import sys
sys.path.insert(0, '.')

with open('_compra_parallel.py', encoding='utf-8') as f:
    src = f.read()

events = sorted(set(re.findall(r'log\(slot,\s*[\"\'](\w+)[\"\']', src)))
print(f"Total event names unicos: {len(events)}")

from core.observability.state_machine import (
    classify_event, EventKind, State,
    EVENT_TO_TRANSITION, all_transition_events, all_loop_events,
    all_observability_events, LOOPS_ALLOWED,
)


def heuristic_state(evt):
    """Mapea eventos al modelo final de 5 estados (SETUP, BROWSING, LOGIN, CHECKOUT, EXIT)."""
    el = evt.lower()
    # SETUP — profile + session + warming
    if any(k in el for k in ['profile_', 'morelogin', 'pick_mutex', 'pick_retry',
                              'cdp_', 'persona_', 'stealth_', 'pre_flight', 'storage_state',
                              'storage_swap', 'eps_warming', 'eps_sid', 'human_journey', 'warmup_',
                              'warming_home', 'camoufox_launching']):
        return State.SETUP
    # BROWSING — page hydration + seatmap + cart pre-Continuar
    if any(k in el for k in ['hydration_', 'page_state', 'quick_mode', 'best_available',
                              'cookies_post_hydration', 'cookies_force_close', 'cookies_accepted',
                              'seatmap', 'section_', 'sector_', 'separated_seats',
                              'policy_allow', 'ajustar_seleccion', 'blockers',
                              'stepper', 'qty', 'find_tickets', 'checkbox_acepto',
                              'continuar_', 'click_continuar', 'post_continuar']):
        return State.BROWSING
    # LOGIN — OAuth + recovery + account swap
    if any(k in el for k in ['login_', 'email_', 'pwd_', 'oauth', 'bad_credentials',
                              'recovery', 'recover', 'force_reset', 'auto_recovery',
                              'reset_method', 'tm_forced', 'radio_email', 'post_recovery',
                              'imap_', 'otp_', 'account_swap', 'max_account_swaps',
                              'no_more_accounts', 'no_email_input', 'no_pwd_input',
                              'mid_login', 'login_full', 'login_swap', 'login_poll',
                              'account_picked', 'pick_account']):
        return State.LOGIN
    # CHECKOUT — checkout + Adyen + 3DS detect
    if any(k in el for k in ['pre_checkout', 'ir_al_pago', 'partners_seguro',
                              'fill_card_start', 'fill_card_data', 'fill_card_inputs',
                              'province', 'guardar_continuar', 'fill_card_done',
                              'address', 'checkout_stage',
                              'adyen', 'payments_', 'card_radio', 'card_iframe',
                              'card_inputs', 'card_fallback', 'card_fields',
                              'card_fill', 'anadir_tarjeta', 'realizar_compra',
                              '3ds', 'fill_card_adyen', 'iframes_', 'main_page',
                              'watch_for_checkout', 'watch_initial', 'watch_err']):
        return State.CHECKOUT
    # EXIT — terminal / cleanup
    if any(k in el for k in ['stop_received', 'history_cleanup', 'history_files',
                              'profile_closed', 'profile_auto_destroyed',
                              'profile_auto_burned', 'profile_burned',
                              'auto_burn', 'destroy', 'camoufox_close',
                              'camoufox_explicit', 'outcome_persist',
                              'timeout_no_checkout', 'shot_err', 'queue_it',
                              'ready_for_user', 'bot_block']):
        return State.EXIT
    if evt.startswith('FATAL_'):
        return State.EXIT
    return None


results = {
    'transition_mapped': [],
    'loop_mapped': [],
    'observability_mapped': [],
    'heuristic_only': {},
    'truly_unmapped': [],
}
state_population = {s: [] for s in State}

for evt in events:
    kind, target = classify_event(evt)
    if kind == EventKind.TRANSITION:
        results['transition_mapped'].append((evt, target))
        if target: state_population[target].append(evt)
    elif kind == EventKind.LOOP:
        results['loop_mapped'].append((evt, target))
        if target: state_population[target].append(evt)
    elif kind == EventKind.OBSERVABILITY:
        results['observability_mapped'].append(evt)
    else:
        h = heuristic_state(evt)
        if h is not None:
            results['heuristic_only'].setdefault(h, []).append(evt)
            state_population[h].append(evt)
        else:
            results['truly_unmapped'].append(evt)

# === REPORTE ===
print()
print("=== TIPO DE EVENTO (modelo 5 estados) ===")
print(f"  TRANSITION explicit:    {len(results['transition_mapped']):3}")
print(f"  LOOP explicit:          {len(results['loop_mapped']):3}")
print(f"  OBSERVABILITY explicit: {len(results['observability_mapped']):3}")
h_total = sum(len(v) for v in results['heuristic_only'].values())
print(f"  Heuristic-only:         {h_total:3}")
print(f"  Truly UNMAPPED:         {len(results['truly_unmapped']):3}")
total = (len(results['transition_mapped']) + len(results['loop_mapped'])
         + len(results['observability_mapped']) + h_total + len(results['truly_unmapped']))
print(f"  TOTAL:                  {total}")

print()
print("=== POBLACION POR ESTADO ===")
for state in State:
    pop = state_population[state]
    pct = 100 * len(pop) / 231
    bar = '#' * int(pct / 2)
    print(f"  {state.value:10} {len(pop):3}  ({pct:5.1f}%)  {bar}")

print()
print("=== ESTADOS CON <3 EVENTOS ===")
empty = [s for s in State if len(state_population[s]) < 3]
if not empty:
    print("  (ninguno — todos los estados tienen >=3 eventos)")
else:
    for state in empty:
        print(f"  [LOW] {state.value:10} {len(state_population[state])} eventos: {state_population[state]}")

print()
print(f"=== EVENTOS NO MAPEABLES ({len(results['truly_unmapped'])}) ===")
for e in results['truly_unmapped']:
    print(f"  {e}")

print()
print("=== LOOPS REALES ===")
loop_by_state = {}
for evt, state in results['loop_mapped']:
    if state:
        loop_by_state.setdefault(state, []).append(evt)
for state in State:
    if state in loop_by_state:
        evts = loop_by_state[state]
        print(f"  {state.value:10} {len(evts)} loops: {evts}")
print()
print(f"  LOOPS_ALLOWED declarados: {[s.value for s in LOOPS_ALLOWED]}")
missing = [s.value for s in LOOPS_ALLOWED if s not in loop_by_state]
if missing:
    print(f"  Declarados sin uso real: {missing}")
else:
    print(f"  Todos los LOOPS_ALLOWED tienen eventos reales: OK")

# Coverage final
explicit = len(results['transition_mapped']) + len(results['loop_mapped']) + len(results['observability_mapped'])
total_classified = explicit + h_total
coverage = 100 * total_classified / 231
print()
print(f"=== COBERTURA TOTAL ===")
print(f"  Mapeo explicito:      {explicit}/{231} = {100*explicit/231:.1f}%")
print(f"  + Heuristic-only:     {h_total}/{231} = {100*h_total/231:.1f}%")
print(f"  = COVERAGE TOTAL:     {total_classified}/{231} = {coverage:.1f}%")
print(f"  Truly unmapped:       {len(results['truly_unmapped'])}/{231} = {100*len(results['truly_unmapped'])/231:.1f}%")

# Save
import json
out = {
    'model_version': 'v2_5states',
    'state_population': {s.value: state_population[s] for s in State},
    'truly_unmapped': results['truly_unmapped'],
    'heuristic_only': {s.value: results['heuristic_only'].get(s, []) for s in State if s in results['heuristic_only']},
    'loop_by_state': {s.value: loop_by_state[s] for s in loop_by_state},
    'totals': {
        'transition_explicit': len(results['transition_mapped']),
        'loop_explicit': len(results['loop_mapped']),
        'observability_explicit': len(results['observability_mapped']),
        'heuristic_only': h_total,
        'truly_unmapped': len(results['truly_unmapped']),
        'coverage_pct': round(coverage, 1),
    },
}
import os
os.makedirs('runs', exist_ok=True)
with open('runs/fsm_gap_mapping.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print()
print(f"Mapping JSON: runs/fsm_gap_mapping.json")
