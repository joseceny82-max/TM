"""Registro REAL en TM ES con datos del usuario 1 de plantilla_tm.csv.
Browser visible, paso a paso. Si OTP no llega (alias HME iCloud no recibe TM),
se queda en VERIFICA TU CUENTA esperando — el user puede meter el código manualmente."""
import sys, time, os, csv, random, json, re
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')
from datetime import datetime
import requests

# ─── 1. Cargar usuario 1 del CSV ───
with open('plantilla_tm.csv', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = next(reader)
    rows = list(reader)
H = {n: i for i, n in enumerate(header)}
# Si TM ya conoce la fila 1 (medals-96staves@), saltar a una fila no quemada.
# El user puede pasar índice por env var TM_ROW (1-based).
import os
ROW_INDEX = int(os.environ.get('TM_ROW', '1')) - 1
u = rows[ROW_INDEX] if 0 <= ROW_INDEX < len(rows) else rows[0]
print(f'  fila usada: #{ROW_INDEX+1}/{len(rows)}', flush=True)
EMAIL = u[H['Email_TM']]
PWD = u[H['Password_TM']]
CONTACT_EMAIL = u[H['Email_Contacto']]
CONTACT_PWD = u[H['Password_Email']]
FNAME = u[H['Nombre']]
LNAME = u[H['Apellidos']]
ZIP = u[H['ZIP']]

print(f'═══ TEST REGISTRO REAL TM — Usuario 1 ═══', flush=True)
print(f'  email TM:       {EMAIL}', flush=True)
print(f'  email contacto: {CONTACT_EMAIL}', flush=True)
print(f'  IMAP pwd:       {CONTACT_PWD[:4]}***{CONTACT_PWD[-4:]}', flush=True)
print(f'  nombre:         {FNAME} {LNAME}  zip={ZIP}', flush=True)

# ─── 2. Arrancar perfil ES nuevo (regla "perfil nuevo por ejecución") ───
import bot_orchestrator as bo
print('\n[1] Arrancar perfil MoreLogin ES nuevo', flush=True)
prof = bo._pick_fresh_profile(country_code='ES')
if not prof:
    print('  ❌ no hay perfiles ES disponibles', flush=True)
    sys.exit(1)
port = bo._start_profile(prof['env_id'])
print(f'  perfil: {prof["profile"]} (IP {prof["ip"]}) port={port}', flush=True)
time.sleep(5)

SHOTS = r'datos\buy\register_u1'
os.makedirs(SHOTS, exist_ok=True)

# ─── 3. Conectar Playwright + bloquear notif desde antes ───
from playwright.sync_api import sync_playwright
v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
ctx = b.contexts[0]
ctx.add_init_script("""
    if (window.Notification) {
        Notification.requestPermission = () => Promise.resolve('denied');
        try { Object.defineProperty(Notification, 'permission', {get:()=>'denied', configurable:true}); } catch(e){}
    }
    if (navigator.serviceWorker && navigator.serviceWorker.register) {
        navigator.serviceWorker.register = function(){ return Promise.reject(new Error('blocked')); };
    }
""")
page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.bring_to_front()

def shot(n):
    p = os.path.join(SHOTS, f'{n}.png')
    page.screenshot(path=p, full_page=False)
    print(f'    shot {n}', flush=True)

# ─── 3.5 COOKIE WARMING (HTTP REAL via cookie_warmer) ───
print('\n[2a] cookie warming HTTP previo a TM ES', flush=True)
try:
    import cookie_warmer
    warm = cookie_warmer.warm_cookies(domain_id='es', pages=4, timeout=10.0)
    if warm.get('ok'):
        print(f'  ✓ warmed {warm.get("pages_warmed",0)} pags · cookies={len(warm.get("cookies",{}))} · acpat_present={warm.get("acpat_present",False)}', flush=True)
        # Inyectar cookies de warming en el contexto Playwright (mismo dominio)
        injected = 0
        for name, val in (warm.get('cookies') or {}).items():
            try:
                ctx.add_cookies([{'name': name, 'value': str(val),
                                  'domain': '.ticketmaster.es', 'path': '/'}])
                injected += 1
            except Exception: pass
        print(f'  ✓ {injected} cookies inyectadas al contexto', flush=True)
    else:
        print(f'  ⚠ warm_cookies devolvió ok=False: {warm.get("error","?")}', flush=True)
except Exception as e:
    print(f'  ⚠ cookie_warmer error (no fatal): {e}', flush=True)

# ─── 4. Goto + cookies ───
TM_URL = f'https://my.ticketmaster.es/?_={int(time.time())}{random.randint(1000,9999)}'
print(f'\n[2] goto my.ticketmaster.es', flush=True)
for attempt in range(3):
    try: page.goto(TM_URL, wait_until='domcontentloaded', timeout=30000); break
    except Exception as e: print(f'  retry {attempt+1}: {e}', flush=True); time.sleep(3)
page.wait_for_timeout(7000)

# Helper auto-solve captcha via captcha_manager (2Captcha) — se invoca on-demand
def _try_solve_captcha(label='auto'):
    """Detecta hCaptcha/reCAPTCHA/Turnstile en la página, manda a 2Captcha y devuelve token."""
    try:
        sitekey_info = page.evaluate(r"""() => {
            // hcaptcha
            const h = document.querySelector('[data-sitekey]');
            if (h && (h.classList?.contains('h-captcha') || h.getAttribute('data-hcaptcha-widget-id') || /hcaptcha/i.test(h.outerHTML))) {
                return {type:'hcaptcha', sitekey: h.getAttribute('data-sitekey')};
            }
            // recaptcha v2
            const g = document.querySelector('.g-recaptcha[data-sitekey], [data-sitekey][src*="recaptcha"]');
            if (g) return {type:'recaptcha_v2', sitekey: g.getAttribute('data-sitekey')};
            // generic data-sitekey en iframe
            const f = Array.from(document.querySelectorAll('iframe')).find(x => /hcaptcha|recaptcha/.test(x.src||''));
            if (f) {
                const m = (f.src||'').match(/sitekey=([^&]+)/);
                if (m) return {type: /hcaptcha/.test(f.src)?'hcaptcha':'recaptcha_v2', sitekey: m[1]};
            }
            return null;
        }""")
        if not sitekey_info:
            return None
        print(f'  🤖 captcha detectado [{label}]: {sitekey_info["type"]} sitekey={sitekey_info["sitekey"][:24]}...', flush=True)
        import captcha_manager as cmgr
        url = page.url
        if sitekey_info['type'] == 'hcaptcha':
            res = cmgr.solve_hcaptcha(sitekey_info['sitekey'], url, timeout=120)
        else:
            res = cmgr.solve_recaptcha_v2(sitekey_info['sitekey'], url, timeout=120)
        if not res or not res.get('ok'):
            print(f'  ❌ captcha solver fallo: {res}', flush=True); return None
        token = res.get('token') or res.get('solution')
        print(f'  ✅ token recibido ({len(token or "")} chars) — inyectando', flush=True)
        page.evaluate(f"""() => {{
            const t = {json.dumps(token)};
            const ta = document.querySelector('textarea[name="h-captcha-response"]') || document.querySelector('textarea[name="g-recaptcha-response"]');
            if (ta) {{ ta.style.display='block'; ta.value = t; ta.dispatchEvent(new Event('change',{{bubbles:true}})); }}
            if (window.hcaptcha && window.hcaptcha.execute) {{ try {{ window.hcaptcha.execute(); }} catch(e){{}} }}
        }}""")
        return token
    except Exception as e:
        print(f'  ⚠ captcha helper error: {e}', flush=True); return None
for txt in ['Aceptar todas las cookies', 'Rechazarlas todas']:
    try:
        btn = page.get_by_text(txt, exact=False).first
        if btn.is_visible(timeout=1500): btn.click(); page.wait_for_timeout(1500); break
    except: continue
shot('01_loaded')

# ─── 5. Email + Continuar ───
print(f'\n[3] tipear email {EMAIL}', flush=True)
em = page.locator('input[type="email"]').first
em.wait_for(state='visible', timeout=15000)
em.click(); em.fill('')
for ch in EMAIL: em.type(ch, delay=random.randint(50,110))
page.wait_for_timeout(1200)
shot('02_email')

print('[4] click Continuar', flush=True)
page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
page.wait_for_timeout(7000)
# Si hay captcha tras "Continuar" → resolver con 2Captcha
_try_solve_captcha('post-continuar')
page.wait_for_timeout(2000)
shot('03_after_continuar')

# Si TM dice "Sign Up" → flujo registro nuevo
url = page.url; title = page.title()
print(f'  URL:   {url[:100]}', flush=True)
print(f'  Title: {title}', flush=True)
mode = 'signup' if 'sign up' in title.lower() else ('signin' if 'sign in' in title.lower() else 'unknown')
print(f'  modo detectado: {mode}', flush=True)

# ─── 6. Form de signup (si aplica) ───
if mode == 'signup':
    print(f'\n[5] rellenar form signup (Ryan King…)', flush=True)
    try:
        pw_el = page.locator('input[type="password"]').first
        pw_el.wait_for(state='visible', timeout=10000)
        pw_el.click(); pw_el.fill('')
        for ch in PWD: pw_el.type(ch, delay=random.randint(40,90))
        print('  ✓ password', flush=True)
    except Exception as e: print(f'  pwd err: {e}', flush=True)

    for sel, val, name in [
        ('input[autocomplete="given-name"]', FNAME, 'first_name'),
        ('input[autocomplete="family-name"]', LNAME, 'last_name'),
        ('input[autocomplete="postal-code"]', ZIP, 'zip'),
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500) and not loc.input_value().strip():
                loc.click(); loc.fill('')
                for ch in str(val): loc.type(ch, delay=random.randint(40,80))
                print(f'  ✓ {name}={val}', flush=True)
        except: pass

    # Modal cookies popup interno (Acepto)
    page.wait_for_timeout(1000)
    page.evaluate(r"""() => {const m = Array.from(document.querySelectorAll('button')).find(b => /^acepto$/i.test((b.innerText||'').trim())); if(m) m.click(); document.querySelectorAll('[id*=onetrust i]').forEach(e=>e.remove());}""")
    page.wait_for_timeout(800)
    # Scroll + privacy checkbox
    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
    page.wait_for_timeout(1500)
    cbs = page.locator('input[type="checkbox"]').all()
    for cb in cbs:
        try:
            if cb.is_visible(timeout=200) and not cb.is_checked():
                cb.check(force=True)
        except: pass
    shot('04_form_completo')

    # Captcha pre-Siguiente (TM mete hCaptcha en el form de signup)
    _try_solve_captcha('pre-siguiente')
    page.wait_for_timeout(1500)

    # Click Siguiente
    print(f'\n[6] click Siguiente', flush=True)
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^siguiente$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
    for i in range(8):
        page.wait_for_timeout(1500)
        if 'verify' in page.title().lower() or 'verifica' in (page.evaluate('()=>document.body.innerText||""')[:200] or '').lower():
            print(f'  ✓ llegó a verificación (t+{(i+1)*1.5:.0f}s)', flush=True); break
        # Si Siguiente no avanza por captcha tardío
        _try_solve_captcha(f'post-siguiente-{i}')
    shot('05_verifica_cuenta')

# ─── 7. Click Verificar email + esperar OTP ───
print(f'\n[7] click "Verificar mi correo electrónico"', flush=True)
clicked = page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/verificar mi correo/i.test((b.innerText||'').trim())); if(m){m.click(); return true;} return false;}""")
print(f'  clicked: {clicked}', flush=True)
page.wait_for_timeout(4000)
sent_at = datetime.now()
shot('06_email_modal')

# ─── 8. Poll IMAP del email contacto buscando OTP TM ───
print(f'\n[8] polling IMAP {CONTACT_EMAIL} buscando OTP TM (max 90s)', flush=True)
print(f'    ⚠️ aviso: TM ES históricamente NO entrega OTP a alias HME iCloud', flush=True)
otp_found = None
try:
    from imap_tools import MailBox
    deadline = time.time() + 90
    seen = set()
    patterns = [re.compile(r'\b(\d{6})\b'),
                re.compile(r'(?:c[oó]digo|verificaci[oó]n|verification)[^0-9]{0,40}?\b(\d{4,8})\b', re.I)]
    while time.time() < deadline:
        try:
            with MailBox('imap.mail.me.com', 993).login(CONTACT_EMAIL, CONTACT_PWD, initial_folder='INBOX') as mb:
                msgs = list(mb.fetch(reverse=True, limit=10, mark_seen=False))
                for msg in msgs:
                    if msg.uid in seen: continue
                    seen.add(msg.uid)
                    msg_dt = msg.date.replace(tzinfo=None) if msg.date else datetime.now()
                    if (datetime.now() - msg_dt).total_seconds() > 600: continue
                    fr = (msg.from_ or '').lower()
                    if 'ticketmaster' not in fr and 'ticketmaster' not in (msg.subject or '').lower(): continue
                    haystack = (msg.subject or '') + ' ' + (msg.text or '')[:1000]
                    for rx in patterns:
                        m = rx.search(haystack)
                        if m: otp_found = m.group(1); break
                    if otp_found:
                        print(f'  📧 OTP recibido: {otp_found}', flush=True)
                        break
            if otp_found: break
        except Exception as e:
            print(f'  IMAP poll err: {e}', flush=True); break
        elapsed = int(time.time() - (deadline - 90))
        print(f'  t+{elapsed}s sin OTP…', flush=True)
        time.sleep(8)
except ImportError:
    print('  imap-tools no instalado', flush=True)

# ─── 9. Si OTP llegó → introducirlo ───
if otp_found:
    print(f'\n[9] introducir OTP {otp_found} en TM', flush=True)
    page.evaluate(f"""() => {{
        const inp = document.querySelector('input[autocomplete=\"one-time-code\"]') || document.querySelector('input[type=\"text\"]');
        if(inp){{ inp.click(); inp.value=''; inp.value='{otp_found}'; inp.dispatchEvent(new Event('input',{{bubbles:true}})); }}
    }}""")
    page.wait_for_timeout(1500)
    page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/confirmar c[oó]digo|verify|verificar/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
    page.wait_for_timeout(5000)
    shot('07_post_otp_email')
    print(f'  URL post-OTP: {page.url[:120]}', flush=True)
else:
    print(f'\n[9] ⛔ OTP NO recibido en {CONTACT_EMAIL} en 90s', flush=True)
    print(f'   Confirma el bloqueo conocido TM ES → iCloud HME', flush=True)
    print(f'   Browser queda ABIERTO en port={port} para que metas OTP manual si llega.', flush=True)

# ─── 10. Resumen ───
print(f'\n═══ RESUMEN ═══', flush=True)
print(f'  perfil:      {prof["profile"]}  port={port}', flush=True)
print(f'  env_id:      {prof["env_id"]}', flush=True)
print(f'  URL final:   {page.url[:120]}', flush=True)
print(f'  Title final: {page.title()[:80]}', flush=True)
print(f'  OTP email:   {otp_found if otp_found else "NO RECIBIDO"}', flush=True)
print(f'  Browser OPEN — cuando quieras cerrar, ejecuta cleanup manual.', flush=True)

# Guardar state
with open('_register_u1_state.json', 'w', encoding='utf-8') as f:
    json.dump({'profile': prof['profile'], 'env_id': prof['env_id'], 'port': port,
               'email': EMAIL, 'otp_found': otp_found,
               'final_url': page.url, 'started_at': sent_at.isoformat()}, f, indent=2)

b.close()
pw.stop()
