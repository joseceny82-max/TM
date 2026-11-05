"""Inferencia EMPÍRICA del state graph desde los 231 eventos reales.

NO usa diseño previo como verdad. Extrae:
  - cada log() call con su línea + función enclosing
  - secuencia temporal por función (proxy del flow real)
  - co-ocurrencias consecutivas → clusters emergentes
  - transiciones entre clusters → grafo direccional ponderado
"""
import ast
import re
from collections import Counter, defaultdict
from pathlib import Path

SRC = Path("_compra_parallel.py")
src_text = SRC.read_text(encoding="utf-8")
tree = ast.parse(src_text)

# Mapear cada `log(slot, "X")` a su función enclosing
events_with_context = []  # [(line, event_name, func_name)]


def find_function_for_line(tree, target_line):
    """Devuelve el nombre de la función top-level que contiene `target_line`."""
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = node.end_lineno if hasattr(node, 'end_lineno') else start + 1000
            if start <= target_line <= end:
                candidates.append((node.name, start, end))
    if not candidates:
        return "<module>"
    # Devolver la más específica (la que tiene rango más pequeño)
    candidates.sort(key=lambda c: c[2] - c[1])
    return candidates[0][0]


# Encontrar todos los `log(slot, "X", ...)` calls
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "log":
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            event_name = node.args[1].value
            if isinstance(event_name, str):
                line = node.lineno
                func = find_function_for_line(tree, line)
                events_with_context.append((line, event_name, func))

print(f"Total log() calls extraídos: {len(events_with_context)}")
unique_events = set(e[1] for e in events_with_context)
print(f"Event names únicos: {len(unique_events)}")

# Agrupar por función + ordenar por línea (secuencia temporal en cada función)
by_function = defaultdict(list)
for line, evt, func in events_with_context:
    by_function[func].append((line, evt))

for func in by_function:
    by_function[func].sort()

print(f"\nFunciones con log() calls: {len(by_function)}")
print("Top 10 funciones por número de log() calls:")
for func, items in sorted(by_function.items(), key=lambda x: -len(x[1]))[:10]:
    print(f"  {func:35} {len(items)} log calls")

# === BUILD TRANSITION GRAPH ===
# Edge: (event_A → event_B) si aparecen consecutivos en la misma función.
# Weight: frecuencia.
transition_weights = Counter()
for func, items in by_function.items():
    for i in range(len(items) - 1):
        e1 = items[i][1]
        e2 = items[i+1][1]
        if e1 != e2:  # no auto-loops triviales por duplicación de log call
            transition_weights[(e1, e2)] += 1

print(f"\nTransiciones únicas detectadas: {len(transition_weights)}")
print(f"Total weight (suma): {sum(transition_weights.values())}")

# === CLUSTERS EMERGENTES ===
# Heurística: agrupar eventos que SIEMPRE aparecen consecutivos (en la misma función)
# como cluster = sub-flow del bot.
# Simplificación: un cluster = una función con muchos eventos.
# Más útil: clusters semánticos por prefijo común + función enclosing.

# Build event → list of (func, position) para mostrar co-ocurrencia
event_locations = defaultdict(list)
for func, items in by_function.items():
    for i, (line, evt) in enumerate(items):
        event_locations[evt].append((func, i, line))

# Frecuencia por evento
event_freq = Counter(evt for _, evt, _ in events_with_context)

# === EMERGING STATES por función + prefix ===
# Idea: los estados emergentes coinciden con las funciones grandes del bot.
# Para clusters más finos, usar prefix del event_name dentro de una función.

cluster_by_func_and_prefix = defaultdict(set)
for func, items in by_function.items():
    for line, evt in items:
        # Prefix = primera palabra antes del primer "_"
        prefix = evt.split("_")[0]
        cluster_by_func_and_prefix[(func, prefix)].add(evt)

# Mostrar clusters más grandes
print("\n=== EMERGING CLUSTERS (función + prefix) ===")
top_clusters = sorted(cluster_by_func_and_prefix.items(), key=lambda x: -len(x[1]))[:30]
for (func, prefix), evts in top_clusters:
    print(f"  [{func}/{prefix}] {len(evts)} eventos")

# === TOP 20 TRANSICIONES por peso ===
print("\n=== TOP 20 TRANSITIONS (event_A → event_B, weight) ===")
for (e1, e2), w in transition_weights.most_common(20):
    print(f"  ({w}x)  {e1[:35]:35} → {e2[:35]:35}")

# === LOOPS reales (cycles cortos) ===
# Heurística: A→B→A o A→B→C→A en la misma función
print("\n=== CICLOS CORTOS REALES ===")
cycles_2 = []  # A → B y B → A
for (e1, e2), w in transition_weights.items():
    if transition_weights.get((e2, e1), 0) > 0 and e1 < e2:  # evitar dups
        cycles_2.append((e1, e2, w, transition_weights[(e2, e1)]))
print(f"Ciclos A↔B detectados: {len(cycles_2)}")
for e1, e2, w12, w21 in sorted(cycles_2, key=lambda c: -(c[2]+c[3]))[:15]:
    print(f"  {e1[:30]:30} <-> {e2[:30]:30}  ({w12}x / {w21}x)")

# === HOTSPOTS: nodos con mayor número de outgoing transitions ===
out_degree = Counter()
in_degree = Counter()
for (e1, e2), w in transition_weights.items():
    out_degree[e1] += 1
    in_degree[e2] += 1

print("\n=== HUB NODES (out-degree top 15) ===")
for evt, deg in out_degree.most_common(15):
    print(f"  out={deg:3}  in={in_degree.get(evt,0):3}  {evt}")

# === ERROR sources: nodos que llevan a FATAL_* o _err ===
print("\n=== ERROR PATHWAYS (eventos que transicionan a FATAL_/_err) ===")
error_sources = Counter()
for (e1, e2), w in transition_weights.items():
    if e2.startswith("FATAL_") or "_err" in e2.lower() or "_failed" in e2.lower() or "bot_block" in e2.lower():
        error_sources[e1] += w
for src, w in error_sources.most_common(10):
    print(f"  ({w}x leads to error)  {src}")

# === Save graph data ===
import json
graph_export = {
    "total_log_calls": len(events_with_context),
    "unique_events": len(unique_events),
    "functions_with_logs": len(by_function),
    "functions_top": [(func, len(items)) for func, items in
                       sorted(by_function.items(), key=lambda x: -len(x[1]))[:15]],
    "transitions": [
        {"from": e1, "to": e2, "weight": w}
        for (e1, e2), w in transition_weights.most_common()
    ],
    "event_frequency": dict(event_freq.most_common()),
    "out_degree": dict(out_degree),
    "in_degree": dict(in_degree),
    "cycles_2": [{"a": e1, "b": e2, "ab": w12, "ba": w21}
                  for e1, e2, w12, w21 in cycles_2],
    "error_sources": dict(error_sources),
}
import os
os.makedirs("runs", exist_ok=True)
with open("runs/state_graph_inference.json", "w", encoding="utf-8") as f:
    json.dump(graph_export, f, indent=2, ensure_ascii=False)
print(f"\nGraph data saved: runs/state_graph_inference.json")
print(f"  transitions: {len(transition_weights)}")
print(f"  total weight: {sum(transition_weights.values())}")
