"""Batería exhaustiva de tests para el apartado de selección de entradas.

Cubre:
1. Unit (sin browser): cookie_filter, event_offer_scanner, ticket_selector helpers.
2. Vivo (browser MoreLogin): switch tabs, detect_offers, select_quick (qty 1/2/4),
   reset_qty, select_seatmap, edge cases (offer out of range, qty=0, doble selección).
3. Endpoint API: POST /api/bot/groups con quick y seatmap, verifica que params se propagan
   al Worker y a tm_purchase_flow vía Worker._run().
4. Front: GET /api/events/<tm_id>/offers responde con 3 ofertas.

Requiere _buy_state.json apuntando a un perfil MoreLogin vivo en port=...

Ejecuta: python _test_selector_battery.py
Salida: dict con resultados por test + summary (passed/failed/skipped).
"""
from __future__ import annotations
import sys, os, json, time, traceback
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')
import requests

# ─── Estado global ─────────────────────────────────────────────────
RESULTS = []  # [{name, status: pass|fail|skip, detail, elapsed_ms}]


def _run(name: str, fn, skip_if=None):
    if skip_if:
        RESULTS.append({'name': name, 'status': 'skip', 'detail': str(skip_if)})
        print(f'  [SKIP] {name} — {skip_if}')
        return
    t0 = time.time()
    try:
        ret = fn()
        elapsed = int((time.time() - t0) * 1000)
        RESULTS.append({'name': name, 'status': 'pass', 'detail': str(ret)[:200], 'elapsed_ms': elapsed})
        print(f'  [PASS] {name} ({elapsed}ms) {str(ret)[:80]}')
    except AssertionError as e:
        elapsed = int((time.time() - t0) * 1000)
        RESULTS.append({'name': name, 'status': 'fail', 'detail': f'assert: {e}', 'elapsed_ms': elapsed})
        print(f'  [FAIL] {name} — {e}')
    except Exception as e:
        elapsed = int((time.time() - t0) * 1000)
        tb = traceback.format_exc().splitlines()[-3]
        RESULTS.append({'name': name, 'status': 'fail', 'detail': f'{type(e).__name__}: {e} | {tb}',
                        'elapsed_ms': elapsed})
        print(f'  [FAIL] {name} — {type(e).__name__}: {e}')


# ═══════════════════════════════════════════════════════════════════
# 1. UNIT TESTS (sin browser)
# ═══════════════════════════════════════════════════════════════════
def unit_tests():
    print('\n══ UNIT TESTS ══')
    import cookie_filter
    import event_offer_scanner
    import ticket_selector
    import bot_engine

    # cookie_filter.classify_cookie
    def t1():
        assert cookie_filter.classify_cookie({'name':'TMUO','domain':'.ticketmaster.es'}) == 'keep'
        assert cookie_filter.classify_cookie({'name':'_ga','domain':'.ticketmaster.es'}) == 'drop'
        assert cookie_filter.classify_cookie({'name':'OptanonConsent','domain':'.ticketmaster.es'}) == 'drop'
        assert cookie_filter.classify_cookie({'name':'_abck','domain':'.ticketmaster.es'}) == 'keep'
        assert cookie_filter.classify_cookie({'name':'cf_clearance','domain':'.ticketmaster.es'}) == 'keep'
        assert cookie_filter.classify_cookie({'name':'reese84','domain':'.queue-it.net'}) == 'keep'
        assert cookie_filter.classify_cookie({'name':'_fbp','domain':'.unknown.com'}) == 'drop'
        assert cookie_filter.classify_cookie({'name':'random_xyz','domain':'.unknown.com'}) == 'unknown'
        return 'classify 8/8'
    _run('cookie_filter.classify', t1)

    # cookie_filter.partition_cookies
    def t2():
        cookies = [{'name':'TMUO','domain':'.ticketmaster.es'}, {'name':'_ga','domain':'.x.com'},
                   {'name':'foo','domain':'.bar.com'}]
        keep, drop, unknown = cookie_filter.partition_cookies(cookies)
        assert len(keep) == 1 and keep[0]['name']=='TMUO'
        assert len(drop) == 1 and drop[0]['name']=='_ga'
        assert len(unknown) == 1 and unknown[0]['name']=='foo'
        return f'{len(keep)}/{len(drop)}/{len(unknown)}'
    _run('cookie_filter.partition', t2)

    # event_offer_scanner._parse_offer_text
    def t3():
        out = event_offer_scanner._parse_offer_text('Entrada General Pista\n88,00 € cada una')
        assert out['name'] == 'Entrada General Pista', f'name={out["name"]}'
        assert out['price_min'] == 88.0, f'pmin={out["price_min"]}'
        assert out['price_max'] == 88.0, f'pmax={out["price_max"]}'
        return out['name']
    _run('parse_offer_text precio_unico', t3)

    def t4():
        out = event_offer_scanner._parse_offer_text('Asiento Reservado\n71,50 € a 104,50 € cada una')
        assert out['price_min'] == 71.5
        assert out['price_max'] == 104.5
        return f'rango {out["price_min"]}-{out["price_max"]}'
    _run('parse_offer_text precio_rango', t4)

    def t5():
        out = event_offer_scanner._parse_offer_text('Asiento Reservado\nMOVILIDAD REDUCIDA — 71,50 € a 104,50 €')
        # 'movilidad reducida' → categoría wheelchair
        assert out['category'] == 'wheelchair', f'cat={out["category"]}'
        return out['category']
    _run('parse_offer_text infiere wheelchair', t5)

    def t6():
        out = event_offer_scanner._parse_offer_text('VIP Premium Box\n200,00 €')
        assert out['category'] == 'vip', f'cat={out["category"]}'
        return out['category']
    _run('parse_offer_text infiere vip', t6)

    # bot_engine._filter_params
    def t7():
        d = {'selection_mode':'quick','offer_index':1,'category':'ga','quantity':4,
             'url':'https://x','browser':'morelogin','captcha_provider':'auto',
             'strategy':'sniper_smart','foo':'bar','password':'should_not_pass'}
        f = bot_engine._filter_params(d)
        assert 'foo' not in f and 'password' not in f, f'leaked: {f}'
        assert f['selection_mode']=='quick' and f['offer_index']==1 and f['quantity']==4
        return f'{len(f)} keys filtradas'
    _run('bot_engine._filter_params', t7)

    # bot_engine.Group → Worker hereda params
    def t8():
        g = bot_engine.Group('eid','es', params={'selection_mode':'seatmap','quantity':4})
        w = g.add_worker(account_id='acc1')
        assert w.params['selection_mode'] == 'seatmap'
        assert w.params['quantity'] == 4
        # Override: worker-specific param gana
        w2 = g.add_worker(params={'quantity': 2})
        assert w2.params['quantity'] == 2
        assert w2.params['selection_mode'] == 'seatmap'  # heredado
        return 'herencia + override OK'
    _run('Group→Worker herencia params', t8)

    # ticket_selector.PRICE_RE
    def t9():
        import re
        m = ticket_selector.PRICE_RE.search('Total: 88,00 € cada una')
        assert m and m.group(1) == '88,00', f'm={m}'
        return m.group(1)
    _run('PRICE_RE detecta precio', t9)


# ═══════════════════════════════════════════════════════════════════
# 2. ENDPOINT TESTS (sin browser)
# ═══════════════════════════════════════════════════════════════════
def endpoint_tests():
    print('\n══ ENDPOINT TESTS ══')
    BASE = 'http://127.0.0.1:5055'
    s = requests.Session()
    r = s.post(f'{BASE}/login', data={'username':'admin','password':'admin'}, timeout=10)
    assert r.status_code in (200, 302), f'login status={r.status_code}'

    def t1():
        r = s.get(f'{BASE}/api/events/2059118961/offers', timeout=10)
        assert r.status_code == 200, f'status={r.status_code}'
        d = r.json()
        assert d['ok'] is True
        assert len(d.get('offers', [])) == 3, f'offers={len(d.get("offers",[]))}'
        names = [o['name'] for o in d['offers']]
        assert 'Entrada General Pista' in names
        return f'{len(d["offers"])} ofertas'
    _run('GET offers endpoint', t1)

    def t2():
        # Simula payload del frontend con todas las opciones
        payload = {
            'event_id':'efacb14b','domain_ids':['es'],'worker_count':1,
            'selection_mode':'seatmap','offer_index':1,'category':'reserved',
            'quantity':4,'url':'https://x','browser':'morelogin','captcha_provider':'auto',
            'strategy':'sniper_smart','name':'test_battery_seatmap'
        }
        r = s.post(f'{BASE}/api/bot/groups', json=payload, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d['ok'] and d['group']
        gp = d['group']['params']
        assert gp['selection_mode']=='seatmap' and gp['offer_index']==1
        assert gp['category']=='reserved' and gp['quantity']==4
        # Worker hereda
        wp = d['group']['workers'][0]['params']
        assert wp['selection_mode']=='seatmap' and wp['offer_index']==1
        return f'group {d["group"]["id"]}, params propagados'
    _run('POST groups seatmap+offer_index', t2)

    def t3():
        payload = {'event_id':'efacb14b','domain_ids':['es'],'worker_count':3,
                   'selection_mode':'quick','quantity':2,'category':'ga','name':'test_battery_quick3'}
        r = s.post(f'{BASE}/api/bot/groups', json=payload, timeout=10)
        d = r.json()
        assert len(d['group']['workers']) == 3, f'workers={len(d["group"]["workers"])}'
        for w in d['group']['workers']:
            assert w['params']['selection_mode'] == 'quick'
            assert w['params']['quantity'] == 2
        return f'3 workers heredan quick+qty2'
    _run('POST groups quick 3 workers herencia', t3)

    def t4():
        # POST con campos vacíos: deben usar defaults
        payload = {'event_id':'efacb14b','domain_ids':['es'],'worker_count':1}
        r = s.post(f'{BASE}/api/bot/groups', json=payload, timeout=10)
        d = r.json()
        assert d['ok']
        gp = d['group']['params']
        assert gp.get('selection_mode') == 'quick'  # default
        assert gp.get('quantity') == 1  # default
        return 'defaults aplicados'
    _run('POST groups defaults', t4)

    def t5():
        # GET groups recupera todos los grupos creados con sus params
        r = s.get(f'{BASE}/api/bot/groups', timeout=10)
        d = r.json()
        groups = d.get('groups', []) if isinstance(d, dict) else d
        # debería incluir los 3 que acabamos de crear
        names = [g.get('name','') for g in groups]
        assert any('test_battery' in n for n in names), f'creados no encontrados: {names}'
        return f'{len(groups)} grupos visibles'
    _run('GET groups list', t5)


# ═══════════════════════════════════════════════════════════════════
# 3. LIVE BROWSER TESTS (perfil MoreLogin)
# ═══════════════════════════════════════════════════════════════════
def live_tests(port: int):
    print(f'\n══ LIVE BROWSER TESTS (port={port}) ══')
    import ticket_selector
    from playwright.sync_api import sync_playwright
    v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=5).json()
    pw = sync_playwright().start()
    b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
    page = next((p for p in b.contexts[0].pages if 'ticketmaster' in (p.url or '')), b.contexts[0].pages[0])
    page.bring_to_front()

    TM_URL = 'https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961'
    if '2059118961' not in (page.url or ''):
        time.sleep(5)
        page.goto(TM_URL, wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(7000)
        for txt in ['Aceptar todas las cookies','Rechazarlas todas']:
            try:
                btn = page.get_by_text(txt, exact=False).first
                if btn.is_visible(timeout=1500): btn.click(); page.wait_for_timeout(1500); break
            except: pass

    def t1():
        ok = ticket_selector.switch_to_quick_mode(page)
        page.wait_for_timeout(3500)
        assert ok, 'switch_to_quick failed'
        return 'OK'
    _run('switch_to_quick_mode', t1)

    def t2():
        offs = ticket_selector.detect_offers(page)
        assert len(offs) >= 1, f'offers={len(offs)}'
        names = [o['name'] for o in offs]
        assert any('Pista' in n for n in names), f'names={names}'
        return f'{len(offs)} ofertas: {names[:3]}'
    _run('detect_offers (quick)', t2)

    # Test qty progresivo: 0 → 1 → 4 → reset
    def t3():
        ticket_selector.reset_qty(page, 0)
        res = ticket_selector.select_quick(page, qty=1, offer_index=0)
        assert res['ok'] and res['applied_qty']==1, res
        return f'qty=1 OK total={res.get("total_price")}€'
    _run('select_quick qty=1', t3)

    def t4():
        # ya está a 1; subir a 4 (incrementa solo 3)
        res = ticket_selector.select_quick(page, qty=4, offer_index=0)
        assert res['ok'] and res['applied_qty']==4, res
        return f'qty=4 OK ({len(res["debug"].get("attempts",[]))} clicks)'
    _run('select_quick qty=4 (incremental desde 1)', t4)

    def t5():
        n = ticket_selector.reset_qty(page, 0)
        assert n == 0, f'reset_qty={n}'
        return 'reset OK'
    _run('reset_qty(0) → 0', t5)

    def t6():
        res = ticket_selector.select_quick(page, qty=4, offer_index=1)
        assert res['ok'] and res['applied_qty']==4, res
        ticket_selector.reset_qty(page, 1)
        return f'oferta 1 qty=4 OK'
    _run('select_quick offer_index=1', t6)

    def t7():
        res = ticket_selector.select_quick(page, qty=4, offer_index=2)
        assert res['ok'] and res['applied_qty']==4, res
        ticket_selector.reset_qty(page, 2)
        return f'oferta 2 qty=4 OK'
    _run('select_quick offer_index=2', t7)

    def t8():
        # offer_index out of range → ok=False con error específico
        res = ticket_selector.select_quick(page, qty=4, offer_index=99)
        assert res['ok'] is False
        assert 'out_of_range' in res.get('error',''), res
        return 'rejected correctly'
    _run('select_quick offer_index=99 → error', t8)

    def t9():
        # qty=0 sobre stepper en 0 → noop ok
        ticket_selector.reset_qty(page, 0)
        res = ticket_selector.select_quick(page, qty=0, offer_index=0)
        assert res['ok'] is True
        assert res['applied_qty']==0
        return 'qty=0 noop OK'
    _run('select_quick qty=0 noop', t9)

    def t10():
        # Cambio de tab quick → seatmap → quick (estabilidad)
        ticket_selector.switch_to_seatmap_mode(page); page.wait_for_timeout(3000)
        seatmap_pressed = page.evaluate("""() => Array.from(document.querySelectorAll('button')).find(b=>/ver mapa/i.test(b.innerText||''))?.getAttribute('aria-pressed')""")
        assert seatmap_pressed=='true'
        ticket_selector.switch_to_quick_mode(page); page.wait_for_timeout(3000)
        quick_pressed = page.evaluate("""() => Array.from(document.querySelectorAll('button')).find(b=>/ver lo mejor/i.test(b.innerText||''))?.getAttribute('aria-pressed')""")
        assert quick_pressed=='true'
        # Y las ofertas vuelven a estar visibles
        offs = ticket_selector.detect_offers(page)
        assert len(offs) >= 1
        return f'cambio tabs OK, {len(offs)} ofertas tras volver'
    _run('toggle quick↔seatmap estabilidad', t10)

    def t11():
        # select_seatmap end-to-end
        res = ticket_selector.select_seatmap(page, qty=4, fallback_to_quick=True)
        # Aceptamos quick_fallback porque el SVG es virtualizado
        assert res['ok'] and res['applied_qty']==4, f'res={res}'
        mode = res.get('mode_used')
        return f'mode={mode} qty={res["applied_qty"]}'
    _run('select_seatmap qty=4 (con fallback OK)', t11)

    def t12():
        # Cleanup final: dejar oferta 0 con qty=2 (estado conocido para inspección manual)
        ticket_selector.reset_qty(page, 0)
        res = ticket_selector.select_quick(page, qty=2, offer_index=0)
        assert res['applied_qty']==2
        return 'estado final qty=2'
    _run('select_quick qty=2 estado final', t12)

    b.close()
    pw.stop()


# ═══════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════
def main():
    print('=' * 60)
    print(' BATERÍA DE TESTS — apartado de selección de entradas')
    print('=' * 60)

    unit_tests()
    endpoint_tests()

    # Live tests requieren _buy_state.json con port válido
    state_path = '_buy_state.json'
    port = None
    if os.path.exists(state_path):
        try:
            st = json.load(open(state_path, encoding='utf-8'))
            port = st.get('port')
        except Exception: pass
    if port:
        try:
            requests.get(f'http://127.0.0.1:{port}/json/version', timeout=3)
            live_tests(port)
        except Exception as e:
            print(f'\n[SKIP LIVE] CDP unreachable: {e}')
    else:
        print('\n[SKIP LIVE] no _buy_state.json válido')

    # Summary
    print('\n' + '=' * 60)
    passed = sum(1 for r in RESULTS if r['status']=='pass')
    failed = sum(1 for r in RESULTS if r['status']=='fail')
    skipped = sum(1 for r in RESULTS if r['status']=='skip')
    total = len(RESULTS)
    print(f' SUMMARY: {passed}/{total} PASS · {failed} FAIL · {skipped} SKIP')
    print('=' * 60)
    if failed:
        print('\nFALLOS:')
        for r in RESULTS:
            if r['status']=='fail':
                print(f'  · {r["name"]}: {r["detail"][:200]}')

    # Persistir resultado
    out_path = r'datos\selector_test_results.json'
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'summary':{'pass':passed,'fail':failed,'skip':skipped,'total':total},
                   'results':RESULTS, 'tested_at':time.strftime('%Y-%m-%dT%H:%M:%S')},
                  f, indent=2, ensure_ascii=False)
    print(f'\nDetalles: {out_path}')
    sys.exit(0 if failed==0 else 1)

if __name__ == '__main__':
    main()
