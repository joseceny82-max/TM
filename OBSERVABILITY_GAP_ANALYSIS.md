# Gap Analysis — Modelo vs Realidad del bot

**Fecha**: 2026-05-13
**Método**: análisis programático de los 231 eventos de `_compra_parallel.py`
**Output del análisis**: `/tmp/gap_analysis.txt`
**Decisión final**: 🟡 **AJUSTAR** (simplificación recomendada — NO validado puro, NO rechazar)

---

## 1. GAP ANALYSIS — Cuántos encajan en la taxonomía

### Distribución empírica por domain (heurística keyword-match)

| Domain propuesto | Eventos | % | Comentario |
|------------------|--------:|--:|------------|
| `runtime` | 7 | 3.0% | OK (mínimo justificado) |
| `profile` | 15 | 6.5% | OK |
| `session` | 13 | 5.6% | OK pero solapa con profile/navigation |
| `navigation` | 7 | 3.0% | **escaso** — solapa con session |
| `auth` | 36 | 15.6% | OK (incluye recovery) |
| `cart` | 34 | 14.7% | OK |
| `checkout` | 26 | 11.3% | solapa con payment |
| `payment` | 24 | 10.4% | solapa con checkout |
| `antibot` | 6 | 2.6% | **escaso** |
| `telemetry` | 14 | 6.1% | OK |
| **`unknown`** | **35** | **15.2%** | 🔴 **no encajan claramente** |
| `fatal-prefix` | 14 | 6.1% | UPPERCASE legacy, debería mapear por level |
| **TOTAL** | 231 | 100% | |

**Coverage real de la taxonomía 10-domain**: 196/231 = **84.8%**
**Eventos que NO encajan**: 35 unknown + 14 fatal-prefix = **49 eventos (21.2%)**

### Los 35 `unknown` (sin domain claro)

```
auth_detected_mid              ← entre auth y navigation
camoufox_close_err             ← ¿profile o session?
camoufox_closed                ← idem
camoufox_explicit_cleanup_err  ← cleanup = telemetry o exit?
camoufox_explicit_cleanup_ok
camoufox_launching             ← ¿profile o session?
cookies_accepted               ← ¿navigation o session?
cookies_force_close
cookies_force_close_err
cookies_post_hydration_*  (×4) ← navigation post-goto
history_files_purge_err        ← exit/cleanup
history_files_purged
hydration_done                 ← navigation
imap_err                       ← auth recovery (interno IMAP)
imap_poll
modal_handler_err              ← genérico
no_more_accounts_to_try        ← auth o runtime
otp_* (×5)                     ← auth recovery (IMAP/SMS)
payments_iframe_ready          ← payment (¿pero el heurístico no lo pilló?)
payments_iframe_timeout
pick_mutex_timeout             ← runtime concurrency
pick_mutex_timeout_after_swap
pick_retry
pre_flight_FAIL_abort          ← antibot
pre_flight_http
pre_flight_skipped_err
storage_swap_err               ← session
watch_err                      ← navigation
```

**Hallazgo**: 35 eventos casi todos son **legítimos pero requieren keywords nuevos** en el clasificador. NO son "huérfanos sin domain", son "huérfanos por heurística simple".

### Los 28 eventos **AMBIGUOS** (matchean 2+ domains)

```
FATAL_no_profile                    → profile, runtime
FATAL_no_sector_clickable           → cart, runtime
auto_profile_destroyed_after_pf     → profile, telemetry
blockers_detected_post_continuar    → antibot, cart
bot_block_persisted                 → antibot, telemetry
email_continuar_clicked             → auth, cart       ← repetido 4× pattern
fill_card_data                      → checkout, payment ← repetido 4× pattern
guardar_continuar_clicked           → cart, checkout
morelogin_fresh_*                   → auth, profile     ← repetido 3× pattern
post_continuar_url                  → cart, navigation
post_recovery_*                     → auth, cart        ← repetido 4× pattern
profile_auto_burned_after_use       → profile, telemetry
pwd_continuar_clicked               → auth, cart
recovery_persisted                  → auth, telemetry
```

**Hallazgo**: muchos eventos son **transversales por naturaleza**. `email_continuar_clicked` es legítimamente `auth` (es el botón Continuar dentro del form de login) pero la palabra "continuar" también significa cart-flow.

---

## 2. PROBLEMAS DEL DISEÑO ACTUAL

### A) Overfitting: 97% de eventos son one-shot

```
One-shot events (1 sola llamada):  224 (97.0%)
Multi-call events (2+):              7 (3.0%)
```

Si solo 7 eventos se repiten (cada uno 2 veces), **los 231 nombres son casi todos descriptores únicos**, no clases reutilizables. Crear taxonomía con 10 domains × N actions × 6 outcomes = **600+ combinaciones posibles** para 231 eventos reales (38% utilización máxima).

**Conclusión**: la taxonomía es excesiva para el volumen real. Mejor menos domains, más actions.

### B) Estados con muy pocos eventos (granularidad excesiva)

| Estado | Eventos | Decisión sugerida |
|--------|--------:|-------------------|
| **READY** | 1 | 🔴 **eliminar** — fusionar con CHECKOUT como sub-evento `purchase.ready` |
| **INIT** | 2 | 🟡 fusionar con PROFILE_SETUP → SETUP |
| **RECOVERY** | 22 | 🟡 fusionar con AUTH (es siempre sub-flow de AUTH) |
| **PAYMENT** | 26 | 🟡 fusionar con CHECKOUT (continuación natural) |
| **SETUP+INIT** | 33 | ok como SETUP |
| **BROWSING** | 42 | ✅ mantener |
| **AUTH+RECOVERY** | 43 | ok como LOGIN |
| **CHECKOUT+PAYMENT** | 49 | ok como CHECKOUT |
| **EXIT** | 16 | ✅ mantener |
| **CROSS** | 32 | 🟡 estos NO son un estado — son cross-cutting (shots, FATAL_, bot_block, TRACE_AUTH). Manejar como `domain=telemetry` o `domain=antibot` independientes del estado |
| **UNMAPPED** | 15 | revisar uno a uno |

**8 estados → 5 estados** mantiene el 95% del flow representado.

### C) Eventos UPPERCASE no encajan en domains

22 eventos (10%) usan UPPERCASE como severidad implícita:
- 14 `FATAL_*`
- 1 `READY_FOR_USER`
- 1 `URL_CHANGED`
- 4 `TRACE_AUTH_*`
- 1 `BAD_CREDENTIALS_detected`
- 1 `TIMEOUT_no_checkout`

**El diseño actual los mete en `fatal-prefix`**, que es categoría falsa (UPPERCASE no es un domain — es un level).

### D) Solapamiento real entre dominios

| Solapamiento | Ejemplos | Por qué pasa |
|--------------|----------|--------------|
| `profile` ↔ `session` | `cdp_connected_ok`, `storage_state_loaded` | session lifecycle es PARTE de profile lifecycle |
| `navigation` ↔ `session` | `goto`, `warming_home_done` | navigation es 1 paso dentro de session |
| `cart` ↔ `checkout` | `guardar_continuar_clicked` | botón Continuar aparece en ambos contextos |
| `checkout` ↔ `payment` | `fill_card_data` | "card" es ambiguo (dirección vs tarjeta) |
| `auth` ↔ `cart` | `email_continuar_clicked` | botón "Continuar" del email está en form de auth, pero el verbo "continuar" sugiere cart |
| `auth` ↔ `profile` | `morelogin_fresh_created` | profile pertenece a auth/identity context |

**6 pares solapados** → demasiados domains adyacentes.

### E) Outcomes propuestos NO se usan en realidad

Outcomes propuestos: {`ok`, `err`, `timeout`, `blocked`, `skipped`, `retry`}

Realidad en código:
| Sufijo | Conteo aprox | Mapeo natural |
|--------|------------:|---------------|
| `_err` | ~46 | err |
| `_clicked` | 29 | ok (sin outcome explícito) |
| `_done` | ~10 | ok |
| `_ok` | ~8 | ok |
| `_failed` | 5 | err |
| `_skipped` | 5 | skipped |
| `_detected` | varias | (no es outcome — es action) |
| `_not_found` / `_NOT_FOUND` | 2 | err |
| `_timeout` | 1 (`TIMEOUT_no_checkout`) | err |
| `_blocked` | 0 | — |
| `_retry` | 0 (sí hay `_retry` pero como action, no outcome) | — |

**Hallazgo**: `timeout`, `blocked`, `retry` como outcomes están **infrautilizados**. Mejor 3 outcomes que 6.

---

## 3. PROPUESTA DE SIMPLIFICACIÓN

### Cambio 1: 10 domains → 6 domains

| Antes (10) | Después (6) | Eventos mapeados | Justificación |
|------------|-------------|-----------------:|---------------|
| `runtime` + READY events + EXIT | **`runtime`** | 24 | lifecycle proceso |
| `profile` + `session` + `navigation` | **`browser`** | 35 | gestión del browser context unificada |
| `auth` + RECOVERY events | **`auth`** | 58 | login + recovery + account swap |
| `cart` + `checkout` + `payment` | **`purchase`** | 84 | flow de compra completo |
| `antibot` | **`antibot`** | 6 | bloqueos / EPS / Akamai |
| `telemetry` + screenshots + cleanup persist | **`telemetry`** | 14 | observabilidad + persistence |
| — | — | 10 unmapped revisar | |

**Coverage**: 221/231 = **95.7%** (mejora vs 84.8% inicial).

### Cambio 2: 8 estados → 5 estados

```
ANTES:  INIT → PROFILE_SETUP → BROWSING → AUTH ⇄ RECOVERY → CHECKOUT → PAYMENT → READY → EXIT

DESPUÉS:        SETUP    →    BROWSING   →    LOGIN    →    CHECKOUT    →    EXIT
              (INIT +              (incluye         (incluye          (READY como
              PROFILE_SETUP)        RECOVERY)        PAYMENT)         sub-evento)
```

| Estado | Eventos | Cobertura |
|--------|--------:|----------:|
| **SETUP** | 33 | 14.3% |
| **BROWSING** | 42 | 18.2% |
| **LOGIN** | 43 | 18.6% |
| **CHECKOUT** | 49 | 21.2% |
| **EXIT** | 16 | 6.9% |
| **CROSS** (no es estado) | 32 | 13.9% — manejar como dominio independiente |
| Sin clasificar | 16 | 6.9% — revisar |

5 estados cubren 76% directo, +14% CROSS independiente = **90%**.

### Cambio 3: 6 outcomes → 3 outcomes

| Antes (6) | Después (3) | Mapeo |
|-----------|-------------|-------|
| `ok` | `ok` (implícito si no hay sufijo) | mantener |
| `err`, `timeout`, `blocked` | **`err`** | fusionar — la causa va al payload (`error_type`, `timeout_after_ms`) |
| `skipped` | **`skipped`** | mantener |
| `retry` | — eliminar como outcome | usar como action en payload (`attempt=N`) |

3 outcomes simples + payload rico = lo mismo con menos schema.

### Cambio 4: Eliminar UPPERCASE

22 eventos UPPERCASE migran a:

| Legacy | Canonical | Level |
|--------|-----------|-------|
| `FATAL_bot_block_detected` | `antibot.block.detected` | `fatal` |
| `FATAL_no_profile` | `runtime.no_profile` | `fatal` |
| `FATAL_no_account` | `runtime.no_account` | `fatal` |
| `READY_FOR_USER` | `purchase.ready` | `info` |
| `URL_CHANGED` | `browser.url.changed` | `info` |
| `TRACE_AUTH_*` | `telemetry.trace.*` | `debug` |
| `BAD_CREDENTIALS_detected` | `auth.credentials.invalid` | `error` |
| `TIMEOUT_no_checkout` | `purchase.checkout.timeout` (level=`error`) | `error` |

UPPERCASE se elimina al 100%.

---

## 4. QUÉ ES ESTABLE (confirmar como baseline)

✅ **Schema general** `domain.action[.outcome]` snake_case dot-separated — **valida con 95.7%**.
✅ **Niveles explícitos** (5: debug/info/warn/error/fatal) en lugar de UPPERCASE.
✅ **Payload estándar** con campos comunes (slot, state, account_id, profile_id, sessionid, elapsed_ms, err, error_type).
✅ **Zonas prohibidas F1-F15** — basadas en código real, no en teoría.
✅ **Mapping table approach** para migración legacy → canonical sin breaking changes.
✅ **Shadow vs Legacy stream separation** (stdout / JSONL legacy / JSONL shadow).

❌ **Inestable / reducir**:
- 10 domains → 6 (eliminar `profile/session/navigation` fusionados, `cart/checkout/payment` fusionados)
- 8 estados → 5 (eliminar INIT, RECOVERY, PAYMENT, READY como estados separados)
- 6 outcomes → 3 (fusionar timeout/blocked en err; eliminar retry como outcome)

---

## 5. DECISIÓN

🟡 **AJUSTAR** (no validar puro, no rechazar).

### Justificación

- Solo **84.8%** de eventos encaja en taxonomía 10-domain (objetivo: 95%+).
- **22 estados con <5 eventos** sugieren granularidad excesiva.
- **28 eventos ambiguos** (matchean 2+ domains) indican boundaries mal definidas.
- **97% one-shot** sugiere que la taxonomía estricta es overkill.

### Acciones propuestas (en orden de impacto/seguridad)

| # | Acción | Riesgo | Impacto |
|---|--------|--------|---------|
| 1 | **Reducir a 6 domains** (fusionar profile+session+navigation → browser; cart+checkout+payment → purchase) | bajo | alto: coverage sube a 95.7% |
| 2 | **Reducir a 5 estados** (eliminar INIT, RECOVERY, PAYMENT, READY) | bajo | alto: state machine usable |
| 3 | **Reducir a 3 outcomes** (ok/err/skipped) — info de causa al payload | bajo | medio: menos schema, mismo poder |
| 4 | **Eliminar UPPERCASE** mapeando los 22 al schema + level explícito | bajo | medio: consistencia visual |
| 5 | **Revisar los 15-35 unmapped** y decidir si caben en los 6 domains finales o requieren un domain 7 | bajo | bajo |

### Lo que NO se cambia

- Schema `domain.action[.outcome]`
- 5 levels
- Payload estándar
- 15 zonas prohibidas F1-F15
- Mapping legacy → canonical approach

### Próximo paso natural (cuando lo decidas)

Actualizar `OBSERVABILITY_DESIGN.md` con los 4 cambios anteriores y marcarlo como **v2 final**. Luego se procede a implementar Fase 1 (schema validator) con el modelo simplificado.

---

## RESUMEN EJECUTIVO

| Pregunta del user | Respuesta |
|-------------------|-----------|
| Cuántos de los 231 eventos encajan en la taxonomía | **196/231 = 84.8%** |
| Cuántos quedan fuera | **35 unknown + 14 UPPERCASE = 49 (21.2%)** |
| Dónde el modelo no representa la realidad | 6 pares de domains solapados, 22 UPPERCASE no clasificables, eventos cross-cutting sin home |
| Overfitting | SÍ — 97% one-shot, 600 combinaciones posibles para 231 eventos |
| Eventos redundantes | 14 pares ok/err con misma base (ej: `card_radio_clicked`+`card_radio_err`) — son legítimos |
| Domains mal definidos | profile vs session vs navigation; cart vs checkout vs payment |
| Estados innecesarios | INIT (2 evt), RECOVERY (sub-flow), PAYMENT (continuación), READY (1 evt) |
| Inconsistencias naming vs código | UPPERCASE vs snake_case mix; verbo↔sustantivo invertido |
| Simplificar | SÍ recomendado: 10→6 domains, 8→5 estados, 6→3 outcomes |
| Decisión final | 🟡 **AJUSTAR** con los 5 cambios propuestos |
