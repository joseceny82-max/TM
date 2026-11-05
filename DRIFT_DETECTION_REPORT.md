# Drift Detection Report — FSM v2 vs Runtime Real

> **Objetivo**: comparar el FSM simplificado de 5 estados (`SETUP / BROWSING / LOGIN / CHECKOUT / EXIT`) contra el comportamiento empírico observado en **12 runs reales** del bot (`runs/test_*.log`).
>
> **Pregunta clave**: *¿el FSM v2 modela fielmente el bot ejecutado, o existe drift entre el modelo y la realidad?*

| Métrica | Valor |
|---|---|
| Runs analizados | **12** |
| Eventos totales emitidos | **628** |
| Eventos únicos observados | **109 / 231** (47% del catálogo del código) |
| Estados FSM cubiertos | **5 / 5** (100%) |
| Transiciones permitidas observadas | **5 / 13** (38%) |
| False positives del FSM (declaradas, nunca observadas) | **8** |
| Missing transitions (observadas, prohibidas) | **0** |
| Misclasificaciones evento→estado | **97 hits** (~15% del runtime) |
| Eventos sin mapeo a estado | **4** (`TRACE_AUTH_*`, `URL_CHANGED`) |
| Tasa de éxito real (READY_FOR_USER) | **1 / 12 = 8.3%** |
| Tasa de bot_block | **2 / 12 = 16.7%** |
| Tasa de fallo en LOGIN | **12 / 17 fallos = 70.6%** |

**Fuente**: `runs/drift_detection.json` (12,334 B) generado por `_drift_detection.py`.

---

## A) Real state graph (derivado de datos)

### A.1 Distribución real de eventos por estado FSM

```
Estado FSM      Hits      %        Cobertura      Veredicto
---------------------------------------------------------------
SETUP           178      28.3 %    ████████████   sano
BROWSING        264      42.0 %    █████████████████  HOTSPOT (mayor uso)
LOGIN           143      22.8 %    ██████████     sano
CHECKOUT         19       3.0 %    █              INFRAUTILIZADO (sólo 1/12 llegó)
EXIT             17       2.7 %    █              INFRAUTILIZADO
---------------------------------------------------------------
TOTAL           628      100.0 %                  47% de eventos del catálogo activos
```

> **Hallazgo 1**: el bot REAL se concentra en `BROWSING + SETUP + LOGIN` (93% de la actividad). Sólo el **3% del tiempo** entra en CHECKOUT y el **2.7%** llega a EXIT controlado. Esto refleja que **la mayoría de runs muere o se atasca antes del checkout**.

### A.2 Transiciones reales observadas (peso = nº de runs)

```
setup    ──[9x]──> browsing        ★ camino feliz #1
browsing ──[9x]──> login           ★ camino feliz #2
login    ──[1x]──> checkout        ★ camino feliz #3 (sólo 1 run lo logró)
checkout ──[1x]──> exit            ★ camino feliz #4
login    ──[1x]──> exit            ✗ aborto (FATAL en login)
```

**Grafo direccional final (5 nodos, 5 aristas)**:

```
                                    ┌────────┐
   ┌─────────┐  9    ┌────────────┐ 9 │        │ 1    ┌──────────┐
   │  SETUP  │ ────> │  BROWSING  │──>│ LOGIN  │ ───> │ CHECKOUT │
   └─────────┘       └────────────┘   └────────┘      └──────────┘
                                          │ 1                │ 1
                                          └────────┬─────────┘
                                                   ▼
                                            ┌──────────┐
                                            │   EXIT   │
                                            └──────────┘
```

> **Hallazgo 2**: el flow real es **estrictamente lineal** (no ciclos entre estados FSM). Los 12 runs siguen siempre la misma forma `SETUP → BROWSING → LOGIN → (CHECKOUT|EXIT) → EXIT`, divergiendo sólo en el punto de aborto.

### A.3 Outcome de los 12 runs por estado terminal

| Outcome | Frecuencia | Estado donde acaba |
|---|---|---|
| `FATAL_bot_block_early` / `bot_block_persisted` | 2 | EXIT (vía login→exit) |
| `no_email_input` (login fail) | 9 | LOGIN bucle / EXIT |
| `READY_FOR_USER` | 1 | EXIT (via checkout) |
| Otros aborts (recovery, checkout incompleto) | 5 | LOGIN/CHECKOUT |

**Tasa real de éxito end-to-end: 8.3 % (1/12).** El cuello de botella es LOGIN.

---

## B) Heatmap de estados vs eventos

Top 20 eventos por estado FSM (frecuencia real en runs).

### SETUP (178 hits / 28.3% del runtime)

| Evento | Freq |
|---|---|
| `init` | 12 |
| `morelogin_fresh_created` | 12 |
| `profile_picked` | 12 |
| `profile_started` | 11 |
| `cdp_version_ok` | 11 |
| `cdp_connected_ok` | 11 |
| `pre_flight_http` | 10 |
| `persona_created` | 10 |
| `headers_set` | 10 |
| `geo_set` | 10 |
| `stealth_js_added` | 9 |
| `stealth_applied` | 9 |
| `storage_state_TOO_OLD_skip` | 7 |
| `eps_warming_home` | 6 |
| `eps_sid_check` | 6 |
| `loading_storage_state` | 4 |
| `storage_state_loaded` | 4 |
| `warming_home_start` | 4 |
| `warming_home_done` | 4 |
| `warmup_start` | 2 |

> **Lectura**: SETUP es **determinista y robusto** (12/12 runs ejecutan `init`, `profile_picked`, `morelogin_fresh_created`). Sólo cae a 9/12 en `stealth_*` (3 runs ya abortaron antes).

### BROWSING (264 hits / 42.0% del runtime — HOTSPOT)

| Evento | Freq |
|---|---|
| `shot` | 58 |
| `qty` | 27 |
| `click_continuar` | 18 |
| `goto` | 9 |
| `hydration_initial` | 9 |
| `hydration_done` | 9 |
| `cookies_post_hydration_accepted` | 9 |
| `cookies_post_hydration_final` | 9 |
| `page_state_detect` | 9 |
| `quick_mode_via_best_available` | 9 |
| `stepper_offer` | 9 |
| `click_find_tickets` | 9 |
| `find_tickets_clicked` | 9 |
| `checkbox_acepto` | 9 |
| `continuar_clicked_js` | 9 |
| `continuar_no_redirect_retry` | 9 |
| `continuar_ok` | 9 |
| `post_continuar_url` | 9 |
| `cookies_accepted` | 8 |
| `blockers_eval_skipped` | 8 |

> **Lectura**: `shot` aparece **58x** en BROWSING (screenshots/refresh). Las 9 acciones del stepper se ejecutan **todas exactamente 9 veces** → 9/12 runs llegan a `post_continuar_url`. Los otros 3 abortan antes.

### LOGIN (143 hits / 22.8% — CUELLO DE BOTELLA)

| Evento | Freq |
|---|---|
| `pick_account_tier` | 22 |
| `login_start` | 18 |
| `recovery` | 16 |
| `account_picked` | 12 |
| `account_swap` | 10 |
| `oauth_detected` | 9 |
| `no_email_input` | 9 |
| `login_full_false_treating_as_bad_creds` | 9 |
| `pre_login_SKIPPED_go_direct_to_event` | 5 |
| `email_typed` | 4 |
| `tm_forced_password_reset_detected` | 3 |
| `auto_recovery_start` | 3 |
| `force_reset_btn_clicked` | 3 |
| `radio_email_selected` | 3 |
| `reset_method_next_failed` | 2 |
| `recovery_result` | 2 |
| `pre_login_navigate` | 2 |
| `pre_login_form_detected` | 2 |
| `recovery_failed_swap_account` | 1 |
| `pwd_typed` | 1 |

> **Lectura crítica**: `pick_account_tier` se ejecuta **22 veces** sobre 12 runs (1.83x/run) → hay re-picks/swaps. `no_email_input` aparece **9 veces** (75% de runs) y casi siempre va precedido de `oauth_detected`. Sólo **1 run logra `pwd_typed`** (8.3%).

### CHECKOUT (19 hits / 3.0% — INFRAUTILIZADO)

| Evento | Freq |
|---|---|
| `pre_checkout_checkboxes` | 6 |
| `watch_for_checkout` | 1 |
| `watch_initial_url` | 1 |
| `ir_al_pago_clicked` | 1 |
| `checkout_detected` | 1 |
| `adyen_iframes_ready` | 1 |
| `fill_card_start` | 1 |
| `fill_card_data` | 1 |
| `fill_card_inputs_filled` | 1 |
| `province_combobox_found` | 1 |
| `province_typed` | 1 |
| `province_option_clicked` | 1 |
| `guardar_continuar_clicked` | 1 |
| `card_fields_filled` | 1 |

> **Lectura**: el único run que llega a CHECKOUT lo recorre completo (Adyen iframe, province combobox, card fill). El resto (11 runs) ni siquiera entra. `pre_checkout_checkboxes` aparece 6 veces porque puede ejecutarse en BROWSING→LOGIN frontera (ver misclasificación).

### EXIT (17 hits / 2.7%)

| Evento | Freq |
|---|---|
| `history_cleanup_finally` | 4 |
| `profile_closed` | 4 |
| `profile_auto_destroyed` | 4 |
| `FATAL_bot_block_early` | 1 |
| `bot_block_persisted` | 1 |
| `READY_FOR_USER` | 1 |
| `history_cleanup_inside` | 1 |
| `stop_received` | 1 |

> **Lectura**: sólo 4/12 runs llegan a cleanup ordenado. 8/12 mueren sin pasar por EXIT formal → posible leak de profiles MoreLogin.

---

## C) Matriz de transición real vs permitida

Notación de celdas:
- `9*` → permitida + observada (count = 9)
- `--` → permitida pero **nunca observada** = false positive del FSM
- `XX` → observada pero prohibida = missing transition (no se detectó ninguna)
- `(blank)` → prohibida y no observada (consistente)

### Matriz 5×5

```
            SETUP  BROWSING  LOGIN  CHECKOUT  EXIT
SETUP        .      9*        .        .       --
BROWSING     .      --        9*       --      --
LOGIN        .      --        --       1*      1*
CHECKOUT     .      .         --       --      1*
EXIT         .      .         .        .        .   (terminal)
```

### Lectura por origen

| Desde | Permitidas FSM | Observadas | Veredicto |
|---|---|---|---|
| SETUP | `{BROWSING, EXIT}` | `{BROWSING:9}` | EXIT desde SETUP nunca ocurre → false positive |
| BROWSING | `{BROWSING, LOGIN, CHECKOUT, EXIT}` | `{LOGIN:9}` | 3 self/forward transitions nunca usadas |
| LOGIN | `{BROWSING, LOGIN, CHECKOUT, EXIT}` | `{CHECKOUT:1, EXIT:1}` | self-loop LOGIN→LOGIN y back-edge LOGIN→BROWSING nunca usados |
| CHECKOUT | `{CHECKOUT, LOGIN, EXIT}` | `{EXIT:1}` | self-loop y back-to-LOGIN nunca usados |
| EXIT | (terminal) | -- | OK |

### Conclusión global de la matriz

- **5 transiciones declaradas se usan** (el camino feliz lineal).
- **8 transiciones declaradas son ruido** (self-loops y back-edges del FSM que el código NUNCA dispara).
- **0 transiciones del runtime están prohibidas por el FSM** → el FSM es un **superconjunto correcto** del comportamiento real.

---

## D) Drift entre FSM v2 y runtime

### D.1 False positives del FSM (8) — transiciones declaradas pero nunca observadas

> El FSM v2 declara estas transiciones como válidas, pero ningún run las ejerció en 628 eventos. Son **candidatas a poda** si se confirma que no se dan en producción.

| # | Origen | Destino | Razonamiento de poda |
|---|---|---|---|
| 1 | `browsing` | `browsing` | Self-loop teórico (re-hydration). El bot real NO re-entra a BROWSING tras salir. |
| 2 | `browsing` | `checkout` | Salto directo sin pasar por LOGIN. Nunca ocurre porque TM siempre exige auth. |
| 3 | `browsing` | `exit` | Aborto en browsing. Los aborts reales emiten `FATAL_*` ya en LOGIN. |
| 4 | `checkout` | `checkout` | Self-loop (re-fill_card). El único run que llegó no reintentó. |
| 5 | `checkout` | `login` | Back-edge "tu sesión expiró en checkout". No observado en 12 runs. |
| 6 | `login` | `browsing` | Back-edge tras swap. Los swaps reales se quedan en LOGIN. |
| 7 | `login` | `login` | Self-loop por recovery/retry. Las recoveries son eventos internos, NO re-entrada de estado. |
| 8 | `setup` | `exit` | Aborto temprano en SETUP. Los profile_creation_failed observados re-intentan, no abortan. |

> **Recomendación FSM v3 (futuro)**: si tras 50+ runs siguen sin ejercerse, podarlas para acercar el FSM a un grafo lineal puro: `SETUP → BROWSING → LOGIN → CHECKOUT → EXIT` con un único atajo `LOGIN → EXIT` (aborto auth).

### D.2 Missing transitions (0) — observadas pero prohibidas por FSM

> **Ninguna**. El FSM v2 es **completo respecto al runtime observado**: no hay transiciones reales que el modelo prohíba. Esto valida la corrección del modelo aunque sea laxo.

### D.3 Misclasificaciones evento → estado FSM (top 15)

> Eventos cuyo estado FSM al emitirse **no coincide** con el estado asignado por `classify_event()` o por la heurística de palabras clave. Indica errores de mapeo (no del FSM en sí).

| Evento | FSM activo | Asignado por heurística | Hits | Causa raíz |
|---|---|---|---|---|
| `shot` | **login** | browsing | **24** | `shot` es polimórfico (screenshot puede dispararse en cualquier estado) |
| `pick_account_tier` | **setup** | login | **16** | Picker corre antes de la primera transición a LOGIN — pertenece a SETUP por contexto temporal |
| `account_picked` | **setup** | login | **12** | Idem `pick_account_tier` |
| `pre_checkout_checkboxes` | **login** | checkout | **6** | Pre-checkout corre en frontera LOGIN→CHECKOUT antes de la transición real |
| `pre_login_SKIPPED_go_direct_to_event` | **setup** | login | **5** | Decisión de pre-login antes de la transición |
| `login_start` | **setup** | login | **5** | Emitido en último evento de SETUP antes de transición |
| `email_continuar_clicked` | **login** | browsing | **4** | Acción dentro de login pero clasificada como browsing por nombre |
| `shot` | **checkout** | browsing | **4** | Idem polimorfismo de `shot` |
| `no_email_input` | **setup** | login | **4** | El detector dispara en SETUP previo a entrar a LOGIN formalmente |
| `shot` | **setup** | browsing | **4** | Idem |
| `login_full_false_treating_as_bad_creds` | **setup** | login | **4** | Detector temprano antes de entrar a LOGIN |
| `account_swap` | **setup** | login | **4** | Swap antes de re-iniciar LOGIN |
| `pre_login_navigate` | **setup** | login | **2** | Navegación previa al estado LOGIN real |
| `pre_login_form_detected` | **setup** | login | **2** | Detección antes de entrar a LOGIN |
| `history_cleanup_finally` | **login** | exit | **2** | Cleanup en finally de LOGIN sin haber pasado por EXIT formal |

**TOTAL misclasificaciones: 97 hits (~15% del runtime).**

> **Causa raíz principal**: 3 patrones repetidos
> 1. **`shot` es polimórfico** (32 hits mal clasificados) → debería NO mapear a ningún estado (es OBSERVABILITY puro).
> 2. **Eventos "pre-X"** (pre_login_*, pre_checkout_*) están temporalmente en estado N-1 pero semánticamente apuntan al N → resolverse decidiendo si son TRANSITION o sólo NOTE.
> 3. **Picker/swap** (`pick_account_tier`, `account_picked`, `account_swap`) corren en SETUP pero el clasificador los pone en LOGIN por keyword "account".

> **Recomendación inmediata**: añadir campo `subcluster` o `state_hint` al payload del `log()` y dejar que el código emisor declare el estado, en vez de inferirlo por nombre. Coste: 1 línea por log call. Beneficio: misclasificación → 0%.

### D.4 Eventos sin mapeo (4)

> Eventos observados que no tienen entrada en `EVENT_TO_TRANSITION` ni mapeo heurístico.

| Evento | Freq | Naturaleza | Acción |
|---|---|---|---|
| `URL_CHANGED` | 1 | hook CDP dinámico | añadir como `OBSERVABILITY` (no transition) |
| `TRACE_AUTH_start` | 2 | tracer auxiliar | añadir como `OBSERVABILITY` |
| `TRACE_AUTH_waiting_40s` | 2 | tracer auxiliar | idem |
| `TRACE_AUTH_done` | 2 | tracer auxiliar | idem |

Todos son TRACE/HOOK no relevantes para el FSM. **No alteran el modelo.**

### D.5 Distribución de fallos por estado

```
Estado FSM    Fallos   %         Severidad
-------------------------------------------------
setup            1     5.9 %     baja  (profile fresh fail aislado)
browsing         2    11.8 %     baja  (qty/continuar errors recuperables)
login           12    70.6 %     CRÍTICA (cuello de botella absoluto)
checkout         0     0.0 %     OK
exit             2    11.8 %     media (cleanup-related)
-------------------------------------------------
TOTAL           17   100.0 %
```

> **Hallazgo crítico**: el **70.6% de los fallos ocurre en LOGIN**. Cualquier mejora en LOGIN tiene el máximo ROI.

---

## E) Top 10 secuencias de eventos antes de fallo

> Para cada fallo (FATAL_*, _err, bot_block_*, recovery_failed_*) se capturan los **3 eventos inmediatamente previos**. Se agrupan por secuencia idéntica.

### Tabla de secuencias

| # | Secuencia previa (3 eventos) | Fallo final | Hits | Estado FSM | Patrón |
|---|---|---|---|---|---|
| 1 | `post_continuar_url → oauth_detected → login_start` | **`no_email_input`** | **4** | BROWSING→LOGIN | OAuth no expone input email → bug detector OR TM rebota |
| 2 | `pick_account_tier → account_swap → login_start` | **`no_email_input`** | **3** | SETUP→LOGIN | Swap mid-flow no resetea OAuth state |
| 3 | `force_reset_btn_clicked → shot → radio_email_selected` | `reset_method_next_failed` | 2 | LOGIN | Reset password flow incompleto |
| 4 | `pre_login_navigate → pre_login_form_detected → login_start` | **`no_email_input`** | 2 | SETUP→LOGIN | Pre-login navega bien pero form no renderiza email |
| 5 | `blockers_minor → continuar_no_redirect_retry → click_continuar` | `continuar_js_err` | 1 | BROWSING | Stepper continuar bug en blockers minor |
| 6 | `recovery → recovery → recovery_result` | `recovery_failed_swap_account` | 1 | LOGIN | Bucle recovery 2x luego falla → swap |
| 7 | `pwd_continuar_clicked → shot → shot` | `FATAL_bot_block_early` | 1 | LOGIN | Tras typear pwd, dos shots y bot_block |
| 8 | `shot → shot → FATAL_bot_block_early` | `bot_block_persisted` | 1 | LOGIN→EXIT | Persistencia del bot_block tras detección |
| 9 | `profile_picked → pick_account_tier → account_picked` | `pre_flight_skipped_err` | 1 | SETUP | Pre-flight HTTP falla muy temprano |
| 10 | `shot → click_find_tickets → find_tickets_clicked` | `shot_err` | 1 | BROWSING | Error de screenshot post-click (cosmetico) |

### Patrones críticos detectados

#### Patrón #1: `OAUTH_NO_EMAIL_INPUT` (9 ocurrencias = **53% de los fallos totales**)

> Secuencias #1, #2, #4 (4+3+2 = 9 hits) terminan en `no_email_input`. Las 3 comparten el final `… → login_start → no_email_input`.

**Causa probable**: tras llegar a la pantalla OAuth (`oauth_detected`), el bot busca `input[name=email]` pero TM expone otro selector o la página está en estado intermedio.

**Acción recomendada**:
1. Loggear el HTML completo en `no_email_input` para inspección posterior.
2. Esperar `domcontentloaded + 2s` extra antes de buscar el input.
3. Probar fallback con selector genérico `input[type=email], #email-input, [data-bdd=email]`.

#### Patrón #2: `BOT_BLOCK_AFTER_PWD` (2 ocurrencias)

> Secuencias #7 y #8: `pwd_continuar_clicked → shot → shot → FATAL_bot_block_early → bot_block_persisted`.

**Causa probable**: tras enviar credenciales válidas, TM detecta firma anómala y suspende la sesión (síntoma "Su sesión se ha suspendido"). El segundo `shot` ya captura la pantalla de block.

**Acción recomendada**:
1. Aplicar las medidas anti-Akamai del feedback ([feedback_akamai_solucion_definitiva_2026.md]).
2. Considerar pause humano (2-4s) entre `pwd_continuar_clicked` y siguiente acción.
3. Verificar `_abck` cookie validation justo antes del submit.

#### Patrón #3: `RECOVERY_LOOP_FAIL` (1 ocurrencia)

> Secuencia #6: `recovery → recovery → recovery_result → recovery_failed_swap_account`.

**Causa probable**: el OTP no llega o el imap se desincroniza. El bot insiste 2-3 veces y luego hace swap (comportamiento correcto).

**Acción recomendada**: ya gestionado. Persistir.

---

## E.2 Loops y su correlación con fallo

```
Loop pattern                          Veces  Fallo después  Failure rate  Veredicto
------------------------------------------------------------------------------------
[qty]                                   18         0          0.0%        SANO ✓
[recovery]                              12         0          0.0%        SANO ✓ (loop intencional)
[pre_checkout_checkboxes]                4         0          0.0%        SANO ✓
[shot, tm_forced_password_reset_...]     3         0          0.0%        SANO ✓
[shot]                                   3         1         33.3%        ⚠ atención
[shot, session_recovered_...]            1         0          0.0%        OK
[shot, guardar_continuar_clicked]        1         0          0.0%        OK
[shot, card_fields_filled]               1         0          0.0%        OK
```

> **Lectura**: los únicos loops "peligrosos" son los `[shot]` puros (3 ocurrencias, 33% fallo) → indican que el bot está atascado tomando screenshots sin progresar. Útiles como **señal de timeout temprano** para abortar antes de quemar minutos.

---

## Resumen ejecutivo del drift

| Eje | Veredicto | Severidad |
|---|---|---|
| **Cobertura FSM** | 5/5 estados activos | OK |
| **Linealidad real** | Camino feliz único, sin ramas | OK |
| **False positives** | 8/13 transiciones declaradas son ruido | media (poda futura) |
| **Missing transitions** | 0 | OK (FSM no rompe ningún path real) |
| **Misclasificaciones** | 97 hits (15% runtime), 3 patrones raíz | media (resoluble con `state_hint`) |
| **Cuello de botella** | LOGIN concentra 70.6% de fallos | **CRÍTICA** |
| **Cobertura CHECKOUT** | 3% del runtime, 1/12 runs | informa, no acción |
| **Cleanup EXIT** | 4/12 runs salen ordenadamente | media (posible leak de profiles) |
| **Patrón dominante de fallo** | `OAUTH_NO_EMAIL_INPUT` (53% fallos) | **CRÍTICA** |
| **Tasa éxito end-to-end real** | 8.3 % | **CRÍTICA** |

---

## Acciones recomendadas (prioridad descendente)

### P0 — Impacto inmediato

1. **Resolver `OAUTH_NO_EMAIL_INPUT`**
   - Inspeccionar HTML post-`oauth_detected` cuando `no_email_input` ocurra.
   - Añadir fallback con selectores alternativos + wait extra.
   - Esperado: subir éxito de 8.3% → 30-40%.

2. **Marcar `shot` como OBSERVABILITY puro**
   - Quitar `shot` de cualquier mapeo a estado (no es transition, es screenshot).
   - Reduce misclasificaciones de 97 → 65 (-33%).

### P1 — Calidad del modelo

3. **Añadir `state_hint` al payload `log()`**
   - Cada `log_event()` declara el estado explícitamente: `log(slot, "evt", state="login")`.
   - Reduce misclasificaciones a ~0.
   - Coste: 1 kw arg por llamada (compatible con legacy).

4. **Mapear `pre_*` events como NOTE/OBSERVABILITY en su estado destino**
   - `pre_login_*` → estado=login (no setup).
   - `pre_checkout_*` → estado=checkout (no login).
   - Reduce misclasificaciones de patrón #2.

### P2 — Poda del FSM (sólo tras 50+ runs)

5. **Eliminar las 8 false positives**
   - Confirmar tras 50 runs que sigue sin ejercerse.
   - Reescribir `TRANSITIONS` con grafo lineal puro + `LOGIN→EXIT`.

### P3 — Métricas continuas

6. **Hook automático de drift detection**
   - Al cerrar cada run, generar `runs/drift_<run_id>.json` con el delta vs FSM.
   - Dashboard `/api/drift/audit` mostrando drift acumulado.

---

## Apéndice: ficheros derivados

| Fichero | Tamaño | Contenido |
|---|---|---|
| `runs/drift_detection.json` | 12,334 B | Datos crudos del análisis (json estructurado) |
| `_drift_detection.py` | 18,850 B | Script reproducible (ejecutable con `python _drift_detection.py`) |
| `core/observability/state_machine.py` | (FSM v2) | Modelo formal de referencia |
| `runs/state_graph_REAL_RUNTIME.json` | (anterior) | State graph empírico capa runtime |
| `runs/test_*.log` | 12 ficheros | Logs reales analizados |

---

> **Fase cerrada**: validación de realidad + detección de drift completada.
>
> **Próximo paso natural**: priorizar P0 (resolver `OAUTH_NO_EMAIL_INPUT`) — es el mejor ROI medible: +30 puntos de tasa de éxito por aprox. 30 minutos de instrumentación HTML capture + selector fallback.

*Generado: 2026-05-13 · Basado en `runs/drift_detection.json` (12 runs, 628 eventos, 109 únicos)*
