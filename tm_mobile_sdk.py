"""tm_mobile_sdk.py — Ticketmaster iOS/Android Mobile SDK impersonation.

Los endpoints del SDK móvil TM (iOS Purchase SDK 0.12.1+, Android SDK) tienen
**MENOS protección** que la web (sin Akamai full, sin reCAPTCHA v3, más laxos).

Estrategia:
  1. Replicar headers exactos del iOS SDK (Accept, User-Agent, X-TM-* headers)
  2. Usar endpoints `/discovery/v2/`, `/commerce/v2/`, `/identity-api/v2/`
  3. TLS fingerprint iOS (NSURLSession) via tls-client safari_ios_17
  4. Device attestation tokens (placeholder, requiere SDK real)

API:
  get_mobile_headers(platform, app_version)
  fetch_event_info(event_id, platform='ios')
  search_events(query, geo, platform='ios')
  generate_device_id()
  get_session_id()
"""
import hashlib
import json
import secrets
import threading
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, List

import config
import json_atomic

_CONFIG_KEY = "tm_mobile_sdk_config.json"
_LOG_KEY = "tm_mobile_sdk_log.json"
_lock = threading.Lock()

# iOS Purchase SDK 0.12.1+ typical headers
IOS_APP_VERSION = "11.11.1"
IOS_SDK_VERSION = "0.12.1"
ANDROID_APP_VERSION = "282.0.0"
ANDROID_SDK_VERSION = "10.4.0"

# Endpoints públicos (Discovery API) — OK, son oficiales
DISCOVERY_V2 = "https://app.ticketmaster.com/discovery/v2"
COMMERCE_V2 = "https://app.ticketmaster.com/commerce/v2"


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "enabled": True,
        "default_platform": "ios",
        "discovery_api_key": "",
        "default_market_id": "122",  # EE.UU.
        "mode": "simulate",
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def generate_device_id(platform: str = "ios") -> str:
    """Genera un device_id realista tipo UUID."""
    # iOS usa UUID v4 uppercase
    # Android usa SSAID (64-bit hex)
    if platform == "ios":
        return str(uuid.uuid4()).upper()
    else:
        return secrets.token_hex(8)  # 16 hex chars


def get_mobile_headers(platform: str = "ios",
                         app_version: Optional[str] = None,
                         session_id: Optional[str] = None,
                         device_id: Optional[str] = None) -> Dict[str, str]:
    """Devuelve headers exactos que envía el SDK móvil."""
    session_id = session_id or ("sess_" + secrets.token_hex(8))
    device_id = device_id or generate_device_id(platform)

    if platform == "ios":
        app_ver = app_version or IOS_APP_VERSION
        return {
            "User-Agent": f"Ticketmaster/{app_ver} (iPhone; iOS 17.2.1; Scale/3.00)",
            "X-TM-APP-PLATFORM": "iOS",
            "X-TM-APP-VERSION": app_ver,
            "X-TM-SDK-VERSION": IOS_SDK_VERSION,
            "X-TM-Device-Id": device_id,
            "X-TM-Session-Id": session_id,
            "Accept": "application/json",
            "Accept-Language": "en-US;q=1.0",
            "Accept-Encoding": "gzip, deflate, br",
        }
    else:  # android
        app_ver = app_version or ANDROID_APP_VERSION
        return {
            "User-Agent": f"Ticketmaster/{app_ver} (Linux; Android 14; Pixel 8 Pro Build/UQ1A.240105.004)",
            "X-TM-APP-PLATFORM": "Android",
            "X-TM-APP-VERSION": app_ver,
            "X-TM-SDK-VERSION": ANDROID_SDK_VERSION,
            "X-TM-Device-Id": device_id,
            "X-TM-Session-Id": session_id,
            "Accept": "application/json",
            "Accept-Language": "en-US",
            "Accept-Encoding": "gzip",
        }


def _log_call(op: str, status: int, elapsed_ms: int, platform: str, error: str = ""):
    try:
        with _lock:
            d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
            d["log"].insert(0, {
                "ts": datetime.now().isoformat(),
                "op": op, "status": status, "elapsed_ms": elapsed_ms,
                "platform": platform, "error": error[:150],
            })
            d["log"] = d["log"][:200]
            json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


def fetch_event_info(event_id: str,
                        platform: str = "ios",
                        tls_profile: Optional[str] = None) -> Dict:
    """Fetch event info usando el endpoint Discovery API v2.

    En modo simulate devuelve mock. En live hace request real (Discovery API
    oficial de TM — pública).
    """
    cfg = get_config()
    started = time.time()

    if cfg.get("mode") == "simulate":
        _log_call("fetch_event_info", 200, 10, platform)
        return {
            "ok": True, "mode": "simulate",
            "event_id": event_id,
            "name": "Mock Event via Mobile SDK",
            "venue": "Mock Arena",
            "platform": platform,
            "note": "simulate — configura discovery_api_key para llamadas reales",
        }

    if not cfg.get("discovery_api_key"):
        return {"ok": False, "error": "discovery_api_key requerida para live mode"}

    # Live: Discovery API v2 (endpoint público con API key)
    try:
        import requests
        headers = get_mobile_headers(platform=platform)
        params = {"apikey": cfg["discovery_api_key"]}

        # Prefer tls-client para spoof iOS NSURLSession
        if tls_profile:
            try:
                import tls_client
                session = tls_client.Session(client_identifier=tls_profile or "safari_ios_17_0")
                r = session.get(f"{DISCOVERY_V2}/events/{event_id}.json",
                                  params=params, headers=headers)
            except Exception:
                r = requests.get(f"{DISCOVERY_V2}/events/{event_id}.json",
                                  params=params, headers=headers, timeout=10)
        else:
            r = requests.get(f"{DISCOVERY_V2}/events/{event_id}.json",
                              params=params, headers=headers, timeout=10)

        elapsed_ms = int((time.time() - started) * 1000)
        _log_call("fetch_event_info", r.status_code, elapsed_ms, platform)

        if r.status_code == 200:
            data = r.json() if hasattr(r, "json") else json.loads(r.text)
            return {
                "ok": True, "mode": "live",
                "platform": platform,
                "event_id": event_id,
                "name": data.get("name"),
                "dates": data.get("dates"),
                "venue": (data.get("_embedded", {}).get("venues", [{}])[0].get("name")),
                "status": data.get("dates", {}).get("status", {}).get("code"),
                "elapsed_ms": elapsed_ms,
            }
        return {"ok": False, "status": r.status_code,
                 "error": r.text[:200], "elapsed_ms": elapsed_ms}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def search_events(query: str = "",
                    country_code: str = "US",
                    city: str = "",
                    platform: str = "ios",
                    size: int = 20) -> Dict:
    """Search events via Discovery API v2."""
    cfg = get_config()
    started = time.time()

    if cfg.get("mode") == "simulate":
        return {
            "ok": True, "mode": "simulate",
            "events": [{"id": f"mock_{i}", "name": f"Mock Event {i}"} for i in range(min(size, 5))],
            "note": "simulate",
        }

    if not cfg.get("discovery_api_key"):
        return {"ok": False, "error": "discovery_api_key requerida"}

    try:
        import requests
        headers = get_mobile_headers(platform=platform)
        params = {
            "apikey": cfg["discovery_api_key"],
            "size": size,
            "countryCode": country_code,
        }
        if query: params["keyword"] = query
        if city: params["city"] = city

        r = requests.get(f"{DISCOVERY_V2}/events.json",
                          params=params, headers=headers, timeout=10)
        elapsed_ms = int((time.time() - started) * 1000)
        _log_call("search_events", r.status_code, elapsed_ms, platform)

        if r.status_code == 200:
            data = r.json()
            events = data.get("_embedded", {}).get("events", [])
            return {
                "ok": True, "mode": "live",
                "count": len(events),
                "page": data.get("page", {}),
                "events": [{
                    "id": e.get("id"), "name": e.get("name"),
                    "date": e.get("dates", {}).get("start", {}).get("localDate"),
                    "venue": (e.get("_embedded", {}).get("venues", [{}])[0].get("name")),
                    "url": e.get("url"),
                } for e in events],
                "elapsed_ms": elapsed_ms,
            }
        return {"ok": False, "status": r.status_code, "error": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def get_stats() -> Dict:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
    log = d.get("log", [])
    by_op = {}
    by_platform = {}
    total_success = 0
    for e in log:
        op = e.get("op", "?")
        by_op[op] = by_op.get(op, 0) + 1
        p = e.get("platform", "?")
        by_platform[p] = by_platform.get(p, 0) + 1
        if e.get("status", 0) == 200:
            total_success += 1
    return {
        "total_calls": len(log),
        "success_calls": total_success,
        "by_op": by_op,
        "by_platform": by_platform,
        "mode": get_config().get("mode"),
        "configured": bool(get_config().get("discovery_api_key")),
    }


def self_test() -> Dict:
    results = {"ok": True, "checks": {}}

    # Headers
    ios_h = get_mobile_headers("ios")
    results["checks"]["ios_headers_ok"] = "X-TM-APP-PLATFORM" in ios_h
    results["checks"]["ios_has_ua"] = "iPhone" in ios_h.get("User-Agent", "")

    android_h = get_mobile_headers("android")
    results["checks"]["android_headers_ok"] = android_h.get("X-TM-APP-PLATFORM") == "Android"
    results["checks"]["android_has_ua"] = "Android" in android_h.get("User-Agent", "")

    # Device ID
    did = generate_device_id("ios")
    results["checks"]["ios_device_id_uuid"] = len(did) == 36 and did.count("-") == 4

    # Simulate fetch
    r = fetch_event_info("test_event_id", platform="ios")
    results["checks"]["fetch_event_simulate"] = r.get("ok", False)

    # Simulate search
    rs = search_events("Taylor Swift", country_code="US", platform="android")
    results["checks"]["search_simulate"] = rs.get("ok", False)

    # Stats
    s = get_stats()
    results["checks"]["stats_ok"] = "total_calls" in s

    return results


def get_info() -> Dict:
    return {
        "module": "tm_mobile_sdk",
        "ios_app_version": IOS_APP_VERSION,
        "ios_sdk_version": IOS_SDK_VERSION,
        "android_app_version": ANDROID_APP_VERSION,
        "android_sdk_version": ANDROID_SDK_VERSION,
        "discovery_v2_endpoint": DISCOVERY_V2,
        "commerce_v2_endpoint": COMMERCE_V2,
        "stats": get_stats(),
    }
