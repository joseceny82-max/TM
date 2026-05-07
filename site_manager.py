"""site_manager.py — Gestion de sitios de venta (TM oficial, revendedores, etc.)."""
import uuid
from datetime import datetime
import config
import json_atomic


DEFAULT_SITES = [
    # OFICIAL TICKETMASTER
    {"id": "tm_us",    "name": "Ticketmaster US",       "url": "https://www.ticketmaster.com",     "category": "Oficial", "region": "NA", "logo": "🎫", "enabled": True,  "has_queue": True,  "priority": 10, "notes": "Portal principal USA"},
    {"id": "tm_es",    "name": "Ticketmaster Espana",   "url": "https://www.ticketmaster.es",      "category": "Oficial", "region": "EU", "logo": "🎫", "enabled": True,  "has_queue": True,  "priority": 10, "notes": "Portal oficial Espana"},
    {"id": "tm_uk",    "name": "Ticketmaster UK",       "url": "https://www.ticketmaster.co.uk",   "category": "Oficial", "region": "EU", "logo": "🎫", "enabled": True,  "has_queue": True,  "priority": 10, "notes": "Portal oficial UK"},
    {"id": "tm_de",    "name": "Ticketmaster Alemania", "url": "https://www.ticketmaster.de",      "category": "Oficial", "region": "EU", "logo": "🎫", "enabled": True,  "has_queue": True,  "priority": 9,  "notes": "Portal oficial Alemania"},
    {"id": "tm_fr",    "name": "Ticketmaster Francia",  "url": "https://www.ticketmaster.fr",      "category": "Oficial", "region": "EU", "logo": "🎫", "enabled": True,  "has_queue": True,  "priority": 9,  "notes": "Portal oficial Francia"},
    {"id": "tm_it",    "name": "Ticketmaster Italia",   "url": "https://www.ticketmaster.it",      "category": "Oficial", "region": "EU", "logo": "🎫", "enabled": True,  "has_queue": True,  "priority": 9,  "notes": "Portal oficial Italia"},
    {"id": "tm_au",    "name": "Ticketmaster Australia","url": "https://www.ticketmaster.com.au",  "category": "Oficial", "region": "OC", "logo": "🎫", "enabled": True,  "has_queue": True,  "priority": 9,  "notes": "Portal oficial Australia"},
    {"id": "tm_ca",    "name": "Ticketmaster Canada",   "url": "https://www.ticketmaster.ca",      "category": "Oficial", "region": "NA", "logo": "🎫", "enabled": True,  "has_queue": True,  "priority": 9,  "notes": "Portal oficial Canada"},

    # PLATAFORMAS VERIFICADAS TM
    {"id": "livenation",  "name": "Live Nation",       "url": "https://www.livenation.com",       "category": "Grupo TM",     "region": "Global", "logo": "🎵", "enabled": True,  "has_queue": True,  "priority": 8, "notes": "Propietario de Ticketmaster"},
    {"id": "tm_verified", "name": "TM Verified Fan",   "url": "https://verified.ticketmaster.com","category": "Oficial",      "region": "NA",     "logo": "✅", "enabled": True,  "has_queue": True,  "priority": 10, "notes": "Sistema de fans verificados"},
    {"id": "tm_resale",   "name": "TM Resale (oficial)","url": "https://www.ticketmaster.com/resale","category": "Reventa oficial","region": "NA","logo": "♻", "enabled": True, "has_queue": False, "priority": 7, "notes": "Reventa oficial entre fans"},

    # REVENDEDORES (solo referencia / scraping para precios)
    {"id": "stubhub",  "name": "StubHub",              "url": "https://www.stubhub.com",          "category": "Revendedor", "region": "Global", "logo": "💰", "enabled": False, "has_queue": False, "priority": 5, "notes": "Reventa principal"},
    {"id": "viagogo",  "name": "Viagogo",              "url": "https://www.viagogo.com",          "category": "Revendedor", "region": "Global", "logo": "💰", "enabled": False, "has_queue": False, "priority": 4, "notes": "Reventa global"},
    {"id": "seatgeek", "name": "SeatGeek",             "url": "https://seatgeek.com",             "category": "Revendedor", "region": "NA",     "logo": "💰", "enabled": False, "has_queue": False, "priority": 4, "notes": "Reventa NA"},
    {"id": "vivid",    "name": "Vivid Seats",          "url": "https://www.vividseats.com",       "category": "Revendedor", "region": "NA",     "logo": "💰", "enabled": False, "has_queue": False, "priority": 3, "notes": "Reventa NA"},
]


def _path():
    return config.ruta("sites.json")


def list_sites():
    d = json_atomic.load(_path(), default={"sites": []})
    if not d.get("sites"):
        d["sites"] = list(DEFAULT_SITES)
        json_atomic.save(_path(), d)
    return d["sites"]


def add_site(site):
    d = json_atomic.load(_path(), default={"sites": []})
    site = dict(site)
    site.setdefault("id", uuid.uuid4().hex[:8])
    site.setdefault("created_at", datetime.now().isoformat())
    site.setdefault("enabled", True)
    d["sites"].append(site)
    json_atomic.save(_path(), d)
    return site


def update_site(sid, patch):
    d = json_atomic.load(_path(), default={"sites": []})
    for s in d["sites"]:
        if s["id"] == sid:
            s.update(patch)
            json_atomic.save(_path(), d)
            return s
    return None


def delete_site(sid):
    d = json_atomic.load(_path(), default={"sites": []})
    d["sites"] = [s for s in d["sites"] if s["id"] != sid]
    json_atomic.save(_path(), d)
    return True


def toggle_site(sid, enabled=None):
    """Invierte enabled si enabled=None, si no usa el valor dado."""
    d = json_atomic.load(_path(), default={"sites": []})
    for s in d["sites"]:
        if s["id"] == sid:
            s["enabled"] = (not s.get("enabled")) if enabled is None else bool(enabled)
            json_atomic.save(_path(), d)
            return s
    return None


def bulk_update(enabled, scope="all", category=None, favorite=False, exclusive=False):
    """Actualiza enabled en lote.

    scope:
      - "all"       → todos los sitios
      - "category"  → solo los de la categoría dada
      - "favorite"  → solo los marcados como favorite
    exclusive:
      Si True, desactiva el resto y activa sólo los que caen en el scope.
    """
    # Cargar sitios (asegura que existen los defaults)
    list_sites()
    d = json_atomic.load(_path(), default={"sites": []})
    sites = d.get("sites") or []
    changed = 0
    for s in sites:
        matches = False
        if scope == "all":
            matches = True
        elif scope == "category":
            matches = (s.get("category") == category)
        elif scope == "favorite":
            matches = bool(s.get("favorite"))

        if matches:
            if s.get("enabled") != bool(enabled):
                s["enabled"] = bool(enabled)
                changed += 1
        elif exclusive:
            # scope exclusivo: resto se desactiva
            if s.get("enabled"):
                s["enabled"] = False
                changed += 1
    json_atomic.save(_path(), d)
    active = sum(1 for s in sites if s.get("enabled"))
    return {"ok": True, "changed": changed, "total": len(sites), "active": active}


def health_check(timeout: float = 5.0):
    """Comprueba si cada sitio enabled responde a un HEAD HTTP."""
    import concurrent.futures
    sites = [s for s in list_sites() if s.get("enabled") and s.get("url")]
    results = []
    def _probe(s):
        started = datetime.now()
        try:
            import requests
            r = requests.head(s["url"], allow_redirects=True, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
            elapsed_ms = int((datetime.now() - started).total_seconds() * 1000)
            return {"id": s["id"], "name": s["name"], "status_code": r.status_code,
                    "ok": 200 <= r.status_code < 400, "elapsed_ms": elapsed_ms}
        except Exception as e:
            return {"id": s["id"], "name": s["name"], "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:80]}"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(sites)))) as ex:
        results = list(ex.map(_probe, sites))
    ok_count = sum(1 for r in results if r.get("ok"))
    return {"ok": True, "ok_count": ok_count, "fail_count": len(results) - ok_count,
            "results": results}
