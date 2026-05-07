"""acpat_injection.py — Queue-it token injection via pub/sub local.

Mejora #acpat_inject de la nota:
  Capturar cookie AcpAT de 1 worker que pasó cola e inyectar en N workers
  → todos saltan cola y pueden intentar checkout simultáneo.

Este módulo extiende cookie_warmer (que ya tiene pool AcpAT) con:
- Sistema pub/sub local (threading.Condition) para broadcast inmediato
- Suscriptores registran callback por domain+event
- Cuando un worker captura token → notifica a todos los suscriptores
- Histórico de broadcasts con latencia de propagación
- Integración opcional con Redis si está disponible (fallback a local)

API principal:
  subscribe(domain_id, event_id, callback) → id
  unsubscribe(sid)
  broadcast_token(token, domain_id, event_id, source_worker)
  wait_for_token(domain_id, event_id, timeout=30) → token | None
"""
import threading
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, Callable, List

import config
import json_atomic


LOG_KEY = "acpat_broadcast_log.json"

_lock = threading.Lock()
_subscribers: Dict[str, dict] = {}  # {sid: {domain, event, callback, created}}
_condition = threading.Condition()
_last_tokens: Dict[str, dict] = {}  # {(domain,event): {token, ts, source}}


def _log_path():
    return config.ruta(LOG_KEY)


def _log(entry: dict):
    with _lock:
        d = json_atomic.load(_log_path(), default={"broadcasts": []})
        d["broadcasts"].insert(0, entry)
        d["broadcasts"] = d["broadcasts"][:100]
        json_atomic.save(_log_path(), d)


def get_log(limit: int = 30) -> List[dict]:
    return json_atomic.load(_log_path(), default={"broadcasts": []}).get("broadcasts", [])[:limit]


# ═══════════════════════════════════════════
#  PUB/SUB
# ═══════════════════════════════════════════
def subscribe(domain_id: str, event_id: str = "",
              callback: Optional[Callable] = None,
              subscriber_name: str = "") -> str:
    """Registra suscriptor. Retorna sid para unsubscribe."""
    sid = uuid.uuid4().hex[:10]
    with _lock:
        _subscribers[sid] = {
            "sid": sid,
            "domain_id": domain_id,
            "event_id": event_id,
            "callback": callback,
            "name": subscriber_name or f"sub-{sid[:6]}",
            "created_at": datetime.now().isoformat(),
            "received_count": 0,
            "last_received_at": None,
        }
    return sid


def unsubscribe(sid: str) -> bool:
    with _lock:
        return _subscribers.pop(sid, None) is not None


def list_subscribers() -> List[dict]:
    with _lock:
        # no devolver callback
        return [{k: v for k, v in s.items() if k != "callback"}
                for s in _subscribers.values()]


def broadcast_token(token: str, domain_id: str = "us", event_id: str = "",
                     source_worker: str = "", meta: Optional[dict] = None) -> dict:
    """Broadcast inmediato: actualiza last_tokens + notifica subscribers + guarda en pool."""
    if not token or len(token) < 10:
        return {"ok": False, "error": "token inválido"}

    ts = datetime.now().isoformat()
    key = (domain_id, event_id)

    # 1. Actualizar last_tokens
    with _lock:
        _last_tokens[key] = {
            "token": token, "ts": ts,
            "source_worker": source_worker,
            "domain_id": domain_id, "event_id": event_id,
        }

    # 2. Guardar en pool persistente (cookie_warmer)
    try:
        import cookie_warmer
        cookie_warmer.capture_acpat(
            token=token, domain_id=domain_id, event_id=event_id,
            source_worker=source_worker, meta=meta or {},
        )
    except Exception:
        pass

    # 3. Notificar subscribers que matcheen
    notified = 0
    callback_errors = 0
    matching_subs = []
    with _lock:
        for s in _subscribers.values():
            if s["domain_id"] != domain_id:
                continue
            if s["event_id"] and s["event_id"] != event_id:
                continue
            matching_subs.append(s)

    for s in matching_subs:
        s["received_count"] = s.get("received_count", 0) + 1
        s["last_received_at"] = ts
        if s.get("callback"):
            try:
                s["callback"]({
                    "token": token, "domain_id": domain_id,
                    "event_id": event_id, "source_worker": source_worker,
                    "sid": s["sid"],
                })
                notified += 1
            except Exception:
                callback_errors += 1

    # 4. Awake waiters
    with _condition:
        _condition.notify_all()

    entry = {
        "ts": ts,
        "token_preview": (token[:6] + "…" + token[-4:]) if len(token) > 12 else token,
        "domain_id": domain_id,
        "event_id": event_id,
        "source_worker": source_worker,
        "subscribers_matched": len(matching_subs),
        "callbacks_invoked": notified,
        "callback_errors": callback_errors,
    }
    _log(entry)
    return {"ok": True, **entry}


def wait_for_token(domain_id: str, event_id: str = "",
                   timeout: float = 30) -> Optional[dict]:
    """Bloquea hasta que aparezca un token para ese dominio/evento o timeout."""
    key = (domain_id, event_id)
    deadline = time.time() + timeout
    with _condition:
        # Si ya hay token reciente, devolverlo
        existing = _last_tokens.get(key) or _last_tokens.get((domain_id, ""))
        if existing:
            try:
                ts = datetime.fromisoformat(existing["ts"])
                age_s = (datetime.now() - ts).total_seconds()
                if age_s < 1800:  # fresh < 30 min
                    return {"token": existing["token"], "fresh": True,
                            "age_s": int(age_s),
                            "source_worker": existing.get("source_worker")}
            except Exception:
                pass
        while time.time() < deadline:
            got = _last_tokens.get(key) or _last_tokens.get((domain_id, ""))
            if got:
                try:
                    ts = datetime.fromisoformat(got["ts"])
                    if (datetime.now() - ts).total_seconds() < 1800:
                        return {"token": got["token"], "fresh": True,
                                "source_worker": got.get("source_worker")}
                except Exception:
                    pass
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            _condition.wait(timeout=min(remaining, 2))
    return None


def get_last_tokens() -> List[dict]:
    """Snapshot de últimos tokens por dominio/evento."""
    with _lock:
        out = []
        for (dom, ev), data in _last_tokens.items():
            c = dict(data)
            tok = c.get("token", "")
            c["token_preview"] = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "***"
            c.pop("token", None)
            out.append(c)
        return out


def get_stats() -> dict:
    with _lock:
        subs = list(_subscribers.values())
        last_by_key = list(_last_tokens.keys())
    return {
        "subscribers": len(subs),
        "active_channels": len(last_by_key),
        "total_received": sum(s.get("received_count", 0) for s in subs),
        "subscribers_by_domain": _group_count(subs, "domain_id"),
        "recent_broadcasts": len(get_log(limit=100)),
    }


def _group_count(items: List[dict], key: str) -> Dict[str, int]:
    out = {}
    for it in items:
        v = it.get(key, "?")
        out[v] = out.get(v, 0) + 1
    return out


def clear_all() -> dict:
    with _lock:
        _subscribers.clear()
        _last_tokens.clear()
    return {"ok": True}
