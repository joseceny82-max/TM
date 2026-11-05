"""Validación estrictamente empírica del FSM v2 contra runtime real.

REGLA: RUNS_REALES > LOGS > AST > MODELO
NO usa el FSM como verdad. Reconstruye comportamiento desde datos.

OUTPUTS:
  A) Real state graph (clusters emergentes por co-ocurrencia)
  B) Transition matrix REAL vs FSM (lado a lado)
  C) Heatmap real por estado (hits)
  D) Drift crítico (FSM dice X pero pasa Y)
  E) Top failure patterns (secuencias previas a fallo)

NO rediseña, NO simplifica, NO optimiza. Solo MIDE.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Pattern para parsear los logs `[slotN][HH:MM:SS] event_name {payload}?`
LOG_PATTERN = re.compile(r'^\[slot(\d+)\]\[(\d{2}:\d{2}:\d{2})\]\s+(\S+)(?:\s+(\{.*?\}))?$')

# Eventos que indican FALLO (no se inventan, solo se reconocen patrones reales)
FAILURE_PATTERNS = (
    "FATAL_", "_err", "_failed", "no_email_input", "no_more_accounts",
    "bot_block", "TIMEOUT_", "max_account_swaps", "force_reset_btn_failed",
    "reset_method_next_failed", "recovery_failed",
)

# ════════════════════════════════════════════════════════════════════
# PASO 1: PARSEAR LOGS — sin interpretación
# ════════════════════════════════════════════════════════════════════
logs_dir = Path("runs")
log_files = sorted(logs_dir.glob("test_*.log"))
print(f"=== PASO 1: Parsear logs (sin interpretar) ===")
print(f"Logs encontrados: {len(log_files)}\n")

runs = []  # [(filename, [(event, ts, payload), ...]), ...]
for lf in log_files:
    seq = []
    with open(lf, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_PATTERN.match(line.strip())
            if not m:
                continue
            _slot, ts, evt, payload = m.groups()
            try:
                p = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                p = {}
            seq.append((evt, ts, p))
    if seq:
        runs.append((lf.name, seq))
        print(f"  {lf.name:50}  {len(seq):4} eventos")

total_events = sum(len(s) for _, s in runs)
event_freq = Counter()
for _, seq in runs:
    for evt, _, _ in seq:
        event_freq[evt] += 1
unique_events = set(event_freq.keys())
print(f"\nTOTAL: {total_events} eventos, {len(unique_events)} únicos en {len(runs)} runs\n")

# ════════════════════════════════════════════════════════════════════
# PASO 2: CONSTRUIR GRAFO EVENTO→EVENTO (transiciones reales, no estados)
# ════════════════════════════════════════════════════════════════════
print("=== PASO 2: Construir grafo evento→evento (sin FSM) ===")
transitions_e2e = Counter()  # {(e1, e2): count}
for _, seq in runs:
    for i in range(len(seq) - 1):
        e1, _, _ = seq[i]
        e2, _, _ = seq[i + 1]
        transitions_e2e[(e1, e2)] += 1
print(f"Transiciones únicas evento→evento: {len(transitions_e2e)}")
print(f"Suma total de pesos: {sum(transitions_e2e.values())}\n")

# ════════════════════════════════════════════════════════════════════
# PASO 3: CLUSTERS EMERGENTES — por vecindario común (sin FSM)
# ════════════════════════════════════════════════════════════════════
# Heurística empírica: dos eventos están en el mismo "cluster real" si
# - su vecindario (predecesores/sucesores) se solapa fuertemente
# - o si aparecen en secuencia inmediata muy frecuente
print("=== PASO 3: Identificar clusters emergentes (Jaccard de vecindarios) ===")
# Vecindario de cada evento
neighbors_out = defaultdict(set)
neighbors_in = defaultdict(set)
for (e1, e2), _w in transitions_e2e.items():
    neighbors_out[e1].add(e2)
    neighbors_in[e2].add(e1)

def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)

# Para cada evento, encontrar otros con Jaccard alto en vecindario combinado
event_list = sorted(unique_events)
similar_pairs = []
for i, e1 in enumerate(event_list):
    n1 = neighbors_out[e1] | neighbors_in[e1]
    for e2 in event_list[i+1:]:
        n2 = neighbors_out[e2] | neighbors_in[e2]
        j = jaccard(n1, n2)
        if j >= 0.5 and (len(n1) >= 2 and len(n2) >= 2):
            similar_pairs.append((e1, e2, j))

similar_pairs.sort(key=lambda x: -x[2])
print(f"Pares con Jaccard>=0.5: {len(similar_pairs)}\n")

# ════════════════════════════════════════════════════════════════════
# PASO 4: AGRUPAR en cluster IDs (union-find sobre similar_pairs)
# ════════════════════════════════════════════════════════════════════
print("=== PASO 4: Union-find clustering ===")
parent = {e: e for e in unique_events}

def find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x

def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[ra] = rb

# Solo unir pares con Jaccard fuerte (>=0.6) y al menos 3 vecinos comunes
for e1, e2, j in similar_pairs:
    if j >= 0.6:
        common = (neighbors_out[e1] | neighbors_in[e1]) & (neighbors_out[e2] | neighbors_in[e2])
        if len(common) >= 3:
            union(e1, e2)

# Construir clusters
clusters_emergent = defaultdict(set)
for e in unique_events:
    clusters_emergent[find(e)].add(e)

# Renombrar clusters por su "evento ancla" (el más frecuente del cluster)
clusters_named = {}
for root, evts in clusters_emergent.items():
    anchor = max(evts, key=lambda e: event_freq[e])
    clusters_named[anchor] = evts

# Filtrar singletons (clusters de 1 evento) - se reportan separados
multi_clusters = {k: v for k, v in clusters_named.items() if len(v) > 1}
singletons = {k: v for k, v in clusters_named.items() if len(v) == 1}
print(f"Clusters emergentes (>=2 eventos): {len(multi_clusters)}")
print(f"Eventos singleton: {len(singletons)}")
print()

# Mostrar top clusters
top_clusters = sorted(multi_clusters.items(),
                       key=lambda kv: -sum(event_freq[e] for e in kv[1]))
print("Top 10 clusters emergentes (sin nombre, por anchor event):")
for anchor, evts in top_clusters[:10]:
    total = sum(event_freq[e] for e in evts)
    print(f"  anchor={anchor:35} size={len(evts):3}  total_hits={total:4}")
print()

# ════════════════════════════════════════════════════════════════════
# PASO 5: CARGAR FSM v2 — SOLO COMO REFERENCIA, no para clasificar
# ════════════════════════════════════════════════════════════════════
print("=== PASO 5: Cargar FSM v2 (referencia, no verdad) ===")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.observability.state_machine import (
    State, TRANSITIONS, LOOPS_ALLOWED, EVENT_TO_TRANSITION, EventKind, classify_event,
)

fsm_states = [s.value for s in State]
fsm_allowed = set()
for src, dsts in TRANSITIONS.items():
    for dst in dsts:
        fsm_allowed.add((src.value, dst.value))

print(f"FSM declara {len(fsm_states)} estados, {len(fsm_allowed)} transiciones permitidas")
print(f"FSM declara {len(EVENT_TO_TRANSITION)} eventos mapeados explícitamente\n")

# ════════════════════════════════════════════════════════════════════
# PASO 6: ASIGNAR EVENTOS A ESTADOS FSM — pero MIDIENDO si encajan
# ════════════════════════════════════════════════════════════════════
# Para cada evento observado, ver si el FSM lo mapea explícitamente o no.
# NO inferimos heurísticamente: si no hay mapping, queda "UNMAPPED".
print("=== PASO 6: Coverage del FSM sobre eventos reales ===")
fsm_mapped = {}
unmapped = []
for evt in unique_events:
    spec = EVENT_TO_TRANSITION.get(evt)
    if spec:
        kind, target, _ = spec
        fsm_mapped[evt] = (kind.value, target.value if target else None)
    else:
        unmapped.append(evt)

print(f"Eventos observados: {len(unique_events)}")
print(f"  Mapeados por FSM:    {len(fsm_mapped)}")
print(f"  UNMAPPED en FSM:     {len(unmapped)}  ({100*len(unmapped)/len(unique_events):.1f}%)")
print()

# ════════════════════════════════════════════════════════════════════
# PASO 7: SIMULAR FSM SOBRE LOGS REALES — para extraer transiciones de estados
# ════════════════════════════════════════════════════════════════════
print("=== PASO 7: Simular FSM sobre logs reales ===")
# Para cada run, recorrer eventos. Cada vez que un evento TRANSITION o LOOP
# del FSM aparece, intentar la transición. Anotar las que fallarían vs las
# que se ejecutan. NO inferimos estado para eventos UNMAPPED.

observed_state_transitions = Counter()  # {(src, dst): count}
events_emitted_per_state = defaultdict(Counter)  # state -> Counter({event: n})
runs_outcome = []  # [(run_name, last_state, events_count, exits_observed)]

for run_name, seq in runs:
    current = "setup"  # punto inicial empírico (todos los runs empiezan aquí)
    saw_states = ["setup"]
    for evt, _ts, _p in seq:
        # Atribuir evento al estado ACTUAL (sin reclasificar)
        events_emitted_per_state[current][evt] += 1
        # Si el FSM mapea como TRANSITION, intentar mover
        spec = EVENT_TO_TRANSITION.get(evt)
        if spec:
            kind, target, _ = spec
            if kind == EventKind.TRANSITION and target is not None:
                tgt = target.value
                if tgt != current:
                    observed_state_transitions[(current, tgt)] += 1
                    current = tgt
                    saw_states.append(current)
            elif kind == EventKind.LOOP and target is not None and target.value == current:
                # self-loop solo si estamos en el estado correcto
                observed_state_transitions[(current, current)] += 1
    runs_outcome.append((run_name, current, len(seq), saw_states))

print("Final state por run:")
for rn, last, n_events, path in runs_outcome:
    print(f"  {rn[:40]:40}  events={n_events:4}  final={last:10}  path={' → '.join(path)}")
print()

# ════════════════════════════════════════════════════════════════════
# PASO 8: B) TRANSITION MATRIX REAL vs FSM
# ════════════════════════════════════════════════════════════════════
print("=== PASO 8: B) TRANSITION MATRIX — FSM permitidas vs observadas ===")
all_pairs = set(fsm_allowed) | set(observed_state_transitions.keys())
fsm_not_observed = sorted(fsm_allowed - set(observed_state_transitions.keys()))
observed_not_fsm = sorted(set(observed_state_transitions.keys()) - fsm_allowed)
intersection = sorted(fsm_allowed & set(observed_state_transitions.keys()))

print(f"\nTransiciones permitidas por FSM:       {len(fsm_allowed)}")
print(f"Transiciones observadas en runtime:    {len(observed_state_transitions)}")
print(f"Intersección (permitidas + observadas): {len(intersection)}")
print(f"FSM permite pero NUNCA observa:          {len(fsm_not_observed)}")
print(f"Observadas pero FSM NO permite:          {len(observed_not_fsm)}")
print()
print("Matriz 5x5 (R=runtime count, A=allowed, ─=neither):")
print(f"  {'':12}", "  ".join(f"{s:9}" for s in fsm_states))
for src in fsm_states:
    row = [f"{src:12}"]
    for dst in fsm_states:
        a = (src, dst) in fsm_allowed
        c = observed_state_transitions.get((src, dst), 0)
        if c > 0 and a:
            row.append(f"{c:>3}A     ")  # observada + permitida
        elif c > 0 and not a:
            row.append(f"{c:>3}X!    ")  # observada pero NO permitida (DRIFT crítico)
        elif a:
            row.append(f"  -A     ")  # permitida no observada (false positive FSM)
        else:
            row.append(f"  --     ")  # ni una ni otra
    print(" ", "  ".join(row))
print()

# ════════════════════════════════════════════════════════════════════
# PASO 9: C) HEATMAP — hits reales por estado FSM
# ════════════════════════════════════════════════════════════════════
print("=== PASO 9: C) HEATMAP — hits reales por estado FSM ===")
state_totals = {s: sum(events_emitted_per_state[s].values()) for s in fsm_states}
total = sum(state_totals.values()) or 1
print(f"\nEstado FSM         Hits    %        Bar")
print(f"{'─'*64}")
for s in fsm_states:
    n = state_totals[s]
    pct = 100 * n / total
    bar_len = int(pct / 2)
    bar = "█" * bar_len
    print(f"  {s:15} {n:5}  {pct:5.1f}%   {bar}")
print()

# Top eventos por estado
print("Top 5 eventos por estado FSM (estado=donde se emitió, según simulación):")
for s in fsm_states:
    top = events_emitted_per_state[s].most_common(5)
    print(f"  [{s}]")
    for evt, n in top:
        print(f"    {n:4}  {evt}")
print()

# ════════════════════════════════════════════════════════════════════
# PASO 10: D) DRIFT CRÍTICO
# ════════════════════════════════════════════════════════════════════
print("=== PASO 10: D) DRIFT CRÍTICO ===")

# D1: Transiciones permitidas por FSM pero NUNCA observadas
print("\nD1) FSM permite pero NUNCA observadas (false positives del modelo):")
for src, dst in fsm_not_observed:
    print(f"  - {src:10} → {dst}")
print(f"  ({len(fsm_not_observed)} transiciones declaradas son ruido)")

# D2: Observadas pero NO permitidas por FSM
print("\nD2) Observadas pero FSM NO modela (missing transitions):")
if observed_not_fsm:
    for src, dst in observed_not_fsm:
        c = observed_state_transitions[(src, dst)]
        print(f"  - {src:10} → {dst:10}  count={c}  (FSM lo prohíbe)")
else:
    print("  NINGUNA. El FSM es superconjunto correcto del runtime.")

# D3: Eventos UNMAPPED por FSM (no clasificados explícitamente)
print(f"\nD3) Eventos observados sin mapping FSM (queda como 'observability implícita'): {len(unmapped)}")
# Mostrar top 30 por frecuencia
unmapped_with_freq = [(e, event_freq[e]) for e in unmapped]
unmapped_with_freq.sort(key=lambda x: -x[1])
print("    Top 30 unmapped por frecuencia:")
for evt, n in unmapped_with_freq[:30]:
    print(f"    {n:4}x  {evt}")

# D4: Estados "cajón de sastre" — los que reciben más eventos sin mapping
print("\nD4) Estados que actúan como cajón de sastre (eventos UNMAPPED atribuidos):")
unmapped_set = set(unmapped)
catchall = {}
for s in fsm_states:
    n = sum(c for e, c in events_emitted_per_state[s].items() if e in unmapped_set)
    total_s = state_totals[s] or 1
    catchall[s] = (n, 100 * n / total_s)
for s, (n, pct) in sorted(catchall.items(), key=lambda kv: -kv[1][0]):
    print(f"  {s:10} {n:4} eventos unmapped ({pct:5.1f}% del estado)")

# ════════════════════════════════════════════════════════════════════
# PASO 11: LOOPS REALES — solo si aparecen en runtime
# ════════════════════════════════════════════════════════════════════
print("\n=== PASO 11: LOOPS REALES (solo runtime, no FSM) ===")
# Loops a nivel evento: (A, A) o (A, B) y (B, A) con frecuencia >= 2
self_loops_event = []
for (e1, e2), w in transitions_e2e.items():
    if e1 == e2 and w >= 2:
        self_loops_event.append((e1, w))

cycles_event = []
for (e1, e2), w in transitions_e2e.items():
    rev = transitions_e2e.get((e2, e1), 0)
    if rev > 0 and e1 < e2:
        cycles_event.append((e1, e2, w, rev))

self_loops_event.sort(key=lambda x: -x[1])
cycles_event.sort(key=lambda x: -(x[2] + x[3]))

print(f"\nSelf-loops evento (A→A, freq>=2): {len(self_loops_event)}")
for evt, w in self_loops_event[:10]:
    print(f"  {w:3}x  {evt}")

print(f"\nCiclos cortos evento (A↔B, freq>=1 en ambos sentidos): {len(cycles_event)}")
for e1, e2, w12, w21 in cycles_event[:15]:
    print(f"  ({w12}↔{w21}x)  {e1:35} <-> {e2}")

# Loops a nivel estado FSM
state_self_loops = {}
for (src, dst), c in observed_state_transitions.items():
    if src == dst:
        state_self_loops[src] = c

print(f"\nSelf-loops estado FSM observados: {len(state_self_loops)}")
for s, c in sorted(state_self_loops.items(), key=lambda kv: -kv[1]):
    declared = s in {st.value for st in LOOPS_ALLOWED}
    mark = "OK" if declared else "DRIFT"
    print(f"  {c:3}x  {s:10}  [{mark}]")

# Loops declarados por FSM pero NO observados
declared_loops = {s.value for s in LOOPS_ALLOWED}
loops_not_observed = declared_loops - set(state_self_loops.keys())
print(f"\nLoops declarados por FSM pero NUNCA observados: {len(loops_not_observed)}")
for s in sorted(loops_not_observed):
    print(f"  - {s}  (FSM dice LOOPS_ALLOWED, runtime nunca lo ejerció)")

# ════════════════════════════════════════════════════════════════════
# PASO 12: E) TOP FAILURE PATTERNS — secuencias previas a fallo
# ════════════════════════════════════════════════════════════════════
print("\n=== PASO 12: E) TOP FAILURE PATTERNS ===")

def is_failure(evt: str) -> bool:
    return any(p in evt for p in FAILURE_PATTERNS)

# Recolectar todas las secuencias previas a fallo
failure_seqs = []  # [(tuple_prev3, failure_event)]
failure_locations = []  # [(failure_event, run_name, idx, state_at)]
for run_name, seq in runs:
    current_state = "setup"
    for i, (evt, _, _) in enumerate(seq):
        # Update state if FSM maps
        spec = EVENT_TO_TRANSITION.get(evt)
        if spec:
            kind, target, _ = spec
            if kind == EventKind.TRANSITION and target is not None:
                current_state = target.value
        if is_failure(evt):
            prev3 = tuple(seq[max(0, i-3):i])
            prev3_evts = tuple(e for e, _, _ in prev3)
            failure_seqs.append((prev3_evts, evt))
            failure_locations.append((evt, run_name, i, current_state))

failure_freq = Counter(s[0] + (s[1],) for s in failure_seqs)
print(f"\nTotal fallos detectados: {len(failure_seqs)}")
print(f"Patrones únicos (3-prev + failure): {len(failure_freq)}")

print("\nTop 10 secuencias previas a fallo:")
for seq, c in failure_freq.most_common(10):
    pre, fail = seq[:-1], seq[-1]
    pre_str = " → ".join(pre) if pre else "(start)"
    print(f"  ({c}x)  {pre_str}  ──FALLO──>  {fail}")

# Estado donde más se concentran los fallos
fail_by_state = Counter(loc[3] for loc in failure_locations)
print(f"\nDistribución de fallos por estado FSM:")
for s, c in fail_by_state.most_common():
    pct = 100 * c / max(len(failure_locations), 1)
    print(f"  {s:10}  {c:3} fallos ({pct:.1f}%)")

# ════════════════════════════════════════════════════════════════════
# PASO 13: GUARDAR JSON CONSOLIDADO
# ════════════════════════════════════════════════════════════════════
print("\n=== PASO 13: Guardar JSON consolidado ===")
out = {
    "policy": "RUNS_REAL > LOGS > AST > MODELO",
    "fsm_version": "v2_5states (reference only, NOT ground truth)",
    "logs_analyzed": [lf.name for lf in log_files],
    "runs_count": len(runs),
    "total_events": total_events,
    "unique_events_observed": len(unique_events),
    "unique_events_in_fsm_mapping": len(EVENT_TO_TRANSITION),
    # A) Real state graph
    "real_state_graph": {
        "nodes_states": fsm_states,
        "edges_observed": [
            {"from": src, "to": dst, "count": c}
            for (src, dst), c in sorted(observed_state_transitions.items(),
                                         key=lambda kv: -kv[1])
        ],
        "hits_per_state": state_totals,
        "clusters_emergent_event_level": {
            anchor: {
                "size": len(evts),
                "total_hits": sum(event_freq[e] for e in evts),
                "events": sorted(evts),
            }
            for anchor, evts in sorted(multi_clusters.items(),
                                        key=lambda kv: -sum(event_freq[e] for e in kv[1]))[:20]
        },
    },
    # B) Transition matrix
    "transition_matrix": {
        "fsm_allowed_count": len(fsm_allowed),
        "observed_count": len(observed_state_transitions),
        "intersection_count": len(intersection),
        "fsm_only_count": len(fsm_not_observed),
        "observed_only_count": len(observed_not_fsm),
        "fsm_only_pairs": [{"from": s, "to": d} for s, d in fsm_not_observed],
        "observed_only_pairs": [
            {"from": s, "to": d, "count": observed_state_transitions[(s, d)]}
            for s, d in observed_not_fsm
        ],
    },
    # C) Heatmap
    "heatmap_state_hits": [
        {"state": s, "hits": state_totals[s], "pct": 100 * state_totals[s] / total}
        for s in fsm_states
    ],
    "top_events_per_state": {
        s: events_emitted_per_state[s].most_common(10) for s in fsm_states
    },
    # D) Drift crítico
    "drift_critical": {
        "fsm_allows_never_observed": [{"from": s, "to": d} for s, d in fsm_not_observed],
        "observed_but_fsm_forbids": [
            {"from": s, "to": d, "count": observed_state_transitions[(s, d)]}
            for s, d in observed_not_fsm
        ],
        "unmapped_events_count": len(unmapped),
        "unmapped_top30": unmapped_with_freq[:30],
        "catchall_states": [
            {"state": s, "unmapped_hits": n, "pct_of_state": pct}
            for s, (n, pct) in sorted(catchall.items(), key=lambda kv: -kv[1][0])
        ],
        "loops_declared_not_observed": sorted(loops_not_observed),
        "loops_observed": [{"state": s, "count": c} for s, c in state_self_loops.items()],
        "real_event_self_loops": [{"event": e, "count": c} for e, c in self_loops_event[:15]],
        "real_event_cycles_AB": [
            {"a": e1, "b": e2, "ab": w12, "ba": w21}
            for e1, e2, w12, w21 in cycles_event[:20]
        ],
    },
    # E) Top failure patterns
    "failure_patterns_top10": [
        {
            "prev_events": list(seq[:-1]),
            "failure": seq[-1],
            "count": c,
        }
        for seq, c in failure_freq.most_common(10)
    ],
    "failures_by_state": dict(fail_by_state),
    "total_failures": len(failure_seqs),
    # Outcomes
    "runs_final_state": [
        {"run": rn, "events": n, "final_state": last, "path": path}
        for rn, last, n, path in runs_outcome
    ],
}
out_path = Path("runs/empirical_validation.json")
out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str),
                     encoding="utf-8")
print(f"Saved: {out_path}  ({out_path.stat().st_size:,}B)")
