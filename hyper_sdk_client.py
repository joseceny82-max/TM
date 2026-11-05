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

    # LIVE mode — flow OFICIAL Hyper Solutions Akamai (3 POSTs):
    # https://docs.hypersolutions.co/akamai-web/getting-started
    #
    # 1. GET https://target/ con curl_cffi Chrome impersonate
    #    → HTML + cookies iniciales (_abck "~-1~", bm_sz)
    # 2. parse_akamai_script_path(html) → URL del bmak.js
    # 3. GET bmak_url → contenido del script
    # 4. generate_sensor_data(script=<content>, context="") → sensor1, ctx1
    # 5. POST sensor1 al bmak_url → new cookies (_abck rotado)
    # 6. generate_sensor_data(script="", context=ctx1) → sensor2, ctx2
    # 7. POST sensor2 → new cookies
    # 8. Si _abck contiene "~0~" → DONE
    #    Sino: generate_sensor_data(script="", context=ctx2) → sensor3
    #          POST sensor3 → final cookies con "~0~"
    # 9. Retornar cookies finales para inyectar en bot session.
    import time as _t
    t0 = _t.time()
    try:
        from hyper_sdk import Session, SensorInput, parse_akamai_script_path
        try:
            from curl_cffi import requests as _rq
            _impersonate = "chrome131"
            _curl_cffi_available = True
        except ImportError:
            import requests as _rq
            _impersonate = None
            _curl_cffi_available = False
        from urllib.parse import urljoin

        UA = user_agent or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
        target_url = page_url or "https://www.ticketmaster.es/"
        IP = "88.26.11.159"   # public IP (Hyper requiere ip válido)
        ACCEPT_LANG = "es-ES,es;q=0.9,en;q=0.8"

        # Sesión HTTP persistente para mantener cookies entre POSTs
        if _curl_cffi_available:
            http_sess = _rq.Session(impersonate=_impersonate)
        else:
            http_sess = _rq.Session()
            http_sess.headers["User-Agent"] = UA

        # ─── PASO 1: GET home → cookies iniciales ───
        home_r = http_sess.get(target_url, timeout=cfg.get("timeout_s", 15))
        home_html = home_r.text
        abck_cur = abck or http_sess.cookies.get('_abck') or ''
        bmsz_cur = bmsz or http_sess.cookies.get('bm_sz') or ''
        _log({"op": "hyper_step1_home_get",
              "status": home_r.status_code,
              "abck_init_len": len(abck_cur),
              "bmsz_init_len": len(bmsz_cur)})

        # ─── PASO 2-3: parse + fetch bmak.js ───
        bmak_url = ""
        bmak_script = ""
        try:
            bmak_path = parse_akamai_script_path(home_html)
            if bmak_path:
                bmak_url = urljoin(target_url, bmak_path)
                s_r = http_sess.get(bmak_url, timeout=cfg.get("timeout_s", 15))
                if s_r.status_code == 200:
                    bmak_script = s_r.text
                _log({"op": "hyper_step2_script",
                      "bmak_url": bmak_url[:120],
                      "script_len": len(bmak_script)})
        except Exception as _pe:
            _log({"op": "hyper_parse_script_err", "err": str(_pe)[:80]})

        # ─── PASO 4-8: 3 POSTs de sensor_data con rotación de context ───
        sess = Session(cfg["api_key"])
        sensor_context = ""
        posts_done = 0
        final_abck = abck_cur
        post_results = []

        # Fallback: si no se obtuvo bmak.js (TM no lo sirve en home — solo en
        # /checkout/), usar un stub mínimo válido como `script` inicial.
        # La API requiere `script OR context must be set` en el primer POST.
        if not bmak_script:
            bmak_script = "(function(){window.bmak={ver:3};})();"
        if not bmak_url:
            bmak_url = urljoin(target_url, "/_bm/_data")

        for post_idx in range(3):
            # SensorInput: 1st request → script+context="";  2nd/3rd → script="", context=anterior
            is_first = (post_idx == 0)
            si = SensorInput(
                abck=final_abck or abck_cur or "AAAAAAAA~-1~-1~-1",
                bmsz=bmsz_cur or "TEST123",
                version="3",
                page_url=target_url,
                user_agent=UA,
                ip=IP,
                accept_language=ACCEPT_LANG,
                context=("" if is_first else sensor_context),
                script=(bmak_script if is_first else ""),
                script_url=(bmak_url if is_first else ""),
            )
            t_gen = _t.time()
            sensor_data, sensor_context_new = sess.generate_sensor_data(si)
            gen_ms = int((_t.time() - t_gen) * 1000)
            sensor_context = sensor_context_new or sensor_context

            # POST sensor_data al endpoint bmak (mismo path del script)
            post_status = 0
            post_ms = 0
            if bmak_url:
                t_post = _t.time()
                try:
                    p_r = http_sess.post(
                        bmak_url,
                        json={"sensor_data": sensor_data},
                        timeout=cfg.get("timeout_s", 15),
                    )
                    post_status = p_r.status_code
                    post_ms = int((_t.time() - t_post) * 1000)
                    # Capturar _abck actualizado (rota cada POST)
                    new_abck = http_sess.cookies.get('_abck') or ''
                    if new_abck:
                        final_abck = new_abck
                except Exception as _pe:
                    _log({"op": f"hyper_post_{post_idx+1}_err",
                          "err": str(_pe)[:80]})

            posts_done += 1
            post_results.append({
                "idx": post_idx + 1,
                "gen_ms": gen_ms,
                "post_status": post_status,
                "post_ms": post_ms,
                "sensor_len": len(sensor_data or ''),
                "abck_marker": ("~0~" if "~0~" in final_abck
                                 else ("~-1~" if "~-1~" in final_abck else "?")),
            })

            # Stopping condition: si _abck contiene ~0~ → humano, salir early
            if "~0~" in final_abck:
                _log({"op": f"hyper_abck_validated_post_{post_idx+1}",
                      "marker": "~0~"})
                break

        try: sess.close()
        except Exception: pass
        try: http_sess.close()
        except Exception: pass

        dt_ms = int((_t.time() - t0) * 1000)
        is_human = "~0~" in final_abck
        _log({"op": "generate_akamai_sensor", "mode": "live",
              "elapsed_ms": dt_ms, "posts_done": posts_done,
              "abck_marker": "~0~" if is_human else "~-1~",
              "ok": is_human})
        return {
            "ok": True,   # operación completó (aunque marker no sea ~0~)
            "mode": "live",
            "abck_marker": "~0~" if is_human else "~-1~",
            "is_human": is_human,
            "posts_done": posts_done,
            "post_results": post_results,
            "cookies": {
                "_abck": final_abck,
                "bm_sz": bmsz_cur,
            },
            "elapsed_ms": dt_ms,
            "user_agent": UA,
            "bmak_url": bmak_url,
            "bmak_script_len": len(bmak_script or ""),
            "curl_cffi_used": _curl_cffi_available,
            "sensor_length": len(sensor_data or '') if 'sensor_data' in dir() else 0,
        }
    except Exception as e:
        dt_ms = int((_t.time() - t0) * 1000)
        err_str = f"{type(e).__name__}: {e}"
        _log({"op": "generate_akamai_sensor", "mode": "live",
              "error": err_str[:200], "elapsed_ms": dt_ms})
        return {"ok": False, "mode": "live",
                "error": err_str[:300], "elapsed_ms": dt_ms}


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
