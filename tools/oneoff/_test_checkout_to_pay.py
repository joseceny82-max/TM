"""Continuar checkout: rellenar Dirección de Entrega → click "Guardar y continuar" (azul)
→ click "Ir al pago". STOP justo antes de "Pagar"."""
import sys, io, os, time, json, re, requests, random
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
os.chdir(r'C:\Users\user\TM\proyecto')
from playwright.sync_api import sync_playwright

PORT = 57511
SHOTS = r'C:\Users\user\TM\proyecto\datos\buy\seatmap_visibilidad'

ADDR = {
    'address1': 'Avenida del Puerto 102',
    'address2': '',
    'city':     'Murcia',
    'zip':      '30283',
    'province': 'Murcia',
}

def log(msg): print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)
def shot(page, name):
    p = os.path.join(SHOTS, f'{int(time.time())}_{name}.png')
    try: page.screenshot(path=p, full_page=False); log(f'  📸 {os.path.basename(p)}')
    except Exception as e: log(f'  shot_err {e}')

def human_fill(page, sel, val):
    el = page.locator(sel).first
    el.scroll_into_view_if_needed(timeout=3000)
    el.click()
    page.keyboard.press('Control+A'); page.keyboard.press('Delete')
    page.wait_for_timeout(150)
    for ch in val:
        el.type(ch, delay=random.randint(60, 130))
    page.wait_for_timeout(300)

v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=5).json()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
    ctx = b.contexts[0]
    page = next((p for p in ctx.pages if 'ticketmaster' in (p.url or '')), ctx.pages[0])
    page.bring_to_front()
    log(f'url: {page.url[:120]}')
    shot(page, '08_before_addr')

    log('[1] inspect inputs')
    inputs = page.evaluate("""
      () => {
        const out = [];
        document.querySelectorAll('input, select, textarea').forEach(el => {
          if (!el.offsetParent) return;
          let lblTxt = '';
          if (el.id) {
            const l = document.querySelector(`label[for="${el.id}"]`);
            if (l) lblTxt = (l.innerText||'').trim();
          }
          if (!lblTxt) {
            const wrap = el.closest('[class*="formField"], [class*="form-field"], div, fieldset');
            if (wrap) lblTxt = ((wrap.querySelector('label')||{}).innerText||'').trim();
          }
          out.push({
            tag: el.tagName.toLowerCase(), type: el.type || '',
            name: el.name || '', id: el.id || '',
            placeholder: el.placeholder || '', value: el.value || '',
            label: lblTxt.slice(0, 60),
          });
        });
        return out;
      }
    """)
    for i, x in enumerate(inputs):
        log(f'  [{i}] {x["tag"]}.{x["type"]} id={x["id"]} name={x["name"]} label="{x["label"]}" value="{x["value"][:30]}"')

    def find_input(label_re):
        for x in inputs:
            if re.search(label_re, x['label'], re.I):
                if x['id']: return f'[id="{x["id"]}"]'   # id contiene `:` → no usar # selector
                if x['name']: return f'[name="{x["name"]}"]'
        return None

    sel_addr1 = find_input(r'direcci[oó]n\s*1|^direcci[oó]n$')
    sel_city  = find_input(r'ciudad')
    sel_zip   = find_input(r'c[oó]digo postal|^cp$')
    sel_prov  = find_input(r'provincia')

    log(f'\n[2] selectores → addr1={sel_addr1} city={sel_city} zip={sel_zip} prov={sel_prov}')

    log('[3] rellenar (humano)')
    if sel_addr1:
        human_fill(page, sel_addr1, ADDR['address1']); log(f'  ✓ Dirección 1 = "{ADDR["address1"]}"')
    if sel_city:
        human_fill(page, sel_city, ADDR['city']); log(f'  ✓ Ciudad = "{ADDR["city"]}"')
    if sel_zip:
        human_fill(page, sel_zip, ADDR['zip']); log(f'  ✓ CP = "{ADDR["zip"]}"')

    if sel_prov:
        try:
            tag = page.locator(sel_prov).evaluate('el => el.tagName.toLowerCase()')
            if tag == 'select':
                page.locator(sel_prov).select_option(label=ADDR['province'])
                log(f'  ✓ Provincia (select) = "{ADDR["province"]}"')
            else:
                human_fill(page, sel_prov, ADDR['province'])
                log(f'  ✓ Provincia (input) = "{ADDR["province"]}"')
        except Exception as e:
            log(f'  prov err: {e}')

    page.wait_for_timeout(800)
    shot(page, '09_addr_filled')

    log('[4] click botón AZUL "Guardar y continuar"')
    try:
        btn = page.get_by_role('button', name=re.compile(r'^guardar y continuar$', re.I)).first
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(force=True, timeout=5000)
        log('  ✓ click Guardar y continuar')
        page.wait_for_timeout(5000)
    except Exception as e:
        log(f'  ❌ guardar err: {e}')
    shot(page, '10_after_guardar')

    log('[5] check checkbox CONDICIONES DE COMPRA si aparece')
    try:
        cond = page.evaluate("""
          () => {
            const labels = [...document.querySelectorAll('label')];
            const lbl = labels.find(l => /condiciones de compra|t[eé]rminos|acepto/i.test(l.textContent||''));
            if (!lbl) return { found: false };
            const cb = lbl.querySelector('input[type="checkbox"]') ||
                       document.getElementById(lbl.getAttribute('for')||'');
            if (cb && !cb.checked) cb.click();
            return { found: true, checked: cb ? cb.checked : null };
          }
        """)
        log(f'  cond: {cond}')
        page.wait_for_timeout(800)
    except Exception as e: log(f'  cond err: {e}')

    log('[6] esperar "Ir al pago" enabled (max 15s)')
    pago_ready = False
    for k in range(15):
        try:
            state = page.evaluate("""
              () => {
                const btns = [...document.querySelectorAll('button')];
                const b = btns.find(x => /^ir al pago$/i.test((x.innerText||'').trim()));
                if (!b) return { found: false };
                const r = b.getBoundingClientRect();
                return { found: true, disabled: b.disabled, visible: r.width > 0 };
              }
            """)
            if state.get('found') and not state.get('disabled') and state.get('visible'):
                pago_ready = True
                log(f'  ✓ "Ir al pago" enabled tras {k+1}s'); break
        except Exception: pass
        page.wait_for_timeout(1000)
    if not pago_ready:
        log('  ⚠ no enabled — clicaré igualmente')

    log('[7] click "Ir al pago"')
    try:
        btn = page.get_by_role('button', name=re.compile(r'^ir al pago$', re.I)).first
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(force=True, timeout=5000)
        log('  ✓ click Ir al pago')
        page.wait_for_timeout(8000)
    except Exception as e:
        log(f'  ❌ ir al pago err: {e}')
    shot(page, '11_payment_screen')

    log(f'  url tras Ir al pago: {page.url[:140]}')

    log('[8] localizar botón "Pagar" final (NO clickear)')
    pay_btn = page.evaluate("""
      () => {
        const btns = [...document.querySelectorAll('button, [role="button"]')];
        const pays = btns
          .filter(b => /^(pagar|pagar ahora|completar pago|finalizar pago|pay( now)?)/i.test((b.innerText||'').trim()))
          .filter(b => b.offsetParent !== null);
        return pays.slice(0, 5).map(b => ({
          text: (b.innerText||'').trim().slice(0, 60),
          disabled: b.disabled, testid: b.getAttribute('data-testid') || '',
        }));
      }
    """)
    if pay_btn:
        log('  ✓ botón(es) Pagar detectado(s):')
        for p in pay_btn:
            mark = '🔒 disabled' if p['disabled'] else '🟢 ENABLED'
            log(f'    {mark}  "{p["text"]}" testid={p["testid"]}')
    else:
        log('  ⚠ aún no aparece "Pagar" (puede requerir tarjeta primero)')

    shot(page, '12_FINAL_stop_before_pagar')
    log('\n=== STOP — justo antes del botón Pagar ===')
