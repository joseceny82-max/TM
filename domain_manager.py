"""
domain_manager.py — Gestion de dominios de Ticketmaster.

Permite seleccionar uno, varios o todos los dominios TM.
El bot usara estos dominios como targets para compra de entradas.
"""
import os
import json
import config


def _load():
    path = config.VIVO_DOMINIOS
    if not os.path.exists(path):
        return {"selected": [config.TM_DEFAULT_DOMAIN], "multi_mode": False}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"selected": [config.TM_DEFAULT_DOMAIN], "multi_mode": False}


def _save(data):
    os.makedirs(os.path.dirname(config.VIVO_DOMINIOS), exist_ok=True)
    tmp = config.VIVO_DOMINIOS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config.VIVO_DOMINIOS)


def list_all():
    """Lista todos los dominios disponibles."""
    out = []
    for key, info in config.TM_DOMAINS.items():
        out.append({
            "id": key,
            "domain": info["domain"],
            "url": f"https://{info['domain']}",
            "country": info["country"],
            "country_en": info["country_en"],
            "flag": info["flag"],
            "currency": info["currency"],
            "lang": info["lang"],
            "region": info["region"],
        })
    return out


def get_selected():
    """Devuelve la seleccion actual (lista de ids + multi_mode)."""
    data = _load()
    # Validacion: eliminar ids que no existan
    data["selected"] = [x for x in data.get("selected", []) if x in config.TM_DOMAINS]
    if not data["selected"]:
        data["selected"] = [config.TM_DEFAULT_DOMAIN]
    return data


def set_selected(ids, multi_mode=False):
    """Guarda seleccion. `ids` es una lista de claves de TM_DOMAINS."""
    valid = [x for x in ids if x in config.TM_DOMAINS]
    if not valid:
        valid = [config.TM_DEFAULT_DOMAIN]
    data = {"selected": valid, "multi_mode": bool(multi_mode)}
    _save(data)
    return data


def get_active_urls():
    """Devuelve URLs activas segun seleccion (para que el bot sepa a que dominio atacar)."""
    sel = get_selected()
    urls = []
    for key in sel["selected"]:
        info = config.TM_DOMAINS.get(key)
        if info:
            urls.append({
                "id": key,
                "url": f"https://{info['domain']}",
                "country": info["country"],
                "currency": info["currency"],
                "lang": info["lang"],
            })
    return urls


def by_region():
    """Agrupa dominios por region."""
    groups = {}
    for key, info in config.TM_DOMAINS.items():
        r = info["region"]
        groups.setdefault(r, []).append({
            "id": key,
            "domain": info["domain"],
            "country": info["country"],
            "flag": info["flag"],
        })
    return groups
