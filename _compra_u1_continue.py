"""Continuación v4: click 'Ver lo mejor disponible' (modo quick) → seleccionar 4
en stepper Pista → Comprar → login OTP → autorrelleno tarjeta."""
from __future__ import annotations
import sys, io, time, os, json, re, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

PORT = int(os.environ.get('CDP_PORT', '60937'))
SHOTDIR = os.path.join(os.path.dirname(__file__), "datos", "compra_test")
os.makedirs(SHOTDIR, exist_ok=True)

def log(stage, **kw):
    print(f"[{time.strftime('%H:%M:%S')}] {stage}", json.dumps(kw, ensure_ascii=False) if kw else "", flush=True)

def shoot(page, name):
    p = os.path.join(SHOTDIR, f"{int(time.time())}_{name}.png")
    try: page.screenshot(path=p, full_page=False); log("shot", file=os.path.basename(p))
    except Exception as e: log("shot_err", err=str(e)[:80])

import account_manager as am
account = next(a for a in am.account_manager.list_accounts() if a['label'].startswith('User-1'))
card = next(c for c in am.account_manager.list_cards() if c.get('account_id') == account['id'])
log("user", email=account['tm_email'])

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

def js_click_by_text(page, txt_re):
    """Click via JS el primer botón cuyo texto matchea regex (case-insensitive)."""
    rx = re.compile(txt_re, re.I)
    res = page.evaluate(f"""
      () => {{
        const rx = new RegExp({json.dumps(txt_re)}, 'i');
        const els = Array.from(document.querySelectorAll('button, a[role="button"], [role="button"]'));
        for (const el of els) {{
            const t = (el.innerText || el.textContent || '').trim();
            if (rx.test(t) && !el.disabled) {{
                el.scrollIntoView({{block:'center'}});
                el.click();
                return {{clicked: true, text: t.slice(0,80), tag: el.tagName}};
            }}
        }}
        return {{clicked: false}};
      }}
    """)
    return res

def login_full(page, account):
    log("login_start", email=account['tm_email'])
    try: page.wait_for_selector('input[type="email"]', timeout=15000)
    except: log("no_email_input"); shoot(page, "no_email_input"); return False
    page.fill('input[type="email"]', account['tm_email']); log("email_filled")
    page.wait_for_timeout(800)
    js_click_by_text(page, r"continuar|continue|siguiente|next")
    page.wait_for_timeout(3500)
    try: page.wait_for_selector('input[type="password"]', timeout=15000)
    except: log("no_pwd_input"); return False
    page.fill('input[type="password"]', account['tm_password']); log("pwd_filled")
    page.wait_for_timeout(800)
    js_click_by_text(page, r"iniciar sesi|sign in|login|continuar")
    page.wait_for_timeout(7000); shoot(page, "post_login_full")
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
                js_click_by_text(page, r"verificar|confirmar|continuar|verify")
                page.wait_for_timeout(5000); return True
            return False
    except: pass
    return True

def fill_card(page, card, account):
    log("fill_card_start"); page.wait_for_timeout(3500); shoot(page, "checkout_initial")
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
        page = next((p for p in ctx.pages if 'ticketmaster' in (p.url or '').lower()), ctx.pages[0])
        page.bring_to_front()
        log("page_initial", url=page.url)

        # 0) SIEMPRE goto fresh para resetear stepper qty=0
        EVENT_URL = "https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961"
        log("goto_fresh")
        page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(7000)
        # Cerrar cookies si aparecieron
        for txt in ["Aceptar todas las cookies","Aceptar","Accept"]:
            r = js_click_by_text(page, re.escape(txt))
            if r.get('clicked'): log("cookies_accepted", txt=txt); page.wait_for_timeout(1500); break
        shoot(page, "v4_start")

        # 1) Click "Ver lo mejor disponible" para ir a modo quick
        log("step1_ver_lo_mejor")
        res = js_click_by_text(page, r"ver lo mejor disponible")
        log("ver_lo_mejor_click", **res)
        page.wait_for_timeout(5000)
        shoot(page, "v4_after_ver_lo_mejor")

        # 2) Esperar panel offers
        page.wait_for_timeout(2000)

        # 3) Subir cantidad a 4 en stepper "Asiento Reservado - Visibilidad Reducida"
        # (Pista no tiene disponibilidad real → ir directo al fallback Vis. Reducida)
        # Steppers: [0]=Pista, [1]=Asiento Reservado, [2]=Vis. Reducida
        log("step2_qty_4_vis_reducida")
        STEPPER_IDX = 2  # Vis. Reducida
        for i in range(8):
            cur = page.evaluate(f"""
              () => {{
                const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{STEPPER_IDX}];
                if (!s) return -1;
                return parseInt(s.innerText.replace(/\\D/g,''))||0;
              }}
            """)
            log("qty_check", stepper_idx=STEPPER_IDX, current=cur)
            if cur >= 4: break
            r = page.evaluate("""
              (idx) => {
                const steppers = document.querySelectorAll('[data-testid="quantityStepper"]');
                if (steppers.length <= idx) return {ok:false, reason:'stepper_idx_out_of_range', count: steppers.length};
                const first = steppers[idx];
                const btns = first.querySelectorAll('button');
                if (!btns.length) return {ok:false, reason:'no_buttons_in_stepper'};
                let plus = null;
                for (const b of btns) {
                    const al = (b.getAttribute('aria-label')||'').toLowerCase();
                    if (al.includes('aumentar') || al.includes('increase') || al.includes('plus')) { plus = b; break; }
                }
                if (!plus && btns.length >= 2) plus = btns[btns.length-1];
                if (!plus) plus = btns[0];
                if (plus.disabled) return {ok:false, reason:'disabled'};
                plus.scrollIntoView({block:'center'});
                plus.click();
                return {ok:true, qty_text: first.innerText.trim().slice(0,30), aria: plus.getAttribute('aria-label')||''};
              }
            """, STEPPER_IDX)
            log(f"plus_click_{i+1}", **r)
            page.wait_for_timeout(700)
        shoot(page, "v6_after_qty4")

        # 4) Inspect todos los botones HABILITADOS y clica el correcto
        log("step3_inspect_enabled_buttons")
        page.wait_for_timeout(1500)
        enabled = page.evaluate("""
          () => {
            const out = [];
            document.querySelectorAll('button, a[role="button"], [role="button"]').forEach((el,i) => {
                if (el.disabled) return;
                const r = el.getBoundingClientRect();
                if (r.width<30 || r.height<15) return;
                const txt = (el.innerText||'').trim().slice(0,80);
                if (!txt) return;
                out.push({i, txt, y:Math.round(r.y+window.scrollY), tid:el.getAttribute('data-testid')||''});
            });
            return out;
          }
        """)
        log("enabled_buttons", count=len(enabled))
        for b in enabled:
            log("enabled_btn", **b)

        # 4-PRE) Click botón "Entradas" verde (findTicketsBtn) — abre modal con resumen + checkbox
        log("step3a_click_entradas_verde")
        try:
            btn = page.locator('button[data-testid="findTicketsBtn"]').first
            btn.scroll_into_view_if_needed(timeout=4000)
            page.wait_for_timeout(600)
            btn.click(timeout=10000, force=True)
            log("entradas_verde_clicked")
        except Exception as e:
            log("entradas_verde_err", err=str(e)[:120])
        page.wait_for_timeout(5000)
        shoot(page, "v14_after_entradas_verde")

        # 4a) Marcar checkbox "Acepto que he leído esta información" (ahora en modal)
        log("step3b_acepto_info")
        # Scroll al fondo del modal por si está abajo
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

        r = page.evaluate("""
          () => {
            // Buscar usando textContent (no innerText, que ignora elementos ocultos)
            const labels = Array.from(document.querySelectorAll('label'));
            const lbl = labels.find(l => /acepto que he le[ií]do/i.test(l.textContent||''));
            if (!lbl) return {ok:false, reason:'no_label', count_labels: labels.length};
            const forId = lbl.getAttribute('for');
            let cb = null;
            if (forId) cb = document.getElementById(forId);
            if (!cb) cb = lbl.querySelector('input[type="checkbox"]');
            if (!cb && lbl.parentElement) cb = lbl.parentElement.querySelector('input[type="checkbox"]');
            // Buscar checkbox cercano (siguientes hermanos del label)
            if (!cb) {
                let p = lbl;
                for (let i=0; i<4 && p; i++) {
                    const found = p.querySelector('input[type="checkbox"]');
                    if (found) { cb = found; break; }
                    p = p.parentElement;
                }
            }
            if (!cb) return {ok:false, reason:'no_cb', label_html: lbl.outerHTML.slice(0,200)};
            cb.scrollIntoView({block:'center'});
            // Click via JS (puede que TM lo permita)
            cb.click();
            return {ok:true, was_checked: cb.checked, after_click: cb.checked, id: cb.id||'', name:cb.name||'', for_id: forId||''};
          }
        """)
        log("acepto_lookup", **r)

        # Refuerzo: localizar label por textContent y clicarlo + checkbox via coordenadas
        if not r.get('after_click'):
            # Estrategia 1: get_by_text (Playwright)
            try:
                lbl_loc = page.get_by_text("Acepto que he leído", exact=False).first
                lbl_loc.scroll_into_view_if_needed(timeout=4000)
                page.wait_for_timeout(500)
                lbl_loc.click(timeout=5000, force=True)
                log("acepto_clicked_label_text")
            except Exception as e:
                log("acepto_label_err", err=str(e)[:100])
                # Estrategia 2: localizar coordenadas exactas y click
                try:
                    coord = page.evaluate("""
                      () => {
                        const labels = Array.from(document.querySelectorAll('label'));
                        const lbl = labels.find(l => /acepto que he le/i.test(l.textContent||''));
                        if (!lbl) return null;
                        const r = lbl.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2};
                      }
                    """)
                    if coord:
                        page.mouse.click(coord['x'], coord['y'])
                        log("acepto_mouse_click", **coord)
                except Exception as e2: log("acepto_mouse_err", err=str(e2)[:80])
        page.wait_for_timeout(2000)
        # Verificar si quedó marcado
        post_state = page.evaluate("""
          () => {
            const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
            return cbs.map(c => ({checked: c.checked, name:c.name||'', id:c.id||''}));
          }
        """)
        log("checkboxes_post_click", states=post_state[:5])
        shoot(page, "v13_after_acepto")

        # 4b) Click "Continuar comprando" (el CTA que aparece tras qty>0)
        prev = page.url
        log("clicking_continuar_post_qty")
        # Verificar si está enabled (debería estar tras checkbox)
        cta_state = page.evaluate("""
          () => {
            const btns = Array.from(document.querySelectorAll('button'));
            const c = btns.find(b => /^continuar comprando$/i.test((b.textContent||'').trim()));
            if (!c) return {found:false};
            return {found:true, disabled:c.disabled, text:(c.textContent||'').trim()};
          }
        """)
        log("cta_state", **cta_state)

        try:
            btn = page.get_by_role("button", name=re.compile(r"^continuar comprando$", re.I)).first
            btn.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(800)
            btn.click(timeout=10000, force=True)
            log("continuar_clicked_trusted")
            page.wait_for_timeout(15000)
            if page.url != prev: log("URL_CHANGED", to=page.url)
        except Exception as e:
            log("continuar_trusted_err", err=str(e)[:120])
        shoot(page, "v13_after_continuar")

        # 5) Login
        u = page.url.lower()
        if any(k in u for k in ['identity','login','auth','signin','my.tick']):
            login_full(page, account); page.wait_for_timeout(5000)
            u = page.url.lower()

        # 6) Checkout
        if any(k in u for k in ['checkout','payment','cart','pago']):
            fill_card(page, card, account)
        else:
            log("not_checkout", url=page.url)

        log("DONE", url=page.url)
        print("\n" + "="*60)
        print(f"URL: {page.url}")
        print("Para cerrar: crear archivo _compra_u1.STOP")
        print("="*60)
        flag = os.path.join(os.path.dirname(__file__), "_compra_u1.STOP")
        while not os.path.exists(flag): time.sleep(5)
        os.remove(flag); log("stop")

if __name__ == '__main__': main()
