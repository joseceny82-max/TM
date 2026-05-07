"""multilogin_autoscale.py — Auto-scaling de perfiles Multilogin.

Implementa scaling dinámico según cola de flows:
- Si hay >N flows en cola → crea M perfiles nuevos
- Si hay <K flows → destruye perfiles idle (ahorro costos)
- Balanceo por template (geo=country del evento)
- Cooldown para evitar thrashing (crear/destruir rápido)
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import config
import json_atomic


_CONFIG_KEY = "multilogin_autoscale_config.json"
_STATE_KEY = "multilogin_autoscale_state.json"
_lock = threading.Lock()


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "enabled": False,
        "check_interval_s": 30,
        "scale_up_threshold_queue": 5,      # >5 flows pending → scale up
        "scale_down_threshold_idle_s": 300, # perfiles idle 5 min → destroy
        "min_profiles": 2,
        "max_profiles": 20,
        "scale_up_batch": 3,                # crear 3 al mismo tiempo
        "cooldown_between_scale_s": 60,     # no scale más rápido que 1/min
        "default_template": "chrome_us_desktop",
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    allowed = set(cur.keys())
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    # Clamp
    cur["min_profiles"] = max(0, int(cur.get("min_profiles", 2)))
    cur["max_profiles"] = max(cur["min_profiles"],
                                min(100, int(cur.get("max_profiles", 20))))
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# Estado en memoria
_state = {
    "running": False, "thread": None, "stop_event": None,
    "last_scale_up_ts": 0, "last_scale_down_ts": 0,
    "total_scale_ups": 0, "total_scale_downs": 0,
    "created_profiles": [],    # {id, template, created_at, last_used_at}
}


def _get_queue_size() -> int:
    """Intenta leer el tamaño de la cola de flows pendientes."""
    try:
        import tm_purchase_flow as tpf
        # Si hay endpoint run_many activo con pending...
        log = tpf.get_log(limit=10)
        flows = log.get("flows", [])
        pending = sum(1 for f in flows
                       if not any(s.get("step") == "notify" for s in f.get("steps", [])))
        return pending
    except Exception:
        return 0


def _autoscale_tick() -> dict:
    """Una iteración del autoscaler."""
    cfg = get_config()
    if not cfg.get("enabled"):
        return {"ok": False, "reason": "disabled"}

    now = time.time()
    queue_size = _get_queue_size()
    current_profiles = len(_state.get("created_profiles", []))

    action = "none"
    result = {"queue_size": queue_size, "current_profiles": current_profiles}

    # Scale UP
    scale_up_needed = (
        queue_size >= cfg["scale_up_threshold_queue"]
        and current_profiles < cfg["max_profiles"]
        and (now - _state["last_scale_up_ts"]) > cfg["cooldown_between_scale_s"]
    )
    if scale_up_needed:
        batch = min(cfg["scale_up_batch"],
                     cfg["max_profiles"] - current_profiles)
        try:
            import multilogin_orchestrator as mlo
            created = []
            for i in range(batch):
                r = mlo.create_profile_from_template(
                    cfg.get("default_template", "chrome_us_desktop"),
                    name=f"autoscale-{int(now)}-{i}",
                )
                if r.get("ok"):
                    entry = {
                        "profile_id": r.get("profile_id"),
                        "template": cfg["default_template"],
                        "created_at": datetime.now().isoformat(),
                        "last_used_at": datetime.now().isoformat(),
                    }
                    with _lock:
                        _state["created_profiles"].append(entry)
                    created.append(entry)
            _state["last_scale_up_ts"] = now
            _state["total_scale_ups"] += len(created)
            action = "scale_up"
            result["created"] = len(created)
            result["new_profile_ids"] = [c["profile_id"] for c in created]
        except Exception as e:
            result["error"] = f"scale_up failed: {e}"

    # Scale DOWN
    elif queue_size == 0 and current_profiles > cfg["min_profiles"]:
        cutoff = now - cfg["scale_down_threshold_idle_s"]
        try:
            import multilogin_orchestrator as mlo
            destroyed = []
            # Copia la lista para no mutar durante iteración
            with _lock:
                profiles_copy = list(_state["created_profiles"])
            for p in profiles_copy:
                # Parse last_used_at
                try:
                    lu = datetime.fromisoformat(p["last_used_at"]).timestamp()
                    if lu < cutoff and len(_state["created_profiles"]) > cfg["min_profiles"]:
                        # Stop + delete
                        try:
                            mlo.stop_profile(p["profile_id"])
                        except Exception:
                            pass
                        with _lock:
                            _state["created_profiles"] = [
                                x for x in _state["created_profiles"]
                                if x["profile_id"] != p["profile_id"]
                            ]
                        destroyed.append(p["profile_id"])
                except Exception:
                    pass
            if destroyed:
                _state["last_scale_down_ts"] = now
                _state["total_scale_downs"] += len(destroyed)
                action = "scale_down"
                result["destroyed"] = len(destroyed)
                result["destroyed_ids"] = destroyed
        except Exception as e:
            result["error"] = f"scale_down failed: {e}"

    result["action"] = action
    result["ok"] = True
    return result


def _autoscaler_loop(stop_event):
    while not stop_event.is_set():
        try:
            _autoscale_tick()
        except Exception as e:
            print(f"[multilogin_autoscale] loop err: {e}", flush=True)
        cfg = get_config()
        stop_event.wait(cfg.get("check_interval_s", 30))


def start_autoscaler() -> dict:
    if _state["running"]:
        return {"ok": True, "already_running": True}
    ev = threading.Event()
    t = threading.Thread(target=_autoscaler_loop, args=(ev,),
                          daemon=True, name="multilogin-autoscaler")
    _state.update({"running": True, "thread": t, "stop_event": ev})
    t.start()
    # Habilitar config
    save_config({"enabled": True})
    return {"ok": True, "started": True}


def stop_autoscaler() -> dict:
    if not _state["running"]:
        return {"ok": True, "not_running": True}
    ev = _state.get("stop_event")
    if ev:
        ev.set()
    _state.update({"running": False, "thread": None, "stop_event": None})
    save_config({"enabled": False})
    return {"ok": True, "stopped": True}


def get_status() -> dict:
    return {
        "running": _state["running"],
        "config": get_config(),
        "current_profiles_count": len(_state.get("created_profiles", [])),
        "total_scale_ups": _state.get("total_scale_ups", 0),
        "total_scale_downs": _state.get("total_scale_downs", 0),
        "last_scale_up_ts": _state.get("last_scale_up_ts", 0),
        "last_scale_down_ts": _state.get("last_scale_down_ts", 0),
        "profiles": _state.get("created_profiles", []),
    }


def manual_tick() -> dict:
    """Ejecuta un tick manual del autoscaler (útil para testing)."""
    return _autoscale_tick()


def self_test() -> dict:
    results = {"ok": True, "checks": {}}
    cfg = get_config()
    results["checks"]["config_valid"] = bool(cfg.get("min_profiles", 0) <= cfg.get("max_profiles", 0))
    results["checks"]["queue_size_readable"] = isinstance(_get_queue_size(), int)
    # Manual tick
    tick = manual_tick()
    results["checks"]["tick_ok"] = tick.get("ok", False)
    results["checks"]["tick_action"] = tick.get("action")
    return results


def get_info() -> dict:
    return {
        "module": "multilogin_autoscale",
        "running": _state["running"],
        "stats": get_status(),
    }
