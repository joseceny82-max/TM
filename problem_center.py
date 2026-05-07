"""problem_center.py — Centro de problemas auto-categorizados.

Mejoras inspiradas en Sentry:
- Fingerprint (hash category+title) para agrupar duplicados
- Contador de ocurrencias (count), first_seen, last_seen
- Timeline últimas 24h, búsqueda, orden por frecuencia
- Auto-resolve de problemas antiguos
"""
import uuid
import hashlib
from datetime import datetime, timedelta
import config
import json_atomic


CATEGORIES = [
    "Bot de compra", "Filas", "Cookies", "CAPTCHA", "Fingerprint",
    "Navegadores", "Anti-Detect", "Extensiones", "Bypass", "Anti-Bot",
    "Proxies", "Eventos", "Cuentas/Tarjetas", "Sistema", "Dominios",
]

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _path():
    return config.ruta("problems.json")


def _fingerprint(category, title):
    """Hash estable para agrupar duplicados (category+title normalizados)."""
    key = f"{(category or '').lower().strip()}|{(title or '').lower().strip()}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def _now():
    return datetime.now().isoformat()


def _migrate(p):
    """Retrocompatibilidad: añade campos nuevos a problemas antiguos."""
    if "count" not in p:
        p["count"] = 1
    if "first_seen" not in p:
        p["first_seen"] = p.get("created_at", _now())
    if "last_seen" not in p:
        p["last_seen"] = p.get("created_at", _now())
    if "fingerprint" not in p:
        p["fingerprint"] = _fingerprint(p.get("category", ""), p.get("title", ""))
    return p


def _load():
    d = json_atomic.load(_path(), default={"problems": []})
    d["problems"] = [_migrate(p) for p in d.get("problems", [])]
    return d


def get_problems(include_resolved=False):
    d = _load()
    items = d["problems"]
    if not include_resolved:
        items = [p for p in items if not p.get("resolved")]
    return items


def report_problem(category, title, message, severity="medium", meta=None):
    """Crea o incrementa problema existente (grouping por fingerprint)."""
    d = _load()
    fp = _fingerprint(category, title)
    sev = severity if severity in SEVERITY_ORDER else "medium"
    cat = category if category in CATEGORIES else "Sistema"
    now = _now()

    # Buscar existente abierto con mismo fingerprint → incrementar
    for p in d["problems"]:
        if p.get("fingerprint") == fp and not p.get("resolved"):
            p["count"] = p.get("count", 1) + 1
            p["last_seen"] = now
            p["message"] = message or p.get("message", "")
            if SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(p.get("severity", "low"), 0):
                p["severity"] = sev
            if meta:
                p.setdefault("meta", {}).update(meta)
            json_atomic.save(_path(), d)
            return p

    # Nuevo
    problem = {
        "id": uuid.uuid4().hex[:8],
        "fingerprint": fp,
        "category": cat,
        "title": title,
        "message": message,
        "severity": sev,
        "meta": meta or {},
        "created_at": now,
        "first_seen": now,
        "last_seen": now,
        "count": 1,
        "resolved": False,
    }
    d["problems"].append(problem)
    json_atomic.save(_path(), d)
    return problem


def resolve_problem(pid, resolution=""):
    d = _load()
    for p in d["problems"]:
        if p["id"] == pid:
            p["resolved"] = True
            p["resolution"] = resolution
            p["resolved_at"] = _now()
            json_atomic.save(_path(), d)
            return p
    return None


def resolve_many(ids, resolution=""):
    """Resuelve múltiples problemas en batch."""
    d = _load()
    count = 0
    ids_set = set(ids)
    for p in d["problems"]:
        if p["id"] in ids_set and not p.get("resolved"):
            p["resolved"] = True
            p["resolution"] = resolution
            p["resolved_at"] = _now()
            count += 1
    json_atomic.save(_path(), d)
    return count


def clear_resolved():
    d = _load()
    d["problems"] = [p for p in d["problems"] if not p.get("resolved")]
    json_atomic.save(_path(), d)


def auto_resolve_all():
    d = _load()
    count = 0
    for p in d["problems"]:
        if not p.get("resolved") and p.get("severity") == "low":
            p["resolved"] = True
            p["resolution"] = "auto-resolved"
            p["resolved_at"] = _now()
            count += 1
    json_atomic.save(_path(), d)
    return count


def auto_resolve_stale(hours=48):
    """Auto-resuelve problemas low/medium sin actividad > N horas."""
    d = _load()
    cutoff = datetime.now() - timedelta(hours=hours)
    count = 0
    for p in d["problems"]:
        if p.get("resolved"):
            continue
        if p.get("severity") in ("low", "medium"):
            try:
                last = datetime.fromisoformat(p.get("last_seen", p.get("created_at", "")))
            except Exception:
                continue
            if last < cutoff:
                p["resolved"] = True
                p["resolution"] = f"auto-resolved (inactivo >{hours}h)"
                p["resolved_at"] = _now()
                count += 1
    json_atomic.save(_path(), d)
    return count


def search_problems(query="", severity=None, category=None, include_resolved=False,
                    sort="recent"):
    """Filtra y ordena problemas."""
    items = get_problems(include_resolved=include_resolved)
    q = (query or "").lower().strip()
    if q:
        items = [p for p in items if q in (p.get("title", "") + " " + p.get("message", "")).lower()]
    if severity:
        items = [p for p in items if p.get("severity") == severity]
    if category:
        items = [p for p in items if p.get("category") == category]

    if sort == "frequency":
        items.sort(key=lambda p: p.get("count", 1), reverse=True)
    elif sort == "severity":
        items.sort(key=lambda p: SEVERITY_ORDER.get(p.get("severity", "low"), 0), reverse=True)
    elif sort == "oldest":
        items.sort(key=lambda p: p.get("first_seen", ""))
    else:  # recent (default)
        items.sort(key=lambda p: p.get("last_seen", p.get("created_at", "")), reverse=True)
    return items


def get_timeline(hours=24):
    """Conteo de problemas por hora durante últimas N horas."""
    d = _load()
    now = datetime.now()
    buckets = [0] * hours
    labels = []
    for i in range(hours):
        t = now - timedelta(hours=hours - 1 - i)
        labels.append(t.strftime("%H:00"))

    for p in d["problems"]:
        try:
            ls = datetime.fromisoformat(p.get("last_seen", p.get("created_at", "")))
        except Exception:
            continue
        delta_h = (now - ls).total_seconds() / 3600
        if 0 <= delta_h < hours:
            idx = hours - 1 - int(delta_h)
            if 0 <= idx < hours:
                buckets[idx] += p.get("count", 1)
    return {"labels": labels, "values": buckets}


def get_category_breakdown():
    """Conteo de problemas abiertos agrupados por categoría."""
    items = get_problems(include_resolved=False)
    by_cat = {}
    for p in items:
        c = p.get("category", "Sistema")
        by_cat[c] = by_cat.get(c, 0) + p.get("count", 1)
    return [{"category": k, "count": v} for k, v in sorted(by_cat.items(), key=lambda x: -x[1])]


def get_stats():
    d = _load()
    items = d["problems"]
    open_items = [p for p in items if not p.get("resolved")]
    return {
        "total":     len(items),
        "open":      len(open_items),
        "resolved":  sum(1 for p in items if p.get("resolved")),
        "critical":  sum(1 for p in open_items if p.get("severity") == "critical"),
        "high":      sum(1 for p in open_items if p.get("severity") == "high"),
        "medium":    sum(1 for p in open_items if p.get("severity") == "medium"),
        "low":       sum(1 for p in open_items if p.get("severity") == "low"),
        "occurrences": sum(p.get("count", 1) for p in open_items),
    }


def get_categories():
    return CATEGORIES
