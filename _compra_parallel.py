"""Compra paralela — flow real completo HASTA tarjeta autorrellenada (NO paga).

Uso:
    python _compra_parallel.py --slot 1 --event-url URL --offer-idx N --qty 4

Cada slot:
  1) Pide perfil MoreLogin ES nuevo (no quemado).
  2) Pide cuenta TM activa con tarjeta asociada.
  3) Arranca el perfil → puerto CDP propio.
  4) Goto evento → "Ver lo mejor disponible" → stepper offer-idx hasta qty.
  5) Click "Find Tickets / Entradas verde" → checkbox Acepto → "Continuar".
  6) OAuth login con OTP IMAP (joaquina_polo0 → contact_email cuenta).
  7) Watcher hasta checkout → fill_card.
  8) Crea screenshot final + JSON resultado en datos/compra_parallel/.
  NUNCA pulsa Pagar (parametro --confirm es por defecto False).
"""
from __future__ import annotations
import sys, io, time, os, json, re, random, argparse, requests
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

PROYECTO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROYECTO)

# human_motion: Bezier mouse + Fitts + Markov dwell. Si no está disponible,
# fallback automático a page.type/page.click (gracioso). NO bloqueante.
try:
    import human_motion as _hm
    _HUMAN_MOTION = True
except Exception:
    _hm = None
    _HUMAN_MOTION = False

# ════ outcome.json IPC ════
# tm_real_purchase.run_real_purchase llama a este script y lee `outcome.json` al
# final para saber si el flow tuvo éxito (sin parsear logs frágiles).
_OUTCOME_PATH = None  # se setea en main() según --slot

def _outcome_path(slot: int) -> str:
    return os.path.join(PROYECTO, f"_compra_parallel_slot{slot}.outcome.json")

def write_outcome(slot: int, result: dict) -> None:
    """Escribe outcome.json estructurado (status + step_reached + order_id + error).

    Robusto contra fallos: si no se puede escribir, log y sigue.
    """
    try:
        path = _outcome_path(slot)
        result.setdefault("ts", datetime.now().isoformat())
        result.setdefault("slot", slot)
        try:
            import json_atomic
            json_atomic.save(path, result)
        except Exception:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    except Exception:
        pass

import account_manager as am
import bot_orchestrator as bo
from imap_tools import MailBox, AND


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--slot", type=int, default=1)
    p.add_argument("--event-url", required=True)
    p.add_argument("--offer-idx", type=int, default=3, help="0=PIT, 3=Asiento Reservado (Karol G)")
    p.add_argument("--qty", type=int, default=4)
    p.add_argument("--mode", default="quick", choices=("quick", "seatmap"),
                    help="quick=Ver lo mejor disponible / seatmap=selección sector por sector")
    p.add_argument("--sections", default="",
                    help="seatmap: lista de section_id o tm_api_id separados por coma "
                         "(en orden de prioridad)")
    p.add_argument("--allow-separated", action="store_true",
                    help="Si TM dice 'asientos no juntos', aceptar igual. Por defecto "
                         "el bot pulsa 'Ajustar selección' para forzar consecutivos.")
    p.add_argument("--confirm", action="store_true",
                    help="SOLO si quieres pulsar Pagar al final (peligroso)")
    return p.parse_args()


def handle_separated_seats_modal(slot, page, allow_separated=False):
    """Detecta el modal 'Es posible que las entradas no estén juntas' que TM
    muestra a veces tras añadir qty>1 en quick mode. Política del usuario:
    pulsar 'Ajustar selección' SIEMPRE para forzar asientos consecutivos.
    Solo si allow_separated=True acepta entradas dispersas."""
    try:
        result = page.evaluate(r"""() => {
            const dialogs = Array.from(document.querySelectorAll(
                '[role=dialog], [role=alertdialog], .modal, [class*="modal" i]'));
            for (const d of dialogs) {
                const txt = (d.innerText || '').toLowerCase();
                if (/no est[eé]n juntas|tipos de entrada diferentes|no consecutiv/i.test(txt)) {
                    const btns = Array.from(d.querySelectorAll('button'))
                        .filter(b => b.offsetParent !== null);
                    return {found: true, button_texts: btns.map(b => (b.innerText||'').trim())};
                }
            }
            return {found: false};
        }""")
        if not result.get("found"):
            return {"detected": False}
        log(slot, "separated_seats_modal_detected", buttons=result.get("button_texts"))
        if allow_separated:
            log(slot, "policy_allow_separated", note="cerrando diálogo, aceptando dispersas")
            page.evaluate(r"""() => {
                const dialogs = Array.from(document.querySelectorAll(
                    '[role=dialog], [role=alertdialog], .modal, [class*="modal" i]'));
                for (const d of dialogs) {
                    const txt = (d.innerText || '').toLowerCase();
                    if (/no est[eé]n juntas|tipos de entrada diferentes/i.test(txt)) {
                        const close = Array.from(d.querySelectorAll('button'))
                            .find(b => /cerrar/i.test(b.innerText || ''));
                        if (close) close.click();
                        return;
                    }
                }
            }""")
            return {"detected": True, "action": "closed_dialog"}
        # Default: ajustar selección
        clicked = page.evaluate(r"""() => {
            const dialogs = Array.from(document.querySelectorAll(
                '[role=dialog], [role=alertdialog], .modal, [class*="modal" i]'));
            for (const d of dialogs) {
                const txt = (d.innerText || '').toLowerCase();
                if (!/no est[eé]n juntas|tipos de entrada diferentes/i.test(txt)) continue;
                const btns = Array.from(d.querySelectorAll('button'))
                    .filter(b => b.offsetParent !== null);
                const adjust = btns.find(b => /ajustar selecci[oó]n/i.test(b.innerText || ''));
                if (adjust) { adjust.click(); return {clicked: 'ajustar'}; }
                // Fallback: si no encuentra Ajustar, volver atrás
                const back = btns.find(b => /atr[aá]s|volver/i.test(b.innerText || ''));
                if (back) { back.click(); return {clicked: 'atras'}; }
                return {clicked: null};
            }
            return {clicked: null};
        }""")
        log(slot, "ajustar_seleccion_clicked", **clicked)
        return {"detected": True, "action": clicked.get("clicked")}
    except Exception as e:
        log(slot, "modal_handler_err", err=str(e)[:120])
        return {"detected": False, "error": str(e)[:120]}


def handle_pre_checkout_step(slot, page):
    """Detecta y completa la pantalla intermedia entre 'Continuar comprando' y
    el checkout final. TM muestra:
      1. Checkbox 'Partners del evento' (marcar=SÍ).
      2. Checkbox 'Condiciones de compra / Acepto' (obligatorio).
      3. Botón verde 'Ir al pago'.
    Si la pantalla aparece, marca todo y pulsa el botón. Si no aparece, no
    hace nada (no es bloqueante)."""
    try:
        result = page.evaluate(r"""() => {
            const out = {found: false, partners: false, conditions: false, btn: false};
            const labels = Array.from(document.querySelectorAll('label'));
            // 1) Marcar checkbox 'partners del evento'
            const partnersLbl = labels.find(l =>
                /partners?\s+del\s+evento|partners?\s+del\s+organizador|recibir.*ofertas|comunicaciones.*partner/i
                  .test(l.textContent || ''));
            if (partnersLbl) {
                const id = partnersLbl.getAttribute('for');
                let cb = id ? document.getElementById(id) : partnersLbl.querySelector('input[type=checkbox]');
                if (cb && !cb.checked) {
                    cb.scrollIntoView({block:'center'});
                    cb.click();
                    out.partners = true;
                    out.found = true;
                }
            }
            // 2) Marcar 'Condiciones de compra' / 'Acepto'
            const condsLbl = labels.find(l =>
                /condiciones\s+de\s+compra|acepto.*condiciones|t[eé]rminos\s+y\s+condiciones|acepto\s+que\s+he\s+le[ií]do/i
                  .test(l.textContent || ''));
            if (condsLbl) {
                const id = condsLbl.getAttribute('for');
                let cb = id ? document.getElementById(id) : condsLbl.querySelector('input[type=checkbox]');
                if (cb && !cb.checked) {
                    cb.scrollIntoView({block:'center'});
                    cb.click();
                    out.conditions = true;
                    out.found = true;
                }
            }
            return out;
        }""")
        log(slot, "pre_checkout_checkboxes", **result)
        if result.get("found"):
            # Pulsar botón verde "Ir al pago"
            human_pause(0.8, 1.5)
            clicked = page.evaluate(r"""() => {
                const btns = Array.from(document.querySelectorAll('button, a'))
                    .filter(b => b.offsetParent !== null);
                const ir = btns.find(b => /ir al pago|proceed to payment|continuar al pago|pagar ahora/i
                    .test(b.innerText || b.textContent || ''));
                if (ir) {
                    ir.scrollIntoView({block:'center'});
                    ir.click();
                    return {clicked: true, label: (ir.innerText||'').slice(0,40)};
                }
                return {clicked: false};
            }""")
            log(slot, "ir_al_pago_clicked", **clicked)
            if clicked.get("clicked"):
                human_pause(4, 6)
            return clicked.get("clicked", False)
        return False
    except Exception as e:
        log(slot, "pre_checkout_handler_err", err=str(e)[:120])
        return False


def select_seatmap_sections(slot, page, sections, qty):
    """Modo seatmap: intenta cada sección de la lista en orden hasta encontrar
    una con disponibilidad de qty asientos consecutivos. Usa preferentemente
    la API ismds via fetch (rapidez); fallback a click en el SVG.
    Devuelve {ok, used_section, attempts}."""
    attempts = []
    # 1) Cambiar a modo seatmap si no estamos
    try:
        page.evaluate(r"""() => {
            const tabs = Array.from(document.querySelectorAll('[role=tab], button'));
            const t = tabs.find(x => /mapa de asientos|seat\s*map/i.test(x.innerText||''));
            if (t) t.click();
        }""")
        page.wait_for_timeout(2500)
    except Exception: pass
    # 2) Para cada sección candidata, click sobre [data-section-id=<id>]
    for sec in sections:
        sec_id = sec.strip()
        if not sec_id: continue
        clicked = page.evaluate(f"""() => {{
            const el = document.querySelector('[data-section-id="{sec_id}"], '
                + '[data-section-name="{sec_id}"]');
            if (!el) return {{found: false}};
            if (el.getAttribute('data-active') === 'false') return {{found: true, active: false}};
            el.scrollIntoView({{block: 'center'}});
            el.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
            return {{found: true, active: true}};
        }}""")
        attempts.append({"section": sec_id, **clicked})
        log(slot, "section_click_attempt", **{"section": sec_id, **clicked})
        if clicked.get("found") and clicked.get("active"):
            page.wait_for_timeout(2500)
            # Verificar si TM aceptó (panel de seats aparece)
            ok = page.evaluate(r"""() => {
                return document.querySelector('[data-testid*="seats" i], [class*="seat-picker" i]') != null;
            }""")
            if ok:
                return {"ok": True, "used_section": sec_id, "attempts": attempts}
    return {"ok": False, "attempts": attempts, "error": "ninguna_section_disponible"}


def log(slot, stage, **kw):
    msg = f"[slot{slot}][{time.strftime('%H:%M:%S')}] {stage}"
    if kw: msg += " " + json.dumps(kw, ensure_ascii=False, default=str)[:500]
    print(msg, flush=True)


def shoot(slot, page, name):
    d = os.path.join(PROYECTO, "datos", "compra_parallel")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"slot{slot}_{int(time.time())}_{name}.png")
    try:
        page.screenshot(path=p, full_page=False)
        log(slot, "shot", file=os.path.basename(p))
    except Exception as e:
        log(slot, "shot_err", err=str(e)[:80])
    return p


def human_pause(min_s=0.8, max_s=2.0):
    time.sleep(random.uniform(min_s, max_s))


def imap_otp(slot, email_madre, pwd, since_ts, max_wait=180):
    deadline = time.time() + max_wait
    pat6 = re.compile(r'\b(\d{6})\b')
    while time.time() < deadline:
        try:
            with MailBox('imap.mail.me.com').login(email_madre, pwd) as mb:
                mb.folder.set('INBOX')
                msgs = list(mb.fetch(AND(date_gte=since_ts.date()),
                                     limit=20, reverse=True, mark_seen=False))
                tm = [m for m in msgs if 'ticketmaster' in (m.from_ or '').lower()
                      and m.date and m.date.replace(tzinfo=None) >= since_ts.replace(tzinfo=None)]
                log(slot, "imap_poll", recent=len(tm))
                for m in tm:
                    body = (m.text or '') + ' ' + (m.subject or '')
                    for code in pat6.findall(body):
                        if 100000 <= int(code) <= 999999:
                            log(slot, "otp_found", code=code)
                            return code
        except Exception as e:
            log(slot, "imap_err", err=str(e)[:80])
        time.sleep(8)
    return None


_BAD_CREDS_PATH = os.path.join(PROYECTO, "_compra_parallel_bad_credentials.json")


def _load_bad_credentials():
    try:
        if os.path.exists(_BAD_CREDS_PATH):
            with open(_BAD_CREDS_PATH, encoding='utf-8') as f:
                return set(json.load(f))
    except Exception: pass
    return set()


def _mark_bad_credentials(account_id):
    """Persiste account_id como credenciales inválidas para no reusar."""
    bad = _load_bad_credentials()
    bad.add(account_id)
    try:
        with open(_BAD_CREDS_PATH, 'w', encoding='utf-8') as f:
            json.dump(list(bad), f)
    except Exception: pass


def detect_invalid_credentials(page):
    """Detecta mensaje TM 'No podemos encontrar una cuenta con el correo y
    contraseña indicados'. Devuelve True si está visible."""
    try:
        return page.evaluate(r"""() => {
            const txt = (document.body && document.body.innerText || '').toLowerCase();
            return /no podemos encontrar una cuenta|cuenta con el correo y contrase|contrase[ñn]a indicados|incorrect.*password|invalid.*credentials/i.test(txt);
        }""")
    except Exception:
        return False


def _hm_type(page, selector, text, fallback_delay=80):
    """Intenta tipear con human_motion (Bezier + flight time NuData). Fallback page.type."""
    if _HUMAN_MOTION:
        try:
            r = _hm.human_type(page, selector, text, dwell_ms_avg=85, std=30)
            if r.get("ok"):
                return True
        except Exception:
            pass
    try:
        page.click(selector); page.type(selector, text, delay=fallback_delay)
        return True
    except Exception:
        return False


def _hm_click_button(page, txt_regex, timeout_ms=1500):
    """Click humano (Bezier path + Markov pre-dwell) sobre primer botón con texto match.
    Fallback: page.locator(...).click(force=True). Devuelve True si clicó.
    """
    try:
        b = page.get_by_role("button", name=re.compile(txt_regex, re.I)).first
        if not b.is_visible(timeout=timeout_ms):
            return False
        if _HUMAN_MOTION:
            try:
                box = b.bounding_box(timeout=2000)
                if box:
                    cx = box["x"] + box["width"] / 2 + random.uniform(-box["width"]*0.2, box["width"]*0.2)
                    cy = box["y"] + box["height"] / 2 + random.uniform(-box["height"]*0.2, box["height"]*0.2)
                    r = _hm.human_click(page, cx, cy)
                    if r.get("ok"):
                        return True
            except Exception:
                pass
        b.click(force=True, timeout=3000)
        return True
    except Exception:
        return False


def login_full(slot, page, account):
    log(slot, "login_start", email=account.get('tm_email'),
        human_motion=_HUMAN_MOTION)
    human_pause(2, 4)
    try:
        page.wait_for_selector('input[type="email"]', timeout=30000)
    except Exception:
        log(slot, "no_email_input"); shoot(slot, page, "no_email_input"); return False
    _hm_type(page, 'input[type="email"]', account['tm_email'])
    log(slot, "email_typed")
    human_pause(1.5, 2.5)
    for txt in ["Continuar", "Continue", "Siguiente", "Next"]:
        if _hm_click_button(page, txt):
            break
    human_pause(3, 5)
    try:
        page.wait_for_selector('input[type="password"]', timeout=20000)
    except Exception:
        log(slot, "no_pwd_input"); shoot(slot, page, "no_pwd"); return False
    _hm_type(page, 'input[type="password"]', account['tm_password'])
    log(slot, "pwd_typed")
    human_pause(1.5, 3)
    for txt in ["Iniciar sesión", "Sign In", "Login", "Continuar"]:
        if _hm_click_button(page, txt):
            break
    page.wait_for_timeout(8000); shoot(slot, page, "post_login")
    # Detectar error "No podemos encontrar una cuenta..."
    if detect_invalid_credentials(page):
        log(slot, "BAD_CREDENTIALS_detected",
            email=account.get('tm_email'),
            note="Marcando cuenta como inválida y devolviendo bad_credentials")
        _mark_bad_credentials(account.get('id'))
        shoot(slot, page, "bad_credentials")
        return "bad_credentials"
    # OTP
    try:
        otp = page.locator('input[name*="code" i], input[autocomplete*="one-time" i], input[type="tel"][maxlength]').first
        if otp.is_visible(timeout=3000):
            log(slot, "otp_required"); shoot(slot, page, "otp_screen")
            since = datetime.now()
            code = imap_otp(slot, account.get('contact_email') or account.get('email', ''),
                             account.get('contact_password') or account.get('email_password', ''),
                             since, max_wait=180)
            if code:
                otp.fill(code); page.wait_for_timeout(800)
                for txt in ["Verificar", "Confirmar", "Continuar"]:
                    if _hm_click_button(page, txt):
                        break
                page.wait_for_timeout(5000); return True
            return False
    except Exception: pass
    return True


def fill_card(slot, page, card, account):
    log(slot, "fill_card_start"); page.wait_for_timeout(4500); shoot(slot, page, "checkout_initial")
    holder = card.get('holder', '')
    number = re.sub(r'\D', '', card.get('number', ''))
    month = str(card.get('month', '')).zfill(2)
    year = str(card.get('year', ''))
    cvv = card.get('cvv', '')
    yyyy = ("20" + year) if len(year) == 2 else year

    def try_fill(loc, val, lbl):
        try:
            if loc.is_visible(timeout=800):
                loc.fill(val); log(slot, "filled", field=lbl); return True
        except Exception: pass
        return False

    targets = [page] + list(page.frames)
    for t in targets:
        for sel in ['input[name*="number" i]', 'input[id*="cardNumber" i]',
                    'input[autocomplete="cc-number"]', 'input[data-cse="encryptedCardNumber"]']:
            if try_fill(t.locator(sel).first, number, "card_number"): break
        for sel in ['input[name*="holder" i]', 'input[id*="holderName" i]',
                    'input[autocomplete="cc-name"]']:
            if try_fill(t.locator(sel).first, holder, "holder"): break
        for sel in ['input[name*="expiry" i]', 'input[id*="expiryDate" i]',
                    'input[autocomplete="cc-exp"]', 'input[data-cse="encryptedExpiryDate"]']:
            if try_fill(t.locator(sel).first, f"{month}/{year[-2:]}", "expiry"): break
        for sel in ['input[autocomplete="cc-exp-month"]', 'input[name*="expMonth" i]']:
            if try_fill(t.locator(sel).first, month, "exp_month"): break
        for sel in ['input[autocomplete="cc-exp-year"]', 'input[name*="expYear" i]']:
            if try_fill(t.locator(sel).first, yyyy, "exp_year"): break
        for sel in ['input[name*="cvv" i]', 'input[name*="cvc" i]',
                    'input[id*="securityCode" i]', 'input[autocomplete="cc-csc"]',
                    'input[data-cse="encryptedSecurityCode"]']:
            if try_fill(t.locator(sel).first, cvv, "cvv"): break
    # Dirección de entrega + datos personales (multi-selector exhaustivo)
    address1 = account.get('address1', '') or account.get('address', '')
    city = account.get('city', '')
    zip_code = account.get('zip', '') or account.get('postal', '')
    state = account.get('state', '')
    phone_full = account.get('phone_prefix', '') + account.get('phone', '')
    for t in targets:
        # Nombre / apellidos
        for sel in ['input[name*="firstName" i]', 'input[id*="firstName" i]',
                    'input[autocomplete="given-name"]', 'input[name*="first_name" i]',
                    'input[name*="nombre" i]:not([name*="apel" i])']:
            if try_fill(t.locator(sel).first, account.get('first_name', ''), "first_name"): break
        for sel in ['input[name*="lastName" i]', 'input[id*="lastName" i]',
                    'input[autocomplete="family-name"]', 'input[name*="last_name" i]',
                    'input[name*="apellid" i]']:
            if try_fill(t.locator(sel).first, account.get('last_name', ''), "last_name"): break
        # Teléfono
        for sel in ['input[name*="phone" i]', 'input[autocomplete="tel"]',
                    'input[type="tel"]', 'input[id*="phone" i]',
                    'input[name*="telefono" i]', 'input[name*="mobile" i]']:
            if try_fill(t.locator(sel).first, phone_full, "phone"): break
        # Dirección de entrega — múltiples nombres TM/Adyen usa
        for sel in ['input[name*="address1" i]', 'input[name*="addressLine1" i]',
                    'input[name*="address-line-1" i]', 'input[name*="street" i]',
                    'input[autocomplete="address-line1"]', 'input[autocomplete="street-address"]',
                    'input[name*="shippingAddress" i]', 'input[name*="deliveryAddress" i]',
                    'input[name*="direccion" i]', 'input[id*="addressLine1" i]']:
            if try_fill(t.locator(sel).first, address1, "address1"): break
        # Address line 2 (opcional, vacío si no hay)
        addr2 = account.get('address2', '')
        if addr2:
            for sel in ['input[name*="address2" i]', 'input[name*="addressLine2" i]',
                        'input[autocomplete="address-line2"]']:
                if try_fill(t.locator(sel).first, addr2, "address2"): break
        # Ciudad
        for sel in ['input[name*="city" i]', 'input[autocomplete="address-level2"]',
                    'input[id*="city" i]', 'input[name*="ciudad" i]',
                    'input[name*="locality" i]']:
            if try_fill(t.locator(sel).first, city, "city"): break
        # Código postal / ZIP
        for sel in ['input[name*="postal" i]', 'input[name*="zip" i]',
                    'input[autocomplete="postal-code"]', 'input[id*="postal" i]',
                    'input[name*="postcode" i]', 'input[id*="zip" i]']:
            if try_fill(t.locator(sel).first, zip_code, "postal_code"): break
        # Estado/Provincia (opcional)
        if state:
            for sel in ['input[name*="state" i]', 'input[name*="province" i]',
                        'input[autocomplete="address-level1"]', 'input[name*="provincia" i]']:
                if try_fill(t.locator(sel).first, state, "state"): break
    # Click "Usar esta dirección" / "Continuar con esta dirección" si TM lo
    # muestra como confirmación tras rellenar (modo billing → shipping).
    try:
        page.evaluate(r"""() => {
            const btns = Array.from(document.querySelectorAll('button'))
                .filter(b => b.offsetParent !== null);
            const addr = btns.find(b => /usar esta direcci[oó]n|continuar con esta direcci|use this address/i.test(b.innerText || ''));
            if (addr) addr.click();
        }""")
    except Exception: pass
    shoot(slot, page, "checkout_filled")


_PROFILE_LOCK_PATH = os.path.join(PROYECTO, "_compra_parallel_active_profiles.lock")
_PROFILE_LOCK_MUTEX = os.path.join(PROYECTO, "_compra_parallel_pick.mutex")


def _acquire_pick_mutex(timeout=30):
    """Mutex cross-process via O_EXCL para serializar pick+claim atómico.
    Otros procesos esperan hasta que se libere. Stale > 60s se descarta."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(_PROFILE_LOCK_MUTEX, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try: os.write(fd, str(os.getpid()).encode())
            finally: os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(_PROFILE_LOCK_MUTEX)
                if age > 60:
                    try: os.unlink(_PROFILE_LOCK_MUTEX)
                    except Exception: pass
            except Exception: pass
            time.sleep(0.5)
    return False


def _release_pick_mutex():
    try: os.unlink(_PROFILE_LOCK_MUTEX)
    except Exception: pass


def _read_active_profiles():
    try:
        if os.path.exists(_PROFILE_LOCK_PATH):
            with open(_PROFILE_LOCK_PATH, encoding='utf-8') as f:
                return set(json.load(f))
    except Exception: pass
    return set()


def _add_active_profile(name):
    """Lock cross-process: añade un perfil al lock file + bo._active_profiles."""
    bo._active_profiles.add(name)
    active = _read_active_profiles()
    active.add(name)
    try:
        with open(_PROFILE_LOCK_PATH, 'w', encoding='utf-8') as f:
            json.dump(list(active), f)
    except Exception: pass


def _remove_active_profile(name):
    bo._active_profiles.discard(name)
    active = _read_active_profiles()
    active.discard(name)
    try:
        with open(_PROFILE_LOCK_PATH, 'w', encoding='utf-8') as f:
            json.dump(list(active), f)
    except Exception: pass


def pick_morelogin_profile(slot):
    """Pick profile ES no quemado y no en uso por otro slot.
    Lock cross-process via _PROFILE_LOCK_PATH."""
    burned = set()
    used_ips = set()
    try:
        import register_account as ra
        if hasattr(ra, 'load_burned_profiles'):
            burned = ra.load_burned_profiles()
        if hasattr(ra, 'load_used_ips'):
            used_ips = ra.load_used_ips()
    except Exception:
        pass
    # Cross-process lock: leer perfiles ya en uso por OTROS slots
    cross_active = _read_active_profiles()
    for page_no in range(1, 21):
        try:
            r = requests.post(f'{bo.MORELOGIN_API}/api/env/page',
                               json={'pageNo': page_no, 'pageSize': 100},
                               timeout=10).json()
        except Exception:
            return None
        items = (r.get('data') or {}).get('dataList') or []
        if not items: break
        for it in items:
            p = it.get('proxy') or {}
            name = it.get('envName') or ''
            cc = (p.get('countryCode') or '').upper()
            user = p.get('username') or ''
            if not name or name in burned: continue
            if name in bo._active_profiles: continue
            if name in cross_active: continue   # otro slot ya lo está usando
            is_es = (cc == 'ES') or ('cc-ES' in user) or ('-ES-' in user)
            if not is_es: continue
            if p.get('proxyCheckStatus') == 2: continue
            exp_ip = p.get('exportIp') or ''
            if exp_ip and exp_ip in used_ips: continue
            _add_active_profile(name)
            return {'profile': name, 'env_id': it['id'], 'ip': exp_ip,
                    'proxy_username': user, 'proxy_country': cc}
    return None


def pick_account(slot, used_ids, bad_creds=None):
    """Pick cuenta TM ES activa con tarjeta asociada, no usada por otro slot
    y no marcada con credenciales inválidas."""
    if bad_creds is None:
        bad_creds = _load_bad_credentials()
    accs = am.account_manager.list_accounts()
    cards = am.account_manager.list_cards()
    cards_by_acc = {c.get('account_id'): c for c in cards if c.get('account_id')}
    candidates = [a for a in accs
                  if a.get('status') == 'active'
                  and a.get('id') not in used_ids
                  and a.get('id') not in bad_creds
                  and (cards_by_acc.get(a.get('id')) or a.get('card_id'))]
    if not candidates:
        candidates = [a for a in accs if a.get('status') == 'active'
                      and a.get('id') not in used_ids
                      and a.get('id') not in bad_creds]
    if not candidates:
        return None, None
    a = candidates[0]
    c = cards_by_acc.get(a.get('id'))
    return a, c


def login_with_account_swap(slot, page, ctx, account, card,
                             used_ids_lock_path, max_account_swaps=5):
    """Login robusto: si detecta credenciales inválidas, marca la cuenta como
    bad, libera lock, pickea OTRA cuenta de la lista, y reintenta hasta
    `max_account_swaps` veces. Devuelve dict {ok, account, card, swaps}.
    Si tras max_swaps sigue fallando → ok=False y caller debe cerrar browser."""
    swaps = 0
    current_account = account
    current_card = card
    while True:
        result = login_full(slot, page, current_account)
        if result == "bad_credentials":
            # Liberar la cuenta del lock + marcar bad
            try:
                with open(used_ids_lock_path, encoding='utf-8') as f:
                    ids = set(json.load(f))
                ids.discard(current_account.get('id'))
                with open(used_ids_lock_path, 'w', encoding='utf-8') as f:
                    json.dump(list(ids), f)
            except Exception: pass
            swaps += 1
            if swaps >= max_account_swaps:
                log(slot, "max_account_swaps_reached", swaps=swaps)
                return {"ok": False, "reason": "max_swaps", "swaps": swaps,
                        "account": current_account, "card": current_card}
            # Pick otra cuenta
            try:
                with open(used_ids_lock_path, encoding='utf-8') as f:
                    ids = set(json.load(f))
            except Exception:
                ids = set()
            new_account, new_card = pick_account(slot, ids)
            if not new_account:
                log(slot, "no_more_accounts_to_try")
                return {"ok": False, "reason": "no_more_accounts",
                        "swaps": swaps}
            log(slot, "account_swap", from_email=current_account.get('tm_email'),
                to_email=new_account.get('tm_email'))
            ids.add(new_account.get('id'))
            try:
                with open(used_ids_lock_path, 'w', encoding='utf-8') as f:
                    json.dump(list(ids), f)
            except Exception: pass
            # Cargar nuevo storage_state si existe
            ssf = new_account.get('storage_state_file')
            if ssf and os.path.exists(ssf):
                try:
                    with open(ssf, encoding='utf-8') as f:
                        state = json.load(f)
                    ctx.clear_cookies()
                    if state.get('cookies'):
                        ctx.add_cookies(state['cookies'])
                except Exception as e:
                    log(slot, "storage_swap_err", err=str(e)[:80])
            current_account = new_account
            current_card = new_card
            # Volver a abrir formulario de login
            try:
                page.goto("https://my.ticketmaster.es/", timeout=30000,
                          wait_until="domcontentloaded")
                human_pause(2, 4)
            except Exception: pass
            continue
        # Resultado True/False normal del login_full
        return {"ok": bool(result), "account": current_account,
                "card": current_card, "swaps": swaps}


def main():
    args = parse_args()
    # Reset outcome al iniciar (si hubo run anterior)
    try:
        op = _outcome_path(args.slot)
        if os.path.exists(op):
            os.remove(op)
    except Exception:
        pass
    write_outcome(args.slot, {"status": "started",
                                "event_url": args.event_url[:120],
                                "qty": args.qty, "offer_idx": args.offer_idx,
                                "started_at": datetime.now().isoformat()})
    slot = args.slot
    log(slot, "init", event_url=args.event_url[:60], offer_idx=args.offer_idx, qty=args.qty)

    # Espera escalonada por slot para que el cross-process lock se vea bien.
    # slot 1 → 0s, slot 2 → 5s, slot 3 → 10s, slot 4 → 15s
    pre_sleep = max(0, (slot - 1) * 5)
    if pre_sleep:
        log(slot, "pre_sleep_for_lock", seconds=pre_sleep)
        time.sleep(pre_sleep)

    # 1) Pick perfil MoreLogin con MUTEX cross-process (O_EXCL atomic)
    prof = None
    for retry in range(5):
        if not _acquire_pick_mutex(timeout=30):
            log(slot, "pick_mutex_timeout"); time.sleep(3); continue
        try:
            prof = pick_morelogin_profile(slot)
        finally:
            _release_pick_mutex()
        if prof: break
        log(slot, "pick_retry", retry=retry+1)
        time.sleep(3)
    if not prof:
        log(slot, "FATAL_no_profile"); return 1
    log(slot, "profile_picked", **prof)

    # 2) Pick cuenta + tarjeta
    used_lock_path = os.path.join(PROYECTO, "_compra_parallel_used_accounts.lock")
    used_ids = set()
    try:
        if os.path.exists(used_lock_path):
            with open(used_lock_path, encoding='utf-8') as f:
                used_ids = set(json.load(f))
    except Exception: pass
    account, card = pick_account(slot, used_ids)
    if not account:
        log(slot, "FATAL_no_account"); return 1
    used_ids.add(account.get('id'))
    try:
        with open(used_lock_path, 'w', encoding='utf-8') as f:
            json.dump(list(used_ids), f)
    except Exception: pass
    log(slot, "account_picked", email=account.get('tm_email'),
        has_card=bool(card))

    if not card:
        # Buscar 1ª tarjeta del CSV plantilla
        cards = am.account_manager.list_cards()
        card = cards[0] if cards else {}
        log(slot, "card_fallback", id=card.get('id'))

    # 3) Arrancar perfil MoreLogin
    try:
        r = requests.post(f'{bo.MORELOGIN_API}/api/env/start',
                           json={'envId': prof['env_id']}, timeout=20).json()
        port = int(r['data']['debugPort'])
        log(slot, "profile_started", port=port)
    except Exception as e:
        log(slot, "FATAL_profile_start", err=str(e)[:120]); return 1
    time.sleep(5)

    try:
        from playwright.sync_api import sync_playwright
        v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
            ctx = b.contexts[0] if b.contexts else b.new_context()
            try:
                ctx.add_init_script("""
                    if (window.Notification) Notification.requestPermission = () => Promise.resolve('denied');
                """)
            except Exception: pass
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.bring_to_front()

            # ─── PRE-LOGIN PROACTIVO ───
            # TM bloquea silenciosamente "Continuar comprando" si no hay sesión:
            # tras click, la URL no cambia y el watcher se queda colgado. Solución:
            # 1) Si la cuenta tiene storage_state_file → cargar cookies/localStorage.
            # 2) Si no → ir a /login PRIMERO, autenticar, luego al evento.
            ssf = account.get('storage_state_file')
            if ssf and os.path.exists(ssf):
                try:
                    log(slot, "loading_storage_state", path=os.path.basename(ssf))
                    with open(ssf, encoding='utf-8') as f:
                        state = json.load(f)
                    if state.get('cookies'):
                        ctx.add_cookies(state['cookies'])
                    log(slot, "storage_state_loaded", cookies=len(state.get('cookies') or []))
                except Exception as e:
                    log(slot, "storage_state_err", err=str(e)[:120])
            else:
                # Login proactivo en página /login antes del evento
                login_url = "https://my.ticketmaster.es/" if "ticketmaster.es" in args.event_url else \
                            "https://my.ticketmaster.com/"
                try:
                    log(slot, "pre_login_navigate", url=login_url)
                    page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                    human_pause(3, 5)
                    # Cookies banner
                    for txt in ["Aceptar todas las cookies", "Aceptar", "Accept"]:
                        try:
                            el = page.get_by_text(txt, exact=False).first
                            if el.is_visible(timeout=1500):
                                el.click(); human_pause(1, 2); break
                        except Exception: continue
                    # Si redirige a evento ya logueado → skip login
                    if 'sign' in page.url.lower() or 'login' in page.url.lower() or 'auth' in page.url.lower():
                        log(slot, "pre_login_form_detected")
                        login_res = login_with_account_swap(
                            slot, page, ctx, account, card, used_lock_path,
                            max_account_swaps=5)
                        if not login_res.get("ok"):
                            log(slot, "FATAL_login_swap_failed",
                                reason=login_res.get("reason"))
                            return 1
                        # Actualizar account/card si hubo swap
                        if login_res.get("swaps", 0) > 0:
                            account = login_res.get("account", account)
                            card = login_res.get("card", card)
                        human_pause(3, 5)
                    else:
                        log(slot, "pre_login_skipped_already_logged")
                except Exception as e:
                    log(slot, "pre_login_err", err=str(e)[:120])

            # 4) Goto evento
            log(slot, "goto", url=args.event_url[:80])
            page.goto(args.event_url, wait_until="domcontentloaded", timeout=30000)
            human_pause(5, 7)
            # Cookies
            for txt in ["Aceptar todas las cookies", "Aceptar", "Accept"]:
                try:
                    el = page.get_by_text(txt, exact=False).first
                    if el.is_visible(timeout=1500):
                        el.click(); human_pause(1, 2); break
                except Exception: continue
            shoot(slot, page, "event_loaded")

            if args.mode == "quick":
                # === MODO GENÉRICO: "Ver lo mejor disponible" + stepper ===
                log(slot, "quick_mode")
                page.evaluate("""() => {
                    const b = Array.from(document.querySelectorAll('button')).find(x => /ver lo mejor disponible/i.test(x.innerText||''));
                    if (b) b.click();
                }""")
                human_pause(3, 5)

                # 6) Stepper qty en offer-idx específico
                log(slot, "stepper_offer", idx=args.offer_idx, target_qty=args.qty)
                for i in range(args.qty + 4):
                    cur = page.evaluate(f"""() => {{
                        const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{args.offer_idx}];
                        return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
                    }}""")
                    log(slot, "qty", current=cur)
                    if cur >= args.qty: break
                    page.evaluate(f"""() => {{
                        const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{args.offer_idx}];
                        if (!s) return;
                        const btns = s.querySelectorAll('button');
                        (btns[btns.length-1] || btns[0]).click();
                    }}""")
                    human_pause(0.4, 0.8)
                shoot(slot, page, "stepper_done")

                # 7) Click Entradas verde
                log(slot, "click_find_tickets")
                try:
                    btn = page.locator('button[data-testid="findTicketsBtn"]').first
                    btn.scroll_into_view_if_needed(timeout=4000)
                    human_pause(0.6, 1.2)
                    btn.click(timeout=10000, force=True)
                except Exception as e:
                    log(slot, "find_tickets_err", err=str(e)[:120])
                human_pause(4, 6)
                shoot(slot, page, "after_find_tickets")

                # 7b) Handler "asientos no juntos" — política: SIEMPRE Ajustar selección
                # (a menos que --allow-separated esté pasado)
                for ck in range(3):
                    h = handle_separated_seats_modal(slot, page,
                                                      allow_separated=args.allow_separated)
                    if h.get("detected"):
                        human_pause(2, 4)
                    else:
                        break

            else:
                # === MODO SEATMAP: selección sector por sector vía API/SVG ===
                log(slot, "seatmap_mode")
                sections_csv = (args.sections or "").strip()
                sections = [s.strip() for s in sections_csv.split(",") if s.strip()]
                if not sections:
                    log(slot, "FATAL_seatmap_no_sections",
                        note="--mode seatmap requiere --sections id1,id2,id3")
                    return 1
                res = select_seatmap_sections(slot, page, sections, qty=args.qty)
                log(slot, "seatmap_result", **{k: res.get(k) for k in ("ok", "used_section")})
                if not res.get("ok"):
                    shoot(slot, page, "seatmap_failed")
                    return 1
                shoot(slot, page, "seatmap_section_picked")

            # 8) Checkbox Acepto + Continuar
            log(slot, "checkbox_acepto")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            human_pause(1, 2)
            page.evaluate("""() => {
                const labels = Array.from(document.querySelectorAll('label'));
                const lbl = labels.find(l => /acepto que he le[ií]do/i.test(l.textContent||''));
                if (!lbl) return;
                const forId = lbl.getAttribute('for');
                let cb = forId ? document.getElementById(forId) : null;
                if (!cb) cb = lbl.querySelector('input[type=checkbox]');
                if (cb) { cb.scrollIntoView({block:'center'}); cb.click(); }
            }""")
            human_pause(1.2, 2)

            # 9) Click "Continuar comprando" con retry — TM ignora silenciosamente
            # el click cuando hay bloqueo invisible (queue-it/captcha/sin sesión).
            # Reintentamos hasta 3 veces verificando cambio de URL.
            url_before = page.url
            continuar_ok = False
            for attempt in range(3):
                log(slot, "click_continuar", attempt=attempt + 1)
                try:
                    btn = page.get_by_role("button",
                            name=re.compile(r"^continuar comprando$", re.I)).first
                    btn.scroll_into_view_if_needed(timeout=4000)
                    human_pause(0.7, 1.4)
                    btn.click(timeout=10000, force=True)
                except Exception as e:
                    log(slot, "continuar_err", err=str(e)[:120])
                human_pause(8, 12)

                # Esperar que la navegación se estabilice antes de evaluate
                # (TM puede redirigir tras "Continuar" → context destroyed si
                # evaluamos durante la navegación).
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception: pass
                # Detectar bloqueos COMUNES tras el click — try/except por si
                # el contexto sigue cambiando.
                blockers = {}
                try:
                    blockers = page.evaluate(r"""() => {
                        const txt = (document.body && document.body.innerText || '').toLowerCase();
                        const out = {};
                        if (/queue-it|estamos comprobando|verifying you are human/i.test(txt)) out.queueit = true;
                        if (/press.{0,20}hold|mant[eé]n pulsado/i.test(txt)) out.press_hold = true;
                        if (document.querySelector('iframe[src*="recaptcha"], iframe[src*="captcha"]')) out.captcha = true;
                        if (/access denied|acceso denegado|forbidden/i.test(txt)) out.blocked = true;
                        return out;
                    }""")
                except Exception as _e:
                    log(slot, "blockers_eval_skipped", err=str(_e)[:80])
                    blockers = {}
                if blockers:
                    log(slot, "blockers_detected_post_continuar", **blockers)
                    shoot(slot, page, f"blocker_continuar_{attempt}")
                    if blockers.get("queueit"):
                        log(slot, "waiting_queue_it_max_3min")
                        # Esperar hasta 3 min a que queue-it termine
                        deadline_q = time.time() + 180
                        while time.time() < deadline_q:
                            page.wait_for_timeout(5000)
                            txt2 = page.evaluate("() => document.body.innerText.toLowerCase()")
                            if 'queue-it' not in txt2 and 'estamos comprobando' not in txt2:
                                log(slot, "queue_it_passed")
                                break

                # Si la URL ya no es la del evento, hemos avanzado
                u_after = page.url.lower()
                if u_after != url_before.lower() and "/event/" not in u_after:
                    continuar_ok = True
                    log(slot, "continuar_ok", new_url=u_after[:80])
                    break
                # Si seguimos en la misma página, reintenta
                log(slot, "continuar_no_redirect_retry", url=u_after[:80])
                human_pause(2, 4)

            if not continuar_ok:
                shoot(slot, page, "continuar_failed_final")
                log(slot, "FATAL_continuar_no_avanza",
                    note="TM no acepta click. Posible: sesión inválida, queue-it activo, NuData fingerprint")
                return 1

            # 10) OAuth login si redirige (segunda fase post-cart) con account-swap
            u = page.url.lower()
            log(slot, "post_continuar_url", url=u[:100])
            if any(k in u for k in ['auth.ticketmaster', 'identity', 'login', 'signin']):
                log(slot, "oauth_detected")
                login_res = login_with_account_swap(
                    slot, page, ctx, account, card, used_lock_path,
                    max_account_swaps=5)
                if not login_res.get("ok"):
                    log(slot, "FATAL_oauth_login_failed",
                        reason=login_res.get("reason"))
                    return 1
                if login_res.get("swaps", 0) > 0:
                    account = login_res.get("account", account)
                    card = login_res.get("card", card)
                human_pause(4, 6)

            # 10b) Pantalla intermedia "Partners del evento + Condiciones + Ir al pago"
            # Puede aparecer ANTES o DESPUÉS del OAuth. Reintentamos hasta 5 veces
            # mientras el watcher avanza al checkout final.
            for i in range(5):
                if handle_pre_checkout_step(slot, page):
                    break
                page.wait_for_timeout(2500)

            # 10) Watcher hasta checkout → fill_card → PAUSA
            log(slot, "watch_for_checkout")
            deadline = time.time() + 600
            prev = page.url
            filled = False
            while time.time() < deadline:
                time.sleep(2)
                try:
                    cur = page.url
                    if cur != prev:
                        log(slot, "URL_CHANGED", to=cur[:120])
                        prev = cur
                        page.wait_for_timeout(3000)
                        # Tras cada cambio de URL, intentar handler "Partners +
                        # Condiciones + Ir al pago" — puede aparecer en
                        # múltiples pantallas intermedias.
                        try:
                            handle_pre_checkout_step(slot, page)
                        except Exception: pass
                        u = cur.lower()
                        if any(k in u for k in ['identity', 'login', 'auth', 'signin', 'my.tick']):
                            log(slot, "auth_detected_mid")
                            login_res = login_with_account_swap(
                                slot, page, ctx, account, card,
                                used_lock_path, max_account_swaps=5)
                            if not login_res.get("ok"):
                                log(slot, "FATAL_mid_login_failed",
                                    reason=login_res.get("reason"))
                                return 1
                            if login_res.get("swaps", 0) > 0:
                                account = login_res.get("account", account)
                                card = login_res.get("card", card)
                            human_pause(3, 5)
                        u = page.url.lower()
                        if any(k in u for k in ['checkout', 'payment', 'cart', 'pago']) and not filled:
                            log(slot, "checkout_detected")
                            # Esperar a que iframes Adyen estén ready (suele tardar
                            # 2-5s) — el motivo principal de "no rellenó la tarjeta"
                            # antes era empezar el fill demasiado pronto.
                            for _ in range(8):
                                page.wait_for_timeout(800)
                                ready = page.evaluate(r"""() => {
                                    const ifs = document.querySelectorAll('iframe[src*="adyen" i], iframe[src*="checkout" i], iframe[name*="card" i]');
                                    return ifs.length > 0;
                                }""")
                                if ready:
                                    log(slot, "adyen_iframes_ready"); break
                            fill_card(slot, page, card, account)
                            # Verificar que rellenó al menos card_number; si no, reintentar.
                            verify = page.evaluate(r"""() => {
                                const all = [];
                                for (const f of [document, ...document.querySelectorAll('iframe')]) {
                                    try {
                                        const doc = f.contentDocument || f;
                                        doc.querySelectorAll('input').forEach(i => {
                                            if (i.value && i.value.length > 0) all.push({n: i.name, v: i.value.length});
                                        });
                                    } catch(e) {}
                                }
                                return all;
                            }""")
                            log(slot, "card_fields_filled", count=len(verify or []),
                                fields=[v.get("n") for v in (verify or [])][:8])
                            if not verify or len(verify) < 3:
                                log(slot, "card_fill_retry", reason="muy pocos campos rellenados")
                                page.wait_for_timeout(2500)
                                fill_card(slot, page, card, account)
                            filled = True
                            shoot(slot, page, "checkout_ready")
                            log(slot, "READY_FOR_USER", url=page.url)
                            print("\n" + "=" * 70)
                            print(f"✅ SLOT {slot}: tarjeta autorrellenada. NO he pulsado Pagar.")
                            print(f"URL: {page.url}")
                            print(f"Para cerrar: crear archivo _compra_parallel.STOP")
                            print("=" * 70 + "\n")
                            flag = os.path.join(PROYECTO, "_compra_parallel.STOP")
                            while not os.path.exists(flag):
                                time.sleep(5)
                            # Limpiar historial DENTRO del bloque sync_playwright
                            try:
                                import browser_cleanup
                                cdp_res = browser_cleanup.clear_history_via_cdp(ctx)
                                log(slot, "history_cleanup_inside", **cdp_res)
                            except Exception as _e:
                                log(slot, "history_cleanup_inside_err", err=str(_e)[:80])
                            log(slot, "stop_received"); return 0
                except Exception as e:
                    log(slot, "watch_err", err=str(e)[:80])
            log(slot, "TIMEOUT_no_checkout")
            # Limpiar historial al timeout (todavía dentro del with)
            try:
                import browser_cleanup
                browser_cleanup.clear_history_via_cdp(ctx)
                log(slot, "history_cleanup_timeout_ok")
            except Exception: pass

    finally:
        # ─── Solo cerrar perfil MoreLogin + locks (CDP cleanup ya hecho dentro) ───
        try:
            requests.post(f'{bo.MORELOGIN_API}/api/env/close',
                           json={'envId': prof['env_id']}, timeout=15)
            log(slot, "profile_closed")
        except Exception as e:
            log(slot, "profile_close_err", err=str(e)[:80])
        # Liberar locks
        _remove_active_profile(prof['profile'])
        try:
            with open(used_lock_path, encoding='utf-8') as f:
                ids = set(json.load(f))
            ids.discard(account.get('id'))
            with open(used_lock_path, 'w', encoding='utf-8') as f:
                json.dump(list(ids), f)
        except Exception: pass

    write_outcome(args.slot, {"status": "completed",
                                "step_reached": "checkout_ok",
                                "finished_at": datetime.now().isoformat(),
                                "rc": 0})
    return 0


if __name__ == '__main__':
    _slot = None
    try:
        # Pre-parse para tener slot disponible en outcome on crash
        for i, a in enumerate(sys.argv[1:], 1):
            if a == "--slot" and i < len(sys.argv) - 1:
                try: _slot = int(sys.argv[i+1])
                except: pass
                break
        rc = main()
    except SystemExit:
        raise
    except Exception as e:
        if _slot is not None:
            try:
                write_outcome(_slot, {"status": "fatal_exception",
                                        "step_reached": "unknown",
                                        "error": f"{type(e).__name__}: {str(e)[:200]}",
                                        "finished_at": datetime.now().isoformat(),
                                        "rc": 99})
            except Exception:
                pass
        raise
    sys.exit(rc)
