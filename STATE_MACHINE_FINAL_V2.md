# State Machine FINAL — 5 estados (baseline congelado)

**Fecha**: 2026-05-13
**Decisión**: ✅ **VALIDADO** (97.4% cobertura empírica)
**Modelo previo**: 11 estados → **DEPRECATED**

---

## 1. STATE MACHINE FINAL — 5 estados

```python
class State(Enum):
    SETUP     = "setup"      # arranque + profile + session ready + warming
    BROWSING  = "browsing"   # evento target, hydration, stepper, cart pre-Continuar
    LOGIN     = "login"      # OAuth + recovery interno + account swap
    CHECKOUT  = "checkout"   # checkout URL + Adyen iframe + 3DS detect
    EXIT      = "exit"       # terminal — cleanup + outcome en payload
```

**Una sola terminal**: `EXIT`. Los antiguos SUCCESS/BLOCKED son ahora **outcomes en payload**:

```python
class ExitOutcome(Enum):
    SUCCESS = "success"   # READY_FOR_USER + stop_received
    BLOCKED = "blocked"   # bot_block detected
    FATAL   = "fatal"     # FATAL_*, max_swaps, etc.
    STOPPED = "stopped"   # STOP file sin completar
```

### Transiciones permitidas (matriz cerrada)

```
   ┌─────────────────────────────────────────────────────────┐
   │                                                          │
   ▼                                                          │
SETUP ──► BROWSING ──► LOGIN ⇄ CHECKOUT ──► EXIT             │
            │ ▲          │  ▲                                 │
            │ │ (loops    │  │ (loops checkout)               │
            │ │  browsing)│  │                                 │
            │ └──────────┘  └──── login internal loops ──────┘
            │
            ▼
         CHECKOUT (sesión válida, salta LOGIN)
```

| Desde | Hacia permitidos |
|-------|------------------|
| SETUP | BROWSING, EXIT |
| BROWSING | BROWSING (loop), LOGIN, CHECKOUT, EXIT |
| LOGIN | LOGIN (loop), CHECKOUT, BROWSING (post-recovery), EXIT |
| CHECKOUT | CHECKOUT (loop), LOGIN (mid-flow re-auth), EXIT |
| EXIT | ∅ (terminal) |

### Loops permitidos

```python
LOOPS_ALLOWED = {BROWSING, LOGIN, CHECKOUT}
```

---

## 2. TABLA EVENTO → ESTADO (simplificada)

Mapping explícito: **41 eventos** (vs 49 en modelo previo). El resto son observabilidad implícita que vive en el estado actual.

### TRANSITIONS (23 eventos disparan cambio de estado)

| Event | Target | Comment |
|-------|--------|---------|
| `goto` | BROWSING | navigate to event |
| `oauth_detected` | LOGIN | OAuth redirect |
| `auth_detected_mid` | LOGIN | mid-flow re-login |
| `checkout_detected` | CHECKOUT | checkout URL entered |
| `checkout_detected_at_entry` | CHECKOUT | direct checkout |
| `stop_received` | EXIT | user stop |
| `FATAL_bot_block_detected` | EXIT | outcome=BLOCKED |
| `FATAL_bot_block_early` | EXIT | outcome=BLOCKED |
| `FATAL_no_profile` | EXIT | outcome=FATAL |
| `FATAL_no_account` | EXIT | outcome=FATAL |
| `FATAL_profile_start` | EXIT | outcome=FATAL |
| `FATAL_cdp_connect` | EXIT | outcome=FATAL |
| `FATAL_cdp_version` | EXIT | outcome=FATAL |
| `FATAL_camoufox_import` | EXIT | outcome=FATAL |
| `FATAL_continuar_no_avanza` | EXIT | outcome=FATAL |
| `FATAL_mid_login_failed` | EXIT | outcome=FATAL |
| `FATAL_no_sector_clickable` | EXIT | outcome=FATAL |
| `FATAL_no_tickets_available` | EXIT | outcome=FATAL |
| `FATAL_oauth_login_failed` | EXIT | outcome=FATAL |
| `FATAL_seatmap_no_sections` | EXIT | outcome=FATAL |
| `TIMEOUT_no_checkout` | EXIT | outcome=FATAL |
| `max_account_swaps_reached` | EXIT | outcome=FATAL |
| `no_more_accounts_to_try` | EXIT | outcome=FATAL |

### LOOPS (12 eventos auto-transición)

| Event | State | Comment |
|-------|-------|---------|
| `click_continuar` | BROWSING | retry Continuar |
| `continuar_clicked_js` | BROWSING | JS click variant |
| `continuar_no_redirect_retry` | BROWSING | retry |
| `auto_recovery_start` | LOGIN | recovery cycle |
| `tm_forced_password_reset_detected` | LOGIN | TM forced reset |
| `account_swap` | LOGIN | try different account |
| `recovery_result` | LOGIN | recovery outcome |
| `checkout_stage_url` | CHECKOUT | stage iter |
| `checkout_stage_btn_clicked` | CHECKOUT | stage btn |
| `ir_al_pago_clicked` | CHECKOUT | pay button |
| `card_fill_retry` | CHECKOUT | fill retry |
| `card_iframe_trying` | CHECKOUT | iframe poll |

### OBSERVABILITY (6 eventos cross-cutting)

| Event | Comment |
|-------|---------|
| `init` | bot started (en SETUP) |
| `pre_sleep_for_lock` | setup wait |
| `READY_FOR_USER` | checkout success marker — marca outcome=SUCCESS pendiente |
| `URL_CHANGED` | URL change marker |
| `bot_block_persisted` | bot_block persistence (no transición) |
| `shot` | screenshot |

### Resto (190 eventos) → OBSERVABILIDAD IMPLÍCITA

NO se mapean explícitamente. Viven en el estado actual sin disparar nada. Política deliberada para evitar overfitting.

---

## 3. REGLAS DE EJECUCIÓN

### Cuándo cambia estado

1. Un evento de tipo `TRANSITION` se procesa.
2. El estado destino está en `TRANSITIONS[current]`.
3. La FSM aplica `transition_to(target)` y emite `state.transition` con `from`/`to`/`event_name`.

### Cuándo NO cambia estado

- Evento de tipo `LOOP` pero estado actual ≠ target del loop → ignorado.
- Evento de tipo `OBSERVABILITY` → solo log, no cambio.
- Evento `UNKNOWN` (no mapeado) → observabilidad implícita.

### Evento obligatorio `state.transition`

Cada transición debe emitir un evento canónico:

```json
{
  "event": "state.transition",
  "from_state": "browsing",
  "to_state": "login",
  "trigger_event": "oauth_detected",
  "reason": "OAuth redirect",
  "ts": "2026-05-13T...Z"
}
```

### Outcome al entrar en EXIT

`StateMachine._infer_exit_outcome()` decide:
- `stop_received` + `READY_FOR_USER` previo → `SUCCESS`
- `stop_received` sin READY → `STOPPED`
- `FATAL_bot_block_*` → `BLOCKED`
- Cualquier otro `FATAL_*` / `TIMEOUT_*` / `max_*` → `FATAL`

---

## 4. QUÉ SE PIERDE vs MODELO ANTERIOR (11 estados)

| Pieza eliminada | Razón | Pérdida funcional |
|-----------------|-------|-------------------|
| **`INIT` como estado separado** | 0 eventos propios. Era entry point virtual. | NINGUNA. El bot empieza en SETUP. |
| **`PROFILE_SETUP`/`SESSION_READY` separados** | Eran sub-fases del setup. SESSION_READY tenía 3 eventos. | NINGUNA. Fusionados en SETUP (43 evt). |
| **`AUTH` renombrado a `LOGIN`** | Solo cambio de nombre — más descriptivo. | NINGUNA. |
| **`CART` fusionado en BROWSING** | El flow es BROWSING + Continuar = LOGIN. CART era estado intermedio breve. | NINGUNA. Eventos cart viven ahora en BROWSING (49 evt total). |
| **`PAYMENT` fusionado en CHECKOUT** | Misma URL `/checkout/`, mismo flow continuo. | NINGUNA. Adyen iframe + 3DS detect viven en CHECKOUT (51 evt). |
| **`RECOVERY` como estado** | Era sub-flow de AUTH (post_recovery_*). | NINGUNA. Es LOOP interno de LOGIN. |
| **`SUCCESS` como estado** | Era trampolín de 1 evento (READY_FOR_USER) → EXIT. | NINGUNA. Ahora es **outcome del payload** en EXIT. |
| **`BLOCKED` como estado** | Era trampolín de 2 eventos. | NINGUNA. Ahora es **outcome del payload** en EXIT. |

### Nada se pierde en términos de información

- **Granularidad observable**: cada uno de los 231 eventos sigue emitiéndose. Ningún log se pierde.
- **Trazabilidad**: el `state.transition` event preserva `from`/`to`/`trigger_event`/`reason`.
- **Outcome semántico**: SUCCESS/BLOCKED se siguen distinguiendo en `exit_outcome`.
- **Loops**: los 3 estados con loops reales se mantienen.

### Lo que SÍ se gana

- Menos superficie de modelo (5 vs 11 estados).
- Mayor cobertura empírica (97.4% vs 93.9%).
- Cero estados infrautilizados (todos ≥33 eventos).
- Cero loops declarados sin uso.

---

## 5. CONFIRMACIÓN DE COHERENCIA CON RUNTIME REAL

### Validación contra los 231 eventos del legacy

| Métrica | Modelo previo (11 est) | Modelo final (5 est) | Cambio |
|---------|-----------------------:|---------------------:|-------:|
| **Cobertura total** | 93.9% | **97.4%** | +3.5 pp |
| Eventos truly UNMAPPED | 14 | **6** | -57% |
| Estados con <3 eventos | 4 | **0** | -100% |
| Loops declarados sin uso real | 1 (BROWSING) | **0** | -100% |
| Distribución uniforme entre estados | desigual (0% a 18.6%) | balanceada (14% a 22%) | ✅ |

### Distribución por estado (97.4% del total)

```
setup       43 evt (18.6%)  #########
browsing    49 evt (21.2%)  ##########
login       43 evt (18.6%)  #########
checkout    51 evt (22.1%)  ###########
exit        33 evt (14.3%)  #######
```

### Loops reales detectados (100% coinciden con LOOPS_ALLOWED)

| State | Loops reales | LOOPS_ALLOWED |
|-------|------------:|:-------------:|
| BROWSING | 3 events | ✅ |
| LOGIN | 4 events | ✅ |
| CHECKOUT | 5 events | ✅ |

Match perfecto: **3/3 loops declarados con eventos reales**.

### 6 eventos truly UNMAPPED

```
TRACE_AUTH_done             ← debug instrumentación cross-cutting
TRACE_AUTH_err              ← idem
TRACE_AUTH_start            ← idem
TRACE_AUTH_waiting_40s      ← idem
modal_handler_err           ← handler genérico (decisión de no mapear)
no_more_profiles_after_swap ← edge case raro (setup/login boundary)
```

Análisis: **5 de 6 son debug/instrumentación cross-cutting** (TRACE_AUTH_*, modal_handler_err). El 6º (`no_more_profiles_after_swap`) es una transición rara entre SETUP y LOGIN que se podría mapear si fuese frecuente.

**Aceptable**: cubrir 100% requeriría añadir entradas explícitas para 6 eventos raros — overhead vs beneficio bajo.

### Tests automáticos

```
$ python -m pytest tests/test_state_machine.py -v
29 passed in 0.10s

- 5 estados verificados
- Solo EXIT terminal
- 4 ExitOutcome correctos
- Todas las transiciones a estados válidos
- LOOPS_ALLOWED coinciden con matriz
- Todos los estados alcanzables desde SETUP
- Classifier: 4 categorías (TRANSITION/LOOP/OBS/UNKNOWN)
- Loop events ignorados si estado actual ≠ target del loop
- 4 outcomes (success/blocked/fatal/stopped) inferidos correctamente
- Walkthroughs: success, blocked, fatal, stopped
- classifier_handles_231_real_events: ningún crash
- Mapping ≤60 entradas (anti-overfitting check)
```

---

## 6. ESTADO DE ARCHIVOS

| Archivo | Estado | LOC |
|---------|--------|----:|
| `core/observability/state_machine.py` | reescrito v2 | 343 |
| `tests/test_state_machine.py` | 29 tests pasando | ~210 |
| `_fsm_gap_analysis.py` | actualizado para 5 estados | ~155 |
| `runs/fsm_gap_mapping.json` | regenerado v2 | ~5 KB |
| `STATE_MACHINE_FINAL_V2.md` | este reporte | — |

### Modelo previo (11 estados) — DEPRECATED

Documentos `STATE_MACHINE_FORMAL_AND_VALIDATION.md` y `OBSERVABILITY_DESIGN.md` quedan como referencia histórica pero su modelo de 11 estados está **superseded por este v2 de 5 estados**.

---

## 7. DECISIÓN

🟢 **MODELO FINAL VALIDADO — BASELINE CONGELADO**

| Pregunta | Respuesta |
|----------|-----------|
| ¿Cubre el comportamiento real del bot? | ✅ 97.4% con mapping mínimo + observabilidad implícita |
| ¿Reduce complejidad sin perder control? | ✅ 11 estados → 5. -45% superficie. 0 pérdida funcional. |
| ¿Introduce pérdida funcional? | ❌ Ninguna. Ningún log se pierde. Outcomes preservados en payload. |
| ¿Es consistente con runtime real? | ✅ Validado con 231 eventos del legacy + 29 tests automáticos. |

### El modelo final es:

- **5 estados**: SETUP, BROWSING, LOGIN, CHECKOUT, EXIT
- **1 terminal**: EXIT con 4 outcomes en payload (success/blocked/fatal/stopped)
- **3 loops permitidos**: BROWSING, LOGIN, CHECKOUT (todos con uso real)
- **41 eventos mapeados explícitamente** (23 transitions + 12 loops + 6 obs)
- **190 eventos observabilidad implícita** (viven en estado actual, no requieren mapping)

**No requiere más diseño**. Listo para integración cuando se decida.
