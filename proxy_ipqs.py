"""proxy_ipqs.py — Pre-check proxy IPs con ipqualityscore.com / proxycheck.io.

Verifica fraud score + blacklist ANTES de asignar proxy al bot.
Evita bans silenciosos por usar IPs ya quemadas.

Planes:
  - IPQualityScore: 5000 lookups/mes gratis, $0.002 extra
  - proxycheck.io: 1000/día free, $0.0004 cada 1000 extra

API:
  save_config({provider, api_key})
  check_ip(ip) → {fraud_score, blacklisted, ...}
  prescreen_before_use(proxy_url) → bool
  batch_check(ips) → parallel lookups
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import json_atomic

_CONFIG_KEY = "proxy_ipqs_config.json"
_CACHE_KEY = "proxy_ipqs_cache.json"
_lock = threading.Lock()

CACHE_TTL_HOURS = 24  # re-check cada 24h


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "provider": "ipqualityscore",     # ipqualityscore | proxycheck
        "api_key": "",
        "fraud_score_threshold": 75,       # rechazar si score >= 75
        "allow_proxy_type_datacenter": False,
        "allow_proxy_type_residential": True,
        "allow_proxy_type_mobile": True,
        "cache_ttl_hours": CACHE_TTL_HOURS,
        "mode": "simulate",
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def is_configured() -> bool:
    cfg = get_config()
    return bool(cfg.get("api_key")) and cfg.get("mode") == "live"


def _load_cache() -> dict:
    return json_atomic.load(config.ruta(_CACHE_KEY), default={"cache": {}})


def _save_cache(d: dict):
    json_atomic.save(config.ruta(_CACHE_KEY), d)


def _get_cached(ip: str) -> Optional[dict]:
    d = _load_cache()
    entry = d.get("cache", {}).get(ip)
    if not entry:
        return None
    try:
        cached_at = datetime.fromisoformat(entry.get("cached_at", ""))
        ttl = get_config().get("cache_ttl_hours", CACHE_TTL_HOURS)
        if datetime.now() - cached_at > timedelta(hours=ttl):
            return None
    except Exception:
        return None
    return entry


def _cache_result(ip: str, result: dict):
    with _lock:
        d = _load_cache()
        d.setdefault("cache", {})[ip] = {
            **result,
            "cached_at": datetime.now().isoformat(),
        }
        # Cap cache
        if len(d["cache"]) > 10000:
            # Remove older half
            items = sorted(d["cache"].items(),
                            key=lambda x: x[1].get("cached_at", ""))
            d["cache"] = dict(items[len(items)//2:])
        _save_cache(d)


def check_ip(ip: str, use_cache: bool = True) -> dict:
    """Pre-check IP con IPQualityScore o proxycheck.io.

    Returns:
      {ok, ip, fraud_score, is_proxy, is_vpn, is_tor,
       proxy_type, country_code, recommended_action}
    """
    if not ip:
        return {"ok": False, "error": "ip vacío"}

    # Cache hit
    if use_cache:
        cached = _get_cached(ip)
        if cached:
            cached["from_cache"] = True
            return cached

    cfg = get_config()

    # Simulate mode
    if cfg.get("mode") == "simulate":
        import random
        fake = {
            "ok": True, "mode": "simulate", "ip": ip,
            "fraud_score": random.randint(0, 100),
            "is_proxy": random.random() > 0.7,
            "is_vpn": False, "is_tor": False,
            "proxy_type": random.choice(["residential", "mobile", "datacenter"]),
            "country_code": random.choice(["US", "ES", "UK", "DE"]),
            "isp": "Mock ISP",
        }
        thresh = cfg.get("fraud_score_threshold", 75)
        fake["recommended_action"] = (
            "reject" if fake["fraud_score"] >= thresh
            else ("caution" if fake["fraud_score"] >= 50 else "allow")
        )
        _cache_result(ip, fake)
        return fake

    # LIVE mode
    if not cfg.get("api_key"):
        return {"ok": False, "error": "api_key requerida para mode=live",
                 "hint": "save_config({'api_key': 'xxx', 'mode': 'live'})"}

    import requests
    try:
        if cfg["provider"] == "proxycheck":
            url = f"https://proxycheck.io/v2/{ip}?key={cfg['api_key']}&vpn=1&asn=1&risk=1"
            r = requests.get(url, timeout=8)
            data = r.json()
            ip_data = data.get(ip, {})
            result = {
                "ok": True, "mode": "live", "ip": ip,
                "fraud_score": int(ip_data.get("risk", 0)),
                "is_proxy": ip_data.get("proxy") == "yes",
                "is_vpn": ip_data.get("type") == "VPN",
                "is_tor": ip_data.get("type") == "TOR",
                "proxy_type": ip_data.get("type", "unknown").lower(),
                "country_code": ip_data.get("isocode"),
                "isp": ip_data.get("provider"),
            }
        else:  # ipqualityscore
            url = f"https://www.ipqualityscore.com/api/json/ip/{cfg['api_key']}/{ip}"
            r = requests.get(url, params={"strictness": 1}, timeout=8)
            data = r.json()
            result = {
                "ok": data.get("success", False), "mode": "live", "ip": ip,
                "fraud_score": data.get("fraud_score", 0),
                "is_proxy": data.get("proxy", False),
                "is_vpn": data.get("vpn", False),
                "is_tor": data.get("tor", False),
                "proxy_type": "mobile" if data.get("mobile") else
                               ("residential" if data.get("is_crawler") is False else "datacenter"),
                "country_code": data.get("country_code"),
                "isp": data.get("ISP"),
            }

        thresh = cfg.get("fraud_score_threshold", 75)
        result["recommended_action"] = (
            "reject" if result.get("fraud_score", 0) >= thresh
            else ("caution" if result.get("fraud_score", 0) >= 50 else "allow")
        )
        _cache_result(ip, result)
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}


def prescreen_before_use(proxy_url: str) -> dict:
    """Extrae IP del proxy_url y ejecuta check + decisión allow/reject.

    proxy_url formato: http://user:pass@host:port o http://ip:port
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname
    except Exception:
        return {"ok": False, "error": "proxy_url malformado"}

    if not host:
        return {"ok": False, "error": "no host en proxy_url"}

    # Si es hostname no-IP, intentar resolver
    import socket
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = host

    check = check_ip(ip)
    if not check.get("ok"):
        # Si no podemos chequear, default allow (fail open)
        return {"ok": True, "allow": True, "reason": "check_failed_fail_open",
                 "check_error": check.get("error")}

    action = check.get("recommended_action", "allow")
    cfg = get_config()
    allowed = action != "reject"

    # Check por tipo
    proxy_type = check.get("proxy_type", "unknown")
    if proxy_type == "datacenter" and not cfg.get("allow_proxy_type_datacenter"):
        allowed = False
        action = "reject_datacenter"
    if check.get("is_tor") and not cfg.get("allow_tor", False):
        allowed = False
        action = "reject_tor"

    return {
        "ok": True, "allow": allowed,
        "action": action,
        "ip": ip, "fraud_score": check.get("fraud_score"),
        "proxy_type": proxy_type,
        "country": check.get("country_code"),
        "from_cache": check.get("from_cache", False),
    }


def batch_check(ips: List[str], max_workers: int = 10) -> List[dict]:
    """Check N IPs en paralelo."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(check_ip, ip): ip for ip in ips}
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def get_cache_stats() -> dict:
    d = _load_cache()
    cache = d.get("cache", {})
    by_country = {}
    by_action = {}
    by_type = {}
    for entry in cache.values():
        c = entry.get("country_code", "?")
        by_country[c] = by_country.get(c, 0) + 1
        a = entry.get("recommended_action", "?")
        by_action[a] = by_action.get(a, 0) + 1
        t = entry.get("proxy_type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "cached_ips": len(cache),
        "by_country": by_country,
        "by_action": by_action,
        "by_proxy_type": by_type,
    }


def clear_cache() -> dict:
    _save_cache({"cache": {}})
    return {"ok": True, "cleared": True}


def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    cfg = get_config()
    results["checks"]["config_mode"] = cfg.get("mode")

    # check_ip simulate
    r = check_ip("8.8.8.8")
    results["checks"]["check_ip_ok"] = r.get("ok", False)
    results["checks"]["has_fraud_score"] = "fraud_score" in r
    results["checks"]["has_recommended_action"] = "recommended_action" in r

    # prescreen
    pre = prescreen_before_use("http://user:pass@1.2.3.4:8080")
    results["checks"]["prescreen_ok"] = pre.get("ok", False)
    results["checks"]["has_allow_decision"] = "allow" in pre

    # batch_check
    batch = batch_check(["1.1.1.1", "8.8.8.8", "9.9.9.9"], max_workers=3)
    results["checks"]["batch_count"] = len(batch)

    # cache stats
    stats = get_cache_stats()
    results["checks"]["cache_accessible"] = "cached_ips" in stats

    return results


def get_info() -> dict:
    return {
        "module": "proxy_ipqs",
        "configured": is_configured(),
        "cache": get_cache_stats(),
        "config": {k: v for k, v in get_config().items() if k != "api_key"},
    }
