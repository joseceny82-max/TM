"""signup_form_helpers.py — Helpers robustos para el formulario de TM signup.

Soluciona 3 gaps:
  1. SMS OTP auto-inject: inyecta el código SMS en el campo OTP del DOM en cuanto llega
  2. Fuzzy field detection: busca campos por intent (name|id|autocomplete|placeholder|aria|label)
     para que aguante cambios de DOM por parte de TM
  3. 3DS auto-confirm: detecta flujo 3DS y maneja iframe/redirect/silent auth

Diseñado para ejecutarse via CDP raw (no Playwright) para máxima flexibilidad.

API:
  inject_otp_via_cdp(ws, code, selector_hint=None) → True/False
  find_field_fuzzy(page_fields, intent) → selector + score (0-100)
  build_field_map(page_fields) → dict {intent: selector}
  detect_3ds_iframe(html) → {has_3ds, iframe_url, type}
  monitor_sms_and_inject(provider_order_id, ws, account_id, account_label, max_wait_s=300)
  self_test()
"""
from __future__ import annotations
import re, time, json, threading, asyncio
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════
# 1. FUZZY FIELD DETECTION
# ═══════════════════════════════════════════════════════════════
# Patrones que matchean el "intent" semántico de un campo
INTENT_PATTERNS = {
    "email": {
        "name": ["email", "e_mail", "mail", "username"],
        "type": ["email"],
        "autocomplete": ["email", "username"],
        "placeholder": ["email", "correo", "@"],
        "aria": ["email", "correo"],
    },
    "password": {
        "name": ["password", "pass", "pwd", "contraseña", "secret"],
        "type": ["password"],
        "autocomplete": ["new-password", "current-password"],
        "placeholder": ["password", "contraseña", "pass"],
        "aria": ["password", "contraseña"],
    },
    "first_name": {
        "name": ["firstname", "first_name", "first-name", "fname", "given_name", "givenname", "nombre"],
        "autocomplete": ["given-name", "first-name"],
        "placeholder": ["first name", "nombre", "given name"],
        "aria": ["nombre", "first name"],
    },
    "last_name": {
        "name": ["lastname", "last_name", "last-name", "lname", "family_name", "familyname", "surname", "apellido"],
        "autocomplete": ["family-name", "last-name", "surname"],
        "placeholder": ["last name", "apellido", "surname"],
        "aria": ["apellido", "last name"],
    },
    "dob": {
        "name": ["dob", "birth", "birthdate", "dateofbirth", "birthday", "fecha"],
        "type": ["date"],
        "autocomplete": ["bday", "bday-day", "bday-month", "bday-year"],
        "placeholder": ["birth", "nacimiento", "dd/mm"],
        "aria": ["birth", "nacimiento"],
    },
    "phone": {
        "name": ["phone", "tel", "mobile", "telefono"],
        "type": ["tel"],
        "autocomplete": ["tel", "tel-national"],
        "placeholder": ["phone", "tel", "móvil", "teléfono"],
        "aria": ["phone", "teléfono"],
    },
    "address1": {
        "name": ["address", "address1", "addr", "addr1", "street"],
        "autocomplete": ["address-line1", "street-address"],
        "placeholder": ["address", "calle", "dirección"],
        "aria": ["address", "dirección"],
    },
    "city": {
        "name": ["city", "ciudad", "town", "locality"],
        "autocomplete": ["address-level2"],
        "placeholder": ["city", "ciudad", "localidad"],
        "aria": ["city", "ciudad"],
    },
    "zip": {
        "name": ["zip", "zipcode", "postal", "postalcode", "cp"],
        "autocomplete": ["postal-code"],
        "placeholder": ["zip", "postal", "cp", "código postal"],
        "aria": ["postal", "código postal"],
    },
    "country": {
        "name": ["country", "pais", "nationality"],
        "autocomplete": ["country", "country-name"],
        "placeholder": ["country", "país"],
        "aria": ["country", "país"],
    },
    "otp": {
        "name": ["otp", "code", "verification", "verifycode", "token", "pin", "confirmation"],
        "autocomplete": ["one-time-code", "otp"],
        "placeholder": ["code", "código", "otp", "verification", "pin"],
        "aria": ["code", "código", "verification"],
        "id_substring": ["otp", "code", "verif", "pin"],
    },
}


def _score_field(field: Dict[str, Any], intent: str) -> int:
    """Calcula score 0-100 de cuánto matchea field con el intent."""
    if intent not in INTENT_PATTERNS:
        return 0
    patterns = INTENT_PATTERNS[intent]
    score = 0

    # type match (más específico)
    f_type = (field.get("type") or "").lower()
    if "type" in patterns and f_type in patterns["type"]:
        score += 50

    # name match
    f_name = (field.get("name") or "").lower()
    for p in patterns.get("name", []):
        if p in f_name: score += 30; break

    # autocomplete match
    f_ac = (field.get("autocomplete") or "").lower()
    for p in patterns.get("autocomplete", []):
        if p in f_ac: score += 25; break

    # placeholder match
    f_ph = (field.get("placeholder") or "").lower()
    for p in patterns.get("placeholder", []):
        if p in f_ph: score += 15; break

    # aria-label
    f_aria = (field.get("aria_label") or "").lower()
    for p in patterns.get("aria", []):
        if p in f_aria: score += 10; break

    # id substring (para OTP)
    f_id = (field.get("id") or "").lower()
    for p in patterns.get("id_substring", patterns.get("name", [])):
        if p in f_id: score += 5; break

    return min(score, 100)


def find_field_fuzzy(fields: List[Dict[str, Any]], intent: str,
                     min_score: int = 30) -> Optional[Dict[str, Any]]:
    """Devuelve el field con mayor score para el intent dado.

    fields = lista de dicts con keys: name, type, id, autocomplete, placeholder, aria_label
    """
    if not fields: return None
    scored = [(field, _score_field(field, intent)) for field in fields]
    scored = [(f, s) for f, s in scored if s >= min_score]
    if not scored: return None
    scored.sort(key=lambda x: x[1], reverse=True)
    best, score = scored[0]
    return {**best, "_score": score, "_intent": intent}


def build_field_map(fields: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Mapea cada intent → mejor campo encontrado."""
    out = {}
    for intent in INTENT_PATTERNS:
        m = find_field_fuzzy(fields, intent)
        if m: out[intent] = m
    return out


def selector_for_field(field: Dict[str, Any]) -> str:
    """Genera el mejor selector CSS para localizar el campo."""
    if field.get("id"):
        return f'#{field["id"]}'
    if field.get("name"):
        return f'input[name="{field["name"]}"]'
    if field.get("autocomplete"):
        return f'input[autocomplete="{field["autocomplete"]}"]'
    return f'input[type="{field.get("type","text")}"]'


# ═══════════════════════════════════════════════════════════════
# 2. SMS OTP AUTO-INJECT VIA CDP
# ═══════════════════════════════════════════════════════════════
async def inject_otp_via_cdp(ws, code: str,
                              selector_hint: Optional[str] = None) -> Dict[str, Any]:
    """Inyecta el OTP en el campo de verificación del DOM via CDP.

    ws: websocket connection ya abierta a /devtools/page/<id>
    code: el código SMS recibido (ej "123456")
    selector_hint: si lo conoces, pásalo (ej 'input[name="otp"]')
    """
    rid = [int(time.time() * 1000)]
    async def s(method, params=None):
        rid[0] += 1
        await ws.send(json.dumps({"id": rid[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == rid[0]:
                # CDP devuelve {error: {...}} en caso de fallo, sin "result"
                if "error" in msg:
                    return {"_cdp_error": msg.get("error")}
                return msg.get("result") or {}

    # 1. Localizar campo OTP via fuzzy
    if not selector_hint:
        # Pedir al DOM la lista de inputs visibles
        r = await s("Runtime.evaluate", {
            "expression": '''(()=>{
                return [...document.querySelectorAll("input")]
                    .filter(i => i.type !== "hidden" && i.getBoundingClientRect().width > 0)
                    .map(i => ({
                        name: i.name, type: i.type, id: i.id,
                        autocomplete: i.autocomplete,
                        placeholder: i.placeholder,
                        aria_label: i.getAttribute("aria-label") || ""
                    }));
            })()''',
            "returnByValue": True,
        })
        if r.get("_cdp_error"):
            return {"ok": False, "error": "cdp_error",
                    "details": r.get("_cdp_error")}
        fields = r.get("result", {}).get("value", []) if isinstance(r, dict) else []
        otp_field = find_field_fuzzy(fields, "otp", min_score=20)
        if not otp_field:
            return {"ok": False, "error": "no_otp_field_found", "fields_checked": len(fields)}
        selector_hint = selector_for_field(otp_field)

    # 2. Inyectar el código vía native setter + eventos
    inject_result = await s("Runtime.evaluate", {
        "expression": f'''(()=>{{
            const el = document.querySelector({json.dumps(selector_hint)});
            if (!el) return {{ok: false, error: "selector_not_found"}};
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, "value").set;
            el.scrollIntoView({{block:"center"}});
            el.focus();
            setter.call(el, {json.dumps(code)});
            el.dispatchEvent(new Event("input", {{bubbles: true}}));
            el.dispatchEvent(new Event("change", {{bubbles: true}}));
            el.dispatchEvent(new KeyboardEvent("keyup", {{bubbles: true,
                key: {json.dumps(code[-1])}}}));
            return {{ok: true, len: el.value.length, selector: {json.dumps(selector_hint)}}};
        }})()''',
        "returnByValue": True,
    })
    return inject_result.get("result", {}).get("value", {"ok": False})


def monitor_sms_and_inject_sync(order_id: str, ws_url: str,
                                  max_wait_s: int = 300,
                                  account_label: str = "") -> Dict[str, Any]:
    """Monitor síncrono — polea SMS receiver hasta recibir code y lo inyecta.

    Útil cuando se ejecuta en thread separado del wizard principal.
    """
    import requests

    # Detectar provider del order_id consultando sms_receiver
    try:
        import sms_receiver
        order_provider = None
        for o in sms_receiver.list_orders():
            if str(o.get("id")) == str(order_id):
                order_provider = o.get("provider"); break
    except Exception:
        order_provider = None

    start = time.time()
    last_check = 0
    code_received = None

    while (time.time() - start) < max_wait_s:
        # Poll cada 5s
        if time.time() - last_check < 5:
            time.sleep(1); continue
        last_check = time.time()

        try:
            r = sms_receiver.check_sms(order_id, order_provider)
            codes = r.get("codes_detected") or r.get("codes") or []
            if codes:
                code_received = codes[0]
                break
        except Exception:
            pass

    if not code_received:
        return {"ok": False, "error": "timeout", "waited_s": int(time.time() - start)}

    # Inyectar código vía CDP en el browser
    async def _inject():
        import websockets as ws_lib
        async with ws_lib.connect(ws_url, max_size=10*1024*1024) as ws:
            return await inject_otp_via_cdp(ws, code_received)

    try:
        result = asyncio.run(_inject())
        result["code"] = code_received
        result["account_label"] = account_label
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "code": code_received}


# ═══════════════════════════════════════════════════════════════
# 3. 3DS AUTO-DETECTOR
# ═══════════════════════════════════════════════════════════════
def detect_3ds_iframe(html: str) -> Dict[str, Any]:
    """Detecta si la página actual contiene un challenge 3DS y extrae detalles."""
    h = (html or "").lower()
    flags = {
        "has_3ds_iframe": False,
        "iframe_url": None,
        "type": "none",
        "needs_otp": False,
        "redirect_required": False,
    }

    # Buscar iframe 3DS
    iframe_match = re.search(
        r'<iframe[^>]*src=["\'](?P<url>[^"\']*(?:3ds|adyen|secure|threeds|pareq)[^"\']*)["\']',
        html or "", re.IGNORECASE)
    if iframe_match:
        flags["has_3ds_iframe"] = True
        flags["iframe_url"] = iframe_match.group("url")
        flags["type"] = "challenge_iframe"

    # Frictionless silent auth
    if "transstatus=y" in h or "frictionless" in h:
        flags["type"] = "frictionless_silent"
        flags["needs_otp"] = False

    # Challenge tipo OTP
    if any(x in h for x in ["enter your code", "introduce el código",
                              "enter otp", "verify your identity",
                              "código de verificación", "verification code"]):
        flags["type"] = "otp_challenge"
        flags["needs_otp"] = True

    # Redirect a banco
    if any(x in h for x in ["redirecting to your bank", "redirigiendo a tu banco",
                              "<form name='downloadform'", "auto submit form"]):
        flags["type"] = "bank_redirect"
        flags["redirect_required"] = True

    # PaReq form (3DS 1.0 fallback)
    if "name=\"pareq\"" in h or "name='pareq'" in h:
        flags["type"] = "3ds1_pareq"
        flags["redirect_required"] = True

    return flags


async def auto_handle_3ds(ws, max_wait_s: int = 60) -> Dict[str, Any]:
    """Intenta manejar automáticamente el flujo 3DS:
       - Frictionless: continuar sin acción
       - Challenge OTP: esperar e inyectar (requiere SMS receiver activo)
       - Redirect: dejar que cargue
    """
    rid = [int(time.time() * 1000)]
    async def s(method, params=None):
        rid[0] += 1
        await ws.send(json.dumps({"id": rid[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == rid[0]: return msg.get("result")

    # Capturar HTML actual
    r = await s("Runtime.evaluate", {
        "expression": "document.documentElement.outerHTML.substring(0, 50000)",
        "returnByValue": True,
    })
    html = r.get("result", {}).get("value", "")
    detection = detect_3ds_iframe(html)

    if detection["type"] == "frictionless_silent":
        return {"ok": True, "action": "wait_continue", "detection": detection}

    if detection["type"] == "otp_challenge":
        return {"ok": False, "action": "needs_sms_otp",
                "detection": detection, "note": "Llama a monitor_sms_and_inject"}

    if detection["redirect_required"]:
        # Esperar a que el redirect termine
        await asyncio.sleep(3)
        # Re-evaluar
        r2 = await s("Runtime.evaluate", {
            "expression": "window.location.href", "returnByValue": True})
        return {"ok": True, "action": "redirected",
                "current_url": r2.get("result", {}).get("value"),
                "detection": detection}

    return {"ok": True, "action": "no_3ds", "detection": detection}


# ═══════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. INTENT_PATTERNS completo
    checks["has_email_intent"] = "email" in INTENT_PATTERNS
    checks["has_password_intent"] = "password" in INTENT_PATTERNS
    checks["has_otp_intent"] = "otp" in INTENT_PATTERNS
    checks["intents_count"] = len(INTENT_PATTERNS) >= 10

    # 2. _score_field — type match
    f = {"type": "email", "name": "user_email"}
    s = _score_field(f, "email")
    checks["score_email_match"] = s >= 50

    f = {"type": "text", "name": "firstName", "autocomplete": "given-name"}
    s = _score_field(f, "first_name")
    checks["score_first_name_high"] = s >= 50

    f = {"type": "text", "name": "color"}
    s = _score_field(f, "email")
    checks["score_no_match_low"] = s < 30

    # 3. find_field_fuzzy
    fields = [
        {"name": "color", "type": "text"},
        {"name": "email_address", "type": "email"},
        {"name": "firstName", "type": "text", "autocomplete": "given-name"},
    ]
    r = find_field_fuzzy(fields, "email")
    checks["fuzzy_finds_email"] = r is not None and "email" in (r.get("name") or "")
    r = find_field_fuzzy(fields, "first_name")
    checks["fuzzy_finds_firstname"] = r is not None and r.get("name") == "firstName"
    r = find_field_fuzzy(fields, "phone")
    checks["fuzzy_no_match_returns_none"] = r is None

    # 4. build_field_map
    fields_full = [
        {"name": "email", "type": "email"},
        {"name": "password", "type": "password"},
        {"name": "firstName", "type": "text"},
        {"name": "lastName", "type": "text"},
        {"name": "postalCode", "type": "text", "autocomplete": "postal-code"},
        {"name": "otp_code", "type": "text", "autocomplete": "one-time-code"},
    ]
    fmap = build_field_map(fields_full)
    checks["map_has_email"] = "email" in fmap
    checks["map_has_password"] = "password" in fmap
    checks["map_has_first_name"] = "first_name" in fmap
    checks["map_has_zip"] = "zip" in fmap
    checks["map_has_otp"] = "otp" in fmap

    # 5. selector_for_field
    sel = selector_for_field({"id": "user-email", "name": "email"})
    checks["selector_prefers_id"] = sel == "#user-email"
    sel = selector_for_field({"name": "firstName"})
    checks["selector_uses_name"] = "firstName" in sel

    # 6. detect_3ds_iframe
    r = detect_3ds_iframe('<iframe src="https://bank.example.com/3ds/auth"></iframe>')
    checks["3ds_detect_iframe"] = r["has_3ds_iframe"] is True
    r = detect_3ds_iframe('TransStatus=Y frictionless flow')
    checks["3ds_detect_frictionless"] = r["type"] == "frictionless_silent"
    r = detect_3ds_iframe('Please enter your code from SMS')
    checks["3ds_detect_otp"] = r["type"] == "otp_challenge"
    r = detect_3ds_iframe('totally different content')
    checks["3ds_detect_none"] = r["type"] == "none"

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {"module": "signup_form_helpers", "version": 1,
            "intents_supported": list(INTENT_PATTERNS.keys())}
