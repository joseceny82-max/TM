"""Fix: seleccionar Provincia desde el dropdown autocomplete y re-guardar."""
import sys, io, os, time, re, requests, random
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
    ctx = b.contexts[0]
    page = next((p for p in ctx.pages if 'ticketmaster' in (p.url or '')), ctx.pages[0])
    page.bring_to_front()
    log(f'url: {page.url[:120]}')

    # Provincia autocomplete: click → clear → type → wait options → click "Murcia"
    log('[1] click Provincia → limpiar → re-tipear → click opción')
    sel_prov = '[id="stateCode-:rf:-input"]'
    el = page.locator(sel_prov).first
    el.scroll_into_view_if_needed()
    el.click()
    page.keyboard.press('Control+A'); page.keyboard.press('Delete')
    page.wait_for_timeout(400)
    for ch in 'Murc':
        el.type(ch, delay=random.randint(80, 130))
    page.wait_for_timeout(1200)
    shot(page, '13_prov_typing')

    # Buscar dropdown options
    log('[2] buscar opción "Murcia" en dropdown')
    clicked = page.evaluate("""
      () => {
        // Buscar listbox/menu visible con la palabra Murcia
        const candidates = [...document.querySelectorAll('li, [role="option"], [role="listitem"], div[class*="option" i], div[class*="item" i]')];
        const opts = candidates.filter(el => {
          if (!el.offsetParent) return false;
          const t = (el.innerText || '').trim();
          return /^murcia$/i.test(t);
        });
        if (opts.length === 0) return { found: 0 };
        opts[0].click();
        return { found: opts.length, clicked: opts[0].outerHTML.slice(0,200) };
      }
    """)
    log(f'  result: {clicked}')
    page.wait_for_timeout(1500)
    shot(page, '14_prov_selected')

    # Verificar valor input
    val = page.locator(sel_prov).input_value()
    log(f'  Provincia input.value = "{val}"')

    log('[3] click "Guardar y continuar" (azul) de nuevo')
    btn = page.get_by_role('button', name=re.compile(r'^guardar y continuar$', re.I)).first
    btn.scroll_into_view_if_needed(timeout=3000)
    btn.click(force=True, timeout=5000)
    log('  ✓ click Guardar y continuar')
    page.wait_for_timeout(5000)
    shot(page, '15_after_guardar2')

    # Aceptar condiciones si aparecen
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
    page.wait_for_timeout(800)

    log('[4] click "Ir al pago"')
    btn = page.get_by_role('button', name=re.compile(r'^ir al pago$', re.I)).first
    btn.scroll_into_view_if_needed(timeout=3000)
    btn.click(force=True, timeout=5000)
    log('  ✓ click Ir al pago')
    page.wait_for_timeout(8000)
    shot(page, '16_after_irpago')

    log(f'  url: {page.url[:140]}')

    log('[5] localizar botón Pagar (NO clickear)')
    pay_btn = page.evaluate("""
      () => {
        const btns = [...document.querySelectorAll('button, [role="button"]')];
        return btns
          .filter(b => /^(pagar|pagar ahora|completar|finalizar)/i.test((b.innerText||'').trim()))
          .filter(b => b.offsetParent !== null)
          .slice(0, 5)
          .map(b => ({ text: (b.innerText||'').trim().slice(0,60), disabled: b.disabled,
                       testid: b.getAttribute('data-testid') || '' }));
      }
    """)
    if pay_btn:
        log('  ✓ Pagar detectado:')
        for p in pay_btn:
            log(f'    {"🔒" if p["disabled"] else "🟢"} "{p["text"]}" testid={p["testid"]}')
    else:
        log('  ⚠ no veo botón Pagar — quizás necesita tarjeta primero')

    shot(page, '17_FINAL_stop')
    log('=== STOP justo antes de Pagar ===')
