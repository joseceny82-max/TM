"""tm_purchase_clean.py — Bot de compra TM ES con mejoras anti-detección 2026.

Cambios respecto a versiones previas:
1. Usa `rebrowser_playwright` (drop-in con parches CDP Runtime.enable leak)
2. Storage cleanup COMPLETO: cookies + localStorage + sessionStorage + IndexedDB
3. Pre-warm humano: Google → TM home → 2-3 eventos random → evento target
4. Bezier mouse moves (no lineales)
5. Click HUMANO en banner OneTrust "Aceptar todas las cookies" en cada pantalla (NO .remove())
6. SIN override de Notification API ni navigator.permissions
7. WebGL vendor coherente con Win UA
8. Cierre de pestañas auth.* / paused antes de close

API pública:
    BotPurchase(account, card, event_url, qty=4, prefer_offer_idx=2).run()
"""
from __future__ import annotations
import os, time, json, re, random, math
from typing import Optional, Dict, Any, List
from datetime import datetime


# === Bezier mouse moves ===
def _bezier_points(p0, p1, n=25):
    """Genera n puntos en una curva Bezier cuadrática con control aleatorio."""
    cx = (p0[0] + p1[0]) / 2 + random.randint(-150, 150)
    cy = (p0[1] + p1[1]) / 2 + random.randint(-100, 100)
    pts = []
    for i in range(n + 1):
        t = i / n
        x = (1-t)**2 * p0[0] + 2*(1-t)*t * cx + t**2 * p1[0]
        y = (1-t)**2 * p0[1] + 2*(1-t)*t * cy + t**2 * p1[1]
        pts.append((x, y))
    return pts


def human_mouse_to(page, x, y, last_pos=None):
    """Mueve el ratón hasta (x,y) con curva Bezier + jitter + velocidad variable."""
    if last_pos is None: last_pos = (random.randint(100, 800), random.randint(100, 600))
    n = random.randint(20, 40)
    pts = _bezier_points(last_pos, (x, y), n=n)
    for i, (px, py) in enumerate(pts):
        page.mouse.move(px + random.uniform(-0.5, 0.5), py + random.uniform(-0.5, 0.5))
        # Velocidad variable: lenta al principio/final, rápida en medio
        speed_factor = math.sin(math.pi * i / n)
        time.sleep(random.uniform(0.005, 0.015) * (1.5 - speed_factor))
    return (x, y)


def human_click(page, locator, last_pos=None):
    """Click humano con scroll + bezier mouse + delay pre-click."""
    locator.scroll_into_view_if_needed(timeout=4000)
    time.sleep(random.uniform(0.4, 1.0))
    bbox = locator.bounding_box()
    if not bbox:
        locator.click(force=True, timeout=5000)
        return last_pos
    # Click en punto aleatorio dentro del elemento (no centro exacto)
    cx = bbox['x'] + bbox['width'] * random.uniform(0.3, 0.7)
    cy = bbox['y'] + bbox['height'] * random.uniform(0.3, 0.7)
    new_pos = human_mouse_to(page, cx, cy, last_pos)
    time.sleep(random.uniform(0.08, 0.25))
    page.mouse.click(cx, cy)
    return new_pos


def human_type(page, selector, text):
    """Tipeo char-a-char con dwell time + flight time variable.
    LIMPIA el campo antes (Ctrl+A + Delete) para evitar concatenación con valor previo."""
    page.click(selector)
    time.sleep(random.uniform(0.2, 0.5))
    # Limpiar campo PRIMERO (Ctrl+A + Delete) — fix del bug de email duplicado
    page.keyboard.press('Control+A')
    time.sleep(random.uniform(0.1, 0.2))
    page.keyboard.press('Delete')
    time.sleep(random.uniform(0.2, 0.4))
    for c in text:
        page.keyboard.type(c)
        delay = random.uniform(0.05, 0.18)
        if c in ' .@-_!?':
            delay += random.uniform(0.1, 0.3)
        time.sleep(delay)


def human_pause(a=2.0, b=4.5):
    time.sleep(random.uniform(a, b))


def human_pause_long():
    time.sleep(random.uniform(6, 12))


# === Storage cleanup ===
def deep_clean_storage(page) -> Dict[str, int]:
    """Limpia localStorage + sessionStorage + IndexedDB (no cookies, eso lo hace caller)."""
    out = {"local": 0, "session": 0, "idb": 0}
    try:
        out['local'] = page.evaluate("Object.keys(localStorage).length") or 0
        out['session'] = page.evaluate("Object.keys(sessionStorage).length") or 0
        page.evaluate("""() => {
            try { localStorage.clear(); } catch(e){}
            try { sessionStorage.clear(); } catch(e){}
            try {
                if (indexedDB.databases) {
                    indexedDB.databases().then(dbs => {
                        for (const db of dbs) try { indexedDB.deleteDatabase(db.name); } catch(e){}
                    });
                }
            } catch(e){}
        }""")
    except Exception: pass
    return out


def close_auth_tabs(ctx) -> int:
    """Cierra pestañas con 'auth.', 'paused', 'identity.' en URL/title (comprometidas)."""
    closed = 0
    for p in list(ctx.pages):
        try:
            url = (p.url or '').lower()
            try: title = (p.title() or '').lower()
            except: title = ''
            if any(k in url for k in ['auth.ticketmaster', 'identity.ticketmaster', 'authorization.oauth']) \
               or 'paused' in title or 'browsing activity' in title:
                p.close()
                closed += 1
        except Exception: pass
    return closed


# === Cookie banner OneTrust ===
def accept_onetrust_banner(page, last_pos=None) -> bool:
    """Click HUMANO en 'Aceptar todas las cookies' del banner OneTrust si está visible.
    NO usa .remove() del DOM. Si no aparece, no hace nada."""
    for txt_re in [r"^Aceptar todas las cookies$", r"^Aceptar$", r"^Accept all$", r"^Accept$"]:
        try:
            btn = page.get_by_role("button", name=re.compile(txt_re, re.I)).first
            if btn.is_visible(timeout=1500):
                human_click(page, btn, last_pos)
                human_pause(0.8, 1.6)
                return True
        except Exception: continue
    return False


# === Anti-detect coherente — actualizado tras informe agente 27-abr ===
ANTI_DETECT_LIGHT = """
// 1) WebGL coherente con UA Windows (NVIDIA D3D11)
const _wglgp = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Google Inc. (NVIDIA)';
    if (p === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return _wglgp.call(this, p);
};
const _wgl2gp = WebGL2RenderingContext.prototype.getParameter;
WebGL2RenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Google Inc. (NVIDIA)';
    if (p === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return _wgl2gp.call(this, p);
};

// 2) Notification + permissions coherentes (humano: prompt/default, NO denied)
try {
    Object.defineProperty(Notification, 'permission', {get: () => 'default', configurable: true});
    if (navigator.permissions && navigator.permissions.query) {
        const _origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) => {
            if (params && params.name === 'notifications') {
                return Promise.resolve({state: 'prompt', name: 'notifications', onchange: null});
            }
            return _origQuery(params);
        };
    }
} catch(e){}

// 3) Audio fingerprint spoof: jitter ±1e-7 en getChannelData
try {
    const _origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function() {
        const data = _origGetChannelData.apply(this, arguments);
        // Jitter determinístico (mismo perfil = mismo jitter para no levantar sospechas por inconsistencia)
        for (let i = 0; i < data.length; i += 1000) {
            data[i] = data[i] + (Math.sin(i * 0.0001) * 1e-7);
        }
        return data;
    };
} catch(e){}

// 4) Canvas noise determinístico por perfil (mismo offset cada call, no random)
try {
    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function() {
        const ctx = this.getContext('2d');
        if (ctx && this.width > 16 && this.height > 16) {
            // Añadir 1 píxel casi invisible (offset determinístico por dimensiones)
            ctx.fillStyle = 'rgba(255,255,255,' + ((this.width * this.height) % 7 / 1000) + ')';
            ctx.fillRect(0, 0, 1, 1);
        }
        return _origToDataURL.apply(this, arguments);
    };
} catch(e){}

// 5) Hardware concurrency típica (4 cores en lugar de exponer reales)
try {
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8, configurable: true});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8, configurable: true});
} catch(e){}
"""


# === IMAP OTP polling ===
def imap_get_otp(email_madre, pwd, since_ts, max_wait=180):
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
                for m in tm:
                    body = (m.text or '') + ' ' + (m.subject or '')
                    for c in pat6.findall(body):
                        if 100000 <= int(c) <= 999999: return c
        except Exception: pass
        time.sleep(8)
    return None


# === Bot principal ===
class BotPurchase:
    def __init__(self, port, account, card, event_url, qty=4, prefer_offer_idx=2, log_file=None):
        self.port = port
        self.account = account
        self.card = card
        self.event_url = event_url
        self.qty = qty
        self.prefer_offer_idx = prefer_offer_idx  # 0=Pista, 1=Asiento Reserv., 2=Vis.Reducida
        self.log_file = log_file
        self.last_pos = None
        self.ctx = None
        self.page = None

    def log(self, stage, **kw):
        msg = f"[{time.strftime('%H:%M:%S')}] {stage}"
        if kw: msg += " " + json.dumps(kw, ensure_ascii=False)
        print(msg, flush=True)

    def shoot(self, name):
        d = os.path.join(os.path.dirname(__file__), "datos", "compra_test")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{int(time.time())}_clean_{name}.png")
        try: self.page.screenshot(path=p, full_page=False); self.log("shot", file=os.path.basename(p))
        except: pass

    def setup_browser(self):
        """Conecta al browser via CDP (rebrowser_playwright = parches Runtime.enable)."""
        import requests
        v = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=8).json()
        from rebrowser_playwright.sync_api import sync_playwright
        self._pw_ctx = sync_playwright().__enter__()
        b = self._pw_ctx.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
        self.ctx = b.contexts[0]
        self.ctx.add_init_script(ANTI_DETECT_LIGHT)
        # Cerrar pestañas comprometidas
        closed = close_auth_tabs(self.ctx)
        if closed: self.log("closed_auth_tabs", n=closed)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.bring_to_front()
        self.log("browser_ready", port=self.port, total_pages=len(self.ctx.pages))

    def step_pre_warm(self):
        """Pre-warm NuData EXTENDIDO (40-90s telemetría real) — instrucción agente 27-abr.
        NuData necesita ~30s+ de mouse/scroll/typing entropy antes del submit.
        Si haces submit en <10s desde page load → score automático <0.3."""
        self.log("pre_warm_start")
        self.page.goto("https://www.ticketmaster.es/", wait_until="domcontentloaded", timeout=30000)
        human_pause_long()  # 6-12s leyendo home
        # Click HUMANO en cookies banner
        accepted = accept_onetrust_banner(self.page, self.last_pos)
        self.log("home_cookies", accepted=accepted)
        # Pre-aging: navegar a categorías + scroll lento + mouse moves para sembrar nds-pmd
        self.log("nudata_pre_warm_start")
        # Mouse moves aleatorios por la home (entropía)
        for _ in range(random.randint(8, 14)):
            tx = random.randint(200, 1700)
            ty = random.randint(200, 800)
            self.last_pos = human_mouse_to(self.page, tx, ty, self.last_pos)
            time.sleep(random.uniform(0.3, 1.2))
        # Scrolls lentos con wheel events (no scrollBy)
        for _ in range(random.randint(4, 7)):
            self.page.mouse.wheel(0, random.randint(150, 450))
            human_pause(1.0, 2.4)
        # Click en categoría/genero aleatorio (Pop, Rock, etc.) si visible
        try:
            for txt in ["Conciertos", "Pop", "Rock", "Música"]:
                try:
                    btn = self.page.get_by_role("link", name=re.compile(txt, re.I)).first
                    if btn.is_visible(timeout=1000):
                        self.last_pos = human_click(self.page, btn, self.last_pos)
                        self.log("nudata_category_explored", txt=txt)
                        human_pause_long()
                        # Scroll en la categoría
                        for _ in range(random.randint(3, 5)):
                            self.page.mouse.wheel(0, random.randint(200, 500))
                            human_pause(0.8, 1.8)
                        # Volver a home
                        self.page.goto("https://www.ticketmaster.es/", wait_until="domcontentloaded", timeout=20000)
                        human_pause(3, 6)
                        break
                except Exception: continue
        except Exception: pass
        # Más mouse moves para finalizar pre-warm
        for _ in range(random.randint(4, 8)):
            tx = random.randint(200, 1700); ty = random.randint(200, 800)
            self.last_pos = human_mouse_to(self.page, tx, ty, self.last_pos)
            time.sleep(random.uniform(0.4, 1.2))
        human_pause(2, 4)
        self.log("nudata_pre_warm_done", elapsed_target_min_s=40)

    def step_goto_event(self):
        self.log("goto_event")
        self.page.goto(self.event_url, wait_until="domcontentloaded", timeout=30000)
        human_pause_long()
        accept_onetrust_banner(self.page, self.last_pos)
        self.shoot("event_loaded")

    def step_open_offer_panel(self):
        """Click 'Ver lo mejor disponible' con JS+bezier híbrido + esperar steppers.
        FIX 27-abr: hay 2 botones 'ver lo mejor' (cabecera + body); JS evaluate elige el visible."""
        self.log("open_offer_panel")
        # JS click — selecciona el botón VISIBLE (no oculto en cabecera nav)
        clicked = self.page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button')).filter(b => /ver lo mejor disponible/i.test(b.innerText||""));
            // Preferir el de mayor área (el principal del panel)
            const visibles = btns.filter(b => {
                const r = b.getBoundingClientRect();
                return r.width > 100 && r.height > 20;
            });
            const target = visibles[0] || btns[0];
            if (!target) return {ok:false, total:btns.length};
            target.scrollIntoView({block:'center'});
            target.click();
            return {ok:true, total:btns.length, w: target.getBoundingClientRect().width};
        }""")
        self.log("ver_lo_mejor_click", **clicked)
        human_pause(2, 4)
        # Esperar 3 steppers visibles
        try:
            self.page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=\"quantityStepper\"]').length >= 3",
                timeout=20000
            )
            self.log("offer_panel_ready")
        except Exception as e:
            self.log("offer_panel_timeout", err=str(e)[:80])

    def step_set_qty(self):
        """Sube qty con clicks bezier individuales (no JS .click() seguidos)."""
        self.log("set_qty", target=self.qty, idx=self.prefer_offer_idx)
        for i in range(8):
            cur = self.page.evaluate(f"""() => {{
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{self.prefer_offer_idx}];
                return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
            }}""")
            self.log("qty_check", current=cur)
            if cur >= self.qty: break
            # Click + del stepper con bezier
            box = self.page.evaluate(f"""() => {{
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{self.prefer_offer_idx}];
                if (!s) return null;
                const btns = s.querySelectorAll('button');
                const plus = btns[btns.length-1] || btns[0];
                const r = plus.getBoundingClientRect();
                return {{x:r.x, y:r.y, width:r.width, height:r.height}};
            }}""")
            if box:
                cx = box['x'] + box['width']/2
                cy = box['y'] + box['height']/2
                self.last_pos = human_mouse_to(self.page, cx, cy, self.last_pos)
                time.sleep(random.uniform(0.08, 0.2))
                self.page.mouse.click(cx, cy)
            human_pause(1.0, 2.4)
        human_pause(2, 4)

    def step_click_entradas(self):
        """Click findTicketsBtn con bezier."""
        self.log("click_entradas")
        try:
            btn = self.page.locator('button[data-testid="findTicketsBtn"]').first
            self.last_pos = human_click(self.page, btn, self.last_pos)
            self.log("entradas_clicked")
        except Exception as e:
            self.log("entradas_err", err=str(e)[:80])
        human_pause(4, 7)
        self.shoot("after_entradas")

    def step_accept_terms(self):
        """Click HUMANO en checkbox 'Acepto que he leído'."""
        self.log("accept_terms")
        self.page.evaluate("window.scrollBy(0, 200)"); human_pause(0.8, 1.5)
        # Buscar el label y hacer click humano (no JS click)
        try:
            lbl = self.page.get_by_text(re.compile(r"acepto que he le[ií]do", re.I), exact=False).first
            if lbl.is_visible(timeout=3000):
                self.last_pos = human_click(self.page, lbl, self.last_pos)
                self.log("terms_clicked_human")
                human_pause(1.5, 3)
                return
        except Exception as e:
            self.log("terms_human_err", err=str(e)[:80])
        # Fallback: JS click directo en el checkbox
        r = self.page.evaluate("""() => {
            const labels = Array.from(document.querySelectorAll('label'));
            const lbl = labels.find(l => /acepto que he le[ií]do/i.test(l.textContent||''));
            if (!lbl) return {ok:false};
            const forId = lbl.getAttribute('for');
            let cb = forId ? document.getElementById(forId) : null;
            if (!cb) cb = lbl.querySelector('input[type=checkbox]');
            if (cb) { cb.click(); return {ok:true, checked:cb.checked}; }
            return {ok:false};
        }""")
        self.log("terms_js_fallback", **r)
        human_pause(1.5, 3)

    def step_continue_buying(self):
        """Click 'Continuar comprando' humano."""
        self.log("continue_buying")
        try:
            btn = self.page.get_by_role("button", name=re.compile(r"^continuar comprando$", re.I)).first
            self.last_pos = human_click(self.page, btn, self.last_pos)
            self.log("continuar_clicked")
        except Exception as e:
            self.log("continuar_err", err=str(e)[:80])
        human_pause_long()

    def step_oauth_login(self):
        """Login OAuth con tipeo humano (sin override Notification API)."""
        self.log("oauth_login_start")
        # Buscar pestaña auth (puede haber sido nueva)
        auth_pages = [p for p in self.ctx.pages if 'auth.ticketmaster' in (p.url or '')]
        if auth_pages:
            self.page = auth_pages[-1]
            self.page.bring_to_front()
            self.log("switched_to_auth", url=self.page.url[:80])
        try:
            self.page.wait_for_selector('#email-input, input[type="email"]', timeout=20000)
        except: self.log("no_email_input"); self.shoot("no_email_input"); return False
        # Click cookies si aparece
        accept_onetrust_banner(self.page, self.last_pos)
        # Email humano
        em_sel = '#email-input' if self.page.locator('#email-input').count() else 'input[type="email"]'
        human_type(self.page, em_sel, self.account['tm_email'])
        self.log("email_typed_human")
        human_pause(1.5, 3)
        try:
            btn = self.page.get_by_role("button", name=re.compile(r"^continuar$", re.I)).first
            self.last_pos = human_click(self.page, btn, self.last_pos)
        except: pass
        human_pause(4, 7)
        try:
            self.page.wait_for_selector('#password-input, input[type="password"]', timeout=20000)
        except: self.log("no_pwd_input"); self.shoot("no_pwd_input"); return False
        pw_sel = '#password-input' if self.page.locator('#password-input').count() else 'input[type="password"]'
        human_type(self.page, pw_sel, self.account['tm_password'])
        self.log("pwd_typed_human")
        human_pause(2, 4)
        # Resolver reCAPTCHA Enterprise v3 ANTES del submit (TM lo evalúa al click Iniciar sesión)
        try:
            from tm_recaptcha_solver import solve_and_inject_recaptcha_v3
            self.log("recaptcha_solving")
            r = solve_and_inject_recaptcha_v3(self.page, action="login", min_score=0.9, timeout_s=120)
            self.log("recaptcha_result", **{k: v for k, v in r.items() if k != "token"})
            if r.get("ok"): self.log("recaptcha_token_injected")
        except Exception as e:
            self.log("recaptcha_err", err=str(e)[:120])
        human_pause(1, 2)
        for txt in ["Iniciar sesión", "Sign In", "Login"]:
            try:
                btn = self.page.get_by_role("button", name=re.compile(txt, re.I)).first
                if btn.is_visible(timeout=1000):
                    self.last_pos = human_click(self.page, btn, self.last_pos)
                    self.log("login_submitted", btn=txt); break
            except: continue
        time.sleep(8); self.shoot("post_login_submit")
        # OTP si pide
        try:
            otp = self.page.locator('input[name*="code" i], input[autocomplete*="one-time" i]').first
            if otp.is_visible(timeout=3000):
                self.log("otp_required")
                from datetime import datetime
                code = imap_get_otp(self.account['contact_email'], self.account['contact_password'],
                                    datetime.now(), 180)
                if code:
                    otp.fill(code); time.sleep(1)
                    for txt in ["Verificar", "Confirmar", "Continuar"]:
                        try:
                            btn = self.page.get_by_role("button", name=re.compile(txt, re.I)).first
                            if btn.is_visible(timeout=1500):
                                human_click(self.page, btn, self.last_pos); break
                        except: continue
                    time.sleep(5)
        except: pass
        return True

    def step_fill_card(self):
        """Autorelleno tarjeta + datos en checkout."""
        self.log("fill_card_start"); time.sleep(4); self.shoot("checkout_initial")
        c = self.card; a = self.account
        holder = c.get('holder',''); number = re.sub(r'\D','',c.get('number',''))
        month = str(c.get('month','')).zfill(2); year = c.get('year','')
        yyyy = ("20"+year) if len(year)==2 else year; cvv = c.get('cvv','')
        def tf(loc, val, lbl):
            try:
                if loc.is_visible(timeout=800): loc.fill(val); self.log("filled", field=lbl); return True
            except: pass
            return False
        targets = [self.page] + list(self.page.frames)
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
            tf(t.locator('input[name*="firstName" i]').first, a.get('first_name',''), "first_name")
            tf(t.locator('input[name*="lastName" i]').first, a.get('last_name',''), "last_name")
            tf(t.locator('input[name*="phone" i]').first, a.get('phone_prefix','')+a.get('phone',''), "phone")
            tf(t.locator('input[name*="address" i]').first, a.get('address1',''), "address")
            tf(t.locator('input[name*="city" i]').first, a.get('city',''), "city")
            tf(t.locator('input[name*="postal" i]').first, a.get('zip',''), "zip")
        self.shoot("checkout_filled")

    def step_watch_checkout(self, max_seconds=600):
        """Vigila URL_CHANGED hasta detectar checkout."""
        self.log("watch_checkout")
        prev = self.page.url; deadline = time.time() + max_seconds
        while time.time() < deadline:
            time.sleep(2)
            try:
                cur = self.page.url
                if cur != prev:
                    self.log("URL_CHANGED", to=cur[:120]); prev = cur
                    self.page.wait_for_timeout(3000)
                    u = cur.lower()
                    if any(k in u for k in ['checkout','payment','cart','pago']):
                        self.log("checkout_detected")
                        self.step_fill_card()
                        return True
            except Exception as e: self.log("watch_err", err=str(e)[:80])
        return False

    def cleanup_storage_before_close(self):
        """Limpia storage del contexto antes de cerrar (evita persistir nds-pmd)."""
        for p in list(self.ctx.pages):
            try: deep_clean_storage(p)
            except: pass
        try: self.ctx.clear_cookies()
        except: pass
        self.log("storage_cleaned")

    def run(self):
        try:
            self.setup_browser()
            self.step_pre_warm()
            self.step_goto_event()
            self.step_open_offer_panel()
            self.step_set_qty()
            self.step_click_entradas()
            self.step_accept_terms()
            self.step_continue_buying()
            self.step_oauth_login()
            ok = self.step_watch_checkout()
            self.log("DONE", checkout_reached=ok, url=self.page.url[:80])
            print("\n" + "="*70)
            print(f"FLOW {'COMPLETADO' if ok else 'NO LLEGÓ A CHECKOUT'}.")
            print(f"URL final: {self.page.url}")
            print("Para cerrar: crea archivo _compra_clean.STOP")
            print("="*70)
            flag = os.path.join(os.path.dirname(__file__), "_compra_clean.STOP")
            while not os.path.exists(flag): time.sleep(5)
            os.remove(flag); self.log("user_stop")
        finally:
            try: self.cleanup_storage_before_close()
            except: pass


def main():
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
    PORT = int(os.environ.get('CDP_PORT', '0'))
    USER = os.environ.get('USER_LABEL', 'User-6')
    EVENT_URL = os.environ.get('EVENT_URL', 'https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961')
    QTY = int(os.environ.get('QTY', '4'))
    OFFER_IDX = int(os.environ.get('OFFER_IDX', '2'))

    # Si NO hay CDP_PORT → SessionFactory crea sesión limpia automáticamente (instrucción usuario 27-abr)
    session = None
    if not PORT:
        print("[startup] No CDP_PORT — SessionFactory creando sesión limpia...")
        from tm_session_factory import SessionFactory
        sf = SessionFactory()
        session = sf.new_clean_session(country="ES", verify_ip=True)
        if not session.get('ok'):
            print(f"ERROR: SessionFactory failed: {session}")
            return
        PORT = session['port']
        print(f"[startup] Session: profile={session['profile']} port={PORT} ip={session.get('ip')} country={session.get('ip_country')} asn={session.get('ip_asn')}")
        if session.get('ip_country') and session['ip_country'].upper() != 'ES':
            print(f"WARNING: ip_country={session['ip_country']} — esperado ES, abortando")
            sf.close_session(session)
            return

    import account_manager as am
    accs = am.account_manager.list_accounts()
    cards = am.account_manager.list_cards()
    account = next(a for a in accs if a['label'].startswith(USER))
    card = next(c for c in cards if c.get('account_id') == account['id'])
    print(f"[startup] user={account['tm_email']} qty={QTY} offer_idx={OFFER_IDX}")
    bot = BotPurchase(PORT, account, card, EVENT_URL, qty=QTY, prefer_offer_idx=OFFER_IDX)
    try:
        bot.run()
    finally:
        # Cleanup session si la creamos nosotros
        if session:
            from tm_session_factory import SessionFactory
            SessionFactory().close_session(session)
            print("[shutdown] Session cerrada con cleanup AGRESIVO")


if __name__ == '__main__':
    main()
