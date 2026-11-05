"""Click método de pago = Tarjeta de Crédito/Débito."""
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
    page = next((p for p in b.contexts[0].pages if 'ticketmaster' in (p.url or '') or 'payments' in (p.url or '')), b.contexts[0].pages[0])
    page.bring_to_front()
    log(f'url: {page.url[:120]}')

    log('[1] inspect métodos de pago disponibles')
    methods = page.evaluate("""
      () => {
        const out = [];
        const radios = [...document.querySelectorAll('input[type="radio"]')];
        radios.forEach(r => {
          if (!r.offsetParent && !r.parentElement?.offsetParent) return;
          const lbl = document.querySelector(`label[for="${r.id}"]`) || r.closest('label');
          out.push({
            id: r.id, name: r.name, value: r.value, checked: r.checked,
            label: ((lbl && lbl.innerText) || '').slice(0, 100),
          });
        });
        return out;
      }
    """)
    for m in methods: log(f'  · {m}')

    log('[2] click radio "Tarjeta de Crédito / Débito"')
    r = page.evaluate("""
      () => {
        // Buscar radio o card cuya label/texto matche /tarjeta de cr[eé]dito|d[eé]bito/i
        const labels = [...document.querySelectorAll('label')];
        const lbl = labels.find(l => /tarjeta de cr[eé]dito|cr[eé]dito\s*\/\s*d[eé]bito|tarjeta.*d[eé]bito/i.test(l.textContent||''));
        if (lbl) {
          const radio = lbl.querySelector('input[type="radio"]') || document.getElementById(lbl.getAttribute('for')||'');
          if (radio) { radio.scrollIntoView({block:'center'}); radio.click(); return { via:'label', label: (lbl.textContent||'').slice(0,80), checked: radio.checked }; }
          lbl.click();
          return { via:'label_click_only', label: (lbl.textContent||'').slice(0,80) };
        }
        // Fallback: buscar radio por value/name
        const radios = [...document.querySelectorAll('input[type="radio"]')];
        const card = radios.find(r => /credit|card|tarjeta/i.test((r.value||'') + (r.name||'') + (r.id||'')));
        if (card) { card.scrollIntoView({block:'center'}); card.click(); return { via:'radio_attr', value: card.value, name: card.name, checked: card.checked }; }
        return { found: false };
      }
    """)
    log(f'  result: {r}')
    page.wait_for_timeout(2500)
    shot(page, '26_tarjeta_selected')

    log(f'  url: {page.url[:140]}')

    log('[3] inspeccionar campos tarjeta + botón final')
    info = page.evaluate("""
      () => {
        const inputs = [...document.querySelectorAll('input')]
          .filter(i => i.offsetParent && i.type !== 'hidden')
          .slice(0, 20)
          .map(i => ({ id: i.id.slice(0,40), name: i.name.slice(0,40), type: i.type,
                        placeholder: (i.placeholder||'').slice(0,40), value: (i.value||'').slice(0,30) }));
        const btns = [...document.querySelectorAll('button')]
          .filter(b => b.offsetParent && (b.innerText||'').trim().length > 0)
          .slice(0, 15)
          .map(b => ({ text: (b.innerText||'').trim().slice(0,60), disabled: b.disabled,
                        testid: b.getAttribute('data-testid')||'' }));
        return { inputs, btns };
      }
    """)
    log(f'  inputs ({len(info["inputs"])}):')
    for i in info['inputs']: log(f'    · {i}')
    log(f'  buttons ({len(info["btns"])}):')
    for b in info['btns']:
        log(f'    {"🔒" if b["disabled"] else "🟢"} "{b["text"]}" testid={b["testid"]}')

    shot(page, '27_FINAL_with_card_form')
    log('=== STOP — método tarjeta seleccionado, sin clickear botón final ===')
