"""Watcher: deja el browser en qty=4 Vis. Reducida + vigila cambio de URL.
Cuando usuario hace clicks manuales (checkbox Acepto + Continuar comprando),
detecta navegación a checkout/auth → login User-1 + autorelleno tarjeta + PAUSA."""
from __future__ import annotations
import sys, io, time, os, json, re, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

PORT = int(os.environ.get('CDP_PORT', '60937'))
SHOTDIR = os.path.join(os.path.dirname(__file__), "datos", "compra_test")
EVENT_URL = "https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961"

def log(stage, **kw):
    print(f"[{time.strftime('%H:%M:%S')}] {stage}", json.dumps(kw, ensure_ascii=False) if kw else "", flush=True)

def shoot(page, name):
    p = os.path.join(SHOTDIR, f"{int(time.time())}_{name}.png")
    try: page.screenshot(path=p, full_page=False); log("shot", file=os.path.basename(p))
    except Exception as e: log("shot_err", err=str(e)[:80])

import account_manager as am
account = next(a for a in am.account_manager.list_accounts() if a['label'].startswith('User-1'))
card = next(c for c in am.account_manager.list_cards() if c.get('account_id') == account['id'])

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
    try: page.wait_for_selector('input[type="email"]', timeout=20000)
    except: log("no_email_input"); return False
    page.fill('input[type="email"]', account['tm_email']); log("email_filled")
    page.wait_for_timeout(1000)
    for txt in ["Continuar","Continue","Siguiente","Next"]:
        try:
            b = page.get_by_role("button", name=re.compile(txt, re.I)).first
            if b.is_visible(timeout=1500): b.click(force=True, timeout=3000); break
        except: continue
    page.wait_for_timeout(4000)
    try: page.wait_for_selector('input[type="password"]', timeout=15000)
    except: log("no_pwd_input"); return False
    page.fill('input[type="password"]', account['tm_password']); log("pwd_filled")
    page.wait_for_timeout(1000)
    for txt in ["Iniciar sesión","Sign In","Login","Continuar"]:
        try:
            b = page.get_by_role("button", name=re.compile(txt, re.I)).first
            if b.is_visible(timeout=1500): b.click(force=True, timeout=3000); break
        except: continue
    page.wait_for_timeout(7000); shoot(page, "watch_post_login")
    try:
        otp = page.locator('input[name*="code" i], input[autocomplete*="one-time" i], input[type="tel"][maxlength]').first
        if otp.is_visible(timeout=3000):
            from datetime import datetime
            log("otp_required"); shoot(page, "watch_otp_screen")
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
    log("fill_card_start"); page.wait_for_timeout(4000); shoot(page, "watch_checkout_initial")
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
    shoot(page, "watch_checkout_filled")

def main():
    v = requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=8).json()
    log("connected_cdp", port=PORT)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
        ctx = b.contexts[0]
        page = next((p for p in ctx.pages if 'ticketmaster' in (p.url or '').lower()), ctx.pages[0])
        page.bring_to_front()
        log("page", url=page.url)

        # Asegurar estado: navegar al evento + click "Ver lo mejor disponible" + qty=4 Vis. Reducida
        if "2059118961" not in page.url:
            page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(7000)
        page.evaluate("""() => {
            const b = Array.from(document.querySelectorAll('button')).find(x => /ver lo mejor disponible/i.test(x.innerText||''));
            if (b) b.click();
        }""")
        page.wait_for_timeout(3500)
        # Aplicar qty=4 a Vis. Reducida si no está ya
        for i in range(8):
            cur = page.evaluate("""() => {
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[2];
                return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
            }""")
            log("vis_qty", current=cur)
            if cur >= 4: break
            page.evaluate("""() => {
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[2];
                if (!s) return;
                const btns = s.querySelectorAll('button');
                (btns[btns.length-1] || btns[0]).click();
            }""")
            page.wait_for_timeout(700)

        log("READY_FOR_USER_MANUAL_STEP")
        print("\n" + "="*70)
        print("✅ Estado: 4 entradas Vis. Reducida (88€×4 = 352€) seleccionadas")
        print("")
        print("AHORA TÚ EN EL BROWSER MOREOLOGIN:")
        print("  1) Marca el checkbox 'Acepto que he leído esta información'")
        print("  2) Click en el botón 'Continuar comprando' (o 'Entradas')")
        print("")
        print("El bot detectará el cambio de URL automáticamente y continuará")
        print("con login (User-1 abseil-medical-2p) + OTP IMAP joaquina_polo0 +")
        print("autorelleno tarjeta. PAUSA antes de Pagar.")
        print("="*70)

        # Vigilar cambio de URL cada 2s
        prev = page.url
        deadline = time.time() + 600  # max 10min
        while time.time() < deadline:
            time.sleep(2)
            try:
                cur = page.url
                if cur != prev:
                    log("URL_CHANGED", from_=prev, to=cur)
                    prev = cur
                    page.wait_for_timeout(3000)
                    u = cur.lower()
                    if any(k in u for k in ['identity','login','auth','signin','my.tick']):
                        log("auth_detected"); login_full(page, account)
                        page.wait_for_timeout(4000)
                        u = page.url.lower()
                    if any(k in u for k in ['checkout','payment','cart','pago']):
                        log("checkout_detected"); fill_card(page, card, account)
                        log("READY_FOR_USER_CONFIRMATION", url=page.url)
                        print("\n" + "="*70)
                        print("✅ TARJETA AUTORELLENADA. Revisa y pulsa Pagar manualmente.")
                        print(f"URL: {page.url}")
                        print("Para cerrar: crea archivo _compra_u1.STOP")
                        print("="*70)
                        # Esperar STOP
                        flag = os.path.join(os.path.dirname(__file__), "_compra_u1.STOP")
                        while not os.path.exists(flag): time.sleep(5)
                        os.remove(flag); log("user_stop"); return
            except Exception as e:
                log("watch_err", err=str(e)[:80])
        log("TIMEOUT")

if __name__ == '__main__': main()
