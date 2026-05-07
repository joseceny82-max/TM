"""health_monitor.py — Status page + métricas de salud del sistema.

Monitorea:
- Uptime del servidor
- Latencia API (rolling window últimos 60)
- Estado de cada módulo crítico
- Success rate de requests (últimos 500)
- Estado WebSocket / scheduler / workers
- Latencia real contra ticketmaster.com
"""
import time
import threading
from datetime import datetime, timedelta
from collections import deque
from typing import Dict, List

import config
import json_atomic


HEALTH_PATH_KEY = "health_monitor.json"
_lock = threading.Lock()

# Rolling windows en memoria
_api_latencies = deque(maxlen=100)   # últimas 100 requests (ms)
_api_results = deque(maxlen=500)     # últimas 500 ({ts, path, ok})
_startup_ts = time.time()


def record_request(path: str, elapsed_ms: float, status_code: int):
    """Middleware-style: registra cada request HTTP del dashboard.

    No contabiliza:
    - /static/* y /socket.io/  (ruido)
    - 401/403 (autorización fallida — el server respondió correctamente con
      'no autorizado'; ocurren legítimamente en pollings pre-login y NO
      son errores del servidor que deban bajar la tasa de éxito).
    """
    if path.startswith("/static/") or path == "/socket.io/":
        return
    if status_code in (401, 403):
        return
    with _lock:
        _api_latencies.append(float(elapsed_ms))
        _api_results.append({
            "ts": time.time(),
            "path": path[:100],
            "ok": 200 <= status_code < 400,
            "code": status_code,
        })


def reset_metrics():
    """Limpia contadores de éxito y latencia. Útil tras solucionar errores
    legacy para que la métrica refleje el estado actual."""
    with _lock:
        _api_latencies.clear()
        _api_results.clear()
    return {"ok": True, "cleared": True}


def _uptime_str(seconds: float) -> str:
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if d: return f"{d}d {h}h {m}m"
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"


def _module_status() -> Dict:
    """Comprueba cada módulo importable + estado interno."""
    modules = {}

    # problem_center
    try:
        import problem_center
        st = problem_center.get_stats()
        modules["problem_center"] = {"ok": True, "open": st.get("open", 0),
                                      "critical": st.get("critical", 0)}
    except Exception as e:
        modules["problem_center"] = {"ok": False, "error": str(e)[:80]}

    # event_manager
    try:
        import event_manager
        evs = event_manager.list_events()
        enabled = sum(1 for e in evs if e.get("enabled"))
        modules["event_manager"] = {"ok": True, "total": len(evs), "enabled": enabled}
    except Exception as e:
        modules["event_manager"] = {"ok": False, "error": str(e)[:80]}

    # notes_manager
    try:
        import notes_manager
        st = notes_manager.get_stats()
        modules["notes"] = {"ok": True, "total": st.get("total", 0)}
    except Exception as e:
        modules["notes"] = {"ok": False, "error": str(e)[:80]}

    # cookie_warmer / acpat
    try:
        import cookie_warmer
        st = cookie_warmer.pool_stats()
        modules["acpat_pool"] = {"ok": True, "tokens": st.get("size", 0)}
    except Exception as e:
        modules["acpat_pool"] = {"ok": False, "error": str(e)[:80]}

    # ja4
    try:
        import ja4_spoof
        modules["ja4_spoof"] = {"ok": True, "profiles": len(ja4_spoof.available_profiles())}
    except Exception as e:
        modules["ja4_spoof"] = {"ok": False, "error": str(e)[:80]}

    # event_discovery
    try:
        import event_discovery
        cfg = event_discovery.get_config()
        sch = event_discovery.scheduler_status()
        modules["discovery"] = {
            "ok": True,
            "configured": bool(cfg.get("api_key")),
            "scheduler_running": sch.get("running", False),
            "last_sync": cfg.get("last_sync"),
        }
    except Exception as e:
        modules["discovery"] = {"ok": False, "error": str(e)[:80]}

    # tm_registrations
    try:
        import tm_registrations
        st = tm_registrations.get_stats()
        modules["tm_registrations"] = {
            "ok": True,
            "total": st.get("total", 0),
            "pending": st.get("pending", 0),
            "worker_running": st.get("worker", {}).get("running", False),
        }
    except Exception as e:
        modules["tm_registrations"] = {"ok": False, "error": str(e)[:80]}

    # proxy_health
    try:
        import proxy_health
        st = proxy_health.get_stats()
        sch = proxy_health.scheduler_status()
        modules["proxy_health"] = {
            "ok": True,
            "total": st.get("total", 0),
            "fast": st.get("fast", 0),
            "dead": st.get("dead", 0),
            "scheduler_running": sch.get("running", False),
        }
    except Exception as e:
        modules["proxy_health"] = {"ok": False, "error": str(e)[:80]}

    # notifications
    try:
        import notifications
        cfg = notifications.get_config()
        modules["notifications"] = {
            "ok": True,
            "discord": bool(cfg.get("discord", {}).get("enabled")),
            "telegram": bool(cfg.get("telegram", {}).get("enabled")),
        }
    except Exception as e:
        modules["notifications"] = {"ok": False, "error": str(e)[:80]}

    # 3DS OTP Autofill
    try:
        import otp_3ds
        st = otp_3ds.get_stats()
        modules["otp_3ds"] = {"ok": True, "total": st.get("total_otps", 0),
                               "listener": st.get("listener_running", False),
                               "mode": st.get("mode")}
    except Exception as e:
        modules["otp_3ds"] = {"ok": False, "error": str(e)[:80]}

    # Proxy Session Pinning
    try:
        import proxy_session_pin
        st = proxy_session_pin.get_stats()
        modules["proxy_session_pin"] = {"ok": True, "active": st.get("active_pins", 0),
                                          "total_hits": st.get("total_hits", 0)}
    except Exception as e:
        modules["proxy_session_pin"] = {"ok": False, "error": str(e)[:80]}

    # Browser Pool
    try:
        import browser_pool
        st = browser_pool.get_stats()
        modules["browser_pool"] = {"ok": True, "ready": st.get("ready", 0),
                                     "in_use": st.get("in_use", 0),
                                     "manager_running": st.get("manager_running", False)}
    except Exception as e:
        modules["browser_pool"] = {"ok": False, "error": str(e)[:80]}

    # Discovery Adaptive
    try:
        import discovery_adaptive
        st = discovery_adaptive.get_stats()
        modules["discovery_adaptive"] = {"ok": True, "running": st.get("running", False),
                                           "fast": st.get("events_in_fast", 0),
                                           "medium": st.get("events_in_medium", 0)}
    except Exception as e:
        modules["discovery_adaptive"] = {"ok": False, "error": str(e)[:80]}

    # Card rotation
    try:
        import card_rotation
        st = card_rotation.get_stats()
        modules["card_rotation"] = {"ok": True,
                                     "total_cards": st.get("total_cards", 0),
                                     "active_bl": st.get("active_blacklist", 0),
                                     "success_pct": st.get("success_rate_pct", 0)}
    except Exception as e:
        modules["card_rotation"] = {"ok": False, "error": str(e)[:80]}

    # Cluster (multi-host)
    try:
        import cluster_manager, redis_backend
        stats = cluster_manager.get_cluster_stats()
        modules["cluster"] = {"ok": True,
                               "backend": type(redis_backend.backend).__name__,
                               "distributed": redis_backend.is_distributed(),
                               "hosts_alive": stats.get("hosts_alive", 0),
                               "hosts_total": stats.get("hosts_total", 0)}
    except Exception as e:
        modules["cluster"] = {"ok": False, "error": str(e)[:80]}

    # API tokens
    try:
        import api_tokens
        st = api_tokens.get_stats()
        modules["api_tokens"] = {"ok": True, "active": st.get("active", 0),
                                  "total_uses": st.get("total_uses", 0)}
    except Exception as e:
        modules["api_tokens"] = {"ok": False, "error": str(e)[:80]}

    # HTTP/3 QUIC
    try:
        import http3_quic
        info = http3_quic.get_info()
        modules["http3_quic"] = {"ok": True,
                                  "aioquic": info.get("aioquic_available", False),
                                  "version": info.get("aioquic_version")}
    except Exception as e:
        modules["http3_quic"] = {"ok": False, "error": str(e)[:80]}

    # Mobile proxies
    try:
        import mobile_proxies
        info = mobile_proxies.get_info()
        modules["mobile_proxies"] = {"ok": True, "mode": info.get("mode"),
                                       "configured": info.get("configured"),
                                       "providers": info.get("providers_count", 0)}
    except Exception as e:
        modules["mobile_proxies"] = {"ok": False, "error": str(e)[:80]}

    # Akamai bot manager
    try:
        import akamai_bmsc
        st = akamai_bmsc.get_stats()
        modules["akamai_bmsc"] = {"ok": True, "mode": st.get("mode"),
                                    "active_sessions": st.get("active_sessions", 0)}
    except Exception as e:
        modules["akamai_bmsc"] = {"ok": False, "error": str(e)[:80]}

    # Akamai bm.js analyzer
    try:
        import akamai_bmjs
        st = akamai_bmjs.get_stats()
        modules["akamai_bmjs"] = {"ok": True,
                                    "cached_versions": st.get("cached_bmjs_versions", 0),
                                    "captures_active": st.get("captures_active", 0)}
    except Exception as e:
        modules["akamai_bmjs"] = {"ok": False, "error": str(e)[:80]}

    # Multilogin
    try:
        import multilogin_orchestrator as mlo
        cfg = mlo.get_config()
        modules["multilogin"] = {"ok": True, "mode": cfg.get("mode"),
                                  "configured": mlo.is_configured()}
    except Exception as e:
        modules["multilogin"] = {"ok": False, "error": str(e)[:80]}

    # Human behavior
    try:
        import human_behavior
        info = human_behavior.get_info()
        modules["human_behavior"] = {"ok": True,
                                       "capabilities": len(info.get("capabilities", [])),
                                       "geo_countries": len(info.get("geo_countries", []))}
    except Exception as e:
        modules["human_behavior"] = {"ok": False, "error": str(e)[:80]}

    # Akamai validator
    try:
        import akamai_validator
        info = akamai_validator.get_info()
        modules["akamai_validator"] = {"ok": True,
                                        "baseline_fields": info.get("baseline", {}).get("fields_count", 0),
                                        "samples": info.get("baseline", {}).get("samples", 0)}
    except Exception as e:
        modules["akamai_validator"] = {"ok": False, "error": str(e)[:80]}

    # Multilogin autoscale
    try:
        import multilogin_autoscale
        info = multilogin_autoscale.get_info()
        modules["multilogin_autoscale"] = {"ok": True,
                                             "running": info.get("running", False),
                                             "profiles": info.get("stats", {}).get("current_profiles_count", 0)}
    except Exception as e:
        modules["multilogin_autoscale"] = {"ok": False, "error": str(e)[:80]}

    # Bot intelligence
    try:
        import bot_intelligence
        info = bot_intelligence.get_info()
        modules["bot_intelligence"] = {"ok": True,
                                         "runs_learned": info.get("total_runs_learned", 0),
                                         "best_domain": info.get("best_domain")}
    except Exception as e:
        modules["bot_intelligence"] = {"ok": False, "error": str(e)[:80]}

    # ML validator
    try:
        import ml_validator
        info = ml_validator.get_info()
        modules["ml_validator"] = {"ok": True,
                                     "dataset_size": info.get("dataset_stats", {}).get("total_samples", 0),
                                     "model_trained": info.get("model_trained", False),
                                     "sklearn": info.get("sklearn_available", False)}
    except Exception as e:
        modules["ml_validator"] = {"ok": False, "error": str(e)[:80]}

    # Cluster capture
    try:
        import cluster_capture
        info = cluster_capture.get_info()
        fs = info.get("fleet_status", {})
        modules["cluster_capture"] = {"ok": True,
                                        "running": fs.get("running", False),
                                        "hosts_alive": fs.get("hosts_alive", 0),
                                        "captures_contributed": fs.get("total_captures_by_fleet", 0)}
    except Exception as e:
        modules["cluster_capture"] = {"ok": False, "error": str(e)[:80]}

    # Captcha CV solver
    try:
        import captcha_cv_solver
        info = captcha_cv_solver.get_info()
        ds = info.get("dataset_stats", {})
        gpu = info.get("model_info", {}).get("gpu_info", {})
        modules["captcha_cv_solver"] = {"ok": True,
                                          "model_loaded": info.get("model_info", {}).get("loaded", False),
                                          "dataset_samples": ds.get("total_samples", 0),
                                          "labeled": ds.get("total_labeled", 0),
                                          "models_loaded": info.get("model_info", {}).get("loaded_slots", 0),
                                          "has_gpu": gpu.get("has_gpu", False),
                                          "supported_types": len(info.get("supported_types", []))}
    except Exception as e:
        modules["captcha_cv_solver"] = {"ok": False, "error": str(e)[:80]}

    # Apple/Google Pay
    try:
        import apple_google_pay
        info = apple_google_pay.get_info()
        stats = info.get("stats", {})
        modules["apple_google_pay"] = {"ok": True,
                                         "mode": info.get("mode"),
                                         "active_tokens": stats.get("active_tokens", 0),
                                         "apple_configured": stats.get("apple_configured", False),
                                         "google_configured": stats.get("google_configured", False)}
    except Exception as e:
        modules["apple_google_pay"] = {"ok": False, "error": str(e)[:80]}

    # ═══ MÓDULOS NUEVOS POST-RESEARCH ═══
    for mod_name in [
        "bots_compliance", "nodriver_engine", "rebrowser_stealth",
        "hyper_sdk_client", "llm_captcha", "proxy_ipqs",
        "proxy_per_account", "sensor_js_watcher",
        "advanced_fingerprint", "presale_farming", "tm_mobile_sdk",
    ]:
        try:
            mod = __import__(mod_name)
            info = mod.get_info() if hasattr(mod, "get_info") else {}
            modules[mod_name] = {
                "ok": True,
                **{k: v for k, v in info.items()
                    if k in ("stats", "configured", "running", "mode",
                              "active_sessions", "total_codes", "rebrowser_available",
                              "nodriver_available")}
            }
        except Exception as e:
            modules[mod_name] = {"ok": False, "error": str(e)[:80]}

    return modules


def _latency_stats() -> Dict:
    with _lock:
        lats = list(_api_latencies)
        results = list(_api_results)
    if not lats:
        return {"avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "samples": 0}
    s = sorted(lats)
    n = len(s)
    p50 = s[int(n * 0.5)] if n else 0
    p95 = s[int(n * 0.95)] if n > 0 else 0
    return {
        "avg_ms": int(sum(lats) / len(lats)),
        "p50_ms": int(p50),
        "p95_ms": int(p95),
        "samples": len(lats),
        "max_ms": int(max(lats)) if lats else 0,
        "min_ms": int(min(lats)) if lats else 0,
    }


def _success_rate() -> Dict:
    with _lock:
        results = list(_api_results)
    if not results:
        return {"rate": 100.0, "total": 0, "errors": 0, "last_5min": 0, "errors_5min": 0}
    now = time.time()
    recent = [r for r in results if now - r["ts"] < 300]
    errors_all = sum(1 for r in results if not r["ok"])
    errors_recent = sum(1 for r in recent if not r["ok"])
    return {
        "rate": round((len(results) - errors_all) / len(results) * 100, 2),
        "total": len(results),
        "errors": errors_all,
        "last_5min": len(recent),
        "errors_5min": errors_recent,
        "rate_5min": round((len(recent) - errors_recent) / max(1, len(recent)) * 100, 2),
    }


# Caché en memoria para TM probe (más rápido que leer JSON de disco en cada call)
_tm_probe_cache = {"data": None, "ts": 0}

def _tm_latency_probe(timeout=5) -> Dict:
    """Ping a ticketmaster.com para medir latencia real (cached 60s en memoria)."""
    # Memoria primero (microsegundos)
    if _tm_probe_cache["data"] and (time.time() - _tm_probe_cache["ts"]) < 60:
        return _tm_probe_cache["data"]
    # Disco como fallback (en caso de reinicio reciente)
    d = json_atomic.load(config.ruta(HEALTH_PATH_KEY), default={})
    probe = d.get("tm_probe", {})
    try:
        last = probe.get("ts", 0)
        if isinstance(last, (int, float)) and (time.time() - last) < 60:
            _tm_probe_cache["data"] = probe
            _tm_probe_cache["ts"] = time.time()
            return probe
    except Exception:
        pass
    # Live probe (cae aquí máximo una vez por minuto)
    import requests
    try:
        start = time.time()
        r = requests.head("https://www.ticketmaster.com/", timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0 TM-PERROS-HealthCheck"},
                          allow_redirects=True)
        elapsed = int((time.time() - start) * 1000)
        out = {"ok": r.status_code < 500, "status": r.status_code,
               "elapsed_ms": elapsed, "ts": time.time()}
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}",
               "elapsed_ms": 0, "ts": time.time()}
    d["tm_probe"] = out
    json_atomic.save(config.ruta(HEALTH_PATH_KEY), d)
    _tm_probe_cache["data"] = out
    _tm_probe_cache["ts"] = time.time()
    return out


# Caché en memoria para el snapshot completo (TTL 3s)
_health_cache = {"data": None, "ts": 0}
_health_cache_lock = None
def _get_health_lock():
    global _health_cache_lock
    if _health_cache_lock is None:
        import threading as _th
        _health_cache_lock = _th.Lock()
    return _health_cache_lock

HEALTH_CACHE_TTL = 3.0  # segundos


def get_health() -> Dict:
    """Snapshot completo de salud (con caché 3s para evitar cálculos repetidos).
    Operaciones costosas internamente: iterar 40 módulos con self_test + TM probe HTTP.
    """
    lock = _get_health_lock()
    with lock:
        if _health_cache["data"] and (time.time() - _health_cache["ts"]) < HEALTH_CACHE_TTL:
            return _health_cache["data"]

    # Fuera del lock: calcular (puede ser 200-400ms, no queremos bloquear)
    uptime = time.time() - _startup_ts
    modules = _module_status()
    ok_count = sum(1 for m in modules.values() if m.get("ok"))
    total_count = len(modules)
    overall = "healthy"
    if ok_count < total_count:
        overall = "degraded" if ok_count >= total_count * 0.7 else "down"
    problems_critical = modules.get("problem_center", {}).get("critical", 0)
    if problems_critical > 0 and overall == "healthy":
        overall = "degraded"

    data = {
        "ok": True,
        "overall": overall,
        "uptime_seconds": int(uptime),
        "uptime_human": _uptime_str(uptime),
        "started_at": datetime.fromtimestamp(_startup_ts).isoformat(),
        "now": datetime.now().isoformat(),
        "modules_ok": ok_count,
        "modules_total": total_count,
        "modules": modules,
        "api_latency": _latency_stats(),
        "api_success": _success_rate(),
        "tm_probe": _tm_latency_probe(),
        "app_version": config.APP_VERSION,
        "app_name": config.APP_NAME,
    }
    with lock:
        _health_cache["data"] = data
        _health_cache["ts"] = time.time()
    return data


def get_minimal() -> Dict:
    """Health mínimo (ideal para endpoint público /api/healthz)."""
    uptime = time.time() - _startup_ts
    return {
        "ok": True,
        "status": "up",
        "uptime_seconds": int(uptime),
        "version": config.APP_VERSION,
    }
