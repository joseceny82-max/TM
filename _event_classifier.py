"""Event Contract Classifier — 4-tier semantic classification.

Política: NO toca FSM. NO toca arquitectura. NO toca estados.
Solo CLASIFICA cada event_name en una de 4 capas semánticas:

  STATE         → cambia estado FSM (transición)
  ACTION        → intent del bot, side-effect, NO cambia FSM
  OBSERVABILITY → telemetría/métricas/lifecycle, NO cambia FSM
  NOISE         → ruido (shot polimórfico, pre_* sin semántica, duplicados)

Genera:
  1) Tabla event_name → tipo + razón
  2) Top 10 eventos que rompen el modelo actual
  3) Métricas de impacto (misclasificación antes vs después)
  4) Datos para EVENT_CONTRACT.md
"""
from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ════════════════════════════════════════════════════════════════════
# REGLAS DE CLASIFICACIÓN — orden de prioridad descendente
# ════════════════════════════════════════════════════════════════════
# Una regla = (categoría, regex, reason)
# Primera regla que matchea gana. Reglas más específicas primero.
RULES: list[tuple[str, re.Pattern, str]] = [
    # ── EXCEPCIONES (ACTION/OBSERVABILITY antes de R2 NOISE) ──────────
    ("ACTION", re.compile(r"^pre_login_navigate$"), "Rx1: ACTION — navegación pre-login"),
    ("ACTION", re.compile(r"^pre_login_SKIPPED_go_direct_to_event$"), "Rx2: ACTION — decisión de skip"),
    ("OBSERVABILITY", re.compile(r"^pre_login_form_detected$"), "Rx3: OBSERVABILITY — detección de form"),
    ("OBSERVABILITY", re.compile(r"^pre_flight_http$"), "Rx4: OBSERVABILITY — pre-flight check"),
    ("STATE", re.compile(r"^pre_flight_FAIL_abort$"), "Rx5: STATE — pre-flight fatal abort"),
    ("OBSERVABILITY", re.compile(r"^pre_checkout_checkboxes$"), "Rx6: OBSERVABILITY — pre-checkout checkbox detector"),
    ("OBSERVABILITY", re.compile(r"^pre_flight_skipped_err$"), "Rx7: OBSERVABILITY — pre-flight skip error diagnostic"),

    # ── NOISE (filtrar siempre, no debe afectar nada) ─────────────────
    ("NOISE", re.compile(r"^shot$"), "R1: screenshot polimórfico — emitido en cualquier estado"),
    ("NOISE", re.compile(r"^shot_err$"), "R1: screenshot error — diagnóstico de la propia herramienta"),
    ("NOISE", re.compile(r"^pre_"), "R2: pre_* — preámbulo sin semántica de estado (post-excepciones)"),
    ("NOISE", re.compile(r"_skipped$"), "R3: *_skipped — telemetría de skip puro"),
    ("NOISE", re.compile(r"^blockers_(eval_)?(skipped|none|minor)$"), "R3: blockers_* — diagnóstico interno opcional"),

    # ── STATE (cambia FSM — transición real) ──────────────────────────
    ("STATE", re.compile(r"^goto$"), "R4: SETUP → BROWSING (navegación al evento)"),
    ("STATE", re.compile(r"^oauth_detected$"), "R4: BROWSING → LOGIN"),
    ("STATE", re.compile(r"^auth_detected_mid$"), "R4: * → LOGIN (re-auth mid-flow)"),
    ("STATE", re.compile(r"^checkout_detected(_at_entry)?$"), "R4: * → CHECKOUT"),
    ("STATE", re.compile(r"^stop_received$"), "R4: * → EXIT (user abort)"),
    # READY_FOR_USER: NO transiciona por sí mismo, marca outcome pendiente
    # Lo movemos a OBSERVABILITY más abajo (R12c)

    ("STATE", re.compile(r"^FATAL_"), "R5: → EXIT outcome=FATAL/BLOCKED según subtipo"),
    ("STATE", re.compile(r"^TIMEOUT_"), "R5: → EXIT outcome=FATAL"),
    ("STATE", re.compile(r"^max_account_swaps_reached$"), "R5: → EXIT outcome=FATAL"),
    ("STATE", re.compile(r"^no_more_accounts_to_try$"), "R5: → EXIT outcome=FATAL"),
    # bot_block_persisted: post-transition marker, NO transiciona por sí mismo
    # Lo movemos a OBSERVABILITY más abajo (R12d)


    # ── ACTION (intent/side-effect del bot, NO cambia FSM) ────────────
    ("ACTION", re.compile(r"_clicked(_js)?$"), "R6: *_clicked — UI click action"),
    ("ACTION", re.compile(r"^click_(?!err)"), "R6: click_* — UI click action"),
    ("ACTION", re.compile(r"_typed$"), "R6: *_typed — input typed"),
    ("ACTION", re.compile(r"_filled$"), "R6: *_filled — form filled"),
    ("ACTION", re.compile(r"_selected$"), "R6: *_selected — option selected"),
    ("ACTION", re.compile(r"^pick_(account|morelogin_profile|account_tier)"), "R7: account/profile picker action"),
    ("ACTION", re.compile(r"^account_(picked|swap)$"), "R7: account selection action"),
    ("ACTION", re.compile(r"^(auto_)?recovery(_start)?$"), "R7: recovery action"),
    ("ACTION", re.compile(r"^recovery_result$"), "R7: recovery outcome action"),
    ("ACTION", re.compile(r"^force_reset_btn_clicked$"), "R7: forced password reset action"),
    ("ACTION", re.compile(r"^continuar_(no_redirect_)?retry$"), "R8: continuar retry action"),
    ("ACTION", re.compile(r"^continuar_ok$"), "R8: continuar success action"),
    ("ACTION", re.compile(r"^continuar$"), "R8: continuar action"),
    ("ACTION", re.compile(r"^login_start$"), "R7: login intent action"),
    ("ACTION", re.compile(r"^login_full_false_treating_as_bad_creds$"), "R7: login decision action"),
    ("ACTION", re.compile(r"^email_continuar"), "R8: email continue action"),
    ("ACTION", re.compile(r"^checkbox_"), "R6: checkbox action"),
    ("ACTION", re.compile(r"^radio_"), "R6: radio button action"),
    ("ACTION", re.compile(r"^find_tickets_clicked$"), "R6: find tickets action"),
    ("ACTION", re.compile(r"^pwd_continuar_clicked$"), "R6: password continue action"),
    ("ACTION", re.compile(r"^ir_al_pago_clicked$"), "R6: pay button action"),
    ("ACTION", re.compile(r"^guardar_continuar_clicked$"), "R6: save+continue action"),
    ("ACTION", re.compile(r"^card_fill_retry$"), "R7: card fill retry action"),
    ("ACTION", re.compile(r"^card_iframe_trying$"), "R7: card iframe action"),
    ("ACTION", re.compile(r"^province_(combobox_found|typed|option_clicked)$"), "R6: province dropdown action"),
    ("ACTION", re.compile(r"^pre_login_navigate$"), "R8: pre-login navigation action (excepción R2)"),
    ("ACTION", re.compile(r"^pre_login_SKIPPED_go_direct_to_event$"), "R8: skip decision action"),

    # ── OBSERVABILITY (telemetría/lifecycle, NO cambia FSM) ───────────
    ("OBSERVABILITY", re.compile(r"^init$"), "R9: bot start marker"),
    ("OBSERVABILITY", re.compile(r"^profile_(picked|started|closed|auto_destroyed)$"), "R9: profile lifecycle"),
    ("OBSERVABILITY", re.compile(r"^morelogin_fresh_created$"), "R9: profile creation marker"),
    ("OBSERVABILITY", re.compile(r"^cdp_"), "R9: CDP connection telemetry"),
    ("OBSERVABILITY", re.compile(r"^stealth_"), "R9: stealth setup telemetry"),
    ("OBSERVABILITY", re.compile(r"^persona_(created|primed)$"), "R9: persona setup telemetry"),
    ("OBSERVABILITY", re.compile(r"^headers_set$"), "R9: HTTP headers set"),
    ("OBSERVABILITY", re.compile(r"^geo_set$"), "R9: geolocation set"),
    ("OBSERVABILITY", re.compile(r"^pre_flight_http$"), "R9: pre-flight check (excepción R2)"),
    ("OBSERVABILITY", re.compile(r"^eps_(warming_home|sid_check)$"), "R9: EPS warming telemetry"),
    ("OBSERVABILITY", re.compile(r"^loading_storage_state$"), "R9: storage state load"),
    ("OBSERVABILITY", re.compile(r"^storage_state_(loaded|TOO_OLD_skip)$"), "R9: storage state outcome"),
    ("OBSERVABILITY", re.compile(r"^warming_home_(start|done)$"), "R9: warming lifecycle"),
    ("OBSERVABILITY", re.compile(r"^warmup_start$"), "R9: warmup marker"),
    ("OBSERVABILITY", re.compile(r"^cookies_(accepted|post_hydration_)"), "R9: cookies UI lifecycle"),
    ("OBSERVABILITY", re.compile(r"^hydration_(initial|done)$"), "R9: page hydration lifecycle"),
    ("OBSERVABILITY", re.compile(r"^page_state_detect$"), "R10: page state detector"),
    ("OBSERVABILITY", re.compile(r"^stepper_offer$"), "R10: stepper detected"),
    ("OBSERVABILITY", re.compile(r"^quick_mode_via_best_available$"), "R10: mode detection"),
    ("OBSERVABILITY", re.compile(r"^qty$"), "R11: counter (incremental, not transition)"),
    ("OBSERVABILITY", re.compile(r"^post_continuar_url$"), "R10: post-step URL marker"),
    ("OBSERVABILITY", re.compile(r"^URL_CHANGED$"), "R10: CDP URL change hook"),
    ("OBSERVABILITY", re.compile(r"^TRACE_AUTH_"), "R10: auth tracer telemetry"),
    ("OBSERVABILITY", re.compile(r"^session_recovered_re_navigating_event$"), "R10: session recovery marker"),
    ("OBSERVABILITY", re.compile(r"^history_cleanup_(inside|finally)$"), "R9: cleanup lifecycle"),
    ("OBSERVABILITY", re.compile(r"^no_email_input$"), "R12: detection — diagnostic of UI state"),
    ("OBSERVABILITY", re.compile(r"^email_typed_dom_check$"), "R12: DOM verification"),
    ("OBSERVABILITY", re.compile(r"^tm_forced_password_reset_detected$"), "R12: detection — TM step-up"),
    ("OBSERVABILITY", re.compile(r"^adyen_iframes_ready$"), "R10: Adyen iframe detector"),
    ("OBSERVABILITY", re.compile(r"^fill_card_(start|data)$"), "R10: card fill lifecycle"),
    ("OBSERVABILITY", re.compile(r"^card_fields_filled$"), "R10: card fill completion marker"),
    ("OBSERVABILITY", re.compile(r"^watch_(for_checkout|initial_url)$"), "R10: watcher telemetry"),
    ("OBSERVABILITY", re.compile(r"^pwd_typed$"), "R6: password typed action"),  # could be ACTION too
    ("OBSERVABILITY", re.compile(r"^reset_method_next_failed$"), "R5: reset method outcome (terminal of recovery)"),
    ("OBSERVABILITY", re.compile(r"^recovery_failed_swap_account$"), "R7: recovery failure outcome action"),
    ("OBSERVABILITY", re.compile(r"^pre_flight_skipped_err$"), "R12: pre-flight skip diagnostic"),
    ("OBSERVABILITY", re.compile(r"^READY_FOR_USER$"), "R12c: outcome marker — NO transiciona por sí mismo"),
    ("OBSERVABILITY", re.compile(r"^bot_block_persisted$"), "R12d: post-transition outcome marker"),
    # Reglas para los UNCLASSIFIED runtime detectados
    ("OBSERVABILITY", re.compile(r"^human_journey_done$"), "R13: human_journey marker"),
    ("OBSERVABILITY", re.compile(r"^warmup_(done|home_scrolled|padding|visibility_events)$"), "R13: warmup telemetry"),
    ("OBSERVABILITY", re.compile(r"^auto_profile_destroyed_after_pre_flight$"), "R13: profile destroyed marker"),
    ("ACTION", re.compile(r"^cookies_force_close$"), "R14: cookies force-close action"),
    ("OBSERVABILITY", re.compile(r"^post_recovery_url$"), "R13: post-recovery URL marker"),
    ("OBSERVABILITY", re.compile(r"^post_recovery_human_warming_(start|done)$"), "R13: post-recovery warming lifecycle"),
    ("ACTION", re.compile(r"^post_recovery_checkbox_acepto$"), "R6: post-recovery checkbox action"),
    ("OBSERVABILITY", re.compile(r"^recovery_persisted$"), "R13: recovery persistence marker"),
    ("OBSERVABILITY", re.compile(r"_err$"), "R12: error diagnostic suffix"),

    # ── catch-all DEBUG (eventos sin matching) ────────────────────────
    ("UNCLASSIFIED", re.compile(r".*"), "no rule matched — needs review"),
]


def classify_event(name: str) -> tuple[str, str, str]:
    """Returns (kind, rule_id_or_reason, regex_pattern_str)."""
    for kind, rgx, reason in RULES:
        if rgx.search(name):
            return (kind, reason, rgx.pattern)
    return ("UNCLASSIFIED", "no rule matched", "")


# ════════════════════════════════════════════════════════════════════
# 1) Recolectar TODOS los eventos (runtime + código AST)
# ════════════════════════════════════════════════════════════════════
print("=== PASO 1: Recolectar eventos runtime + código ===")

# Runtime
LOG_PATTERN = re.compile(r'^\[slot\d+\]\[\d{2}:\d{2}:\d{2}\]\s+(\S+)')
runtime_freq = Counter()
log_files = sorted(Path("runs").glob("test_*.log"))
for lf in log_files:
    with open(lf, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_PATTERN.match(line.strip())
            if m:
                runtime_freq[m.group(1)] += 1
runtime_events = set(runtime_freq.keys())

# Código (AST)
src = Path("_compra_parallel.py").read_text(encoding="utf-8")
tree = ast.parse(src)
code_events = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "log":
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            v = node.args[1].value
            if isinstance(v, str):
                code_events.add(v)

print(f"Eventos en código (AST):  {len(code_events)}")
print(f"Eventos en runtime:        {len(runtime_events)}")
print(f"Unión total:                {len(code_events | runtime_events)}")

all_events = sorted(code_events | runtime_events)

# ════════════════════════════════════════════════════════════════════
# 2) Clasificar cada evento
# ════════════════════════════════════════════════════════════════════
print("\n=== PASO 2: Clasificar cada evento ===")
classification = {}
for evt in all_events:
    kind, reason, pattern = classify_event(evt)
    classification[evt] = {
        "kind": kind,
        "rule": reason,
        "pattern": pattern,
        "in_code": evt in code_events,
        "in_runtime": evt in runtime_events,
        "runtime_freq": runtime_freq.get(evt, 0),
    }

# Stats por categoría
by_kind = Counter(c["kind"] for c in classification.values())
print(f"\nDistribución (sobre {len(all_events)} eventos):")
for kind in ("STATE", "ACTION", "OBSERVABILITY", "NOISE", "UNCLASSIFIED"):
    n = by_kind.get(kind, 0)
    pct = 100 * n / max(len(all_events), 1)
    print(f"  {kind:15} {n:4} eventos ({pct:5.1f}%)")

# Stats SOLO sobre runtime (incidencia real)
runtime_by_kind = Counter()
runtime_hits_by_kind = Counter()
for evt, c in classification.items():
    if c["in_runtime"]:
        runtime_by_kind[c["kind"]] += 1
        runtime_hits_by_kind[c["kind"]] += c["runtime_freq"]

print(f"\nDistribución RUNTIME ({len(runtime_events)} eventos, {sum(runtime_freq.values())} hits):")
for kind in ("STATE", "ACTION", "OBSERVABILITY", "NOISE", "UNCLASSIFIED"):
    n_evts = runtime_by_kind.get(kind, 0)
    n_hits = runtime_hits_by_kind.get(kind, 0)
    pct_evts = 100 * n_evts / max(len(runtime_events), 1)
    pct_hits = 100 * n_hits / max(sum(runtime_freq.values()), 1)
    print(f"  {kind:15} {n_evts:4} ev ({pct_evts:5.1f}%)  |  {n_hits:4} hits ({pct_hits:5.1f}%)")

# ════════════════════════════════════════════════════════════════════
# 3) Comparar contra el FSM actual (EVENT_TO_TRANSITION)
# ════════════════════════════════════════════════════════════════════
print("\n=== PASO 3: Comparar contra clasificación FSM actual ===")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.observability.state_machine import EVENT_TO_TRANSITION, EventKind

# Convertir FSM mapping a categorías comparables:
#   EventKind.TRANSITION → STATE
#   EventKind.LOOP       → ACTION (no cambia FSM strictly, es self-transition opcional)
#   EventKind.OBSERVABILITY → OBSERVABILITY
fsm_kind_of = {}
for evt, (kind, _, _) in EVENT_TO_TRANSITION.items():
    if kind == EventKind.TRANSITION:
        fsm_kind_of[evt] = "STATE"
    elif kind == EventKind.LOOP:
        fsm_kind_of[evt] = "ACTION"  # loops del FSM son acciones internas
    else:
        fsm_kind_of[evt] = "OBSERVABILITY"

# Mismatches: eventos donde FSM dice X, nuestro classifier dice Y
mismatches = []
for evt, fsm_kind in fsm_kind_of.items():
    new_kind = classification.get(evt, {}).get("kind", "<missing>")
    if new_kind != fsm_kind:
        mismatches.append((evt, fsm_kind, new_kind, runtime_freq.get(evt, 0)))

mismatches.sort(key=lambda x: -x[3])
print(f"\nEventos donde FSM y classifier difieren: {len(mismatches)}")
for evt, fsm_k, new_k, freq in mismatches[:20]:
    print(f"  freq={freq:3}  {evt:45}  FSM={fsm_k:13} → NEW={new_k}")

# ════════════════════════════════════════════════════════════════════
# 4) Top 10 eventos que ROMPEN el modelo
# ════════════════════════════════════════════════════════════════════
print("\n=== PASO 4: Top 10 eventos que rompen el modelo ===")
# Criterios:
#   1. NOISE alta freq → contaminan estados FSM
#   2. UNMAPPED en FSM (no en EVENT_TO_TRANSITION) con alta freq → "observability implícita" ambigua
#   3. Mismatches activos con FSM con alta freq
breakers = []

# A) Top NOISE eventos por freq
for evt, c in classification.items():
    if c["kind"] == "NOISE" and c["runtime_freq"] >= 3:
        breakers.append((evt, c["runtime_freq"], "NOISE_CONTAMINATING_STATES",
                          f"FSM trataba como obs/state implícito; contract = NOISE filter"))

# B) Eventos UNMAPPED en FSM pero alta freq runtime (ambigüedad en FSM)
for evt, c in classification.items():
    if c["runtime_freq"] >= 9 and evt not in fsm_kind_of:
        new_k = c["kind"]
        breakers.append((evt, c["runtime_freq"], f"FSM_UNMAPPED→CONTRACT={new_k}",
                          f"FSM no lo clasifica → contract lo asigna a {new_k}"))

# C) Mismatches activos con FSM
for evt, fsm_k, new_k, freq in mismatches:
    if freq >= 3:
        breakers.append((evt, freq, f"FSM={fsm_k}→CONTRACT={new_k}",
                          classification[evt]["rule"]))

# Dedupe + sort by freq
seen = set()
breakers_unique = []
for evt, freq, reason, rule in breakers:
    if evt not in seen:
        seen.add(evt)
        breakers_unique.append((evt, freq, reason, rule))
breakers_unique.sort(key=lambda x: -x[1])

print(f"\nTop 10 rompedores (alta freq + clasificación problemática):")
for evt, freq, reason, rule in breakers_unique[:10]:
    print(f"  {freq:3}x  {evt:45}  [{reason}]")
    print(f"        {rule}")

# ════════════════════════════════════════════════════════════════════
# 5) Métrica de impacto: misclasificación antes vs después
# ════════════════════════════════════════════════════════════════════
print("\n=== PASO 5: Impacto cuantificado ===")

# ANTES (FSM actual):
#   - 41 eventos mapeados explícitamente
#   - 91 eventos UNMAPPED en runtime (83.5%)
#   - 97 hits de eventos en estado FSM "equivocado" según drift análisis previo
#   - shot mapeado como OBSERVABILITY (correcto) pero clasificado a estado por heurística (24 misclassif)

# DESPUÉS (con event contract):
#   - 4 categorías estrictas
#   - NOISE explícitamente filtrado (no entra al FSM)
#   - ACTION separado de STATE → no falsos cambios de estado
#   - OBSERVABILITY puro → no contamina

# Calculo aproximado:
runtime_total = sum(runtime_freq.values())
noise_total = sum(c["runtime_freq"] for c in classification.values() if c["kind"] == "NOISE")
state_total = sum(c["runtime_freq"] for c in classification.values() if c["kind"] == "STATE")
action_total = sum(c["runtime_freq"] for c in classification.values() if c["kind"] == "ACTION")
obs_total = sum(c["runtime_freq"] for c in classification.values() if c["kind"] == "OBSERVABILITY")
unclass_total = sum(c["runtime_freq"] for c in classification.values() if c["kind"] == "UNCLASSIFIED")

print(f"\nRuntime total events: {runtime_total}")
print(f"  STATE         hits: {state_total:4} ({100*state_total/runtime_total:5.1f}%)")
print(f"  ACTION        hits: {action_total:4} ({100*action_total/runtime_total:5.1f}%)")
print(f"  OBSERVABILITY hits: {obs_total:4} ({100*obs_total/runtime_total:5.1f}%)")
print(f"  NOISE         hits: {noise_total:4} ({100*noise_total/runtime_total:5.1f}%)")
print(f"  UNCLASSIFIED  hits: {unclass_total:4} ({100*unclass_total/runtime_total:5.1f}%)")

# Previous drift: 97 misclassifications / 628 events = 15.4%
# After classification: NOISE explicitly excluded + ACTION separated → expected misclassif drops
previous_misclassif = 97
previous_misclassif_pct = 100 * previous_misclassif / runtime_total

# How many of those 97 were `shot` polimorfico, pre_*, picker?
# From previous analysis: shot=32, pick_account_tier=16, pre_*=10, account_swap=4, no_email=4, etc.
# After contract:
#   - shot → NOISE (32 misclassif eliminated)
#   - pre_* (non-flight) → NOISE (~10 misclassif eliminated)
#   - pick_account_tier, account_*, recovery → ACTION (no longer "wrong state")
# Conservative estimate:
recoverable = 32 + 16 + 12 + 10 + 4  # shot + picker + account_picked + pre_* + swap
new_misclassif = max(0, previous_misclassif - recoverable)
new_misclassif_pct = 100 * new_misclassif / runtime_total

print(f"\nMisclasificación ANTES (drift_detection.json): {previous_misclassif} hits ({previous_misclassif_pct:.1f}%)")
print(f"Misclasificación DESPUÉS (event contract):    ~{new_misclassif} hits ({new_misclassif_pct:.1f}%)")
print(f"Reducción estimada:                            ~{recoverable} hits eliminados")
print(f"% reducción: ~{100*recoverable/max(previous_misclassif,1):.1f}% del drift previo")

# ════════════════════════════════════════════════════════════════════
# 6) Generar tabla completa para Markdown
# ════════════════════════════════════════════════════════════════════
print("\n=== PASO 6: Tabla final ===")
table_rows = []
for evt in all_events:
    c = classification[evt]
    table_rows.append({
        "event": evt,
        "kind": c["kind"],
        "rule": c["rule"],
        "runtime_freq": c["runtime_freq"],
        "in_code": c["in_code"],
        "fsm_currently": fsm_kind_of.get(evt, "—"),
    })

# Sort: kind orden lógico, luego freq desc
kind_order = {"STATE": 0, "ACTION": 1, "OBSERVABILITY": 2, "NOISE": 3, "UNCLASSIFIED": 4}
table_rows.sort(key=lambda r: (kind_order[r["kind"]], -r["runtime_freq"], r["event"]))

# ════════════════════════════════════════════════════════════════════
# 7) Guardar JSON consolidado
# ════════════════════════════════════════════════════════════════════
out = {
    "policy": "NO toca FSM, NO toca arquitectura. Solo CLASIFICA eventos en 4 capas.",
    "categories": {
        "STATE": "cambia estado FSM (transición real)",
        "ACTION": "intent/side-effect del bot, NO cambia FSM",
        "OBSERVABILITY": "telemetría/lifecycle, NO cambia FSM",
        "NOISE": "ruido (shot polimórfico, pre_* sin semántica, duplicados)",
    },
    "total_events_classified": len(all_events),
    "events_in_code": len(code_events),
    "events_in_runtime": len(runtime_events),
    "by_kind_global": dict(by_kind),
    "by_kind_runtime_events": dict(runtime_by_kind),
    "by_kind_runtime_hits": dict(runtime_hits_by_kind),
    "mismatches_with_fsm": [
        {"event": e, "fsm_says": f, "contract_says": n, "freq": fr}
        for e, f, n, fr in mismatches
    ],
    "top_10_breakers": [
        {"event": e, "freq": f, "reason": r, "rule": ru}
        for e, f, r, ru in breakers_unique[:10]
    ],
    "impact": {
        "runtime_total_events": runtime_total,
        "previous_misclassif_count": previous_misclassif,
        "previous_misclassif_pct": round(previous_misclassif_pct, 2),
        "new_misclassif_estimated": new_misclassif,
        "new_misclassif_pct": round(new_misclassif_pct, 2),
        "reduction_count": recoverable,
        "reduction_pct_of_drift": round(100*recoverable/max(previous_misclassif,1), 1),
    },
    "table": table_rows,
    "rules": [
        {"id": f"R{i+1}", "kind": k, "pattern": rgx.pattern, "reason": reason}
        for i, (k, rgx, reason) in enumerate(RULES)
        if k != "UNCLASSIFIED"
    ],
}
out_path = Path("runs/event_contract.json")
out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str),
                     encoding="utf-8")
print(f"\nSaved: {out_path}  ({out_path.stat().st_size:,}B)")
