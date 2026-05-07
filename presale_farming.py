"""presale_farming.py — Automatiza captura de presale codes.

Ticketmaster presales:
- Verified Fan — registro explícito del artista (fan club)
- AMEX — propietarios tarjeta American Express
- VISA / Mastercard promos
- Artist fan club codes
- Chase cardholder
- Spotify / Amazon Music insiders

API:
  add_code(code, event_id, type, source)
  list_codes(event_id?, type?)
  find_code_for_event(event_id) → best match
  save_config(subreddits_to_scrape, alert_enabled)
  scrape_reddit_for_codes() → dict con codes encontrados
"""
import hashlib
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import config
import json_atomic

_CODES_KEY = "presale_codes.json"
_CONFIG_KEY = "presale_farming_config.json"
_lock = threading.Lock()

CODE_TYPES = (
    "verified_fan", "amex", "visa", "mastercard", "chase",
    "spotify", "amazon_music", "artist_fanclub",
    "citi_presale", "platinum_tickets", "generic",
)


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "enabled": False,
        "scrape_reddit": False,
        "reddit_subreddits": ["r/ticketmaster", "r/presale_codes"],
        "scrape_interval_hours": 6,
        "alert_on_new_code": True,
        "auto_try_on_flow": True,  # intenta todos los codes conocidos del evento
        # OAuth Reddit (opcional — solo si el scraping anónimo se bloquea)
        "reddit_oauth_enabled": False,
        "reddit_client_id": "",
        "reddit_client_secret": "",
        "reddit_username": "",
        "reddit_password": "",
        "reddit_oauth_token": "",      # cache del access_token
        "reddit_oauth_expires_at": 0,  # timestamp UTC
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def _load() -> dict:
    return json_atomic.load(config.ruta(_CODES_KEY), default={"codes": []})


def _save(d: dict):
    json_atomic.save(config.ruta(_CODES_KEY), d)


def add_code(code: str, event_id: str = "",
                code_type: str = "generic",
                source: str = "manual",
                artist: str = "",
                valid_until: Optional[str] = None,
                notes: str = "") -> dict:
    """Añade un presale code al repositorio."""
    if not code:
        return {"ok": False, "error": "code vacío"}

    code_upper = code.strip().upper()
    if code_type not in CODE_TYPES:
        code_type = "generic"

    code_id = "pc_" + hashlib.md5(f"{code_upper}{event_id}".encode()).hexdigest()[:10]

    entry = {
        "id": code_id,
        "code": code_upper,
        "event_id": event_id,
        "type": code_type,
        "source": source[:100],
        "artist": artist,
        "added_at": datetime.now().isoformat(),
        "valid_until": valid_until,
        "notes": notes[:500],
        "uses": 0,
        "successes": 0,
        "last_tried_at": None,
        "status": "untested",  # untested | valid | invalid | expired
    }

    with _lock:
        d = _load()
        # Dedupe
        for existing in d["codes"]:
            if existing["id"] == code_id:
                return {"ok": True, "dedupe": True, "existing": existing}
        d["codes"].insert(0, entry)
        d["codes"] = d["codes"][:2000]
        _save(d)

    return {"ok": True, "added": True, "code": entry}


def list_codes(event_id: Optional[str] = None,
                 code_type: Optional[str] = None,
                 status: Optional[str] = None,
                 limit: int = 200) -> List[dict]:
    d = _load()
    items = d.get("codes", [])
    if event_id:
        items = [c for c in items if c.get("event_id") == event_id]
    if code_type:
        items = [c for c in items if c.get("type") == code_type]
    if status:
        items = [c for c in items if c.get("status") == status]
    return items[:limit]


def find_code_for_event(event_id: str,
                          artist: Optional[str] = None) -> Optional[dict]:
    """Mejor match por (event_id exacto > artist > generic)."""
    d = _load()
    codes = d.get("codes", [])
    now = datetime.now()

    # Priority 1: event_id exact match + valid status
    exact = [c for c in codes
              if c.get("event_id") == event_id and c.get("status") != "invalid"]
    if exact:
        # preferir los validados, luego untested
        exact.sort(key=lambda c: (c.get("status") != "valid",
                                     -c.get("successes", 0)))
        return exact[0]

    # Priority 2: mismo artista
    if artist:
        by_artist = [c for c in codes
                      if c.get("artist", "").lower() == artist.lower()
                      and c.get("status") != "invalid"]
        if by_artist:
            return by_artist[0]

    # Priority 3: genéricos nunca usados
    untested = [c for c in codes
                 if c.get("type") == "generic" and c.get("status") == "untested"]
    if untested:
        return untested[0]

    return None


def record_try(code_id: str, success: bool, message: str = "") -> dict:
    """Registra un intento de usar el code."""
    with _lock:
        d = _load()
        for c in d["codes"]:
            if c["id"] == code_id:
                c["uses"] = c.get("uses", 0) + 1
                if success:
                    c["successes"] = c.get("successes", 0) + 1
                    c["status"] = "valid"
                else:
                    if c.get("uses", 0) >= 3:
                        c["status"] = "invalid"
                    c["last_error"] = message[:200]
                c["last_tried_at"] = datetime.now().isoformat()
                _save(d)
                return {"ok": True, **c}
    return {"ok": False, "error": "code no encontrado"}


def delete_code(code_id: str) -> dict:
    with _lock:
        d = _load()
        before = len(d["codes"])
        d["codes"] = [c for c in d["codes"] if c["id"] != code_id]
        _save(d)
        return {"ok": True, "removed": before > len(d["codes"])}


def extract_codes_from_text(text: str) -> List[str]:
    """Extrae códigos que parecen presale codes de un texto arbitrario."""
    # Patrones típicos: 4-10 chars alphanumeric, mayúsculas
    patterns = [
        r'\b[A-Z]{4,8}\d{2,4}\b',       # ARTIST2026
        r'\b[A-Z0-9]{6,10}\b',             # TICKET123
        r'(?i)presale[:\s]+([A-Z0-9]{4,12})',  # "presale: XYZ123"
        r'(?i)code[:\s]+([A-Z0-9]{4,12})',
    ]
    found = set()
    for pat in patterns:
        for m in re.finditer(pat, text):
            code = (m.group(1) if m.lastindex else m.group(0)).upper()
            if 4 <= len(code) <= 12:
                found.add(code)
    # Filtrar palabras comunes que no son codes
    stopwords = {"PRESALE", "TICKET", "TICKETS", "CONCERT", "EVENT", "ACCESS",
                  "VERIFIED", "FANCLUB", "MEMBER", "SIGNUP", "VIP", "2026", "2025"}
    return [c for c in found if c not in stopwords]


def import_codes_from_text(text: str, event_id: str = "",
                              source: str = "manual_paste") -> dict:
    """Extrae y añade todos los codes de un texto (útil para pegar reddit posts)."""
    codes = extract_codes_from_text(text)
    added = []
    for c in codes:
        r = add_code(c, event_id=event_id, source=source, code_type="generic")
        if r.get("added"):
            added.append(c)
    return {
        "ok": True,
        "extracted": len(codes),
        "added": len(added),
        "duplicates": len(codes) - len(added),
        "codes_added": added,
    }


def get_stats() -> dict:
    d = _load()
    codes = d.get("codes", [])
    by_status = {}
    by_type = {}
    for c in codes:
        by_status[c.get("status", "?")] = by_status.get(c.get("status", "?"), 0) + 1
        by_type[c.get("type", "?")] = by_type.get(c.get("type", "?"), 0) + 1

    valid_codes = sum(1 for c in codes if c.get("status") == "valid")
    total_uses = sum(c.get("uses", 0) for c in codes)
    total_success = sum(c.get("successes", 0) for c in codes)

    return {
        "total_codes": len(codes),
        "valid_codes": valid_codes,
        "by_status": by_status,
        "by_type": by_type,
        "total_uses": total_uses,
        "total_successes": total_success,
        "success_rate_pct": round(100 * total_success / max(1, total_uses), 1),
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    # Limpieza inicial — borra residuos de runs anteriores del self_test
    for c in list_codes(event_id="test_event"):
        delete_code(c.get("id"))

    # Add
    r = add_code("TEST2026", event_id="test_event",
                   code_type="verified_fan", artist="Test Artist")
    results["checks"]["add_ok"] = r.get("ok", False)
    code_id = r.get("code", {}).get("id")

    # List
    codes = list_codes(event_id="test_event")
    results["checks"]["list_returns"] = len(codes) >= 1

    # Find
    found = find_code_for_event("test_event")
    results["checks"]["find_exact_match"] = bool(found and found.get("code") == "TEST2026")

    # Record
    if code_id:
        rt = record_try(code_id, success=True)
        results["checks"]["record_try_ok"] = rt.get("ok", False)

    # Extract from text
    sample = "Taylor Swift presale: SWIFT2026 and AMEX code AMEXTS or verified fan VF123ABC"
    codes = extract_codes_from_text(sample)
    results["checks"]["extract_codes_count"] = len(codes)
    results["checks"]["extract_has_swift"] = "SWIFT2026" in codes

    import_r = import_codes_from_text(sample, event_id="test_event", source="self_test")
    results["checks"]["import_from_text"] = import_r.get("added", 0) > 0

    # Stats
    s = get_stats()
    results["checks"]["stats_accessible"] = "total_codes" in s

    # Cleanup completo — borra TODOS los codes con event_id="test_event"
    for c in list_codes(event_id="test_event"):
        delete_code(c.get("id"))

    return results


def get_info() -> dict:
    return {
        "module": "presale_farming",
        "supported_code_types": list(CODE_TYPES),
        "stats": get_stats(),
        "reddit_scraper": True,
        "scheduler": _scrape_state.get("running", False),
    }


# ═══════════════════════════════════════════════════════════════
#  Tier-based try pipeline
#  Prioridad: event_id exact → artist → fan_club → AMEX/VISA → genéricos
# ═══════════════════════════════════════════════════════════════
TIER_ORDER = (
    ("verified_fan", 1),
    ("artist_fanclub", 1),
    ("amex", 2),
    ("citi_presale", 2),
    ("chase", 2),
    ("visa", 3),
    ("mastercard", 3),
    ("spotify", 3),
    ("amazon_music", 3),
    ("platinum_tickets", 4),
    ("generic", 5),
)


def codes_for_event_ranked(event_id: str,
                              artist: Optional[str] = None,
                              skip_invalid: bool = True) -> List[dict]:
    """Devuelve TODOS los codes aplicables ordenados por tier+success rate.

    Útil para iterar tiers (try → next on fail) en el flow de compra.
    """
    d = _load()
    codes = list(d.get("codes", []))
    out: List[dict] = []
    artist_low = (artist or "").lower().strip()
    tier_lookup = {t: n for t, n in TIER_ORDER}

    for c in codes:
        if skip_invalid and c.get("status") == "invalid":
            continue
        score = 0
        # event_id exacto = max prioridad
        if c.get("event_id") == event_id:
            score += 100
        # artista coincidente
        if artist_low and c.get("artist", "").lower() == artist_low:
            score += 50
        # tier (lower = better)
        tier = tier_lookup.get(c.get("type"), 5)
        score += (10 - tier)
        # validados que ya funcionaron > untested
        if c.get("status") == "valid":
            score += 30
        # success rate
        uses = c.get("uses", 0)
        if uses > 0:
            score += int(20 * (c.get("successes", 0) / uses))
        c["_rank_score"] = score
        c["_tier"] = tier
        out.append(c)

    out.sort(key=lambda c: c.get("_rank_score", 0), reverse=True)
    return out


def try_codes_for_event(event_id: str,
                          tester_callback,
                          artist: Optional[str] = None,
                          max_tries: int = 5) -> dict:
    """Itera codes en orden de tier llamando a `tester_callback(code) → bool`.

    Devuelve el primero que resulta válido, o reporta cuántos falló.
    """
    ranked = codes_for_event_ranked(event_id, artist)
    if not ranked:
        return {"ok": False, "error": "no_codes_available", "tried": 0}

    tried = []
    for c in ranked[:max_tries]:
        try:
            ok = bool(tester_callback(c["code"]))
        except Exception as e:
            ok = False
            record_try(c["id"], False, message=str(e)[:200])
            tried.append({**c, "result": "error", "error": str(e)[:120]})
            continue
        record_try(c["id"], ok, message="" if ok else "rejected_by_TM")
        tried.append({**c, "result": "valid" if ok else "invalid"})
        if ok:
            return {
                "ok": True,
                "valid_code": c["code"],
                "code_id": c["id"],
                "tier": c.get("_tier"),
                "tried_count": len(tried),
                "tried": tried,
            }
    return {"ok": False, "error": "all_failed",
             "tried_count": len(tried), "tried": tried}


# ═══════════════════════════════════════════════════════════════
#  Reddit scraper — sin auth (usa .json público)
# ═══════════════════════════════════════════════════════════════
DEFAULT_SUBREDDITS = [
    "ticketmaster", "presale_codes", "Concerts", "TaylorSwift",
    "kpop", "Music", "popheads",
]

_USER_AGENT = "TM-Presale-Farmer/1.0 (educational; +https://github.com/local)"


def _get_reddit_oauth_token(force_refresh: bool = False) -> Optional[str]:
    """Devuelve un access_token Reddit OAuth válido (caché en config).

    Reddit OAuth grants 600 req/10min vs 60 anónimo. Renueva si expira.
    """
    try:
        import requests
    except ImportError:
        return None
    cfg = get_config()
    if not cfg.get("reddit_oauth_enabled"):
        return None
    cid = cfg.get("reddit_client_id", "").strip()
    csec = cfg.get("reddit_client_secret", "").strip()
    if not cid or not csec:
        return None
    now = time.time()
    cached = cfg.get("reddit_oauth_token", "")
    expires = float(cfg.get("reddit_oauth_expires_at") or 0)
    if cached and not force_refresh and now < expires - 60:
        return cached
    # Pedir nuevo token
    user = cfg.get("reddit_username", "").strip()
    pw = cfg.get("reddit_password", "").strip()
    if user and pw:
        # Script app — password grant
        data = {"grant_type": "password", "username": user, "password": pw}
    else:
        # Application-only OAuth
        data = {"grant_type": "client_credentials"}
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            data=data,
            auth=(cid, csec),
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        tok = r.json()
        access = tok.get("access_token")
        ttl = int(tok.get("expires_in") or 3600)
        if access:
            save_config({
                "reddit_oauth_token": access,
                "reddit_oauth_expires_at": now + ttl,
            })
            return access
    except Exception:
        return None
    return None


def scrape_reddit_for_codes(subreddits: Optional[List[str]] = None,
                             limit_per_sub: int = 25,
                             event_id: str = "",
                             auto_import: bool = True,
                             timeout_s: int = 12) -> dict:
    """Scrape Reddit posts buscando presale codes.

    Para cada sub: GET https://old.reddit.com/r/{sub}/new.json?limit=N
    Recoge titles + bodies, pasa por extract_codes_from_text(), dedupe e
    importa (si auto_import) marcando source=reddit:{sub}.
    """
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests no instalado"}

    subs = subreddits or DEFAULT_SUBREDDITS
    started = time.time()
    results = []
    total_codes = set()
    imported_total = 0
    posts_total = 0
    errors = []

    # OAuth opcional — si configurado, usa endpoint oauth.reddit.com
    token = _get_reddit_oauth_token()
    if token:
        base_host = "https://oauth.reddit.com"
        headers = {"User-Agent": _USER_AGENT,
                   "Authorization": f"Bearer {token}"}
    else:
        base_host = "https://old.reddit.com"
        headers = {"User-Agent": _USER_AGENT}

    for sub in subs:
        sub_clean = sub.lstrip("r/").strip().lstrip("/")
        if not sub_clean:
            continue
        url = f"{base_host}/r/{sub_clean}/new.json?limit={limit_per_sub}"
        try:
            r = requests.get(url, headers=headers, timeout=timeout_s)
            if r.status_code != 200:
                errors.append({"sub": sub_clean, "status": r.status_code})
                continue
            data = r.json()
            posts = (data.get("data") or {}).get("children") or []
            posts_total += len(posts)
            sub_codes = set()
            for p in posts:
                d = p.get("data", {})
                title = d.get("title", "") or ""
                selftext = d.get("selftext", "") or ""
                # Filtra posts irrelevantes
                low = (title + " " + selftext).lower()
                if not any(k in low for k in
                            ("presale", "code", "código", "verified",
                             "fan club", "amex", "visa")):
                    continue
                codes = extract_codes_from_text(title + "\n" + selftext)
                for c in codes:
                    sub_codes.add(c)
                    total_codes.add(c)
            sub_imported = 0
            if auto_import:
                for c in sub_codes:
                    res = add_code(c, event_id=event_id,
                                     code_type="generic",
                                     source=f"reddit:{sub_clean}")
                    if res.get("added"):
                        sub_imported += 1
                        imported_total += 1
            results.append({"sub": sub_clean,
                              "posts_scanned": len(posts),
                              "codes_found": len(sub_codes),
                              "imported": sub_imported,
                              "codes": sorted(sub_codes)})
        except Exception as e:
            errors.append({"sub": sub_clean,
                              "error": f"{type(e).__name__}: {str(e)[:100]}"})

    return {
        "ok": True,
        "subs_scraped": len(results),
        "posts_total": posts_total,
        "unique_codes": len(total_codes),
        "imported": imported_total,
        "results": results,
        "errors": errors,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


# ═══════════════════════════════════════════════════════════════
#  Scheduler periódico (background thread)
# ═══════════════════════════════════════════════════════════════
_scrape_state = {
    "running": False,
    "thread": None,
    "stop_event": None,
    "last_run": "",
    "last_imported": 0,
    "runs": 0,
    "interval_h": 6,
}


def _scrape_loop(stop_evt: threading.Event, interval_h: int):
    while not stop_evt.is_set():
        try:
            cfg = get_config()
            if cfg.get("scrape_reddit"):
                res = scrape_reddit_for_codes(
                    subreddits=cfg.get("reddit_subreddits"),
                    limit_per_sub=25,
                    auto_import=True)
                _scrape_state["last_run"] = datetime.now().isoformat()
                _scrape_state["last_imported"] = int(res.get("imported", 0))
                _scrape_state["runs"] += 1
        except Exception:
            pass
        # Sleep en chunks
        secs = max(60, int(interval_h * 3600))
        for _ in range(secs):
            if stop_evt.is_set():
                break
            time.sleep(1)


def start_scraper(interval_h: Optional[int] = None) -> dict:
    if _scrape_state.get("running"):
        return {"ok": True, "status": "already_running"}
    cfg = get_config()
    h = int(interval_h or cfg.get("scrape_interval_hours") or 6)
    h = max(1, min(168, h))
    evt = threading.Event()
    th = threading.Thread(target=_scrape_loop, args=(evt, h),
                           daemon=True, name="presale_scraper")
    _scrape_state.update({
        "running": True, "thread": th, "stop_event": evt,
        "interval_h": h, "started_at": datetime.now().isoformat(),
        "runs": 0,
    })
    th.start()
    save_config({"scrape_reddit": True, "scrape_interval_hours": h})
    return {"ok": True, "status": "started", "interval_h": h}


def stop_scraper() -> dict:
    if not _scrape_state.get("running"):
        return {"ok": True, "status": "not_running"}
    evt = _scrape_state.get("stop_event")
    if evt:
        evt.set()
    _scrape_state["running"] = False
    save_config({"scrape_reddit": False})
    return {"ok": True, "status": "stopped"}


def scraper_status() -> dict:
    return {
        "running": _scrape_state.get("running", False),
        "interval_h": _scrape_state.get("interval_h", 6),
        "started_at": _scrape_state.get("started_at", ""),
        "last_run": _scrape_state.get("last_run", ""),
        "last_imported": _scrape_state.get("last_imported", 0),
        "runs": _scrape_state.get("runs", 0),
    }
