# Catálogo de `except Exception: pass` — `_compra_parallel.py`

**Generado**: 2026-05-13 02:42
**Total ocurrencias**: 62 (era 86 en audit inicial; bajó tras quick wins)

## Clasificación

| Categoría | Count | Política propuesta | Riesgo de cambio |
|-----------|-------|--------------------|--------------------|
| `unknown` (sin clasificar — examinar manual) | 24 | Revisar caso a caso | medio |
| `telemetry_log` | 13 | **Añadir `warn` log mínimo** sin elevar | **bajo** ← quick win |
| `ui_interaction` | 12 | OK silencio (intentos opcionales) | bajo si dejamos igual |
| `ui_cookies_banner` | 5 | OK silencio (banner puede no existir) | bajo |
| `timing` | 3 | OK silencio (sleep/wait_for_timeout) | bajo |
| `filesystem` | 3 | **Añadir log si rmlock falla** | medio |
| `cleanup` | 2 | OK silencio (finally) — pero log si falla | bajo |

## Política recomendada (no aplicar todavía)

### 1) **OPTIONAL STEALTH / UI** (silenciar OK)
Operaciones que **deben fallar silenciosamente** sin romper el flow:
- Cookies banner detection (puede no existir)
- Stealth init scripts (degradación gracious)
- Hover/mouse_move opcional
- Scroll opcional

→ **Mantener `except: pass`**. Documentar con comentario `# optional, silent`.

### 2) **TELEMETRY** (warn log mínimo)
Persistencia de estado, write_outcome, save JSON, log calls:
- L465 `_save_bad_credentials` — si write falla, perdemos estado
- L1865 `write_outcome` cleanup
- L2940 queue-it telemetry (YA FIXED por bug it.get)
- L3030 telemetry post-recovery

→ **Cambiar a `except Exception as e: log_event("telemetry_err", error_type=...)`**.
   Throttle si el mismo error se repite >3 veces seguidas en 1 min.

### 3) **CLEANUP** (warn log mínimo)
Cierre de browser/profile/storage:
- L2011 `requests.post(env/close)`
- L3260+ `history_cleanup_finally`
- L3290 `bo._release_profile`

→ **Cambiar a `except Exception as e: log_event("cleanup_err", error_type=...)`**.
   No re-raise (estamos en finally).

### 4) **FILESYSTEM** (warn log)
Operaciones de lock / unlink:
- L1181 `os.unlink(_PROFILE_LOCK_MUTEX)` — race condition común
- L1209 `os.unlink(_PROFILE_LOCK_PATH)` — idem
- L1219 idem

→ **`except OSError as e: log_event("lock_unlink_err", err=str(e))`**.
   Diferenciar `FileNotFoundError` (esperado) de otros (warn).

### 5) **UNKNOWN** (revisar caso a caso — 24 ocurrencias)
Sin patrón claro. Tres acciones:
- Si el bloque es <5 LOC y operación claramente opcional → dejar como está.
- Si el bloque maneja datos críticos → añadir log warn.
- Si el bloque silencia errores de sintaxis JS → añadir log error.

## Ejemplos por categoría

### timing (L177) — DEJAR IGUAL
```python
page.evaluate("window.scrollTo({top: document.body.scrollHeight})")
page.wait_for_timeout(400)
except Exception: pass   # OK: scroll opcional, no afecta flow
```

### telemetry_log (L465) — MIGRAR A WARN
```python
# ANTES
try:
    with open(_BAD_CREDS_PATH, 'w', encoding='utf-8') as f:
        json.dump(list(bad), f)
except Exception: pass

# DESPUÉS (propuesta)
try:
    with open(_BAD_CREDS_PATH, 'w', encoding='utf-8') as f:
        json.dump(list(bad), f)
except Exception as e:
    log_event(slot, "bad_credentials_persist_err",
              level="warn", error_type=type(e).__name__,
              err=str(e)[:120])
```

### ui_interaction (L373) — DEJAR IGUAL
```python
const t = btns.find(b => /aceptar|continuar/i.test(b.innerText));
if (t) t.click();
page.wait_for_timeout(2500)
except Exception: pass   # OK: click opcional de modal que puede no existir
```

### cleanup (L2011) — MIGRAR A WARN
```python
# ANTES
try:
    requests.post(f'{bo.MORELOGIN_API}/api/env/close',
                  json={'envId': prof['env_id']}, timeout=10)
except Exception: pass

# DESPUÉS (propuesta)
try:
    requests.post(f'{bo.MORELOGIN_API}/api/env/close',
                  json={'envId': prof['env_id']}, timeout=10)
except Exception as e:
    log_event(slot, "morelogin_close_err",
              level="warn", error_type=type(e).__name__,
              env_id=prof['env_id'], err=str(e)[:120])
```

## Plan incremental (próximas sesiones)

**Sesión A** (low-risk, ~30 min):
- Migrar 13 `telemetry_log` (L465, L1865, L3030, L3045, etc.) a warn log.
- Riesgo: bajo (no cambia lógica, solo añade visibilidad).
- Validación: smoke test E2E + revisar `runs/structured_*.jsonl` para warnings nuevos.

**Sesión B** (medium-risk, ~45 min):
- Migrar 2 `cleanup` + 3 `filesystem` a warn log diferenciando errores esperados.
- Riesgo: medio (locks pueden generar ruido en pool 2+ slots).
- Validación: ejecutar pool de 2 slots y verificar no flood de warnings.

**Sesión C** (revisión manual, ~1h):
- Revisar las 24 `unknown` caso a caso.
- Riesgo: medio si encontramos bugs ocultos como QW8.
- Validación: regression test E2E.

**NO HACER**:
- Migración global automática con sed/regex — alto riesgo de romper comportamiento.
- Eliminar los `except: pass` de stealth/UI opcionales — son intencionales.

## Throttling de warns recomendado

Para evitar spam si el mismo error se repite:
```python
# Pseudo
class WarnThrottler:
    def __init__(self, max_per_min: int = 3):
        self._counts = {}  # error_type -> (count, window_start_ts)
    def should_emit(self, error_type: str) -> bool:
        # rolling window 60s
        ...
```

Añadir al `StructuredLogger` como método `warn_throttled(name, error_type, **kw)`.

---

**Conclusión**: NO se aplica refactor masivo automático. Catálogo listo para
acción incremental en sesiones siguientes (orden: telemetry → cleanup →
filesystem → unknown).
