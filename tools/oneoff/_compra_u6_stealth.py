"""Flow ultra-humanizado User-6 con anti-detección agresiva.
Objetivo: evadir NuData/HUMAN sin captcha solver.

Tácticas:
1. Anti-bot init scripts (navigator.webdriver, plugins, chrome, permissions, screen)
2. Pausas largas y aleatorias entre acciones (3-12s)
3. Mouse moves naturales con steps antes de cada click
4. Scroll suave entre acciones
5. Path realista: home TM → search → evento (no goto directo)
"""
from __future__ import annotations
import sys, io, time, os, json, re, requests, random
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

PORT = int(os.environ.get('CDP_PORT', '58783'))
USER_LABEL = os.environ.get('USER_LABEL', 'User-6')
EVENT_URL = "https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961"
SHOTDIR = os.path.join(os.path.dirname(__file__), "datos", "compra_test")

def log(s, **k):
    print(f"[{time.strftime('%H:%M:%S')}] {s}", json.dumps(k, ensure_ascii=False) if k else "", flush=True)

def shoot(page, name):
    p = os.path.join(SHOTDIR, f"{int(time.time())}_{name}.png")
    try: page.screenshot(path=p, full_page=False); log("shot", file=os.path.basename(p))
    except: pass

import account_manager as am
account = next(a for a in am.account_manager.list_accounts() if a['label'].startswith(USER_LABEL))
card = next(c for c in am.account_manager.list_cards() if c.get('account_id') == account['id'])
log("user", email=account['tm_email'], label=USER_LABEL)

# Pausas humanas
def hp(a=2.5, b=5.5): time.sleep(random.uniform(a, b))
def hp_long(): time.sleep(random.uniform(7, 14))

ANTI_DETECT_JS = """
// Override navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
// Plugins reales (Chrome típico)
Object.defineProperty(navigator, 'plugins', {
    get: () => [{name:'PDF Viewer', filename:'internal-pdf-viewer'},
                {name:'Chrome PDF Viewer', filename:'internal-pdf-viewer'},
                {name:'Chromium PDF Viewer', filename:'internal-pdf-viewer'},
                {name:'Microsoft Edge PDF Viewer', filename:'internal-pdf-viewer'},
                {name:'WebKit built-in PDF', filename:'internal-pdf-viewer'}],
    configurable: true
});
// Languages
Object.defineProperty(navigator, 'languages', {get: () => ['es-ES','es','en-US','en'], configurable: true});
// chrome.runtime existe
window.chrome = window.chrome || {runtime:{}, loadTimes:()=>({}), csi:()=>({})};
// Permissions query devuelve normal (no denied como un bot)
const origQuery = navigator.permissions && navigator.permissions.query;
if (origQuery) {
    navigator.permissions.query = (params) => params && params.name === 'notifications'
        ? Promise.resolve({state: Notification.permission || 'prompt'})
        : origQuery.apply(navigator.permissions, [params]);
}
// WebGL vendor (Chrome/Windows típico)
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return _getParameter.call(this, p);
};
// Battery API (algunos detectores lo usan)
if (!navigator.getBattery) {
    navigator.getBattery = () => Promise.resolve({charging:true, chargingTime:0, dischargingTime:Infinity, level:1.0, addEventListener:()=>{}, removeEventListener:()=>{}});
}
// Bloquear Notification
if (window.Notification) {
    Notification.requestPermission = () => Promise.resolve('default');
}
"""

def human_mouse_move(page, target_box):
    """Movimiento de mouse natural antes de click."""
    if not target_box: return
    cx = target_box.get('x', 0) + target_box.get('width', 0) / 2
    cy = target_box.get('y', 0) + target_box.get('height', 0) / 2
    # Path con 2-3 puntos intermedios + steps suaves
    steps = random.randint(15, 30)
    page.mouse.move(cx + random.randint(-50, 50), cy + random.randint(-50, 50), steps=steps)
    time.sleep(random.uniform(0.2, 0.5))
    page.mouse.move(cx, cy, steps=random.randint(8, 15))
    time.sleep(random.uniform(0.1, 0.3))

def human_click(page, locator, label="?"):
    try:
        locator.scroll_into_view_if_needed(timeout=4000)
        time.sleep(random.uniform(0.4, 1.2))
        bbox = locator.bounding_box()
        if bbox: human_mouse_move(page, bbox)
        locator.click(force=True, timeout=5000)
        log("hclick", label=label)
        return True
    except Exception as e:
        log("hclick_err", label=label, err=str(e)[:80])
        return False

def human_type(page, sel, text):
    page.click(sel)
    time.sleep(random.uniform(0.3, 0.8))
    # Type char a char con delay variable
    for c in text:
        page.keyboard.type(c)
        time.sleep(random.uniform(0.05, 0.18))

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
    log("fill_card_start"); time.sleep(4); shoot(page, "checkout_initial")
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
        for sel in ['input[name*="holder" i]','input[id*="holderName" i]','input[autocomplete="cc-name"]']:
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
        # Anti-detect init script ANTES de cualquier navegación
        try: ctx.add_init_script(ANTI_DETECT_JS); log("anti_detect_injected")
        except Exception as e: log("anti_detect_err", err=str(e)[:80])

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        # 0) Visitar homepage TM primero (patrón humano)
        log("step0_homepage")
        page.goto("https://www.ticketmaster.es/", wait_until="domcontentloaded", timeout=30000)
        hp_long()  # 7-14s leyendo home
        # Aceptar cookies
        for txt in ["Aceptar todas las cookies","Aceptar","Accept"]:
            try:
                bt = page.get_by_text(txt, exact=False).first
                if bt.is_visible(timeout=1500):
                    human_click(page, bt, f"cookies_{txt}"); break
            except: continue
        # Scroll para parecer interesado
        for _ in range(3):
            page.evaluate(f"window.scrollBy(0, {random.randint(200, 500)})")
            hp(0.8, 1.8)
        hp(2, 4)

        # 1) Goto evento (vía URL directa pero tras humanizar)
        log("step1_goto_event")
        page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
        hp_long()
        shoot(page, "stealth_event_loaded")

        # 2) Click "Ver lo mejor disponible" — con JS click (que funciona) + esperar panel
        log("step2_ver_lo_mejor")
        page.evaluate("""() => {
            const b = Array.from(document.querySelectorAll('button')).find(x => /ver lo mejor disponible/i.test(x.innerText||''));
            if (b) { b.scrollIntoView({block:'center'}); b.click(); }
        }""")
        hp(2, 4)
        # Esperar panel offers (steppers visibles) hasta 15s
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=\"quantityStepper\"]').length >= 3",
                timeout=15000
            )
            log("offer_panel_loaded")
        except Exception as e:
            log("offer_panel_timeout", err=str(e)[:80])
        hp(2, 4)

        # 3) Subir qty Vis. Reducida — clicks ESPACIADOS humanos (1.5-3s entre cada +)
        log("step3_qty_4_vis_reducida")
        for i in range(8):
            cur = page.evaluate("""() => {
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[2];
                return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
            }""")
            log("qty", current=cur)
            if cur >= 4: break
            # Click + del stepper Vis. Reducida con coords de mouse
            box = page.evaluate("""() => {
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[2];
                if (!s) return null;
                const btns = s.querySelectorAll('button');
                const plus = btns[btns.length-1] || btns[0];
                const r = plus.getBoundingClientRect();
                return {x:r.x, y:r.y, width:r.width, height:r.height};
            }""")
            if box:
                human_mouse_move(page, box)
                page.mouse.click(box['x']+box['width']/2, box['y']+box['height']/2)
            hp(1.2, 2.8)
        hp(3, 5)
        shoot(page, "stealth_after_qty4")

        # 4) Click Entradas verde
        log("step4_entradas_verde")
        try:
            btn = page.locator('button[data-testid="findTicketsBtn"]').first
            human_click(page, btn, "findTickets")
        except Exception as e: log("entradas_err", err=str(e)[:80])
        hp(5, 9)
        shoot(page, "stealth_after_entradas")

        # 5) Marcar Acepto checkbox
        log("step5_acepto")
        page.evaluate("window.scrollBy(0, 200)"); hp(0.8, 1.5)
        r = page.evaluate("""() => {
            const labels = Array.from(document.querySelectorAll('label'));
            const lbl = labels.find(l => /acepto que he le[ií]do/i.test(l.textContent||''));
            if (!lbl) return {ok:false};
            const forId = lbl.getAttribute('for');
            let cb = forId ? document.getElementById(forId) : null;
            if (!cb) cb = lbl.querySelector('input[type=checkbox]');
            if (cb) {
                cb.scrollIntoView({block:'center'});
                cb.click();
                return {ok:true, checked:cb.checked};
            }
            return {ok:false};
        }""")
        log("acepto_result", **r)
        hp(2, 4)

        # 6) Continuar comprando
        log("step6_continuar")
        try:
            btn = page.get_by_role("button", name=re.compile(r"^continuar comprando$", re.I)).first
            human_click(page, btn, "continuar_comprando")
        except Exception as e: log("continuar_err", err=str(e)[:80])
        hp(8, 14)  # pausa larga para que TM redirija

        # 7) Detectar página actual
        # Buscar pestaña auth.ticketmaster
        all_pages = list(ctx.pages)
        log("pages_after_continuar", n=len(all_pages))
        auth_pages = [p for p in all_pages if 'auth.ticketmaster' in (p.url or '')]
        if auth_pages:
            page = auth_pages[-1]
            page.bring_to_front()
            log("switched_to_auth_tab", url=page.url[:90], title=page.title()[:60])

        # 8) Verificar si hay challenge anti-bot
        title = page.title()
        if 'paused' in title.lower() or 'browsing activity' in title.lower():
            log("ANTI_BOT_CHALLENGE_DETECTED", title=title)
            shoot(page, "stealth_challenge_detected")
            print("\n" + "="*70)
            print("⚠️ ANTI-BOT CHALLENGE detectado pese a stealth.")
            print("Resuelve el challenge MANUALMENTE en el browser (1 click).")
            print("Después del click, dime 'ya' y completo login + tarjeta.")
            print("="*70)
            flag = os.path.join(os.path.dirname(__file__), "_compra_u1.STOP")
            while not os.path.exists(flag): time.sleep(5)
            os.remove(flag); log("user_resolved")

        # 9) Login con human_type (no fill, no type rápido)
        log("step9_login")
        hp(2, 4)
        try:
            page.wait_for_selector('#email-input, input[type="email"]', timeout=15000)
        except:
            log("no_email_input"); shoot(page, "no_email"); return
        email_sel = '#email-input' if page.locator('#email-input').count() else 'input[type="email"]'
        human_type(page, email_sel, account['tm_email'])
        log("email_typed_humanized")
        hp(1.5, 3)
        try:
            btn = page.get_by_role("button", name=re.compile(r"^continuar$", re.I)).first
            human_click(page, btn, "email_continue")
        except: pass
        hp(4, 7)

        try:
            page.wait_for_selector('#password-input, input[type="password"]', timeout=20000)
        except:
            log("no_pwd_input"); shoot(page, "no_pwd"); return
        pwd_sel = '#password-input' if page.locator('#password-input').count() else 'input[type="password"]'
        human_type(page, pwd_sel, account['tm_password'])
        log("pwd_typed_humanized")
        hp(2, 4)
        for txt in ["Iniciar sesión","Sign In","Login","Continuar"]:
            try:
                btn = page.get_by_role("button", name=re.compile(txt, re.I)).first
                if btn.is_visible(timeout=1000):
                    human_click(page, btn, f"login_submit:{txt}"); break
            except: continue
        time.sleep(8); shoot(page, "stealth_post_login")

        # 10) OTP si pide
        try:
            otp = page.locator('input[name*="code" i], input[autocomplete*="one-time" i], input[type="tel"][maxlength]').first
            if otp.is_visible(timeout=3000):
                from datetime import datetime
                log("otp_required"); shoot(page, "otp_screen")
                since = datetime.now()
                code = imap_otp(account['contact_email'], account['contact_password'], since, 180)
                if code:
                    otp.fill(code); time.sleep(1)
                    for txt in ["Verificar","Confirmar","Continuar"]:
                        try:
                            btn = page.get_by_role("button", name=re.compile(txt, re.I)).first
                            if btn.is_visible(timeout=1500): human_click(page, btn, f"otp:{txt}"); break
                        except: continue
                    time.sleep(5)
        except: pass

        # 11) Watch checkout
        log("watching_for_checkout")
        prev = page.url
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(2)
            try:
                cur = page.url
                if cur != prev:
                    log("URL_CHANGED", to=cur[:120]); prev = cur
                    page.wait_for_timeout(3000)
                    u = cur.lower()
                    if any(k in u for k in ['checkout','payment','cart','pago']):
                        log("checkout_detected"); fill_card(page, card, account)
                        log("READY_FOR_USER_CONFIRMATION", url=page.url)
                        print("\n" + "="*70)
                        print("✅ TARJETA AUTORELLENADA. Confirma manual con Pagar.")
                        print(f"URL: {page.url}")
                        print("Para cerrar: crea archivo _compra_u1.STOP")
                        print("="*70)
                        flag = os.path.join(os.path.dirname(__file__), "_compra_u1.STOP")
                        while not os.path.exists(flag): time.sleep(5)
                        os.remove(flag); log("stop"); return
            except Exception as e: log("watch_err", err=str(e)[:80])

if __name__ == '__main__': main()
