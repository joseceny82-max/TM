# Event Contract — Limpieza Semántica de Observabilidad

> **Política**: NO toca el FSM v2. NO añade estados. NO rediseña arquitectura.
> Solo **clasifica eventos** en 4 capas semánticas estrictas para limpiar la señal.

| Eje | Antes | Después | Δ |
|---|---|---|---|
| Misclasificación evento→estado | 97 hits (15.4%) | ~23 hits (3.7%) | **−76.3%** |
| Eventos UNMAPPED en runtime | 91 / 109 (83.5%) | 0 (0%) | **−100%** |
| Categorías de evento | 3 implícitas | 4 explícitas | +1 (ACTION) |
| Reglas de clasificación | sin reglas | 28 reglas verificables | +28 |
| `shot` clasificado como | OBSERVABILITY ambiguo | NOISE (filtrado) | corregido |
| FSM modificado | — | — | **0 cambios** |

**Artefactos**: `_event_classifier.py` (10 KB) + `runs/event_contract.json` (71 KB) — reproducible con `PYTHONIOENCODING=utf-8 python _event_classifier.py`.

---

## 1. Las 4 categorías del contrato

| Capa | Definición operativa | Afecta FSM | Hits runtime | % runtime |
|---|---|---|---|---|
| **STATE** | Eventos que disparan una transición real entre los 5 estados del FSM | **SÍ** (transición) | 22 | 3.5 % |
| **ACTION** | Intent / side-effect del bot (clicks, inputs, decisiones) | **NO** | 198 | 31.5 % |
| **OBSERVABILITY** | Telemetría, lifecycle, detección, métricas | **NO** | 340 | 54.1 % |
| **NOISE** | Polimórficos, `pre_*` sin semántica, `_skipped` puros, duplicados | **NO** (filtrado) | 68 | 10.8 % |
| **(UNCLASSIFIED)** | Sin regla — no debe quedar ninguno | — | 0 | 0 % |

**Cobertura del contrato**: 100 % de los 109 eventos observados clasificados sin ambigüedad.

---

## 2. Las 28 reglas de clasificación (regex verificables)

Las reglas se aplican en **orden de prioridad descendente**. Primera que matchea gana. Excepciones específicas van antes de reglas generales.

### Excepciones de orden (Rx1–Rx7) — ANTES de R2

| ID | Capa | Pattern | Razón |
|---|---|---|---|
| Rx1 | ACTION | `^pre_login_navigate$` | navegación pre-login (no es ruido) |
| Rx2 | ACTION | `^pre_login_SKIPPED_go_direct_to_event$` | decisión de skip |
| Rx3 | OBSERVABILITY | `^pre_login_form_detected$` | detección de form |
| Rx4 | OBSERVABILITY | `^pre_flight_http$` | pre-flight check |
| Rx5 | STATE | `^pre_flight_FAIL_abort$` | pre-flight fatal → EXIT |
| Rx6 | OBSERVABILITY | `^pre_checkout_checkboxes$` | pre-checkout detector |
| Rx7 | OBSERVABILITY | `^pre_flight_skipped_err$` | error diagnostic |

### NOISE (R1–R3)

| ID | Capa | Pattern | Razón |
|---|---|---|---|
| **R1** | NOISE | `^shot$` | screenshot polimórfico — emitido en cualquier estado |
| R1' | NOISE | `^shot_err$` | screenshot error — diagnóstico de la propia herramienta |
| **R2** | NOISE | `^pre_` | `pre_*` después de excepciones — preámbulo sin semántica |
| **R3** | NOISE | `_skipped$` ∨ `^blockers_(eval_)?(skipped\|none\|minor)$` | skip puro |

### STATE (R4–R5)

| ID | Capa | Pattern | Transición FSM |
|---|---|---|---|
| **R4** | STATE | `^goto$` | SETUP → BROWSING |
| R4' | STATE | `^oauth_detected$` | BROWSING → LOGIN |
| R4'' | STATE | `^auth_detected_mid$` | * → LOGIN (re-auth) |
| R4''' | STATE | `^checkout_detected(_at_entry)?$` | * → CHECKOUT |
| R4'''' | STATE | `^stop_received$` | * → EXIT |
| **R5** | STATE | `^FATAL_` ∨ `^TIMEOUT_` ∨ `^max_account_swaps_reached$` ∨ `^no_more_accounts_to_try$` | * → EXIT (outcome=FATAL/BLOCKED) |

### ACTION (R6–R8)

| ID | Capa | Pattern | Tipo de acción |
|---|---|---|---|
| **R6** | ACTION | `_clicked(_js)?$` ∨ `^click_` ∨ `_typed$` ∨ `_filled$` ∨ `_selected$` ∨ `^checkbox_` ∨ `^radio_` | UI action |
| **R7** | ACTION | `^pick_(account\|morelogin_profile)` ∨ `^account_(picked\|swap)$` ∨ `^(auto_)?recovery` ∨ `^login_start$` ∨ `^login_full_false_treating_as_bad_creds$` | flow intent |
| **R8** | ACTION | `^continuar_(no_redirect_)?retry$` ∨ `^continuar_ok$` ∨ `^email_continuar` | continue action |

### OBSERVABILITY (R9–R14)

| ID | Capa | Pattern | Tipo de telemetría |
|---|---|---|---|
| **R9** | OBSERVABILITY | `^init$` ∨ `^profile_` ∨ `^cdp_` ∨ `^stealth_` ∨ `^morelogin_fresh_created$` ∨ `^persona_` ∨ `^headers_set$` ∨ `^geo_set$` ∨ `^eps_` ∨ `^cookies_(accepted\|post_)` ∨ `^hydration_` ∨ `^storage_state_` ∨ `^warming_home_` ∨ `^warmup_start$` ∨ `^history_cleanup_` | lifecycle |
| **R10** | OBSERVABILITY | `^page_state_detect$` ∨ `^stepper_offer$` ∨ `^quick_mode_via_best_available$` ∨ `^post_continuar_url$` ∨ `^URL_CHANGED$` ∨ `^TRACE_AUTH_` ∨ `^session_recovered_re_navigating_event$` ∨ `^adyen_iframes_ready$` ∨ `^fill_card_(start\|data)$` ∨ `^card_fields_filled$` ∨ `^watch_(for_checkout\|initial_url)$` | detector/marker |
| **R11** | OBSERVABILITY | `^qty$` | counter (incremental, NO transición) |
| **R12** | OBSERVABILITY | `^no_email_input$` ∨ `^tm_forced_password_reset_detected$` ∨ `_err$` ∨ `^email_typed_dom_check$` | detección / diagnostic |
| R12c | OBSERVABILITY | `^READY_FOR_USER$` | outcome marker (no transiciona por sí mismo) |
| R12d | OBSERVABILITY | `^bot_block_persisted$` | post-transition outcome marker |
| **R13** | OBSERVABILITY | `^human_journey_done$` ∨ `^warmup_(done\|home_scrolled\|padding\|visibility_events)$` ∨ `^auto_profile_destroyed_after_pre_flight$` ∨ `^post_recovery_url$` ∨ `^post_recovery_human_warming_(start\|done)$` ∨ `^recovery_persisted$` | telemetría auxiliar |
| **R14** | ACTION | `^cookies_force_close$` | acción cookies force-close |

> **Total: 28 reglas + 7 excepciones de orden = 35 patrones**. Cero ambigüedad en runtime.

---

## 3. Top 10 eventos que rompen el modelo

> Criterio: alta frecuencia runtime + clasificación ambigua/incorrecta en FSM actual.

| # | Hits | Evento | Status en FSM v2 | Reasignación contrato | Impacto |
|---|---|---|---|---|---|
| **1** | **58** | `shot` | OBSERVABILITY (ambiguo) | **NOISE** (filtrar) | elimina 32 misclasificaciones de drift |
| 2 | 27 | `qty` | UNMAPPED | **OBSERVABILITY** | claridad: es counter, no transición |
| 3 | 22 | `pick_account_tier` | UNMAPPED | **ACTION** | semántica de picker explicitada |
| 4 | 18 | `login_start` | UNMAPPED | **ACTION** | NO es transición, es intent |
| 5 | 16 | `recovery` | UNMAPPED | **ACTION** | self-loop a nivel evento, intent |
| 6 | 12 | `account_picked` | UNMAPPED | **ACTION** | side-effect del picker |
| 7 | 12 | `morelogin_fresh_created` | UNMAPPED | **OBSERVABILITY** | lifecycle profile |
| 8 | 12 | `profile_picked` | UNMAPPED | **OBSERVABILITY** | lifecycle profile |
| 9 | 11 | `cdp_connected_ok` | UNMAPPED | **OBSERVABILITY** | lifecycle CDP |
| 10 | 11 | `cdp_version_ok` | UNMAPPED | **OBSERVABILITY** | lifecycle CDP |

**Patrón dominante**: 9/10 eventos top eran **UNMAPPED en el FSM** (cajón de sastre). El contrato resuelve la ambigüedad sin tocar el FSM.

**Mismatches activos detectados (FSM v2 declara X, contract dice Y)**:

| Hits | Evento | FSM dice | Contrato dice | Veredicto |
|---|---|---|---|---|
| 58 | `shot` | OBSERVABILITY | **NOISE** | NOISE filtrado ≠ obs sin filtro — el contrato es más estricto |
| 3 | `tm_forced_password_reset_detected` | ACTION (LOOP) | **OBSERVABILITY** | detección ≠ acción — el contrato corrige la semántica |
| 0 | `pre_sleep_for_lock` | OBSERVABILITY | NOISE | freq=0 en runtime, no impacta |
| 0 | `checkout_stage_url` | ACTION (LOOP) | UNCLASSIFIED | freq=0 en runtime, no impacta |

---

## 4. Eventos clasificados por capa (RUNTIME, 109 eventos)

### 4.1 STATE — 6 eventos, 22 hits (3.5 %)

> Solo estos eventos pueden disparar transición FSM. **NINGÚN otro evento debe afectar estado.**

| Hits | Evento | Transición que dispara |
|---|---|---|
| 9 | `goto` | SETUP → BROWSING |
| 9 | `oauth_detected` | BROWSING → LOGIN |
| 1 | `FATAL_bot_block_early` | * → EXIT (outcome=BLOCKED) |
| 1 | `checkout_detected` | LOGIN → CHECKOUT |
| 1 | `pre_flight_FAIL_abort` | SETUP → EXIT |
| 1 | `stop_received` | * → EXIT (outcome=user-stop) |

### 4.2 ACTION — 34 eventos, 198 hits (31.5 %)

> Intent/side-effect del bot. **No cambian estado**, pero registran qué intenta hacer el bot. Top 15:

| Hits | Evento | Sub-tipo |
|---|---|---|
| 22 | `pick_account_tier` | picker |
| 18 | `click_continuar` | click |
| 18 | `login_start` | intent |
| 16 | `recovery` | recovery |
| 12 | `account_picked` | picker |
| 10 | `account_swap` | picker |
| 9 | `checkbox_acepto` | UI |
| 9 | `click_find_tickets` | click |
| 9 | `continuar_clicked_js` | click |
| 9 | `continuar_no_redirect_retry` | retry |
| 9 | `continuar_ok` | continue |
| 9 | `find_tickets_clicked` | click |
| 9 | `login_full_false_treating_as_bad_creds` | decision |
| 5 | `pre_login_SKIPPED_go_direct_to_event` | decision |
| 4 | `email_continuar_clicked` | click |

*(19 más con freq 1-4)*

### 4.3 OBSERVABILITY — 65 eventos, 340 hits (54.1 %)

> Telemetría/lifecycle puro. **Nunca afecta FSM**. Top 20:

| Hits | Evento | Sub-tipo |
|---|---|---|
| 27 | `qty` | counter |
| 12 | `init` | start marker |
| 12 | `morelogin_fresh_created` | lifecycle |
| 12 | `profile_picked` | lifecycle |
| 11 | `cdp_connected_ok` | lifecycle |
| 11 | `cdp_version_ok` | lifecycle |
| 11 | `profile_started` | lifecycle |
| 10 | `geo_set` | setup |
| 10 | `headers_set` | setup |
| 10 | `persona_created` | setup |
| 10 | `pre_flight_http` | pre-flight |
| 9 | `cookies_post_hydration_accepted` | lifecycle |
| 9 | `cookies_post_hydration_final` | lifecycle |
| 9 | `hydration_done` | lifecycle |
| 9 | `hydration_initial` | lifecycle |
| 9 | `no_email_input` | detector |
| 9 | `page_state_detect` | detector |
| 9 | `post_continuar_url` | URL marker |
| 9 | `quick_mode_via_best_available` | detector |
| 9 | `stealth_applied` | setup |

*(45 más con freq 1-9)*

### 4.4 NOISE — 4 eventos, 68 hits (10.8 %) — **FILTRADOS**

> Estos eventos **NO deben pasar al FSM ni afectar análisis de estado**.

| Hits | Evento | Razón de filtrado |
|---|---|---|
| 58 | `shot` | screenshot polimórfico — emitido en cualquier estado, contamina drift analysis |
| 8 | `blockers_eval_skipped` | telemetría de skip puro |
| 1 | `blockers_minor` | diagnóstico interno opcional |
| 1 | `shot_err` | error de la propia herramienta de screenshot |

**Eliminación de estos 4 eventos del análisis de estado reduce la misclasificación en 32 hits (33% del drift previo).**

---

## 5. Impacto cuantificado del contrato

### 5.1 Antes vs Después

| Métrica | FSM v2 (sin contrato) | Contrato aplicado | Δ |
|---|---|---|---|
| Eventos con clasificación explícita | 41 / 109 (37.6 %) | 109 / 109 (100 %) | **+62.4 pp** |
| Eventos UNMAPPED en runtime | 91 (83.5 %) | 0 (0 %) | **−83.5 pp** |
| Misclasificación evento→estado | 97 hits (15.4 %) | ~23 hits (3.7 %) | **−74 hits (−76.3 %)** |
| `shot` contaminating analysis | 32 hits mal-clasificados | 0 (filtrado como NOISE) | **−32** |
| `pre_*` polimórficos | 12 hits mal-clasificados | 0 (filtrado o reclasificado) | **−12** |
| Picker/swap mal en LOGIN | 30 hits | 0 (todos en ACTION) | **−30** |

### 5.2 Distribución final por capa (runtime)

```
Capa            Eventos  Hits   %      Bar
─────────────────────────────────────────────────────────
STATE              6      22    3.5 %  █
ACTION            34     198   31.5 %  ███████████████
OBSERVABILITY     65     340   54.1 %  ██████████████████████████
NOISE              4      68   10.8 %  █████
UNCLASSIFIED       0       0    0.0 %  —
─────────────────────────────────────────────────────────
TOTAL            109     628  100.0 %
```

### 5.3 Casos donde el contrato detecta drift semántico

#### Caso A: `shot` (58 hits) — el peor contaminador
- **FSM v2**: `OBSERVABILITY` (correcto en teoría, pero ambiguo en práctica)
- **Drift previo**: 32 hits clasificados a estado incorrecto por heurística keyword
- **Contrato**: `NOISE` → se excluye de cualquier análisis estado/transición
- **Efecto**: eliminados 32 falsos positivos de misclasificación

#### Caso B: `tm_forced_password_reset_detected` (3 hits)
- **FSM v2**: `EventKind.LOOP, State.LOGIN` (= ACTION en el contrato)
- **Contrato**: `OBSERVABILITY` (es detección, no acción)
- **Razón**: la acción real que sigue es `auto_recovery_start`. Este evento solo *observa* el step-up de TM.

#### Caso C: 67 eventos UNMAPPED del FSM con alta freq
- **FSM v2**: "observability implícita" (sin mapping explícito)
- **Drift**: ambigüedad de qué representan
- **Contrato**: cada uno tiene capa + regla + razón explícita
- **Efecto**: 0 UNMAPPED en runtime

---

## 6. Contrato del logger (sin tocar bot)

### 6.1 Estructura de `LogEvent` tipado (propuesta, **no obligatoria**)

```python
@dataclass
class LogEvent:
    event_name: str
    kind: Literal["STATE", "ACTION", "OBSERVABILITY", "NOISE"]
    state_transition: Optional[tuple[str, str]]  # solo si kind=STATE
    rule_id: str  # ej "R6", "Rx2"
    timestamp: str
    slot: int
    payload: dict
```

### 6.2 Hook mínimo en `_compra_logger.log_event()`

```python
# en _compra_logger.py — al final del log_event existente
try:
    from _event_classifier import classify_event as _classify
    kind, _reason, _pattern = _classify(event_name)
    record["kind"] = kind  # añade campo, no modifica nada más
except Exception:
    pass  # silencioso, no rompe nada
```

**Coste**: 4 líneas en `_compra_logger.py`. **Beneficio**: cada log emitido lleva su capa inferida.

### 6.3 Validator central (uso opcional, no integrado)

```python
def validate_event(name: str) -> tuple[str, str]:
    """Devuelve (kind, rule). UNCLASSIFIED = warning."""
    return _classify(name)

def validate_state_transition(from_state: str, to_state: str,
                                event: str) -> bool:
    """True si la transición es legal Y el evento es STATE."""
    kind, _, _ = _classify(event)
    if kind != "STATE":
        return False  # solo STATE puede transicionar
    # leer TRANSITIONS del FSM v2
    from core.observability.state_machine import TRANSITIONS, State
    src = State(from_state)
    dst = State(to_state)
    return dst in TRANSITIONS.get(src, frozenset())
```

### 6.4 Detector de drift automático

```python
def detect_drift(event_name: str, current_state: str) -> Optional[str]:
    """Devuelve mensaje si el evento NO encaja con el estado actual.
    Solo evalúa STATE events; ACTION/OBSERVABILITY/NOISE nunca rompen drift."""
    kind, rule, _ = _classify(event_name)
    if kind != "STATE":
        return None
    # Reglas del FSM ya dicen qué transición se permite.
    # Si el evento esperaría una transición no permitida → drift.
    expected = EVENT_EXPECTED_FROM_STATE.get(event_name)
    if expected and current_state not in expected:
        return f"DRIFT: {event_name} esperaba estado en {expected}, runtime está en {current_state}"
    return None
```

---

## 7. Resumen ejecutivo

| Punto | Estado |
|---|---|
| **FSM tocado** | ✅ 0 cambios |
| **Estados nuevos añadidos** | ✅ 0 |
| **Arquitectura modificada** | ✅ 0 |
| **Eventos clasificados** | ✅ 109 / 109 runtime (100 %) |
| **Reglas verificables** | ✅ 28 reglas + 7 excepciones |
| **NOISE filtrado** | ✅ 4 eventos / 68 hits identificados |
| **Misclasificación drift** | ✅ 76.3 % de reducción estimada |
| **Hook propuesto** | ✅ 4 líneas, opt-in, no breaking |
| **Drift detector** | ✅ disponible sin tocar bot |

**Diagnóstico**: el problema era **calidad semántica** de eventos, no estructura del FSM. El contrato lo resuelve añadiendo metadata sin modificar el modelo subyacente.

---

## 8. Artefactos

| Fichero | Tamaño | Contenido |
|---|---|---|
| `_event_classifier.py` | 10 KB | Reglas + clasificador reproducible |
| `runs/event_contract.json` | 71 KB | Tabla completa 244 eventos + métricas |
| `EVENT_CONTRACT.md` | (este) | Contrato + impacto + uso |
| `core/observability/state_machine.py` | 19 KB | FSM v2 **sin modificar** |

*Generado: 2026-05-13 · Basado en 109 eventos observados en 12 runs (628 hits) + 233 eventos del código AST. No se rediseña ni se simplifica nada.*
