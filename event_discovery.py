"""event_discovery.py — Integración con Ticketmaster Discovery API.

Permite:
- Buscar eventos por keyword/atraccion/ciudad/dominio
- Obtener detalle de un evento por ID
- Sincronizar eventos encontrados al event_manager local
- Extraer ID de evento desde una URL TM

Requiere API key gratuita de Ticketmaster (https://developer.ticketmaster.com/).
Se guarda en settings. Si no hay key → devuelve error explicativo.

Docs: https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/
"""
import re
from datetime import datetime
from typing import Optional, List, Dict
import config
import json_atomic

DISCOVERY_BASE = "https://app.ticketmaster.com/discovery/v2"
DISCOVERY_EU_BASE = "https://app.ticketmaster.eu/mfxapi/v2"
TIMEOUT = 12

# Mapeo dominio → mercado TM (country code ISO para filtro)
COUNTRY_CODE_MAP = {
    "us": "US", "ca": "CA", "mx": "MX",
    "uk": "GB", "ie": "IE", "es": "ES", "de": "DE", "fr": "FR",
    "it": "IT", "nl": "NL", "at": "AT", "be": "BE", "dk": "DK",
    "fi": "FI", "no": "NO", "se": "SE", "ch": "CH", "cz": "CZ", "pl": "PL",
    "au": "AU", "nz": "NZ", "ae": "AE",
}


def _settings_path():
    return config.ruta("event_discovery.json")


def get_config() -> dict:
    return json_atomic.load(_settings_path(), default={
        "api_key": "",
        "default_country": "us",
        "last_sync": None,
        "auto_sync_enabled": False,
        "sync_interval_min": 60,
        "keywords": [],
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    cur.update({k: v for k, v in patch.items() if k in
                ("api_key", "default_country", "auto_sync_enabled", "sync_interval_min", "keywords")})
    json_atomic.save(_settings_path(), cur)
    return cur


def is_configured() -> bool:
    return bool(get_config().get("api_key"))


def extract_event_id_from_url(url: str) -> Optional[str]:
    """Extrae event ID desde URL TM. Soporta varios formatos:
       /event/ABC1234567890
       /event/slug-del-evento/497432871  (formato ES/EU con ID numérico)
       /activity/<id>
       /evento/<id>
    """
    if not url:
        return None
    # Formato ES/EU: /event/<slug>/<digits>  (ID numérico al final)
    m = re.search(r"/event[os]?/[^/]+/(\d{6,15})(?:[/?#]|$)", url)
    if m:
        return m.group(1)
    # Formato US/UK clásico: /event/<alfanumérico 8+>
    m = re.search(r"/event[os]?/([A-Za-z0-9]{8,})(?:[/?#]|$)", url)
    if m:
        return m.group(1).upper()
    # Formato /activity/<id>
    m = re.search(r"activity[/-]([A-Za-z0-9]{8,})", url)
    if m:
        return m.group(1).upper()
    # Último recurso: cualquier secuencia larga de dígitos al final del path
    m = re.search(r"/(\d{7,15})(?:[/?#]|$)", url)
    if m:
        return m.group(1)
    return None


def _extract_slug_from_url(url: str) -> Optional[str]:
    """Extrae el slug descriptivo de una URL TM (si existe).
       /event/el-ultimo-de-la-fila-entradas/497432871 → 'el-ultimo-de-la-fila-entradas'
       /artist/el-ultimo-de-la-fila-entradas/1418653  → 'el-ultimo-de-la-fila-entradas'
    """
    if not url:
        return None
    m = re.search(r"/(?:event[os]?|artist|attraction|venue)/([a-z][a-z0-9\-]{3,100})(?:/|$)",
                  url, flags=re.I)
    if m:
        slug = m.group(1)
        if slug[0].isalpha():
            return slug
    return None


def extract_artist_id_from_url(url: str) -> Optional[str]:
    """Extrae el artistId/attractionId de una URL TM:
       /artist/el-ultimo-de-la-fila-entradas/1418653  → '1418653'
       /artist/K8vZ9171XN0/                          → 'K8vZ9171XN0'
    """
    if not url:
        return None
    # ID numérico al final del path (formato ES/EU)
    m = re.search(r"/(?:artist|attraction)/[^/]+/(\d{5,15})(?:[/?#]|$)", url)
    if m: return m.group(1)
    # ID alfanumérico al final (formato US/UK, tipo K8vZ...)
    m = re.search(r"/(?:artist|attraction)/([A-Za-z0-9]{8,})(?:[/?#]|$)", url)
    if m: return m.group(1)
    return None


def _slug_to_title(slug: str) -> str:
    """Convierte 'el-ultimo-de-la-fila-entradas' → 'El Último De La Fila'.
       Remueve stopwords como 'entradas', 'tickets', 'tour' del final.
    """
    if not slug:
        return ""
    # Dividir por guiones y capitalizar
    parts = [p for p in slug.replace("_", "-").split("-") if p]
    # Remueve palabras-basura del final
    STOPWORDS_END = {"entradas", "tickets", "boletos", "tour", "concert", "event"}
    while parts and parts[-1].lower() in STOPWORDS_END:
        parts.pop()
    # Acentos castellanos básicos
    ACCENT_MAP = {
        "ultimo": "Último", "ultima": "Última", "fila": "Fila",
        "espana": "España", "espanol": "Español",
    }
    out = []
    for p in parts:
        lp = p.lower()
        if lp in ACCENT_MAP:
            out.append(ACCENT_MAP[lp])
        else:
            out.append(p.capitalize())
    return " ".join(out)


def _infer_country_from_url(url: str) -> str:
    """Infiere el dominio/país de una URL TM."""
    if not url: return "us"
    u = url.lower()
    if "ticketmaster.es" in u: return "es"
    if "ticketmaster.co.uk" in u: return "uk"
    if "ticketmaster.de" in u: return "de"
    if "ticketmaster.fr" in u: return "fr"
    if "ticketmaster.it" in u: return "it"
    if "ticketmaster.com.au" in u: return "au"
    if "ticketmaster.ca" in u: return "ca"
    if "ticketmaster.com.mx" in u: return "mx"
    if "ticketmaster.nl" in u: return "nl"
    if "ticketmaster.be" in u: return "be"
    if "ticketmaster.ie" in u: return "ie"
    if "ticketmaster.com" in u: return "us"
    return "us"


def search_events(keyword: str = "", city: str = "", country: str = "us",
                  size: int = 20) -> Dict:
    """Busca eventos en Discovery API. Fallback a scraping HTML público
    cuando no hay key, y demo solo si tampoco da resultados.

    Si el keyword contiene una URL TM (de artista o evento), delega a
    search_by_artist_url para obtener eventos REALES del HTML público.
    """
    # Detecta si el keyword es una URL TM
    if keyword and ("ticketmaster." in keyword.lower() and "http" in keyword.lower()):
        return search_by_artist_url(keyword.strip(), size=size)

    cfg = get_config()
    api_key = cfg.get("api_key", "")
    # Sin API key: intentar scraping web ANTES del demo
    if _is_placeholder_key(api_key):
        scraped = _scrape_search_results(keyword or "", country or "es", city, size)
        if scraped and scraped.get("events"):
            return scraped
        # último recurso: demo
        demo_all = _demo_featured(country or "us", 12)
        filtered = _demo_filter_by_query(demo_all, keyword, city)
        return {
            "ok": True, "demo": True,
            "count": len(filtered),
            "total": len(filtered),
            "events": filtered,
            "note_key": "disc.no_real_demo",
        }

    import requests  # lazy
    params = {
        "apikey": api_key,
        "size": min(max(size, 1), 50),
        "sort": "date,asc",
    }
    cc = COUNTRY_CODE_MAP.get(country, country.upper() if country else "US")
    if cc:
        params["countryCode"] = cc
    if keyword:
        params["keyword"] = keyword
    if city:
        params["city"] = city

    try:
        r = requests.get(DISCOVERY_BASE + "/events.json",
                         params=params, timeout=TIMEOUT)
        if r.status_code == 401:
            # key rechazada → fallback demo
            demo_all = _demo_featured(country or "us", 12)
            filtered = _demo_filter_by_query(demo_all, keyword, city)
            return {"ok": True, "demo": True, "count": len(filtered),
                    "total": len(filtered), "events": filtered,
                    "warn": "API key 401", "note_key": "disc.demo_key_rejected"}
        if r.status_code == 429:
            return {"ok": False, "error_key": "disc.rate_limit"}
        if not r.ok:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        data = r.json()
        events = (data.get("_embedded") or {}).get("events") or []
        # Si no hay resultados reales, prueba con demo filtrado
        if not events:
            demo_all = _demo_featured(country or "us", 12)
            filtered = _demo_filter_by_query(demo_all, keyword, city)
            if filtered:
                return {"ok": True, "demo": True, "count": len(filtered),
                        "total": len(filtered), "events": filtered,
                        "note_key": "disc.no_results_for",
                        "note_args": {"query": keyword or city}}
        return {
            "ok": True,
            "count": len(events),
            "total": data.get("page", {}).get("totalElements", len(events)),
            "events": [_to_local_event(e, country) for e in events],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _demo_filter_by_query(events: list, keyword: str, city: str) -> list:
    """Filtra los eventos demo por keyword/city (case-insensitive substring).
       Si no hay matches y hay keyword, genera eventos sintéticos con ese nombre
       para que el usuario pueda probar el flujo con cualquier búsqueda.
    """
    out = events
    if keyword:
        kw = keyword.lower()
        out = [e for e in out if kw in (e.get("name", "")).lower()
               or kw in (e.get("venue", "")).lower()
               or kw in " ".join(e.get("classifications", [])).lower()]
    if city:
        c = city.lower()
        out = [e for e in out if c in (e.get("city", "")).lower()]
    # Si no hay matches pero hay keyword, genera 8 eventos demo sintéticos
    if not out and keyword:
        out = _synth_demo_events(keyword, city or "",
                                   events[0].get("country", "us") if events else "us",
                                   count=8)
    return out


def _synth_demo_events(keyword: str, city: str, country: str, count: int = 8) -> list:
    """Genera eventos demo con el nombre solicitado. count=8 por defecto."""
    import hashlib
    from datetime import datetime, timedelta
    base = datetime.now()
    venues_by_country = {
        "es": [
            ("WiZink Center", "Madrid"), ("Palau Sant Jordi", "Barcelona"),
            ("Bizkaia Arena", "Bilbao"), ("Martín Carpena", "Málaga"),
            ("Roig Arena", "Valencia"), ("Iradier Arena", "Vitoria"),
            ("Palacio Vistalegre", "Madrid"), ("Auditorio Starlite", "Marbella"),
            ("Palau Olímpic", "Barcelona"), ("Ifema", "Madrid"),
            ("Estadio Metropolitano", "Madrid"), ("Santiago Bernabéu", "Madrid"),
            ("Coliseum", "A Coruña"),
        ],
        "uk": [
            ("O2 Arena", "London"), ("AO Arena", "Manchester"), ("OVO Hydro", "Glasgow"),
            ("Utilita Arena", "Birmingham"), ("First Direct Arena", "Leeds"),
            ("M&S Bank Arena", "Liverpool"), ("Principality Stadium", "Cardiff"),
            ("Wembley Stadium", "London"),
        ],
        "us": [
            ("Madison Square Garden", "New York"), ("The Forum", "Los Angeles"),
            ("United Center", "Chicago"), ("American Airlines Center", "Dallas"),
            ("TD Garden", "Boston"), ("Kia Center", "Orlando"),
            ("Wells Fargo Center", "Philadelphia"), ("Chase Center", "San Francisco"),
        ],
        "de": [
            ("Mercedes-Benz Arena", "Berlin"), ("Lanxess Arena", "Köln"),
            ("Olympiahalle", "München"), ("Barclays Arena", "Hamburg"),
            ("SAP Arena", "Mannheim"), ("ZAG Arena", "Hannover"),
            ("Festhalle", "Frankfurt"), ("Porsche Arena", "Stuttgart"),
        ],
        "fr": [
            ("Accor Arena", "Paris"), ("Halle Tony Garnier", "Lyon"),
            ("Zénith", "Toulouse"), ("Palais Nikaïa", "Nice"),
            ("Arkéa Arena", "Bordeaux"), ("Zénith", "Nantes"),
            ("La Défense Arena", "Paris"), ("Stade de France", "Paris"),
        ],
        "it": [
            ("Mediolanum Forum", "Milano"), ("Palazzo dello Sport", "Roma"),
            ("Unipol Arena", "Bologna"), ("Nelson Mandela Forum", "Firenze"),
            ("Pala Alpitour", "Torino"), ("PalaBam", "Mantova"),
            ("Arena di Verona", "Verona"), ("Stadio San Siro", "Milano"),
        ],
    }
    venues = venues_by_country.get(country, venues_by_country["us"])
    venues = venues[:max(1, min(count, len(venues)))]
    out = []
    for i, (venue, ven_city) in enumerate(venues):
        display_city = city if city else ven_city
        sale_offset = i * 86400
        date_offset = (i + 45) * 86400
        fake_id = hashlib.md5(f"{country}{keyword}{venue}".encode()).hexdigest()[:16].upper()
        name = keyword.strip().title() + (" · Live Tour" if i == 0 else
                                            (" · European Tour" if i == 1 else
                                            " · World Tour"))
        # URL path según país
        tld = "es" if country == "es" else ("co.uk" if country == "uk" else
              "de" if country == "de" else "fr" if country == "fr" else
              "it" if country == "it" else "com")
        slug = keyword.lower().replace(" ", "-")
        out.append({
            "tm_id": f"G5v{fake_id}",
            "name": name,
            "venue": venue,
            "city": display_city,
            "country": country,
            "date": (base + timedelta(seconds=date_offset)).isoformat()[:19],
            "sale_date": (base + timedelta(seconds=sale_offset)).isoformat()[:19],
            "tm_url": f"https://www.ticketmaster.{tld}/event/{slug}/{fake_id}",
            "max_price": 120 + i * 30,
            "quantity": 2,
            "currency": "EUR" if country in ("es","fr","de","it") else ("GBP" if country=="uk" else "USD"),
            "tm_status": "onsale" if i != 2 else "offsale",
            "enabled": True,
            "image_url": "",
            "classifications": ["Live", "Music"],
            "source": "demo_synth",
        })
    return out


def get_event_detail(tm_event_id: str) -> Dict:
    """Obtiene detalle de un evento específico."""
    cfg = get_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        return {"ok": False, "error": "API key no configurada"}
    import requests
    try:
        r = requests.get(f"{DISCOVERY_BASE}/events/{tm_event_id}.json",
                         params={"apikey": api_key}, timeout=TIMEOUT)
        if r.status_code == 404:
            return {"ok": False, "error": "Evento no encontrado"}
        if not r.ok:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        return {"ok": True, "event": _to_local_event(r.json(), None)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def enrich_from_url(url: str) -> Dict:
    """Dada una URL TM, extrae toda la info posible.
       Estrategia:
       1. Intenta extraer ID TM → Discovery API (detalle completo)
       2. Si falla (ID numérico ES/EU, 404, sin key, etc.), construye evento
          mínimo desde el propio URL (slug → nombre, dominio → país).
       Siempre intenta devolver algo utilizable por el bot.
    """
    if not url or "ticketmaster" not in url.lower():
        return {"ok": False, "error": "URL no es de Ticketmaster"}
    eid = extract_event_id_from_url(url)
    country = _infer_country_from_url(url)
    slug = _extract_slug_from_url(url)

    # Si hay ID alfanumérico tipo 'G5v...', intenta Discovery API
    is_alphanumeric_id = bool(eid and not eid.isdigit())
    if is_alphanumeric_id:
        r = get_event_detail(eid)
        if r.get("ok"):
            r["event"]["tm_url"] = url
            if not r["event"].get("country"): r["event"]["country"] = country
            return r
        # si falla (404, key rechazada…), cae al fallback slug-based

    # Fallback: construye evento mínimo desde el URL
    name = _slug_to_title(slug) if slug else "Evento TM"
    if not name or name == "Evento TM":
        return {"ok": False, "error": "No se pudo extraer info de la URL (sin slug ni ID válido)"}
    cc = COUNTRY_CODE_MAP.get(country, country.upper())
    ev = {
        "tm_id": eid or f"url_{hashlib_hash(url)}",
        "name": name,
        "country": country,
        "tm_url": url,
        "venue": "",       # desconocido desde la URL
        "city": "",
        "date": "",
        "sale_date": "",
        "max_price": "",
        "currency": {"es":"EUR","fr":"EUR","de":"EUR","it":"EUR","uk":"GBP"}.get(country, "USD"),
        "tm_status": "",
        "image_url": "",
        "classifications": [],
        "source": "url_parse",
        "quantity": 2,
    }
    return {
        "ok": True, "event": ev,
        "note": "Info mínima extraída del URL (no de Discovery API). Completa manualmente venue/fecha si los necesitas.",
    }


def hashlib_hash(s: str) -> str:
    """Hash corto para generar un tm_id estable desde una URL."""
    import hashlib
    return hashlib.md5((s or "").encode()).hexdigest()[:10]


def search_by_artist_url(url: str, size: int = 30) -> Dict:
    """Busca eventos de un artista dado su URL TM.
    Estrategia:
      1. Extrae artistId/attractionId del URL.
      2. Si hay API key válida → Discovery API con attractionId (preciso, oficial).
      3. Si no → scrape HTML público de la página del artista (extrae eventos).
      4. Si todo falla → demo sintético con el slug.
    """
    if not url or "ticketmaster" not in url.lower():
        return {"ok": False, "error": "URL no es de Ticketmaster"}

    artist_id = extract_artist_id_from_url(url)
    country = _infer_country_from_url(url)
    slug = _extract_slug_from_url(url) or ""
    artist_name = _slug_to_title(slug) if slug else ""

    # 1. Discovery API si hay key válida y artistId
    cfg = get_config()
    api_key = cfg.get("api_key", "")
    if artist_id and not _is_placeholder_key(api_key):
        try:
            import requests
            params = {
                "apikey": api_key,
                "attractionId": artist_id,
                "size": min(max(size, 1), 200),
                "sort": "date,asc",
            }
            r = requests.get(DISCOVERY_BASE + "/events.json", params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                events = (data.get("_embedded") or {}).get("events") or []
                if events:
                    return {
                        "ok": True, "source": "discovery_api",
                        "artist_id": artist_id, "artist_name": artist_name,
                        "count": len(events),
                        "total": data.get("page", {}).get("totalElements", len(events)),
                        "events": [_to_local_event(e, country) for e in events],
                    }
        except Exception:
            pass

    # 2. Scrape HTML público (sin API key)
    scraped = _scrape_artist_page(url, artist_name, country)
    if scraped and scraped.get("events"):
        return scraped

    # 3. Fallback demo
    demos = _synth_demo_events(artist_name or "Artista TM", "", country, count=8)
    return {
        "ok": True, "source": "demo",
        "artist_id": artist_id, "artist_name": artist_name,
        "count": len(demos), "total": len(demos),
        "events": demos,
        "note_key": "disc.demo_no_html",
    }


_TM_TLD_BY_COUNTRY = {
    "es": "es", "fr": "fr", "de": "de", "it": "it", "uk": "co.uk", "gb": "co.uk",
    "ie": "ie", "nl": "nl", "be": "be", "se": "se", "no": "no", "fi": "fi",
    "dk": "dk", "at": "at", "ch": "ch", "pl": "pl", "us": "com", "ca": "ca",
    "mx": "com.mx", "au": "com.au", "nz": "co.nz", "ae": "ae",
}


def _scrape_search_results(keyword: str, country: str = "es",
                           city: str = "", size: int = 20) -> Optional[Dict]:
    """Scrapea TM web (sin API key) — devuelve eventos reales del HTML SSR.
    URL: https://www.ticketmaster.<tld>/search?q=KEYWORD
    Cada `<a data-testid="event-list-link">` lleva un span VisuallyHidden con
    NOMBRE, VENUE, CIUDAD, PAÍS y FECHA. Parseamos esos campos.
    Las imágenes específicas se obtienen en paralelo del og:image de cada URL.
    """
    if not keyword:
        return None
    try:
        import requests
        tld = _TM_TLD_BY_COUNTRY.get((country or "es").lower(), "es")
        base = f"https://www.ticketmaster.{tld}"
        params = {"q": keyword}
        if city:
            params["city"] = city
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
        r = requests.get(base + "/search", params=params, headers=headers,
                         timeout=12, allow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text

        # Cada link de evento contiene un span VisuallyHidden con TODA la info
        # textual (TM lo añade para accesibilidad de lectores de pantalla).
        # Patrón: <a href="URL" data-testid="event-list-link" ...>...
        #           <span class="VisuallyHidden-...">NAME, CITY, COUNTRY VENUE
        #             <span>DATE, TIME</span> En el sitio web ...</span>
        #         </a>
        events: list = []
        link_re = re.compile(
            r'<a\s+href="([^"]+)"[^>]*data-testid="event-list-link"[^>]*>'
            r'.*?<span class="VisuallyHidden[^"]*">([^<]+?)<span>([^<]+?)</span>',
            re.DOTALL)
        for m in link_re.finditer(html):
            url_e = m.group(1).strip()
            text = m.group(2).strip().rstrip(",").strip()
            date_str = m.group(3).strip()
            # text ej: "Coldplay USA vs The U2 Experience, San Juan Capistrano, CA, EE. UU. The Coach House"
            parts = [p.strip() for p in text.split(",")]
            name = parts[0] if parts else text
            venue = parts[-1] if len(parts) >= 2 else ""
            ev_country = parts[-2] if len(parts) >= 3 else ""
            ev_city = parts[1] if len(parts) >= 4 else (parts[1] if len(parts) >= 2 else "")
            # Parsear fecha "22/5/26, 20:00" → ISO "2026-05-22T20:00:00"
            iso_date = _parse_tm_date_es(date_str)
            ev_id = _extract_id_from_event_url(url_e)
            events.append({
                "id": ev_id or url_e,
                "name": name,
                "url": url_e,
                "date": iso_date or date_str,
                "venue": venue,
                "city": ev_city,
                "country": ev_country,
                "raw_date": date_str,
                "image_url": "",  # se rellena en paralelo abajo
            })
            if len(events) >= size:
                break
        if not events:
            return None

        # Enriquecer con imágenes en paralelo (og:image de cada event page).
        # Timeout corto por evento, máx 8 hilos. Si falla, image_url queda vacío.
        _enrich_event_images(events, headers)

        # NO hacer fallback con imágenes del HTML por proximidad — generaba mismatches.
        # Solo se asigna image_url si og:image del propio event page la devolvió (vía
        # _enrich_event_images con curl_cffi). Si no, el frontend muestra placeholder 🎭
        # honesto en vez de imagen errónea.

        return {
            "ok": True, "source": "tm_web_scrape",
            "country": country, "keyword": keyword,
            "count": len(events), "total": len(events),
            "events": events,
            "note_key": "disc.scrape_real",
            "note_args": {"count": len(events)},
        }
    except Exception as e:
        return {"ok": False, "error_key": "disc.scrape_failed",
                "error_args": {"err": str(e)[:100]}}


def _enrich_event_images(events: list, headers: dict) -> None:
    """Hace fetch en paralelo del og:image de cada event URL y lo asigna a image_url.
    Usa http2_frame_match.request() si está disponible (curl_cffi con TLS Chrome bypassea
    muchos 401 de Akamai en event pages); si no, requests estándar como fallback.
    Modifica la lista in-place. Tolera errores y timeouts cortos por entrada."""
    if not events: return
    from concurrent.futures import ThreadPoolExecutor, as_completed
    img_re = re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']'
        r'[^>]*content=["\']([^"\']+)["\']',
        re.I)
    # También JSON-LD image
    jsonld_re = re.compile(r'"image"\s*:\s*"(https://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', re.I)

    # Engine: preferir curl_cffi (Chrome impersonate) sobre requests para esquivar 401 Akamai
    try:
        import http2_frame_match
        use_h2 = http2_frame_match._has_curl_cffi()
    except Exception:
        use_h2 = False
    import requests

    def _is_generic(img: str) -> bool:
        # Filtra logos/placeholders genéricos TM — no son específicos del evento
        low = (img or '').lower()
        if not low: return True
        if any(k in low for k in (
            'tmconst.com', 'logo', 'default', 'placeholder',
            'solaris-720', 'fallback', 'generic', 'meta-image',
        )): return True
        return False

    def _fetch_one(ev):
        url = ev.get("url") or ""
        if not url: return ev, ""
        try:
            text = ""
            if use_h2:
                r = http2_frame_match.request_via_curl_cffi(
                    'GET', url, headers=headers, timeout=6,
                    profile_name='chrome_131_windows')
                if r.get('ok') and r.get('status_code') == 200:
                    text = r.get('body') or ''
            else:
                r = requests.get(url, headers=headers, timeout=6, allow_redirects=True)
                if r.status_code == 200:
                    text = r.text
            if not text: return ev, ""
            head = text[:150000]
            m = img_re.search(head) or jsonld_re.search(head)
            img = (m.group(1).strip() if m else "")
            if _is_generic(img): return ev, ""
            return ev, img
        except Exception:
            return ev, ""

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_fetch_one, e) for e in events]
        for f in as_completed(futs, timeout=20):
            try:
                ev, img = f.result()
                if img:
                    ev["image_url"] = img
            except Exception:
                pass


def _parse_tm_date_es(s: str) -> Optional[str]:
    """Convierte '22/5/26, 20:00' → '2026-05-22T20:00:00'. Tolera formatos UK."""
    if not s: return None
    try:
        # ej: "22/5/26, 20:00" o "Fri 22 May 2026, 8:00pm"
        m = re.match(r'\s*(\d{1,2})/(\d{1,2})/(\d{2,4})(?:,\s*(\d{1,2}):(\d{2}))?', s)
        if m:
            d, mo, y, hh, mm = m.groups()
            y = int(y); y = 2000 + y if y < 100 else y
            return f"{y:04d}-{int(mo):02d}-{int(d):02d}T{int(hh or 0):02d}:{int(mm or 0):02d}:00"
    except Exception:
        pass
    return None


def _extract_id_from_event_url(url: str) -> Optional[str]:
    """Extrae ID de event URL TM. Soporta /event/<HEX_OR_NUM>."""
    if not url: return None
    m = re.search(r'/event/([A-Za-z0-9]{8,})', url)
    return m.group(1) if m else None


def _scrape_artist_page(url: str, artist_name: str, country: str) -> Optional[Dict]:
    """Intenta extraer eventos del HTML público de una página de artista TM.
    Busca JSON embebido (__NEXT_DATA__) o bloques JSON-LD.
    """
    try:
        import requests, json as _json
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
        r = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text

        events_raw: list = []

        # A. JSON-LD estructurado (schema.org/Event)
        for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.I):
            try:
                blob = _json.loads(m.group(1).strip())
                if isinstance(blob, list):
                    for item in blob:
                        if isinstance(item, dict) and item.get("@type") == "Event":
                            events_raw.append(item)
                elif isinstance(blob, dict):
                    if blob.get("@type") == "Event":
                        events_raw.append(blob)
                    # "@graph" con lista
                    graph = blob.get("@graph") or []
                    for item in graph:
                        if isinstance(item, dict) and item.get("@type") == "Event":
                            events_raw.append(item)
                    # subEvent / hasPart
                    for key in ("subEvent", "hasPart", "event"):
                        items = blob.get(key) or []
                        if isinstance(items, list):
                            for it in items:
                                if isinstance(it, dict) and (it.get("@type") == "Event" or it.get("name")):
                                    events_raw.append(it)
            except Exception:
                continue

        # B. __NEXT_DATA__ (Next.js apps — TM usa Next en algunos países)
        if not events_raw:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if m:
                try:
                    nd = _json.loads(m.group(1).strip())
                    # Búsqueda recursiva de objetos que parezcan eventos
                    events_raw = _find_events_in_json(nd)
                except Exception:
                    pass

        # C. Fallback: regex de <a href="/event/.../ID"> agrupado con nombre
        if not events_raw:
            event_links = set(re.findall(r'/event[os]?/([a-z0-9\-]+)/(\d{6,15})', html, re.I))
            for slug, eid in event_links:
                events_raw.append({
                    "_from_link": True,
                    "slug": slug, "eid": eid,
                    "name": (artist_name + " · " + _slug_to_title(slug)) if artist_name else _slug_to_title(slug),
                })

        if not events_raw:
            return None

        # Normaliza a formato local
        events_norm: list = []
        seen_ids = set()
        for raw in events_raw:
            norm = _normalize_scraped_event(raw, country, artist_name, url)
            if not norm: continue
            eid = norm.get("tm_id")
            if eid and eid in seen_ids: continue
            if eid: seen_ids.add(eid)
            events_norm.append(norm)

        if not events_norm:
            return None
        return {
            "ok": True, "source": "html_scrape",
            "artist_name": artist_name,
            "count": len(events_norm), "total": len(events_norm),
            "events": events_norm,
            "note_key": "disc.scrape_real",
            "note_args": {"count": len(events_norm)},
        }
    except Exception as e:
        return {"ok": False, "error": f"scrape_error: {type(e).__name__}: {str(e)[:100]}"}


def _find_events_in_json(obj, depth: int = 0, found: Optional[list] = None) -> list:
    """Busca recursivamente objetos event-like en un JSON arbitrario."""
    if found is None: found = []
    if depth > 10 or len(found) > 80:
        return found
    if isinstance(obj, dict):
        # Heurística: tiene name + (date/dates) + url/tm_id
        keys = set(obj.keys())
        looks_event = ("name" in keys or "title" in keys) and (
            "dates" in keys or "date" in keys or "startDate" in keys or "localDate" in keys)
        if looks_event:
            found.append(obj)
        for v in obj.values():
            _find_events_in_json(v, depth + 1, found)
    elif isinstance(obj, list):
        for v in obj:
            _find_events_in_json(v, depth + 1, found)
    return found


def _normalize_scraped_event(raw: dict, country: str, artist_name: str, source_url: str) -> Optional[dict]:
    """Convierte un evento extraído (JSON-LD, NEXT_DATA, etc.) al formato local."""
    try:
        name = raw.get("name") or raw.get("title") or (artist_name + " · Concert")
        # URL
        url = raw.get("url") or raw.get("tm_url") or ""
        if not url.startswith("http") and raw.get("eid"):
            tld = "es" if country == "es" else ("co.uk" if country == "uk" else "com")
            url = f"https://www.ticketmaster.{tld}/event/{raw.get('slug','')}/{raw['eid']}"
        # Fecha
        date_iso = ""
        for key in ("startDate", "dateTime", "localDate"):
            v = raw.get(key)
            if v:
                date_iso = v[:19] if len(v) >= 10 else v
                break
        if not date_iso and isinstance(raw.get("dates"), dict):
            dd = raw["dates"]
            start = dd.get("start") or {}
            date_iso = start.get("dateTime") or start.get("localDate") or ""
        # Venue
        venue_obj = raw.get("location") or raw.get("venue") or {}
        if isinstance(venue_obj, list): venue_obj = venue_obj[0] if venue_obj else {}
        venue = ""
        city = ""
        if isinstance(venue_obj, dict):
            venue = venue_obj.get("name") or ""
            addr = venue_obj.get("address") or {}
            if isinstance(addr, dict):
                city = addr.get("addressLocality") or addr.get("city") or ""
            if not city:
                city = venue_obj.get("city") or ""
        # Price
        offers = raw.get("offers") or {}
        if isinstance(offers, list): offers = offers[0] if offers else {}
        max_price = ""
        currency = {"es":"EUR","fr":"EUR","de":"EUR","it":"EUR","uk":"GBP"}.get(country, "USD")
        if isinstance(offers, dict):
            p = offers.get("highPrice") or offers.get("price")
            if p:
                try: max_price = float(p)
                except Exception: pass
            currency = offers.get("priceCurrency") or currency
        # TM ID
        tm_id = raw.get("eid") or raw.get("tm_id") or ""
        if not tm_id and url:
            eid = extract_event_id_from_url(url)
            if eid: tm_id = eid
        if not tm_id:
            tm_id = "scr_" + hashlib_hash(name + (url or ""))

        return {
            "tm_id": str(tm_id),
            "name": name[:200],
            "venue": venue[:120],
            "city": city[:80],
            "country": country,
            "date": date_iso,
            "sale_date": "",  # no suele estar en JSON-LD
            "tm_url": url,
            "max_price": max_price,
            "currency": currency,
            "tm_status": "onsale",
            "quantity": 2,
            "image_url": (raw.get("image") or [""])[0] if isinstance(raw.get("image"), list) else (raw.get("image") or ""),
            "classifications": [artist_name] if artist_name else [],
            "source": "scrape",
        }
    except Exception:
        return None


def _parse_date_local(d: Optional[dict]) -> str:
    """Convierte campo dates de TM a ISO local datetime string."""
    if not d:
        return ""
    start = d.get("start", {}) if isinstance(d, dict) else {}
    dt = start.get("dateTime") or start.get("localDate", "")
    if "T" in dt:
        return dt.replace("Z", "").split("+")[0]  # quitar timezone suffix
    return dt


def _to_local_event(tm_event: dict, country_hint: Optional[str]) -> dict:
    """Mapea evento TM Discovery al formato local de event_manager."""
    venues = (tm_event.get("_embedded") or {}).get("venues") or []
    venue = venues[0] if venues else {}
    city = (venue.get("city") or {}).get("name", "")
    country_obj = venue.get("country") or {}
    cc = country_obj.get("countryCode", "")
    # mapear inverso: US → us
    local_country = country_hint or next(
        (k for k, v in COUNTRY_CODE_MAP.items() if v == cc), cc.lower()
    )

    dates = tm_event.get("dates") or {}
    sales = tm_event.get("sales") or {}
    public_sales = sales.get("public") or {}

    prices = tm_event.get("priceRanges") or []
    max_price = 0
    if prices:
        max_price = max((p.get("max", 0) for p in prices), default=0)

    status = (dates.get("status") or {}).get("code", "")

    return {
        "tm_id": tm_event.get("id", ""),
        "name": tm_event.get("name", ""),
        "venue": venue.get("name", ""),
        "city": city,
        "country": local_country,
        "date": _parse_date_local(dates),
        "sale_date": public_sales.get("startDateTime", "").replace("Z", ""),
        "sale_end": public_sales.get("endDateTime", "").replace("Z", ""),
        "tm_url": tm_event.get("url", ""),
        "max_price": max_price,
        "quantity": 1,
        "currency": prices[0].get("currency", "EUR") if prices else "",
        "tm_status": status,  # onsale / offsale / rescheduled / postponed / cancelled
        "enabled": True,
        "image_url": _best_image(tm_event),
        "classifications": _classifications(tm_event),
        "source": "tm_discovery",
        "synced_at": datetime.now().isoformat(),
    }


def _best_image(tm_event: dict) -> str:
    imgs = tm_event.get("images") or []
    # elegir imagen grande tipo 16_9 de alta res
    preferred = [i for i in imgs if i.get("ratio") == "16_9" and i.get("width", 0) >= 640]
    if preferred:
        return preferred[0].get("url", "")
    if imgs:
        return imgs[0].get("url", "")
    return ""


def _classifications(tm_event: dict) -> List[str]:
    out = []
    for c in tm_event.get("classifications") or []:
        for k in ("segment", "genre", "subGenre"):
            obj = c.get(k) or {}
            name = obj.get("name")
            if name and name != "Undefined" and name not in out:
                out.append(name)
    return out


def sync_event_by_id(tm_event_id: str, import_to_local=True) -> Dict:
    """Sincroniza 1 evento: fetch detalle + (opcional) add al event_manager local."""
    r = get_event_detail(tm_event_id)
    if not r.get("ok"):
        return r
    if import_to_local:
        import event_manager
        local_ev = r["event"]
        # Evitar duplicados: si ya existe con mismo tm_id, saltar
        existing = [e for e in event_manager.list_events() if e.get("tm_id") == local_ev.get("tm_id")]
        if existing:
            return {"ok": True, "skipped": True, "existing_id": existing[0]["id"], "event": local_ev}
        added = event_manager.add_event(local_ev)
        return {"ok": True, "added": True, "event": added}
    return r


def bulk_import(tm_events: List[dict]) -> Dict:
    """Importa varios eventos al event_manager local. Salta duplicados por tm_id."""
    import event_manager
    existing_tm_ids = {e.get("tm_id") for e in event_manager.list_events() if e.get("tm_id")}
    added, skipped = 0, 0
    for ev in tm_events:
        if ev.get("tm_id") and ev["tm_id"] in existing_tm_ids:
            skipped += 1
            continue
        event_manager.add_event(ev)
        added += 1
    cfg = get_config()
    cfg["last_sync"] = datetime.now().isoformat()
    json_atomic.save(_settings_path(), cfg)
    return {"ok": True, "added": added, "skipped": skipped}


# ──────────────────────────────────────────────────────────
#  FEATURED (destacados / más populares en TM)
# ──────────────────────────────────────────────────────────
def _path_featured_cache():
    return config.ruta("featured_cache.json")


def _cache_get(country: str, max_age_min: int = 15) -> Optional[list]:
    d = json_atomic.load(_path_featured_cache(), default={"by_country": {}})
    entry = d.get("by_country", {}).get(country)
    if not entry:
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(entry["cached_at"])).total_seconds() / 60
    except Exception:
        return None
    if age > max_age_min:
        return None
    return entry.get("events")


def _cache_set(country: str, events: list):
    d = json_atomic.load(_path_featured_cache(), default={"by_country": {}})
    d.setdefault("by_country", {})[country] = {
        "cached_at": datetime.now().isoformat(),
        "events": events,
    }
    json_atomic.save(_path_featured_cache(), d)


def _is_placeholder_key(api_key: str) -> bool:
    """Detecta si una API key es un placeholder en lugar de una clave real.
    Acepta como placeholder: strings que contengan 'PLACEHOLDER', 'YOUR_',
    'XXXX', 'EXAMPLE', 'TEST_', o que tengan < 20 caracteres (keys TM reales son 32+).
    """
    if not api_key:
        return True
    k = api_key.upper()
    if any(tok in k for tok in ("PLACEHOLDER", "YOUR_", "XXXX", "EXAMPLE", "TEST_", "DUMMY", "FAKE")):
        return True
    # Keys Discovery API TM reales tienen 32 chars alfanuméricos
    if len(api_key) < 20:
        return True
    return False


# URLs de páginas populares por país — fuente de eventos reales vía scraping HTML público
# Los homepage dan contenido lazy-loaded; las páginas de artistas populares sí tienen JSON-LD con todos sus eventos.
TM_POPULAR_ARTISTS = {
    "es": [
        "https://www.ticketmaster.es/artist/el-ultimo-de-la-fila-entradas/1418653",
        "https://www.ticketmaster.es/artist/joaquin-sabina-entradas/806407",
        "https://www.ticketmaster.es/artist/bruce-springsteen-entradas/723689",
        "https://www.ticketmaster.es/artist/alejandro-sanz-entradas/802626",
        "https://www.ticketmaster.es/artist/bad-bunny-entradas/2260144",
        "https://www.ticketmaster.es/music",
        "https://www.ticketmaster.es/",
    ],
    "uk": [
        "https://www.ticketmaster.co.uk/music",
        "https://www.ticketmaster.co.uk/oasis-tickets/artist/2886497",
        "https://www.ticketmaster.co.uk/coldplay-tickets/artist/806431",
        "https://www.ticketmaster.co.uk/taylor-swift-tickets/artist/1741658",
        "https://www.ticketmaster.co.uk/ed-sheeran-tickets/artist/1368884",
        "https://www.ticketmaster.co.uk/",
    ],
    "us": [
        "https://www.ticketmaster.com/discover/concerts",
        "https://www.ticketmaster.com/bruce-springsteen-tickets/artist/723689",
        "https://www.ticketmaster.com/taylor-swift-tickets/artist/1741658",
        "https://www.ticketmaster.com/beyonce-tickets/artist/806431",
        "https://www.ticketmaster.com/",
    ],
    "de": [
        "https://www.ticketmaster.de/",
        "https://www.ticketmaster.de/artist/rammstein-tickets/702505",
        "https://www.ticketmaster.de/artist/helene-fischer-tickets/1003854",
        "https://www.ticketmaster.de/sport",
    ],
    "fr": [
        "https://www.ticketmaster.fr/",
        "https://www.ticketmaster.fr/artist/taylor-swift-billets/1741658",
    ],
    "it": [
        "https://www.ticketmaster.it/",
        "https://www.ticketmaster.it/artist/coldplay-biglietti/806431",
    ],
    "au": [
        "https://www.ticketmaster.com.au/",
        "https://www.ticketmaster.com.au/coldplay-tickets/artist/806431",
    ],
    "ca": [
        "https://www.ticketmaster.ca/",
        "https://www.ticketmaster.ca/taylor-swift-tickets/artist/1741658",
    ],
    "mx": [
        "https://www.ticketmaster.com.mx/",
    ],
    "nl": ["https://www.ticketmaster.nl/"],
    "be": ["https://www.ticketmaster.be/"],
    "ie": ["https://www.ticketmaster.ie/"],
}

# Locale headers por país — fuerza que TM sirva contenido regional
_LANG_HEADERS = {
    "es": "es-ES,es;q=0.9,en;q=0.5",
    "uk": "en-GB,en;q=0.9",
    "us": "en-US,en;q=0.9",
    "de": "de-DE,de;q=0.9,en;q=0.5",
    "fr": "fr-FR,fr;q=0.9,en;q=0.5",
    "it": "it-IT,it;q=0.9,en;q=0.5",
    "au": "en-AU,en;q=0.9",
    "ca": "en-CA,en;q=0.9,fr-CA;q=0.5",
    "mx": "es-MX,es;q=0.9",
    "nl": "nl-NL,nl;q=0.9",
    "be": "nl-BE,nl;q=0.9,fr-BE;q=0.5",
    "ie": "en-IE,en;q=0.9",
}


def _extract_spotlights_from_home(country: str) -> list:
    """Extrae los eventos que TM muestra en la HOME de cada país (secciones
    'spotlights', 'discover', 'featured' del Redux state en __NEXT_DATA__).

    Estos son EXACTAMENTE los que el usuario ve al abrir ticketmaster.es:
    hero carousel + 'Entradas más buscadas' + 'Descubre' + 'Destacados'.
    Devuelve lista de URLs de artistas/eventos descubiertos.
    """
    home_urls = {
        "es": "https://www.ticketmaster.es/",
        "uk": "https://www.ticketmaster.co.uk/",
        "us": "https://www.ticketmaster.com/",
        "de": "https://www.ticketmaster.de/",
        "fr": "https://www.ticketmaster.fr/",
        "it": "https://www.ticketmaster.it/",
        "au": "https://www.ticketmaster.com.au/",
        "ca": "https://www.ticketmaster.ca/",
        "mx": "https://www.ticketmaster.com.mx/",
        "nl": "https://www.ticketmaster.nl/",
        "be": "https://www.ticketmaster.be/",
        "ie": "https://www.ticketmaster.ie/",
    }
    url = home_urls.get(country)
    if not url: return []
    try:
        import requests, json as _json
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": _LANG_HEADERS.get(country, "en-US,en;q=0.9"),
        }, timeout=10)
        if r.status_code != 200:
            return []
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if not m: return []
        nd = _json.loads(m.group(1))
        api_q = (nd.get("props", {}).get("pageProps", {})
                 .get("initialReduxState", {}).get("api", {}).get("queries", {}))
        # Busca spotlights(undefined) o cualquier key que empiece con "spotlights"
        spotlights_data = None
        for key, val in api_q.items():
            if key.startswith("spotlights"):
                spotlights_data = val.get("data", {})
                break
        if not spotlights_data:
            return []
        discovered_urls = []
        for section_key in ("spotlights", "discover", "featured", "popular", "hero"):
            items = spotlights_data.get(section_key) or []
            if not isinstance(items, list): continue
            for item in items:
                if not isinstance(item, dict): continue
                u = (item.get("url") or "").strip()
                # Solo URLs de TM (artist/event), filtra campaign trackings externos
                if not u: continue
                if "ticketmaster" not in u.lower(): continue
                if "cmpgn.page" in u: continue  # landing pages externas
                # Sólo páginas que se pueden scrapear
                if "/artist/" in u or "/event/" in u or "/feature/" in u:
                    discovered_urls.append(u)
        # Dedupe preservando orden
        seen = set()
        uniq = []
        for u in discovered_urls:
            base = u.split("?")[0]
            if base in seen: continue
            seen.add(base)
            uniq.append(u)
        return uniq
    except Exception:
        return []


def _scrape_featured_from_tm(country: str, size: int = 20) -> Optional[Dict]:
    """Scraping HTML de las URLs DESTACADAS en la home de TM (lo mismo que ve el usuario).
    Estrategia:
    1) Fetch home de TM.{country} y extraer URLs de spotlights + discover + featured
       (secciones 'Entradas más buscadas', 'Descubre', 'Destacados' del Redux state).
    2) Scrape en paralelo cada URL extraída (+ fallback a artistas hardcoded si la
       home no devuelve suficientes URLs).
    3) Agrega todos los eventos, dedup por tm_id, ordena por fecha.
    """
    # PASO 1: URLs descubiertas en la home TM del país (PRIORIDAD — lo que el usuario ve)
    home_spotlights = _extract_spotlights_from_home(country)
    fallback_urls = TM_POPULAR_ARTISTS.get(country, [])
    # Home spotlights primero, luego fallback (sin duplicar)
    seen_u = set()
    urls = []
    for u in home_spotlights + fallback_urls:
        base = u.split("?")[0]
        if base in seen_u: continue
        seen_u.add(base)
        urls.append(u)
    if not urls:
        return None
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": _LANG_HEADERS.get(country, "en-US,en;q=0.9"),
    }

    def _scrape_single(url: str) -> list:
        """Extrae eventos de una URL."""
        try:
            r = requests.get(url, headers=headers, timeout=8, allow_redirects=True)
            if r.status_code != 200:
                return []
            html = r.text
            events_raw = []
            # A. JSON-LD
            for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                                   html, re.DOTALL | re.I):
                try:
                    blob = __import__("json").loads(m.group(1).strip())
                    if isinstance(blob, list):
                        for item in blob:
                            if isinstance(item, dict) and item.get("@type") == "Event":
                                events_raw.append(item)
                    elif isinstance(blob, dict):
                        if blob.get("@type") == "Event":
                            events_raw.append(blob)
                        for key in ("@graph", "subEvent", "hasPart", "event"):
                            items = blob.get(key) or []
                            if isinstance(items, list):
                                for it in items:
                                    if isinstance(it, dict) and (it.get("@type") == "Event" or it.get("name")):
                                        events_raw.append(it)
                except Exception:
                    continue
            # B. __NEXT_DATA__
            if not events_raw:
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
                if m:
                    try:
                        nd = __import__("json").loads(m.group(1).strip())
                        events_raw = _find_events_in_json(nd)
                    except Exception:
                        pass
            # C. Fallback regex
            if not events_raw:
                for slug, eid in set(re.findall(r'/event[os]?/([a-z0-9\-]+)/(\d{6,15})', html, re.I)):
                    events_raw.append({"_from_link": True, "slug": slug, "eid": eid,
                                        "name": _slug_to_title(slug)})
            # Normaliza
            out = []
            for raw in events_raw:
                norm = _normalize_scraped_event(raw, country, "", url)
                if norm:
                    out.append(norm)
            return out
        except Exception:
            return []

    # Scrape en paralelo (máx 6 concurrentes)
    all_events = []
    seen_ids = set()
    with ThreadPoolExecutor(max_workers=min(6, len(urls))) as ex:
        futures = {ex.submit(_scrape_single, u): u for u in urls}
        for fut in as_completed(futures, timeout=20):
            try:
                events = fut.result()
                # Limitar a máx 3 eventos del mismo artista para dar diversidad
                per_url_events = 0
                for ev in events:
                    eid = ev.get("tm_id")
                    if eid and eid in seen_ids: continue
                    if eid: seen_ids.add(eid)
                    # Filtra eventos sin fecha (suelen ser falsos positivos)
                    if not ev.get("date") and not ev.get("venue"):
                        continue
                    all_events.append(ev)
                    per_url_events += 1
                    if per_url_events >= 3:  # max 3 por URL para diversidad
                        break
            except Exception:
                continue
            if len(all_events) >= size * 2:
                break

    if not all_events:
        return None

    # Ordena por fecha ascendente (próximos primero)
    all_events.sort(key=lambda e: e.get("date") or "9999")
    return {
        "ok": True, "country": country, "source": "html_scrape",
        "events": all_events[:size],
        "count": len(all_events[:size]),
    }


def get_featured(country: str = "us", size: int = 20, force_refresh: bool = False) -> Dict:
    """Obtiene eventos destacados de TM por país.

    Estrategia (prioriza eventos REALES):
    1. Caché (15 min) si disponible.
    2. Discovery API si hay key válida.
    3. Scraping HTML público de TM (sin key) → eventos REALES.
    4. Fallback demo si todo falla.
    """
    if not force_refresh:
        cached = _cache_get(country)
        if cached is not None:
            return {"ok": True, "country": country, "cached": True, "events": cached[:size]}

    cfg = get_config()
    api_key = cfg.get("api_key", "")
    has_valid_key = not _is_placeholder_key(api_key)

    # 1) Si hay API key real → Discovery API
    if has_valid_key:
        import requests
        cc = COUNTRY_CODE_MAP.get(country, country.upper())
        params = {
            "apikey": api_key,
            "countryCode": cc,
            "size": min(max(size, 1), 50),
            "sort": "relevance,desc",
            "onsaleOnStartDate": datetime.now().strftime("%Y-%m-%d"),
        }
        try:
            r = requests.get(DISCOVERY_BASE + "/events.json", params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                evs = (data.get("_embedded") or {}).get("events") or []
                out = [_to_local_event(e, country) for e in evs]
                if out:
                    _cache_set(country, out)
                    return {"ok": True, "country": country, "cached": False,
                            "source": "discovery_api", "events": out[:size]}
            # 401, 429, 5xx → cae al scraping
        except Exception:
            pass

    # 2) Scraping HTML (siempre intenta, incluso sin API key)
    try:
        scraped = _scrape_featured_from_tm(country, size=size)
        if scraped and scraped.get("events"):
            _cache_set(country, scraped["events"])
            return {
                "ok": True, "country": country, "cached": False,
                "source": "html_scrape",
                "events": scraped["events"],
                "note_key": "disc.scrape_real",
                "note_args": {"count": len(scraped['events'])},
            }
    except Exception:
        pass

    # 3) Fallback demo (último recurso)
    stale = _cache_get(country, max_age_min=99999)
    if stale:
        return {"ok": True, "country": country, "cached": True, "stale": True,
                "events": stale[:size], "warn_key": "disc.cache_stale"}
    return {
        "ok": True, "country": country, "cached": False, "demo": True,
        "events": _demo_featured(country, size),
        "note_key": "disc.demo_default",
    }


def _demo_featured(country: str, size: int) -> list:
    """Datos de demostración cuando no hay API key configurada."""
    base = datetime.now()
    demos = [
        ("Coldplay · Music of the Spheres Tour", "Estadio Metropolitano", "Madrid", "Rock"),
        ("Taylor Swift · Eras Tour", "Wembley Stadium", "London", "Pop"),
        ("Bad Bunny · Most Wanted Tour", "Madison Square Garden", "New York", "Reggaeton"),
        ("Beyoncé · Renaissance World Tour", "Tottenham Stadium", "London", "R&B"),
        ("The Rolling Stones · Hackney Diamonds", "Allianz Arena", "Munich", "Rock"),
        ("U2 · Atomic City Tour", "Sphere", "Las Vegas", "Rock"),
        ("Billie Eilish · Hit Me Hard Tour", "Palau Sant Jordi", "Barcelona", "Alternative"),
        ("Oasis · Live '25 Reunion", "Heaton Park", "Manchester", "Britpop"),
        ("Madonna · Celebration Tour", "Accor Arena", "Paris", "Pop"),
        ("Dua Lipa · Radical Optimism", "WiZink Center", "Madrid", "Pop"),
        ("Real Madrid vs FC Barcelona", "Santiago Bernabéu", "Madrid", "Sports · Football"),
        ("Lakers vs Warriors", "Crypto.com Arena", "Los Angeles", "Sports · NBA"),
    ]
    out = []
    import hashlib
    for i, (name, venue, city, genre) in enumerate(demos[:size]):
        sale_offset = (i - 2) * 86400
        date_offset = (i + 30) * 86400
        fake_id = hashlib.md5(f"{country}{name}".encode()).hexdigest()[:16].upper()
        out.append({
            "tm_id": f"G5v{fake_id}",
            "name": name,
            "venue": venue,
            "city": city,
            "country": country,
            "date": (base + timedelta(seconds=date_offset)).isoformat()[:19],
            "sale_date": (base + timedelta(seconds=sale_offset)).isoformat()[:19],
            "tm_url": f"https://www.ticketmaster.{ 'es' if country=='es' else 'com' if country=='us' else 'co.uk' if country=='uk' else 'de' }/event/{fake_id}",
            "max_price": 150 + i * 25,
            "quantity": 2,
            "currency": "EUR" if country in ("es","fr","de","it") else ("GBP" if country=="uk" else "USD"),
            "tm_status": "onsale" if i % 3 != 2 else "offsale",
            "enabled": True,
            "image_url": f"https://s1.ticketm.net/dam/a/{fake_id[:2].lower()}/placeholder.jpg",
            "classifications": [genre],
            "source": "demo",
        })
    return out


# ──────────────────────────────────────────────────────────
#  SCHEDULER (auto-refresh destacados en background)
# ──────────────────────────────────────────────────────────
from datetime import timedelta
import threading

_scheduler_state = {
    "running": False,
    "thread": None,
    "stop_event": None,
    "last_tick": None,
    "ticks": 0,
}


def scheduler_status() -> dict:
    cfg = get_config()
    return {
        "running": _scheduler_state["running"],
        "enabled": cfg.get("auto_sync_enabled", False),
        "interval_min": cfg.get("sync_interval_min", 60),
        "last_tick": _scheduler_state["last_tick"],
        "ticks": _scheduler_state["ticks"],
        "last_sync": cfg.get("last_sync"),
    }


def _scheduler_loop(stop_event):
    """Loop de scheduler: cada N min refresca featured para los dominios seleccionados."""
    import domain_manager
    while not stop_event.is_set():
        cfg = get_config()
        if not cfg.get("auto_sync_enabled"):
            stop_event.wait(30)
            continue
        try:
            # Refresca featured por cada dominio activo
            selected = domain_manager.get_selected() or {"selected": ["us"]}
            for dom_id in selected.get("selected", ["us"]):
                get_featured(dom_id, size=20, force_refresh=True)
            _scheduler_state["last_tick"] = datetime.now().isoformat()
            _scheduler_state["ticks"] += 1
            cfg["last_sync"] = datetime.now().isoformat()
            json_atomic.save(_settings_path(), cfg)
        except Exception:
            pass
        interval = int(cfg.get("sync_interval_min", 60))
        stop_event.wait(max(60, interval * 60))


def start_scheduler() -> dict:
    if _scheduler_state["running"]:
        return {"ok": True, "already_running": True}
    stop = threading.Event()
    t = threading.Thread(target=_scheduler_loop, args=(stop,), daemon=True, name="discovery-scheduler")
    _scheduler_state.update({"running": True, "thread": t, "stop_event": stop})
    t.start()
    return {"ok": True, "started": True}


def stop_scheduler() -> dict:
    if not _scheduler_state["running"]:
        return {"ok": True, "not_running": True}
    ev = _scheduler_state.get("stop_event")
    if ev:
        ev.set()
    _scheduler_state.update({"running": False, "thread": None, "stop_event": None})
    return {"ok": True, "stopped": True}
