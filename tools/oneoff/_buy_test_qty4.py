"""Test depurado del selector quick: selecciona 4 entradas en cada oferta de un evento.
Usa ticket_selector.select_quick(). Reporta resultado de cada oferta."""
import sys, os, json, time
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')
import requests
from playwright.sync_api import sync_playwright

import ticket_selector

with open('_buy_state.json', encoding='utf-8') as f:
    st = json.load(f)
PORT = st['port']
EV = st['event']
QTY = int(os.environ.get('TM_QTY', '4'))

print(f'== TEST QTY={QTY} | port={PORT} | profile={st["profile"]} ==')
print(f'   evento: "{EV["name"]}"')

SHOTS = r'C:\Users\user\TM\proyecto\datos\buy\qty4'
os.makedirs(SHOTS, exist_ok=True)
def shot(p, name):
    p.screenshot(path=os.path.join(SHOTS, f'{name}.png'), full_page=False)
    print(f'  shot {name}')

v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=5).json()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
    page = next((p for p in b.contexts[0].pages if 'ticketmaster' in (p.url or '')), b.contexts[0].pages[0])
    page.bring_to_front()

    # Si la página no es la del evento, navegar
    if EV['tm_id'] not in (page.url or ''):
        print(f'  navegando a {EV["tm_url"][:60]}')
        page.goto(EV['tm_url'], wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(7000)
        # cookies banner externo
        for txt in ['Aceptar todas las cookies', 'Rechazarlas todas']:
            try:
                btn = page.get_by_text(txt, exact=False).first
                if btn.is_visible(timeout=1500):
                    btn.click(); page.wait_for_timeout(1200); break
            except: pass

    # Modo rápido
    print('\n[1] cambiar a modo Ver lo mejor disponible')
    ok = ticket_selector.switch_to_quick_mode(page)
    print(f'  switch={ok}')
    page.wait_for_timeout(3500)
    shot(page, '01_quick_mode')

    # Detectar ofertas
    offers = ticket_selector.detect_offers(page)
    print(f'\n[2] ofertas detectadas: {len(offers)}')
    for o in offers:
        ptxt = f'{o["price_min"]}€' if o['price_min']==o['price_max'] else f'{o["price_min"]}-{o["price_max"]}€'
        print(f'  [{o["index"]}] {o["name"]} · {ptxt}')

    # Para cada oferta, intentar seleccionar QTY (resetear primero)
    results = []
    for o in offers:
        idx = o['index']
        print(f'\n[3.{idx}] testeando "{o["name"]}" - resetear y subir a {QTY}')
        # reset previo (por si quedó qty residual)
        prev = ticket_selector.reset_qty(page, idx)
        print(f'  reset → qty={prev}')

        res = ticket_selector.select_quick(page, qty=QTY, offer_index=idx)
        print(f'  resultado: ok={res["ok"]} applied_qty={res["applied_qty"]} '
              f'offer={res["offer_name"]} total={res.get("total_price")}€')
        if not res['ok']:
            print(f'  debug.attempts: {len(res["debug"].get("attempts", []))}')
            print(f'  debug.hit_limit: {res["debug"].get("hit_limit")}')
            print(f'  debug.stepper_final: {res["debug"].get("stepper_final")}')
        shot(page, f'02_offer{idx}_qty{res["applied_qty"]}')
        results.append({'offer_index': idx, **{k:v for k,v in res.items() if k!='debug'}})

        # Reset al final para no mezclar entre ofertas
        ticket_selector.reset_qty(page, idx)

    # Test combinado: subir solo en la primera y dejar
    print(f'\n[4] selección final: oferta 0 → {QTY}')
    final = ticket_selector.select_quick(page, qty=QTY, offer_index=0)
    print(f'  final: ok={final["ok"]} applied_qty={final["applied_qty"]} total={final.get("total_price")}€')
    shot(page, '03_final')

    print('\n[5] resumen JSON:')
    summary = {
        'event': EV['name'],
        'qty_target': QTY,
        'offers_tested': results,
        'final_selection': {k:v for k,v in final.items() if k!='debug'},
        'tested_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    with open(os.path.join(SHOTS, 'qty4_test_result.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    b.close()
print('\nDone')
