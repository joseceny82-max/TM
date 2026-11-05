# Shadow Hooks — Propuesta de inserción segura

**Archivo objetivo**: `_compra_parallel.py` (3731 LOC)
**Fecha**: 2026-05-13
**Fase**: 3.5 — observabilidad pasiva (extiende FASE 3 shadow bridge)

---

## CONTEXTO IMPORTANTE

El bridge ya inyectado en `_compra_logger.log_event` (FASE 3 anterior) significa
que **TODOS los `log()` actuales** de `_compra_parallel.py` ya emiten al shadow
JSONL cuando `CORE_SHADOW_LOG=1`. Eso es ~245 puntos de telemetría gratis.

Por tanto, esta propuesta **NO duplica** lo ya cubierto. Solo añade hooks en
sitios donde **NO hay `log()` actualmente** pero hay un evento observable
crítico: entry/exit de funciones grandes, `except: pass` que silencian estado
del sistema, y argumentos/entorno al inicio.

---

## PATRÓN SAFE_HOOK ESTÁNDAR

```python
# SAFE SHADOW HOOK (non-blocking, opt-in via env)
try:
    from core.observability.logger import log_bridge as _shadow
    _shadow(slot=slot, event="<event_name>", <kwargs>=<...>)
except Exception:
    pass
```

### Reglas obligatorias

| Regla | Cómo se cumple |
|-------|----------------|
| Nunca lanza excepción | `try/except: pass` envolviendo TODO |
| Nunca bloquea | `log_bridge` no hace I/O síncrono pesado (escribe línea append) |
| Nunca afecta return values | Hook DESPUÉS de la línea `return`/asignación si aplica |
| Siempre opcional | `log_bridge` retorna sin efecto si `CORE_SHADOW_LOG != "1"` |
| Cero side-effects en `kwargs` | Solo copiar referencias, no llamar métodos costosos |
| Lazy import | `from core...` DENTRO del try, no top-level |

### Variante para post-exit (después de `return`)

```python
# Para hooks de EXIT (después de calcular return value pero antes del return)
_result = ...  # cálculo normal del return
try:
    from core.observability.logger import log_bridge as _shadow
    _shadow(slot=slot, event="<func>_exit", returned=bool(_result))
except Exception:
    pass
return _result
```

---

## 12 HOOKS PROPUESTOS (ordenados por seguridad/utilidad)

### A) Entry / startup (3 hooks)

| # | Hook | Ubicación | Evento | Por qué es seguro |
|---|------|-----------|--------|-------------------|
| **H1** | `main()` args completos | `_compra_parallel.py:1922` (después del `log(slot, "init", ...)`) | `main_args_full` con `mode`, `sections`, `allow_separated`, `confirm`, `engine`, `headless` | El `init` actual solo registra 3 campos. Añadir hook con el resto NO altera el flow (es solo más logging). |
| **H2** | Entorno y engine flags | `_compra_parallel.py:1921` (justo tras `slot = args.slot`) | `runtime_env` con `human_motion_available=_HUMAN_MOTION`, `trace_auth_enabled=os.environ.get("TRACE_AUTH")`, `engine=args.engine` | Lecturas pasivas de globals/env. Cero side effects. |
| **H3** | Outcome reset confirmation | `_compra_parallel.py:1916` (dentro del `except: pass` del outcome reset) | `outcome_reset_err` con `err=str(e)[:80]` | Convierte un silenciador a observador. NO cambia el `pass` semánticamente. |

### B) Exit de funciones top-level (5 hooks)

| # | Hook | Ubicación | Evento | Por qué es seguro |
|---|------|-----------|--------|-------------------|
| **H4** | `login_full()` exit | `_compra_parallel.py` justo antes de cada `return True`/`return False` de `login_full` | `login_full_exit` con `returned=<bool>`, `email=account.get('tm_email')` | Hook DESPUÉS del cálculo, antes del return. No altera flow. |
| **H5** | `fill_card()` exit | similar (cerca del último return) | `fill_card_exit` con `returned=<bool>` | Idem |
| **H6** | `fill_card_adyen()` exit | similar | `fill_card_adyen_exit` con `returned=<bool>` | Idem |
| **H7** | `pick_morelogin_profile()` exit | en `pick_morelogin_profile`, justo antes del `return prof` | `profile_picked_final` con `profile=prof.get('profile')`, `sessionid=prof.get('sessionid')`, `auto_created=prof.get('auto_created')` | Hook lee `prof` después de elegirlo. No modifica. |
| **H8** | `login_with_account_swap()` exit | justo antes del último `return result_dict` | `login_swap_exit` con `ok=result.get('ok')`, `swaps=result.get('swaps')`, `reason=result.get('reason')` | El dict ya está calculado, sólo se observa. |

### C) `except: pass` que silencian estado del sistema (4 hooks)

Estos son los 4 `except: pass` más críticos del catálogo que silencian estado
útil. Convertir a observador NO altera el flow porque el `pass` se mantiene.

| # | Hook | Ubicación | Evento | Por qué es seguro |
|---|------|-----------|--------|-------------------|
| **H9** | scroll-into-view en `handle_separated_seats` | `_compra_parallel.py:194` (dentro del `except`) | `separated_seats_scroll_err` con `err=str(e)[:80]` | El `pass` permanece. Solo añadimos visibilidad. |
| **H10** | `requests.post(env/close)` en profile_start retry | `_compra_parallel.py:2057` (dentro del `except`) | `profile_start_retry_close_err` con `err=str(e)[:80]`, `env_id=prof.get('env_id')` | Idem. Crítico para detectar profile leaks. |
| **H11** | `register_account.burn` en profile retry | `_compra_parallel.py:2067` (dentro del `except`) | `profile_burn_persist_err` con `err=str(e)[:80]`, `profile=prof.get('profile')` | Idem. Detecta si burns se pierden. |
| **H12** | `_bad_credentials.json` write | `_compra_parallel.py:510` (en `_mark_bad_credentials`) | `bad_creds_persist_err` con `err=str(e)[:80]` | Telemetría persiste = si falla, queremos saberlo. |

---

## REFACTOR DE PATRÓN (cero cambio, solo conversión)

### Antes (H9 ejemplo)

```python
try:
    ...
    page.wait_for_timeout(400)
except Exception: pass
```

### Después (mismo comportamiento + observabilidad)

```python
try:
    ...
    page.wait_for_timeout(400)
except Exception as _e:
    # SAFE SHADOW HOOK — pass se preserva, solo añadimos visibilidad
    try:
        from core.observability.logger import log_bridge as _shadow
        _shadow(slot=slot, event="separated_seats_scroll_err", err=str(_e)[:80])
    except Exception:
        pass
    # comportamiento original (pass implícito tras el hook):
```

**Cambio neto**: 5 líneas añadidas dentro del except. El `pass` queda implícito
al terminar el bloque. Flow del programa idéntico.

---

## ZONAS PROHIBIDAS — NO INSTRUMENTAR

Estas zonas determinan **decisiones críticas del bot**. Añadir hooks puede:
- Cambiar timing por μs (afecta race conditions)
- Interferir con observers de TM (Akamai monitorea timing entre eventos JS)
- Romper retry semantics si el hook acaba escribiendo en stdout por accidente

| # | Zona | Razón |
|---|------|-------|
| **F1** | DENTRO de `page.evaluate(...)` JavaScript blocks | No tiene sentido — el JS no puede llamar al bridge. Pero tampoco hay que envolver el evaluate. |
| **F2** | DENTRO de `login_full` después de `email_typed` y antes de `pwd_typed` | Window donde TM analiza keystroke timing. Hook puede añadir μs. |
| **F3** | DENTRO de retry loops (`for retry in range(RETRY_PROFILE_PICK)`, etc.) | El loop tiene timing implícito; los hooks en cada iteración pueden saturar shadow JSONL. Solo hookear el ENTRY/EXIT del loop, no cada iteración. |
| **F4** | `_persist_bot_block(...)` interior | Función que escribe estado crítico de blacklist. Si falla el hook (que no debería), no queremos confusión. Hook SOLO en su entry. |
| **F5** | `_acquire_pick_mutex` / `_release_pick_mutex` interior | Lock cross-process; añadir cualquier I/O extra puede aumentar contention. |
| **F6** | `watch_for_checkout` polling loop interior | Bucle que detecta URL_CHANGED; el bridge ya emite por cada `log()`. NO hookear cada iter. |
| **F7** | Bloque `finally` de cleanup (history_cleanup, profile_close) | Ya hay `log()` ahí. NO duplicar. |
| **F8** | Cualquier path crítico de `imap_otp` / `account_recovery.recover_inline` | Recovery flow tiene IMAP I/O sensible a timing. Hooks aumentan riesgo de race con IMAP. |
| **F9** | `fill_card_adyen` iframe polling | Polling cada 500ms × 20 reintentos. Hookear cada iter = 20 líneas JSONL extra. |
| **F10** | `dispatchEvent('input'/'change')` en native setters | El navegador procesa eventos; hook entre dispatch y siguiente paso puede mover orden React. |

---

## ESTRATEGIA DE VALIDACIÓN — REGRESIÓN CERO

### Paso 1: Pre-instrumentación (baseline)

```bash
# Capturar baseline con shadow OFF
unset CORE_SHADOW_LOG
python _compra_parallel.py --slot 1 --event-url ... \
  --engine morelogin --offer-idx 3 --qty 2 > runs/baseline_stdout.log 2>&1

# Hash del stdout (sin timestamps que cambian) para diff posterior
grep -v "^\[slot1\]\[" runs/baseline_stdout.log | sha256sum > runs/baseline_hash.txt
```

### Paso 2: Aplicar hooks (12 hooks puntuales)

### Paso 3: Post-instrumentación con shadow OFF (debe ser IDÉNTICO al baseline)

```bash
unset CORE_SHADOW_LOG  # OFF
python _compra_parallel.py --slot 1 --event-url ... > runs/post_off.log 2>&1

grep -v "^\[slot1\]\[" runs/post_off.log | sha256sum > runs/post_off_hash.txt

# Comparar
diff runs/baseline_hash.txt runs/post_off_hash.txt
# Esperado: IDÉNTICOS — el hook no debe cambiar nada cuando OFF
```

### Paso 4: Post-instrumentación con shadow ON (stdout debe seguir igual)

```bash
CORE_SHADOW_LOG=1 python _compra_parallel.py --slot 1 --event-url ... > runs/post_on.log 2>&1

grep -v "^\[slot1\]\[" runs/post_on.log | sha256sum > runs/post_on_hash.txt

# Comparar stdout (no debería cambiar)
diff runs/baseline_hash.txt runs/post_on_hash.txt
# Esperado: IDÉNTICOS — el shadow escribe a JSONL, no a stdout

# Verificar shadow JSONL existe y tiene los 12 hooks nuevos
ls -la runs/shadow_core_*.jsonl
grep -c "main_args_full\|runtime_env\|profile_picked_final\|login_swap_exit" \
  runs/shadow_core_*.jsonl
# Esperado: ≥4 (al menos los hooks llamados en el flow)
```

### Paso 5: Métricas comparativas

| Métrica | Baseline | Post OFF | Post ON | Esperado |
|---------|---------|----------|---------|----------|
| Tiempo total run | T₀ | T₀ ± 0.1s | T₀ ± 0.3s | < +1% |
| stdout líneas | N | N | N | IDÉNTICO |
| stdout hash | H | H | H | IDÉNTICO |
| Exit code | RC | RC | RC | IDÉNTICO |
| Eventos JSONL legacy (`structured_slot1.jsonl`) | M | M | M | IDÉNTICO |
| Eventos JSONL shadow (`shadow_core_*.jsonl`) | 0 | 0 | M + 12 | M+12 |
| Screenshots generados | S | S | S | IDÉNTICO |

### Test automático

```python
# tests/test_hooks_no_regression.py (FUTURO — placeholder)
def test_hook_off_produces_identical_stdout(...):
    """Stdout del bot con shadow OFF debe ser bit-exacto al pre-instrumentación."""
    ...

def test_hook_on_does_not_pollute_stdout(...):
    """Stdout del bot con shadow ON solo añade líneas en shadow JSONL."""
    ...
```

---

## PLAN DE EJECUCIÓN PROPUESTO

| Sub-fase | Acción | LOC añadidas | Riesgo |
|----------|--------|------------:|--------|
| **3.5.a** | Aplicar H1-H3 (entry/startup) | +15 | bajo |
| **3.5.b** | Validación: hash stdout idéntico | 0 | nulo |
| **3.5.c** | Aplicar H4-H8 (exit functions) | +25 | bajo |
| **3.5.d** | Validación: hash stdout idéntico | 0 | nulo |
| **3.5.e** | Aplicar H9-H12 (except: pass → observer) | +20 | bajo-medio |
| **3.5.f** | Validación final + smoke E2E | 0 | nulo |
| **3.5.g** | Commit | +60 LOC totales | bajo |

**Total**: ~60 LOC añadidas. Cero modificación de lógica.

---

## ANTI-PATRONES EXPLÍCITOS

NO HACER en esta fase:

1. **NO añadir hook dentro de `try` de operación crítica** — solo en `except` o post-action.
2. **NO emitir hooks dentro de loops por iteración** — solo entry/exit del loop completo.
3. **NO añadir parámetros nuevos** a funciones existentes para "pasar contexto al hook".
4. **NO refactorizar `except: pass`** para extraer la excepción si no se va a usar fuera del hook.
5. **NO añadir cálculos** dentro del `try` del hook (lazy: solo lectura de variables ya existentes).
6. **NO depender del orden de hooks** — cada uno debe ser independiente.
7. **NO añadir `import` top-level** de `core.observability.logger` (lazy import dentro del try).

---

## RESULTADO ESPERADO

Tras aplicar los 12 hooks:

- **Cero cambios en comportamiento** del bot (stdout idéntico, timing idéntico, exit codes idénticos)
- **Cobertura observabilidad** extendida en 12 puntos NUEVOS además de los ~245 actuales
- **Cero líneas eliminadas** del legacy
- **Reversible**: borrar las 12 zonas con `git revert` o regex
- **Auditable**: cada hook tiene un `event_name` único en `runs/shadow_core_*.jsonl`

---

## NO EJECUTADO TODAVÍA

Esta propuesta es **SOLO el mapa** según pediste. No he aplicado ningún hook
todavía. Espero tu OK explícito para proceder con la sub-fase 3.5.a (H1-H3),
validar, y continuar incremental.
