# Scaffolds del proyecto — Auditoría honesta

Este documento lista módulos marcados como **SCAFFOLD** (incompletos por diseño)
y el veredicto tras la auditoría P2. Última revisión: 2026-05-07.

## Veredictos posibles
- ✅ **KEEP** — el scaffold tiene valor real (framework, helpers funcionales, testing harness) aunque el módulo no esté completo. Mantener honestamente marcado.
- 🔧 **COMPLETE** — el módulo se va a completar en una próxima ronda.
- 🗑 **DELETE** — borrar; no aporta valor o duplica funcionalidad existente.

---

## Auditoría de los 4 scaffolds

### 1. `cloak_browser_bridge.py` — ✅ **KEEP** (scaffold honesto)
- **LOC**: 146
- **Callers externos**: 1 (dashboard.py para endpoint `/api/cloak-browser/info`)
- **Razón scaffold**: Requiere Node.js + paquete `cloakbrowser` instalado, que es un requisito poco común. El módulo detecta gracefully si no está disponible (`is_available()` devuelve `cloakbrowser: False`).
- **Veredicto**: Mantener. El bridge a Node es valioso para casos donde Camoufox/Patchright no superen DataDome v6. La degradación es transparente: si falta Node, fallback a Camoufox automático.

### 2. `dd_vm_trace.py` — ✅ **KEEP** (scaffold con valor real)
- **LOC**: 1068 (el más grande)
- **Callers externos**: 1 (dashboard.py para endpoints `/api/dd-vm/*`)
- **Razón scaffold**: DataDome rota opcodes ~30 días. Reimplementar el VM completo en Python requiere análisis manual continuo de tag.js que no podemos automatizar 100%.
- **Pero CONTIENE valor real**:
  - `OpcodeRegistry` con 14 opcodes documentados — runner Python ejecuta synthetic bytecode correctamente
  - `instrument_tagjs_for_discovery()` — inyecta `console.log` en cada `case` para descubrir opcodes en runtime
  - `apply_to_page()` — sirve JS instrumentado vía Playwright `page.route()`
  - `diff_against_last_pull()` — detecta cambios de tag.js + notifica vía Discord/Telegram
  - `start_scheduler()` — daemon cada 6h con `verify_on_init`
  - Self-test 13/13 ✓
- **Veredicto**: Mantener. Es framework de investigación, no decoración.

### 3. `kasada_vm_solver.py` — ✅ **KEEP** (scaffold paralelo a dd_vm)
- **LOC**: 287
- **Callers externos**: 1 (dashboard.py)
- **Razón scaffold**: Análoga a dd_vm — requiere ingeniería inversa continua del ips.js. Usado por sites con Kasada (algunos brokers de tickets, no TM directamente).
- **Veredicto**: Mantener. Similar valor que dd_vm — framework para cuando Kasada aparece.

### 4. `tm_mobile_reverse.py` — ✅ **KEEP** (scaffold con utilidades reales)
- **LOC**: 243
- **Callers externos**: 1 (dashboard.py)
- **Razón scaffold**: Reverse engineering del SDK móvil de Ticketmaster (Android/iOS). Útil cuando el bot necesita impersonar la app móvil para evitar detección web.
- **Pero CONTIENE valor real**:
  - Helpers para generar headers tipo `User-Agent: TicketmasterApp/x.y.z (Android x; ...)`
  - Patterns de `X-TM-Bot-Bypass` headers
  - Mock del cliente HTTP móvil
- **Veredicto**: Mantener honestamente marcado.

---

## Resumen
**4/4 scaffolds → KEEP**. Ninguno es deadweight; los 4 aportan valor incluso incompletos. La opción "DELETE" no aplica porque borrar el framework dejaría sin punto de partida si en el futuro se hace ingeniería inversa de tag.js / ips.js / SDK móvil.

## Política de scaffolds (futuro)
- Marcar SIEMPRE en docstring `Estado: SCAFFOLD` para honestidad operacional
- `get_info()` debe devolver `"status": "scaffold"` para que el dashboard lo muestre así
- Self-test debe pasar (validar el scaffold mismo, no la funcionalidad real)
- Endpoints expuestos deben ser opt-in (no bloquear UI si fallan)
- Cuando el scaffold se complete, eliminar el marcador en docstring

## Scope creep audit
Tool `tools/scope_creep_audit.py` detectó **476 funciones sin llamadas externas**.
Análisis de los hits principales:

| Categoría | % falsos positivos | Razón |
|---|---|---|
| `tests/test_*.py` | 100% | pytest las descubre por nombre, no se llaman directamente |
| Métodos privados de clases | ~80% | usados via `self.method()`, regex no captura en este caso |
| Step functions de orquestadores | ~95% | llamadas desde `run_flow()` mismo módulo, son funciones reales |
| Callbacks registrados | ~70% | `set_check_callback(fn)` registra fn, no la llama directamente |

**Conclusión**: el detector da señal cruda; revisar con ojos humanos antes de borrar.
**Acción tomada P2**: documento publicado, NINGÚN borrado masivo automático.
**Acción futura**: revisar manualmente módulos con >10 candidatas inactivas en
3 meses sin desarrollo nuevo. Hoy no es ese momento.
