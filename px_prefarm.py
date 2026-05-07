"""px_prefarm.py — PerimeterX (HUMAN Security) cookie pre-farming.

⚠️ AVISO:
Este módulo lanza navegadores REALES (Playwright/Camoufox) y navega por
ticketmaster.com simulando interacciones humanas para pre-generar las
cookies `_px*`, `_pxhd`, `_pxvid`, `_pxde`, `visid_incap_*`. Después las
cookies se inyectan en los workers de compra para saltarse el challenge
JS inicial de PerimeterX (reduce tiempo de checkout 10-30s).

Estrategia:
  1. Lanza browser (camoufox/chromium) con stealth
  2. GET home TM del país → PerimeterX genera challenge
  3. Interacciones humanas 2-30 min:
     - Movimientos de ratón curvos (Bezier)
     - Scrolls naturales con velocidad variable
     - Hover sobre links aleatorios
     - Pausas y lecturas
  4. Navega al URL del evento (si se proporciona)
  5. Más interacciones
  6. Extrae cookies `_px*`, `_pxvid`, `_pxhd`, `visid_incap_*`, AcpAT
  7. Guarda en pool con TTL 30 min
  8. `pick_cookies(domain_id)` → devuelve cookies frescas al worker

Integra con tm_browser_engine para stealth + multi-engine.
Por defecto usa `camoufox` (mejor anti-PerimeterX free).
"""
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Callable

import config
import json_atomic


POOL_KEY = "px_prefarm_pool.json"
LOG_KEY = "px_prefarm_log.json"
COOKIE_TTL_SECONDS = 30 * 60  # 30 min

# Cookies objetivo (PerimeterX + Imperva + TM core)
PX_COOKIE_PATTERNS = ["_px", "_pxvid", "_pxhd", "_pxde", "_pxff_",
                       "visid_incap_", "incap_ses_", "nlbi_",
                       "AcpAT", "TmsAuth", "SOURCE", "language", "eid_",
                       "bm_sz", "bm_mi", "bm_sv", "ak_bmsc", "_abck"]

_TM_DOMAINS = {
    "us": "https://www.ticketmaster.com",
    "ca": "https://www.ticketmaster.ca",
    "mx": "https://www.ticketmaster.com.mx",
    "uk": "https://www.ticketmaster.co.uk",
    "ie": "https://www.ticketmaster.ie",
    "es": "https://www.ticketmaster.es",
    "de": "https://www.ticketmaster.de",
    "fr": "https://www.ticketmaster.fr",
    "it": "https://www.ticketmaster.it",
    "nl": "https://www.ticketmaster.nl",
    "au": "https://www.ticketmaster.com.au",
    "nz": "https://www.ticketmaster.co.nz",
    "at": "https://www.ticketmaster.at",
    "be": "https://www.ticketmaster.be",
    "se": "https://www.ticketmaster.se",
    "dk": "https://www.ticketmaster.dk",
    "no": "https://www.ticketmaster.no",
    "fi": "https://www.ticketmaster.fi",
    "pl": "https://www.ticketmaster.pl",
    "cz": "https://www.ticketmaster.cz",
    "ch": "https://www.ticketmaster.ch",
    "ae": "https://www.ticketmaster.ae",
}


_lock = threading.Lock()

# Scheduler global
_scheduler = {"running": False, "thread": None, "stop_event": None,
              "last_tick": None, "ticks": 0}

# Sesiones activas (engine browser corriendo)
_active_sessions = {}


def _pool_path():
    return config.ruta(POOL_KEY)


def _log_path():
    return config.ruta(LOG_KEY)


# ═══════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════
def get_config() -> dict:
    return json_atomic.load(config.ruta("px_prefarm_config.json"), default={
        "default_engine": "camoufox",     # camoufox | chromium | patchright | firefox
        "default_duration_min": 3,         # 2-30 min (corto para test, alto para real)
        "default_interactions_per_min": 20,
        "pool_size_target": 5,              # mantener N sesiones cacheadas
        "auto_farm_enabled": False,
        "auto_interval_min": 20,            # cada 20 min lanza nueva sesión
        "headless": True,
        "visit_home": True,
        "visit_category_pages": True,
        "max_parallel_sessions": 2,
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    allowed = {"default_engine", "default_duration_min", "default_interactions_per_min",
               "pool_size_target", "auto_farm_enabled", "auto_interval_min",
               "headless", "visit_home", "visit_category_pages", "max_parallel_sessions"}
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    cur["default_duration_min"] = max(0.5, min(float(cur.get("default_duration_min", 3)), 30))
    cur["pool_size_target"] = max(1, min(int(cur.get("pool_size_target", 5)), 50))
    cur["max_parallel_sessions"] = max(1, min(int(cur.get("max_parallel_sessions", 2)), 5))
    json_atomic.save(config.ruta("px_prefarm_config.json"), cur)
    return cur


# ═══════════════════════════════════════════
#  POOL
# ═══════════════════════════════════════════
def _load_pool() -> dict:
    return json_atomic.load(_pool_path(), default={"sessions": []})


def _save_pool(d: dict):
    json_atomic.save(_pool_path(), d)


def _prune_expired(pool: dict) -> int:
    now = datetime.now()
    fresh = []
    removed = 0
    for s in pool.get("sessions", []):
        try:
            exp = datetime.fromisoformat(s.get("expires_at", ""))
            if exp > now:
                fresh.append(s)
            else:
                removed += 1
        except Exception:
            removed += 1
    pool["sessions"] = fresh
    return removed


def pool_stats() -> dict:
    with _lock:
        pool = _load_pool()
        removed = _prune_expired(pool)
        if removed:
            _save_pool(pool)
    sessions = pool.get("sessions", [])
    by_domain = {}
    by_engine = {}
    total_cookies = 0
    for s in sessions:
        d = s.get("domain_id", "?")
        e = s.get("engine", "?")
        by_domain[d] = by_domain.get(d, 0) + 1
        by_engine[e] = by_engine.get(e, 0) + 1
        total_cookies += len(s.get("cookies", []))
    return {
        "size": len(sessions),
        "ttl_seconds": COOKIE_TTL_SECONDS,
        "by_domain": by_domain,
        "by_engine": by_engine,
        "total_cookies": total_cookies,
        "avg_cookies_per_session": round(total_cookies / max(1, len(sessions)), 1),
        "total_uses": sum(s.get("used_count", 0) for s in sessions),
        "active_sessions_running": len(_active_sessions),
    }


def list_pool_entries(limit: int = 20, include_cookies: bool = False) -> List[dict]:
    pool = _load_pool()
    _prune_expired(pool)
    out = []
    for s in pool.get("sessions", [])[:limit]:
        c = dict(s)
        if not include_cookies:
            c["cookie_names"] = [ck.get("name", "") for ck in c.get("cookies", [])]
            c.pop("cookies", None)
        out.append(c)
    return out


def pick_cookies(domain_id: str, min_px_cookies: int = 1) -> dict:
    """Extrae el mejor set de cookies frescas del pool para el dominio."""
    with _lock:
        pool = _load_pool()
        _prune_expired(pool)
        candidates = [s for s in pool["sessions"] if s.get("domain_id") == domain_id]
        if not candidates:
            return {"ok": False, "error": f"sin cookies en pool para {domain_id}"}
        # Filtrar por mínimo de cookies _px*
        good = []
        for s in candidates:
            px_count = sum(1 for c in s.get("cookies", []) if any(c.get("name", "").startswith(p) for p in PX_COOKIE_PATTERNS))
            if px_count >= min_px_cookies:
                good.append((s, px_count))
        if not good:
            # fallback: devolver el más reciente aunque no tenga muchas _px
            good = [(s, 0) for s in candidates]
        # Preferir menos usado + más reciente + con más _px cookies
        good.sort(key=lambda t: (t[0].get("used_count", 0),
                                  -_to_ts(t[0].get("created_at", "")),
                                  -t[1]))
        pick = good[0][0]
        pick["used_count"] = pick.get("used_count", 0) + 1
        pick["last_used_at"] = datetime.now().isoformat()
        _save_pool(pool)
        # Devolver copia sin modificar pool
        return {
            "ok": True,
            "session_id": pick["id"],
            "cookies": list(pick.get("cookies", [])),
            "used_count": pick.get("used_count"),
            "age_s": int((datetime.now() - datetime.fromisoformat(pick.get("created_at"))).total_seconds()),
            "domain_id": pick.get("domain_id"),
            "engine": pick.get("engine"),
            "score": pick.get("score") or _score_session(pick),
        }


def _to_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0


def clear_pool() -> dict:
    with _lock:
        _save_pool({"sessions": []})
    return {"ok": True}


def delete_session(sid: str) -> bool:
    with _lock:
        pool = _load_pool()
        before = len(pool["sessions"])
        pool["sessions"] = [s for s in pool["sessions"] if s.get("id") != sid]
        _save_pool(pool)
    return len(pool["sessions"]) < before


# ═══════════════════════════════════════════
#  INTERACCIONES HUMANAS
# ═══════════════════════════════════════════
def _humanize_once(page, width: int = 1440, height: int = 900) -> dict:
    """Ejecuta una interacción humana aleatoria en la página."""
    action = random.choice([
        "mouse_move", "mouse_move", "mouse_move",  # más frecuentes
        "scroll", "scroll", "scroll",
        "hover_link", "pause", "pause",
    ])
    details = {"action": action}
    try:
        if action == "mouse_move":
            # Movimiento curvo tipo Bezier (aproximado)
            start_x = random.randint(100, width - 100)
            start_y = random.randint(100, height - 100)
            end_x = random.randint(100, width - 100)
            end_y = random.randint(100, height - 100)
            steps = random.randint(8, 20)
            page.mouse.move(start_x, start_y, steps=1)
            for i in range(1, steps + 1):
                # Interpolación con ruido
                progress = i / steps
                x = start_x + (end_x - start_x) * progress + random.randint(-5, 5)
                y = start_y + (end_y - start_y) * progress + random.randint(-5, 5)
                page.mouse.move(int(x), int(y), steps=1)
                time.sleep(random.uniform(0.01, 0.04))
            details["from"] = (start_x, start_y)
            details["to"] = (end_x, end_y)
        elif action == "scroll":
            amount = random.choice([100, 200, 300, 400, -100, -200, -300, 500, 600])
            # Scroll gradual para que parezca rueda humana
            steps = abs(amount) // 40
            sign = 1 if amount > 0 else -1
            for _ in range(max(1, steps)):
                page.mouse.wheel(0, sign * random.randint(30, 50))
                time.sleep(random.uniform(0.03, 0.1))
            details["amount"] = amount
        elif action == "hover_link":
            try:
                # Contar links visibles y hacer hover sobre uno aleatorio
                link_count = page.locator("a[href]").count()
                if link_count > 0:
                    idx = random.randint(0, min(link_count - 1, 30))
                    link = page.locator("a[href]").nth(idx)
                    if link.is_visible(timeout=500):
                        link.hover(timeout=2000, force=False)
                        details["link_idx"] = idx
                        time.sleep(random.uniform(0.3, 1.2))
            except Exception:
                pass
        # pause (no-op, sólo delay)
        time.sleep(random.uniform(0.5, 2.5))
    except Exception as e:
        details["error"] = f"{type(e).__name__}: {str(e)[:60]}"
    return details


def _extract_px_cookies(context) -> List[dict]:
    """Extrae todas las cookies relevantes del contexto."""
    try:
        all_cookies = context.cookies()
    except Exception:
        return []
    return [dict(c) for c in all_cookies
            if any(c.get("name", "").startswith(p) for p in PX_COOKIE_PATTERNS)
            or "ticketmaster" in (c.get("domain") or "").lower()
            or "queue-it" in (c.get("domain") or "").lower()
            or "incap" in (c.get("name") or "").lower()]


# ═══════════════════════════════════════════
#  SESSION EXECUTION
# ═══════════════════════════════════════════
def _log_entry(entry: dict):
    with _lock:
        d = json_atomic.load(_log_path(), default={"log": []})
        d["log"].insert(0, entry)
        d["log"] = d["log"][:50]
        json_atomic.save(_log_path(), d)


def get_log(limit: int = 20) -> List[dict]:
    return json_atomic.load(_log_path(), default={"log": []}).get("log", [])[:limit]


def run_farming_session(domain_id: str = "us",
                         event_url: Optional[str] = None,
                         duration_min: float = 3,
                         engine: str = "camoufox",
                         headless: bool = True,
                         interactions_per_min: int = 20,
                         broadcast: Optional[Callable] = None,
                         pin_proxy: bool = True,
                         retry_on_low_score: bool = False,
                         min_score_threshold: int = 40) -> dict:
    """Lanza sesión de farming real (bloqueante).

    Args:
      pin_proxy: si True, crea un pin en proxy_session_pin para mantener
        la misma IP entre farming y el worker posterior.
      retry_on_low_score: si True, si la sesión resulta con score<threshold
        la marca como "low_quality" y registra stat para reintento.
      min_score_threshold: umbral por debajo del cual se considera low-quality.
    """
    import uuid
    session_id = "px_" + uuid.uuid4().hex[:10]
    started = datetime.now()
    base_url = _TM_DOMAINS.get(domain_id, _TM_DOMAINS["us"])
    result = {
        "id": session_id,
        "started_at": started.isoformat(),
        "domain_id": domain_id,
        "engine": engine,
        "duration_min": duration_min,
        "status": "running",
        "steps": [],
    }

    # ═══ Integración proxy_session_pin ═══
    pin_owner = None
    if pin_proxy:
        try:
            import proxy_session_pin as psp
            pin_res = psp.pin_session(
                session_id=session_id,
                proxy_id=f"px-farm-{domain_id}",
                geo=domain_id,
                event_id=event_url or "",
                meta={"purpose": "px_prefarm", "engine": engine},
            )
            if pin_res.get("ok"):
                pin_owner = pin_res.get("owner")
                result["pin_session_id"] = session_id
                result["pin_owner"] = pin_owner
        except Exception:
            pass

    def _step(label, ok, **kw):
        s = {"step": label, "ok": ok, "ts": datetime.now().isoformat(), **kw}
        result["steps"].append(s)
        if broadcast:
            try:
                broadcast("px_prefarm_step", {"session_id": session_id, "step": s})
            except Exception:
                pass

    _active_sessions[session_id] = {"started": time.time(), "domain_id": domain_id}
    try:
        # Lanzar browser via tm_browser_engine
        try:
            import tm_browser_engine as be
            browser, context, page, meta = be._launch_engine(engine, headless, domain_id)
        except Exception as e:
            _step("launch", False, error=f"{type(e).__name__}: {e}")
            result["status"] = "failed"
            result["error"] = str(e)
            _log_entry({**result, "ended_at": datetime.now().isoformat()})
            _active_sessions.pop(session_id, None)
            return result
        _step("launch", True, engine=engine, ua=(meta.get("ua") or "")[:80])

        # 1) Visit home
        try:
            page.goto(base_url, timeout=45000, wait_until="domcontentloaded")
            # Esperar a que cargue PX challenge
            time.sleep(random.uniform(2, 4))
            _step("visit_home", True, url=base_url, title=page.title()[:80])
        except Exception as e:
            _step("visit_home", False, error=f"{type(e).__name__}: {e}")

        # 2) Interacciones aleatorias
        total_s = duration_min * 60
        interval = 60 / max(interactions_per_min, 5)
        interactions_count = 0
        interaction_end = time.time() + total_s
        # Si hay event URL: la mitad del tiempo en home, mitad en evento
        half_time = time.time() + total_s / 2 if event_url else interaction_end
        switched = False
        while time.time() < interaction_end:
            if event_url and not switched and time.time() >= half_time:
                try:
                    page.goto(event_url, timeout=45000, wait_until="domcontentloaded")
                    time.sleep(random.uniform(2, 4))
                    _step("visit_event", True, url=event_url[:80])
                    switched = True
                except Exception as e:
                    _step("visit_event", False, error=str(e)[:80])
                    switched = True  # no reintentar
            det = _humanize_once(page,
                                  width=meta.get("viewport_w", 1440),
                                  height=meta.get("viewport_h", 900))
            interactions_count += 1
            time.sleep(max(0.5, interval - 0.5))  # spacing base

        _step("interactions_done", True, count=interactions_count,
              elapsed_s=int(time.time() - _active_sessions[session_id]["started"]))

        # 3) Extraer cookies
        if context is None:
            # Camoufox devuelve browser directamente (no context separado)
            try:
                # camoufox: cookies via browser.contexts[0]
                ctx = browser.contexts[0] if browser.contexts else None
                cookies = _extract_px_cookies(ctx) if ctx else []
            except Exception:
                cookies = []
        else:
            cookies = _extract_px_cookies(context)

        px_count = sum(1 for c in cookies if any(c.get("name", "").startswith(p)
                                                   for p in ["_px", "_pxvid", "_pxhd"]))
        _step("extract_cookies", True, total=len(cookies), px_family=px_count,
              cookie_names=[c.get("name") for c in cookies[:8]])

        # 4) Guardar en pool
        elapsed_now = int((datetime.now() - started).total_seconds())
        entry = {
            "id": session_id,
            "domain_id": domain_id,
            "event_url": event_url,
            "engine": engine,
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(seconds=COOKIE_TTL_SECONDS)).isoformat(),
            "cookies": cookies,
            "cookie_count": len(cookies),
            "px_family_count": px_count,
            "interactions_count": interactions_count,
            "duration_min": duration_min,
            "elapsed_s": elapsed_now,
            "used_count": 0,
        }
        # Score de calidad
        entry["score"] = _score_session(entry)
        with _lock:
            pool = _load_pool()
            pool["sessions"].insert(0, entry)
            pool["sessions"] = pool["sessions"][:200]
            _save_pool(pool)
        _mark_farm_domain(domain_id)
        _step("save_pool", True, pool_size=len(pool["sessions"]),
              score=entry["score"])

        # 5) Cerrar
        try:
            be._close_engine(browser, meta)
            _step("close_browser", True)
        except Exception:
            pass

        result["status"] = "done"
        result["cookies_saved"] = len(cookies)
        result["px_family_count"] = px_count
        result["interactions"] = interactions_count
        result["elapsed_s"] = int((datetime.now() - started).total_seconds())
        result["score"] = entry["score"]
        # detección cookie _pxhd granular (broadcast)
        if any("_pxhd" in c.get("name", "") for c in cookies) and broadcast:
            try:
                broadcast("px_prefarm_cookie_detected", {
                    "session_id": session_id, "cookie": "_pxhd",
                    "domain_id": domain_id, "score": entry["score"],
                })
            except Exception:
                pass
        # auto-retry flag
        if retry_on_low_score and entry["score"] < min_score_threshold:
            result["low_quality"] = True
            result["retry_suggested"] = True
        # stats por dominio
        _record_domain_stat(domain_id, success=True, score=entry["score"],
                             has_pxhd=any("_pxhd" in c.get("name","") for c in cookies))
    except Exception as e:
        result["status"] = "failed"
        result["error"] = f"{type(e).__name__}: {e}"
        _step("exception", False, error=result["error"])
        _record_domain_stat(domain_id, success=False, score=0)
    finally:
        # Release proxy pin
        if pin_owner:
            try:
                import proxy_session_pin as psp
                psp.release(session_id)
            except Exception:
                pass
        _active_sessions.pop(session_id, None)
        result["ended_at"] = datetime.now().isoformat()
        _log_entry(result)
        if broadcast:
            try:
                broadcast("px_prefarm_done", {"session_id": session_id,
                                                "status": result["status"],
                                                "score": result.get("score", 0),
                                                "domain_id": domain_id})
            except Exception:
                pass
    return result


# ═══════════════════════════════════════════
#  STATS POR DOMINIO
# ═══════════════════════════════════════════
_DOMAIN_STATS: Dict[str, dict] = {}


def _record_domain_stat(domain_id: str, success: bool, score: int,
                          has_pxhd: bool = False):
    with _lock:
        d = _DOMAIN_STATS.setdefault(domain_id, {
            "sessions": 0, "successful": 0, "failed": 0,
            "total_score": 0, "sessions_with_pxhd": 0,
            "scores_history": [],
        })
        d["sessions"] += 1
        if success:
            d["successful"] += 1
            d["total_score"] += score
            if has_pxhd:
                d["sessions_with_pxhd"] += 1
            d["scores_history"].append(score)
            d["scores_history"] = d["scores_history"][-50:]  # cap
        else:
            d["failed"] += 1
        d["last_at"] = datetime.now().isoformat()


def get_domain_stats() -> List[dict]:
    """Stats de éxito por dominio: % sesiones con _pxhd, avg score, etc."""
    with _lock:
        out = []
        for dom, s in _DOMAIN_STATS.items():
            sessions = s.get("sessions", 0)
            succ = s.get("successful", 0)
            with_pxhd = s.get("sessions_with_pxhd", 0)
            scores = s.get("scores_history", [])
            out.append({
                "domain_id": dom,
                "sessions": sessions,
                "successful": succ,
                "failed": s.get("failed", 0),
                "success_rate_pct": round(100 * succ / sessions, 1) if sessions else 0,
                "pxhd_rate_pct": round(100 * with_pxhd / succ, 1) if succ else 0,
                "avg_score": round(s.get("total_score", 0) / succ, 1) if succ else 0,
                "min_score": min(scores) if scores else 0,
                "max_score": max(scores) if scores else 0,
                "last_at": s.get("last_at"),
            })
    out.sort(key=lambda x: x["sessions"], reverse=True)
    return out


def _pick_best_engine(preferred: str) -> str:
    """Resuelve el engine real a usar. Si `camoufox` está pedido pero no
    disponible, cae a `chromium`. Si es patchright, idem."""
    try:
        import tm_browser_engine as be
        # Probar si el engine está disponible
        available = getattr(be, "_AVAILABLE_ENGINES", None)
        if available and preferred in available:
            return preferred
        # camoufox → fallback patchright → chromium
        if preferred == "camoufox":
            for fb in ("patchright", "chromium"):
                if available is None or fb in available:
                    return fb
        if preferred == "patchright":
            return "chromium" if available is None or "chromium" in available else preferred
    except Exception:
        pass
    return preferred


def start_farming_async(domain_id: str = "us", event_url: Optional[str] = None,
                        duration_min: Optional[float] = None,
                        engine: Optional[str] = None,
                        broadcast: Optional[Callable] = None,
                        retry_on_low_score: bool = False,
                        pin_proxy: bool = True) -> dict:
    """Lanza sesión en thread background y devuelve inmediatamente."""
    cfg = get_config()
    duration_min = duration_min if duration_min is not None else cfg.get("default_duration_min", 3)
    engine = engine or cfg.get("default_engine", "camoufox")
    # Fallback inteligente si camoufox no disponible
    engine = _pick_best_engine(engine)
    headless = cfg.get("headless", True)
    ipm = cfg.get("default_interactions_per_min", 20)
    # Limit parallel
    max_par = cfg.get("max_parallel_sessions", 2)
    if len(_active_sessions) >= max_par:
        return {"ok": False, "error": f"máximo {max_par} sesiones paralelas ya corriendo"}
    # Cooldown per-domain
    if not _can_farm_domain(domain_id, cooldown_s=60):
        return {"ok": False, "error": f"cooldown activo para {domain_id} (espera 60s)"}
    t = threading.Thread(
        target=run_farming_session,
        kwargs={"domain_id": domain_id, "event_url": event_url,
                "duration_min": duration_min, "engine": engine,
                "headless": headless, "interactions_per_min": ipm,
                "broadcast": broadcast,
                "pin_proxy": pin_proxy,
                "retry_on_low_score": retry_on_low_score},
        daemon=True, name=f"px-farm-{domain_id}",
    )
    t.start()
    return {"ok": True, "started": True, "engine": engine,
            "duration_min": duration_min, "domain_id": domain_id,
            "active_sessions": len(_active_sessions) + 1,
            "pin_proxy": pin_proxy, "retry_on_low_score": retry_on_low_score}


# ═══════════════════════════════════════════
#  SCHEDULER (background auto-farming)
# ═══════════════════════════════════════════
def _scheduler_loop(stop_event, broadcast):
    while not stop_event.is_set():
        cfg = get_config()
        if not cfg.get("auto_farm_enabled"):
            stop_event.wait(30)
            continue
        try:
            # Decidir qué dominios necesitan refresh: los que tienen <target
            pool = _load_pool()
            _prune_expired(pool)
            by_domain = {}
            for s in pool.get("sessions", []):
                d = s.get("domain_id", "?")
                by_domain[d] = by_domain.get(d, 0) + 1
            import domain_manager
            selected = domain_manager.get_selected() if hasattr(domain_manager, "get_selected") else {"selected": ["us"]}
            target = cfg.get("pool_size_target", 5)
            # Para cada dominio seleccionado con pool < target, lanzar 1 sesión
            for dom in selected.get("selected", ["us"]):
                if by_domain.get(dom, 0) < target:
                    start_farming_async(domain_id=dom, broadcast=broadcast)
                    break  # sólo una sesión por tick
            _scheduler["last_tick"] = datetime.now().isoformat()
            _scheduler["ticks"] += 1
        except Exception:
            pass
        interval = int(cfg.get("auto_interval_min", 20))
        stop_event.wait(max(60, interval * 60))


def scheduler_status() -> dict:
    cfg = get_config()
    return {
        "running": _scheduler["running"],
        "enabled": cfg.get("auto_farm_enabled", False),
        "interval_min": cfg.get("auto_interval_min", 20),
        "pool_size_target": cfg.get("pool_size_target", 5),
        "last_tick": _scheduler["last_tick"],
        "ticks": _scheduler["ticks"],
    }


def start_scheduler(broadcast: Optional[Callable] = None) -> dict:
    if _scheduler["running"]:
        return {"ok": True, "already_running": True}
    ev = threading.Event()
    t = threading.Thread(target=_scheduler_loop, args=(ev, broadcast),
                         daemon=True, name="px-prefarm-scheduler")
    _scheduler.update({"running": True, "thread": t, "stop_event": ev})
    t.start()
    return {"ok": True, "started": True}


def stop_scheduler() -> dict:
    if not _scheduler["running"]:
        return {"ok": True, "not_running": True}
    ev = _scheduler.get("stop_event")
    if ev:
        ev.set()
    _scheduler.update({"running": False, "thread": None, "stop_event": None})
    return {"ok": True, "stopped": True}


# ═══════════════════════════════════════════
#  SCORE DE CALIDAD DE SESIONES
# ═══════════════════════════════════════════
def _score_session(session: dict) -> int:
    """Score 0-100 de la calidad de una sesión prefarmed.

    Ponderación:
      - 40 pts por tener _px*, _pxvid o _pxhd (los reales de PerimeterX)
      - 20 pts por tener visid_incap_* o _abck (Imperva/Akamai)
      - 15 pts por número de interacciones realizadas
      - 15 pts por duración cercana a la configurada
      - 10 pts por frescura (< 10 min = full score, decay lineal hasta TTL)
    """
    cookies = session.get("cookies", [])
    names = [c.get("name", "") for c in cookies]
    score = 0

    # PerimeterX core (40 pts)
    has_pxhd = any("_pxhd" in n for n in names)
    has_pxvid = any("_pxvid" in n for n in names)
    has_pxde = any("_pxde" in n for n in names)
    if has_pxhd: score += 15
    if has_pxvid: score += 15
    if has_pxde: score += 10

    # Imperva/Akamai (20 pts)
    has_incap = any(n.startswith("visid_incap_") for n in names)
    has_abck = any(n == "_abck" for n in names)
    has_bm = any(n.startswith("bm_") or n == "ak_bmsc" for n in names)
    if has_incap: score += 10
    if has_abck: score += 7
    if has_bm: score += 3

    # Interacciones (15 pts)
    inter = session.get("interactions_count", 0)
    score += min(15, inter // 4)

    # Duración (15 pts)
    dur_cfg = session.get("duration_min", 3)
    elapsed = session.get("elapsed_s", 0) / 60.0
    if elapsed >= dur_cfg * 0.8:
        score += 15
    else:
        score += int(15 * elapsed / max(1, dur_cfg))

    # Frescura (10 pts) - age hasta 30 min
    try:
        age = (datetime.now() - datetime.fromisoformat(session.get("created_at",""))).total_seconds()
        if age <= 600:
            score += 10
        else:
            score += max(0, int(10 * (1800 - age) / 1200))
    except Exception:
        pass

    return max(0, min(100, score))


def get_session_with_score(sid: str) -> Optional[dict]:
    pool = _load_pool()
    for s in pool.get("sessions", []):
        if s.get("id") == sid:
            c = dict(s)
            c["score"] = _score_session(s)
            return c
    return None


# ═══════════════════════════════════════════
#  INJECTION helpers
# ═══════════════════════════════════════════
def inject_into_context(playwright_context, domain_id: str,
                         min_score: int = 30) -> dict:
    """Inyecta cookies del pool en un contexto Playwright existente.

    Uso:
      ctx = browser.new_context()
      px_prefarm.inject_into_context(ctx, "us", min_score=50)
      page = ctx.new_page()
      page.goto("https://www.ticketmaster.com/...")
      # → ya lleva las cookies _px* del pre-farming
    """
    picked = pick_cookies(domain_id, min_px_cookies=1)
    if not picked.get("ok"):
        return {"ok": False, "error": picked.get("error", "no cookies")}
    cookies = picked.get("cookies", [])
    # Filter por score (devuelto por pick_cookies) — si lo conocemos
    try:
        # Adaptar cookies al formato Playwright
        playwright_cookies = []
        for c in cookies:
            pc = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            if c.get("expires"):
                pc["expires"] = c["expires"]
            if c.get("httpOnly") is not None:
                pc["httpOnly"] = bool(c["httpOnly"])
            if c.get("secure") is not None:
                pc["secure"] = bool(c["secure"])
            if c.get("sameSite"):
                pc["sameSite"] = c["sameSite"]
            playwright_cookies.append(pc)
        playwright_context.add_cookies(playwright_cookies)
        return {"ok": True, "injected": len(playwright_cookies),
                "session_id": picked.get("session_id"),
                "age_s": picked.get("age_s")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def get_cookies_for_requests(domain_id: str) -> dict:
    """Devuelve cookies en formato dict para requests/tls-client:
       {"name1": "value1", "name2": "value2"}
    """
    picked = pick_cookies(domain_id, min_px_cookies=1)
    if not picked.get("ok"):
        return {"ok": False, "error": picked.get("error")}
    cookies_dict = {c.get("name", ""): c.get("value", "") for c in picked.get("cookies", [])}
    return {
        "ok": True,
        "cookies": cookies_dict,
        "session_id": picked.get("session_id"),
        "count": len(cookies_dict),
        "age_s": picked.get("age_s"),
    }


# ═══════════════════════════════════════════
#  COOLDOWN PER DOMAIN
# ═══════════════════════════════════════════
_last_farm_at: Dict[str, float] = {}


def _can_farm_domain(domain_id: str, cooldown_s: int = 120) -> bool:
    last = _last_farm_at.get(domain_id, 0)
    return (time.time() - last) >= cooldown_s


def _mark_farm_domain(domain_id: str):
    _last_farm_at[domain_id] = time.time()


# ═══════════════════════════════════════════
#  SELF-TEST (sin navegador real)
# ═══════════════════════════════════════════
def self_test() -> dict:
    """Smoke test: valida imports, config, pool, scoring — sin lanzar browser."""
    started = time.time()
    results = {"ok": True, "checks": {}}

    # 1. Imports opcionales
    try:
        import tm_browser_engine  # noqa
        results["checks"]["tm_browser_engine"] = True
    except Exception as e:
        results["checks"]["tm_browser_engine"] = f"FAIL: {e}"
        results["ok"] = False

    # 2. Config válida
    cfg = get_config()
    results["checks"]["config_ok"] = bool(cfg.get("default_engine"))
    results["checks"]["engine"] = cfg.get("default_engine")

    # 3. Pool accesible
    stats = pool_stats()
    results["checks"]["pool_readable"] = "size" in stats

    # 4. Scoring funciona con sesión fake
    fake_session = {
        "cookies": [
            {"name": "_pxhd", "value": "x"},
            {"name": "_pxvid", "value": "y"},
            {"name": "_pxde", "value": "z"},
            {"name": "visid_incap_123", "value": "i"},
            {"name": "_abck", "value": "a"},
        ],
        "interactions_count": 40,
        "duration_min": 3,
        "elapsed_s": 170,
        "created_at": datetime.now().isoformat(),
    }
    fake_score = _score_session(fake_session)
    results["checks"]["score_fake_ideal"] = fake_score
    results["checks"]["score_formula_ok"] = fake_score >= 80  # debería ser ~90+

    # 5. Score sesión pobre
    poor_session = {"cookies": [], "interactions_count": 0,
                    "duration_min": 3, "elapsed_s": 10,
                    "created_at": datetime.now().isoformat()}
    results["checks"]["score_poor"] = _score_session(poor_session)

    # 6. TM_DOMAINS cobertura
    results["checks"]["tm_domains_count"] = len(_TM_DOMAINS)

    # 7. Patterns cobertura
    results["checks"]["px_patterns_count"] = len(PX_COOKIE_PATTERNS)

    # 8. Cooldown
    _mark_farm_domain("test")
    results["checks"]["cooldown_block"] = not _can_farm_domain("test", 60)
    results["checks"]["cooldown_allow_old"] = _can_farm_domain("test_other", 60)

    results["elapsed_ms"] = int((time.time() - started) * 1000)
    return results


# ═══════════════════════════════════════════
#  LIST POOL CON SCORE
# ═══════════════════════════════════════════
def list_pool_with_scores(limit: int = 20) -> List[dict]:
    """Como list_pool_entries pero añade score a cada entrada."""
    pool = _load_pool()
    _prune_expired(pool)
    out = []
    for s in pool.get("sessions", [])[:limit]:
        c = dict(s)
        c["score"] = _score_session(s)
        c["cookie_names"] = [ck.get("name", "") for ck in c.get("cookies", [])]
        c.pop("cookies", None)
        out.append(c)
    # Ordenar por score desc
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return out
