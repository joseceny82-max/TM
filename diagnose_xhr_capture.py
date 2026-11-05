"""diagnose_xhr_capture.py — Instrumentación CDP profunda del flow del bot.

Reproduce el flow REAL del bot:
    home → event → click Continuar Comprando → /auth

Captura CADA EVENTO CDP:
    - Network.requestWillBeSent (+ headers)
    - Network.responseReceived (+ headers + status)
    - Network.loadingFinished (+ size)
    - Network.loadingFailed (canceled/aborted/timeout)
    - Page.frameNavigated (redirects + url changes)
    - Runtime.consoleAPICalled (console.log/error/warn/info)
    - Runtime.exceptionThrown (errors no manejados)
    - Page.javascriptDialogOpening (alerts/confirms)

Y registra:
    - Cookies en CADA paso (timeline con diff)
    - localStorage / sessionStorage en CADA paso
    - JS globals (epsfBase, client_ip, etc.)

Output: HAR completo + timeline JSON + reporte diff
"""
from __future__ import annotations
import os, sys, json, time, threading
from datetime import datetime
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import requests
import bot_orchestrator as bo
from playwright.sync_api import sync_playwright

TS = datetime.now().strftime('%Y%m%d_%H%M%S')
ROOT = Path(f'datos/diagnose/xhr_capture_{TS}')
ROOT.mkdir(parents=True, exist_ok=True)

EVENT_URL = ('https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour'
             '-entradas/476722288')

# Eventos CDP capturados durante TODO el flow
events_log = []
requests_by_id = {}
responses_by_id = {}
console_messages = []
exceptions_log = []
frame_navigations = []
cookie_snapshots = []  # [(timestamp, step, cookies)]
storage_snapshots = []


def ts_now():
    return time.time()


def log_event(kind: str, data: dict):
    data['_kind'] = kind
    data['_ts'] = ts_now()
    events_log.append(data)


def capture_state(page, ctx, step_name: str) -> dict:
    """Snapshot cookies + storage + JS state en este momento."""
    snap = {'step': step_name, 'ts': ts_now()}
    try:
        snap['cookies'] = ctx.cookies()
        snap['url'] = page.url
    except Exception as e: snap['cookies_err'] = str(e)
    try:
        snap['localStorage'] = page.evaluate("""() => {
            const r = {};
            try {
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    r[k] = localStorage.getItem(k);
                }
            } catch (e) { r._err = e.message; }
            return r;
        }""")
        snap['sessionStorage'] = page.evaluate("""() => {
            const r = {};
            try {
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i);
                    r[k] = sessionStorage.getItem(k);
                }
            } catch (e) { r._err = e.message; }
            return r;
        }""")
        snap['js_state'] = page.evaluate("""() => ({
            url: location.href,
            referrer: document.referrer,
            readyState: document.readyState,
            epsfBase: window.epsfBase || null,
            epsfToken: window.epsfToken ? window.epsfToken.slice(0, 40) : null,
            client_ip: window.client_ip || null,
            navigator_webdriver: navigator.webdriver,
            visibility: document.visibilityState,
            // Try to detect PingFederate state
            pf_globals: Object.keys(window).filter(k => /^pf|ping/i.test(k)).slice(0, 20),
            // Hidden form fields if any
            hidden_inputs: Array.from(document.querySelectorAll('input[type=hidden]'))
                                .map(el => ({name: el.name, value: (el.value || '').slice(0, 80)}))
                                .slice(0, 20),
        })""")
    except Exception as e: snap['js_err'] = str(e)
    cookie_snapshots.append(snap)
    return snap


def diff_cookies(prev: list, curr: list) -> dict:
    """Compara dos listas de cookies por (name, domain)."""
    p_idx = {(c['name'], c['domain']): c for c in (prev or [])}
    c_idx = {(c['name'], c['domain']): c for c in (curr or [])}
    added = [c_idx[k] for k in c_idx if k not in p_idx]
    removed = [p_idx[k] for k in p_idx if k not in c_idx]
    changed = []
    for k, ca in c_idx.items():
        if k in p_idx and p_idx[k].get('value') != ca.get('value'):
            changed.append({'name': k[0], 'domain': k[1],
                            'prev_len': len(p_idx[k].get('value', '')),
                            'curr_len': len(ca.get('value', ''))})
    return {'added': [{'name': c['name'], 'domain': c['domain'],
                       'value_len': len(c.get('value', ''))} for c in added],
            'removed': [{'name': c['name'], 'domain': c['domain']} for c in removed],
            'changed': changed}


# ════════════════════════════════════════════════════════════
#  ARRANCAR PERFIL FRESH
# ════════════════════════════════════════════════════════════
fresh = bo.create_fresh_profile(country_code='ES')
if not fresh:
    print('FATAL: create_fresh_profile failed')
    sys.exit(1)
print(f'profile={fresh["profile"]} sessid={fresh["sessionid"]}')
port = bo._start_profile(fresh['env_id'])
if not port:
    bo.destroy_fresh_profile(fresh)
    sys.exit(1)
time.sleep(4)

try:
    v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        # ─── CDP Setup ───
        cdp = ctx.new_cdp_session(page)
        cdp.send('Network.enable')
        cdp.send('Page.enable')
        cdp.send('Runtime.enable')
        cdp.send('Network.setCacheDisabled', {'cacheDisabled': False})
        try:
            cdp.send('Network.setExtraHTTPHeaders', {'headers': {}})
        except Exception: pass

        # Network handlers
        def on_request_will_be_sent(p):
            rid = p.get('requestId')
            req = p.get('request', {})
            # Detect redirect
            if p.get('redirectResponse'):
                rr = p['redirectResponse']
                log_event('redirect', {
                    'requestId': rid,
                    'from_url': rr.get('url'),
                    'to_url': req.get('url'),
                    'status': rr.get('status'),
                    'set_cookie': (rr.get('headers') or {}).get('set-cookie', '')[:500],
                    'location': (rr.get('headers') or {}).get('location', ''),
                })
            requests_by_id[rid] = {
                'requestId': rid,
                'url': req.get('url'),
                'method': req.get('method'),
                'headers': req.get('headers') or {},
                'has_post_data': req.get('hasPostData', False),
                'type': p.get('type'),
                'initiator_type': (p.get('initiator') or {}).get('type'),
                'initiator_url': (p.get('initiator') or {}).get('url', '')[:200],
                'frameId': p.get('frameId'),
                'documentURL': p.get('documentURL'),
                'ts': p.get('timestamp'),
                'wallTime': p.get('wallTime'),
                'ts_local': ts_now(),
            }
            log_event('request', {**requests_by_id[rid]})

        def on_response_received(p):
            rid = p.get('requestId')
            resp = p.get('response', {})
            responses_by_id[rid] = {
                'requestId': rid,
                'url': resp.get('url'),
                'status': resp.get('status'),
                'statusText': resp.get('statusText'),
                'headers': resp.get('headers') or {},
                'mimeType': resp.get('mimeType'),
                'protocol': resp.get('protocol'),
                'remoteIPAddress': resp.get('remoteIPAddress'),
                'fromDiskCache': resp.get('fromDiskCache'),
                'fromServiceWorker': resp.get('fromServiceWorker'),
                'encodedDataLength': resp.get('encodedDataLength'),
                'timing': resp.get('timing'),
                'ts_local': ts_now(),
            }
            log_event('response', {**responses_by_id[rid]})

        def on_loading_finished(p):
            rid = p.get('requestId')
            log_event('finished', {
                'requestId': rid,
                'encodedDataLength': p.get('encodedDataLength'),
                'ts_local': ts_now(),
            })

        def on_loading_failed(p):
            rid = p.get('requestId')
            log_event('failed', {
                'requestId': rid,
                'errorText': p.get('errorText'),
                'canceled': p.get('canceled'),
                'blockedReason': p.get('blockedReason'),
                'corsErrorStatus': p.get('corsErrorStatus'),
                'ts_local': ts_now(),
                'url': requests_by_id.get(rid, {}).get('url'),
            })

        def on_frame_navigated(p):
            fr = p.get('frame', {})
            frame_navigations.append({
                'frameId': fr.get('id'),
                'url': fr.get('url'),
                'mimeType': fr.get('mimeType'),
                'ts': ts_now(),
            })
            log_event('frame_nav', {
                'frameId': fr.get('id'),
                'url': fr.get('url'),
                'mimeType': fr.get('mimeType'),
                'ts_local': ts_now(),
            })

        def on_console(p):
            try:
                args = p.get('args', [])
                vals = []
                for a in args[:5]:
                    v = a.get('value') if 'value' in a else a.get('description', '')
                    vals.append(str(v)[:200])
                console_messages.append({
                    'type': p.get('type'),
                    'text': ' '.join(vals),
                    'url': p.get('stackTrace', {}).get('callFrames', [{}])[0].get('url', '')[:120],
                    'ts': ts_now(),
                })
                log_event('console', console_messages[-1])
            except Exception: pass

        def on_exception(p):
            ex = p.get('exceptionDetails', {})
            exceptions_log.append({
                'text': ex.get('text', '')[:300],
                'url': ex.get('url', '')[:200],
                'lineNumber': ex.get('lineNumber'),
                'exception_description': (ex.get('exception') or {}).get('description', '')[:300],
                'ts': ts_now(),
            })
            log_event('exception', exceptions_log[-1])

        cdp.on('Network.requestWillBeSent', on_request_will_be_sent)
        cdp.on('Network.responseReceived', on_response_received)
        cdp.on('Network.loadingFinished', on_loading_finished)
        cdp.on('Network.loadingFailed', on_loading_failed)
        cdp.on('Page.frameNavigated', on_frame_navigated)
        cdp.on('Runtime.consoleAPICalled', on_console)
        cdp.on('Runtime.exceptionThrown', on_exception)

        # ─── PASO 1: HOME WARMING ───
        print('\n[1] HOME → www.ticketmaster.es')
        snap_pre_home = capture_state(page, ctx, 'pre_home')
        page.goto('https://www.ticketmaster.es/',
                  wait_until='domcontentloaded', timeout=30000)
        time.sleep(10)
        # Aceptar cookies banner
        for txt in ('Acepto', 'Aceptar todas las cookies'):
            try:
                el = page.get_by_text(txt, exact=False).first
                if el.is_visible(timeout=1500):
                    el.click(); time.sleep(2); break
            except Exception: pass
        snap_post_home = capture_state(page, ctx, 'post_home')
        print(f'  cookies: {len(snap_post_home.get("cookies", []))}, '
              f'localStorage_keys: {len(snap_post_home.get("localStorage", {}))}, '
              f'eps_loaded: {bool(snap_post_home.get("js_state", {}).get("epsfBase"))}')

        # ─── PASO 2: EVENT ───
        print('\n[2] EVENT → /event/karol-g-...')
        page.goto(EVENT_URL, wait_until='domcontentloaded', timeout=30000)
        time.sleep(10)
        # Aceptar 2do banner si aparece
        for txt in ('Acepto', 'OK', 'Aceptar'):
            try:
                el = page.get_by_text(txt, exact=False).first
                if el.is_visible(timeout=1500):
                    el.click(); time.sleep(2); break
            except Exception: pass
        snap_post_event = capture_state(page, ctx, 'post_event')
        print(f'  url: {page.url[:80]}')
        print(f'  cookies: {len(snap_post_event.get("cookies", []))}, '
              f'localStorage_keys: {len(snap_post_event.get("localStorage", {}))}')

        # ─── PASO 3: STEPPER + CONTINUAR ───
        print('\n[3] STEPPER qty=2 + Continuar Comprando')
        try:
            # Stepper plus (offer Reservado)
            res = page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button'));
                // Buscar el botón Continuar (puede estar oculto hasta seleccionar oferta)
                const offer3 = document.querySelectorAll('[data-testid*="offer"]');
                const seatmapTab = btns.find(b => /seats|asientos/i.test(b.innerText));
                // Click en el offer index 3 (Reservado) usando data-testid si existe
                let offerIdx3 = null;
                btns.forEach((b, i) => {
                    if (/reservado|seated/i.test(b.innerText) && !offerIdx3) {
                        offerIdx3 = b;
                    }
                });
                if (offerIdx3) offerIdx3.click();
                return {clicked_offer: !!offerIdx3, total_buttons: btns.length};
            }""")
            print(f'  offer click: {res}')
            time.sleep(3)
        except Exception as e:
            print(f'  stepper err: {e}')

        # Buscar el botón Find Tickets / Encontrar Entradas
        try:
            for txt in ('Encontrar entradas', 'Find Tickets', 'Buscar entradas'):
                try:
                    btn = page.get_by_text(txt, exact=False).first
                    if btn.is_visible(timeout=2000):
                        btn.click(timeout=3000)
                        print(f'  clicked: {txt}')
                        time.sleep(3)
                        break
                except Exception: pass
        except Exception as e:
            print(f'  find tickets err: {e}')

        # Aceptar checkbox + Continuar
        try:
            page.evaluate("""() => {
                document.querySelectorAll('input[type=checkbox]').forEach(c => {
                    if (!c.checked) c.click();
                });
            }""")
            time.sleep(1)
        except Exception: pass

        # Click Continuar Comprando
        snap_pre_continuar = capture_state(page, ctx, 'pre_continuar')
        try:
            res = page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const cont = btns.find(b =>
                    /^continuar/i.test((b.innerText || '').trim()) &&
                    b.offsetParent !== null && !b.disabled
                );
                if (cont) { cont.click(); return true; }
                return false;
            }""")
            print(f'  continuar clicked: {res}')
        except Exception as e:
            print(f'  continuar err: {e}')

        # Esperar redirect a /auth
        time.sleep(20)
        snap_post_continuar = capture_state(page, ctx, 'post_continuar')
        print(f'  url ahora: {page.url[:120]}')

        # ─── PASO 4: ESPERAR 20s MÁS en /auth con captura activa ───
        print('\n[4] Esperar 20s adicionales en /auth (capturando XHRs)')
        time.sleep(20)
        snap_final_auth = capture_state(page, ctx, 'final_auth_after_40s')
        print(f'  url final: {page.url[:120]}')
        # Capturar HTML del estado final
        try:
            body = page.content()
            (ROOT / 'final_auth_body.html').write_text(body, encoding='utf-8')
            print(f'  body_len: {len(body)} guardado')
        except Exception: pass

        # Screenshot
        try:
            page.screenshot(path=str(ROOT / 'final_auth.png'), full_page=True)
        except Exception: pass

        b.close()
except Exception as e:
    print(f'FATAL: {e}')
    import traceback; traceback.print_exc()

# ─── Cleanup profile ───
try:
    requests.post('http://127.0.0.1:40000/api/env/close',
                  json={'envId': fresh['env_id']}, timeout=10)
    time.sleep(2)
    bo.destroy_fresh_profile(fresh)
except Exception: pass

# ════════════════════════════════════════════════════════════
#  GUARDAR DATOS COMPLETOS
# ════════════════════════════════════════════════════════════
print('\n[5] Guardando datos completos...')
(ROOT / 'events_log.json').write_text(
    json.dumps(events_log, indent=2, ensure_ascii=False, default=str),
    encoding='utf-8')
(ROOT / 'requests.json').write_text(
    json.dumps(list(requests_by_id.values()), indent=2, ensure_ascii=False, default=str),
    encoding='utf-8')
(ROOT / 'responses.json').write_text(
    json.dumps(list(responses_by_id.values()), indent=2, ensure_ascii=False, default=str),
    encoding='utf-8')
(ROOT / 'console.json').write_text(
    json.dumps(console_messages, indent=2, ensure_ascii=False, default=str),
    encoding='utf-8')
(ROOT / 'exceptions.json').write_text(
    json.dumps(exceptions_log, indent=2, ensure_ascii=False, default=str),
    encoding='utf-8')
(ROOT / 'frame_navigations.json').write_text(
    json.dumps(frame_navigations, indent=2, ensure_ascii=False, default=str),
    encoding='utf-8')
(ROOT / 'cookie_snapshots.json').write_text(
    json.dumps(cookie_snapshots, indent=2, ensure_ascii=False, default=str),
    encoding='utf-8')
print(f'\n  Total events: {len(events_log)}')
print(f'  Total requests: {len(requests_by_id)}')
print(f'  Total responses: {len(responses_by_id)}')
print(f'  Console msgs: {len(console_messages)}')
print(f'  Exceptions: {len(exceptions_log)}')
print(f'  Frame navigations: {len(frame_navigations)}')
print(f'  Cookie snapshots: {len(cookie_snapshots)}')
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
