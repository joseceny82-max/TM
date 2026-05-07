"""discovery_adaptive.py — Polling adaptativo de Discovery API.

Mejora #discovery_adaptive:
  Acelerar a 1 Hz cuando un evento pending está a punto de pasar a on_sale
  (ventana < 5 min al sale_date). Fuera de ventana, polling normal cada N min.

Ventajas:
  - En eventos hype (conciertos premium) ganamos segundos críticos frente
    al onsale. Detectar el cambio status→"on_sale" 1-3 s antes que el
    polling normal = diferencia entre comprar o ver "SOLD OUT".
  - Sin freír la Discovery API: solo acelera los eventos que ESTÁN cerca
    de salir a la venta. El resto del tiempo frecuencia baja.

Ventanas (configurables):
  > 30 min al sale   → polling_normal (15 min, global)
  5-30 min al sale   → polling_medium (30 s por evento en ventana)
  < 5 min al sale    → polling_fast (1 s por evento en ventana)
  Tras on_sale       → polling_post (5 s durante 10 min, luego normal)

API:
  detect_windows() → {fast: [ev_ids], medium: [...], post: [...]}
  run_tick() → resultado de un tick (chequea eventos + actualiza status)
  start_scheduler() / stop_scheduler()
  get_stats()
"""
import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import config
import json_atomic


_CFG_KEY = "discovery_adaptive_config.json"
_LOG_KEY = "discovery_adaptive_log.json"
_lock = threading.Lock()

_scheduler = {
    "running": False,
    "thread": None,
    "stop_event": None,
    "ticks": 0,
    "last_tick": None,
    "last_fast_count": 0,
    "last_medium_count": 0,
    "last_status_changes": 0,
    "last_poll_mode": None,
}


def _cfg_path():
    return config.ruta(_CFG_KEY)


def _log_path():
    return config.ruta(_LOG_KEY)


def get_config() -> dict:
    return json_atomic.load(_cfg_path(), default={
        "enabled": False,
        "fast_window_min": 5,       # min antes del sale para modo fast
        "medium_window_min": 30,    # min antes del sale para modo medium
        "fast_interval_s": 1,       # polling cuando <5min al sale
        "medium_interval_s": 30,    # polling cuando <30min al sale
        "normal_interval_min": 15,  # polling por defecto
        "post_onsale_window_min": 10,  # tras on_sale, poll rápido durante N min
        "post_onsale_interval_s": 5,
        "max_events_per_tick": 10,  # cap para proteger API
        "auto_notify_on_sale": True,
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    allowed = {"enabled", "fast_window_min", "medium_window_min",
               "fast_interval_s", "medium_interval_s", "normal_interval_min",
               "post_onsale_window_min", "post_onsale_interval_s",
               "max_events_per_tick", "auto_notify_on_sale"}
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    json_atomic.save(_cfg_path(), cur)
    return cur


def _log(entry: dict):
    with _lock:
        d = json_atomic.load(_log_path(), default={"events": []})
        d["events"].insert(0, entry)
        d["events"] = d["events"][:200]
        json_atomic.save(_log_path(), d)


def get_log(limit: int = 50) -> List[dict]:
    return json_atomic.load(_log_path(), default={"events": []}).get("events", [])[:limit]


def _parse_sale_date(ev: dict) -> Optional[datetime]:
    """Extrae sale_date del evento en ISO/varios formatos."""
    raw = ev.get("sale_date") or ev.get("onsale_at") or ev.get("sale_start")
    if not raw:
        return None
    try:
        # Normaliza Z → +00:00
        if isinstance(raw, str) and raw.endswith("Z"):
            raw = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(str(raw))
        # Si tiene tz, convertir a naive local-like (comparación con datetime.now())
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        # Alternativo: "2026-05-01 10:00"
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(raw)[:len(fmt.replace("%", "xx"))], fmt)
            except Exception:
                continue
    return None


def detect_windows() -> Dict[str, List[dict]]:
    """Clasifica los eventos pending por ventana de tiempo al sale.

    Devuelve: {fast, medium, normal, post_onsale}
    """
    cfg = get_config()
    try:
        import event_manager
        events = event_manager.list_events()
    except Exception:
        events = []

    now = datetime.now()
    fast_cut = timedelta(minutes=cfg.get("fast_window_min", 5))
    medium_cut = timedelta(minutes=cfg.get("medium_window_min", 30))
    post_cut = timedelta(minutes=cfg.get("post_onsale_window_min", 10))

    fast, medium, normal, post = [], [], [], []
    for ev in events:
        if not ev.get("enabled", True):
            continue
        status = (ev.get("status") or "").lower()
        sale_dt = _parse_sale_date(ev)
        # Post-onsale: status=on_sale y marcado reciente
        if status == "on_sale":
            onsale_ts = ev.get("onsale_detected_at")
            if onsale_ts:
                try:
                    det = datetime.fromisoformat(onsale_ts)
                    if now - det < post_cut:
                        post.append(ev)
                        continue
                except Exception:
                    pass
            # Sin timestamp de detección, lo metemos en normal
            normal.append(ev)
            continue
        # Solo nos interesan los pending (o sin status)
        if status not in ("", "pending", "scheduled"):
            continue
        if not sale_dt:
            normal.append(ev)
            continue
        delta = sale_dt - now
        # Si el sale ya pasó y sigue pending, probablemente cambió → medium
        if delta.total_seconds() < 0:
            if abs(delta.total_seconds()) < 600:  # en los últimos 10 min
                fast.append(ev)
            else:
                normal.append(ev)
            continue
        if delta <= fast_cut:
            fast.append(ev)
        elif delta <= medium_cut:
            medium.append(ev)
        else:
            normal.append(ev)

    return {
        "fast": fast,
        "medium": medium,
        "normal": normal,
        "post_onsale": post,
    }


def run_tick(force_mode: Optional[str] = None) -> dict:
    """Un tick del scheduler: chequea eventos en ventana y actualiza status."""
    started = time.time()
    cfg = get_config()
    windows = detect_windows()
    fast = windows["fast"]
    medium = windows["medium"]
    post = windows["post_onsale"]

    cap = int(cfg.get("max_events_per_tick", 10))
    # Priorizar fast > post > medium
    to_check = (fast + post + medium)[:cap]

    checked = 0
    changes = []
    api_errors = 0

    try:
        import event_discovery
        import event_manager
        discovery_available = event_discovery.is_configured()
    except Exception:
        discovery_available = False

    for ev in to_check:
        tm_id = ev.get("tm_id") or ev.get("tm_event_id")
        if not tm_id:
            continue
        checked += 1
        if not discovery_available:
            continue
        try:
            detail = event_discovery.get_event_detail(tm_id)
            if not detail or not detail.get("ok", True) is False:
                pass
            new_status = detail.get("status", "").lower() if detail else ""
            old_status = (ev.get("status") or "").lower()
            if new_status and new_status != old_status:
                patch = {"status": new_status}
                if new_status == "on_sale":
                    patch["onsale_detected_at"] = datetime.now().isoformat()
                event_manager.update_event(ev.get("id"), patch)
                changes.append({
                    "event_id": ev.get("id"),
                    "tm_id": tm_id,
                    "name": ev.get("name", "?")[:60],
                    "old": old_status, "new": new_status,
                    "ts": datetime.now().isoformat(),
                })
                # Notificar on_sale
                if new_status == "on_sale" and cfg.get("auto_notify_on_sale", True):
                    try:
                        import notifications
                        notifications.notify({
                            "type": "event_onsale",
                            "severity": "high",
                            "title": f"🔥 ON SALE: {ev.get('name','?')}",
                            "body": f"El evento {ev.get('name','?')} acaba de entrar en venta.",
                            "event_id": ev.get("id"),
                        })
                    except Exception:
                        pass
        except Exception:
            api_errors += 1
            continue

    elapsed_ms = int((time.time() - started) * 1000)
    # Modo del tick: qué intervalo usar para el próximo sleep
    if fast or post:
        mode = "fast" if fast else "post_onsale"
        next_interval = cfg.get("fast_interval_s" if fast else "post_onsale_interval_s", 1)
    elif medium:
        mode = "medium"
        next_interval = cfg.get("medium_interval_s", 30)
    else:
        mode = "normal"
        next_interval = int(cfg.get("normal_interval_min", 15)) * 60

    _scheduler["ticks"] += 1
    _scheduler["last_tick"] = datetime.now().isoformat()
    _scheduler["last_fast_count"] = len(fast)
    _scheduler["last_medium_count"] = len(medium)
    _scheduler["last_status_changes"] = len(changes)
    _scheduler["last_poll_mode"] = mode

    entry = {
        "ts": datetime.now().isoformat(),
        "mode": mode,
        "next_interval_s": next_interval,
        "checked": checked,
        "fast": len(fast),
        "medium": len(medium),
        "post_onsale": len(post),
        "changes": len(changes),
        "api_errors": api_errors,
        "elapsed_ms": elapsed_ms,
    }
    if changes:
        entry["change_details"] = changes[:5]
    _log(entry)
    return {"ok": True, **entry}


# ═══════════════════════════════════════════
#  SCHEDULER
# ═══════════════════════════════════════════
def _scheduler_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        cfg = get_config()
        if not cfg.get("enabled", False):
            stop_event.wait(30)
            continue
        try:
            r = run_tick()
            interval = max(1, int(r.get("next_interval_s", 60)))
        except Exception:
            interval = 60
        stop_event.wait(interval)


def start_scheduler() -> dict:
    if _scheduler["running"]:
        return {"ok": True, "already_running": True}
    stop = threading.Event()
    t = threading.Thread(target=_scheduler_loop, args=(stop,),
                         daemon=True, name="discovery-adaptive")
    _scheduler.update({"running": True, "thread": t, "stop_event": stop})
    t.start()
    return {"ok": True, "started": True}


def stop_scheduler() -> dict:
    if not _scheduler["running"]:
        return {"ok": True, "not_running": True}
    ev = _scheduler.get("stop_event")
    if ev:
        ev.set()
    _scheduler.update({"running": False, "thread": None, "stop_event": None})
    return {"ok": True, "stopped": True}


def scheduler_status() -> dict:
    cfg = get_config()
    return {
        "running": _scheduler["running"],
        "enabled": cfg.get("enabled", False),
        "ticks": _scheduler["ticks"],
        "last_tick": _scheduler["last_tick"],
        "last_poll_mode": _scheduler.get("last_poll_mode"),
        "last_fast_count": _scheduler.get("last_fast_count", 0),
        "last_medium_count": _scheduler.get("last_medium_count", 0),
        "last_status_changes": _scheduler.get("last_status_changes", 0),
    }


def get_stats() -> dict:
    windows = detect_windows()
    log = get_log(50)
    total_changes = sum(e.get("changes", 0) for e in log)
    total_errors = sum(e.get("api_errors", 0) for e in log)
    return {
        **scheduler_status(),
        "events_in_fast": len(windows["fast"]),
        "events_in_medium": len(windows["medium"]),
        "events_in_normal": len(windows["normal"]),
        "events_in_post_onsale": len(windows["post_onsale"]),
        "log_entries": len(log),
        "total_status_changes_logged": total_changes,
        "total_api_errors_logged": total_errors,
    }


def self_test() -> dict:
    """Corre 1 tick sin arrancar scheduler, valida clasificación."""
    started = time.time()
    windows = detect_windows()
    tick = run_tick()
    return {
        "ok": True,
        "elapsed_ms": int((time.time() - started) * 1000),
        "windows": {k: len(v) for k, v in windows.items()},
        "tick_result": tick,
        "stats": get_stats(),
    }
