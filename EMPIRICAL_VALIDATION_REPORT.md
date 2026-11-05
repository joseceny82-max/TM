# Validación Empírica — FSM v2 vs Runtime Real

> **Política**: `RUNS_REALES > LOGS > AST > MODELO`.
> **Restricción**: NO rediseñar, NO simplificar, NO inventar estados, NO optimizar. Solo MEDIR.
> **Pregunta**: ¿el FSM v2 (5 estados) representa el comportamiento real del bot ejecutado?

| Métrica | Valor |
|---|---|
| Runs analizados | **12** |
| Eventos totales emitidos | **628** |
| Eventos únicos observados en runtime | **109** |
| Eventos mapeados explícitamente por FSM | **41 declarados** / **18 realmente vistos** |
| Eventos UNMAPPED en runtime | **91 / 109 = 83.5%** |
| Transiciones evento→evento únicas | **147** |
| Transiciones de estado observadas | **7** (de 13 declaradas en FSM) |
| Transiciones del FSM nunca observadas | **6** |
| Transiciones observadas que el FSM prohíbe | **0** |
| Loops FSM declarados nunca ejercidos | **1** (`checkout→checkout`) |
| Fallos totales detectados | **17** |
| Patrones únicos pre-fallo (3-evt) | **10** |
| Runs que llegan a CHECKOUT | **1 / 12 = 8.3 %** |
| Runs que llegan a EXIT controlado | **4 / 12 = 33.3 %** |
| Runs que quedan colgados en SETUP | **3 / 12 = 25.0 %** |

**Fuente reproducible**: `_empirical_validation.py` + `runs/empirical_validation.json` (14,356 B).

---

## A) REAL STATE GRAPH (empírico)

### A.1 Trayectorias finales reales por run (12 runs)

```
run                                         events  path real
─────────────────────────────────────────────────────────────────────────────
test_AB_no_stealth_no_warmup_20260513.log     51    setup → browsing → login
test_compra_20260513.log                       72    setup → browsing → login
test_CONSOLIDATION_20260513.log                57    setup → browsing → login → exit
test_E2E_post_fix_20260513.log                108    setup → browsing → login → checkout → exit
test_fix_full_20260513.log                     51    setup → browsing → login
test_fix_P0_20260513.log                        8    setup
test_fix_P0_v2_20260513.log                    29    setup
test_fix_P0_v3_eps_warm_20260513.log           32    setup
test_stealth_20260513.log                      57    setup → browsing → login
test_stealth_v2_20260513.log                   63    setup → browsing → login
test_TRACE_AUTH_20260513.log                   50    setup → browsing → login
test_TRACE_AUTH_POST_FIX_20260513.log          50    setup → browsing → login
```

**Lectura empírica**:
- 3/12 runs (25%) mueren en SETUP (events ≤ 32, fallo previo a `goto`)
- 7/12 runs (58%) quedan en LOGIN (no avanzan)
- 1/12 run (8%) llega a CHECKOUT
- 4/12 runs (33%) cierran con EXIT formal (1 desde LOGIN, 1 desde CHECKOUT, 2 implícitos)

### A.2 Grafo de transiciones REAL (pesos = runs observados)

```
                  9
   ┌─────────┐──────────────┐ 36 (self-loop)
   │  SETUP  │              ▼
   │  3 runs │       ┌────────────┐
   │  mueren │       │  BROWSING  │──┐  14 (self-loop)
   │  aquí   │       │            │  │
   └─────────┘       └────────────┘  ▼
                       │ 9   ┌────────────┐ 1   ┌──────────┐
                       └────>│   LOGIN    │────>│ CHECKOUT │
                             │            │     │          │
                             └────────────┘     └──────────┘
                                    │ 1                │ 1
                                    └────────┬─────────┘
                                             ▼
                                       ┌──────────┐
                                       │   EXIT   │
                                       └──────────┘
```

> Cinco aristas direccionales reales. Dos self-loops fuertes (`browsing×36`, `login×14`). El resto de transiciones declaradas por el FSM no aparecen.

### A.3 Clusters emergentes por co-ocurrencia (sin FSM como prior)

Algoritmo: Jaccard ≥ 0.6 sobre vecindarios (predecesores ∪ sucesores) + ≥3 vecinos comunes.

| Cluster anchor | Tamaño | Total hits | Eventos |
|---|---|---|---|
| `continuar_no_redirect_retry` | 2 | 18 | `{continuar_clicked_js, continuar_no_redirect_retry}` |

**Hallazgo**: con criterio empírico estricto sólo emerge **1 cluster fuerte** (par `continuar_clicked_js ↔ continuar_no_redirect_retry`). El resto de eventos son **singletons en términos de vecindario** — cada evento tiene una firma de transición única.

**Implicación**: el flow del bot es **altamente lineal y secuencial** a nivel de evento. El FSM agrupa eventos en estados, pero a nivel runtime no hay agrupaciones naturales fuertes — son trayectorias casi siempre idénticas.

---

## B) TRANSITION MATRIX REAL vs FSM (lado a lado)

### B.1 Matriz 5×5

Notación:
- `N+A`   → observada **N** veces + permitida por FSM ✅
- `N+X!`  → observada **N** veces pero **NO** permitida (DRIFT crítico)
- `−+A`   → permitida pero **nunca** observada (false positive FSM)
- `−+−`   → ni permitida ni observada (consistente)

```
            ┌─ setup ──┬─ browsing ─┬─ login ──┬─ checkout ┬─── exit ──┐
   setup    │   −+−    │    9+A     │   −+−    │   −+−     │   −+A     │  ← FSM permite exit pero nunca pasa
   browsing │   −+−    │   36+A     │   9+A    │   −+A     │   −+A     │  ← 2 false positives
   login    │   −+−    │    −+A     │  14+A    │   1+A     │   1+A     │  ← back-edge browsing nunca
   checkout │   −+−    │    −+−     │   −+A    │   −+A     │   1+A     │  ← 2 false positives
   exit     │   −+−    │    −+−     │   −+−    │   −+−     │   −+−     │  (terminal)
            └──────────┴────────────┴──────────┴───────────┴───────────┘
```

### B.2 Lado-a-lado: allowed vs observed

| Transición | Permitida FSM | Observada runtime | Status |
|---|---|---|---|
| setup → browsing | ✅ | **9** | OK |
| setup → exit | ✅ | 0 | **FSM-only (false positive)** |
| browsing → browsing | ✅ | **36** | OK (loop) |
| browsing → login | ✅ | **9** | OK |
| browsing → checkout | ✅ | 0 | **FSM-only (false positive)** |
| browsing → exit | ✅ | 0 | **FSM-only (false positive)** |
| login → login | ✅ | **14** | OK (loop) |
| login → browsing | ✅ | 0 | **FSM-only (false positive)** |
| login → checkout | ✅ | **1** | OK |
| login → exit | ✅ | **1** | OK |
| checkout → checkout | ✅ | 0 | **FSM-only (false positive)** |
| checkout → login | ✅ | 0 | **FSM-only (false positive)** |
| checkout → exit | ✅ | **1** | OK |

**Totales**:
- FSM permite: 13 transiciones
- Runtime observa: 7 transiciones
- **Intersección (correctas)**: 7
- **Sólo FSM (ruido del modelo)**: 6
- **Sólo runtime (modelo lo prohibiría)**: 0

> El FSM es **superconjunto correcto** del runtime: no rompe ningún path real. Pero **declara 6 transiciones que en 628 eventos no se han ejercido**.

---

## C) HEATMAP DE ESTADOS (hits reales)

### C.1 Tabla principal

```
Estado FSM       Hits    %        Bar (1 bloque = 2%)
────────────────────────────────────────────────────────────────────
setup             245   39.0 %   ███████████████████  (HOTSPOT)
browsing          226   36.0 %   █████████████████
login             134   21.3 %   ██████████
checkout           16    2.5 %   █                     (INFRAUTILIZADO)
exit                7    1.1 %                          (INFRAUTILIZADO)
────────────────────────────────────────────────────────────────────
                  628   100 %
```

> **Observación crítica vs análisis anterior**: cuando el evento se atribuye al estado FSM **donde realmente se emitió** (sin heurística por keyword), SETUP pasa de 28% → **39%** y BROWSING/LOGIN bajan. Esto desmiente la suposición de que el bot pasa "poco tiempo" en SETUP — es realmente el estado **más activo en absoluto** del runtime.

### C.2 Top 10 eventos por estado FSM (sin reclasificar)

#### SETUP (245 hits, 39.0%)

| Evento | Hits |
|---|---|
| `pick_account_tier` | **16** |
| `init` | 12 |
| `morelogin_fresh_created` | 12 |
| `profile_picked` | 12 |
| `account_picked` | 12 |
| `profile_started` | 11 |
| `cdp_version_ok` | 11 |
| `cdp_connected_ok` | 11 |
| `pre_flight_http` | 10 |
| `persona_created` | 10 |

#### BROWSING (226 hits, 36.0%)

| Evento | Hits |
|---|---|
| `qty` | 27 |
| `shot` | 26 |
| `click_continuar` | 18 |
| `hydration_initial` | 9 |
| `hydration_done` | 9 |
| `cookies_post_hydration_accepted` | 9 |
| `cookies_post_hydration_final` | 9 |
| `page_state_detect` | 9 |
| `quick_mode_via_best_available` | 9 |
| `stepper_offer` | 9 |

#### LOGIN (134 hits, 21.3%)

| Evento | Hits |
|---|---|
| `shot` | 24 |
| `recovery` | 16 |
| `login_start` | 13 |
| `pick_account_tier` | 6 |
| `account_swap` | 6 |
| `pre_checkout_checkboxes` | 6 |
| `no_email_input` | 5 |
| `login_full_false_treating_as_bad_creds` | 5 |
| `email_typed` | 4 |
| `email_continuar_clicked` | 4 |

#### CHECKOUT (16 hits, 2.5%)

| Evento | Hits |
|---|---|
| `shot` | 4 |
| `adyen_iframes_ready` | 1 |
| `fill_card_start` | 1 |
| `fill_card_data` | 1 |
| `fill_card_inputs_filled` | 1 |
| `province_combobox_found` | 1 |
| `province_typed` | 1 |
| `province_option_clicked` | 1 |
| `guardar_continuar_clicked` | 1 |
| `card_fields_filled` | 1 |

#### EXIT (7 hits, 1.1%)

| Evento | Hits |
|---|---|
| `history_cleanup_finally` | 2 |
| `profile_closed` | 2 |
| `profile_auto_destroyed` | 2 |
| `bot_block_persisted` | 1 |

---

## D) DRIFT CRÍTICO

### D.1 FSM permite pero NUNCA pasa (false positives, 6/13)

```
DRIFT-A1   setup    → exit       (declarada, 0 hits en 628 eventos)
DRIFT-A2   browsing → checkout   (declarada, 0 hits)
DRIFT-A3   browsing → exit       (declarada, 0 hits)
DRIFT-A4   login    → browsing   (declarada, 0 hits)   ← back-edge "post-recovery"
DRIFT-A5   checkout → checkout   (declarada, 0 hits)   ← loop CHECKOUT
DRIFT-A6   checkout → login      (declarada, 0 hits)   ← back-edge re-auth
```

**46 % de las transiciones declaradas por el FSM son ruido**. No las ejerce ningún run en 628 eventos.

### D.2 Observadas pero FSM NO modela (missing transitions)

```
NINGUNA.
```

> El FSM v2 no rompe ningún path observado. Toda transición ejecutada está prevista en el modelo.

### D.3 Eventos UNMAPPED por FSM (no clasificados)

**91 de los 109 eventos observados (83.5 %) no tienen mapeo explícito en `EVENT_TO_TRANSITION`**. El FSM los relega a "observability implícita".

#### Top 30 UNMAPPED por frecuencia

| Hits | Evento | Estado emitido |
|---|---|---|
| 27 | `qty` | browsing |
| 22 | `pick_account_tier` | setup/login |
| 18 | `login_start` | setup/login |
| 16 | `recovery` | login |
| 12 | `account_picked` | setup |
| 12 | `profile_picked` | setup |
| 12 | `morelogin_fresh_created` | setup |
| 11 | `cdp_connected_ok` | setup |
| 11 | `profile_started` | setup |
| 11 | `cdp_version_ok` | setup |
| 10 | `persona_created` | setup |
| 10 | `geo_set` | setup |
| 10 | `pre_flight_http` | setup |
| 10 | `headers_set` | setup |
| 9 | `stepper_offer` | browsing |
| 9 | `stealth_applied` | setup |
| 9 | `post_continuar_url` | browsing |
| 9 | `hydration_initial` | browsing |
| 9 | `quick_mode_via_best_available` | browsing |
| 9 | `find_tickets_clicked` | browsing |
| 9 | `cookies_post_hydration_accepted` | browsing |
| 9 | `page_state_detect` | browsing |
| 9 | `cookies_post_hydration_final` | browsing |
| 9 | `checkbox_acepto` | browsing |
| 9 | `no_email_input` | login |
| 9 | `continuar_ok` | browsing |
| 9 | `stealth_js_added` | setup |
| 9 | `login_full_false_treating_as_bad_creds` | login |
| 9 | `click_find_tickets` | browsing |
| 9 | `hydration_done` | browsing |

### D.4 Estados como "cajón de sastre" (% eventos UNMAPPED por estado)

```
Estado FSM    UNMAPPED hits   % del estado    Veredicto
─────────────────────────────────────────────────────────────────
setup            216           88.2 %         CAJÓN DE SASTRE (peor)
exit               6           85.7 %         (tamaño bajo, no significativo)
login             92           68.7 %         CAJÓN parcial
browsing         155           68.6 %         CAJÓN parcial
checkout          10           62.5 %         (tamaño bajo)
```

> **SETUP es el cajón de sastre absoluto**: el 88% de sus 245 eventos no están clasificados explícitamente. El FSM contiene **0 eventos mapeados como TRANSITION para entrar a SETUP** (es el estado inicial implícito) y **1 solo evento explícito que sale de SETUP** (`goto` → BROWSING).

### D.5 Loops FSM declarados vs observados

| Estado | FSM `LOOPS_ALLOWED` | Self-transitions observadas | Status |
|---|---|---|---|
| browsing | ✅ | 36 | OK |
| login | ✅ | 14 | OK |
| checkout | ✅ | **0** | **DRIFT** — declarado, jamás ejercido |

> El loop `checkout → checkout` del FSM es ruido: declarado pero nunca observado. Si se mantiene es por previsión teórica, no por observación.

### D.6 Loops reales a nivel evento (no en FSM)

Loops puros `evento→mismo_evento`:

| Hits | Evento | Estado emitido | ¿Modelado por FSM? |
|---|---|---|---|
| 18 | `qty` | browsing | NO — el FSM no lo lista como LOOP event |
| 13 | `recovery` | login | NO — el FSM no lo declara como LOOP event explícito |
| 4 | `pre_checkout_checkboxes` | login | NO — sin mapping |
| 3 | `shot` | browsing/login | OBSERVABILITY (sí mapeado) |

Ciclos cortos `A↔B`:

| A↔B (freq) | Eventos | ¿Modelado? |
|---|---|---|
| 3↔3 | `shot ↔ tm_forced_password_reset_detected` | NO |
| 1↔1 | `session_recovered_re_navigating_event ↔ shot` | NO |
| 1↔1 | `guardar_continuar_clicked ↔ shot` | NO |
| 1↔1 | `card_fields_filled ↔ shot` | NO |

> Los 4 self-loops reales más fuertes (`qty`, `recovery`, `pre_checkout_checkboxes`) y los 4 ciclos AB **no están explícitamente modelados** como LOOP events en `EVENT_TO_TRANSITION`. El FSM marca el self-transition a nivel de **estado**, no de evento.

---

## E) TOP FAILURE PATTERNS (secuencias previas a fallo)

**Definición de fallo (sin inventar)**: el evento contiene uno de `FATAL_`, `_err`, `_failed`, `no_email_input`, `no_more_accounts`, `bot_block`, `TIMEOUT_`, `max_account_swaps`, `reset_method_next_failed`, `recovery_failed`.

**Total fallos detectados**: 17 en 628 eventos.

### E.1 Top 10 patrones (3 eventos previos → fallo)

| # | Hits | Secuencia previa (3 evt) | Fallo | Estado FSM |
|---|---|---|---|---|
| 1 | **4** | `post_continuar_url → oauth_detected → login_start` | `no_email_input` | LOGIN |
| 2 | **3** | `pick_account_tier → account_swap → login_start` | `no_email_input` | LOGIN |
| 3 | **2** | `force_reset_btn_clicked → shot → radio_email_selected` | `reset_method_next_failed` | LOGIN |
| 4 | **2** | `pre_login_navigate → pre_login_form_detected → login_start` | `no_email_input` | SETUP→LOGIN |
| 5 | 1 | `blockers_minor → continuar_no_redirect_retry → click_continuar` | `continuar_js_err` | BROWSING |
| 6 | 1 | `recovery → recovery → recovery_result` | `recovery_failed_swap_account` | LOGIN |
| 7 | 1 | `pwd_continuar_clicked → shot → shot` | `FATAL_bot_block_early` | LOGIN |
| 8 | 1 | `shot → shot → FATAL_bot_block_early` | `bot_block_persisted` | LOGIN/EXIT |
| 9 | 1 | `profile_picked → pick_account_tier → account_picked` | `pre_flight_skipped_err` | SETUP |
| 10 | 1 | `shot → click_find_tickets → find_tickets_clicked` | `shot_err` | BROWSING |

### E.2 Concentración de fallos por estado

```
Estado FSM    Fallos   %        Severidad
──────────────────────────────────────────────
login            8     47.1 %   ALTA — cuello de botella
setup            5     29.4 %   ALTA — pre-flight/profile init
browsing         2     11.8 %   media
exit             2     11.8 %   media (post-bot_block)
checkout         0      0.0 %   OK
──────────────────────────────────────────────
TOTAL           17    100.0 %
```

> El **76.5 %** de los fallos ocurre en SETUP+LOGIN (pre-checkout). El FSM modela esto con 2/5 estados — un tercio del modelo concentra tres cuartos de los fallos.

### E.3 Patrones canónicos extraídos

**Patrón α — OAUTH_NO_EMAIL_INPUT** (9 ocurrencias, 53 % de los fallos)
> Secuencias #1, #2, #4: todas terminan en `login_start → no_email_input`. El bot llega al OAuth, dispara `login_start`, y no encuentra el input de email.

**Patrón β — BOT_BLOCK_AFTER_PWD** (2 ocurrencias)
> Secuencias #7, #8: tras `pwd_continuar_clicked` → 2 screenshots → `FATAL_bot_block_early` → `bot_block_persisted`.

**Patrón γ — RECOVERY_LOOP** (1 ocurrencia)
> `recovery → recovery → recovery_result → recovery_failed_swap_account`. Loop de 3 con fallo final.

**Patrón δ — PRE_FLIGHT_SKIPPED** (1 ocurrencia)
> `profile_picked → pick_account_tier → account_picked → pre_flight_skipped_err`. Setup falla antes de `goto`.

---

## RESUMEN DEL DRIFT (sin recomendación, sin diseño)

| Eje | Observación empírica | Severidad del drift |
|---|---|---|
| **Cobertura de eventos** | FSM mapea 41 / 109 observados = 37.6 %. UNMAPPED = 83.5 % de runtime | ALTA |
| **Transiciones declaradas** | 13 declaradas, 7 ejercidas, 6 nunca observadas | media |
| **Transiciones prohibidas** | 0 violaciones | OK |
| **Loops declarados** | 3 declarados, 2 ejercidos, 1 (`checkout`) nunca observado | baja |
| **Loops reales (evento)** | 4 self-loops fuertes (`qty`, `recovery`, `pre_checkout_checkboxes`, `shot`) no modelados como LOOP events | media |
| **Cajón de sastre** | SETUP absorbe 88.2 % de los UNMAPPED. Atribución temporal-bruta sin clasificación. | ALTA |
| **Topología de fallo** | 76.5 % de fallos en SETUP+LOGIN; CHECKOUT registra 0 fallos pero solo 1/12 runs llegó | ALTA |
| **Linealidad real** | Camino real lineal (5 aristas direccionales únicas + 2 self-loops). Cero ramificaciones observadas. | El FSM modela más complejidad que la que el bot ejerce |
| **Patrón de fallo dominante** | `… → login_start → no_email_input` (9/17 fallos = 53 %) | crítico |
| **Tasa real de éxito end-to-end** | 1/12 runs llega a CHECKOUT completo (8.3 %) | crítico |

---

## ¿Dónde el modelo se aleja de la realidad?

**Empíricamente, en 4 lugares concretos:**

1. **Sobre-declaración de transiciones**: el FSM declara 6 transiciones que en 628 eventos nunca se han ejercido (`setup→exit`, `browsing→checkout`, `browsing→exit`, `login→browsing`, `checkout→checkout`, `checkout→login`).

2. **Sub-declaración de eventos**: el FSM mapea explícitamente 41 eventos; el runtime emite 109. **El 83.5 % del runtime es "observability implícita"** según el modelo, lo que reduce la utilidad del FSM como guía de comportamiento real.

3. **Estado SETUP usado como cajón temporal**: el 88 % de los eventos en SETUP son UNMAPPED. El modelo dice que SETUP "es el estado inicial" sin caracterizar qué pasa dentro — pero es **el estado con más actividad runtime (245 hits, 39%)**.

4. **Loops reales no modelados a nivel evento**: `qty` (18), `recovery` (13), `pre_checkout_checkboxes` (4) son self-loops fuertes a nivel evento que no aparecen en `EVENT_TO_TRANSITION` como LOOP. El FSM modela el self-transition a nivel de estado, no de evento.

---

## ¿Dónde el modelo coincide con la realidad?

1. **Camino feliz**: las 5 transiciones del camino lineal (`setup→browsing→login→checkout→exit` + `login→exit`) son las únicas que el runtime ejerce, y todas están declaradas correctamente por el FSM.

2. **Self-loops principales**: los 2 self-loops fuertes (`browsing×36`, `login×14`) están declarados correctamente como `LOOPS_ALLOWED`.

3. **Cero violaciones**: no se observó ninguna transición prohibida por el FSM. El modelo es **un superconjunto correcto** del runtime.

4. **Topología de outcomes**: el único path a EXIT desde CHECKOUT (`checkout→exit` con outcome=SUCCESS) coincide con el único run exitoso real.

---

## Apéndice: artefactos generados

| Fichero | Tamaño | Contenido |
|---|---|---|
| `_empirical_validation.py` | ~15 KB | Script reproducible (ejecutable con `PYTHONIOENCODING=utf-8 python _empirical_validation.py`) |
| `runs/empirical_validation.json` | 14.4 KB | Datos crudos del análisis |
| `runs/test_*.log` × 12 | varios | Logs de runs reales |
| `core/observability/state_machine.py` | 19 KB | FSM v2 (referencia, no verdad) |

*Generado: 2026-05-13 · Basado exclusivamente en datos empíricos de 12 runs, 628 eventos. No se reclasifica ni se reinterpreta runtime.*
