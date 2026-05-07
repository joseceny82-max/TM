"""queue_monitor.py — Monitor de posición en cola para múltiples workers.

Cada worker (perfil MoreLogin / browser) reporta su posición en la queue
virtual de Ticketmaster cada N segundos. El módulo guarda el estado y
permite:

- Identificar al líder por dominio (menor posición)
- Heatmap por dominio (cuántos workers en cada bucket)
- Auto-prune de entradas stale (TTL configurable)
- Push vía SocketIO si está disponible (eventos: `queue_monitor.update`)

API:
  report(worker_id, domain, position, queue_total=None, profile_id=None,
          event_id=None, status="queued") → entry
  get_all(domain=None) → list[entry]
  get_leader(domain=None) → entry|None
  heatmap_by_domain() → {domain: {0_100:N, 100_500:N, ...}}
  clear(worker_id=None|domain=None|all=True)
  cleanup_stale(ttl_s=60)
  start_cleaner(interval_s=30) / stop_cleaner()
  self_test()
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

# ── SocketIO opcional ─────────────────────────────────────────
try:
    from flask import current_app  # noqa
    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

# ── Persistencia opcional (json_atomic) ───────────────────────
try:
    import config
    import json_atomic
    _HAS_PERSIST = True
    _STATE_KEY = "queue_monitor_state.json"
except ImportError:
    _HAS_PERSIST = False

_state: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()
DEFAULT_TTL_S = 60
_cleaner_state = {"running": False, "thread": None, "stop_event": None}
_persist_dirty = False
_persist_last = 0.0
_PERSIST_DEBOUNCE_S = 2.0  # batched writes


def _persist_save():
    """Persiste state a disco (debounced)."""
    if not _HAS_PERSIST:
        return
    global _persist_dirty, _persist_last
    try:
        with _lock:
            json_atomic.save(config.ruta(_STATE_KEY),
                              {"workers": _state, "saved_at": _now_iso()})
            _persist_dirty = False
            _persist_last = time.time()
    except Exception:
        pass


def _persist_load():
    """Carga state desde disco al arrancar."""
    if not _HAS_PERSIST:
        return
    try:
        d = json_atomic.load(config.ruta(_STATE_KEY), default={"workers": {}})
        with _lock:
            for k, v in (d.get("workers") or {}).items():
                # Solo restaura entradas no demasiado viejas (< 1h)
                last_ts = v.get("last_update_ts", 0)
                if time.time() - last_ts < 3600:
                    _state[k] = v
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now().isoformat()


def _emit(event: str, payload: Dict[str, Any]):
    """Push a Socket.IO si está disponible."""
    try:
        from dashboard import socketio  # type: ignore
        socketio.emit(event, payload)
    except Exception:
        pass


def report(worker_id: str,
           domain: str,
           position: int,
           queue_total: Optional[int] = None,
           profile_id: Optional[str] = None,
           event_id: Optional[str] = None,
           status: str = "queued") -> Dict[str, Any]:
    """Reporta posición actual del worker. Llamar cada 10s desde el bot."""
    if not worker_id or not domain:
        return {"ok": False, "error": "worker_id_and_domain_required"}
    try:
        position = max(0, int(position))
    except (TypeError, ValueError):
        return {"ok": False, "error": "position_invalid"}
    with _lock:
        prev = _state.get(worker_id, {})
        prev_position = prev.get("position")
        entry = {
            "worker_id": worker_id,
            "domain": domain,
            "position": position,
            "queue_total": queue_total,
            "profile_id": profile_id,
            "event_id": event_id,
            "status": status,
            "last_update": _now_iso(),
            "last_update_ts": time.time(),
            "started_at": prev.get("started_at") or _now_iso(),
            "first_position": prev.get("first_position", position),
            "history": (prev.get("history") or [])[-49:] + [
                {"ts": _now_iso(), "position": position}],
            "delta_per_s": None,
        }
        # Velocidad de avance (positions/sec)
        if prev_position is not None and prev.get("last_update_ts"):
            dt = max(1.0, time.time() - prev["last_update_ts"])
            entry["delta_per_s"] = round((prev_position - position) / dt, 3)
        _state[worker_id] = entry
    _persist_save()
    _emit("queue_monitor.update", entry)
    return {"ok": True, "entry": entry}


def get_all(domain: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        items = list(_state.values())
    if domain:
        items = [e for e in items if e.get("domain") == domain]
    items.sort(key=lambda e: (e.get("position") if e.get("position") is not None else 1e9))
    return items


def get_leader(domain: Optional[str] = None) -> Optional[Dict[str, Any]]:
    items = get_all(domain)
    return items[0] if items else None


def heatmap_by_domain() -> Dict[str, Dict[str, int]]:
    """Distribución por buckets: 0-100, 100-500, 500-2k, 2k-10k, 10k+."""
    buckets = ("0-100", "100-500", "500-2k", "2k-10k", "10k+")
    out: Dict[str, Dict[str, int]] = {}
    with _lock:
        for e in _state.values():
            dom = e.get("domain") or "?"
            pos = e.get("position") or 0
            if pos < 100: b = "0-100"
            elif pos < 500: b = "100-500"
            elif pos < 2000: b = "500-2k"
            elif pos < 10000: b = "2k-10k"
            else: b = "10k+"
            d = out.setdefault(dom, {k: 0 for k in buckets})
            d[b] += 1
    return out


def stats() -> Dict[str, Any]:
    with _lock:
        items = list(_state.values())
    domains = set(e.get("domain") for e in items if e.get("domain"))
    leaders_by_domain = {d: get_leader(d) for d in domains}
    return {
        "total_workers": len(items),
        "domains_count": len(domains),
        "domains": list(domains),
        "leaders_by_domain": {d: (l or {}) for d, l in leaders_by_domain.items()},
        "global_leader": get_leader(),
        "heatmap": heatmap_by_domain(),
        "stale_cleaner_running": _cleaner_state.get("running", False),
    }


def clear(worker_id: Optional[str] = None,
          domain: Optional[str] = None,
          all_workers: bool = False) -> Dict[str, Any]:
    with _lock:
        if all_workers:
            n = len(_state)
            _state.clear()
            _persist_save()
            return {"ok": True, "removed": n}
        if worker_id and worker_id in _state:
            _state.pop(worker_id)
            _persist_save()
            return {"ok": True, "removed": 1}
        if domain:
            removed = [k for k, e in _state.items() if e.get("domain") == domain]
            for k in removed:
                _state.pop(k, None)
            if removed:
                _persist_save()
            return {"ok": True, "removed": len(removed)}
    return {"ok": False, "error": "nothing_to_clear"}


def cleanup_stale(ttl_s: int = DEFAULT_TTL_S) -> Dict[str, Any]:
    deadline = time.time() - ttl_s
    removed: List[str] = []
    with _lock:
        for k, e in list(_state.items()):
            if e.get("last_update_ts", 0) <= deadline:
                removed.append(k)
                _state.pop(k, None)
    if removed:
        _persist_save()
    return {"ok": True, "removed": removed, "count": len(removed),
             "ttl_s": ttl_s}


# Auto-load al import
_persist_load()


# ─── Cleaner thread ─────────────────────────────────────────────
def _cleaner_loop(stop_evt: threading.Event, interval_s: int, ttl_s: int):
    while not stop_evt.is_set():
        try:
            cleanup_stale(ttl_s=ttl_s)
        except Exception:
            pass
        for _ in range(max(1, int(interval_s))):
            if stop_evt.is_set():
                break
            time.sleep(1)


def start_cleaner(interval_s: int = 30, ttl_s: int = DEFAULT_TTL_S) -> Dict[str, Any]:
    if _cleaner_state.get("running"):
        return {"ok": True, "status": "already_running"}
    evt = threading.Event()
    th = threading.Thread(target=_cleaner_loop, args=(evt, interval_s, ttl_s),
                           daemon=True, name="queue_monitor_cleaner")
    _cleaner_state.update({"running": True, "stop_event": evt, "thread": th,
                             "interval_s": interval_s, "ttl_s": ttl_s,
                             "started_at": _now_iso()})
    th.start()
    return {"ok": True, "status": "started", "interval_s": interval_s,
             "ttl_s": ttl_s}


def stop_cleaner() -> Dict[str, Any]:
    if not _cleaner_state.get("running"):
        return {"ok": True, "status": "not_running"}
    evt = _cleaner_state.get("stop_event")
    if evt:
        evt.set()
    _cleaner_state["running"] = False
    return {"ok": True, "status": "stopped"}


# ─── Self-test ──────────────────────────────────────────────────
def self_test() -> Dict[str, Any]:
    out = {"ok": True, "checks": {}}
    clear(all_workers=True)
    r1 = report("w1", "ticketmaster.es", 1500, queue_total=20000,
                  profile_id="p1", event_id="e1")
    out["checks"]["report_ok"] = r1.get("ok", False)
    r2 = report("w2", "ticketmaster.es", 800, profile_id="p2", event_id="e1")
    r3 = report("w3", "ticketmaster.com", 50, profile_id="p3", event_id="e2")
    leader = get_leader("ticketmaster.es")
    out["checks"]["leader_correct"] = (leader and leader.get("worker_id") == "w2")
    global_leader = get_leader()
    out["checks"]["global_leader_correct"] = (global_leader and
                                               global_leader.get("worker_id") == "w3")
    h = heatmap_by_domain()
    out["checks"]["heatmap_es_buckets"] = ("ticketmaster.es" in h)
    # avance (delta_per_s)
    time.sleep(0.5)
    r2b = report("w2", "ticketmaster.es", 750, profile_id="p2", event_id="e1")
    out["checks"]["delta_calculated"] = (r2b["entry"].get("delta_per_s") is not None)
    s = stats()
    out["checks"]["stats_total_workers"] = (s.get("total_workers", 0) >= 3)
    out["checks"]["stats_domains_count"] = (s.get("domains_count", 0) >= 2)
    # Stale cleanup
    cleanup_stale(ttl_s=0)
    out["checks"]["cleanup_clears_all"] = (len(_state) == 0)
    out["passed"] = sum(1 for v in out["checks"].values() if v)
    out["total"] = len(out["checks"])
    out["ok"] = out["passed"] == out["total"]
    return out


def get_info() -> Dict[str, Any]:
    return {
        "module": "queue_monitor",
        "active_workers": len(_state),
        "stats": stats(),
        "endpoints": [
            "POST /api/queue-monitor/report",
            "GET /api/queue-monitor/state",
            "GET /api/queue-monitor/leader",
            "POST /api/queue-monitor/clear",
            "POST /api/queue-monitor/cleaner/<action>",
            "POST /api/queue-monitor/self-test",
        ],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=2))
