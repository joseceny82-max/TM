"""
event_manager.py — Gestion de eventos (antes 'partidos').
Guarda eventos de Ticketmaster que el usuario quiere comprar.
"""
import re
import uuid
from datetime import datetime
import config
import json_atomic


def _fetch_image_from_tm_url(tm_url):
    """Descarga la página de TM y extrae la mejor imagen disponible:
    - <meta property="og:image">
    - JSON-LD "image": [...]
    - <img class="event-hero" src=...> (fallback)
    Devuelve URL absoluta o '' si no encuentra nada."""
    if not tm_url or not tm_url.startswith("http"):
        return ""
    try:
        import requests
        r = requests.get(tm_url, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                                  "Accept-Language": "es,en;q=0.9"})
        if r.status_code != 200:
            return ""
        html = r.text
        m = re.search(r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                      html, re.IGNORECASE)
        if m: return m.group(1)
        m = re.search(r'"image"\s*:\s*"(https?://[^"]+)"', html)
        if m: return m.group(1)
        m = re.search(r'"image"\s*:\s*\[\s*"(https?://[^"]+)"', html)
        if m: return m.group(1)
        m = re.search(r'(https?://s1\.ticketm\.net/img/[^"\'\\)]+)', html)
        if m: return m.group(1)
    except Exception:
        return ""
    return ""


def _try_recover_image_from_siblings(event, all_events):
    """Si no hay tm_url ni image_url, busca un evento 'hermano' con mismo
    nombre+venue+ciudad y reusa su image_url/tm_url."""
    name = (event.get("name") or "").strip().lower()
    venue = (event.get("venue") or "").strip().lower()
    city = (event.get("city") or "").strip().lower()
    if not name: return None
    for sib in all_events:
        if sib.get("id") == event.get("id"): continue
        if (sib.get("name") or "").strip().lower() != name: continue
        if venue and (sib.get("venue") or "").strip().lower() != venue: continue
        if city and (sib.get("city") or "").strip().lower() != city: continue
        if sib.get("image_url") or sib.get("tm_url"):
            return {"image_url": sib.get("image_url", ""),
                    "tm_url_template": sib.get("tm_url", "")}
    return None


def enrich_event_image(event, all_events=None):
    """Si event no tiene image_url, intenta resolverla:
    1) fetch tm_url y parsear og:image / JSON-LD.
    2) buscar evento hermano con misma combinación (nombre+venue+ciudad).
    Devuelve True si la actualiza, False si no encuentra nada.
    Marca image_lookup_attempted=True para no reintentar en cada carga."""
    if event.get("image_url"):
        return False
    if event.get("image_lookup_attempted"):
        return False
    event["image_lookup_attempted"] = True
    img = _fetch_image_from_tm_url(event.get("tm_url"))
    if img:
        event["image_url"] = img
        return True
    if all_events:
        sib = _try_recover_image_from_siblings(event, all_events)
        if sib:
            if sib.get("image_url"):
                event["image_url"] = sib["image_url"]
            if sib.get("tm_url_template") and not event.get("tm_url"):
                event["tm_url"] = sib["tm_url_template"]
            if event.get("image_url"):
                return True
            if event.get("tm_url"):
                img2 = _fetch_image_from_tm_url(event["tm_url"])
                if img2:
                    event["image_url"] = img2
                    return True
    return False


def _path():
    return config.ruta("events.json")


def list_events():
    data = json_atomic.load(_path(), default={"events": []})
    events = data.get("events", [])
    # Auto-enrich imágenes que falten — pero sin bloquear: si tarda o falla, devolver tal cual.
    # Solo se intenta UNA VEZ por evento (image_lookup_attempted) para no martillear.
    pending = [e for e in events
               if not e.get("image_url") and not e.get("image_lookup_attempted")]
    if pending:
        try:
            import threading
            def _worker(snapshot):
                changed = False
                for ev in snapshot:
                    if enrich_event_image(ev, snapshot):
                        changed = True
                if changed:
                    cur = json_atomic.load(_path(), default={"events": []})
                    by_id = {e.get("id"): e for e in snapshot}
                    for e in cur.get("events", []):
                        if e.get("id") in by_id:
                            patch = by_id[e["id"]]
                            for k in ("image_url", "tm_url", "image_lookup_attempted"):
                                if patch.get(k) and not e.get(k):
                                    e[k] = patch[k]
                            if patch.get("image_lookup_attempted"):
                                e["image_lookup_attempted"] = True
                    json_atomic.save(_path(), cur)
            t = threading.Thread(target=_worker, args=([dict(e) for e in events],),
                                 daemon=True)
            t.start()
        except Exception:
            pass
    return events


def _sanitize_event(event):
    """Limpia un evento antes de guardar:
    - id: si parece una URL, no es hex de ≤16 chars o trae '/', se regenera.
    - country: si está vacío, se intenta deducir desde tm_url (.es/.fr/.com/.uk/etc).
    - venue duplicado en city: si city == venue (artefacto de scraper roto), separar.
      Se asume que el último token con prefijo conocido (Estadio/Estadi/Palau/Auditorio/...)
      es el venue real y la primera palabra es la ciudad.
    """
    event = dict(event)
    eid = str(event.get("id") or "")
    if (not eid) or "/" in eid or " " in eid or eid.startswith("http") or len(eid) > 16:
        # Si el id era una URL y tm_url está vacío, recuperarla allí.
        if eid.startswith("http") and not event.get("tm_url"):
            event["tm_url"] = eid
        event["id"] = uuid.uuid4().hex[:8]
    # Si tm_id falta pero tm_url tiene un ID numérico al final → extraer.
    # TM URLs siguen el formato .../entradas/<digits> o .../tickets/<digits>.
    if not event.get("tm_id") and event.get("tm_url"):
        m = re.search(r"/(\d{6,})(?:\?|/|$)", event["tm_url"])
        if m:
            event["tm_id"] = m.group(1)
    if not event.get("country"):
        url = (event.get("tm_url") or "").lower()
        for tld, cc in (("ticketmaster.es", "es"), ("ticketmaster.fr", "fr"),
                        ("ticketmaster.de", "de"), ("ticketmaster.it", "it"),
                        ("ticketmaster.co.uk", "uk"), ("ticketmaster.ie", "ie"),
                        ("ticketmaster.nl", "nl"), ("ticketmaster.be", "be"),
                        ("ticketmaster.at", "at"), ("ticketmaster.ch", "ch"),
                        ("ticketmaster.com.mx", "mx"), ("ticketmaster.com.au", "au"),
                        ("ticketmaster.ca", "ca"), ("ticketmaster.com", "us")):
            if tld in url:
                event["country"] = cc
                break
    if not event.get("country"):
        city_lc = (event.get("city") or "").lower()
        venue_lc = (event.get("venue") or "").lower()
        es_cities = ("madrid", "barcelona", "sevilla", "valencia", "bilbao",
                     "zaragoza", "málaga", "malaga", "marbella", "nerja",
                     "granada", "salamanca", "alicante", "murcia", "palma",
                     "santander", "vigo", "córdoba", "cordoba", "gijón", "gijon")
        for c in es_cities:
            if c in city_lc or c in venue_lc:
                event["country"] = "es"
                break
    venue = (event.get("venue") or "").strip()
    city = (event.get("city") or "").strip()
    if venue and city and venue == city:
        venue_tokens = ("Estadio", "Estadi", "Palau", "Auditorio", "Auditori",
                        "Arena", "Sala", "Pabellón", "Pabellon", "Teatro",
                        "Teatre", "Plaza", "Coliseum", "WiZink", "Bizkaia",
                        "Movistar", "Iberdrola")
        text = venue
        cut = -1
        for tok in venue_tokens:
            idx = text.find(tok)
            if idx >= 0 and (cut == -1 or idx < cut):
                cut = idx
        if cut > 0:
            event["city"] = text[:cut].strip()
            event["venue"] = text[cut:].strip()
    return event


def add_event(event):
    data = json_atomic.load(_path(), default={"events": []})
    event = _sanitize_event(event)
    event.setdefault("id", uuid.uuid4().hex[:8])
    event.setdefault("created_at", datetime.now().isoformat())
    event.setdefault("enabled", True)
    event.setdefault("status", "pending")
    # Si falta imagen, intentar resolverla ANTES de guardar (síncrono solo en add — el
    # usuario espera el evento creado completo).
    if not event.get("image_url"):
        try:
            enrich_event_image(event, data.get("events", []))
        except Exception:
            pass
    data["events"].append(event)
    json_atomic.save(_path(), data)
    return event


def update_event(event_id, patch):
    data = json_atomic.load(_path(), default={"events": []})
    for ev in data.get("events", []):
        if ev.get("id") == event_id:
            ev.update(patch)
            json_atomic.save(_path(), data)
            return ev
    return None


def health_check(event):
    """Devuelve {ok, ready_for_bot, missing, warnings} para un evento.
    ready_for_bot = True solo si tiene los 5 campos críticos para que el bot
    pueda navegar+escanear+lanzar: tm_id, tm_url, country, venue, city.
    image_url y date son recomendables pero no bloquean."""
    if not event:
        return {"ok": False, "ready_for_bot": False, "missing": ["event_not_found"]}
    missing = []
    warnings = []
    if not event.get("tm_id"):     missing.append("tm_id")
    if not event.get("tm_url"):    missing.append("tm_url")
    if not event.get("country"):   missing.append("country")
    if not event.get("venue"):     missing.append("venue")
    if not event.get("city"):      missing.append("city")
    if not event.get("image_url"): warnings.append("image_url")
    if not event.get("date"):      warnings.append("date")
    if not event.get("sale_date"): warnings.append("sale_date")
    return {
        "ok": True,
        "ready_for_bot": not missing,
        "missing": missing,
        "warnings": warnings,
        "id": event.get("id"),
        "name": event.get("name"),
    }


def health_all():
    """Health-check de todos los eventos. Devuelve {total, ready, broken, list[]}."""
    items = list_events()
    out = []
    ready = 0
    broken = 0
    for e in items:
        h = health_check(e)
        out.append({"id": e.get("id"), "name": e.get("name", "")[:60],
                    **{k: h[k] for k in ("ready_for_bot", "missing", "warnings")}})
        if h["ready_for_bot"]: ready += 1
        else: broken += 1
    return {"ok": True, "total": len(items), "ready": ready,
            "broken": broken, "events": out}


def from_tm_url(tm_url, fallback_name=""):
    """Crea un dict de evento mínimo a partir de UNA URL de Ticketmaster.
    Extrae tm_id del path, country del TLD, intenta enrich de imagen y deduce
    venue/city scrapeando la página si _fetch_event_meta_from_tm está disponible.
    Devuelve un dict listo para pasar a add_event (sin guardar todavía)."""
    ev = {"tm_url": tm_url, "name": fallback_name or "", "enabled": True}
    ev = _sanitize_event(ev)   # extrae tm_id + country
    # Intentar scrape ligero del HTML para name/venue/city/date
    try:
        meta = _fetch_event_meta_from_tm(tm_url)
        for k in ("name", "venue", "city", "date", "image_url"):
            if not ev.get(k) and meta.get(k):
                ev[k] = meta[k]
    except Exception:
        pass
    # Si todavía falta image_url y hay tm_url → fetch og:image
    if not ev.get("image_url"):
        try:
            img = _fetch_image_from_tm_url(tm_url)
            if img: ev["image_url"] = img
        except Exception:
            pass
    return ev


def _fetch_event_meta_from_tm(tm_url):
    """Scrape ligero del HTML del evento TM para extraer name (og:title),
    venue + city + date desde JSON-LD si existe.
    Devuelve dict (puede contener name/venue/city/date/image_url) o {}."""
    out = {}
    if not tm_url or not tm_url.startswith("http"):
        return out
    try:
        import requests
        r = requests.get(tm_url, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                                  "Accept-Language": "es,en;q=0.9"})
        if r.status_code != 200:
            return out
        html = r.text
        # og:title
        m = re.search(r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
                      html, re.IGNORECASE)
        if m: out["name"] = m.group(1).strip()
        # og:image (image_url)
        m = re.search(r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                      html, re.IGNORECASE)
        if m: out["image_url"] = m.group(1).strip()
        # JSON-LD (Event/MusicEvent)
        for ld in re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.DOTALL):
            try:
                import json as _json
                data = _json.loads(ld.strip())
                cand = data if isinstance(data, list) else [data]
                for d in cand:
                    if not isinstance(d, dict): continue
                    types = d.get("@type") or []
                    if isinstance(types, str): types = [types]
                    if not any(("Event" in t or "Concert" in t) for t in types):
                        continue
                    if d.get("name") and not out.get("name"):
                        out["name"] = d["name"]
                    if d.get("startDate") and not out.get("date"):
                        out["date"] = d["startDate"]
                    loc = d.get("location") or {}
                    if isinstance(loc, list) and loc: loc = loc[0]
                    if isinstance(loc, dict):
                        if loc.get("name") and not out.get("venue"):
                            out["venue"] = loc["name"]
                        addr = loc.get("address") or {}
                        if isinstance(addr, dict):
                            city = addr.get("addressLocality") or ""
                            if city and not out.get("city"):
                                out["city"] = city
                    break
            except Exception:
                continue
    except Exception:
        pass
    return out


def repair_all():
    """Aplica _sanitize_event a todos los eventos del JSON + intenta enrich de imagen
    a los que la tengan vacía. Devuelve resumen de cuántos se modificaron y qué cambios."""
    data = json_atomic.load(_path(), default={"events": []})
    changed = 0
    img_recovered = 0
    details = []
    new_list = []
    for ev in data.get("events", []):
        before = dict(ev)
        sanitized = _sanitize_event(ev)
        diffs = {k: (before.get(k), sanitized.get(k))
                 for k in ("id", "country", "venue", "city")
                 if before.get(k) != sanitized.get(k)}
        if diffs:
            changed += 1
            details.append({"name": sanitized.get("name", "?")[:50],
                            "diffs": diffs})
        new_list.append(sanitized)
    # Pasada de imágenes — los siblings se buscan en la lista ya saneada
    for ev in new_list:
        if not ev.get("image_url"):
            ev.pop("image_lookup_attempted", None)
            if enrich_event_image(ev, new_list):
                img_recovered += 1
    data["events"] = new_list
    json_atomic.save(_path(), data)
    return {"ok": True, "total": len(new_list), "changed": changed,
            "img_recovered": img_recovered, "details": details}


def delete_event(event_id):
    data = json_atomic.load(_path(), default={"events": []})
    data["events"] = [e for e in data.get("events", []) if e.get("id") != event_id]
    json_atomic.save(_path(), data)
    return True


def get_enabled():
    return [e for e in list_events() if e.get("enabled")]


def seed_defaults():
    """Carga eventos de ejemplo si no hay ninguno."""
    if list_events():
        return
    sample = [
        {
            "name": "Taylor Swift - Eras Tour",
            "venue": "Madison Square Garden",
            "city": "Nueva York",
            "country": "us",
            "date": "2026-07-15T20:00:00",
            "sale_date": "2026-04-25T10:00:00",
            "tm_url": "https://www.ticketmaster.com/taylor-swift-tickets/artist/1741188",
            "max_price": 500,
            "quantity": 2,
            "category_pref": "any",
            "enabled": True,
        },
        {
            "name": "Coldplay - Music of the Spheres",
            "venue": "Estadio Santiago Bernabeu",
            "city": "Madrid",
            "country": "es",
            "date": "2026-06-20T21:00:00",
            "sale_date": "2026-05-01T11:00:00",
            "tm_url": "https://www.ticketmaster.es/artist/coldplay",
            "max_price": 300,
            "quantity": 4,
            "category_pref": "any",
            "enabled": True,
        },
        {
            "name": "Oasis Reunion Tour",
            "venue": "Wembley Stadium",
            "city": "Londres",
            "country": "uk",
            "date": "2026-08-10T19:00:00",
            "sale_date": "2026-04-22T09:00:00",
            "tm_url": "https://www.ticketmaster.co.uk/oasis-tickets",
            "max_price": 250,
            "quantity": 2,
            "category_pref": "any",
            "enabled": True,
        },
    ]
    for ev in sample:
        add_event(ev)
