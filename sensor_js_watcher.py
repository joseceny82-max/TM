"""sensor_js_watcher.py — Monitor cambios en bm.js de Akamai cada hora.

Hash sensor.js periódicamente. Si cambia:
- Alerta Discord/Telegram
- Invalida capturas en cache
- Opcional: trigger re-capture automático

API:
  save_config({check_interval_min, target_urls, alert_enabled})
  check_now(target_url) → dict con resultado
  start_watcher() / stop_watcher()
  get_history()
"""
import hashlib
import threading
import time
from datetime import datetime
from typing import Optional, List

import config
import json_atomic

_CONFIG_KEY = "sensor_js_watcher_config.json"
_HISTORY_KEY = "sensor_js_watcher_history.json"
_lock = threading.Lock()

_watcher_state = {"running": False, "thread": None, "stop_event": None,
                   "checks_made": 0, "changes_detected": 0,
                   "last_check_at": None}


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "enabled": False,
        "check_interval_min": 60,
        "target_urls": [
            "https://www.ticketmaster.com",
            "https://www.ticketmaster.es",
            "https://www.ticketmaster.co.uk",
        ],
        "alert_enabled": True,
        "auto_recapture_on_change": True,
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def _log_history(entry: dict):
    with _lock:
        d = json_atomic.load(config.ruta(_HISTORY_KEY), default={"history": []})
        d["history"].insert(0, entry)
        d["history"] = d["history"][:500]
        json_atomic.save(config.ruta(_HISTORY_KEY), d)


def check_now(target_url: str = "https://www.ticketmaster.com") -> dict:
    """Verifica hash actual de bm.js vs anterior.

    Returns:
      {ok, changed, old_hash, new_hash, size_delta, target_url}
    """
    try:
        import akamai_bmjs as bmjs
    except Exception as e:
        return {"ok": False, "error": f"akamai_bmjs no disponible: {e}"}

    started = time.time()
    result = {"ok": True, "target_url": target_url,
               "checked_at": datetime.now().isoformat()}

    try:
        fetch = bmjs.fetch_bm_js(target_url, timeout=10)
        _watcher_state["checks_made"] += 1
        _watcher_state["last_check_at"] = result["checked_at"]

        if not fetch.get("found"):
            result["ok"] = False
            result["error"] = "bm.js no encontrado"
            result["paths_tried"] = len(fetch.get("tried", []))
            _log_history({"type": "check_fail", **result,
                           "elapsed_ms": int((time.time() - started) * 1000)})
            return result

        new_hash = fetch["sha256"]
        new_size = fetch["size_bytes"]

        # Comparar con anterior
        cached = bmjs.list_cached_versions()
        previous = [v for v in cached if v.get("url", "").startswith(target_url[:30])]
        if previous:
            prev = previous[0] if previous[0].get("sha256") != new_hash else \
                   (previous[1] if len(previous) > 1 else None)
            if prev and prev.get("sha256") != new_hash:
                result["changed"] = True
                result["old_hash"] = prev.get("sha256", "")[:16]
                result["new_hash"] = new_hash[:16]
                result["size_delta"] = new_size - prev.get("size_bytes", 0)
                result["size_delta_pct"] = round(100 * result["size_delta"] /
                                                    max(1, prev.get("size_bytes", 1)), 1)
                _watcher_state["changes_detected"] += 1

                # Alert
                cfg = get_config()
                if cfg.get("alert_enabled"):
                    try:
                        import notifications
                        notifications.notify(
                            event_type="bmjs_changed",
                            title=f"⚠️ Akamai bm.js cambió — {target_url.split('/')[2]}",
                            message=(f"Akamai actualizó el bm.js.\n"
                                     f"Old hash: {result['old_hash']}\n"
                                     f"New hash: {result['new_hash']}\n"
                                     f"Size delta: {result['size_delta']:+d} bytes\n\n"
                                     f"Las capturas existentes pueden quedar obsoletas."),
                            level="warning",
                        )
                        result["alert_sent"] = True
                    except Exception as e:
                        result["alert_error"] = str(e)[:80]

                # Auto-recapture
                if cfg.get("auto_recapture_on_change"):
                    try:
                        rc = bmjs.capture_sensor_data(
                            target_url=target_url, duration_s=30,
                            engine="chromium", headless=True,
                        )
                        result["auto_recaptured"] = rc.get("captures_count", 0)
                    except Exception as e:
                        result["recapture_error"] = str(e)[:80]
            else:
                result["changed"] = False
                result["hash"] = new_hash[:16]
                result["size"] = new_size
        else:
            # Primera vez
            result["changed"] = False
            result["first_capture"] = True
            result["hash"] = new_hash[:16]
            result["size"] = new_size

        result["elapsed_ms"] = int((time.time() - started) * 1000)
        _log_history({"type": "check", **result})
        return result
    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        _log_history({"type": "check_error", **result})
        return result


def _watcher_loop(stop_event):
    while not stop_event.is_set():
        cfg = get_config()
        if not cfg.get("enabled"):
            stop_event.wait(60)
            continue
        try:
            for url in cfg.get("target_urls", []):
                check_now(url)
                if stop_event.is_set():
                    break
        except Exception as e:
            print(f"[sensor_js_watcher] loop err: {e}", flush=True)
        stop_event.wait(cfg.get("check_interval_min", 60) * 60)


def start_watcher() -> dict:
    if _watcher_state["running"]:
        return {"ok": True, "already_running": True}
    save_config({"enabled": True})
    ev = threading.Event()
    t = threading.Thread(target=_watcher_loop, args=(ev,),
                          daemon=True, name="sensor-js-watcher")
    _watcher_state.update({"running": True, "thread": t, "stop_event": ev})
    t.start()
    return {"ok": True, "started": True}


def stop_watcher() -> dict:
    if not _watcher_state["running"]:
        return {"ok": True, "not_running": True}
    ev = _watcher_state.get("stop_event")
    if ev:
        ev.set()
    _watcher_state.update({"running": False, "thread": None, "stop_event": None})
    save_config({"enabled": False})
    return {"ok": True, "stopped": True}


def get_history(limit: int = 50) -> List[dict]:
    d = json_atomic.load(config.ruta(_HISTORY_KEY), default={"history": []})
    return d.get("history", [])[:limit]


def get_status() -> dict:
    return {
        "running": _watcher_state["running"],
        "checks_made": _watcher_state.get("checks_made", 0),
        "changes_detected": _watcher_state.get("changes_detected", 0),
        "last_check_at": _watcher_state.get("last_check_at"),
        "config": get_config(),
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}
    cfg = get_config()
    results["checks"]["config_ok"] = "target_urls" in cfg
    results["checks"]["targets_count"] = len(cfg.get("target_urls", []))
    # No check_now real (network dependent)
    results["checks"]["check_now_callable"] = callable(check_now)
    results["checks"]["status_readable"] = "running" in get_status()
    return results


def get_info() -> dict:
    return {
        "module": "sensor_js_watcher",
        "status": get_status(),
    }
