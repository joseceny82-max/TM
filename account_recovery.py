"""account_recovery.py — Auto-recovery de password cuando TM dice 'cuenta ya existe'.

Cuando `register_account.py` detecta que el email YA está registrado en TM (signin mode),
en lugar de archivar como abandoned, este módulo intenta recuperar la cuenta haciendo
el flow de reset password usando el browser ya abierto:

  1. Click "He olvidado mi contraseña" / "Solicita uno nuevo"
  2. Selecciona radio email + click Siguiente
  3. Espera reset URL en IMAP madre (HME relay puede tardar 1-15 min)
  4. Navega al link
  5. Tipea nueva password
  6. Submit
  7. Verifica éxito
  8. Persiste nueva password en plantilla_tm.csv + accounts.json

Devuelve dict con:
  ok: bool
  new_password: str (si ok)
  error: str (si !ok)

API pública:
  recover_inline(page, ctx, user, run) -> Dict
"""
from __future__ import annotations

import csv
import os
import random
import re
import string
import time
from datetime import datetime
from typing import Any, Dict, Optional


def _gen_password(length: int = 14) -> str:
    """Genera password TM-compliant: 12+ chars, mayús+minús+número+símbolo."""
    upper = random.choice(string.ascii_uppercase)
    lower = random.choice(string.ascii_lowercase)
    digit = random.choice(string.digits)
    symbol = random.choice('!@#$%&*+-=?^')
    rest_pool = string.ascii_letters + string.digits + '!@#$%&*+-=?^'
    rest = ''.join(random.choice(rest_pool) for _ in range(max(0, length - 4)))
    pwd_list = list(upper + lower + digit + symbol + rest)
    random.shuffle(pwd_list)
    return ''.join(pwd_list)


def _click_button_by_text(page, patterns, timeout_ms: int = 2000) -> bool:
    """Intenta clickear el primer botón que matchee uno de los patterns regex."""
    for pat in patterns:
        try:
            btn = page.get_by_role('button', name=re.compile(pat, re.I)).first
            if btn.is_visible(timeout=timeout_ms):
                btn.click()
                return True
        except Exception:
            continue
        # también intentar como link
        try:
            link = page.get_by_role('link', name=re.compile(pat, re.I)).first
            if link.is_visible(timeout=timeout_ms):
                link.click()
                return True
        except Exception:
            continue
    return False


def _select_email_reset_radio(page) -> bool:
    """En la pantalla 'cómo enviar reset', selecciona radio EMAIL (no teléfono)."""
    try:
        # primero intenta por id estándar
        page.locator('#email-radio-input').click(force=True, timeout=2000)
        time.sleep(0.5)
        checked = page.evaluate(
            "() => { const e=document.getElementById('email-radio-input'); return e ? e.checked : false; }"
        )
        if checked:
            return True
    except Exception:
        pass
    # Fallback: label que contenga "correo" / "email"
    try:
        for txt in ['correo electr', 'email', 'send.*email', 'enviar.*correo']:
            lbl = page.get_by_text(re.compile(txt, re.I)).first
            if lbl.is_visible(timeout=1500):
                lbl.click()
                time.sleep(0.4)
                return True
    except Exception:
        pass
    return False


def _wait_reset_email_via_imap(email_madre: str, pwd_madre: str,
                                target_email: str, since: datetime,
                                max_wait_s: int = 300) -> Dict[str, Optional[str]]:
    """Polling IMAP buscando reset URL O CÓDIGO OTP de 6 dígitos desde TM.
    Devuelve dict {url, otp} - puede contener ambos, uno o ninguno (timeout)."""
    try:
        from tm_reset_password import _wait_for_reset_email
        res = _wait_for_reset_email(email_madre, pwd_madre, since, target_email,
                                     max_wait_s=max_wait_s)
        if isinstance(res, dict):
            return {'url': res.get('url'), 'otp': res.get('otp')}
    except Exception:
        pass
    return {'url': None, 'otp': None}


def _capture_session_after_recovery(page, ctx, email, new_pwd, user, run, *, log=print) -> Dict[str, Any]:
    """Tras reset password OK, hace login real con email+new_pwd para capturar
    storage_state + cookies. Norma user 2026-05-09. Devuelve {state_file, cookies_file}
    o {} si falla (no fatal — cuenta queda sin storage pero sigue accesible)."""
    import time, json as _json
    out = {}
    try:
        log('  🔐 capturando sesión post-recovery (login real)...')
        # Navegar a página de login
        page.goto('https://my.ticketmaster.es/', wait_until='domcontentloaded', timeout=25000)
        time.sleep(3)

        # Email field
        try:
            em_inp = page.locator('input[type=email], input[name*=email i]').first
            em_inp.wait_for(timeout=10000)
            em_inp.click()
            try: page.keyboard.press('Control+A'); page.keyboard.press('Delete')
            except Exception: pass
            em_inp.type(email, delay=60)
            time.sleep(0.6)
        except Exception as e:
            log(f'    ⚠ email input err: {str(e)[:80]} — abort capture (no fatal)')
            return {}

        # Click "Continuar" / "Next" si TM usa flow 2-pasos
        try:
            cont = page.locator('button[type=submit], button:has-text("Continu")').first
            if cont.is_visible(timeout=2000):
                cont.click()
                time.sleep(2)
        except Exception: pass

        # Password field
        try:
            pwd_inp = page.locator('input[type=password]').first
            pwd_inp.wait_for(timeout=10000)
            pwd_inp.click()
            pwd_inp.type(new_pwd, delay=60)
            time.sleep(0.5)
        except Exception as e:
            log(f'    ⚠ pwd input err: {str(e)[:80]} — abort capture (no fatal)')
            return {}

        # Click "Iniciar sesión"
        try:
            login_btn = page.locator('button[type=submit]').first
            if login_btn.is_visible(timeout=2000):
                login_btn.click()
                time.sleep(5)
        except Exception:
            try: page.keyboard.press('Enter')
            except Exception: pass

        # Esperar landing (dashboard / settings / cualquier URL my.ticketmaster.*)
        try:
            page.wait_for_url(lambda u: 'my.ticketmaster' in u and 'sign' not in u.lower()
                              and 'auth' not in u.lower(), timeout=20000)
        except Exception:
            time.sleep(4)  # fallback espera ciega

        # ¿Sigue en sign-in? Si sí, falló el login (pwd incorrecta o algo)
        cur_url = page.url.lower()
        if 'sign' in cur_url or 'auth' in cur_url:
            log(f'    ⚠ login no avanzó (url={cur_url[:60]}) — sin storage capture')
            return {}

        # OK — capturar storage_state + cookies
        from datetime import datetime
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        n = getattr(user, 'n', '?')
        proj = os.path.dirname(os.path.abspath(__file__))
        sessions_dir = os.path.join(proj, 'datos', 'sessions')
        cookies_dir = os.path.join(proj, 'datos', 'cookies')
        os.makedirs(sessions_dir, exist_ok=True)
        os.makedirs(cookies_dir, exist_ok=True)

        state_file = os.path.join(sessions_dir, f'tm_u{n}_recovered_{ts}_state.json')
        cookies_file = os.path.join(cookies_dir, f'tm_morelogin_u{n}_recovered_{ts}.json')

        try:
            state = ctx.storage_state()
            with open(state_file, 'w', encoding='utf-8') as f:
                _json.dump(state, f, indent=2)
            out['state_file'] = state_file
            log(f'    ✓ storage_state → {os.path.basename(state_file)} ({len(state.get("cookies",[]))} cookies)')
        except Exception as e:
            log(f'    ⚠ storage_state err: {str(e)[:80]}')

        try:
            cookies = ctx.cookies()
            with open(cookies_file, 'w', encoding='utf-8') as f:
                _json.dump(cookies, f, indent=2)
            out['cookies_file'] = cookies_file
            log(f'    ✓ cookies → {os.path.basename(cookies_file)} ({len(cookies)} cookies)')
        except Exception as e:
            log(f'    ⚠ cookies err: {str(e)[:80]}')

        return out
    except Exception as e:
        log(f'    ⚠ capture session exception: {str(e)[:120]} — abortado (no fatal)')
        return {}


def _persist_recovered_password(email: str, new_pwd: str, user, *, session_files=None) -> Dict:
    """Sincroniza la NUEVA password en TODOS los archivos donde aparezca la cuenta.

    Cobertura:
      - plantilla_tm.csv (Password_TM + Notas)
      - accounts.json (tm_password + recovered_at + status=active + pwd_history)
      - datos/cuentas/cuentas_tarjetas.csv (Password_TM si existe la fila)
      - datos/buy/runs/registered_archive.csv (todas las apariciones — historial)
      - datos/buy/runs/abandoned_archive.csv (idem)
      - tm_registrations.json (campo password si existe)
    """
    out = {
        'plantilla_updated': False,
        'accounts_updated': False,
        'cuentas_tarjetas_updated': 0,
        'registered_archive_updated': 0,
        'abandoned_archive_updated': 0,
        'tm_registrations_updated': False,
        'old_pwd_prefix': '',
    }
    base = os.path.dirname(__file__)
    em_low = (email or '').lower().strip()
    if not em_low:
        out['error'] = 'no_email'
        return out

    # Helper genérico para actualizar Password_TM en cualquier CSV con esa columna
    def _update_csv_password(path: str, key_field_candidates=('Email_TM', 'Email')) -> int:
        if not os.path.exists(path):
            return 0
        try:
            with open(path, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                fields = reader.fieldnames
                rows = list(reader)
            if not fields:
                return 0
            updated = 0
            pwd_field = next((k for k in fields if k in ('Password_TM', 'tm_password', 'Password', 'password')), None)
            if not pwd_field:
                return 0
            for r in rows:
                em = ''
                for kf in key_field_candidates:
                    em = (r.get(kf) or '').strip().lower()
                    if em: break
                if em == em_low:
                    if not out['old_pwd_prefix'] and r.get(pwd_field):
                        out['old_pwd_prefix'] = (r.get(pwd_field) or '')[:4] + '***'
                    r[pwd_field] = new_pwd
                    if 'Notas' in fields:
                        cur = r.get('Notas') or ''
                        extra = f' | recuperada {datetime.now().strftime("%Y-%m-%d %H:%M")}'
                        if 'recuperada' not in cur:
                            r['Notas'] = (cur + extra).strip()[:200]
                    updated += 1
            if updated:
                with open(path, 'w', encoding='utf-8', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=fields)
                    w.writeheader()
                    w.writerows(rows)
            return updated
        except Exception as e:
            out.setdefault('csv_errors', []).append(f'{os.path.basename(path)}: {str(e)[:80]}')
            return 0

    # 1) plantilla_tm.csv
    n = _update_csv_password(os.path.join(base, 'plantilla_tm.csv'))
    out['plantilla_updated'] = n > 0

    # 2) accounts.json (via account_manager) — preserva pwd_history
    try:
        import account_manager as am
        existing = None
        for a in am.account_manager.list_accounts():
            if (a.get('tm_email') or '').lower() == em_low:
                existing = a
                break
        old_pwd = (existing or {}).get('tm_password', '')
        if old_pwd and not out['old_pwd_prefix']:
            out['old_pwd_prefix'] = old_pwd[:4] + '***'
        pwd_history = list((existing or {}).get('pwd_history') or [])
        if old_pwd:
            pwd_history.append({
                'old_prefix': old_pwd[:4] + '***',
                'changed_at': datetime.now().isoformat(),
                'reason': 'auto_recovery_signin_mode',
            })
        first = (getattr(user, 'first', '') or '').strip()
        last = (getattr(user, 'last', '') or '').strip()
        # Label estilo dashboard: "Nombre Apellidos · email" (igual al
        # generado por register_in_dashboard para que la UI renderice
        # correctamente la cabecera de la cuenta).
        label = f'{first} {last} · {email}'.strip() if (first or last) else email
        # Phone separado por componentes (igual a register_in_dashboard)
        phone_prefix = getattr(user, 'phone_prefix', '') or ''
        phone_local = getattr(user, 'phone_local', '') or ''
        phone_full = f"{phone_prefix}{phone_local}"
        meta = {
            'tm_email': email,
            'tm_password': new_pwd,
            'first_name': first,
            'last_name': last,
            'label': label,
            'phone': phone_local,
            'phone_prefix': phone_prefix,
            'phone_full': phone_full,
            'country': getattr(user, 'country', '') or 'es',
            'address1': getattr(user, 'address1', '') or '',
            'city': getattr(user, 'city', '') or '',
            'state': getattr(user, 'state', '') or '',
            'postal': getattr(user, 'postal', '') or '',
            'dob': getattr(user, 'dob', '') or '',
            'nationality': getattr(user, 'nationality', '') or '',
            'passport': getattr(user, 'passport', '') or '',
            'email': getattr(user, 'contact_email', '') or '',
            'email_password': getattr(user, 'contact_pwd', '') or '',
            'status': 'active',
            'purchase_ready': True,   # norma user 2026-05-09: cuenta usable en compra
            'phone_verified_at': datetime.now().isoformat(),
            'registration_completed_at': datetime.now().isoformat(),
            'recovered_at': datetime.now().isoformat(),
            'pwd_last_changed_at': datetime.now().isoformat(),
            'pwd_history': pwd_history[-20:],  # cap historial
            'notes': f'Recovered via auto-reset_password',
        }
        # Session files capturados tras login real (si hay)
        if session_files:
            if session_files.get('state_file'):
                meta['storage_state_file'] = session_files['state_file']
            if session_files.get('cookies_file'):
                meta['cookies_file'] = session_files['cookies_file']
        if existing:
            am.account_manager.update_account(existing['id'], meta)
            out['accounts_updated'] = True
            out['account_id'] = existing['id']
        else:
            new_a = am.account_manager.add_account(meta)
            out['accounts_updated'] = True
            out['account_id'] = (new_a or {}).get('id', '')

        # Asociar tarjeta desde UserRow (si la cuenta no tiene una ya)
        # Norma user 2026-05-09: cuentas recovered deben tener card para compra.
        try:
            aid = out.get('account_id')
            if aid:
                existing_cards = [c for c in am.account_manager.list_cards()
                                   if c.get('account_id') == aid]
                if not existing_cards:
                    card_number = (getattr(user, 'card_number', '') or '').strip()
                    if card_number:
                        am.account_manager.add_card({
                            'account_id': aid,
                            'label': f'Tarjeta principal recovery U{getattr(user, "n", "?")}',
                            'holder': getattr(user, 'card_holder', '') or '',
                            'number': card_number,
                            'exp_month': getattr(user, 'card_month', '') or '',
                            'exp_year': getattr(user, 'card_year', '') or '',
                            'cvv': getattr(user, 'card_cvv', '') or '',
                            'last4': card_number[-4:],
                            'brand': 'visa' if card_number.startswith('4') else 'mastercard',
                        })
                        out['card_associated'] = True
        except Exception as e:
            out['card_error'] = str(e)[:120]
    except Exception as e:
        out['accounts_error'] = str(e)[:120]

    # 3) cuentas_tarjetas.csv
    out['cuentas_tarjetas_updated'] = _update_csv_password(
        os.path.join(base, 'datos', 'cuentas', 'cuentas_tarjetas.csv'))

    # 4) registered_archive.csv (historial — actualiza todas las apariciones)
    out['registered_archive_updated'] = _update_csv_password(
        os.path.join(base, 'datos', 'buy', 'runs', 'registered_archive.csv'))

    # 5) abandoned_archive.csv
    out['abandoned_archive_updated'] = _update_csv_password(
        os.path.join(base, 'datos', 'buy', 'runs', 'abandoned_archive.csv'))

    # 6) tm_registrations.json — actualiza campo password en items que match
    try:
        import json as _json
        path = os.path.join(base, 'tm_registrations.json')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                d = _json.load(f)
            items = d.get('items') if isinstance(d, dict) else d
            if isinstance(items, list):
                changed = False
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if (it.get('email') or '').lower() == em_low:
                        for pk in ('password', 'tm_password', 'Password_TM'):
                            if pk in it or it.get(pk) is not None:
                                it[pk] = new_pwd
                                changed = True
                        # también guardar metadatos
                        it['pwd_recovered_at'] = datetime.now().isoformat()
                        changed = True
                if changed:
                    try:
                        import json_atomic
                        json_atomic.save(path, d)
                    except Exception:
                        with open(path, 'w', encoding='utf-8') as f:
                            _json.dump(d, f, indent=2, ensure_ascii=False)
                    out['tm_registrations_updated'] = True
    except Exception as e:
        out['tm_registrations_error'] = str(e)[:120]

    # 7) WebSocket broadcast → fuerza al dashboard a recargar las pestañas
    #    "Cuentas y Tarjetas" + "Registros TM" sin que el usuario haga F5.
    try:
        import urllib.request as _urlreq
        import json as _json
        # Llamada HTTP local al endpoint del dashboard que dispara ws_broadcast
        # Cada uno de estos eventos refresca una pestaña distinta del dashboard.
        events = [
            ('account_updated', {'email': email, 'reason': 'recovered',
                                  'recovered_at': datetime.now().isoformat()}),
            ('accounts_changed', {'changed': 1}),
            ('registros_changed', {'email': email}),
            ('password_changed', {'email': email,
                                    'old_prefix': out.get('old_pwd_prefix', ''),
                                    'reason': 'auto_recovery'}),
        ]
        for ev_name, payload in events:
            try:
                req = _urlreq.Request(
                    'http://127.0.0.1:5055/api/internal/ws-broadcast',
                    data=_json.dumps({'event': ev_name, 'payload': payload}).encode(),
                    headers={'Content-Type': 'application/json',
                             'X-Internal-Token': 'recovery'},
                    method='POST',
                )
                _urlreq.urlopen(req, timeout=2)
            except Exception:
                pass  # dashboard puede no estar arriba; no bloquear recovery
        out['ws_broadcast_sent'] = True
    except Exception as e:
        out['ws_broadcast_error'] = str(e)[:120]

    return out


def recover_inline(page, ctx, user, run, *, log=print,
                    max_imap_wait_s: int = 300,
                    request_already_sent: bool = False) -> Dict[str, Any]:
    """Intenta recuperar la cuenta usando el browser ya abierto en signin mode.

    Args:
      page: rebrowser_playwright Page actualmente en pantalla signin TM
      ctx:  BrowserContext (para abrir nuevas pestañas si hace falta)
      user: UserRow del registro que falló (tiene email + contact_email + contact_pwd)
      run:  RunState (para logging)
      log:  función para logear (debe aceptar str)
      max_imap_wait_s: cuánto esperar el reset email (default 5 min)

    Returns:
      {ok, new_password?, error?, persist?}
    """
    target_email = (user.email or '').strip()
    if not target_email:
        return {'ok': False, 'error': 'no_email_in_user'}

    new_pwd = _gen_password(14)
    log(f'  🔓 RECOVERY iniciado para {target_email} (nueva pwd preview: {new_pwd[:4]}***)')

    try:
        if not request_already_sent:
            # 1. Click forgot-password — la SPA de TM puede seguir en transición
            #    (title Sign In → Sign Up) justo al entrar al recovery; los
            #    is_visible lanzan excepción rápida por contexto destruido y el
            #    clic fallaba en <100ms dando no_forgot_button falso (row8,
            #    2026-11-07). Reintentar con espera de estabilidad entre pasadas.
            clicked = False
            for stab_retry in range(6):
                clicked = _click_button_by_text(page, [
                    r'has olvidado tu contrase', r'olvidaste.*contrase',
                    r'forgot.*password', r'restablecer.*contrase',
                    r'solicita uno nuevo',
                ], timeout_ms=3000)
                if clicked:
                    break
                log(f'  ⏳ forgot-button no encontrado (stab retry {stab_retry+1}/6) — esperando SPA…')
                time.sleep(2.0)
                try:
                    page.wait_for_load_state('networkidle', timeout=4000)
                except Exception:
                    pass
            if not clicked:
                # Antes se navegaba a /account/forgot-password, pero esa URL da 404
                # en TM y el flujo esperaba 300 s un email que nunca se pedía.
                try:
                    body = (page.evaluate('() => document.body.innerText') or '').lower()
                except Exception:
                    body = ''
                if 'suspendido' in body or 'suspended' in body:
                    return {'ok': False, 'error': 'bot_block_detected: sesión suspendida (recovery)'}
                return {'ok': False, 'error': 'no_forgot_button'}

            # 2. Tipear email (puede que ya esté pre-rellenado)
            time.sleep(2)
            try:
                em_inp = page.locator('input[type=email], input[name*=email i]').first
                em_inp.wait_for(timeout=10000)
                cur = (em_inp.input_value() or '').strip().lower()
                if cur != target_email.lower():
                    em_inp.click()
                    page.keyboard.press('Control+A')
                    page.keyboard.press('Delete')
                    em_inp.type(target_email, delay=80)
                    time.sleep(0.5)
            except Exception:
                pass

            # 3. Click Siguiente / Continuar
            _click_button_by_text(page, [r'^siguiente$', r'^continuar$', r'^next$',
                                           r'^submit$', r'^enviar$'], timeout_ms=2000)
            time.sleep(3)

            # 4. Si hay pantalla "cómo recibir el reset" → seleccionar email
            try:
                if page.locator('#email-radio-input').is_visible(timeout=2000):
                    _select_email_reset_radio(page)
                    _click_button_by_text(page, [r'^siguiente$', r'^continuar$',
                                                   r'^enviar$'], timeout_ms=2000)
            except Exception:
                pass
        else:
            log('  ⏭ skip steps 1-4 (request_already_sent=True)')

        # 5. Esperar reset URL/OTP en IMAP (sin sleep previo — TM envía en <5s)
        log(f'  📬 esperando reset email (max {max_imap_wait_s}s, IMAP madre={user.contact_email})...')
        since = datetime.now()
        time.sleep(2)  # mínimo para que TM dispare envío
        mail = _wait_reset_email_via_imap(
            email_madre=user.contact_email,
            pwd_madre=user.contact_pwd,
            target_email=target_email,
            since=since,
            max_wait_s=max_imap_wait_s,
        )
        reset_url = mail.get('url') if isinstance(mail, dict) else None
        otp = mail.get('otp') if isinstance(mail, dict) else None
        if not reset_url and not otp:
            return {'ok': False, 'error': f'reset_email_not_received_{max_imap_wait_s}s'}

        # Detectar si TM está pidiendo OTP en la página actual (signin/reset).
        # Si la pantalla actual contiene "código de un solo uso" / "Escribe tu código" /
        # "one-time code" / placeholder de OTP, IGNORAR la URL y usar OTP path.
        page_asks_otp = False
        try:
            body_now = page.evaluate('() => (document.body.innerText||"").slice(0, 2000)') or ''
            otp_keywords = ['código de un solo uso', 'escribe tu código',
                             'codigo de un solo uso', 'escribe tu codigo',
                             'one-time code', 'one time code',
                             'verification code', 'enter your code',
                             'introducir el código', 'tu código a continuación',
                             'tu codigo a continuacion']
            page_asks_otp = any(k in body_now.lower() for k in otp_keywords)
        except Exception:
            pass

        log(f'  📊 url_disponible={bool(reset_url)} otp_disponible={bool(otp)} pagina_pide_otp={page_asks_otp}')

        page2 = None
        # Estrategia: si la página YA pide OTP, usar OTP path (ignorar URL).
        # Si no pide OTP y hay URL, usar URL. Si hay solo OTP, usar OTP.
        prefer_otp = page_asks_otp and otp
        if not prefer_otp and reset_url:
            log(f'  ✓ reset URL recibida: {reset_url[:90]}')
            # 6a. Navegar al link en pestaña nueva
            page2 = ctx.new_page()
            try:
                page2.goto(reset_url, wait_until='domcontentloaded', timeout=30000)
                time.sleep(4)
            except Exception as e:
                try: page2.close()
                except Exception: pass
                page2 = None
                # si tenemos OTP de fallback, seguimos en la página actual
                if not otp:
                    return {'ok': False, 'error': f'reset_url_nav_failed: {str(e)[:100]}'}

        if otp and not page2:
            # 6b. TM pide código OTP en la página actual (signin/reset) — tipear los 6 dígitos
            log(f'  ✓ código OTP recibido por email: {otp} (page_asks_otp={page_asks_otp})')
            try:
                # Probar TODOS los frames (TM puede meter OTP form en iframe)
                frames = [page] + (page.frames if hasattr(page, 'frames') else [])
                code_inp = None
                inps_separate = []  # 6 inputs de 1 char
                for fr in frames:
                    if code_inp or inps_separate: break
                    # Selectores en orden de especificidad
                    sels = [
                        'input[autocomplete="one-time-code"]',
                        'input[inputmode="numeric"][maxlength="6"]',
                        'input[name*="code" i][type="text"]',
                        'input[name*="otp" i]',
                        'input[id*="code" i]',
                        'input[id*="otp" i]',
                        'input[placeholder*="código" i]',
                        'input[placeholder*="codigo" i]',
                        'input[placeholder*="code" i]',
                        'input[type="tel"][maxlength="6"]',
                        'input[type="number"][maxlength="6"]',
                        'input[maxlength="6"]',
                    ]
                    for sel in sels:
                        try:
                            loc = fr.locator(sel).first
                            if loc.is_visible(timeout=1200):
                                code_inp = loc; break
                        except Exception: continue
                    # Fallback: 6 inputs de 1 dígito (TM a veces hace eso)
                    if not code_inp:
                        try:
                            singles = fr.locator('input[maxlength="1"]').all()
                            visible_singles = [x for x in singles if x.is_visible(timeout=500)]
                            if len(visible_singles) >= 6:
                                inps_separate = visible_singles[:6]
                                break
                        except Exception: pass

                if not code_inp and not inps_separate:
                    return {'ok': False, 'error': 'no_otp_input_found'}

                if inps_separate:
                    log(f'  ⌨ tipeando OTP en 6 inputs separados')
                    for i, ch in enumerate(str(otp)[:6]):
                        try:
                            inps_separate[i].click()
                            time.sleep(0.15)
                            inps_separate[i].fill(ch)
                            time.sleep(0.1 + random.uniform(0, 0.1))
                        except Exception:
                            try: page.keyboard.type(ch); time.sleep(0.1)
                            except: pass
                else:
                    log(f'  ⌨ tipeando OTP con keystrokes humanos reales (NuData)')
                    try:
                        code_inp.scroll_into_view_if_needed(timeout=2000)
                    except Exception: pass
                    code_inp.click()
                    time.sleep(0.3)
                    # Limpiar previous content
                    try:
                        code_inp.fill('')
                    except Exception:
                        page.keyboard.press('Control+A')
                        page.keyboard.press('Delete')
                    time.sleep(0.2)
                    # FIX 2026-05-14 — typing REAL con keyboard.type + delay humano
                    # Antes hacía .fill() instant → TM/NuData rechazaba OTP submit
                    # con error "VALIDATEOTPPASSWORDRESET" porque typing demasiado
                    # rápido. Ahora simula humano: 120-280ms entre teclas.
                    try:
                        for c in str(otp):
                            page.keyboard.type(c)
                            # Delay humano: 120-280ms entre teclas, jitter
                            time.sleep(0.12 + random.uniform(0, 0.16))
                    except Exception:
                        # Fallback: fill() si keyboard.type falla
                        try: code_inp.fill(str(otp))
                        except Exception: pass
                time.sleep(1.5)
                # Disparar eventos input/change explícitos para activar React validation
                # (algunos widgets TM solo habilitan el botón tras un input event real)
                try:
                    code_inp.evaluate(
                        "(el) => { el.dispatchEvent(new Event('input', {bubbles:true}));"
                        " el.dispatchEvent(new Event('change', {bubbles:true}));"
                        " el.dispatchEvent(new Event('blur', {bubbles:true})); }"
                    )
                except Exception:
                    pass
                time.sleep(0.4)
                # Submit OTP — esperar a que el botón sea CLICKABLE (no disabled)
                try:
                    page.evaluate(
                        "() => { const btns = [...document.querySelectorAll('button')];"
                        " const b = btns.find(x => /confirmar|verificar|continuar|siguiente|next|submit/i.test(x.textContent||''));"
                        " if (b) b.removeAttribute('disabled'); }"
                    )
                except Exception:
                    pass
                _click_button_by_text(page, [r'^confirmar', r'^verificar',
                                              r'^continuar$', r'^siguiente$',
                                              r'^enviar$', r'^validar',
                                              r'^next$', r'^submit$'], timeout_ms=3000)
                # Capturar screenshot diagnóstico post-OTP-submit
                try:
                    _shot_path = f"runs/recovery_post_otp_{int(time.time())}.png"
                    page.screenshot(path=_shot_path, full_page=True, timeout=5000)
                except Exception:
                    pass
                time.sleep(3.0)
                page2 = page  # seguimos en la misma página, ahora en pantalla password
            except Exception as e:
                return {'ok': False, 'error': f'otp_paste_failed: {str(e)[:100]}'}

        if not page2:
            return {'ok': False, 'error': 'no_reset_path_available'}

        # 7. Tipear nueva password en input(s) password
        # FIX: timeout extendido a 30s + screenshot diagnóstico + dump del body
        # para entender qué TM muestra realmente (ej. otro modal, captcha, etc.)
        try:
            page2.wait_for_selector('input[type=password]', timeout=30000)
        except Exception:
            try:
                _shot_path = f"runs/recovery_no_pwd_form_{int(time.time())}.png"
                page2.screenshot(path=_shot_path, full_page=True, timeout=5000)
                _body_preview = (page2.evaluate(
                    "() => (document.body && document.body.innerText || '').slice(0, 800)"
                ) or '')[:500]
            except Exception:
                _body_preview = ""
            if page2 is not page:
                try: page2.close()
                except Exception: pass
            return {'ok': False, 'error': 'no_password_form_in_reset_page',
                    'body_preview': _body_preview}

        pwds = page2.locator('input[type=password]').all()
        if not pwds:
            if page2 is not page:
                try: page2.close()
                except Exception: pass
            return {'ok': False, 'error': 'no_password_inputs'}

        for pi in pwds:
            try:
                pi.scroll_into_view_if_needed(timeout=3000)
                pi.click()
                time.sleep(0.3)
                page2.keyboard.press('Control+A')
                page2.keyboard.press('Delete')
                time.sleep(0.2)
                # FIX 2026-05-14: delay humano 80-200ms (NuData kb)
                for c in new_pwd:
                    page2.keyboard.type(c)
                    time.sleep(0.08 + random.uniform(0, 0.12))
                time.sleep(0.5)
            except Exception:
                pass

        # 8. Submit
        _click_button_by_text(page2, [r'restablecer', r'cambiar', r'guardar',
                                        r'confirmar', r'^continuar$', r'^next$',
                                        r'reset', r'^submit$'], timeout_ms=2000)
        time.sleep(3.5)

        # 9. Verificar éxito (multi-señal: body, title, URL, ausencia de input password)
        body = ''
        try:
            body = page2.evaluate('() => (document.body.innerText||"").slice(0, 1500)')
        except Exception:
            pass
        title = ''
        try: title = page2.title() or ''
        except Exception: pass
        url_now = ''
        try: url_now = page2.url or ''
        except Exception: pass

        success_markers = ['contraseña actualizada', 'password updated',
                            'has cambiado', 'cambiada con éxito', 'cambiada correctamente',
                            'éxito', 'success', 'iniciar sesión', 'sign in',
                            'log in', 'restablecida', 'restablecido',
                            'password has been', 'updated your password',
                            'tu nueva contraseña', 'password changed']
        success_url_paths = ['/signin', '/login', '/account', '/my', '/home',
                              'success', 'completed', 'updated',
                              # OAuth landing tras reset success en TM
                              '/as/authorization', 'authorization.oauth2',
                              'identity.ticketmaster', 'pf-ws/authorization']
        # Marker A: text en body/title
        text_match = any(k in (body + title).lower() for k in success_markers)
        # Marker B: URL cambió a sign-in / cuenta / etc
        url_match = any(p in url_now.lower() for p in success_url_paths) and 'reset' not in url_now.lower() and 'forgot' not in url_now.lower()
        # Marker C: ya no hay form de password (TM aceptó la nueva pwd y avanzó)
        try:
            still_pwd_form = page2.locator('input[type=password]').count() > 0
        except Exception:
            still_pwd_form = False
        no_pwd_form_match = not still_pwd_form

        # Detectar errores explícitos en página
        error_keywords = ['no válido', 'inválido', 'incorrecto', 'invalid',
                           'error', 'inténtalo de nuevo', 'try again',
                           'expirado', 'expired', 'caducado']
        explicit_error = any(k in body.lower() for k in error_keywords)

        success = (text_match or url_match or no_pwd_form_match) and not explicit_error
        log(f'  📊 success_check: text={text_match} url={url_match} no_pwd_form={no_pwd_form_match} explicit_err={explicit_error} url={url_now[:80]}')

        if page2 is not page:
            try: page2.close()
            except Exception: pass

        if not success:
            return {'ok': False, 'error': 'no_success_marker_after_submit',
                    'final_title': title[:80], 'final_url': url_now[:120],
                    'body_sample': body[:200]}

        # 10. POST-RESET: hacer login REAL con la nueva pwd para capturar
        #     storage_state + cookies. Sin esto, la cuenta queda sin sesión usable
        #     en compra (limitación documentada en feedback_recovery_complete_persistence.md).
        #     Norma user 2026-05-09.
        session_files = _capture_session_after_recovery(
            page, ctx, target_email, new_pwd, user, run, log=log)

        # 11. Persistir (incluye session_files si se capturaron)
        persist = _persist_recovered_password(
            target_email, new_pwd, user,
            session_files=session_files)
        log(f'  💾 persist: {persist}')

        return {
            'ok': True,
            'new_password': new_pwd,
            'persist': persist,
            'session_files': session_files,
            'final_title': title[:80],
        }
    except Exception as e:
        return {'ok': False, 'error': f'recovery_exception: {str(e)[:150]}'}


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
