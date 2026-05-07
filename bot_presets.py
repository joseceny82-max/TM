"""bot_presets.py — Selecciones guardadas de eventos + config para el bot.

Un preset = conjunto de eventos marcados + configuración del panel Lanzar Bot
(browser, captcha provider, account, category, reintentos, timeout, etc.).

Permite al usuario preparar una selección compleja una vez, guardarla, y
relanzarla cuando sea necesario (p.ej. el día del drop) con 1 click.

API pública:
    list_presets()              → List[preset]
    get_preset(preset_id)       → preset dict o None
    save_preset(name, events, config) → preset creado
    delete_preset(preset_id)    → bool
    self_test()                 → {ok, checks}
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any

import config
import json_atomic

_CONFIG_KEY = "bot_presets.json"
_lock = threading.RLock()


def _default() -> Dict[str, Any]:
    return {"version": 1, "presets": []}


def _load() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default=_default())


def _persist(data: Dict[str, Any]):
    json_atomic.save(config.ruta(_CONFIG_KEY), data)


def list_presets() -> List[Dict[str, Any]]:
    """Devuelve todos los presets, más recientes primero."""
    with _lock:
        d = _load()
        presets = d.get("presets", [])
        return sorted(presets, key=lambda p: p.get("created_at", ""), reverse=True)


def get_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    if not preset_id:
        return None
    with _lock:
        for p in _load().get("presets", []):
            if p.get("id") == preset_id:
                return p
    return None


def save_preset(name: str, events: List[Dict[str, Any]],
                config_data: Dict[str, Any]) -> Dict[str, Any]:
    """Crea un preset nuevo. Los eventos se guardan snapshot (campos mínimos necesarios
    para que el bot pueda reconstruirlos si se borran de /api/events).
    """
    with _lock:
        d = _load()
        # Limpia events a solo campos necesarios
        clean_events = []
        for ev in (events or []):
            if not isinstance(ev, dict): continue
            ce = {}
            for k in ("id", "tm_id", "name", "country", "tm_url", "quantity",
                      "venue", "city", "date", "sale_date", "image_url",
                      "classifications", "max_price", "currency"):
                if k in ev and ev[k] not in (None, ""):
                    ce[k] = ev[k]
            if ce.get("id") or ce.get("tm_id"):
                clean_events.append(ce)
        # Limpia config
        clean_cfg = {}
        allowed_cfg = {"category", "account_id", "browser", "captcha_provider",
                       "retries", "retry_delay", "queue_timeout", "cooldown",
                       "strategy", "worker_count"}
        for k, v in (config_data or {}).items():
            if k in allowed_cfg:
                clean_cfg[k] = v
        preset = {
            "id": "ps_" + uuid.uuid4().hex[:10],
            "name": (name or "Preset sin nombre").strip()[:120],
            "events": clean_events,
            "config": clean_cfg,
            "created_at": datetime.now().isoformat(),
            "events_count": len(clean_events),
        }
        d.setdefault("presets", []).append(preset)
        # Mantén máximo 100 presets
        if len(d["presets"]) > 100:
            d["presets"] = sorted(d["presets"], key=lambda p: p.get("created_at", ""))[-100:]
        _persist(d)
        return preset


def delete_preset(preset_id: str) -> bool:
    with _lock:
        d = _load()
        before = len(d.get("presets", []))
        d["presets"] = [p for p in d.get("presets", []) if p.get("id") != preset_id]
        after = len(d["presets"])
        if after < before:
            _persist(d)
            return True
        return False


def get_stats() -> Dict[str, Any]:
    presets = list_presets()
    total_events = sum(len(p.get("events", [])) for p in presets)
    return {
        "total_presets": len(presets),
        "total_events_saved": total_events,
        "last_created": presets[0]["created_at"] if presets else "",
    }


# ═══════════════════════════════════════════════════════════════
#  Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    results = {"checks": {}}
    # Guardar un preset test
    test_events = [
        {"id": "test_ev_1", "name": "Test Evento 1", "country": "es",
         "tm_url": "https://ticketmaster.es/event/test/111"},
        {"id": "test_ev_2", "name": "Test Evento 2", "country": "uk",
         "tm_url": "https://ticketmaster.co.uk/event/test/222"},
    ]
    test_cfg = {"category": "floor", "browser": "auto", "retries": 3}
    p = save_preset("__self_test__", test_events, test_cfg)
    results["checks"]["save_preset_ok"] = bool(p.get("id") and p.get("id").startswith("ps_"))
    results["checks"]["save_events_count"] = (p.get("events_count") == 2)
    results["checks"]["save_config_preserved"] = (p.get("config", {}).get("browser") == "auto")

    # Get by id
    got = get_preset(p["id"])
    results["checks"]["get_preset"] = (got is not None and got["id"] == p["id"])
    # List
    all_p = list_presets()
    results["checks"]["list_contains_test"] = any(pp.get("id") == p["id"] for pp in all_p)
    # Delete
    removed = delete_preset(p["id"])
    results["checks"]["delete_preset"] = removed
    after = get_preset(p["id"])
    results["checks"]["delete_confirmed"] = (after is None)
    # Unknown
    nothing = get_preset("nonexistent_preset_id")
    results["checks"]["get_unknown_returns_none"] = (nothing is None)
    # Clean config filtering
    p2 = save_preset("__test2__", [{"id": "e1", "name": "x"}],
                    {"browser": "camoufox", "evil_field": "should_be_dropped"})
    results["checks"]["config_filters_unknown_keys"] = ("evil_field" not in (p2.get("config") or {}))
    delete_preset(p2["id"])

    results["total_checks"] = len(results["checks"])
    results["passed_checks"] = sum(1 for v in results["checks"].values() if v)
    results["ok"] = results["passed_checks"] == results["total_checks"]
    return results


def get_info() -> Dict[str, Any]:
    return {
        "module": "bot_presets",
        "version": 1,
        "stats": get_stats(),
    }
