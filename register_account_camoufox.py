"""register_account_camoufox.py — Versión con Camoufox (Firefox+Juggler).

Usa Camoufox en lugar de MoreLogin/Chromium para evadir Akamai BMSC/HUMAN
que está bloqueando el rango de IPs QuantumProxies sobre Chromium.

Camoufox ataca el vector "CDP Protocol Detection" (#1 detector).

Uso:
  python register_account_camoufox.py --row 22
  python register_account_camoufox.py --rows 22-30

Reusa toda la lógica de register_account.py: load_user, RunState, email_otp,
phone_relentless, register_in_dashboard, archive plantilla. Solo cambia el
método de abrir el browser.
"""
from __future__ import annotations
import argparse, json, os, random, re, socket, sys, time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding='utf-8')
import requests

ROOT = r'C:\Users\user\TM\proyecto'
os.chdir(ROOT)

# Reusar todo el módulo register_account
import register_account as ra
from register_account import (
    log, load_user, looks_garbage, RunState, load_burned, save_burned,
    load_used_ips, save_used_ip, fill_signup_form, fill_signin_form,
    email_otp, open_phone_modal, phone_relentless,
    finalize_terms_and_passkey, save_artifacts, logout_account,
    register_in_dashboard, record_in_tm_registrations,
    remove_from_plantilla, _archive_abandoned, screenshot_on_error,
    detect_bot_block, RUNS_DIR, COOKIES_DIR, SESSIONS_DIR,
)

import account_manager as am
import sms_receiver
import accounts_csv_sync
from imap_tools import MailBox

# Camoufox
from camoufox.sync_api import Camoufox


def load_proxy_pool() -> list:
    """Carga proxies ES de proxy_health.json."""
    try:
        with open(os.path.join(ROOT, 'proxy_health.json'), encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return []
    out = []
    for p in d.get('proxies', []):
        if (p.get('geo') or '').lower() != 'es': continue
        u = urlparse(p.get('url') or '')
        if not u.hostname: continue
        out.append({
            'server': f'http://{u.hostname}:{u.port}',
            'username': u.username,
            'password': u.password,
            'score': p.get('score', 0),
        })
    return sorted(out, key=lambda x: -x['score'])


def pick_proxy(used_session_ids: set) -> Optional[dict]:
    """Pick proxy ES con sessionid no usado todavía en esta corrida.
    Devuelve dict con SOLO server/username/password (Camoufox rechaza extras)."""
    pool = load_proxy_pool()
    for p in pool:
        m = re.search(r'sessionid-(\d+)-', p.get('username') or '')
        sid = m.group(1) if m else None
        if sid and sid in used_session_ids: continue
        return {'server': p['server'], 'username': p['username'], 'password': p['password']}
    if pool:
        p = random.choice(pool)
        return {'server': p['server'], 'username': p['username'], 'password': p['password']}
    return None


def register_one_camoufox(row_idx: int, override_first=None, override_last=None) -> dict:
    run_id = f'run_{datetime.now().strftime("%Y%m%d_%H%M%S")}_u{row_idx}_camoufox'
    log(f'═══ {run_id} ═══')

    user = load_user(row_idx)
    log(f'  email={user.email}  raw name={user.first!r} {user.last!r}')

    if override_first or override_last:
        user.first = override_first or user.first
        user.last = override_last or user.last
    elif looks_garbage(user.first) or looks_garbage(user.last):
        user.first, user.last = 'Julio', 'Martin Perez'
        log(f'  auto-override → {user.first} {user.last}')

    run = RunState(run_id=run_id, row_idx=row_idx, email=user.email,
                   started_at=datetime.now(timezone.utc).isoformat())
    run.persist()

    # Pre-flight
    if any((a.get('tm_email') or '').lower() == user.email.lower()
           for a in am.account_manager.list_accounts()):
        log(f'  ⏭ ya registrada — skip', level='WARN')
        run.error = 'already_registered'; run.persist(); return run

    log('preflight: IMAP madre health check')
    try:
        socket.setdefaulttimeout(15)
        with MailBox('imap.mail.me.com').login(user.contact_email, user.contact_pwd):
            pass
        log(f'  ✓ IMAP OK')
    except Exception as e:
        log(f'  ❌ IMAP err: {e}', level='ERROR')
        run.error = 'imap_unreachable'; run.persist(); return run

    # Pick proxy
    proxy = pick_proxy(set())
    if not proxy:
        log('  ❌ no hay proxies en proxy_health.json', level='ERROR')
        run.error = 'no_proxy'; run.persist(); return run
    sid_m = re.search(r'sessionid-(\d+)-', proxy.get('username') or '')
    log(f'  proxy: {proxy["server"]}  sessionid={sid_m.group(1) if sid_m else "?"}')

    # Camoufox launch
    log('camoufox_launching ES + ES locale + Win11 fingerprint')
    try:
        with Camoufox(
            headless=False,
            os='windows',
            humanize=True,
            block_webrtc=True,
            geoip=True,
            locale=['es-ES', 'es'],
            proxy=proxy,
        ) as browser:
            page = browser.new_page()
            ctx = browser.contexts[0] if browser.contexts else page.context

            # Verify IP
            try:
                page.goto('https://api.ipify.org/?format=json',
                          wait_until='domcontentloaded', timeout=20000)
                txt = page.evaluate('()=>document.body.innerText') or ''
                m = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', txt)
                ip = m.group(1) if m else '?'
                log(f'  ✓ IP detectada: {ip}')
                run.ip = ip
            except Exception as e:
                log(f'  ⚠ ip check err: {e}')

            # Goto TM signup
            url = f'https://my.ticketmaster.es/?_={int(time.time())}{random.randint(1000,9999)}'
            log(f'goto {url[:60]}')
            for i in range(3):
                try: page.goto(url, wait_until='domcontentloaded', timeout=30000); break
                except Exception as e: log(f'  retry {i+1}: {e}'); time.sleep(3)
            try: page.wait_for_load_state('networkidle', timeout=15000)
            except Exception: pass
            page.wait_for_timeout(3000)

            # Bot block check
            try:
                blk = detect_bot_block(page)
                if blk:
                    screenshot_on_error(page, run, 'bot_blocked_camoufox')
                    raise RuntimeError(f'bot_block_detected: "{blk}"')
            except RuntimeError: raise
            except Exception: pass

            # Cookies banner
            for txt in ['Aceptar todas las cookies', 'Rechazarlas todas']:
                try:
                    btn = page.get_by_text(txt, exact=False).first
                    if btn.is_visible(timeout=1500): btn.click(); page.wait_for_timeout(1500); break
                except Exception: pass

            # Email
            em = None
            for retry in range(3):
                try:
                    em = page.locator('input[type="email"]').first
                    em.wait_for(state='visible', timeout=15000); break
                except Exception:
                    page.wait_for_timeout(3000)
            if not em:
                raise RuntimeError('email input nunca apareció')
            em.click(); em.fill('')
            for ch in user.email: em.type(ch, delay=random.randint(60,140))
            log('  email tipeado')
            page.wait_for_timeout(900)

            # Click Continuar
            page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
            # Esperar a que TM termine de evaluar el email + redirigir a signup/signin
            try: page.wait_for_load_state('networkidle', timeout=20000)
            except Exception: pass
            page.wait_for_timeout(5000)
            # Reintento de detección de modo (TM tarda variable)
            mode = 'unknown'
            for retry in range(6):
                title = page.title().lower()
                body = ''
                try: body = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
                except Exception: pass
                if 'sign up' in title or 'crea una cuenta' in body or 'create your account' in body or 'crear cuenta' in body:
                    mode = 'signup'; break
                if 'sign in' in title and ('contraseña' in body or 'password' in body):
                    mode = 'signin'; break
                # Title puede ser stale "Sign In" mientras carga form de signup
                page.wait_for_timeout(2500)
            log(f'  modo={mode} title={page.title()}')

            if mode == 'signup':
                fill_signup_form(page, user)
            elif mode == 'signin':
                fill_signin_form(page, user)
            else:
                screenshot_on_error(page, run, 'unknown_mode_camoufox')
                raise RuntimeError(f'modo desconocido: {page.title()}')

            if not email_otp(page, user, run):
                raise RuntimeError('email OTP timeout')

            log('click "Añadir mi teléfono"')
            phone_inp = open_phone_modal(page)
            if not phone_inp: raise RuntimeError('no abrió phone form')

            phone_info = phone_relentless(page, user, phone_inp, run)
            if not phone_info: raise RuntimeError('phone failed tras 4 ciclos')

            finalize_terms_and_passkey(page, run)
            artifacts = save_artifacts(ctx, run)
            logout_account(page, run)

            info = register_in_dashboard(user, phone_info, run, artifacts)
            record_in_tm_registrations(user, info['account_id'], run)
            remove_from_plantilla(user.email, run)

            run.success = True
            run.final = {'phone': phone_info['phone'],
                         'account_id': info['account_id'],
                         'visible_in_dashboard': info['visible_in_dashboard']}
    except Exception as e:
        log(f'❌ ERROR: {e}', level='ERROR')
        run.error = str(e)
        if 'signin_password_input_missing' in str(e).lower() or 'tm_account_already' in str(e).lower():
            try: _archive_abandoned(user.email, run, str(e))
            except Exception: pass

    run.persist()
    log(f'═══ FIN {run_id} success={run.success} ═══')
    return run


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--row', type=int)
    p.add_argument('--rows')
    p.add_argument('--first')
    p.add_argument('--last')
    args = p.parse_args()
    runs = []
    if args.rows:
        a, b = args.rows.split('-')
        rows_to_do = list(range(int(a), int(b) + 1))
        for i, r in enumerate(rows_to_do):
            runs.append(register_one_camoufox(r, args.first, args.last))
            if i < len(rows_to_do) - 1:
                d = random.randint(60, 120)
                log(f'⏱ delay {d}s')
                time.sleep(d)
    elif args.row:
        runs.append(register_one_camoufox(args.row, args.first, args.last))
    else:
        p.error('--row o --rows requerido')

    print('\n═══ RESUMEN CAMOUFOX ═══')
    for r in runs:
        mark = '✅' if r.success else '❌'
        extra = (f'phone={r.final.get("phone")} acc={r.final.get("account_id")}'
                 if r.success else f'err={r.error}')
        print(f'  {mark} U{r.row_idx} {r.email}  {extra}')


if __name__ == '__main__':
    main()
