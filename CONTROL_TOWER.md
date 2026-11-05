# TM Control Tower — Self-Diagnosing Runtime Dashboard

> **Plataforma FastAPI de observabilidad en 4 fases incrementales** sobre el sistema `runtime_diagnostics` ya existente. Lee del stream JSONL sin tocar el bot, el FSM ni el logger.

| Métrica | Valor |
|---|---|
| Fases | **4** (MVP → Realtime → Control → Industrial) |
| Endpoints | **17** (v1, v2, v3, v4 + utility) |
| UIs HTML | **4** (una por fase, navegación entre ellas) |
| Módulos nuevos | **5** (`server`, `analytics`, `alerts`, `historical` + `__init__`) |
| Tests pasando | **23** Control Tower + 13 dashboard + 20 runtime + 29 FSM = **85 total** |
| LOC tocadas en bot/FSM/logger | **0** |
| LOC nuevas | ~2500 (backend + UI + tests) |
| Stack | FastAPI + uvicorn + SSE + HTML/vanilla JS |
| Puerto default | 5060 (`CT_PORT` override) |
| Reutiliza | `core.dashboard.stream`, `core.dashboard.aggregator` |

---

## 1. Las 4 fases en cascada

| Fase | Ruta | Endpoints API | Funcionalidad |
|---|---|---|---|
| **1. MVP** | `/` | `/api/v1/state`, `/api/v1/health`, `/api/v1/stream` (SSE) | Event stream live + FSM state + health |
| **2. Realtime** | `/realtime` | `/api/v2/state/live`, `/api/v2/drift/current`, `/api/v2/loops/current`, `/api/v2/health/current` | 4 paneles (state + health + drift + loops) |
| **3. Control** | `/control` | `/api/v3/run/<slot>/analytics`, `/api/v3/run/<slot>/root-cause`, `/api/v3/events/recent` | Run analytics + root cause + failure trace + filtering |
| **4. Industrial** | `/tower` | `/api/v4/runs/all`, `/api/v4/drift/global`, `/api/v4/alerts`, `/api/v4/history`, `/api/v4/history/stats`, `/api/v4/history/compare/<a>/<b>` | Multi-run + alerts + historical + comparativa |

Cada fase añade encima sin romper las anteriores. **Compatibilidad total entre fases.**

---

## 2. Estructura de carpetas

```
proyecto/
├── _compra_parallel.py              ← bot (0 LOC tocados)
├── _compra_logger.py                ← hook FASE 4 (8 LOC, sin tocar)
├── dashboard.py                     ← Flask dashboard (sin tocar)
├── run_control_tower.py             ← 🆕 Launcher uvicorn
│
├── core/
│   ├── observability/               ← (existente)
│   │   ├── runtime_diagnostics.py
│   │   ├── state_machine.py         ← FSM v2 (sin tocar)
│   │   └── logger.py
│   ├── contracts/                   ← (existente)
│   │   └── event_classifier.py
│   ├── diagnostics/                 ← (existente)
│   │   ├── drift_detector.py
│   │   └── health_score.py
│   ├── runtime/                     ← (existente)
│   │   └── run_session.py
│   ├── dashboard/                   ← (existente, Flask)
│   │   ├── stream.py                ← REUTILIZADO
│   │   ├── aggregator.py            ← REUTILIZADO
│   │   └── api.py
│   └── control_tower/               ← 🆕 NUEVO (FastAPI)
│       ├── __init__.py
│       ├── server.py                ← 🆕 FastAPI app principal
│       ├── analytics.py             ← 🆕 run analytics + root cause
│       ├── alerts.py                ← 🆕 alert engine
│       ├── historical.py            ← 🆕 historical index
│       └── ui/                      ← 🆕 4 UIs
│           ├── mvp.html             ← Fase 1
│           ├── realtime.html        ← Fase 2
│           ├── control.html         ← Fase 3
│           └── industrial.html      ← Fase 4 (control tower)
│
├── tests/
│   └── test_control_tower.py        ← 🆕 23 tests
│
└── runs/
    ├── diagnostics_slot*.jsonl      ← stream JSONL (sigue alimentando)
    └── ...
```

---

## 3. Cómo arrancar

### Instalar dependencias

```bash
pip install fastapi uvicorn
```

### Lanzar Control Tower

```bash
python run_control_tower.py
```

```
=== TM Control Tower ===
  MVP:        http://127.0.0.1:5060/
  Realtime:   http://127.0.0.1:5060/realtime
  Control:    http://127.0.0.1:5060/control
  Industrial: http://127.0.0.1:5060/tower
  API docs:   http://127.0.0.1:5060/docs

INFO:     Uvicorn running on http://127.0.0.1:5060
```

### Variables de entorno

| Variable | Default | Función |
|---|---|---|
| `CT_HOST` | `127.0.0.1` | Bind interface |
| `CT_PORT` | `5060` | Puerto HTTP |
| `CT_LOG_LEVEL` | `info` | Log level uvicorn |
| `OBSERVABILITY_ENABLED` | `1` | Hook runtime_diagnostics |
| `CT_ALERT_DRIFT_SPIKE` | `5` | Threshold drift global |
| `CT_ALERT_LOGIN_FAILURES` | `3` | Threshold login fail spike |
| `CT_ALERT_BOT_BLOCK_BURST` | `2` | Threshold bot_block burst |
| `CT_ALERT_HEALTH_DEGR` | `50` | Threshold health degradation |
| `CT_ALERT_CHECKOUT_DROP_PCT` | `20` | % runs llegando a checkout |
| `CT_ALERT_MIN_RUNS_RATIO` | `3` | Mínimo runs para ratios |

---

## 4. FASE 1 — MVP

**Lo más básico**: ver eventos en vivo, estado actual y health score.

### UI `/`

```
┌─────────────────────────────────────┬───────────────────┐
│                                     │  FSM STATE        │
│  Event stream · live (SSE)          │   ┌──────────┐    │
│  ─────────────────────────────────  │   │  LOGIN   │    │
│  04:12:14  STATE   goto             │   └──────────┘    │
│  04:12:14  OBS     qty              │                   │
│  04:12:15  ACTION  click_continuar  │                   │
│  04:12:15  STATE   oauth_detected   │  HEALTH SCORE     │
│  04:12:16  ACTION  login_start      │       70          │
│  04:12:16  NOISE   shot             │   DEGRADED        │
│  04:12:17  OBS     no_email_input   │   ▓▓▓▓▓▓▓░░░     │
│  ...                                │                   │
└─────────────────────────────────────┴───────────────────┘
```

### Endpoints

```bash
GET  /              # UI HTML
GET  /api/v1/state  # {"state": "login", "score": 70, "verdict": "DEGRADED", "slot": 1}
GET  /api/v1/health # {"score": 70, "verdict": "DEGRADED", "event_count": 42, "elapsed_s": 12.3}
GET  /api/v1/stream # SSE — push de cada evento JSONL
```

### Ejemplo SSE

```
event: hello
data: {"connected": true, "ts": 1731463934.5}

data: {"slot": 1, "event": "goto", "kind": "STATE", "state": "browsing", "score": 50}

data: {"slot": 1, "event": "qty", "kind": "OBSERVABILITY", "state": "browsing", "score": 50}

: ping 1731463950
```

---

## 5. FASE 2 — Realtime (4 paneles)

**Visión live completa** del run actual con 4 paneles especializados.

### UI `/realtime`

```
┌─────────────────────────┬─────────────────────────┐
│ STATE LIVE              │ HEALTH GAUGE            │
│                         │                         │
│   ┌─────────────┐       │       72                │
│   │   LOGIN     │       │   DEGRADED              │
│   └─────────────┘       │   ▓▓▓▓▓▓▓░░░           │
│ previous: BROWSING      │ events:42 · 12.3s       │
│                         │                         │
│ History:                │                         │
│ browsing→login via      │                         │
│   oauth_detected        │                         │
└─────────────────────────┼─────────────────────────┘
│ DRIFT PANEL             │ LOOP DETECTOR           │
│  0 drifts               │  qty → qty      18× 🔴 │
│  ─────────────          │  recovery → ... 12× 🟠 │
│  No drifts              │  shot           6×  🟡 │
└─────────────────────────┴─────────────────────────┘
│                Event stream · live                 │
│  ...                                               │
└────────────────────────────────────────────────────┘
```

### Endpoints

```bash
GET /api/v2/state/live      # estado + previous + history (8 últimos)
GET /api/v2/drift/current   # drift_count + lista drifts recientes
GET /api/v2/loops/current   # active_loops (count >=3) + todos los loops
GET /api/v2/health/current  # score + verdict + event_count + elapsed_s
```

---

## 6. FASE 3 — Control (analytics + root cause)

**Análisis profundo** del run actual: por qué falló, qué hacer.

### UI `/control`

```
┌───────────────────┬───────────────────┬───────────────────┐
│ RUN ANALYTICS     │ ROOT CAUSE        │ FAILURE TRACE     │
│ [BLOCKED] [high]  │                   │                   │
│ Duration: 30.0s   │ AKAMAI_BOT_BLOCK  │ login_start  ↓    │
│ Events: 60        │ confidence: high  │ shot         ↓    │
│ Throughput: 2.0/s │ cat: anti_bot     │ no_email_input ↓  │
│ States: 5/5       │                   │ FATAL_bot_blo... ❌│
│ FSM coverage: 80% │ Description:      │ outcome: BLOCKED  │
│ Health: 25        │ Akamai detectó    │                   │
│ Drifts: 2         │ comportamiento de │                   │
│ Outcome: BLOCKED  │ bot tras cred.    │                   │
│                   │                   │                   │
│                   │ Remediation:      │                   │
│                   │ Verificar WebRTC  │                   │
│                   │ IP leak, FP, ...  │                   │
└───────────────────┴───────────────────┴───────────────────┘
│  Event stream · filtered                                   │
│  [STATE] [ACTION] [OBSERVABILITY] [NOISE] [⚠ only drift]   │
│  search: ____________________                              │
│  ...                                                       │
└────────────────────────────────────────────────────────────┘
```

### Endpoints

```bash
GET /api/v3/run/<slot>/analytics
# Devuelve:
# {
#   "analytics": {
#     "status": "blocked",
#     "duration_s": 30,
#     "event_count": 60,
#     "event_throughput": 2.0,
#     "states_visited": ["setup","browsing","login","checkout","exit"],
#     "fsm_coverage_pct": 100,
#     "kind_distribution_pct": {"STATE":6.7, "ACTION":33.3, ...},
#     "severity": "high",
#     "drift_count": 2,
#     "exit_outcome": "BLOCKED"
#   }
# }

GET /api/v3/run/<slot>/root-cause
# Devuelve:
# {
#   "root_cause": {
#     "root_cause": "AKAMAI_BOT_BLOCK",
#     "category": "anti_bot",
#     "description": "Akamai detectó comportamiento de bot tras credenciales",
#     "remediation": "Verificar WebRTC IP leak, fingerprint TZ/UA, ...",
#     "confidence": "high",
#     "failure_event": "FATAL_bot_block_early",
#     "failure_path": ["login_start","shot","no_email_input","FATAL_..."],
#     "state_at_failure": "exit"
#   }
# }

GET /api/v3/events/recent?n=50&kinds=STATE,ACTION&state=login&with_drift=true&search=oauth
# Filtros combinables
```

### Patrones de root cause catalogados (9)

| Pattern → Root cause | Categoría |
|---|---|
| `no_email_input` | OAUTH_NO_EMAIL_INPUT (login_failure) |
| `FATAL_bot_block_early` | AKAMAI_BOT_BLOCK (anti_bot) |
| `bot_block_persisted` | AKAMAI_BOT_BLOCK_PERSISTED (anti_bot) |
| `reset_method_next_failed` | PASSWORD_RESET_FLOW_BROKEN (auth_flow) |
| `recovery_failed_swap_account` | RECOVERY_LOOP_EXHAUSTED (auth_flow) |
| `continuar_js_err` | CONTINUAR_BUTTON_BROKEN (ui_interaction) |
| `pre_flight_skipped_err` | PRE_FLIGHT_HTTP_FAILED (infra) |
| `TIMEOUT_no_checkout` | CHECKOUT_TIMEOUT (timeout) |
| `max_account_swaps_reached` | ACCOUNTS_EXHAUSTED (resource_exhaustion) |

Patrones no catalogados → fallback `FATAL_UNKNOWN` / `OP_FAILED_GENERIC` con confidence=low.

---

## 7. FASE 4 — Industrial (control tower)

**Vista de operaciones** con todos los runs simultáneos, alertas y comparativa histórica.

### UI `/tower`

```
┌──────────────────────────────────────────────────────────────────┐
│ GLOBAL · cross-run                                               │
│ ┌────────┬────────┬────────┬────────┬────────┐                  │
│ │ Active │ Events │ Drift  │ Health │ Alerts │                  │
│ │   3    │  248   │   5    │  61    │   2    │                  │
│ └────────┴────────┴────────┴────────┴────────┘                  │
├──────────────────────────────────────┬───────────────────────────┤
│ MULTI-RUN LIVE                       │ ALERTS                    │
│ ┌─────────────┐ ┌─────────────┐      │ [warn] LOGIN_FAILURE_SPIKE│
│ │ slot 1      │ │ slot 2      │      │  3 runs no_email_input    │
│ │ LOGIN  70/100│ │ BLOCKED 25/100│   │                           │
│ │ ▓▓▓░░       │ │ ▓░░░░       │     │ [critical] BOT_BLOCK_BURST│
│ │ events:42   │ │ events:58   │     │  2 runs subnet quemada    │
│ │ drifts:0    │ │ drifts:3    │     │                           │
│ └─────────────┘ └─────────────┘      │                           │
│ ┌─────────────┐                      │                           │
│ │ slot 3      │                      │ thresholds:               │
│ │ EXIT  37/100│                      │ DRIFT_SPIKE=5             │
│ │ ▓░░░░       │                      │ LOGIN_FAILURES=3          │
│ │ events:32   │                      │ BOT_BLOCK_BURST=2         │
│ │ drifts:2    │                      │                           │
│ └─────────────┘                      │                           │
├──────────────────────────────────────┴───────────────────────────┤
│ HISTORICAL RUNS                                                  │
│  Stats: Total=12 · Events=628 · Drifts=10 · Avg=42 · Blocked=17% │
│  ┌────┬────────┬────────┬───────┬───────┬─────────┬─────────────┐│
│  │Slot│Start   │End     │Events │Drifts │Outcome  │Score        ││
│  ├────┼────────┼────────┼───────┼───────┼─────────┼─────────────┤│
│  │ 1  │04:12:14│04:12:30│ 60    │  1    │ BLOCKED │   37        ││
│  │ 2  │04:12:08│04:12:35│ 32    │  2    │ FATAL   │   42        ││
│  │ 3  │04:11:55│04:12:00│ 8     │  0    │ STOPPED │   30        ││
│  └────┴────────┴────────┴───────┴───────┴─────────┴─────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

### Endpoints

```bash
GET /api/v4/runs/all              # todos los runs activos + global summary
GET /api/v4/drift/global          # drift agregado entre runs
GET /api/v4/alerts                # alertas evaluadas + thresholds
GET /api/v4/history               # runs históricos (filtrable por outcome/slot)
GET /api/v4/history/stats         # estadísticas históricas
GET /api/v4/history/compare/<a>/<b>  # comparativa entre dos slots
```

### Tipos de alerta (5)

| Alerta | Cuándo se dispara | Severidad |
|---|---|---|
| `DRIFT_SPIKE` | Total drifts en runs activos ≥ `CT_ALERT_DRIFT_SPIKE` | warn → critical |
| `LOGIN_FAILURE_SPIKE` | N runs con `no_email_input` ≥ `CT_ALERT_LOGIN_FAILURES` | critical |
| `BOT_BLOCK_BURST` | N runs con `BLOCKED` ≥ `CT_ALERT_BOT_BLOCK_BURST` | critical |
| `CHECKOUT_DROP` | % runs llegando a checkout < `CT_ALERT_CHECKOUT_DROP_PCT` | warn |
| `HEALTH_DEGRADATION` | avg_health_score < `CT_ALERT_HEALTH_DEGR` | warn |

---

## 8. Flujo runtime completo

```
┌────────────────────────────────────────────────────────────────┐
│                _compra_parallel.py (bot REAL, no tocado)       │
└──────────────────────┬─────────────────────────────────────────┘
                       │ log(slot, "event")
                       ▼
┌────────────────────────────────────────────────────────────────┐
│             _compra_logger.py (legacy + hook FASE 4)           │
└──────────────────────┬─────────────────────────────────────────┘
                       │ observe(event, payload)
                       ▼
┌────────────────────────────────────────────────────────────────┐
│       core/observability/runtime_diagnostics.py                │
│  → classify (STATE/ACTION/OBS/NOISE)                           │
│  → drift detect                                                │
│  → health score                                                │
│  → JSONL append → runs/diagnostics_*.jsonl                     │
└──────────────────────┬─────────────────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────────────────────┐
        │  core/dashboard/stream.py (JSONLTailer)      │
        │   poll cada 500ms, ring buffer 500 events    │
        └──────────────────────┬───────────────────────┘
                               │ subscribe(callback)
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
   ┌────────────────────┐               ┌────────────────────┐
   │ aggregator         │               │ SSE broadcast      │
   │  RunStats por slot │               │  asyncio.Queue x N │
   └────────┬───────────┘               └────────┬───────────┘
            │                                     │
            ▼                                     │
   ┌──────────────────────────────────────────────┴───────────┐
   │              core/control_tower/                          │
   │                                                           │
   │   ┌───────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐│
   │   │ server.py │  │analytics │  │  alerts  │  │historical││
   │   │ (FastAPI) │  │ root cau.│  │  engine  │  │  index   ││
   │   └─────┬─────┘  └─────┬────┘  └────┬─────┘  └────┬─────┘│
   │         │              │             │             │      │
   │         └──────────────┴─────────────┴─────────────┘      │
   │         registra 17 endpoints + serve 4 UIs               │
   └──────────────────────┬────────────────────────────────────┘
                          │
                          ▼
                    ┌──────────────┐
                    │   Browser    │
                    │ / /realtime  │
                    │ /control     │
                    │ /tower       │
                    └──────────────┘
```

---

## 9. Tests — 23/23 pasando

```bash
$ python -m pytest tests/test_control_tower.py -v

test_app_imports                            ✓  (17 endpoints registrados)
test_analytics_compute                      ✓  (status=blocked, severity=high)
test_analytics_running_status               ✓  (status=running)
test_analytics_empty                        ✓  (fail-safe ante dict vacío)
test_root_cause_no_email_input              ✓  (OAUTH_NO_EMAIL_INPUT, high conf)
test_root_cause_bot_block                   ✓  (AKAMAI_BOT_BLOCK)
test_root_cause_none_when_no_failure        ✓
test_root_cause_unknown_fallback            ✓  (FATAL_weird → low conf)
test_filter_events                          ✓  (kinds + state + search)
test_alerts_no_runs                         ✓
test_alerts_drift_spike                     ✓  (threshold configurable)
test_alerts_bot_block_burst                 ✓
test_historical_index_creates               ✓  (extract metadata from JSONL)
test_historical_index_stats                 ✓
test_endpoint_state_v1                      ✓
test_endpoint_health_v1                     ✓
test_endpoint_v2_drift                      ✓
test_endpoint_v3_events                     ✓
test_endpoint_v4_runs_all                   ✓
test_endpoint_v4_alerts                     ✓
test_endpoint_v4_history                    ✓
test_ui_routes_render                       ✓  (4 UIs renderizadas)
test_healthz                                ✓

============================== 23 passed in 0.30s ===========================

Test suite global: 85/85 pasando (Control Tower 23 + Dashboard 13 +
                                    Runtime Diagnostics 20 + FSM 29).
```

---

## 10. Resumen ejecutivo

| Restricción del usuario | Verificación |
|---|---|
| No modificar `_compra_parallel.py` | 0 LOC tocados |
| No cambiar FSM | `state_machine.py` intacto (29 tests pasan) |
| No cambiar bot logic | El sistema lee JSONL, no llama al bot |
| Solo lectura archivos JSONL | `JSONLTailer` reutilizado del Flask layer |
| FastAPI backend | ✅ (fastapi 0.136 + uvicorn 0.44) |
| SSE | ✅ `/api/v1/stream` |
| Streaming real time | ✅ poll 500ms + SSE push |
| Plug-in | ✅ launcher separado (`run_control_tower.py`) |
| Apagable | ✅ `OBSERVABILITY_ENABLED=0` |
| Compatibilidad entre fases | ✅ Cada fase añade encima sin romper |

| Métrica final | Valor |
|---|---|
| **Bot tocado** | 0 LOC |
| **FSM tocado** | 0 LOC |
| **Logger tocado** | 0 LOC (hook ya estaba) |
| **dashboard.py tocado** | 0 LOC (no se toca Flask) |
| **Módulos nuevos** | 5 (`server.py`, `analytics.py`, `alerts.py`, `historical.py`, `__init__.py`) |
| **UIs nuevas** | 4 (`mvp.html`, `realtime.html`, `control.html`, `industrial.html`) |
| **Tests añadidos** | 23 |
| **Tests rotos** | 0 |
| **Endpoints HTTP** | 17 (v1: 3, v2: 4, v3: 3, v4: 6, utility: 2, healthz: 1) + 4 UI routes |
| **Launcher** | `run_control_tower.py` |

---

## 11. Coexistencia con dashboard Flask existente

| Sistema | Puerto | Función |
|---|---|---|
| **Flask `dashboard.py`** | 5055 | Dashboard original TM PERROS + capa diagnostics |
| **FastAPI Control Tower** | 5060 | Self-diagnosing en 4 fases |

Ambos leen del **mismo JSONL stream** (no compiten). Pueden correr en paralelo sin conflictos. Ningún recurso compartido en escritura.

---

## 12. Próximos pasos opcionales (no incluidos)

| Idea | Coste | Beneficio |
|---|---|---|
| WebSocket (en vez de SSE) | 1 h | Bidirectional para acciones (pause/resume) |
| Chart.js timeline | 1 h | Health score gráfico temporal |
| Persistencia SQLite del historical | 2 h | Queries cross-day eficientes |
| Discord/Telegram alerts | 1 h | Notificaciones push |
| Comparativa A/B de runs | 2 h | Diff visual de event sequences |

---

*Generado: 2026-05-13 · FastAPI Control Tower con 4 fases incrementales completadas. 85/85 tests pasando.*
