"""audit_log.py — Registro estructurado de acciones del usuario.

Events recorded automatically:
- auth:login_ok / auth:login_fail / auth:logout
- user:created / user:deleted / user:role_changed
- event:created / event:updated / event:deleted
- note:created / note:updated / note:deleted / note:sent_to_claude
- purchase:flow_run / purchase:flow_ok / purchase:flow_fail
- bot:worker_start / bot:worker_stop
- browser:engine_launched / browser:engine_fail
- settings:updated
- backup:created / backup:restored
- notification:test / notification:sent
- captcha:solved / captcha:failed
- proxy:added / proxy:check / proxy:dead

Fields: ts, actor (user), action, target, ip, user_agent, meta (dict), success.
Retention: 5000 entries rolling.
"""
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from collections import deque

import config
import json_atomic
import sqlite_store


_PATH_KEY = "audit_log.json"
_MAX_ENTRIES = 5000
_lock = threading.Lock()


def _path():
    return config.ruta(_PATH_KEY)


# In-memory ring for fast access, persisted to disk every N writes
_ring = deque(maxlen=_MAX_ENTRIES)
_dirty_count = 0
_DIRTY_FLUSH = 10


def _load_initial():
    """Carga los eventos del disco al arrancar."""
    global _ring
    try:
        d = sqlite_store.load(_path(), default={"entries": []})
        entries = d.get("entries", [])[-_MAX_ENTRIES:]
        _ring = deque(entries, maxlen=_MAX_ENTRIES)
    except Exception:
        _ring = deque(maxlen=_MAX_ENTRIES)


_load_initial()


def record(action: str, actor: str = "system", target: str = "",
           ip: str = "", user_agent: str = "",
           meta: Optional[Dict] = None, success: bool = True) -> dict:
    """Registra un evento. action debe ser 'categoria:accion' (p.ej. auth:login_ok)."""
    global _dirty_count
    entry = {
        "ts": datetime.now().isoformat(),
        "action": action,
        "actor": actor or "system",
        "target": target or "",
        "ip": ip or "",
        "user_agent": (user_agent or "")[:200],
        "meta": meta or {},
        "success": bool(success),
    }
    with _lock:
        _ring.append(entry)
        _dirty_count += 1
        if _dirty_count >= _DIRTY_FLUSH:
            _flush()
    return entry


def _flush():
    """Persiste el ring a disco."""
    global _dirty_count
    try:
        sqlite_store.save(_path(), {"entries": list(_ring)})
        _dirty_count = 0
    except Exception:
        pass


def flush():
    """Fuerza flush a disco."""
    with _lock:
        _flush()


def query(action: str = "", actor: str = "", success: Optional[bool] = None,
          since: Optional[str] = None, limit: int = 200) -> List[dict]:
    """Consulta con filtros básicos. since en ISO."""
    with _lock:
        items = list(_ring)
    results = []
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except Exception:
            since_dt = None
    for e in reversed(items):  # más recientes primero
        if action and action not in e["action"]:
            continue
        if actor and e["actor"] != actor:
            continue
        if success is not None and e["success"] != success:
            continue
        if since_dt:
            try:
                if datetime.fromisoformat(e["ts"]) < since_dt:
                    continue
            except Exception:
                continue
        results.append(e)
        if len(results) >= limit:
            break
    return results


def get_stats(hours: int = 24) -> dict:
    """Estadísticas últimas N horas."""
    with _lock:
        items = list(_ring)
    cutoff = datetime.now() - timedelta(hours=hours)
    recent = []
    for e in items:
        try:
            ts = datetime.fromisoformat(e["ts"])
            if ts >= cutoff:
                recent.append(e)
        except Exception:
            continue

    by_action = {}
    by_actor = {}
    failures = 0
    timeline = [0] * hours  # buckets por hora
    for e in recent:
        cat = e["action"].split(":")[0] if ":" in e["action"] else e["action"]
        by_action[cat] = by_action.get(cat, 0) + 1
        by_actor[e["actor"]] = by_actor.get(e["actor"], 0) + 1
        if not e["success"]:
            failures += 1
        try:
            ts = datetime.fromisoformat(e["ts"])
            hours_ago = (datetime.now() - ts).total_seconds() / 3600
            bucket = hours - 1 - int(hours_ago)
            if 0 <= bucket < hours:
                timeline[bucket] += 1
        except Exception:
            continue

    return {
        "total": len(recent),
        "window_hours": hours,
        "by_action": by_action,
        "by_actor": by_actor,
        "failures": failures,
        "timeline": timeline,
        "unique_actors": len(by_actor),
        "last_event": recent[-1] if recent else None,
    }


def clear() -> dict:
    """Limpia todo el log."""
    with _lock:
        _ring.clear()
        _flush()
    return {"ok": True}


def event_types() -> List[str]:
    """Retorna todos los tipos de evento registrados."""
    with _lock:
        items = list(_ring)
    return sorted({e["action"] for e in items})
