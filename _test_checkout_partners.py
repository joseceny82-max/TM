"""Marcar Partners del evento (¡Sí!), Seguro entradas, condiciones, y click "Ir al pago" verde."""
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
    ctx = b.contexts[0]
    page = next((p for p in ctx.pages if 'ticketmaster' in (p.url or '')), ctx.pages[0])
    page.bring_to_front()
    log(f'url: {page.url[:120]}')

    log('[1] PARTNERS — click radio "¡Sí! Quiero estar informado..."')
    r = page.evaluate("""
      () => {
        const radio = document.getElementById('marketingConsents-yes-input');
        if (!radio) return { found: false };
        radio.scrollIntoView({block:'center'});
        radio.click();
        return { found: true, checked: radio.checked };
      }
    """)
    log(f'  partners_yes: {r}')
    page.wait_for_timeout(1200)
    shot(page, '18_partners_yes')

    log('[2] SEGURO DE ENTRADAS — inspect options')
    seguro = page.evaluate("""
      () => {
        const radios = [...document.querySelectorAll('input[type="radio"]')];
        return radios.filter(r => r.offsetParent).map(r => ({
          id: r.id, name: r.name, value: r.value, checked: r.checked,
          label: ((document.querySelector(`label[for="${r.id}"]`)||{}).innerText||'').slice(0,80),
        }));
      }
    """)
    log(f'  radios visibles: {len(seguro)}')
    for s in seguro: log(f'    · {s["name"]}={s["value"]} checked={s["checked"]} "{s["label"][:70]}"')

    # Click "no" en seguro (no quiero seguro)
    log('[3] SEGURO — click "No quiero seguro" (o variante No)')
    r2 = page.evaluate("""
      () => {
        const radios = [...document.querySelectorAll('input[type="radio"]')];
        // Buscar radios que NO sean marketing
        const segs = radios.filter(r => /seguro|insurance/i.test(r.name||'') || /seguro|insurance/i.test(r.id||''));
        if (segs.length === 0) {
          // fallback: cualquier "No quiero" / "No, gracias"
          const labels = [...document.querySelectorAll('label')];
          const noLbl = labels.find(l => /no quiero|no,? gracias|sin seguro|prefiero no/i.test((l.textContent||'').slice(0,200)) && !/comunicaciones/i.test(l.textContent||''));
          if (noLbl) {
            const cb = noLbl.querySelector('input') || document.getElementById(noLbl.getAttribute('for')||'');
            if (cb) { cb.click(); return { clicked: 'label', label: (noLbl.textContent||'').slice(0,80) }; }
          }
          return { found: 0 };
        }
        // Click el value "no"
        const no = segs.find(r => /no/i.test(r.value || '') && !/yes|si/i.test(r.value));
        if (no) { no.click(); return { clicked: 'radio', value: no.value, name: no.name }; }
        return { segs_count: segs.length };
      }
    """)
    log(f'  seguro_no: {r2}')
    page.wait_for_timeout(1500)
    shot(page, '19_seguro_no')

    log('[4] CONDICIONES — marcar checkbox "Confirmo que he leído..."')
    r3 = page.evaluate("""
      () => {
        const labels = [...document.querySelectorAll('label')];
        const lbl = labels.find(l => /confirmo que he le[ií]do|condiciones de compra/i.test(l.textContent||''));
        if (!lbl) return { found: false };
        const cb = lbl.querySelector('input[type="checkbox"]') ||
                   document.getElementById(lbl.getAttribute('for')||'');
        if (cb && !cb.checked) cb.click();
        return { found: true, checked: cb ? cb.checked : null };
      }
    """)
    log(f'  condiciones: {r3}')
    page.wait_for_timeout(1500)
    shot(page, '20_condiciones')

    log('[5] click "Ir al pago" verde')
    btn = page.get_by_role('button', name=re.compile(r'^ir al pago$', re.I)).first
    btn.scroll_into_view_if_needed(timeout=3000)
    btn.click(force=True, timeout=5000)
    log('  ✓ click Ir al pago')
    page.wait_for_timeout(8000)
    shot(page, '21_payment_screen')

    log(f'  url: {page.url[:140]}')

    log('[6] localizar botón "Pagar" (NO clickear)')
    pay = page.evaluate("""
      () => {
        const btns = [...document.querySelectorAll('button, [role="button"]')];
        return btns
          .filter(b => /^(pagar|pagar ahora|completar|finalizar)/i.test((b.innerText||'').trim()))
          .filter(b => b.offsetParent !== null)
          .slice(0,5)
          .map(b => ({ text: (b.innerText||'').trim().slice(0,60), disabled: b.disabled,
                        testid: b.getAttribute('data-testid')||'' }));
      }
    """)
    if pay:
        log('  ✓ Pagar detectado:')
        for p in pay:
            log(f'    {"🔒" if p["disabled"] else "🟢"} "{p["text"]}" testid={p["testid"]}')
    else:
        log('  ⚠ no Pagar visible aún — necesita tarjeta')
    shot(page, '22_FINAL_stop')
    log('=== STOP justo antes de Pagar ===')
