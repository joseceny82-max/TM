"""research_catalog.py — Catálogo MASIVO de mejoras aplicables a TM PERROS.

Cambios respecto a la versión anterior:
- **Todas** las mejoras están visibles por defecto. Ya no se ocultan por
  detectar que existe un módulo instalado.
- Una mejora solo desaparece del catálogo cuando el usuario crea una
  **tarea** vinculada a ella y la marca como `done`.
- Categorías ampliadas (12) con 60+ mejoras basadas en investigación
  pública del mercado de bots de tickets 2025/2026:
    · Akamai Bot Manager + JA4+
    · PerimeterX (HUMAN Security)
    · DataDome, Kasada, Cloudflare Turnstile, Imperva Incapsula
    · Queue-it bypass + AcpAT
    · Fingerprinting avanzado (Canvas/WebGL/Audio/Font/WebRTC)
    · HTTP/3 QUIC, HTTP/2 frames, header ordering
    · Mobile proxies 4G/5G, ISP sticky, residential
    · ML drop prediction, competitor watching
    · Adyen/Stripe/Checkout tokens, 3DS autofill
    · Observability: Sentry, analytics, structured logs
    · Performance: caching, compression, gzip, CDN
    · AI/ML: behavior simulation, captcha AI, anomaly detection

API pública:
  get_catalog()          → lista de mejoras con estado + task asociada
  create_task(id, ...)   → crear tarea para una mejora
  list_tasks(status?)    → todas las tareas o filtradas
  update_task(task_id, patch)
  complete_task(task_id)
  delete_task(task_id)
  get_stats()
"""
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import config
import json_atomic


_TASKS_KEY = "research_tasks.json"
_lock = threading.Lock()


# ═══════════════════════════════════════════
#  CATÁLOGO 2026 (60+ mejoras en 12 categorías)
# ═══════════════════════════════════════════
#
# Cada entrada:
#   id          → slug único (no cambiar)
#   cat         → categoría (para agrupar)
#   cat_icon    → emoji de la categoría
#   name        → nombre corto
#   desc        → descripción técnica
#   difficulty  → fácil | media | difícil
#   impact      → Bajo | Medio | Alto | Muy alto | Máximo
#   time        → estimación humana
#   effort      → S | M | L | XL
#   prerequisites → lista
#   steps       → lista
#   source      → referencia de investigación (web)
#
CATALOG = [

    # ═══════════ 🛡 ANTI-BOT (11) ═══════════
    {"id": "qit_rotation", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Queue-it token rotation",
     "desc": "Rotar tokens Queue-it cada 30 min para evitar detección por reuso de patrones.",
     "difficulty": "media", "impact": "Alto", "time": "2-3 días", "effort": "M",
     "prerequisites": ["Workers con sticky IP", "Capture cookie AcpAT"],
     "steps": ["Capturar AcpAT tras pasar cola", "Pool con TTL 30 min", "Rotar entre workers", "Invalidar al fallar handshake"]},

    {"id": "px_prefarm", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "PerimeterX cookie pre-farming",
     "desc": "Pre-generar cookies _pxhd/_pxvid 15-30 min antes del drop visitando TM de forma orgánica.",
     "difficulty": "difícil", "impact": "Muy alto", "time": "1 semana", "effort": "L",
     "prerequisites": ["Navegador con WebGL spoof", "Proxies ISP"],
     "steps": ["Script visita home+evento", "Scroll/mouse aleatorio 15 min", "Guardar cookies _px*", "Inyectar al disparar"]},

    {"id": "imperva_bypass", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Imperva/Incapsula JS challenge bypass",
     "desc": "Resolver JS challenge _incap_ ejecutando el VM inline sin esperar timeout de 10 s.",
     "difficulty": "difícil", "impact": "Alto", "time": "1-2 semanas", "effort": "XL",
     "prerequisites": ["Node.js worker", "VM sandbox"],
     "steps": ["Capturar JS obfuscado", "Ejecutar en sandbox V8", "Extraer visid_incap_*", "Cachear 20 min"]},

    {"id": "shape_ja4", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "JA4+ TLS fingerprint spoof",
     "desc": "Imitar JA4+ fingerprint de Chrome estable real. JA3 obsoleto desde 2023.",
     "difficulty": "difícil", "impact": "Muy alto", "time": "1 semana", "effort": "L",
     "prerequisites": ["curl-impersonate / tls-client"],
     "steps": ["Usar tls-client Chrome 131+", "Alinear cipher suites order", "Verificar con ja4db.com", "Rotar perfiles"]},

    {"id": "akamai_ab_bypass", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Akamai Bot Manager — sensor data spoof",
     "desc": "Akamai captura sensor_data del cliente JS con señales de mouse/keyboard. Hay que generar payload válido.",
     "difficulty": "difícil", "impact": "Muy alto", "time": "2 semanas", "effort": "XL",
     "prerequisites": ["Playwright real + stealth", "Conocimiento JS Akamai"],
     "steps": ["Interceptar bm.js", "Generar sensor_data humano", "POST a /_bm_ endpoint", "Refrescar cada 30 s"]},

    {"id": "akamai_bmjs_deobf", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Akamai bm.js analyzer + sensor capture/replay",
     "desc": "Vía práctica al sensor_data de Akamai: capturar desde Chrome real vía Playwright + pool con TTL 60s + mutaciones safe al replay. Complementa akamai_ab_bypass.",
     "difficulty": "difícil", "impact": "Máximo", "time": "1-2 semanas", "effort": "L",
     "prerequisites": ["Playwright", "akamai_bmsc"],
     "steps": ["fetch_bm_js + analyze_bm_js (obfuscation score, entropy)",
                "deobfuscate_basic (hex/unicode/char codes)",
                "capture_sensor_data via Playwright CDP (intercepta POST /_bm_/)",
                "replay + mutate_sensor_data sin repetir bytes exactos",
                "diff_bm_js para detectar cambios de versión"]},

    {"id": "datadome_bypass", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "DataDome — stealth browser + mobile proxies",
     "desc": "DataDome combina fingerprint, behavioral + IP reputation. Stack ganador: Camoufox + proxies 4G.",
     "difficulty": "media", "impact": "Alto", "time": "3-5 días", "effort": "M",
     "prerequisites": ["Camoufox / Nodriver", "Mobile proxies"],
     "steps": ["Perfil Camoufox único por worker", "Proxy 4G carrier-level", "Mouse Bezier humano", "Retry on 403 con IP nueva"]},

    {"id": "kasada_bypass", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Kasada KPSDK bypass (ips.js)",
     "desc": "Kasada calcula x-kpsdk-ct token en cliente. Requiere ejecutar JS real o solver API externa.",
     "difficulty": "difícil", "impact": "Alto", "time": "1-2 semanas", "effort": "XL",
     "prerequisites": ["Browser real + JS", "o solver: NSLSolver / CapSolver Kasada"],
     "steps": ["Detectar header x-kpsdk", "Ejecutar ips.js en VM", "Inyectar ct/cd tokens", "Renovar cada request"]},

    {"id": "cf_challenge_bypass", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Cloudflare Challenge/UAM bypass",
     "desc": "Cloudflare managed challenge (5s interstitial). Resolver con FlareSolverr o SeleniumBase UC Mode.",
     "difficulty": "media", "impact": "Medio", "time": "2-3 días", "effort": "M",
     "prerequisites": ["FlareSolverr docker"],
     "steps": ["Montar FlareSolverr:5055", "Proxy reverso en cookie_warmer", "Cache cf_clearance cookie", "Reuse 30 min"]},

    {"id": "http2_fingerprint", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "HTTP/2 fingerprint (Akamai H2 hash)",
     "desc": "Akamai identifica scrapers por SETTINGS frame, WINDOW_UPDATE size, y header order pseudo-header.",
     "difficulty": "difícil", "impact": "Alto", "time": "1 semana", "effort": "L",
     "prerequisites": ["httpcloak / tls-client"],
     "steps": ["Replicar SETTINGS de Chrome", "Alinear WINDOW_UPDATE=6291456", "Pseudo-header order :method:authority:scheme:path", "Verificar con scrapfly"]},

    {"id": "header_ordering", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Header ordering consistente con browser",
     "desc": "Orden de headers debe coincidir con el del User-Agent. requests de Python los reordena automáticamente.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["tls-client / httpcloak / curl_cffi"],
     "steps": ["Usar OrderedDict", "Copiar orden real de Chrome 131", "No añadir headers que Chrome no envía", "Verificar con httpbin"]},

    {"id": "webrtc_leak_block", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "WebRTC IP leak protection",
     "desc": "Detectores comparan IP del proxy vs IP de WebRTC STUN. Hay que bloquear o spoofear.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Playwright con flags"],
     "steps": ["--disable-features=WebRtcHideLocalIpsWithMdns", "Policy disable_non_proxied_udp", "O spoofear STUN a IP del proxy"]},


    # ═══════════ 🧠 CAPTCHA (8) ═══════════
    {"id": "recaptcha_v3", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "reCAPTCHA v3 score evasion (score > 0.7)",
     "desc": "v3 no muestra challenge — da score 0-1. Simular mouse humano + tiempo de foco para ganar score alto.",
     "difficulty": "media", "impact": "Alto", "time": "3-5 días", "effort": "M",
     "prerequisites": ["Playwright stealth"],
     "steps": ["Mouse Bezier humano", "Foco 3-5 s antes click", "Idle mínimo 2 s", "Medir score con endpoint dummy"]},

    {"id": "hcaptcha_ent", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "hCaptcha Enterprise solver",
     "desc": "hCaptcha Enterprise en TM resale/secondary. CapSolver resuelve con ~$2/1000.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Cuenta CapSolver"],
     "steps": ["Crear HCaptchaTaskProxyless", "Polling 3 s", "Inyectar h-captcha-response", "Enviar form"]},

    {"id": "turnstile_solve", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Cloudflare Turnstile auto-solver",
     "desc": "Turnstile crece en 2026. Integrar solver (CapSolver/2Captcha/NSLSolver) preventivo.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Cuenta CapSolver"],
     "steps": ["Detectar widget", "Enviar TurnstileTaskProxyless", "Inyectar cf-turnstile-response"]},

    {"id": "arkose_solve", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Arkose FunCaptcha (Arkose Labs)",
     "desc": "Arkose usa juegos 3D. TM secondary market lo usa. CapSolver AI lo resuelve con ~$3/1000.",
     "difficulty": "media", "impact": "Medio", "time": "2 días", "effort": "M",
     "prerequisites": ["Cuenta CapSolver"],
     "steps": ["Detectar public_key Arkose", "Enviar FunCaptchaTask", "Polling 10-20 s", "Inyectar verification_token"]},

    {"id": "press_hold_captcha", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Press-and-hold CAPTCHA (Kasada) bypass",
     "desc": "Kasada a veces muestra \"press and hold\" button. Requiere simular hold 3-5 s con mouse real.",
     "difficulty": "media", "impact": "Medio", "time": "2 días", "effort": "M",
     "prerequisites": ["Playwright + mouse.down/up"],
     "steps": ["Detectar iframe kasada", "mouse.down sobre botón", "Wait random 3-5 s", "mouse.up"]},

    {"id": "captcha_ai_local", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Local AI solver (imagen/audio) — offline",
     "desc": "Modelo YOLO/ResNet local resuelve reCAPTCHA v2 image/audio sin depender de solver externo.",
     "difficulty": "difícil", "impact": "Alto", "time": "2 semanas", "effort": "XL",
     "prerequisites": ["PyTorch/ONNX", "Modelo pre-entrenado recaptcha-solver"],
     "steps": ["Integrar modelo ONNX", "Inferencia sobre imágenes tiles", "Audio speech-to-text whisper", "Fallback a solver externo"]},

    {"id": "captcha_warm_cache", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "CAPTCHA token pre-cache",
     "desc": "Resolver CAPTCHA 30 s antes del drop y cachear el token para usar instantáneo al añadir al carrito.",
     "difficulty": "fácil", "impact": "Alto", "time": "1 día", "effort": "S",
     "prerequisites": ["captcha_manager"],
     "steps": ["Arrancar solver 30 s antes", "Guardar token con TTL 2 min", "Inyectar en cart", "Invalidar post-uso"]},

    {"id": "captcha_multi_provider", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Rotación multi-provider CAPTCHA",
     "desc": "Si CapSolver falla → fallback a 2Captcha → Anti-Captcha → CapMonster. Evita downtime en drops.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["4 API keys configuradas"],
     "steps": ["Orden preferencia por coste/speed", "Retry tras timeout", "Mark dead tras 3 fallos", "Dashboard balance per-provider"]},


    # ═══════════ 🔀 PROXIES (10) ═══════════
    {"id": "isp_sticky", "cat": "Proxies", "cat_icon": "🔀",
     "name": "ISP sticky 30 min para Queue-it",
     "desc": "Mantener IP fija durante toda la cola virtual. Cambio de IP invalida AcpAT.",
     "difficulty": "fácil", "impact": "Alto", "time": "1-2 días", "effort": "S",
     "prerequisites": ["Proveedor ISP (NetNut/Bright/SOAX)"],
     "steps": ["session-id en URL proxy", "TTL 30 min", "1 IP por worker", "Log cambios de IP"]},

    {"id": "proxy_bench", "cat": "Proxies", "cat_icon": "🔀",
     "name": "Benchmark proxies TM-specific",
     "desc": "Medir latencia HEAD contra ticketmaster.com antes de usar. Descartar >800 ms.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Pool proxies 100+"],
     "steps": ["GET / con proxy", "Medir TTFB", "Marcar 'fast' si <400 ms", "Worker pool sort"]},

    {"id": "geo_match", "cat": "Proxies", "cat_icon": "🔀",
     "name": "Geo-match proxy ↔ dominio TM",
     "desc": "Proxy español a TM.es, UK a TM.co.uk, etc. Evita redirects + detección geo.",
     "difficulty": "fácil", "impact": "Alto", "time": "1 día", "effort": "S",
     "prerequisites": ["Proxies multi-país"],
     "steps": ["Mapa dominio→país", "Selector por país", "Fallback genérico"]},

    {"id": "mobile_4g_5g", "cat": "Proxies", "cat_icon": "🔀",
     "name": "Mobile 4G/5G proxies (carrier-level)",
     "desc": "IPs reales de carriers móviles. Más caras pero casi indetectables. Ideal drops top-tier.",
     "difficulty": "fácil", "impact": "Muy alto", "time": "2 días", "effort": "M",
     "prerequisites": ["Cuenta ProxyEmpire / IPRoyal Mobile"],
     "steps": ["Configurar endpoint mobile", "Rotation por request o sticky", "Target por carrier (Verizon/T-Mobile)", "Benchmark vs ISP"]},

    {"id": "proxy_reputation", "cat": "Proxies", "cat_icon": "🔀",
     "name": "Reputation scoring — blacklist auto",
     "desc": "Track fallos por IP. Si 2 errores 403 en 10 min → blacklist 1 h. Evita reuso de IP quemada.",
     "difficulty": "fácil", "impact": "Alto", "time": "2 días", "effort": "M",
     "prerequisites": ["proxy_health"],
     "steps": ["Log fail/success per-IP", "Score con decay temporal", "Blacklist automático", "Alert Discord cuando pool <20%"]},

    {"id": "ipv6_rotation", "cat": "Proxies", "cat_icon": "🔀",
     "name": "IPv6 rotation (pools /64 baratos)",
     "desc": "IPv6 da pools enormes /64 por pocos €. Útil cuando IPv6 está habilitado en el target.",
     "difficulty": "media", "impact": "Medio", "time": "3 días", "effort": "M",
     "prerequisites": ["VPS con /64 IPv6"],
     "steps": ["Configurar interfaz con /64", "Random sub-IP cada request", "DNS AAAA prefetch", "Verificar que TM acepta IPv6"]},

    {"id": "proxy_chaining", "cat": "Proxies", "cat_icon": "🔀",
     "name": "Proxy chaining (2 hops)",
     "desc": "Encadenar ISP → residencial para disimular trazas. Más latencia pero más difícil de detectar.",
     "difficulty": "media", "impact": "Medio", "time": "3 días", "effort": "M",
     "prerequisites": ["2 proveedores proxies"],
     "steps": ["ISP-proxy como salida final", "Residential como intermedio", "Túnel SOCKS5 encadenado", "Medir latencia extra"]},

    {"id": "sticky_session_id", "cat": "Proxies", "cat_icon": "🔀",
     "name": "Sticky session-id determinístico",
     "desc": "Usar hash(account_id + event_id) como session-id. La misma cuenta siempre misma IP → natural.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Proveedor soporte session-id custom"],
     "steps": ["session = sha1(account+event)[:8]", "user-pass__session-XXX format", "Cache mapping", "Log cambios"]},

    {"id": "proxy_health_drop", "cat": "Proxies", "cat_icon": "🔀",
     "name": "Pre-warm proxy pool antes del drop",
     "desc": "30 min antes del drop, hacer HEAD contra TM con cada proxy del pool. Descartar dead.",
     "difficulty": "fácil", "impact": "Alto", "time": "1 día", "effort": "S",
     "prerequisites": ["proxy_health + scheduler"],
     "steps": ["Scheduler T-30min", "HEAD con timeout 3 s", "Score cada proxy", "Pool ordenado por score"]},

    {"id": "proxy_per_account", "cat": "Proxies", "cat_icon": "🔀",
     "name": "1 proxy fijo por cuenta TM",
     "desc": "Asignar proxy estable a cada cuenta TM. Cambios bruscos de IP por cuenta = ban.",
     "difficulty": "fácil", "impact": "Alto", "time": "1 día", "effort": "S",
     "prerequisites": ["Mapa accounts+proxies"],
     "steps": ["Tabla account_id→proxy_id", "Mantener 30 días min", "Rotar solo si proxy muere", "Log in audit"]},


    # ═══════════ 🧪 BROWSERS (9) ═══════════
    {"id": "camoufox", "cat": "Browsers", "cat_icon": "🧪",
     "name": "Camoufox — Firefox antidetect",
     "desc": "Fork de Firefox con fingerprint aleatorio por worker. WebGL+Canvas+Font únicos.",
     "difficulty": "fácil", "impact": "Medio", "time": "2-3 días", "effort": "M",
     "prerequisites": ["Python Playwright"],
     "steps": ["Descargar camoufox", "Perfil por worker (seed)", "Integrar con Playwright"]},

    {"id": "patchright", "cat": "Browsers", "cat_icon": "🧪",
     "name": "Patchright — Chromium antidetect",
     "desc": "Playwright patcheado quitando CDP leaks (Runtime.enable, etc). Más rápido que vanilla + stealth JS.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["pip install patchright"],
     "steps": ["Importar patchright en vez de playwright", "Verificar con creepjs", "Launch con --disable-blink-features=AutomationControlled"]},

    {"id": "multilogin_api", "cat": "Browsers", "cat_icon": "🧪",
     "name": "Multilogin API REST orchestration",
     "desc": "Orquestar 50+ perfiles Multilogin vía API REST para paralelización total. $159-299/mes.",
     "difficulty": "media", "impact": "Muy alto", "time": "1 semana", "effort": "L",
     "prerequisites": ["Licencia Multilogin Scale"],
     "steps": ["Batch profiles", "POST /profile/f/{id}/start", "Connect Playwright al CDP", "Close tras uso"]},

    {"id": "canvas_spoof", "cat": "Browsers", "cat_icon": "🧪",
     "name": "Canvas fingerprint aleatorio por worker",
     "desc": "Canvas toDataURL devuelve hash único. Perturbar 1 px por frame → hash distinto sin romper render.",
     "difficulty": "media", "impact": "Alto", "time": "3 días", "effort": "M",
     "prerequisites": ["Playwright + init script JS"],
     "steps": ["Hook CanvasRenderingContext2D.getImageData", "Añadir noise ±1 px", "Verificar creepjs canvas hash cambia", "Seed por worker"]},

    {"id": "webgl_spoof", "cat": "Browsers", "cat_icon": "🧪",
     "name": "WebGL vendor/renderer aleatorio",
     "desc": "WebGL.getParameter() revela GPU exacta. Randomizar renderer entre NVIDIA/AMD/Intel por worker.",
     "difficulty": "media", "impact": "Alto", "time": "2 días", "effort": "M",
     "prerequisites": ["Init script JS"],
     "steps": ["Pool realistic GPUs", "Hook getParameter(37446/37447)", "Persist per worker", "Verificar fingerprint cambia"]},

    {"id": "audio_ctx_spoof", "cat": "Browsers", "cat_icon": "🧪",
     "name": "AudioContext fingerprint spoof",
     "desc": "AudioContext.createOscillator + hash devuelve fingerprint único HW. Perturbar con noise imperceptible.",
     "difficulty": "media", "impact": "Medio", "time": "2 días", "effort": "M",
     "prerequisites": ["Init script JS"],
     "steps": ["Hook getChannelData/getFloatFrequencyData", "Add noise ±0.0001", "Seed per worker"]},

    {"id": "font_enum_spoof", "cat": "Browsers", "cat_icon": "🧪",
     "name": "Font enumeration spoof",
     "desc": "document.fonts.check devuelve fuentes instaladas. Randomizar set de fuentes comunes por worker.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Init script JS"],
     "steps": ["Pool 150 fuentes realistas", "Random 60-80 per worker", "Hook document.fonts.check", "Verificar creepjs"]},

    {"id": "battery_api_spoof", "cat": "Browsers", "cat_icon": "🧪",
     "name": "Battery / DeviceMotion / Connection spoof",
     "desc": "APIs modernas revelan device info. Spoofear battery level, deviceMemory, hardwareConcurrency, network type.",
     "difficulty": "fácil", "impact": "Bajo", "time": "1 día", "effort": "S",
     "prerequisites": ["Init script JS"],
     "steps": ["navigator.deviceMemory=8", "hardwareConcurrency=4/8/12/16 random", "Battery API variable", "Connection.effectiveType=4g"]},

    {"id": "timezone_locale_match", "cat": "Browsers", "cat_icon": "🧪",
     "name": "Timezone/Locale coherente con proxy",
     "desc": "Proxy ES + navigator.language=en-US = flag. Alinear timezone + lang + Accept-Language con país del proxy.",
     "difficulty": "fácil", "impact": "Alto", "time": "1 día", "effort": "S",
     "prerequisites": ["Playwright context options"],
     "steps": ["Geo-IP lookup proxy", "Timezone Europe/Madrid si ES", "locale='es-ES'", "Accept-Language coherente"]},


    # ═══════════ 🍪 COOKIES / SESSIONS (5) ═══════════
    {"id": "cookie_warm", "cat": "Cookies", "cat_icon": "🍪",
     "name": "Cookie warming sintético multi-dominio",
     "desc": "Generar cookies TM 15-30 min antes del drop con tráfico orgánico. Multiplica éxito.",
     "difficulty": "fácil", "impact": "Alto", "time": "2-3 días", "effort": "M",
     "prerequisites": ["Headless browser + ISP proxy"],
     "steps": ["Visita 3-5 páginas TM", "Scroll aleatorio natural", "Persistir cookies+storage", "Inyectar al disparar"]},

    {"id": "acpat_inject", "cat": "Cookies", "cat_icon": "🍪",
     "name": "AcpAT Queue-it injection pub/sub",
     "desc": "Capturar AcpAT de 1 worker que pasó cola e inyectar en N workers → saltan cola.",
     "difficulty": "media", "impact": "Máximo", "time": "4-5 días", "effort": "M",
     "prerequisites": ["Worker con sticky IP"],
     "steps": ["Worker 1 pasa cola → capture AcpAT", "Pub/sub a demás", "Set cookie en N", "Checkout simultáneo"]},

    {"id": "localstorage_prefarm", "cat": "Cookies", "cat_icon": "🍪",
     "name": "localStorage / sessionStorage prefarm",
     "desc": "TM guarda state en localStorage (lastSeen, experiments). Replicar en worker fresco ayuda.",
     "difficulty": "fácil", "impact": "Medio", "time": "2 días", "effort": "S",
     "prerequisites": ["Playwright evaluate"],
     "steps": ["Dump localStorage tras sesión real", "Replay en worker nuevo", "Match keys exp_* / user_*", "Refresh cada 24 h"]},

    {"id": "third_party_cookies", "cat": "Cookies", "cat_icon": "🍪",
     "name": "3rd-party cookies (Google/Facebook) warm",
     "desc": "Visitar google.com/facebook.com antes de TM → crea 3rd-party cookies que TM espera en usuarios reales.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Browser real con 3rd-party enabled"],
     "steps": ["Visit google.com + login fake", "Visit facebook.com", "Wait 60 s con scroll", "Luego ir a TM"]},

    {"id": "session_persistence", "cat": "Cookies", "cat_icon": "🍪",
     "name": "Session persistence Redis-based",
     "desc": "Guardar storage_state completo de Playwright en Redis con TTL. Reuse entre workers.",
     "difficulty": "media", "impact": "Alto", "time": "3 días", "effort": "M",
     "prerequisites": ["Redis"],
     "steps": ["Save storage_state a Redis JSON", "TTL 24 h", "Load storage_state al launch", "Invalidate si fails"]},


    # ═══════════ 💳 PAYMENT (5) ═══════════
    {"id": "adyen_token", "cat": "Payment", "cat_icon": "💳",
     "name": "Adyen pre-tokenize (recurringDetailReference)",
     "desc": "Pre-tokenizar tarjeta en Adyen antes del drop → checkout < 2 s.",
     "difficulty": "media", "impact": "Alto", "time": "3-4 días", "effort": "M",
     "prerequisites": ["Adyen test key", "Tarjeta propia"],
     "steps": ["Cargar Adyen JS", "Enviar tarjeta a Checkout API", "Obtener recurringDetailReference", "Cache y usar en checkout"]},

    {"id": "multi_card_rotation", "cat": "Payment", "cat_icon": "💳",
     "name": "Rotación multi-tarjeta anti-fraude",
     "desc": "Rotar tarjetas con cooldown 24-168 h según outcome para evitar patrones bancarios.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Multi cuentas+tarjetas"],
     "steps": ["Mapa cuenta→N tarjetas", "Cooldown por outcome", "Pick least-used", "Auto-blacklist on decline"]},

    {"id": "otp_3ds_imap", "cat": "Payment", "cat_icon": "💳",
     "name": "Autofill 3DS OTP (IMAP/SMS)",
     "desc": "Lector automático de código 3DS desde email IMAP o webhook SMS. Checkout sin intervención.",
     "difficulty": "media", "impact": "Alto", "time": "3-4 días", "effort": "M",
     "prerequisites": ["Gmail app-password / Twilio webhook"],
     "steps": ["IMAP poll INBOX", "Regex patterns banco", "Extraer 6 dígitos", "Inyectar en form 3DS"]},

    {"id": "apple_google_pay", "cat": "Payment", "cat_icon": "💳",
     "name": "Apple Pay / Google Pay tokens",
     "desc": "Apple/Google Pay vía Playwright con cert digital evita 3DS en la mayoría de bancos.",
     "difficulty": "difícil", "impact": "Muy alto", "time": "2 semanas", "effort": "XL",
     "prerequisites": ["Apple Developer cert", "Google Pay merchant"],
     "steps": ["Setup PaymentRequest API", "Token encryption client-side", "Inyectar en TM checkout", "Test en sandbox"]},

    {"id": "paypal_checkout", "cat": "Payment", "cat_icon": "💳",
     "name": "PayPal express checkout automation",
     "desc": "TM acepta PayPal. Automatizar PayPal login (con 2FA cache) + express button reduce 3DS.",
     "difficulty": "media", "impact": "Medio", "time": "1 semana", "effort": "L",
     "prerequisites": ["Cuenta PayPal business"],
     "steps": ["Login PayPal con cookies cached", "Click express", "Accept terms", "Return to TM"]},


    # ═══════════ 🎯 STRATEGY (8) ═══════════
    {"id": "polling_rotate", "cat": "Strategy", "cat_icon": "🎯",
     "name": "Polling rotatorio multi-evento",
     "desc": "Worker itera N eventos buscando stock → maximiza CPU util durante drops simultáneos.",
     "difficulty": "fácil", "impact": "Alto", "time": "2 días", "effort": "M",
     "prerequisites": ["Event scheduler"],
     "steps": ["Cola prioritaria", "Worker saca → check stock", "Si stock → dispara", "Sigue con siguiente"]},

    {"id": "multi_domain", "cat": "Strategy", "cat_icon": "🎯",
     "name": "Distributed multi-domain (25 TM)",
     "desc": "Lanzar en 25 dominios TM simultáneamente. Tours internacionales → éxito x25.",
     "difficulty": "difícil", "impact": "Máximo", "time": "1-2 semanas", "effort": "XL",
     "prerequisites": ["Proxies multi-país", "Cuentas multi-dominio"],
     "steps": ["Detectar tour internacional", "Worker por dominio", "Geo-match proxy↔dominio", "Priorizar menor cola"]},

    {"id": "queue_monitor", "cat": "Strategy", "cat_icon": "🎯",
     "name": "Queue position monitor multi-perfil",
     "desc": "Monitor de posición en cola de todos los perfiles — priorizar el que va ganando.",
     "difficulty": "media", "impact": "Medio", "time": "3-4 días", "effort": "M",
     "prerequisites": ["WebSocket dashboard"],
     "steps": ["Worker reporta posición cada 10 s", "WS push", "Heatmap per domain", "Mover recursos al líder"]},

    {"id": "discovery_adaptive", "cat": "Strategy", "cat_icon": "🎯",
     "name": "Discovery adaptive polling (1 Hz)",
     "desc": "Polling Discovery API a 1 Hz cuando <5 min al drop, 30 s si <30 min, normal fuera.",
     "difficulty": "fácil", "impact": "Alto", "time": "2 días", "effort": "M",
     "prerequisites": ["Discovery API key"],
     "steps": ["Ventana temporal por evento", "Intervalo adaptativo", "Auto-notify on status change", "Max 10 events/tick"]},

    {"id": "ml_drop_prediction", "cat": "Strategy", "cat_icon": "🎯",
     "name": "ML drop prediction (features históricos)",
     "desc": "Entrenar XGBoost con features (venue size, artist popularity, day, hour) para predecir hora exacta de drop.",
     "difficulty": "difícil", "impact": "Alto", "time": "3 semanas", "effort": "XL",
     "prerequisites": ["Dataset drops pasados", "scikit-learn"],
     "steps": ["Collect 1000+ drops históricos", "Feature engineering", "Train XGBoost", "Serve predictions al dashboard"]},

    {"id": "competitor_watching", "cat": "Strategy", "cat_icon": "🎯",
     "name": "Competitor monitoring (StubHub/Viagogo)",
     "desc": "Scrape StubHub/Viagogo cada 5 min. Si un evento aparece ahí → señal temprana de drop en TM.",
     "difficulty": "media", "impact": "Medio", "time": "1 semana", "effort": "L",
     "prerequisites": ["Scraper stubhub"],
     "steps": ["Scraper listings por artista", "Diff vs última captura", "Nuevo listing → alerta", "Correlacionar con TM Discovery"]},

    {"id": "early_access_presale", "cat": "Strategy", "cat_icon": "🎯",
     "name": "Presale code farming (AMEX/VISA/fan club)",
     "desc": "Cada evento tiene varios presale codes (AMEX, VISA, artista fan club). Farmer los captura antes del drop público.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "L",
     "prerequisites": ["Cuentas multi-tier TM"],
     "steps": ["DB de presale codes", "Scraping Reddit r/ticketmaster", "Intento con cada code", "Siguiente tier tras fallo"]},

    {"id": "browser_pool", "cat": "Strategy", "cat_icon": "🎯",
     "name": "Browser pool pre-warmed",
     "desc": "2-3 contextos Playwright ya abiertos con cookies PX frescas → tiempo de arranque < 500 ms.",
     "difficulty": "media", "impact": "Alto", "time": "3 días", "effort": "M",
     "prerequisites": ["Playwright + memoria"],
     "steps": ["Manager background", "Inject PX cookies", "TTL 5 min", "Acquire/release pattern"]},


    # ═══════════ 🚀 PERFORMANCE (6) ═══════════
    {"id": "http3_quic", "cat": "Performance", "cat_icon": "🚀",
     "name": "HTTP/3 QUIC support (menos latencia)",
     "desc": "HTTP/3 QUIC reduce latencia 20-40 % vs TCP. TM usa HTTP/3 en edge. Clientes deben soportarlo.",
     "difficulty": "difícil", "impact": "Medio", "time": "1 semana", "effort": "L",
     "prerequisites": ["httpcloak / quiche"],
     "steps": ["pip install aioquic", "HTTP/3 handshake", "Alpn h3", "Fallback TCP H2"]},

    {"id": "connection_pooling", "cat": "Performance", "cat_icon": "🚀",
     "name": "Connection pooling keep-alive",
     "desc": "Reutilizar conexiones TCP/TLS entre requests. Ahorra ~200 ms por conexión.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["requests.Session"],
     "steps": ["HTTPAdapter pool_connections=100", "Keep-alive headers", "Timeout 10 s", "Reuse between flow steps"]},

    {"id": "dns_prefetch", "cat": "Performance", "cat_icon": "🚀",
     "name": "DNS prefetch + DoH",
     "desc": "Prefetch DNS de ticketmaster.* y CDN Akamai antes del drop. DoH evita DNS interception.",
     "difficulty": "fácil", "impact": "Bajo", "time": "1 día", "effort": "S",
     "prerequisites": ["dnslib / aiodns"],
     "steps": ["Lookup ticketmaster.{com,es,de,...} T-5min", "Cache 5 min", "DoH via Cloudflare 1.1.1.1", "IPs prioritarias en /etc/hosts-like"]},

    {"id": "cdn_edge_cache", "cat": "Performance", "cat_icon": "🚀",
     "name": "Edge POP selection (Akamai)",
     "desc": "TM usa Akamai edge. Conectar al POP más cercano manualmente via edgehost resolver.",
     "difficulty": "media", "impact": "Bajo", "time": "2 días", "effort": "M",
     "prerequisites": ["Akamai edge IPs"],
     "steps": ["Scan Akamai edge IPs", "Ping todos", "Route traffic al min latencia", "Refresh cada 24 h"]},

    {"id": "parallel_flows", "cat": "Performance", "cat_icon": "🚀",
     "name": "Parallel flows asyncio + uvloop",
     "desc": "asyncio + uvloop da 2-3x throughput vs threading para I/O bound (HTTP).",
     "difficulty": "media", "impact": "Medio", "time": "1 semana", "effort": "L",
     "prerequisites": ["aiohttp / httpx async"],
     "steps": ["Convert flows a async", "uvloop install", "asyncio.gather workers", "Benchmark vs threading"]},

    {"id": "gzip_brotli", "cat": "Performance", "cat_icon": "🚀",
     "name": "Brotli + gzip compression",
     "desc": "Brotli responses hasta 20 % menos tamaño que gzip. TM sirve br cuando Accept-Encoding lo incluye.",
     "difficulty": "fácil", "impact": "Bajo", "time": "1 día", "effort": "S",
     "prerequisites": ["pip install brotli"],
     "steps": ["Accept-Encoding: br, gzip", "Decompress responses", "Benchmark size/speed"]},


    # ═══════════ 📊 OBSERVABILITY (5) ═══════════
    {"id": "sentry_integration", "cat": "Observability", "cat_icon": "📊",
     "name": "Sentry error tracking",
     "desc": "Sentry captura excepciones con breadcrumbs + context. Free tier: 5k events/mes suficiente.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Cuenta Sentry"],
     "steps": ["sentry_sdk.init(dsn)", "FlaskIntegration", "capture_exception en workers", "Release tagging"]},

    {"id": "structured_logging", "cat": "Observability", "cat_icon": "📊",
     "name": "Structured logging (JSON) + Loki",
     "desc": "Logs JSON permiten queries complejas. Loki + Grafana para búsqueda/dashboards.",
     "difficulty": "media", "impact": "Medio", "time": "3 días", "effort": "M",
     "prerequisites": ["Loki instance o Grafana Cloud"],
     "steps": ["python-json-logger", "Log all steps con flow_id", "Ship a Loki", "Dashboard Grafana"]},

    {"id": "prometheus_metrics", "cat": "Observability", "cat_icon": "📊",
     "name": "Prometheus metrics + Grafana",
     "desc": "Counters (requests, success, fail), histograms (latency), gauges (pool size). Dashboard tiempo real.",
     "difficulty": "media", "impact": "Medio", "time": "3 días", "effort": "M",
     "prerequisites": ["prometheus_client"],
     "steps": ["/metrics endpoint", "Counter per step", "Histogram per flow", "Grafana dashboard"]},

    {"id": "audit_replay", "cat": "Observability", "cat_icon": "📊",
     "name": "Flow replay + timeline",
     "desc": "Cada flow_id guardado con todos los pasos + screenshots. Replay visual para debugging.",
     "difficulty": "media", "impact": "Medio", "time": "1 semana", "effort": "L",
     "prerequisites": ["audit_log + screenshots"],
     "steps": ["Capturar screenshot en cada step", "Storage en /data/replays/{flow_id}", "UI timeline", "Download ZIP"]},

    {"id": "cost_tracking", "cat": "Observability", "cat_icon": "📊",
     "name": "Cost tracker — proxies + solver + compute",
     "desc": "Track $$ gastado por flow: proxy bandwidth + CAPTCHA solve + compute time. ROI per drop.",
     "difficulty": "fácil", "impact": "Medio", "time": "2 días", "effort": "M",
     "prerequisites": ["Pricing providers"],
     "steps": ["Hook proxy_health (MB used)", "Hook captcha_manager (solves)", "Compute hours per flow", "Dashboard $ per event"]},


    # ═══════════ 🤖 AI / ML (5) ═══════════
    {"id": "mouse_bezier_ai", "cat": "AI/ML", "cat_icon": "🤖",
     "name": "Mouse movement Bezier realista (GAN)",
     "desc": "Entrenar GAN con grabaciones de mouse humano real. Genera trayectorias indetectables por ML anti-bot.",
     "difficulty": "difícil", "impact": "Alto", "time": "3 semanas", "effort": "XL",
     "prerequisites": ["Dataset mouse traces", "PyTorch"],
     "steps": ["Record 100 h mouse humano", "Train mouse-GAN", "Inferencia generando paths", "Integrar Playwright"]},

    {"id": "typing_cadence_ml", "cat": "AI/ML", "cat_icon": "🤖",
     "name": "Typing cadence humano (ML)",
     "desc": "Delay entre teclas sigue distribución gaussiana + clustering (palabras rápidas, pausas reflexión).",
     "difficulty": "media", "impact": "Medio", "time": "1 semana", "effort": "M",
     "prerequisites": ["Dataset typing logs"],
     "steps": ["Analizar patrones humanos", "Gaussian μ=150 ms σ=50", "Clustering palabras", "Aplicar a password fields"]},

    {"id": "behavior_chain", "cat": "AI/ML", "cat_icon": "🤖",
     "name": "Markov chain de navegación",
     "desc": "Modelar secuencia 'home→search→event→buy' con probabilidades reales. Evita patrón de bot que salta directo.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "L",
     "prerequisites": ["Analytics sessions reales"],
     "steps": ["Build transition matrix", "Sample desde homepage", "Dwell time por página", "Exit patterns"]},

    {"id": "captcha_cv_solver", "cat": "AI/ML", "cat_icon": "🤖",
     "name": "Computer vision CAPTCHA solver",
     "desc": "ResNet/YOLO entrenado con dataset propio de CAPTCHAs TM. No depender de solver externo.",
     "difficulty": "difícil", "impact": "Alto", "time": "1 mes", "effort": "XL",
     "prerequisites": ["GPU + dataset labelled"],
     "steps": ["Collect 10k CAPTCHA samples", "Label con Label Studio", "Train YOLO", "Serve ONNX inference"]},

    {"id": "anomaly_detection", "cat": "AI/ML", "cat_icon": "🤖",
     "name": "Anomaly detection de respuestas TM",
     "desc": "Autoencoder detecta cuando TM responde con HTML diferente (honeypot, silent ban). Alerta inmediata.",
     "difficulty": "difícil", "impact": "Medio", "time": "2 semanas", "effort": "XL",
     "prerequisites": ["Dataset responses OK"],
     "steps": ["Encode N responses OK", "Train autoencoder", "Reconstruction error → anomaly", "Alert Discord"]},


    # ═══════════ ⚙️ INFRA / DEVOPS (4) ═══════════
    {"id": "redis_pubsub", "cat": "Infra", "cat_icon": "⚙️",
     "name": "Redis pub/sub distribuido",
     "desc": "Reemplazar threading.Condition por Redis pub/sub → AcpAT injection funciona cross-host.",
     "difficulty": "media", "impact": "Medio", "time": "3 días", "effort": "M",
     "prerequisites": ["Redis / Upstash"],
     "steps": ["Redis publish on capture", "Subscribers en cada host", "Fallback local si Redis down", "Latency <50 ms"]},

    {"id": "docker_deploy", "cat": "Infra", "cat_icon": "⚙️",
     "name": "Dockerize + compose (N workers)",
     "desc": "Multi-container: dashboard, redis, N workers, playwright browsers. Escalable + reproducible.",
     "difficulty": "media", "impact": "Medio", "time": "3 días", "effort": "M",
     "prerequisites": ["Docker + compose"],
     "steps": ["Dockerfile slim Python", "compose.yml con redis+browsers", "Workers con scale=N", "Volumes para data/"]},

    {"id": "kubernetes_ha", "cat": "Infra", "cat_icon": "⚙️",
     "name": "Kubernetes HA (multi-node)",
     "desc": "Deploy a k3s/GKE para HA. HPA escala workers según carga. Ideal para drops gigantes.",
     "difficulty": "difícil", "impact": "Alto", "time": "2 semanas", "effort": "XL",
     "prerequisites": ["Cluster k8s"],
     "steps": ["Helm chart", "HPA per-worker", "Shared Redis", "Ingress TLS"]},

    {"id": "cicd_gh_actions", "cat": "Infra", "cat_icon": "⚙️",
     "name": "CI/CD GitHub Actions — tests + deploy",
     "desc": "Push a main → tests → build Docker → deploy. Rollback automático si fallan tests.",
     "difficulty": "fácil", "impact": "Medio", "time": "2 días", "effort": "M",
     "prerequisites": ["GitHub repo"],
     "steps": ["workflow.yml pytest", "Docker build+push", "SSH deploy", "Slack notify"]},


    # ═══════════ 🔐 SECURITY (4) ═══════════
    {"id": "2fa_dashboard", "cat": "Security", "cat_icon": "🔐",
     "name": "2FA en dashboard (TOTP)",
     "desc": "Dashboard con admin/admin es riesgo. TOTP con Google Authenticator + backup codes.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["pyotp"],
     "steps": ["QR code enrollment", "Verify TOTP en login", "Backup codes 10 únicos", "Rate limit login 5/min"]},

    {"id": "secret_encryption", "cat": "Security", "cat_icon": "🔐",
     "name": "Encryption at rest (API keys)",
     "desc": "API keys guardadas en plain JSON. Encriptar con Fernet + key en env var.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["cryptography lib"],
     "steps": ["Fernet.generate_key() → env var", "Encrypt json_atomic.save", "Decrypt json_atomic.load", "Migration script"]},

    {"id": "audit_log_enrich", "cat": "Security", "cat_icon": "🔐",
     "name": "Audit log enriquecido (immutable)",
     "desc": "Log append-only con hash chain. Detecta tampering. IP, user, action, timestamp, hash.",
     "difficulty": "media", "impact": "Bajo", "time": "2 días", "effort": "M",
     "prerequisites": ["audit_log"],
     "steps": ["hash = sha256(prev_hash + entry)", "Verify chain on startup", "Alert if broken", "Export monthly"]},

    {"id": "ip_allowlist", "cat": "Security", "cat_icon": "🔐",
     "name": "IP allowlist + geofencing dashboard",
     "desc": "Dashboard solo accesible desde IPs autorizadas. Cloudflare Access o fail2ban.",
     "difficulty": "fácil", "impact": "Medio", "time": "1 día", "effort": "S",
     "prerequisites": ["Cloudflare / reverse proxy"],
     "steps": ["Middleware check X-Forwarded-For", "Allowlist en settings.json", "Geofence ES+EU", "fail2ban log"]},


    # ═══════════════════════════════════════════════════════════════════
    #  🆕 INVESTIGACIÓN 2026 — 27 mejoras nuevas (Nov 2026)
    #  Fuentes: investigación web profunda de Akamai v3, PerimeterX PX4,
    #  DataDome VM, Kasada, Cloudflare 2026, CAPTCHA SOTA, YOLOv10,
    #  Claude computer-use, TrOCR, Whisper, Turnstile v2
    # ═══════════════════════════════════════════════════════════════════

    # ═══════════ 🛡 Anti-Bot 2026 (15) ═══════════
    {"id": "akamai_sbsd_gen", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Akamai SBSD (Sentinel Bot Signal Data) payload generator",
     "desc": "Generar sbsd_o/bm_so cookies + SBSD sensor payload requerido por Akamai Bot Manager v3 en endpoints de alto riesgo (checkout, login).",
     "difficulty": "difícil", "impact": "Muy alto", "time": "3 semanas", "effort": "L",
     "prerequisites": ["Akamai v3 reverse engineering", "UUID v4 gen", "script hash extractor"],
     "steps": ["Fetch main SBSD script y extraer hash dinámico", "Build payload con userAgent, uuid, pageUrl, oCookie, IP, acceptLanguage, index", "Encriptar payload con PRNG sembrado por cookieHash+fileHash", "POST a /_sbsd endpoint y guardar sbsd_o cookie", "Refresh token cada 2 min por sesión"],
     "source": "https://docs.hypersolutions.co/akamai-web/sbsd-introduction"},

    {"id": "akamai_v3_cookiehash_chain", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Akamai v3 bm_sz cookie-hash chaining",
     "desc": "Reemplazar el hash default 8888888 con el hash dinámico extraído de bm_sz index 2 en cada request sensor_data siguiente para sobrevivir validación v3.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "M",
     "prerequisites": ["sensor_data v3 generator", "bm_sz parser"],
     "steps": ["Parse bm_sz tras primera response splitting por ~ para index 2 hash", "Feed hash al PRNG char-substitution del siguiente payload", "Update chain en cada /_bm/_data request", "Detectar rotación bm_sz y resembrar"],
     "source": "https://medium.com/@glizzykingdreko/akamai-v3-sensor-data-deep-dive-into-encryption-decryption-and-bypass-tools-da0adad2a784"},

    {"id": "dd_vm_bytecode_trace", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "DataDome VM bytecode interpreter",
     "desc": "Runner mínimo de los opcodes VM de DataDome (update Enero 2026) para producir ddCaptchaEncodedPayload headless sin browser completo.",
     "difficulty": "difícil", "impact": "Muy alto", "time": "4 semanas", "effort": "XL",
     "prerequisites": ["JS VM knowledge", "bytecode disassembler", "DataDome tag download"],
     "steps": ["Pull latest tag.js e isolar VM dispatcher", "Mapear opcode numbers a operaciones primitivas", "Reimplementar intérprete en Python con side effects", "Feed browser env (navigator, screen, canvas) como memoria VM", "Emitir ddCaptchaEncodedPayload y verificar contra endpoint live"],
     "source": "https://datadome.co/changelog/vm-based-obfuscation/"},

    {"id": "dd_intent_pattern_shape", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "DataDome intent-based navigation shaping",
     "desc": "Counter el ML layer de intent-detection (2025) shaping request graph (homepage → category browse → pausas) para que parezca navegación exploratoria.",
     "difficulty": "media", "impact": "Alto", "time": "5 días", "effort": "M",
     "prerequisites": ["sitemap target", "referer chain builder"],
     "steps": ["Grabar click-streams reales en target", "Build Markov model sobre page transitions", "Insertar 2-6 dummy page visits antes del target", "Add jittered dwell times 3-18s", "Preservar Referer y sec-fetch-site entre hops"],
     "source": "https://www.startertutorials.com/blog/bypassing-datadome-in-2026-the-ultimate-engine-level-guide.html"},

    {"id": "px_pxhd_signal_fwd", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "HUMAN _pxhd signal forwarding",
     "desc": "Extraer header data cifrada de _pxhd en primera response y propagar campos derivados en cada POST siguiente para que validación server-side coincida.",
     "difficulty": "media", "impact": "Alto", "time": "4 días", "effort": "M",
     "prerequisites": ["_pxhd parser", "session store"],
     "steps": ["Capturar _pxhd en primer hit de px.js", "Decode base64 y extraer session/device payload", "Inyectar valores derivados en body del siguiente challenge", "Rotar cuando _px3 refresh (~60s)"],
     "source": "https://scrapfly.io/bypass/perimeterx"},

    {"id": "px_press_hold_timing", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "PerimeterX Press-and-Hold biometric emulator",
     "desc": "Replay press-and-hold challenges con curvas de presión realistas, hold 1600-2400ms y micro-release jitter para derrotar el clasificador temporal de HUMAN.",
     "difficulty": "difícil", "impact": "Alto", "time": "2 semanas", "effort": "L",
     "prerequisites": ["pointer-event synthesizer", "pressure sample dataset"],
     "steps": ["Recolectar 500+ traces reales de press-hold", "Fit distribución beta a duración y presión", "Samplear traces sintéticos e inyectar via CDP Input.dispatchTouchEvent", "Verificar captcha success rate >90%"],
     "source": "https://roundproxies.com/blog/bypass-perimeterx/"},

    {"id": "kasada_vm_opcode_solver", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Kasada ips.js VM opcode solver",
     "desc": "Devirtualizar el p.js/ips.js polimórfico de Kasada para computar headers x-kpsdk-ct y x-kpsdk-cd offline sin browser.",
     "difficulty": "difícil", "impact": "Muy alto", "time": "4 semanas", "effort": "XL",
     "prerequisites": ["JS deobfuscator", "VM disassembler", "PoW solver"],
     "steps": ["Fetch /149e9513-.../2d206a39-.../ips.js", "Decrypt array-based string encryption", "Mapear VM opcodes a JS primitives", "Resolver PoW embebido y emitir ct/cd tokens", "Auto-refresh cuando Kasada rota script"],
     "source": "https://github.com/Humphryyy/Kasada-Deobfuscated"},

    {"id": "cf_jafour_ua_align", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Cloudflare JA4/User-Agent cross-layer alignment",
     "desc": "Matchear JA4 TLS fingerprint exactamente al User-Agent spoofeado. Cloudflare 2026 cross-checkea handshake vs UA y flaggea mismatches al instante.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "M",
     "prerequisites": ["curl_cffi o rustls", "JA4 database"],
     "steps": ["Build per-UA JA4 profile table", "On UA rotation swap TLS library profile", "Verify via ja4-check.com antes del target", "Monitor for JA4 drift across sessions"],
     "source": "https://www.tapscape.com/cloudflare-turnstile-bypass-2026-the-core-level-stealth-guide/"},

    {"id": "cf_clearance_binding", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "cf_clearance device-binding preservation",
     "desc": "cf_clearance está vinculado a visitor+device: preservar TLS, HTTP/2 e IP exactos que lo generaron durante toda la sesión en lugar de rotar mid-flow.",
     "difficulty": "fácil", "impact": "Medio", "time": "3 días", "effort": "S",
     "prerequisites": ["sticky proxy", "session store"],
     "steps": ["Lock sticky residential IP 30-60 min", "Pin TLS/HTTP2 fingerprint por session id", "Nunca replay cf_clearance en otra IP/UA", "Evict cookie en cualquier fingerprint drift"],
     "source": "https://developers.cloudflare.com/cloudflare-challenges/concepts/clearance/"},

    {"id": "imperva_reese84_payload", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Incapsula reese84 telemetry payload builder",
     "desc": "Producir bundle JSON reese84 (device props + behavioural signals) que Incapsula requiere antes de issuing un visid_incap válido.",
     "difficulty": "difícil", "impact": "Alto", "time": "2 semanas", "effort": "L",
     "prerequisites": ["reese84 script parser", "fingerprint bank"],
     "steps": ["Download reese84 challenge script y de-obfuscate", "Recolectar signals (screen, plugins, timezone, fonts, canvas)", "Build JSON según schema esperado", "POST a /_Incapsula_Resource?SWKMTFSR", "Guardar trust score token retornado"],
     "source": "https://github.com/BottingRocks/Incapsula"},

    {"id": "shape_vm_opcode_map", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "F5 Shape VM opcode mapping",
     "desc": "Construir disassembler para el VM JS de opcodes randomizados de Shape para producir signal payloads sin headless browser, tolerante a rotación frecuente.",
     "difficulty": "difícil", "impact": "Muy alto", "time": "6 semanas", "effort": "XL",
     "prerequisites": ["launcher+VM file fetcher", "opcode auto-remapper"],
     "steps": ["Fetch launcher y VM file secuencialmente", "Identificar dispatcher loop y opcode table", "Brute-force opcode semantics contra reference run", "Rebuild tras cada Shape rotation usando delta diff", "Emit signal blob y attach a /TMTickets request"],
     "source": "https://svebaa.github.io/personal/blog/shape-security/"},

    {"id": "http2_akamai_fp", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Akamai HTTP/2 fingerprint alignment",
     "desc": "Alinear SETTINGS, WINDOW_UPDATE, PRIORITY frames y orden pseudo-header (:method,:authority,:scheme,:path) exactamente como Chrome — el mismatch cross-layer es la señal 2026 más fuerte.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "M",
     "prerequisites": ["nghttp2 o h2 library", "browser h2 reference"],
     "steps": ["Capturar Chrome SETTINGS: HEADER_TABLE_SIZE=65536, MAX_CONCURRENT_STREAMS=1000", "Set WINDOW_UPDATE increment a 15663105", "Send PRIORITY frames para stream 3,5,7,9,11", "Ordenar pseudo-headers m,a,s,p", "Lock a UA y nunca enviar default Go/Python h2 profile"],
     "source": "https://scrapfly.io/blog/posts/http2-http3-fingerprinting-guide"},

    {"id": "http3_quic_ja4q", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "HTTP/3 QUIC JA4q fingerprint support",
     "desc": "Añadir emulación QUIC transport-parameter (JA4q prefix) ya que CDNs mayores ahora incluyen QUIC en scoring JA4+ y clientes TCP-only están siendo demoted.",
     "difficulty": "difícil", "impact": "Medio", "time": "3 semanas", "effort": "L",
     "prerequisites": ["QUIC client (quiche/msquic)", "JA4q reference"],
     "steps": ["Integrar quiche en HTTP client pool", "Populate initial_max_data, ack_delay_exponent según Chrome", "Match ALPN y QUIC version token order", "Verificar vs ja4+quic check tool", "Fallback a HTTP/2 solo si negotiation fail"],
     "source": "https://www.proxies.sx/use-cases/privacy/http3-quic"},

    {"id": "cdp_patches_os_level", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "OS-level CDP leak patching (Patchright + CDP-Patches)",
     "desc": "Reemplazar Playwright/Selenium estándar con Patchright o CDP-Patches para tapar marcadores cdc_, Runtime.enable leaks y Input-dispatch hints a nivel OS.",
     "difficulty": "fácil", "impact": "Alto", "time": "2 días", "effort": "S",
     "prerequisites": ["Playwright", "Python 3.10+"],
     "steps": ["pip install patchright y swap import", "Habilitar CDP-Patches pointer injection para clicks", "Disable Runtime.enable globally", "Test contra CreepJS y fingerprint.com score", "Validar contra Akamai/Shape/DataDome antes de rollout"],
     "source": "https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches"},

    {"id": "bezier_mouse_biometric", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "Bézier + Fitts's-law mouse trajectory synth",
     "desc": "Generar trayectorias cubic-Bézier moduladas por perfiles de velocidad Fitts's-law + micro-tremor noise para que ML conductual (HUMAN, Netacea, Radware) no distinga de usuarios reales.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "M",
     "prerequisites": ["pointer synthesizer", "real trace dataset"],
     "steps": ["Samplear start/end points + 2 control points", "Aplicar Fitts's-law duration t = a + b*log2(D/W+1)", "Overlay Gaussian tremor ±1.2 px a 60 Hz", "Dispatch via CDP Input.dispatchMouseEvent con subpixel coords", "Log y replay traces exitosos por domain"],
     "source": "https://www.researchgate.net/publication/393981520"},


    # ═══════════ 🧠 CAPTCHA 2026 (12) ═══════════
    {"id": "cap_recaptcha_v3_behavior_entropy", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "reCAPTCHA v3 score warmup via behavioral entropy",
     "desc": "Algoritmo score 2026 de Google pesa mouse trajectory entropy, scroll velocity cadence y page-focus dwell. Target 0.7+ pre-warming sesiones con GhostCursor Bezier paths + Fitts-Law timing + overshoot+readjust antes de redeem del token.",
     "difficulty": "media", "impact": "Alto", "time": "8-12h", "effort": "M",
     "prerequisites": ["Playwright/undetected-chromedriver", "ghost-cursor npm", "fingerprint seed store"],
     "steps": ["Install ghost-cursor (npm i ghost-cursor)", "Inject cursor con installMouseHelper+createCursor", "Simular 3-8s de pre-interaction (mouse weave, scroll, tab focus blur)", "Persistir device seed determinista por cuenta", "Request grecaptcha token y loggear score; auto-rotate IP si <0.5"],
     "source": "https://github.com/Xetera/ghost-cursor"},

    {"id": "cap_turnstile_cdp_mode_seleniumbase", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Cloudflare Turnstile invisible-mode defeat via SeleniumBase CDP Mode",
     "desc": "CDP Mode conduce Chrome over DevTools Protocol SIN chromedriver, evadiendo la CDP-latency detection que Turnstile añadió en 2026. Usa uc_gui_click_captcha() + connect/disconnect strategy.",
     "difficulty": "media", "impact": "Alto", "time": "4-6h", "effort": "S",
     "prerequisites": ["Python 3.10+", "SeleniumBase >=4.25", "PyAutoGUI"],
     "steps": ["pip install seleniumbase", "Launch con Driver(uc=True, cdp_mode=True)", "driver.uc_open_with_reconnect(url, 4)", "driver.uc_gui_click_captcha() cuando widget aparece", "Fallback a residential proxy en 403"],
     "source": "https://seleniumbase.io/examples/cdp_mode/ReadMe/"},

    {"id": "cap_tls_ja4_curl_cffi", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "JA4+ TLS fingerprint impersonation con curl_cffi",
     "desc": "Cloudflare/Akamai/Kasada cross-checkean JA4 contra User-Agent; mismatch = CAPTCHA escalation instantáneo. curl_cffi emite handshakes TLS byte-exact Chrome-124/Safari-17, cipher order, ALPN y HTTP/2 SETTINGS frames.",
     "difficulty": "fácil", "impact": "Alto", "time": "2-4h", "effort": "S",
     "prerequisites": ["Python 3.9+"],
     "steps": ["pip install curl_cffi", "Reemplazar requests.get con curl_cffi.requests.get(url, impersonate='chrome124')", "Rotar impersonation profile por session", "Verificar JA4 en tls.peet.ws"],
     "source": "https://github.com/lexiforest/curl_cffi"},

    {"id": "cap_trocr_text_local", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Local TrOCR text-CAPTCHA solver (Hugging Face)",
     "desc": "Microsoft TrOCR encoder-decoder supera Tesseract en CAPTCHAs de texto distorsionado (Queue-it legacy, BotDetect). Pre-trained microsoft/trocr-base-printed fine-tuned hit >95% en alfanumérico. GPU sub-segundo, CPU ~3s — zero API cost.",
     "difficulty": "media", "impact": "Medio", "time": "6-10h", "effort": "M",
     "prerequisites": ["transformers", "torch", "PIL"],
     "steps": ["pip install transformers torch pillow", "Load TrOCRProcessor.from_pretrained('DunnBC22/trocr-base-printed_captcha_ocr')", "Fine-tune 300 samples labeled de Queue-it 3 epochs", "Wrap inference en endpoint Flask local", "Usar como first-tier antes del API pagado"],
     "source": "https://huggingface.co/DunnBC22/trocr-base-printed_captcha_ocr"},

    {"id": "cap_whisper_audio_solver", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Whisper-large-v3 audio-challenge solver (offline)",
     "desc": "Audio fallback de reCAPTCHA/hCaptcha decoded con >93% accuracy por Whisper-large-v3 local. Clip 10s → transcribed digits/words en ~1s GPU. Click speaker icon, capture MP3, feed a whisper.cpp, submit answer.",
     "difficulty": "fácil", "impact": "Alto", "time": "3-5h", "effort": "S",
     "prerequisites": ["whisper.cpp o faster-whisper", "ffmpeg"],
     "steps": ["pip install faster-whisper", "Load WhisperModel('large-v3', device='cuda')", "Interceptar payload MP3 via browser CDP", "Transcribir y strip fillers (please, the)", "Fill response textbox y submit"],
     "source": "https://github.com/theriley106/WhisperCaptcha"},

    {"id": "cap_pow_wasm_worker_pool", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Proof-of-work challenge accelerator (WASM + Web Workers)",
     "desc": "Cap, ALTCHA y hCaptcha PoW emiten SHA-256 partial-collision puzzles. Resolver en pool WASM multi-threaded (navigator.hardwareConcurrency workers) para que difficulty 6-digit clear en <300ms. Para bulk farming, drop a Rust/Go CLI con 10M H/s per core.",
     "difficulty": "media", "impact": "Medio", "time": "6-8h", "effort": "M",
     "prerequisites": ["Node 20", "Rust toolchain (opcional)"],
     "steps": ["Detectar challenge type (cap.js / altcha)", "Spawn N Web Workers con shared SHA-256 wasm", "Distribuir nonce ranges; first hit wins", "Para headless bulk: port a sha2 crate, 8-core = ~80 MH/s", "Return signed token"],
     "source": "https://capjs.js.org/"},

    {"id": "cap_claude_computer_use", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Claude Opus 4.x computer-use fallback para CAPTCHAs desconocidos",
     "desc": "Cuando un nuevo custom-image CAPTCHA aparece, enviar screenshot a Claude via tool computer_20250124; devuelve (x,y) click coords o rotation angle. Claude Opus 4.x vision supera GPT-4o en rotación 3D + semantic-similarity que Arkose ahora ships. Cost ~$0.02/solve.",
     "difficulty": "media", "impact": "Alto", "time": "4-6h", "effort": "M",
     "prerequisites": ["anthropic SDK >=0.40", "screenshot capture"],
     "steps": ["pip install anthropic", "Usar model claude-opus-4-5 con computer tool", "Capturar 1024x768 PNG del challenge area", "Prompt: return click coords for image matching X", "Ejecutar click via Playwright"],
     "source": "https://docs.claude.com/en/docs/build-with-claude/computer-use"},

    {"id": "cap_geetest_v4_trajectory_ml", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "GeeTest v4 slider trajectory replay + gap-detect CNN",
     "desc": "GeeTest v4 scorea cursor jerk, acceleration distribution y release-speed. Train CNN pequeña en la notch image (Canny + U-Net) para localizar gap pixel, luego replay human-recorded drag trajectory dataset escalado a la distancia. Drops failure rate de 40% a <5% vs pure PID drag.",
     "difficulty": "difícil", "impact": "Alto", "time": "12-16h", "effort": "L",
     "prerequisites": ["opencv-python", "tensorflow-lite", "trajectory dataset"],
     "steps": ["Screenshot slider bg + piece", "Run Canny + template-match para gap X offset", "Samplear trajectory de replay bank escalado a offset", "Feed micro-step events via CDP Input.dispatchMouseEvent", "Retry hasta 3 veces"],
     "source": "https://2captcha.com/blog/geetest-v4-support"},

    {"id": "cap_residential_mobile_asn_targeting", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Carrier-ASN-matched mobile proxy pool (city-locked, sticky)",
     "desc": "Ticketmaster + Queue-it scorea reputación ASN heavy. Mobile 4G/5G IPs en Verizon/T-Mobile casi nunca CAPTCHA. Pair venue city → carrier ASN, hold sticky 30 min (queue+checkout), hot-swap solo on clean logout. Kills 60-80% de image challenges antes de que rendericen.",
     "difficulty": "fácil", "impact": "Muy alto", "time": "2-3h", "effort": "S",
     "prerequisites": ["IPRoyal/SOAX/Bright Data mobile plan"],
     "steps": ["Suscribir a mobile proxy con ASN targeting", "Map venue → carrier en city_asn.json", "Build URL http://user-session-{id}-asn-AS6167:pass@proxy:port", "Bind a 1 session por browser context", "Rotar solo on queue exit / 429"],
     "source": "https://spyderproxy.com/blog/best-proxies-ticketmaster-event-tickets"},

    {"id": "cap_datadome_behavioral_biometrics", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Datadome/Shape behavioral-biometric emulator (35-signal profile)",
     "desc": "El modelo Descartes de Datadome lee 35+ signals (mouse jerk, scroll velocity, click pressure, keystroke cadence). Pre-built 'human session recorder' replaya eventos reales capturados (anonimizados de tus propios analytics) con ±8% jitter. Beat 40-seg classifier window.",
     "difficulty": "difícil", "impact": "Alto", "time": "14-20h", "effort": "L",
     "prerequisites": ["Human event corpus (self-collected)", "Playwright"],
     "steps": ["Capturar sessions reales via rrweb en staging propio", "Extraer mouse+scroll+keystroke streams", "Build replay engine que jitter timing 5-10%", "Inyectar en Playwright via CDP Input domain", "A/B medir Datadome pass rate"],
     "source": "https://datadome.co/anti-detect-tools/behavioral-bot-classification/"},

    {"id": "cap_yolov10_image_grid", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "YOLOv10 image-grid classifier para reCAPTCHA v2 + hCaptcha",
     "desc": "Upgrade detección local a YOLOv10 (NMS-free, 30% más rápido que v8 con igual mAP). Fine-tune en dataset labeled existente para grids 3x3/4x4 de 'bicycles/traffic lights/crosswalks'. Corre <50ms en RTX 3060, first-try antes del LLM fallback — ahorra ~70% costs API.",
     "difficulty": "media", "impact": "Alto", "time": "10-14h", "effort": "M",
     "prerequisites": ["ultralytics >=8.3", "CUDA GPU", "labels existentes"],
     "steps": ["pip install ultralytics", "Exportar labels existentes a YOLO format", "Train yolo detect train model=yolov10s.pt data=captcha.yaml epochs=80", "Export a ONNX + TensorRT", "Wrap en servicio FastAPI inference"],
     "source": "https://github.com/THU-MIG/yolov10"},

    {"id": "cap_funcaptcha_tguess_vm", "cat": "CAPTCHA", "cat_icon": "🧠",
     "name": "Arkose FunCaptcha tguess VM + rotation-angle regressor",
     "desc": "Arkose 2026 variants (3D rotation, dice-count, same-shape) necesitan: (a) tguess PoW proof corriendo dentro del JS VM obfuscado de Arkose, (b) rotation regressor. Usar node-vm para ejecutar tguess script dinámico + ResNet-18 head para rotation delta (MAE <8°).",
     "difficulty": "difícil", "impact": "Alto", "time": "16-24h", "effort": "L",
     "prerequisites": ["Node + vm2", "PyTorch", "GPU"],
     "steps": ["Interceptar funcaptcha/api.js session init", "Run tguess script en node vm2 con harness", "Download 3D-rotation pairs, train ResNet-18 regressor", "POST answer con token VAFTM-compatible", "Rotar Arkose key por session"],
     "source": "https://github.com/unfuncaptcha/tguess"},

    # ═══════════════════════════════════════════════════════════════════
    #  🔥 RESEARCH CRÍTICO 2026 — 10 mejoras urgentes (Abril 2026)
    #  Detectadas vía análisis web profundo de updates Q1-Q2 2026
    # ═══════════════════════════════════════════════════════════════════
    {"id": "ja4_per_session_pinning", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "JA4+ TLS fingerprint pinning per-session (CRÍTICO)",
     "desc": "Akamai/Imperva integraron JA4+ en 2026 específicamente para detectar JA3 spoofing post-Chrome 108 ClientHello randomization. UNA IP presentando MÚLTIPLES JA4 distintos = bot signal fortísimo. Necesario per-account JA4 pinning + match de subcomponentes JA4_a/JA4_b/JA4_c.",
     "difficulty": "difícil", "impact": "Máximo", "time": "1-2 semanas", "effort": "L",
     "prerequisites": ["curl_cffi o tls-client", "JA4 reference DB"],
     "steps": ["Pin JA4 por (account_id, IP) tuple", "Match JA4_a/b/c subcomponentes", "Verify en ja4-check.com", "Rotar solo en logout limpio", "Alert si JA4 drift detectado"],
     "source": "https://deveshshetty.com/blog/ja4-client-fingerprinting/"},

    {"id": "http2_frame_order_match", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "HTTP/2 frame-order fingerprint match (CRÍTICO)",
     "desc": "Akamai 2026 flagea clientes ANTES de que termine el primer request leyendo HTTP/2 SETTINGS, WINDOW_UPDATE y orden de frames. requests/httpx producen patrones non-browser. Integrar curl_cffi o httpcloak con priorities H2 idénticas a Chrome.",
     "difficulty": "media", "impact": "Máximo", "time": "1 semana", "effort": "M",
     "prerequisites": ["curl_cffi", "Chrome H2 reference capture"],
     "steps": ["Capturar Chrome H2 SETTINGS (HEADER_TABLE_SIZE, etc)", "WINDOW_UPDATE increment 15663105", "PRIORITY frames stream 3,5,7,9,11", "Pseudo-headers order m,a,s,p", "Lock por User-Agent"],
     "source": "https://scrapfly.io/blog/posts/http2-http3-fingerprinting-guide"},

    {"id": "datadome_integrity_probe_proxy", "cat": "Anti-Bot", "cat_icon": "🛡",
     "name": "DataDome Integrity Probe defense (Proxy hooks)",
     "desc": "DataDome 2026 deploya integrity probes que detectan property tampering via Object.getOwnPropertyDescriptor en native methods, defeating la mayoría de stealth patches. Solución: usar Proxy-based property hooks en lugar de overrides directos.",
     "difficulty": "difícil", "impact": "Alto", "time": "5-8 días", "effort": "M",
     "prerequisites": ["Conocimiento JS Proxy/Reflect", "Stealth patch base"],
     "steps": ["Identificar properties que DataDome chequea", "Wrap con Proxy traps (get/has)", "Implement getOwnPropertyDescriptor handler", "Test contra DataDome live page", "A/B vs stealth patches directos"],
     "source": "https://www.startertutorials.com/blog/bypassing-datadome-in-2026-the-ultimate-engine-level-guide.html"},

    {"id": "safetix_screenshot_handler", "cat": "Strategy", "cat_icon": "🎯",
     "name": "SafeTix mobile-only screenshot-block handler (CRÍTICO Mar 2026)",
     "desc": "TM rolled out March 2026 mobile-only ticket flow que BLOQUEA screenshots y screen-recordings en iOS/Android. Cualquier flow asumiendo barcode export ROMPE. Necesario rotating-barcode session capture via instrumented device (Frida hook on token refresh ~15s).",
     "difficulty": "difícil", "impact": "Máximo", "time": "2-3 semanas", "effort": "XL",
     "prerequisites": ["Android emulator + Frida", "iOS sideloading o jailbreak", "TM mobile app reverse engineering"],
     "steps": ["Hook Frida en TM mobile app", "Interceptar token refresh JWT", "Capturar barcode rotating ~15s antes que cambie", "Bridge a desktop via API local", "Display en navegador del comprador o entry gate"],
     "source": "https://www.ticketnews.com/2026/03/ticketmaster-touts-enhanced-protections-in-new-mobile-only-ticket-rollout/"},

    {"id": "uach_grease_consistency", "cat": "Browsers", "cat_icon": "🧪",
     "name": "UA-CH GREASE + UA cross-consistency validator",
     "desc": "Chrome 2026 reduced UA returns minor versions como 0.0.0 y ships GREASE entries (ej. 'Not;A Brand'). Bots que envían minor versions reales en UA pero Sec-CH-UA-* válidos AHORA destacan como bots. Cross-check UA<->UA-CH + GREASE rotation.",
     "difficulty": "fácil", "impact": "Alto", "time": "2-4h", "effort": "S",
     "prerequisites": ["headers builder existente"],
     "steps": ["UA con versions 'X.0.0.0' (no minor)", "Sec-CH-UA con GREASE entry (ej. 'Not;A Brand')", "Validar UA mayor matches Sec-CH-UA-Full-Version-List", "Rotar GREASE strings", "Test contra fingerprint.com"],
     "source": "https://privacysandbox.google.com/protections/user-agent"},

    {"id": "asn_ip_reputation_drift", "cat": "Proxies", "cat_icon": "🔀",
     "name": "ASN+IP reputation cross-drift detector",
     "desc": "GreyNoise/RSAC 2026: 78% de IPs residenciales se evaporan antes de que reputation systems flaggen, pero combos good-ASN+bad-IP AHORA tankean trust scores. Pre-flight Spamhaus/IPQS check + ASN consistency over session lifetime.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "M",
     "prerequisites": ["IPQS API key (existente)", "Spamhaus DNSBL access"],
     "steps": ["Pre-flight check IP en IPQS + Spamhaus", "Compute ASN reputation score", "Reject combos good-ASN+bad-IP", "Track ASN consistency entre requests del session", "Alert on drift > 10%"],
     "source": "https://ipinfo.io/blog/vpns-residential-proxies-abuse-findings-rsac-2026"},

    {"id": "tls_ct_log_monitor", "cat": "Observability", "cat_icon": "📊",
     "name": "Certificate Transparency log monitor (TM/Queue-it)",
     "desc": "Let's Encrypt RFC6962 logs cierran Feb 2026; nuevo static CT API live. Monitorear rotaciones cert TM/Queue-it da early warning de WAF/CDN migration ANTES de que rompa el bot. Cron diario.",
     "difficulty": "fácil", "impact": "Medio", "time": "2-3 días", "effort": "S",
     "prerequisites": ["crt.sh API o sslmate ct-search"],
     "steps": ["Daily query crt.sh para ticketmaster.com SAN", "Compute hash de cert chain", "Diff vs último snapshot", "Alert via Discord si cambia issuer/SAN", "Auto-update test endpoints si cambia"],
     "source": "https://letsencrypt.org/docs/ct-logs/"},

    {"id": "psd3_tra_aware_retry", "cat": "Payment", "cat_icon": "💳",
     "name": "PSD3 TRA-exemption-aware payment retry policy",
     "desc": "PSD3 (2026) tightens TRA (Transaction Risk Analysis) fraud-rate thresholds. Adyen authentication engine analiza 100+ data points y mass-retry on SCA failure AHORA tankea auth rates fast. PSD3-aware backoff: detect TRA-exemption denial vs hard-fail y ruta acordemente.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "M",
     "prerequisites": ["adyen_tokenizer.py funcional"],
     "steps": ["Detect TRA-exemption denial (refusalReason)", "Backoff 60-120s antes de retry (no instant)", "Switch a fallback card si 2 intentos fallan", "Track TRA denial rate per card", "Alert si > 20% para una card"],
     "source": "https://www.adyen.com/knowledge-hub/psd3"},

    {"id": "eu_dfa_compliance_gate", "cat": "Compliance", "cat_icon": "⚖",
     "name": "EU Digital Fairness Act Q3 2026 compliance gate (CRÍTICO legal)",
     "desc": "DFA adoption planned Q3 2026 explícitamente direcciona scalper-bot ticket resale; 130+ EU orgs lobbying for hard enforcement. Operating from Spain sin per-event consent logging + face-value resale check crea takedown risk.",
     "difficulty": "media", "impact": "Máximo", "time": "1-2 semanas", "effort": "M",
     "prerequisites": ["audit_log.py", "BOTS Act waivers (ya existe)"],
     "steps": ["compliance_gate.py decorator on bot.run", "Per-event consent log (account_id, event_id, ts)", "Block resale > face value para EU events", "Hash chain audit log para non-tampering", "Quarterly export para auditoría"],
     "source": "https://www.freshfields.com/en/our-thinking/blogs/technology-quotient/digital-fitness-check-and-digital-fairness-act-part-8-dropshipping-bots-ticket-102lr1q"},

    {"id": "trust_tuple_warming", "cat": "Cookies", "cat_icon": "🍪",
     "name": "(account, ASN, geo, device-fp) tuple warming consistency",
     "desc": "TM 2026 builds multi-dim trust profiles. Accounts que switch IPs o cuyo IP geo no matchea registration trigger CAPTCHA/ban rápido. Current cookie_warmer warm cookies pero NO la (account, ASN, geo, device-fp) tuple stickiness que TM ahora espera.",
     "difficulty": "media", "impact": "Alto", "time": "1 semana", "effort": "M",
     "prerequisites": ["cookie_warmer.py", "account_manager.py", "proxy ISP/residential"],
     "steps": ["Asignar (ASN, geo) sticky a cada account creado", "Verificar geo-IP match a registration country", "Mantener device-fp consistente entre sesiones del mismo account", "Reject sessions si tuple drift detectado", "Stats por account_id de tuple stability"],
     "source": "https://multilogin.com/blog/ticketmaster-queue/"},
]


# ═══════════════════════════════════════════
#  TASKS (persistentes)
# ═══════════════════════════════════════════
def _load_tasks() -> dict:
    return json_atomic.load(config.ruta(_TASKS_KEY),
                             default={"tasks": []})


def _save_tasks(data: dict):
    json_atomic.save(config.ruta(_TASKS_KEY), data)


def list_tasks(status: Optional[str] = None,
               improvement_id: Optional[str] = None) -> List[dict]:
    with _lock:
        data = _load_tasks()
    items = data.get("tasks", [])
    if status:
        items = [t for t in items if t.get("status") == status]
    if improvement_id:
        items = [t for t in items if t.get("improvement_id") == improvement_id]
    return items


def get_task(task_id: str) -> Optional[dict]:
    for t in list_tasks():
        if t.get("id") == task_id:
            return t
    return None


def create_task(improvement_id: str, priority: str = "medium",
                assignee: str = "", notes: str = "") -> dict:
    # verifica que la improvement existe
    imp = next((c for c in CATALOG if c["id"] == improvement_id), None)
    if not imp:
        return {"ok": False, "error": f"improvement_id '{improvement_id}' no existe en catálogo"}
    # verifica que no hay ya una task open/in_progress para esa mejora
    existing = [t for t in list_tasks(improvement_id=improvement_id)
                if t.get("status") in ("open", "in_progress")]
    if existing:
        return {"ok": False, "error": "ya existe una tarea activa para esta mejora",
                "existing_task_id": existing[0]["id"]}

    task = {
        "id": uuid.uuid4().hex[:10],
        "improvement_id": improvement_id,
        "improvement_name": imp["name"],
        "improvement_cat": imp["cat"],
        "status": "open",
        "priority": priority if priority in ("low", "medium", "high", "critical") else "medium",
        "assignee": assignee,
        "notes": notes,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "completed_at": None,
    }
    with _lock:
        data = _load_tasks()
        data["tasks"].insert(0, task)
        _save_tasks(data)
    return {"ok": True, "task": task}


def update_task(task_id: str, patch: dict) -> dict:
    allowed = {"status", "priority", "assignee", "notes"}
    with _lock:
        data = _load_tasks()
        for t in data["tasks"]:
            if t.get("id") == task_id:
                for k, v in (patch or {}).items():
                    if k in allowed:
                        t[k] = v
                t["updated_at"] = datetime.now().isoformat()
                if t["status"] == "done" and not t.get("completed_at"):
                    t["completed_at"] = datetime.now().isoformat()
                _save_tasks(data)
                return {"ok": True, "task": t}
    return {"ok": False, "error": "task_id no encontrado"}


def complete_task(task_id: str) -> dict:
    return update_task(task_id, {"status": "done"})


def delete_task(task_id: str) -> dict:
    with _lock:
        data = _load_tasks()
        before = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != task_id]
        _save_tasks(data)
    return {"ok": True, "removed": before - len(data["tasks"])}


# ═══════════════════════════════════════════
#  CATALOG VIEW (con task asociada y filtrado)
# ═══════════════════════════════════════════
def get_catalog(hide_completed: bool = True,
                category: Optional[str] = None,
                search: Optional[str] = None) -> List[dict]:
    """Devuelve el catálogo con info de task asociada.
    Si hide_completed=True, oculta las mejoras cuya task está en status 'done'.
    """
    all_tasks = list_tasks()
    tasks_by_imp = {}
    for t in all_tasks:
        iid = t.get("improvement_id")
        if iid not in tasks_by_imp:
            tasks_by_imp[iid] = []
        tasks_by_imp[iid].append(t)

    out = []
    for imp in CATALOG:
        iid = imp["id"]
        imp_tasks = tasks_by_imp.get(iid, [])
        # status más reciente
        latest_done = any(t.get("status") == "done" for t in imp_tasks)
        active = next((t for t in imp_tasks
                       if t.get("status") in ("open", "in_progress")), None)

        # filtro hide_completed
        if hide_completed and latest_done:
            continue

        # filtro cat
        if category and imp.get("cat") != category:
            continue

        # filtro search
        if search:
            s = search.lower()
            if (s not in imp["name"].lower() and
                s not in imp["desc"].lower() and
                s not in imp["cat"].lower() and
                s not in imp["id"].lower()):
                continue

        entry = dict(imp)
        entry["task"] = active
        entry["has_completed_task"] = latest_done
        entry["total_tasks"] = len(imp_tasks)
        # retrocompat con frontend viejo
        entry.setdefault("dificultad", entry.get("difficulty"))
        entry.setdefault("impacto", entry.get("impact"))
        out.append(entry)
    return out


def get_categories() -> List[dict]:
    cats = {}
    for imp in CATALOG:
        c = imp["cat"]
        if c not in cats:
            cats[c] = {"cat": c, "icon": imp.get("cat_icon", "•"), "count": 0}
        cats[c]["count"] += 1
    return sorted(cats.values(), key=lambda x: x["cat"])


def get_stats() -> dict:
    all_tasks = list_tasks()
    by_status = {}
    for t in all_tasks:
        s = t.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1

    done_ids = set(t["improvement_id"] for t in all_tasks if t.get("status") == "done")
    return {
        "catalog_total": len(CATALOG),
        "catalog_visible": len(CATALOG) - len(done_ids),
        "tasks_total": len(all_tasks),
        "tasks_by_status": by_status,
        "tasks_open": by_status.get("open", 0),
        "tasks_in_progress": by_status.get("in_progress", 0),
        "tasks_done": by_status.get("done", 0),
        "categories_count": len(get_categories()),
    }
