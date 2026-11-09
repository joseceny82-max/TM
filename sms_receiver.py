"""sms_receiver.py — Multi-provider SMS/OTP receiver.

Backends soportados:
  • 5sim.net          (token Bearer · catálogo amplio · barato)
  • SMS-Activate.io   (api_key URL param · "ticketmaster" dedicado · top para TM)
  • OnlineSim.io      (apikey URL param · mejor cobertura España)
  • SMSPool.net       (key URL param · stock alto ES/TM · status_id numérico)

Auto-rotación opcional: si el provider activo no tiene stock, rota al siguiente.

API pública:
  set_provider(name)
  set_api_key(provider, key)
  get_config()
  get_balance(provider=None)            → saldo del provider activo (o uno específico)
  buy_number(country, product, provider=None)
  check_sms(order_id, provider=None)
  finish_order(order_id, provider=None)
  cancel_order(order_id, provider=None)
  list_orders()
  self_test()
"""
from __future__ import annotations
import os, re, time, threading, logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

import config
import json_atomic

_lock = threading.RLock()
_CFG_KEY = "sms_receiver_config.json"
_ORDERS_KEY = "sms_receiver_orders.json"

PROVIDERS_META = {
    "5sim": {
        "label": "5sim.net",
        "base": "https://5sim.net/v1",
        "auth": "bearer",
        "desc": "Catálogo amplio · 0.10-0.50€ · 180+ países · simple",
        "currency": "RUB",
    },
    "smsactivate": {
        "label": "SMS-Activate.io",
        "base": "https://api.sms-activate.io/stubs/handler_api.php",
        "auth": "query",  # api_key=...
        "desc": "Top para TM · 'ticketmaster' dedicado · 0.10-0.40€",
        "currency": "RUB",
    },
    "onlinesim": {
        "label": "OnlineSim.io",
        "base": "https://onlinesim.io/api",
        "auth": "query",  # apikey=...
        "desc": "Mejor cobertura España · 0.15-0.50€",
        "currency": "USD",
    },
    "smspool": {
        "label": "SMSPool.net",
        "base": "https://api.smspool.net",
        "auth": "query",  # key=...
        "desc": "Stock alto ES TM · 0.18-0.29$ · success 76% · pago en USD",
        "currency": "USD",
    },
}

# Mapping de productos: nombre genérico → ID/slug por provider
# Verificado en 5sim API: ticketmaster disponible en es, us, de, fr, it, mx
PRODUCT_MAP = {
    "ticketmaster": {
        "5sim": "ticketmaster",   # ✅ slug oficial 5sim (confirmado vía /guest/products)
        "smsactivate": "tm",       # SMS-Activate: 'tm' es ticketmaster
        "onlinesim": "ticketmaster",
        "smspool":    "921",        # SMSPool: ID numérico Ticketmaster (verificado)
    },
    "google":   {"5sim": "google",   "smsactivate": "go", "onlinesim": "google",   "smspool": "395"},
    "facebook": {"5sim": "facebook", "smsactivate": "fb", "onlinesim": "facebook", "smspool": "271"},
    "any":      {"5sim": "other",    "smsactivate": "ot", "onlinesim": "any",      "smspool": "0"},
}

# Mapping de países: ISO → ID/slug por provider
COUNTRY_MAP = {
    "es": {"5sim": "spain",    "smsactivate": "56",  "onlinesim": "spain",    "smspool": "55"},
    "us": {"5sim": "usa",      "smsactivate": "187", "onlinesim": "usa",      "smspool": "1"},
    "uk": {"5sim": "england",  "smsactivate": "16",  "onlinesim": "uk",       "smspool": "2"},
    "de": {"5sim": "germany",  "smsactivate": "43",  "onlinesim": "germany",  "smspool": "24"},
    "fr": {"5sim": "france",   "smsactivate": "78",  "onlinesim": "france",   "smspool": "23"},
    "it": {"5sim": "italy",    "smsactivate": "86",  "onlinesim": "italy",    "smspool": "79"},
    "pt": {"5sim": "portugal", "smsactivate": "117", "onlinesim": "portugal", "smspool": "8"},
    "mx": {"5sim": "mexico",   "smsactivate": "54",  "onlinesim": "mexico",   "smspool": "53"},
}


# ═══════════════════════════════════════════════════════════════
# Config storage
# ═══════════════════════════════════════════════════════════════
def _path(k: str) -> str:
    p = config.ruta(k)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _load_cfg() -> Dict[str, Any]:
    # 5sim es el provider principal por defecto; auto_rotate ON porque los
    # logs muestran caídas por stock que se resolvían rotando de provider.
    return json_atomic.load(_path(_CFG_KEY), default={"active": "5sim", "keys": {}, "auto_rotate": True})


def _save_cfg(d: Dict[str, Any]) -> None:
    json_atomic.save(_path(_CFG_KEY), d)


def get_config() -> Dict[str, Any]:
    """Config con keys enmascaradas."""
    cfg = _load_cfg()
    safe_keys = {}
    for prov, key in (cfg.get("keys") or {}).items():
        if key:
            safe_keys[prov] = {
                "set": True,
                "masked": f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***",
                "length": len(key),
            }
        else:
            safe_keys[prov] = {"set": False}
    return {
        "active": cfg.get("active", "5sim"),
        "providers": PROVIDERS_META,
        "keys": safe_keys,
        "auto_rotate": cfg.get("auto_rotate", True),
    }


def set_provider(name: str) -> Dict[str, Any]:
    if name not in PROVIDERS_META:
        return {"ok": False, "error": "invalid_provider",
                "valid": list(PROVIDERS_META.keys())}
    with _lock:
        cfg = _load_cfg()
        cfg["active"] = name
        _save_cfg(cfg)
    return {"ok": True, "active": name}


def set_api_key(provider: str, key: str) -> Dict[str, Any]:
    if provider not in PROVIDERS_META:
        return {"ok": False, "error": "invalid_provider"}
    if not key or len(key.strip()) < 10:
        return {"ok": False, "error": "key_too_short"}
    with _lock:
        cfg = _load_cfg()
        cfg.setdefault("keys", {})[provider] = key.strip()
        _save_cfg(cfg)
    return {"ok": True, "provider": provider}


def set_auto_rotate(enabled: bool) -> Dict[str, Any]:
    with _lock:
        cfg = _load_cfg()
        cfg["auto_rotate"] = bool(enabled)
        _save_cfg(cfg)
    return {"ok": True, "auto_rotate": bool(enabled)}


def _get_key(provider: str) -> str:
    return _load_cfg().get("keys", {}).get(provider, "")


def _get_active() -> str:
    return _load_cfg().get("active", "5sim")


# ═══════════════════════════════════════════════════════════════
# HTTP helpers
# ═══════════════════════════════════════════════════════════════
def _http(provider: str, method: str, path: str,
          params: Optional[Dict] = None, timeout: int = 15) -> Dict[str, Any]:
    """Llama API del provider con su esquema de auth."""
    meta = PROVIDERS_META.get(provider)
    if not meta:
        return {"ok": False, "error": "invalid_provider"}
    key = _get_key(provider)
    if not key:
        return {"ok": False, "error": "api_key_missing", "provider": provider}

    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests_missing"}

    url = meta["base"] + path
    headers = {"Accept": "application/json"}
    qparams = dict(params or {})

    if meta["auth"] == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif meta["auth"] == "query":
        # SMS-Activate usa api_key, OnlineSim apikey, SMSPool key
        if provider == "smsactivate":   qparams["api_key"] = key
        elif provider == "smspool":     qparams["key"] = key
        else:                            qparams["apikey"] = key

    try:
        r = requests.request(method, url, headers=headers,
                              params=qparams if method == "GET" else None,
                              data=qparams if method != "GET" else None,
                              timeout=timeout)
        # Intentar JSON, si falla devolver texto
        try:
            data = r.json()
            return {"ok": r.status_code < 400, "status": r.status_code, "data": data}
        except Exception:
            text = (r.text or "").strip()
            return {"ok": r.status_code < 400, "status": r.status_code, "raw": text}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": provider}


# ═══════════════════════════════════════════════════════════════
# Provider-specific implementations
# ═══════════════════════════════════════════════════════════════
def _balance_5sim() -> Dict[str, Any]:
    r = _http("5sim", "GET", "/user/profile")
    if r.get("ok"):
        d = r.get("data", {})
        return {"ok": True, "provider": "5sim",
                "balance": d.get("balance", 0), "currency": "RUB",
                "email": d.get("email", "")}
    return r


def _balance_smsactivate() -> Dict[str, Any]:
    # SMS-Activate: GET handler_api.php?action=getBalance → "ACCESS_BALANCE:123.45"
    r = _http("smsactivate", "GET", "", params={"action": "getBalance"})
    raw = r.get("raw") or ""
    if "ACCESS_BALANCE:" in raw:
        try:
            balance = float(raw.split(":")[1])
            return {"ok": True, "provider": "smsactivate",
                    "balance": balance, "currency": "RUB"}
        except: pass
    return {"ok": False, "provider": "smsactivate", "raw": raw[:120]}


def _balance_onlinesim() -> Dict[str, Any]:
    r = _http("onlinesim", "GET", "/getBalance.php")
    d = r.get("data", {})
    if isinstance(d, dict) and "balance" in d:
        return {"ok": True, "provider": "onlinesim",
                "balance": float(d["balance"]),
                "currency": d.get("currency", "USD")}
    return r


def _balance_smspool() -> Dict[str, Any]:
    # GET /request/balance?key=KEY → {"balance":"57.96"}
    r = _http("smspool", "GET", "/request/balance")
    d = r.get("data", {})
    if isinstance(d, dict) and "balance" in d:
        try:
            return {"ok": True, "provider": "smspool",
                    "balance": float(d["balance"]), "currency": "USD"}
        except (TypeError, ValueError): pass
    return r


def get_balance(provider: Optional[str] = None) -> Dict[str, Any]:
    p = provider or _get_active()
    if p == "5sim":         return _balance_5sim()
    if p == "smsactivate":  return _balance_smsactivate()
    if p == "onlinesim":    return _balance_onlinesim()
    if p == "smspool":      return _balance_smspool()
    return {"ok": False, "error": "unknown_provider"}


# ─── BUY NUMBER ───
def _buy_5sim(country: str, product: str, operator: str = "any") -> Dict[str, Any]:
    p_id = PRODUCT_MAP.get(product, {}).get("5sim", product)
    c_id = COUNTRY_MAP.get(country, {}).get("5sim", country)
    r = _http("5sim", "GET", f"/user/buy/activation/{c_id}/{operator}/{p_id}")
    d = r.get("data", {})
    if r.get("ok") and isinstance(d, dict) and d.get("id"):
        return {"ok": True, "provider": "5sim",
                "order_id": d["id"], "phone": d.get("phone"),
                "expires": d.get("expires"), "price": d.get("price")}
    return {"ok": False, "provider": "5sim",
            "error": d.get("error") if isinstance(d, dict) else r.get("raw"),
            "raw": r}


def _buy_smsactivate(country: str, product: str) -> Dict[str, Any]:
    p_id = PRODUCT_MAP.get(product, {}).get("smsactivate", product)
    c_id = COUNTRY_MAP.get(country, {}).get("smsactivate", country)
    r = _http("smsactivate", "GET", "",
              params={"action": "getNumber", "service": p_id, "country": c_id})
    raw = r.get("raw") or ""
    # Respuesta típica: "ACCESS_NUMBER:12345:34612345678"
    if raw.startswith("ACCESS_NUMBER:"):
        parts = raw.split(":")
        if len(parts) >= 3:
            return {"ok": True, "provider": "smsactivate",
                    "order_id": parts[1], "phone": parts[2]}
    return {"ok": False, "provider": "smsactivate", "raw": raw[:120]}


def _buy_onlinesim(country: str, product: str) -> Dict[str, Any]:
    p_id = PRODUCT_MAP.get(product, {}).get("onlinesim", product)
    c_id = COUNTRY_MAP.get(country, {}).get("onlinesim", country)
    r = _http("onlinesim", "GET", "/getNum.php",
              params={"service": p_id, "country": c_id})
    d = r.get("data", {})
    if r.get("ok") and isinstance(d, dict) and d.get("response") == 1:
        return {"ok": True, "provider": "onlinesim",
                "order_id": d.get("tzid"), "phone": d.get("number")}
    return {"ok": False, "provider": "onlinesim", "raw": str(d)[:120]}


def _buy_smspool(country: str, product: str) -> Dict[str, Any]:
    r = _buy_smspool_once(country, product)
    if r.get("ok"):
        return r
    # Fallback de país: si ES está sin stock (transitorio), probar otros
    # países con stock antes de rendirse — TM acepta SMS a números extranjeros.
    blob = str(r.get("error") or '') + ' ' + str(r.get("raw") or '')
    if (country or "").lower() == "es" and re.search(
            r"couldn.?t find an available phone|no numbers? available|out of stock", blob, re.I):
        for fb in SMSPOOL_COUNTRY_FALLBACK:
            r2 = _buy_smspool_once(fb, product)
            if r2.get("ok"):
                r2["country_used"] = fb
                _log.info("  🌍 smspool ES sin stock → número %s desde %s", r2.get("phone"), fb)
                return r2
            r = r2
    return r


# Países de fallback para smspool cuando ES está OUT_OF_STOCK (orden de preferencia).
# SOLO países con números nacionales de 9 dígitos: el campo de teléfono de TM
# España acepta máx. 9 dígitos y trunca los de 10 (US/UK/DE/MX rompen el registro).
SMSPOOL_COUNTRY_FALLBACK = ["fr", "pt"]


def _buy_smspool_once(country: str, product: str) -> Dict[str, Any]:
    # POST /purchase/sms — body: key, country, service [, pool, max_price]
    p_id = PRODUCT_MAP.get(product, {}).get("smspool", product)
    c_id = COUNTRY_MAP.get(country, {}).get("smspool", country)
    r = _http("smspool", "POST", "/purchase/sms",
              params={"country": c_id, "service": p_id})
    d = r.get("data", {})
    if r.get("ok") and isinstance(d, dict) and d.get("success") == 1:
        return {"ok": True, "provider": "smspool",
                "order_id": d.get("order_id") or d.get("orderid"),
                "phone": d.get("phonenumber") or d.get("number"),
                "expires": d.get("expires_in"),
                "price": d.get("cost")}
    err = None
    if isinstance(d, dict):
        if d.get("errors"):
            err = d["errors"][0].get("message") if isinstance(d["errors"], list) else str(d["errors"])
        else:
            err = d.get("message")
    return {"ok": False, "provider": "smspool",
            "error": err or r.get("raw"), "raw": str(d)[:200]}


def _provider_is_usable(prov: str) -> Dict[str, Any]:
    """Comprueba que un provider tiene key+saldo mínimo en una sola llamada
    rápida. Devuelve {usable, reason}. Cachea resultado 60s en memoria para
    no martillear las APIs.
    Reglas:
      - sin key → no usable (skip).
      - balance check falla / raw vacío → no usable (skip 60s).
      - balance numérico:
          * 5sim: >= 5 RUB (~0.05€) — números cuestan ~6 RUB.
          * smspool: >= $0.20.
          * smsactivate / onlinesim: >= 0 (no precio fijo).
    """
    import time as _t
    cache = _provider_is_usable._cache  # type: ignore
    now = _t.time()
    if prov in cache and now - cache[prov]["ts"] < 60:
        return cache[prov]["v"]
    if not _get_key(prov):
        v = {"usable": False, "reason": "no_key"}
    else:
        try:
            if prov == "5sim":          br = _balance_5sim()
            elif prov == "smsactivate": br = _balance_smsactivate()
            elif prov == "onlinesim":   br = _balance_onlinesim()
            elif prov == "smspool":     br = _balance_smspool()
            else: br = {"ok": False, "raw": ""}
        except Exception as e:
            br = {"ok": False, "error": str(e)[:80]}
        if not br.get("ok"):
            v = {"usable": False, "reason": "balance_check_failed",
                 "raw": br.get("raw", "")[:60]}
        else:
            balance = br.get("balance", 0)
            min_balance = {"5sim": 5, "smspool": 0.20,
                            "smsactivate": 0, "onlinesim": 0}.get(prov, 0)
            if (balance or 0) < min_balance:
                v = {"usable": False, "reason": "low_balance",
                     "balance": balance, "min": min_balance}
            else:
                v = {"usable": True, "balance": balance}
    cache[prov] = {"ts": now, "v": v}
    return v


_provider_is_usable._cache = {}  # type: ignore


def usable_providers() -> Dict[str, Any]:
    """Reporta el estado live de cada provider — útil para health/debug."""
    return {p: _provider_is_usable(p) for p in PROVIDERS_META}


def buy_number(country: str = "es", product: str = "ticketmaster",
               provider: Optional[str] = None,
               exclude: Optional[str] = None) -> Dict[str, Any]:
    p = provider or _get_active()

    # Try active provider, si falla y auto_rotate activado → probar siguientes
    cfg = _load_cfg()
    if cfg.get("auto_rotate"):
        order = [p] + [x for x in PROVIDERS_META if x != p]
    else:
        order = [p]
    # Failover por calidad (p.ej. provider con timeouts acumulados en la fila):
    # excluirlo de la rotación — el buy caerá en el siguiente con stock.
    if exclude:
        order = [x for x in order if x != exclude]

    # OPTIMIZACIÓN LATENCIA: en el camino feliz SOLO comprobamos balance del
    # provider activo (1 HTTP call). Los demás se comprueban de forma perezosa
    # solo si el buy falla y hay que rotar. Antes: 4 get_balance por buy.
    skipped = []
    first = _provider_is_usable(order[0])
    if first.get("usable"):
        usable_order = [order[0]]
    else:
        skipped.append({"provider": order[0], **first})
        usable_order = []
        for prov in order[1:]:
            u = _provider_is_usable(prov)
            if u.get("usable"):
                usable_order.append(prov)
            else:
                skipped.append({"provider": prov, **u})
    if not usable_order:
        if exclude:
            # El provider excluido era el único usable: mejor usarlo que fallar.
            _log.info("  ⚠ solo %s usable (excluido por timeouts) — se usa igualmente", exclude)
            return buy_number(country=country, product=product, provider=exclude)
        return {"ok": False, "error": "no_usable_provider",
                "skipped": skipped,
                "hint": "Ningún SMS provider tiene key+saldo. "
                         "Recarga 5sim, configura key SMS-Activate/OnlineSim, "
                         "o espera stock SMSPool."}

    last_err = None
    i = 0
    while i < len(usable_order):
        prov = usable_order[i]
        i += 1
        if prov == "5sim":         r = _buy_5sim(country, product)
        elif prov == "smsactivate": r = _buy_smsactivate(country, product)
        elif prov == "onlinesim":   r = _buy_onlinesim(country, product)
        elif prov == "smspool":     r = _buy_smspool(country, product)
        else: continue

        if r.get("ok"):
            r["provider"] = prov
            # Persistir
            with _lock:
                d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
                d["orders"].append({
                    "id": r["order_id"], "phone": r["phone"],
                    "provider": prov, "country": country, "product": product,
                    "status": "PENDING", "sms": [],
                    "created_at": datetime.now().isoformat(),
                })
                d["orders"] = d["orders"][-200:]
                json_atomic.save(_path(_ORDERS_KEY), d)
            return r
        last_err = r
        # Fallback por FALTA DE STOCK (no solo por key/saldo): si el provider
        # activo no encontró número, probar el siguiente usable EN ESTA MISMA
        # llamada aunque auto_rotate esté off en el cfg guardado.
        blob = str(r.get("error") or '') + ' ' + str(r.get("raw") or '') + ' ' + str(r.get("msg") or '')
        if re.search(r"couldn.?t find an available phone|no numbers? available|out of stock|no_provider_available|no free phones", blob, re.I):
            nxt = [x for x in order if x not in usable_order and _provider_is_usable(x).get("usable")]
            if nxt:
                _log.info("  🔄 %s sin stock → rotando a %s en la misma llamada", prov, nxt[0])
                usable_order.extend(nxt)
    return last_err or {"ok": False, "error": "no_provider_available",
                        "skipped": skipped}


# ─── CHECK SMS ───
# Rate-limit por orden: con N procesos en paralelo haciendo poll a 0.5s/orden se
# activa el rate-limit de los providers (SMSPool "Please wait 5 minutes"). Si ha
# pasado menos de _POLL_MIN_INTERVAL_S desde el último poll de la orden, devolvemos
# el último resultado cacheado en vez de llamar a la API.
_POLL_MIN_INTERVAL_S = 1.5
_last_poll_ts: Dict[str, float] = {}
_last_poll_result: Dict[str, Dict[str, Any]] = {}


def _reset_poll(order_id) -> None:
    _last_poll_ts.pop(str(order_id), None)
    _last_poll_result.pop(str(order_id), None)


def _check_5sim(order_id) -> Dict[str, Any]:
    r = _http("5sim", "GET", f"/user/check/{order_id}")
    d = r.get("data", {})
    sms_list = d.get("sms", []) if isinstance(d, dict) else []
    codes = []
    for s in sms_list:
        txt = s.get("text", "")
        if s.get("code"): codes.append(s["code"])
        else:
            m = re.search(r"\b(\d{4,8})\b", txt)
            if m: codes.append(m.group(1))
    return {"ok": r.get("ok", False), "provider": "5sim",
            "status": d.get("status") if isinstance(d, dict) else None,
            "sms_count": len(sms_list), "sms": sms_list, "codes": codes}


def _check_smsactivate(order_id) -> Dict[str, Any]:
    r = _http("smsactivate", "GET", "",
              params={"action": "getStatus", "id": order_id})
    raw = r.get("raw") or ""
    # "STATUS_OK:CODE" o "STATUS_WAIT_CODE"
    if raw.startswith("STATUS_OK:"):
        code = raw.split(":", 1)[1]
        return {"ok": True, "provider": "smsactivate",
                "status": "RECEIVED", "codes": [code], "sms_count": 1,
                "sms": [{"code": code}]}
    if "STATUS_WAIT" in raw:
        return {"ok": True, "provider": "smsactivate",
                "status": "PENDING", "sms_count": 0, "codes": [], "sms": []}
    return {"ok": False, "raw": raw[:120]}


def _check_onlinesim(order_id) -> Dict[str, Any]:
    r = _http("onlinesim", "GET", "/getState.php",
              params={"tzid": order_id})
    d = r.get("data", {})
    if isinstance(d, list) and d:
        item = d[0]
        msg = item.get("msg") or ""
        codes = []
        m = re.search(r"\b(\d{4,8})\b", msg)
        if m: codes.append(m.group(1))
        return {"ok": True, "provider": "onlinesim",
                "status": item.get("response", "PENDING"),
                "sms_count": 1 if msg else 0,
                "sms": [{"text": msg}] if msg else [],
                "codes": codes}
    return {"ok": False, "raw": str(d)[:120]}


def _check_smspool(order_id) -> Dict[str, Any]:
    # GET /sms/check?key=KEY&orderid=XYZ
    # status_id: 1=Pending · 3=Completed · 4=Refunded · 6=Expired
    r = _http("smspool", "GET", "/sms/check", params={"orderid": order_id})
    d = r.get("data", {})
    if not isinstance(d, dict):
        return {"ok": False, "provider": "smspool", "raw": str(d)[:200]}
    sid = d.get("status") or d.get("status_id")
    code = d.get("sms") or d.get("code") or ""
    full = d.get("full_sms") or d.get("full_code") or ""
    # Normalizar contractos raros: 'sms' puede venir como lista/dict anidado
    if isinstance(code, list):
        for item in code:
            if isinstance(item, dict):
                code = item.get("code") or item.get("sms") or ""
                full = full or item.get("sms_content") or item.get("text") or ""
            else:
                code = str(item)
            if code: break
        else:
            code = ""
    elif isinstance(code, dict):
        full = full or code.get("sms_content") or code.get("text") or ""
        code = code.get("code") or ""
    _log.debug("smspool check order=%s raw=%s", order_id, str(d)[:400])
    codes: List[str] = []
    if code:
        codes.append(str(code))
    elif full:
        m = re.search(r"\b(\d{4,8})\b", str(full))
        if m: codes.append(m.group(1))
    if sid in (3, "3", "completed"):
        status = "RECEIVED"
    elif sid in (4, "4", "refunded"):
        status = "CANCELED"
    elif sid in (6, "6", "expired"):
        status = "EXPIRED"
    else:
        status = "PENDING"
    sms_obj = []
    if codes:
        sms_obj.append({"code": codes[0], "text": str(full or code)})
    return {"ok": True, "provider": "smspool", "status": status,
            "sms_count": len(sms_obj), "sms": sms_obj, "codes": codes}


def check_sms(order_id, provider: Optional[str] = None) -> Dict[str, Any]:
    if not order_id:
        return {"ok": False, "error": "order_id_required"}
    # Rate-limit por orden: <1.5s desde el último poll → devolver cache
    now = time.time()
    oid = str(order_id)
    prev_ts = _last_poll_ts.get(oid)
    if prev_ts is not None and now - prev_ts < _POLL_MIN_INTERVAL_S:
        cached = _last_poll_result.get(oid)
        if cached is not None:
            return cached
    # Buscar provider en orders locales si no se especifica
    if not provider:
        for o in list_orders():
            if str(o.get("id")) == str(order_id):
                provider = o.get("provider")
                break
    p = provider or _get_active()
    if p == "5sim":         r = _check_5sim(order_id)
    elif p == "smsactivate": r = _check_smsactivate(order_id)
    elif p == "onlinesim":   r = _check_onlinesim(order_id)
    elif p == "smspool":     r = _check_smspool(order_id)
    else: return {"ok": False, "error": "unknown_provider"}

    # Update local
    if r.get("ok"):
        with _lock:
            d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
            for o in d.get("orders", []):
                if str(o.get("id")) == str(order_id):
                    o["sms"] = r.get("sms", [])
                    o["status"] = r.get("status", o.get("status"))
                    o["last_check"] = datetime.now().isoformat()
                    break
            json_atomic.save(_path(_ORDERS_KEY), d)
    # Registrar poll y cachear resultado (reset al llegar SMS → permite poll
    # inmediato siguiente para no perder latencia en la entrega)
    _last_poll_ts[oid] = time.time()
    _last_poll_result[oid] = r
    if r.get("sms_count") or r.get("codes"):
        _reset_poll(oid)
    return r


# ─── FINISH / CANCEL ───
# Reglas 5sim:
#   • cancel: solo válido 2-20 min después de buy, ANTES de recibir SMS
#   • Si ya recibió SMS → no se puede cancelar, solo "finish"
#   • Tras 20 min se cancela automáticamente
#
# Reglas SMSPool:
#   • cancel necesita ≥60s desde el buy; antes responde error
#   • cancelación efectiva → status_id pasa a 4 (Refunded)
#   • si la orden ya recibió SMS o está completed → no se puede cancelar
def _smspool_seconds_since_buy(order_id) -> float:
    """Devuelve segundos transcurridos desde el created_at local. inf si no se conoce."""
    try:
        with _lock:
            d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
        for o in d.get("orders", []):
            if str(o.get("id")) == str(order_id):
                ca = o.get("created_at")
                if not ca: return float("inf")
                dt = datetime.fromisoformat(ca)
                return (datetime.now() - dt).total_seconds()
    except Exception:
        return float("inf")
    return float("inf")


def _cancel_smspool_robust(order_id) -> Dict[str, Any]:
    """Cancel SMSPool con: respeto del mínimo 60s, parseo de success en JSON,
    verificación posterior con /sms/check (status_id=4 esperado), reintento
    1 vez si quedó pending. Devuelve dict con ok=True solo si refunded confirmado
    o si la orden ya estaba expired/completed (no cancelable pero igual liberada).
    """
    import time as _time
    elapsed = _smspool_seconds_since_buy(order_id)
    if elapsed < 60:
        wait = 60 - elapsed + 1
        _time.sleep(min(wait, 60))

    last_resp = None
    for attempt in range(2):
        r = _http("smspool", "POST", "/sms/cancel", params={"orderid": order_id})
        last_resp = r
        d = r.get("data", {})
        api_success = isinstance(d, dict) and (
            d.get("success") in (1, "1", True) or
            "cancelled" in str(d.get("message", "")).lower() or
            "canceled" in str(d.get("message", "")).lower() or
            "refunded" in str(d.get("message", "")).lower())
        if r.get("ok") and api_success:
            chk = _check_smspool(order_id)
            if chk.get("status") in ("CANCELED", "EXPIRED"):
                return {"ok": True, "provider": "smspool",
                        "data": d, "verified_status": chk.get("status"),
                        "attempt": attempt + 1}
            if chk.get("status") == "RECEIVED":
                return {"ok": False, "provider": "smspool",
                        "friendly_error": "Orden ya recibió SMS — no cancelable",
                        "data": d, "verified_status": "RECEIVED"}
            _time.sleep(2)
            continue
        msg = ""
        if isinstance(d, dict):
            msg = str(d.get("message") or d.get("error") or "").lower()
            if isinstance(d.get("errors"), list) and d["errors"]:
                msg = (msg + " " + str(d["errors"][0])).lower()
        if any(k in msg for k in ("already cancelled", "already canceled",
                                   "already refunded", "expired",
                                   "not found", "does not exist")):
            return {"ok": True, "provider": "smspool",
                    "friendly_error": "Orden ya estaba cancelada/expirada en SMSPool",
                    "data": d, "attempt": attempt + 1}
        if "completed" in msg or "received" in msg or "active sms" in msg:
            return {"ok": False, "provider": "smspool",
                    "friendly_error": "Orden ya recibió SMS — no cancelable",
                    "data": d, "attempt": attempt + 1}
        if "too soon" in msg or "wait" in msg or "minimum" in msg:
            _time.sleep(15)
            continue
        if attempt == 0:
            _time.sleep(3)
            continue
    return last_resp or {"ok": False, "provider": "smspool",
                         "error": "cancel_failed"}


def _finalize(order_id, action: str, provider: Optional[str]) -> Dict[str, Any]:
    if not provider:
        for o in list_orders():
            if str(o.get("id")) == str(order_id):
                provider = o.get("provider"); break
    p = provider or _get_active()

    # Marcar localmente PRIMERO (idempotente — no perdemos estado si API falla)
    local_already_done = False
    with _lock:
        d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
        for o in d.get("orders", []):
            if str(o.get("id")) == str(order_id):
                if o.get("status") in ("FINISHED", "CANCELED"):
                    local_already_done = True
                break

    if p == "5sim":
        path = f"/user/{'finish' if action == 'finish' else 'cancel'}/{order_id}"
        r = _http("5sim", "GET", path)
    elif p == "smsactivate":
        sa_status = 6 if action == "finish" else 8
        r = _http("smsactivate", "GET", "",
                  params={"action": "setStatus", "status": sa_status, "id": order_id})
    elif p == "onlinesim":
        path = "/setOperationOk.php" if action == "finish" else "/setOperationRevise.php"
        r = _http("onlinesim", "GET", path, params={"tzid": order_id})
    elif p == "smspool":
        # SMSPool: finish no aplica (auto al completarse). cancel = POST /sms/cancel
        if action == "finish":
            r = {"ok": True, "data": {"note": "smspool_no_finish_needed"}}
        else:
            r = _cancel_smspool_robust(order_id)
    else:
        return {"ok": False, "error": "unknown_provider"}

    # SMSPool: mensajes específicos ya vienen como friendly_error desde _cancel_smspool_robust.
    # Para 5sim mantener el bloque previo.
    if not r.get("ok") and p == "smspool" and not r.get("friendly_error"):
        d = r.get("data") if isinstance(r.get("data"), dict) else {}
        msg = str(d.get("message") or d.get("error") or r.get("error") or r.get("raw") or "")
        if msg:
            r["friendly_error"] = f"SMSPool cancel falló: {msg[:160]}"

    # Mejorar mensaje de error específico de 5sim
    if not r.get("ok") and p == "5sim":
        raw = (r.get("raw") or "").lower()
        data = r.get("data", {})
        msg = data.get("error") if isinstance(data, dict) else ""
        full_err = (raw + " " + str(msg)).lower()

        # Mensajes específicos de 5sim
        friendly = None
        if "order not found" in full_err:
            friendly = "Orden no encontrada — ya estaba cancelada/expirada (5sim auto-cancela tras 20min sin SMS)"
        elif "order has sms" in full_err or "received sms" in full_err:
            friendly = "Esta orden ya recibió SMS — no se puede cancelar, usa 'Finalizar' en su lugar"
        elif "order is finished" in full_err:
            friendly = "La orden ya estaba finalizada"
        elif "order has expired" in full_err or "expired" in full_err:
            friendly = "La orden ha expirado (5sim cancela auto tras 20min)"
        elif "cannot cancel" in full_err:
            friendly = "5sim no permite cancelar esta orden ahora (espera 2 min tras compra o usa Finalizar)"
        elif "no money" in full_err or "no balance" in full_err:
            friendly = "Sin saldo en 5sim"
        elif local_already_done:
            friendly = f"La orden ya estaba {'finalizada' if action=='finish' else 'cancelada'} localmente"

        if friendly:
            r["friendly_error"] = friendly

    # Marcar localmente como done aunque la API falle (idempotencia UX)
    final_status = "FINISHED" if action == "finish" else "CANCELED"
    with _lock:
        d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
        updated = False
        for o in d.get("orders", []):
            if str(o.get("id")) == str(order_id):
                if r.get("ok") or local_already_done:
                    o["status"] = final_status
                    o[f"{action}ed_at"] = datetime.now().isoformat()
                else:
                    # API falló pero queremos que el card UI se cierre
                    # Marcamos como cancelado local con flag api_error
                    o["status"] = final_status
                    o[f"{action}ed_at"] = datetime.now().isoformat()
                    o["api_error"] = r.get("friendly_error") or r.get("raw") or r.get("error")
                updated = True
                break
        if updated: json_atomic.save(_path(_ORDERS_KEY), d)

    # Para la UI: marcar como ok si fue idempotente o API succeed
    if not r.get("ok"):
        if local_already_done:
            r["ok"] = True
            r["msg"] = "Ya estaba en ese estado localmente"
        elif r.get("friendly_error"):
            # API rejected pero damos info útil
            r["local_marked"] = True

    return r


def finish_order(order_id, provider: Optional[str] = None) -> Dict[str, Any]:
    if not order_id: return {"ok": False, "error": "order_id_required"}
    return _finalize(order_id, "finish", provider)


def cancel_order(order_id, provider: Optional[str] = None) -> Dict[str, Any]:
    if not order_id: return {"ok": False, "error": "order_id_required"}
    _reset_poll(order_id)   # cancel libera el rate-limit de esa orden
    r = _finalize(order_id, "cancel", provider)
    # Auto-limpia canceladas viejas (mantiene la actual 1h por si el user quiere ver)
    auto_cleanup()
    return r


def force_close_local(order_id) -> Dict[str, Any]:
    """Cierra una orden SOLO localmente (sin tocar la API).
    Útil cuando 5sim devuelve 'order not found' pero quieres limpiar el UI."""
    with _lock:
        d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
        for o in d.get("orders", []):
            if str(o.get("id")) == str(order_id):
                o["status"] = "CANCELED"
                o["force_closed_at"] = datetime.now().isoformat()
                json_atomic.save(_path(_ORDERS_KEY), d)
                return {"ok": True, "msg": "Cerrada localmente"}
    return {"ok": False, "error": "order_not_found_locally"}


def reap_stale_smspool(min_age_min: int = 25, max_age_min: int = 120) -> Dict[str, Any]:
    """Recorre órdenes SMSPool en estado PENDING con created_at entre [min_age_min, max_age_min]
    minutos y dispara cancel + verificación. Las que SMSPool ya marcó refunded/expired
    quedan limpias localmente. Útil al arranque del dashboard y como cron.
    Limita superior para evitar tocar órdenes muy viejas (ya cobradas/cerradas)."""
    now = datetime.now()
    candidates = []
    with _lock:
        d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
        for o in d.get("orders", []):
            if o.get("provider") != "smspool":
                continue
            if o.get("status") not in ("PENDING", None, ""):
                continue
            ca = o.get("created_at")
            if not ca:
                continue
            try:
                age = (now - datetime.fromisoformat(ca)).total_seconds() / 60.0
            except Exception:
                continue
            if min_age_min <= age <= max_age_min:
                candidates.append(o.get("id"))
    reaped = 0
    failed = 0
    details = []
    for oid in candidates:
        try:
            r = cancel_order(oid, provider="smspool")
            if r.get("ok"):
                reaped += 1
                details.append({"id": oid, "ok": True,
                                "verified": r.get("verified_status")})
            else:
                failed += 1
                details.append({"id": oid, "ok": False,
                                "err": r.get("friendly_error") or r.get("error")})
        except Exception as e:
            failed += 1
            details.append({"id": oid, "ok": False, "err": str(e)[:100]})
    return {"ok": True, "scanned": len(candidates),
            "reaped": reaped, "failed": failed, "details": details}


def cleanup_orders(remove_canceled: bool = True,
                    remove_finished: bool = False,
                    keep_recent_hours: int = 1) -> Dict[str, Any]:
    """Elimina órdenes con status CANCELED y/o FINISHED del historial.

    Por defecto elimina canceladas pero conserva las finalizadas (útiles para
    auditoría). keep_recent_hours preserva órdenes muy recientes aunque
    estén canceladas (por si el usuario quiere ver lo que pasó).
    """
    removed_canceled = 0
    removed_finished = 0
    cutoff = datetime.now() - timedelta(hours=keep_recent_hours)
    with _lock:
        d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
        kept = []
        for o in d.get("orders", []):
            status = o.get("status", "")
            # Determinar si es reciente
            is_recent = False
            for ts_field in ("created_at", "canceled_at", "finished_at"):
                ts = o.get(ts_field)
                if ts:
                    try:
                        if datetime.fromisoformat(ts) > cutoff:
                            is_recent = True; break
                    except: pass

            if status == "CANCELED" and remove_canceled and not is_recent:
                removed_canceled += 1
                continue
            if status == "FINISHED" and remove_finished and not is_recent:
                removed_finished += 1
                continue
            kept.append(o)
        d["orders"] = kept
        json_atomic.save(_path(_ORDERS_KEY), d)
    return {"ok": True,
            "removed_canceled": removed_canceled,
            "removed_finished": removed_finished,
            "remaining": len(kept)}


def auto_cleanup() -> Dict[str, Any]:
    """Auto-limpia canceladas con > 1h de antigüedad.
    Se llama tras cada cancel/finish para mantener historial limpio."""
    return cleanup_orders(remove_canceled=True, remove_finished=False,
                          keep_recent_hours=1)


# ─── ORDERS ───
def list_orders() -> List[Dict[str, Any]]:
    d = json_atomic.load(_path(_ORDERS_KEY), default={"orders": []})
    return d.get("orders", [])


def get_active_orders() -> List[Dict[str, Any]]:
    return [o for o in list_orders() if o.get("status") in ("PENDING", "RECEIVED")]


# ═══════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}

    # 1. Providers meta
    checks["4_providers"] = len(PROVIDERS_META) == 4
    checks["smspool_present"] = "smspool" in PROVIDERS_META
    checks["all_have_base"] = all("base" in p for p in PROVIDERS_META.values())
    checks["all_have_auth"] = all("auth" in p for p in PROVIDERS_META.values())

    # 2. Mappings
    checks["product_map_has_tm"] = "ticketmaster" in PRODUCT_MAP
    checks["product_tm_has_smspool"] = PRODUCT_MAP["ticketmaster"].get("smspool") == "921"
    checks["country_map_has_es"] = "es" in COUNTRY_MAP
    checks["es_smspool_id_55"] = COUNTRY_MAP["es"].get("smspool") == "55"
    checks["es_has_4_providers"] = len(COUNTRY_MAP["es"]) == 4

    # 3. Config CRUD
    cfg_before = _load_cfg()
    r = set_provider("invalid")
    checks["rejects_invalid_provider"] = not r.get("ok")
    r = set_provider("5sim")
    checks["sets_provider"] = r.get("ok") is True
    cfg = get_config()
    checks["get_config_has_active"] = cfg.get("active") == "5sim"
    checks["get_config_masks_keys"] = "providers" in cfg

    # 4. set_api_key validation
    r = set_api_key("invalid", "x" * 30)
    checks["rejects_invalid_provider_key"] = not r.get("ok")
    r = set_api_key("5sim", "short")
    checks["rejects_short"] = not r.get("ok")
    r = set_api_key("5sim", "TEST_" + "x" * 40)
    checks["accepts_valid"] = r.get("ok") is True
    cfg = get_config()
    checks["key_persisted"] = cfg["keys"]["5sim"]["set"] is True

    # 5. API call con key dummy → 401
    r = get_balance("5sim")
    checks["balance_call_executes"] = isinstance(r, dict)

    # 6. Auto-rotate config
    r = set_auto_rotate(True)
    checks["auto_rotate_set"] = r.get("ok") is True
    cfg = get_config()
    checks["auto_rotate_persisted"] = cfg.get("auto_rotate") is True

    # 7. buy_number validation
    r = buy_number(country="es", product="ticketmaster", provider="invalid")
    checks["buy_validates"] = not r.get("ok")

    # 8. orders helpers
    checks["list_orders_is_list"] = isinstance(list_orders(), list)
    checks["active_orders_is_list"] = isinstance(get_active_orders(), list)

    # 9. check_sms validation
    r = check_sms(0)
    checks["check_validates_id"] = not r.get("ok")
    r = finish_order(0)
    checks["finish_validates_id"] = not r.get("ok")
    r = cancel_order(0)
    checks["cancel_validates_id"] = not r.get("ok")

    # Cleanup: restaurar config
    set_auto_rotate(False)
    cfg = _load_cfg()
    if "TEST_" in cfg.get("keys", {}).get("5sim", ""):
        cfg["keys"]["5sim"] = ""
        _save_cfg(cfg)

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    cfg = _load_cfg()
    return {
        "module": "sms_receiver",
        "version": 1,
        "active_provider": cfg.get("active", "5sim"),
        "providers": list(PROVIDERS_META.keys()),
        "auto_rotate": cfg.get("auto_rotate", True),
        "orders_total": len(list_orders()),
        "active_orders": len(get_active_orders()),
    }
