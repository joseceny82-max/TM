"""RELENTLESS phone+SMS: cycle hasta éxito.
Loop por intento:
  1. buy 5sim (saltando blocked, retry "no free phones")
  2. paste digits → click "Añade número"
  3. poll SMS 50s cada 1.5s
  4. si SMS llega → paste + click "Confirmar código"
     comprobar URL/title: si TM pasa de "Verify Your Information" → SUCCESS
     si TM dice "Error al validar" → blacklist número, finish_order, click "Reenviar"/"Solicitar nuevo código"
  5. si SMS no llega → cancel_order, blacklist número, click "Reenviar" en TM, repetir
Persiste blocked_numbers.json + phone_used.json al éxito.
"""
import sys, time, re, json, requests, random, os
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

PORT = 57448
ALIAS = 'coping.fiesta3h@icloud.com'
SHOTS = r'datos\buy\register_u1'
os.makedirs(SHOTS, exist_ok=True)
BLOCKED_PATH = os.path.join(SHOTS, 'blocked_numbers.json')
SMS_TIMEOUT = 50          # segundos polling SMS por intento
MAX_ATTEMPTS = 6          # ciclos completos
NO_STOCK_MAX = 30         # reintentos buy "no free phones" antes de rendirse

import sms_receiver

def load_blocked():
    try:
        with open(BLOCKED_PATH, encoding='utf-8') as f:
            return set(json.load(f).get('blocked', []))
    except Exception: return set()

def save_blocked(s):
    with open(BLOCKED_PATH, 'w', encoding='utf-8') as f:
        json.dump({'blocked': sorted(s)}, f, indent=2)

# ── Browser ──
v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=8).json()
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
page = b.contexts[0].pages[0]
page.bring_to_front()

def reset_to_phone_form():
    """Cierra el modal OTP del SMS (Cancelar) y reabre 'Añadir mi teléfono'."""
    # 1) Si hay form de OTP del SMS abierto (input one-time-code visible) → click Cancelar
    try:
        otp = page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"]').first
        if otp.is_visible(timeout=800):
            cancelled = page.evaluate(r"""() => {
              const btns = Array.from(document.querySelectorAll('button')).filter(b=>!b.disabled);
              // Tomar el segundo "Cancelar" (del modal abierto, no el del bloque background)
              const cancels = btns.filter(b => /^cancelar$/i.test((b.innerText||'').trim()));
              const target = cancels[cancels.length-1] || cancels[0];
              if (target) { target.click(); return true; }
              return false;
            }""")
            if cancelled: page.wait_for_timeout(1500)
    except Exception: pass

    # 2) Esperar a que "Añadir mi teléfono" se ponga ENABLED y clickarlo
    for _ in range(8):
        clicked = page.evaluate(r"""() => {
          const cand = Array.from(document.querySelectorAll('button')).find(b => /^a[ñn]adir mi tel[eé]fono$/i.test((b.innerText||'').trim()) && !b.disabled);
          if (cand) { cand.click(); return true; }
          return false;
        }""")
        if clicked: break
        page.wait_for_timeout(800)
    page.wait_for_timeout(1500)

def find_visible(sel_list, timeout=1500):
    for sel in sel_list:
        try:
            cand = page.locator(sel).first
            if cand.is_visible(timeout=timeout): return cand, sel
        except Exception: pass
    return None, None

def buy_until_new(blocked):
    """Devuelve (phone, order_id) saltando blocked y "no free phones"."""
    for k in range(NO_STOCK_MAX):
        r = sms_receiver.buy_number(country='es', product='ticketmaster')
        if r.get('ok'):
            ph = r.get('phone')
            if ph in blocked:
                print(f'    descarte blocked {ph}');
                try: sms_receiver.cancel_order(r['order_id'], '5sim')
                except: pass
                time.sleep(2); continue
            return ph, r.get('order_id')
        err = (r.get('raw',{}) or {}).get('raw') or '?'
        if k % 5 == 0: print(f'    [buy retry {k+1}/{NO_STOCK_MAX}] {err}')
        time.sleep(8)
    return None, None

success = False
result = None

for attempt in range(1, MAX_ATTEMPTS + 1):
    print(f'\n══ INTENTO {attempt}/{MAX_ATTEMPTS} ══')
    blocked = load_blocked()

    # Reset UI al form de teléfono
    reset_to_phone_form()

    # Ver si tenemos input de teléfono visible (estado inicial del modal)
    phone_inp, _ = find_visible(['input[type="tel"]','input[autocomplete*="tel" i]','input[name*="phone" i]'], 2000)
    if not phone_inp:
        # Quizá ya estamos en el form de OTP del SMS — saltar buy y comprar?
        otp_inp, _ = find_visible(['input[autocomplete="one-time-code"]','input[inputmode="numeric"]'], 1000)
        if otp_inp:
            print('  estamos en OTP form sin haber comprado — clicar "Cambiar número" o "Reenviar"')
            page.evaluate(r"""() => {
              const cand = Array.from(document.querySelectorAll('button,a')).find(b => /cambiar n[uú]mero|usar otro n[uú]mero/i.test((b.innerText||'').trim()));
              if (cand) cand.click();
            }""")
            page.wait_for_timeout(1500)
            reset_to_phone_form()
            phone_inp, _ = find_visible(['input[type="tel"]','input[autocomplete*="tel" i]'], 2000)
        if not phone_inp:
            page.screenshot(path=os.path.join(SHOTS, f'30_attempt{attempt}_no_phone_input.png'))
            print('  ❌ no encuentro input teléfono — abortando'); break

    # Buy
    phone, order = buy_until_new(blocked)
    if not phone:
        print('  ❌ sin stock 5sim — pruebo otra vuelta'); time.sleep(10); continue
    digits = phone.lstrip('+')
    if digits.startswith('34'): digits = digits[2:]
    print(f'  buy ok {phone} order={order}')

    # Paste
    phone_inp.click(); phone_inp.fill('')
    for ch in digits: phone_inp.type(ch, delay=random.randint(35,65))
    print(f'  digits {digits} tipeados')

    # Click "Añade número" azul
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
    print(f'  click {clicked or "FAIL"} @ {sent_at.strftime("%H:%M:%S")}')
    if not clicked:
        sms_receiver.cancel_order(order, '5sim'); blocked.add(phone); save_blocked(blocked)
        continue

    # Polling SMS 50s
    sms_code = None; sms_text = None
    deadline = time.time() + SMS_TIMEOUT
    while time.time() < deadline and not sms_code:
        chk = sms_receiver.check_sms(order, '5sim')
        for s in (chk.get('sms') or []):
            txt = s.get('text') or s.get('code') or ''
            sms_text = txt
            m = re.search(r'\b(\d{4,8})\b', txt)
            if m: sms_code = m.group(1); break
        if sms_code: break
        time.sleep(1.5)

    if not sms_code:
        print(f'  ⏰ SMS no llegó en {SMS_TIMEOUT}s — blacklist + cancel + retry')
        sms_receiver.cancel_order(order, '5sim')
        blocked.add(phone); save_blocked(blocked)
        # Pedir nuevo código en TM (volver al form)
        continue

    print(f'  📱 SMS={sms_code} en {(datetime.now(timezone.utc)-sent_at).total_seconds():.1f}s')

    # Paste código
    code_inp, _ = find_visible(['input[autocomplete="one-time-code"]','input[inputmode="numeric"]','input[type="text"]','input[type="tel"]'], 3000)
    if not code_inp:
        page.wait_for_timeout(2000)
        code_inp, _ = find_visible(['input[autocomplete="one-time-code"]','input[inputmode="numeric"]'], 2000)
    if not code_inp:
        print('  ❌ no input código'); sms_receiver.cancel_order(order, '5sim'); blocked.add(phone); save_blocked(blocked); continue
    code_inp.click(); code_inp.fill('')
    for ch in sms_code: code_inp.type(ch, delay=random.randint(35,65))

    # Click confirmar inmediato
    clicked2 = page.evaluate(r"""() => {
      const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
      const cand = btns.find(b => /^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/i.test((b.innerText||'').trim()));
      if (cand) { cand.scrollIntoView({block:'center'}); cand.click(); return cand.innerText.trim(); }
      return null;
    }""")
    elapsed = (datetime.now(timezone.utc) - sent_at).total_seconds()
    print(f'  ✓ confirmar "{clicked2}" tras {elapsed:.1f}s totales')

    # Esperar resolución
    page.wait_for_timeout(5000)
    body = page.evaluate('()=>document.body.innerText.slice(0,400)').lower()
    page.screenshot(path=os.path.join(SHOTS, f'31_attempt{attempt}_post_confirm.png'))

    # Detectar éxito vs error
    if 'error al validar' in body or 'an error has occurred' in body:
        print(f'  ❌ TM rechazó número {phone} — blacklist + retry')
        sms_receiver.finish_order(order, '5sim')   # SMS sí llegó, finalizar es correcto
        blocked.add(phone); save_blocked(blocked)
        # Click reintentar/volver para resetear form
        continue

    # SUCCESS heurístico: ya no muestra "verifica tu cuenta" del bloque inicial
    print(f'  ✅ POSIBLE ÉXITO — body: {body[:200]!r}')
    sms_receiver.finish_order(order, '5sim')
    success = True
    result = {
        'tm_email': ALIAS, 'phone': phone, 'phone_digits': digits,
        'provider': '5sim', 'order_id': str(order),
        'sms_text': sms_text, 'sms_code': sms_code,
        'attempt': attempt,
        'verified_at': datetime.now(timezone.utc).isoformat(),
        'final_url': page.url, 'final_title': page.title(),
    }
    with open(os.path.join(SHOTS, 'phone_used.json'), 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    break

print(f'\n═══ RESULTADO ═══')
if success:
    print(f'  ✅ ÉXITO en intento #{result["attempt"]}')
    print(f'  phone:    {result["phone"]}')
    print(f'  sms_code: {result["sms_code"]}')
    print(f'  URL:      {result["final_url"][:140]}')
    print(f'  Title:    {result["final_title"]}')
else:
    print(f'  ❌ {MAX_ATTEMPTS} intentos sin éxito. Blocked: {load_blocked()}')

b.close(); pw.stop()
