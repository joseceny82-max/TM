"""Login User-1 + autorelleno tarjeta — directo desde la URL OAuth actual."""
from __future__ import annotations
import sys, io, time, os, json, re, requests, random
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

PORT = int(os.environ.get('CDP_PORT', '61810'))
SHOTDIR = os.path.join(os.path.dirname(__file__), "datos", "compra_test")

def log(s, **k):
    print(f"[{time.strftime('%H:%M:%S')}] {s}", json.dumps(k, ensure_ascii=False) if k else "", flush=True)

def shoot(page, name):
    p = os.path.join(SHOTDIR, f"{int(time.time())}_{name}.png")
    try: page.screenshot(path=p, full_page=False); log("shot", file=os.path.basename(p))
    except: pass

import account_manager as am
USER_LABEL_PREFIX = os.environ.get('USER_LABEL', 'User-6')
account = next(a for a in am.account_manager.list_accounts() if a['label'].startswith(USER_LABEL_PREFIX))
card = next(c for c in am.account_manager.list_cards() if c.get('account_id') == account['id'])
log("user", email=account['tm_email'])

def hp(a=0.7, b=1.8): time.sleep(random.uniform(a, b))

def imap_otp(em, pw, since_ts, max_wait=180):
    from imap_tools import MailBox, AND
    deadline = time.time() + max_wait
    pat = re.compile(r'\b(\d{6})\b')
    while time.time() < deadline:
        try:
            with MailBox('imap.mail.me.com').login(em, pw) as mb:
                mb.folder.set('INBOX')
                msgs = list(mb.fetch(AND(date_gte=since_ts.date()), limit=20, reverse=True, mark_seen=False))
                tm = [m for m in msgs if 'ticketmaster' in (m.from_ or '').lower()
                      and m.date and m.date.replace(tzinfo=None) >= since_ts.replace(tzinfo=None)]
                log("imap_poll", recent=len(tm))
                for m in tm:
                    body = (m.text or '') + ' ' + (m.subject or '')
                    for c in pat.findall(body):
                        if 100000 <= int(c) <= 999999: log("otp_found", code=c); return c
        except Exception as e: log("imap_err", err=str(e)[:80])
        time.sleep(8)
    return None

def fill_card(page, card, account):
    log("fill_card_start"); page.wait_for_timeout(4000); shoot(page, "checkout_initial")
    holder = card.get('holder',''); number = re.sub(r'\D','',card.get('number',''))
    month = str(card.get('month','')).zfill(2); year = card.get('year','')
    yyyy = ("20"+year) if len(year)==2 else year; cvv = card.get('cvv','')
    def tf(loc, val, lbl):
        try:
            if loc.is_visible(timeout=800): loc.fill(val); log("filled", field=lbl); return True
        except: pass
        return False
    targets = [page] + list(page.frames)
    for t in targets:
        for sel in ['input[name*="number" i]','input[id*="cardNumber" i]','input[autocomplete="cc-number"]','input[data-cse="encryptedCardNumber"]','input[aria-label*="card" i]','input[aria-label*="tarjeta" i]']:
            if tf(t.locator(sel).first, number, "card_number"): break
        for sel in ['input[name*="holder" i]','input[id*="holderName" i]','input[autocomplete="cc-name"]','input[name*="cardholder" i]']:
            if tf(t.locator(sel).first, holder, "holder"): break
        for sel in ['input[name*="expiry" i]','input[id*="expiryDate" i]','input[autocomplete="cc-exp"]','input[data-cse="encryptedExpiryDate"]']:
            if tf(t.locator(sel).first, f"{month}/{year[-2:]}", "expiry"): break
        for sel in ['input[autocomplete="cc-exp-month"]','input[name*="expMonth" i]']:
            if tf(t.locator(sel).first, month, "exp_month"): break
        for sel in ['input[autocomplete="cc-exp-year"]','input[name*="expYear" i]']:
            if tf(t.locator(sel).first, yyyy, "exp_year"): break
        for sel in ['input[name*="cvv" i]','input[name*="cvc" i]','input[id*="securityCode" i]','input[autocomplete="cc-csc"]','input[data-cse="encryptedSecurityCode"]','input[aria-label*="cvv" i]']:
            if tf(t.locator(sel).first, cvv, "cvv"): break
    for t in targets:
        tf(t.locator('input[name*="firstName" i]').first, account.get('first_name',''), "first_name")
        tf(t.locator('input[name*="lastName" i]').first, account.get('last_name',''), "last_name")
        tf(t.locator('input[name*="phone" i]').first, account.get('phone_prefix','')+account.get('phone',''), "phone")
        tf(t.locator('input[name*="address" i], input[name*="street" i]').first, account.get('address1',''), "address")
        tf(t.locator('input[name*="city" i]').first, account.get('city',''), "city")
        tf(t.locator('input[name*="postal" i], input[name*="zip" i]').first, account.get('zip',''), "zip")
    shoot(page, "checkout_filled")

def main():
    v = requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=8).json()
    log("connected_cdp", port=PORT)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
        ctx = b.contexts[0]
        # Coger la pestaña auth ACTIVA (la última, no la primera)
        auth_pages = [p for p in ctx.pages if 'auth.ticketmaster' in (p.url or '') or 'identity' in (p.url or '')]
        page = auth_pages[-1] if auth_pages else next((p for p in ctx.pages if 'ticketmaster' in (p.url or '')), ctx.pages[0])
        page.bring_to_front()
        log("page", url=page.url[:100])

        # Aceptar cookies si aparecen en login page
        for txt in ["Aceptar todas las cookies","Aceptar","Accept"]:
            try:
                bt = page.get_by_text(txt, exact=False).first
                if bt.is_visible(timeout=1500):
                    bt.click(); log("cookies_accepted"); hp(1, 2); break
            except: continue

        # 1) EMAIL — usar el id real "email-input"
        log("step_email")
        try:
            page.wait_for_selector('#email-input, input[type="email"]', timeout=15000)
        except Exception as e: log("no_email", err=str(e)[:80]); shoot(page, "no_email"); return
        email_sel = '#email-input' if page.locator('#email-input').count() else 'input[type="email"]'
        page.click(email_sel); hp(0.5, 1.0)
        page.type(email_sel, account['tm_email'], delay=80); log("email_typed")
        hp(1.5, 2.5)
        # Click Continuar
        for txt in ["Continuar","Continue","Siguiente","Next"]:
            try:
                b = page.get_by_role("button", name=re.compile(f"^{txt}$", re.I)).first
                if b.is_visible(timeout=1500):
                    b.click(force=True); log("continue_clicked", txt=txt); break
            except: continue
        hp(4, 6)
        shoot(page, "after_email_continue")

        # 2) PASSWORD
        log("step_password")
        try:
            page.wait_for_selector('input[type="password"], #password-input', timeout=20000)
        except Exception as e: log("no_pwd", err=str(e)[:80]); shoot(page, "no_pwd"); return
        pwd_sel = '#password-input' if page.locator('#password-input').count() else 'input[type="password"]'
        page.click(pwd_sel); hp(0.4, 1.0)
        page.type(pwd_sel, account['tm_password'], delay=80); log("pwd_typed")
        hp(1.5, 2.5)
        for txt in ["Iniciar sesión","Sign In","Login","Continuar"]:
            try:
                b = page.get_by_role("button", name=re.compile(txt, re.I)).first
                if b.is_visible(timeout=1500):
                    b.click(force=True); log("login_submitted", txt=txt); break
            except: continue
        page.wait_for_timeout(8000); shoot(page, "post_login_submit")

        # 3) OTP si aparece
        try:
            otp = page.locator('input[name*="code" i], input[autocomplete*="one-time" i], input[type="tel"][maxlength]').first
            if otp.is_visible(timeout=3000):
                from datetime import datetime
                log("otp_required"); shoot(page, "otp_screen")
                since = datetime.now()
                code = imap_otp(account['contact_email'], account['contact_password'], since, 180)
                if code:
                    otp.fill(code); page.wait_for_timeout(800)
                    for txt in ["Verificar","Confirmar","Continuar"]:
                        try:
                            b = page.get_by_role("button", name=re.compile(txt, re.I)).first
                            if b.is_visible(timeout=1500): b.click(force=True); break
                        except: continue
                    page.wait_for_timeout(6000)
        except: pass

        # 4) Watcher checkout
        log("watching_for_checkout")
        prev = page.url
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(2)
            try:
                cur = page.url
                if cur != prev:
                    log("URL_CHANGED", to=cur[:120])
                    prev = cur
                    page.wait_for_timeout(3000)
                    u = cur.lower()
                    if any(k in u for k in ['checkout','payment','cart','pago']):
                        log("checkout_detected"); fill_card(page, card, account)
                        log("READY_FOR_USER_CONFIRMATION", url=page.url)
                        print("\n" + "="*70)
                        print("✅ TARJETA AUTORELLENADA. Confirma manualmente con Pagar.")
                        print(f"URL: {page.url}")
                        print("Para cerrar: crea archivo _compra_u1.STOP")
                        print("="*70)
                        flag = os.path.join(os.path.dirname(__file__), "_compra_u1.STOP")
                        while not os.path.exists(flag): time.sleep(5)
                        os.remove(flag); log("stop"); return
            except Exception as e: log("watch_err", err=str(e)[:80])

if __name__ == '__main__': main()
