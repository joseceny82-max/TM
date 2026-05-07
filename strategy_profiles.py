"""strategy_profiles.py — Persistencia y validación de perfiles de Estrategia.

Almacena en `datos/strategy_profiles.json` los presets de configuración + los
parámetros precisos de compra (cantidad, filtros precio/sección, stop conditions, etc.)

API pública:
    save_preset(name, config) -> dict
    list_presets() -> list[dict]
    get_preset(name) -> dict | None
    delete_preset(name) -> bool
    save_active_config(config) -> dict
    get_active_config() -> dict
    validate_config(config) -> tuple[bool, list[str]]
    get_strategies_catalog() -> list[dict]
    self_test() -> dict
"""
from __future__ import annotations
import os
import json
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

_PRESETS_KEY = "strategy_profiles.json"
_ACTIVE_KEY = "strategy_active.json"
_LOG_KEY = "strategy_log.json"

_lock = threading.RLock()

# ═══════════════════════════════════════════════════════════════
# Catálogo extendido
# ═══════════════════════════════════════════════════════════════
STRATEGIES_CATALOG = [
    {"id": "best_available",  "label": "⭐ Best Available",          "difficulty": "easy",   "success": "high",      "category": "general"},
    {"id": "cheapest",        "label": "💰 Cheapest First",          "difficulty": "easy",   "success": "medium",    "category": "general"},
    {"id": "fastest",         "label": "⚡ Fastest Checkout",         "difficulty": "easy",   "success": "very_high", "category": "speed"},
    {"id": "sniper",          "label": "🎯 Sniper",                   "difficulty": "medium", "success": "medium",    "category": "precision"},
    {"id": "sniper_smart",    "label": "🧠 Sniper Smart",             "difficulty": "medium", "success": "very_high", "category": "precision"},
    {"id": "distributed",     "label": "🔀 Distributed Multi-domain", "difficulty": "hard",   "success": "max",       "category": "scale"},
    {"id": "vip_first",       "label": "👑 VIP First",                "difficulty": "easy",   "success": "medium",    "category": "premium"},
    {"id": "floor_only",      "label": "🎶 Floor / GA Only",          "difficulty": "easy",   "success": "medium",    "category": "premium"},
    {"id": "ranked_sections", "label": "🏆 Ranked Sections",          "difficulty": "medium", "success": "high",      "category": "precision"},
    {"id": "presale_sniper",  "label": "🎫 Presale Sniper",           "difficulty": "hard",   "success": "max",       "category": "speed"},
    {"id": "resale_hunter",   "label": "🔄 Resale Hunter",            "difficulty": "medium", "success": "medium",    "category": "patience"},
    {"id": "aisle_seeker",    "label": "🚪 Aisle Seeker",             "difficulty": "medium", "success": "low_medium","category": "preference"},
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "event_id": "",
    "quantity": 2,
    "adjacent": "must",
    "workers": 3,
    "primary": "best_available",
    "fallback": "best_available",
    "fallback2": "none",
    "price_min": 0,
    "price_max": None,
    "budget_total": None,
    "fees_included": True,
    "resale_allow": False,
    "sec_allow": [],
    "sec_block": [],
    "row_min": None,
    "row_max": None,
    "zone_type": "any",
    "max_attempts": 20,
    "timeout": 600,
    "cooldown": 3,
    "stop_on_success": True,
    "cancel_no_match": False,
    "notif_success": True,
    "notif_fail": False,
    "notif_cart": False,
    "notif_captcha": False,
    "spread_accounts": True,
    "rotate_cards": True,
    "isp_sticky": True,
    "mode_live": False,
}


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════
def _path(key: str) -> str:
    p = config.ruta(key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _load(key: str, default: Any) -> Any:
    return json_atomic.load(_path(key), default=default)


def _save(key: str, data: Any) -> None:
    json_atomic.save(_path(key), data)


# ═══════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════
def validate_config(cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Valida un config. Devuelve (ok, errors)."""
    errors: List[str] = []
    if not isinstance(cfg, dict):
        return False, ["config must be a dict"]

    qty = cfg.get("quantity", 2)
    if not (isinstance(qty, int) and 1 <= qty <= 8):
        errors.append("quantity must be int 1-8 (TM max)")

    workers = cfg.get("workers", 3)
    if not (isinstance(workers, int) and 1 <= workers <= 50):
        errors.append("workers must be int 1-50")

    primary = cfg.get("primary", "")
    valid = {s["id"] for s in STRATEGIES_CATALOG}
    if primary not in valid:
        errors.append(f"primary must be one of {sorted(valid)}")

    fallback = cfg.get("fallback", "best_available")
    if fallback not in valid and fallback != "none":
        errors.append("fallback invalid")

    adjacent = cfg.get("adjacent", "must")
    if adjacent not in ("must", "prefer", "any"):
        errors.append("adjacent must be must|prefer|any")

    zone = cfg.get("zone_type", "any")
    if zone not in ("any", "floor", "lower", "upper", "vip", "balcony"):
        errors.append("zone_type invalid")

    pmin = cfg.get("price_min")
    pmax = cfg.get("price_max")
    if pmin is not None and pmax is not None and isinstance(pmin, (int, float)) and isinstance(pmax, (int, float)):
        if pmax > 0 and pmin > pmax:
            errors.append("price_min > price_max")

    rmin = cfg.get("row_min")
    rmax = cfg.get("row_max")
    if rmin and rmax and rmin > rmax:
        errors.append("row_min > row_max")

    timeout = cfg.get("timeout", 600)
    if not (isinstance(timeout, int) and 30 <= timeout <= 7200):
        errors.append("timeout must be 30-7200 s")

    return (len(errors) == 0), errors


def merge_with_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Mergea cfg sobre DEFAULT_CONFIG."""
    out = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG or k in ("event_id",)})
    return out


# ═══════════════════════════════════════════════════════════════
# Presets CRUD
# ═══════════════════════════════════════════════════════════════
def list_presets() -> List[Dict[str, Any]]:
    with _lock:
        d = _load(_PRESETS_KEY, default={"presets": []})
        return d.get("presets", [])


def get_preset(name: str) -> Optional[Dict[str, Any]]:
    for p in list_presets():
        if p.get("name") == name:
            return p
    return None


def save_preset(name: str, config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Guarda (o sobreescribe) un preset por nombre."""
    if not name or not name.strip():
        return {"ok": False, "error": "name_required"}
    name = name.strip()[:80]
    cfg = merge_with_defaults(config_dict or {})
    ok, errs = validate_config(cfg)
    if not ok:
        return {"ok": False, "error": "validation", "errors": errs}
    with _lock:
        d = _load(_PRESETS_KEY, default={"presets": []})
        items = [p for p in d.get("presets", []) if p.get("name") != name]
        items.append({
            "name": name,
            "config": cfg,
            "created_at": datetime.now().isoformat(),
        })
        d["presets"] = items
        _save(_PRESETS_KEY, d)
    return {"ok": True, "name": name}


def delete_preset(name: str) -> Dict[str, Any]:
    with _lock:
        d = _load(_PRESETS_KEY, default={"presets": []})
        before = len(d.get("presets", []))
        d["presets"] = [p for p in d.get("presets", []) if p.get("name") != name]
        if len(d["presets"]) == before:
            return {"ok": False, "error": "not_found"}
        _save(_PRESETS_KEY, d)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# Active config
# ═══════════════════════════════════════════════════════════════
def get_active_config() -> Dict[str, Any]:
    d = _load(_ACTIVE_KEY, default={})
    if not d:
        return dict(DEFAULT_CONFIG)
    return merge_with_defaults(d)


def save_active_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg = merge_with_defaults(cfg or {})
    ok, errs = validate_config(cfg)
    if not ok:
        return {"ok": False, "error": "validation", "errors": errs}
    cfg["_saved_at"] = datetime.now().isoformat()
    with _lock:
        _save(_ACTIVE_KEY, cfg)
    return {"ok": True, "config": cfg}


# ═══════════════════════════════════════════════════════════════
# Log circular
# ═══════════════════════════════════════════════════════════════
def append_log(line: str, max_lines: int = 500) -> None:
    with _lock:
        d = _load(_LOG_KEY, default={"log": []})
        log = d.get("log", [])
        ts = datetime.now().strftime("%H:%M:%S")
        log.append(f"[{ts}] {line}")
        d["log"] = log[-max_lines:]
        _save(_LOG_KEY, d)


def get_log() -> List[str]:
    return _load(_LOG_KEY, default={"log": []}).get("log", [])


def clear_log() -> None:
    with _lock:
        _save(_LOG_KEY, {"log": []})


# ═══════════════════════════════════════════════════════════════
# Catalog public
# ═══════════════════════════════════════════════════════════════
def get_strategies_catalog() -> List[Dict[str, Any]]:
    return list(STRATEGIES_CATALOG)


# ═══════════════════════════════════════════════════════════════
# Dry-run simulator
# ═══════════════════════════════════════════════════════════════
def dry_run(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Simula la decisión del bot sin ejecutar nada. Devuelve los pasos."""
    cfg = merge_with_defaults(cfg or {})
    ok, errs = validate_config(cfg)
    decisions = []
    if not ok:
        return {"ok": False, "errors": errs, "decisions": [{"step": "validate", "msg": str(errs)}]}

    decisions.append({"step": "validate", "msg": "Config válido"})
    decisions.append({"step": "event", "msg": f"Evento: {cfg.get('event_id') or '(sin seleccionar)'}"})
    decisions.append({"step": "quantity", "msg": f"Comprar {cfg['quantity']} ticket(s) — adjacent={cfg['adjacent']}"})
    decisions.append({"step": "primary", "msg": f"Estrategia principal: {cfg['primary']}"})
    if cfg.get("fallback") and cfg["fallback"] != "none":
        decisions.append({"step": "fallback", "msg": f"Fallback: {cfg['fallback']}"})
    if cfg.get("fallback2") and cfg["fallback2"] != "none":
        decisions.append({"step": "fallback2", "msg": f"Fallback final: {cfg['fallback2']}"})

    # Filtros precio
    pmin = cfg.get("price_min") or 0
    pmax = cfg.get("price_max")
    budget = cfg.get("budget_total")
    if pmax:
        decisions.append({"step": "price_filter", "msg": f"Filtro precio: {pmin}-{pmax} {'(con tasas)' if cfg.get('fees_included') else '(sin tasas)'}"})
    if budget:
        decisions.append({"step": "budget", "msg": f"Presupuesto total max: {budget}"})

    # Filtros sección
    if cfg.get("sec_allow"):
        decisions.append({"step": "sec_allow", "msg": f"Sólo secciones: {cfg['sec_allow']}"})
    if cfg.get("sec_block"):
        decisions.append({"step": "sec_block", "msg": f"Evitar secciones: {cfg['sec_block']}"})
    if cfg.get("row_min") or cfg.get("row_max"):
        decisions.append({"step": "row_filter", "msg": f"Filas {cfg.get('row_min','*')}-{cfg.get('row_max','*')}"})
    if cfg.get("zone_type") and cfg["zone_type"] != "any":
        decisions.append({"step": "zone", "msg": f"Tipo zona: {cfg['zone_type']}"})

    # Workers
    decisions.append({"step": "workers", "msg": f"Lanzar {cfg['workers']} workers en paralelo"})
    if cfg.get("spread_accounts"):
        decisions.append({"step": "anti_fraud", "msg": "Distribuir entre cuentas (anti-fraude bancario)"})
    if cfg.get("rotate_cards"):
        decisions.append({"step": "card_rotation", "msg": "Rotar tarjetas si declinan"})
    if cfg.get("isp_sticky"):
        decisions.append({"step": "proxy_pin", "msg": "ISP sticky proxy (1 IP por sesión)"})

    # Stop
    decisions.append({"step": "stop_cond", "msg": f"Stop: max {cfg['max_attempts']} intentos · timeout {cfg['timeout']}s · cooldown {cfg['cooldown']}s"})
    if cfg.get("stop_on_success"):
        decisions.append({"step": "stop_success", "msg": "Detener al primer éxito"})
    if cfg.get("cancel_no_match"):
        decisions.append({"step": "cancel_no_match", "msg": "Cancelar si no coincide preferencia"})

    # Modo
    mode = "🚨 LIVE (compra real)" if cfg.get("mode_live") else "🧪 SIMULATE"
    decisions.append({"step": "mode", "msg": mode})

    return {"ok": True, "decisions": decisions, "config": cfg}


# ═══════════════════════════════════════════════════════════════
# Suggested sections (heurística por evento)
# ═══════════════════════════════════════════════════════════════
def suggested_sections_for(event_id: str) -> List[str]:
    """Devuelve secciones sugeridas para un evento. Heurística simple basada en
    el id (en producción podría consultar la API real de TM o un cache)."""
    if not event_id:
        return []
    # Heurística: las primeras secciones suelen estar más cerca del escenario
    return ["FLOOR", "100", "101", "102", "200", "201"]


# ═══════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}

    # 1. Defaults
    cfg0 = dict(DEFAULT_CONFIG)
    ok, errs = validate_config(cfg0)
    checks["defaults_valid"] = ok and not errs

    # 2. Validation rejects bad config
    bad = dict(DEFAULT_CONFIG); bad["quantity"] = 99
    ok2, _ = validate_config(bad)
    checks["validation_rejects_bad_qty"] = not ok2

    # 3. Merge with defaults preserves overrides
    m = merge_with_defaults({"quantity": 4, "primary": "vip_first"})
    checks["merge_preserves"] = m["quantity"] == 4 and m["primary"] == "vip_first"
    checks["merge_default_keys"] = m.get("workers") == 3

    # 4. Save / get / delete preset
    pn = "__test_preset__"
    r1 = save_preset(pn, {"primary": "sniper_smart", "quantity": 2})
    checks["preset_save"] = bool(r1.get("ok"))
    p = get_preset(pn)
    checks["preset_get"] = p is not None and p.get("name") == pn
    r2 = delete_preset(pn)
    checks["preset_delete"] = bool(r2.get("ok"))
    checks["preset_after_delete"] = get_preset(pn) is None

    # 5. Active config save+load
    r3 = save_active_config({"primary": "best_available", "quantity": 3})
    checks["active_save"] = bool(r3.get("ok"))
    a = get_active_config()
    checks["active_get"] = a.get("primary") == "best_available" and a.get("quantity") == 3

    # 6. Catalog
    cat = get_strategies_catalog()
    checks["catalog_has_12"] = len(cat) == 12
    checks["catalog_unique_ids"] = len({c["id"] for c in cat}) == 12

    # 7. Dry run
    dr = dry_run({"event_id": "evt123", "primary": "sniper_smart"})
    checks["dry_run_ok"] = bool(dr.get("ok"))
    checks["dry_run_decisions"] = len(dr.get("decisions", [])) >= 5

    # 8. Suggested sections
    s = suggested_sections_for("X1234")
    checks["suggested_returns_list"] = isinstance(s, list) and len(s) > 0

    # 9. Log
    append_log("self_test entry")
    checks["log_works"] = "self_test entry" in (get_log()[-1] if get_log() else "")

    # 10. Validation: row_min > row_max
    bad2 = dict(DEFAULT_CONFIG); bad2["row_min"] = 20; bad2["row_max"] = 5
    ok3, _ = validate_config(bad2)
    checks["validation_row_range"] = not ok3

    # 11. Validation: price_min > price_max
    bad3 = dict(DEFAULT_CONFIG); bad3["price_min"] = 100; bad3["price_max"] = 50
    ok4, _ = validate_config(bad3)
    checks["validation_price_range"] = not ok4

    # 12. Validation: invalid primary
    bad4 = dict(DEFAULT_CONFIG); bad4["primary"] = "made_up"
    ok5, _ = validate_config(bad4)
    checks["validation_primary"] = not ok5

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {
        "ok": passed == total,
        "total_checks": total,
        "passed_checks": passed,
        "checks": checks,
    }


def get_info() -> Dict[str, Any]:
    return {
        "module": "strategy_profiles",
        "version": 1,
        "presets_count": len(list_presets()),
        "active_config_set": bool(_load(_ACTIVE_KEY, default=None)),
        "catalog_size": len(STRATEGIES_CATALOG),
    }
