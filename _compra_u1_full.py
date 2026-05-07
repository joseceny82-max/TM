"""Flow completo Compra User-1 (v15 todo-en-uno):
1. Goto evento
2. Ver lo mejor disponible
3. Stepper Vis. Reducida → qty=4
4. Click Entradas verde (trusted)
5. Marcar checkbox Acepto
6. Continuar comprando (trusted) → OAuth login
7. Login User-1 (con OTP IMAP joaquina_polo0) — con waits humanizados anti-bot
8. Watcher hasta checkout → autorelleno tarjeta → PAUSA
"""
from __future__ import annotations
import sys, io, time, os, json, re, requests, random
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

PORT = int(os.environ.get('CDP_PORT', '61810'))
SHOTDIR = os.path.join(os.path.dirname(__file__), "datos", "compra_test")
EVENT_URL = "https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961"

def log(stage, **kw):
    print(f"[{time.strftime('%H:%M:%S')}] {stage}", json.dumps(kw, ensure_ascii=False) if kw else "", flush=True)

def shoot(page, name):
    p = os.path.join(SHOTDIR, f"{int(time.time())}_{name}.png")
    try: page.screenshot(path=p, full_page=False); log("shot", file=os.path.basename(p))
    except Exception as e: log("shot_err", err=str(e)[:80])

import account_manager as am
USER_LABEL_PREFIX = os.environ.get('USER_LABEL', 'User-6')
account = next(a for a in am.account_manager.list_accounts() if a['label'].startswith(USER_LABEL_PREFIX))
card = next(c for c in am.account_manager.list_cards() if c.get('account_id') == account['id'])
log("user", email=account['tm_email'])

def human_pause(min_s=0.8, max_s=2.0): time.sleep(random.uniform(min_s, max_s))

def imap_otp(email_madre, pwd, since_ts, max_wait=180):
    from imap_tools import MailBox, AND
    deadline = time.time() + max_wait
    pat6 = re.compile(r'\b(\d{6})\b')
    while time.time() < deadline:
        try:
            with MailBox('imap.mail.me.com').login(email_madre, pwd) as mb:
                mb.folder.set('INBOX')
                msgs = list(mb.fetch(AND(date_gte=since_ts.date()), limit=20, reverse=True, mark_seen=False))
                tm = [m for m in msgs if 'ticketmaster' in (m.from_ or '').lower()
                      and m.date and m.date.replace(tzinfo=None) >= since_ts.replace(tzinfo=None)]
                log("imap_poll", recent=len(tm))
                for m in tm:
                    body = (m.text or '') + ' ' + (m.subject or '')
                    for code in pat6.findall(body):
                        if 100000 <= int(code) <= 999999:
                            log("otp_found", code=code); return code
        except Exception as e: log("imap_err", err=str(e)[:80])
        time.sleep(8)
    return None

def login_full(page, account):
    log("login_start", email=account['tm_email'])
    human_pause(2, 4)  # Espera más larga para que reCAPTCHA cargue
    # Email
    try: page.wait_for_selector('input[type="email"]', timeout=30000)
    except: log("no_email_input"); shoot(page, "no_email_input"); return False
    # Click en input antes de fill (humano)
    page.click('input[type="email"]'); human_pause(0.5, 1.0)
    page.type('input[type="email"]', account['tm_email'], delay=80); log("email_typed")
    human_pause(1.5, 2.5)
    for txt in ["Continuar","Continue","Siguiente","Next","Next →"]:
        try:
            b = page.get_by_role("button", name=re.compile(txt, re.I)).first
            if b.is_visible(timeout=1500):
                b.click(force=True, timeout=3000); break
        except: continue
    human_pause(3, 5)
    # Password
    try: page.wait_for_selector('input[type="password"]', timeout=20000)
    except: log("no_pwd_input"); shoot(page, "no_pwd"); return False
    page.click('input[type="password"]'); human_pause(0.4, 1.0)
    page.type('input[type="password"]', account['tm_password'], delay=80); log("pwd_typed")
    human_pause(1.5, 3)
    for txt in ["Iniciar sesión","Sign In","Login","Continuar"]:
        try:
            b = page.get_by_role("button", name=re.compile(txt, re.I)).first
            if b.is_visible(timeout=1500):
                b.click(force=True, timeout=3000); break
        except: continue
    page.wait_for_timeout(8000); shoot(page, "post_login")
    # OTP
    try:
        otp = page.locator('input[name*="code" i], input[autocomplete*="one-time" i], input[type="tel"][maxlength]').first
        if otp.is_visible(timeout=3000):
            from datetime import datetime
            log("otp_required"); shoot(page, "otp_screen")
            since = datetime.now()
            code = imap_otp(account['contact_email'], account['contact_password'], since, max_wait=180)
            if code:
                otp.fill(code); page.wait_for_timeout(800)
                for txt in ["Verificar","Confirmar","Continuar"]:
                    try:
                        b = page.get_by_role("button", name=re.compile(txt, re.I)).first
                        if b.is_visible(timeout=1500): b.click(force=True); break
                    except: continue
                page.wait_for_timeout(5000); return True
            return False
    except: pass
    return True

def fill_card(page, card, account):
    log("fill_card_start"); page.wait_for_timeout(4500); shoot(page, "checkout_initial")
    holder = card.get('holder',''); number = re.sub(r'\D','',card.get('number',''))
    month = str(card.get('month','')).zfill(2); year = card.get('year','')
    yyyy = ("20"+year) if len(year)==2 else year; cvv = card.get('cvv','')
    def try_fill(loc, val, lbl):
        try:
            if loc.is_visible(timeout=800): loc.fill(val); log("filled", field=lbl); return True
        except: pass
        return False
    targets = [page] + list(page.frames)
    for t in targets:
        for sel in ['input[name*="number" i]','input[id*="cardNumber" i]','input[autocomplete="cc-number"]','input[data-cse="encryptedCardNumber"]','input[aria-label*="card" i]','input[aria-label*="tarjeta" i]']:
            if try_fill(t.locator(sel).first, number, "card_number"): break
        for sel in ['input[name*="holder" i]','input[id*="holderName" i]','input[autocomplete="cc-name"]','input[name*="cardholder" i]']:
            if try_fill(t.locator(sel).first, holder, "holder"): break
        for sel in ['input[name*="expiry" i]','input[id*="expiryDate" i]','input[autocomplete="cc-exp"]','input[data-cse="encryptedExpiryDate"]']:
            if try_fill(t.locator(sel).first, f"{month}/{year[-2:]}", "expiry"): break
        for sel in ['input[autocomplete="cc-exp-month"]','input[name*="expMonth" i]']:
            if try_fill(t.locator(sel).first, month, "exp_month"): break
        for sel in ['input[autocomplete="cc-exp-year"]','input[name*="expYear" i]']:
            if try_fill(t.locator(sel).first, yyyy, "exp_year"): break
        for sel in ['input[name*="cvv" i]','input[name*="cvc" i]','input[id*="securityCode" i]','input[autocomplete="cc-csc"]','input[data-cse="encryptedSecurityCode"]','input[aria-label*="cvv" i]']:
            if try_fill(t.locator(sel).first, cvv, "cvv"): break
    for t in targets:
        try_fill(t.locator('input[name*="firstName" i]').first, account.get('first_name',''), "first_name")
        try_fill(t.locator('input[name*="lastName" i]').first, account.get('last_name',''), "last_name")
        try_fill(t.locator('input[name*="phone" i]').first, account.get('phone_prefix','')+account.get('phone',''), "phone")
        try_fill(t.locator('input[name*="address" i], input[name*="street" i]').first, account.get('address1',''), "address")
        try_fill(t.locator('input[name*="city" i]').first, account.get('city',''), "city")
        try_fill(t.locator('input[name*="postal" i], input[name*="zip" i]').first, account.get('zip',''), "zip")
    shoot(page, "checkout_filled")

def main():
    v = requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=8).json()
    log("connected_cdp", port=PORT)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
        ctx = b.contexts[0]
        # Bloquear notificaciones
        try:
            ctx.add_init_script("""
                if (window.Notification) Notification.requestPermission = () => Promise.resolve('denied');
            """)
        except: pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        # 1) Goto evento
        log("goto"); page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
        human_pause(5, 7)
        # Cookies
        for txt in ["Aceptar todas las cookies","Aceptar","Accept"]:
            try:
                b_ = page.get_by_text(txt, exact=False).first
                if b_.is_visible(timeout=1500): b_.click(); human_pause(1, 2); break
            except: continue
        shoot(page, "v15_event_loaded")

        # 2) Ver lo mejor disponible
        log("step1_ver_lo_mejor")
        page.evaluate("""() => {
            const b = Array.from(document.querySelectorAll('button')).find(x => /ver lo mejor disponible/i.test(x.innerText||''));
            if (b) b.click();
        }""")
        human_pause(3, 5)

        # 3) Subir qty=4 en stepper Vis. Reducida [2]
        log("step2_qty_vis_reducida")
        for i in range(8):
            cur = page.evaluate("""() => {
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[2];
                return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
            }""")
            log("qty", current=cur)
            if cur >= 4: break
            page.evaluate("""() => {
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[2];
                if (!s) return;
                const btns = s.querySelectorAll('button');
                (btns[btns.length-1] || btns[0]).click();
            }""")
            human_pause(0.4, 0.8)

        # 4) Click Entradas verde
        log("step3_entradas_verde")
        try:
            btn = page.locator('button[data-testid="findTicketsBtn"]').first
            btn.scroll_into_view_if_needed(timeout=4000)
            human_pause(0.6, 1.2)
            btn.click(timeout=10000, force=True)
            log("entradas_clicked")
        except Exception as e: log("entradas_err", err=str(e)[:120])
        human_pause(4, 6)
        shoot(page, "v15_after_entradas")

        # 5) Checkbox Acepto
        log("step4_acepto")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        human_pause(1, 2)
        r = page.evaluate("""() => {
            const labels = Array.from(document.querySelectorAll('label'));
            const lbl = labels.find(l => /acepto que he le[ií]do/i.test(l.textContent||''));
            if (!lbl) return {ok:false};
            const forId = lbl.getAttribute('for');
            let cb = forId ? document.getElementById(forId) : null;
            if (!cb) cb = lbl.querySelector('input[type=checkbox]');
            if (cb) { cb.scrollIntoView({block:'center'}); cb.click(); return {ok:true, checked:cb.checked}; }
            return {ok:false, no_cb:true};
        }""")
        log("acepto_result", **r)
        human_pause(1.2, 2)

        # 6) Continuar comprando
        log("step5_continuar")
        prev = page.url
        try:
            btn = page.get_by_role("button", name=re.compile(r"^continuar comprando$", re.I)).first
            btn.scroll_into_view_if_needed(timeout=4000)
            human_pause(0.7, 1.4)
            btn.click(timeout=10000, force=True)
            log("continuar_clicked")
        except Exception as e: log("continuar_err", err=str(e)[:120])
        human_pause(8, 12)

        # 7) Si llegamos a OAuth, login
        u = page.url.lower()
        log("post_continuar_url", url=u[:100])
        if any(k in u for k in ['auth.ticketmaster','identity','login','signin']):
            log("oauth_detected")
            login_full(page, account)
            human_pause(4, 6)

        # 8) Watcher hasta checkout
        log("watch_for_checkout")
        deadline = time.time() + 600
        prev = page.url
        while time.time() < deadline:
            time.sleep(2)
            try:
                cur = page.url
                if cur != prev:
                    log("URL_CHANGED", to=cur[:120])
                    prev = cur
                    page.wait_for_timeout(3000)
                    u = cur.lower()
                    if any(k in u for k in ['identity','login','auth','signin','my.tick']):
                        log("auth_detected")
                        login_full(page, account)
                        human_pause(3, 5)
                    u = page.url.lower()
                    if any(k in u for k in ['checkout','payment','cart','pago']):
                        log("checkout_detected")
                        fill_card(page, card, account)
                        log("READY_FOR_USER_CONFIRMATION", url=page.url)
                        print("\n" + "="*70)
                        print("✅ TARJETA AUTORELLENADA. Revisa y confirma manualmente con Pagar.")
                        print(f"URL: {page.url}")
                        print("Para cerrar: crear archivo _compra_u1.STOP")
                        print("="*70)
                        flag = os.path.join(os.path.dirname(__file__), "_compra_u1.STOP")
                        while not os.path.exists(flag): time.sleep(5)
                        os.remove(flag); log("stop"); return
            except Exception as e: log("watch_err", err=str(e)[:80])
        log("TIMEOUT")

if __name__ == '__main__': main()
