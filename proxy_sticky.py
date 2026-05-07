"""proxy_sticky.py — Sesiones sticky de proxy (1 IP por worker, TTL 30 min).

Mantener la misma IP durante toda la cola virtual de Queue-it es CRÍTICO:
TM invalida el AcpAT/cookie si cambia la IP origen. Este módulo:

- Asigna un session-id único por (worker_id, domain) con TTL configurable
- Genera el username con session sticky (formato proveedor-agnóstico)
  ej: "user-session-{sid}-sessTime-{ttl_minutes}"
- Renueva session-id cuando expira el TTL
- Loggea cambios de IP (informados desde proxy_health.py o el bot)
- Self-test completo

API:
  get_session(worker_id, domain, ttl_s=1800, force_renew=False) → dict
  format_username(base_user, session_id, ttl_minutes=30, scheme="bright_data")
  report_ip(worker_id, ip) → dict (registra y detecta cambio)
  release(worker_id, domain=None|all=False)
  cleanup_expired() → list[released_keys]
  get_state(worker_id=None) → dict|list
  start_cleaner(interval_s=60) / stop_cleaner()
  self_test()

Soporta esquemas de username sticky de:
  - Bright Data: user-session-{sid}-sessTime-{min}
  - Smartproxy: user-session-{sid}
  - Oxylabs: customer-{user}-sessid-{sid}-sesstime-{min}
  - SOAX: package-{pkg}-sessionid-{sid}-sessionlength-{min}
"""
from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

import config
import json_atomic

_STATE_KEY = "proxy_sticky_state.json"
_LOG_KEY = "proxy_sticky_iplog.json"
_lock = threading.RLock()

DEFAULT_TTL_S = 1800  # 30 min — recomendado para Queue-it AcpAT
_cleaner_state = {"running": False, "thread": None, "stop_event": None}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _load_state() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_STATE_KEY), default={"sessions": {}})


def _save_state(d: Dict[str, Any]):
    json_atomic.save(config.ruta(_STATE_KEY), d)


def _load_log() -> List[Dict[str, Any]]:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"entries": []})
    return d.get("entries", [])


def _append_log(entry: Dict[str, Any]):
    cur = json_atomic.load(config.ruta(_LOG_KEY), default={"entries": []})
    cur.setdefault("entries", []).insert(0, entry)
    cur["entries"] = cur["entries"][:1000]
    json_atomic.save(config.ruta(_LOG_KEY), cur)


def _new_session_id() -> str:
    """16 hex chars — único, alphanum, OK para todos los proveedores."""
    return secrets.token_hex(8)


def _key(worker_id: str, domain: str) -> str:
    return f"{worker_id}|{domain}"


def get_session(worker_id: str,
                domain: str = "ticketmaster.es",
                ttl_s: int = DEFAULT_TTL_S,
                force_renew: bool = False) -> Dict[str, Any]:
    """Obtiene (o crea) un session-id sticky para (worker, dominio).

    Si el TTL no expiró y no se fuerza renew, devuelve la sesión actual.
    """
    if not worker_id:
        return {"ok": False, "error": "worker_id_required"}
    k = _key(worker_id, domain)
    now = time.time()
    with _lock:
        st = _load_state()
        sessions = st.setdefault("sessions", {})
        existing = sessions.get(k)
        if existing and not force_renew:
            ends_at = existing.get("ends_at_ts", 0)
            if now < ends_at:
                # Aún válida
                existing["accessed_at"] = _now_iso()
                existing["accessed_at_ts"] = now
                existing["seconds_remaining"] = int(ends_at - now)
                _save_state(st)
                return {"ok": True, "renewed": False, "session": existing}
        # Crear nueva
        sid = _new_session_id()
        ttl_minutes = max(1, ttl_s // 60)
        new_session = {
            "session_id": sid,
            "worker_id": worker_id,
            "domain": domain,
            "ttl_s": ttl_s,
            "ttl_minutes": ttl_minutes,
            "created_at": _now_iso(),
            "created_at_ts": now,
            "ends_at_ts": now + ttl_s,
            "ends_at": datetime.fromtimestamp(now + ttl_s).isoformat(),
            "accessed_at": _now_iso(),
            "accessed_at_ts": now,
            "seconds_remaining": ttl_s,
            "ip_history": [],
            "renewals_count": (existing.get("renewals_count", 0) + 1) if existing else 0,
        }
        sessions[k] = new_session
        _save_state(st)
    if force_renew or existing:
        _append_log({"ts": _now_iso(), "event": "renewed",
                      "worker_id": worker_id, "domain": domain,
                      "session_id": sid})
    return {"ok": True, "renewed": True, "session": new_session}


def format_username(base_user: str,
                     session_id: str,
                     ttl_minutes: int = 30,
                     scheme: str = "bright_data") -> str:
    """Devuelve el username sticky formateado para el proveedor."""
    scheme = (scheme or "bright_data").lower()
    if scheme in ("bright_data", "brightdata"):
        return f"{base_user}-session-{session_id}-sessTime-{ttl_minutes}"
    if scheme == "smartproxy":
        return f"{base_user}-session-{session_id}"
    if scheme == "oxylabs":
        return f"customer-{base_user}-sessid-{session_id}-sesstime-{ttl_minutes}"
    if scheme == "soax":
        return f"package-{base_user}-sessionid-{session_id}-sessionlength-{ttl_minutes}"
    if scheme in ("iproyal", "rayobyte"):
        return f"{base_user}-session-{session_id}-lifetime-{ttl_minutes}m"
    # Fallback: query string
    return f"{base_user}?session={session_id}&ttl={ttl_minutes}"


def report_ip(worker_id: str,
              ip: str,
              domain: str = "ticketmaster.es") -> Dict[str, Any]:
    """Registra IP vista por el worker. Detecta cambios y loggea.

    Llamar tras cada request para verificar que la sticky está funcionando.
    """
    if not worker_id or not ip:
        return {"ok": False, "error": "worker_and_ip_required"}
    k = _key(worker_id, domain)
    now_iso = _now_iso()
    changed = False
    prev_ip = None
    with _lock:
        st = _load_state()
        sessions = st.setdefault("sessions", {})
        s = sessions.get(k)
        if not s:
            return {"ok": False, "error": "no_session_for_worker_domain",
                    "hint": "Llama a get_session() primero."}
        ip_hist = s.setdefault("ip_history", [])
        if ip_hist:
            prev_ip = ip_hist[-1].get("ip")
            if prev_ip != ip:
                changed = True
        ip_hist.append({"ip": ip, "ts": now_iso})
        s["ip_history"] = ip_hist[-100:]
        s["last_ip"] = ip
        s["last_ip_ts"] = time.time()
        _save_state(st)
    if changed:
        _append_log({"ts": now_iso, "event": "ip_changed",
                      "worker_id": worker_id, "domain": domain,
                      "from_ip": prev_ip, "to_ip": ip,
                      "session_id": s.get("session_id"),
                      "warning": "Cambio de IP invalida AcpAT — Queue-it dropoff probable"})
    return {"ok": True, "changed": changed, "previous_ip": prev_ip,
             "current_ip": ip, "session_id": s.get("session_id")}


def release(worker_id: Optional[str] = None,
            domain: Optional[str] = None,
            all_workers: bool = False) -> Dict[str, Any]:
    with _lock:
        st = _load_state()
        sessions = st.setdefault("sessions", {})
        if all_workers:
            n = len(sessions)
            sessions.clear()
            _save_state(st)
            return {"ok": True, "removed": n}
        removed = 0
        for k in list(sessions.keys()):
            wid, dom = k.split("|", 1) if "|" in k else (k, "")
            if worker_id and wid != worker_id:
                continue
            if domain and dom != domain:
                continue
            sessions.pop(k, None)
            removed += 1
        _save_state(st)
        return {"ok": True, "removed": removed}


def cleanup_expired() -> Dict[str, Any]:
    now = time.time()
    removed: List[str] = []
    with _lock:
        st = _load_state()
        sessions = st.setdefault("sessions", {})
        for k, s in list(sessions.items()):
            if s.get("ends_at_ts", 0) <= now:
                removed.append(k)
                sessions.pop(k, None)
        if removed:
            _save_state(st)
    return {"ok": True, "removed": removed, "count": len(removed)}


def get_state(worker_id: Optional[str] = None) -> Any:
    with _lock:
        st = _load_state()
    sessions = st.get("sessions", {})
    now = time.time()
    out = []
    for k, s in sessions.items():
        s2 = dict(s)
        s2["seconds_remaining"] = max(0, int(s.get("ends_at_ts", 0) - now))
        s2["expired"] = s2["seconds_remaining"] <= 0
        s2["ip_count"] = len(s.get("ip_history", []))
        out.append(s2)
    if worker_id:
        out = [e for e in out if e.get("worker_id") == worker_id]
    out.sort(key=lambda e: e.get("created_at_ts", 0), reverse=True)
    return out


def get_log(limit: int = 100) -> List[Dict[str, Any]]:
    return _load_log()[:limit]


def stats() -> Dict[str, Any]:
    state = get_state()
    log = _load_log()[:500]
    ip_changes = sum(1 for e in log if e.get("event") == "ip_changed")
    renewals = sum(1 for e in log if e.get("event") == "renewed")
    active = sum(1 for s in state if not s.get("expired"))
    expired = sum(1 for s in state if s.get("expired"))
    return {
        "active_sessions": active,
        "expired_sessions": expired,
        "total_sessions": len(state),
        "log_size": len(log),
        "ip_changes_recent": ip_changes,
        "renewals_recent": renewals,
        "cleaner_running": _cleaner_state.get("running", False),
    }


# ─── Cleaner thread ─────────────────────────────────────────────
def _cleaner_loop(stop_evt: threading.Event, interval_s: int):
    while not stop_evt.is_set():
        try:
            cleanup_expired()
        except Exception:
            pass
        for _ in range(max(1, int(interval_s))):
            if stop_evt.is_set():
                break
            time.sleep(1)


def start_cleaner(interval_s: int = 60) -> Dict[str, Any]:
    if _cleaner_state.get("running"):
        return {"ok": True, "status": "already_running"}
    evt = threading.Event()
    th = threading.Thread(target=_cleaner_loop, args=(evt, interval_s),
                           daemon=True, name="proxy_sticky_cleaner")
    _cleaner_state.update({"running": True, "stop_event": evt, "thread": th,
                             "interval_s": interval_s,
                             "started_at": _now_iso()})
    th.start()
    return {"ok": True, "status": "started", "interval_s": interval_s}


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
    release(all_workers=True)
    # 1. crear sesión
    s1 = get_session("worker_test_1", "ticketmaster.es", ttl_s=60)
    out["checks"]["session_created"] = s1.get("ok", False)
    sid = s1.get("session", {}).get("session_id")
    out["checks"]["has_session_id"] = bool(sid)
    # 2. reusar sesión (no debe renovar)
    s2 = get_session("worker_test_1", "ticketmaster.es", ttl_s=60)
    out["checks"]["session_reused"] = (
        s2.get("session", {}).get("session_id") == sid and
        not s2.get("renewed"))
    # 3. force renew
    s3 = get_session("worker_test_1", "ticketmaster.es", force_renew=True)
    out["checks"]["force_renew_changes_id"] = (
        s3.get("session", {}).get("session_id") != sid)
    # 4. format username schemes
    fmt_bd = format_username("user1", "abc123", 30, "bright_data")
    out["checks"]["fmt_bright_data"] = ("session-abc123" in fmt_bd
                                          and "sessTime-30" in fmt_bd)
    fmt_sm = format_username("user1", "abc123", 30, "smartproxy")
    out["checks"]["fmt_smartproxy"] = (fmt_sm == "user1-session-abc123")
    fmt_ox = format_username("user1", "abc123", 30, "oxylabs")
    out["checks"]["fmt_oxylabs"] = ("sessid-abc123" in fmt_ox)
    # 5. report IP — primero
    r_ip1 = report_ip("worker_test_1", "1.2.3.4", "ticketmaster.es")
    out["checks"]["report_first_ip"] = (r_ip1.get("ok") and
                                          not r_ip1.get("changed"))
    # 6. report IP — misma (no cambio)
    r_ip2 = report_ip("worker_test_1", "1.2.3.4", "ticketmaster.es")
    out["checks"]["report_same_ip_no_change"] = not r_ip2.get("changed")
    # 7. report IP — distinta (cambio detectado)
    r_ip3 = report_ip("worker_test_1", "5.6.7.8", "ticketmaster.es")
    out["checks"]["report_change_detected"] = r_ip3.get("changed", False)
    # 8. estado
    state = get_state("worker_test_1")
    out["checks"]["state_returns_session"] = (len(state) >= 1)
    out["checks"]["state_has_ip_history"] = (len(state[0].get("ip_history", [])) >= 2)
    # 9. expiración manual
    release("worker_test_1", "ticketmaster.es")
    state2 = get_state("worker_test_1")
    out["checks"]["release_clears"] = (len(state2) == 0)
    # 10. cleanup_expired con TTL=0
    s4 = get_session("worker_temp", "ticketmaster.es", ttl_s=0)
    cleanup_expired()
    state3 = get_state("worker_temp")
    out["checks"]["cleanup_removes_expired"] = (len(state3) == 0)
    # 11. release all
    get_session("worker_a", "x.com")
    get_session("worker_b", "y.com")
    rel = release(all_workers=True)
    out["checks"]["release_all_works"] = (rel.get("removed", 0) >= 2)

    out["passed"] = sum(1 for v in out["checks"].values() if v)
    out["total"] = len(out["checks"])
    out["ok"] = out["passed"] == out["total"]
    return out


def get_info() -> Dict[str, Any]:
    return {
        "module": "proxy_sticky",
        "default_ttl_s": DEFAULT_TTL_S,
        "supported_schemes": ["bright_data", "smartproxy", "oxylabs",
                                "soax", "iproyal", "rayobyte"],
        "stats": stats(),
        "endpoints": [
            "POST /api/proxy-sticky/get-session",
            "POST /api/proxy-sticky/format-username",
            "POST /api/proxy-sticky/report-ip",
            "POST /api/proxy-sticky/release",
            "GET /api/proxy-sticky/state",
            "GET /api/proxy-sticky/log",
            "GET /api/proxy-sticky/stats",
            "POST /api/proxy-sticky/cleaner/<action>",
            "POST /api/proxy-sticky/self-test",
        ],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=2))
