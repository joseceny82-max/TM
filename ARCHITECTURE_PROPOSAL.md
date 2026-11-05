# Arquitectura híbrida — Core QA + Purchase Bot

**Fecha**: 2026-05-13
**Estado**: PROPUESTA + skeleton inicial (sin breaking changes)
**Objetivo**: separar **core reusable** (QA engine limpio) de **bot de compra** (lógica de negocio)

---

## 1. Arquitectura final

```
proyecto/
│
├── core/                          # 🧠 BASE LIMPIA (QA ENGINE)
│   ├── __init__.py
│   ├── browser_engine.py         # arrancar/conectar browser, contextos, cleanup
│   ├── config.py                 # timeouts, urls, settings (re-export _compra_constants)
│   │
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── logger.py             # StructuredLogger + log_event (re-export _compra_logger)
│   │   ├── tracer.py             # AuthTracer (re-export _trace_auth)
│   │   └── reports.py            # exports HAR / logs / errors
│   │
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── actions.py            # click, fill, wait, scroll (re-export tm_ui_helpers)
│   │   └── selectors.py          # helpers de elementos (futuro: extraer regex/locators)
│   │
│   └── utils/
│       ├── __init__.py
│       ├── timeouts.py           # constantes timeout (alias _compra_constants subset)
│       └── retries.py            # decorator/helper de retry (futuro)
│
├── bot/                           # 🛒 LÓGICA DE COMPRA (TM-SPECIFIC)
│   ├── __init__.py
│   ├── purchase_flow.py          # flujo end-to-end de compra
│   ├── auth_flow.py              # login OAuth + forced-reset recovery
│   ├── cart_flow.py              # stepper qty + Continuar Comprando
│   ├── checkout_flow.py          # Adyen iframe + fill card + 3DS
│   ├── recovery.py               # account-swap + retry logic
│   ├── stealth.py                # purchase_stealth.py (TM-specific, NO en core)
│   ├── human_journey.py          # warm-up TM (TM-specific, NO en core)
│   └── tm_handlers.py            # cookies banner TM, Partners+Seguro, find_tickets
│
├── tests/                         # 🧪 TESTS QA
│   ├── test_auth.py
│   ├── test_checkout.py
│   ├── test_cart.py
│   ├── test_regression.py
│   └── test_intl_integrity.py    # YA EXISTE, mover aquí
│
├── runners/
│   ├── test_runner.py            # pytest-style runner
│   └── bot_runner.py             # main entry para `python bot_runner.py --event-url ...`
│
├── legacy/                        # 🧯 PRE-MIGRATION (a borrar tras migración completa)
│   ├── _compra_parallel.py       # bot monolítico actual (3731 LOC)
│   ├── _compra_camoufox.py
│   ├── _compra_with_swap.py
│   └── (otros _compra_*.py experimentales que ya no se usan)
│
└── main.py                        # punto de entrada (selector: bot|test|tools)
```

---

## 2. Mapeo de archivos existentes

### → `core/` (LIMPIO, sin lógica TM)

| Archivo actual | Destino core | Razón |
|----------------|--------------|-------|
| `_compra_logger.py` | `core/observability/logger.py` | StructuredLogger es genérico |
| `_trace_auth.py` | `core/observability/tracer.py` | AuthTracer es CDP genérico (renombrar conceptual) |
| `_compra_constants.py` (subset genérico) | `core/config.py` + `core/utils/timeouts.py` | Solo timeouts/polls/retries genéricos |
| `browser_cleanup.py` | `core/browser_engine.py` | Cleanup CDP genérico |
| `json_atomic.py` | `core/utils/atomic_io.py` | I/O atómico genérico |
| `cookie_filter.py` | `core/utils/cookie_filter.py` | Clasificador genérico de cookies (no TM-specific) |
| `human_motion.py` | `core/ui/human_motion.py` | Bezier/Markov genérico (si existe) |

### → `bot/` (LÓGICA TM-SPECIFIC)

| Archivo actual | Destino bot | Razón |
|----------------|-------------|-------|
| `_compra_parallel.py` (`main()`) | `bot/purchase_flow.py` (orquestador) | Flow específico TM |
| `_compra_parallel.py` (`login_full`, `login_with_account_swap`) | `bot/auth_flow.py` | OAuth TM + forced-reset |
| `_compra_parallel.py` (stepper, find_tickets, Continuar) | `bot/cart_flow.py` | Compra TM |
| `_compra_parallel.py` (`fill_card`, `fill_card_adyen`) | `bot/checkout_flow.py` | Adyen + 3DS TM |
| `purchase_stealth.py` | `bot/stealth.py` | Stealth TM-aware (TZ Madrid, Sec-CH-UA TM) |
| `human_journey.py` | `bot/human_journey.py` | Warm-up TM (decoy events) |
| `ip_reputation.py` | `bot/ip_reputation.py` | Validación IP residencial para TM |
| `bot_orchestrator.py` (`create_fresh_profile`) | `bot/profile_manager.py` | MoreLogin Quantum cc-ES (TM proxy) |
| `account_manager.py` | `bot/account_manager.py` | Cuentas TM |
| `account_recovery.py` | `bot/recovery.py` | Password reset via IMAP madre TM |
| `tm_ui_helpers.py` | `bot/tm_handlers.py` | accept_cookies, click_continue (texto en español TM) |
| `cookie_warmer.py` | `bot/cookie_warmer.py` | Warming HTTP TM-specific |

### → `tests/`

| Archivo actual | Destino tests |
|----------------|---------------|
| `test_intl_integrity.py` | `tests/test_intl_integrity.py` (mover) |
| `EXCEPT_PASS_CATALOG.md` | `tests/CATALOG.md` (referencia) |
| `WAIT_STRATEGY_AUDIT.md` | `tests/AUDIT.md` (referencia) |

### → `legacy/` (a borrar tras migración estable)

| Archivo actual | Destino |
|----------------|---------|
| `_compra_u1_*.py` (debug scripts varios) | `legacy/` |
| `_compra_u6_stealth.py` | `legacy/` |
| `_compra_camoufox.py` (engine alternativo) | `legacy/` (si Camoufox no se usa más) |
| `_compra_with_swap.py` (wrapper antiguo) | `legacy/` o reemplazado por `runners/bot_runner.py` |
| `_buy_test_qty4.py`, `_step.py`, etc. (experimentales) | `legacy/` |

### Imports compartidos (mantener en root o `core/utils`)

| Archivo | Justificación |
|---------|---------------|
| `config.py` | Settings globales del proyecto (rutas datos/) |
| `dashboard.py` | Flask app (no afectado por refactor) |

---

## 3. Reglas de separación core vs bot

### CORE puede:
- Definir interfaces genéricas (`Browser`, `Tracer`, `Logger`)
- Implementar utilidades agnósticas (timeouts, retries, CDP, screenshots)
- Exponer APIs para que cualquier QA test/bot las use
- Manejar lifecycle (start, attach, detach, cleanup)

### CORE NO puede:
- Importar de `bot/` ❌
- Referenciar selectores TM-specific (`ticketmaster.es`, `eps_sid`) ❌
- Conocer Adyen, PingFederate, MoreLogin ❌
- Tener stealth JS inline (Intl Proxy, navigator.webdriver) ❌
- Tener strings en español ❌

### BOT puede:
- Importar de `core/` ✅
- Definir flows específicos TM (login, checkout, recovery)
- Usar selectores y textos TM (`Continuar comprando`, `Acepto`)
- Implementar stealth especializado (con justificación de negocio)
- Hacer retries adaptativos según error TM

### BOT NO debe:
- Implementar primitivas que ya están en core (logger, tracer, browser_engine)
- Duplicar lógica de retry/timeout genérica

---

## 4. Plan de migración incremental (NO breaking)

### Fase 0 — Skeleton + shims (AHORA, sin breaking)

```
core/
├── observability/
│   ├── logger.py        # `from _compra_logger import *` (re-export)
│   ├── tracer.py        # `from _trace_auth import *` (re-export)
│   └── reports.py       # placeholder
├── ui/
│   ├── actions.py       # `from tm_ui_helpers import *` (re-export)
│   └── selectors.py     # placeholder
├── browser_engine.py    # `from browser_cleanup import *` (re-export)
└── config.py            # `from _compra_constants import *` (re-export)

bot/
├── purchase_flow.py     # placeholder (import legacy `_compra_parallel`)
├── stealth.py           # `from purchase_stealth import *`
└── ...
```

**Garantía**: el bot actual `_compra_parallel.py` SIGUE funcionando porque los archivos originales no se tocan. Solo añadimos re-exports.

**Validación**: smoke test E2E del bot debe pasar idéntico.

### Fase 1 — Migrar imports a paths nuevos (no breaking)

Por cada módulo:
1. Cambiar `from _compra_logger import log_event` → `from core.observability.logger import log_event`
2. Mantener archivo original como redirector (`from core.observability.logger import *`).
3. Validar smoke test.
4. Después de N sesiones sin issues, borrar redirector original.

**Garantía**: si algo se rompe, revertimos UN import a la vez.

### Fase 2 — Trocear `main()` en módulos `bot/`

ESTO ES EL REFACTOR ESTRUCTURAL GRANDE. Solo cuando todo lo demás esté estable.

`_compra_parallel.main()` 1931 LOC → `bot/purchase_flow.PurchaseFlow.run()`:
- `_step_pick_profile_account` → `bot/profile_manager.py`
- `_step_browser_setup` → `core/browser_engine.py` + `bot/stealth.py` + `bot/human_journey.py`
- `_step_event_stepper` → `bot/cart_flow.py`
- `_step_continuar_oauth` → `bot/auth_flow.py`
- `_step_login_recovery` → `bot/recovery.py`
- `_step_checkout_fill_card` → `bot/checkout_flow.py`
- `_cleanup` → `core/browser_engine.cleanup_context()`

**Cada step se extrae SOLO si se puede probar aisladamente.**

### Fase 3 — `runners/bot_runner.py` reemplaza `_compra_parallel.py`

```python
# runners/bot_runner.py
from bot.purchase_flow import PurchaseFlow
from core.config import load_settings

if __name__ == '__main__':
    settings = load_settings()
    flow = PurchaseFlow(settings)
    rc = flow.run(event_url=..., engine="morelogin", ...)
    sys.exit(rc)
```

`_compra_parallel.py` se mueve a `legacy/` y se mantiene como referencia hasta que `runners/bot_runner.py` esté validado en producción.

### Fase 4 — Tests reales en `tests/`

```python
# tests/test_intl_integrity.py (ya existe, mover)
# tests/test_eps_warming.py (nuevo)
# tests/test_auth_flow.py (nuevo)
# tests/test_checkout_fill_card.py (nuevo)
```

Cada test usa el **core directamente**, NO el bot. El bot se testa con tests E2E:

```python
# tests/test_e2e_purchase.py (mocking TM)
```

### Fase 5 — Cleanup `legacy/`

Cuando todos los tests pasan y el bot lleva ≥2 semanas sin issues:
- Borrar `legacy/_compra_parallel.py` y similares.
- Reducir `legacy/` a 0 archivos.

---

## 5. Cómo evitar duplicación entre capas

### Regla 1 — Interfaces, no implementaciones
`core/browser_engine.py` define la **interfaz** de Browser:
```python
class BrowserEngine(Protocol):
    def attach_cdp(self, port: int) -> CDPSession: ...
    def cleanup_context(self, ctx) -> dict: ...
```

`bot/` la usa pero NO implementa una versión paralela.

### Regla 2 — Constantes solo en `core/config.py`
El bot puede definir constantes **TM-specific** (URLs, selectores) en `bot/constants.py`. Pero las **timeouts genéricos** y **retries** vienen de `core/config.py`.

### Regla 3 — Tests del core SOLO testan core
- `tests/test_intl_integrity.py` testa el stealth_js (que vive en `bot/stealth.py`)
- `tests/test_logger.py` testa solo `core/observability/logger.py`

### Regla 4 — Logger único compartido
TODO log (core o bot) usa `core.observability.logger.log_event`. Cero `print()` no documentados.

### Regla 5 — Imports unidireccionales
- `bot/` puede importar de `core/` ✅
- `core/` NO puede importar de `bot/` ❌ (verificable con linter)

---

## 6. Roadmap concreto (recomendado)

| Sesión | Acción | Riesgo | Reversible |
|--------|--------|--------|------------|
| **AHORA** | Crear skeleton + re-export shims | bajo | sí (eliminar carpetas) |
| Próxima | Migrar imports de `_compra_logger` → `core.observability.logger` | bajo | sí |
| +1 | Migrar `_trace_auth` → `core.observability.tracer` | bajo | sí |
| +1 | Migrar `_compra_constants` → `core.config` | bajo | sí |
| +1 | Migrar `browser_cleanup` → `core.browser_engine` | bajo | sí |
| +2 | Mover `purchase_stealth` → `bot/stealth.py` | bajo | sí |
| +2 | Mover `human_journey` → `bot/human_journey.py` | bajo | sí |
| +2 | Mover `tm_ui_helpers` → `bot/tm_handlers.py` | bajo | sí |
| +3 | Crear `bot/auth_flow.py` extrayendo `login_full` + `login_with_account_swap` | **medio** | parcial |
| +3 | Crear `bot/checkout_flow.py` extrayendo `fill_card`/`fill_card_adyen` | **medio** | parcial |
| +4 | Crear `bot/purchase_flow.py` que orqueste los flows extraídos | **alto** | parcial |
| +4 | `runners/bot_runner.py` reemplaza `_compra_parallel.py` | **alto** | con backup |
| +5 | Mover `_compra_parallel.py` y `_compra_*.py` a `legacy/` | bajo | sí |
| +6 | Tests reales en `tests/` | bajo | sí |
| +∞ | Eliminar `legacy/` cuando estable ≥2 semanas | bajo | sí |

---

## 7. Lo que se hace AHORA (FASE 0)

**Solo skeleton + shims** — cero breaking changes:

1. Crear estructura de directorios:
   ```
   core/__init__.py
   core/observability/__init__.py
   core/observability/logger.py        # re-export _compra_logger
   core/observability/tracer.py        # re-export _trace_auth
   core/ui/__init__.py
   core/ui/actions.py                   # re-export tm_ui_helpers
   core/config.py                       # re-export _compra_constants
   core/browser_engine.py               # re-export browser_cleanup
   bot/__init__.py
   bot/stealth.py                       # re-export purchase_stealth
   bot/human_journey.py                 # re-export human_journey original
   bot/recovery.py                      # re-export account_recovery
   bot/ip_reputation.py                 # re-export ip_reputation
   tests/__init__.py
   runners/__init__.py
   legacy/__init__.py
   ```

2. **NO mover** `_compra_parallel.py` aún. Sigue funcionando como está.

3. **NO mover** `purchase_stealth.py`, `human_journey.py`, etc. Los originales se mantienen, los re-exports apuntan a ellos.

4. Validar smoke test del bot — debe pasar idéntico.

---

## 8. Beneficios esperados

| Beneficio | Cuándo se materializa |
|-----------|----------------------|
| Imports paths claros (`core.X` vs `bot.X`) | Fase 0 (skeleton) |
| Identificar fácilmente qué es core vs bot | Fase 0 |
| Tests pueden importar `core/` sin acarrear lógica TM | Fase 1-2 |
| Refactor de `main()` con menos miedo (módulos pequeños) | Fase 3-4 |
| Eliminar `_compra_*.py` legacy duplicados | Fase 5 |
| Reuso del core para otro proyecto QA | Post-Fase 5 |

---

## 9. Riesgos identificados

| Riesgo | Mitigación |
|--------|------------|
| Re-export shims aumentan tiempo de import | Lazy imports en módulos opcionales |
| `_compra_parallel.py` sigue siendo monolito 3731 LOC | Tolerable hasta Fase 3 |
| Tests aún no existen para core/bot por separado | Crear gradualmente en Fase 1+ |
| Confusión sobre dónde poner código nuevo | Política: `core/` solo si NO menciona "ticketmaster" |
| Round-trip imports si core mal diseñado | Linter check: `core/` NO importa de `bot/` |
