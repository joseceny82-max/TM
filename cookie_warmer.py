"""cookie_warmer.py — Cookie warming sintético + AcpAT token pool.

Funciones:
- warm_cookies(): hace 3-5 peticiones GET a TM simulando navegación natural
  y captura cookies _pxhd, _pxvid, visid_incap_*, AcpAT si aparece, etc.
- Pool de tokens AcpAT (Queue-it) con TTL de 30 min — captura + inyección.
- Estadísticas del pool (size, oldest, newest, dominios).

Inspirado en técnicas Multilogin 2025/2026 y guías de bypass Queue-it.
"""
import time
import random
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import config
import json_atomic


POOL_PATH_KEY = "acpat_pool.json"
WARM_LOG_KEY = "cookie_warm_log.json"
ACPAT_TTL_SECONDS = 30 * 60  # 30 min

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

_DOMAIN_MAP = {
    "us": "https://www.ticketmaster.com",
    "es": "https://www.ticketmaster.es",
    "uk": "https://www.ticketmaster.co.uk",
    "de": "https://www.ticketmaster.de",
    "fr": "https://www.ticketmaster.fr",
    "it": "https://www.ticketmaster.it",
    "au": "https://www.ticketmaster.com.au",
    "ca": "https://www.ticketmaster.ca",
    "mx": "https://www.ticketmaster.com.mx",
}

_lock = threading.Lock()


# ───────────────────────── Warming ─────────────────────────
def _path_pool():
    return config.ruta(POOL_PATH_KEY)


def _path_log():
    return config.ruta(WARM_LOG_KEY)


def _log_warm(entry: dict):
    d = json_atomic.load(_path_log(), default={"log": []})
    d["log"].insert(0, entry)
    d["log"] = d["log"][:50]  # limitar a últimos 50
    json_atomic.save(_path_log(), d)


def warm_cookies(domain_id: str = "us", pages: int = 4, timeout: float = 10.0,
                 client: str = "auto") -> dict:
    """Realiza warming sintético: GETs a home + categorías + un evento aleatorio.

    `client`:
      - 'curl_cffi' → impersonate Chrome (JA3/JA4 + HTTP/2 + TLS Chrome real)
      - 'requests'  → fallback HTTP/1.1 stdlib
      - 'auto'      → curl_cffi si disponible, sino requests

    Devuelve dict con: success, elapsed_ms, cookies_count, cookie_names, base_url, client_used.
    """
    base = _DOMAIN_MAP.get(domain_id, _DOMAIN_MAP["us"])
    ua = random.choice(_USER_AGENTS)
    paths = ["/", "/discover/concerts", "/discover/sports", "/discover/arts-theater"]
    random.shuffle(paths)
    paths = paths[:max(1, min(pages, len(paths)))]

    headers_common = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }

    use_cffi = client in ("curl_cffi", "auto")
    cffi_session = None
    if use_cffi:
        try:
            from curl_cffi import requests as cffi_requests
            cffi_session = cffi_requests.Session(impersonate="chrome120")
            cffi_session.headers.update(headers_common)
        except Exception:
            cffi_session = None

    if cffi_session is None and client == "curl_cffi":
        return {"success": False, "error": "curl_cffi unavailable",
                "domain_id": domain_id, "client_used": None}

    if cffi_session is None:
        import requests
        rq_session = requests.Session()
        rq_session.headers.update(headers_common)
        client_used = "requests"
    else:
        rq_session = None
        client_used = "curl_cffi"

    started = time.time()
    status_codes = []
    last_err = None
    cookies_dict: dict = {}
    for p in paths:
        try:
            if cffi_session is not None:
                resp = cffi_session.get(base + p, timeout=timeout, allow_redirects=True)
                cookies_dict.update({k: v for k, v in resp.cookies.items()})
            else:
                resp = rq_session.get(base + p, timeout=timeout, allow_redirects=True)
            status_codes.append(resp.status_code)
            time.sleep(random.uniform(0.8, 1.8))
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            status_codes.append(0)
            break

    elapsed_ms = int((time.time() - started) * 1000)
    if cffi_session is not None:
        # ya acumulamos en cookies_dict
        pass
    else:
        cookies_dict = {c.name: c.value for c in rq_session.cookies}
    cookie_names = list(cookies_dict.keys())

    result = {
        "success": bool(cookies_dict) and any(200 <= s < 400 for s in status_codes),
        "domain_id": domain_id,
        "base_url": base,
        "user_agent": ua,
        "client_used": client_used,
        "pages_visited": len(status_codes),
        "status_codes": status_codes,
        "elapsed_ms": elapsed_ms,
        "cookies_count": len(cookie_names),
        "cookie_names": cookie_names,
        "timestamp": datetime.now().isoformat(),
        "error": last_err,
    }
    _log_warm(result)
    return result


def get_warm_log(limit: int = 20) -> List[dict]:
    d = json_atomic.load(_path_log(), default={"log": []})
    return d.get("log", [])[:limit]


def check_stock(url: str, timeout: float = 6.0) -> dict:
    """Quick stock probe vía HTTP — retorna {stock_found, hint, status}.

    Usa HEAD primero (rápido) y, si responde 405/no-info, fallback a GET ligero.
    Heurísticas:
      - status 302→/queue/* o /sold-out o redirect a /event/* genérico → SIN stock
      - status 200 + body contiene "comprar"/"buy now"/"select tickets" → CON stock
      - status 200 + body contiene "sold out"/"agotado"/"unavailable" → SIN stock
      - status 503/429 → queue activa, podría haber stock detrás
    """
    if not url:
        return {"stock_found": False, "hint": "no_url"}
    try:
        import requests
    except ImportError:
        return {"stock_found": False, "hint": "requests_missing"}
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/"
                       "537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    try:
        # HEAD primero
        r = requests.head(url, headers=headers, timeout=timeout,
                           allow_redirects=True)
        final_url = (r.url or "").lower()
        # Patrones de redirect "no stock"
        if any(m in final_url for m in ("/sold-out", "/queue/", "/notfound",
                                          "/error", "queue-it.net")):
            return {"stock_found": False, "hint": "redirect_blocked",
                     "status": r.status_code, "final_url": final_url}
        if r.status_code in (404, 410, 451):
            return {"stock_found": False, "hint": "event_unavailable",
                     "status": r.status_code}
        if r.status_code in (503, 429):
            return {"stock_found": False, "hint": "queue_or_rate_limit",
                     "status": r.status_code, "queue_active": True}
        # Si status OK, hacer GET ligero para inspeccionar body
        if r.status_code in (200, 301, 302):
            try:
                rg = requests.get(url, headers=headers, timeout=timeout,
                                    allow_redirects=True, stream=True)
                # Lee primeros 60KB (suficiente para meta tags + first ticket panel)
                chunk = rg.raw.read(60_000) if hasattr(rg, "raw") else b""
                if not chunk:
                    chunk = (rg.text or "").encode("utf-8", errors="replace")[:60_000]
                body = chunk.decode("utf-8", errors="replace").lower()
                rg.close()
                # Negativos primero
                if any(m in body for m in ("sold out", "agotado", "ausverkauft",
                                              "no available tickets",
                                              "no hay entradas", "épuisé",
                                              "no tickets available",
                                              "currently unavailable")):
                    return {"stock_found": False, "hint": "sold_out_marker",
                             "status": rg.status_code,
                             "final_url": (rg.url or "").lower()}
                # Positivos
                if any(m in body for m in ("select your tickets",
                                              "elige tus entradas",
                                              "comprar entradas",
                                              "buy tickets", "buy now",
                                              "seatmap", "select-seats",
                                              "data-testid=\"ticket-")):
                    return {"stock_found": True, "hint": "buy_panel_detected",
                             "status": rg.status_code,
                             "final_url": (rg.url or "").lower()}
                # Inconcluso
                return {"stock_found": False, "hint": "no_clear_marker",
                         "status": rg.status_code,
                         "final_url": (rg.url or "").lower()}
            except Exception as e:
                return {"stock_found": False, "hint": "get_error",
                         "error": f"{type(e).__name__}: {str(e)[:120]}"}
        return {"stock_found": False, "hint": "unexpected_status",
                 "status": r.status_code}
    except requests.exceptions.Timeout:
        return {"stock_found": False, "hint": "timeout"}
    except Exception as e:
        return {"stock_found": False, "hint": "request_error",
                 "error": f"{type(e).__name__}: {str(e)[:120]}"}


def warm_with_akamai_sensor(domain_id: str = "es", page_url: str = "",
                            user_agent: str = "") -> dict:
    """Warming + intento de obtener `_abck`/`bm_sz` válidos vía hyper_sdk_chain.

    Si `hyper_sdk_chain.mode='live'` y hay api_key configurada (Hyper Solutions /
    TakionAPI), se obtiene sensor_data REAL. Si está en `simulate`, se obtienen
    cookies mock para test del orquestador.

    Devuelve: {success, cookies_dict, sensor_length?, vendor?, mode?, error?}.
    """
    try:
        import hyper_sdk_chain
    except Exception as e:
        return {"success": False, "error": f"hyper_sdk_chain not available: {e}"}

    base = _DOMAIN_MAP.get(domain_id, _DOMAIN_MAP["us"])
    payload = {
        "page_url": page_url or base + "/",
        "user_agent": user_agent or random.choice(_USER_AGENTS),
        "domain": base.replace("https://", "").replace("http://", ""),
    }
    r = hyper_sdk_chain.solve_with_chain(target="akamai", payload_data=payload)
    if not r.get("ok"):
        return {"success": False, "error": r.get("error", "chain_failed"),
                "attempts": r.get("attempts", [])}

    final = r.get("result", {})
    return {
        "success": True,
        "vendor": r.get("success_vendor") or final.get("vendor"),
        "mode": final.get("mode"),
        "cookies_dict": final.get("cookies", {}),
        "sensor_length": final.get("sensor_length"),
        "user_agent": final.get("user_agent"),
        "elapsed_ms": r.get("total_elapsed_ms"),
    }


# ───────────────────────── AcpAT pool ─────────────────────────
def _load_pool() -> dict:
    return json_atomic.load(_path_pool(), default={"tokens": []})


def _save_pool(d: dict):
    json_atomic.save(_path_pool(), d)


def _prune_expired(pool: dict) -> int:
    """Elimina tokens caducados. Retorna nº eliminados."""
    now = datetime.now()
    before = len(pool.get("tokens", []))
    fresh = []
    for t in pool.get("tokens", []):
        try:
            captured = datetime.fromisoformat(t.get("captured_at", ""))
        except Exception:
            continue
        if now - captured < timedelta(seconds=ACPAT_TTL_SECONDS):
            fresh.append(t)
    pool["tokens"] = fresh
    return before - len(fresh)


def capture_acpat(token: str, domain_id: str = "us", event_id: str = "",
                  source_worker: str = "", meta: Optional[dict] = None) -> dict:
    """Captura un token AcpAT al pool (cuando un worker pasa la cola)."""
    if not token or len(token) < 10:
        return {"ok": False, "error": "token inválido"}
    with _lock:
        pool = _load_pool()
        _prune_expired(pool)
        entry = {
            "token": token,
            "domain_id": domain_id,
            "event_id": event_id,
            "source_worker": source_worker,
            "captured_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(seconds=ACPAT_TTL_SECONDS)).isoformat(),
            "used_count": 0,
            "meta": meta or {},
        }
        pool["tokens"].insert(0, entry)
        pool["tokens"] = pool["tokens"][:200]  # cap a 200
        _save_pool(pool)
    return {"ok": True, "entry": entry}


def inject_acpat(domain_id: str = "us", event_id: str = "") -> dict:
    """Extrae un token fresco del pool para inyectarlo en un worker.

    Prefiere tokens con menor used_count y más recientes.
    """
    with _lock:
        pool = _load_pool()
        _prune_expired(pool)
        # Soporta tokens con clave 'domain_id' (nuevos) o 'domain' (legacy/placeholder)
        candidates = [t for t in pool["tokens"]
                      if t.get("domain_id", t.get("domain")) == domain_id]
        if event_id:
            # si hay event_id, preferir tokens del mismo evento
            same = [t for t in candidates if t.get("event_id") == event_id]
            if same:
                candidates = same
        if not candidates:
            return {"ok": False, "error": "sin tokens disponibles"}
        # elegir menos usado, más reciente
        candidates.sort(key=lambda t: (t.get("used_count", 0), -1 * _to_ts(t.get("captured_at", ""))))
        pick = candidates[0]
        pick["used_count"] = pick.get("used_count", 0) + 1
        pick["last_used_at"] = datetime.now().isoformat()
        _save_pool(pool)
        return {"ok": True, "token": pick["token"], "entry": pick}


def _to_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0


def invalidate_token(token: str) -> dict:
    with _lock:
        pool = _load_pool()
        before = len(pool["tokens"])
        pool["tokens"] = [t for t in pool["tokens"] if t.get("token") != token]
        _save_pool(pool)
    return {"ok": True, "removed": before - len(pool["tokens"])}


def clear_pool() -> dict:
    with _lock:
        _save_pool({"tokens": []})
    return {"ok": True}


def pool_stats() -> dict:
    pool = _load_pool()
    _prune_expired(pool)
    _save_pool(pool)
    tokens = pool.get("tokens", [])
    by_domain = {}
    for t in tokens:
        d = t.get("domain_id", "?")
        by_domain[d] = by_domain.get(d, 0) + 1
    oldest = None
    newest = None
    for t in tokens:
        ts = t.get("captured_at")
        if not ts:
            continue
        if oldest is None or ts < oldest:
            oldest = ts
        if newest is None or ts > newest:
            newest = ts
    return {
        "size": len(tokens),
        "ttl_seconds": ACPAT_TTL_SECONDS,
        "by_domain": by_domain,
        "oldest": oldest,
        "newest": newest,
        "total_uses": sum(t.get("used_count", 0) for t in tokens),
    }


def list_tokens(limit: int = 50, include_value: bool = False) -> List[dict]:
    """Lista tokens del pool (enmascarando el valor por defecto)."""
    pool = _load_pool()
    _prune_expired(pool)
    _save_pool(pool)
    out = []
    for t in pool.get("tokens", [])[:limit]:
        item = {k: v for k, v in t.items() if k != "token"}
        tok = t.get("token", "")
        item["token_preview"] = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else tok
        if include_value:
            item["token"] = tok
        out.append(item)
    return out
