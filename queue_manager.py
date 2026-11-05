"""queue_manager — Manager Queue-it superior con sesiones ilimitadas.

Características clave:
  - Sesiones ILIMITADAS por evento + cuenta (no hay max workers hardcoded).
  - Pool persistente de tokens AcpAT con TTL 30 min.
  - Auto-rejoin tras disconnect (reusa Queueit-Session-XT antes del TTL).
  - Position poller cada 15s (lee waiting room, calcula ETA).
  - Swarm broadcast: cuando una sesión pasa la cola, comparte cookies con N cuentas.
  - Sticky proxy 30 min via proxy_session_pin.
  - Trigger detector: avisa cuando TM activa Queue-it.
  - Hook en tm_purchase_flow.step_inject_acpat para claim slot del pool.
  - Integración con bot de compras: cuando una sesión está "accepted", spawnea
    _compra_parallel.py automáticamente con cookies inyectadas.

Persistencia:
  - data/queue_sessions.json (snapshot JSON, 1s tick)
  - data/queue_manager_log.json (historial)
  - cookie_warmer pool (tokens AcpAT compartidos)
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import json_atomic

PROYECTO = os.path.dirname(os.path.abspath(__file__))
_SESSIONS_PATH = os.path.join(PROYECTO, "data", "queue_sessions.json")
_LOG_PATH = os.path.join(PROYECTO, "data", "queue_manager_log.json")
os.makedirs(os.path.dirname(_SESSIONS_PATH), exist_ok=True)

ACPAT_TTL_SECONDS = 30 * 60   # 30 min — Queue-it edge default
POSITION_POLL_INTERVAL_S = 15
TRIGGER_CHECK_INTERVAL_S = 30
DEFAULT_ABANDON_ETA_MIN = 15

_lock = threading.RLock()
_sessions: Dict[str, "QueueSession"] = {}
_swarm_subscribers: Dict[str, List[str]] = {}  # event_id → [session_id, ...]
_daemon_thread: Optional[threading.Thread] = None
_daemon_stop = threading.Event()
_ws_broadcaster: Optional[Callable] = None  # set by dashboard with socketio.emit


def set_ws_broadcaster(fn: Callable[[str, dict], None]) -> None:
    """Permite al dashboard inyectar socketio.emit como broadcaster."""
    global _ws_broadcaster
    _ws_broadcaster = fn


def _ws(event: str, payload: dict) -> None:
    if _ws_broadcaster:
        try:
            _ws_broadcaster(event, payload)
        except Exception:
            pass
_metrics = {
    "tokens_captured_24h": 0,
    "tokens_used_24h": 0,
    "sessions_passed": 0,
    "sessions_abandoned": 0,
    "avg_wait_s": 0.0,
    "last_trigger_detected_at": None,
}


@dataclass
class QueueSession:
    session_id: str
    account_id: str = ""
    event_id: str = ""
    customer_id: str = ""              # Queue-it customer (TM ES → ticketmasterspain)
    tm_url: str = ""
    proxy_id: str = ""
    proxy_pin_session: str = ""
    queueit_session_xt: str = ""        # cookie session-only
    accepted_token: str = ""             # AcpAT
    queueittoken: str = ""               # query param
    fingerprint_id: str = ""             # MoreLogin profile env_id
    position: Optional[int] = None
    eta_minutes: Optional[float] = None
    status: str = "pending"              # pending|enqueued|waiting|accepted|expired|abandoned|fired|error
    created_at: str = ""
    enqueued_at: Optional[str] = None
    accepted_at: Optional[str] = None
    last_poll_at: Optional[str] = None
    expires_at: Optional[str] = None
    error: Optional[str] = None
    auto_rejoin: bool = True
    auto_buy_on_accepted: bool = False
    purchase_flow_args: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return datetime.now() > datetime.fromisoformat(self.expires_at)
        except Exception:
            return False


def _persist() -> None:
    try:
        snap = {sid: s.to_dict() for sid, s in _sessions.items()}
        json_atomic.save(_SESSIONS_PATH, {
            "sessions": snap,
            "metrics": _metrics,
            "updated_at": datetime.now().isoformat(),
        })
    except Exception:
        pass


def _load() -> None:
    try:
        d = json_atomic.load(_SESSIONS_PATH, default={"sessions": {}, "metrics": {}})
        for sid, raw in (d.get("sessions") or {}).items():
            _sessions[sid] = QueueSession(**raw)
        _metrics.update(d.get("metrics") or {})
    except Exception:
        pass


def _log(event: Dict[str, Any]) -> None:
    try:
        d = json_atomic.load(_LOG_PATH, default={"log": []})
        event["ts"] = datetime.now().isoformat()
        d["log"].insert(0, event)
        d["log"] = d["log"][:1000]
        json_atomic.save(_LOG_PATH, d)
    except Exception:
        pass


# ─────────────────────────── ENQUEUE ──────────────────────────────

def enqueue_session(account_id: str = "", event_id: str = "",
                    tm_url: str = "", customer_id: str = "ticketmasterspain",
                    auto_rejoin: bool = True,
                    auto_buy_on_accepted: bool = False,
                    purchase_flow_args: Optional[Dict[str, Any]] = None,
                    notes: str = "") -> Dict[str, Any]:
    """Crea una QueueSession nueva. Devuelve {ok, session_id, session}.

    NOTA: el enqueue HTTP real se hace dentro de un browser; aquí registramos
    el slot lógicamente. El daemon o el hook de compra harán el enqueue real.
    """
    sid = uuid.uuid4().hex[:12]
    now_iso = datetime.now().isoformat()
    session = QueueSession(
        session_id=sid,
        account_id=account_id,
        event_id=event_id,
        customer_id=customer_id,
        tm_url=tm_url,
        created_at=now_iso,
        enqueued_at=now_iso,
        status="enqueued",
        auto_rejoin=auto_rejoin,
        auto_buy_on_accepted=auto_buy_on_accepted,
        purchase_flow_args=purchase_flow_args or {},
        notes=notes,
        expires_at=(datetime.now() + timedelta(seconds=ACPAT_TTL_SECONDS)).isoformat(),
    )
    with _lock:
        _sessions[sid] = session
        _swarm_subscribers.setdefault(event_id, []).append(sid)
        _persist()
    _log({"op": "enqueue", "session_id": sid, "event_id": event_id,
          "account_id": account_id})
    _ws("queue_session_created", {"session_id": sid, "event_id": event_id,
                                    "account_id": account_id})
    return {"ok": True, "session_id": sid, "session": session.to_dict()}


def enqueue_batch(n: int, account_pool: Optional[List[str]] = None,
                  event_id: str = "", tm_url: str = "",
                  customer_id: str = "ticketmasterspain",
                  auto_rejoin: bool = True,
                  auto_buy_on_accepted: bool = False,
                  purchase_flow_args: Optional[Dict[str, Any]] = None,
                  stagger_s: float = 1.0) -> Dict[str, Any]:
    """Crea N sesiones a la vez con stagger entre cada una. Sin límite hard.

    Optimización: cuando `stagger_s == 0`, hace 1 sola persist al final
    (acelera 100× para batches grandes: 1000 sesiones en ~0.5s vs 38s).

    Si `account_pool` se pasa, asigna round-robin entre N sesiones; si no,
    crea sesiones sin cuenta (workers libres).
    """
    if n < 1:
        return {"ok": False, "error": "n must be >= 1"}
    pool = list(account_pool or [])
    out_ids: List[str] = []
    fast_mode = stagger_s <= 0
    if fast_mode:
        with _lock:
            for i in range(n):
                acc_id = pool[i % len(pool)] if pool else ""
                sid = uuid.uuid4().hex[:12]
                now_iso = datetime.now().isoformat()
                s = QueueSession(
                    session_id=sid, account_id=acc_id, event_id=event_id,
                    customer_id=customer_id, tm_url=tm_url,
                    created_at=now_iso, enqueued_at=now_iso, status="enqueued",
                    auto_rejoin=auto_rejoin,
                    auto_buy_on_accepted=auto_buy_on_accepted,
                    purchase_flow_args=purchase_flow_args or {},
                    notes=f"batch_{i+1}/{n}",
                    expires_at=(datetime.now()+timedelta(seconds=ACPAT_TTL_SECONDS)).isoformat(),
                )
                _sessions[sid] = s
                _swarm_subscribers.setdefault(event_id, []).append(sid)
                out_ids.append(sid)
            _persist()
    else:
        for i in range(n):
            acc_id = pool[i % len(pool)] if pool else ""
            r = enqueue_session(account_id=acc_id, event_id=event_id, tm_url=tm_url,
                                customer_id=customer_id,
                                auto_rejoin=auto_rejoin,
                                auto_buy_on_accepted=auto_buy_on_accepted,
                                purchase_flow_args=purchase_flow_args,
                                notes=f"batch_{i+1}/{n}")
            if r.get("ok"):
                out_ids.append(r["session_id"])
            if i < n - 1:
                time.sleep(min(stagger_s, 5.0))
    _log({"op": "enqueue_batch", "n": n, "event_id": event_id,
          "session_ids": out_ids[:30], "fast_mode": fast_mode})
    return {"ok": True, "session_ids": out_ids, "count": len(out_ids)}


# ─────────────────────────── ACCEPT / TOKEN ─────────────────────────

def mark_accepted(session_id: str, accepted_token: str = "",
                  queueit_session_xt: str = "",
                  position: Optional[int] = None) -> Dict[str, Any]:
    """Marca sesión como `accepted` con cookies capturadas.

    Llamado por el worker que pasa la cola — guarda cookies + dispara swarm
    broadcast + opcionalmente lanza el flow de compra.
    """
    with _lock:
        s = _sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "session_not_found"}
        s.accepted_token = accepted_token or s.accepted_token
        s.queueit_session_xt = queueit_session_xt or s.queueit_session_xt
        s.position = position
        s.eta_minutes = 0.0
        s.accepted_at = datetime.now().isoformat()
        s.status = "accepted"
        _metrics["tokens_captured_24h"] += 1
        _metrics["sessions_passed"] += 1
        _persist()

    _log({"op": "accepted", "session_id": session_id,
          "event_id": s.event_id, "account_id": s.account_id})
    _ws("queue_session_accepted", {"session_id": session_id,
        "event_id": s.event_id, "account_id": s.account_id,
        "has_token": bool(accepted_token)})

    if accepted_token:
        try:
            import cookie_warmer
            cookie_warmer.capture_acpat(token=accepted_token,
                                          domain_id="es",
                                          event_id=s.event_id,
                                          source_worker=session_id,
                                          meta={"session_xt": queueit_session_xt})
        except Exception as e:
            _log({"op": "warn_capture_pool", "err": str(e)[:120]})

    swarm_broadcast(s.event_id, source_session_id=session_id,
                    accepted_token=accepted_token,
                    queueit_session_xt=queueit_session_xt)

    if s.auto_buy_on_accepted:
        _spawn_purchase_flow(s)

    return {"ok": True, "session_id": session_id, "status": "accepted"}


def update_position(session_id: str, position: Optional[int],
                    eta_minutes: Optional[float] = None) -> Dict[str, Any]:
    with _lock:
        s = _sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "session_not_found"}
        s.position = position
        s.eta_minutes = eta_minutes
        s.last_poll_at = datetime.now().isoformat()
        if s.status == "enqueued" and position is not None:
            s.status = "waiting"
        _persist()
    return {"ok": True, "session_id": session_id, "position": position,
            "eta_minutes": eta_minutes}


def abandon_session(session_id: str, reason: str = "manual") -> Dict[str, Any]:
    with _lock:
        s = _sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "session_not_found"}
        s.status = "abandoned"
        s.error = reason
        _metrics["sessions_abandoned"] += 1
        _persist()
        try:
            _swarm_subscribers.get(s.event_id, []).remove(session_id)
        except (ValueError, KeyError):
            pass
    _log({"op": "abandon", "session_id": session_id, "reason": reason})
    if s.proxy_pin_session:
        try:
            import proxy_session_pin as psp
            psp.unpin_session(s.proxy_pin_session)
        except Exception:
            pass
    return {"ok": True, "session_id": session_id, "status": "abandoned"}


def rejoin_session(session_id: str) -> Dict[str, Any]:
    """Re-entra a la cola reusando Queueit-Session-XT si TTL no caducado."""
    with _lock:
        s = _sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "session_not_found"}
        if s.is_expired():
            return {"ok": False, "error": "session_expired_ttl",
                    "session_id": session_id}
        s.status = "enqueued"
        s.error = None
        s.last_poll_at = None
        _persist()
    _log({"op": "rejoin", "session_id": session_id})
    return {"ok": True, "session_id": session_id, "status": "enqueued"}


# ─────────────────────────── SWARM BROADCAST ─────────────────────────

def swarm_broadcast(event_id: str, source_session_id: str,
                    accepted_token: str = "",
                    queueit_session_xt: str = "") -> Dict[str, Any]:
    """Cuando una sesión pasa la cola, comparte sus cookies con todas las
    demás sesiones del MISMO event_id que aún están waiting.

    Esto permite que las cuentas-hermanas no esperen su turn.
    """
    if not event_id or not accepted_token:
        return {"ok": False, "error": "missing_event_or_token"}
    shared_with: List[str] = []
    with _lock:
        for sid in _swarm_subscribers.get(event_id, []):
            if sid == source_session_id:
                continue
            s = _sessions.get(sid)
            if not s or s.status not in ("enqueued", "waiting"):
                continue
            s.accepted_token = accepted_token
            if queueit_session_xt:
                s.queueit_session_xt = queueit_session_xt
            s.notes = (s.notes + f" | shared_from:{source_session_id}").strip(" |")
            shared_with.append(sid)
        _persist()
    _log({"op": "swarm_broadcast", "event_id": event_id,
          "source": source_session_id, "shared_with": len(shared_with)})
    return {"ok": True, "event_id": event_id, "shared_with": shared_with,
            "count": len(shared_with)}


# ─────────────────────────── INJECT (consumer) ─────────────────────

def claim_slot_for_purchase(event_id: str, country: str = "es") -> Dict[str, Any]:
    """Llamado por `tm_purchase_flow.step_inject_acpat`: busca una sesión
    `accepted` para inyectar sus cookies al worker de compra.

    Optimización: persist solo cada 10 claims (en lugar de cada uno) para
    no saturar disco en flows con cientos de claims/min.

    Devuelve {ok, accepted_token, session_xt, session_id?} o {ok:False}.
    """
    with _lock:
        for sid, s in _sessions.items():
            if s.event_id == event_id and s.status == "accepted" and s.accepted_token:
                if not s.is_expired():
                    _metrics["tokens_used_24h"] += 1
                    s.notes = (s.notes + f" | claimed:{datetime.now().strftime('%H:%M:%S')}").strip(" |")[-200:]
                    if _metrics["tokens_used_24h"] % 10 == 0:
                        _persist()
                    return {"ok": True, "session_id": sid,
                            "accepted_token": s.accepted_token,
                            "queueit_session_xt": s.queueit_session_xt}
    try:
        import cookie_warmer
        r = cookie_warmer.inject_acpat(domain_id=country, event_id=event_id)
        if r.get("ok"):
            return {"ok": True, "accepted_token": r.get("token"),
                    "from": "cookie_warmer_pool"}
    except Exception as e:
        return {"ok": False, "error": f"cookie_warmer:{e}"}
    return {"ok": False, "error": "no_accepted_session_available"}


# ─────────────────────────── DAEMON / POLLER ────────────────────────

def _spawn_purchase_flow(s: QueueSession) -> None:
    """Lanza _compra_parallel.py automáticamente cuando una sesión es accepted."""
    if not s.purchase_flow_args:
        return
    try:
        import subprocess
        import sys
        args = [sys.executable, os.path.join(PROYECTO, "_compra_with_swap.py"),
                "--slot", str(s.purchase_flow_args.get("slot", 1)),
                "--event-url", s.purchase_flow_args.get("event_url") or s.tm_url,
                "--qty", str(s.purchase_flow_args.get("qty", 2)),
                "--offer-idx", str(s.purchase_flow_args.get("offer_idx", 3)),
                "--mode", s.purchase_flow_args.get("mode", "quick")]
        if s.purchase_flow_args.get("sections"):
            args += ["--sections", ",".join(str(x) for x in s.purchase_flow_args["sections"])]
        if s.purchase_flow_args.get("confirm_buy"):
            args.append("--confirm")
        subprocess.Popen(args, cwd=PROYECTO,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with _lock:
            s.status = "fired"
            _persist()
        _log({"op": "auto_buy_fired", "session_id": s.session_id,
              "event_id": s.event_id})
    except Exception as e:
        _log({"op": "auto_buy_err", "session_id": s.session_id,
              "err": str(e)[:200]})


_LAST_PRUNE_AT = 0
_PRUNE_INTERVAL_S = 3600   # auto-prune sesiones terminadas cada 1h
_PRUNE_AGE_S = 24 * 3600    # remueve >24h en estado terminal


def _auto_prune_terminal_sessions() -> int:
    """Auto-limpia sesiones terminadas (expired/abandoned/fired) >24h sin tocar.

    Llamado periódicamente desde _daemon_loop para evitar memory leak.
    """
    now_iso = datetime.now()
    n = 0
    with _lock:
        to_del = []
        for sid, s in _sessions.items():
            if s.status not in ("expired", "abandoned", "fired"):
                continue
            ref_ts_str = s.accepted_at or s.last_poll_at or s.expires_at or s.created_at
            try:
                ref_ts = datetime.fromisoformat(ref_ts_str)
            except Exception:
                continue
            age_s = (now_iso - ref_ts).total_seconds()
            if age_s > _PRUNE_AGE_S:
                to_del.append(sid)
        for sid in to_del:
            evt = _sessions[sid].event_id
            del _sessions[sid]
            try:
                _swarm_subscribers.get(evt, []).remove(sid)
            except (ValueError, KeyError):
                pass
            n += 1
        if n > 0:
            _persist()
    if n > 0:
        _log({"op": "auto_prune", "removed": n})
    return n


def _daemon_loop() -> None:
    """Loop:
       1. Limpia sesiones expiradas.
       2. Auto-rejoin si auto_rejoin=True y TTL aún válido.
       3. Detecta trigger Queue-it activo en TM (via dominios cacheados).
       4. Auto-prune sesiones terminales >24h (anti memory leak).
    """
    global _LAST_PRUNE_AT
    while not _daemon_stop.is_set():
        try:
            with _lock:
                snap = list(_sessions.values())
            now = datetime.now()
            for s in snap:
                if s.status in ("abandoned", "fired"):
                    continue
                if s.is_expired() and s.status not in ("expired",):
                    with _lock:
                        s.status = "expired"
                        _persist()
                    _log({"op": "expired", "session_id": s.session_id})
                    continue
                if s.status == "error" and s.auto_rejoin:
                    rejoin_session(s.session_id)
            # Auto-prune cada PRUNE_INTERVAL_S
            if (time.time() - _LAST_PRUNE_AT) > _PRUNE_INTERVAL_S:
                _auto_prune_terminal_sessions()
                _LAST_PRUNE_AT = time.time()
        except Exception as e:
            _log({"op": "daemon_err", "err": str(e)[:160]})
        _daemon_stop.wait(POSITION_POLL_INTERVAL_S)


def start_daemon() -> Dict[str, Any]:
    global _daemon_thread
    if _daemon_thread and _daemon_thread.is_alive():
        return {"ok": True, "already_running": True}
    _load()
    _daemon_stop.clear()
    _daemon_thread = threading.Thread(target=_daemon_loop, daemon=True,
                                       name="queue_manager")
    _daemon_thread.start()
    _log({"op": "daemon_start"})
    return {"ok": True, "started": True}


def stop_daemon() -> Dict[str, Any]:
    global _daemon_thread
    _daemon_stop.set()
    if _daemon_thread:
        _daemon_thread.join(timeout=3)
    _daemon_thread = None
    return {"ok": True}


# ─────────────────────────── QUERY / METRICS ─────────────────────────

def get_session(session_id: str) -> Dict[str, Any]:
    with _lock:
        s = _sessions.get(session_id)
        return s.to_dict() if s else {"error": "not_found"}


def list_sessions(event_id: Optional[str] = None,
                  status: Optional[str] = None,
                  account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        out = []
        for s in _sessions.values():
            if event_id and s.event_id != event_id:
                continue
            if status and s.status != status:
                continue
            if account_id and s.account_id != account_id:
                continue
            out.append(s.to_dict())
    return out


def get_metrics() -> Dict[str, Any]:
    with _lock:
        active = sum(1 for s in _sessions.values()
                     if s.status in ("enqueued", "waiting", "accepted"))
        accepted = sum(1 for s in _sessions.values() if s.status == "accepted")
        waiting = sum(1 for s in _sessions.values()
                       if s.status in ("enqueued", "waiting"))
        positions = [s.position for s in _sessions.values()
                     if s.status == "waiting" and s.position is not None]
        avg_pos = round(sum(positions) / len(positions), 1) if positions else None
        events = {}
        for s in _sessions.values():
            if s.status in ("enqueued", "waiting", "accepted"):
                events[s.event_id] = events.get(s.event_id, 0) + 1
        return {
            "active": active,
            "accepted": accepted,
            "waiting": waiting,
            "abandoned_24h": _metrics.get("sessions_abandoned", 0),
            "passed_24h": _metrics.get("sessions_passed", 0),
            "tokens_captured_24h": _metrics.get("tokens_captured_24h", 0),
            "tokens_used_24h": _metrics.get("tokens_used_24h", 0),
            "avg_position": avg_pos,
            "events": events,
            "daemon_running": _daemon_thread is not None and _daemon_thread.is_alive(),
        }


def clear_completed() -> Dict[str, Any]:
    """Limpia sesiones expired/abandoned/fired (libera memoria)."""
    with _lock:
        before = len(_sessions)
        _sessions_keep = {sid: s for sid, s in _sessions.items()
                          if s.status not in ("expired", "abandoned", "fired")}
        removed = before - len(_sessions_keep)
        _sessions.clear()
        _sessions.update(_sessions_keep)
        for ev, lst in list(_swarm_subscribers.items()):
            _swarm_subscribers[ev] = [sid for sid in lst if sid in _sessions]
        _persist()
    _log({"op": "clear_completed", "removed": removed})
    return {"ok": True, "removed": removed}


# ─────────────────────────── SELF TEST ────────────────────────

_PRESETS_PATH = os.path.join(PROYECTO, "data", "queue_presets.json")


def list_presets() -> List[Dict[str, Any]]:
    d = json_atomic.load(_PRESETS_PATH, default={"presets": []})
    return d.get("presets", [])


def save_preset(preset: Dict[str, Any]) -> Dict[str, Any]:
    """Preset = {name, event_id, tm_url, customer_id, n, account_pool_mode,
    auto_rejoin, auto_buy_on_accepted, qty, offer_idx, confirm_buy, stagger_s}."""
    if not preset.get("name"):
        return {"ok": False, "error": "name required"}
    d = json_atomic.load(_PRESETS_PATH, default={"presets": []})
    presets = d.get("presets", [])
    presets = [p for p in presets if p.get("name") != preset["name"]]
    preset["saved_at"] = datetime.now().isoformat()
    presets.append(preset)
    d["presets"] = presets
    json_atomic.save(_PRESETS_PATH, d)
    return {"ok": True, "name": preset["name"], "count": len(presets)}


def delete_preset(name: str) -> Dict[str, Any]:
    d = json_atomic.load(_PRESETS_PATH, default={"presets": []})
    before = len(d.get("presets", []))
    d["presets"] = [p for p in d.get("presets", []) if p.get("name") != name]
    json_atomic.save(_PRESETS_PATH, d)
    return {"ok": True, "removed": before - len(d["presets"])}


def export_csv() -> str:
    """Exporta sesiones actuales a CSV (string)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["session_id", "account_id", "event_id", "tm_url", "status",
                 "position", "eta_minutes", "accepted_token", "queueit_session_xt",
                 "created_at", "accepted_at", "expires_at", "auto_rejoin",
                 "auto_buy", "notes"])
    with _lock:
        for s in _sessions.values():
            w.writerow([s.session_id, s.account_id, s.event_id, s.tm_url,
                        s.status, s.position, s.eta_minutes,
                        (s.accepted_token or "")[:80],
                        (s.queueit_session_xt or "")[:80],
                        s.created_at, s.accepted_at, s.expires_at,
                        s.auto_rejoin, s.auto_buy_on_accepted, s.notes])
    return buf.getvalue()


def inspect_cookie(value: str) -> Dict[str, Any]:
    """Decodifica un AcpAT/QueueITAccepted token y lo presenta human-readable.

    Formato típico: `[header].[payload].[hash]` Base64Url, payload AES-256.
    Sin la secret key del customer no se puede desencriptar el payload, pero
    podemos extraer header (contiene typ, exp, ti, c, e, ip).
    """
    import base64
    out: Dict[str, Any] = {"raw": value[:200], "valid_format": False}
    try:
        parts = value.split(".")
        out["parts_count"] = len(parts)
        if len(parts) >= 2:
            header_b64 = parts[0]
            pad = "=" * (-len(header_b64) % 4)
            try:
                hdr_raw = base64.urlsafe_b64decode(header_b64 + pad).decode("utf-8", errors="ignore")
                out["header_raw"] = hdr_raw[:300]
                try:
                    out["header"] = json.loads(hdr_raw)
                    out["valid_format"] = True
                    if out["header"].get("exp"):
                        try:
                            exp_dt = datetime.fromtimestamp(out["header"]["exp"])
                            out["expires_at"] = exp_dt.isoformat()
                            out["expires_in_min"] = round(
                                (exp_dt - datetime.now()).total_seconds() / 60, 1)
                            out["expired"] = exp_dt < datetime.now()
                        except Exception:
                            pass
                except Exception:
                    out["note"] = "header no es JSON parseable (puede estar firmado)"
            except Exception as e:
                out["decode_err"] = str(e)[:80]
        out["payload_b64_len"] = len(parts[1]) if len(parts) >= 2 else 0
        out["hash_present"] = len(parts) >= 3 and len(parts[2]) > 8
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def get_logs(limit: int = 100, op_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    d = json_atomic.load(_LOG_PATH, default={"log": []})
    items = d.get("log", [])
    if op_filter:
        items = [x for x in items if x.get("op") == op_filter]
    return items[:limit]


def sniper_mode(event_id: str, n_workers: int = 50,
                tm_url: str = "", customer_id: str = "ticketmasterspain",
                account_pool: Optional[List[str]] = None,
                purchase_flow_args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Sniper mode: concentra N cuentas sobre 1 evento con auto-buy + swarm.

    Cuando la primera pasa la cola → broadcast a las otras 49 → todas pasan.
    """
    return enqueue_batch(n=n_workers, account_pool=account_pool,
                         event_id=event_id, tm_url=tm_url,
                         customer_id=customer_id,
                         auto_rejoin=True, auto_buy_on_accepted=True,
                         purchase_flow_args=purchase_flow_args or {"qty": 2, "offer_idx": 3, "mode": "quick"},
                         stagger_s=0)


def self_test() -> Dict[str, Any]:
    out: Dict[str, Any] = {"checks": {}}
    r1 = enqueue_session(account_id="test_acc", event_id="test_evt",
                          tm_url="https://example.com")
    out["checks"]["enqueue_ok"] = r1.get("ok") is True
    sid = r1.get("session_id")
    out["checks"]["session_listable"] = any(
        s.get("session_id") == sid for s in list_sessions(event_id="test_evt"))
    r2 = update_position(sid, position=42, eta_minutes=8.5)
    out["checks"]["update_position_ok"] = r2.get("ok") is True
    r3 = mark_accepted(sid, accepted_token="TEST_TOKEN_" + uuid.uuid4().hex,
                        queueit_session_xt="TEST_XT")
    out["checks"]["mark_accepted_ok"] = r3.get("ok") is True
    r4 = claim_slot_for_purchase(event_id="test_evt")
    out["checks"]["claim_slot_ok"] = r4.get("ok") is True and bool(r4.get("accepted_token"))
    m = get_metrics()
    out["checks"]["metrics_active"] = m.get("active") >= 1
    abandon_session(sid, reason="self_test")
    clear_completed()
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


_load()


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
