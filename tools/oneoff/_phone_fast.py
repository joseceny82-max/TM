"""SPEED MODE: phone+SMS en ≤30s desde click "Añade número" hasta confirmar."""
import sys, time, re, json, requests, random, socket, os
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

PORT = 57448
ALIAS = 'coping.fiesta3h@icloud.com'
SHOTS = r'datos\buy\register_u1'
os.makedirs(SHOTS, exist_ok=True)

v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=8).json()
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
page = b.contexts[0].pages[0]
page.bring_to_front()
print(f't0 URL: {page.url[:100]}')

# Si quedó en pantalla de error, click "Volver" / "Reintentar" / refresh
try:
    page.evaluate(r"""() => {
      const back = Array.from(document.querySelectorAll('button,a')).find(b => /reintentar|volver|try again|retry/i.test((b.innerText||'').trim()));
      if (back) back.click();
    }""")
    page.wait_for_timeout(1000)
except Exception: pass

# Reabrir form si hace falta — click "Añadir mi teléfono"
try:
    page.evaluate(r"""() => {
      const cand = Array.from(document.querySelectorAll('button')).find(b => /a[ñn]adir mi tel[eé]fono/i.test((b.innerText||'').trim()) && !b.disabled);
      if (cand) cand.click();
    }""")
    page.wait_for_timeout(1500)
except Exception: pass

import sms_receiver
# Bloquear números ya rechazados por TM en intentos previos
BLOCKED = {'+34642665536'}
try:
    blocked_path = os.path.join(SHOTS, 'blocked_numbers.json')
    if os.path.isfile(blocked_path):
        with open(blocked_path, encoding='utf-8') as f:
            BLOCKED.update(json.load(f).get('blocked', []))
except Exception: pass
print(f'[blocked] {len(BLOCKED)} número(s) descartados: {sorted(BLOCKED)}')

# Loop persistente: hasta 20 intentos × ~10s = ~3 min
buy = None
PHONE = None
for attempt in range(1, 25):
    print(f'[buy {attempt}/24]', end=' ', flush=True)
    cand = sms_receiver.buy_number(country='es', product='ticketmaster')
    if cand.get('ok'):
        ph = cand.get('phone')
        if ph in BLOCKED:
            print(f'descartado (blocked) {ph} → cancelling')
            try: sms_receiver.cancel_order(cand.get('order_id'), '5sim')
            except Exception: pass
            time.sleep(3)
            continue
        buy = cand; PHONE = ph; print(f'OK {PHONE} order={cand.get("order_id")}'); break
    err = cand.get('raw', {}).get('raw') or cand.get('error') or '?'
    print(f'fail: {err}')
    # Si "no free phones" → backoff
    time.sleep(10)
if not buy:
    print('❌ 5sim sin stock tras 24 intentos — abortando')
    b.close(); pw.stop(); sys.exit(2)

ORDER_ID = buy.get('order_id'); PROVIDER = buy.get('provider') or '5sim'
DIGITS = (PHONE.lstrip('+'))[2:] if PHONE.lstrip('+').startswith('34') else PHONE.lstrip('+')

# Localizar input teléfono y pegar
inp = None
for sel in ['input[type="tel"]', 'input[autocomplete*="tel" i]', 'input[name*="phone" i]']:
    try:
        cand = page.locator(sel).first
        if cand.is_visible(timeout=1500): inp = cand; break
    except: pass
if not inp:
    print('no phone input'); sms_receiver.cancel_order(ORDER_ID, PROVIDER); b.close(); pw.stop(); sys.exit(3)
inp.click(); inp.fill('')
# Tipeo rápido (delay corto pero no 0 para no levantar bot detection)
for ch in DIGITS: inp.type(ch, delay=random.randint(35,65))
print(f'  digits {DIGITS} tipeados')

# Click "Añade número" azul (rgb 2,77,223 / 2,108,223)
sent_at = datetime.now(timezone.utc)
clicked = page.evaluate(r"""() => {
  const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
  const isBlue = b => {
    const bg = getComputedStyle(b).backgroundColor;
    return /rgb\(\s*2\s*,\s*77\s*,\s*223\s*\)/i.test(bg) || /rgb\(\s*2\s*,\s*108\s*,\s*223\s*\)/i.test(bg);
  };
  const cand = btns.find(b => /^(a[ñn]ade n[uú]mero|a[ñn]ade tu n[uú]mero)$/i.test((b.innerText||'').trim()) && isBlue(b))
            || btns.find(b => /a[ñn]ade n[uú]mero/i.test((b.innerText||'').trim()) && isBlue(b));
  if (cand) { cand.scrollIntoView({block:'center'}); cand.click(); return cand.innerText.trim(); }
  return null;
}""")
sent_at = datetime.now(timezone.utc)
print(f'  click {clicked} @ {sent_at.strftime("%H:%M:%S.%f")[:-3]}')

# Polling SMS cada 1.5s × 120s — apenas llega, paste & confirm sin esperar
deadline = time.time() + 120
sms_code = None; sms_text = None
poll_n = 0
while time.time() < deadline and not sms_code:
    poll_n += 1
    chk = sms_receiver.check_sms(ORDER_ID, PROVIDER)
    sms_list = chk.get('sms') or []
    if sms_list:
        for s in reversed(sms_list):
            txt = s.get('text') or s.get('code') or ''
            sms_text = txt
            m = re.search(r'\b(\d{4,8})\b', txt)
            if m: sms_code = m.group(1); break
    if sms_code: break
    time.sleep(1.5)

if not sms_code:
    print('SMS timeout'); sms_receiver.cancel_order(ORDER_ID, PROVIDER); b.close(); pw.stop(); sys.exit(4)
arrived = datetime.now(timezone.utc)
delay_s = (arrived - sent_at).total_seconds()
print(f'  📱 SMS={sms_code} ({sms_text!r}) tras {delay_s:.1f}s')

# Localizar input código rápidamente
inp2 = None
for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]', 'input[type="text"]', 'input[type="tel"]']:
    try:
        cand = page.locator(sel).first
        if cand.is_visible(timeout=800): inp2 = cand; break
    except: pass
if not inp2:
    # esperar un poco si TM está montando el form de OTP
    page.wait_for_timeout(1500)
    for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]']:
        try:
            cand = page.locator(sel).first
            if cand.is_visible(timeout=1000): inp2 = cand; break
        except: pass
if not inp2:
    print('no code input'); sms_receiver.finish_order(ORDER_ID, PROVIDER); b.close(); pw.stop(); sys.exit(5)

inp2.click(); inp2.fill('')
for ch in sms_code: inp2.type(ch, delay=random.randint(35,65))
print(f'  ✓ code typed')

# Click confirmar inmediatamente
clicked2 = page.evaluate(r"""() => {
  const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
  const cand = btns.find(b => /^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/i.test((b.innerText||'').trim()));
  if (cand) { cand.scrollIntoView({block:'center'}); cand.click(); return cand.innerText.trim(); }
  return null;
}""")
print(f'  ✓ click confirmar: {clicked2}')
total = (datetime.now(timezone.utc) - sent_at).total_seconds()
print(f'\n[T_TOTAL click→confirmar] {total:.1f}s')

page.wait_for_timeout(5000)
page.screenshot(path=os.path.join(SHOTS, '25_post_phone_fast.png'))

print(f'  finish: {sms_receiver.finish_order(ORDER_ID, PROVIDER)}')

# Save info
out = {
  'tm_email': ALIAS, 'phone': PHONE, 'phone_digits': DIGITS,
  'provider': PROVIDER, 'order_id': str(ORDER_ID),
  'sms_text': sms_text, 'sms_code': sms_code,
  't_send_to_confirm_s': total,
  'verified_at': datetime.now(timezone.utc).isoformat(),
  'final_url': page.url, 'final_title': page.title(),
}
with open(os.path.join(SHOTS, 'phone_used.json'), 'w', encoding='utf-8') as f:
    json.dump(out, f, indent=2, ensure_ascii=False)

print(f'\nURL final: {page.url[:140]}')
print(f'Title:     {page.title()}')
body = page.evaluate('()=>document.body.innerText.slice(0,500)')
print(f'BODY: {body}')
b.close(); pw.stop()
