"""Prueba aislada: comprar 4 entradas en categoría 'Asiento Reservado -
Visibilidad Reducida' (stepper [2]) en evento marcado del dashboard,
usando MoreLogin con cuenta registrada chancy.aniline-6c@icloud.com.

Flow real TM ES (memoria sección 12):
  1. Ver lo mejor disponible (modo quick)
  2. Stepper qty=4 en offer_index=2 (Vis.Reducida)
  3. Click "Entradas" verde (findTicketsBtn) — trusted click
  4. Check "Acepto que he leído"
  5. Click "Continuar comprando"
  → redirect OAuth (ahí termina mi prueba — el bot solo selecciona)
"""
import sys, io, os, time, json, re, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
os.chdir(r'C:\Users\user\TM\proyecto')
from playwright.sync_api import sync_playwright
import ticket_selector

PORT = 57511
EV_URL = 'https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961'
QTY = 4
OFFER_IDX = 2  # Vis.Reducida según memoria sección 12
SHOTS = r'C:\Users\user\TM\proyecto\datos\buy\seatmap_visibilidad'
os.makedirs(SHOTS, exist_ok=True)

def log(msg): print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)
def shot(page, name):
    p = os.path.join(SHOTS, f'{int(time.time())}_{name}.png')
    try: page.screenshot(path=p, full_page=False); log(f'  📸 {os.path.basename(p)}')
    except Exception as e: log(f'  shot_err {e}')

log(f'== TEST quick stepper[{OFFER_IDX}] Vis.Reducida qty={QTY} ==')

v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=5).json()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
    ctx = b.contexts[0] if b.contexts else b.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.bring_to_front()

    if '2059118961' not in (page.url or ''):
        log('[1] navegando al evento')
        page.goto(EV_URL, wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(4000)
    else:
        log(f'[1] ya en evento')

    log('[2] dismiss cookie/privacy modal')
    for txt in ['Acepto', 'Aceptar todas las cookies', 'Aceptar todas', 'Aceptar']:
        try:
            btn = page.get_by_role('button', name=re.compile(rf'^{re.escape(txt)}$', re.I)).first
            if btn.is_visible(timeout=1500):
                btn.click(); log(f'  ✓ click "{txt}"'); page.wait_for_timeout(1500); break
        except Exception: pass

    page.wait_for_timeout(2000)
    shot(page, '01_landing')

    log('[3] cambiar a modo QUICK ("Ver lo mejor disponible")')
    ok_q = ticket_selector.switch_to_quick_mode(page)
    log(f'  switch_to_quick_mode={ok_q}')
    page.wait_for_timeout(3500)
    shot(page, '02_quick')

    log('[4] detectar ofertas')
    offers = ticket_selector.detect_offers(page)
    log(f'  ofertas: {len(offers)}')
    for o in offers:
        ptxt = f'{o["price_min"]}€' if o.get('price_min') == o.get('price_max') else f'{o.get("price_min")}-{o.get("price_max")}€'
        log(f'    [{o["index"]}] {o["name"]} · {ptxt}')

    if len(offers) <= OFFER_IDX:
        log(f'  ⚠ stepper[{OFFER_IDX}] no existe (solo {len(offers)} ofertas) — buscando por nombre')
        target = next((o for o in offers if re.search(r'visibilidad reducida|vis\.?\s*reducida', o['name'], re.I)), None)
        if not target:
            log('  ❌ ninguna oferta matchea "Visibilidad Reducida" — abortando')
            for o in offers: log(f'    - "{o["name"]}"')
            sys.exit(0)
        target_idx = target['index']
        log(f'  ✓ matched por nombre: idx={target_idx} "{target["name"]}"')
    else:
        target_idx = OFFER_IDX
        target = offers[OFFER_IDX]
        log(f'  ✓ usando stepper[{OFFER_IDX}]: "{target["name"]}"')

    log(f'[5] reset qty offer[{target_idx}]')
    prev = ticket_selector.reset_qty(page, target_idx)
    log(f'  reset → qty previa={prev}')
    page.wait_for_timeout(800)

    log(f'[6] select_quick qty={QTY} offer_index={target_idx}')
    res = ticket_selector.select_quick(page, qty=QTY, offer_index=target_idx)
    log(f'  resultado: ok={res["ok"]} applied_qty={res["applied_qty"]} total={res.get("total_price")}€ name="{res.get("offer_name")}"')
    if not res['ok']:
        dbg = res.get('debug', {})
        log(f'  debug.hit_limit: {dbg.get("hit_limit")}')
        log(f'  debug.stepper_final: {dbg.get("stepper_final")}')
        log(f'  debug.attempts: {len(dbg.get("attempts", []))}')
    shot(page, '03_qty_set')

    if res.get('applied_qty', 0) >= QTY:
        log('[7] click "Entradas" verde (findTicketsBtn) — trusted')
        try:
            btn = page.locator('button[data-testid="findTicketsBtn"]').first
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(force=True, timeout=5000)
            log('  ✓ click Entradas')
            page.wait_for_timeout(3000)
        except Exception as e:
            log(f'  ❌ click Entradas err: {e}')
        shot(page, '04_after_entradas')

        log('[8] check "Acepto que he leído"')
        try:
            checked = page.evaluate("""
              () => {
                const labels = [...document.querySelectorAll('label')];
                const lbl = labels.find(l => /acepto que he le[ií]do/i.test(l.textContent || ''));
                if (!lbl) return { found: false };
                const cb = lbl.querySelector('input[type="checkbox"]') ||
                           document.getElementById((lbl.getAttribute('for')||''));
                if (!cb) return { found: true, cb: false };
                if (!cb.checked) cb.click();
                return { found: true, cb: true, checked: cb.checked };
              }
            """)
            log(f'  acepto: {checked}')
            page.wait_for_timeout(1500)
        except Exception as e:
            log(f'  acepto err: {e}')
        shot(page, '05_acepto')

        log('[9] click "Continuar comprando" — trusted')
        try:
            btn = page.get_by_role('button', name=re.compile(r'^continuar comprando$', re.I)).first
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(force=True, timeout=5000)
            log('  ✓ click Continuar comprando')
            page.wait_for_timeout(5000)
        except Exception as e:
            log(f'  ❌ continuar err: {e}')
        shot(page, '06_continuar')

        log(f'  url final: {page.url[:120]}')

    log('=== TEST COMPLETADO — browser sigue abierto ===')
