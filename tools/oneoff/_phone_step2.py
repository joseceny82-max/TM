"""Compra número 5sim ES ticketmaster, lo pega en TM, espera SMS, inyecta código.
Guarda info usada en datos/buy/register_u1/phone_used.json."""
import sys, time, re, json, requests, random, socket, os
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

PORT = 57448
ALIAS = 'coping.fiesta3h@icloud.com'
SHOTS = r'datos\buy\register_u1'
os.makedirs(SHOTS, exist_ok=True)

# ── 1. Conectar al browser ──
v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=8).json()
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
page = b.contexts[0].pages[0]
page.bring_to_front()
print(f'URL: {page.url[:120]}')

# ── 2. Comprar número en 5sim ES ticketmaster ──
import sms_receiver
print('\n[1] sms_receiver.buy_number(es, ticketmaster)')
buy = sms_receiver.buy_number(country='es', product='ticketmaster')
print(f'  buy: {json.dumps(buy, indent=2, default=str)[:600]}')
if not buy.get('ok'):
    page.screenshot(path=os.path.join(SHOTS, '20_buy_fail.png'))
    print('❌ buy_number falló — abortando'); b.close(); pw.stop(); sys.exit(2)

PHONE = buy.get('phone') or buy.get('number')
ORDER_ID = buy.get('order_id') or buy.get('id')
PROVIDER = buy.get('provider') or '5sim'
print(f'  ✓ phone={PHONE}  order_id={ORDER_ID}')

# ── 3. Pegar teléfono en el form ──
# El número debe ir SIN prefijo +34 si TM ya tiene país=ES seleccionado
def _just_digits(p):
    p = str(p or '').strip()
    if p.startswith('+'): p = p[1:]
    if p.startswith('34') and len(p) >= 11: return p[2:]
    return p

DIGITS = _just_digits(PHONE)
print(f'  digits a pegar: {DIGITS}')

# Asegurar país ES seleccionado en country picker (si lo hay)
try:
    page.evaluate(r"""() => {
      // Country picker: clicar y elegir España si aparece
      const trig = Array.from(document.querySelectorAll('[aria-label*="country" i],button,div')).find(b=>/country picker|c[oó]digo de pa[ií]s|^es$|spain|espa[ñn]a/i.test(b.innerText||b.getAttribute('aria-label')||''));
      // Si ya pone "España" no hacer nada
    }""")
except Exception: pass

inp = None
for sel in ['input[type="tel"]', 'input[autocomplete*="tel" i]', 'input[name*="phone" i]', 'input[id*="phone" i]', 'input[type="text"]']:
    try:
        cand = page.locator(sel).first
        if cand.is_visible(timeout=1500):
            inp = cand; print(f'  ✓ input vía {sel}'); break
    except Exception: pass

if not inp:
    page.screenshot(path=os.path.join(SHOTS, '21_no_phone_input.png'))
    print('❌ no encuentro input teléfono')
    sms_receiver.cancel_order(ORDER_ID, PROVIDER)
    b.close(); pw.stop(); sys.exit(3)

inp.click(); inp.fill('')
for ch in DIGITS:
    inp.type(ch, delay=random.randint(60,110))
print(f'  ✓ tipeado {DIGITS}')
page.wait_for_timeout(800)
page.screenshot(path=os.path.join(SHOTS, '21_phone_typed.png'))

# ── 4. Click "Continuar" / "Enviar código" para que TM mande SMS ──
sent_at = datetime.now(timezone.utc)
clicked = page.evaluate(r"""() => {
  const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
  // Botón azul TM (rgb(2,77,223)) con texto correcto del modal de teléfono
  const isBlue = (b) => {
    const bg = getComputedStyle(b).backgroundColor;
    return /rgb\(\s*2\s*,\s*77\s*,\s*223\s*\)/i.test(bg) || /rgb\(\s*2\s*,\s*108\s*,\s*223\s*\)/i.test(bg);
  };
  // Prioridad: "Añade número" / "Añade tu número" / "Add number" en el modal
  const cand = btns.find(b => /^(a[ñn]ade n[uú]mero|a[ñn]ade tu n[uú]mero|add number|^a[ñn]adir$)$/i.test((b.innerText||'').trim()) && isBlue(b))
            || btns.find(b => /a[ñn]ade n[uú]mero|add number/i.test((b.innerText||'').trim()) && isBlue(b))
            || btns.find(b => /^(continuar|enviar c[oó]digo|send code|siguiente)$/i.test((b.innerText||'').trim()) && isBlue(b));
  if (cand) { cand.scrollIntoView({block:'center'}); cand.click(); return cand.innerText.trim(); }
  return null;
}""")
print(f'  click envío: {clicked or "NO encontrado"}')
page.wait_for_timeout(3500)
sent_at = datetime.now(timezone.utc)
print(f'  sent_at(utc)={sent_at.isoformat()}')
page.screenshot(path=os.path.join(SHOTS, '22_after_send_code.png'))

# ── 5. Polling 5sim cada 3s × 180s ──
deadline = time.time() + 180
sms_code = None; sms_text = None
poll_n = 0
while time.time() < deadline and not sms_code:
    poll_n += 1
    chk = sms_receiver.check_sms(ORDER_ID, PROVIDER)
    status = chk.get('status') or chk.get('state') or ''
    sms_list = chk.get('sms') or []
    if sms_list:
        # Buscar último con código de 4-8 dígitos
        for s in reversed(sms_list):
            txt = (s.get('text') or s.get('code') or '')
            sms_text = txt
            m = re.search(r'\b(\d{4,8})\b', txt)
            if m: sms_code = m.group(1); break
        if sms_code:
            print(f'  📱 SMS recibido (status={status}): {sms_text[:80]}')
            print(f'  📱 código extraído: {sms_code}')
            break
    if poll_n % 5 == 0:
        print(f'  …polling t+{int(time.time() - (deadline-180))}s status={status}')
    time.sleep(3)

if not sms_code:
    print('❌ SMS no llegó en 180s — cancelando orden')
    sms_receiver.cancel_order(ORDER_ID, PROVIDER)
    b.close(); pw.stop(); sys.exit(4)

# ── 6. Pegar código en TM ──
inp2 = None
for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]', 'input[type="tel"]', 'input[type="text"]']:
    try:
        cand = page.locator(sel).first
        if cand.is_visible(timeout=1500):
            inp2 = cand; print(f'  ✓ input código vía {sel}'); break
    except Exception: pass

if inp2 is None:
    for _ in range(5):
        page.wait_for_timeout(2000)
        for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]']:
            try:
                cand = page.locator(sel).first
                if cand.is_visible(timeout=1000): inp2 = cand; break
            except: pass
        if inp2: break

if inp2 is None:
    page.screenshot(path=os.path.join(SHOTS, '23_no_code_input.png'))
    print('❌ no encuentro input para código SMS')
    sms_receiver.finish_order(ORDER_ID, PROVIDER)
    b.close(); pw.stop(); sys.exit(5)

inp2.click(); inp2.fill('')
for ch in sms_code:
    inp2.type(ch, delay=random.randint(60,110))
print(f'  ✓ código {sms_code} tipeado')
page.wait_for_timeout(1000)

clicked2 = page.evaluate(r"""() => {
  const btns = Array.from(document.querySelectorAll('button'));
  const cand = btns.find(b => /^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/i.test((b.innerText||'').trim()) && !b.disabled);
  if (cand) { cand.click(); return cand.innerText.trim(); }
  return null;
}""")
print(f'  click confirmar: {clicked2 or "NO encontrado"}')
page.wait_for_timeout(6000)
page.screenshot(path=os.path.join(SHOTS, '24_post_phone_ok.png'))

# ── 7. Cerrar orden 5sim ──
print(f'\n[7] finish_order')
print(f'  finish: {sms_receiver.finish_order(ORDER_ID, PROVIDER)}')

# ── 8. Guardar info ──
out = {
    'tm_email': ALIAS,
    'phone': PHONE,
    'phone_digits': DIGITS,
    'provider': PROVIDER,
    'order_id': str(ORDER_ID),
    'sms_text': sms_text,
    'sms_code': sms_code,
    'verified_at': datetime.now(timezone.utc).isoformat(),
    'final_url': page.url,
    'final_title': page.title(),
}
out_path = os.path.join(SHOTS, 'phone_used.json')
with open(out_path, 'w', encoding='utf-8') as f: json.dump(out, f, indent=2, ensure_ascii=False)
print(f'\nGuardado: {out_path}')

print(f'\n═══ RESUMEN ═══')
print(f'  phone:    {PHONE}')
print(f'  order:    {ORDER_ID}')
print(f'  sms_code: {sms_code}')
print(f'  URL final:{page.url[:140]}')
print(f'  Title:    {page.title()}')
print(f'  BODY 400c: {page.evaluate("()=>document.body.innerText.slice(0,400)")}')
b.close(); pw.stop()
