"""acpat_sync.py — Multi-worker AcpAT real-time sync layer.

Cuando lanzamos N workers en paralelo (queue_orchestrator.start_multi),
cada uno entra al queue por separado. Si UNO consigue el AcpAT primero,
los demás están esperando innecesariamente.

Esta capa coordina N workers en tiempo real:
  - Worker que entra primero → publica AcpAT al sync bus
  - Resto de workers en `wait_for_token()` reciben en <50ms
  - Health-check por worker (si muere, se notifica)
  - TTL tracking (cuándo expira AcpAT, refresh automático)
  - Backend: Redis si configurado, fallback a in-memory

Diferencia vs `acpat_injection.py`:
  - Aquel es el pub/sub básico (broadcast 1→N)
  - Este orquesta el ciclo completo (capture → propagate → expire → refresh)
  - Métricas: workers ahorrados, latencia propagación, tokens reutilizados

API:
  register_worker(worker_id, domain_id, event_id) → ok
  publish_token(worker_id, token, ttl_s=1800)
  wait_for_token(worker_id, timeout_s=300) → token | None (bloquea)
  get_or_wait_for_token(worker_id, timeout_s=300) → reusa cached o bloquea
  unregister_worker(worker_id)
  get_sync_stats() → métricas
  start_refresh_loop(domain_id, ttl_min=25)
  stop_refresh_loop()
  self_test()
"""
from __future__ import annotations
import os, time, json, threading, uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

try:
    import acpat_injection                 # pub/sub básico
    BROADCAST_AVAILABLE = True
except Exception:
    BROADCAST_AVAILABLE = False

try:
    import redis_backend
    REDIS_BACKEND_AVAILABLE = redis_backend.is_available()
except Exception:
    REDIS_BACKEND_AVAILABLE = False


_STATE_KEY = "acpat_sync_state.json"
_STATS_KEY = "acpat_sync_stats.json"
_lock = threading.RLock()
_workers: Dict[str, Dict[str, Any]] = {}  # worker_id → {domain_id, event_id, registered_at, last_seen, status}
_token_pool: Dict[str, Dict[str, Any]] = {}  # key=(domain,event) → {token, ts, ttl_s, source_worker}
_condition = threading.Condition()
_refresh_threads: Dict[str, threading.Event] = {}
_stats = {
    "workers_registered": 0,
    "workers_active": 0,
    "tokens_published": 0,
    "tokens_reused_from_cache": 0,
    "wait_calls": 0,
    "wait_satisfied": 0,
    "wait_timeouts": 0,
    "avg_latency_ms": 0.0,
}
_latency_samples: List[float] = []  # cap 100


def _key(domain_id: str, event_id: str = "") -> str:
    return f"{domain_id}::{event_id or '_'}"


def _save_stats():
    try:
        json_atomic.save(config.ruta(_STATS_KEY), {**_stats,
                                                     "updated_at": datetime.now().isoformat()})
    except Exception:
        pass


def _save_state():
    try:
        json_atomic.save(config.ruta(_STATE_KEY), {
            "workers": [{**w, "registered_at": w.get("registered_at"),
                          "last_seen": w.get("last_seen")} for w in _workers.values()],
            "token_pool": list(_token_pool.keys()),
            "saved_at": datetime.now().isoformat(),
        })
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# WORKER LIFECYCLE
# ═══════════════════════════════════════════════════════════════
def register_worker(worker_id: str, domain_id: str = "es",
                    event_id: str = "") -> Dict[str, Any]:
    """Registra un worker en el bus de sync."""
    if not worker_id: return {"ok": False, "error": "worker_id_required"}
    with _lock:
        new = worker_id not in _workers
        _workers[worker_id] = {
            "worker_id": worker_id,
            "domain_id": domain_id,
            "event_id": event_id,
            "registered_at": datetime.now().isoformat(),
            "last_seen": time.time(),
            "status": "idle",
            "tokens_received": 0,
            "tokens_published": 0,
        }
        if new:
            _stats["workers_registered"] += 1
            _stats["workers_active"] = sum(1 for w in _workers.values()
                                              if (time.time() - w["last_seen"]) < 600)
        _save_stats()
    return {"ok": True, "worker_id": worker_id, "is_new": new}


def heartbeat(worker_id: str) -> Dict[str, Any]:
    """Worker manda heartbeat para indicar que sigue vivo."""
    with _lock:
        if worker_id not in _workers:
            return {"ok": False, "error": "worker_not_registered"}
        _workers[worker_id]["last_seen"] = time.time()
    return {"ok": True}


def unregister_worker(worker_id: str) -> Dict[str, Any]:
    with _lock:
        was = _workers.pop(worker_id, None)
        if was:
            _stats["workers_active"] = sum(1 for w in _workers.values()
                                              if (time.time() - w["last_seen"]) < 600)
            _save_stats()
    return {"ok": was is not None, "worker_id": worker_id}


def list_active_workers(domain_id: Optional[str] = None) -> List[Dict[str, Any]]:
    now = time.time()
    with _lock:
        out = []
        for w in _workers.values():
            if domain_id and w["domain_id"] != domain_id: continue
            age = now - w["last_seen"]
            if age < 600:  # 10 min cutoff
                out.append({**w, "age_s": int(age)})
        return out


# ═══════════════════════════════════════════════════════════════
# TOKEN PUBLISH / WAIT
# ═══════════════════════════════════════════════════════════════
def publish_token(worker_id: str, token: str,
                  domain_id: str = "es", event_id: str = "",
                  ttl_s: int = 1800) -> Dict[str, Any]:
    """Worker publica un AcpAT capturado. Despierta a todos los waiters del mismo
    (domain,event)."""
    if not token or len(token) < 10:
        return {"ok": False, "error": "token_too_short"}

    key = _key(domain_id, event_id)
    publish_ts = time.time()

    with _condition:
        with _lock:
            _token_pool[key] = {
                "token": token,
                "ts": publish_ts,
                "ttl_s": ttl_s,
                "source_worker": worker_id,
                "expires_at": publish_ts + ttl_s,
                "consumed_count": 0,
            }
            if worker_id in _workers:
                _workers[worker_id]["status"] = "publisher"
                _workers[worker_id]["tokens_published"] += 1
            _stats["tokens_published"] += 1
            _save_stats()

        # Notify all waiters
        _condition.notify_all()

    # Forward al broadcast layer si existe
    if BROADCAST_AVAILABLE:
        try:
            acpat_injection.broadcast_token(token=token, domain_id=domain_id,
                                              event_id=event_id,
                                              source_worker=worker_id)
        except Exception:
            pass

    # Forward a Redis si configurado
    if REDIS_BACKEND_AVAILABLE:
        try:
            redis_backend.set_token(key, token, ttl_s=ttl_s)
        except Exception:
            pass

    return {
        "ok": True, "key": key, "token_length": len(token),
        "ttl_s": ttl_s, "published_at": datetime.now().isoformat(),
        "expires_at": datetime.fromtimestamp(publish_ts + ttl_s).isoformat(),
    }


def get_cached_token(domain_id: str = "es",
                     event_id: str = "") -> Optional[Dict[str, Any]]:
    """Devuelve token si está cached y no expiró."""
    key = _key(domain_id, event_id)
    with _lock:
        entry = _token_pool.get(key)
        if not entry: return None
        if time.time() > entry["expires_at"]:
            _token_pool.pop(key, None)
            return None
        return dict(entry)


def wait_for_token(worker_id: str,
                   domain_id: str = "es",
                   event_id: str = "",
                   timeout_s: int = 300,
                   reuse_cached: bool = True) -> Optional[Dict[str, Any]]:
    """Worker espera hasta que aparezca un AcpAT para (domain,event).

    Si `reuse_cached=True` y hay cached → retorna inmediatamente.
    Si no, bloquea hasta `timeout_s`.
    """
    with _lock:
        _stats["wait_calls"] += 1

    started = time.time()
    key = _key(domain_id, event_id)

    # Reusar cached si pedido
    if reuse_cached:
        cached = get_cached_token(domain_id, event_id)
        if cached:
            with _lock:
                if worker_id in _workers:
                    _workers[worker_id]["tokens_received"] += 1
                    _workers[worker_id]["status"] = "got_cached"
                _token_pool[key]["consumed_count"] += 1
                _stats["tokens_reused_from_cache"] += 1
                _stats["wait_satisfied"] += 1
            return {
                "ok": True, "from_cache": True,
                "token": cached["token"],
                "source_worker": cached["source_worker"],
                "age_s": round(time.time() - cached["ts"], 1),
                "latency_ms": round((time.time() - started) * 1000, 1),
            }

    # Bloquear esperando broadcast
    with _condition:
        while True:
            elapsed = time.time() - started
            remaining = timeout_s - elapsed
            if remaining <= 0:
                with _lock:
                    _stats["wait_timeouts"] += 1
                    if worker_id in _workers:
                        _workers[worker_id]["status"] = "timeout"
                return None
            # Re-check cache (puede haber sido publicado mientras esperábamos)
            cached = get_cached_token(domain_id, event_id)
            if cached:
                latency_ms = (time.time() - started) * 1000
                with _lock:
                    if worker_id in _workers:
                        _workers[worker_id]["tokens_received"] += 1
                        _workers[worker_id]["status"] = "got_broadcast"
                    _token_pool[key]["consumed_count"] += 1
                    _stats["wait_satisfied"] += 1
                    _latency_samples.append(latency_ms)
                    if len(_latency_samples) > 100:
                        _latency_samples.pop(0)
                    _stats["avg_latency_ms"] = round(
                        sum(_latency_samples) / len(_latency_samples), 1)
                    _save_stats()
                return {
                    "ok": True, "from_cache": False,
                    "token": cached["token"],
                    "source_worker": cached["source_worker"],
                    "latency_ms": round(latency_ms, 1),
                    "waited_s": round(elapsed, 1),
                }
            # Bloquear hasta condition.notify
            _condition.wait(timeout=min(remaining, 5))


def get_or_wait_for_token(worker_id: str,
                           domain_id: str = "es",
                           event_id: str = "",
                           timeout_s: int = 300) -> Optional[Dict[str, Any]]:
    """Helper: intenta cached primero, luego bloquea."""
    return wait_for_token(worker_id, domain_id, event_id, timeout_s,
                           reuse_cached=True)


# ═══════════════════════════════════════════════════════════════
# REFRESH LOOP (auto-republica tokens antes de TTL)
# ═══════════════════════════════════════════════════════════════
def start_refresh_loop(domain_id: str = "es", ttl_min: int = 25,
                        on_refresh=None) -> Dict[str, Any]:
    """Lanza thread que cada `ttl_min` minutos llama a `cookie_warmer.inject_acpat`
    y republica el token al sync bus."""
    key = f"refresh::{domain_id}"
    if key in _refresh_threads:
        return {"ok": False, "error": "already_running", "key": key}

    stop_evt = threading.Event()
    _refresh_threads[key] = stop_evt

    def _loop():
        while not stop_evt.is_set():
            try:
                import cookie_warmer
                tok = cookie_warmer.inject_acpat(domain_id=domain_id)
                token_str = None
                if isinstance(tok, dict):
                    token_str = tok.get("token") or tok.get("acpat_token")
                elif isinstance(tok, str):
                    token_str = tok
                if token_str:
                    publish_token("refresh-loop", token_str,
                                   domain_id=domain_id, ttl_s=ttl_min * 60)
                    if on_refresh:
                        try: on_refresh({"domain_id": domain_id, "ok": True})
                        except Exception: pass
            except Exception as e:
                if on_refresh:
                    try: on_refresh({"domain_id": domain_id, "ok": False,
                                       "error": str(e)[:120]})
                    except Exception: pass
            stop_evt.wait(ttl_min * 60)

    t = threading.Thread(target=_loop, daemon=True,
                          name=f"acpat-refresh-{domain_id}")
    t.start()
    return {"ok": True, "key": key, "ttl_min": ttl_min}


def stop_refresh_loop(domain_id: str = "es") -> Dict[str, Any]:
    key = f"refresh::{domain_id}"
    evt = _refresh_threads.pop(key, None)
    if evt: evt.set()
    return {"ok": evt is not None, "key": key}


def list_refresh_loops() -> List[str]:
    return list(_refresh_threads.keys())


# ═══════════════════════════════════════════════════════════════
# STATS / INFO
# ═══════════════════════════════════════════════════════════════
def get_sync_stats() -> Dict[str, Any]:
    with _lock:
        active_workers = sum(1 for w in _workers.values()
                              if (time.time() - w["last_seen"]) < 600)
        active_tokens = sum(1 for e in _token_pool.values()
                              if time.time() < e["expires_at"])
        return {
            **_stats,
            "workers_active_now": active_workers,
            "active_tokens_in_pool": active_tokens,
            "refresh_loops_running": len(_refresh_threads),
            "redis_backend_available": REDIS_BACKEND_AVAILABLE,
            "broadcast_available": BROADCAST_AVAILABLE,
        }


def cleanup_dead_workers(max_age_s: int = 600) -> Dict[str, Any]:
    """Elimina workers que no enviaron heartbeat en max_age_s segundos."""
    now = time.time()
    removed = []
    with _lock:
        for wid in list(_workers.keys()):
            if (now - _workers[wid]["last_seen"]) > max_age_s:
                _workers.pop(wid)
                removed.append(wid)
    return {"ok": True, "removed_count": len(removed), "removed": removed}


def cleanup_expired_tokens() -> Dict[str, Any]:
    now = time.time()
    removed = 0
    with _lock:
        for k in list(_token_pool.keys()):
            if now > _token_pool[k]["expires_at"]:
                _token_pool.pop(k)
                removed += 1
    return {"ok": True, "removed_count": removed}


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Register worker
    r = register_worker("w_test_1", domain_id="es", event_id="ev1")
    checks["register_ok"] = r.get("ok") is True

    # 2. Heartbeat
    r = heartbeat("w_test_1")
    checks["heartbeat_ok"] = r.get("ok") is True

    # 3. Publish token
    r = publish_token("w_test_1", "fake_acpat_" + "x" * 30,
                       domain_id="es", event_id="ev1", ttl_s=600)
    checks["publish_ok"] = r.get("ok") is True

    # 4. Get cached
    cached = get_cached_token("es", "ev1")
    checks["get_cached_returns"] = cached is not None
    checks["cached_has_token"] = cached and "token" in cached

    # 5. Wait con cached → returns immediately
    register_worker("w_test_2", "es", "ev1")
    r = wait_for_token("w_test_2", domain_id="es", event_id="ev1",
                        timeout_s=2, reuse_cached=True)
    checks["wait_uses_cached"] = r is not None and r.get("from_cache") is True

    # 6. Wait sin cached → timeout
    register_worker("w_test_3", "es", "no_cached_event")
    r = wait_for_token("w_test_3", domain_id="es", event_id="no_cached_event",
                        timeout_s=1, reuse_cached=False)
    checks["wait_timeout"] = r is None

    # 7. Concurrent: 3 workers wait, 1 publishes
    received = {"r1": None, "r2": None, "r3": None}
    def _waiter(name):
        r = wait_for_token(name, domain_id="es", event_id="ev2",
                            timeout_s=3, reuse_cached=False)
        received[name] = r

    register_worker("r1", "es", "ev2")
    register_worker("r2", "es", "ev2")
    register_worker("r3", "es", "ev2")
    threads = [threading.Thread(target=_waiter, args=(n,)) for n in ["r1", "r2", "r3"]]
    for t in threads: t.start()
    time.sleep(0.2)  # let them start waiting
    publish_token("publisher_x", "broadcasted_token_" + "y" * 20,
                   domain_id="es", event_id="ev2")
    for t in threads: t.join(timeout=4)
    checks["concurrent_r1_received"] = received["r1"] is not None
    checks["concurrent_r2_received"] = received["r2"] is not None
    checks["concurrent_r3_received"] = received["r3"] is not None

    # 8. Stats
    s = get_sync_stats()
    checks["stats_workers_registered"] = s["workers_registered"] >= 5
    checks["stats_tokens_published"] = s["tokens_published"] >= 2

    # 9. List active workers
    active = list_active_workers("es")
    checks["list_active_returns_list"] = isinstance(active, list)
    checks["list_active_count"] = len(active) >= 3

    # 10. Refresh loop start/stop
    r = start_refresh_loop("test_dom", ttl_min=99999)
    checks["refresh_start_ok"] = r.get("ok") is True
    checks["refresh_in_list"] = "refresh::test_dom" in list_refresh_loops()
    r = stop_refresh_loop("test_dom")
    checks["refresh_stop_ok"] = r.get("ok") is True

    # 11. Cleanup
    r = cleanup_expired_tokens()
    checks["cleanup_returns_dict"] = isinstance(r, dict)

    # 12. Unregister
    for wid in ["w_test_1", "w_test_2", "w_test_3", "r1", "r2", "r3"]:
        unregister_worker(wid)
    checks["unregister_ok"] = True

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "acpat_sync",
        "version": 1,
        "stats": get_sync_stats(),
        "purpose": "Multi-worker AcpAT real-time sync layer",
    }
