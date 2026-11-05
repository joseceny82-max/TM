"""_compra_camoufox.py v2 — Bot de compras TM con Camoufox.

Flow IDÉNTICO al de _compra_parallel.py (MoreLogin) pero con engine Camoufox:
  - UNA sola ventana visible (Firefox 135 patched a C++)
  - Proxy Quantum cc-ES con sessionid aleatorio (IP fresh por launch)
  - networkidle + retry hasta 8s para detector
  - Quick mode "Ver lo mejor disponible" + stepper qty
  - checkbox_acepto + 3 retries click_continuar
  - OAuth detect → email_typed → reset_required handler (recovery inline)
  - pwd_typed → post_login
  - Bot-block check post-OAuth + en watch_loop cada 5s
  - watch_for_checkout con URL inicial check (fix 2026-05-11)
  - handle_partners_seguro (Partners=Sí, Seguro=No)
  - handle_pre_checkout (Aceptar partners + Acepto condiciones + Ir al pago)
  - fill_card + STOP antes de "Realizar Compra"
  - Cleanup completo al cerrar (cookies/cache/localStorage)

USO:
    python _compra_camoufox.py --slot 1 --event-url URL --offer-idx 3 --qty 2 --mode quick
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

PROYECTO = Path(__file__).parent
sys.path.insert(0, str(PROYECTO))

import _compra_parallel as cp


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--slot', type=int, default=1)
    p.add_argument('--event-url', required=True)
    p.add_argument('--offer-idx', type=int, default=3)
    p.add_argument('--qty', type=int, default=2)
    p.add_argument('--mode', default='quick')
    return p.parse_args()


def _outcome_path(slot: int) -> str:
    return str(PROYECTO / f'_compra_camoufox_slot{slot}.outcome.json')


def write_outcome(slot: int, payload: dict):
    payload['slot'] = slot
    payload['ts'] = datetime.now().isoformat()
    p = _outcome_path(slot)
    try:
        with open(p + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(p + '.tmp', p)
    except Exception: pass


def log(slot, event, **kw):
    ts = datetime.now().strftime('%H:%M:%S')
    body = (' ' + json.dumps(kw, default=str, ensure_ascii=False)) if kw else ''
    print(f'[slot{slot}][{ts}] {event}{body}', flush=True)


def shoot(slot, page, label):
    try:
        outdir = PROYECTO / 'datos' / 'compra_camoufox'
        outdir.mkdir(parents=True, exist_ok=True)
        f = outdir / f'slot{slot}_{int(time.time())}_{label}.png'
        page.screenshot(path=str(f), full_page=False)
        log(slot, "shot", file=f.name)
    except Exception as e:
        log(slot, "shot_err", err=str(e)[:80])


def human_pause(a, b): time.sleep(random.uniform(a, b))


_BOT_MARKERS = [
    'su sesión se ha suspendido', 'tu sesión se ha suspendido',
    'session has been paused', 'browsing activity has been paused',
    'forma de navegar o en su red nos hizo pensar que era un bot',
]


def check_bot_block(slot, page, where) -> bool:
    """Retorna True si bot-block detectado."""
    try:
        body = page.evaluate(
            '() => (document.body && document.body.innerText || "").slice(0,1500)'
        ) or ''
        low = body.lower()
        for m in _BOT_MARKERS:
            if m in low:
                log(slot, "FATAL_bot_block_camoufox",
                    where=where, marker=m[:50])
                shoot(slot, page, f"bot_block_{where}")
                return True
    except Exception: pass
    return False


def main():
    args = parse_args()
    slot = args.slot
    write_outcome(slot, {
        "status": "started", "engine": "camoufox",
        "event_url": args.event_url[:120],
        "qty": args.qty, "offer_idx": args.offer_idx,
        "started_at": datetime.now().isoformat(),
    })
    log(slot, "init",
        event_url=args.event_url[:80], qty=args.qty, engine="camoufox")

    # ─── 1. Pick cuenta TIER 1 ───
    used_lock_path = str(PROYECTO / "_compra_camoufox_used_accounts.lock")
    used_ids = set()
    try:
        if os.path.exists(used_lock_path):
            with open(used_lock_path, encoding='utf-8') as f:
                used_ids = set(json.load(f))
    except Exception: pass
    account, card = cp.pick_account(slot, used_ids)
    if not account:
        log(slot, "FATAL_no_account")
        write_outcome(slot, {"status": "no_account", "rc": 1})
        return 1
    used_ids.add(account.get('id'))
    try:
        with open(used_lock_path, 'w', encoding='utf-8') as f:
            json.dump(list(used_ids), f)
    except Exception: pass
    log(slot, "account_picked",
        email=account.get('tm_email'), has_card=bool(card))

    if not card:
        try:
            import account_manager as am
            cards = am.account_manager.list_cards()
            card = cards[0] if cards else {}
        except Exception: pass

    # ─── 2. Camoufox config: VISIBLE + Quantum cc-ES random sessionid ───
    sessid = random.randint(100000, 999999)
    quantum_user = f"Quantum_927evhgx-cc-ES-pool-qntm-sessionid-{sessid}-sesstime-60"
    quantum_pass = "ycp4e57m"
    proxy_cfg = {
        "server": "http://residential.quantumproxies.net:8888",
        "username": quantum_user,
        "password": quantum_pass,
    }

    try:
        from camoufox.sync_api import Camoufox
    except ImportError as e:
        log(slot, "FATAL_camoufox_import", err=str(e))
        write_outcome(slot, {"status": "camoufox_missing", "rc": 1})
        return 1

    cf_kwargs = {
        "headless": False,           # VISIBLE — norma user
        "geoip": False,
        "locale": "es-ES",
        "humanize": True,
        "i_know_what_im_doing": True,
        "proxy": proxy_cfg,
        "os": "windows",
        "exclude_addons": [],        # sin uBlock/extras
    }
    log(slot, "camoufox_launching",
        sessionid=sessid, proxy_host="residential.quantumproxies.net:8888")

    rc_final = 1
    try:
        with Camoufox(**cf_kwargs) as browser:
            # USAR la página inicial del browser (UNA sola ventana)
            # No crear new_context() — eso abre ventana extra
            if not browser.contexts:
                ctx = browser.new_context()
            else:
                ctx = browser.contexts[0]
            if not ctx.pages:
                page = ctx.new_page()
            else:
                page = ctx.pages[0]

            log(slot, "browser_ready",
                ua=page.evaluate("() => navigator.userAgent")[:80])

            # ─── 3. Goto evento + cookies ───
            log(slot, "goto", url=args.event_url[:80])
            page.goto(args.event_url, wait_until="domcontentloaded",
                      timeout=60000)
            human_pause(5, 8)

            # Accept cookies
            for txt in ["Aceptar todas las cookies", "Aceptar", "Accept"]:
                try:
                    el = page.get_by_text(txt, exact=False).first
                    if el.is_visible(timeout=2000):
                        el.click(); human_pause(1, 2); break
                except Exception: continue
            shoot(slot, page, "event_loaded")

            # ─── 4. networkidle + page_state detector con retry ───
            try:
                page.wait_for_load_state('networkidle', timeout=10000)
            except Exception: pass

            def detect_state():
                return page.evaluate(r"""() => {
                    const txt = (document.body && document.body.innerText || '').toLowerCase();
                    const noTickets = /entradas\s+no\s+est[aá]n\s+disponibles|no\s+tickets\s+available/i.test(txt);
                    const all = Array.from(document.querySelectorAll(
                        'button, a, [role="button"], [role="tab"]'));
                    const bestBtn = all.find(x => /ver\s+lo\s+mejor\s+disponible|best\s+available/i.test(
                        x.innerText || x.textContent || ''));
                    const stepperReady = !!document.querySelector('[data-testid="quantityStepper"]');
                    return {no_tickets: noTickets, has_best_available: !!bestBtn,
                            stepper_ready: stepperReady, body_len: txt.length};
                }""")

            page_state = detect_state()
            tries = 0
            while (not page_state.get("no_tickets")
                   and not page_state.get("has_best_available")
                   and not page_state.get("stepper_ready")
                   and tries < 10):
                page.wait_for_timeout(1500)
                try:
                    page_state = detect_state()
                except Exception: pass
                tries += 1
            log(slot, "page_state_detect", retries=tries, **page_state)

            if check_bot_block(slot, page, "post_event_load"):
                write_outcome(slot, {"status": "bot_block",
                                     "step_reached": "event_load", "rc": 99,
                                     "engine": "camoufox"})
                return 99

            if page_state.get("no_tickets"):
                log(slot, "FATAL_no_tickets")
                write_outcome(slot, {"status": "no_tickets", "rc": 1})
                return 1

            # ─── 5. Quick mode + stepper + find_tickets ───
            if args.mode == "quick" and page_state.get("has_best_available"):
                log(slot, "quick_mode_via_best_available")
                page.evaluate(r"""() => {
                    const all = Array.from(document.querySelectorAll(
                        'button, a, [role="button"], [role="tab"]'));
                    const b = all.find(x => /ver\s+lo\s+mejor\s+disponible|best\s+available/i.test(x.innerText||''));
                    if (b) b.click();
                }""")
                human_pause(3, 5)

                log(slot, "stepper", target_qty=args.qty,
                    offer_idx=args.offer_idx)
                for i in range(args.qty + 4):
                    cur = page.evaluate(f"""() => {{
                        const s = document.querySelectorAll(
                            '[data-testid="quantityStepper"]')[{args.offer_idx}];
                        return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
                    }}""")
                    if cur >= args.qty: break
                    page.evaluate(f"""() => {{
                        const s = document.querySelectorAll(
                            '[data-testid="quantityStepper"]')[{args.offer_idx}];
                        if (!s) return;
                        const btns = s.querySelectorAll('button');
                        (btns[btns.length-1] || btns[0]).click();
                    }}""")
                    human_pause(0.4, 0.8)
                shoot(slot, page, "stepper_done")

                log(slot, "click_find_tickets")
                try:
                    btn = page.locator(
                        'button[data-testid="findTicketsBtn"]').first
                    btn.scroll_into_view_if_needed(timeout=4000)
                    human_pause(0.6, 1.2)
                    btn.click(timeout=10000, force=True)
                except Exception as e:
                    log(slot, "find_tickets_err", err=str(e)[:120])
                human_pause(4, 6)
                shoot(slot, page, "after_find_tickets")

            # ─── 6. Acepto + Continuar ───
            try:
                cb = page.locator('input[type="checkbox"]').first
                if cb.is_visible(timeout=2000):
                    cb.click(force=True); log(slot, "checkbox_acepto")
                    human_pause(0.5, 1)
            except Exception: pass

            for ctry in range(3):
                try:
                    cont = page.get_by_role('button',
                        name=re.compile(r'continuar|continue', re.I)).first
                    cont.scroll_into_view_if_needed(timeout=3000)
                    cont.click(timeout=5000, force=True)
                    log(slot, "continuar_clicked", attempt=ctry + 1)
                    break
                except Exception: continue
            human_pause(5, 7)
            log(slot, "post_continuar_url", url=page.url[:100])

            # Check bot-block post-continuar
            if check_bot_block(slot, page, "post_continuar"):
                write_outcome(slot, {"status": "bot_block",
                                     "step_reached": "post_continuar",
                                     "rc": 99, "engine": "camoufox"})
                return 99

            # ─── 7. OAuth login + recovery automático ───
            u = page.url.lower()
            if any(k in u for k in ['auth.ticketmaster', 'identity',
                                       'login', 'signin']):
                log(slot, "oauth_detected")
                try:
                    em_input = page.locator('input[type="email"]').first
                    em_input.wait_for(timeout=20000)
                    em_input.fill(account['tm_email'])
                    log(slot, "email_typed")
                    human_pause(1.5, 2.5)
                except Exception as e:
                    log(slot, "email_err", err=str(e)[:120])

                for txt in ["Continuar", "Continue", "Siguiente", "Next"]:
                    try:
                        b = page.get_by_role('button',
                            name=re.compile(f'^{txt}$', re.I)).first
                        if b.is_visible(timeout=1500):
                            b.click(timeout=3000); break
                    except Exception: continue
                human_pause(3, 5)

                # Detectar reset forzado
                body_now = (page.evaluate(
                    '() => (document.body.innerText||"").slice(0,1500)'
                ) or '').lower()
                reset_markers = [
                    'restablecer la contraseña', 'restablece tu contraseña',
                    'mantener la seguridad de tu cuenta',
                    'pedimos que restablezcas tu contraseña',
                    'reset your password',
                ]
                if any(m in body_now for m in reset_markers):
                    log(slot, "tm_forced_password_reset_detected")
                    shoot(slot, page, "tm_forced_reset")
                    write_outcome(slot, {"status": "reset_required",
                                         "step_reached": "oauth_login",
                                         "rc": 1, "engine": "camoufox"})
                    # En MoreLogin teníamos recovery_inline; aquí dejamos para
                    # próxima iteración (requiere session HTTP separada)
                    return 1

                # Password
                try:
                    pw_input = page.locator(
                        'input[type="password"]').first
                    pw_input.wait_for(timeout=15000)
                    pw_input.fill(account['tm_password'])
                    log(slot, "pwd_typed")
                    human_pause(1.5, 3)
                except Exception as e:
                    log(slot, "pwd_err", err=str(e)[:120])
                    write_outcome(slot, {"status": "no_pwd_field",
                                         "rc": 1, "engine": "camoufox"})
                    return 1

                for txt in ["Iniciar sesión", "Sign In", "Login", "Continuar"]:
                    try:
                        b = page.get_by_role('button',
                            name=re.compile(f'^{txt}$', re.I)).first
                        if b.is_visible(timeout=1500):
                            b.click(timeout=3000); break
                    except Exception: continue
                page.wait_for_timeout(8000)
                shoot(slot, page, "post_login")
                log(slot, "post_login_url", url=page.url[:100])

                if check_bot_block(slot, page, "post_login"):
                    write_outcome(slot, {"status": "bot_block",
                                         "step_reached": "post_login",
                                         "rc": 99, "engine": "camoufox"})
                    return 99

            # ─── 8. handle_pre_checkout (Partners/Condiciones/Ir al pago) ───
            for _ in range(5):
                try:
                    if cp.handle_pre_checkout_step(slot, page):
                        break
                except Exception: pass
                page.wait_for_timeout(2500)

            # ─── 9. Watch for checkout con URL inicial check ───
            log(slot, "watch_for_checkout")

            # Helper: detección de checkout REAL — solo path/host, NO query string.
            # Bug fix 2026-05-12: TM OAuth incluye `?headerTitle=Checkout` que
            # disparaba falsos positivos en login screen.
            def _is_checkout_url(url: str) -> bool:
                try:
                    from urllib.parse import urlparse
                    p = urlparse(url or '')
                    host = (p.hostname or '').lower()
                    path = (p.path or '').lower()
                    # NO en host de auth/login (incluso si query trae Checkout)
                    if any(h in host for h in ('auth.ticketmaster',
                                                  'identity.ticketmaster',
                                                  'login.ticketmaster')):
                        return False
                    # SÍ en host payments o path /checkout/, /payment/, /cart/, /pago
                    if 'payments.tick' in host: return True
                    if any(seg in path for seg in ('/checkout/', '/checkout',
                                                      '/payment', '/cart',
                                                      '/pago')):
                        return True
                except Exception: pass
                return False

            # Check URL INICIAL
            u0 = page.url
            log(slot, "watch_initial_url", url=u0[:100])
            filled = False
            if _is_checkout_url(u0):
                log(slot, "checkout_detected_at_entry")
                # 6 etapas: Partners/Seguro + Guardar y continuar + Ir al pago
                for stage in range(6):
                    page.wait_for_timeout(2000)
                    try:
                        cp.handle_partners_seguro_step(slot, page)
                    except Exception: pass
                    for pat in [r'^guardar y continuar$',
                                r'^continuar$', r'^ir al pago$']:
                        try:
                            b = page.get_by_role('button',
                                name=re.compile(pat, re.I)).first
                            if b.is_visible(timeout=900):
                                b.scroll_into_view_if_needed(timeout=1500)
                                b.click(timeout=2500, force=True)
                                log(slot, "checkout_stage_btn",
                                    pattern=pat, stage=stage)
                                page.wait_for_timeout(2500)
                                break
                        except Exception: continue
                    if 'payments.tick' in (page.url or '').lower():
                        log(slot, "payments_screen_reached")
                        break

                # fill_card
                try:
                    cp.fill_card(slot, page, card, account)
                    log(slot, "fill_card_done")
                    shoot(slot, page, "card_filled")
                    filled = True
                    write_outcome(slot, {"status": "ready_for_user",
                                         "step_reached": "fill_card",
                                         "url": page.url, "rc": 0,
                                         "engine": "camoufox",
                                         "note": "STOP before Pagar"})
                    rc_final = 0
                except Exception as e:
                    log(slot, "fill_card_err", err=str(e)[:200])

            # Watch loop tradicional
            if not filled:
                deadline = time.time() + 300
                prev = page.url
                _bb_checks = 0
                while time.time() < deadline and not filled:
                    time.sleep(2)
                    _bb_checks += 1
                    if _bb_checks % 3 == 0:
                        if check_bot_block(slot, page, "watch_loop"):
                            write_outcome(slot, {"status": "bot_block",
                                                 "step_reached": "watch_loop",
                                                 "rc": 99,
                                                 "engine": "camoufox"})
                            return 99
                    try:
                        cur = page.url
                        if cur != prev:
                            log(slot, "URL_CHANGED", to=cur[:100])
                            prev = cur
                            page.wait_for_timeout(3000)
                            try:
                                cp.handle_pre_checkout_step(slot, page)
                                cp.handle_partners_seguro_step(slot, page)
                            except Exception: pass
                            if _is_checkout_url(cur):
                                log(slot, "checkout_detected")
                                cp.fill_card(slot, page, card, account)
                                log(slot, "fill_card_done")
                                shoot(slot, page, "card_filled")
                                filled = True
                                write_outcome(slot, {"status": "ready_for_user",
                                                     "step_reached": "fill_card",
                                                     "url": page.url,
                                                     "rc": 0,
                                                     "engine": "camoufox"})
                                rc_final = 0
                    except Exception as we:
                        log(slot, "watch_err", err=str(we)[:80])

            if not filled:
                log(slot, "TIMEOUT_no_checkout")
                shoot(slot, page, "timeout")
                write_outcome(slot, {"status": "timeout_no_checkout",
                                     "rc": 1, "engine": "camoufox"})

            # ─── 10. Pausa final hasta STOP signal ───
            if filled:
                print("\n" + "=" * 70)
                print(f"✅ SLOT {slot}: Camoufox llegó a fill_card.")
                print(f"   URL: {page.url}")
                print(f"   Cerrar: touch _compra_camoufox.STOP")
                print("=" * 70 + "\n")
                stop_flag = PROYECTO / "_compra_camoufox.STOP"
                wait_deadline = time.time() + 600
                while not stop_flag.exists() and time.time() < wait_deadline:
                    time.sleep(5)
                try: stop_flag.unlink()
                except Exception: pass
                log(slot, "stop_received")

            # Cleanup browser cookies/cache antes de cerrar
            try:
                import browser_cleanup
                cleanup_res = browser_cleanup.clear_history_via_cdp(ctx)
                log(slot, "history_cleanup",
                    steps=cleanup_res.get('steps', []))
            except Exception as e:
                log(slot, "cleanup_err", err=str(e)[:80])

    except Exception as e:
        log(slot, "FATAL_camoufox_exception",
            err=f"{type(e).__name__}: {str(e)[:200]}")
        import traceback; traceback.print_exc()
        write_outcome(slot, {"status": "fatal_exception",
                             "error": f"{type(e).__name__}: {str(e)[:200]}",
                             "rc": 1, "engine": "camoufox"})
        rc_final = 1
    finally:
        # Liberar lock
        try:
            with open(used_lock_path, encoding='utf-8') as f:
                ids = set(json.load(f))
            ids.discard(account.get('id'))
            with open(used_lock_path, 'w', encoding='utf-8') as f:
                json.dump(list(ids), f)
        except Exception: pass

    return rc_final


if __name__ == "__main__":
    sys.exit(main())
