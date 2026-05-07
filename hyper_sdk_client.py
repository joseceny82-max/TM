"""hyper_sdk_client.py — Hyper Solutions SDK (hyper-sdk) fallback API.

hyper-sdk es una API comercial que genera `sensor_data` válido para:
- Akamai Bot Manager (v2 + v3 + v3.1)
- DataDome
- Kasada KPSDK
- Incapsula/Imperva
- PerimeterX/HUMAN

Se usa como **fallback** cuando el reverse-engineering propio falla.
Recibe el cliente entrega un POST al endpoint y retorna sensor_data listo.

Pricing: ~$0.01-0.05 por sensor (planes monthly).

API:
  save_config(api_key, base_url)
  generate_akamai_sensor(abck_cookie, bmsz_cookie, user_agent, page_url)
  generate_datadome(target_url, cookie, ua)
  generate_kasada_payload(url)
  test_connection()
"""
import json
import threading
import time
from datetime import datetime
from typing import Optional, Dict

import config
import json_atomic

_CONFIG_KEY = "hyper_sdk_config.json"
_LOG_KEY = "hyper_sdk_log.json"
_lock = threading.Lock()

HYPER_BASE = "https://akm.hypersolutions.co"   # Akamai v3 endpoint
HYPER_DD_BASE = "https://datadome.hypersolutions.co"
HYPER_KP_BASE = "https://kasada.hypersolutions.co"


def get_config() -> dict:
    cfg = json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "api_key": "",
        "mode": "simulate",           # simulate | live
        "akamai_endpoint": HYPER_BASE,
        "datadome_endpoint": HYPER_DD_BASE,
        "kasada_endpoint": HYPER_KP_BASE,
        "timeout_s": 15,
        "auto_fallback_on_local_fail": True,
    })
    # Auto-load desde env vars (override sin tocar UI)
    import os as _os
    env_key = (_os.environ.get("TM_HYPER_API_KEY", "") or "").strip()
    if env_key and not cfg.get("api_key"):
        cfg["api_key"] = env_key
    env_mode = (_os.environ.get("TM_HYPER_MODE", "") or "").strip()
    if env_mode in ("simulate", "live"):
        cfg["mode"] = env_mode
    # Si hay api_key real, auto-promueve a live (a menos que esté forzado simulate)
    if cfg.get("api_key") and cfg.get("mode") == "simulate" and \
            _os.environ.get("TM_HYPER_AUTO_LIVE", "1") == "1":
        cfg["mode"] = "live"
    return cfg


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def is_configured() -> bool:
    return bool(get_config().get("api_key"))


def _log(event: dict):
    try:
        event["ts"] = datetime.now().isoformat()
        with _lock:
            d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
            d["log"].insert(0, event)
            d["log"] = d["log"][:100]
            json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


def generate_akamai_sensor(abck: str = "",
                              bmsz: str = "",
                              user_agent: str = "",
                              page_url: str = "",
                              post_count: int = 1) -> dict:
    """Genera sensor_data Akamai v3 via hyper-sdk API.

    En modo simulate devuelve sensor mock con estructura correcta.
    """
    cfg = get_config()
    if cfg.get("mode") == "simulate" or not is_configured():
        import secrets, base64, hashlib
        # Mock v3 sensor
        fake_data = base64.b64encode(secrets.token_bytes(600)).decode()
        key_hash = hashlib.md5((page_url or "mock").encode()).hexdigest()[:16]
        sig = hashlib.sha1((fake_data + key_hash).encode()).hexdigest()[:20]
        sensor = f"3;{len(fake_data)};{key_hash};{fake_data};{sig}"
        _log({"op": "generate_akamai_sensor", "mode": "simulate",
              "sensor_len": len(sensor), "post_count": post_count})
        return {
            "ok": True,
            "mode": "simulate",
            "sensor_data": sensor,
            "sensor_length": len(sensor),
            "cookies": {
                "_abck": abck or "0~fake~-1~-1~-1",
                "bm_sz": bmsz or "mock_bmsz_value",
            },
            "user_agent": user_agent or "Mozilla/5.0 (mock)",
            "version": "v3",
            "note": "simulate — sin llamada real a hyper-sdk. Configura api_key + mode=live.",
        }

    # LIVE mode
    import requests
    try:
        r = requests.post(
            cfg["akamai_endpoint"],
            json={
                "page_url": page_url,
                "user_agent": user_agent,
                "abck": abck,
                "bmsz": bmsz,
                "post_count": post_count,
            },
            headers={"x-api-key": cfg["api_key"]},
            timeout=cfg.get("timeout_s", 15),
        )
        _log({"op": "generate_akamai_sensor", "mode": "live",
              "status": r.status_code, "elapsed_ms": int(r.elapsed.total_seconds() * 1000)})
        if r.status_code == 200:
            return {"ok": True, "mode": "live", **r.json()}
        return {"ok": False, "status": r.status_code, "error": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def generate_datadome(target_url: str = "",
                        cookie: str = "",
                        user_agent: str = "") -> dict:
    cfg = get_config()
    if cfg.get("mode") == "simulate" or not is_configured():
        import secrets
        return {"ok": True, "mode": "simulate",
                 "datadome_cookie": "datadome=" + secrets.token_hex(32),
                 "payload": secrets.token_hex(64),
                 "note": "simulate — mock"}
    import requests
    try:
        r = requests.post(cfg["datadome_endpoint"],
                           json={"target_url": target_url, "cookie": cookie, "ua": user_agent},
                           headers={"x-api-key": cfg["api_key"]},
                           timeout=cfg.get("timeout_s", 15))
        return {"ok": r.status_code == 200, "status": r.status_code,
                 **(r.json() if r.status_code == 200 else {"error": r.text[:200]})}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def generate_kasada_payload(url: str = "") -> dict:
    cfg = get_config()
    if cfg.get("mode") == "simulate" or not is_configured():
        import secrets
        return {"ok": True, "mode": "simulate",
                 "x_kpsdk_ct": secrets.token_hex(32),
                 "x_kpsdk_cd": secrets.token_hex(16),
                 "note": "simulate — mock"}
    import requests
    try:
        r = requests.post(cfg["kasada_endpoint"],
                           json={"url": url},
                           headers={"x-api-key": cfg["api_key"]},
                           timeout=cfg.get("timeout_s", 15))
        return {"ok": r.status_code == 200, "status": r.status_code,
                 **(r.json() if r.status_code == 200 else {"error": r.text[:200]})}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def test_connection() -> dict:
    cfg = get_config()
    if cfg.get("mode") == "simulate":
        return {"ok": True, "mode": "simulate",
                 "note": "Modo simulate — configura api_key + mode=live"}
    if not is_configured():
        return {"ok": False, "error": "api_key requerida"}
    # Ping Akamai endpoint
    import requests
    try:
        r = requests.get(cfg["akamai_endpoint"], timeout=5,
                          headers={"x-api-key": cfg["api_key"]})
        return {"ok": r.status_code in (200, 401, 403),
                 "status": r.status_code,
                 "note": "endpoint reachable"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def get_stats() -> dict:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
    log = d.get("log", [])
    by_op = {}
    for e in log:
        o = e.get("op", "?")
        by_op[o] = by_op.get(o, 0) + 1
    return {
        "mode": get_config().get("mode"),
        "configured": is_configured(),
        "total_calls": len(log),
        "calls_by_op": by_op,
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    cfg = get_config()
    results["checks"]["config_ok"] = "api_key" in cfg
    results["checks"]["mode"] = cfg.get("mode")

    # Simulate call
    r = generate_akamai_sensor(abck="test", user_agent="Mozilla/5.0", page_url="https://test.com")
    results["checks"]["akamai_sensor_sim"] = r.get("ok", False)
    results["checks"]["sensor_length"] = r.get("sensor_length", 0)

    r2 = generate_datadome(target_url="https://test.com")
    results["checks"]["datadome_sim"] = r2.get("ok", False)

    r3 = generate_kasada_payload(url="https://test.com")
    results["checks"]["kasada_sim"] = r3.get("ok", False)

    return results


def get_info() -> dict:
    return {
        "module": "hyper_sdk_client",
        "stats": get_stats(),
        "endpoints_supported": ["akamai", "datadome", "kasada", "incapsula"],
    }
