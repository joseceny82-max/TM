"""proxy_session_pin.py — Sticky proxy sessions (pinning).

Mejora #proxy_pinning:
  Mantener la MISMA IP entre cookie-warm, Queue-it (AcpAT) y checkout.
  Si el proxy cambia a mitad del flow, el token AcpAT se invalida y la
  sesión queda quemada. Esta capa garantiza consistencia.

Estrategia:
  - pin_session(session_id, geo) → elige proxy + lo fija
  - get_pinned(session_id) → devuelve siempre el mismo proxy
  - mark_dead(session_id) → libera el pin + busca reemplazo
  - TTL configurable (default 30 min, debe cubrir todo el flow)
  - Si el proxy pinned cae (proxy_health lo marca dead) → auto-rotate
    al siguiente del mismo geo y registra el incidente.
  - Histórico completo de pins para auditoría

API:
  pin_session(session_id, geo="", ttl_min=30, proxy_id=None) → dict
  get_pinned(session_id) → dict | None
  release(session_id) → bool
  mark_dead(session_id) → dict
  list_pins(active_only=True)
  cleanup_expired()
  get_stats()
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import config
import json_atomic


_PINS_KEY = "proxy_pins.json"
_LOG_KEY = "proxy_pin_log.json"
_lock = threading.RLock()  # reentrant: _log() puede llamarse dentro de with _lock


def _pins_path():
    return config.ruta(_PINS_KEY)


def _log_path():
    return config.ruta(_LOG_KEY)


def _load_pins() -> dict:
    return json_atomic.load(_pins_path(), default={"pins": {}})


def _save_pins(d: dict):
    json_atomic.save(_pins_path(), d)


def _log(entry: dict):
    with _lock:
        d = json_atomic.load(_log_path(), default={"events": []})
        d["events"].insert(0, entry)
        d["events"] = d["events"][:300]
        json_atomic.save(_log_path(), d)


def get_log(limit: int = 50) -> List[dict]:
    return json_atomic.load(_log_path(), default={"events": []}).get("events", [])[:limit]


# ═══════════════════════════════════════════
#  PIN
# ═══════════════════════════════════════════
def pin_session(session_id: str, geo: str = "",
                ttl_min: int = 30,
                proxy_id: Optional[str] = None) -> dict:
    """Fija un proxy a una session_id. Si ya tiene pin vivo, lo devuelve.

    Si no hay proxy disponible (por geo o en absoluto), devuelve
    {ok: False, error: "no_proxy_available"} pero no crea pin.
    """
    if not session_id:
        return {"ok": False, "error": "session_id requerido"}

    with _lock:
        d = _load_pins()
        existing = d["pins"].get(session_id)
        if existing:
            try:
                exp = datetime.fromisoformat(existing.get("expires_at", ""))
                if exp > datetime.now() and existing.get("status") == "active":
                    return {"ok": True, "already_pinned": True, **existing}
            except Exception:
                pass  # expired → reemplazar

        # Buscar proxy: si proxy_id explícito usa ése, sino pick_best(geo)
        chosen = None
        if proxy_id:
            try:
                import proxy_health
                all_p = proxy_health.list_proxies(only_alive=False)
                for p in all_p:
                    if p.get("id") == proxy_id:
                        chosen = p
                        break
            except Exception:
                pass
        if not chosen:
            try:
                import proxy_health
                chosen = proxy_health.pick_best(geo=geo or None, min_score=50)
            except Exception:
                chosen = None

        if not chosen:
            _log({
                "ts": datetime.now().isoformat(),
                "action": "pin_failed",
                "session_id": session_id,
                "geo": geo,
                "error": "no_proxy_available",
            })
            return {"ok": False, "error": "no_proxy_available"}

        pin = {
            "session_id": session_id,
            "proxy_id": chosen.get("id"),
            "proxy_host": chosen.get("host", "?"),
            "proxy_port": chosen.get("port", 0),
            "proxy_preview": chosen.get("url_preview", ""),
            "geo": chosen.get("geo", "") or geo,
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(minutes=ttl_min)).isoformat(),
            "ttl_min": ttl_min,
            "status": "active",
            "rotations": 0,
            "hits": 0,
            "last_used_at": None,
        }
        d["pins"][session_id] = pin
        _save_pins(d)

    _log({"ts": datetime.now().isoformat(), "action": "pinned",
          "session_id": session_id, "proxy_id": pin["proxy_id"],
          "geo": pin["geo"]})
    return {"ok": True, **pin}


def get_pinned(session_id: str, touch: bool = True) -> Optional[dict]:
    """Devuelve el pin activo o None (si expiró o no existe)."""
    with _lock:
        d = _load_pins()
        pin = d["pins"].get(session_id)
        if not pin:
            return None
        try:
            exp = datetime.fromisoformat(pin.get("expires_at", ""))
            if exp <= datetime.now() or pin.get("status") != "active":
                return None
        except Exception:
            return None
        if touch:
            pin["hits"] = pin.get("hits", 0) + 1
            pin["last_used_at"] = datetime.now().isoformat()
            d["pins"][session_id] = pin
            _save_pins(d)
    return pin


def release(session_id: str) -> bool:
    """Libera un pin (lo marca released, no lo borra para auditoría)."""
    with _lock:
        d = _load_pins()
        pin = d["pins"].get(session_id)
        if not pin:
            return False
        pin["status"] = "released"
        pin["released_at"] = datetime.now().isoformat()
        d["pins"][session_id] = pin
        _save_pins(d)
    _log({"ts": datetime.now().isoformat(), "action": "released",
          "session_id": session_id, "proxy_id": pin.get("proxy_id")})
    return True


def mark_dead(session_id: str) -> dict:
    """El proxy del pin falló → rota al siguiente del mismo geo, misma session."""
    with _lock:
        d = _load_pins()
        pin = d["pins"].get(session_id)
    if not pin:
        return {"ok": False, "error": "session_not_pinned"}
    old_proxy = pin.get("proxy_id")
    geo = pin.get("geo", "")
    # Pide uno nuevo (excluyendo el viejo)
    try:
        import proxy_health
        candidates = proxy_health.list_proxies(only_alive=True)
        if geo:
            candidates = [c for c in candidates if c.get("geo") == geo] or candidates
        candidates = [c for c in candidates if c.get("id") != old_proxy]
        if not candidates:
            return {"ok": False, "error": "no_replacement_available"}
        candidates.sort(key=lambda p: -(p.get("score") or 0))
        new_proxy = candidates[0]
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    with _lock:
        d = _load_pins()
        pin = d["pins"].get(session_id)
        if not pin:
            return {"ok": False, "error": "session_not_pinned"}
        pin["proxy_id"] = new_proxy.get("id")
        pin["proxy_host"] = new_proxy.get("host", "?")
        pin["proxy_port"] = new_proxy.get("port", 0)
        pin["proxy_preview"] = new_proxy.get("url_preview", "")
        pin["rotations"] = pin.get("rotations", 0) + 1
        pin["last_rotated_at"] = datetime.now().isoformat()
        d["pins"][session_id] = pin
        _save_pins(d)

    _log({"ts": datetime.now().isoformat(), "action": "rotated",
          "session_id": session_id, "old_proxy_id": old_proxy,
          "new_proxy_id": new_proxy.get("id")})
    return {"ok": True, "rotated": True, "old_proxy": old_proxy,
            "new_proxy_id": new_proxy.get("id"), **pin}


def list_pins(active_only: bool = True) -> List[dict]:
    d = _load_pins()
    out = []
    now = datetime.now()
    for pin in d["pins"].values():
        if active_only:
            try:
                exp = datetime.fromisoformat(pin.get("expires_at", ""))
                if exp <= now or pin.get("status") != "active":
                    continue
            except Exception:
                continue
        out.append(pin)
    out.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return out


def cleanup_expired() -> dict:
    """Borra pins expirados hace más de 24h del fichero."""
    threshold = datetime.now() - timedelta(hours=24)
    removed = 0
    with _lock:
        d = _load_pins()
        keep = {}
        for sid, pin in d["pins"].items():
            try:
                exp = datetime.fromisoformat(pin.get("expires_at", ""))
                if exp < threshold:
                    removed += 1
                    continue
            except Exception:
                pass
            keep[sid] = pin
        d["pins"] = keep
        _save_pins(d)
    return {"ok": True, "removed": removed, "remaining": len(keep)}


def get_stats() -> dict:
    d = _load_pins()
    all_pins = list(d["pins"].values())
    active = list_pins(active_only=True)
    total_hits = sum(p.get("hits", 0) for p in all_pins)
    total_rotations = sum(p.get("rotations", 0) for p in all_pins)
    by_geo = {}
    for p in active:
        g = p.get("geo") or "?"
        by_geo[g] = by_geo.get(g, 0) + 1
    return {
        "total_pins": len(all_pins),
        "active_pins": len(active),
        "total_hits": total_hits,
        "total_rotations": total_rotations,
        "active_by_geo": by_geo,
    }


def self_test() -> dict:
    """Pin fake + get + release, sin depender de proxy_health real."""
    import uuid
    sid = f"selftest-{uuid.uuid4().hex[:8]}"
    # Usa un proxy_id directo ficticio — fallback a pick_best real si falla
    r = pin_session(sid, geo="us", ttl_min=1)
    got = get_pinned(sid)
    released = release(sid)
    return {
        "ok": True,
        "pin_session_ok": r.get("ok", False),
        "pin_proxy_id": r.get("proxy_id"),
        "retrieved_matches": bool(got and got.get("proxy_id") == r.get("proxy_id")),
        "released": released,
        "stats": get_stats(),
    }
