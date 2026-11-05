"""Click directo insurance=false → click Ir al pago."""
import sys, io, os, time, re, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
os.chdir(r'C:\Users\user\TM\proyecto')
from playwright.sync_api import sync_playwright

PORT = 57511
SHOTS = r'C:\Users\user\TM\proyecto\datos\buy\seatmap_visibilidad'

def log(msg): print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)
def shot(page, name):
    p = os.path.join(SHOTS, f'{int(time.time())}_{name}.png')
    page.screenshot(path=p, full_page=False); log(f'  📸 {os.path.basename(p)}')

v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=5).json()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
    page = next((p for p in b.contexts[0].pages if 'ticketmaster' in (p.url or '')), b.contexts[0].pages[0])
    page.bring_to_front()

    log('[1] click insurance=false (No quiero seguro)')
    r = page.evaluate("""
      () => {
        const radio = [...document.querySelectorAll('input[type="radio"]')]
          .find(r => r.name === 'insurance' && r.value === 'false');
        if (!radio) return { found: false };
        radio.scrollIntoView({block:'center'});
        radio.click();
        return { found: true, checked: radio.checked };
      }
    """)
    log(f'  insurance=false: {r}')
    page.wait_for_timeout(1500)
    shot(page, '23_seguro_no_real')

    log('[2] re-marcar condiciones de compra')
    page.evaluate("""
      () => {
        const labels = [...document.querySelectorAll('label')];
        const lbl = labels.find(l => /confirmo que he le[ií]do|condiciones de compra/i.test(l.textContent||''));
        if (lbl) {
          const cb = lbl.querySelector('input[type="checkbox"]') || document.getElementById(lbl.getAttribute('for')||'');
          if (cb && !cb.checked) cb.click();
        }
      }
    """)
    page.wait_for_timeout(1000)

    log('[3] click "Ir al pago" verde')
    btn = page.get_by_role('button', name=re.compile(r'^ir al pago$', re.I)).first
    btn.scroll_into_view_if_needed(timeout=3000)
    btn.click(force=True, timeout=5000)
    log('  ✓ click Ir al pago')
    page.wait_for_timeout(9000)
    shot(page, '24_payment_real')

    log(f'  url: {page.url[:140]}')

    log('[4] localizar botón Pagar (NO clickear)')
    pay = page.evaluate("""
      () => {
        const btns = [...document.querySelectorAll('button, [role="button"]')];
        return btns
          .filter(b => /^(pagar|pagar ahora|completar|finalizar|pay)/i.test((b.innerText||'').trim()))
          .filter(b => b.offsetParent !== null)
          .slice(0,5)
          .map(b => ({ text: (b.innerText||'').trim().slice(0,60), disabled: b.disabled,
                        testid: b.getAttribute('data-testid')||'' }));
      }
    """)
    if pay:
        log('  ✓ botón Pagar detectado:')
        for p in pay:
            log(f'    {"🔒" if p["disabled"] else "🟢"} "{p["text"]}" testid={p["testid"]}')
    else:
        log('  ⚠ no Pagar visible — quizás falta tarjeta')

    # Headings de la nueva pantalla
    h = page.evaluate("""
      () => [...document.querySelectorAll('h1, h2, h3')].slice(0,8).map(x => (x.innerText||'').trim().slice(0,80))
    """)
    log('  headings:')
    for hh in h: log(f'    · {hh}')

    shot(page, '25_FINAL')
    log('=== STOP — antes del botón Pagar ===')
