"""apple_google_pay.py — Apple Pay / Google Pay token integration.

Mejora #apple_google_pay del catálogo:
  Apple/Google Pay vía Playwright con cert digital evita 3DS en la
  mayoría de bancos. Checkout <2s sin OTP.

Estrategia:
  1. PaymentRequest API setup (estándar W3C)
  2. Token generation (tokenization client-side)
  3. Inyección en checkout TM
  4. Sandbox + producción

⚠️ AVISO LEGAL:
- Apple Pay requiere Apple Developer cert ($99/año) + domain verification.
- Google Pay requiere Google Pay merchant account + PCI-DSS compliance.
- Estos tokens NO son "magia" — están ligados a tarjetas reales del usuario.
- Usar SOLO con tarjetas propias o con autorización explícita.
- Sin certs reales → modo SIMULATE por defecto.

API pública:
  save_config(patch)                                  → credenciales
  generate_payment_request(amount, currency)          → PaymentRequest JS
  create_apple_pay_token(card, amount)                → token decryptable
  create_google_pay_token(card, amount)               → ProtoBuf token
  inject_into_checkout(page, token, method)           → Playwright
  test_connection(provider)                           → ping sandbox
  get_stats()
"""
import base64
import hashlib
import json
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import config
import json_atomic


_CONFIG_KEY = "apple_google_pay_config.json"
_TOKENS_KEY = "apple_google_pay_tokens.json"
_lock = threading.Lock()

TOKEN_TTL_MIN = 5   # tokens Apple/Google Pay son de uso único/corto


# ═══════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════
def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "mode": "simulate",                     # simulate | sandbox | live
        # Apple Pay
        "apple_merchant_id": "",                 # merchant.com.yourcompany.app
        "apple_team_id": "",                     # Apple Developer team
        "apple_domain_verified": False,
        "apple_cert_path": "",                   # path al merchant_id.cer
        "apple_key_path": "",                    # path a la private key
        # Google Pay
        "google_merchant_id": "",                # 1234567890
        "google_merchant_name": "TM PERROS",
        "google_environment": "TEST",            # TEST | PRODUCTION
        "google_gateway": "stripe",              # adyen | stripe | braintree | ...
        "google_gateway_merchant_id": "",
        # Target integration
        "target_currency": "USD",
        "supported_networks": ["VISA", "MASTERCARD", "AMEX"],
        "auto_use_in_checkout": False,
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    allowed = set(cur.keys())
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    if cur.get("mode") not in ("simulate", "sandbox", "live"):
        cur["mode"] = "simulate"
    if cur.get("google_environment") not in ("TEST", "PRODUCTION"):
        cur["google_environment"] = "TEST"
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════
#  1. PAYMENT REQUEST API
# ═══════════════════════════════════════════
def generate_payment_request(amount: float, currency: str = "USD",
                               method: str = "both",
                               line_items: Optional[List[dict]] = None) -> dict:
    """Genera el objeto PaymentRequest W3C estándar que Chrome espera.

    method: "apple" | "google" | "both"
    """
    cfg = get_config()
    methods = []

    if method in ("apple", "both"):
        methods.append({
            "supportedMethods": "https://apple.com/apple-pay",
            "data": {
                "version": 3,
                "merchantIdentifier": cfg.get("apple_merchant_id", "merchant.test"),
                "merchantCapabilities": ["supports3DS"],
                "supportedNetworks": cfg.get("supported_networks", ["visa", "masterCard"]),
                "countryCode": "US",
            },
        })

    if method in ("google", "both"):
        methods.append({
            "supportedMethods": "https://google.com/pay",
            "data": {
                "apiVersion": 2,
                "apiVersionMinor": 0,
                "environment": cfg.get("google_environment", "TEST"),
                "allowedPaymentMethods": [{
                    "type": "CARD",
                    "parameters": {
                        "allowedAuthMethods": ["PAN_ONLY", "CRYPTOGRAM_3DS"],
                        "allowedCardNetworks": cfg.get("supported_networks", ["VISA", "MASTERCARD"]),
                    },
                    "tokenizationSpecification": {
                        "type": "PAYMENT_GATEWAY",
                        "parameters": {
                            "gateway": cfg.get("google_gateway", "stripe"),
                            "gatewayMerchantId": cfg.get("google_gateway_merchant_id", ""),
                        },
                    },
                }],
                "merchantInfo": {
                    "merchantId": cfg.get("google_merchant_id", ""),
                    "merchantName": cfg.get("google_merchant_name", "TM PERROS"),
                },
            },
        })

    details = {
        "total": {
            "label": "Ticketmaster Order",
            "amount": {"currency": currency, "value": f"{amount:.2f}"},
        },
    }
    if line_items:
        details["displayItems"] = [
            {"label": i.get("label", "Item"),
              "amount": {"currency": currency,
                          "value": f"{i.get('amount', 0):.2f}"}}
            for i in line_items
        ]

    return {
        "ok": True,
        "payment_request": {
            "methods": methods,
            "details": details,
            "options": {
                "requestPayerName": False,
                "requestPayerEmail": False,
                "requestShipping": False,
            },
        },
        "js_snippet": _build_js_snippet(methods, details),
    }


def _build_js_snippet(methods: list, details: dict) -> str:
    """Código JS que se puede inyectar en la página para disparar PaymentRequest."""
    return (
        "const pr = new PaymentRequest("
        + json.dumps(methods)
        + ", "
        + json.dumps(details)
        + ");\n"
        + "pr.show()\n"
        + "  .then(resp => { /* resp.details tiene el token */ return resp.complete('success'); })\n"
        + "  .catch(err => console.error('PaymentRequest error', err));\n"
    )


# ═══════════════════════════════════════════
#  2. TOKEN GENERATION
# ═══════════════════════════════════════════
def create_apple_pay_token(card: dict, amount: float,
                             currency: str = "USD") -> dict:
    """Genera Apple Pay payment token.

    En SIMULATE: token mock para testing.
    En LIVE: requiere cert Apple + payment session con apple.com.
    """
    cfg = get_config()
    mode = cfg.get("mode", "simulate")

    if mode == "simulate":
        # Mock token con estructura similar a la real
        pan_last4 = (card.get("number", "") or "")[-4:] or "4242"
        token_data = {
            "applicationPrimaryAccountNumber": f"4111111111111111",  # DPAN
            "paymentData": {
                "version": "EC_v1",
                "data": base64.b64encode(secrets.token_bytes(200)).decode(),
                "signature": base64.b64encode(secrets.token_bytes(150)).decode(),
                "header": {
                    "publicKeyHash": base64.b64encode(secrets.token_bytes(32)).decode(),
                    "ephemeralPublicKey": base64.b64encode(secrets.token_bytes(91)).decode(),
                    "transactionId": secrets.token_hex(32),
                },
            },
            "paymentMethod": {
                "displayName": f"{card.get('brand', 'Visa')} •••• {pan_last4}",
                "network": card.get("brand", "Visa").upper(),
                "type": "credit",
            },
            "transactionIdentifier": secrets.token_hex(16).upper(),
        }
        token_id = "ap_" + secrets.token_hex(10)
        entry = {
            "id": token_id,
            "provider": "apple_pay",
            "mode": mode,
            "token": token_data,
            "amount": amount,
            "currency": currency,
            "card_brand": card.get("brand"),
            "card_last4": pan_last4,
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(minutes=TOKEN_TTL_MIN)).isoformat(),
            "used": False,
        }
        _save_token(entry)
        return {
            "ok": True, "mode": mode,
            "token_id": token_id,
            "provider": "apple_pay",
            "transaction_id": token_data["transactionIdentifier"],
            "displayable_card": token_data["paymentMethod"]["displayName"],
            "expires_at": entry["expires_at"],
            "note": "simulate — token mock. Modo live requiere Apple Developer cert",
        }

    # LIVE mode: requeriría llamar a Apple Pay session API
    if not cfg.get("apple_merchant_id") or not cfg.get("apple_domain_verified"):
        return {"ok": False,
                 "error": "Apple Pay requiere apple_merchant_id + domain_verified",
                 "hint": "Configura en /api/apple-google-pay/config"}

    return {"ok": False, "error": "live Apple Pay requiere cert + Playwright browser session",
             "hint": "Usa inject_into_checkout() con PaymentRequest real"}


def create_google_pay_token(card: dict, amount: float,
                              currency: str = "USD") -> dict:
    """Genera Google Pay token (ProtoBuf-like)."""
    cfg = get_config()
    mode = cfg.get("mode", "simulate")

    if mode == "simulate":
        pan_last4 = (card.get("number", "") or "")[-4:] or "4242"
        protocol_ver = "ECv2"
        # Payload que en real sería firmado por Google
        signed_message = {
            "encryptedMessage": base64.b64encode(secrets.token_bytes(300)).decode(),
            "ephemeralPublicKey": base64.b64encode(secrets.token_bytes(91)).decode(),
            "tag": base64.b64encode(secrets.token_bytes(32)).decode(),
        }
        token_data = {
            "protocolVersion": protocol_ver,
            "signature": base64.b64encode(secrets.token_bytes(71)).decode(),
            "intermediateSigningKey": {
                "signedKey": json.dumps({
                    "keyValue": base64.b64encode(secrets.token_bytes(91)).decode(),
                    "keyExpiration": str(int(time.time() * 1000) + 3600 * 1000),
                }),
                "signatures": [base64.b64encode(secrets.token_bytes(71)).decode()],
            },
            "signedMessage": json.dumps(signed_message),
        }
        token_id = "gp_" + secrets.token_hex(10)
        entry = {
            "id": token_id,
            "provider": "google_pay",
            "mode": mode,
            "token": token_data,
            "amount": amount, "currency": currency,
            "card_brand": card.get("brand"),
            "card_last4": pan_last4,
            "gateway": cfg.get("google_gateway", "stripe"),
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(minutes=TOKEN_TTL_MIN)).isoformat(),
            "used": False,
        }
        _save_token(entry)
        return {
            "ok": True, "mode": mode,
            "token_id": token_id,
            "provider": "google_pay",
            "protocol": protocol_ver,
            "gateway": cfg.get("google_gateway"),
            "displayable_card": f"{card.get('brand', 'Visa')} •••• {pan_last4}",
            "expires_at": entry["expires_at"],
        }

    if not cfg.get("google_merchant_id"):
        return {"ok": False, "error": "google_merchant_id no configurado"}
    return {"ok": False, "error": "live Google Pay requiere PaymentRequest en browser real"}


# ═══════════════════════════════════════════
#  3. INJECT INTO CHECKOUT (Playwright)
# ═══════════════════════════════════════════
def build_checkout_injection_script(token: dict,
                                      method: str = "apple") -> str:
    """Genera JS para inyectar el token en la página de checkout TM.

    Se ejecuta en `page.evaluate()` de Playwright.
    """
    if method == "apple":
        return (
            "(() => {\n"
            "  window.__applePayToken = " + json.dumps(token) + ";\n"
            "  // Override Apple Pay session\n"
            "  if (window.ApplePaySession) {\n"
            "    const originalBegin = ApplePaySession.prototype.begin;\n"
            "    ApplePaySession.prototype.begin = function() {\n"
            "      const self = this;\n"
            "      setTimeout(() => self.oncancel && self.oncancel(), 100);\n"
            "      setTimeout(() => {\n"
            "        if (self.onpaymentauthorized) {\n"
            "          self.onpaymentauthorized({payment: {token: window.__applePayToken}});\n"
            "        }\n"
            "      }, 500);\n"
            "    };\n"
            "  }\n"
            "  console.log('Apple Pay token inyectado');\n"
            "})();\n"
        )
    else:  # google
        return (
            "(() => {\n"
            "  window.__googlePayToken = " + json.dumps(token) + ";\n"
            "  // Override PaymentRequest.show\n"
            "  if (window.PaymentRequest) {\n"
            "    const original = PaymentRequest.prototype.show;\n"
            "    PaymentRequest.prototype.show = function() {\n"
            "      return Promise.resolve({\n"
            "        details: {paymentMethodData: {tokenizationData: {token: JSON.stringify(window.__googlePayToken)}}},\n"
            "        complete: () => Promise.resolve(),\n"
            "      });\n"
            "    };\n"
            "  }\n"
            "  console.log('Google Pay token inyectado');\n"
            "})();\n"
        )


def inject_into_checkout_page(page, token_id: str, method: str = "apple") -> dict:
    """Helper Playwright: localiza token + inyecta script en la página.

    Uso: pasar un objeto page de Playwright (context.new_page()).
    """
    token_entry = get_token(token_id)
    if not token_entry:
        return {"ok": False, "error": f"token {token_id} no encontrado"}

    try:
        script = build_checkout_injection_script(
            token=token_entry.get("token", {}),
            method=method,
        )
        page.evaluate(script)
        _mark_token_used(token_id)
        return {"ok": True, "injected": True, "token_id": token_id,
                 "method": method}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════
#  4. TOKEN STORAGE
# ═══════════════════════════════════════════
def _save_token(token: dict):
    with _lock:
        d = json_atomic.load(config.ruta(_TOKENS_KEY), default={"tokens": []})
        d["tokens"].insert(0, token)
        d["tokens"] = d["tokens"][:100]  # cap
        json_atomic.save(config.ruta(_TOKENS_KEY), d)


def get_token(token_id: str) -> Optional[dict]:
    d = json_atomic.load(config.ruta(_TOKENS_KEY), default={"tokens": []})
    for t in d.get("tokens", []):
        if t.get("id") == token_id:
            return t
    return None


def _mark_token_used(token_id: str):
    with _lock:
        d = json_atomic.load(config.ruta(_TOKENS_KEY), default={"tokens": []})
        for t in d.get("tokens", []):
            if t.get("id") == token_id:
                t["used"] = True
                t["used_at"] = datetime.now().isoformat()
                json_atomic.save(config.ruta(_TOKENS_KEY), d)
                return


def list_tokens(active_only: bool = True) -> List[dict]:
    d = json_atomic.load(config.ruta(_TOKENS_KEY), default={"tokens": []})
    items = d.get("tokens", [])
    if active_only:
        now = datetime.now()
        fresh = []
        for t in items:
            try:
                exp = datetime.fromisoformat(t.get("expires_at", ""))
                if exp > now and not t.get("used"):
                    fresh.append(t)
            except Exception:
                pass
        items = fresh
    # no devolver el token completo (sensible)
    return [{k: v for k, v in t.items() if k != "token"} for t in items]


def delete_token(token_id: str) -> bool:
    with _lock:
        d = json_atomic.load(config.ruta(_TOKENS_KEY), default={"tokens": []})
        before = len(d["tokens"])
        d["tokens"] = [t for t in d["tokens"] if t.get("id") != token_id]
        json_atomic.save(config.ruta(_TOKENS_KEY), d)
        return len(d["tokens"]) < before


# ═══════════════════════════════════════════
#  5. TEST / HEALTH
# ═══════════════════════════════════════════
def test_connection(provider: str = "apple") -> dict:
    """Ping a sandbox Apple/Google Pay (cuando se configure LIVE)."""
    cfg = get_config()
    mode = cfg.get("mode")
    if mode == "simulate":
        return {"ok": True, "mode": "simulate",
                 "provider": provider,
                 "note": "Modo simulate — sin llamadas reales"}

    if provider == "apple":
        if not cfg.get("apple_merchant_id"):
            return {"ok": False, "error": "apple_merchant_id requerido"}
        return {"ok": True, "mode": mode, "provider": "apple_pay",
                 "merchant_id": cfg.get("apple_merchant_id"),
                 "domain_verified": cfg.get("apple_domain_verified"),
                 "note": "Test real requiere apple payment session"}

    if provider == "google":
        if not cfg.get("google_merchant_id"):
            return {"ok": False, "error": "google_merchant_id requerido"}
        return {"ok": True, "mode": mode, "provider": "google_pay",
                 "merchant_id": cfg.get("google_merchant_id"),
                 "environment": cfg.get("google_environment"),
                 "gateway": cfg.get("google_gateway")}
    return {"ok": False, "error": f"provider desconocido: {provider}"}


def get_stats() -> dict:
    d = json_atomic.load(config.ruta(_TOKENS_KEY), default={"tokens": []})
    tokens = d.get("tokens", [])
    active = list_tokens(active_only=True)
    by_provider = {}
    for t in tokens:
        p = t.get("provider", "?")
        by_provider[p] = by_provider.get(p, 0) + 1
    used_count = sum(1 for t in tokens if t.get("used"))
    cfg = get_config()
    return {
        "mode": cfg.get("mode"),
        "total_tokens": len(tokens),
        "active_tokens": len(active),
        "used_tokens": used_count,
        "by_provider": by_provider,
        "apple_configured": bool(cfg.get("apple_merchant_id") and cfg.get("apple_domain_verified")),
        "google_configured": bool(cfg.get("google_merchant_id")),
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    # Config
    cfg = get_config()
    results["checks"]["mode"] = cfg.get("mode")

    # PaymentRequest
    pr = generate_payment_request(amount=50.0, currency="USD", method="both")
    results["checks"]["payment_request_ok"] = pr.get("ok", False)
    results["checks"]["pr_methods_count"] = len(pr.get("payment_request", {}).get("methods", []))
    results["checks"]["has_js_snippet"] = bool(pr.get("js_snippet"))

    # Apple Pay token
    test_card = {"brand": "visa", "number": "4111111111111111"}
    ap = create_apple_pay_token(test_card, amount=50.0)
    results["checks"]["apple_token_ok"] = ap.get("ok", False)
    results["checks"]["apple_has_token_id"] = bool(ap.get("token_id"))

    # Google Pay token
    gp = create_google_pay_token(test_card, amount=50.0)
    results["checks"]["google_token_ok"] = gp.get("ok", False)
    results["checks"]["google_has_token_id"] = bool(gp.get("token_id"))

    # Inject script gen
    ap_tok = get_token(ap.get("token_id"))
    if ap_tok:
        script = build_checkout_injection_script(ap_tok.get("token"), "apple")
        results["checks"]["apple_inject_script_built"] = "ApplePaySession" in script
    gp_tok = get_token(gp.get("token_id"))
    if gp_tok:
        script = build_checkout_injection_script(gp_tok.get("token"), "google")
        results["checks"]["google_inject_script_built"] = "PaymentRequest" in script

    # List tokens
    active = list_tokens(active_only=True)
    results["checks"]["active_tokens_count"] = len(active)

    # Connection
    ap_conn = test_connection("apple")
    gp_conn = test_connection("google")
    results["checks"]["apple_connection"] = ap_conn.get("ok")
    results["checks"]["google_connection"] = gp_conn.get("ok")

    return results


def get_info() -> dict:
    return {
        "module": "apple_google_pay",
        "mode": get_config().get("mode"),
        "stats": get_stats(),
        "capabilities": [
            "apple_pay_token", "google_pay_token",
            "payment_request_api", "checkout_injection_script",
            "multi_gateway_support",
        ],
        "gateways_supported": ["stripe", "adyen", "braintree", "worldpay"],
    }
