"""Rellenar tarjeta crédito (5469915287607214 / 06/29 / 260) y click "Añadir tarjeta" (azul). STOP."""
import sys, io, os, time, re, requests, random
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
os.chdir(r'C:\Users\user\TM\proyecto')
from playwright.sync_api import sync_playwright

PORT = 57511
SHOTS = r'C:\Users\user\TM\proyecto\datos\buy\seatmap_visibilidad'

CARD = {
    'number':  '5469915287607214',
    'expiry':  '06/29',
    'cvv':     '260',
    'holder':  'Monica Serrano Garcia',
}

def log(msg): print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)
def shot(page, name):
    p = os.path.join(SHOTS, f'{int(time.time())}_{name}.png')
    page.screenshot(path=p, full_page=False); log(f'  📸 {os.path.basename(p)}')

def human_type(target, val, delay_min=50, delay_max=110):
    target.click()
    target.evaluate('el => el.value=""')  # belt+suspenders
    for ch in val:
        target.type(ch, delay=random.randint(delay_min, delay_max))

v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=5).json()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
    page = next((p for p in b.contexts[0].pages if 'payments' in (p.url or '') or 'ticketmaster' in (p.url or '')), b.contexts[0].pages[0])
    page.bring_to_front()
    log(f'url: {page.url[:120]}')

    # Inputs de tarjeta probablemente en iframe Adyen — buscar todos los frames
    log('[1] inspect frames')
    for i, fr in enumerate(page.frames):
        log(f'  frame[{i}] url={fr.url[:80]}')

    # Buscar inputs por placeholder/aria en page principal Y en frames
    log('[2] localizar inputs tarjeta (frame por frame)')
    targets = {}
    for fr in [page] + list(page.frames):
        try:
            inputs = fr.evaluate("""
              () => [...document.querySelectorAll('input')].filter(i => i.offsetParent && i.type !== 'hidden').map(i => ({
                  id: (i.id||'').slice(0,40),
                  name: (i.name||'').slice(0,40),
                  type: i.type, placeholder: (i.placeholder||'').slice(0,40),
                  aria: (i.getAttribute('aria-label')||'').slice(0,40),
                  inputmode: i.inputMode||'',
                  value: (i.value||'').slice(0,30),
              }))
            """)
            for inp in inputs:
                blob = (inp['id'] + ' ' + inp['name'] + ' ' + inp['placeholder'] + ' ' + inp['aria']).lower()
                kind = None
                if re.search(r'card.*number|n[uú]mero.*tarjeta|encryptedCardNumber|cardNumber', blob): kind = 'number'
                elif re.search(r'expir|caduc|encryptedExpiryDate|expiryDate', blob): kind = 'expiry'
                elif re.search(r'security|cvc|cvv|c[oó]digo.*seguridad|encryptedSecurityCode', blob): kind = 'cvv'
                elif re.search(r'holder|titular|name.*card', blob): kind = 'holder'
                if kind and kind not in targets:
                    targets[kind] = (fr, inp)
                    log(f'  → {kind}: id={inp["id"]} name={inp["name"]} ph="{inp["placeholder"]}" aria="{inp["aria"]}"')
        except Exception as e: log(f'  frame err: {e}')

    log(f'\n[3] rellenar campos detectados: {list(targets.keys())}')
    field_to_value = {'number': CARD['number'].replace(' ',''),
                      'expiry': CARD['expiry'],
                      'cvv': CARD['cvv'],
                      'holder': CARD['holder']}

    for kind in ('holder', 'number', 'expiry', 'cvv'):
        if kind not in targets: continue
        fr, inp = targets[kind]
        # Localizar via name preferentemente
        if inp['name']: sel = f'[name="{inp["name"]}"]'
        elif inp['id']: sel = f'[id="{inp["id"]}"]'
        else: continue
        try:
            el = fr.locator(sel).first
            el.scroll_into_view_if_needed(timeout=3000)
            el.click()
            el.evaluate('el => el.value=""')
            for ch in field_to_value[kind]:
                el.type(ch, delay=random.randint(50, 110))
            log(f'  ✓ {kind} rellenado')
            page.wait_for_timeout(400)
        except Exception as e:
            log(f'  ❌ {kind} err: {e}')

    page.wait_for_timeout(800)
    shot(page, '28_card_filled')

    log('[4] click "Añadir tarjeta" (azul)')
    try:
        btn = page.get_by_role('button', name=re.compile(r'^a[ñn]adir tarjeta$', re.I)).first
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(force=True, timeout=5000)
        log('  ✓ click Añadir tarjeta')
        page.wait_for_timeout(5000)
    except Exception as e:
        log(f'  ❌ añadir err: {e}')
    shot(page, '29_after_anyadir')

    log(f'  url: {page.url[:140]}')

    log('[5] verificar estado botón "Realizar Compra" (NO clickear)')
    state = page.evaluate("""
      () => {
        const btns = [...document.querySelectorAll('button')];
        const r = btns.find(b => /^realizar compra/i.test((b.innerText||'').trim()));
        if (!r) return { found: false };
        return { found: true, disabled: r.disabled, testid: r.getAttribute('data-testid')||'' };
      }
    """)
    log(f'  Realizar Compra: {state}')
    shot(page, '30_FINAL_card_added')
    log('=== STOP — tarjeta añadida, justo antes de Realizar Compra ===')
