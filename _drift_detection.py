"""Drift detection: FSM v2 (5 estados) vs comportamiento real del bot.

NO re-diseña el FSM. SOLO mide drift entre modelo y runtime real.

Inputs:
  - core/observability/state_machine.py (FSM v2 baseline)
  - runs/test_*.log (12 logs reales con 628 eventos emitidos)
  - _compra_parallel.py (233 eventos definidos en código)

Outputs:
  A) Real state graph (derivado de datos)
  B) Heatmap estado × evento
  C) Matriz transiciones permitidas vs observed
  D) False positives + missing transitions
  E) Top 10 event sequences pre-FATAL/_err/bot_block
"""
from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.observability.state_machine import (
    EVENT_TO_TRANSITION, EventKind, LOOPS_ALLOWED, State, StateMachine,
    TRANSITIONS, classify_event,
)

LOG_PATTERN = re.compile(r'^\[slot\d+\]\[\d{2}:\d{2}:\d{2}\]\s+(\S+)(?:\s+(\{.*?\}))?$')


# ════════════════════════════════════════════════════════════════════
#  1. Cargar runs reales
# ════════════════════════════════════════════════════════════════════
def load_runs():
    """Parsea todos los runs/test_*.log y devuelve [(run_name, [event_names])]."""
    runs = []
    for lf in sorted(Path("runs").glob("test_*.log")):
        seq = []
        with open(lf, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LOG_PATTERN.match(line.strip())
                if m:
                    seq.append(m.group(1))
        if seq:
            runs.append((lf.name, seq))
    return runs


# ════════════════════════════════════════════════════════════════════
#  2. Heuristic state assigner (para eventos no en EVENT_TO_TRANSITION)
# ════════════════════════════════════════════════════════════════════
HEURISTIC_KEYWORDS = {
    State.SETUP: [
        "profile_", "morelogin", "pick_mutex", "pick_retry", "pick_account",
        "cdp_", "persona_", "stealth_", "pre_flight", "storage_state",
        "storage_swap", "eps_warming", "eps_sid", "human_journey", "warmup_",
        "warming_home", "camoufox_launching", "loading_storage_state",
        "stealth_js_added", "headers_set", "geo_set", "stealth_applied",
        "init", "pre_sleep", "auto_profile_destroyed", "destroy_err",
    ],
    State.BROWSING: [
        "hydration_", "page_state", "quick_mode", "best_available",
        "cookies_post_hydration", "cookies_force_close", "cookies_accepted",
        "seatmap", "section_", "sector_", "separated_seats",
        "policy_allow", "ajustar_seleccion", "blockers",
        "stepper", "qty", "find_tickets", "checkbox_acepto",
        "continuar_", "click_continuar", "post_continuar",
        "shot",  # screenshot ubicuo, mayoría en browsing
    ],
    State.LOGIN: [
        "login_", "email_", "pwd_", "oauth", "bad_credentials",
        "recovery", "recover", "force_reset", "auto_recovery",
        "reset_method", "tm_forced", "radio_email", "post_recovery",
        "imap_", "otp_", "account_swap", "max_account_swaps",
        "no_more_accounts", "no_email_input", "no_pwd_input",
        "mid_login", "login_full", "login_swap", "login_poll",
        "account_picked", "pick_account_tier",
    ],
    State.CHECKOUT: [
        "pre_checkout", "ir_al_pago", "partners_seguro",
        "fill_card_start", "fill_card_data", "fill_card_inputs",
        "province", "guardar_continuar", "fill_card_done",
        "address", "checkout_stage", "checkout_detected",
        "adyen", "payments_", "card_radio", "card_iframe",
        "card_inputs", "card_fallback", "card_fields",
        "card_fill", "anadir_tarjeta", "realizar_compra",
        "3ds", "fill_card_adyen", "iframes_", "main_page",
        "watch_for_checkout", "watch_initial", "watch_err",
    ],
    State.EXIT: [
        "stop_received", "history_cleanup", "history_files",
        "profile_closed", "profile_auto_destroyed",
        "profile_auto_burned", "profile_burned",
        "auto_burn", "destroy", "camoufox_close",
        "camoufox_explicit", "outcome_persist",
        "timeout_no_checkout", "queue_it",
        "ready_for_user", "bot_block",
    ],
}


def assign_state_heuristic(event_name: str) -> Optional[State]:
    """Asigna estado por keyword match (ignora primera asignación FSM)."""
    el = event_name.lower()
    matches = []
    for state, kws in HEURISTIC_KEYWORDS.items():
        for kw in kws:
            if kw in el:
                matches.append((state, len(kw)))
    if event_name.startswith("FATAL_") and not matches:
        return State.EXIT
    if not matches:
        return None
    # Más específico gana (palabra clave más larga)
    matches.sort(key=lambda m: -m[1])
    return matches[0][0]


def assign_state_with_fsm(event_name: str) -> Optional[State]:
    """Si EVENT_TO_TRANSITION lo mapea, usar ese target. Sino heuristic."""
    kind, target = classify_event(event_name)
    if kind in (EventKind.TRANSITION, EventKind.LOOP) and target is not None:
        return target
    return assign_state_heuristic(event_name)


# ════════════════════════════════════════════════════════════════════
#  3. Análisis principal
# ════════════════════════════════════════════════════════════════════
runs = load_runs()
print(f"Runs cargados: {len(runs)}")
total_events = sum(len(s) for _, s in runs)
print(f"Total eventos emitidos: {total_events}")

# Frecuencia real por evento
event_freq = Counter()
for _, seq in runs:
    event_freq.update(seq)
print(f"Eventos únicos en runs: {len(event_freq)}")


# ── A) STATE COVERAGE REAL ──────────────────────────────────────────
print("\n" + "=" * 70)
print("A) STATE COVERAGE REAL (runtime)")
print("=" * 70)

state_hits = Counter()
unmapped_events = []
event_to_state = {}
for evt, freq in event_freq.items():
    s = assign_state_with_fsm(evt)
    event_to_state[evt] = s
    if s is None:
        unmapped_events.append((evt, freq))
    else:
        state_hits[s] += freq

print("\nDistribución de runtime hits por estado FSM v2:")
for state in State:
    hits = state_hits.get(state, 0)
    pct = 100 * hits / total_events
    bar = "#" * min(int(pct * 0.7), 60)
    print(f"  {state.value:10} {hits:4} hits  ({pct:5.1f}%)  {bar}")

print(f"\nEventos NO asignables a ningún estado: {len(unmapped_events)}")
for evt, n in unmapped_events:
    print(f"  ({n}x)  {evt}")


# ── DETECCIÓN DE MISCLASSIFICATION via FSM simulator ────────────────
# Para cada run, simular FSM. Si un evento aparece cuando el estado actual
# del FSM != estado asignado al evento → misclassification candidate.
print("\n" + "=" * 70)
print("MISCLASSIFICATION: eventos en estado FSM diferente al asignado")
print("=" * 70)

misclass = Counter()  # (event_name, current_state) → count
run_traces = []  # [(run_name, [(event, state_at_emit)])]

for run_name, seq in runs:
    sm = StateMachine(strict=False)  # non-strict: registra invalid pero sigue
    trace = []
    for evt in seq:
        # Estado FSM ANTES de procesar el evento
        state_before = sm.current
        sm.feed_event(evt)
        # Estado asignado al evento (de mapping/heurística)
        assigned = event_to_state.get(evt)
        trace.append((evt, state_before, assigned))
        # Si el evento "vive" en un estado distinto al actual FSM → misclassification
        if assigned is not None and assigned != state_before and assigned != sm.current:
            misclass[(evt, state_before.value, assigned.value)] += 1
    run_traces.append((run_name, trace))

print(f"\nMisclassifications detectadas: {len(misclass)} pares evento×estado_actual")
top_misc = misclass.most_common(15)
print("\nTop misclassifications:")
for (evt, current_fsm, assigned), n in top_misc:
    print(f"  ({n}x)  {evt:35} emitido en estado={current_fsm:10} pero asignado→{assigned}")


# ── B) HEATMAP estado × eventos top ─────────────────────────────────
print("\n" + "=" * 70)
print("B) HEATMAP — eventos dominantes por estado FSM v2")
print("=" * 70)

events_per_state = defaultdict(Counter)
for evt, state in event_to_state.items():
    if state is not None:
        events_per_state[state][evt] = event_freq[evt]

for state in State:
    bag = events_per_state.get(state, Counter())
    if not bag:
        print(f"\n[{state.value}] (sin eventos)")
        continue
    total = sum(bag.values())
    print(f"\n[{state.value}] {total} hits ({len(bag)} eventos)")
    for evt, n in bag.most_common(10):
        pct_state = 100 * n / total
        pct_global = 100 * n / total_events
        bar = "#" * min(int(pct_state * 0.6), 40)
        print(f"  {n:3}  {pct_state:5.1f}%  {evt:38} {bar}")


# ── C) MATRIZ transiciones permitidas vs observed ───────────────────
print("\n" + "=" * 70)
print("C) MATRIZ transiciones (PERMITIDAS por FSM vs OBSERVADAS en runs)")
print("=" * 70)

# Construir transiciones runtime entre ESTADOS (no entre eventos)
state_transitions_observed = Counter()
for run_name, trace in run_traces:
    prev_state = None
    for evt, state_before, assigned in trace:
        # Usar estado FSM efectivo (after feed_event) para tracking
        # Re-simular para obtener estado after de forma limpia
        pass

# Re-simular para tracking de estado after
for run_name, seq in runs:
    sm = StateMachine(strict=False)
    prev_state = sm.current
    for evt in seq:
        before = sm.current
        sm.feed_event(evt)
        after = sm.current
        if before != after:
            state_transitions_observed[(before, after)] += 1

print("\nMatriz: (filas=FROM, columnas=TO). Cells = #observados | * si permitida\n")
header = f"  {'FROM\\TO':12} " + " ".join(f"{s.value[:8]:>8}" for s in State)
print(header)
print("  " + "-" * (len(header) - 2))
for from_state in State:
    row_str = f"  {from_state.value:12} "
    for to_state in State:
        observed = state_transitions_observed.get((from_state, to_state), 0)
        allowed = to_state in TRANSITIONS.get(from_state, frozenset())
        if observed > 0 and allowed:
            cell = f"{observed:>5}* "  # observed + allowed
        elif observed > 0:
            cell = f"{observed:>5}! "  # observed pero NO allowed (drift)
        elif allowed:
            cell = "    -*  "  # allowed pero NO observed (false positive)
        else:
            cell = "     .  "
        row_str += cell
    print(row_str)
print("\nLeyenda:  * = permitido por FSM  | número = observado en runs")
print("           ! = OBSERVADO pero NO permitido (drift hacia FSM)")
print("           -* = PERMITIDO pero NUNCA observado (false positive FSM)")


# ── D) FALSE POSITIVES + MISSING TRANSITIONS ───────────────────────
print("\n" + "=" * 70)
print("D) FALSE POSITIVES (FSM permite, runtime nunca lo hace)")
print("   + MISSING (runtime lo hace, FSM no permite)")
print("=" * 70)

allowed_transitions = set()
for from_state, to_set in TRANSITIONS.items():
    for to_state in to_set:
        allowed_transitions.add((from_state, to_state))

observed_set = set(state_transitions_observed.keys())
false_positives = allowed_transitions - observed_set
missing = observed_set - allowed_transitions

print(f"\nFalse positives (FSM declara, runtime no lo hace): {len(false_positives)}")
for fs, ts in sorted(false_positives, key=lambda x: (x[0].value, x[1].value)):
    print(f"  {fs.value:10} -> {ts.value:10}  (FSM lo permite, 0 observados)")

print(f"\nMissing transitions (runtime lo hace, FSM no lo permite): {len(missing)}")
for fs, ts in sorted(missing, key=lambda x: (x[0].value, x[1].value)):
    n = state_transitions_observed[(fs, ts)]
    print(f"  {fs.value:10} -> {ts.value:10}  ({n} observados, FSM lo prohíbe)")


# ── ESTADOS INFRAUTILIZADOS / SOBREUSADOS ──────────────────────────
print("\n" + "=" * 70)
print("ESTADOS INFRAUTILIZADOS (<5% del runtime)")
print("=" * 70)
threshold = total_events * 0.05
for state in State:
    hits = state_hits.get(state, 0)
    pct = 100 * hits / total_events
    if hits < threshold:
        print(f"  [LOW] {state.value:10} {hits:4} hits ({pct:.1f}%) — infrautilizado")

# Cajón de sastre: estado con muchos eventos diferentes con baja frecuencia individual
print("\nESTADOS CAJÓN DE SASTRE (alta variedad de eventos):")
for state in State:
    bag = events_per_state.get(state, Counter())
    if not bag:
        continue
    n_unique = len(bag)
    total = sum(bag.values())
    avg_freq = total / n_unique if n_unique > 0 else 0
    variance_ratio = n_unique / max(total, 1)
    print(f"  {state.value:10} {n_unique:3} eventos únicos / {total:4} hits = "
          f"variedad ratio {variance_ratio:.3f} (avg freq {avg_freq:.1f})")


# ── E) FAILURE TOPOLOGY ────────────────────────────────────────────
print("\n" + "=" * 70)
print("E) FAILURE TOPOLOGY: dónde + cómo se rompe el bot")
print("=" * 70)

# Identificar eventos de fallo
def is_failure(evt: str) -> bool:
    el = evt.lower()
    return (evt.startswith("FATAL_") or "bot_block" in el or
            "_err" in el or "_failed" in el or
            evt == "BAD_CREDENTIALS_detected" or "no_email_input" == evt or
            "no_pwd_input" == evt or evt == "TIMEOUT_no_checkout")

failure_events = [e for e in event_freq if is_failure(e)]
print(f"\nEventos de fallo detectados: {len(failure_events)}")

# Frecuencia de fallos por estado
failure_by_state = Counter()
for fevt in failure_events:
    state = event_to_state.get(fevt)
    if state:
        failure_by_state[state] += event_freq[fevt]
print("\nFallos por estado FSM v2:")
for state in State:
    n = failure_by_state.get(state, 0)
    print(f"  {state.value:10} {n} fallos")

# Top 10 event sequences ANTES de un fallo
print("\nTop 10 event sequences PRE-FALLO (3 eventos antes del fallo):")
pre_failure_sequences = Counter()
for run_name, seq in runs:
    for i, evt in enumerate(seq):
        if is_failure(evt) and i >= 3:
            # Capturar los 3 anteriores
            pre_seq = tuple(seq[i-3:i])
            pre_failure_sequences[(pre_seq, evt)] += 1

for (pre, failure), n in pre_failure_sequences.most_common(15):
    arrows = " -> ".join(pre)
    print(f"  ({n}x)  {arrows} -> [{failure}]")


# ── LOOPS REALES correlacionados con bloqueos ──────────────────────
print("\n" + "=" * 70)
print("LOOPS REALES correlacionados con failure")
print("=" * 70)

# Detectar loops A→A o A→B→A en runs reales y ver si están seguidos de failure
loop_counts = Counter()  # (loop_pattern) → count
loop_to_failure = Counter()  # (loop_pattern) → next event is failure
for run_name, seq in runs:
    for i in range(len(seq) - 2):
        if seq[i] == seq[i+1]:
            # Auto-loop A-A
            loop_counts[(seq[i],)] += 1
            if i+2 < len(seq) and is_failure(seq[i+2]):
                loop_to_failure[(seq[i],)] += 1
        elif i + 2 < len(seq) and seq[i] == seq[i+2]:
            # A-B-A cycle
            loop_counts[(seq[i], seq[i+1])] += 1
            if i+3 < len(seq) and is_failure(seq[i+3]):
                loop_to_failure[(seq[i], seq[i+1])] += 1

print("\nTop loops reales y % que termina en failure:")
for loop, n in loop_counts.most_common(10):
    fail_count = loop_to_failure.get(loop, 0)
    pct = 100 * fail_count / n if n > 0 else 0
    name = " -> ".join(loop) + " (-> back)"
    print(f"  ({n}x loop)  fail next: {fail_count} ({pct:.0f}%)  pattern: {name}")


# ── SAVE TODO ─────────────────────────────────────────────────────
print("\n" + "=" * 70)

drift_export = {
    "fsm_version": "v2_5states",
    "runs_analyzed": len(runs),
    "total_events": total_events,
    "unique_events_observed": len(event_freq),
    "state_coverage": {s.value: state_hits[s] for s in State},
    "unmapped_events": [{"event": e, "freq": n} for e, n in unmapped_events],
    "misclassification_top15": [
        {"event": e, "fsm_state_when_emitted": c, "assigned_state": a, "count": n}
        for (e, c, a), n in misclass.most_common(15)
    ],
    "events_per_state": {
        s.value: events_per_state.get(s, Counter()).most_common(20)
        for s in State
    },
    "state_transitions_observed": [
        {"from": fs.value, "to": ts.value, "count": n}
        for (fs, ts), n in state_transitions_observed.most_common()
    ],
    "false_positives_fsm": [
        {"from": fs.value, "to": ts.value}
        for fs, ts in sorted(false_positives, key=lambda x: (x[0].value, x[1].value))
    ],
    "missing_transitions": [
        {"from": fs.value, "to": ts.value,
         "observed_count": state_transitions_observed[(fs, ts)]}
        for fs, ts in sorted(missing, key=lambda x: (x[0].value, x[1].value))
    ],
    "failure_by_state": {s.value: failure_by_state[s] for s in State},
    "pre_failure_sequences_top10": [
        {"sequence": list(pre), "failure": f, "count": n}
        for (pre, f), n in pre_failure_sequences.most_common(10)
    ],
    "loops_with_failure_rate": [
        {"pattern": list(p), "count": n,
         "followed_by_failure": loop_to_failure.get(p, 0),
         "failure_rate_pct": round(100 * loop_to_failure.get(p, 0) / n, 1)}
        for p, n in loop_counts.most_common(10)
    ],
}

Path("runs").mkdir(exist_ok=True)
with open("runs/drift_detection.json", "w", encoding="utf-8") as f:
    json.dump(drift_export, f, indent=2, ensure_ascii=False)

print(f"Drift detection report saved: runs/drift_detection.json")
print(f"  {Path('runs/drift_detection.json').stat().st_size:,}B")
