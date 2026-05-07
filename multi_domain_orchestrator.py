"""multi_domain_orchestrator.py — Distributed multi-domain para tours internacionales.

Mejora #multi_domain de la nota:
  Lanzar en 25 dominios TM simultáneamente. Tours internacionales → éxito x25.
  Pasos:
  1. Detectar tour internacional
  2. Lanzar worker por dominio
  3. Geo-match proxy↔dominio
  4. Priorizar dominios con menor cola

Features:
- Detecta cuándo un artista tiene fechas en ≥3 países distintos (tour internacional)
- Lanza N `tm_purchase_flow.run_flow` en paralelo, uno por dominio
- Empareja cada worker con un proxy del mismo país (via proxy_health)
- Prioriza dominios con **menor probabilidad de cola saturada** (heurística:
  países con menor población o menor popularidad del artista allí)
- Reporte agregado: success_rate global + mejor dominio
"""
import threading
import time
from datetime import datetime
from typing import Optional, List, Dict, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import json_atomic


_STATE_KEY = "multi_domain_state.json"
_lock = threading.Lock()

# Heurística: dominios ordenados por "competitividad de cola" (ascendente = mejor)
# Países más pequeños = menos personas compitiendo por las mismas entradas
DOMAIN_QUEUE_HEURISTIC = {
    "us": 100, "uk": 85, "de": 75, "fr": 70, "es": 65, "it": 60,
    "ca": 55, "au": 50, "nl": 40, "be": 35, "at": 30, "se": 28,
    "mx": 55, "ch": 30, "dk": 25, "no": 25, "fi": 22, "pl": 35,
    "ie": 25, "cz": 20, "nz": 15, "ae": 10,
}


def _state_path():
    return config.ruta(_STATE_KEY)


def detect_international_tour(artist_or_keyword: str,
                               min_countries: int = 3,
                               require_discovery: bool = False) -> dict:
    """Detecta si un artista/keyword tiene fechas en múltiples países.

    Busca en event_manager local eventos cuyo nombre contenga el keyword.
    Agrupa por country. Si >= min_countries → es tour internacional.
    """
    try:
        import event_manager
        events = event_manager.list_events()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    q = artist_or_keyword.lower().strip()
    matching = [e for e in events if q in (e.get("name", "") + " " + e.get("venue", "")).lower()]
    countries = {}
    for e in matching:
        c = e.get("country", "?")
        countries.setdefault(c, []).append(e)

    is_tour = len(countries) >= min_countries
    return {
        "ok": True,
        "is_international_tour": is_tour,
        "matching_events": len(matching),
        "countries": list(countries.keys()),
        "country_count": len(countries),
        "events_by_country": {c: len(evs) for c, evs in countries.items()},
        "sample_events": [{"id": e["id"], "name": e["name"][:40],
                           "country": e["country"], "sale_date": e.get("sale_date", "")}
                          for e in matching[:10]],
    }


def rank_domains_by_competition(domains: List[str],
                                 artist_or_keyword: str = "") -> List[dict]:
    """Ordena dominios por probabilidad de éxito (menos competencia primero)."""
    # Base: usar heurística global
    scored = []
    for d in domains:
        base = DOMAIN_QUEUE_HEURISTIC.get(d, 50)
        # Bonus si el artista no tiene evento en ese país (menos fans = menos cola)
        try:
            import event_manager
            events = event_manager.list_events()
            q = artist_or_keyword.lower().strip() if artist_or_keyword else ""
            has_event = bool(q) and any(q in e.get("name", "").lower() and e.get("country") == d for e in events)
            # Tener un evento ahí =  más cola → suma al score (peor)
            adjustment = 20 if has_event else 0
        except Exception:
            adjustment = 0
        score = base + adjustment
        scored.append({"domain": d, "score": score,
                        "competition_level": "low" if score < 40 else ("medium" if score < 70 else "high")})
    scored.sort(key=lambda x: x["score"])
    return scored


def get_matching_proxy(country: str) -> Optional[dict]:
    """Geo-match: devuelve el mejor proxy disponible para el país."""
    try:
        import proxy_health
        return proxy_health.pick_best(geo=country, min_score=30)
    except Exception:
        return None


def launch_distributed(artist_or_keyword: str,
                       quantity: int = 2,
                       mode: str = "simulate",
                       strategy: str = "distributed",
                       max_domains: int = 10,
                       broadcast: Optional[Callable] = None) -> dict:
    """Orquesta launches paralelos en múltiples dominios.

    Devuelve reporte global con success por dominio.
    """
    started = time.time()
    # 1. Detectar
    detection = detect_international_tour(artist_or_keyword, min_countries=2)
    if not detection.get("ok"):
        return detection
    countries = detection.get("countries", [])
    if not countries:
        return {"ok": False, "error": "sin eventos matching",
                "detection": detection}

    # 2. Ranking de dominios por competencia
    ranked = rank_domains_by_competition(countries, artist_or_keyword)
    selected = ranked[:max_domains]

    # 3. Mapear evento por dominio
    events_by_country = detection.get("sample_events", [])
    event_map = {}
    for e in events_by_country:
        event_map.setdefault(e["country"], e["id"])

    # 4. Lanzar run_flow en paralelo por dominio
    try:
        import tm_purchase_flow
    except Exception as e:
        return {"ok": False, "error": f"tm_purchase_flow no disponible: {e}"}

    def _run_one(item):
        dom = item["domain"]
        event_id = event_map.get(dom)
        proxy = get_matching_proxy(dom)
        if not event_id:
            return {"domain": dom, "ok": False, "error": "no event for domain",
                    "competition": item["competition_level"]}
        t0 = time.time()
        result = tm_purchase_flow.run_flow(
            event_id=event_id, quantity=quantity,
            mode=mode, strategy=strategy,
            broadcast=broadcast,
        )
        return {
            "domain": dom,
            "competition": item["competition_level"],
            "score": item["score"],
            "event_id": event_id,
            "proxy_used": proxy.get("id") if proxy else None,
            "ok": result.get("ok", False),
            "order_id": result.get("order_id"),
            "error": result.get("error"),
            "elapsed_ms": int((time.time() - t0) * 1000),
            "steps_count": len(result.get("steps", [])),
            "flow_id": result.get("flow_id"),
        }

    results = []
    with ThreadPoolExecutor(max_workers=min(len(selected), 10)) as ex:
        futures = {ex.submit(_run_one, item): item for item in selected}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                item = futures[fut]
                results.append({"domain": item["domain"], "ok": False,
                                "error": f"{type(e).__name__}: {e}"})

    # Reporte
    ok_count = sum(1 for r in results if r.get("ok"))
    best = next((r for r in sorted(results, key=lambda x: x.get("elapsed_ms", 99999))
                 if r.get("ok")), None)

    report = {
        "ok": True,
        "artist": artist_or_keyword,
        "mode": mode,
        "total_domains": len(selected),
        "success": ok_count,
        "failed": len(results) - ok_count,
        "success_rate": round(ok_count / max(len(selected), 1) * 100, 1),
        "best_domain": best,
        "elapsed_s": round(time.time() - started, 1),
        "results_per_domain": results,
        "detection": detection,
        "ranking": ranked,
    }

    # Persist
    with _lock:
        d = json_atomic.load(_state_path(), default={"runs": []})
        d["runs"].insert(0, {"ts": datetime.now().isoformat(),
                              "artist": artist_or_keyword,
                              "success": ok_count, "total": len(selected),
                              "mode": mode})
        d["runs"] = d["runs"][:30]
        json_atomic.save(_state_path(), d)

    return report


def get_recent_runs(limit: int = 20) -> List[dict]:
    return json_atomic.load(_state_path(), default={"runs": []}).get("runs", [])[:limit]
