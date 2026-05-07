"""Registro completo TM con perfil ES NUEVO + email OTP fast + phone 5sim relentless.
Uso: python _register_full.py [TM_ROW]   (default 2)
"""
import sys, time, re, json, requests, random, os, csv, socket
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

ROW = int(sys.argv[1]) if len(sys.argv) > 1 else 2
SHOTS = rf'datos\buy\register_u{ROW}'
os.makedirs(SHOTS, exist_ok=True)
BLOCKED_PATH = os.path.join(SHOTS, 'blocked_numbers.json')

with open('plantilla_tm.csv', encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
u = rows[ROW-1]

EMAIL = u['Email_TM']; PWD = u['Password_TM']
MADRE = u['Email_Contacto']; MADRE_PWD = u['Password_Email']
ZIP = u['ZIP']
NAME = u['Nombre']; LAST = u['Apellidos']

# Override fijo solicitado por usuario para U2: Julio Martin Perez
def looks_garbage(s):
    s = (s or '').strip()
    if len(s) < 3: return True
    if not re.match(r'^[A-Za-zÁÉÍÓÚáéíóúÑñ ]+$', s): return True
    return bool(re.match(r'^([a-z])\1+$', s.lower())) or bool(re.match(r'^[qwerty]+$', s.lower()))
if looks_garbage(NAME) or looks_garbage(LAST):
    NAME, LAST = 'Julio', 'Martin Perez'
    print(f'[override] Nombre/Apellidos basura → {NAME} {LAST}')

print(f'═══ REGISTER U{ROW} — {EMAIL} | {NAME} {LAST} | ZIP {ZIP} ═══')

import bot_orchestrator as bo
import sms_receiver, cookie_warmer

def load_blocked():
    try:
        with open(BLOCKED_PATH, encoding='utf-8') as f: return set(json.load(f).get('blocked', []))
    except: return set()
def save_blocked(s):
    with open(BLOCKED_PATH, 'w', encoding='utf-8') as f: json.dump({'blocked': sorted(s)}, f, indent=2)

# ── 1. Perfil ES nuevo ──
print('\n[1] perfil ES nuevo (excluyendo P-170131 quemado)')
prof = bo._pick_fresh_profile(country_code='ES', quemados={'P-170131'})
if not prof:
    print('❌ no hay perfil ES disponible'); sys.exit(1)
port = bo._start_profile(prof['env_id'])
print(f'  perfil={prof["profile"]} (IP {prof["ip"]}) port={port}')
time.sleep(5)

# ── 2. Cookie warming ──
warm = cookie_warmer.warm_cookies(domain_id='es', pages=4, timeout=10.0)
print(f'  warmer ok={warm.get("ok")} cookies={len(warm.get("cookies") or {})}')

# ── 3. Conectar Playwright ──
v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
ctx = b.contexts[0]
ctx.add_init_script("""
  if (window.Notification) {
    Notification.requestPermission = () => Promise.resolve('denied');
    try { Object.defineProperty(Notification,'permission',{get:()=>'denied'}); } catch(e){}
  }
  if (navigator.serviceWorker?.register) navigator.serviceWorker.register = () => Promise.reject(new Error('blocked'));
""")
inj = 0
for n,vc in (warm.get('cookies') or {}).items():
    try:
        ctx.add_cookies([{'name':n,'value':str(vc),'domain':'.ticketmaster.es','path':'/'}])
        inj += 1
    except: pass
print(f'  ✓ {inj} cookies inyectadas')

page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.bring_to_front()

# ── 4. Goto + email + continuar ──
TM_URL = f'https://my.ticketmaster.es/?_={int(time.time())}{random.randint(1000,9999)}'
print(f'\n[4] goto TM ES')
for i in range(3):
    try: page.goto(TM_URL, wait_until='domcontentloaded', timeout=30000); break
    except Exception as e: print(f'  retry {i+1}: {e}'); time.sleep(3)
page.wait_for_timeout(7000)
for txt in ['Aceptar todas las cookies','Rechazarlas todas']:
    try:
        btn = page.get_by_text(txt, exact=False).first
        if btn.is_visible(timeout=1500): btn.click(); page.wait_for_timeout(1500); break
    except: pass

em = page.locator('input[type="email"]').first
em.wait_for(state='visible', timeout=15000)
em.click(); em.fill('')
for ch in EMAIL: em.type(ch, delay=random.randint(45,90))
print(f'  email tipeado')
page.wait_for_timeout(900)
page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
page.wait_for_timeout(7000)

title = page.title()
print(f'  Title: {title}')
mode = 'signup' if 'sign up' in title.lower() else ('signin' if 'sign in' in title.lower() else 'unknown')
print(f'  modo: {mode}')

# ── 5. Form signup ──
if mode == 'signup':
    print(f'\n[5] form signup')
    try:
        pw_el = page.locator('input[type="password"]').first
        pw_el.wait_for(state='visible', timeout=10000)
        pw_el.click(); pw_el.fill('')
        for ch in PWD: pw_el.type(ch, delay=random.randint(40,80))
        print('  ✓ password')
    except Exception as e: print(f'  pwd err: {e}')

    for sel, val, n in [
        ('input[autocomplete="given-name"]', NAME, 'first_name'),
        ('input[autocomplete="family-name"]', LAST, 'last_name'),
        ('input[autocomplete="postal-code"]', ZIP, 'zip'),
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500) and not loc.input_value().strip():
                loc.click(); loc.fill('')
                for ch in str(val): loc.type(ch, delay=random.randint(40,80))
                print(f'  ✓ {n}={val}')
        except: pass

    page.wait_for_timeout(800)
    # Acepto popup
    page.evaluate(r"""() => {const m = Array.from(document.querySelectorAll('button')).find(b => /^acepto$/i.test((b.innerText||'').trim())); if(m) m.click(); document.querySelectorAll('[id*=onetrust i]').forEach(e=>e.remove());}""")
    page.wait_for_timeout(800)
    # privacy checkboxes
    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
    page.wait_for_timeout(1200)
    for cb in page.locator('input[type="checkbox"]').all():
        try:
            if cb.is_visible(timeout=200) and not cb.is_checked(): cb.check(force=True)
        except: pass

    # Siguiente
    print('  click Siguiente')
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^siguiente$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
    for i in range(8):
        page.wait_for_timeout(1500)
        ttl = page.title().lower(); body0 = page.evaluate('()=>document.body.innerText.slice(0,200).toLowerCase()')
        if 'verify' in ttl or 'verifica' in body0:
            print(f'  ✓ verify form (t+{(i+1)*1.5:.0f}s)'); break
elif mode == 'signin':
    print('\n[5b] cuenta existe → login')
    try:
        pw_el = page.locator('input[type="password"]').first
        pw_el.wait_for(state='visible', timeout=10000)
        pw_el.click(); pw_el.fill('')
        for ch in PWD: pw_el.type(ch, delay=random.randint(40,80))
        page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/iniciar sesi[oó]n|sign in|^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
        page.wait_for_timeout(6000)
    except Exception as e: print(f'  err: {e}')

page.screenshot(path=os.path.join(SHOTS, '50_post_form.png'))

# ── 6. Email OTP fast ──
def fast_email_otp():
    body = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
    if 'verifica' not in body and 'verify' not in page.title().lower():
        return True
    print('\n[email-OTP] click "Verificar mi correo"')
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/verificar mi correo|verify my email/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
    page.wait_for_timeout(2500)
    sent_at = datetime.now(timezone.utc)
    socket.setdefaulttimeout(20)
    from imap_tools import MailBox
    deadline = time.time() + 120
    otp = None; seen = set()
    while time.time() < deadline and not otp:
        try:
            with MailBox('imap.mail.me.com').login(MADRE, MADRE_PWD) as mb:
                for m in mb.fetch(reverse=True, limit=8, mark_seen=False, bulk=False):
                    if m.uid in seen: continue
                    seen.add(m.uid)
                    msg_dt = m.date if m.date else datetime.now(timezone.utc)
                    if msg_dt.tzinfo is None: msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    else: msg_dt = msg_dt.astimezone(timezone.utc)
                    if msg_dt < sent_at: continue
                    if EMAIL.lower() not in ', '.join(m.to or []).lower(): continue
                    if 'ticketmaster' not in (m.from_ or '').lower() and 'ticketmaster' not in (m.subject or '').lower(): continue
                    g = re.search(r'\b(\d{6})\b', (m.subject or '') + ' ' + (m.text or m.html or '')[:1500])
                    if g: otp = g.group(1); break
        except Exception as e: print(f'  imap err: {e}')
        if otp: break
        time.sleep(2)
    if not otp: print('  ❌ email OTP timeout'); return False
    print(f'  📧 OTP={otp}')
    inp = None
    for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]', 'input[type="text"]']:
        try:
            cand = page.locator(sel).first
            if cand.is_visible(timeout=1500): inp = cand; break
        except: pass
    if not inp: print('  ❌ no input'); return False
    inp.click(); inp.fill('')
    for ch in otp: inp.type(ch, delay=random.randint(50,90))
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).filter(b=>!b.disabled).find(b=>/^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/i.test((b.innerText||'').trim())); if(m) m.click();}""")
    page.wait_for_timeout(5000)
    print(f'  post-OTP: {page.title()}'); return True

if not fast_email_otp():
    page.screenshot(path=os.path.join(SHOTS, '51_email_otp_fail.png'))
    print('❌ email-otp falló'); b.close(); pw.stop(); sys.exit(2)
page.screenshot(path=os.path.join(SHOTS, '52_post_email_otp.png'))

# ── 7. Phone relentless ──
print('\n[7] click "Añadir mi teléfono"')
for _ in range(8):
    clicked = page.evaluate(r"""() => {
      const cand = Array.from(document.querySelectorAll('button')).find(b => /^a[ñn]adir mi tel[eé]fono$/i.test((b.innerText||'').trim()) && !b.disabled);
      if (cand) { cand.click(); return true; } return false;
    }""")
    if clicked: break
    page.wait_for_timeout(800)
page.wait_for_timeout(2000)

phone_inp = None
for sel in ['input[type="tel"]', 'input[autocomplete*="tel" i]']:
    try:
        cand = page.locator(sel).first
        if cand.is_visible(timeout=2000): phone_inp = cand; break
    except: pass
if not phone_inp:
    page.screenshot(path=os.path.join(SHOTS, '53_no_phone_input.png'))
    print('❌ no phone input'); b.close(); pw.stop(); sys.exit(3)

SMS_TIMEOUT = 50
NO_STOCK_RETRIES = 30
success = False; result = None

for cycle in range(1, 5):
    print(f'\n══ CICLO PHONE {cycle}/4 ══')
    blocked = load_blocked()

    if not phone_inp.is_visible():
        # reset al form si vino tras error
        page.evaluate(r"""() => {
          const btns = Array.from(document.querySelectorAll('button')).filter(b=>!b.disabled);
          const cancels = btns.filter(b => /^cancelar$/i.test((b.innerText||'').trim()));
          (cancels[cancels.length-1] || cancels[0])?.click();
        }""")
        page.wait_for_timeout(1500)
        for _ in range(8):
            ok = page.evaluate(r"""() => {const c = Array.from(document.querySelectorAll('button')).find(b => /^a[ñn]adir mi tel[eé]fono$/i.test((b.innerText||'').trim()) && !b.disabled); if (c) {c.click(); return true} return false;}""")
            if ok: break
            page.wait_for_timeout(800)
        page.wait_for_timeout(1500)
        try:
            phone_inp = page.locator('input[type="tel"]').first
            phone_inp.wait_for(state='visible', timeout=3000)
        except:
            print('  ❌ no input phone'); break

    phone, order = None, None
    for k in range(NO_STOCK_RETRIES):
        r = sms_receiver.buy_number(country='es', product='ticketmaster')
        if r.get('ok'):
            ph = r.get('phone')
            if ph in blocked:
                print(f'    blocked {ph}');
                try: sms_receiver.cancel_order(r['order_id'], '5sim')
                except: pass
                time.sleep(2); continue
            phone, order = ph, r.get('order_id'); break
        if k % 5 == 0: print(f'    [buy retry {k+1}] {(r.get("raw",{}) or {}).get("raw","?")}')
        time.sleep(8)
    if not phone: print('  ❌ stock 5sim agotado'); break
    digits = phone.lstrip('+')
    if digits.startswith('34'): digits = digits[2:]
    print(f'  buy ok {phone} order={order}')

    phone_inp.click(); phone_inp.fill('')
    for ch in digits: phone_inp.type(ch, delay=random.randint(35,65))

    sent_at = datetime.now(timezone.utc)
    clicked = page.evaluate(r"""() => {
      const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
      const isBlue = b => /rgb\(\s*2\s*,\s*77\s*,\s*223\s*\)/i.test(getComputedStyle(b).backgroundColor);
      const cand = btns.find(b => /^(a[ñn]ade n[uú]mero|a[ñn]ade tu n[uú]mero)$/i.test((b.innerText||'').trim()) && isBlue(b))
                || btns.find(b => /a[ñn]ade n[uú]mero/i.test((b.innerText||'').trim()) && isBlue(b));
      if (cand) { cand.scrollIntoView({block:'center'}); cand.click(); return cand.innerText.trim(); }
      return null;
    }""")
    sent_at = datetime.now(timezone.utc)
    print(f'  click "{clicked}" @ {sent_at.strftime("%H:%M:%S")}')
    if not clicked:
        sms_receiver.cancel_order(order, '5sim'); blocked.add(phone); save_blocked(blocked); continue

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
        print(f'  ⏰ SMS timeout {SMS_TIMEOUT}s')
        sms_receiver.cancel_order(order, '5sim')
        blocked.add(phone); save_blocked(blocked); continue
    print(f'  📱 SMS={sms_code} ({(datetime.now(timezone.utc)-sent_at).total_seconds():.1f}s)')

    code_inp = None
    for sel in ['input[autocomplete="one-time-code"]','input[inputmode="numeric"]','input[type="text"]','input[type="tel"]']:
        try:
            cand = page.locator(sel).first
            if cand.is_visible(timeout=1500): code_inp = cand; break
        except: pass
    if not code_inp:
        page.wait_for_timeout(2000)
        for sel in ['input[autocomplete="one-time-code"]','input[inputmode="numeric"]']:
            try:
                cand = page.locator(sel).first
                if cand.is_visible(timeout=1000): code_inp = cand; break
            except: pass
    if not code_inp:
        sms_receiver.cancel_order(order, '5sim'); blocked.add(phone); save_blocked(blocked); continue
    code_inp.click(); code_inp.fill('')
    for ch in sms_code: code_inp.type(ch, delay=random.randint(35,65))

    page.evaluate(r"""() => {
      const cand = Array.from(document.querySelectorAll('button')).filter(b=>!b.disabled).find(b => /^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/i.test((b.innerText||'').trim()));
      if (cand) { cand.scrollIntoView({block:'center'}); cand.click(); }
    }""")
    elapsed = (datetime.now(timezone.utc) - sent_at).total_seconds()
    print(f'  ✓ confirmar @ {elapsed:.1f}s totales')
    page.wait_for_timeout(5500)
    body = page.evaluate('()=>document.body.innerText.slice(0,400)').lower()
    page.screenshot(path=os.path.join(SHOTS, f'60_cycle{cycle}_post_confirm.png'))

    if 'error al validar' in body or 'an error has occurred' in body or 'error inesperado' in body:
        print(f'  ❌ TM rechazó {phone} — blacklist')
        sms_receiver.finish_order(order, '5sim')
        blocked.add(phone); save_blocked(blocked); continue

    sms_receiver.finish_order(order, '5sim')
    success = True
    result = {'tm_email':EMAIL, 'phone':phone, 'phone_digits':digits,
              'name':NAME, 'last_name':LAST,
              'provider':'5sim','order_id':str(order),
              'sms_text':sms_text,'sms_code':sms_code,
              'cycle':cycle, 'verified_at':datetime.now(timezone.utc).isoformat(),
              'final_url':page.url, 'final_title':page.title(),
              'profile':prof['profile'], 'env_id':prof['env_id'], 'port':port}
    with open(os.path.join(SHOTS,'phone_used.json'),'w',encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f'  ✅ ÉXITO ciclo {cycle}'); break

print(f'\n═══ RESULTADO U{ROW} ═══')
if success:
    print(f'  ✅ phone={result["phone"]} sms={result["sms_code"]}')
else:
    print(f'  ❌ sin éxito tras 4 ciclos. blocked={load_blocked()}')
print(f'  perfil:    {prof["profile"]} (IP {prof["ip"]}) port={port}')
print(f'  URL:       {page.url[:140]}')
print(f'  Title:     {page.title()}')
print(f'  body 200c: {page.evaluate("()=>document.body.innerText.slice(0,200)")}')
b.close(); pw.stop()
print('\n--- BROWSER SIGUE ABIERTO en MoreLogin ---')
