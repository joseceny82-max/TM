"""tm_reset_password.py — Restablece contraseña TM de una cuenta y persiste la nueva.

Flujo:
1. Conecta al browser MoreLogin existente (CDP_PORT)
2. Va a /account (o el login OAuth) y click "¿Has olvidado tu contraseña?" → "Solicita uno nuevo"
3. Tipea el email humanizado, submit
4. Polling IMAP madre buscando email TM con link reset (asunto contiene 'restablecer'/'reset')
5. Extrae link único de reset, lo abre en pestaña nueva
6. Genera password segura nueva (12-16 chars, mix upper/lower/dig/special compatibles)
7. Tipea password nueva 2 veces (confirmación) + submit
8. Persiste nueva pwd en account_manager con nota timestamp + actualiza plantilla CSV

Usage:
    from tm_reset_password import reset_password_for
    result = reset_password_for(port=56957, account_label='User-3')
    # → {ok, new_password, account_id, error?}
"""
from __future__ import annotations
import os, time, re, random, string, json
from datetime import datetime
from typing import Optional, Dict, Any


def _gen_password(length: int = 14) -> str:
    """Genera password TM-compatible: mix mayús/minus/dig/símbolo simple, sin chars problemáticos."""
    upper = random.choice(string.ascii_uppercase)
    lower = random.choice(string.ascii_lowercase)
    digit = random.choice(string.digits)
    sym = random.choice("!#@$%&*-_+")
    rest = ''.join(random.choices(string.ascii_letters + string.digits, k=length - 4))
    chars = list(upper + lower + digit + sym + rest)
    random.shuffle(chars)
    return ''.join(chars)


def _human_type(page, selector: str, text: str):
    page.click(selector); time.sleep(random.uniform(0.2, 0.5))
    page.keyboard.press('Control+A'); time.sleep(0.1)
    page.keyboard.press('Delete'); time.sleep(0.3)
    for c in text:
        page.keyboard.type(c)
        time.sleep(0.05 + random.uniform(0, 0.13))


def _human_click(page, locator):
    locator.scroll_into_view_if_needed(timeout=4000)
    time.sleep(random.uniform(0.4, 0.9))
    bbox = locator.bounding_box()
    if not bbox: locator.click(force=True); return
    cx = bbox['x'] + bbox['width']/2
    cy = bbox['y'] + bbox['height']/2
    page.mouse.move(cx - 60, cy - 40, steps=12); time.sleep(0.2)
    page.mouse.move(cx, cy, steps=15); time.sleep(0.15)
    page.mouse.click(cx, cy)


def _accept_cookies_banner(page):
    for txt_re in [r'^Aceptar todas las cookies', r'^Aceptar todo', r'^Accept all']:
        try:
            btn = page.get_by_role('button', name=re.compile(txt_re, re.I)).first
            if btn.is_visible(timeout=1500):
                _human_click(page, btn); return True
        except: continue
    return False


def _wait_for_reset_email(email_madre: str, pwd_madre: str, since_ts: datetime,
                          target_email: str, max_wait_s: int = 600) -> Dict[str, Any]:
    """Polling IMAP iterando TODAS las carpetas dinámicamente. HME relay tarda hasta 15min.
    Mejorado tras informe agente 27-abr: max_wait 600s (10min), folders dinámicos, sleep 4s."""
    from imap_tools import MailBox, AND
    deadline = time.time() + max_wait_s
    target_low = (target_email or '').lower()
    url_pat = re.compile(r'https?://[a-z0-9.-]*ticketmaster\.[a-z]+(?:/[A-Za-z0-9_./?=&%~+#-]+)?', re.I)
    otp_pat = re.compile(r'\b(\d{6})\b')
    # FIX timezone bug: convertir since_ts a UTC-aware si es naive (asume hora local)
    # y comparar TODO en UTC. Evita que emails con timezone distinto al local
    # (e.g. TM envía con offset -06:00 desde mta.emails.ticketmaster.com.mx) sean
    # descartados por aparentar estar en el pasado tras un .replace(tzinfo=None).
    from datetime import timezone as _tz
    if since_ts.tzinfo is None:
        since_utc = since_ts.astimezone().astimezone(_tz.utc)
    else:
        since_utc = since_ts.astimezone(_tz.utc)
    # Damos un margen de 60s hacia atrás por jitter de relojes/TM batching
    since_utc = since_utc.replace(microsecond=0)
    from datetime import timedelta as _td
    since_utc_grace = since_utc - _td(seconds=60)

    while time.time() < deadline:
        try:
            with MailBox('imap.mail.me.com').login(email_madre, pwd_madre) as mb:
                # LISTAR TODOS los folders dinámicamente (no hardcoded)
                all_folders = [f.name for f in mb.folder.list()]
                for fname in all_folders:
                    try: mb.folder.set(fname)
                    except: continue
                    try:
                        # buscar 1 día hacia atrás para no perder por timezone shifts
                        date_search = (since_utc_grace - _td(days=1)).date()
                        msgs = list(mb.fetch(AND(date_gte=date_search), limit=20, reverse=True, mark_seen=False))
                    except: continue
                    def _to_utc(d):
                        if d is None: return None
                        if d.tzinfo is None: return d.astimezone().astimezone(_tz.utc)
                        return d.astimezone(_tz.utc)
                    tm_recent = [m for m in msgs if 'ticketmaster' in (m.from_ or '').lower()
                                  and m.date and _to_utc(m.date) >= since_utc_grace]
                    for m in tm_recent:
                        to_str = ','.join((m.to or [])).lower() + ' ' + (m.cc and ','.join(m.cc).lower() or '')
                        if target_low and target_low not in to_str: continue
                        body = (m.html or '') + '\n' + (m.text or '')
                        subj = (m.subject or '').lower()
                        if not any(k in subj for k in ['restable','reset','contraseña','password','código','code','verify']):
                            continue
                        # OTP detection: priorizar codigos cerca de keywords explicitas
                        # ("código de reinicio", "código de un solo uso", "reset code",
                        # "verification code", "tu código"). Fallback al primer 6-digit
                        # alejado de URL params para evitar matches en query strings.
                        kw_re = re.compile(
                            r'(?:c[oó]digo\s+(?:de\s+(?:reinicio|verificaci[oó]n|seguridad|un\s+solo\s+uso))?|'
                            r'tu\s+c[oó]digo|verification\s+code|reset\s+code|one[\s-]time)\W{0,40}(\d{6})',
                            re.I,
                        )
                        kw_match = kw_re.search(body)
                        otp = None
                        if kw_match:
                            otp = kw_match.group(1)
                        else:
                            # Fallback: tomar el ultimo 6-digit standalone (TM suele
                            # poner el OTP real al final, despues del template ID)
                            cand_otps = []
                            for cm in otp_pat.finditer(body):
                                ctx_before = body[max(0, cm.start()-20):cm.start()]
                                ctx_after = body[cm.end():cm.end()+20]
                                # excluir si esta en un URL/query param
                                if any(c in ctx_before+ctx_after for c in ('=', '?', '&', '/qs')):
                                    continue
                                # excluir IDs repetidos (ej. "121212", "999999")
                                if len(set(cm.group(1))) <= 2:
                                    continue
                                cand_otps.append(cm.group(1))
                            otp = cand_otps[-1] if cand_otps else None
                        urls = url_pat.findall(body)
                        reset_url = next((u for u in urls if any(k in u.lower() for k in
                                          ['reset','password','restablecer','token=','tk='])), None)
                        if not reset_url:
                            long_links = [u for u in urls if len(u) > 80]
                            if long_links: reset_url = long_links[0]
                        return {"otp": otp, "url": reset_url, "subject": m.subject, "folder": fname}
        except Exception: pass
        time.sleep(4)  # más frecuente para no perder ventana
    return {"otp": None, "url": None}


def imap_idle_wait_for_otp(email_madre: str, pwd_madre: str, since_ts: datetime,
                            target_email: str = "", max_wait_s: int = 600) -> Dict[str, Any]:
    """IMAP IDLE — push real desde el servidor (latencia <2s vs polling 4s).
    Requiere `imapclient`. Si no instalado → cae a polling clásico."""
    try:
        from imapclient import IMAPClient
    except ImportError:
        return _wait_for_reset_email(email_madre, pwd_madre, since_ts, target_email, max_wait_s)
    target_low = (target_email or '').lower()
    url_pat = re.compile(r'https?://[a-z0-9.-]*ticketmaster\.[a-z]+(?:/[A-Za-z0-9_./?=&%~+#-]+)?', re.I)
    otp_pat = re.compile(r'\b(\d{6})\b')
    deadline = time.time() + max_wait_s
    try:
        with IMAPClient('imap.mail.me.com', ssl=True) as server:
            server.login(email_madre, pwd_madre)
            server.select_folder('INBOX')
            while time.time() < deadline:
                # Check actual mensajes desde since
                ids = server.search(['SINCE', since_ts.strftime('%d-%b-%Y'), 'FROM', 'ticketmaster'])
                if ids:
                    msgs = server.fetch(ids[-10:], ['ENVELOPE', 'BODY[]'])
                    for mid, data in msgs.items():
                        env = data.get(b'ENVELOPE')
                        body_raw = data.get(b'BODY[]', b'').decode('utf-8', errors='ignore')
                        if target_low and target_low not in body_raw.lower():
                            continue
                        codes = otp_pat.findall(body_raw)
                        otp = next((c for c in codes if 100000 <= int(c) <= 999999), None)
                        urls = url_pat.findall(body_raw)
                        reset_url = next((u for u in urls if any(k in u.lower() for k in
                                          ['reset','password','restablecer','token=','tk='])), None)
                        if otp or reset_url:
                            return {"otp": otp, "url": reset_url, "via": "imap_idle"}
                # IDLE — push (max 29min, recheck cada 60s)
                server.idle()
                try: server.idle_check(timeout=min(60, max(5, deadline - time.time())))
                finally: server.idle_done()
    except Exception as e:
        return _wait_for_reset_email(email_madre, pwd_madre, since_ts, target_email, max_wait_s)
    return {"otp": None, "url": None}


def select_email_reset_option(page) -> bool:
    """Marca el radio 'Envíame un correo electrónico' (id=email-radio-input) y click Siguiente.
    En TM ES la opción TELÉFONO viene preseleccionada por defecto — hay que cambiar a EMAIL.
    Returns True si el radio email quedó checked."""
    try:
        # Click humano en el label asociado al radio email
        lbl = page.locator('label[for="email-radio-input"]').first
        bbox = lbl.bounding_box()
        if bbox:
            cx = bbox['x'] + bbox['width']/2
            cy = bbox['y'] + bbox['height']/2
            page.mouse.move(cx-50, cy-30, steps=12); time.sleep(0.2)
            page.mouse.move(cx, cy, steps=15); time.sleep(0.2)
            page.mouse.click(cx, cy)
        time.sleep(1.5)
        # Verificar
        checked = page.evaluate("""() => {
            const e = document.getElementById('email-radio-input');
            return e ? e.checked : false;
        }""")
        if not checked:
            # Fallback: click directo en el input
            try:
                page.locator('#email-radio-input').click(force=True)
                time.sleep(0.8)
                checked = page.evaluate("""() => document.getElementById('email-radio-input').checked""")
            except: pass
        return bool(checked)
    except Exception:
        return False


def reset_password_for(port: int, account_label: str = "User-3",
                       email_madre: str = "joaquina_polo0@icloud.com",
                       pwd_madre: str = "qspw-lshy-icdg-unbm") -> Dict[str, Any]:
    import requests
    import account_manager as am
    accs = am.account_manager.list_accounts()
    account = next((a for a in accs if a.get('label', '').startswith(account_label)), None)
    if not account: return {"ok": False, "error": f"account_not_found: {account_label}"}
    target_email = account['tm_email']
    old_pwd = account.get('tm_password', '')
    new_pwd = _gen_password(14)
    print(f"[reset] {account_label} email={target_email}")
    print(f"[reset] new_password={new_pwd}")

    v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
    from rebrowser_playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
    ctx = b.contexts[0]

    # Buscar pestaña auth o crear una nueva yendo a /account/forgot-password
    page = next((p for p in ctx.pages if 'auth.ticketmaster' in (p.url or '') or 'identity' in (p.url or '')), None)
    if not page:
        page = ctx.new_page()
        page.goto("https://www.ticketmaster.es/account/forgot-password", wait_until="domcontentloaded", timeout=30000)
    page.bring_to_front()
    time.sleep(2)
    _accept_cookies_banner(page)

    # Click "¿Has olvidado tu contraseña?" o "Solicita uno nuevo"
    for txt_re in [r'solicita uno nuevo', r'has olvidado tu contrase', r'forgot password']:
        try:
            btn = page.get_by_role('button', name=re.compile(txt_re, re.I)).first
            if btn.is_visible(timeout=2000):
                _human_click(page, btn); break
        except: continue
    time.sleep(4)

    # Tipear email
    try:
        page.wait_for_selector('input[type=email], input[name*=email i]', timeout=15000)
    except Exception as e:
        pw.stop(); return {"ok": False, "error": f"no_email_input: {str(e)[:100]}"}
    sel = 'input[type=email]'
    _human_type(page, sel, target_email)
    time.sleep(1.5)
    for txt in ['Siguiente', 'Continuar', 'Next', 'Submit']:
        try:
            btn = page.get_by_role('button', name=re.compile(f'^{txt}$', re.I)).first
            if btn.is_visible(timeout=1500):
                _human_click(page, btn); break
        except: continue
    time.sleep(6)
    print(f"[reset] reset email submitted, waiting for IMAP...")

    since = datetime.now()
    reset_url = _wait_for_reset_email(email_madre, pwd_madre, since, target_email, max_wait_s=180)
    if not reset_url:
        pw.stop()
        return {"ok": False, "error": "reset_email_not_received_180s"}
    print(f"[reset] reset URL received: {reset_url[:80]}...")

    # Navegar al link
    page2 = ctx.new_page()
    page2.goto(reset_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    _accept_cookies_banner(page2)

    # Buscar form de password nueva (2 inputs password)
    try:
        page2.wait_for_selector('input[type=password]', timeout=15000)
    except Exception as e:
        pw.stop(); return {"ok": False, "error": f"no_pwd_form: {str(e)[:100]}"}
    pwds = page2.locator('input[type=password]').all()
    if len(pwds) < 1:
        pw.stop(); return {"ok": False, "error": "no_password_inputs"}
    # Tipear password nueva en el(los) input(s)
    for pi in pwds:
        try:
            pi.scroll_into_view_if_needed(timeout=3000)
            pi.click(); time.sleep(0.4)
            page2.keyboard.press('Control+A'); time.sleep(0.1); page2.keyboard.press('Delete'); time.sleep(0.3)
            for c in new_pwd:
                page2.keyboard.type(c); time.sleep(0.05 + random.uniform(0, 0.1))
            time.sleep(0.8)
        except: pass
    print(f"[reset] new password typed in {len(pwds)} fields")

    # Submit
    for txt in ['Restablecer', 'Cambiar', 'Guardar', 'Confirmar', 'Continuar', 'Next', 'Reset']:
        try:
            btn = page2.get_by_role('button', name=re.compile(txt, re.I)).first
            if btn.is_visible(timeout=1500):
                _human_click(page2, btn); break
        except: continue
    time.sleep(6)

    # Verificar éxito (URL/title cambió, no hay error visible)
    final_url = page2.url
    final_title = ''
    try: final_title = page2.title()
    except: pass
    body = ''
    try: body = page2.evaluate('() => (document.body.innerText||"").slice(0, 600)')
    except: pass
    success = any(k in (body + final_title).lower() for k in ['contraseña actualizada','password updated',
                  'has cambiado','éxito','success','iniciar sesión'])
    print(f"[reset] final URL: {final_url[:90]}")
    print(f"[reset] success markers: {success}")

    pw.stop()

    # Persistir nueva pwd en account_manager
    note = (account.get('notes') or '') + (' | ' if account.get('notes') else '') + \
           f"PWD CAMBIADA {datetime.now().strftime('%Y-%m-%d %H:%M')}: anterior {old_pwd[:4]}***, nueva {new_pwd[:4]}***"
    am.account_manager.update_account(account['id'], {
        'tm_password': new_pwd,
        'notes': note,
        'pwd_last_changed_at': datetime.now().isoformat(),
        'pwd_history': (account.get('pwd_history') or []) + [{
            'old_prefix': old_pwd[:4] + '***',
            'changed_at': datetime.now().isoformat(),
        }],
    })
    print(f"[reset] account_manager updated for {account['id']}")

    # Actualizar plantilla CSV (fila correspondiente)
    try:
        import csv
        csv_path = os.path.join(os.path.dirname(__file__), 'plantilla_tm.csv')
        rows = []
        with open(csv_path, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for r in reader: rows.append(r)
        for r in rows:
            if (r.get('Email_TM') or '').strip().lower() == target_email.lower():
                r['Password_TM'] = new_pwd
                if 'Notas' in r:
                    cur = r.get('Notas') or ''
                    extra = f" | PWD cambiada {datetime.now().strftime('%Y-%m-%d')}"
                    r['Notas'] = (cur + extra).strip()[:200]
                break
        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(rows)
        print(f"[reset] plantilla_tm.csv updated")
    except Exception as e:
        print(f"[reset] csv update fail: {e}")

    return {"ok": True, "account_id": account['id'], "tm_email": target_email,
            "new_password": new_pwd, "old_password_prefix": old_pwd[:4] + '***',
            "success_markers_in_page": success, "final_url": final_url}


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    PORT = int(os.environ.get('CDP_PORT', '0'))
    LABEL = os.environ.get('USER_LABEL', 'User-3')
    if not PORT:
        print("ERROR: set CDP_PORT"); sys.exit(1)
    r = reset_password_for(PORT, LABEL)
    print("\nRESULT:")
    print(json.dumps(r, indent=2, ensure_ascii=False))
