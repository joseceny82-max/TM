"""adyen_tokenizer.py — Pre-tokenización de tarjetas Adyen para checkout < 2s.

Mejora #adyen_token de la nota:
  Pre-tokenizar tarjeta en Adyen antes del drop → checkout en <2s.
  Pasos:
  1. Cargar Adyen JS
  2. Enviar tarjeta a Adyen Checkout API
  3. Obtener token de pago
  4. Cache y usar en checkout final

⚠️ AVISO LEGAL:
- Adyen pertenece al titular de la tarjeta. Tokenizar tarjetas ajenas sin
  autorización puede constituir fraude. Usar SOLO con tarjetas propias
  o con consentimiento explícito.
- La tokenización de Adyen devuelve un `recurringDetailReference` o
  `storedPaymentMethodId` que permite cobrar sin el PAN.
- Este módulo usa endpoint de **TEST** (`checkout-test.adyen.com`) por
  defecto. El modo LIVE requiere API key de producción y cumplir PCI-DSS
  mediante Adyen Web Drop-in (el PAN nunca debe tocar este servidor —
  usa el componente cliente de Adyen para enviar directo).

API pública:
  save_config(api_key, merchant_account, mode)
  tokenize_card(card_data)  → {token_id, brand, last4, expires_at}
  get_token(token_id)  → detalles sin PAN
  list_tokens(), delete_token(tid)
  check_connection() → /paymentMethods test
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import config
import json_atomic


_CONFIG_KEY = "adyen_config.json"
_TOKENS_KEY = "adyen_tokens.json"
_lock = threading.Lock()

TEST_BASE = "https://checkout-test.adyen.com/v71"
LIVE_BASE_TEMPLATE = "https://{prefix}-checkout-live.adyenpayments.com/checkout/v71"

# TTL del token tokenizado (config Adyen)
TOKEN_TTL_DAYS = 395  # ~13 meses, límite Adyen estándar


def _config_path():
    return config.ruta(_CONFIG_KEY)


def _tokens_path():
    return config.ruta(_TOKENS_KEY)


def get_config() -> dict:
    return json_atomic.load(_config_path(), default={
        "api_key": "",                 # Adyen API key (X-API-Key header)
        "merchant_account": "",        # ej: "YourMerchantAccountTest"
        "live_url_prefix": "",         # para modo live: algo como "1797a841fbb37ca7-XXX"
        "client_key": "",              # pública (para Adyen.js en UI)
        "mode": "simulate",            # simulate | test | live
        "default_shopper_country": "ES",
        "default_shopper_locale": "es_ES",
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    allowed = {"api_key", "merchant_account", "live_url_prefix",
               "client_key", "mode", "default_shopper_country", "default_shopper_locale"}
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    if cur.get("mode") not in ("simulate", "test", "live"):
        cur["mode"] = "simulate"
    json_atomic.save(_config_path(), cur)
    return cur


def _safe_card(card: dict) -> dict:
    """Oculta el PAN para respuestas."""
    num = card.get("number", "") or ""
    masked = ("•" * max(0, len(num) - 4)) + num[-4:] if num else ""
    return {
        "last4": num[-4:] if num else "",
        "brand": card.get("brand", _detect_brand(num)),
        "masked": masked,
        "holder": card.get("holder_name", ""),
        "exp_month": card.get("expiry_month", ""),
        "exp_year": card.get("expiry_year", ""),
    }


def _detect_brand(num: str) -> str:
    n = (num or "").replace(" ", "").replace("-", "")
    if not n:
        return ""
    if n.startswith("4"):
        return "visa"
    if n[:2] in ("51", "52", "53", "54", "55") or (2221 <= int(n[:4] or "0") <= 2720):
        return "mc"
    if n[:2] in ("34", "37"):
        return "amex"
    if n[:2] == "62":
        return "unionpay"
    if n[:4] == "6011" or n[:2] == "65":
        return "discover"
    return "unknown"


def _get_base_url() -> str:
    cfg = get_config()
    if cfg.get("mode") == "live" and cfg.get("live_url_prefix"):
        return LIVE_BASE_TEMPLATE.format(prefix=cfg["live_url_prefix"])
    return TEST_BASE


# ═══════════════════════════════════════════
#  TOKENIZE
# ═══════════════════════════════════════════
def tokenize_card(card_data: dict, shopper_reference: Optional[str] = None) -> dict:
    """Tokeniza una tarjeta y devuelve reference reutilizable.

    card_data = {
        number, expiry_month, expiry_year, cvc, holder_name
    }
    shopper_reference = identificador estable del usuario (para stored methods).
    """
    cfg = get_config()
    mode = cfg.get("mode", "simulate")

    card = _safe_card(card_data)
    now = datetime.now()
    shopper = shopper_reference or f"TM-{int(time.time())}"

    if mode == "simulate":
        # Genera token fake para testing sin tocar Adyen
        import hashlib
        tid = "mock-" + hashlib.md5((card["last4"] + shopper + str(time.time())).encode()).hexdigest()[:16]
        result = {
            "ok": True,
            "mode": "simulate",
            "token_id": tid,
            "recurring_reference": tid,
            "shopper_reference": shopper,
            "brand": card["brand"],
            "last4": card["last4"],
            "holder": card["holder"],
            "exp_month": card["exp_month"],
            "exp_year": card["exp_year"],
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=TOKEN_TTL_DAYS)).isoformat(),
            "note": "simulate mode — no real tokenization",
        }
        _save_token(result)
        return result

    # test/live
    api_key = cfg.get("api_key", "")
    merchant = cfg.get("merchant_account", "")
    if not api_key or not merchant:
        return {"ok": False, "error": "api_key y merchant_account requeridos"}

    import requests
    base = _get_base_url()
    payload = {
        "merchantAccount": merchant,
        "shopperReference": shopper,
        "recurringProcessingModel": "CardOnFile",
        "storePaymentMethod": True,
        "paymentMethod": {
            "type": "scheme",
            "number": card_data.get("number", "").replace(" ", ""),
            "expiryMonth": str(card_data.get("expiry_month", "")),
            "expiryYear": str(card_data.get("expiry_year", "")),
            "cvc": str(card_data.get("cvc", "")),
            "holderName": card_data.get("holder_name", ""),
        },
        "amount": {"currency": "EUR", "value": 0},  # $0 → zero-auth verification
        "reference": f"TOKENIZE-{int(time.time())}",
        "returnUrl": "https://www.ticketmaster.com/",
        "shopperInteraction": "Ecommerce",
        "channel": "Web",
        "countryCode": cfg.get("default_shopper_country", "ES"),
    }
    try:
        r = requests.post(
            f"{base}/payments",
            json=payload,
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        data = r.json()
        result_code = data.get("resultCode", "")
        if r.status_code == 200 and result_code in ("Authorised", "Received", "Pending"):
            stored = data.get("additionalData", {}) or {}
            token = (stored.get("recurring.recurringDetailReference")
                     or data.get("pspReference", ""))
            out = {
                "ok": True,
                "mode": mode,
                "token_id": token,
                "recurring_reference": token,
                "shopper_reference": shopper,
                "brand": stored.get("paymentMethod", card["brand"]),
                "last4": stored.get("cardSummary", card["last4"]),
                "holder": card["holder"],
                "exp_month": card["exp_month"],
                "exp_year": card["exp_year"],
                "psp_reference": data.get("pspReference"),
                "result_code": result_code,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(days=TOKEN_TTL_DAYS)).isoformat(),
            }
            _save_token(out)
            return out
        return {"ok": False, "mode": mode,
                "error": f"Adyen responded {r.status_code}: {result_code or data.get('message', 'unknown')}",
                "raw": data}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _save_token(token: dict):
    with _lock:
        d = json_atomic.load(_tokens_path(), default={"tokens": []})
        # dedupe por token_id
        d["tokens"] = [t for t in d["tokens"] if t.get("token_id") != token["token_id"]]
        d["tokens"].insert(0, token)
        d["tokens"] = d["tokens"][:200]
        json_atomic.save(_tokens_path(), d)


def list_tokens(active_only: bool = True) -> List[dict]:
    d = json_atomic.load(_tokens_path(), default={"tokens": []})
    items = d.get("tokens", [])
    if active_only:
        now = datetime.now()
        fresh = []
        for t in items:
            try:
                exp = datetime.fromisoformat(t.get("expires_at", ""))
                if exp > now:
                    fresh.append(t)
            except Exception:
                pass
        items = fresh
    return items


def get_token(token_id: str) -> Optional[dict]:
    for t in list_tokens(active_only=False):
        if t.get("token_id") == token_id:
            return t
    return None


def delete_token(token_id: str) -> bool:
    with _lock:
        d = json_atomic.load(_tokens_path(), default={"tokens": []})
        before = len(d["tokens"])
        d["tokens"] = [t for t in d["tokens"] if t.get("token_id") != token_id]
        json_atomic.save(_tokens_path(), d)
    return len(d["tokens"]) < before


def get_stats() -> dict:
    tokens = list_tokens(active_only=False)
    active = list_tokens(active_only=True)
    by_brand = {}
    for t in active:
        b = t.get("brand", "?")
        by_brand[b] = by_brand.get(b, 0) + 1
    return {
        "total": len(tokens),
        "active": len(active),
        "expired": len(tokens) - len(active),
        "by_brand": by_brand,
        "by_mode": _count_by(active, "mode"),
    }


def _count_by(items: List[dict], key: str) -> Dict[str, int]:
    out = {}
    for i in items:
        v = i.get(key, "?")
        out[v] = out.get(v, 0) + 1
    return out


def check_connection() -> dict:
    """Prueba credenciales haciendo un GET a /paymentMethods con monto 0."""
    cfg = get_config()
    mode = cfg.get("mode", "simulate")
    if mode == "simulate":
        return {"ok": True, "mode": "simulate",
                "note": "Modo simulate — sin llamadas reales. Configura mode=test o live para probar."}
    if not cfg.get("api_key") or not cfg.get("merchant_account"):
        return {"ok": False, "error": "api_key y merchant_account requeridos"}
    import requests
    try:
        base = _get_base_url()
        r = requests.post(
            f"{base}/paymentMethods",
            json={
                "merchantAccount": cfg["merchant_account"],
                "countryCode": cfg.get("default_shopper_country", "ES"),
                "amount": {"currency": "EUR", "value": 1000},
                "channel": "Web",
            },
            headers={"X-API-Key": cfg["api_key"],
                      "Content-Type": "application/json"},
            timeout=12,
        )
        if r.status_code == 200:
            data = r.json()
            methods = [pm.get("type") for pm in data.get("paymentMethods", [])]
            return {"ok": True, "mode": mode, "methods_count": len(methods),
                    "methods": methods[:10]}
        return {"ok": False, "mode": mode,
                "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def self_test() -> dict:
    """Test rápido: conexión + tokenización con tarjeta de test Adyen."""
    started = time.time()
    conn = check_connection()
    if not conn.get("ok") and get_config().get("mode") != "simulate":
        return {"ok": False, "elapsed_ms": int((time.time() - started) * 1000),
                "error": conn.get("error"), "phase": "connection"}
    # Adyen test card: 4111 1111 1111 1111 / 03/2030 / 737
    test_card = {
        "number": "4111111111111111",
        "expiry_month": "03",
        "expiry_year": "2030",
        "cvc": "737",
        "holder_name": "TM PERROS Test",
    }
    r = tokenize_card(test_card, shopper_reference="selftest")
    r["elapsed_ms"] = int((time.time() - started) * 1000)
    r["connection_check"] = conn
    return r
