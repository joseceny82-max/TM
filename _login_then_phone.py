"""Login con credenciales del user1 → email OTP fast → relentless phone."""
import sys, time, re, json, requests, random, os, csv, socket
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

PORT = 57448
SHOTS = r'datos\buy\register_u1'
os.makedirs(SHOTS, exist_ok=True)

# user1 credentials desde plantilla
with open('plantilla_tm.csv', encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
u = rows[0]
EMAIL = u['Email_TM']; PWD = u['Password_TM']
MADRE = u['Email_Contacto']; MADRE_PWD = u['Password_Email']
print(f'login: {EMAIL}')

v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=8).json()
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
page = b.contexts[0].pages[0]
page.bring_to_front()
print('URL:', page.url[:100])

# ── 1. Email + Continuar ──
em = page.locator('input[type="email"]').first
em.wait_for(state='visible', timeout=10000)
em.click(); em.fill('')
for ch in EMAIL: em.type(ch, delay=random.randint(35,70))
print('  email tipeado')
page.wait_for_timeout(800)
page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
page.wait_for_timeout(4000)
print('  click Continuar →', page.title())

# ── 2. Password ──
try:
    pw_el = page.locator('input[type="password"]').first
    pw_el.wait_for(state='visible', timeout=8000)
    pw_el.click(); pw_el.fill('')
    for ch in PWD: pw_el.type(ch, delay=random.randint(35,70))
    print('  password tipeado')
    page.wait_for_timeout(800)
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/iniciar sesi[oó]n|sign in|^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
    page.wait_for_timeout(5000)
    print(f'  post-password URL: {page.url[:100]}')
    print(f'  Title: {page.title()}')
except Exception as e:
    print(f'  password err: {e}')

page.screenshot(path=os.path.join(SHOTS, '40_post_login.png'))
body = page.evaluate('()=>document.body.innerText.slice(0,500)')
print(f'  body: {body[:300]}')

# ── 3. Si TM pide OTP email (re-verify), capturar y pegar ──
need_email_otp = 'verifica' in body.lower() or 'verify' in page.title().lower()
if need_email_otp:
    print('\n[email OTP] click Verificar mi correo')
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/verificar mi correo|verify my email/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
    sent_at = datetime.now(timezone.utc)
    page.wait_for_timeout(2500)

    # poll IMAP rápido
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
                    to_low = ', '.join(m.to or []).lower()
                    if EMAIL.lower() not in to_low: continue
                    if 'ticketmaster' not in (m.from_ or '').lower() and 'ticketmaster' not in (m.subject or '').lower(): continue
                    hay = (m.subject or '') + ' ' + (m.text or m.html or '')[:1500]
                    g = re.search(r'\b(\d{6})\b', hay)
                    if g: otp = g.group(1); break
        except Exception as e:
            print(f'  imap err: {e}')
        if otp: break
        time.sleep(2)

    if otp:
        print(f'  📧 OTP={otp}')
        # Localizar input OTP
        inp = None
        for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]', 'input[type="text"]']:
            try:
                cand = page.locator(sel).first
                if cand.is_visible(timeout=1500): inp = cand; break
            except: pass
        if inp:
            inp.click(); inp.fill('')
            for ch in otp: inp.type(ch, delay=random.randint(50,90))
            page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
            page.wait_for_timeout(5000)
            print(f'  post-confirm: {page.title()}')
        else:
            print('  ⚠ no input OTP visible')
    else:
        print('  ⚠ OTP no llegó en 120s')

page.screenshot(path=os.path.join(SHOTS, '41_post_email_otp.png'))
body = page.evaluate('()=>document.body.innerText.slice(0,500)')
print(f'\nESTADO ACTUAL:')
print(f'  URL: {page.url[:120]}')
print(f'  Title: {page.title()}')
print(f'  body: {body[:300]}')

b.close(); pw.stop()
print('\nDone — ahora lanzar _phone_relentless.py')
