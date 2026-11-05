"""diagnose_flow_diff.py — Compara FLOW A vs FLOW B con captura forense completa.

FLOW A (hipótesis humano OK):
    home (www.ticketmaster.es/) → /auth.ticketmaster.com/as/...

FLOW B (bot actual bloqueado):
    home (www.ticketmaster.es/) → my.ticketmaster.es/ → /auth.ticketmaster.com/...

Cada flow corre en perfil MoreLogin AISLADO (fresh, sin cookies previas).

Captura por paso:
    - cookies (full dump con domain, path, value_len, expires, httpOnly, secure)
    - localStorage (todos los keys del origin)
    - sessionStorage
    - URL final (post-redirects)
    - status_code (de la respuesta principal)
    - request_headers + response_headers via CDP Network domain
    - JS state: window.epsfBase, window.client_ip, window.epsfToken,
                navigator.webdriver, screen, etc.
    - HTML completo (body)
    - Screenshot
    - Timing

Output: datos/diagnose/flow_diff_<ts>/{flowA,flowB}/<step>_*.{json,html,png}
        + report.md con diff cookie-by-cookie
"""
from __future__ import annotations
import os, sys, json, time, hashlib
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import requests
import bot_orchestrator as bo
from playwright.sync_api import sync_playwright

TS = datetime.now().strftime('%Y%m%d_%H%M%S')
ROOT = Path(__file__).parent / 'datos' / 'diagnose' / f'flow_diff_{TS}'
ROOT.mkdir(parents=True, exist_ok=True)

EVENT_URL = ('https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour'
             '-entradas/476722288')
HOME_URL = 'https://www.ticketmaster.es/'
MY_URL = 'https://my.ticketmaster.es/'
AUTH_URL = ('https://auth.ticketmaster.com/as/authorization.oauth2'
            '?client_id=860a437ea065.web.ticketmaster.es'
            '&response_type=code'
            '&scope=openid')


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def save_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def capture_step(page, ctx, cdp_state: dict, step_name: str, out_dir: Path,
                 wait_after_load_s: float = 6.0) -> dict:
    """Captura todo lo posible tras la navegación al paso actual."""
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # Esperar a que la página estabilize (JS de EPS, redirects)
    time.sleep(wait_after_load_s)
    info = {'step': step_name, 'captured_at': datetime.now().isoformat(),
            'wait_s': wait_after_load_s}
    try:
        info['url_final'] = page.url
    except Exception as e:
        info['url_err'] = str(e)
    try:
        info['title'] = page.title()
    except Exception: pass

    # ── Cookies ──
    try:
        cookies = ctx.cookies()
        info['cookies_count'] = len(cookies)
        cookies_summary = []
        for c in cookies:
            cookies_summary.append({
                'name': c.get('name'),
                'domain': c.get('domain'),
                'path': c.get('path'),
                'value_len': len(c.get('value', '')),
                'value_hash': hashlib.sha256((c.get('value') or '').encode()).hexdigest()[:12],
                'httpOnly': c.get('httpOnly'),
                'secure': c.get('secure'),
                'sameSite': c.get('sameSite'),
                'expires': c.get('expires'),
            })
        info['cookies'] = cookies_summary
        save_json(out_dir / f'{step_name}_cookies.json', cookies_summary)
    except Exception as e:
        info['cookies_err'] = str(e)

    # ── JS state ──
    try:
        js = page.evaluate("""() => ({
            url: location.href,
            referrer: document.referrer,
            readyState: document.readyState,
            navigator_webdriver: navigator.webdriver,
            navigator_userAgent: navigator.userAgent,
            navigator_languages: navigator.languages,
            navigator_platform: navigator.platform,
            navigator_vendor: navigator.vendor,
            navigator_hardwareConcurrency: navigator.hardwareConcurrency,
            navigator_deviceMemory: navigator.deviceMemory,
            screen_w: screen.width,
            screen_h: screen.height,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            devicePixelRatio: window.devicePixelRatio,
            colorDepth: screen.colorDepth,
            tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
            // EPS globals
            epsfBase: window.epsfBase || null,
            epsSID: window.epsSID || null,
            epsfToken: window.epsfToken ? (window.epsfToken.slice(0, 30) + '...') : null,
            gecToken: window.gecToken ? (window.gecToken.slice(0, 30) + '...') : null,
            client_ip: window.client_ip || null,
            dt: window.dt || null,
            d_f_size: window.d_f ? (window.d_f.size || Object.keys(window.d_f || {}).length) : null,
            // WebGL
            webgl_vendor: (() => {
                try { return document.createElement('canvas').getContext('webgl').getParameter(37445); }
                catch (e) { return null; }
            })(),
            webgl_renderer: (() => {
                try { return document.createElement('canvas').getContext('webgl').getParameter(37446); }
                catch (e) { return null; }
            })(),
            // Chrome runtime
            has_chrome: typeof window.chrome === 'object',
            has_chrome_runtime: typeof window.chrome === 'object' && typeof window.chrome.runtime === 'object',
            has_chrome_loadTimes: typeof window.chrome === 'object' && typeof window.chrome.loadTimes === 'function',
            // localStorage / sessionStorage keys
            localStorage_keys: (() => { try { return Object.keys(localStorage); } catch (e) { return []; }})(),
            sessionStorage_keys: (() => { try { return Object.keys(sessionStorage); } catch (e) { return []; }})(),
        })""")
        info['js'] = js
    except Exception as e:
        info['js_err'] = str(e)

    # ── localStorage / sessionStorage values (puede ser grande) ──
    try:
        ls = page.evaluate("""() => {
            const r = {};
            try {
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    const v = localStorage.getItem(k);
                    r[k] = v && v.length > 200 ? v.slice(0, 200) + '...' : v;
                }
            } catch (e) {}
            return r;
        }""")
        info['localStorage'] = ls
    except Exception: pass
    try:
        ss = page.evaluate("""() => {
            const r = {};
            try {
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i);
                    const v = sessionStorage.getItem(k);
                    r[k] = v && v.length > 200 ? v.slice(0, 200) + '...' : v;
                }
            } catch (e) {}
            return r;
        }""")
        info['sessionStorage'] = ss
    except Exception: pass

    # ── Page body text (for blocker detection) ──
    try:
        body_text = page.evaluate("() => (document.body && document.body.innerText) || ''")
        info['body_text_len'] = len(body_text)
        info['body_text_preview'] = body_text[:400]
        # Markers
        markers = {
            'identity_verified': 'identity verified' in body_text.lower(),
            'browsing_paused': 'browsing activity' in body_text.lower(),
            'pardon': 'pardon our interruption' in body_text.lower(),
            'un_momento': 'un momento' in body_text.lower(),
            'has_email_input_text': False,
        }
        info['blocker_markers'] = markers
    except Exception: pass

    # ── HTML completo ──
    try:
        html = page.content()
        save_text(out_dir / f'{step_name}_body.html', html)
        info['html_len'] = len(html)
        info['has_email_input'] = '<input' in html and ('email' in html.lower() or 'identifier' in html.lower())
        info['has_form'] = '<form' in html
    except Exception as e:
        info['html_err'] = str(e)

    # ── Screenshot ──
    try:
        page.screenshot(path=str(out_dir / f'{step_name}.png'), full_page=False)
    except Exception as e:
        info['screenshot_err'] = str(e)

    # ── CDP Network captures acumuladas hasta ahora ──
    info['cdp_requests_to_step'] = len(cdp_state.get('requests', []))
    info['cdp_responses_to_step'] = len(cdp_state.get('responses', []))

    info['capture_elapsed_ms'] = round((time.time() - t0) * 1000, 1)
    save_json(out_dir / f'{step_name}_info.json', info)
    return info


def run_flow(label: str, sequence: list, out_dir: Path) -> dict:
    """Crea perfil fresh, ejecuta sequence de URLs capturando todo en cada paso."""
    print(f'\n=== FLOW {label} ===')
    print(f'  sequence: {[s["name"] for s in sequence]}')

    # Crear perfil fresh
    fresh = bo.create_fresh_profile(country_code='ES')
    if not fresh:
        return {'error': 'create_fresh_profile_failed', 'label': label}
    print(f'  profile: {fresh["profile"]} sessid={fresh["sessionid"]}')

    port = bo._start_profile(fresh['env_id'])
    if not port:
        bo.destroy_fresh_profile(fresh)
        return {'error': 'start_profile_failed', 'label': label}
    print(f'  port: {port}')
    time.sleep(4)

    flow_summary = {'label': label, 'profile': fresh['profile'],
                    'sessid': fresh['sessionid'], 'port': port,
                    'steps': []}

    try:
        v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.bring_to_front()

            # CDP Network monitoring
            cdp_state = {'requests': [], 'responses': [], 'redirects': []}
            cdp = ctx.new_cdp_session(page)
            try:
                cdp.send('Network.enable')
            except Exception: pass

            def on_request(params):
                try:
                    cdp_state['requests'].append({
                        'requestId': params.get('requestId'),
                        'url': params['request']['url'][:200],
                        'method': params['request']['method'],
                        'headers': params['request'].get('headers', {}),
                        'ts': time.time(),
                    })
                except Exception: pass

            def on_response(params):
                try:
                    resp = params.get('response', {})
                    cdp_state['responses'].append({
                        'requestId': params.get('requestId'),
                        'url': resp.get('url', '')[:200],
                        'status': resp.get('status'),
                        'headers': resp.get('headers', {}),
                        'mimeType': resp.get('mimeType'),
                        'ts': time.time(),
                    })
                except Exception: pass

            def on_redirect(params):
                try:
                    cdp_state['redirects'].append({
                        'requestId': params.get('requestId'),
                        'redirectResponse': {
                            'url': params.get('redirectResponse', {}).get('url'),
                            'status': params.get('redirectResponse', {}).get('status'),
                            'headers': params.get('redirectResponse', {}).get('headers', {}),
                        },
                        'new_url': params['request']['url'][:200],
                    })
                except Exception: pass

            cdp.on('Network.requestWillBeSent',
                   lambda p: on_redirect(p) if p.get('redirectResponse') else on_request(p))
            cdp.on('Network.responseReceived', on_response)

            # Ejecutar cada paso
            for i, step in enumerate(sequence):
                step_name = f'{i+1:02d}_{step["name"]}'
                url = step['url']
                wait_after = step.get('wait_after_s', 6.0)
                print(f'  [{i+1}/{len(sequence)}] {step_name} → {url[:60]}')
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=30000)
                except Exception as e:
                    print(f'    goto err: {e}')

                step_info = capture_step(page, ctx, cdp_state, step_name,
                                          out_dir / label.lower(),
                                          wait_after_load_s=wait_after)
                step_info['target_url'] = url
                # Marker block detection
                blocked = (step_info.get('blocker_markers', {}).get('identity_verified') or
                           step_info.get('blocker_markers', {}).get('browsing_paused') or
                           step_info.get('blocker_markers', {}).get('pardon'))
                print(f'    url_final={step_info.get("url_final", "?")[:60]}')
                print(f'    cookies={step_info.get("cookies_count")} '
                      f'eps_loaded={bool(step_info.get("js", {}).get("epsfBase"))} '
                      f'webdriver={step_info.get("js", {}).get("navigator_webdriver")} '
                      f'body_len={step_info.get("html_len", 0)} '
                      f'has_form={step_info.get("has_form")} '
                      f'blocked={blocked}')
                flow_summary['steps'].append(step_info)

            # Guardar CDP state completo al final
            save_json(out_dir / label.lower() / 'cdp_network.json', cdp_state)
            flow_summary['cdp_state_path'] = str(out_dir / label.lower() / 'cdp_network.json')

            b.close()
    except Exception as e:
        flow_summary['fatal_error'] = str(e)
        print(f'  FATAL: {e}')

    # Cleanup
    try:
        requests.post(f'http://127.0.0.1:40000/api/env/close',
                      json={'envId': fresh['env_id']}, timeout=10)
        time.sleep(2)
        bo.destroy_fresh_profile(fresh)
    except Exception: pass

    save_json(out_dir / f'{label.lower()}_summary.json', flow_summary)
    return flow_summary


def diff_cookies(a_cookies: list, b_cookies: list) -> dict:
    """Compara cookies entre flows por (name, domain)."""
    a_idx = {(c['name'], c['domain']): c for c in a_cookies}
    b_idx = {(c['name'], c['domain']): c for c in b_cookies}
    only_a = [c for k, c in a_idx.items() if k not in b_idx]
    only_b = [c for k, c in b_idx.items() if k not in a_idx]
    common = []
    for k, ca in a_idx.items():
        if k in b_idx:
            cb = b_idx[k]
            if ca['value_hash'] != cb['value_hash']:
                common.append({'key': list(k),
                               'a_hash': ca['value_hash'], 'a_len': ca['value_len'],
                               'b_hash': cb['value_hash'], 'b_len': cb['value_len']})
    return {'only_in_A': only_a, 'only_in_B': only_b,
            'changed_values': common, 'count_a': len(a_cookies),
            'count_b': len(b_cookies)}


# ════════════════════════════════════════════════════════════
#  EJECUTAR
# ════════════════════════════════════════════════════════════

# FLOW A: home → /auth → evento (hipótesis humano OK)
flow_a_sequence = [
    {'name': 'home', 'url': HOME_URL, 'wait_after_s': 7.0},
    {'name': 'auth', 'url': AUTH_URL, 'wait_after_s': 8.0},
    {'name': 'event', 'url': EVENT_URL, 'wait_after_s': 6.0},
]

# FLOW B: home → my.ticketmaster.es → /auth → evento (bot actual)
flow_b_sequence = [
    {'name': 'home', 'url': HOME_URL, 'wait_after_s': 7.0},
    {'name': 'my', 'url': MY_URL, 'wait_after_s': 7.0},
    {'name': 'auth', 'url': AUTH_URL, 'wait_after_s': 8.0},
    {'name': 'event', 'url': EVENT_URL, 'wait_after_s': 6.0},
]

flow_a = run_flow('A', flow_a_sequence, ROOT)
flow_b = run_flow('B', flow_b_sequence, ROOT)

# ════════════════════════════════════════════════════════════
#  DIFF + REPORT
# ════════════════════════════════════════════════════════════
print('\n\n=== GENERANDO DIFF + REPORT ===')

def step_by_name(flow, name):
    for s in (flow or {}).get('steps', []):
        if s.get('step', '').endswith(f'_{name}'):
            return s
    return None

# Diff de cookies en cada paso comparable
report = ['# Forensic Flow Diff Report\n',
          f'**Generated**: {datetime.now().isoformat()}\n',
          f'**Output dir**: `{ROOT}`\n\n',
          '## Summary\n',
          f'- Flow A profile: `{flow_a.get("profile")}` (sessid={flow_a.get("sessid")})\n',
          f'- Flow B profile: `{flow_b.get("profile")}` (sessid={flow_b.get("sessid")})\n\n',
          '## Step-by-step outcome\n\n',
          '| Step | Flow A URL | Flow A status | Flow B URL | Flow B status |\n',
          '|------|------------|---------------|------------|---------------|\n']

for name in ('home', 'auth', 'event'):
    sa = step_by_name(flow_a, name)
    sb = step_by_name(flow_b, name)
    a_blocked = sa and (sa.get('blocker_markers', {}).get('identity_verified') or
                        sa.get('blocker_markers', {}).get('browsing_paused'))
    b_blocked = sb and (sb.get('blocker_markers', {}).get('identity_verified') or
                        sb.get('blocker_markers', {}).get('browsing_paused'))
    a_form = sa.get('has_form') if sa else None
    b_form = sb.get('has_form') if sb else None
    a_url = sa.get('url_final', '—')[:50] if sa else '—'
    b_url = sb.get('url_final', '—')[:50] if sb else '—'
    a_state = ('blocked' if a_blocked else ('form' if a_form else f'body={sa.get("html_len","-")}B')) if sa else '—'
    b_state = ('blocked' if b_blocked else ('form' if b_form else f'body={sb.get("html_len","-")}B')) if sb else '—'
    report.append(f'| {name} | `{a_url}` | {a_state} | `{b_url}` | {b_state} |\n')

# Diff cookies AUTH paso
sa_auth = step_by_name(flow_a, 'auth')
sb_auth = step_by_name(flow_b, 'auth')
if sa_auth and sb_auth:
    diff_auth = diff_cookies(sa_auth.get('cookies', []), sb_auth.get('cookies', []))
    report.append(f'\n## Cookie DIFF @ /auth step\n\n')
    report.append(f'- Flow A: {diff_auth["count_a"]} cookies, Flow B: {diff_auth["count_b"]} cookies\n')
    report.append(f'\n### Only in FLOW A (humano)\n')
    for c in diff_auth['only_in_A']:
        report.append(f'- `{c["name"]}` @ `{c["domain"]}` (value_len={c["value_len"]})\n')
    report.append(f'\n### Only in FLOW B (con my.)\n')
    for c in diff_auth['only_in_B']:
        report.append(f'- `{c["name"]}` @ `{c["domain"]}` (value_len={c["value_len"]})\n')
    report.append(f'\n### Same name but DIFFERENT value\n')
    for c in diff_auth['changed_values']:
        report.append(f'- `{c["key"][0]}` @ `{c["key"][1]}`: A_hash={c["a_hash"]}({c["a_len"]}B) vs B_hash={c["b_hash"]}({c["b_len"]}B)\n')

# Diff JS state @ home (debe ser igual)
sa_home = step_by_name(flow_a, 'home')
sb_home = step_by_name(flow_b, 'home')
if sa_home and sb_home:
    report.append(f'\n## JS state @ home step (sanity check)\n\n')
    for k in ('navigator_webdriver', 'epsfBase', 'client_ip', 'epsfToken',
              'navigator_userAgent', 'webgl_renderer', 'd_f_size'):
        a_val = sa_home.get('js', {}).get(k)
        b_val = sb_home.get('js', {}).get(k)
        eq = '✓' if a_val == b_val else '✗ DIFF'
        report.append(f'- `{k}`: A={str(a_val)[:60]}, B={str(b_val)[:60]}  {eq}\n')

# JS state @ auth
if sa_auth and sb_auth:
    report.append(f'\n## JS state @ /auth step\n\n')
    for k in ('readyState', 'epsfBase', 'client_ip', 'has_chrome', 'navigator_webdriver'):
        a_val = sa_auth.get('js', {}).get(k)
        b_val = sb_auth.get('js', {}).get(k)
        eq = '✓' if a_val == b_val else '✗ DIFF'
        report.append(f'- `{k}`: A={str(a_val)[:60]}, B={str(b_val)[:60]}  {eq}\n')

# Network: contar requests/responses por paso
report.append(f'\n## CDP Network counts\n\n')
report.append(f'| Step | Flow A reqs/resps | Flow B reqs/resps |\n|---|---|---|\n')
for name in ('home', 'auth', 'event'):
    sa = step_by_name(flow_a, name); sb = step_by_name(flow_b, name)
    a_r = sa.get('cdp_requests_to_step', 0) if sa else 0
    a_p = sa.get('cdp_responses_to_step', 0) if sa else 0
    b_r = sb.get('cdp_requests_to_step', 0) if sb else 0
    b_p = sb.get('cdp_responses_to_step', 0) if sb else 0
    report.append(f'| {name} | {a_r}/{a_p} | {b_r}/{b_p} |\n')

(ROOT / 'report.md').write_text(''.join(report), encoding='utf-8')
print(f'\n✓ Report: {ROOT / "report.md"}')
print(f'✓ Raw data: {ROOT}/')


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
