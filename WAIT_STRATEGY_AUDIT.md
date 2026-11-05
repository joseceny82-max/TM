# Wait Strategy Audit — `_compra_parallel.py`

**Generado**: 2026-05-13 02:50
**Total waits/polls**: 122 ocurrencias
**Ratio event-driven**: **3.8%** (3 event-driven / 77 polling-based)

## Estado actual

| Pattern | Count | Categoría | Política |
|---------|------:|-----------|----------|
| `page.wait_for_timeout(N)` | **61** | sleep arbitrario | candidato migración |
| `human_pause(min, max)` | **33** | delay humano intencional | **MANTENER** |
| `time.sleep(N)` | **12** | sleep arbitrario | candidato migración |
| `for _ in range(N):` | **12** | mix retries + polls | clasificar caso a caso |
| `while time.time() < deadline:` | **4** | polling loop | **MIGRAR** prioridad |
| `page.wait_for_selector(...)` | **3** | event-driven | mantener/usar más |
| `page.wait_for_load_state(...)` | 1 | event-driven | OK |
| `page.wait_for_function(...)` | **0** | event-driven | **OPORTUNIDAD** |
| `page.wait_for_url(...)` | **0** | event-driven | **OPORTUNIDAD** |

## Clasificación de los 77 polls no event-driven

### A) DEBE MANTENERSE (intencional)
- **33 `human_pause()`** — delays gaussianos para parecer humano. NO tocar.
- **~10 retries genuinos** dentro de `for _ in range(N):` — son intentos múltiples,
  no polls. Ej.: `for swap_attempt in range(5)` al cambiar profile.

### B) CANDIDATO MIGRACIÓN BAJA RIESGO (~15 instancias)

Polls cuyo objetivo es **esperar un selector concreto**:

```python
# L939-967 — iframe payments-cc poll
for poll in range(20):
    page.wait_for_timeout(500)
    iframe_ready = page.evaluate("...frame existe + body ready...")
    if iframe_ready: break

# → CANDIDATO:
page.wait_for_function(
    "Array.from(document.querySelectorAll('iframe')).some(f => /payments-cc/.test(f.src) && f.contentDocument)",
    timeout=10_000)
```

```python
# L1099-1142 — poll Añadir tarjeta (16 × 500ms = 8s)
for poll_attempt in range(16):
    page.wait_for_timeout(500)
    anadir_visible = page.evaluate(...)
    if anadir_visible: break

# → CANDIDATO:
page.wait_for_function("/* anadir_tarjeta visible */", timeout=8_000)
```

```python
# L597 — login pwd poll 25s
while (time.time() - poll_start) < 25:
    if page.locator('input[type="password"]').is_visible(timeout=500): break
    page.wait_for_timeout(500)

# → CANDIDATO:
page.wait_for_selector('input[type="password"]', timeout=25_000)
```

### C) CANDIDATO MIGRACIÓN MEDIA RIESGO (~10 instancias)

Polls cuyo objetivo es **esperar URL change**:

```python
# L2965-2978 — poll URL cambio post Continuar
url_changed_fast = False
for _ in range(10):
    page.wait_for_timeout(300)
    if page.url.lower() != url_before.lower() and "/event/" not in page.url.lower():
        url_changed_fast = True
        break

# → CANDIDATO:
try:
    page.wait_for_url(lambda u: "/event/" not in u.lower() and u.lower() != url_before.lower(),
                       timeout=3_000)
    url_changed_fast = True
except Exception:
    url_changed_fast = False
```

```python
# L3621 — watch_for_checkout (deadline 600s)
while time.time() < deadline:
    time.sleep(2)
    if "/checkout/" in page.url.lower(): break

# → CANDIDATO:
page.wait_for_url(lambda u: "/checkout/" in u.lower(), timeout=600_000)
```

### D) NO MIGRAR — son `wait_for_timeout` post-acción que TM necesita

`wait_for_timeout(2500)` después de click típicamente espera **hidratación
React intermedia**. Migrar a `wait_for_function` requiere conocer EXACTAMENTE
qué condición de estado esperamos (ej. `document.querySelector('.spinner') === null`).

**Recomendación**: NO migrar hasta tener trace_auth captura del estado intermedio
en cada caso.

## Propuesta incremental (3 sesiones)

### Sesión A — Quick wins event-driven (low-risk, ~30 min)

| Cambio | Archivo:línea | Estimación |
|--------|---------------|------------|
| Login pwd poll (L597) → `wait_for_selector('input[type=password]', timeout=25_000)` | _compra_parallel.py:597 | -10 LOC |
| Iframe payments-cc (L939) → `wait_for_function` | _compra_parallel.py:939 | -15 LOC |
| Añadir tarjeta (L1099) → `wait_for_function` | _compra_parallel.py:1099 | -15 LOC |
| Hidratación body (L2482) → `wait_for_function(document.body.innerText.length > 2500)` | _compra_parallel.py:2482 | -8 LOC |

**Total**: -48 LOC, riesgo bajo (mismo comportamiento observable).

### Sesión B — URL change waits (medium-risk, ~30 min)

| Cambio | Archivo:línea | Razón |
|--------|---------------|-------|
| post-Continuar (L2965) → `wait_for_url(lambda u: u != before)` | _compra_parallel.py:2965 | menos ruido logs |
| watch_for_checkout (L3621) → `wait_for_url(/checkout/)` | _compra_parallel.py:3621 | event-driven |

**Total**: -20 LOC, riesgo medio (URL puede cambiar con queue-it intermedio).

### Sesión C — Auditoría `for range()` (revisión manual, ~45 min)

Para los 12 `for _ in range(N):` hay que clasificar caso a caso:
- ¿Es un **retry** (intento N veces operación completa)? → mantener.
- ¿Es un **poll** (re-check de condición)? → migrar a wait_for_*.

## NO TOCAR (decisión explícita)

- **33 `human_pause(min, max)`** — son delays gaussianos intencionales. Si
  migramos a event-driven, perdemos el "ruido" que necesitamos para parecer
  humanos ante NuData/Akamai keystroke timing analysis.
- **`time.sleep(5)` post-`start_profile`** — espera fija para que MoreLogin
  inicialice el browser. No es event-observable.
- **`time.sleep(2)` antes de `destroy_fresh_profile`** — espera para que
  MoreLogin close API termine antes del delete. No es event-observable.
- **`page.wait_for_timeout(2500)` post-modal** — espera hidratación React
  intermedia sin condición observable clara.

## Métricas objetivo (3 sesiones)

| Métrica | Hoy | Objetivo (3 sesiones) |
|---------|-----|----------------------|
| Event-driven ratio | 3.8% | 25-30% |
| `time.sleep()` total | 12 | 6-8 |
| `wait_for_timeout()` total | 61 | 35-40 |
| `while time.time() < deadline` | 4 | 0 |
| `wait_for_function` total | 0 | 8-12 |
| `wait_for_url` total | 0 | 3-5 |

## Conclusión

El bot acumula **3.8% event-driven** vs **96.2% polling**. Esto no es un
problema funcional (todo funciona) pero sí de mantenibilidad y consumo de CPU.

**NO se aplica ningún cambio en esta sesión** según política user. Documento
listo para acción incremental.

Riesgo aceptable:
- Sesión A (4 cambios `wait_for_selector` / `wait_for_function`) — **bajo**.
- Sesión B (2 cambios `wait_for_url`) — **medio**.
- Sesión C (revisión 12 retries vs polls) — **bajo si solo se reclassifica**.
