# Modelo de Observabilidad — Diseño estructural

**Fecha**: 2026-05-13
**Estado**: Diseño SOLO (cero implementación, cero hooks nuevos)
**Sucesor lógico de**: `SHADOW_HOOKS_PROPOSAL.md` (deferred hasta consolidar este modelo)

---

## 0. CONTEXTO — Estado actual del logging

Análisis del código real de `_compra_parallel.py`:

- **231 event names únicos** emitidos vía `log(slot, "X", **kw)`
- Distribución por severidad implícita:
  - 14 FATAL_* (UPPERCASE)
  - 56 errores con sufijo `_err` / `_failed`
  - 5 warnings (`_skipped`, `TIMEOUT_*`, `_persisted`)
  - 29 successes (`_clicked`, `_ok`, `_done`)
  - 127 info (sin sufijo claro)
- **Inconsistencia detectada**:
  - Mix UPPERCASE / snake_case (`FATAL_*`, `READY_FOR_USER`, `URL_CHANGED`, `TRACE_*`, `BAD_CREDENTIALS_*` vs el resto)
  - Verbo↔sustantivo invertido (`click_continuar` vs `continuar_clicked`)
  - Outcomes inconsistentes (`_err`, `_failed`, `_NOT_FOUND`, `_skipped`)
  - Prefijos solapados (`pre_*`, `post_*` genéricos vs `card_*`, `cookies_*` dominio)

Si añadimos 12 hooks más sin convención, el ruido se duplica. **De ahí este diseño antes de continuar**.

---

## 1. TAXONOMÍA DE EVENTOS (extensible con reglas)

### Schema canónico

```
domain.action[.outcome]
```

| Componente | Obligatorio | Valor | Ejemplos |
|-----------|:-----------:|-------|----------|
| `domain`  | ✅ | minúsculas, sin underscore | `auth`, `cart`, `payment` |
| `action`  | ✅ | verbo en past tense o stative | `started`, `loaded`, `detected` |
| `outcome` | ⚠️ opcional | enum cerrado | `ok`, `err`, `timeout`, `blocked`, `skipped` |

### 10 DOMAINS cerrados (no añadir sin justificación)

| Domain | Cobertura | Ejemplo de eventos actuales que mapean |
|--------|-----------|----------------------------------------|
| **runtime** | Lifecycle proceso (init, args, env, exit) | `init`, `pre_sleep_for_lock`, `READY_FOR_USER`, `stop_received` |
| **profile** | MoreLogin/Camoufox profile lifecycle | `profile_started`, `profile_closed`, `profile_burned_local`, `morelogin_fresh_created` |
| **session** | Browser/CDP context lifecycle | `cdp_connected_ok`, `storage_state_loaded`, `eps_warming_home` |
| **navigation** | Page.goto y URL transitions | `goto`, `URL_CHANGED`, `post_continuar_url` |
| **auth** | Login + OAuth + recovery | `oauth_detected`, `login_start`, `email_typed`, `auto_recovery_start` |
| **cart** | Seleccionar evento + qty + Continuar | `stepper_offer`, `qty`, `find_tickets_clicked`, `click_continuar` |
| **checkout** | Pre-checkout + Adyen address form | `checkout_detected`, `pre_checkout_checkboxes`, `fill_card_start` |
| **payment** | Card fields + Adyen iframe + 3DS | `adyen_iframes_ready`, `card_fields_filled`, `3ds_check` |
| **antibot** | Detección bot_block / EPS / Akamai / DataDome | `FATAL_bot_block_detected`, `eps_sid_check`, `blockers_minor` |
| **telemetry** | Outcomes, screenshots, queue-it, métricas | `shot`, `queue_it_passed`, `outcome_persist_*` |

### Mapping de los 231 actuales → schema nuevo

Ejemplos representativos (no exhaustivo):

| Actual | Propuesto | Domain | Notas |
|--------|-----------|--------|-------|
| `init` | `runtime.started` | runtime | |
| `READY_FOR_USER` | `runtime.ready_for_user` | runtime | de UPPERCASE a snake |
| `pre_sleep_for_lock` | `runtime.lock_wait` | runtime | |
| `morelogin_fresh_created` | `profile.created` | profile | dato `engine` ya en payload |
| `profile_started` | `profile.session_open` | profile | |
| `profile_closed` | `profile.session_closed` | profile | |
| `profile_auto_destroyed` | `profile.destroyed` | profile | |
| `cdp_connected_ok` | `session.cdp_attached` | session | |
| `storage_state_loaded` | `session.storage_loaded` | session | |
| `storage_state_TOO_OLD_skip` | `session.storage_skipped.timeout` | session | outcome explícito |
| `eps_warming_home` | `session.eps_warming.started` | session | |
| `eps_sid_check` | `session.eps_sid.detected` | session | |
| `goto` | `navigation.goto` | navigation | |
| `URL_CHANGED` | `navigation.url_changed` | navigation | |
| `post_continuar_url` | `navigation.post_continuar` | navigation | |
| `oauth_detected` | `auth.oauth.detected` | auth | |
| `login_start` | `auth.login.started` | auth | |
| `email_typed` | `auth.email.typed` | auth | |
| `pwd_typed` | `auth.password.typed` | auth | |
| `tm_forced_password_reset_detected` | `auth.forced_reset.detected` | auth | |
| `auto_recovery_start` | `auth.recovery.started` | auth | |
| `recovery_result` | `auth.recovery.completed` | auth | con `outcome` en payload |
| `account_swap` | `auth.account.swapped` | auth | |
| `max_account_swaps_reached` | `auth.account.swap_exhausted` | auth | |
| `BAD_CREDENTIALS_detected` | `auth.credentials.invalid` | auth | |
| `stepper_offer` | `cart.stepper.opened` | cart | |
| `qty` | `cart.qty.set` | cart | |
| `find_tickets_clicked` | `cart.find_tickets.clicked` | cart | |
| `checkbox_acepto` | `cart.checkbox.accepted` | cart | |
| `click_continuar` | `cart.continuar.attempted` | cart | |
| `continuar_clicked_js` | `cart.continuar.clicked` | cart | |
| `continuar_ok` | `cart.continuar.completed` | cart | |
| `continuar_no_redirect_retry` | `cart.continuar.retry` | cart | |
| `checkout_detected` | `checkout.entered` | checkout | |
| `pre_checkout_checkboxes` | `checkout.checkboxes.detected` | checkout | |
| `ir_al_pago_clicked` | `checkout.pay_button.clicked` | checkout | |
| `fill_card_start` | `checkout.address_form.started` | checkout | |
| `fill_card_data` | `checkout.address_form.data` | checkout | |
| `guardar_continuar_clicked` | `checkout.address_form.submitted` | checkout | |
| `adyen_iframes_ready` | `payment.iframes.ready` | payment | |
| `card_radio_clicked` | `payment.card_type.selected` | payment | |
| `card_inputs_filled` | `payment.card_fields.filled` | payment | |
| `anadir_tarjeta_clicked` | `payment.add_card.clicked` | payment | |
| `card_fields_filled` | `payment.completed` | payment | total event |
| `3ds_check` | `payment.three_ds.detected` | payment | |
| `FATAL_bot_block_detected` | `antibot.block.detected` | antibot | level=fatal |
| `bot_block_persisted` | `antibot.block.persisted` | antibot | level=fatal |
| `eps_sid_check` | `antibot.eps_sid.checked` | antibot | |
| `blockers_minor` | `antibot.minor.detected` | antibot | level=warn |
| `shot` | `telemetry.screenshot` | telemetry | |
| `queue_it_passed` | `telemetry.queue_it.passed` | telemetry | |

### Reglas de extensibilidad

- **Añadir un nuevo domain** requiere:
  1. Documentar en este archivo
  2. Justificar por qué no encaja en uno existente
  3. Mínimo 3 eventos previstos en el domain
- **Añadir una nueva action** no requiere documentación si sigue el patrón verbal estándar (past tense / stative).
- **Añadir un nuevo outcome** SÍ requiere documentación — outcomes son enum cerrado.

---

## 2. STATE MACHINE DEL BOT

### Estados oficiales (8 cerrados)

```
                    ┌──────────────┐
                    │ STATE_INIT   │ ← (entry, args parsed)
                    └──────┬───────┘
                           │ runtime.started
                           ▼
                    ┌──────────────────┐
                    │ STATE_PROFILE_   │ ← pick profile, start, CDP attach,
                    │       SETUP      │   eps warming, storage_state load
                    └──────┬───────────┘
                           │ session.ready
                           ▼
                    ┌──────────────────┐         ┌────────────┐
                    │ STATE_BROWSING   │ ←──────┤ STATE_RECOV│
                    │                  │        │  (forced   │
                    └──────┬───────────┘        │   reset)   │
                           │ cart.continuar.    └─────▲──────┘
                           │   completed              │ auth.recovery
                           ▼                          │   .started
                    ┌──────────────────┐              │
                    │  STATE_AUTH      │──────────────┘
                    │  (OAuth login)   │ auth.forced_reset.detected
                    └──────┬───────────┘
                           │ auth.login.completed
                           ▼
                    ┌──────────────────┐
                    │  STATE_CHECKOUT  │ ← address form, partners, conditions
                    └──────┬───────────┘
                           │ checkout.address_form.submitted
                           ▼
                    ┌──────────────────┐
                    │  STATE_PAYMENT   │ ← Adyen iframe + card fields + 3DS detect
                    └──────┬───────────┘
                           │ payment.completed
                           ▼
                    ┌──────────────────┐
                    │  STATE_READY     │ ← READY_FOR_USER, espera STOP
                    └──────┬───────────┘
                           │ runtime.stop_received
                           ▼
                    ┌──────────────────┐
                    │  STATE_EXIT      │ ← cleanup, profile destroy, outcome.json
                    └──────────────────┘
```

### Transiciones permitidas (matriz)

| Desde \ Hacia | INIT | PROFILE_SETUP | BROWSING | AUTH | RECOVERY | CHECKOUT | PAYMENT | READY | EXIT |
|---------------|:----:|:-------------:|:--------:|:----:|:--------:|:--------:|:-------:|:-----:|:----:|
| **INIT**          | —   | ✅            | ❌       | ❌   | ❌       | ❌       | ❌      | ❌    | ✅(fatal) |
| **PROFILE_SETUP** | ❌   | —             | ✅       | ❌   | ❌       | ❌       | ❌      | ❌    | ✅(fatal) |
| **BROWSING**      | ❌   | ❌            | —        | ✅   | ❌       | ✅(skip auth) | ❌  | ❌    | ✅(fatal) |
| **AUTH**          | ❌   | ❌            | ❌       | ✅(swap) | ✅   | ✅       | ❌      | ❌    | ✅(fatal) |
| **RECOVERY**      | ❌   | ❌            | ❌       | ✅(retry) | —  | ❌       | ❌      | ❌    | ✅(fatal) |
| **CHECKOUT**      | ❌   | ❌            | ❌       | ✅(re-auth) | ❌ | —     | ✅      | ❌    | ✅(fatal) |
| **PAYMENT**       | ❌   | ❌            | ❌       | ❌   | ❌       | ❌       | —       | ✅    | ✅(fatal) |
| **READY**         | ❌   | ❌            | ❌       | ❌   | ❌       | ❌       | ❌      | —     | ✅    |
| **EXIT**          | —    | —             | —        | —    | —        | —        | —       | —     | —    |

**Reglas**:
- Cada transición DEBE emitir un evento `runtime.state.transition` con campos `from`, `to`, `reason`.
- Transiciones NO permitidas son bugs — el bot debe terminar con error.
- `EXIT(fatal)` es siempre posible desde cualquier estado (exit code 1 o 99).

### Mapeo eventos → estados

| Event | Lo emite el estado | Trigger de transición |
|-------|-------------------|----------------------|
| `runtime.started` | (entry → INIT) | INIT |
| `profile.created` | INIT | → PROFILE_SETUP |
| `session.cdp_attached` | PROFILE_SETUP | (no transición) |
| `session.eps_sid.detected` | PROFILE_SETUP | → BROWSING |
| `navigation.url_changed` (event/) | BROWSING | (no transición) |
| `cart.continuar.completed` | BROWSING | → AUTH (si OAuth) o CHECKOUT |
| `auth.oauth.detected` | BROWSING | → AUTH |
| `auth.forced_reset.detected` | AUTH | → RECOVERY |
| `auth.recovery.completed` | RECOVERY | → AUTH |
| `auth.account.swapped` | AUTH | (no transición de macro-estado) |
| `auth.account.swap_exhausted` | AUTH | → EXIT (fatal) |
| `checkout.entered` | AUTH o BROWSING | → CHECKOUT |
| `checkout.address_form.submitted` | CHECKOUT | → PAYMENT |
| `payment.completed` | PAYMENT | → READY |
| `runtime.ready_for_user` | READY | (no transición; espera STOP) |
| `runtime.stop_received` | READY | → EXIT |
| `antibot.block.detected` | cualquiera | → EXIT (rc=99) |

---

## 3. CONVENCIÓN DE NAMING / LOGGING

### Niveles obligatorios (5)

| Level | Cuándo | Ejemplos | Acción del wrapper |
|-------|--------|----------|--------------------|
| `debug` | Detalle interno (eval JS, polls intermedios) | `session.poll_iter` | Solo a JSONL si DEBUG=1 |
| `info` | State transitions, decisiones, eventos normales | `auth.email.typed`, `profile.created` | stdout + JSONL |
| `warn` | Anomalía no fatal | `session.storage_skipped.timeout`, `antibot.minor.detected` | stdout + JSONL + bridge |
| `error` | Operación falla, retry posible | `payment.add_card.err`, `session.cdp_attach.err` | stdout + JSONL + bridge + telemetry |
| `fatal` | Bot termina con error | `antibot.block.detected`, `auth.account.swap_exhausted` | stdout + JSONL + bridge + outcome.json |

### Reglas de naming

1. **`domain.action[.outcome]`**: snake_case dot-separated, ASCII solo.
2. **`domain`**: ∈ {`runtime`, `profile`, `session`, `navigation`, `auth`, `cart`, `checkout`, `payment`, `antibot`, `telemetry`}
3. **`action`**: verbo past tense (`started`, `clicked`, `detected`, `failed`) o stative (`ready`, `pending`)
4. **`outcome`**: ∈ {`ok`, `err`, `timeout`, `blocked`, `skipped`, `retry`} — opcional, solo si añade info
5. **NO UPPERCASE** en nombres. El level explicito reemplaza el FATAL_*.
6. **NO acentos ni ñ** en event names. Sí en `kw` payload.
7. **Length**: máximo 50 chars. Si más → mover info al payload.

### Reglas de payload (`kw`)

| Campo común | Tipo | Ejemplo | Cuándo |
|-------------|------|---------|--------|
| `slot` | int | 1 | siempre (auto inyectado) |
| `state` | str | "AUTH" | en transiciones |
| `from_state` / `to_state` | str | "BROWSING"/"AUTH" | en `runtime.state.transition` |
| `account_id` | str | "0ed0ed39" | dominio `auth`, `checkout`, `payment` |
| `profile_id` | str | "AUTO-ES-380962" | dominio `profile`, `session` |
| `sessionid` | str | "380962" | idem |
| `event_url` | str (truncado 80c) | "https://..." | dominio `navigation` |
| `url` | str (truncado 80c) | similar | events post-navegación |
| `engine` | str | "morelogin"/"camoufox" | dominio `profile`, `session` |
| `elapsed_ms` | float | 123.4 | después de operación medida |
| `err` | str (truncado 200c) | "TypeError: ..." | level=error/fatal |
| `error_type` | str | "TimeoutError" | level=error/fatal |
| `outcome` | str | "blocked", "skipped" | si `action` lo requiere |
| `attempt` | int | 2 | dentro de retries |
| `total_attempts` | int | 5 | idem |

### Reglas de severidad implícita REMOVIDAS

```
ANTES (implícito en nombre):
  FATAL_bot_block_detected
  BAD_CREDENTIALS_detected
  blockers_eval_skipped
  cdp_connected_ok
  anadir_tarjeta_NOT_FOUND

DESPUÉS (explícito en level + nombre limpio):
  log_event(slot, "antibot.block.detected", level="fatal", ...)
  log_event(slot, "auth.credentials.invalid", level="error", ...)
  log_event(slot, "session.blockers_eval.skipped", level="warn", ...)
  log_event(slot, "session.cdp_attached", level="info", ...)
  log_event(slot, "payment.add_card.not_found", level="warn", ...)
```

---

## 4. REGLAS DE INSTRUMENTACIÓN

### Qué SÍ se loggea (obligatorio)

| Categoría | Ejemplos |
|-----------|----------|
| **State transitions** | Cada transición del state machine → 1 evento `runtime.state.transition` |
| **Decisiones del bot** | pick profile, pick account, swap, retry |
| **Operaciones I/O completadas** | goto OK, request resuelto, file written |
| **Errores capturados** | `except Exception as e: log(slot, "...err", err=str(e)[:200])` |
| **State invalidations** | bot_block, account swapped, profile destroyed |
| **Telemetry counters** | screenshots, JSONL writes, profile burns |
| **Entry/exit funciones top-level** | `login_full`, `fill_card_adyen`, `pick_account` |

### Qué NO se loggea (prohibido)

| Categoría | Razón |
|-----------|-------|
| **Secretos** | Passwords, OTPs, CVVs, tokens completos, full PII |
| **Cada iteración de polling** | Solo entry/exit del loop |
| **Resultado de cada JS evaluate** | Sólo si afecta decisión del bot |
| **Movimientos de mouse intermedios** | `human_motion` interno no se observa |
| **Eventos de keystroke timing** | TM analiza estos — instrumentación los altera |
| **Estado de IMAP madre poll interno** | Solo el outcome del recovery |
| **DOM partial scans en watchers** | Solo el match final |

### Shadow vs Legacy

| Origen | Stream stdout legacy | JSONL legacy (`structured_slot<N>.jsonl`) | JSONL shadow (`shadow_core_*.jsonl`) |
|--------|:--------------------:|:-----------------------------------------:|:------------------------------------:|
| `log(slot, ...)` del bot | ✅ | ✅ | ✅ (si `CORE_SHADOW_LOG=1`) |
| `StructuredLogger.event(...)` | ✅ | ✅ | ✅ (si `CORE_SHADOW_LOG=1`) |
| `log_event(...)` directo | ✅ | ✅ | ✅ (si `CORE_SHADOW_LOG=1`) |
| Hooks SAFE_HOOK (futuros) | ❌ | ❌ | ✅ (si `CORE_SHADOW_LOG=1`) |

**Regla**: shadow es **espejo + extensión** del legacy. Legacy sigue siendo la verdad. Shadow añade observabilidad opcional.

---

## 5. ZONAS PROHIBIDAS DE INSTRUMENTACIÓN

### Lista cerrada (extiende F1-F10 del SHADOW_HOOKS_PROPOSAL.md)

| # | Zona | Razón |
|---|------|-------|
| **F1** | Dentro de `page.evaluate(...)` JS | JS no puede llamar al bridge Python |
| **F2** | Entre `email_typed` y `pwd_typed` | TM analiza keystroke timing |
| **F3** | Dentro de retry loops por iteración | Solo entry/exit del loop completo |
| **F4** | `_persist_bot_block` interior | Función crítica de blacklist |
| **F5** | `_acquire_pick_mutex` / `_release_pick_mutex` interior | Lock cross-process — I/O extra aumenta contention |
| **F6** | `watch_for_checkout` polling loop interior | Bridge ya emite por cada `log()` existente |
| **F7** | `finally` blocks de cleanup | Ya hay `log()` ahí; no duplicar |
| **F8** | `imap_otp` / `recover_inline` paths críticos | IMAP timing sensible a race con TM |
| **F9** | `fill_card_adyen` iframe polling (20 × 500ms) | Saturaría JSONL con 20 líneas inútiles |
| **F10** | Entre `dispatchEvent('input')` y siguiente acción | React event ordering |
| **F11** | Después de `getOwnPropertyDescriptor(...).set` | Native setter calls — orden React-critical |
| **F12** | Dentro del bloque que detecta 3DS modal | TM observa polling JS aquí |
| **F13** | Bloques que generan `outcome.json` final | Atómico — un solo log al final |
| **F14** | El propio `log()` legacy y `_compra_logger.log_event` | Auto-recursión |
| **F15** | El propio `log_bridge` interior | Auto-recursión + romper contrato silent |

---

## 6. ESTRATEGIA DE VALIDACIÓN DE COHERENCIA

### A) Schema validator (futuro — `core/observability/schema.py`)

```python
VALID_DOMAINS = {"runtime", "profile", "session", "navigation",
                 "auth", "cart", "checkout", "payment",
                 "antibot", "telemetry"}
VALID_OUTCOMES = {"ok", "err", "timeout", "blocked", "skipped", "retry"}
VALID_LEVELS = {"debug", "info", "warn", "error", "fatal"}

def validate_event(event_name: str, level: str) -> tuple[bool, str]:
    """Devuelve (ok, error_msg). True si cumple schema."""
    if level not in VALID_LEVELS: return False, f"bad level: {level}"
    parts = event_name.split(".")
    if not 2 <= len(parts) <= 3: return False, "expected 2-3 dot-separated parts"
    domain, action, *outcome = parts
    if domain not in VALID_DOMAINS: return False, f"unknown domain: {domain}"
    if not action.replace("_", "").isalnum(): return False, "action must be alphanumeric+_"
    if outcome and outcome[0] not in VALID_OUTCOMES:
        return False, f"unknown outcome: {outcome[0]}"
    return True, ""
```

### B) State machine validator (post-run)

```python
def validate_state_transitions(jsonl_path: Path) -> dict:
    """Recorre JSONL en orden y verifica que las transiciones
    state.transition siguen la matriz de TRANSITION_MATRIX."""
    # Detecta:
    # - eventos de un dominio en estado incorrecto (ej: payment.* en BROWSING)
    # - transiciones no permitidas
    # - fatales sin EXIT subsiguiente
```

### C) Coverage tests (`tests/test_observability_coverage.py`)

```python
def test_each_flow_step_emits_required_events():
    """Smoke E2E que recorre flow completo y verifica que cada
    state transition de la matriz emite su evento canónico."""
    expected_events = [
        "runtime.started",
        "profile.created",
        "session.cdp_attached",
        "session.eps_sid.detected",
        # ... etc
        "runtime.ready_for_user",
    ]
    # ejecutar bot, leer JSONL, assert presencia
```

### D) Linter de eventos nuevos (pre-commit hook futuro)

```bash
# scripts/check_event_naming.py
# - Grep todos los `log(slot, "..."` y `log_event(slot, "..."`
# - Validar contra schema
# - FAIL build si alguno no cumple
```

### E) Métricas de salud del logging

| Métrica | Cómo medir | Umbral aceptable |
|---------|-----------|------------------|
| Cobertura state transitions | % de transiciones que emiten evento canónico | 100% |
| Ratio eventos sin domain válido | conteo / total | 0% |
| Ratio FATAL sin EXIT | conteo / total fatales | 0% |
| Duplicación de eventos por acción | misma acción 2× consecutivas | < 1% |
| Eventos UPPERCASE residuales | conteo | 0 (post-migración) |

---

## 7. PLAN DE EJECUCIÓN (cuando se decida implementar)

| Fase | Acción | Riesgo |
|------|--------|--------|
| **0** (HOY) | Diseño documentado | nulo |
| **1** | `core/observability/schema.py` con validator + lista cerrada | bajo |
| **2** | `tests/test_event_schema.py` valida 231 actuales contra schema (revela 100% incumplimiento esperado) | bajo |
| **3** | Mapping table en `core/observability/event_map.py`: legacy_name → canonical_name | bajo |
| **4** | Wrapper en `log_event` que traduce legacy_name → canonical antes de emitir JSONL | bajo |
| **5** | Hooks SAFE_HOOK ya usan canonical names desde el inicio | bajo |
| **6** | Después de N sesiones estable, refactor de los 231 names en `_compra_parallel.py` | medio |
| **7** | Deprecar legacy_name (warning logger si se usa) | bajo |
| **8** | Eliminar mapping table cuando 100% migrado | bajo |

**NO se ejecuta nada hasta confirmar este diseño.**

---

## 8. PREGUNTAS PENDIENTES PARA ITERAR

Antes de implementar Fase 1+, decidir:

1. **¿La taxonomía 10-domain es suficiente?** ¿Falta algún área crítica? Candidatos descartados: `metrics`, `cleanup` (subsumidos por `telemetry` y dominio específico).

2. **¿`state.transition` debe ser evento separado o atributo en cada evento?**
   - Opción A: 1 evento dedicado `runtime.state.transition` con `from`/`to`. Limpio en JSONL.
   - Opción B: cada evento incluye `state` en el payload. Más verbose pero auto-correlable.
   - **Mi recomendación**: AMBAS — emitir transition como evento + incluir `state` en payload de todos los eventos siguientes hasta la próxima transition.

3. **¿Permitimos extensibilidad de outcomes?**
   - Actual: {`ok`, `err`, `timeout`, `blocked`, `skipped`, `retry`} (6).
   - ¿Falta `partial`, `degraded`, `recovered`?

4. **¿Eventos de severity `debug` se emiten siempre?**
   - Opción A: solo si `LOG_LEVEL=debug` env var.
   - Opción B: siempre a JSONL, nunca a stdout.
   - **Recomendación**: B — JSONL siempre, stdout solo nivel ≥ info.

5. **¿`shadow_core_*.jsonl` debe seguir el schema canónico desde el inicio?**
   - Si SÍ: el bridge debe validar antes de escribir.
   - Si NO: el bridge es transparent pipe (validación delayed).
   - **Recomendación**: NO al inicio — bridge sigue capturando lo que el legacy emite. Validación en post-process (`tests/test_event_schema.py`).

---

## 9. CHECKLIST PRE-IMPLEMENTACIÓN

Antes de aplicar Fase 1, confirmar:

- [ ] Lista de 10 domains aprobada
- [ ] State machine de 8 estados aprobada
- [ ] 6 outcomes enum aprobado
- [ ] 5 niveles de severity aprobados
- [ ] Reglas de payload aprobadas
- [ ] 15 zonas prohibidas (F1-F15) aprobadas
- [ ] Estrategia de migración (legacy → canonical via mapping) aprobada
- [ ] Decisión sobre las 5 preguntas pendientes (sección 8)

Cuando estos checkboxes estén ✅, procedemos con la implementación incremental.

---

## RESUMEN EJECUTIVO

| Pieza del diseño | Definida en |
|------------------|-------------|
| Taxonomía | Sección 1 (10 domains, 6 outcomes, schema `domain.action[.outcome]`) |
| State machine | Sección 2 (8 estados, matriz de transiciones, mapeo eventos→estados) |
| Convención naming | Sección 3 (5 levels, reglas snake_case, payload estándar) |
| Reglas instrumentación | Sección 4 (qué SÍ / qué NO, shadow vs legacy) |
| Zonas prohibidas | Sección 5 (15 zonas F1-F15) |
| Validación coherencia | Sección 6 (schema validator + state machine validator + coverage tests + linter + métricas) |
| Plan ejecución | Sección 7 (8 fases incrementales) |
| Decisiones pendientes | Sección 8 (5 preguntas) |

**NO se ha tocado código.** Esperando tu OK al diseño antes de continuar.
