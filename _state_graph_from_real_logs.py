"""Inferencia EMPÍRICA del state graph desde LOGS REALES del bot.

Parsea `runs/test_*.log` y reconstruye:
  - frecuencia real de cada evento
  - transiciones reales observadas (peso = frecuencia)
  - secuencias por run (cada test_*.log es 1 run)
  - clusters emergentes por co-ocurrencia secuencial real

Esto es el flow REAL del bot ejecutado, no estructural del código.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_PATTERN = re.compile(r'^\[slot(\d+)\]\[(\d{2}:\d{2}:\d{2})\]\s+(\S+)(?:\s+(\{.*?\}))?$')

logs_dir = Path("runs")
log_files = sorted(logs_dir.glob("test_*.log"))
print(f"Log files encontrados: {len(log_files)}")
for lf in log_files:
    size = lf.stat().st_size
    print(f"  {lf.name:60} {size:7}B")

# === Parse all logs ===
all_runs = []   # cada elemento es lista de (event_name, ts) en orden
event_freq = Counter()
transitions = Counter()  # {(e1, e2): count}
events_by_run = []

for lf in log_files:
    sequence = []
    with open(lf, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_PATTERN.match(line.strip())
            if not m:
                continue
            slot, ts, evt, payload = m.groups()
            sequence.append((evt, ts))
            event_freq[evt] += 1
    if sequence:
        events_by_run.append((lf.name, sequence))
        # Build transitions
        for i in range(len(sequence) - 1):
            e1 = sequence[i][0]
            e2 = sequence[i+1][0]
            transitions[(e1, e2)] += 1

print(f"\nTotal runs parseados: {len(events_by_run)}")
print(f"Total eventos extraídos: {sum(event_freq.values())}")
print(f"Eventos únicos en runs reales: {len(event_freq)}")
print(f"Transiciones únicas en runs reales: {len(transitions)}")

# === TOP eventos por frecuencia real ===
print("\n=== TOP 30 eventos por frecuencia REAL en runs ejecutados ===")
for evt, n in event_freq.most_common(30):
    bar = "#" * min(int(n * 50 / event_freq.most_common(1)[0][1]), 50)
    print(f"  {n:3}x  {evt:40} {bar}")

# === Comparar con eventos del CÓDIGO ===
import ast
src = Path("_compra_parallel.py").read_text(encoding="utf-8")
tree = ast.parse(src)
events_in_code = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "log":
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            v = node.args[1].value
            if isinstance(v, str):
                events_in_code.add(v)

events_in_runs = set(event_freq.keys())
events_unused = events_in_code - events_in_runs
events_runtime_only = events_in_runs - events_in_code  # bridge calls / dynamic

print(f"\n=== EVENTOS DEFINIDOS EN CÓDIGO vs OBSERVADOS EN RUNS ===")
print(f"  En código:  {len(events_in_code)}")
print(f"  En runs:    {len(events_in_runs)}")
print(f"  En código pero NUNCA emitidos: {len(events_unused)}  ({100*len(events_unused)/len(events_in_code):.1f}%)")
print(f"  En runs pero no en código (dinámicos): {len(events_runtime_only)}")

# === TOP TRANSITIONS reales (peso real) ===
print(f"\n=== TOP 20 TRANSICIONES REALES (event_A → event_B, frecuencia) ===")
for (e1, e2), w in transitions.most_common(20):
    print(f"  ({w}x)  {e1[:35]:35} -> {e2[:35]:35}")

# === LOOPS REALES (A→B y B→A en runs) ===
print("\n=== LOOPS A↔B REALES ===")
cycles = []
for (e1, e2), w in transitions.items():
    rev = transitions.get((e2, e1), 0)
    if rev > 0 and e1 < e2:
        cycles.append((e1, e2, w, rev))
cycles.sort(key=lambda c: -(c[2] + c[3]))
print(f"Ciclos detectados: {len(cycles)}")
for e1, e2, w12, w21 in cycles[:15]:
    print(f"  ({w12}x | {w21}x)  {e1[:35]:35} <-> {e2[:35]:35}")

# === HUBS reales (eventos con mayor in/out degree REAL) ===
out_degree = Counter()
in_degree = Counter()
for (e1, e2), w in transitions.items():
    out_degree[e1] += w
    in_degree[e2] += w

print("\n=== HUBS REALES (top 20 por out-degree ponderado) ===")
for evt, deg in out_degree.most_common(20):
    print(f"  out={deg:3}  in={in_degree.get(evt,0):3}  freq={event_freq[evt]:3}  {evt}")

# === EMERGING STATES (clusters por co-ocurrencia consecutiva) ===
# Estados emergentes: grupos de eventos que aparecen en secuencia consistente.
# Heurística: dos eventos están en el mismo cluster si:
#   - transition[(A,B)] >= 2  (al menos 2 runs los tienen consecutivos)
#   - y no son terminales del flow (no FATAL_, no _err mayoría del tiempo)
#
# Simplificación: usar prefix common + función enclosing del código fuente.

# Mapear cada evento a su función enclosing
from collections import defaultdict
event_to_func = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "log":
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            v = node.args[1].value
            if isinstance(v, str):
                line = node.lineno
                # Find enclosing function
                for n2 in ast.walk(tree):
                    if isinstance(n2, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if hasattr(n2, "end_lineno") and n2.lineno <= line <= n2.end_lineno:
                            if v not in event_to_func or (n2.end_lineno - n2.lineno) < 200:
                                event_to_func[v] = n2.name

# Cluster eventos por (función_enclosing). Para events runtime-only sin código, etiqueta "<dynamic>".
func_clusters = defaultdict(list)
for evt in events_in_runs:
    func = event_to_func.get(evt, "<dynamic>")
    func_clusters[func].append(evt)

print("\n=== EMERGING STATES (clusters por función enclosing) ===")
total_emerging = 0
for func in sorted(func_clusters.keys(), key=lambda f: -sum(event_freq[e] for e in func_clusters[f])):
    evts = func_clusters[func]
    total_freq = sum(event_freq[e] for e in evts)
    total_emerging += total_freq
    print(f"  [{func:30}] {len(evts):3} eventos | {total_freq:3} ocurrencias en runs")

# === Mapear cluster a estado del modelo v2 ===
print("\n=== MAPEO CLUSTERS → MODELO v2 (5 estados) ===")
func_to_state_v2 = {
    "main":                          "MIXED (todas)",
    "login_full":                    "LOGIN",
    "login_with_account_swap":       "LOGIN",
    "fill_card":                     "CHECKOUT",
    "fill_card_adyen":               "CHECKOUT",
    "pick_morelogin_profile":        "SETUP",
    "pick_account":                  "LOGIN",
    "handle_pre_checkout_step":      "CHECKOUT",
    "handle_partners_seguro_step":   "CHECKOUT",
    "handle_separated_seats_modal":  "BROWSING",
    "select_seatmap_sections":       "BROWSING",
    "imap_otp":                      "LOGIN",
    "_acquire_pick_mutex":           "SETUP",
    "_persist_bot_block":            "EXIT",
}
for func, state in func_to_state_v2.items():
    n = sum(event_freq[e] for e in func_clusters.get(func, []))
    print(f"  {func:32} -> {state:15} ({n} runtime hits)")

# === Save graph data ===
graph_export = {
    "source": "real_runtime_logs",
    "log_files_parsed": [lf.name for lf in log_files],
    "total_runs": len(events_by_run),
    "total_events_emitted": sum(event_freq.values()),
    "unique_events_in_runs": len(event_freq),
    "events_in_code_only_unused": sorted(events_unused),
    "events_in_runs_only_dynamic": sorted(events_runtime_only),
    "top_events_by_freq": event_freq.most_common(50),
    "top_transitions_by_freq": [
        {"from": e1, "to": e2, "weight": w}
        for (e1, e2), w in transitions.most_common(50)
    ],
    "cycles": [{"a": a, "b": b, "ab": w12, "ba": w21}
               for a, b, w12, w21 in cycles],
    "hubs_by_out_degree": [(e, out_degree[e], in_degree[e], event_freq[e])
                            for e, _ in out_degree.most_common(20)],
    "emerging_state_clusters": {
        func: {
            "events": evts,
            "total_runtime_hits": sum(event_freq[e] for e in evts),
            "model_v2_state": func_to_state_v2.get(func, "<unmapped>"),
        }
        for func, evts in func_clusters.items()
    },
}
out_path = Path("runs/state_graph_REAL_RUNTIME.json")
out_path.write_text(json.dumps(graph_export, indent=2, ensure_ascii=False, default=str),
                     encoding="utf-8")
print(f"\nReal-runtime graph saved: {out_path}  ({out_path.stat().st_size:,}B)")
