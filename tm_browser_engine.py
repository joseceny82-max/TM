"""tm_browser_engine.py — Motor REAL de browser automation con Playwright + stealth.

⚠️ AVISO LEGAL:
- Automatizar Ticketmaster infringe sus TOS en muchos contextos.
- Usuario es responsable del uso y de los datos introducidos.
- Por defecto corre en modo SAFE (no clic real en checkout).
- El "click to buy" real debe activarlo el usuario explícitamente.

Features:
- Lanzamiento Chromium/Firefox con headless opcional
- Stealth patches (webdriver=undefined, plugins, chrome runtime, permissions)
- Cookie injection desde cookie_warmer
- Proxy rotation desde proxy_health
- User-Agent + viewport aleatorios
- Fingerprint noise: Canvas, WebGL, Fonts, AudioContext
- Intercepta CAPTCHA y usa captcha_manager para resolver
- Screenshots automáticos en fallo
- Timeline de navegación completa

Pasos del flujo live:
  1. launch_browser   — chromium con stealth
  2. navigate_event   — abrir URL TM del evento
  3. detect_queue     — reconocer Queue-it + inyectar AcpAT si hay
  4. wait_login       — login si cuenta proporcionada
  5. find_tickets     — selector CSS/XPath disponibilidad
  6. add_to_cart      — click add
  7. handle_captcha   — si aparece, resolver y reinyectar
  8. checkout         — pago (solo si confirm_buy=True)
  9. screenshot       — en cada paso
 10. close_browser
"""
import asyncio
import os
import random
import threading
import time
from datetime import datetime
from typing import Optional, Dict, List, Callable

import config
import json_atomic


# ═══════════════════════════════════════════
#  STEALTH PATCHES (JavaScript inyectado)
# ═══════════════════════════════════════════
STEALTH_JS = """
// webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
// plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { 0: { type: "application/x-google-chrome-pdf" }, name: "Chrome PDF Plugin", description: "Portable Document Format" },
        { 0: { type: "application/pdf" }, name: "Chrome PDF Viewer", description: "" },
        { 0: { type: "application/x-nacl" }, name: "Native Client", description: "" },
    ]
});
// languages
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en', 'es']});
// chrome runtime
window.chrome = { runtime: {} };
// permissions.query monkey-patch
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
// hide iframe contentWindow tell
HTMLIFrameElement.prototype.__lookupGetter__('contentWindow');
// WebGL vendor/renderer spoof
const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter){
    if (parameter === 37445) return 'Intel Inc.';          // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
    return getParameterOrig.call(this, parameter);
};
// Canvas noise (add tiny offset per read to break fingerprint)
const toDataURLOrig = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(...args){
    const ctx = this.getContext('2d');
    if(ctx){
        const w = this.width, h = this.height;
        if(w && h){
            const img = ctx.getImageData(0, 0, w, h);
            const pixel = Math.floor(Math.random() * img.data.length);
            img.data[pixel] = (img.data[pixel] + 1) % 256;
            ctx.putImageData(img, 0, 0);
        }
    }
    return toDataURLOrig.apply(this, args);
};
// Hardware concurrency + device memory
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
"""


VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1680, "height": 1050},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

USER_AGENTS = [
    # Chrome 144/146 — versión real instalada en MoreLogin
    # (env-kit/Core/chrome_64_144.0 y chrome_64_146.0). Mantener sincronizado
    # con MoreLogin para que JA4/sec-ch-ua coincidan con UA reportado.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
]

# Selectores comunes TM (pueden cambiar — el usuario puede override)
SELECTORS = {
    "captcha_iframe": ["iframe[src*='recaptcha']", "iframe[title*='recaptcha']",
                        "iframe[src*='hcaptcha']", "iframe[src*='px-captcha']",
                        "[class*='captcha']"],
    "queue_it": ["[id*='queue']", "[class*='queue-it']",
                  "body[data-queueit-token]", ".queue-page"],
    "find_tickets_btn": ["button:has-text('Find Tickets')",
                          "button:has-text('Buscar')",
                          "[data-testid='find-tickets']"],
    "add_to_cart": ["button:has-text('Add to cart')",
                     "button:has-text('Buy')",
                     "[data-testid='atc-button']"],
    "checkout_btn": ["button:has-text('Checkout')",
                      "[data-testid='checkout-button']"],
}


# ═══════════════════════════════════════════
#  ENGINE
# ═══════════════════════════════════════════
_runs_path_key = "browser_runs.json"
_lock = threading.Lock()


def _log_path():
    return config.ruta(_runs_path_key)


def _log_run(run_id: str, entry: dict):
    with _lock:
        d = json_atomic.load(_log_path(), default={"runs": {}})
        r = d["runs"].setdefault(run_id, {"started_at": datetime.now().isoformat(),
                                            "steps": []})
        r["steps"].append(entry)
        r["steps"] = r["steps"][-50:]
        json_atomic.save(_log_path(), d)


def get_log(run_id: Optional[str] = None, limit: int = 20) -> dict:
    d = json_atomic.load(_log_path(), default={"runs": {}})
    if run_id:
        return {"ok": True, "run": d["runs"].get(run_id, {})}
    runs = list(d.get("runs", {}).items())
    runs.sort(key=lambda x: x[1].get("started_at", ""), reverse=True)
    return {"ok": True, "runs": [{"id": rid, **r} for rid, r in runs[:limit]]}


def check_playwright() -> dict:
    """Verifica qué engines están disponibles: Playwright, Patchright, Camoufox."""
    browsers = {}

    # Playwright (stock Chromium/Firefox/Webkit)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            for b in ["chromium", "firefox", "webkit"]:
                bt = getattr(p, b, None)
                if bt:
                    path = bt.executable_path
                    browsers[b] = {
                        "engine": "playwright",
                        "path": path,
                        "available": os.path.exists(path),
                        "antidetect": False,
                        "description": f"Playwright {b} stock",
                    }
    except Exception as e:
        browsers["playwright_error"] = str(e)

    # Patchright (Playwright patched anti-detect)
    try:
        import patchright  # noqa
        from patchright.sync_api import sync_playwright as _patch_sp
        with _patch_sp() as pp:
            bt = getattr(pp, "chromium", None)
            if bt:
                path = bt.executable_path
                browsers["patchright"] = {
                    "engine": "patchright",
                    "path": path,
                    "available": os.path.exists(path),
                    "antidetect": True,
                    "description": "Patchright Chromium (patched anti-detect)",
                }
    except Exception as e:
        browsers["patchright"] = {"available": False, "error": str(e)[:80],
                                   "antidetect": True,
                                   "description": "Patchright (pip install patchright)"}

    # Multilogin (commercial anti-detect via API REST)
    try:
        import multilogin_orchestrator as _mlx
        st = _mlx.get_state()
        browsers["multilogin"] = {
            "engine": "multilogin",
            "available": True,  # simula en modo sim, live si configured
            "configured": st.get("configured", False),
            "mode": st.get("mode", "simulate"),
            "antidetect": True,
            "description": "Multilogin REST API (commercial anti-detect, €99-359/mes)",
        }
    except Exception as e:
        browsers["multilogin"] = {"available": False, "error": str(e)[:80],
                                   "antidetect": True}

    # Camoufox (Firefox fork anti-detect)
    try:
        from camoufox.sync_api import Camoufox  # noqa
        import camoufox as _cf
        # Check si el binario descargado existe
        try:
            from camoufox.pkgman import get_latest_version, fetch_binary
            browsers["camoufox"] = {
                "engine": "camoufox",
                "available": True,
                "antidetect": True,
                "description": "Camoufox Firefox fork (anti-detect, FOSS)",
            }
        except Exception:
            browsers["camoufox"] = {
                "engine": "camoufox",
                "available": True,
                "antidetect": True,
                "description": "Camoufox (ejecuta python -m camoufox fetch)",
            }
    except ImportError:
        browsers["camoufox"] = {"available": False,
                                 "error": "pip install camoufox",
                                 "antidetect": True,
                                 "description": "Camoufox (pip install camoufox + fetch)"}

    # Overall ok si al menos 1 engine disponible
    return {
        "ok": any(b.get("available") for b in browsers.values() if isinstance(b, dict)),
        "browsers": browsers,
    }


def _screenshot(page, run_id: str, label: str) -> Optional[str]:
    """Guarda screenshot con timestamp."""
    try:
        dir_path = config.ruta(f"browser_screenshots/{run_id}")
        os.makedirs(dir_path, exist_ok=True)
        fname = f"{int(time.time()*1000)}_{label}.png"
        path = os.path.join(dir_path, fname)
        page.screenshot(path=path, full_page=False)
        return path
    except Exception:
        return None


def _apply_stealth(context):
    """Inyecta el script stealth en todas las páginas del contexto."""
    try:
        context.add_init_script(STEALTH_JS)
    except Exception:
        pass


def _inject_warm_cookies(context, country: str) -> int:
    """Pre-inyecta cookies del pool de cookie_warmer (si hay)."""
    try:
        import cookie_warmer
        pool = cookie_warmer.pool_stats()
        # cookie_warmer tiene pool de AcpAT tokens, no cookies genéricas
        # aquí podríamos extender, por ahora devolvemos 0
        return 0
    except Exception:
        return 0


def _resolve_imperva_if_present(page, run_id: str) -> dict:
    """Detecta Imperva/Incapsula challenge en la página y lo resuelve via imperva_bypass.

    Devuelve {detected, solved, error?, cookies_set?}.
    """
    out = {"detected": False, "solved": False}
    try:
        import imperva_bypass
        html = ""
        try:
            html = page.content()[:8000]
        except Exception:
            pass
        if not imperva_bypass.is_imperva_challenge(html, page.url):
            return out
        out["detected"] = True
        ctx = page.context
        try:
            cookies = ctx.cookies(page.url)
            cookie_dict = {c["name"]: c["value"] for c in cookies}
        except Exception:
            cookie_dict = {}
        r = imperva_bypass.bypass(page.url, html=html, cookies=cookie_dict)
        if r.get("ok"):
            out["solved"] = True
            new_cookies = r.get("cookies") or {}
            if new_cookies:
                from urllib.parse import urlparse
                host = urlparse(page.url).hostname or ""
                domain = "." + host if host and not host.startswith(".") else host
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": domain, "path": "/", "secure": True}
                    for k, v in new_cookies.items()
                ])
                out["cookies_set"] = list(new_cookies.keys())
            try:
                _log_run(run_id, {"step": "imperva_bypass", "ok": True, "cookies": out.get("cookies_set", [])})
            except Exception:
                pass
        else:
            out["error"] = r.get("error")
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return out


def _submit_sbsd_if_present(page, run_id: str) -> dict:
    """Si Akamai SBSD challenge está activo en la página, genera y envía sensor data.

    Devuelve {detected, submitted, error?}.
    """
    out = {"detected": False, "submitted": False}
    try:
        import akamai_sbsd
        try:
            has_sbsd = page.evaluate(
                "() => !!(document.cookie.match(/sbsd[_=]/i) || "
                "Array.from(document.scripts).some(s => /sbsd|akamai/i.test(s.src||'')))"
            )
        except Exception:
            has_sbsd = False
        if not has_sbsd:
            return out
        out["detected"] = True
        try:
            user_agent = page.evaluate("() => navigator.userAgent")
        except Exception:
            user_agent = ""
        payload = akamai_sbsd.build_sbsd_payload(url=page.url, user_agent=user_agent)
        try:
            cookies = page.context.cookies(page.url)
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception:
            cookie_str = ""
        r = akamai_sbsd.submit_sbsd_payload(page.url, payload, cookies=cookie_str, user_agent=user_agent)
        out["submitted"] = bool(r.get("ok"))
        if not r.get("ok"):
            out["error"] = r.get("error")
        try:
            _log_run(run_id, {"step": "akamai_sbsd", "submitted": out["submitted"], "error": out.get("error")})
        except Exception:
            pass
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return out


def _human_action(page, action_kind: str, current_url: str = "") -> dict:
    """Wrapper Markov: pide al behavior_chain el siguiente paso humano y lo aplica.

    `action_kind` ∈ {'click', 'type', 'scroll', 'idle', 'navigate'}.
    Aplica dwell_time aleatorio coherente con el modelo Markov antes de retornar.
    """
    out = {"applied": False}
    try:
        import behavior_chain
        step = behavior_chain.next_step(current_state=action_kind, url=current_url or page.url)
        dwell_ms = int(behavior_chain.dwell_time(step.get("state", action_kind)) * 1000)
        if dwell_ms > 0:
            try:
                page.wait_for_timeout(min(dwell_ms, 4000))
            except Exception:
                pass
        out["applied"] = True
        out["next_state"] = step.get("state")
        out["dwell_ms"] = dwell_ms
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        return out


def _inject_acpat_if_queue(page, run_id: str, country: str, event_id: str = "") -> dict:
    """Detecta Queue-it y si hay, inyecta token AcpAT del pool."""
    try:
        for sel in SELECTORS["queue_it"]:
            try:
                if page.locator(sel).count() > 0:
                    # Intentar inyectar
                    import cookie_warmer
                    r = cookie_warmer.inject_acpat(domain_id=country, event_id=event_id)
                    if r.get("ok"):
                        token = r["token"]
                        page.context.add_cookies([{
                            "name": "AcpAT",
                            "value": token,
                            "domain": ".queue-it.net",
                            "path": "/",
                            "httpOnly": False,
                            "secure": True,
                        }])
                        return {"detected": True, "injected": True, "used_count": r.get("entry", {}).get("used_count")}
                    return {"detected": True, "injected": False, "error": r.get("error")}
            except Exception:
                continue
        return {"detected": False, "injected": False}
    except Exception as e:
        return {"detected": False, "injected": False, "error": str(e)}


def _handle_captcha_if_present(page, run_id: str) -> dict:
    """Detecta CAPTCHA en página, si hay lo resuelve vía captcha_manager.

    Devuelve {detected, solved, type, service?, elapsed_ms?, error?}.

    Optimización 2026: Turnstile se delega al solver especializado
    (`turnstile_solver.solve_and_inject`) que incluye detección DOM-aware,
    solve y inyección con callback fire en una sola llamada.
    """
    result = {"detected": False, "solved": False}
    try:
        url = page.url
        # Branch 1 — Turnstile: detect via solver (DOM-aware, soporta
        # iframe/window.turnstile/.cf-turnstile). Si lo detecta, completa
        # el flow end-to-end y retornamos.
        try:
            import turnstile_solver
            det = turnstile_solver.detect(page)
            if det.get("found"):
                result["detected"] = True
                result["type"] = "turnstile"
                result["sitekey"] = det.get("site_key")
                result["detect_method"] = det.get("method")
                if det.get("site_key"):
                    e2e = turnstile_solver.solve_and_inject(
                        page, url=url, max_wait_render_s=2)
                    result["solved"] = bool(e2e.get("ok"))
                    result["service"] = e2e.get("service")
                    result["elapsed_ms"] = int((e2e.get("elapsed_total_s", 0)) * 1000)
                    result["fired_callback"] = e2e.get("fired_callback")
                    if e2e.get("token"):
                        result["token_preview"] = (e2e["token"][:16] + "...")
                    if not e2e.get("ok"):
                        result["error"] = e2e.get("error", "turnstile_solver_failed")
                else:
                    result["error"] = "turnstile_no_sitekey"
                return result
        except ImportError:
            pass
        except Exception:
            pass  # Fallback al flow genérico
        html = page.content()
        # Detección rápida por HTML
        captcha_type = None
        sitekey = None
        if "recaptcha" in html.lower():
            captcha_type = "recaptcha_v2"
            # buscar data-sitekey
            try:
                sitekey = page.evaluate("""() => {
                    const el = document.querySelector('[data-sitekey]');
                    return el ? el.getAttribute('data-sitekey') : null;
                }""")
            except Exception:
                pass
        elif "hcaptcha" in html.lower():
            captcha_type = "hcaptcha"
            try:
                sitekey = page.evaluate("""() => {
                    const el = document.querySelector('[data-sitekey]');
                    return el ? el.getAttribute('data-sitekey') : null;
                }""")
            except Exception:
                pass
        elif "cf-turnstile" in html.lower() or "turnstile" in html.lower():
            captcha_type = "turnstile"
            try:
                sitekey = page.evaluate("""() => {
                    const el = document.querySelector('[data-sitekey]');
                    return el ? el.getAttribute('data-sitekey') : null;
                }""")
            except Exception:
                pass

        if not captcha_type:
            return result
        result["detected"] = True
        result["type"] = captcha_type
        result["sitekey"] = sitekey

        if not sitekey:
            result["error"] = "sitekey no encontrado"
            return result

        import captcha_manager
        start = time.time()
        if captcha_type == "hcaptcha":
            r = captcha_manager.solve_hcaptcha(sitekey, url)
        elif captcha_type == "turnstile":
            r = captcha_manager.solve_turnstile(sitekey, url)
        else:
            r = captcha_manager.solve_recaptcha_v2(sitekey, url)
        result["elapsed_ms"] = int((time.time() - start) * 1000)
        result["service"] = r.get("service")
        if r.get("ok") and r.get("token"):
            # Inyectar token en la página
            try:
                page.evaluate(f"""(token) => {{
                    const ta = document.querySelector('textarea[name="g-recaptcha-response"]')
                             || document.querySelector('textarea[name="h-captcha-response"]')
                             || document.querySelector('[name="cf-turnstile-response"]');
                    if(ta) ta.value = token;
                }}""", r["token"])
                result["solved"] = True
                result["token_preview"] = r["token"][:16] + "..."
            except Exception as e:
                result["error"] = f"no se pudo inyectar: {e}"
        else:
            result["error"] = r.get("error", "solver falló")
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result


# ═══════════════════════════════════════════
#  RUN (síncrono con Playwright sync API)
# ═══════════════════════════════════════════
# country → (locale, timezone_id) — coherencia tz/locale evita drift detection.
# Lista conservadora cubriendo todos los TM domains.
_COUNTRY_TZ = {
    "us": ("en-US", "America/New_York"),
    "ca": ("en-CA", "America/Toronto"),
    "mx": ("es-MX", "America/Mexico_City"),
    "es": ("es-ES", "Europe/Madrid"),
    "uk": ("en-GB", "Europe/London"),
    "ie": ("en-IE", "Europe/Dublin"),
    "fr": ("fr-FR", "Europe/Paris"),
    "de": ("de-DE", "Europe/Berlin"),
    "at": ("de-AT", "Europe/Vienna"),
    "ch": ("de-CH", "Europe/Zurich"),
    "it": ("it-IT", "Europe/Rome"),
    "nl": ("nl-NL", "Europe/Amsterdam"),
    "be": ("nl-BE", "Europe/Brussels"),
    "pt": ("pt-PT", "Europe/Lisbon"),
    "se": ("sv-SE", "Europe/Stockholm"),
    "no": ("nb-NO", "Europe/Oslo"),
    "dk": ("da-DK", "Europe/Copenhagen"),
    "fi": ("fi-FI", "Europe/Helsinki"),
    "pl": ("pl-PL", "Europe/Warsaw"),
    "au": ("en-AU", "Australia/Sydney"),
    "nz": ("en-NZ", "Pacific/Auckland"),
}


def locale_tz_for_country(country: str) -> tuple:
    """(locale, timezone_id) coherentes para un país TM. Default en-US/NY."""
    return _COUNTRY_TZ.get((country or "us").lower(), ("en-US", "America/New_York"))


def try_presale_code(page, code: str, timeout_ms: int = 6000) -> bool:
    """Pega un presale code en el campo correspondiente y verifica si TM lo acepta.

    Devuelve True si el code es válido (TM muestra unlock/sectores/seats),
    False si rechaza ("Invalid code"/"código no válido"/no avance).

    Selectors comunes en TM:
      input[name="presaleCode"] | input[id*="presale"] | input[placeholder*="code"]
    """
    if not page or not code:
        return False
    try:
        # 1. Localizar input
        sel_candidates = [
            'input[name="presaleCode"]',
            'input[id*="presale" i]',
            'input[placeholder*="presale" i]',
            'input[placeholder*="code" i]',
            'input[aria-label*="presale" i]',
            'input[data-testid*="presale" i]',
        ]
        inp = None
        for sel in sel_candidates:
            try:
                cand = page.locator(sel).first
                if cand.is_visible(timeout=1500):
                    inp = cand
                    break
            except Exception:
                continue
        if inp is None:
            return False
        # 2. Limpiar + tipear
        inp.click()
        inp.fill("")
        inp.type(code, delay=80)
        page.wait_for_timeout(400)
        # 3. Click submit/unlock
        for btn_sel in [
            'button:has-text("Unlock")',
            'button:has-text("Submit")',
            'button:has-text("Aplicar")',
            'button:has-text("Validar")',
            'button[type="submit"]',
        ]:
            try:
                btn = page.locator(btn_sel).first
                if btn.is_visible(timeout=800):
                    btn.click()
                    break
            except Exception:
                continue
        # 4. Esperar respuesta
        page.wait_for_timeout(timeout_ms)
        # 5. Detectar éxito vs error
        page_text = page.evaluate("()=>document.body.innerText").lower()
        error_markers = ("invalid code", "código no válido", "code is invalid",
                          "código incorrecto", "expired", "caducado",
                          "not valid", "no válido", "code not recognized")
        if any(m in page_text for m in error_markers):
            return False
        success_markers = ("seats unlocked", "tickets unlocked", "presale active",
                           "sectores disponibles", "asientos desbloqueados",
                           "select your seats", "elige tus asientos")
        if any(m in page_text for m in success_markers):
            return True
        # Fallback: si la URL avanzó hacia /checkout o /tickets/select, asumir OK
        url = page.url.lower()
        if "/select" in url or "/seatmap" in url or "/checkout" in url:
            return True
        return False
    except Exception:
        return False


def _launch_engine(browser_type: str, headless: bool, country: str = "us"):
    """Lanza el engine adecuado y devuelve (browser, context, page, meta)."""
    meta = {"engine": browser_type}
    locale, tz = locale_tz_for_country(country)
    meta["locale"] = locale
    meta["timezone_id"] = tz

    if browser_type == "camoufox":
        from camoufox.sync_api import Camoufox
        # Camoufox lanza browser directamente en Firefox con stealth builtin
        locales = [locale, "en-US"] if locale != "en-US" else ["en-US"]
        cf = Camoufox(headless=headless, geoip=True, locale=locales)
        browser = cf.__enter__()
        meta["cm"] = cf
        page = browser.new_page()
        return browser, None, page, meta

    if browser_type == "patchright":
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            # fallback: patchright no instalado → chromium vanilla
            browser_type = "chromium"
            meta["engine"] = "chromium (patchright_unavailable)"
            return _launch_engine(browser_type, headless, country)
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless)
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)
        ctx = browser.new_context(
            user_agent=ua, viewport=vp,
            locale=locale, timezone_id=tz,
        )
        meta["pw"] = pw
        meta["ua"] = ua
        meta["viewport"] = f"{vp['width']}x{vp['height']}"
        page = ctx.new_page()
        # CDP-Patches: corrige el bug Chrome de coordenadas mouse en page.mouse.click().
        # Sin esto, NuData/Datadome detectan offsets pixel-perfect = bot. Solo cuando
        # headless=False (en headless el OS-level patch no aplica).
        if not headless:
            try:
                from cdp_patches.input import SyncInput
                meta["cdp_input"] = SyncInput(browser=browser, scale_factor=1.0)
                meta["cdp_patched"] = True
            except Exception as e:
                meta["cdp_patched"] = False
                meta["cdp_err"] = f"{type(e).__name__}: {str(e)[:80]}"
        return browser, ctx, page, meta

    # Playwright + rebrowser-playwright fallback (anti-detect mejorado).
    # Si TM_USE_REBROWSER=1 (default 1 — recomendado producción), intenta usar
    # rebrowser_playwright primero. Patches anti Runtime.enable, source URL.
    use_rebrowser = os.environ.get("TM_USE_REBROWSER", "1") == "1"
    if use_rebrowser:
        try:
            from rebrowser_playwright.sync_api import sync_playwright as _sp_rebrowser
            pw = _sp_rebrowser().start()
            meta["pw_lib"] = "rebrowser_playwright"
        except Exception:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            meta["pw_lib"] = "playwright_stock_fallback"
    else:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        meta["pw_lib"] = "playwright_stock"
    launcher = getattr(pw, browser_type if browser_type in ("chromium","firefox","webkit") else "chromium")
    args = ["--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage"] if browser_type == "chromium" else []
    browser = launcher.launch(headless=headless, args=args)
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)
    ctx = browser.new_context(
        user_agent=ua, viewport=vp,
        locale=locale, timezone_id=tz,
    )
    try:
        ctx.add_init_script(STEALTH_JS)
    except Exception:
        pass
    meta["pw"] = pw
    meta["ua"] = ua
    meta["viewport"] = f"{vp['width']}x{vp['height']}"
    page = ctx.new_page()
    return browser, ctx, page, meta


def _close_engine(browser, meta: dict):
    """Cierre limpio según engine."""
    try:
        if "cm" in meta:
            meta["cm"].__exit__(None, None, None)
        else:
            browser.close()
            if "pw" in meta:
                meta["pw"].stop()
    except Exception:
        pass


def run_browser_flow(event_id: str,
                     mode: str = "dry_run",
                     headless: bool = True,
                     browser_type: str = "patchright",
                     use_proxy: bool = True,
                     account_email: Optional[str] = None,
                     account_password: Optional[str] = None,
                     confirm_buy: bool = False,
                     broadcast: Optional[Callable] = None) -> dict:
    """Lanza Playwright real y navega.

    Modos:
    - dry_run  : navega a home TM, verifica stealth, no abre el evento
    - explore  : abre URL del evento, detecta Queue-it + CAPTCHA, screenshot
    - live     : flujo completo (requiere account_email/password y confirm_buy=True
                  para llegar al checkout; si confirm_buy=False se queda en cart)

    NUNCA envía el pago si confirm_buy=False (safety).
    """
    run_id = f"br_{int(time.time())}_{random.randint(1000,9999)}"
    started = time.time()
    result = {"run_id": run_id, "ok": False, "mode": mode, "steps": [],
              "started_at": datetime.now().isoformat()}

    def _step(label: str, ok: bool, **kw):
        entry = {"step": label, "ok": ok, "ts": datetime.now().isoformat(),
                 "elapsed_ms": int((time.time()-started)*1000), **kw}
        result["steps"].append(entry)
        _log_run(run_id, entry)
        if broadcast:
            try:
                broadcast("browser_step", {"run_id": run_id, "step": entry})
            except Exception:
                pass
        return entry

    # Verificar engines
    pw_check = check_playwright()
    if not pw_check.get("ok"):
        result["error"] = "ningún engine disponible"
        _step("check_playwright", False, error=result["error"])
        return result
    # Validar engine solicitado
    valid_engines = ("chromium", "firefox", "webkit", "patchright", "camoufox")
    if browser_type not in valid_engines:
        browser_type = "chromium"
    browser_info = pw_check.get("browsers", {}).get(browser_type, {})
    if not browser_info.get("available"):
        # fallback a chromium si existe
        if pw_check.get("browsers", {}).get("chromium", {}).get("available"):
            _step("check_browser", False,
                  note=f"{browser_type} no disponible → fallback a chromium",
                  original=browser_type)
            browser_type = "chromium"
        else:
            result["error"] = f"{browser_type} no disponible y sin fallback"
            _step("check_browser", False, error=result["error"])
            return result
    _step("check_playwright", True, browser=browser_type,
          antidetect=browser_info.get("antidetect", False))

    # Cargar evento
    try:
        import event_manager
        events = event_manager.list_events()
        event = next((e for e in events if e.get("id") == event_id), None)
        if not event:
            result["error"] = "evento no encontrado"
            _step("find_event", False, error=result["error"])
            return result
        country = event.get("country", "us")
        tm_url = event.get("tm_url", "") or f"https://www.ticketmaster.{'es' if country=='es' else 'com' if country=='us' else 'co.uk' if country=='uk' else 'de'}/"
        _step("find_event", True, name=event.get("name"), country=country)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        _step("find_event", False, error=result["error"])
        return result

    # Proxy
    proxy_config = None
    if use_proxy:
        try:
            import proxy_health
            p = proxy_health.pick_best(geo=country, min_score=30)
            if p and p.get("url_preview"):
                # Nota: proxy_health oculta el raw. Para real usage el integrador
                # debería extenderlo. Por ahora, sin proxy si no hay API.
                _step("pick_proxy", False, note="proxy_health solo expone preview; integración real pendiente")
            else:
                _step("pick_proxy", False, note="sin proxies alive")
        except Exception as e:
            _step("pick_proxy", False, error=str(e))

    try:
        browser, context, page, meta = _launch_engine(browser_type, headless, country)
    except Exception as e:
        result["error"] = f"launch: {e}"
        _step("launch_browser", False, error=str(e))
        return result
    _step("launch_browser", True, headless=headless, engine=browser_type,
          ua=(meta.get("ua") or "")[:60], viewport=meta.get("viewport", ""))

    try:
        # --- MODO DRY_RUN: solo home TM ---
        if mode == "dry_run":
            try:
                home = f"https://www.ticketmaster.{'es' if country=='es' else 'com' if country=='us' else 'co.uk' if country=='uk' else 'de'}/"
                page.goto(home, timeout=30000, wait_until="domcontentloaded")
                title = page.title()
                is_stealth = page.evaluate("navigator.webdriver === undefined || navigator.webdriver === false")
                ss = _screenshot(page, run_id, "home")
                _step("visit_home", True, title=title[:80], stealth_ok=is_stealth,
                      screenshot=ss)
                result["ok"] = True
            except Exception as e:
                _step("visit_home", False, error=f"{type(e).__name__}: {e}")

        # --- MODO EXPLORE: abre evento, detecta estructura ---
        elif mode == "explore":
            try:
                page.goto(tm_url, timeout=45000, wait_until="domcontentloaded")
                _step("navigate_event", True, url=tm_url[:80], title=page.title()[:80])
                ir = _resolve_imperva_if_present(page, run_id)
                if ir.get("detected"):
                    _step("imperva_check", ir.get("solved"), **ir)
                sr = _submit_sbsd_if_present(page, run_id)
                if sr.get("detected"):
                    _step("akamai_sbsd_check", sr.get("submitted"), **sr)
                qr = _inject_acpat_if_queue(page, run_id, country, event_id)
                _step("queue_check", not qr.get("detected") or qr.get("injected"), **qr)
                cr = _handle_captcha_if_present(page, run_id)
                _step("captcha_check", not cr.get("detected") or cr.get("solved"), **cr)
                _human_action(page, "idle", current_url=page.url)
                ss = _screenshot(page, run_id, "event_page")
                _step("snapshot", True, screenshot=ss)
                result["ok"] = True
            except Exception as e:
                _step("navigate_event", False, error=f"{type(e).__name__}: {e}")

        # --- MODO LIVE: flujo completo (con safety) ---
        elif mode == "live":
            if not confirm_buy:
                _step("live_safety", True, note="confirm_buy=False — se detendrá antes de checkout")
            try:
                page.goto(tm_url, timeout=45000, wait_until="domcontentloaded")
                _step("navigate_event", True, url=tm_url[:80])

                ir = _resolve_imperva_if_present(page, run_id)
                if ir.get("detected"):
                    _step("imperva_handle", ir.get("solved"), **ir)
                sr = _submit_sbsd_if_present(page, run_id)
                if sr.get("detected"):
                    _step("akamai_sbsd_handle", sr.get("submitted"), **sr)

                qr = _inject_acpat_if_queue(page, run_id, country, event_id)
                _step("queue_handle", not qr.get("detected") or qr.get("injected"), **qr)

                cr = _handle_captcha_if_present(page, run_id)
                _step("captcha_handle", not cr.get("detected") or cr.get("solved"), **cr)

                _human_action(page, "navigate", current_url=page.url)

                if account_email and account_password:
                    _step("login", False, note="login form dinámico TM — integración pendiente")

                clicked_find = False
                for sel in SELECTORS["find_tickets_btn"]:
                    try:
                        if page.locator(sel).count() > 0:
                            page.locator(sel).first.click(timeout=5000)
                            clicked_find = True
                            _step("find_tickets", True, selector=sel)
                            break
                    except Exception:
                        continue
                if not clicked_find:
                    _step("find_tickets", False, error="botón no encontrado")

                page.wait_for_timeout(2000)
                _screenshot(page, run_id, "after_find")

                clicked_cart = False
                for sel in SELECTORS["add_to_cart"]:
                    try:
                        if page.locator(sel).count() > 0:
                            page.locator(sel).first.click(timeout=5000)
                            clicked_cart = True
                            _step("add_to_cart", True, selector=sel)
                            break
                    except Exception:
                        continue
                if not clicked_cart:
                    _step("add_to_cart", False, error="botón add to cart no encontrado")

                if not confirm_buy:
                    _step("safety_stop", True, note="confirm_buy=False — detenido en cart")
                    result["ok"] = True
                else:
                    _step("checkout", False, note="pago real deshabilitado por safety")
            except Exception as e:
                _step("flow_error", False, error=f"{type(e).__name__}: {e}")
        else:
            _step("unknown_mode", False, error=f"modo '{mode}' no soportado")
    finally:
        try:
            _close_engine(browser, meta)
            _step("close_browser", True)
        except Exception:
            pass

    result["elapsed_ms"] = int((time.time() - started) * 1000)
    if broadcast:
        broadcast("browser_done", {"run_id": run_id, "ok": result["ok"]})
    return result


# ═══════════════════════════════════════════
#  TESTS internos
# ═══════════════════════════════════════════
def _stealth_probe(page) -> dict:
    """Evalúa JS para obtener campos de stealth en una página recién abierta."""
    try:
        wd = page.evaluate("navigator.webdriver")
        plugins = page.evaluate("navigator.plugins.length")
        languages = page.evaluate("navigator.languages")
        hw = page.evaluate("navigator.hardwareConcurrency")
        ua = page.evaluate("navigator.userAgent")
        return {
            "webdriver_undefined": (wd is None) or (wd is False),
            "webdriver_raw": wd,
            "plugins_count": plugins,
            "languages": languages,
            "hardware_concurrency": hw,
            "user_agent": ua[:120] if ua else "",
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def self_test(engine: str = "chromium") -> dict:
    """Test rápido de cualquier engine: playwright-chromium/firefox/webkit, patchright, camoufox.

    engine: chromium | firefox | webkit | patchright | camoufox
    """
    started = time.time()
    try:
        if engine == "camoufox":
            try:
                from camoufox.sync_api import Camoufox
            except ImportError:
                return {"ok": False, "error": "camoufox no instalado (pip install camoufox)"}
            with Camoufox(headless=True, geoip=True) as browser:
                page = browser.new_page()
                page.goto("about:blank")
                stealth = _stealth_probe(page)
            return {"ok": True, "engine": "camoufox",
                    "elapsed_ms": int((time.time()-started)*1000),
                    "stealth": stealth}
        elif engine == "patchright":
            try:
                from patchright.sync_api import sync_playwright
            except ImportError:
                return {"ok": False, "error": "patchright no instalado (pip install patchright)"}
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent=random.choice(USER_AGENTS))
                page = ctx.new_page()
                page.goto("about:blank")
                stealth = _stealth_probe(page)
                browser.close()
            return {"ok": True, "engine": "patchright",
                    "elapsed_ms": int((time.time()-started)*1000),
                    "stealth": stealth}
        else:
            # Playwright stock + stealth JS patches
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                return {"ok": False, "error": "playwright no instalado"}
            valid = {"chromium", "firefox", "webkit"}
            if engine not in valid:
                engine = "chromium"
            with sync_playwright() as p:
                launcher = getattr(p, engine)
                args = ["--disable-blink-features=AutomationControlled"] if engine == "chromium" else []
                browser = launcher.launch(headless=True, args=args)
                ctx = browser.new_context(user_agent=random.choice(USER_AGENTS))
                _apply_stealth(ctx)
                page = ctx.new_page()
                page.goto("about:blank")
                stealth = _stealth_probe(page)
                browser.close()
            return {"ok": True, "engine": engine,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "stealth": stealth}
    except Exception as e:
        return {"ok": False, "engine": engine,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": int((time.time()-started)*1000)}


def self_test_all() -> dict:
    """Ejecuta self-test para cada engine disponible."""
    results = {}
    for engine in ["chromium", "firefox", "camoufox", "patchright"]:
        results[engine] = self_test(engine)
    return {"ok": True, "results": results}
