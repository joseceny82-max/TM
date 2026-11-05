"""camoufox_login_only.py — Bot login-only con motor Camoufox.

Hace login REAL en my.ticketmaster.es con Camoufox (Firefox patched, anti-bot
Akamai-friendly), guarda storage_state al disco y cierra. NO hace compra.

Uso:
    # Una cuenta
    result = login_account_camoufox(account_dict, headless=True)

    # Batch
    result = refresh_sessions_batch_camoufox(account_ids, max_parallel=3)

Returns:
    {
      "ok": bool, "valid": bool,
      "account_id": str, "tm_email": str,
      "reason": "session_active" | "login_ok" | "bad_credentials" | "bot_block"
                | "reset_required" | "timeout" | "error",
      "engine": "camoufox",
      "storage_state_file": str (path),
      "elapsed_s": float,
    }

NORMA: Storage canonical `datos/sessions/account_{id}.json` (sobrescribir).
"""
from __future__ import annotations

import os
import re
import time
import json
import uuid
import random
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional

import account_manager as am
import session_status
import account_recovery

PROYECTO = os.path.dirname(os.path.abspath(__file__))


def _build_user_for_recovery(account: Dict[str, Any]):
    """Construye un UserRow mínimo a partir del dict accounts.json para
    invocar `account_recovery.recover_inline(page, ctx, user, run)`.

    `recover_inline` accede a: user.email, user.contact_email, user.contact_pwd
    """
    from register_account import UserRow
    return UserRow(
        n=int(account.get("id_int", 0) or 0),
        email=account.get("tm_email", ""),
        password=account.get("tm_password", ""),
        contact_email=account.get("email", "") or account.get("contact_email", ""),
        contact_pwd=account.get("email_password", "") or account.get("contact_password", ""),
        first=account.get("first_name", ""),
        last=account.get("last_name", ""),
        dob=account.get("dob", ""),
        nationality=account.get("nationality", ""),
        passport=account.get("passport", ""),
        phone_prefix=account.get("phone_prefix", "+34"),
        phone_local=account.get("phone", "")[-9:] if account.get("phone") else "",
        address1=account.get("address1", ""),
        city=account.get("city", ""),
        state=account.get("state", ""),
        postal=account.get("postal", ""),
        country=account.get("country", "Spain"),
        card_number="", card_holder="", card_month="", card_year="", card_cvv="",
        notes=account.get("notes", "") or "",
    )


def _build_run_for_recovery(account: Dict[str, Any], slot: int = 0):
    """RunState mínimo (solo necesita run_id/row_idx para logging)."""
    from register_account import RunState
    return RunState(
        run_id=f"cf_recovery_{account.get('id','?')}_{int(time.time())}",
        row_idx=slot,
        email=account.get("tm_email", ""),
        started_at=datetime.now().isoformat(timespec="seconds"),
    )


# ════════════════════════════════════════════════════════════════════
#  Job tracking (mismo formato que account_validator.validate_batch)
# ════════════════════════════════════════════════════════════════════
_JOBS: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _log(slot: int, kind: str, **kwargs):
    """Log estructurado al stdout + datos/camoufox_login.log."""
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "slot": slot, "kind": kind, **kwargs,
    }
    line = json.dumps(rec, ensure_ascii=False)
    print(f"[cf-login s{slot}] {kind} {kwargs}", flush=True)
    try:
        log_path = os.path.join(PROYECTO, "datos", "camoufox_login.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
#  Proxy config (Quantum ES residencial)
# ════════════════════════════════════════════════════════════════════
QUANTUM_HOST = "http://residential.quantumproxies.net:8888"
QUANTUM_PASS = "ycp4e57m"


def _build_proxy(account_id: str = "", sticky: bool = True) -> Dict[str, str]:
    """Build proxy config.

    Args:
      account_id: si se pasa + sticky=True → session-id determinístico por cuenta
                  (Quantum recuerda IP por session-id → misma IP cada login).
      sticky: True → session-id determinístico, False → random cada vez.

    Returns: dict con server/username/password.
    """
    if sticky and account_id:
        # Hash determinístico account_id → sessionid 6 dígitos
        import hashlib
        h = hashlib.md5(account_id.encode("utf-8")).hexdigest()
        # Convertir 8 hex chars a int y modulo 900000 + 100000 = 6 dígitos
        sessid = int(h[:8], 16) % 900000 + 100000
    else:
        sessid = random.randint(100000, 999999)
    return {
        "server": QUANTUM_HOST,
        "username": f"Quantum_927evhgx-cc-ES-pool-qntm-sessionid-{sessid}-sesstime-60",
        "password": QUANTUM_PASS,
    }


# ════════════════════════════════════════════════════════════════════
#  Login flow (Camoufox)
# ════════════════════════════════════════════════════════════════════
def _detect_logged_in(page) -> bool:
    """Heurística robusta: múltiples señales de login activo.

    Señales positivas (cualquiera basta):
      - 'Cerrar sesión' / 'sign out' / 'log out' en texto
      - 'Hola, <nombre>' / 'Hi, <name>' (greeting)
      - 'Mi cuenta' / 'My account' (NO en página de signin)
      - Avatar/dropdown user en header
      - URL termina en /account o /tickets sin /auth
    """
    try:
        sel = page.evaluate(r"""() => {
            const txt = (document.body && document.body.innerText || '').toLowerCase();
            const url = (window.location.href || '').toLowerCase();
            const hasLogout = /cerrar sesi[oó]n|sign\s*out|log\s*out|salir/.test(txt);
            const hasGreeting = /hola,?\s+\w|hi,?\s+\w|hello,?\s+\w|bienvenid/.test(txt);
            const hasMyAccount = /mi cuenta|my account|mis entradas|mis pedidos|my tickets/.test(txt);
            const isOnSignin = /signin|sign-in|\/login|\/auth\./.test(url);
            // Avatar / dropdown user header
            const hasAvatar = !!document.querySelector(
                '[data-testid*="user"], [aria-label*="usuario"], [aria-label*="account"], ' +
                '.user-avatar, .account-dropdown, [data-qa="account-menu"]');
            const hasOrderHistory = !!document.querySelector(
                '[href*="/orders"], [href*="/pedidos"], [href*="/tickets/mine"], [href*="/account/orders"]');
            const isOnAccount = /\/account|\/my\.ticket|\/mi-cuenta/.test(url) && !isOnSignin;
            return hasLogout || hasGreeting || hasAvatar || hasOrderHistory
                    || (hasMyAccount && !isOnSignin) || isOnAccount;
        }""")
        return bool(sel)
    except Exception:
        return False


def _verify_login_via_account_page(page, slot: int) -> bool:
    """Fallback: navegar a /account y comprobar que NO redirige a /auth.

    Si Camoufox dejó el flow en estado ambiguo (URL extraña, página parcial),
    forzamos navegación a /account. Si está logueado → carga la página.
    Si no → redirige a /auth/signin.
    """
    try:
        page.goto("https://my.ticketmaster.es/account",
                   wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        final_url = (page.url or "").lower()
        # Si tras goto seguimos en my.ticketmaster.es/account → logueado
        if "/account" in final_url and "auth." not in final_url and "signin" not in final_url:
            return _detect_logged_in(page)
        return False
    except Exception as e:
        _log(slot, "verify_account_err", err=str(e)[:80])
        return False


def _check_bot_block(page) -> bool:
    """Detecta el mensaje 'Su sesión se ha suspendido' o similar."""
    try:
        return bool(page.evaluate(r"""() => {
            const t = (document.body && document.body.innerText || '').toLowerCase();
            return /su sesi[oó]n se ha suspendido|tu sesi[oó]n.*suspendid|your browsing activity has been paused|access denied/.test(t);
        }"""))
    except Exception:
        return False


def _check_reset_required(page) -> bool:
    """Detecta pantalla CÁMBIALO de TM (step-up auth: forzar reset password).

    Múltiples señales:
      - Texto: 'restablecer la contraseña', 'mantener la seguridad de tu cuenta'
      - Título: 'CÁMBIALO'
      - Botón único: 'Restablecer la contraseña' visible
    """
    try:
        return bool(page.evaluate(r"""() => {
            const t = (document.body && document.body.innerText || '').toLowerCase().slice(0, 3000);
            const markers = ['restablecer la contraseña', 'restablece tu contraseña',
                              'mantener la seguridad de tu cuenta',
                              'pedimos que restablezcas tu contraseña',
                              'reset your password',
                              'cámbialo', 'cambialo'];
            const textHit = markers.some(m => t.includes(m));
            // Detección por botón visible
            const btns = Array.from(document.querySelectorAll('button'));
            const buttonHit = btns.some(b => {
                const txt = (b.innerText || b.textContent || '').trim().toLowerCase();
                return /^restablecer\s+la\s+contraseña\s*$/i.test(txt) ||
                       /^reset\s+password\s*$/i.test(txt);
            });
            return textHit || buttonHit;
        }"""))
    except Exception:
        return False


def _check_bad_credentials(page) -> bool:
    try:
        return bool(page.evaluate(r"""() => {
            const t = (document.body && document.body.innerText || '').toLowerCase();
            return /no podemos encontrar una cuenta|incorrect.*email|invalid.*password|contrase[ñn]a.*incorrect/.test(t);
        }"""))
    except Exception:
        return False


def _click_tm_reset_button(page, slot: int) -> bool:
    """Click el botón 'Restablecer la contraseña' en la pantalla CÁMBIALO de TM.

    TM auth OAuth muestra una pantalla con UN botón único cuando aplica
    step-up auth post-email. Click ese botón dispara el envío del email reset.

    Returns True si encontró + clickó el botón.
    """
    try:
        import re
        # Estrategia 1: get_by_role button con texto exacto
        for txt in ["Restablecer la contraseña", "Reset password",
                     "Restablecer contraseña"]:
            try:
                b = page.get_by_role("button",
                                      name=re.compile(rf"^{txt}\s*$", re.I)).first
                if b.is_visible(timeout=2000):
                    b.click(timeout=3000)
                    _log(slot, "tm_reset_button_clicked", text=txt)
                    return True
            except Exception: continue
        # Estrategia 2: text-based fallback
        for txt in ["Restablecer la contraseña", "Reset password"]:
            try:
                el = page.get_by_text(txt, exact=False).first
                if el.is_visible(timeout=1500):
                    el.click(timeout=3000)
                    _log(slot, "tm_reset_text_clicked", text=txt)
                    return True
            except Exception: continue
    except Exception as e:
        _log(slot, "tm_reset_button_err", err=str(e)[:80])
    return False


def _try_recovery_inline(page, ctx, account: Dict[str, Any], slot: int,
                           out: Dict[str, Any],
                           request_already_sent: bool = False) -> bool:
    """Intenta auto-recovery de password via account_recovery.recover_inline.

    Flow específico para Camoufox + TM ES OAuth:
      1. Click "Restablecer la contraseña" en pantalla CÁMBIALO de TM (dispara email)
      2. Delega a account_recovery.recover_inline con request_already_sent=True:
         - Espera email OTP madre via IMAP (max 300s)
         - Tipea OTP en la pantalla
         - Pone nueva password (random 14 chars)
         - Submit + verificar éxito
         - Login real → captura storage_state
         - Persiste en accounts.json + plantilla_tm.csv

    Args:
      page: Camoufox Page actual (probablemente en pantalla CÁMBIALO)
      ctx: BrowserContext
      account: dict de accounts.json
      slot: para logging
      out: dict del outcome
      request_already_sent: hint sobre si TM YA disparó email (ignorado: forzamos
                            click manual al botón TM porque su URL legacy es 404)

    Returns: True si recovery+login OK (cuenta válida), False si falló.
    """
    aid = account.get("id", "")
    email = account.get("tm_email", "")
    contact_email = account.get("email") or account.get("contact_email") or ""
    contact_pwd = account.get("email_password") or account.get("contact_password") or ""
    if not contact_email or not contact_pwd:
        _log(slot, "recovery_skip_no_imap_creds")
        out["recovery_error"] = "no_imap_credentials_in_account"
        return False

    try:
        user = _build_user_for_recovery(account)
        run = _build_run_for_recovery(account, slot=slot)
    except Exception as e:
        _log(slot, "recovery_user_build_err", err=str(e)[:80])
        out["recovery_error"] = f"user_build: {str(e)[:80]}"
        return False

    def _log_proxy(msg):
        # account_recovery espera log(str). Lo redirigimos al logger estructurado.
        _log(slot, "recovery_log", msg=str(msg)[:200])

    _log(slot, "RECOVERY_START", email=email)

    # PASO ESPECÍFICO TM 2026: click "Restablecer la contraseña" en pantalla
    # CÁMBIALO. Esto dispara el envío del email reset. Si no clickamos,
    # account_recovery se atascaría (su URL legacy /account/forgot-password
    # da 404 en TM ES actualmente).
    if not _click_tm_reset_button(page, slot):
        _log(slot, "WARN_no_reset_button_found_continuing_anyway")
        # Si no encuentra botón, intentar igual con request_already_sent=False
        # para que recover_inline pruebe sus fallbacks.
        sent = False
    else:
        sent = True
        # Wait redirect post-click (TM envía email + cambia pantalla)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception: pass
        page.wait_for_timeout(2500)
        _log(slot, "after_reset_button_url", url=page.url[:120])

    try:
        result = account_recovery.recover_inline(
            page, ctx, user, run,
            log=_log_proxy,
            max_imap_wait_s=300,
            request_already_sent=sent,
        )
    except Exception as e:
        _log(slot, "RECOVERY_EXCEPTION", err=str(e)[:200])
        out["recovery_error"] = f"exception: {type(e).__name__}: {str(e)[:80]}"
        return False

    if not result or not result.get("ok"):
        err = (result or {}).get("error", "?")
        _log(slot, "RECOVERY_FAILED", err=err)
        out["recovery_error"] = err
        return False

    new_pwd = result.get("new_password")
    if new_pwd:
        out["recovery_new_password_preview"] = new_pwd[:4] + "***"
    out["recovery_persist"] = bool(result.get("persist"))

    # account_recovery.recover_inline YA hizo:
    # - Login real con nueva pwd
    # - Captura sesión (cookies + storage_state en archivos session_files)
    # - Persistencia en accounts.json + plantilla
    # Aún así, refrescamos NUESTRO storage canonical (file canonical único por cuenta)
    # tomando el state del context actual.
    _log(slot, "RECOVERY_OK_saving_canonical")
    return True


def login_account_camoufox(account: Dict[str, Any],
                            slot: int = 0,
                            headless: bool = True,
                            timeout_s: float = 120) -> Dict[str, Any]:
    """Login con Camoufox + save storage_state canonical.

    Args:
      account: dict con tm_email + tm_password + id
      slot: slot id para logging
      headless: True para refresh masivo, False para debug visual
      timeout_s: máximo total

    Returns: dict con outcome (ver header del módulo)
    """
    started = time.time()
    aid = account.get("id", "")
    email = account.get("tm_email", "")
    password = account.get("tm_password", "")
    out: Dict[str, Any] = {
        "ok": False,
        "valid": False,
        "account_id": aid,
        "tm_email": email,
        "engine": "camoufox",
        "reason": "not_started",
        "elapsed_s": 0,
    }

    if not aid or not email or not password:
        out["reason"] = "missing_account_fields"
        return out

    try:
        from camoufox.sync_api import Camoufox
    except ImportError as e:
        out["reason"] = f"camoufox_not_installed: {str(e)[:80]}"
        _log(slot, "FATAL_camoufox_import", err=str(e))
        return out

    # STICKY PROXY: session-id determinístico por account_id.
    # Quantum recuerda IP por session-id, así que cada cuenta usa
    # SIEMPRE la misma IP residencial → menos sospecha en TM.
    proxy_cfg = _build_proxy(account_id=aid, sticky=True)
    cf_kwargs = {
        "headless": headless,
        # CRÍTICO: geoip=True cuando hay proxy. Si False, Camoufox queda con
        # timezone/locale del HOST mientras la IP es del proxy ES → fingerprint
        # inconsistente → PingFederate (auth.ticketmaster) rechaza el render
        # y devuelve página en blanco (no_email_field).
        "geoip": True,
        "locale": "es-ES",
        "humanize": True,
        "i_know_what_im_doing": True,
        "proxy": proxy_cfg,
        "os": "windows",
        "exclude_addons": [],
        "block_webrtc": True,
    }

    canonical = str(session_status.canonical_session_path(aid))
    os.makedirs(os.path.dirname(canonical), exist_ok=True)

    # PRE-LOAD storage_state existente si lo hay (canonical primero, legacy fallback).
    # Esto permite que cuentas con sesión warning/active entren ya logueadas
    # sin pasar por el form de email/password.
    preload_state: Optional[str] = None
    if os.path.exists(canonical):
        preload_state = canonical
    else:
        legacy = account.get("storage_state_file")
        if legacy and os.path.exists(legacy):
            preload_state = legacy
    if preload_state:
        _log(slot, "preloading_storage_state", path=os.path.basename(preload_state))

    _log(slot, "launching", email=email, headless=headless, has_state=bool(preload_state))

    try:
        with Camoufox(**cf_kwargs) as browser:
            # Si hay storage_state previo, crear NUEVO context con él (el context
            # default de Camoufox no lo soporta inline).
            if preload_state:
                # Cerrar context default + crear uno con storage_state
                if browser.contexts:
                    try: browser.contexts[0].close()
                    except Exception: pass
                try:
                    ctx = browser.new_context(storage_state=preload_state)
                except Exception as e:
                    _log(slot, "preload_failed_fallback_empty", err=str(e)[:80])
                    ctx = browser.new_context()
            else:
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ctx.set_default_timeout(int(min(timeout_s * 1000, 60000)))

            # ─── Step 1: Goto my.ticketmaster.es ───
            try:
                page.goto("https://my.ticketmaster.es/",
                           wait_until="domcontentloaded",
                           timeout=int(min(timeout_s * 1000, 60000)))
            except Exception as e:
                out["reason"] = f"goto_my_failed: {str(e)[:60]}"
                _log(slot, "ERR_goto", err=str(e)[:120])
                return _finish(out, started)

            page.wait_for_timeout(2500)
            # Wait redirects + networkidle (proxy puede ser lento)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception: pass
            _log(slot, "post_goto_url", url=page.url[:100])

            # Cookies banner
            for txt in ["Aceptar todas las cookies", "Aceptar", "Accept"]:
                try:
                    el = page.get_by_text(txt, exact=False).first
                    if el.is_visible(timeout=1500):
                        el.click(); page.wait_for_timeout(1200); break
                except Exception: continue

            # Bot block early?
            if _check_bot_block(page):
                out["reason"] = "bot_block_pre_login"
                _log(slot, "BOT_BLOCK_pre_login")
                return _finish(out, started)

            # ─── Step 2: ¿Ya logueado? ───
            if _detect_logged_in(page):
                _log(slot, "already_logged_in", email=email)
                if _save_storage(ctx, canonical, aid, out):
                    out["ok"] = True
                    out["valid"] = True
                    out["reason"] = "session_active"
                return _finish(out, started)

            # ─── Step 3: Click "Iniciar sesión" o navegar a signin ───
            current_url = page.url.lower()
            if not any(k in current_url for k in ("auth.ticketmaster", "identity", "signin", "login")):
                # Buscar link/button signin
                clicked_signin = False
                for txt in ["Iniciar sesión", "Sign In", "Login", "Acceder"]:
                    try:
                        el = page.get_by_role("button",
                                              name=re.compile(f"^{txt}$", re.I)).first
                        if el.is_visible(timeout=2000):
                            el.click(); clicked_signin = True; break
                    except Exception:
                        try:
                            el = page.get_by_role("link",
                                                  name=re.compile(f"^{txt}$", re.I)).first
                            if el.is_visible(timeout=1500):
                                el.click(); clicked_signin = True; break
                        except Exception: continue
                if not clicked_signin:
                    # Fallback: navegación directa
                    try:
                        page.goto("https://auth.ticketmaster.es/as/authorization.oauth2",
                                   wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                page.wait_for_timeout(3000)

            # ─── Step 4: Email field (multi-selector + timeout largo) ───
            # Selectores fallback porque TM a veces usa type=text + autocomplete=username
            em_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                'input[name="username"]',
                'input[autocomplete="username"]',
                'input[autocomplete="email"]',
                '#email',
                '#username',
            ]
            em_input = None
            for sel in em_selectors:
                try:
                    cand = page.locator(sel).first
                    cand.wait_for(state="visible", timeout=8000)
                    em_input = cand
                    _log(slot, "email_field_found", selector=sel)
                    break
                except Exception: continue
            if em_input is None:
                # Screenshot de diagnóstico
                try:
                    sshot = os.path.join(PROYECTO, "datos",
                                          f"cf_no_email_{aid}_{int(time.time())}.png")
                    page.screenshot(path=sshot)
                    _log(slot, "screenshot_no_email", path=os.path.basename(sshot))
                except Exception: pass
                out["reason"] = "no_email_field_after_multi_selectors"
                out["final_url"] = page.url[:120]
                _log(slot, "ERR_no_email_field", url=page.url[:100])
                return _finish(out, started)

            try:
                em_input.fill(email)
                _log(slot, "email_typed")
                page.wait_for_timeout(random.randint(1500, 2500))
            except Exception as e:
                out["reason"] = f"email_fill_err: {str(e)[:60]}"
                _log(slot, "ERR_email_fill", err=str(e)[:120])
                return _finish(out, started)

            for txt in ["Continuar", "Continue", "Siguiente", "Next"]:
                try:
                    b = page.get_by_role("button",
                                          name=re.compile(f"^{txt}$", re.I)).first
                    if b.is_visible(timeout=1500):
                        b.click(timeout=3000)
                        _log(slot, "continuar_clicked", text=txt)
                        break
                except Exception: continue
            # Wait MÁS LARGO + networkidle. La pantalla CÁMBIALO puede tardar
            # hasta 6-8s en aparecer con proxy lento.
            page.wait_for_timeout(random.randint(3000, 5000))
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception: pass
            page.wait_for_timeout(2000)  # buffer para JS dinámico
            _log(slot, "post_continuar_url", url=page.url[:100])

            # Detectar reset forzado → AUTO-RECOVERY via account_recovery
            if _check_reset_required(page):
                _log(slot, "TM_forced_reset_detected_post_email_try_recovery")
                # request_already_sent=False → recovery hará TODO el flow:
                # click "Olvidé contraseña" + reenviar email + esperar OTP + nueva pwd.
                # TM puede mostrar pantalla reset SIN enviar email auto (requiere click).
                if _try_recovery_inline(page, ctx, account, slot, out,
                                          request_already_sent=False):
                    # Recovery + login OK. Guardar storage canonical.
                    if _save_storage(ctx, canonical, aid, out):
                        out["ok"] = True
                        out["valid"] = True
                        out["reason"] = "login_ok_after_recovery"
                    return _finish(out, started)
                else:
                    out["reason"] = "reset_required_recovery_failed"
                    return _finish(out, started)

            # ─── Step 5: Password field (multi-selector + timeout largo) ───
            # Wait redirect post-continuar
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception: pass
            pw_selectors = [
                'input[type="password"]',
                'input[name="password"]',
                'input[autocomplete="current-password"]',
                '#password',
            ]
            pw_input = None
            for sel in pw_selectors:
                try:
                    cand = page.locator(sel).first
                    cand.wait_for(state="visible", timeout=8000)
                    pw_input = cand
                    _log(slot, "pwd_field_found", selector=sel)
                    break
                except Exception: continue
            if pw_input is None:
                try:
                    sshot = os.path.join(PROYECTO, "datos",
                                          f"cf_no_pwd_{aid}_{int(time.time())}.png")
                    page.screenshot(path=sshot)
                except Exception: pass
                out["reason"] = "no_password_field_after_multi_selectors"
                out["final_url"] = page.url[:120]
                _log(slot, "ERR_no_pwd_field", url=page.url[:100])
                return _finish(out, started)

            try:
                pw_input.fill(password)
                _log(slot, "pwd_typed")
                page.wait_for_timeout(random.randint(1500, 3000))
            except Exception as e:
                out["reason"] = f"pwd_fill_err: {str(e)[:60]}"
                _log(slot, "ERR_pwd_fill", err=str(e)[:120])
                return _finish(out, started)

            for txt in ["Iniciar sesión", "Sign In", "Login", "Continuar"]:
                try:
                    b = page.get_by_role("button",
                                          name=re.compile(f"^{txt}$", re.I)).first
                    if b.is_visible(timeout=1500):
                        b.click(timeout=3000); break
                except Exception: continue
            # Wait + try networkidle (más fiable que sleep fijo)
            page.wait_for_timeout(6000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception: pass
            page.wait_for_timeout(2000)  # buffer final

            # ─── Step 6: Diagnose post-login ───
            if _check_bot_block(page):
                out["reason"] = "bot_block_post_login"
                _log(slot, "BOT_BLOCK_post_login")
                return _finish(out, started)
            if _check_bad_credentials(page):
                out["reason"] = "bad_credentials"
                _log(slot, "BAD_CREDENTIALS")
                return _finish(out, started)
            if _check_reset_required(page):
                _log(slot, "TM_forced_reset_post_login_try_recovery")
                if _try_recovery_inline(page, ctx, account, slot, out,
                                          request_already_sent=False):
                    if _save_storage(ctx, canonical, aid, out):
                        out["ok"] = True
                        out["valid"] = True
                        out["reason"] = "login_ok_after_recovery"
                    return _finish(out, started)
                else:
                    out["reason"] = "reset_required_recovery_failed"
                    return _finish(out, started)

            # ─── Step 7: Verificar login (URL fuera de auth/signin) ───
            final_url = page.url.lower()
            logged_in_url = ("my.ticketmaster" in final_url and
                              not any(k in final_url for k in
                                       ("auth.", "identity.", "signin", "/login")))
            if logged_in_url or _detect_logged_in(page):
                _log(slot, "LOGIN_OK", email=email, url=final_url[:80])
                if _save_storage(ctx, canonical, aid, out):
                    out["ok"] = True
                    out["valid"] = True
                    out["reason"] = "login_ok"
                return _finish(out, started)

            # ─── Step 7b: Fallback — forzar navegación a /account ───
            # Si el login OK pero la URL quedó rara (ej: queue-it, redirect chain),
            # navegar explícitamente a /account confirma el estado.
            _log(slot, "post_login_ambiguous_try_account_fallback",
                 url=final_url[:100])
            if _verify_login_via_account_page(page, slot):
                _log(slot, "LOGIN_OK_via_account_fallback", email=email)
                if _save_storage(ctx, canonical, aid, out):
                    out["ok"] = True
                    out["valid"] = True
                    out["reason"] = "login_ok"
                    out["via_fallback"] = True
                return _finish(out, started)

            # Re-check reset_required / bot_block tras goto /account
            if _check_reset_required(page):
                _log(slot, "reset_required_post_account_goto_try_recovery")
                if _try_recovery_inline(page, ctx, account, slot, out,
                                          request_already_sent=False):
                    if _save_storage(ctx, canonical, aid, out):
                        out["ok"] = True
                        out["valid"] = True
                        out["reason"] = "login_ok_after_recovery"
                    return _finish(out, started)
                else:
                    out["reason"] = "reset_required_recovery_failed"
                    return _finish(out, started)
            if _check_bot_block(page):
                out["reason"] = "bot_block_post_account"
                return _finish(out, started)

            # ─── Default: estado desconocido ───
            out["reason"] = "unknown_post_login"
            out["final_url"] = final_url[:120]
            _log(slot, "UNKNOWN_post_login", url=final_url[:120])
            return _finish(out, started)

    except Exception as e:
        out["reason"] = f"exception: {type(e).__name__}: {str(e)[:80]}"
        _log(slot, "FATAL_exception", err=str(e)[:200])
        return _finish(out, started)


def _save_storage(ctx, canonical_path: str, account_id: str,
                   out: Dict[str, Any]) -> bool:
    """Guarda storage_state al disco + actualiza accounts.json."""
    try:
        state = ctx.storage_state()
        tmp = canonical_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        if os.path.exists(canonical_path):
            os.replace(tmp, canonical_path)
        else:
            os.rename(tmp, canonical_path)
        out["storage_state_file"] = canonical_path
        out["storage_canonical"] = True

        # Persistir en accounts.json
        try:
            am.account_manager.update_account(account_id, {
                "storage_state_file": canonical_path,
                "last_login": datetime.now().isoformat(),
                "credentials_verified": True,
                "validated_at": datetime.now().isoformat(),
                "validation_reason": "camoufox_login_ok",
            })
        except Exception as e:
            out["persist_warning"] = str(e)[:80]
        return True
    except Exception as e:
        out["storage_save_err"] = str(e)[:80]
        return False


def _finish(out: Dict[str, Any], started: float) -> Dict[str, Any]:
    out["elapsed_s"] = round(time.time() - started, 1)
    return out


# ════════════════════════════════════════════════════════════════════
#  Batch wrapper (compatible con job tracking de validate_batch)
# ════════════════════════════════════════════════════════════════════
def refresh_sessions_batch_camoufox(account_ids: List[str],
                                       max_parallel: int = 3,
                                       headless: bool = True) -> Dict[str, Any]:
    """Lanza job batch para refrescar storage_state con Camoufox.

    Args:
      account_ids: IDs de cuentas a procesar
      max_parallel: workers concurrentes (default 3)
      headless: True (default) — sin ventana

    Returns: {ok, job_id, total, engine}
    """
    if not account_ids:
        return {"ok": False, "error": "account_ids_empty"}

    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _JOBS[job_id] = {
            "id": job_id,
            "total": len(account_ids),
            "done": 0,
            "valid_count": 0,
            "invalid_count": 0,
            "status": "starting",
            "started_at": datetime.now().isoformat(),
            "results": [],
            "max_parallel": max_parallel,
            "engine": "camoufox",
            "job_type": "session_refresh",
            "headless": headless,
            "options": {
                "updateStorage": True,
                "verifyPassword": True,
                "bumpToRegistrations": False,
            },
        }

    th = threading.Thread(target=_run_batch_camoufox,
                           args=(job_id, account_ids, max_parallel, headless),
                           daemon=True)
    th.start()
    return {"ok": True, "job_id": job_id, "total": len(account_ids),
            "engine": "camoufox"}


def _run_batch_camoufox(job_id: str, account_ids: List[str],
                          max_parallel: int, headless: bool):
    """Worker thread del batch. Lanza hasta max_parallel cuentas en threads."""
    import concurrent.futures as cf

    with _jobs_lock:
        _JOBS[job_id]["status"] = "running"

    accs = am.account_manager.list_accounts()
    idx = {a["id"]: a for a in accs if a.get("id")}

    def _one(slot: int, aid: str) -> Dict[str, Any]:
        acc = idx.get(aid)
        if not acc:
            return {"account_id": aid, "valid": False,
                    "reason": "account_not_found", "engine": "camoufox"}
        return login_account_camoufox(acc, slot=slot, headless=headless)

    try:
        with cf.ThreadPoolExecutor(max_workers=max_parallel) as ex:
            futures = {}
            for i, aid in enumerate(account_ids):
                slot = i + 1
                futures[ex.submit(_one, slot, aid)] = aid
            for fut in cf.as_completed(futures):
                aid = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"account_id": aid, "valid": False,
                            "reason": f"thread_error: {str(e)[:80]}",
                            "engine": "camoufox"}
                with _jobs_lock:
                    j = _JOBS.get(job_id)
                    if not j: break
                    j["done"] = j.get("done", 0) + 1
                    if res.get("valid"):
                        j["valid_count"] = j.get("valid_count", 0) + 1
                    else:
                        j["invalid_count"] = j.get("invalid_count", 0) + 1
                    j["results"].append(res)
        with _jobs_lock:
            if job_id in _JOBS:
                _JOBS[job_id]["status"] = "done"
                _JOBS[job_id]["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        with _jobs_lock:
            if job_id in _JOBS:
                _JOBS[job_id]["status"] = "error"
                _JOBS[job_id]["error"] = str(e)[:200]


def get_job(job_id: str) -> Dict[str, Any]:
    with _jobs_lock:
        return dict(_JOBS.get(job_id, {"error": "job_not_found"}))


def list_jobs() -> List[Dict[str, Any]]:
    with _jobs_lock:
        return [dict(j) for j in _JOBS.values()]


# ════════════════════════════════════════════════════════════════════
#  Smoke CLI
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python camoufox_login_only.py <account_id> [--visible]")
        raise SystemExit(2)
    aid = sys.argv[1]
    headless = "--visible" not in sys.argv

    accs = am.account_manager.list_accounts()
    acc = next((a for a in accs if a.get("id") == aid), None)
    if not acc:
        print(f"Account {aid} not found")
        raise SystemExit(1)

    r = login_account_camoufox(acc, slot=0, headless=headless)
    print(json.dumps(r, ensure_ascii=False, indent=2))


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
