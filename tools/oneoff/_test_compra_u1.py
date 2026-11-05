"""Compra de prueba: 4 entradas El Último de la Fila con User-1.
- Perfil MoreLogin nuevo
- Browser visible
- Login User-1 con OTP via IMAP madre joaquina_polo0
- Selección 4 entradas (modo quick smart, fallback seatmap)
- Avance hasta checkout
- Autorelleno datos personales + tarjeta
- PAUSA antes del botón final (usuario confirma manual)
"""
from __future__ import annotations
import sys, io, time, os, json, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

EVENT_TM_ID = "2059118961"
EVENT_URL = "https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961"
QUANTITY = 4
USER_LABEL_PREFIX = "User-1"
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "datos", "compra_test")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

import account_manager as am
import bot_orchestrator as bo
import ticket_selector
import cart_handler

def log(stage, **kw):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {stage}", json.dumps(kw, ensure_ascii=False) if kw else "", flush=True)

def get_user1():
    accs = am.account_manager.list_accounts()
    cards = am.account_manager.list_cards()
    a = next((x for x in accs if x.get('label','').startswith(USER_LABEL_PREFIX)), None)
    if not a: raise RuntimeError("User-1 no encontrado")
    c = next((y for y in cards if y.get('account_id') == a['id']), None)
    if not c: raise RuntimeError("Tarjeta de User-1 no encontrada")
    return a, c

def shoot(page, name):
    p = os.path.join(SCREENSHOT_DIR, f"{int(time.time())}_{name}.png")
    try: page.screenshot(path=p, full_page=False); log("screenshot", file=os.path.basename(p))
    except Exception as e: log("screenshot_fail", err=str(e)[:80])

def imap_get_otp(email_madre, pwd, since_ts, max_wait=120):
    """Polling IMAP buscando OTP de TM (código 6 dígitos en últimos correos)."""
    from imap_tools import MailBox, AND
    from datetime import datetime, timedelta
    deadline = time.time() + max_wait
    pattern = re.compile(r'\b(\d{6})\b')
    last_count = 0
    while time.time() < deadline:
        try:
            with MailBox('imap.mail.me.com').login(email_madre, pwd) as mb:
                mb.folder.set('INBOX')
                msgs = list(mb.fetch(AND(date_gte=since_ts.date()), limit=20, reverse=True, mark_seen=False))
                tm_msgs = [m for m in msgs if 'ticketmaster' in (m.from_ or '').lower()
                           and (m.date and m.date.replace(tzinfo=None) >= since_ts.replace(tzinfo=None))]
                if len(tm_msgs) != last_count:
                    log("imap_poll", total=len(msgs), tm_recent=len(tm_msgs))
                    last_count = len(tm_msgs)
                for m in tm_msgs:
                    body = (m.text or '') + ' ' + (m.subject or '')
                    matches = pattern.findall(body)
                    for code in matches:
                        if 100000 <= int(code) <= 999999:
                            log("otp_found", code=code, subject=(m.subject or '')[:60])
                            return code
        except Exception as e:
            log("imap_err", err=str(e)[:100])
        time.sleep(8)
    return None

def login_tm(page, account):
    """Click en botón Login → email → password → manejar OTP/2FA si aparece."""
    log("login_start", email=account['tm_email'])
    # Buscar botón Sign In / Iniciar sesión
    for txt in ["Iniciar sesión", "Sign In", "Sign in", "Mi cuenta"]:
        try:
            btn = page.get_by_text(txt, exact=False).first
            if btn.is_visible(timeout=1500):
                btn.click(); break
        except Exception: continue
    page.wait_for_timeout(3000)
    # Email
    try:
        email_input = page.locator('input[type="email"], input[name*="email" i], input[id*="email" i]').first
        email_input.fill(account['tm_email']); page.wait_for_timeout(800)
    except Exception as e: log("email_fill_err", err=str(e)[:80])
    # Continuar
    for txt in ["Continuar", "Continue", "Siguiente", "Next"]:
        try:
            b = page.get_by_role("button", name=re.compile(txt, re.I)).first
            if b.is_visible(timeout=1500): b.click(); break
        except Exception: continue
    page.wait_for_timeout(2500)
    # Password
    try:
        pwd_input = page.locator('input[type="password"]').first
        pwd_input.fill(account['tm_password']); page.wait_for_timeout(800)
    except Exception as e: log("pwd_fill_err", err=str(e)[:80])
    # Sign in
    for txt in ["Iniciar sesión", "Sign In", "Iniciar", "Continuar"]:
        try:
            b = page.get_by_role("button", name=re.compile(txt, re.I)).first
            if b.is_visible(timeout=1500): b.click(); break
        except Exception: continue
    page.wait_for_timeout(5000)
    # Si aparece OTP code box
    try:
        otp_box = page.locator('input[name*="code" i], input[autocomplete*="one-time" i], input[type="tel"][maxlength="6"]').first
        if otp_box.is_visible(timeout=3000):
            from datetime import datetime
            log("otp_required")
            shoot(page, "otp_screen")
            since = datetime.now()
            code = imap_get_otp(account['contact_email'], account['contact_password'], since, max_wait=120)
            if code:
                otp_box.fill(code); page.wait_for_timeout(800)
                for txt in ["Verificar", "Verify", "Continuar", "Confirmar"]:
                    try:
                        b = page.get_by_role("button", name=re.compile(txt, re.I)).first
                        if b.is_visible(timeout=1500): b.click(); break
                    except Exception: continue
                page.wait_for_timeout(4000)
                return {"ok": True, "otp_used": True}
            else:
                return {"ok": False, "reason": "otp_not_received"}
    except Exception:
        pass
    return {"ok": True, "otp_used": False}

def fill_card(page, card, account):
    """Autorelleno de tarjeta + datos personales en checkout. NO confirma."""
    log("fill_card_start")
    page.wait_for_timeout(3000)
    shoot(page, "checkout_initial")
    # Iframes Adyen / TM checkout: probar tanto en main como en frames
    targets = [page] + list(page.frames)
    holder = card.get('holder', '')
    number = re.sub(r'\D', '', card.get('number', ''))
    month = card.get('month', '')
    year = card.get('year', '')
    cvv = card.get('cvv', '')
    # Year puede pedirse 2-dígito o 4-dígito
    if year and len(year) == 2: year_full = "20" + year
    else: year_full = year

    def try_fill(loc, value, label):
        try:
            if loc.is_visible(timeout=1000):
                loc.fill(value); log("filled", field=label, val=(value[:4]+'***' if len(value)>6 else value))
                return True
        except Exception: pass
        return False

    filled_any = False
    for t in targets:
        # Card number
        for sel in ['input[name*="number" i]','input[id*="cardNumber" i]','input[autocomplete="cc-number"]','input[data-cse="encryptedCardNumber"]']:
            if try_fill(t.locator(sel).first, number, "card_number"): filled_any = True; break
        # Holder
        for sel in ['input[name*="holder" i]','input[id*="holderName" i]','input[autocomplete="cc-name"]','input[name*="cardholder" i]']:
            if try_fill(t.locator(sel).first, holder, "holder"): break
        # Expiry MM/YY combinado
        for sel in ['input[name*="expiry" i]','input[id*="expiryDate" i]','input[autocomplete="cc-exp"]','input[data-cse="encryptedExpiryDate"]']:
            if try_fill(t.locator(sel).first, f"{month}/{year[-2:]}", "expiry"): break
        # Mes/Año por separado
        for sel in ['input[name*="month" i]','input[id*="expiryMonth" i]','input[autocomplete="cc-exp-month"]']:
            if try_fill(t.locator(sel).first, month, "exp_month"): break
        for sel in ['input[name*="year" i]','input[id*="expiryYear" i]','input[autocomplete="cc-exp-year"]']:
            if try_fill(t.locator(sel).first, year_full, "exp_year"): break
        # CVV
        for sel in ['input[name*="cvv" i]','input[name*="cvc" i]','input[id*="securityCode" i]','input[autocomplete="cc-csc"]','input[data-cse="encryptedSecurityCode"]']:
            if try_fill(t.locator(sel).first, cvv, "cvv"): break
    # Datos personales (a veces TM pide nombre/dirección antes de pago)
    for t in targets:
        try_fill(t.locator('input[name*="firstName" i], input[id*="firstName" i]').first, account.get('first_name',''), "first_name")
        try_fill(t.locator('input[name*="lastName" i], input[id*="lastName" i]').first, account.get('last_name',''), "last_name")
        try_fill(t.locator('input[name*="phone" i]').first, (account.get('phone_prefix','')+account.get('phone','')), "phone")
        try_fill(t.locator('input[name*="address" i], input[name*="street" i]').first, account.get('address1',''), "address")
        try_fill(t.locator('input[name*="city" i]').first, account.get('city',''), "city")
        try_fill(t.locator('input[name*="postal" i], input[name*="zip" i]').first, account.get('zip',''), "zip")
    log("fill_card_done", filled_card=filled_any)
    shoot(page, "checkout_filled")

def main():
    account, card = get_user1()
    log("user_loaded", email=account['tm_email'], card_label=card.get('label'))
    log("event", url=EVENT_URL, qty=QUANTITY)

    # 1) Perfil MoreLogin nuevo
    prof = bo._pick_fresh_profile(country_code="ES", max_pages=20, page_size=100, allow_unverified=True)
    if not prof:
        log("FAIL", reason="no_es_profile_available"); return
    log("profile_picked", profile=prof['profile'], env_id=prof['env_id'], ip=prof.get('ip'))

    # 2) Iniciar perfil
    port = bo._start_profile(prof['env_id'])
    if not port:
        log("FAIL", reason="cannot_start"); return
    log("profile_started", port=port)

    import requests
    from playwright.sync_api import sync_playwright
    time.sleep(5)
    v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()

    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
        ctx = b.contexts[0] if b.contexts else b.new_context()
        try:
            ctx.add_init_script("""
                if (window.Notification) {
                    Notification.requestPermission = () => Promise.resolve('denied');
                    try { Object.defineProperty(Notification,'permission',{get:()=>'denied',configurable:true}); } catch(e){}
                }
            """)
        except Exception: pass

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        # 3) Goto event
        log("goto", url=EVENT_URL)
        page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(7000)

        # 4) Cookies
        for txt in ["Aceptar todas las cookies", "Aceptar", "Accept"]:
            try:
                btn = page.get_by_text(txt, exact=False).first
                if btn.is_visible(timeout=1500):
                    btn.click(); page.wait_for_timeout(1500); log("cookies_accepted"); break
            except Exception: continue
        ticket_selector._block_notifications(page)
        shoot(page, "event_loaded")

        # 5) LOGIN — si TM permite, mejor login antes de seleccionar
        try:
            log("attempting_login_pre_select")
            login_tm(page, account)
            page.wait_for_timeout(3000)
            shoot(page, "post_login")
        except Exception as e:
            log("login_skip", err=str(e)[:100])

        # 6) Volver a página del evento si nos fue a otro lado tras login
        if EVENT_TM_ID not in page.url:
            log("re_goto", current=page.url)
            page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)

        # 7) Selección 4 entradas
        log("selecting", qty=QUANTITY)
        sel = ticket_selector.select_quick_smart(page, qty=QUANTITY, prefer_offer_indices=None)
        log("selection_result", **{k:v for k,v in sel.items() if k!='debug'})
        shoot(page, "after_selection")
        if not sel.get("ok"):
            log("FAIL", reason="select_failed")
            flag = os.path.join(os.path.dirname(__file__), "_compra_u1.STOP")
            while not os.path.exists(flag): time.sleep(5)
            os.remove(flag); return

        # 8) Proceed to checkout
        log("proceed_to_checkout")
        proc = cart_handler.proceed_to_checkout(page, click_attempts=2, post_click_wait_s=12)
        log("destination", **{k:v for k,v in proc.items() if k!='debug'})
        shoot(page, "after_proceed")

        destination = proc.get("destination") or "unknown"
        # 9) Si pide login en este punto
        if destination == "auth":
            log("auth_required_post_proceed")
            login_tm(page, account)
            page.wait_for_timeout(5000)
            destination = cart_handler.detect_destination(page).get("destination") or "unknown"
            log("destination_post_login", destination=destination)
            shoot(page, "after_auth")

        # 10) Llegado a checkout: autorelleno tarjeta
        if destination == "checkout" or "checkout" in (page.url or '').lower():
            fill_card(page, card, account)
        else:
            log("WARN", note=f"destination={destination} no es checkout. Pausando para revisar.")

        log("READY_FOR_USER_CONFIRMATION", url=page.url)
        print("\n" + "="*60)
        print("BROWSER LISTO. Revisa la pantalla:")
        print(f"  · 4 entradas seleccionadas")
        print(f"  · Cuenta: {account['tm_email']}")
        print(f"  · Tarjeta: ****{re.sub(chr(92)+'D','',card['number'])[-4:]}")
        print(f"  · URL actual: {page.url}")
        print("\nNo se hace clic en 'Pagar'/'Confirmar pedido'.")
        print("Revisa los datos manualmente y confirma tú cuando quieras.")
        print("="*60)
        flag = os.path.join(os.path.dirname(__file__), "_compra_u1.STOP")
        log("waiting_for_user", flag_path=flag, hint=f"crea el archivo {flag} para cerrar")
        while not os.path.exists(flag):
            time.sleep(5)
        log("user_signaled_stop"); os.remove(flag)

if __name__ == '__main__':
    main()
