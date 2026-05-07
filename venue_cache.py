"""venue_cache.py — Caché de estadios/recintos compartido entre eventos.

Muchos artistas actúan en el mismo recinto (Estadi Olímpic, Wanda, Bernabéu, etc.).
Este módulo persiste las secciones detectadas y la imagen del mapa **una sola vez por venue**
y los reutiliza para cualquier evento futuro en el mismo sitio.

Estructura del JSON `data/venue_cache.json`:
{
  "venues": {
    "<venue_id>": {
      "venue_id": "estadi-olimpic-lluis-companys-barcelona",
      "name": "Estadi Olímpic Lluis Companys",
      "city": "Barcelona", "country": "es",
      "sections": [
        {"section_id": "s_27", "section_name": "SECTOR 123", "active": true, "fill": "rgb(...)", "color_hex": "#..."},
        ...
      ],
      "image_path": "datos/venues/estadi-olimpic-lluis-companys-barcelona/map.png",
      "scanned_at": "2026-04-26T...",
      "linked_events": ["2059118961", ...],   # tm_ids vistos en este recinto
      "scan_count": 1
    }
  },
  "by_event": {
    "<tm_id>": "<venue_id>"   # lookup rápido evento→venue
  }
}

API:
    venue_id_from_meta(name, city) -> str
    get_venue(venue_id) -> dict|None
    get_venue_for_event(tm_id) -> dict|None
    save_venue(venue_id, name, city, country, sections, image_bytes, tm_id) -> dict
    list_venues() -> list[dict]
    link_event_to_venue(tm_id, venue_id) -> bool
"""
from __future__ import annotations
import os, json, re, hashlib, threading
from datetime import datetime
from typing import Dict, List, Optional, Any

import config
import json_atomic
import sqlite_store

_CACHE_KEY = "venue_cache.json"
_VENUES_DIR = os.path.join(os.path.dirname(__file__), "datos", "venues")
os.makedirs(_VENUES_DIR, exist_ok=True)
_lock = threading.RLock()


def _slugify(s: str) -> str:
    """'Estadi Olímpic Lluis Companys' → 'estadi-olimpic-lluis-companys'"""
    if not s: return "venue-unknown"
    s = s.lower().strip()
    s = re.sub(r'[áàäâã]', 'a', s); s = re.sub(r'[éèëê]', 'e', s)
    s = re.sub(r'[íìïî]', 'i', s); s = re.sub(r'[óòöôõ]', 'o', s)
    s = re.sub(r'[úùüû]', 'u', s); s = s.replace('ñ', 'n').replace('ç', 'c')
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'\s+', '-', s).strip('-')
    return s[:80] or 'venue-' + hashlib.md5((s or 'x').encode()).hexdigest()[:8]


def venue_id_from_meta(name: str, city: str = "") -> str:
    """Genera venue_id estable desde nombre + ciudad."""
    base = _slugify(name)
    if city:
        base = f"{base}-{_slugify(city)}"
    return base


def _load() -> Dict[str, Any]:
    return sqlite_store.load(config.ruta(_CACHE_KEY),
                            default={"venues": {}, "by_event": {}})


def _save(d: Dict[str, Any]) -> None:
    sqlite_store.save(config.ruta(_CACHE_KEY), d)


def get_venue(venue_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        d = _load()
        return d.get("venues", {}).get(venue_id)


def list_venues() -> List[Dict[str, Any]]:
    with _lock:
        d = _load()
        return list((d.get("venues") or {}).values())


def get_venue_for_event(tm_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        d = _load()
        vid = (d.get("by_event") or {}).get(tm_id)
        if not vid: return None
        return (d.get("venues") or {}).get(vid)


def save_venue(venue_id: str, name: str, city: str = "", country: str = "",
               sections: Optional[List[Dict[str, Any]]] = None,
               image_bytes: Optional[bytes] = None,
               tm_id: Optional[str] = None,
               source_url: Optional[str] = None) -> Dict[str, Any]:
    """Guarda/actualiza un venue en caché. Si imagen viene → la persiste a disco.
    Si tm_id viene → vincula evento ↔ venue."""
    with _lock:
        d = _load()
        venues = d.setdefault("venues", {})
        existing = venues.get(venue_id, {})
        # Imagen
        image_path = existing.get("image_path", "")
        if image_bytes:
            vd = os.path.join(_VENUES_DIR, venue_id)
            os.makedirs(vd, exist_ok=True)
            image_path = os.path.join(vd, "map.png")
            with open(image_path, "wb") as f:
                f.write(image_bytes)
            # Path relativo al proyecto para servir
            image_path = os.path.relpath(image_path, os.path.dirname(__file__)).replace("\\", "/")

        # Linked events
        linked = list(existing.get("linked_events", []))
        if tm_id and tm_id not in linked:
            linked.append(tm_id)

        venue = {
            "venue_id": venue_id,
            "name": name or existing.get("name", ""),
            "city": city or existing.get("city", ""),
            "country": country or existing.get("country", ""),
            "sections": sections if sections is not None else existing.get("sections", []),
            "image_path": image_path,
            "source_url": source_url or existing.get("source_url", ""),
            "scanned_at": datetime.now().isoformat(),
            "linked_events": linked,
            "scan_count": int(existing.get("scan_count", 0)) + 1,
        }
        venues[venue_id] = venue

        # By-event lookup
        if tm_id:
            d.setdefault("by_event", {})[tm_id] = venue_id

        _save(d)
        return venue


def link_event_to_venue(tm_id: str, venue_id: str) -> bool:
    with _lock:
        d = _load()
        if venue_id not in d.get("venues", {}):
            return False
        d.setdefault("by_event", {})[tm_id] = venue_id
        # añadir tm_id a linked_events
        v = d["venues"][venue_id]
        if tm_id not in v.get("linked_events", []):
            v.setdefault("linked_events", []).append(tm_id)
        _save(d)
        return True


def get_image_path_abs(venue_id: str) -> Optional[str]:
    """Devuelve path absoluto a la imagen del mapa del venue (o None)."""
    v = get_venue(venue_id)
    if not v or not v.get("image_path"): return None
    p = v["image_path"]
    if not os.path.isabs(p):
        p = os.path.join(os.path.dirname(__file__), p)
    return p if os.path.exists(p) else None


def rebuild_from_disk() -> Dict[str, Any]:
    """Reconstruye venue_cache.json a partir de las carpetas datos/venues/<slug>/
    que tengan map.png. Útil cuando se borró el JSON pero los assets siguen en disco.
    Recupera: venue_id, name, city (desde slug), image_path. Sections queda vacío
    hasta el próximo scan que ejecute escaneo real del SVG."""
    with _lock:
        d = _load()
        venues = d.setdefault("venues", {})
        recovered = 0
        skipped = 0
        if not os.path.isdir(_VENUES_DIR):
            return {"ok": True, "recovered": 0, "skipped": 0,
                    "reason": "no_venues_dir"}
        for slug in os.listdir(_VENUES_DIR):
            vd = os.path.join(_VENUES_DIR, slug)
            if not os.path.isdir(vd):
                continue
            map_png = os.path.join(vd, "map.png")
            if not os.path.exists(map_png):
                skipped += 1
                continue
            if slug in venues and venues[slug].get("image_path"):
                skipped += 1
                continue
            parts = slug.replace("-", " ").split(" ")
            city_guess = parts[-1].title() if parts else ""
            name_guess = " ".join(p.title() for p in parts[:-1]) if len(parts) > 1 \
                else slug.replace("-", " ").title()
            image_path = os.path.relpath(map_png, os.path.dirname(__file__)) \
                .replace("\\", "/")
            venues[slug] = {
                "venue_id": slug,
                "name": (venues.get(slug) or {}).get("name") or name_guess,
                "city": (venues.get(slug) or {}).get("city") or city_guess,
                "country": (venues.get(slug) or {}).get("country", ""),
                "sections": (venues.get(slug) or {}).get("sections", []),
                "image_path": image_path,
                "source_url": (venues.get(slug) or {}).get("source_url", ""),
                "scanned_at": (venues.get(slug) or {}).get("scanned_at",
                               datetime.fromtimestamp(os.path.getmtime(map_png))
                                       .isoformat()),
                "linked_events": (venues.get(slug) or {}).get("linked_events", []),
                "scan_count": int((venues.get(slug) or {}).get("scan_count", 0)) or 1,
                "rebuilt_from_disk": True,
            }
            recovered += 1
        _save(d)
        return {"ok": True, "recovered": recovered, "skipped": skipped,
                "venues_total": len(venues)}


def stats() -> Dict[str, Any]:
    with _lock:
        d = _load()
        return {
            "venues": len(d.get("venues") or {}),
            "events_linked": len(d.get("by_event") or {}),
            "with_image": sum(1 for v in (d.get("venues") or {}).values() if v.get("image_path")),
            "total_sections": sum(len(v.get("sections") or []) for v in (d.get("venues") or {}).values()),
        }


if __name__ == "__main__":
    # Demo
    vid = venue_id_from_meta("Estadi Olímpic Lluis Companys", "Barcelona")
    print(f"venue_id: {vid}")
    print(f"stats: {stats()}")
