# CHANGELOG — TM PERROS

Formato basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y
[Semantic Versioning](https://semver.org/lang/es/).

**Norma del proyecto:** cada cambio al sistema incrementa la versión en `VERSION`
y añade un bloque aquí. Nunca se reutiliza un número.

- `MAJOR` (X.0.0) — cambio incompatible (rotura de API, migración forzosa, cambio de motor browser)
- `MINOR` (X.Y.0) — funcionalidad nueva compatible (nueva pestaña, nuevo endpoint, nuevo flow)
- `PATCH` (X.Y.Z) — fix de bug, mejora menor, refactor interno

---

## [1.1.3] - 2026-11-07

### Security
- `proxies.txt` (credenciales de proxies) deja de estar en Git y se añade a
  `.gitignore`. El archivo local se conserva. Sigue presente en commits
  anteriores del historial (pendiente: reescritura del historial y rotación
  de credenciales).

---

## [1.1.2] - 2026-11-07

**Registro con Silent + proxies Lemon (validado end-to-end, verify 16/16).**

### Added
- `register_account.py` con `--engine silent`: crea un perfil Silent NUEVO por
  registro sobre un proxy Lemon no usado (`TM_SILENT_PROXY_HOST`, por defecto
  `lemonclub`) y comprueba desde el navegador que la IP de salida es ES.
- `TM_SILENT_ENGINE` (por defecto `brave`): motor del navegador del perfil
  (`brave`, `chromium` = Chromium gestionado más reciente, `chrome`, `edge`,
  `opera` o un `eng_...`).
- Huella con idioma/zona/geo de España (`es-ES`, `Europe/Madrid`); los
  perfiles de Silent nacían con huella italiana.
- Detección multilingüe de la pantalla de espera de TM ("Casi terminado",
  "Quasi finito", "Almost there"...).

### Fixed
- El gestor de consentimiento Ketch de TM tapaba el formulario de alta y
  dejaba "Siguiente" desactivado (`siguiente_disabled`): ahora se rechaza lo
  no esencial y se cierra.
- La pestaña de comprobación de IP quedaba abierta y Brave la restauraba en el
  registro: se deja en `about:blank` antes de cerrar.
- Sin credenciales de proxy visibles (Silent) se omite el pre-check HTTP en
  lugar de lanzarlo desde la IP de casa.

---

## [1.1.1] - 2026-11-07

**Registro con perfiles Froxy + plantilla nueva.**

### Fixed
- `register_account.py`: la validación del proxy usaba el puerto 8888 fijo
  (solo válido para Quantum); ahora usa el `proxyPort` real de cada perfil.
- `register_account.py`: credenciales del proxy codificadas en la URL
  (la contraseña de Froxy lleva `;`).
- `register_account.py`: un rechazo por whitelist del proveedor ya no se
  toma por bot-block (antes quemaba la IP); aborta con `proxy_whitelist_denied`
  y avisa en el log de la IP que hay que autorizar.
- `plantilla_tm.csv`: separador `;` (guardado de Excel) convertido a `,`.

### Added
- `TM_PROXY_HOST` (por defecto `froxy`): solo se usan perfiles MoreLogin cuyo
  proxy sea de ese proveedor; con el filtro activo no se crean perfiles
  AUTO-ES (llevaban Quantum caducado). `TM_PROXY_HOST=` lo desactiva.
- `plantilla_tm.csv`: 276 cuentas nuevas rellenadas (DNI, nombre, dirección,
  ZIP, tarjeta sintética); el teléfono se asigna al registrar.

### Pendiente (configuración, no código)
- Añadir la IP del PC a la whitelist de Froxy.
- Descargar en MoreLogin el kernel del navegador de los perfiles Froxy.

---

## [1.1.0] - 2026-11-05

**Motor de perfiles Silent + auto-actualización + fixes de dashboard.**

### Added
- `silent_engine.py` — adaptador de la API local de Silent (perfiles,
  enganche CDP, cleanup) como alternativa a MoreLogin; drop-in de la
  superficie de `bot_orchestrator`.
- Registro con `--engine silent` (`register_account.py`, también vía
  `TM_ENGINE=silent`); default inalterado (morelogin).
- Compra con `--engine silent` (`_compra_parallel.py`, tercer engine junto
  a morelogin/camoufox); passthrough de `engine` en `queue_manager` y
  `tm_real_purchase`.
- `auto_updater.py` — daemon de auto-actualización desde `origin/main`:
  fetch → pull --ff-only (siempre que el árbol esté limpio) → gate pytest
  (rollback automático si falla) → reinicio controlado del dashboard con
  espera de `/api/healthz`. Estado en `logs/auto_updater_state.json`.

### Fixed
- `/api/console/export` — la ruta no existía y el botón Export de la
  pestaña Consola daba 404 permanente.
- `/api/honest-dashboard` — caché TTL 5 min en `classify_modules()`
  (era ~34s por petición; ahora 0.004s en caliente).
- Pestaña Filas verificada end-to-end (enqueue, batch, position,
  swarm-broadcast, accept, claim, auto-rejoin del daemon, export CSV).

### Changed
- Dashboard consolidado: un único proceso en el puerto 5055
  (antes 3 procesos competían el puerto desde copias distintas).

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
