"""complete_account.py — Login a una cuenta TM ya creada parcialmente y completa
los pasos que TM aún reclame (email re-verify, phone, TOC, passkey).

Uso:
  python complete_account.py --acc-id e94db5f6
  python complete_account.py --email loaners_boxier7b@icloud.com
"""
from __future__ import annotations
import argparse, os, sys, time, random, re, socket, json
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

import requests
from playwright.sync_api import sync_playwright

import register_account as ra
from register_account import (
    log, RunState, load_burned, save_burned, load_used_ips,
    open_profile, fill_signin_form,
    email_otp, open_phone_modal, phone_relentless,
    finalize_terms_and_passkey, save_artifacts, logout_account,
    screenshot_on_error, detect_bot_block,
    RUNS_DIR, UserRow,
)
import account_manager as am
import accounts_csv_sync


def find_account(acc_id=None, email=None):
    accs = am.account_manager.list_accounts()
    if acc_id:
        return next((a for a in accs if a.get('id') == acc_id), None)
    if email:
        em = email.lower()
        return next((a for a in accs if (a.get('tm_email') or '').lower() == em), None)
    return None


def account_to_userrow(a: dict) -> UserRow:
    """Construye UserRow desde una cuenta existente para reusar funciones del flow."""
    phone_full = a.get('phone_full') or ((a.get('phone_prefix') or '') + (a.get('phone') or ''))
    local = phone_full.lstrip('+')
    if local.startswith('34'): local = local[2:]
    return UserRow(
        n=0, email=a.get('tm_email',''), password=a.get('tm_password',''),
        contact_email=a.get('email',''), contact_pwd=a.get('email_password',''),
        first=a.get('first_name',''), last=a.get('last_name',''),
        dob=a.get('dob',''), nationality=a.get('nationality',''),
        passport=a.get('passport',''),
        phone_prefix='+34', phone_local=local,
        address1=a.get('address1',''), city=a.get('city',''),
        state=a.get('state',''), postal=a.get('postal',''),
        country=a.get('country','Spain'),
        card_number='', card_holder='', card_month='', card_year='', card_cvv='',
        notes='',
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--acc-id')
    p.add_argument('--email')
    args = p.parse_args()
    if not (args.acc_id or args.email):
        p.error('--acc-id o --email requerido')

    a = find_account(acc_id=args.acc_id, email=args.email)
    if not a:
        log('cuenta no encontrada', level='ERROR'); return
    log(f'═══ COMPLETE {a.get("tm_email")} (acc {a.get("id")}) ═══')

    user = account_to_userrow(a)
    log(f'  email={user.email}  pwd={"*"*len(user.password)}  phone={a.get("phone_full")}')

    # Pre-flight
    burned_acc = load_burned()
    run_id = f'complete_{datetime.now().strftime("%Y%m%d_%H%M%S")}_{a.get("id")}'
    run = RunState(run_id=run_id, row_idx=0, email=user.email,
                   started_at=datetime.now(timezone.utc).isoformat())
    run.persist()

    # Abrir perfil ES nuevo
    ctx, page, prof, port = open_profile('ES', burned_acc, run)

    try:
        # Goto TM ES
        url = f'https://my.ticketmaster.es/?_={int(time.time())}{random.randint(1000,9999)}'
        log(f'goto {url[:60]}')
        for i in range(3):
            try: page.goto(url, wait_until='domcontentloaded', timeout=30000); break
            except Exception as e: log(f'  retry {i+1}: {e}'); time.sleep(3)
        try: page.wait_for_load_state('networkidle', timeout=15000)
        except Exception: pass
        page.wait_for_timeout(2500)

        # Bot block check
        try:
            blk = detect_bot_block(page)
            if blk:
                screenshot_on_error(page, run, 'bot_blocked')
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
        em = page.locator('input[type="email"]').first
        em.wait_for(state='visible', timeout=15000)
        em.click(); em.fill(''); em.fill(user.email)
        page.wait_for_timeout(800)
        log('  ✓ email tipeado')

        # Continuar
        page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
        try: page.wait_for_load_state('networkidle', timeout=15000)
        except Exception: pass
        page.wait_for_timeout(3000)

        # Detect mode
        title = page.title().lower()
        body = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
        log(f'  title="{page.title()}"')

        if 'sign up' in title or 'crear cuenta' in body:
            raise RuntimeError('TM dice signup — la cuenta no existe en TM (¿borrada?)')

        # Si modo signin → introducir password
        log('login: tipear password')
        try:
            pw_el = page.locator('input[type="password"]').first
            pw_el.wait_for(state='visible', timeout=10000)
            pw_el.click(); pw_el.fill(''); pw_el.fill(user.password)
            page.wait_for_timeout(500)
            log(f'  ✓ password tipeada')
        except Exception as e:
            raise RuntimeError(f'password input no visible: {e}')

        page.evaluate(r"""() => {const m=Array.from(document.querySelectorAll('button')).find(b=>/iniciar sesi[oó]n|sign in|^continuar$/i.test((b.innerText||'').trim()) && !b.disabled); if(m) m.click();}""")
        try: page.wait_for_load_state('networkidle', timeout=15000)
        except Exception: pass
        page.wait_for_timeout(3500)

        # Verificar email si TM lo pide
        body = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
        if 'verifica' in body or 'verify' in page.title().lower():
            log('TM pide verify email otra vez')
            if not email_otp(page, user, run):
                raise RuntimeError('email OTP completion failed')

        # Verificar phone si TM lo pide
        body = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
        if 'añadir mi tel' in body or 'verifica tu tel' in body or 'phone' in page.title().lower():
            log('TM pide verify phone — abriendo modal')
            phone_inp = open_phone_modal(page)
            if not phone_inp:
                raise RuntimeError('no abrió phone form')
            phone_info = phone_relentless(page, user, phone_inp, run)
            if not phone_info:
                raise RuntimeError('phone failed tras 3 ciclos')
            # Actualizar phone en account_manager
            am.account_manager.update_account(a['id'], {
                'phone_full': phone_info['phone'],
                'phone': phone_info['phone_digits'],
                'phone_provider': phone_info['provider'],
                'phone_order_id': phone_info['order_id'],
                'phone_sms_code': phone_info['sms_code'],
                'phone_verified_at': phone_info['verified_at'],
            })
            log(f'  ✓ account updated with phone {phone_info["phone"]}')

        # TOC + passkey
        finalize_terms_and_passkey(page, run)

        # Guardar cookies frescas
        artifacts = save_artifacts(ctx, run)
        am.account_manager.update_account(a['id'], {
            'cookies_file': artifacts['cookies_file'],
            'storage_state_file': artifacts['state_file'],
            'last_login_at': datetime.now(timezone.utc).isoformat(),
        })

        # Logout limpio
        logout_account(page, run)
        accounts_csv_sync.sync_out()

        log(f'✅ COMPLETE OK — cuenta {a.get("tm_email")} ({a["id"]}) totalmente operativa')
        run.success = True
    except Exception as e:
        log(f'❌ ERROR: {e}', level='ERROR')
        run.error = str(e)
    finally:
        try: page._b.close(); page._pw.stop()
        except Exception: pass
        try: ra.close_profile(run)
        except Exception: pass
        run.persist()
        log(f'═══ FIN {run_id} success={run.success} ═══')


if __name__ == '__main__':
    main()
