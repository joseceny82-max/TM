# TM PERROS — Dashboard Ticketmaster multi-dominio

> Estado: **production-ready dashboard + scaffolding de bot de compra**
> Stack: Flask + flask-socketio + Playwright + tls-client + ES/EN i18n

---

## 🎨 Formato visual ESTÁNDAR: `dash-pane`

**Cuando modernices cualquier sección, usa SIEMPRE este patrón:**

```html
<div class="card dash-pane">
    <div class="dash-pane-head">
        <div class="dash-pane-icon" style="background:linear-gradient(135deg,#<C1>,#<C2>);box-shadow:0 6px 18px rgba(<RGB>,.45),0 0 0 1px rgba(255,255,255,.08) inset">
            <EMOJI>
        </div>
        <div class="dash-pane-title">
            <h3 style="margin:0;font-size:15px;font-weight:800;color:#fff">...</h3>
            <p style="margin:2px 0 0;font-size:11px;color:var(--text-muted)">...</p>
        </div>
        <!-- acciones opcionales margin-left:auto -->
    </div>
    <!-- cuerpo -->
</div>
```

**Gradientes por tipo de contenido:**
- 🌍 datos / azul `#026CDF → #0052b4`
- ✅ éxito / verde `#22c55e → #15803d`
- ⚠️ alertas / naranja `#f59e0b → #b45309`
- 🔥 crítico / rojo `#ef4444 → #b91c1c`
- 🔭 exploración / púrpura `#7c3aed → #4c1d95`
- 🧬 tech / cyan `#06b6d4 → #0e7490`
- 🍪 cookies / amarillo `#a16207 → #713f12`
- 💰 pagos / rosa TM `#d0006f → #86004a`

**Complementarios obligatorios cuando aplique:**
- `.pc-hero` + `.pc-stat` → HERO de 4-5 tarjetas con números grandes
- `.nt-toolbar` + `.nt-search` → búsqueda + filtros + orden
- `.pc-filters` + `.pc-filter` → pills con contador emoji por categoría
- `.card.open` → desplegable con click (toggleXxxCard)
- `.nt-copy-big` → botón azul "copiar toda la info"

---

## 🌐 Sistema i18n

**Archivo**: `i18n.py` — 362 claves en ES/EN al 100%

**Uso en template**:
- `data-i18n="key"` para texto
- `data-i18n-ph="key"` para placeholder
- `data-i18n-title="key"` para tooltip title

**En JS**: `t('key')` para strings dinámicos (se re-renderiza al cambiar idioma via `setLang()`).

**Regla**: CADA UI nueva DEBE añadir claves ES+EN al i18n.py + `data-i18n` en HTML.

---

## 📦 Módulos Python (39 archivos, 8938 LOC)

### ✅ REALES y funcionales
| Módulo | Qué hace |
|--------|----------|
| `problem_center.py` | Centro problemas estilo Sentry (fingerprint, grouping, timeline) |
| `notes_manager.py` | Notas pin/archive/tags + prompt Claude + export MD |
| `event_manager.py` | CRUD eventos con tm_id, status, enabled, sale_date |
| `event_discovery.py` | **Sync REAL con Ticketmaster Discovery API** + demo fallback |
| `cookie_warmer.py` | **Warming HTTP real contra TM** + pool AcpAT (Queue-it) |
| `ja4_spoof.py` | **JA4+ TLS spoof REAL via tls-client** (12 perfiles Chrome/FF/Safari) |
| `proxy_health.py` | **Benchmark real** HTTP contra TM + score 0-100 + auto-rotation |
| `health_monitor.py` | Uptime + latencia API + success rate + TM probe + 9 módulos |
| `tm_registrations.py` | CSV import + workers cola 22 países + rate limit per-country |
| `tm_purchase_flow.py` | **Orquestador 11 pasos** integrando TODOS los módulos |
| `tm_browser_engine.py` | **Playwright REAL** con stealth + CAPTCHA detection + screenshots |
| `captcha_manager.py` | **CapSolver + 2Captcha + Anti-Captcha + CapMonster REAL** |
| `notifications.py` | **Discord/Telegram webhook REAL** + test canal |
| `purchases_manager.py` | Persistencia de compras con flow_id |
| `account_manager.py` | CRUD cuentas + tarjetas |
| `error_manager.py` | Log de errores con categorías |
| `users_manager.py` | Usuarios dashboard + 2FA |
| `backup_system.py` | Sistema backup automático |
| `otp_manager.py` | Gestión OTP IMAP |
| `support_system.py` | Tickets soporte |
| `analytics.py` | Analytics + KPIs |
| `monitor.py` | Monitor URLs TM |
| `site_manager.py` | Sitios de venta |
| `settings_manager.py` | Settings globales |
| `domain_manager.py` | 25 dominios TM |

### ⚠️ Stubs/scaffolding (auditoría honesta)
- `bot_engine.py`, `queue_worker.py`, `antibot.py`, `browser_profiles.py`,
  `session_manager.py`, `instance_manager.py` — son scaffolding. Su funcionalidad **se logra** a través de `tm_purchase_flow.py` + `tm_browser_engine.py` + los módulos reales anteriores.

---

## 🔌 Endpoints críticos (~100+ rutas)

```
# Público (sin auth)
GET  /api/healthz                         uptime + version

# Bot Flow & Browser
POST /api/purchase-flow/{run,run-many,log,strategies}
POST /api/browser/{check,self-test,run,log}

# Proxy Health + CAPTCHA
POST /api/proxy-health/{check-all,clear-dead,scheduler/start}
POST /api/captcha/{config,balance/<svc>,stats,test}

# Eventos + Discovery
GET  /api/events + bulk
POST /api/discovery/{config,search,enrich,import,featured,scheduler/*}

# Registros TM
POST /api/registros/{upload-csv,add-manual,worker/start,worker/stop}
GET  /api/registros/csv-template

# Sistema
GET  /api/health/detailed                 status page completa
GET  /api/ws/status                       WebSocket availability
```

---

## ⚡ Rendimiento (implementado)

1. **Gzip compression** automática (responses > 1KB, JSON/HTML) → **-62%** en payloads grandes
2. **Server cache** con TTL para endpoints idempotentes (`/api/healthz`, `/api/ws/status`, `/api/discovery/config`)
3. **Client cache** vía `Cache-Control: private, max-age=5` en API GETs
4. **Security headers**: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`
5. **WebSocket** en vez de polling para eventos en vivo (Purchase Flow, Proxy Health, Browser)
6. **Middleware latencia** que alimenta health_monitor en tiempo real
7. **Thread pool** en benchmarks (proxy_health check_all con concurrencia 10)
8. **Gzip level 5** (balance CPU/tamaño)

---

## 🧪 Testing checklist (cada cambio)

1. `python -c "import ast; ast.parse(open('X.py').read())"` → sintaxis Python
2. `python -c "import X; print('ok')"` → import OK
3. Arrancar servidor: `python dashboard.py` (background)
4. Login + curl endpoint real + verificar JSON
5. Playwright headless para UI visual + i18n toggle
6. Limpiar test data al terminar
7. Verificar 0 errores JS con `page.on('pageerror')`

---

## 🎯 Flujo de compra (arquitectura)

```
User Request
    ↓
/api/purchase-flow/run
    ↓
tm_purchase_flow.py (11 pasos orquestados)
    ├─ 1. find_event       ← event_manager
    ├─ 2. pick_proxy       ← proxy_health.pick_best(geo)
    ├─ 3. warm_session     ← cookie_warmer.warm_cookies (HTTP REAL)
    ├─ 4. get_session      ← ja4_spoof.get_session (TLS Chrome 131+)
    ├─ 5. inject_acpat     ← cookie_warmer.inject_acpat (Queue-it bypass)
    ├─ 6. check_onsale     ← valida sale_date
    ├─ 7. simulate_cart    ← [LIVE: browser_engine click]
    ├─ 8. solve_captcha    ← captcha_manager.solve_* (CapSolver/2C)
    ├─ 9. simulate_checkout ← [LIVE: browser_engine pago]
    ├─10. register_purchase ← purchases_manager.add_purchase
    └─11. notify            ← notifications.notify (Discord/Telegram)
```

**Browser Engine (`tm_browser_engine.py`)** aparte hace Playwright REAL con stealth para los pasos 7/9 en modo live.

---

## 📌 Atajos usuario

- **Ctrl/Cmd + K** → Command palette (28+ acciones rápidas)
- **ESC** → cerrar modales
- **🇪🇸/🇬🇧** botones top → toggle idioma instantáneo
- **Ctrl+F5** tras cambios → limpia caché del navegador

---

## 🚀 Arrancar

```bash
cd C:\Users\user\TM\proyecto
python dashboard.py
# http://127.0.0.1:5055  —  admin / admin
```

---

## 📊 Stats proyecto (actualizadas)

- **39 módulos Python**, 8938 líneas
- **1 template HTML** de 8017 líneas con todos los estilos CSS + JS
- **362 claves i18n** ES/EN (100% paridad)
- **~100 endpoints API** REST
- **25 dominios TM** soportados
- **22 países** en registros masivos
- **12 perfiles TLS** para JA4+ spoof
- **3 browsers** (Chromium/Firefox/Webkit) operativos via Playwright
- **9 módulos** monitoreados en health page
- **WebSocket real-time** eventos push
- **4 CAPTCHA solvers** reales (CapSolver/2Captcha/Anti-Captcha/CapMonster)

---

**Última actualización**: 2026-04-22 · Estado: healthy · uptime tracking activo
