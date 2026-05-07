"""Flow completo desde perfil ES NUEVO:
  1. Pick fresh ES profile + start
  2. Cookie warming HTTP
  3. Goto my.ticketmaster.es
  4. Email + Continuar → Password (login U1)
  5. Email OTP fast (si TM lo pide)
  6. Click "Añadir mi teléfono"
  7. Buy 5sim (skip blocked)
  8. Paste digits + click "Añade número"
  9. SMS poll 50s
 10. Paste code + Confirmar
 11. Si TM rechaza → blacklist, browser cleanup, recursivo... (max 3 ciclos)
"""
import sys, time, re, json, requests, random, os, csv, socket
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

SHOTS = r'datos\buy\register_u1'
os.makedirs(SHOTS, exist_ok=True)
BLOCKED_PATH = os.path.join(SHOTS, 'blocked_numbers.json')

with open('plantilla_tm.csv', encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
u = rows[0]
EMAIL = u['Email_TM']; PWD = u['Password_TM']
MADRE = u['Email_Contacto']; MADRE_PWD = u['Password_Email']
print(f'═══ FULL LOGIN+PHONE — {EMAIL} ═══')

import bot_orchestrator as bo
import sms_receiver

def load_blocked():
    try:
        with open(BLOCKED_PATH, encoding='utf-8') as f: return set(json.load(f).get('blocked', []))
    except: return set()
def save_blocked(s):
    with open(BLOCKED_PATH, 'w', encoding='utf-8') as f: json.dump({'blocked': sorted(s)}, f, indent=2)

# ── 1. Perfil ES nuevo ──
print('\n[1] perfil ES nuevo')
prof = bo._pick_fresh_profile(country_code='ES')
if not prof: print('❌ no perfil'); sys.exit(1)
port = bo._start_profile(prof['env_id'])
print(f'  perfil={prof["profile"]} (IP {prof["ip"]}) port={port} env_id={prof["env_id"]}')
time.sleep(5)

# ── 2. Cookie warming ──
import cookie_warmer
warm = cookie_warmer.warm_cookies(domain_id='es', pages=4, timeout=10.0)
print(f'  cookie warmer ok={warm.get("ok")} pages={warm.get("pages_warmed")} cookies={len(warm.get("cookies",{}))}')

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
# Inyectar warmed cookies
inj = 0
for n,v2 in (warm.get('cookies') or {}).items():
    try:
        ctx.add_cookies([{'name':n,'value':str(v2),'domain':'.ticketmaster.es','path':'/'}])
        inj += 1
    except: pass
print(f'  ✓ {inj} cookies warmed inyectadas')

page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.bring_to_front()

# ── 4. Goto + email ──
TM_URL = f'https://my.ticketmaster.es/?_={int(time.time())}{random.randint(1000,9999)}'
print(f'\n[4] goto {TM_URL[:60]}')
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

# ── 5. Password ──
title = page.title()
print(f'  Title post-Continuar: {title}')
if 'sign in' in title.lower() or 'iniciar' in title.lower():
    try:
        pw_el = page.locator('input[type="password"]').first
        pw_el.wait_for(state='visible', timeout=10000)
        pw_el.click(); pw_el.fill('')
        for ch in PWD: pw_el.type(ch, delay=random.randint(40,80))
        print('  password tipeado')
        page.wait_for_timeout(800)
        page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/iniciar sesi[oó]n|sign in|^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
        page.wait_for_timeout(6000)
        print(f'  post-login URL: {page.url[:100]}')
        print(f'  Title: {page.title()}')
    except Exception as e:
        print(f'  ⚠ password err: {e}')

page.screenshot(path=os.path.join(SHOTS, '50_post_login.png'))

# ── 6. Email OTP si TM lo pide ──
def fast_email_otp():
    body = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
    if 'verifica tu cuenta' not in body and 'verify your' not in page.title().lower():
        return True   # no hace falta OTP
    print('\n[email-OTP] click "Verificar mi correo"')
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/verificar mi correo|verify my email/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
    sent_at = datetime.now(timezone.utc)
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
        except Exception as e:
            print(f'  imap err: {e}')
        if otp: break
        time.sleep(2)
    if not otp:
        print('  ❌ email OTP timeout 120s'); return False
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
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
    page.wait_for_timeout(5000)
    print(f'  post-OTP: {page.title()}')
    return True

if not fast_email_otp():
    page.screenshot(path=os.path.join(SHOTS, '51_email_otp_fail.png'))
    print('❌ email-otp falló — abortando')
    b.close(); pw.stop(); sys.exit(2)

page.screenshot(path=os.path.join(SHOTS, '52_post_email_otp.png'))

# ── 7. Click "Añadir mi teléfono" ──
print('\n[phone] click "Añadir mi teléfono"')
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
    print('❌ no phone input — abortando'); b.close(); pw.stop(); sys.exit(3)

# ── 8. Buy + paste + click + poll + paste + confirm (con retries) ──
SMS_TIMEOUT = 50
NO_STOCK_RETRIES = 30
success = False
result = None

for cycle in range(1, 5):
    print(f'\n══ CICLO PHONE {cycle}/4 ══')
    blocked = load_blocked()

    # Asegurar input visible (recién o tras retry)
    try: phone_inp.wait_for(state='visible', timeout=3000)
    except:
        # reset al form
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

    # buy
    phone, order = None, None
    for k in range(NO_STOCK_RETRIES):
        r = sms_receiver.buy_number(country='es', product='ticketmaster')
        if r.get('ok'):
            ph = r.get('phone')
            if ph in blocked:
                print(f'    descarte blocked {ph}')
                try: sms_receiver.cancel_order(r['order_id'], '5sim')
                except: pass
                time.sleep(2); continue
            phone, order = ph, r.get('order_id'); break
        if k % 5 == 0: print(f'    [buy retry {k+1}] {(r.get("raw",{}) or {}).get("raw","?")}')
        time.sleep(8)
    if not phone:
        print('  ❌ stock 5sim agotado — abortando ciclo'); break
    digits = phone.lstrip('+')
    if digits.startswith('34'): digits = digits[2:]
    print(f'  buy ok {phone} order={order}')

    phone_inp.click(); phone_inp.fill('')
    for ch in digits: phone_inp.type(ch, delay=random.randint(35,65))

    sent_at = datetime.now(timezone.utc)
    clicked = page.evaluate(r"""() => {
      const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
      const isBlue = b => { const bg = getComputedStyle(b).backgroundColor; return /rgb\(\s*2\s*,\s*77\s*,\s*223\s*\)/i.test(bg); };
      const cand = btns.find(b => /^(a[ñn]ade n[uú]mero|a[ñn]ade tu n[uú]mero)$/i.test((b.innerText||'').trim()) && isBlue(b))
                || btns.find(b => /a[ñn]ade n[uú]mero/i.test((b.innerText||'').trim()) && isBlue(b));
      if (cand) { cand.scrollIntoView({block:'center'}); cand.click(); return cand.innerText.trim(); }
      return null;
    }""")
    sent_at = datetime.now(timezone.utc)
    print(f'  click "{clicked}" @ {sent_at.strftime("%H:%M:%S")}')
    if not clicked:
        sms_receiver.cancel_order(order, '5sim'); blocked.add(phone); save_blocked(blocked); continue

    # SMS poll 50s
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

    # ÉXITO
    sms_receiver.finish_order(order, '5sim')
    success = True
    result = {'tm_email':EMAIL, 'phone':phone, 'phone_digits':digits,
              'provider':'5sim','order_id':str(order),
              'sms_text':sms_text,'sms_code':sms_code,
              'cycle':cycle, 'verified_at':datetime.now(timezone.utc).isoformat(),
              'final_url':page.url, 'final_title':page.title(),
              'profile':prof['profile'], 'env_id':prof['env_id'], 'port':port}
    with open(os.path.join(SHOTS,'phone_used.json'),'w',encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f'  ✅ ÉXITO en ciclo {cycle}'); break

# ── Resumen ──
print(f'\n═══ RESULTADO FINAL ═══')
if success:
    print(f'  ✅ phone={result["phone"]} sms={result["sms_code"]}')
    print(f'  guardado en datos/buy/register_u1/phone_used.json')
else:
    print(f'  ❌ sin éxito tras 4 ciclos. Blocked={load_blocked()}')

print(f'\n  perfil: {prof["profile"]} env_id={prof["env_id"]} port={port}')
print(f'  URL final: {page.url[:140]}')
print(f'  Title:     {page.title()}')
b.close(); pw.stop()
