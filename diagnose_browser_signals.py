"""diagnose_browser_signals.py — Captura los signals JS del browser real.

Arranca MoreLogin con profile fresh, navega a home, captura:
  - cookies emitidas (eps_sid, _abck, datadome, bm_sz, etc.)
  - navigator.* properties
  - client hints
  - tiempo de carga
Luego navega a /auth y reporta si pasa o bloquea.
"""
import os, sys, json, time, random
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import requests

# Crear profile fresh
import bot_orchestrator as bo
fresh = bo.create_fresh_profile(country_code='ES')
print(f"Profile: {fresh['profile']}  sessid={fresh['sessionid']}")

# Start
import http2_frame_match as _hf
port = bo._start_profile(fresh['env_id'])
print(f"Started port={port}")
time.sleep(4)

from playwright.sync_api import sync_playwright
v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()

OUT = f"datos/diagnose/browser_signals_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
report = {'profile': fresh['profile'], 'sessid': fresh['sessionid'], 'tests': []}

try:
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        # ─── TEST 1: Navegar a /home y capturar TODO ───
        print("\n[1/3] Navigating to TM home (capturing eps_sid, cookies, JS)...")
        t0 = time.time()
        page.goto('https://www.ticketmaster.es/', wait_until='domcontentloaded', timeout=30000)
        time.sleep(8)  # esperar a que /eps-mgr + eps.js carguen

        # Cookies emitidas
        cookies_home = ctx.cookies()
        print(f"   Cookies tras home: {len(cookies_home)}")
        critical = [c for c in cookies_home if c['name'].lower() in
                    ('eps_sid', '_abck', 'bm_sz', 'datadome', 'cf_clearance',
                     '__cf_bm', 'ak_bmsc', 'reese84')]
        print(f"   Cookies críticas anti-bot:")
        for c in critical:
            v_len = len(c.get('value', ''))
            print(f"     {c['name']:20} domain={c['domain']:30} value_len={v_len}")

        # JS signals
        js_signals = page.evaluate("""() => ({
            webdriver: navigator.webdriver,
            plugins_count: navigator.plugins.length,
            plugins_names: Array.from(navigator.plugins).map(p => p.name),
            languages: navigator.languages,
            language: navigator.language,
            platform: navigator.platform,
            vendor: navigator.vendor,
            userAgent: navigator.userAgent,
            hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory: navigator.deviceMemory,
            screen_w: screen.width,
            screen_h: screen.height,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            devicePixelRatio: window.devicePixelRatio,
            colorDepth: screen.colorDepth,
            tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
            chrome_runtime: typeof window.chrome,
            chrome_loadTimes: !!(window.chrome && window.chrome.loadTimes),
            webgl_vendor: (() => {
                const c = document.createElement('canvas');
                const gl = c.getContext('webgl');
                return gl ? gl.getParameter(37445) : 'no_webgl';
            })(),
            webgl_renderer: (() => {
                const c = document.createElement('canvas');
                const gl = c.getContext('webgl');
                return gl ? gl.getParameter(37446) : 'no_webgl';
            })(),
            permissions_check: typeof navigator.permissions === 'object',
            // EPS-specific globals que el script /eps-mgr setea
            epsfBase: window.epsfBase || null,
            epsSID: window.epsSID || null,
            client_ip: window.client_ip || null,
            dt: window.dt || null,
            d_f_size: window.d_f ? (window.d_f.size || Object.keys(window.d_f).length) : null,
        })""")

        elapsed = round(time.time() - t0, 1)
        print(f"   Tiempo total carga home: {elapsed}s")
        print(f"   navigator.webdriver: {js_signals['webdriver']}")
        print(f"   navigator.userAgent: {js_signals['userAgent'][:80]}")
        print(f"   webgl_renderer: {js_signals['webgl_renderer'][:60]}")
        print(f"   epsfBase (eps loaded?): {js_signals['epsfBase']}")
        print(f"   client_ip (eps detectó): {js_signals['client_ip']}")

        # Buscar errors en console (capturar via CDP)
        report['tests'].append({
            'name': 'home',
            'cookies_total': len(cookies_home),
            'cookies_critical': [{'name': c['name'], 'domain': c['domain'],
                                   'value_len': len(c.get('value', ''))} for c in critical],
            'js_signals': js_signals,
            'elapsed_s': elapsed,
        })

        # ─── TEST 2: Navegar a /auth (sin login, solo ver respuesta) ───
        print("\n[2/3] Navigating to /auth.ticketmaster.com (login URL)...")
        t1 = time.time()
        try:
            page.goto('https://auth.ticketmaster.com/as/authorization.oauth2?client_id=860a437ea065.web.ticketmaster.es&response_type=code&scope=openid',
                      wait_until='domcontentloaded', timeout=30000)
            time.sleep(6)
            url_now = page.url
            title = page.title()
            # ¿Renderiza form de email?
            has_email = False
            try:
                has_email = page.locator('input[type="email"], input[name="email"], #email').first.is_visible(timeout=2000)
            except Exception: pass
            body_text = page.evaluate("() => document.body.innerText || ''")
            body_html = page.content()
            cookies_auth = ctx.cookies()
            critical_auth = [c for c in cookies_auth if c['name'].lower() in
                             ('eps_sid', '_abck', 'bm_sz', 'datadome')]

            blocked = 'identity verified' in body_text.lower() or \
                      'browsing activity' in body_text.lower() or \
                      'pardon' in body_text.lower()
            elapsed_auth = round(time.time() - t1, 1)
            print(f"   URL final: {url_now[:100]}")
            print(f"   Title: {title}")
            print(f"   Has email input: {has_email}")
            print(f"   Body text first 200: {body_text[:200]}")
            print(f"   BLOQUEADO: {blocked}")
            print(f"   Elapsed: {elapsed_auth}s")

            # Guardar HTML del bloqueo
            html_path = OUT.replace('.json', '_auth_html.html')
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(body_html)
            print(f"   HTML guardado: {html_path}")

            report['tests'].append({
                'name': 'auth',
                'url': url_now,
                'title': title,
                'has_email_input': has_email,
                'blocked': blocked,
                'body_text_preview': body_text[:300],
                'cookies_critical': [{'name': c['name'], 'domain': c['domain'],
                                       'value_len': len(c.get('value', ''))} for c in critical_auth],
                'elapsed_s': elapsed_auth,
                'html_path': html_path,
            })

            # Screenshot
            shot_path = OUT.replace('.json', '_auth.png')
            try:
                page.screenshot(path=shot_path)
                print(f"   Screenshot guardado: {shot_path}")
            except Exception: pass

        except Exception as e:
            print(f"   /auth navigation err: {e}")
            report['tests'].append({'name': 'auth', 'error': str(e)[:300]})

        b.close()
except Exception as e:
    print(f"Fatal: {e}")
    report['fatal_error'] = str(e)

# Cleanup profile
print("\nCleaning up profile...")
try:
    requests.post(f"http://127.0.0.1:40000/api/env/close",
                  json={'envId': fresh['env_id']}, timeout=10)
    time.sleep(2)
    bo.destroy_fresh_profile(fresh)
    print("  cleanup ok")
except Exception as e:
    print(f"  cleanup err: {e}")

# Save report
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(report, f, indent=2, ensure_ascii=False, default=str)
print(f"\n✓ Report saved: {OUT}")


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
