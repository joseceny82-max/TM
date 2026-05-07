"""fivesim_handler.py — Integración con 5sim.net para recepción SMS/OTP.

5sim.net es un servicio de SMS receiver para registro masivo de cuentas que
requieren verificación SMS. Compra un número virtual, recibe el SMS, y libera.

Uso típico:
  1. set_api_key(key) — guardar token Bearer
  2. get_balance() — verificar saldo
  3. buy_number(country='spain', operator='any', product='ticketmaster')
     → devuelve {id, phone, expires}
  4. check_sms(order_id) → devuelve {sms: [...], status}
  5. finish_order(order_id) cuando recibes el SMS (libera el número)
  6. cancel_order(order_id) si no te sirve

Productos típicos para TM:
  - 'ticketmaster' (puede no existir, usar 'other' con país adecuado)
  - 'other' — número genérico que recibe cualquier SMS

API: https://5sim.net/v1/
Auth: Authorization: Bearer <token>

API pública del módulo:
  set_api_key(key) -> dict
  get_config() -> dict
  get_balance() -> dict
  list_countries() -> dict
  list_products(country='any', operator='any') -> dict
  buy_number(country, operator='any', product='other') -> dict
  check_sms(order_id) -> dict
  finish_order(order_id) -> dict
  cancel_order(order_id) -> dict
  list_orders() -> list
  get_active_orders() -> list
  self_test() -> dict
"""
from __future__ import annotations
import os
import time
import threading
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import json_atomic

_lock = threading.RLock()
_CFG_KEY = "fivesim_config.json"
_ORDERS_KEY = "fivesim_orders.json"

API_BASE = "https://5sim.net/v1"

# Productos comunes en 5sim
COMMON_PRODUCTS = [
    "ticketmaster", "ticketek", "stubhub", "axs",
    "google", "facebook", "instagram", "amazon",
    "telegram", "whatsapp", "discord",
    "other",  # genérico, recibe cualquier SMS
]

# Países comunes
COMMON_COUNTRIES = [
    "spain", "portugal", "france", "italy", "germany", "uk", "usa",
    "mexico", "argentina", "brazil", "canada", "australia", "any",
]


# ═══════════════════════════════════════════════════════════════
# Storage helpers
# ═══════════════════════════════════════════════════════════════
def _path(key: str) -> str:
    p = config.ruta(key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def get_config() -> Dict[str, Any]:
    """Devuelve config (API key enmascarada)."""
    d = json_atomic.load(_path(_CFG_KEY), default={})
    safe = dict(d)
    if safe.get("api_key"):
        k = safe["api_key"]
        safe["api_key_masked"] = f"{k[:8]}...{k[-4:]}" if len(k) > 12 else "***"
        safe["has_key"] = True
    else:
        safe["has_key"] = False
    return safe


def _get_raw_key() -> str:
    d = json_atomic.load(_path(_CFG_KEY), default={})
    return d.get("api_key", "")


def set_api_key(key: str) -> Dict[str, Any]:
    """Guarda la API key de 5sim (Bearer token JWT)."""
    if not key or len(key.strip()) < 20:
        return {"ok": False, "error": "key_too_short"}
    key = key.strip()
    # Validación básica del JWT (3 partes separadas por punto)
    if key.count(".") != 2 and not key.startswith("eyJ"):
        # Aceptamos cualquier string suficientemente largo (algunos servicios usan tokens no-JWT)
        pass
    with _lock:
        d = json_atomic.load(_path(_CFG_KEY), default={})
        d["api_key"] = key
        d["saved_at"] = datetime.now().isoformat()
        json_atomic.save(_path(_CFG_KEY), d)
    return {"ok": True, "saved": True}


def clear_api_key() -> Dict[str, Any]:
    with _lock:
        json_atomic.save(_path(_CFG_KEY), {})
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# HTTP API helpers
# ═══════════════════════════════════════════════════════════════
def _headers() -> Dict[str, str]:
    key = _get_raw_key()
    if not key:
        return {}
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }


def _api_call(method: str, path: str, params: Optional[Dict] = None,
              timeout: int = 15) -> Dict[str, Any]:
    """Llama la API de 5sim con auth + timeout."""
    if not _get_raw_key():
        return {"ok": False, "error": "api_key_missing",
                "hint": "Configura tu API key en /api/fivesim/config"}
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests_missing"}

    url = f"{API_BASE}{path}"
    try:
        r = requests.request(method, url, headers=_headers(),
                              params=params if method == "GET" else None,
                              json=params if method != "GET" else None,
                              timeout=timeout)
        if r.status_code == 401:
            return {"ok": False, "error": "auth_failed",
                    "status": 401, "msg": "API key inválida o expirada"}
        if r.status_code == 402:
            return {"ok": False, "error": "no_balance",
                    "status": 402, "msg": "Sin saldo. Recarga en 5sim.net"}
        if r.status_code == 404:
            return {"ok": False, "error": "not_found", "status": 404,
                    "raw": (r.text or "")[:200]}
        if r.status_code == 429:
            return {"ok": False, "error": "rate_limit", "status": 429,
                    "msg": "Demasiadas peticiones, espera"}

        # Algunos endpoints devuelven texto plano (no JSON) → "no free phones"
        try:
            data = r.json()
        except Exception:
            text = (r.text or "").strip()
            if r.status_code == 200:
                return {"ok": True, "raw": text}
            return {"ok": False, "error": "bad_response",
                    "status": r.status_code, "raw": text[:200]}

        if r.status_code == 200:
            return {"ok": True, **(data if isinstance(data, dict) else {"data": data})}
        return {"ok": False, "error": "http_error",
                "status": r.status_code, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ═══════════════════════════════════════════════════════════════
# API methods
# ═══════════════════════════════════════════════════════════════
def get_balance() -> Dict[str, Any]:
    """GET /user/profile devuelve {balance, rating, email}"""
    r = _api_call("GET", "/user/profile")
    if r.get("ok"):
        return {
            "ok": True,
            "balance": r.get("balance", 0),
            "currency": "RUB",
            "rating": r.get("rating", 0),
            "email": r.get("email", ""),
            "id": r.get("id", ""),
        }
    return r


def list_countries() -> Dict[str, Any]:
    """GET /guest/countries devuelve {country_code: {iso, prefix, text_es, ...}}"""
    r = _api_call("GET", "/guest/countries")
    if r.get("ok"):
        # 5sim devuelve un dict gigante {country: {operators}}
        data = r.get("data") or {k: v for k, v in r.items() if k != "ok"}
        return {"ok": True, "countries": data, "count": len(data)}
    return r


def list_products(country: str = "any", operator: str = "any") -> Dict[str, Any]:
    """GET /guest/products/{country}/{operator}"""
    r = _api_call("GET", f"/guest/products/{country}/{operator}")
    if r.get("ok"):
        data = r.get("data") or {k: v for k, v in r.items() if k != "ok"}
        return {"ok": True, "products": data, "country": country, "operator": operator}
    return r


def get_prices(country: str = "spain") -> Dict[str, Any]:
    """GET /guest/prices?country=spain"""
    r = _api_call("GET", "/guest/prices", params={"country": country})
    return r


def buy_number(country: str = "spain", operator: str = "any",
               product: str = "other") -> Dict[str, Any]:
    """GET /user/buy/activation/{country}/{operator}/{product}
    Devuelve: {id, phone, operator, product, price, status, expires, created_at}"""
    if not country or not product:
        return {"ok": False, "error": "country_and_product_required"}
    r = _api_call("GET", f"/user/buy/activation/{country}/{operator}/{product}")
    if r.get("ok"):
        # Persist order
        with _lock:
            d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
            d["orders"].append({
                "id": r.get("id"),
                "phone": r.get("phone"),
                "country": country,
                "operator": operator,
                "product": product,
                "price": r.get("price"),
                "expires": r.get("expires"),
                "status": "PENDING",
                "created_at": datetime.now().isoformat(),
                "sms": [],
            })
            d["orders"] = d["orders"][-200:]  # cap
            json_atomic.save(_path(_ORDERS_KEY), d)
        return {
            "ok": True,
            "order_id": r.get("id"),
            "phone": r.get("phone"),
            "operator": r.get("operator"),
            "product": r.get("product"),
            "price": r.get("price"),
            "expires": r.get("expires"),
            "status": r.get("status"),
        }
    return r


def check_sms(order_id: int) -> Dict[str, Any]:
    """GET /user/check/{order_id} devuelve {status, sms: [{date, sender, text, code}]}"""
    if not order_id:
        return {"ok": False, "error": "order_id_required"}
    r = _api_call("GET", f"/user/check/{order_id}")
    if r.get("ok"):
        sms_list = r.get("sms") or []
        # Update local order
        with _lock:
            d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
            for o in d.get("orders", []):
                if str(o.get("id")) == str(order_id):
                    o["sms"] = sms_list
                    o["status"] = r.get("status", o.get("status"))
                    o["last_check"] = datetime.now().isoformat()
                    break
            json_atomic.save(_path(_ORDERS_KEY), d)
        # Extraer códigos numéricos típicos
        codes = []
        for s in sms_list:
            txt = s.get("text", "")
            if s.get("code"):
                codes.append(s["code"])
            else:
                import re
                m = re.search(r"\b(\d{4,8})\b", txt)
                if m: codes.append(m.group(1))
        return {
            "ok": True,
            "status": r.get("status"),
            "phone": r.get("phone"),
            "sms_count": len(sms_list),
            "sms": sms_list,
            "codes_detected": codes,
            "expires": r.get("expires"),
        }
    return r


def finish_order(order_id: int) -> Dict[str, Any]:
    """GET /user/finish/{order_id} libera el número (recibido SMS OK)"""
    if not order_id:
        return {"ok": False, "error": "order_id_required"}
    r = _api_call("GET", f"/user/finish/{order_id}")
    if r.get("ok"):
        with _lock:
            d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
            for o in d.get("orders", []):
                if str(o.get("id")) == str(order_id):
                    o["status"] = "FINISHED"
                    o["finished_at"] = datetime.now().isoformat()
                    break
            json_atomic.save(_path(_ORDERS_KEY), d)
    return r


def cancel_order(order_id: int) -> Dict[str, Any]:
    """GET /user/cancel/{order_id} cancela el número (devuelve saldo si aún no recibió SMS)"""
    if not order_id:
        return {"ok": False, "error": "order_id_required"}
    r = _api_call("GET", f"/user/cancel/{order_id}")
    if r.get("ok"):
        with _lock:
            d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
            for o in d.get("orders", []):
                if str(o.get("id")) == str(order_id):
                    o["status"] = "CANCELED"
                    o["canceled_at"] = datetime.now().isoformat()
                    break
            json_atomic.save(_path(_ORDERS_KEY), d)
    return r


def list_orders() -> List[Dict[str, Any]]:
    """Lista ordenes guardadas localmente."""
    d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
    return d.get("orders", [])


def get_active_orders() -> List[Dict[str, Any]]:
    return [o for o in list_orders()
            if o.get("status") in ("PENDING", "RECEIVED")]


def get_history(limit: int = 50) -> List[Dict[str, Any]]:
    return list_orders()[-limit:]


# ═══════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}

    # 1. Helpers básicos
    checks["api_base_set"] = API_BASE.startswith("https://")
    checks["common_countries_present"] = "spain" in COMMON_COUNTRIES
    checks["common_products_present"] = "ticketmaster" in COMMON_PRODUCTS

    # 2. Config CRUD
    cfg = get_config()
    checks["get_config_returns_dict"] = isinstance(cfg, dict)
    checks["get_config_has_has_key_field"] = "has_key" in cfg

    # 3. set_api_key validation
    r = set_api_key("")
    checks["rejects_empty_key"] = not r.get("ok")
    r = set_api_key("short")
    checks["rejects_short_key"] = not r.get("ok")

    # 4. Set y verifica key dummy
    test_key = "TEST_" + "x" * 40
    r = set_api_key(test_key)
    checks["accepts_long_key"] = bool(r.get("ok"))
    cfg = get_config()
    checks["key_persists"] = cfg.get("has_key") is True
    checks["key_masked"] = bool(cfg.get("api_key_masked"))

    # 5. API call con key dummy → debería fallar 401
    r = get_balance()
    checks["api_call_fails_with_bad_key"] = (not r.get("ok")) and r.get("status") == 401

    # 6. orders persistence
    orders = list_orders()
    checks["list_orders_returns_list"] = isinstance(orders, list)

    # 7. Validation funciones
    r = check_sms(0)
    checks["check_sms_validates_id"] = not r.get("ok")
    r = finish_order(0)
    checks["finish_validates_id"] = not r.get("ok")
    r = cancel_order(0)
    checks["cancel_validates_id"] = not r.get("ok")
    r = buy_number("", "any", "")
    checks["buy_validates_args"] = not r.get("ok")

    # 8. Cleanup
    clear_api_key()
    cfg = get_config()
    checks["clear_works"] = cfg.get("has_key") is False

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {
        "ok": passed == total,
        "total_checks": total,
        "passed_checks": passed,
        "checks": checks,
    }


def get_info() -> Dict[str, Any]:
    cfg = get_config()
    orders = list_orders()
    return {
        "module": "fivesim_handler",
        "version": 1,
        "api_base": API_BASE,
        "has_key": cfg.get("has_key", False),
        "orders_total": len(orders),
        "active_orders": len(get_active_orders()),
        "common_products": COMMON_PRODUCTS,
        "common_countries": COMMON_COUNTRIES,
    }
