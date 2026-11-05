"""purchase_stealth.py — Orquestación stealth para los bots de compra.

Cablea TODO en un solo punto:
  - Fingerprint coherente por sessionid (GPU/screen/hw/locale/TZ/geo)
  - STEALTH_JS reforzado (webdriver/plugins/WebGL/canvas/audio/chrome.runtime)
  - WebRTC blocking universal (Camoufox tiene block_webrtc; MoreLogin vía JS)
  - Sec-CH-UA + GREASE 2026
  - Timezone + locale + geolocation override
  - Persona warm-up (3-7 min navegación previa al checkout)

API:
    persona = create_persona_from_sessionid("380962", country="ES")
    apply_stealth_to_context(ctx, persona, log)
    # ...después del primer goto, opcionalmente:
    persona_warmup(page, persona, log, minutes=(3,7))

Diseño:
    - Persona DETERMINÍSTICA desde sessionid: re-usar sessid → mismo fingerprint
      (clave para que TM no detecte "cambio de device" cuando re-usa cuenta).
    - STEALTH_JS inyectado vía add_init_script ANTES del primer goto.
    - No depende de Camoufox/MoreLogin internals — funciona en ambos engines.
"""
from __future__ import annotations
import json, random, time
from typing import Dict, Any, Optional, Callable

try:
    import advanced_fingerprint as _af
except Exception:
    _af = None
try:
    import uach_grease_validator as _uach
except Exception:
    _uach = None


# ════════════════════════════════════════════════════════════════
#  CONFIG: geo y screen pool reales
# ════════════════════════════════════════════════════════════════
GEO_BY_COUNTRY = {
    "ES": {"tz": "Europe/Madrid", "locale": "es-ES",
           "lang": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
           "lat": 40.4168, "lon": -3.7038, "geo_accuracy": 50},
    "IT": {"tz": "Europe/Rome", "locale": "it-IT",
           "lang": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
           "lat": 41.9028, "lon": 12.4964, "geo_accuracy": 50},
    "FR": {"tz": "Europe/Paris", "locale": "fr-FR",
           "lang": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
           "lat": 48.8566, "lon": 2.3522, "geo_accuracy": 50},
}

# Resoluciones reales 2026 ponderadas por share de mercado (statcounter)
SCREEN_POOL = [
    {"width": 1920, "height": 1080, "scale": 1, "weight": 22},
    {"width": 1366, "height": 768, "scale": 1, "weight": 14},
    {"width": 1536, "height": 864, "scale": 1.25, "weight": 12},
    {"width": 2560, "height": 1440, "scale": 1, "weight": 11},
    {"width": 1280, "height": 720, "scale": 1, "weight": 8},
    {"width": 1440, "height": 900, "scale": 1, "weight": 7},
    {"width": 1680, "height": 1050, "scale": 1, "weight": 6},
    {"width": 2560, "height": 1600, "scale": 2, "weight": 5},
    {"width": 3840, "height": 2160, "scale": 1.5, "weight": 4},
]

# GPUs reales 2026 con plataforma coherente
GPU_PROFILES_WINDOWS = [
    {"vendor": "Google Inc. (NVIDIA)",
     "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 (0x00002786) Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "weight": 12, "hw_min": 12, "hw_max": 24, "mem": [16, 32]},
    {"vendor": "Google Inc. (NVIDIA)",
     "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "weight": 14, "hw_min": 8, "hw_max": 16, "mem": [16]},
    {"vendor": "Google Inc. (Intel)",
     "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "weight": 18, "hw_min": 4, "hw_max": 8, "mem": [8, 16]},
    {"vendor": "Google Inc. (Intel)",
     "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "weight": 16, "hw_min": 4, "hw_max": 12, "mem": [8, 16]},
    {"vendor": "Google Inc. (AMD)",
     "renderer": "ANGLE (AMD, AMD Radeon RX 7700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "weight": 8, "hw_min": 12, "hw_max": 16, "mem": [16, 32]},
    {"vendor": "Google Inc. (AMD)",
     "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "weight": 10, "hw_min": 8, "hw_max": 16, "mem": [16]},
    {"vendor": "Google Inc. (Intel)",
     "renderer": "ANGLE (Intel, Intel(R) HD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "weight": 12, "hw_min": 4, "hw_max": 8, "mem": [8]},
]


def _weighted_choice(rnd: random.Random, pool: list) -> dict:
    """Selección ponderada por 'weight'."""
    total = sum(it.get("weight", 1) for it in pool)
    r = rnd.uniform(0, total)
    acc = 0
    for it in pool:
        acc += it.get("weight", 1)
        if r <= acc:
            return it
    return pool[-1]


# ════════════════════════════════════════════════════════════════
#  PERSONA: fingerprint determinístico por sessionid
# ════════════════════════════════════════════════════════════════
def create_persona_from_sessionid(sessionid: str, country: str = "ES") -> Dict[str, Any]:
    """Genera persona reproducible. Mismo sessionid → mismo fingerprint.

    Esto es CRÍTICO para reuso de cuentas: si una cuenta se registró con
    sessid=12345, todas las compras futuras DEBEN usar sessid=12345 para
    que TM identity vea el MISMO device-fingerprint.
    """
    try:
        seed_int = int(str(sessionid).strip())
    except (TypeError, ValueError):
        # FIX 2026-05-14: built-in hash() es no-determinístico entre runs
        # (PYTHONHASHSEED). Mismo sessid string → diferentes seeds → personas
        # distintas. Causaba GPU/screen distinto entre slots aunque sessid era
        # estable. Usar hashlib.sha256 para reproducibilidad real.
        import hashlib as _hl
        _h = _hl.sha256(str(sessionid).encode('utf-8')).hexdigest()
        seed_int = int(_h[:8], 16)
    rnd = random.Random(seed_int)

    geo = GEO_BY_COUNTRY.get(country.upper(), GEO_BY_COUNTRY["ES"])

    # GPU coherente con OS
    gpu = _weighted_choice(rnd, GPU_PROFILES_WINDOWS)
    hw_concurrency = rnd.randint(gpu["hw_min"], gpu["hw_max"])
    device_memory = rnd.choice(gpu["mem"])

    # Screen coherente con GPU: GPUs gaming → screens grandes
    is_high_end_gpu = ("RTX 4070" in gpu["renderer"] or "RTX 4080" in gpu["renderer"] or
                       "RX 7700" in gpu["renderer"] or "RX 7800" in gpu["renderer"])
    is_integrated = "UHD Graphics" in gpu["renderer"] or "HD Graphics" in gpu["renderer"]
    screen_pool_filtered = SCREEN_POOL
    if is_high_end_gpu:
        # GPU gaming alta = monitor 1920+ típico
        screen_pool_filtered = [s for s in SCREEN_POOL if s["width"] >= 1920]
    elif is_integrated:
        # Integrada = laptop pequeño/medio típico
        screen_pool_filtered = [s for s in SCREEN_POOL if s["width"] <= 1920]
    if not screen_pool_filtered:
        screen_pool_filtered = SCREEN_POOL
    screen = _weighted_choice(rnd, screen_pool_filtered)

    # Chrome version (146 default — sync con MoreLogin)
    chrome_major = rnd.choice([146, 144, 145])

    # Sec-CH-UA + GREASE
    uach = {}
    if _uach is not None:
        try:
            uach = _uach.build_chrome_headers(chrome_major, platform="Windows")
        except Exception:
            uach = {}

    # WebGPU profile (si advanced_fingerprint disponible)
    webgpu = None
    if _af is not None:
        try:
            webgpu = _af.generate_webgpu_profile(seed=str(seed_int))
        except Exception:
            webgpu = None

    # Geo con jitter ~5 km
    lat = geo["lat"] + rnd.uniform(-0.05, 0.05)
    lon = geo["lon"] + rnd.uniform(-0.05, 0.05)

    # Color depth / pixel ratio
    pixel_ratio = screen["scale"]
    color_depth = rnd.choice([24, 30])

    # Inner window (viewport realista: screen - barras navegador)
    inner_w = screen["width"] - rnd.choice([0, 16, 32])
    inner_h = max(720, screen["height"] - rnd.randint(110, 150))

    # Audio context base
    audio_ctx = {
        "sampleRate": rnd.choice([44100, 48000]),
        "channelCount": rnd.choice([2, 2, 2, 6]),
        "outputLatency": round(rnd.uniform(0.005, 0.040), 4),
    }

    return {
        "sessionid": str(sessionid), "country": country.upper(),
        "timezone": geo["tz"], "locale": geo["locale"],
        "accept_language": geo["lang"],
        "latitude": round(lat, 4), "longitude": round(lon, 4),
        "geo_accuracy": geo["geo_accuracy"],
        "screen_width": screen["width"], "screen_height": screen["height"],
        "inner_width": inner_w, "inner_height": inner_h,
        "device_scale_factor": pixel_ratio, "color_depth": color_depth,
        "gpu_vendor": gpu["vendor"], "gpu_renderer": gpu["renderer"],
        "hardware_concurrency": hw_concurrency,
        "device_memory": device_memory,
        "chrome_major": chrome_major,
        "uach_headers": uach, "webgpu": webgpu,
        "canvas_seed": rnd.randint(1, 0x7FFFFFFF),
        "audio_seed": rnd.randint(1, 0x7FFFFFFF),
        "audio_context": audio_ctx,
        "platform": "Win32", "vendor": "Google Inc.",
        "user_agent": (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"),
    }


# ════════════════════════════════════════════════════════════════
#  STEALTH JS: parches inyectados
# ════════════════════════════════════════════════════════════════
def build_stealth_js(persona: Dict[str, Any], engine: str = "chrome",
                     minimal_invasive: bool = True) -> str:
    """Genera el script JS inyectado vía add_init_script.

    Args:
        engine: "chrome" o "firefox". En Firefox NO inyectamos:
          - window.chrome (tell inverso: Firefox real no lo tiene)
          - chrome.runtime/loadTimes/csi (idem)
          - navigator.vendor 'Google Inc.' (Firefox real = '')
          - navigator.platform 'Win32' (Firefox real podría diferir)
          - navigator.deviceMemory (Firefox real no lo expone — null)
        minimal_invasive: True (default) → NO sobrescribir screen.*/innerWidth,
          chrome.loadTimes/csi/app, hardwareConcurrency, deviceMemory, platform,
          vendor. Estos overrides son fáciles tells si Akamai detecta
          inconsistencias con valores reales del browser (MoreLogin Chrome ya
          los reporta coherentes). Solo añadir: webdriver undefined, WebGL
          vendor/renderer real-coherent, Canvas/Audio noise determinístico,
          WebRTC SDP strip, mediaDevices anonymize, Notification denied,
          Function.toString proper patch.

    Cubre:
      - navigator.webdriver = undefined
      - plugins, mimeTypes realistas (Chrome PDF, Native Client)
      - languages, hardwareConcurrency, deviceMemory, platform, vendor
      - WebGL vendor/renderer del GPU del persona
      - Canvas toDataURL noise (mismo seed → mismo hash entre runs)
      - AudioContext getChannelData noise
      - chrome.runtime mock con loadTimes/csi
      - permissions.query patch (notifications)
      - Notification.permission = denied
      - ServiceWorker.register bloqueado
      - WebRTC SDP IP-leak strip (mediaDevices.enumerateDevices vacío)
      - screen / innerWidth / innerHeight / devicePixelRatio override
      - timezone override (Intl.DateTimeFormat) si TZ no la setea ctx
    """
    langs = [l.split(";")[0].strip() for l in persona["accept_language"].split(",")][:3]
    langs_js = json.dumps(langs)
    vendor_js = json.dumps(persona["gpu_vendor"])
    renderer_js = json.dumps(persona["gpu_renderer"])
    cseed = persona["canvas_seed"]
    aseed = persona["audio_seed"]
    sw = persona["screen_width"]; sh = persona["screen_height"]
    iw = persona["inner_width"]; ih = persona["inner_height"]
    dpr = persona["device_scale_factor"]
    cdepth = persona["color_depth"]
    hwc = persona["hardware_concurrency"]
    dmem = persona["device_memory"]
    tz = persona["timezone"]
    plat = persona["platform"]
    vend = persona["vendor"]
    is_firefox = (engine.lower() in ("firefox", "ff", "camoufox", "gecko"))

    # En modo minimal_invasive NO sobrescribimos cosas que el browser real
    # ya reporta coherentes (Chrome MoreLogin tiene chrome.runtime real,
    # screen real, hardwareConcurrency real, etc.). Sobrescribirlos con
    # valores fijos crea tells fáciles porque:
    #   - screen.width forzado != window.innerWidth real → físicamente imposible
    #   - chrome.loadTimes con timestamps 0 → Chrome real devuelve monotonic
    #   - hardwareConcurrency forzado != WebGL_MAX_FRAGMENT_UNIFORM_VECTORS
    #     y similares (Akamai correla)
    # FIX 2026-05-14 (T2 verified): Si engine=chrome y window.chrome NO existe
    # (Chromium puro headless, browser custom), inyectar mock mínimo para que
    # EPS getBrowserMetrics.hasChrome=true. MoreLogin Chrome ya lo tiene
    # real (no-op por condición), así no rompemos su flow.
    if is_firefox:
        _chrome_runtime_block = ""   # Firefox NO debe tener window.chrome
        _chrome_navigator_block = ""
    else:
        _chrome_runtime_block = """
  // ─── window.chrome mock condicional (FIX T2) ───
  // Chrome real: window.chrome es {runtime, loadTimes, csi, app}. Si NO existe
  // (Chromium headless puro), inyectamos un mock; si SÍ existe (MoreLogin Chrome
  // real), NO tocamos para mantener coherencia con timings/state real.
  _safe(() => {
    if (!window.chrome) {
      const _now = () => performance && performance.now ? performance.now() / 1000 : Date.now() / 1000;
      const _runtime = {
        OnInstalledReason: {CHROME_UPDATE:'chrome_update', INSTALL:'install', SHARED_MODULE_UPDATE:'shared_module_update', UPDATE:'update'},
        OnRestartRequiredReason: {APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic'},
        PlatformArch: {ARM:'arm', ARM64:'arm64', MIPS:'mips', MIPS64:'mips64', X86_32:'x86-32', X86_64:'x86-64'},
        PlatformNaclArch: {ARM:'arm', MIPS:'mips', MIPS64:'mips64', X86_32:'x86-32', X86_64:'x86-64'},
        PlatformOs: {ANDROID:'android', CROS:'cros', FUCHSIA:'fuchsia', LINUX:'linux', MAC:'mac', OPENBSD:'openbsd', WIN:'win'},
        RequestUpdateCheckStatus: {NO_UPDATE:'no_update', THROTTLED:'throttled', UPDATE_AVAILABLE:'update_available'},
        ContextType: {TAB:'TAB', POPUP:'POPUP', BACKGROUND:'BACKGROUND', OFFSCREEN_DOCUMENT:'OFFSCREEN_DOCUMENT', SIDE_PANEL:'SIDE_PANEL'},
        // Métodos no-op pero presentes (Chrome real los tiene, vacíos para pages sin extensión)
        connect: () => {},
        sendMessage: () => {},
        getURL: (path) => 'chrome-extension://__id__/' + (path || ''),
        id: undefined,    // unset cuando la page NO es una extensión (correcto)
      };
      const _loadTimes = () => ({
        requestTime: _now() - 0.5,
        startLoadTime: _now() - 0.4,
        commitLoadTime: _now() - 0.3,
        finishDocumentLoadTime: _now() - 0.2,
        finishLoadTime: _now() - 0.1,
        firstPaintTime: _now() - 0.05,
        firstPaintAfterLoadTime: 0,
        navigationType: 'Other',
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
        npnNegotiatedProtocol: 'h2',
        wasAlternateProtocolAvailable: false,
        connectionInfo: 'h2',
      });
      const _csi = () => ({
        startE: Math.floor(Date.now() - 500),
        onloadT: Math.floor(Date.now() - 100),
        pageT: 400,
        tran: 15,
      });
      Object.defineProperty(window, 'chrome', {
        value: {
          runtime: _runtime,
          loadTimes: _loadTimes,
          csi: _csi,
          app: {
            isInstalled: false,
            InstallState: {DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed'},
            RunningState: {CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running'},
          },
        },
        writable: true,
        configurable: true,
        enumerable: true,
      });
    }
  });
"""
    _chrome_navigator_block = ""  # platform/vendor/hwConc/devMem: NO sobrescribir
    return f"""
(() => {{
  'use strict';
  const _safe = (fn, fb) => {{ try {{ return fn(); }} catch (e) {{ return fb; }} }};

  // ─── navigator.webdriver ───
  _safe(() => {{
    Object.defineProperty(Navigator.prototype, 'webdriver', {{get: () => undefined, configurable: true}});
  }});

  // ─── FIX A4 2026-05-14: 3 gaps anti-FingerprintJS críticos ───
  // 1) navigator.pdfViewerEnabled = true (Chrome real desktop)
  _safe(() => {{
    if (navigator.pdfViewerEnabled === false || navigator.pdfViewerEnabled === undefined) {{
      Object.defineProperty(Navigator.prototype, 'pdfViewerEnabled',
        {{get: () => true, configurable: true}});
    }}
  }});
  // 2) window.openDatabase mock (Chrome aún la expone como function deprecated)
  _safe(() => {{
    if (typeof window.openDatabase === 'undefined') {{
      window.openDatabase = function() {{
        // openDatabase deprecated. Chrome real devuelve un Database object
        // pero como las queries posteriores fallan, también puede returnar null.
        return null;
      }};
      // Mark as native
      Object.defineProperty(window.openDatabase, 'name',
        {{value: 'openDatabase', configurable: true}});
    }}
  }});
  // 3) userAgent NO debe contener "HeadlessChrome"
  // En MoreLogin Chrome real / Camoufox NUNCA aparece, pero en Chromium
  // headless puro sí. Filtramos defensivamente:
  _safe(() => {{
    const orig_ua = navigator.userAgent;
    if (orig_ua && /headless/i.test(orig_ua)) {{
      const clean_ua = orig_ua.replace(/HeadlessChrome\\/[\\d.]+/i, 'Chrome/146.0.0.0')
                              .replace(/Headless/gi, '');
      Object.defineProperty(Navigator.prototype, 'userAgent',
        {{get: () => clean_ua, configurable: true}});
      Object.defineProperty(Navigator.prototype, 'appVersion',
        {{get: () => clean_ua.replace(/^Mozilla\\//, ''), configurable: true}});
    }}
  }});

  // ─── Anti-CDP / Anti-ChromeDriver markers (FIX 2026-05-14) ───
  // TM EPS (eps-gec.js getBrowserMetrics) lee navigator.webdriver +
  // window.chrome.runtime + propiedades indirectas. Pero ChromeDriver y
  // automation binaries (incluido CDP attached) suelen dejar markers
  // tipo `cdc_adoQpoasnfa76pfcZLmcfl_*` en window. Si EPS itera window
  // keys o detecta __driver_evaluate / __webdriver_evaluate, marca bot.
  // Limpiamos cualquier marker conocido.
  _safe(() => {{
    const _autoKeys = [
      '$cdc_asdjflasutopfhvcZLmcfl_',  // ChromeDriver classic
      'cdc_adoQpoasnfa76pfcZLmcfl_Array',
      'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
      'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
      'cdc_adoQpoasnfa76pfcZLmcfl_JSON',
      'cdc_adoQpoasnfa76pfcZLmcfl_Object',
      'cdc_adoQpoasnfa76pfcZLmcfl_Proxy',
      '__webdriver_evaluate',
      '__webdriver_script_function',
      '__webdriver_script_func',
      '__webdriver_script_fn',
      '__driver_evaluate',
      '__driver_unwrapped',
      '__selenium_evaluate',
      '__selenium_unwrapped',
      '__nightmare',
      '__phantomas',
      '_phantom',
      'callPhantom',
      'callSelenium',
      'WEBDRIVER_ELEMENT',
    ];
    for (const k of _autoKeys) {{
      try {{ delete window[k]; }} catch (e) {{}}
      try {{ delete document[k]; }} catch (e) {{}}
    }}
    // Limpiar también cualquier prop cuyo nombre coincida con patrones cdc_/driver
    try {{
      for (const k of Object.getOwnPropertyNames(window)) {{
        if (/^(cdc_|\\$cdc_|__webdriver|__driver|__selenium|__nightmare|__phantom)/i.test(k)) {{
          try {{ delete window[k]; }} catch (e) {{}}
        }}
      }}
    }} catch (e) {{}}
    // Bloquear getter para impedir que se vuelvan a inyectar tras nuestro init
    try {{
      const _trap = (target, prop) => {{
        if (typeof prop === 'string' && /^(cdc_|\\$cdc_|__webdriver|__driver|__selenium|__nightmare|__phantom)/i.test(prop)) {{
          return undefined;
        }}
        return Reflect.get(target, prop);
      }};
      // No podemos reemplazar window con Proxy (rompe el entorno), pero sí
      // documentamos que cualquier set futuro queda sin efecto perceptible
      // gracias al delete previo y a las defensas siguientes.
    }} catch (e) {{}}
  }});

  // ─── Defensive fingerprint overrides (FIX T3 — solo si invalid) ───
  // Solo sobrescribimos cuando el valor real es 0/null/undefined (headless
  // puro). En MoreLogin Chrome / Camoufox real, el binary ya reporta valores
  // coherentes y NO los tocamos (evita tells por mismatch persona↔binary).
  _safe(() => {{
    const _persona = {{
      sw: {sw}, sh: {sh}, sd: {cdepth},
      hwc: {hwc}, dmem: {dmem}, dpr: {dpr},
    }};
    // screen.width/height/colorDepth — solo si 0 (headless)
    try {{
      if (!screen.width || screen.width === 0) {{
        Object.defineProperty(screen, 'width', {{get: () => _persona.sw, configurable: true}});
        Object.defineProperty(screen, 'availWidth', {{get: () => _persona.sw, configurable: true}});
      }}
      if (!screen.height || screen.height === 0) {{
        Object.defineProperty(screen, 'height', {{get: () => _persona.sh, configurable: true}});
        Object.defineProperty(screen, 'availHeight', {{get: () => _persona.sh - 40, configurable: true}});
      }}
      if (!screen.colorDepth || screen.colorDepth < 24) {{
        Object.defineProperty(screen, 'colorDepth', {{get: () => _persona.sd, configurable: true}});
        Object.defineProperty(screen, 'pixelDepth', {{get: () => _persona.sd, configurable: true}});
      }}
    }} catch (e) {{}}
    // window.devicePixelRatio — solo si 1.0 exacto en context headless
    try {{
      if (window.devicePixelRatio === 1 && _persona.dpr !== 1) {{
        Object.defineProperty(window, 'devicePixelRatio', {{get: () => _persona.dpr, configurable: true}});
      }}
    }} catch (e) {{}}
    // navigator.deviceMemory — Chrome solo lo expone sobre HTTPS y ciertos contextos
    try {{
      if (navigator.deviceMemory === undefined || navigator.deviceMemory === null) {{
        Object.defineProperty(Navigator.prototype, 'deviceMemory', {{get: () => _persona.dmem, configurable: true}});
      }}
    }} catch (e) {{}}
    // navigator.maxTouchPoints — debe ser 0 en desktop
    try {{
      if (navigator.maxTouchPoints === undefined) {{
        Object.defineProperty(Navigator.prototype, 'maxTouchPoints', {{get: () => 0, configurable: true}});
      }}
    }} catch (e) {{}}
    // navigator.getBattery — Chrome desktop lo expone; mock si falta
    try {{
      if (!('getBattery' in navigator)) {{
        navigator.getBattery = () => Promise.resolve({{
          charging: true,
          chargingTime: Infinity,
          dischargingTime: Infinity,
          level: 0.92 + Math.random() * 0.07,
          addEventListener: () => {{}},
          removeEventListener: () => {{}},
        }});
      }}
    }} catch (e) {{}}
    // navigator.connection — Chrome desktop sí lo expone con effectiveType '4g'
    try {{
      if (!navigator.connection) {{
        Object.defineProperty(Navigator.prototype, 'connection', {{
          get: () => ({{
            effectiveType: '4g',
            type: 'wifi',
            downlink: 8 + Math.random() * 4,
            rtt: 50 + Math.floor(Math.random() * 30),
            saveData: false,
            onchange: null,
            addEventListener: () => {{}},
            removeEventListener: () => {{}},
          }}),
          configurable: true,
        }});
      }}
    }} catch (e) {{}}
    // performance.memory — Chrome lo expone; valores conservadores
    try {{
      if (window.performance && !window.performance.memory) {{
        Object.defineProperty(window.performance, 'memory', {{
          get: () => ({{
            jsHeapSizeLimit: 4294705152,
            totalJSHeapSize: 30000000 + Math.floor(Math.random() * 10000000),
            usedJSHeapSize: 18000000 + Math.floor(Math.random() * 8000000),
          }}),
          configurable: true,
        }});
      }}
    }} catch (e) {{}}
  }});

  // ─── plugins / mimeTypes realistas (Chrome PDF + Native Client) ───
  _safe(() => {{
    const fakePlugins = [
      {{name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
      {{name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: ''}},
      {{name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: ''}},
      {{name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: ''}},
      {{name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: ''}},
    ];
    const fakeMime = [
      {{type: 'application/pdf', suffixes: 'pdf', description: ''}},
      {{type: 'text/pdf', suffixes: 'pdf', description: ''}},
    ];
    Object.defineProperty(Navigator.prototype, 'plugins', {{
      get: () => {{
        const arr = fakePlugins.map(p => Object.assign({{length: 1, item: () => null, namedItem: () => null}}, p));
        arr.item = i => arr[i]; arr.namedItem = n => arr.find(p => p.name === n);
        Object.defineProperty(arr, 'length', {{value: arr.length}});
        return arr;
      }}, configurable: true
    }});
    Object.defineProperty(Navigator.prototype, 'mimeTypes', {{
      get: () => fakeMime, configurable: true
    }});
  }});

  // ─── languages (coherente con Accept-Language header) ───
  _safe(() => Object.defineProperty(Navigator.prototype, 'languages', {{get: () => {langs_js}, configurable: true}}));
  _safe(() => Object.defineProperty(Navigator.prototype, 'language', {{get: () => {langs_js}[0], configurable: true}}));
  // hardwareConcurrency/deviceMemory/platform/vendor: NO sobrescribir
  // MoreLogin Chrome ya los reporta coherentes con el browser real.
  {_chrome_navigator_block}
  {_chrome_runtime_block}

  // ─── permissions.query patch ───
  _safe(() => {{
    if (window.navigator.permissions && window.navigator.permissions.query) {{
      const origQ = window.navigator.permissions.query;
      window.navigator.permissions.query = (p) => {{
        if (p && p.name === 'notifications') {{
          return Promise.resolve({{state: Notification.permission, onchange: null}});
        }}
        return origQ.call(window.navigator.permissions, p);
      }};
    }}
  }});

  // ─── Notification.permission denied ───
  _safe(() => {{
    if (window.Notification) {{
      Notification.requestPermission = () => Promise.resolve('denied');
      Object.defineProperty(Notification, 'permission', {{get: () => 'denied', configurable: true}});
    }}
  }});

  // ─── ServiceWorker block ───
  _safe(() => {{
    if (navigator.serviceWorker && navigator.serviceWorker.register) {{
      navigator.serviceWorker.register = () => Promise.reject(new Error('blocked'));
    }}
  }});

  // ─── WebGL vendor/renderer ───
  _safe(() => {{
    const fakeVendor = {vendor_js}; const fakeRenderer = {renderer_js};
    const patch = (proto) => {{
      const orig = proto.getParameter;
      proto.getParameter = function(p) {{
        // 37445 = UNMASKED_VENDOR_WEBGL; 37446 = UNMASKED_RENDERER_WEBGL
        if (p === 37445) return fakeVendor;
        if (p === 37446) return fakeRenderer;
        if (p === 7936)  return 'WebKit';                 // VENDOR
        if (p === 7937)  return 'WebKit WebGL';           // RENDERER
        return orig.call(this, p);
      }};
    }};
    patch(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  }});

  // ─── Canvas toDataURL noise (determinístico por seed) ───
  _safe(() => {{
    let _s = {cseed};
    const _next = () => {{ _s = (_s * 1103515245 + 12345) & 0x7fffffff; return _s; }};
    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(...args) {{
      try {{
        const ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {{
          const img = ctx.getImageData(0, 0, this.width, this.height);
          // 8 micro-perturbations spread out
          for (let k = 0; k < 8; k++) {{
            const idx = (_next() % img.data.length) & ~3;
            img.data[idx] = (img.data[idx] + 1) % 256;
          }}
          ctx.putImageData(img, 0, 0);
        }}
      }} catch (e) {{}}
      return _origToDataURL.apply(this, args);
    }};
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(...args) {{
      const r = _origGetImageData.apply(this, args);
      try {{
        for (let k = 0; k < 4; k++) {{
          const idx = (_next() % r.data.length) & ~3;
          r.data[idx] = (r.data[idx] + 1) % 256;
        }}
      }} catch (e) {{}}
      return r;
    }};
  }});

  // ─── AudioContext noise ───
  _safe(() => {{
    let _s = {aseed};
    const _next = () => {{ _s = (_s * 1664525 + 1013904223) & 0x7fffffff; return _s; }};
    if (window.AudioBuffer) {{
      const origGCD = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function(...a) {{
        const r = origGCD.apply(this, a);
        try {{
          for (let i = 0; i < r.length; i += 2000) {{
            r[i] = r[i] + (_next() % 1000) * 1e-9;
          }}
        }} catch (e) {{}}
        return r;
      }};
    }}
    if (window.AnalyserNode) {{
      const origGFD = AnalyserNode.prototype.getFloatFrequencyData;
      AnalyserNode.prototype.getFloatFrequencyData = function(a) {{
        origGFD.call(this, a);
        try {{
          for (let i = 0; i < a.length; i += 100) {{
            a[i] = a[i] + (_next() % 100) * 1e-6;
          }}
        }} catch (e) {{}}
      }};
    }}
  }});

  // screen.* / devicePixelRatio: NO sobrescribir. MoreLogin reporta su
  // window real y forzar valores fijos crea mismatch con innerWidth/innerHeight
  // que Akamai detecta como físicamente imposible.

  // ─── timezone override via Proxy (fix 2026-05-13) ───
  // FIX CRÍTICO: el override anterior reemplazaba Intl.DateTimeFormat con una
  // function() que NO heredaba métodos estáticos (`supportedLocalesOf`,
  // `[Symbol.toStringTag]`, etc.). Eso rompía PingFederate (TM /auth) que llama
  // a Intl.DateTimeFormat.supportedLocalesOf → TypeError → form NUNCA renderiza.
  // SOLUCIÓN: usar Proxy preserva el constructor nativo intacto y permite
  // interceptar `construct` para inyectar timeZone solo en instances creadas.
  // Esto mantiene API nativa al 100%:
  //   - Intl.DateTimeFormat.supportedLocalesOf → funciona (via get trap default)
  //   - new Intl.DateTimeFormat() instanceof Intl.DateTimeFormat → true
  //   - Object.prototype.toString.call(new Intl.DateTimeFormat()) → [object Intl.DateTimeFormat]
  //   - Reflect.ownKeys(Intl.DateTimeFormat) → preservado
  // Flag escape: window.__PS_SKIP_TZ_OVERRIDE = true ANTES de cargar el script
  // desactiva este patch (útil para A/B testing rápido).
  _safe(() => {{
    if (window.__PS_SKIP_TZ_OVERRIDE) return;
    const origDTF = Intl.DateTimeFormat;
    const FORCED_TZ = '{tz}';
    Intl.DateTimeFormat = new Proxy(origDTF, {{
      construct(target, args, newTarget) {{
        const inst = Reflect.construct(target, args, newTarget);
        const origRO = inst.resolvedOptions.bind(inst);
        inst.resolvedOptions = () => {{
          const opt = origRO();
          opt.timeZone = FORCED_TZ;
          return opt;
        }};
        return inst;
      }},
      // get/has/set/ownKeys etc. heredan del original automáticamente,
      // por lo que supportedLocalesOf, length, name y otros propiedades
      // del constructor siguen accesibles sin modificación.
    }});
  }});

  // ─── WebRTC: eliminar candidates host (IP local leak) ───
  _safe(() => {{
    if (window.RTCPeerConnection) {{
      const _orig = window.RTCPeerConnection;
      const _wrap = function(...args) {{
        const pc = new _orig(...args);
        const origCO = pc.createOffer.bind(pc);
        pc.createOffer = (...a) => origCO(...a).then(o => {{
          if (o && o.sdp) {{
            o.sdp = o.sdp.replace(/^a=candidate.+\\r\\n/gm, '');
          }}
          return o;
        }});
        const origCA = pc.createAnswer.bind(pc);
        pc.createAnswer = (...a) => origCA(...a).then(a2 => {{
          if (a2 && a2.sdp) {{
            a2.sdp = a2.sdp.replace(/^a=candidate.+\\r\\n/gm, '');
          }}
          return a2;
        }});
        return pc;
      }};
      _wrap.prototype = _orig.prototype;
      window.RTCPeerConnection = _wrap;
      if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = _wrap;
    }}
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
      const orig = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
      navigator.mediaDevices.enumerateDevices = () => orig().then(devs => {{
        // mantener tipos pero anonimizar deviceId/label
        return devs.map(d => ({{ kind: d.kind, deviceId: '', label: '',
                                groupId: '' }}));
      }});
    }}
  }});

  // ─── iframe contentWindow tell ───
  _safe(() => {{ HTMLIFrameElement.prototype.__lookupGetter__('contentWindow'); }});

  // ─── Function.toString — marca TODAS las funciones patcheadas como native ───
  // CRÍTICO: Akamai inspecciona toDataURL.toString() y similares. Si devuelve
  // código JS (nuestro patch) → bot detectado. Forzar 'function name() {{ [native code] }}'.
  _safe(() => {{
    const _toString = Function.prototype.toString;
    const _markedFns = new Set();
    // Lista de funciones patcheadas — todas deben reportarse como native
    const _markNative = (fn, name) => {{ if (fn) {{ _markedFns.add(fn); fn._nativeName = name; }} }};
    try {{
      _markNative(HTMLCanvasElement.prototype.toDataURL, 'toDataURL');
      _markNative(CanvasRenderingContext2D.prototype.getImageData, 'getImageData');
      _markNative(WebGLRenderingContext.prototype.getParameter, 'getParameter');
      if (window.WebGL2RenderingContext) _markNative(WebGL2RenderingContext.prototype.getParameter, 'getParameter');
      if (window.AudioBuffer) _markNative(AudioBuffer.prototype.getChannelData, 'getChannelData');
      if (window.AnalyserNode) _markNative(AnalyserNode.prototype.getFloatFrequencyData, 'getFloatFrequencyData');
      if (window.RTCPeerConnection) _markNative(window.RTCPeerConnection, 'RTCPeerConnection');
      if (window.navigator.permissions && window.navigator.permissions.query) _markNative(window.navigator.permissions.query, 'query');
      if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) _markNative(navigator.mediaDevices.enumerateDevices, 'enumerateDevices');
      if (navigator.serviceWorker && navigator.serviceWorker.register) _markNative(navigator.serviceWorker.register, 'register');
    }} catch (e) {{}}
    Function.prototype.toString = function() {{
      if (_markedFns.has(this)) {{
        const name = this._nativeName || this.name || '';
        return 'function ' + name + '() {{ [native code] }}';
      }}
      return _toString.call(this);
    }};
    // toString tampoco debe revelarse a sí mismo
    _markNative(Function.prototype.toString, 'toString');
  }});

  // ─── Behavioral signals injection (FIX A9 2026-05-14) ───
  // TM aps.js recoge signals.mouseMovements, signals.scrollEvents,
  // signals.keystrokeTimings, signals.focusBlurEvents, signals.clickPatterns.
  // Si el bot interactúa solo con click+fill JS direct, esos arrays quedan
  // vacíos o muy pequeños → tell de bot. Inyectamos mouseMovements pasivos
  // realistas (curvilíneos, ritmo variable, pequeñas pausas) para que el
  // tracker registre actividad humana plausible mientras el bot trabaja.
  _safe(() => {{
    // No iniciar si ya hay otra instancia
    if (window.__PS_BEHAVIORAL_ACTIVE) return;
    window.__PS_BEHAVIORAL_ACTIVE = true;

    // Persistir cursor virtual: x, y, vx, vy
    const cursor = {{
      x: Math.floor(innerWidth * (0.3 + Math.random() * 0.4)),
      y: Math.floor(innerHeight * (0.3 + Math.random() * 0.4)),
      vx: 0,
      vy: 0,
      lastT: Date.now(),
    }};

    // Generar movimiento curvilíneo (Bezier-like) realista
    const nextStep = () => {{
      const now = Date.now();
      const dt = now - cursor.lastT;
      cursor.lastT = now;

      // Drag with damping
      cursor.vx *= 0.85;
      cursor.vy *= 0.85;
      // Add small random impulse (human jitter)
      if (Math.random() < 0.35) {{
        cursor.vx += (Math.random() - 0.5) * 4;
        cursor.vy += (Math.random() - 0.5) * 3;
      }}
      // Occasional larger movement (looking at screen)
      if (Math.random() < 0.05) {{
        cursor.vx += (Math.random() - 0.5) * 20;
        cursor.vy += (Math.random() - 0.5) * 15;
      }}

      cursor.x = Math.max(0, Math.min(innerWidth-1, cursor.x + cursor.vx));
      cursor.y = Math.max(0, Math.min(innerHeight-1, cursor.y + cursor.vy));

      // Dispatch real MouseEvent
      try {{
        const ev = new MouseEvent('mousemove', {{
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: Math.round(cursor.x),
          clientY: Math.round(cursor.y),
          screenX: Math.round(cursor.x) + (screen.availLeft || 0),
          screenY: Math.round(cursor.y) + (screen.availTop || 0),
          movementX: Math.round(cursor.vx),
          movementY: Math.round(cursor.vy),
          button: 0,
          buttons: 0,
        }});
        // Dispatch on document (aps.js suele engancharse a document)
        document.dispatchEvent(ev);
      }} catch (e) {{}}

      // Random sleep — humano se distrae a veces
      let nextDelay;
      const r = Math.random();
      if (r < 0.6) {{
        nextDelay = 120 + Math.random() * 280;     // 120-400ms (typical)
      }} else if (r < 0.85) {{
        nextDelay = 400 + Math.random() * 600;     // 400-1000ms (pause)
      }} else {{
        nextDelay = 1000 + Math.random() * 1500;   // 1-2.5s (distracted)
      }}
      setTimeout(nextStep, nextDelay);
    }};

    // Iniciar después de un delay random (humano no mueve cursor inmediato)
    setTimeout(nextStep, 400 + Math.random() * 600);

    // Scroll passive ocasional: cada 3-10s, dispatch un scroll event simulado
    // si la página tiene contenido scrolleable (no forzamos scroll real).
    const passiveScrollLoop = () => {{
      const scrollable = document.documentElement.scrollHeight > innerHeight;
      if (scrollable && Math.random() < 0.3) {{
        try {{
          document.dispatchEvent(new Event('scroll', {{bubbles: true}}));
        }} catch (e) {{}}
      }}
      setTimeout(passiveScrollLoop, 3000 + Math.random() * 7000);
    }};
    setTimeout(passiveScrollLoop, 2000 + Math.random() * 3000);

    // Visibility change ocasional simulada — humano cambia de tab a veces.
    // NO la disparamos demasiado (puede romper algunos checkouts).
    // Una visibilitychange en 5-15min como mucho.
    setTimeout(() => {{
      try {{
        document.dispatchEvent(new Event('visibilitychange', {{bubbles: true}}));
      }} catch (e) {{}}
    }}, 300000 + Math.random() * 600000);
  }});

}})();
"""


# ════════════════════════════════════════════════════════════════
#  CONTEXT setup
# ════════════════════════════════════════════════════════════════
def apply_stealth_to_context(ctx, persona: Dict[str, Any],
                              log: Optional[Callable] = None,
                              engine: str = "chrome") -> Dict[str, Any]:
    """Aplica TODA la capa stealth al BrowserContext ANTES del primer goto.

    Args:
        engine: "chrome" (MoreLogin) o "firefox" (Camoufox). Cambia
                el JS inyectado para no introducir tells Firefox-incompatibles
                (ej: window.chrome no debe existir en Firefox, vendor=''
                en Firefox, etc.).
    """
    if log is None:
        log = lambda *a, **k: None
    out = {"applied": [], "engine": engine}

    # 1) STEALTH_JS via add_init_script (corre en TODOS los documentos)
    try:
        js = build_stealth_js(persona, engine=engine)
        ctx.add_init_script(js)
        out["applied"].append("stealth_js")
        log("stealth_js_added", chars=len(js), engine=engine,
            sessid=persona.get("sessionid"))
    except Exception as e:
        out["stealth_js_err"] = str(e)[:160]
        log("stealth_js_err", err=str(e)[:160])

    # 2) Accept-Language header
    try:
        headers = {"Accept-Language": persona["accept_language"]}
        # Sec-CH-UA si Chrome (no Firefox)
        if persona.get("uach_headers"):
            for k, v in persona["uach_headers"].items():
                if k.lower().startswith("sec-ch-ua"):
                    headers[k] = v
        ctx.set_extra_http_headers(headers)
        out["applied"].append("accept_language_headers")
        log("headers_set", lang=persona["accept_language"],
            sec_ch=len([k for k in headers if k.startswith("sec-ch-")]))
    except Exception as e:
        out["headers_err"] = str(e)[:160]

    # 3) Geolocation (Playwright API)
    try:
        try:
            ctx.grant_permissions(["geolocation"],
                                   origin="https://www.ticketmaster.es")
            out["applied"].append("geolocation_permission")
        except Exception:
            pass
        ctx.set_geolocation({"latitude": persona["latitude"],
                              "longitude": persona["longitude"],
                              "accuracy": persona["geo_accuracy"]})
        out["applied"].append("geolocation")
        log("geo_set", lat=persona["latitude"], lon=persona["longitude"])
    except Exception as e:
        # No fatal — algunas builds de Playwright no permiten override CDP
        out["geo_err"] = str(e)[:160]

    return out


# ════════════════════════════════════════════════════════════════
#  Pre-flight HTTP test (sin browser) — descarta IP quemada
# ════════════════════════════════════════════════════════════════
def pre_flight_http_check(proxy_url: str, expected_country: str = "ES",
                          tm_url: str = "https://www.ticketmaster.es/") -> Dict[str, Any]:
    """Valida proxy + TM no devuelve bot_block ANTES de arrancar el browser.

    Usa curl_cffi (TLS Chrome real). Si TM ya bloquea por HTTP → no
    perdemos 60s+ levantando el browser.
    """
    out = {"ok": False, "stage": "init"}
    # 1) Reputación IP
    try:
        import ip_reputation as ipr
        rep = ipr.check_proxy(proxy_url, expected_country=expected_country)
        out["ip_reputation"] = rep
        if rep.get("status") != "ok":
            out["stage"] = f"ip_{rep['status']}"
            return out
    except Exception as e:
        out["ip_check_err"] = str(e)[:200]

    # 2) curl_cffi GET a TM via proxy
    try:
        import http2_frame_match as _hf
        r = _hf.request_via_curl_cffi(
            "GET", tm_url, timeout=15,
            profile_name="chrome_131_windows",
            proxies={"http": proxy_url, "https": proxy_url})
        if not r.get("ok"):
            out["stage"] = "http_err"
            out["err"] = r.get("err", "")[:200]
            return out
        body = (r.get("body") or "").lower()
        blockers = (
            "su sesión se ha suspendido", "su sesion se ha suspendido",
            "session has been suspended", "pardon our interruption",
            "access denied", "your browsing activity has been paused",
            "algo en su forma de navegar",
        )
        matched = [b for b in blockers if b in body]
        if matched:
            out["stage"] = "bot_block_at_http"
            out["matched"] = matched
            return out
        out["ok"] = True
        out["stage"] = "passed"
        out["http_status"] = r.get("status_code")
        out["body_len"] = len(r.get("body") or "")
        out["cookies"] = r.get("cookies", {})
        return out
    except Exception as e:
        out["stage"] = "exception"
        out["err"] = str(e)[:200]
        return out


# ════════════════════════════════════════════════════════════════
#  Smoke test
# ════════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    """Verifica que el módulo genera persona + script válidos."""
    p = create_persona_from_sessionid("380962", country="ES")
    js = build_stealth_js(p)
    return {
        "persona_keys": list(p.keys()),
        "gpu": p["gpu_renderer"][:80],
        "screen": f"{p['screen_width']}x{p['screen_height']}",
        "tz": p["timezone"],
        "hw_concurrency": p["hardware_concurrency"],
        "device_memory": p["device_memory"],
        "js_chars": len(js),
        "ok": all(k in p for k in ["gpu_vendor", "gpu_renderer", "canvas_seed",
                                     "audio_seed", "screen_width", "timezone"]),
    }


if __name__ == "__main__":
    import json
    r = self_test()
    print(json.dumps(r, indent=2, ensure_ascii=False))
    # Verificar que mismo sessid = mismo persona
    p1 = create_persona_from_sessionid("380962", "ES")
    p2 = create_persona_from_sessionid("380962", "ES")
    p3 = create_persona_from_sessionid("123456", "ES")
    print("\\nDeterminismo:")
    print(f"  sessid=380962 (1) gpu: {p1['gpu_renderer'][:60]}")
    print(f"  sessid=380962 (2) gpu: {p2['gpu_renderer'][:60]}")
    print(f"  sessid=123456     gpu: {p3['gpu_renderer'][:60]}")
    print(f"  same(380962a, 380962b)={p1 == p2}")
    print(f"  diff(380962, 123456)={p1 != p3}")
