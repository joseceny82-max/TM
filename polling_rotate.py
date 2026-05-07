"""polling_rotate.py — Polling rotatorio multi-evento con cola prioritaria.

Worker itera N eventos buscando stock disponible. Maximiza CPU util durante
drops simultáneos: en lugar de tener un worker dedicado por evento (la mayoría
idle), tenemos N workers compartidos rotando una cola prioritaria de eventos.

Lógica:
  1. Cola prioritaria con eventos (priority 1-10, lower = más urgente)
  2. Worker saca el evento con mayor prioridad (least priority value)
  3. Hace check_stock() vía cookie_warmer / browser_engine
  4. Si stock detected → dispara purchase_flow + reduce prioridad en cola
  5. Si no → reencola con priority++ tras N segundos
  6. Loop continúa hasta stop o cola vacía

API:
  add_event(event_id, url, domain, priority=5, sale_date?, max_attempts=100)
  remove_event(event_id)
  list_events() / get_event(event_id)
  set_priority(event_id, priority)
  start_workers(n=2) / stop_workers()
  worker_status()
  set_check_callback(fn)  → fn(event)→{stock_found, hint}
  set_fire_callback(fn)   → fn(event)→{flow_id, status}
  get_stats()
  self_test()
"""
from __future__ import annotations

import heapq
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable

import config
import json_atomic

_STATE_KEY = "polling_rotate_state.json"
_LOG_KEY = "polling_rotate_log.json"
_lock = threading.RLock()

# Cola prioritaria — heapq de tuplas (priority, seq, event_id) para evitar
# comparación de dicts cuando priority empata.
_queue: List[tuple] = []
_events: Dict[str, Dict[str, Any]] = {}    # {event_id: event}
_seq = 0
_inflight: set = set()  # event_ids actualmente siendo procesados (anti-dupe)
_inflight_lock = threading.Lock()
_workers_state = {
    "running": False,
    "workers": [],          # [{id, thread, stop_event, ticks, fired}]
    "stop_event": None,
    "n_workers": 0,
    "started_at": "",
}

# Callbacks inyectables (test stock / fire purchase)
_check_callback: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
_fire_callback: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None


def _now_iso() -> str:
    return datetime.now().isoformat()


def _emit_ws(event: str, payload: Dict[str, Any]):
    """Push a Socket.IO si dashboard está corriendo."""
    try:
        from dashboard import socketio  # type: ignore
        socketio.emit(event, payload)
    except Exception:
        pass


def _promote_if_onsale(ev: Dict[str, Any]) -> None:
    """Si sale_date llegó y priority>1, promueve a 1 automáticamente."""
    sale_date = ev.get("sale_date") or ""
    if not sale_date:
        return
    try:
        sd = datetime.fromisoformat(sale_date.replace("Z", "+00:00"))
        sd_naive = sd.replace(tzinfo=None) if sd.tzinfo else sd
        now = datetime.now()
        if now >= sd_naive and ev.get("priority", 5) > 1:
            old_p = ev["priority"]
            ev["priority"] = 1
            ev["onsale_promoted_at"] = _now_iso()
            _append_log({"ts": _now_iso(), "event": "onsale_promote",
                          "event_id": ev["event_id"],
                          "from_priority": old_p, "to_priority": 1})
            _emit_ws("polling_rotate.onsale_promoted",
                      {"event_id": ev["event_id"],
                       "from_priority": old_p, "to_priority": 1})
    except Exception:
        pass


def _persist():
    try:
        json_atomic.save(config.ruta(_STATE_KEY), {
            "events": _events,
            "saved_at": _now_iso(),
        })
    except Exception:
        pass


def _persist_load():
    try:
        d = json_atomic.load(config.ruta(_STATE_KEY), default={"events": {}})
        with _lock:
            for eid, ev in (d.get("events") or {}).items():
                _events[eid] = ev
                # Re-encolar
                global _seq
                _seq += 1
                heapq.heappush(_queue, (ev.get("priority", 5), _seq, eid))
    except Exception:
        pass


def _append_log(entry: Dict[str, Any]):
    try:
        cur = json_atomic.load(config.ruta(_LOG_KEY), default={"entries": []})
        cur.setdefault("entries", []).insert(0, entry)
        cur["entries"] = cur["entries"][:500]
        json_atomic.save(config.ruta(_LOG_KEY), cur)
    except Exception:
        pass


def add_event(event_id: str,
              url: str = "",
              domain: str = "ticketmaster.es",
              priority: int = 5,
              sale_date: str = "",
              max_attempts: int = 100,
              metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Añade evento a la cola. priority: 1=urgente, 10=baja."""
    if not event_id:
        return {"ok": False, "error": "event_id_required"}
    priority = max(1, min(10, int(priority)))
    global _seq
    with _lock:
        ev = _events.get(event_id, {})
        ev.update({
            "event_id": event_id,
            "url": url,
            "domain": domain,
            "priority": priority,
            "sale_date": sale_date,
            "max_attempts": int(max_attempts),
            "attempts": ev.get("attempts", 0),
            "fires": ev.get("fires", 0),
            "added_at": ev.get("added_at") or _now_iso(),
            "last_check_at": ev.get("last_check_at"),
            "last_result": ev.get("last_result"),
            "status": ev.get("status", "queued"),
            "metadata": metadata or ev.get("metadata") or {},
        })
        _events[event_id] = ev
        _seq += 1
        heapq.heappush(_queue, (priority, _seq, event_id))
    _persist()
    _append_log({"ts": _now_iso(), "event": "added", "event_id": event_id,
                  "priority": priority})
    return {"ok": True, "event": ev}


def remove_event(event_id: str) -> Dict[str, Any]:
    with _lock:
        if event_id not in _events:
            return {"ok": False, "error": "not_found"}
        _events.pop(event_id, None)
        # Filtra cola (heapq no soporta delete random — reconstruimos)
        global _queue
        _queue = [(p, s, e) for (p, s, e) in _queue if e != event_id]
        heapq.heapify(_queue)
    _persist()
    _append_log({"ts": _now_iso(), "event": "removed", "event_id": event_id})
    return {"ok": True, "removed": event_id}


def list_events() -> List[Dict[str, Any]]:
    with _lock:
        items = list(_events.values())
    items.sort(key=lambda e: (e.get("priority", 5), -(e.get("fires", 0))))
    return items


def get_event(event_id: str) -> Optional[Dict[str, Any]]:
    return _events.get(event_id)


def set_priority(event_id: str, priority: int) -> Dict[str, Any]:
    priority = max(1, min(10, int(priority)))
    global _seq
    with _lock:
        ev = _events.get(event_id)
        if not ev:
            return {"ok": False, "error": "not_found"}
        ev["priority"] = priority
        _seq += 1
        heapq.heappush(_queue, (priority, _seq, event_id))
    _persist()
    return {"ok": True, "event_id": event_id, "priority": priority}


def set_check_callback(fn: Callable[[Dict[str, Any]], Dict[str, Any]]):
    """Registra fn(event)→{stock_found:bool, hint:str?}."""
    global _check_callback
    _check_callback = fn


def set_fire_callback(fn: Callable[[Dict[str, Any]], Dict[str, Any]]):
    """Registra fn(event)→{flow_id?, status?} cuando stock detectado."""
    global _fire_callback
    _fire_callback = fn


def _default_check(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Fallback callback: usa cookie_warmer / requests para test simple."""
    try:
        import cookie_warmer
        if hasattr(cookie_warmer, "check_stock"):
            return cookie_warmer.check_stock(ev.get("url", ""))
    except Exception:
        pass
    # Sin callback definido — assume no stock
    return {"stock_found": False, "hint": "no_check_callback"}


def _default_fire(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Fallback fire: envía a tm_purchase_flow.run_flow.

    Llama con la signature correcta (event_id, quantity, mode, ...).
    Default mode='simulate' por seguridad — el caller debe pasar
    `metadata={'mode': 'live'}` si quiere compra real.
    """
    try:
        import tm_purchase_flow
        meta = ev.get("metadata") or {}
        mode = meta.get("mode", "simulate")
        quantity = int(meta.get("quantity") or 2)
        r = tm_purchase_flow.run_flow(
            event_id=str(ev.get("event_id") or ""),
            quantity=quantity,
            mode=mode,
        )
        # run_flow puede devolver dict o ID; normalizamos
        if isinstance(r, dict):
            return {
                "ok": r.get("ok", bool(r.get("flow_id"))),
                "flow_id": r.get("flow_id") or r.get("run_id"),
                "status": r.get("status") or r.get("error"),
                "from_polling_rotate": True,
            }
        return {"ok": True, "flow_id": str(r), "from_polling_rotate": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _worker_loop(worker_id: str, stop_evt: threading.Event):
    """Loop principal del worker: pop event → check → fire si stock → reencola."""
    state = next((w for w in _workers_state["workers"] if w["id"] == worker_id), None)
    if not state:
        return
    while not stop_evt.is_set():
        ev_obj = None
        # Pop con lock + inflight check (anti-dupe entre N workers)
        with _lock:
            while _queue:
                priority, seq, eid = heapq.heappop(_queue)
                ev = _events.get(eid)
                if not ev or ev.get("status") == "abandoned":
                    continue
                # Inflight: si otro worker ya tomó este event_id, lo reencolamos
                with _inflight_lock:
                    if eid in _inflight:
                        # Re-push al final de la cola (priority + tiny delta)
                        global _seq
                        _seq += 1
                        heapq.heappush(_queue, (priority, _seq, eid))
                        continue
                    _inflight.add(eid)
                ev_obj = ev
                break
        if not ev_obj:
            stop_evt.wait(2)  # cola vacía, espera
            continue
        # Promover prioridad si sale_date llegó (Onsale Scheduler integration)
        _promote_if_onsale(ev_obj)
        # Check stock
        state["ticks"] = state.get("ticks", 0) + 1
        ev_obj["last_check_at"] = _now_iso()
        ev_obj["attempts"] = ev_obj.get("attempts", 0) + 1
        try:
            check_fn = _check_callback or _default_check
            result = check_fn(ev_obj)
        except Exception as e:
            result = {"stock_found": False, "error": str(e)[:200]}
        ev_obj["last_result"] = result
        # Fire si stock
        if result.get("stock_found"):
            state["fired"] = state.get("fired", 0) + 1
            ev_obj["fires"] = ev_obj.get("fires", 0) + 1
            ev_obj["status"] = "fired"
            try:
                fire_fn = _fire_callback or _default_fire
                fire_result = fire_fn(ev_obj)
            except Exception as e:
                fire_result = {"ok": False, "error": str(e)[:200]}
            ev_obj["last_fire_result"] = fire_result
            _append_log({"ts": _now_iso(), "event": "fired",
                          "event_id": ev_obj["event_id"],
                          "worker_id": worker_id,
                          "flow_id": fire_result.get("flow_id"),
                          "ok": fire_result.get("ok", False)})
            # WS push live: notifica dashboard y otros clientes
            _emit_ws("polling_rotate.fired", {
                "event_id": ev_obj["event_id"],
                "worker_id": worker_id,
                "fire_ok": fire_result.get("ok", False),
                "flow_id": fire_result.get("flow_id"),
                "url": ev_obj.get("url"),
                "domain": ev_obj.get("domain"),
                "ts": _now_iso(),
            })
            # Re-encola con priority degradada (ya no urgente)
            new_p = min(10, ev_obj["priority"] + 2)
            ev_obj["priority"] = new_p
        # Verificar max_attempts
        if ev_obj["attempts"] >= ev_obj.get("max_attempts", 100):
            ev_obj["status"] = "exhausted"
            _append_log({"ts": _now_iso(), "event": "exhausted",
                          "event_id": ev_obj["event_id"],
                          "attempts": ev_obj["attempts"]})
            # No reencola
        else:
            # Reencola con priority actual
            with _lock:
                _seq += 1
                heapq.heappush(_queue, (ev_obj["priority"], _seq, ev_obj["event_id"]))
        # Liberar inflight ANTES del rate limit
        with _inflight_lock:
            _inflight.discard(ev_obj["event_id"])
        _persist()
        # Pequeña espera entre checks (rate limit)
        stop_evt.wait(1)
    state["stopped_at"] = _now_iso()


def start_workers(n: int = 2) -> Dict[str, Any]:
    if _workers_state.get("running"):
        return {"ok": True, "status": "already_running",
                "n_workers": _workers_state.get("n_workers")}
    n = max(1, min(20, int(n)))
    stop_evt = threading.Event()
    workers = []
    threads = []
    for i in range(n):
        wid = f"pr_w{i+1}"
        state = {
            "id": wid,
            "started_at": _now_iso(),
            "ticks": 0,
            "fired": 0,
            "stop_event": stop_evt,
        }
        th = threading.Thread(target=_worker_loop, args=(wid, stop_evt),
                                daemon=True, name=f"polling_rotate_{wid}")
        state["thread"] = th
        workers.append(state)
        threads.append(th)
    # Poblar state ANTES de arrancar los threads (evita race con next() en _worker_loop)
    _workers_state.update({
        "running": True, "workers": workers, "stop_event": stop_evt,
        "n_workers": n, "started_at": _now_iso(),
    })
    for th in threads:
        th.start()
    return {"ok": True, "status": "started", "n_workers": n}


def stop_workers() -> Dict[str, Any]:
    if not _workers_state.get("running"):
        return {"ok": True, "status": "not_running"}
    evt = _workers_state.get("stop_event")
    if evt:
        evt.set()
    _workers_state["running"] = False
    return {"ok": True, "status": "stopped"}


def worker_status() -> Dict[str, Any]:
    workers = _workers_state.get("workers") or []
    return {
        "running": _workers_state.get("running", False),
        "n_workers": _workers_state.get("n_workers", 0),
        "started_at": _workers_state.get("started_at", ""),
        "queue_size": len(_queue),
        "events_total": len(_events),
        "workers": [{
            "id": w.get("id"),
            "ticks": w.get("ticks", 0),
            "fired": w.get("fired", 0),
            "started_at": w.get("started_at"),
        } for w in workers],
    }


def get_stats() -> Dict[str, Any]:
    with _lock:
        events = list(_events.values())
    by_status: Dict[str, int] = {}
    for e in events:
        s = e.get("status", "queued")
        by_status[s] = by_status.get(s, 0) + 1
    total_attempts = sum(e.get("attempts", 0) for e in events)
    total_fires = sum(e.get("fires", 0) for e in events)
    return {
        "events_total": len(events),
        "queue_size": len(_queue),
        "by_status": by_status,
        "total_attempts": total_attempts,
        "total_fires": total_fires,
        "fire_rate_pct": round(100 * total_fires / max(1, total_attempts), 2),
        "workers_running": _workers_state.get("running", False),
        "n_workers": _workers_state.get("n_workers", 0),
    }


def get_log(limit: int = 100) -> List[Dict[str, Any]]:
    try:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"entries": []})
        return d.get("entries", [])[:limit]
    except Exception:
        return []


def clear_all() -> Dict[str, Any]:
    """Limpia eventos + cola (NO los logs históricos)."""
    with _lock:
        n = len(_events)
        _events.clear()
        _queue.clear()
    _persist()
    return {"ok": True, "removed": n}


def self_test() -> Dict[str, Any]:
    out = {"ok": True, "checks": {}}
    clear_all()
    # 1. add events
    r1 = add_event("evt1", "https://tm.com/e/1", "tm.com", priority=3)
    r2 = add_event("evt2", "https://tm.com/e/2", "tm.com", priority=1)
    r3 = add_event("evt3", "https://tm.com/e/3", "tm.com", priority=5)
    out["checks"]["add_3_events"] = (r1["ok"] and r2["ok"] and r3["ok"])
    out["checks"]["queue_size_3"] = (len(_queue) == 3)
    out["checks"]["events_dict_3"] = (len(_events) == 3)
    # 2. priority order — al pop debería salir evt2 primero (priority=1)
    with _lock:
        if _queue:
            p, s, e = heapq.heappop(_queue)
            out["checks"]["highest_priority_first"] = (e == "evt2" and p == 1)
            heapq.heappush(_queue, (p, s, e))
    # 3. set_priority
    sp = set_priority("evt3", 1)
    out["checks"]["set_priority_works"] = (sp.get("ok") and
                                              _events["evt3"]["priority"] == 1)
    # 4. callbacks — inyectar mocks que firen 1 evento, no firen otro
    fired = []
    def mock_check(ev):
        # evt2 tiene stock, otros no
        return {"stock_found": ev["event_id"] == "evt2"}
    def mock_fire(ev):
        fired.append(ev["event_id"])
        return {"ok": True, "flow_id": "mock_" + ev["event_id"]}
    set_check_callback(mock_check)
    set_fire_callback(mock_fire)
    # 5. Start 1 worker, esperar 3s
    sw = start_workers(n=1)
    out["checks"]["start_worker"] = sw.get("ok", False)
    time.sleep(3.5)
    # 6. evt2 debería haber sido fired
    out["checks"]["evt2_fired"] = ("evt2" in fired)
    # 7. stop
    stop_workers()
    out["checks"]["stop_worker"] = not _workers_state.get("running")
    # 8. stats
    s = get_stats()
    out["checks"]["stats_total_3"] = (s.get("events_total") == 3)
    out["checks"]["stats_some_attempts"] = (s.get("total_attempts", 0) >= 1)
    # 9. remove
    rm = remove_event("evt1")
    out["checks"]["remove_works"] = rm.get("ok", False)
    # 10. clear callbacks
    set_check_callback(None)
    set_fire_callback(None)
    # 11. Worker status
    ws = worker_status()
    out["checks"]["worker_status_returns"] = isinstance(ws, dict)
    # 12. Onsale promote — sale_date pasada debe forzar priority=1
    add_event("evt_onsale", priority=8,
              sale_date=(datetime(2020, 1, 1)).isoformat())
    ev = _events["evt_onsale"]
    _promote_if_onsale(ev)
    out["checks"]["onsale_promote"] = (ev["priority"] == 1)
    # 13. Inflight prevent dupe — añadir manualmente y verificar
    _inflight.add("evt_dummy")
    out["checks"]["inflight_set_works"] = ("evt_dummy" in _inflight)
    _inflight.discard("evt_dummy")
    out["checks"]["inflight_clears"] = ("evt_dummy" not in _inflight)
    # cleanup
    clear_all()

    out["passed"] = sum(1 for v in out["checks"].values() if v)
    out["total"] = len(out["checks"])
    out["ok"] = out["passed"] == out["total"]
    return out


def get_info() -> Dict[str, Any]:
    return {
        "module": "polling_rotate",
        "events_total": len(_events),
        "queue_size": len(_queue),
        "workers_running": _workers_state.get("running", False),
        "stats": get_stats(),
        "endpoints": [
            "POST /api/polling-rotate/event",
            "DELETE /api/polling-rotate/event/<eid>",
            "GET /api/polling-rotate/events",
            "POST /api/polling-rotate/priority",
            "POST /api/polling-rotate/workers/<action>",
            "GET /api/polling-rotate/status",
            "GET /api/polling-rotate/log",
            "POST /api/polling-rotate/self-test",
        ],
    }


# Auto-load al import
_persist_load()


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=2))
