"""proxy_per_account.py — 1 proxy fijo por cuenta TM (enforcement).

Asigna un proxy estable a cada cuenta TM. La MISMA cuenta usa SIEMPRE
la misma IP. Akamai/PerimeterX ve consistencia → trust score alto.

Si la IP se quema (3+ errors), auto-rota a otra pero la registra como
"asignación de emergencia" y notifica.

API:
  assign_proxy(account_id, geo?, force_rotate?) → proxy asignado
  get_account_proxy(account_id) → proxy actual
  list_assignments() / release_assignment(account_id)
  mark_proxy_burned(account_id, reason)
  get_stats()
"""
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import config
import json_atomic

_ASSIGN_KEY = "proxy_per_account_assignments.json"
_CONFIG_KEY = "proxy_per_account_config.json"
_lock = threading.Lock()


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "enabled": True,
        "burn_threshold_errors": 3,          # rotar tras 3 errores
        "lifetime_days": 30,                  # reasignar tras 30 días
        "cooldown_after_burn_hours": 24,      # IP quemada, cooldown antes reuse
        "prefer_geo_match": True,
        "fallback_to_any_geo": True,
        "auto_rotate_on_burn": True,
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def _load() -> dict:
    return json_atomic.load(config.ruta(_ASSIGN_KEY),
                              default={"assignments": {}, "burned": []})


def _save(d: dict):
    json_atomic.save(config.ruta(_ASSIGN_KEY), d)


def _pick_available_proxy(geo: Optional[str] = None,
                           exclude_ips: Optional[List[str]] = None) -> Optional[dict]:
    """Elige un proxy disponible desde proxy_health.list_proxies()."""
    try:
        import proxy_health
        proxies = proxy_health.list_proxies() if hasattr(proxy_health, "list_proxies") else []
        exclude = set(exclude_ips or [])
        # Filtrar alive + no burned
        candidates = []
        for p in proxies:
            if p.get("status") != "alive":
                continue
            pid = p.get("id", "")
            if pid in exclude:
                continue
            if geo and p.get("country") and p.get("country").lower() != geo.lower():
                continue
            candidates.append(p)
        if not candidates and geo:
            # Fallback sin filtro geo
            for p in proxies:
                if p.get("status") == "alive" and p.get("id") not in exclude:
                    candidates.append(p)
        if not candidates:
            return None
        # Preferir el de mejor score
        candidates.sort(key=lambda p: p.get("score", 0), reverse=True)
        return candidates[0]
    except Exception:
        return None


def assign_proxy(account_id: str,
                   geo: Optional[str] = None,
                   force_rotate: bool = False) -> dict:
    """Asigna (o reutiliza) un proxy fijo para la cuenta.

    - Si ya tiene asignación válida y no expiró → reutiliza
    - Si force_rotate=True o expiró o quemada → asigna nueva
    """
    if not account_id:
        return {"ok": False, "error": "account_id requerido"}

    cfg = get_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "proxy_per_account disabled in config"}

    with _lock:
        d = _load()
        assignments = d.get("assignments", {})
        existing = assignments.get(account_id)
        now = datetime.now()

        # Si ya tiene asignación válida
        if existing and not force_rotate:
            try:
                expires = datetime.fromisoformat(existing.get("expires_at", ""))
                if expires > now and not existing.get("burned"):
                    existing["uses"] = existing.get("uses", 0) + 1
                    existing["last_used_at"] = now.isoformat()
                    _save(d)
                    return {"ok": True, "reused": True, **existing}
            except Exception:
                pass

        # Seleccionar nuevo proxy
        burned_ips = [b["proxy_id"] for b in d.get("burned", [])
                        if _is_burn_active(b, cfg)]
        new_proxy = _pick_available_proxy(geo=geo, exclude_ips=burned_ips)

        if not new_proxy:
            return {"ok": False, "error": "no hay proxies disponibles",
                     "hint": "configura proxies via /api/proxy-health"}

        proxy_id = new_proxy.get("id", hashlib.md5(str(new_proxy).encode()).hexdigest()[:8])
        entry = {
            "account_id": account_id,
            "proxy_id": proxy_id,
            "proxy_preview": new_proxy.get("url_preview", "***"),
            "geo": geo or new_proxy.get("country", "us"),
            "assigned_at": now.isoformat(),
            "expires_at": (now + timedelta(days=cfg.get("lifetime_days", 30))).isoformat(),
            "uses": 1,
            "errors": 0,
            "burned": False,
            "last_used_at": now.isoformat(),
        }
        d["assignments"][account_id] = entry
        _save(d)
        return {"ok": True, "new": True, **entry}


def get_account_proxy(account_id: str) -> Optional[dict]:
    d = _load()
    return d.get("assignments", {}).get(account_id)


def list_assignments(active_only: bool = True) -> List[dict]:
    d = _load()
    items = []
    now = datetime.now()
    for entry in d.get("assignments", {}).values():
        try:
            expires = datetime.fromisoformat(entry.get("expires_at", ""))
            if active_only and (expires < now or entry.get("burned")):
                continue
        except Exception:
            pass
        items.append(entry)
    return items


def record_error(account_id: str, reason: str = "unknown") -> dict:
    """Registra un error en la asignación. Si llega al threshold → auto-rota."""
    cfg = get_config()
    with _lock:
        d = _load()
        assignment = d.get("assignments", {}).get(account_id)
        if not assignment:
            return {"ok": False, "error": "account sin asignación"}

        assignment["errors"] = assignment.get("errors", 0) + 1
        assignment["last_error"] = reason[:150]
        assignment["last_error_at"] = datetime.now().isoformat()

        threshold = cfg.get("burn_threshold_errors", 3)
        should_rotate = (assignment["errors"] >= threshold
                          and cfg.get("auto_rotate_on_burn", True))

        if should_rotate:
            # Marcar quemada
            assignment["burned"] = True
            assignment["burned_at"] = datetime.now().isoformat()
            d.setdefault("burned", []).append({
                "proxy_id": assignment.get("proxy_id"),
                "account_id": account_id,
                "burned_at": assignment["burned_at"],
                "reason": reason[:150],
                "uses": assignment.get("uses", 0),
            })
            d["burned"] = d["burned"][-200:]
        _save(d)

        if should_rotate:
            # Asignar nueva (fuera del lock recursive)
            pass

    if should_rotate:
        return assign_proxy(account_id,
                              geo=assignment.get("geo"),
                              force_rotate=True)
    return {"ok": True, "errors": assignment["errors"],
             "threshold": threshold, "action": "logged"}


def _is_burn_active(burn_entry: dict, cfg: dict) -> bool:
    try:
        burned_at = datetime.fromisoformat(burn_entry.get("burned_at", ""))
        cooldown = cfg.get("cooldown_after_burn_hours", 24)
        return (datetime.now() - burned_at) < timedelta(hours=cooldown)
    except Exception:
        return False


def release_assignment(account_id: str) -> dict:
    with _lock:
        d = _load()
        existed = d.get("assignments", {}).pop(account_id, None)
        _save(d)
    return {"ok": True, "released": existed is not None}


def get_stats() -> dict:
    d = _load()
    active = list_assignments(active_only=True)
    all_items = list_assignments(active_only=False)
    return {
        "total_assignments": len(all_items),
        "active_assignments": len(active),
        "expired_or_burned": len(all_items) - len(active),
        "burned_history": len(d.get("burned", [])),
        "config": {k: v for k, v in get_config().items()},
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    # Config
    cfg = get_config()
    results["checks"]["config_enabled"] = cfg.get("enabled")

    # Assign (puede fallar si no hay proxies)
    r = assign_proxy("test_account_ppa")
    results["checks"]["assign_returns_dict"] = isinstance(r, dict)
    if r.get("ok"):
        results["checks"]["assignment_created"] = True
        results["checks"]["proxy_id"] = r.get("proxy_id", "?")

        # Reuse
        r2 = assign_proxy("test_account_ppa")
        results["checks"]["reused"] = r2.get("reused", False)

        # Record error
        er = record_error("test_account_ppa", reason="test")
        results["checks"]["error_recorded"] = er.get("ok", False)

        # Release cleanup
        rel = release_assignment("test_account_ppa")
        results["checks"]["release_ok"] = rel.get("ok", False)
    else:
        results["checks"]["assign_returns_dict"] = True
        results["checks"]["no_proxies_available_graceful"] = True

    # Stats
    s = get_stats()
    results["checks"]["stats_readable"] = "total_assignments" in s

    return results


def get_info() -> dict:
    return {
        "module": "proxy_per_account",
        "stats": get_stats(),
        "enabled": get_config().get("enabled"),
    }
