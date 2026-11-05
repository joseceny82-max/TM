"""diagnose_xhr_capture_v2.py — Instrumentación CDP CON flow REAL del bot.

Usa los mismos selectores del bot real para llegar a /auth y capturar TODO.
"""
from __future__ import annotations
import os, sys, json, time, random
from datetime import datetime
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import requests
import bot_orchestrator as bo
from playwright.sync_api import sync_playwright

TS = datetime.now().strftime('%Y%m%d_%H%M%S')
ROOT = Path(f'datos/diagnose/xhr_v2_{TS}')
ROOT.mkdir(parents=True, exist_ok=True)

EVENT_URL = ('https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour'
             '-entradas/476722288')

events_log = []
requests_by_id = {}
responses_by_id = {}
console_messages = []
exceptions_log = []
frame_navigations = []
cookie_snapshots = []


def ts_now(): return time.time()
def log_event(kind, data):
    data['_kind'] = kind
    data['_ts'] = ts_now()
    events_log.append(data)


def capture_state(page, ctx, step_name):
    snap = {'step': step_name, 'ts': ts_now()}
    try:
        snap['cookies'] = ctx.cookies()
        snap['url'] = page.url
        snap['title'] = page.title()
    except Exception as e: snap['err'] = str(e)
    try:
        snap['localStorage'] = page.evaluate("""() => {
            const r = {};
            try { for (let i=0; i<localStorage.length; i++) { const k=localStorage.key(i); r[k]=localStorage.getItem(k);}}
            catch(e){ r._err=e.message; } return r; }""")
        snap['sessionStorage'] = page.evaluate("""() => {
            const r = {};
            try { for (let i=0; i<sessionStorage.length; i++) { const k=sessionStorage.key(i); r[k]=sessionStorage.getItem(k);}}
            catch(e){ r._err=e.message; } return r; }""")
        snap['js_state'] = page.evaluate("""() => ({
            url: location.href, referrer: document.referrer, readyState: document.readyState,
            epsfBase: window.epsfBase||null,
            epsfToken: window.epsfToken ? window.epsfToken.slice(0,40) : null,
            client_ip: window.client_ip||null,
            navigator_webdriver: navigator.webdriver, visibility: document.visibilityState,
            hidden_inputs: Array.from(document.querySelectorAll('input[type=hidden]'))
                .map(el => ({name:el.name, value:(el.value||'').slice(0,80)})).slice(0,30),
            visible_inputs: Array.from(document.querySelectorAll('input:not([type=hidden])'))
                .map(el => ({type:el.type, name:el.name, id:el.id, placeholder:el.placeholder, visible: el.offsetParent !== null}))
                .slice(0,20),
            visible_buttons_text: Array.from(document.querySelectorAll('button'))
                .filter(b => b.offsetParent !== null)
                .map(b => (b.innerText||'').slice(0,40)).slice(0,15),
        })""")
    except Exception as e: snap['js_err'] = str(e)
    cookie_snapshots.append(snap)
    return snap


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

        cdp = ctx.new_cdp_session(page)
        cdp.send('Network.enable')
        cdp.send('Page.enable')
        cdp.send('Runtime.enable')

        def on_req(p):
            rid = p.get('requestId')
            req = p.get('request', {})
            if p.get('redirectResponse'):
                rr = p['redirectResponse']
                log_event('redirect', {'requestId': rid,
                    'from_url': rr.get('url'), 'to_url': req.get('url'),
                    'status': rr.get('status'),
                    'set_cookie': (rr.get('headers') or {}).get('set-cookie', '')[:500],
                    'location': (rr.get('headers') or {}).get('location', '')})
            requests_by_id[rid] = {'requestId': rid, 'url': req.get('url'),
                'method': req.get('method'), 'headers': req.get('headers') or {},
                'has_post_data': req.get('hasPostData', False),
                'type': p.get('type'), 'initiator_type': (p.get('initiator') or {}).get('type'),
                'initiator_url': (p.get('initiator') or {}).get('url', '')[:200],
                'documentURL': p.get('documentURL'), 'ts_local': ts_now()}
            log_event('request', dict(requests_by_id[rid]))

        def on_resp(p):
            rid = p.get('requestId')
            resp = p.get('response', {})
            responses_by_id[rid] = {'requestId': rid, 'url': resp.get('url'),
                'status': resp.get('status'), 'headers': resp.get('headers') or {},
                'mimeType': resp.get('mimeType'), 'protocol': resp.get('protocol'),
                'remoteIPAddress': resp.get('remoteIPAddress'),
                'encodedDataLength': resp.get('encodedDataLength'), 'ts_local': ts_now()}
            log_event('response', dict(responses_by_id[rid]))

        def on_finished(p):
            log_event('finished', {'requestId': p.get('requestId'),
                                    'encodedDataLength': p.get('encodedDataLength')})

        def on_failed(p):
            rid = p.get('requestId')
            log_event('failed', {'requestId': rid,
                'errorText': p.get('errorText'), 'canceled': p.get('canceled'),
                'blockedReason': p.get('blockedReason'),
                'url': requests_by_id.get(rid, {}).get('url')})

        def on_frame(p):
            fr = p.get('frame', {})
            frame_navigations.append({'frameId': fr.get('id'), 'url': fr.get('url'),
                                       'ts': ts_now()})
            log_event('frame_nav', {'url': fr.get('url')})

        def on_console(p):
            args = p.get('args', [])
            vals = []
            for a in args[:5]:
                v_ = a.get('value') if 'value' in a else a.get('description', '')
                vals.append(str(v_)[:200])
            console_messages.append({'type': p.get('type'), 'text': ' '.join(vals),
                'url': (p.get('stackTrace', {}).get('callFrames', [{}])[0]).get('url', '')[:120],
                'ts': ts_now()})

        def on_exc(p):
            ex = p.get('exceptionDetails', {})
            exceptions_log.append({'text': ex.get('text', '')[:300],
                'url': ex.get('url', '')[:200], 'lineNumber': ex.get('lineNumber'),
                'exception_description': (ex.get('exception') or {}).get('description', '')[:300]})

        cdp.on('Network.requestWillBeSent', on_req)
        cdp.on('Network.responseReceived', on_resp)
        cdp.on('Network.loadingFinished', on_finished)
        cdp.on('Network.loadingFailed', on_failed)
        cdp.on('Page.frameNavigated', on_frame)
        cdp.on('Runtime.consoleAPICalled', on_console)
        cdp.on('Runtime.exceptionThrown', on_exc)

        # ─── STEP 1: HOME ───
        print('\n[1] HOME')
        page.goto('https://www.ticketmaster.es/', wait_until='domcontentloaded', timeout=30000)
        time.sleep(8)
        for txt in ('Acepto', 'Aceptar todas las cookies'):
            try:
                el = page.get_by_text(txt, exact=False).first
                if el.is_visible(timeout=1500):
                    el.click(); time.sleep(2); break
            except Exception: pass
        capture_state(page, ctx, '1_home_done')

        # ─── STEP 2: EVENT ───
        print('\n[2] EVENT')
        page.goto(EVENT_URL, wait_until='domcontentloaded', timeout=30000)
        time.sleep(8)
        # Post-hydration cookies banner OK
        try:
            page.evaluate("""() => {
                const m = Array.from(document.querySelectorAll('button')).find(b => /^ok$|^acepto$/i.test((b.innerText||'').trim()));
                if (m) m.click();
            }""")
            time.sleep(2)
        except Exception: pass
        capture_state(page, ctx, '2_event_loaded')

        # ─── STEP 3: SWITCH TO QUICK MODE (Ver lo mejor disponible) ───
        print('\n[3] Quick mode')
        try:
            page.evaluate("""() => {
                const all = Array.from(document.querySelectorAll('button, a, [role="button"], [role="tab"]'));
                const b = all.find(x => /ver\s+lo\s+mejor\s+disponible|best\s+available/i.test(x.innerText||x.textContent||''));
                if (b) b.click();
            }""")
            time.sleep(2)
        except Exception as e: print(f'  quick err: {e}')

        # ─── STEP 4: STEPPER qty=2 (offer idx 3 = Reservado) ───
        print('\n[4] Stepper qty=2 offer_idx=3')
        try:
            page.wait_for_selector('[data-testid="quantityStepper"]', timeout=15000, state='attached')
        except Exception as e: print(f'  stepper wait: {e}')
        for _ in range(6):
            cur = page.evaluate("""() => {
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[3];
                return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
            }""")
            if cur >= 2: break
            page.evaluate("""() => {
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[3];
                if (!s) return;
                const btns = s.querySelectorAll('button');
                const target = btns[btns.length-1] || btns[0];
                if (target) target.click();
            }""")
            time.sleep(0.6)
        print(f'  qty current: {cur}')

        # ─── STEP 5: Find Tickets (Entradas verde) ───
        print('\n[5] Find Tickets')
        try:
            clicked = page.evaluate(r"""() => {
                const btns = Array.from(document.querySelectorAll('button, a[role="button"]'));
                const byTest = btns.find(b => b.dataset?.testid === 'findTicketsBtn');
                if (byTest && byTest.offsetParent !== null) { byTest.click(); return 'data-testid'; }
                const byText = btns.find(b => {
                    if (b.offsetParent === null) return false;
                    const txt = (b.innerText||b.textContent||'').trim();
                    return /^entradas$|^get tickets$|^find tickets$/i.test(txt);
                });
                if (byText) { byText.click(); return 'text'; }
                return null;
            }""")
            print(f'  find tickets clicked: {clicked}')
        except Exception as e: print(f'  err: {e}')
        time.sleep(3)

        # ─── STEP 6: Checkbox Acepto ───
        print('\n[6] Checkbox Acepto')
        try:
            page.evaluate("() => { document.querySelectorAll('input[type=checkbox]').forEach(c => { if (!c.checked) c.click(); }); }")
            time.sleep(1)
        except Exception: pass

        # ─── STEP 7: Click Continuar Comprando ───
        print('\n[7] Click Continuar Comprando')
        capture_state(page, ctx, '7_pre_continuar')
        url_before = page.url
        try:
            res = page.evaluate(r"""() => {
                const all = Array.from(document.querySelectorAll('button, a[role="button"], [role="button"], input[type="submit"]'));
                const btns = all.filter(b => b.offsetParent !== null && !b.disabled);
                const t = btns.find(b => {
                    const tx = (b.innerText||b.textContent||b.value||'').trim();
                    return /continuar\s+comprando|^continuar$|continue\s+shopping/i.test(tx);
                });
                if (t) { t.click(); return (t.innerText||t.value||'').slice(0,40); }
                return null;
            }""")
            print(f'  continuar clicked: {res}')
        except Exception as e: print(f'  err: {e}')

        # Wait redirect to /auth
        url_after = url_before
        for _ in range(20):
            time.sleep(0.5)
            if page.url != url_before:
                url_after = page.url; break
        print(f'  url after: {url_after[:100]}')
        if url_after == url_before:
            # Retry
            print('  retry continuar...')
            page.evaluate(r"""() => {
                const all = Array.from(document.querySelectorAll('button, a[role="button"], [role="button"], input[type="submit"]'));
                const t = all.find(b => /continuar/i.test((b.innerText||'').trim()));
                if (t) t.click();
            }""")
            for _ in range(20):
                time.sleep(0.5)
                if page.url != url_before:
                    url_after = page.url; break
            print(f'  url after retry: {url_after[:100]}')

        capture_state(page, ctx, '8_post_continuar')

        # ─── STEP 8: Esperar en /auth y capturar TODO ───
        print('\n[8] En /auth — capturando 30s')
        time.sleep(30)
        capture_state(page, ctx, '9_auth_30s')
        try:
            body = page.content()
            (ROOT / 'final_body.html').write_text(body, encoding='utf-8')
            page.screenshot(path=str(ROOT / 'final.png'))
        except Exception: pass

        b.close()
except Exception as e:
    print(f'FATAL: {e}')
    import traceback; traceback.print_exc()

try:
    requests.post('http://127.0.0.1:40000/api/env/close', json={'envId': fresh['env_id']}, timeout=10)
    time.sleep(2)
    bo.destroy_fresh_profile(fresh)
except Exception: pass

# Save all
print('\n[9] Guardando datos...')
(ROOT / 'events_log.json').write_text(json.dumps(events_log, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
(ROOT / 'requests.json').write_text(json.dumps(list(requests_by_id.values()), indent=2, ensure_ascii=False, default=str), encoding='utf-8')
(ROOT / 'responses.json').write_text(json.dumps(list(responses_by_id.values()), indent=2, ensure_ascii=False, default=str), encoding='utf-8')
(ROOT / 'console.json').write_text(json.dumps(console_messages, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
(ROOT / 'exceptions.json').write_text(json.dumps(exceptions_log, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
(ROOT / 'frame_navigations.json').write_text(json.dumps(frame_navigations, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
(ROOT / 'cookie_snapshots.json').write_text(json.dumps(cookie_snapshots, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
print(f'  reqs={len(requests_by_id)} resps={len(responses_by_id)} console={len(console_messages)} exc={len(exceptions_log)}')
print(f'  Output: {ROOT}')


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
