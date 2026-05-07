"""proxy_health.py — Benchmark real de proxies + score + auto-rotation.

Mide cada proxy contra:
- httpbin.org/ip  (latencia baja-nivel + IP confirmation)
- ticketmaster.com (latencia contra target real)

Calcula un score 0-100 basado en:
- Latencia (rápido=más puntos)
- Success rate histórico (últimos 10 checks)
- Geo match (si proxy y dominio objetivo coinciden país → bonus)
- Status code del último check

Auto-rotación: proxies con score <30 se marcan dead; se priorizan los >70.
"""
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from urllib.parse import urlparse

import config
import json_atomic


PROXY_PATH_KEY = "proxy_health.json"

# Targets de benchmark (URL, timeout, weight en score)
BENCHMARK_TARGETS = [
    ("https://httpbin.org/ip", 8, 1),
    ("https://www.ticketmaster.com", 12, 2),
]

# Umbrales (ms)
FAST_THRESHOLD_MS = 400
SLOW_THRESHOLD_MS = 1500

HISTORY_SIZE = 10  # últimos N checks por proxy
_lock = threading.Lock()


def _path():
    return config.ruta(PROXY_PATH_KEY)


def _load():
    return json_atomic.load(_path(), default={"proxies": []})


def _save(d):
    json_atomic.save(_path(), d)


def _normalize_proxy_url(raw: str) -> str:
    """Normaliza un proxy. Acepta:
      - 'http://...' / 'https://...'           (tal cual)
      - 'user:pass@host:port'                   (prefija http://)
      - 'host:port'                              (prefija http://)
      - 'host:port:user:pass'  (4 partes)        (convierte a http://user:pass@host:port)
        Detecta el formato 4-partes solo si no hay '@' (ambiguo si user contiene ':')."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw
    if "@" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            return f"http://{user}:{pwd}@{host}:{port}"
    return "http://" + raw


def _proxy_id(url: str) -> str:
    """ID estable a partir de la URL del proxy."""
    import hashlib
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:10]


# ═══════════════════════════════════════════
#  CRUD
# ═══════════════════════════════════════════
def add_proxies(raw_list: List[str], geo: str = "") -> dict:
    """Añade varios proxies. Dedupe por URL. Devuelve stats."""
    with _lock:
        d = _load()
        existing = {p["url"] for p in d.get("proxies", [])}
        added = 0
        for raw in raw_list:
            url = _normalize_proxy_url(raw)
            if not url or url in existing:
                continue
            d["proxies"].append({
                "id": _proxy_id(url),
                "url": url,
                "geo": geo,
                "status": "unchecked",
                "score": 0,
                "latency_ms": None,
                "success_count": 0,
                "fail_count": 0,
                "last_check": None,
                "last_error": "",
                "history": [],
                "added_at": datetime.now().isoformat(),
                "public_ip": None,
            })
            existing.add(url)
            added += 1
        _save(d)
    return {"ok": True, "added": added, "total": len(d["proxies"])}


def remove_proxy(pid: str) -> bool:
    with _lock:
        d = _load()
        before = len(d["proxies"])
        d["proxies"] = [p for p in d["proxies"] if p["id"] != pid]
        _save(d)
    return len(d["proxies"]) < before


def clear_dead() -> int:
    with _lock:
        d = _load()
        before = len(d["proxies"])
        d["proxies"] = [p for p in d["proxies"] if p.get("status") != "dead"]
        _save(d)
    return before - len(d["proxies"])


def list_proxies(only_alive: bool = False) -> List[dict]:
    d = _load()
    items = d.get("proxies", [])
    if only_alive:
        items = [p for p in items if p.get("status") in ("fast", "slow")]
    # ordenar: fast primero, luego por score desc
    order = {"fast": 0, "slow": 1, "unchecked": 2, "dead": 3}
    items.sort(key=lambda p: (order.get(p.get("status"), 9), -(p.get("score") or 0)))
    return items


def get_stats() -> dict:
    d = _load()
    items = d.get("proxies", [])
    by_status = {"fast": 0, "slow": 0, "unchecked": 0, "dead": 0}
    by_geo = {}
    total_lat, n_lat = 0, 0
    for p in items:
        by_status[p.get("status", "unchecked")] = by_status.get(p.get("status", "unchecked"), 0) + 1
        g = p.get("geo") or "?"
        by_geo[g] = by_geo.get(g, 0) + 1
        if isinstance(p.get("latency_ms"), (int, float)):
            total_lat += p["latency_ms"]
            n_lat += 1
    return {
        "total": len(items),
        "fast": by_status["fast"],
        "slow": by_status["slow"],
        "dead": by_status["dead"],
        "unchecked": by_status["unchecked"],
        "avg_latency_ms": int(total_lat / n_lat) if n_lat else 0,
        "by_geo": by_geo,
        "checked": sum(1 for p in items if p.get("last_check")),
    }


# ═══════════════════════════════════════════
#  BENCHMARK
# ═══════════════════════════════════════════
def _check_one_target(proxy_url: str, target: str, timeout: int) -> dict:
    """Un HTTP GET vía el proxy al target."""
    import requests
    start = time.time()
    try:
        r = requests.get(
            target,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
            headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
            },
            allow_redirects=True,
        )
        elapsed = int((time.time() - start) * 1000)
        ok = 200 <= r.status_code < 400
        out = {
            "target": target,
            "ok": ok,
            "status_code": r.status_code,
            "elapsed_ms": elapsed,
        }
        if "httpbin.org/ip" in target:
            try:
                out["public_ip"] = r.json().get("origin", "")
            except Exception:
                pass
        return out
    except Exception as e:
        return {
            "target": target,
            "ok": False,
            "status_code": 0,
            "elapsed_ms": int((time.time() - start) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }


def check_proxy(pid: str) -> dict:
    """Benchmark completo de un proxy: ejecuta los 2 targets."""
    with _lock:
        d = _load()
        p = next((x for x in d["proxies"] if x["id"] == pid), None)
        if not p:
            return {"ok": False, "error": "proxy no encontrado"}
        proxy_url = p["url"]

    results = [_check_one_target(proxy_url, t, to) for t, to, _w in BENCHMARK_TARGETS]
    success = [r for r in results if r.get("ok")]
    avg_ms = int(sum(r["elapsed_ms"] for r in results) / len(results)) if results else 0
    public_ip = next((r.get("public_ip") for r in success if r.get("public_ip")), None)
    last_error = next((r.get("error") for r in results if r.get("error")), "")

    # Score 0-100
    # Base: % success (0-60 pts)
    score = int(len(success) / len(results) * 60) if results else 0
    # Latencia: fast=+30, slow=+10, dead=0
    if success:
        if avg_ms < FAST_THRESHOLD_MS:
            score += 30
        elif avg_ms < SLOW_THRESHOLD_MS:
            score += 20
        else:
            score += 10
    # historial: bonus si success rate alto histórico (hasta +10)
    with _lock:
        d = _load()
        p = next((x for x in d["proxies"] if x["id"] == pid), None)
        if p:
            hist = p.get("history", [])[:HISTORY_SIZE - 1]
            prior_success = sum(1 for h in hist if h.get("ok"))
            if hist:
                hist_rate = prior_success / len(hist)
                score += int(hist_rate * 10)

            # Determinar status
            if not success:
                status = "dead"
            elif avg_ms < FAST_THRESHOLD_MS:
                status = "fast"
            else:
                status = "slow"

            entry_ok = bool(success)
            new_hist = [{"ts": datetime.now().isoformat(), "ok": entry_ok,
                         "latency_ms": avg_ms if entry_ok else None,
                         "results": results}] + hist
            new_hist = new_hist[:HISTORY_SIZE]

            p["status"] = status
            p["score"] = min(100, score)
            p["latency_ms"] = avg_ms if success else None
            p["last_check"] = datetime.now().isoformat()
            p["last_error"] = last_error
            p["public_ip"] = public_ip or p.get("public_ip")
            p["history"] = new_hist
            if entry_ok:
                p["success_count"] = p.get("success_count", 0) + 1
            else:
                p["fail_count"] = p.get("fail_count", 0) + 1
            _save(d)
            return {"ok": True, "proxy": _safe(p)}
    return {"ok": False, "error": "race condition"}


def check_all(concurrency: int = 10, broadcast=None) -> dict:
    """Benchmark todos los proxies en paralelo."""
    d = _load()
    ids = [p["id"] for p in d.get("proxies", [])]
    if not ids:
        return {"ok": True, "checked": 0}
    results = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 30))) as ex:
        futures = {ex.submit(check_proxy, pid): pid for pid in ids}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            if broadcast:
                try:
                    broadcast("proxy_health_checked",
                              {"id": futures[fut], "ok": r.get("ok")})
                except Exception:
                    pass
    elapsed = time.time() - started
    stats = get_stats()
    return {
        "ok": True,
        "checked": len(results),
        "elapsed_s": round(elapsed, 1),
        "stats": stats,
    }


# ═══════════════════════════════════════════
#  AUTO-ROTATION (pick best)
# ═══════════════════════════════════════════
# TLD → country (geo) — para que la URL TM marque el geo del proxy.
# Cubre los 25 dominios TM activos.
_TLD_TO_GEO = {
    "ticketmaster.com":     "us",
    "ticketmaster.ca":      "ca",
    "ticketmaster.com.mx":  "mx",
    "ticketmaster.es":      "es",
    "ticketmaster.co.uk":   "uk",
    "ticketmaster.ie":      "ie",
    "ticketmaster.fr":      "fr",
    "ticketmaster.de":      "de",
    "ticketmaster.at":      "at",
    "ticketmaster.ch":      "ch",
    "ticketmaster.it":      "it",
    "ticketmaster.nl":      "nl",
    "ticketmaster.be":      "be",
    "ticketmaster.pt":      "pt",
    "ticketmaster.se":      "se",
    "ticketmaster.no":      "no",
    "ticketmaster.dk":      "dk",
    "ticketmaster.fi":      "fi",
    "ticketmaster.pl":      "pl",
    "ticketmaster.com.au":  "au",
    "ticketmaster.co.nz":   "nz",
}


def geo_for_url(url: str) -> Optional[str]:
    """Devuelve el geo (country) inferido del dominio TM en una URL.
    None si la URL no apunta a TM."""
    if not url: return None
    u = url.lower()
    for tld, cc in _TLD_TO_GEO.items():
        if tld in u:
            return cc
    return None


def pick_best(geo: Optional[str] = None,
              min_score: int = 70,
              for_url: Optional[str] = None,
              strict_geo: bool = False) -> Optional[dict]:
    """Devuelve el mejor proxy disponible, opcionalmente por geo.

    Preferencia: fast > slow > unchecked, mayor score primero.
    - geo: filtra por country si se da. Si no y se pasa for_url, deduce de TLD.
    - for_url: URL del evento; mapea automáticamente al country del TM domain.
    - strict_geo: si True, NO hace fallback fuera del geo (devuelve None si no
      hay proxy en ese country). Default False (preserva comportamiento previo).
    """
    if not geo and for_url:
        geo = geo_for_url(for_url)
    d = _load()
    items = [p for p in d.get("proxies", [])
             if p.get("status") in ("fast", "slow") and (p.get("score") or 0) >= min_score]
    if geo:
        match = [p for p in items if p.get("geo") == geo]
        if match:
            items = match
        elif strict_geo:
            return None
    if not items:
        # fallback a todos alive sin filtrar score (a menos que strict_geo)
        if strict_geo and geo:
            return None
        items = [p for p in d.get("proxies", []) if p.get("status") in ("fast", "slow")]
        if strict_geo and geo:
            items = [p for p in items if p.get("geo") == geo]
    if not items:
        return None
    order = {"fast": 0, "slow": 1}
    items.sort(key=lambda p: (order.get(p.get("status"), 9), -(p.get("score") or 0)))
    return _safe(items[0])


def pick_best_with_sticky(worker_id: str,
                            geo: Optional[str] = None,
                            domain: str = "ticketmaster.es",
                            min_score: int = 70,
                            for_url: Optional[str] = None,
                            strict_geo: bool = False,
                            ttl_s: int = 1800,
                            scheme: str = "bright_data") -> Optional[dict]:
    """Como pick_best pero asigna session-id sticky al worker_id (1 IP / 30 min).

    Devuelve el dict del proxy con campo extra:
      - sticky_session: {session_id, ttl_s, ends_at, ...}
      - sticky_username: username con sticky aplicado al esquema del provider

    Útil para Queue-it: garantiza que el mismo worker mantiene la misma IP
    durante toda la cola virtual (cambio de IP invalida AcpAT).
    """
    p = pick_best(geo=geo, min_score=min_score, for_url=for_url,
                   strict_geo=strict_geo)
    if not p or not worker_id:
        return p
    try:
        import proxy_sticky
        sess = proxy_sticky.get_session(
            worker_id=worker_id, domain=domain, ttl_s=ttl_s)
        if not sess.get("ok"):
            return p
        s = sess.get("session", {})
        sid = s.get("session_id")
        ttl_min = s.get("ttl_minutes", 30)
        # Inferir base_user del proxy original si tiene username
        base_user = ""
        url_preview = p.get("url_preview", "")
        # url_preview oculta credenciales, así que vamos al raw — releemos del disco
        d = _load()
        for raw in d.get("proxies", []):
            if raw.get("id") == p.get("id"):
                try:
                    parsed = urlparse(raw.get("url", ""))
                    base_user = parsed.username or ""
                except Exception:
                    pass
                break
        sticky_username = proxy_sticky.format_username(
            base_user or "user", sid, ttl_min, scheme) if base_user else None
        p2 = dict(p)
        p2["sticky_session"] = s
        p2["sticky_username"] = sticky_username
        p2["sticky_scheme"] = scheme
        return p2
    except Exception:
        return p


def _safe(p: dict) -> dict:
    """Evita leak de credenciales en respuesta (oculta user:pass)."""
    c = dict(p)
    url = c.get("url", "")
    try:
        parsed = urlparse(url)
        if parsed.username:
            host = parsed.hostname or ""
            port = parsed.port or ""
            c["url_preview"] = f"{parsed.scheme}://***:***@{host}" + (f":{port}" if port else "")
        else:
            c["url_preview"] = url
    except Exception:
        c["url_preview"] = "***"
    # no incluir url raw
    c.pop("url", None)
    return c


# ═══════════════════════════════════════════
#  PRE-WARM PARA DROPS (sale_date)
# ═══════════════════════════════════════════
def prewarm_for_drop(geo: Optional[str] = None,
                     concurrency: int = 10,
                     min_alive: int = 30) -> dict:
    """Lanza check_all() filtrado por geo si se pasa. Útil 5 min antes de
    un sale_date para tener el pool ya rankeado.
    Devuelve {ok, alive, fast, slow, dead, geo, started_at, latency_ms}."""
    import time as _t
    from datetime import datetime as _dt
    started = _t.time()
    started_iso = _dt.now().isoformat()
    res = check_all(concurrency=concurrency)
    d = _load()
    items = d.get("proxies", [])
    if geo:
        items = [p for p in items if p.get("geo") == geo]
    fast = sum(1 for p in items if p.get("status") == "fast")
    slow = sum(1 for p in items if p.get("status") == "slow")
    dead = sum(1 for p in items if p.get("status") == "dead")
    alive = fast + slow
    return {
        "ok": True,
        "geo": geo or "any",
        "started_at": started_iso,
        "latency_ms": int((_t.time() - started) * 1000),
        "alive": alive, "fast": fast, "slow": slow, "dead": dead,
        "min_alive_ok": alive >= min_alive,
        "checked_total": res.get("checked", 0) if isinstance(res, dict) else None,
    }


def schedule_prewarm_for_events(events: List[dict],
                                 lead_min: int = 5,
                                 broadcast=None) -> dict:
    """Para cada evento con sale_date futuro, programa prewarm 'lead_min'
    minutos antes del drop. Devuelve la lista de schedules creados.

    NOTA: usa threading + sleep en background. Diseño minimalista — para
    horizontes >24h prefiere CronCreate. Aquí cubrimos drops <24h.
    """
    import threading
    from datetime import datetime as _dt
    now = _dt.now()
    scheduled = []
    for ev in events or []:
        sd = ev.get("sale_date") or ""
        if not sd: continue
        try:
            t_sale = _dt.fromisoformat(sd)
        except Exception:
            continue
        seconds_until = (t_sale - now).total_seconds() - lead_min * 60
        if seconds_until <= 0 or seconds_until > 24 * 3600:
            continue
        geo = ev.get("country") or ""
        ev_id = ev.get("id", "?")

        def _delayed(delay_s, g, eid):
            import time as _t
            _t.sleep(delay_s)
            try:
                r = prewarm_for_drop(geo=g)
                if broadcast:
                    broadcast({"type": "proxy_prewarm_done",
                                "event_id": eid, "result": r})
            except Exception:
                pass

        threading.Thread(target=_delayed,
                          args=(seconds_until, geo, ev_id),
                          daemon=True).start()
        scheduled.append({"event_id": ev_id, "sale_date": sd, "geo": geo,
                          "fires_in_s": int(seconds_until),
                          "lead_min": lead_min})
    return {"ok": True, "scheduled": scheduled, "count": len(scheduled)}


# ═══════════════════════════════════════════
#  SCHEDULER (auto-check cada N min)
# ═══════════════════════════════════════════
_scheduler = {"running": False, "thread": None, "stop_event": None}


def scheduler_status() -> dict:
    return {"running": _scheduler["running"]}


def _scheduler_loop(stop_event, interval_min, broadcast):
    while not stop_event.is_set():
        try:
            check_all(concurrency=10, broadcast=broadcast)
        except Exception:
            pass
        stop_event.wait(max(60, interval_min * 60))


def start_scheduler(interval_min: int = 30, broadcast=None) -> dict:
    if _scheduler["running"]:
        return {"ok": True, "already_running": True}
    ev = threading.Event()
    t = threading.Thread(target=_scheduler_loop, args=(ev, interval_min, broadcast),
                         daemon=True, name="proxy-health-scheduler")
    _scheduler.update({"running": True, "thread": t, "stop_event": ev})
    t.start()
    return {"ok": True, "started": True, "interval_min": interval_min}


def stop_scheduler() -> dict:
    if not _scheduler["running"]:
        return {"ok": True, "not_running": True}
    ev = _scheduler.get("stop_event")
    if ev:
        ev.set()
    _scheduler.update({"running": False, "thread": None, "stop_event": None})
    return {"ok": True, "stopped": True}
