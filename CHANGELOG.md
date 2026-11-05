# CHANGELOG — TM PERROS

Formato basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y
[Semantic Versioning](https://semver.org/lang/es/).

**Norma del proyecto:** cada cambio al sistema incrementa la versión en `VERSION`
y añade un bloque aquí. Nunca se reutiliza un número.

- `MAJOR` (X.0.0) — cambio incompatible (rotura de API, migración forzosa, cambio de motor browser)
- `MINOR` (X.Y.0) — funcionalidad nueva compatible (nueva pestaña, nuevo endpoint, nuevo flow)
- `PATCH` (X.Y.Z) — fix de bug, mejora menor, refactor interno

---

## [1.0.0] - 2026-11-05

**Baseline consolidada del proyecto.** Consolida ~83 memorias de desarrollo
previas en un punto de partida versionado.

### Added
- `DOSSIER.md` — dossier operativo del proyecto (estado, pendientes, cómo usar)
- `VERSION` — fuente única de versión del sistema (semver)
- `CHANGELOG.md` — este archivo (historial de cambios versionados)

### Fijado / consolidado (estado baseline)
- Dashboard Flask operativo en `http://127.0.0.1:5055` (admin/admin)
- 686 cuentas TM en `accounts.json` (628 purchase_ready, 234 validated, 65 recovered)
- 750 cuentas archivadas en `registered_archive.csv`
- Pipeline registro v3 validado: 91% tasa éxito, 150-250s/cuenta
- Verify 16 checks post-registro end-to-end
- Multi-provider SMS (SMSPool $97.15 activo, 5sim agotado)
- i18n ES/EN 1141+ claves con paridad 100%
- 17 subnets /24 quemadas registradas en `burned_subnets24.json`
- Camoufox único motor production-ready para compra (checkout real validado 2026-05-12)
- Arquitectura Fase 0: skeleton `core/` + `bot/` + `runners/` con shims sin breaking changes
- 62 `except: pass` catalogados + plan A/B/C de migración
- Audit refactor 2026-05-13: -45 LOC, -30KB backup, 9 quick wins aplicados

### Fixed (consolidado — ya aplicado en la baseline)
- Bug archive columnas desplazadas → verify_FAIL
- Bug email_otp detección prematura (fix espera 6s al botón)
- Bug orquestador verify por email-input (fix por timestamp accounts.json)
- Bug TM phone límite no detectado (fix early-detect 4s)
- Bug recovery sin storage_state (fix captura session tras reset)
- Bug cleanup navegador parcial (fix: SIEMPRE limpia cookies+cache+localStorage+IndexedDB)
- Bug detector checkout URL falso positivo con `?headerTitle=Checkout`
- Bug Intl.DateTimeFormat Proxy que rompía PingFederate (fix Proxy preservando API nativa)
- Bug WebRTC IP leak en Camoufox (fix `block_webrtc=True`)
- Bug MoreLogin perfiles reutilizados (fix burn-after-use en 3 bots)

### Deuda técnica conocida (pendiente)
- Bloqueo Akamai `/auth` TM contra MoreLogin Chrome (ver DOSSIER §4.2)
- `main()` 1931 LOC en `_compra_parallel.py` pendiente de trocear
- 3.8% esperas event-driven (resto son `wait_for_timeout` fijos)
- 263 `.py` en raíz, muchos one-off sin consolidar
- Sin CI GitHub Actions
- Coverage 22% (objetivo >70%)

---

## Cómo registrar un cambio futuro

1. Editar `VERSION` incrementando el número (nunca reutilizar uno anterior).
2. Añadir un bloque arriba de este archivo con:
   ```markdown
   ## [X.Y.Z] - YYYY-MM-DD

   ### Added / Changed / Fixed / Removed / Deprecated / Security
   - Descripción breve del cambio
   ```
3. Si el cambio afecta a `dashboard.py`, `register_account.py`,
   `_compra_parallel.py` o `_compra_camoufox.py`, mencionar el archivo.
4. Si introduce un breaking change, incrementar `MAJOR`.

**Reglas:**
- Un número de versión nunca se reutiliza (aunque se revierta un cambio).
- Un revert cuenta como nueva versión, no como vuelta a la anterior.
- Las versiones solo avanzan hacia adelante.
