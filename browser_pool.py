"""browser_pool.py — Pool de contextos Playwright pre-warmed.

Mejora #browser_pool:
  Arrancar 2-3 contextos Playwright antes de que hagan falta y mantenerlos
  "listos para usar" con cookies PX frescas. Así un checkout arranca en
  < 500 ms en vez de los ~8 s que tarda un cold-start de Chromium.

Arquitectura:
  - Worker en background mantiene N contextos listos (target = pool_size)
  - Cada contexto ya tiene: browser iniciado, viewport+UA, stealth JS,
    cookies PX/Imperva frescas del dominio, TLS fingerprint
  - acquire() entrega un contexto pre-calentado → checkout usa instantáneamente
  - release(success=True|False): si True → lo reingresa; si False → lo quema
  - TTL por contexto (default 15 min). Al expirar se quema y se recrea.
  - Stats: avg_acquire_ms (latencia real) + pool_size actual + lifetime

⚠️ Coste: cada contexto Chromium en memoria = ~150-200 MB. 3 contextos
   pueden consumir hasta 600 MB solo en browsers. No abusar.

API:
  configure(pool_size, ttl_min, engine, country)
  start_manager() / stop_manager()
  acquire(country=None, timeout=5) → {ok, handle_id, page, context, ...}
  release(handle_id, success=True)
  burn(handle_id, reason="")
  get_stats()
"""
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import config
import json_atomic


_CFG_KEY = "browser_pool_config.json"
_lock = threading.RLock()
_cond = threading.Condition(_lock)

# pool estructurado en memoria
_contexts: Dict[str, dict] = {}   # handle_id → {status, meta, ts, ...}
_manager = {"running": False, "thread": None, "stop_event": None}
_metrics = {
    "total_acquires": 0,
    "total_hits": 0,     # acquire con context listo (fast path)
    "total_misses": 0,   # acquire que tuvo que cold-start
    "total_burns": 0,
    "total_creates": 0,
    "acquire_times_ms": deque(maxlen=100),
    "create_times_ms": deque(maxlen=50),
}


def _cfg_path():
    return config.ruta(_CFG_KEY)


def get_config() -> dict:
    return json_atomic.load(_cfg_path(), default={
        "pool_size": 2,           # target de contextos listos
        "ttl_min": 15,
        "engine": "chromium",     # chromium | firefox | webkit | patchright
        "country": "us",
        "headless": True,
        "auto_start": False,
        "inject_px_cookies": True,
        "max_acquire_wait_s": 5,
    })


def configure(patch: dict) -> dict:
    cur = get_config()
    allowed = {"pool_size", "ttl_min", "engine", "country",
               "headless", "auto_start", "inject_px_cookies", "max_acquire_wait_s"}
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    # sanity
    cur["pool_size"] = max(1, min(5, int(cur.get("pool_size", 2))))
    cur["ttl_min"] = max(2, min(60, int(cur.get("ttl_min", 15))))
    json_atomic.save(_cfg_path(), cur)
    return cur


# ═══════════════════════════════════════════
#  LIFECYCLE
# ═══════════════════════════════════════════
def _create_context(country: str, engine: str, headless: bool) -> Optional[dict]:
    """Crea un contexto real con stealth + cookies PX + TLS.

    Devuelve {handle_id, browser, context, page, meta, created_at, country}
    o None si falla. NO entra al pool — lo hace el caller.
    """
    handle_id = uuid.uuid4().hex[:12]
    t0 = time.time()
    try:
        import tm_browser_engine
        browser, context, page, meta = tm_browser_engine._launch_engine(
            engine, headless=headless, country=country,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Inyectar cookies PX si están disponibles
    cfg = get_config()
    cookies_injected = 0
    if cfg.get("inject_px_cookies", True) and context is not None:
        try:
            import px_prefarm
            session = px_prefarm.pick_cookies(domain_id=country) or {}
            cookies = session.get("cookies") or {}
            if cookies:
                # domain match amplio
                import config as cfg_mod
                dom = cfg_mod.TM_DOMAINS.get(country, {}).get("domain", "www.ticketmaster.com")
                ck_list = []
                for name, value in cookies.items():
                    ck_list.append({
                        "name": name, "value": str(value),
                        "domain": "." + dom.replace("www.", ""),
                        "path": "/", "httpOnly": False, "secure": True,
                    })
                try:
                    context.add_cookies(ck_list)
                    cookies_injected = len(ck_list)
                except Exception:
                    pass
        except Exception:
            pass

    elapsed_ms = int((time.time() - t0) * 1000)
    _metrics["create_times_ms"].append(elapsed_ms)
    _metrics["total_creates"] += 1

    return {
        "ok": True,
        "handle_id": handle_id,
        "browser": browser,
        "context": context,
        "page": page,
        "meta": meta,
        "created_at": datetime.now().isoformat(),
        "country": country,
        "engine": engine,
        "cookies_injected": cookies_injected,
        "status": "ready",
        "creation_ms": elapsed_ms,
    }


def _destroy_context(handle: dict):
    try:
        import tm_browser_engine
        tm_browser_engine._close_engine(handle.get("browser"), handle.get("meta", {}))
    except Exception:
        pass


def _is_expired(handle: dict) -> bool:
    cfg = get_config()
    try:
        created = datetime.fromisoformat(handle.get("created_at", ""))
        return datetime.now() > created + timedelta(minutes=cfg.get("ttl_min", 15))
    except Exception:
        return True


def _manager_loop(stop_event: threading.Event):
    """Mantiene pool al tamaño target: crea nuevos + quema expirados."""
    while not stop_event.is_set():
        try:
            cfg = get_config()
            target = int(cfg.get("pool_size", 2))
            country = cfg.get("country", "us")
            engine = cfg.get("engine", "chromium")
            headless = bool(cfg.get("headless", True))

            # 1. Quemar expirados
            to_burn = []
            with _lock:
                for hid, h in list(_contexts.items()):
                    if h.get("status") == "ready" and _is_expired(h):
                        to_burn.append(hid)
            for hid in to_burn:
                burn(hid, reason="expired")

            # 2. Contar ready actuales
            with _lock:
                ready = sum(1 for h in _contexts.values() if h.get("status") == "ready")

            # 3. Crear lo que falte (max 1 por tick para no saturar)
            if ready < target:
                handle = _create_context(country, engine, headless)
                if handle and handle.get("ok"):
                    with _lock:
                        _contexts[handle["handle_id"]] = handle
                        _cond.notify_all()
                else:
                    # Si falla creación, espera más antes de reintentar
                    stop_event.wait(20)
                    continue
        except Exception:
            pass
        stop_event.wait(3)


def start_manager() -> dict:
    if _manager["running"]:
        return {"ok": True, "already_running": True}
    stop = threading.Event()
    t = threading.Thread(target=_manager_loop, args=(stop,),
                         daemon=True, name="browser-pool-manager")
    _manager.update({"running": True, "thread": t, "stop_event": stop})
    t.start()
    return {"ok": True, "started": True}


def stop_manager(drain: bool = True) -> dict:
    if not _manager["running"]:
        return {"ok": True, "not_running": True}
    ev = _manager.get("stop_event")
    if ev:
        ev.set()
    _manager.update({"running": False, "thread": None, "stop_event": None})
    if drain:
        # Quema todos los contextos ready
        with _lock:
            handles = list(_contexts.keys())
        for hid in handles:
            burn(hid, reason="drain")
    return {"ok": True, "stopped": True, "drained": drain}


# ═══════════════════════════════════════════
#  ACQUIRE / RELEASE / BURN
# ═══════════════════════════════════════════
def acquire(country: Optional[str] = None, timeout: float = None) -> dict:
    """Entrega un contexto pre-calentado si hay, o crea uno cold (lento).

    country: si indicado, prefiere contextos creados para ese país.
    timeout: segundos a esperar a que el manager prepare uno (default: 5).

    Devuelve: {ok, handle_id, engine, cookies_injected, cold_start, acquire_ms, ...}
    """
    cfg = get_config()
    if timeout is None:
        timeout = float(cfg.get("max_acquire_wait_s", 5))
    t0 = time.time()
    _metrics["total_acquires"] += 1

    # 1. Fast path: buscar ready en el pool
    deadline = time.time() + timeout
    with _cond:
        while True:
            # busca matching por país si posible
            chosen_id = None
            for hid, h in _contexts.items():
                if h.get("status") != "ready":
                    continue
                if country and h.get("country") != country:
                    continue
                chosen_id = hid
                break
            # fallback cualquier ready si no hubo match país
            if not chosen_id and country:
                for hid, h in _contexts.items():
                    if h.get("status") == "ready":
                        chosen_id = hid
                        break
            if chosen_id:
                h = _contexts[chosen_id]
                h["status"] = "in_use"
                h["acquired_at"] = datetime.now().isoformat()
                elapsed_ms = int((time.time() - t0) * 1000)
                _metrics["acquire_times_ms"].append(elapsed_ms)
                _metrics["total_hits"] += 1
                return {
                    "ok": True, "handle_id": chosen_id,
                    "engine": h.get("engine"), "country": h.get("country"),
                    "cookies_injected": h.get("cookies_injected", 0),
                    "cold_start": False, "acquire_ms": elapsed_ms,
                    "creation_ms": h.get("creation_ms"),
                }
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            _cond.wait(timeout=min(remaining, 1))

    # 2. Cold path: crear uno nuevo al vuelo
    _metrics["total_misses"] += 1
    engine = cfg.get("engine", "chromium")
    country_use = country or cfg.get("country", "us")
    h = _create_context(country_use, engine, bool(cfg.get("headless", True)))
    if not h or not h.get("ok"):
        elapsed_ms = int((time.time() - t0) * 1000)
        _metrics["acquire_times_ms"].append(elapsed_ms)
        return {"ok": False, "error": (h or {}).get("error", "create_failed"),
                "cold_start": True, "acquire_ms": elapsed_ms}
    with _lock:
        h["status"] = "in_use"
        h["acquired_at"] = datetime.now().isoformat()
        _contexts[h["handle_id"]] = h
    elapsed_ms = int((time.time() - t0) * 1000)
    _metrics["acquire_times_ms"].append(elapsed_ms)
    return {
        "ok": True, "handle_id": h["handle_id"],
        "engine": h["engine"], "country": h["country"],
        "cookies_injected": h.get("cookies_injected", 0),
        "cold_start": True, "acquire_ms": elapsed_ms,
        "creation_ms": h.get("creation_ms"),
    }


def release(handle_id: str, success: bool = True) -> dict:
    """Devuelve un contexto al pool (si success) o lo quema (si no)."""
    with _lock:
        h = _contexts.get(handle_id)
    if not h:
        return {"ok": False, "error": "handle_not_found"}
    if not success:
        return burn(handle_id, reason="release_failed")
    # contextos tras un flow real suelen estar con cookies de sesión
    # llenas → quemamos para que el próximo use cookies PX frescas.
    # Solo se devuelven "ready" si success Y no hubo navegación relevante,
    # lo que es raro. Default: burn tras uso.
    return burn(handle_id, reason="post_use")


def burn(handle_id: str, reason: str = "") -> dict:
    with _lock:
        h = _contexts.pop(handle_id, None)
    if not h:
        return {"ok": False, "error": "handle_not_found"}
    _destroy_context(h)
    _metrics["total_burns"] += 1
    return {"ok": True, "burned": handle_id, "reason": reason}


# ═══════════════════════════════════════════
#  STATS / STATUS
# ═══════════════════════════════════════════
def _avg(seq) -> int:
    items = list(seq)
    return int(sum(items) / len(items)) if items else 0


def get_stats() -> dict:
    with _lock:
        ready = sum(1 for h in _contexts.values() if h.get("status") == "ready")
        in_use = sum(1 for h in _contexts.values() if h.get("status") == "in_use")
        total = len(_contexts)
    cfg = get_config()
    hits = _metrics["total_hits"]
    misses = _metrics["total_misses"]
    total_acq = hits + misses
    hit_rate = (hits / total_acq * 100) if total_acq else 0
    return {
        "pool_target": cfg.get("pool_size"),
        "ready": ready,
        "in_use": in_use,
        "total_contexts": total,
        "manager_running": _manager["running"],
        "engine": cfg.get("engine"),
        "country": cfg.get("country"),
        "ttl_min": cfg.get("ttl_min"),
        "total_acquires": _metrics["total_acquires"],
        "total_hits": hits,
        "total_misses": misses,
        "total_burns": _metrics["total_burns"],
        "total_creates": _metrics["total_creates"],
        "hit_rate_pct": round(hit_rate, 1),
        "avg_acquire_ms": _avg(_metrics["acquire_times_ms"]),
        "avg_create_ms": _avg(_metrics["create_times_ms"]),
    }


def list_contexts() -> List[dict]:
    """Snapshot (sin browser/page objects)."""
    with _lock:
        out = []
        for hid, h in _contexts.items():
            out.append({
                "handle_id": hid,
                "status": h.get("status"),
                "engine": h.get("engine"),
                "country": h.get("country"),
                "created_at": h.get("created_at"),
                "acquired_at": h.get("acquired_at"),
                "cookies_injected": h.get("cookies_injected", 0),
                "creation_ms": h.get("creation_ms"),
                "expired": _is_expired(h),
            })
        return out


def self_test() -> dict:
    """Test: arranca manager 20s, hace acquire+release, valida latencia."""
    started = time.time()
    import tm_browser_engine
    pw_status = tm_browser_engine.check_playwright()
    if not pw_status.get("ok"):
        return {"ok": False, "error": "no playwright engines", "check": pw_status}

    # Config reducida para test
    configure({"pool_size": 1, "ttl_min": 5, "headless": True})
    start_manager()
    # Esperar a que el manager cree al menos 1 context
    waited = 0
    while waited < 25:
        time.sleep(1)
        waited += 1
        with _lock:
            ready = sum(1 for h in _contexts.values() if h.get("status") == "ready")
        if ready >= 1:
            break
    stats_before = get_stats()

    # Acquire
    acq = acquire(timeout=3)
    if acq.get("ok"):
        release(acq["handle_id"], success=True)
    stop_manager(drain=True)

    return {
        "ok": True,
        "elapsed_s": int(time.time() - started),
        "pw_engines": pw_status.get("engines", []),
        "acquire_ok": acq.get("ok", False),
        "acquire_ms": acq.get("acquire_ms"),
        "cold_start": acq.get("cold_start"),
        "stats_before": {k: stats_before[k] for k in ("ready", "in_use", "avg_create_ms", "total_creates")},
        "stats_after": get_stats(),
    }
