"""tm_section_api.py — API directa de availability/secciones de TM (ismds).

TM expone públicamente endpoints "ismds" (Inventory Smart Display System) que
devuelven las secciones disponibles + price levels + areas en JSON. Mucho más
rápido que parsear el SVG del mapa cargado por JS.

Endpoints conocidos (verificados Q2 2026):
    GET https://www.ticketmaster.<tld>/api/ismds/event/<tm_id>/quickpicks/<qty>
    GET https://www.ticketmaster.<tld>/api/ismds/event/<tm_id>/availability
    GET https://app.ticketmaster.com/api-network/v3/events/<tm_id>/availability

API:
    fetch_availability(tm_id, country='es') -> dict
        Devuelve {ok, sections:[{id, name, available, price_min, price_max,
                                  area_id, places_count}], raw}.
    fetch_via_browser(page, tm_id) -> dict
        Captura las llamadas que hace TM en el browser (page.context) — fallback
        cuando la API directa requiere headers/cookies anti-bot.
    extract_from_intercepted_responses(page, tm_id) -> dict
        Lee responses ismds ya cacheadas en el contexto de la página.
"""
from __future__ import annotations
import re
import time
from typing import Dict, Any, List, Optional


_TM_DOMAINS = {
    "es": "ticketmaster.es",
    "uk": "ticketmaster.co.uk",
    "fr": "ticketmaster.fr",
    "de": "ticketmaster.de",
    "ie": "ticketmaster.ie",
    "it": "ticketmaster.it",
    "nl": "ticketmaster.nl",
    "be": "ticketmaster.be",
    "at": "ticketmaster.at",
    "ch": "ticketmaster.ch",
    "us": "ticketmaster.com",
    "ca": "ticketmaster.ca",
    "mx": "ticketmaster.com.mx",
    "au": "ticketmaster.com.au",
    "nz": "ticketmaster.co.nz",
}


def _domain_for(country: str) -> str:
    return _TM_DOMAINS.get((country or "us").lower(), "ticketmaster.com")


def _candidate_urls(tm_id: str, country: str, qty: int = 4) -> List[str]:
    """Lista de URLs ismds candidatas, en orden de preferencia."""
    dom = _domain_for(country)
    return [
        f"https://{dom}/api/ismds/event/{tm_id}/quickpicks/{qty}",
        f"https://{dom}/api/ismds/event/{tm_id}/availability",
        f"https://{dom}/api/ismds/event/{tm_id}/areas",
        f"https://{dom}/api/products/{tm_id}/availability",
    ]


def _normalize_sections(d: Any) -> List[Dict[str, Any]]:
    """Extrae lista de secciones de cualquier respuesta ismds posible.
    TM tiene varios formatos: {areas:[...]} / {pages:[...]} / {sections:[...]}."""
    out: List[Dict[str, Any]] = []
    if not isinstance(d, dict):
        return out
    candidates = []
    for k in ("areas", "sections", "pages", "places", "items"):
        v = d.get(k)
        if isinstance(v, list):
            candidates.extend(v)
    # También revisar nested .data / .response
    for k in ("data", "response", "result"):
        sub = d.get(k)
        if isinstance(sub, dict):
            out.extend(_normalize_sections(sub))
    for it in candidates:
        if not isinstance(it, dict):
            continue
        sec_id = (it.get("areaId") or it.get("id") or it.get("sectionId")
                  or it.get("section_id") or "")
        sec_name = (it.get("name") or it.get("areaName") or it.get("label")
                    or it.get("section") or "")
        avail = (it.get("availableCount") or it.get("available")
                 or it.get("count") or it.get("places_count") or 0)
        prices = it.get("priceLevels") or it.get("prices") or []
        price_vals = []
        if isinstance(prices, list):
            for p in prices:
                if isinstance(p, dict):
                    v = p.get("price") or p.get("amount") or p.get("value")
                    if v is not None:
                        try: price_vals.append(float(v))
                        except (TypeError, ValueError): pass
                elif isinstance(p, (int, float)):
                    price_vals.append(float(p))
        out.append({
            "id": str(sec_id),
            "name": str(sec_name)[:60],
            "available": int(avail or 0),
            "price_min": min(price_vals) if price_vals else None,
            "price_max": max(price_vals) if price_vals else None,
            "area_id": it.get("areaId") or "",
            "places_count": len(it.get("places", [])) if isinstance(it.get("places"), list) else None,
            "source_keys": list(it.keys())[:8],
        })
    return out


def fetch_availability(tm_id: str, country: str = "es",
                        qty: int = 4, timeout: int = 8,
                        cookies: Optional[Dict[str, str]] = None,
                        proxy: Optional[str] = None) -> Dict[str, Any]:
    """Llamada HTTP directa a la API ismds. Devuelve {ok, sections, raw_url, status}.
    Si TM bloquea (403/406) → ok=False con raw para que el caller pruebe via browser.
    """
    started = time.time()
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests_missing"}
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/146.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": f"https://{_domain_for(country)}/",
        "Origin": f"https://{_domain_for(country)}",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    last_err = None
    for url in _candidate_urls(tm_id, country, qty):
        try:
            r = requests.get(url, headers=headers, cookies=cookies or {},
                             proxies=proxies, timeout=timeout)
            if r.status_code != 200:
                last_err = {"url": url, "status": r.status_code,
                            "body": (r.text or "")[:120]}
                continue
            try:
                data = r.json()
            except Exception:
                last_err = {"url": url, "status": r.status_code,
                            "body": (r.text or "")[:120],
                            "err": "not_json"}
                continue
            sections = _normalize_sections(data)
            return {
                "ok": True, "url": url, "status": r.status_code,
                "sections": sections,
                "sections_count": len(sections),
                "available_total": sum(s.get("available", 0) for s in sections),
                "elapsed_ms": int((time.time() - started) * 1000),
                "raw_top_keys": list(data.keys())[:10] if isinstance(data, dict) else [],
            }
        except Exception as e:
            last_err = {"url": url, "error": f"{type(e).__name__}: {str(e)[:100]}"}
            continue
    return {"ok": False, "tried": len(_candidate_urls(tm_id, country, qty)),
            "last_err": last_err,
            "elapsed_ms": int((time.time() - started) * 1000)}


def fetch_via_browser(page, tm_id: str, qty: int = 4) -> Dict[str, Any]:
    """Captura llamadas ismds que TM hace al cargar la página del evento.
    Si la página ya cargó, mira el cache de responses; si no, las intercepta.
    Devuelve mismo shape que fetch_availability."""
    started = time.time()
    captured = {"data": None, "url": ""}

    def on_response(resp):
        try:
            url = resp.url
            if "/api/ismds/" not in url and "/availability" not in url:
                return
            if resp.status >= 400: return
            try:
                d = resp.json()
            except Exception:
                return
            if isinstance(d, dict):
                captured["data"] = d
                captured["url"] = url
        except Exception:
            pass

    page.on("response", on_response)
    try:
        # Disparar reload o forzar interaction que cause llamada ismds
        try:
            page.evaluate("() => { try { fetch(`/api/ismds/event/" + str(tm_id) +
                          "/quickpicks/" + str(qty) + "`); } catch(e){} }")
        except Exception: pass
        deadline = time.time() + 10
        while time.time() < deadline and not captured["data"]:
            page.wait_for_timeout(400)
    finally:
        try: page.remove_listener("response", on_response)
        except Exception: pass

    if not captured["data"]:
        return {"ok": False, "error": "no_ismds_response_captured",
                "elapsed_ms": int((time.time() - started) * 1000)}
    sections = _normalize_sections(captured["data"])
    return {"ok": True, "url": captured["url"],
            "sections": sections, "sections_count": len(sections),
            "available_total": sum(s.get("available", 0) for s in sections),
            "via": "browser_intercept",
            "elapsed_ms": int((time.time() - started) * 1000)}


def best_section_match(api_sections: List[Dict[str, Any]],
                        svg_sections: List[Dict[str, Any]]) -> Dict[str, str]:
    """Cruza secciones del API ismds con secciones del SVG por nombre normalizado.
    Devuelve mapping {svg_section_id: api_section_id} para que el bot pueda
    elegir un sector del SVG y llamar directamente a la API por su ID real TM."""
    def _norm(s):
        s = (s or "").lower().strip()
        s = re.sub(r"[^a-z0-9]+", " ", s).strip()
        return s
    by_name = {}
    for a in api_sections:
        n = _norm(a.get("name"))
        if n: by_name[n] = a.get("id")
    mapping = {}
    for s in svg_sections:
        n = _norm(s.get("section_name"))
        if n in by_name:
            mapping[s.get("section_id")] = by_name[n]
    return mapping
