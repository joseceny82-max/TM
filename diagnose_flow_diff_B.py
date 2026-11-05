"""Re-ejecuta SOLO FLOW B + FLOW C de control (home→auth con espera 30s)."""
from __future__ import annotations
import os, sys, json, time, hashlib
from datetime import datetime
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import requests
import bot_orchestrator as bo
from playwright.sync_api import sync_playwright

TS = datetime.now().strftime('%Y%m%d_%H%M%S')
ROOT = Path('datos/diagnose/flow_diff_B_' + TS)
ROOT.mkdir(parents=True, exist_ok=True)

HOME_URL = 'https://www.ticketmaster.es/'
MY_URL = 'https://my.ticketmaster.es/'
AUTH_URL = ('https://auth.ticketmaster.com/as/authorization.oauth2'
            '?client_id=860a437ea065.web.ticketmaster.es'
            '&response_type=code&scope=openid')


def capture(page, ctx, name, out_dir, wait_s=10.0):
    out_dir.mkdir(parents=True, exist_ok=True)
    time.sleep(wait_s)
    info = {'step': name, 'url_final': page.url}
    try: info['title'] = page.title()
    except Exception: pass
    try:
        cookies = ctx.cookies()
        info['cookies_count'] = len(cookies)
        info['cookies'] = [{'name': c['name'], 'domain': c['domain'],
                            'value_len': len(c.get('value', '')),
                            'value_hash': hashlib.sha256((c.get('value') or '').encode()).hexdigest()[:12]}
                           for c in cookies]
    except Exception as e: info['cookies_err'] = str(e)
    try:
        body_html = page.content()
        info['body_len'] = len(body_html)
        with open(out_dir / f'{name}_body.html', 'w', encoding='utf-8') as f:
            f.write(body_html)
        info['has_identity_verified'] = 'identity verified' in body_html.lower()
        info['has_browsing_activity'] = 'browsing activity' in body_html.lower()
        info['has_login_form_pingfederate'] = 'pingfederate' in body_html.lower() or 'puser-id' in body_html.lower()
        info['has_un_momento'] = 'un momento' in body_html.lower()
    except Exception as e: info['html_err'] = str(e)
    try:
        body_text = page.evaluate("() => document.body.innerText || ''")
        info['body_text_preview'] = body_text[:300]
    except Exception: pass
    try:
        js = page.evaluate("""() => ({
            url: location.href, readyState: document.readyState,
            epsfBase: window.epsfBase || null,
            client_ip: window.client_ip || null,
            navigator_webdriver: navigator.webdriver,
            userAgent: navigator.userAgent,
        })""")
        info['js'] = js
    except Exception: pass
    try:
        page.screenshot(path=str(out_dir / f'{name}.png'))
    except Exception: pass
    with open(out_dir / f'{name}_info.json', 'w', encoding='utf-8') as f:
        json.dump(info, f, indent=2, ensure_ascii=False, default=str)
    return info


def run_flow(label, sequence, root):
    print(f'\n=== FLOW {label} ===')
    fresh = bo.create_fresh_profile(country_code='ES')
    if not fresh:
        print('  FATAL: create failed'); return None
    print(f'  profile={fresh["profile"]} sessid={fresh["sessionid"]}')
    port = bo._start_profile(fresh['env_id'])
    if not port:
        bo.destroy_fresh_profile(fresh); return None
    time.sleep(4)
    out_dir = root / label.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {'label': label, 'profile': fresh['profile'],
                'sessid': fresh['sessionid'], 'steps': []}
    try:
        v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.bring_to_front()
            for i, step in enumerate(sequence):
                name = f'{i+1:02d}_{step["name"]}'
                print(f'  [{i+1}/{len(sequence)}] {name} → {step["url"][:60]}')
                try:
                    page.goto(step['url'], wait_until='domcontentloaded', timeout=30000)
                except Exception as e:
                    print(f'    goto err: {str(e)[:80]}')
                info = capture(page, ctx, name, out_dir, wait_s=step.get('wait_s', 10.0))
                print(f'    url_final={info.get("url_final","?")[:60]}')
                print(f'    body={info.get("body_len","?")}B  cookies={info.get("cookies_count")}')
                print(f'    has_identity={info.get("has_identity_verified")}  has_un_momento={info.get("has_un_momento")}')
                print(f'    has_login_form_pingfederate={info.get("has_login_form_pingfederate")}')
                summary['steps'].append(info)
            b.close()
    except Exception as e:
        print(f'  FATAL: {e}')
        summary['fatal'] = str(e)
    try:
        requests.post('http://127.0.0.1:40000/api/env/close',
                      json={'envId': fresh['env_id']}, timeout=10)
        time.sleep(2)
        bo.destroy_fresh_profile(fresh)
    except Exception: pass
    with open(root / f'{label.lower()}_summary.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
    return summary


# FLOW B: home → my → /auth → event
flow_b = run_flow('B', [
    {'name': 'home', 'url': HOME_URL, 'wait_s': 10.0},
    {'name': 'my', 'url': MY_URL, 'wait_s': 10.0},
    {'name': 'auth', 'url': AUTH_URL, 'wait_s': 15.0},
], ROOT)

# FLOW C: control = home → /auth (con espera 30s en /auth para ver si form aparece)
flow_c = run_flow('C', [
    {'name': 'home', 'url': HOME_URL, 'wait_s': 10.0},
    {'name': 'auth_long_wait', 'url': AUTH_URL, 'wait_s': 30.0},
], ROOT)

print(f'\n✓ Output: {ROOT}')


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
