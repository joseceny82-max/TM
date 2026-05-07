"""Wizard PASO A PASO para juanjo1 - se invoca con un argumento de fase.
Usage: python _step.py <phase>
  phases: inspect, click_verify_email, wait_otp_email, enter_otp <code>,
          click_verify_phone, buy_phone, enter_phone, wait_sms, enter_sms <code>,
          finalize
Mantiene el estado entre llamadas via _step_state.json."""
import sys, os, json, time, random, re
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')
from datetime import datetime
import requests

PORT = int(os.environ.get('TM_PORT', '60175'))
STATE_FILE = r'C:\Users\user\TM\proyecto\_step_state.json'
SHOTS = r'C:\Users\user\TM\proyecto\datos\step'
os.makedirs(SHOTS, exist_ok=True)

def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f: return json.load(f)
    except: return {}
def save_state(st):
    with open(STATE_FILE, 'w', encoding='utf-8') as f: json.dump(st, f, indent=2, default=str)

def get_page(pw):
    v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=5).json()
    b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
    ctx = b.contexts[0] if b.contexts else b.new_context()
    page = None
    for p in ctx.pages:
        try:
            if 'ticketmaster' in p.url:
                page = p; break
        except: continue
    if page is None:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try: page.bring_to_front()
    except: pass
    return b, ctx, page

def shot(page, name):
    p = os.path.join(SHOTS, f'{int(time.time())}_{name}.png')
    page.screenshot(path=p)
    print(f'shot: {p}')
    return p

def cmd_inspect():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b, ctx, page = get_page(pw)
        url = page.url
        title = page.title()
        print(f'URL: {url}')
        print(f'Title: {title}')
        # Inputs y botones
        info = page.evaluate("""() => {
            const out = {inputs:[], buttons:[]};
            document.querySelectorAll('input,textarea').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width===0 || r.height===0) return;
                out.inputs.push({tag:el.tagName.toLowerCase(),type:el.type||'',name:el.name||'',id:el.id||'',ph:el.placeholder||'',ac:el.autocomplete||'',val:(el.value||'').slice(0,30),x:Math.round(r.x),y:Math.round(r.y)});
            });
            document.querySelectorAll('button,a[role="button"]').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width===0 || r.height===0) return;
                out.buttons.push({tag:el.tagName.toLowerCase(),text:(el.innerText||el.textContent||'').trim().slice(0,80),x:Math.round(r.x),y:Math.round(r.y)});
            });
            return out;
        }""")
        print('\n--- inputs ---')
        for it in info['inputs']:
            print(f'  type={it["type"]:10} name={it["name"]:20} ac={it["ac"]:18} ph="{it["ph"][:30]}" val="{it["val"]}" id={it["id"]} @({it["x"]},{it["y"]})')
        print('\n--- buttons ---')
        for it in info['buttons']:
            print(f'  "{it["text"]}" @({it["x"]},{it["y"]})')
        shot(page, 'inspect')
        b.close()

def cmd_click(text):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b, ctx, page = get_page(pw)
        try:
            btn = page.get_by_text(text, exact=False).first
            btn.scroll_into_view_if_needed()
            btn.click(timeout=8000)
            print(f'click "{text}" OK')
        except Exception as e:
            print(f'click err: {e}')
            # fallback selector role=button
            try:
                page.get_by_role('button', name=text).first.click(timeout=4000)
                print(f'click via role OK')
            except Exception as e2:
                print(f'role click err: {e2}')
        page.wait_for_timeout(3500)
        shot(page, f'after_click_{text[:25].replace(" ","_")}')
        b.close()

def cmd_wait_otp(timeout_s=180):
    import account_manager as am
    import otp_manager as om
    from imap_tools import MailBox
    acc = next(a for a in am.account_manager.list_accounts() if a['label']=='juanjo1')
    target_to = acc['tm_email'].lower()
    imap_accs = om.list_accounts(decrypt_passwords=True)
    imap = next(a for a in imap_accs if a.get('linked_account_id')==acc['id'])
    print(f'Esperando OTP para "{target_to}" en {imap["email"]} (timeout {timeout_s}s)...')
    cfg = om.get_config()
    patterns = [re.compile(p) for p in (cfg.get('patterns') or om.TM_OTP_PATTERNS)]
    senders = cfg.get('senders') or om.TM_KNOWN_SENDERS
    deadline = time.time() + timeout_s
    # baseline UID
    with MailBox(imap['host'], 993).login(imap['email'], imap['_password'], initial_folder='INBOX') as mb:
        uids = list(mb.uids())
        baseline = max(int(u) for u in uids) if uids else 0
        print(f'  baseline UID = {baseline}')
    while time.time() < deadline:
        try:
            with MailBox(imap['host'], 993).login(imap['email'], imap['_password'], initial_folder='INBOX') as mb:
                msgs = list(mb.fetch(reverse=True, limit=15, mark_seen=False))
                for msg in msgs:
                    try: uid = int(msg.uid)
                    except: continue
                    if uid <= baseline: continue
                    all_to = ' '.join((t.email if hasattr(t,'email') else str(t)) for t in (msg.to_values or [])).lower()
                    sender = (msg.from_ or '').lower()
                    if target_to not in all_to:
                        continue
                    haystack = (msg.subject or '') + ' ' + (msg.text or '') + ' ' + (msg.html or '')
                    for rx in patterns:
                        m = rx.search(haystack)
                        if m:
                            code = m.group(1)
                            print(f'\n📧 OTP: {code}')
                            print(f'   subject: {msg.subject}')
                            print(f'   from: {msg.from_}  to: {all_to}')
                            print(f'   uid: {uid}')
                            return code
        except Exception as e:
            print(f'  poll err: {e}')
        time.sleep(8)
    print('TIMEOUT')
    return None

def cmd_fill_otp(code):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b, ctx, page = get_page(pw)
        # 1) input one-time-code
        for sel in ['input[autocomplete="one-time-code"]','input[name*="otp" i]','input[name*="code" i]','input[id*="otp" i]','input[id*="code" i]']:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    loc.click(); loc.fill(code)
                    print(f'fill via selector {sel}')
                    page.wait_for_timeout(800)
                    shot(page, 'otp_filled')
                    b.close(); return
            except: continue
        # 2) 6 inputs maxlength=1
        try:
            inps = page.locator('input[maxlength="1"]')
            n = inps.count()
            if n >= len(code):
                for i, ch in enumerate(code):
                    inps.nth(i).click(); inps.nth(i).fill(ch); page.wait_for_timeout(80)
                print(f'filled {len(code)} split inputs')
                page.wait_for_timeout(800)
                shot(page, 'otp_filled_split')
                b.close(); return
        except Exception as e: print(f'split err: {e}')
        # 3) primer input visible numérico
        try:
            inp = page.locator('input[type="text"], input[type="tel"], input[type="number"]').first
            inp.click(); inp.fill(code)
            print('filled generic input')
            shot(page, 'otp_filled_generic')
        except Exception as e: print(f'generic err: {e}')
        b.close()

def cmd_buy_phone():
    import fivesim_handler as fs
    print('Comprando virtual4 spain ticketmaster...')
    r = fs.buy_number(country='spain', operator='virtual4', product='ticketmaster')
    if not r.get('ok'):
        print(f'virtual4 fail: {r}')
        print('Intentando virtual58...')
        r = fs.buy_number(country='spain', operator='virtual58', product='ticketmaster')
    if not r.get('ok'):
        print(f'virtual58 fail: {r}')
        print('Intentando any...')
        r = fs.buy_number(country='spain', operator='any', product='ticketmaster')
    print(json.dumps(r, indent=2, default=str))
    if r.get('ok'):
        st = load_state()
        st['phone_order_id'] = r['order_id']
        st['phone'] = r['phone']
        st['phone_price'] = r.get('price')
        save_state(st)
        # Balance
        bal = fs.get_balance()
        print(f'Balance restante: {bal}')

def cmd_fill_phone():
    from playwright.sync_api import sync_playwright
    st = load_state()
    phone = st.get('phone')
    if not phone:
        print('No hay phone en state'); return
    digits = phone.lstrip('+')
    if digits.startswith('34'): digits = digits[2:]
    print(f'Phone: {phone} → meter: {digits}')
    with sync_playwright() as pw:
        b, ctx, page = get_page(pw)
        try:
            inp = page.locator('input[type="tel"]').first
            inp.wait_for(state='visible', timeout=10000)
            inp.click(); inp.fill(digits)
            print('phone filled')
            page.wait_for_timeout(800)
            shot(page, 'phone_filled')
        except Exception as e:
            print(f'fill err: {e}')
        b.close()

def cmd_wait_sms(timeout_s=180):
    import fivesim_handler as fs
    st = load_state()
    oid = st.get('phone_order_id')
    if not oid:
        print('No order_id en state'); return
    print(f'Polling SMS para order_id={oid}...')
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = fs.check_sms(oid)
            if r.get('ok'):
                sms = r.get('sms') or []
                if sms:
                    for s in sms:
                        code = s.get('code') or ''
                        text = s.get('text') or ''
                        print(f'\n📱 SMS recibido')
                        print(f'   code: {code}')
                        print(f'   text: {text}')
                        if code:
                            try: fs.finish_order(oid)
                            except Exception as e: print(f'finish err: {e}')
                            return code
        except Exception as e:
            print(f'  err: {e}')
        time.sleep(7)
    print('TIMEOUT SMS')
    try: fs.cancel_order(oid)
    except: pass

def cmd_save_cookies():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b, ctx, page = get_page(pw)
        cookies = ctx.cookies()
        path = os.path.join(SHOTS, f'cookies_juanjo1_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'cookies': cookies, 'url': page.url, 'profile':'P-79756',
                       'account':'juanjo1', 'captured_at': datetime.now().isoformat()}, f, indent=2)
        print(f'cookies → {path}')
        shot(page, 'final')
        b.close()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python _step.py <phase> [args]')
        sys.exit(1)
    phase = sys.argv[1]
    args = sys.argv[2:]
    if phase == 'inspect': cmd_inspect()
    elif phase == 'click': cmd_click(' '.join(args))
    elif phase == 'wait_otp': cmd_wait_otp(int(args[0]) if args else 180)
    elif phase == 'fill_otp': cmd_fill_otp(args[0])
    elif phase == 'buy_phone': cmd_buy_phone()
    elif phase == 'fill_phone': cmd_fill_phone()
    elif phase == 'wait_sms': cmd_wait_sms(int(args[0]) if args else 180)
    elif phase == 'save_cookies': cmd_save_cookies()
    else: print(f'unknown phase: {phase}')
