"""tm_camoufox_purchase.py — Bot TM con Camoufox (Firefox+Juggler) en lugar de MoreLogin/Chromium.

Camoufox usa Firefox con parches a nivel C++ y protocolo Juggler (NO CDP).
Ataca el vector "CDP Protocol Detection" que es el #1 detector de NuData en 2026.

Flow idéntico a tm_purchase_clean.py:
1. Camoufox launch con proxy QuantumProxies + GeoIP ES + locale ES + Win11 fingerprint
2. Pre-warm humano 40-60s
3. Goto evento → ver lo mejor → +4 Vis. Reducida → Acepto → Continuar
4. OAuth login con tipeo humanizado + 2Captcha Enterprise v3
5. OTP via IMAP IDLE
6. Checkout → autorelleno tarjeta → PAUSA antes de Pagar

Usage:
    USER_LABEL=User-3 python -u tm_camoufox_purchase.py
"""
from __future__ import annotations
import os, time, json, re, random, math, sys, io
from datetime import datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

EVENT_URL = os.environ.get('EVENT_URL', 'https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961')
USER_LABEL = os.environ.get('USER_LABEL', 'User-3')
QTY = int(os.environ.get('QTY', '4'))
OFFER_IDX = int(os.environ.get('OFFER_IDX', '2'))  # 2 = Vis.Reducida
SHOTDIR = os.path.join(os.path.dirname(__file__), "datos", "compra_test")
os.makedirs(SHOTDIR, exist_ok=True)


def log(s, **k):
    print(f"[{time.strftime('%H:%M:%S')}] {s}", json.dumps(k, ensure_ascii=False) if k else "", flush=True)


def shoot(page, name):
    p = os.path.join(SHOTDIR, f"{int(time.time())}_camoufox_{name}.png")
    try: page.screenshot(path=p, full_page=False); log("shot", file=os.path.basename(p))
    except: pass


# === Bezier mouse ===
def _bezier_points(p0, p1, n=25):
    cx = (p0[0]+p1[0])/2 + random.randint(-150,150)
    cy = (p0[1]+p1[1])/2 + random.randint(-100,100)
    pts = []
    for i in range(n+1):
        t = i/n
        x = (1-t)**2*p0[0] + 2*(1-t)*t*cx + t**2*p1[0]
        y = (1-t)**2*p0[1] + 2*(1-t)*t*cy + t**2*p1[1]
        pts.append((x,y))
    return pts


def human_mouse_to(page, x, y, last=None):
    if last is None: last = (random.randint(100,800), random.randint(100,600))
    n = random.randint(20,40)
    pts = _bezier_points(last, (x,y), n=n)
    for i,(px,py) in enumerate(pts):
        page.mouse.move(px+random.uniform(-0.5,0.5), py+random.uniform(-0.5,0.5))
        speed = math.sin(math.pi*i/n)
        time.sleep(random.uniform(0.005,0.015)*(1.5-speed))
    return (x,y)


def human_click(page, locator, last=None):
    """Click directo via Playwright — Camoufox.humanize=True ya añade mouse natural.
    NO usar bezier extra (doble overhead). Solo asegurar visible + pausa corta."""
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
        time.sleep(random.uniform(0.3, 0.7))
        locator.click(timeout=4000)
    except Exception:
        try: locator.click(force=True, timeout=2000)
        except Exception: pass
    return last


def human_type(page, sel, text):
    page.click(sel); time.sleep(random.uniform(0.2,0.5))
    page.keyboard.press('Control+A'); time.sleep(0.1)
    page.keyboard.press('Delete'); time.sleep(0.3)
    for c in text:
        page.keyboard.type(c)
        d = random.uniform(0.05,0.18)
        if c in ' .@-_!?': d += random.uniform(0.1,0.3)
        time.sleep(d)


def hp(a=0.6, b=1.5): time.sleep(random.uniform(a,b))
def hp_long(): time.sleep(random.uniform(2,4))


def accept_cookies(page, last=None):
    for txt_re in [r'^Aceptar todas las cookies', r'^Aceptar todo', r'^Accept all']:
        try:
            btn = page.get_by_role('button', name=re.compile(txt_re, re.I)).first
            if btn.is_visible(timeout=1500):
                last = human_click(page, btn, last); hp(0.8,1.6); return last
        except: continue
    return last


# === IMAP OTP ===
def imap_otp(em_madre, pw, since_ts, max_wait=600):
    try: from imapclient import IMAPClient
    except ImportError: pass
    from imap_tools import MailBox, AND
    deadline = time.time()+max_wait
    pat6 = re.compile(r'\b(\d{6})\b')
    while time.time() < deadline:
        try:
            with MailBox('imap.mail.me.com').login(em_madre, pw) as mb:
                folders = [f.name for f in mb.folder.list()]
                for fn in folders:
                    try: mb.folder.set(fn)
                    except: continue
                    try: msgs = list(mb.fetch(AND(date_gte=since_ts.date()), limit=20, reverse=True, mark_seen=False))
                    except: continue
                    tm = [m for m in msgs if 'ticketmaster' in (m.from_ or '').lower()
                          and m.date and m.date.replace(tzinfo=None) >= since_ts.replace(tzinfo=None)]
                    for m in tm:
                        body = (m.text or '')+'\n'+(m.html or '')
                        for c in pat6.findall(body):
                            if 100000 <= int(c) <= 999999:
                                log("otp_found", code=c, folder=fn); return c
        except Exception as e: log("imap_err", err=str(e)[:60])
        time.sleep(4)
    return None


def watch_for_3ds_and_autofill(page, timeout_s: int = 90):
    """Detecta la aparición del challenge 3DS y dispara el autofill OTP automáticamente.
    Usa otp_3ds_autofill.wait_and_inject() — IMAP poll + SMS webhook con todos los bancos
    activos. Si detecta selectores 3DS típicos (input[name*=otp], one-time-code, frame
    Adyen/CCA), invoca el orquestador.
    """
    try:
        import otp_3ds_autofill
    except Exception as e:
        log("otp3ds_module_err", err=str(e)[:80]); return False
    started = time.time()
    deadline = started + timeout_s
    detected = False
    selectors_3ds = [
        'input[autocomplete="one-time-code"]',
        'input[name*="otp" i]',
        'input[name*="challenge" i]',
        'input[id*="threeDS" i]',
        'iframe[name*="acs" i]',
        'iframe[src*="3ds" i]',
    ]
    while time.time() < deadline and not detected:
        try:
            for sel in selectors_3ds:
                try:
                    if page.locator(sel).first.is_visible(timeout=400):
                        log("3ds_challenge_detected", selector=sel); detected = True; break
                except Exception: continue
            for fr in page.frames:
                if any(k in (fr.url or '').lower() for k in ['3ds','acs','challenge','otp']):
                    log("3ds_frame_detected", url=(fr.url or '')[:120]); detected = True; break
            if not detected: time.sleep(1)
        except Exception: time.sleep(1)
    if not detected:
        log("3ds_no_challenge", elapsed=int(time.time() - started)); return False

    log("3ds_autofill_start")
    try:
        res = otp_3ds_autofill.wait_and_inject(page, timeout_s=60)
        log("3ds_autofill_result", **{k: v for k, v in res.items() if k != 'inject'})
        if res.get('ok') and res.get('inject', {}).get('ok'):
            log("3ds_otp_injected_ok"); return True
        log("3ds_autofill_failed"); return False
    except Exception as e:
        log("3ds_autofill_err", err=str(e)[:120]); return False


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
        for sel in ['input[autocomplete="cc-exp-month"]']:
            if tf(t.locator(sel).first, month, "exp_month"): break
        for sel in ['input[autocomplete="cc-exp-year"]']:
            if tf(t.locator(sel).first, yyyy, "exp_year"): break
        for sel in ['input[name*="cvv" i]','input[name*="cvc" i]','input[id*="securityCode" i]','input[autocomplete="cc-csc"]','input[data-cse="encryptedSecurityCode"]','input[aria-label*="cvv" i]']:
            if tf(t.locator(sel).first, cvv, "cvv"): break
    for t in targets:
        tf(t.locator('input[name*="firstName" i]').first, account.get('first_name',''), "first_name")
        tf(t.locator('input[name*="lastName" i]').first, account.get('last_name',''), "last_name")
        tf(t.locator('input[name*="phone" i]').first, account.get('phone_prefix','')+account.get('phone',''), "phone")
        tf(t.locator('input[name*="address" i]').first, account.get('address1',''), "address")
        tf(t.locator('input[name*="city" i]').first, account.get('city',''), "city")
        tf(t.locator('input[name*="postal" i]').first, account.get('zip',''), "zip")
    shoot(page, "checkout_filled")


def main():
    # Cargar cuenta
    import account_manager as am
    account = next(a for a in am.account_manager.list_accounts() if a['label'].startswith(USER_LABEL))
    card = next(c for c in am.account_manager.list_cards() if c.get('account_id') == account['id'])
    log("user_loaded", email=account['tm_email'], qty=QTY, offer_idx=OFFER_IDX)

    # Verificar 2Captcha balance ANTES de empezar (instrucción usuario)
    try:
        from twocaptcha import TwoCaptcha
        from tm_recaptcha_solver import _api_key
        bal = TwoCaptcha(_api_key()).balance()
        log("captcha_balance", balance_usd=round(bal,2))
        if bal < 0.5:
            log("captcha_balance_low", warning=True)
    except Exception as e: log("captcha_balance_err", err=str(e)[:60])

    # Camoufox: Firefox con anti-detect a nivel C++ + Juggler protocolo
    # ROTACIÓN DE PROXY: pickear sessionId aleatorio del pool QuantumProxies (instrucción usuario)
    from camoufox.sync_api import Camoufox
    proxy_cfg = None
    PROXY = os.environ.get('CAMOUFOX_PROXY')
    if PROXY:
        m = re.match(r'^http[s]?://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)', PROXY)
        if m:
            proxy_cfg = {"server": f"http://{m.group(3)}:{m.group(4)}"}
            if m.group(1): proxy_cfg["username"] = m.group(1); proxy_cfg["password"] = m.group(2)
    elif os.environ.get('USE_QUANTUM_PROXY') == '1':
        # Pickear línea aleatoria de proxies.txt para rotar sessionId
        try:
            with open(os.path.join(os.path.dirname(__file__), 'proxies.txt')) as f:
                lines = [l.strip() for l in f if l.strip()]
            line = random.choice(lines)  # host:port:user:pass
            parts = line.split(':')
            if len(parts) >= 4:
                proxy_cfg = {"server": f"http://{parts[0]}:{parts[1]}",
                             "username": parts[2], "password": parts[3]}
                log("proxy_rotated", host=parts[0], session=parts[2][-8:])
        except Exception as e: log("proxy_rotate_err", err=str(e)[:60])
    log("camoufox_launching", with_proxy=bool(proxy_cfg))

    with Camoufox(
        headless=False,
        os="windows",
        humanize=True,
        block_webrtc=True,
        geoip=True,
        locale=["es-ES", "es"],
        proxy=proxy_cfg,
    ) as browser:
        page = browser.new_page()
        log("camoufox_ready")
        ctx = browser.contexts[0] if browser.contexts else page.context

        # Verificar IP
        try:
            page.goto("https://api.ipify.org/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(1)
            ip_text = page.evaluate("() => document.body.innerText.trim()")
            log("ip_detected", ip=ip_text[:50])
        except Exception as e: log("ip_err", err=str(e)[:60])

        # === COOKIE INJECTION (si hay sesión válida guardada) ===
        from tm_cookie_store import has_valid_session, load_cookies, inject_cookies, harvest_cookies
        cookie_label = USER_LABEL
        already_logged_in = False
        if has_valid_session(cookie_label):
            data = load_cookies(cookie_label)
            r = inject_cookies(ctx, data)
            log("cookies_injected", **r, age_h=round((datetime.now()-datetime.fromisoformat(data['captured_at'])).total_seconds()/3600, 1) if False else "?")
            already_logged_in = True
        else:
            log("no_session_cookies", will_request_manual_login=True)

        # === FLOW ===
        last_pos = None

        # 1) Goto DIRECTO al evento + esperar hidratación Next.js
        log("goto_event")
        try:
            page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=25000)
            log("event_loaded")
        except Exception as e:
            log("event_goto_err", err=str(e)[:80])
        # Esperar que el botón clave esté en el DOM (Next.js hidrata ~3-5s)
        try:
            page.wait_for_function("""() => {
                const btns = document.querySelectorAll('button');
                return Array.from(btns).some(b => /ver lo mejor disponible/i.test(b.innerText||""));
            }""", timeout=15000)
            log("hydration_done")
        except: log("hydration_timeout")
        hp(0.8, 1.5)
        last_pos = accept_cookies(page, last_pos)
        shoot(page, "event_loaded")

        # 3) Click Ver lo mejor disponible (JS click - selecciona el visible)
        log("open_offer_panel")
        r = page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button')).filter(b => /ver lo mejor disponible/i.test(b.innerText||""));
            const visibles = btns.filter(b => { const r=b.getBoundingClientRect(); return r.width>100 && r.height>20; });
            const t = visibles[0] || btns[0];
            if (!t) return {ok:false};
            t.scrollIntoView({block:'center'}); t.click();
            return {ok:true, w: t.getBoundingClientRect().width};
        }""")
        log("ver_lo_mejor_click", **r)
        try:
            page.wait_for_function("() => document.querySelectorAll('[data-testid=\"quantityStepper\"]').length >= 3", timeout=12000)
            log("offer_panel_ready")
        except: log("offer_panel_timeout")

        # 4) Subir qty Vis. Reducida — JS evaluate click (funciona en Firefox/Camoufox)
        log("set_qty", target=QTY, idx=OFFER_IDX)
        for i in range(QTY + 2):
            cur = page.evaluate(f"""() => {{
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{OFFER_IDX}];
                return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
            }}""")
            log("qty_check", current=cur)
            if cur >= QTY: break
            r = page.evaluate(f"""() => {{
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{OFFER_IDX}];
                if (!s) return {{ok:false, reason:'no_stepper'}};
                const btns = s.querySelectorAll('button');
                if (!btns.length) return {{ok:false, reason:'no_buttons'}};
                const plus = btns[btns.length-1];
                if (plus.disabled) return {{ok:false, reason:'disabled'}};
                plus.scrollIntoView({{block:'center'}});
                plus.click();
                return {{ok:true}};
            }}""")
            if not r.get('ok'): log("plus_click_fail", **r)
            time.sleep(random.uniform(0.3, 0.6))
        hp(0.5, 1)

        # 5) Click Entradas (esperar transición a panel resumen)
        log("click_entradas")
        try:
            btn = page.locator('button[data-testid="findTicketsBtn"]').first
            last_pos = human_click(page, btn, last_pos)
        except Exception as e: log("entradas_err", err=str(e)[:60])
        # Esperar a que aparezca el checkbox Acepto
        try:
            page.wait_for_selector('label:has-text("Acepto")', timeout=8000)
        except: pass
        hp(0.8, 1.6)

        # 6) Acepto checkbox
        log("accept_terms")
        try:
            lbl = page.get_by_text(re.compile(r"acepto que he le[ií]do", re.I), exact=False).first
            if lbl.is_visible(timeout=3000):
                last_pos = human_click(page, lbl, last_pos)
                log("terms_clicked")
        except: log("terms_err")
        hp(0.6, 1.2)

        # 7) Continuar comprando + esperar redirect OAuth
        log("continue_buying")
        prev_url = page.url
        try:
            btn = page.get_by_role("button", name=re.compile(r"^continuar comprando$", re.I)).first
            last_pos = human_click(page, btn, last_pos)
            log("continuar_clicked")
        except Exception as e: log("continuar_err", err=str(e)[:60])
        # Esperar URL_CHANGED a auth/identity (en lugar de pausa fija larga)
        try:
            page.wait_for_url(re.compile(r'auth\.ticketmaster|identity\.ticketmaster', re.I), timeout=12000)
            log("oauth_redirect_detected")
        except:
            hp(2, 4)

        # 7b) Detectar si NO estamos en auth (= ya logueado por cookies)
        # Parsear URL real (no buscar substring en query) para evitar falsos positivos como ?headerTitle=Checkout
        from urllib.parse import urlparse
        cur_parsed = urlparse(page.url)
        cur_host = (cur_parsed.netloc or "").lower()
        cur_path = (cur_parsed.path or "").lower()
        in_auth = ('auth.ticketmaster' in cur_host or 'identity.ticketmaster' in cur_host)
        if not in_auth:
            log("login_skipped_via_cookies", url=page.url[:80])
            # Saltar directo a watch_checkout
            log("watch_checkout_via_session")
            prev = page.url; deadline = time.time()+600
            while time.time() < deadline:
                time.sleep(2)
                try:
                    cur = page.url
                    if cur != prev:
                        log("URL_CHANGED", to=cur[:120]); prev = cur
                        page.wait_for_timeout(3000)
                        cp = urlparse(cur); host = (cp.netloc or "").lower(); path = (cp.path or "").lower()
                        if any(k in host for k in ['my.ticketmaster','checkout.ticketmaster']) or any(k in path for k in ['/checkout','/payment','/cart','/pago']):
                            log("checkout_detected"); fill_card(page, card, account)
                            log("READY_FOR_USER_CONFIRMATION", url=page.url)
                            print("\n" + "="*70)
                            print("✅ TARJETA AUTORELLENADA. Confirma manual con Pagar.")
                            print(f"URL: {page.url}")
                            print("Para cerrar: crea archivo _compra_camoufox.STOP")
                            print("="*70)
                            flag = os.path.join(os.path.dirname(__file__), "_compra_camoufox.STOP")
                            while not os.path.exists(flag): time.sleep(5)
                            os.remove(flag); log("user_stop"); return
                except Exception as e: log("watch_err", err=str(e)[:60])
            log("DONE_TIMEOUT_VIA_SESSION"); return

        # 8) OAuth login (cookies no válidas o no había)
        log("oauth_login_start")
        # Si NO había sesión guardada, hacer LOGIN MANUAL del usuario:
        if not already_logged_in:
            log("manual_login_required")
            print("\n" + "="*70)
            print("👤 LOGIN MANUAL REQUERIDO en el browser Firefox abierto.")
            print(f"   Usa email + pwd o passkey de: {account['tm_email']}")
            print("   Cuando termines y veas pantalla checkout o cuenta TM, el bot capturará")
            print("   las cookies automáticamente para futuras compras.")
            print("="*70)
            # Esperar URL_CHANGED a checkout/cart/cuenta TM (máximo 5min)
            login_deadline = time.time() + 300
            captured = False
            while time.time() < login_deadline:
                time.sleep(2)
                try: cp = urlparse(page.url); host = (cp.netloc or "").lower(); path = (cp.path or "").lower()
                except: continue
                # Detectar checkout REAL (host www.ticketmaster.* + path /checkout)
                # o página cuenta TM (my.ticketmaster.*)
                in_checkout = ('www.ticketmaster' in host or 'checkout.ticketmaster' in host) and \
                              any(k in path for k in ['/checkout','/payment','/cart','/pago'])
                in_account = 'my.ticketmaster' in host
                # Logueado significa: NO estamos ya en auth.ticketmaster
                logged_out_of_auth = ('auth.ticketmaster' not in host and 'identity.ticketmaster' not in host)
                if (in_checkout or in_account) and logged_out_of_auth:
                    log("manual_login_completed", url=page.url[:80], host=host, path=path[:30])
                    h = harvest_cookies(ctx, cookie_label, page=page)
                    log("cookies_harvested", **h)
                    captured = True
                    if in_checkout:
                        fill_card(page, card, account)
                        log("READY_FOR_USER_CONFIRMATION", url=page.url)
                        print("\n" + "="*70)
                        print("✅ TARJETA AUTORELLENADA. Confirma con Pagar.")
                        print("="*70)
                        flag = os.path.join(os.path.dirname(__file__), "_compra_camoufox.STOP")
                        while not os.path.exists(flag): time.sleep(5)
                        os.remove(flag); return
                    break
            if not captured:
                log("manual_login_timeout"); return
        # Pueden abrirse pestañas nuevas
        all_pages = browser.contexts[0].pages
        auth = [p for p in all_pages if 'auth.ticketmaster' in (p.url or '')]
        if auth:
            page = auth[-1]
            page.bring_to_front()
            log("switched_auth", url=page.url[:80])
        accept_cookies(page, last_pos)
        try:
            page.wait_for_selector('#email-input, input[type="email"]', timeout=20000)
        except: log("no_email"); shoot(page, "no_email"); return
        em_sel = '#email-input' if page.locator('#email-input').count() else 'input[type="email"]'
        human_type(page, em_sel, account['tm_email'])
        log("email_typed_human")
        hp(1.5,3)
        try:
            btn = page.get_by_role("button", name=re.compile(r"^continuar$", re.I)).first
            last_pos = human_click(page, btn, last_pos)
        except: pass
        # Esperar pantalla password (no pausa fija)
        try:
            page.wait_for_selector('#password-input, input[type="password"]', timeout=15000)
        except:
            log("no_pwd"); shoot(page, "no_pwd")
            # Posible reset password redirect
            log("possible_reset_redirect", title=page.title())
            return
        pw_sel = '#password-input' if page.locator('#password-input').count() else 'input[type="password"]'
        human_type(page, pw_sel, account['tm_password'])
        log("pwd_typed_human")
        hp(0.8, 1.5)
        # 2Captcha Enterprise v3 (timeout 90s en lugar de 120)
        try:
            from tm_recaptcha_solver import solve_and_inject_recaptcha_v3
            log("recaptcha_solving")
            r = solve_and_inject_recaptcha_v3(page, action="login", min_score=0.9, timeout_s=90)
            log("recaptcha_result", **{k:v for k,v in r.items() if k != "token"})
        except Exception as e: log("recaptcha_err", err=str(e)[:60])
        hp(0.5, 1)
        for txt in ["Iniciar sesión","Sign In","Login"]:
            try:
                btn = page.get_by_role("button", name=re.compile(txt, re.I)).first
                if btn.is_visible(timeout=1000):
                    last_pos = human_click(page, btn, last_pos)
                    log("login_submitted"); break
            except: continue
        # Esperar pantalla siguiente (OTP o checkout) sin pausa fija larga
        time.sleep(3); shoot(page, "post_login")

        # 9) OTP — esperar hasta 15s a que aparezca el campo OTP, polling IMAP largo
        log("otp_check_start")
        from datetime import datetime
        otp_appeared = False
        otp_locator = None
        for attempt in range(5):  # 5 retries x 3s = 15s
            try:
                otp_locator = page.locator('input[name*="code" i], input[autocomplete*="one-time" i], input[name="otp"], input[id*="otp" i], input[id*="code" i], input[type="tel"][maxlength]').first
                if otp_locator.is_visible(timeout=3000):
                    otp_appeared = True
                    log("otp_required", attempt=attempt+1); shoot(page, "otp_screen")
                    break
            except: pass
            time.sleep(2)

        if otp_appeared and otp_locator:
            since = datetime.now()
            log("imap_polling_otp", since=since.isoformat(), max_wait_s=600)
            code = imap_otp(account['contact_email'], account['contact_password'], since, 600)
            if code:
                log("otp_typing", code=code)
                try:
                    otp_locator.fill(code)
                except:
                    page.keyboard.type(code)
                time.sleep(1)
                for txt in ["Verificar","Confirmar","Continuar","Submit"]:
                    try:
                        btn = page.get_by_role("button", name=re.compile(txt, re.I)).first
                        if btn.is_visible(timeout=1500):
                            last_pos = human_click(page, btn, last_pos)
                            log("otp_submitted", btn=txt); break
                    except: continue
                time.sleep(8)
            else:
                log("otp_imap_timeout_600s")
        else:
            log("no_otp_field_visible")

        # 10) Watch checkout
        log("watch_checkout")
        prev = page.url; deadline = time.time()+600
        while time.time() < deadline:
            time.sleep(2)
            try:
                cur = page.url
                if cur != prev:
                    log("URL_CHANGED", to=cur[:120]); prev = cur
                    page.wait_for_timeout(3000)
                    u = cur.lower()
                    if any(k in u for k in ['checkout','payment','cart','pago']):
                        log("checkout_detected")
                        fill_card(page, card, account)
                        log("READY_FOR_USER_CONFIRMATION", url=page.url)
                        # Vigilante 3DS en background — autofill OTP cuando aparezca challenge
                        try:
                            import threading
                            threading.Thread(
                                target=watch_for_3ds_and_autofill,
                                args=(page, 180),
                                daemon=True,
                            ).start()
                            log("3ds_watcher_armed", timeout_s=180)
                        except Exception as e:
                            log("3ds_watcher_err", err=str(e)[:80])
                        print("\n" + "="*70)
                        print("✅ TARJETA AUTORELLENADA. Confirma manual con Pagar.")
                        print("🔐 3DS auto-trigger ARMADO (180s) — OTP se inyectará solo")
                        print(f"URL: {page.url}")
                        print("Para cerrar: crea archivo _compra_camoufox.STOP")
                        print("="*70)
                        flag = os.path.join(os.path.dirname(__file__), "_compra_camoufox.STOP")
                        while not os.path.exists(flag): time.sleep(5)
                        os.remove(flag); log("user_stop"); return
            except Exception as e: log("watch_err", err=str(e)[:60])

        log("DONE_TIMEOUT")


if __name__ == '__main__':
    main()
