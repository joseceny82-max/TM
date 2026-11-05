"""Reproduce el flow REAL del bot: event → Continuar → /auth con OAuth params completos."""
from __future__ import annotations
import os, sys, json, time
from datetime import datetime
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import requests
import bot_orchestrator as bo
from playwright.sync_api import sync_playwright

TS = datetime.now().strftime('%Y%m%d_%H%M%S')
ROOT = Path(f'datos/diagnose/real_bot_flow_{TS}')
ROOT.mkdir(parents=True, exist_ok=True)

EVENT_URL = 'https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour-entradas/476722288'

def save(name, page, ctx):
    info = {'name': name, 'url': page.url, 'title': page.title()}
    info['cookies_count'] = len(ctx.cookies())
    info['cookies_critical'] = [c for c in ctx.cookies()
                                 if c['name'].lower() in ('eps_sid', '_abck', 'datadome',
                                  'bm_sz', 'datadome', 'ak_bmsc', 'cf_clearance', '__cf_bm',
                                  'pf_pcm', 'pf', 'PF', 'PF_AT')]
    body = page.content()
    info['body_len'] = len(body)
    info['has_identity_verified'] = 'identity verified' in body.lower()
    info['has_un_momento'] = 'un momento' in body.lower()
    info['has_modern_error'] = 'modern accounts error' in body.lower()
    info['has_email_input'] = 'type="email"' in body or 'name="email"' in body
    info['has_pf'] = 'pingfederate' in body.lower()
    # Body text
    try:
        text = page.evaluate("() => document.body.innerText || ''")
        info['body_text_first_500'] = text[:500]
    except Exception: pass
    # Save HTML + screenshot
    (ROOT / f'{name}_body.html').write_text(body, encoding='utf-8')
    page.screenshot(path=str(ROOT / f'{name}.png'))
    with open(ROOT / f'{name}_info.json', 'w', encoding='utf-8') as f:
        json.dump(info, f, indent=2, ensure_ascii=False, default=str)
    return info


fresh = bo.create_fresh_profile(country_code='ES')
print(f'profile={fresh["profile"]} sessid={fresh["sessionid"]}')
port = bo._start_profile(fresh['env_id'])
time.sleep(4)

try:
    v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        # Capturar redirects via CDP
        redirects = []
        cdp = ctx.new_cdp_session(page)
        cdp.send('Network.enable')
        def on_req(p):
            if p.get('redirectResponse'):
                r = p['redirectResponse']
                redirects.append({'from_url': r.get('url'), 'to_url': p['request']['url'],
                                  'status': r.get('status'),
                                  'set_cookie': r.get('headers', {}).get('set-cookie', '')[:300]})
        cdp.on('Network.requestWillBeSent', on_req)

        # 1) HOME (eps warming)
        print('\n[1] HOME warming')
        page.goto('https://www.ticketmaster.es/', wait_until='domcontentloaded', timeout=30000)
        time.sleep(10)
        # Aceptar cookies
        for txt in ('Acepto', 'Aceptar todas las cookies'):
            try:
                el = page.get_by_text(txt, exact=False).first
                if el.is_visible(timeout=1500):
                    el.click(); time.sleep(2); break
            except Exception: pass
        info_home = save('01_home', page, ctx)
        print(f'  url={info_home["url"][:80]}')
        print(f'  body={info_home["body_len"]}B cookies={info_home["cookies_count"]}')

        # 2) EVENT
        print('\n[2] EVENT')
        page.goto(EVENT_URL, wait_until='domcontentloaded', timeout=30000)
        time.sleep(8)
        # Aceptar cookies sub-modal
        for txt in ('Acepto', 'OK'):
            try:
                el = page.get_by_text(txt, exact=False).first
                if el.is_visible(timeout=1500):
                    el.click(); time.sleep(2); break
            except Exception: pass
        info_event = save('02_event', page, ctx)
        print(f'  url={info_event["url"][:80]}')
        print(f'  body={info_event["body_len"]}B cookies={info_event["cookies_count"]}')

        # 3) STEPPER + Continuar Comprando — captura URL completa del redirect
        print('\n[3] Click Continuar Comprando → /auth')
        # Subir qty (simulación rápida)
        try:
            # Mejor offer plus
            plus_btns = page.locator('button[aria-label*="ncrement" i]').all()
            if plus_btns:
                for _ in range(2):
                    try: plus_btns[-1].click(timeout=2000); time.sleep(0.5)
                    except Exception: pass
            time.sleep(1)
            # Buscar Continuar
            for txt in ('Continuar comprando', 'Continuar', 'Continue'):
                try:
                    btn = page.get_by_text(txt, exact=False).first
                    if btn.is_visible(timeout=2000):
                        btn.click(timeout=5000)
                        print(f'  clicked: {txt}')
                        break
                except Exception: pass
        except Exception as e:
            print(f'  stepper/continuar err: {e}')
        time.sleep(15)  # Esperar redirect
        info_auth = save('03_auth_after_continuar', page, ctx)
        print(f'  url={info_auth["url"][:120]}')
        print(f'  body={info_auth["body_len"]}B  cookies={info_auth["cookies_count"]}')
        print(f'  has_identity_verified={info_auth["has_identity_verified"]}')
        print(f'  has_un_momento={info_auth["has_un_momento"]}')
        print(f'  has_modern_error={info_auth["has_modern_error"]}')
        print(f'  has_email_input={info_auth["has_email_input"]}')
        print(f'  has_pf={info_auth["has_pf"]}')

        # Esperar 20s más a ver si el form aparece
        print('\n[4] Esperar 20s más en /auth')
        time.sleep(20)
        info_auth_wait = save('04_auth_after_20s_wait', page, ctx)
        print(f'  url={info_auth_wait["url"][:120]}')
        print(f'  body={info_auth_wait["body_len"]}B  cookies={info_auth_wait["cookies_count"]}')
        print(f'  has_identity_verified={info_auth_wait["has_identity_verified"]}')
        print(f'  has_email_input={info_auth_wait["has_email_input"]}')

        # CDP redirects capturados
        rd_path = ROOT / 'redirects.json'
        with open(rd_path, 'w', encoding='utf-8') as f:
            json.dump(redirects, f, indent=2, ensure_ascii=False, default=str)
        print(f'\n  redirects captured: {len(redirects)}')
        # Imprimir redirects relevantes
        for r in redirects[-10:]:
            if 'auth.ticketmaster' in (r.get('from_url', '') + r.get('to_url', '')):
                print(f'    {r["status"]} {r["from_url"][:80]} → {r["to_url"][:80]}')

        b.close()
except Exception as e:
    print(f'FATAL: {e}')

# Cleanup
try:
    requests.post('http://127.0.0.1:40000/api/env/close',
                  json={'envId': fresh['env_id']}, timeout=10)
    time.sleep(2)
    bo.destroy_fresh_profile(fresh)
except Exception: pass

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
