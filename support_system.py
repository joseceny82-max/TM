"""support_system.py — Sistema de tickets de soporte."""
import uuid
from datetime import datetime
import config
import json_atomic


CATEGORIES = [
    "Bot no encuentra evento", "Cola Queue-it tarda",
    "No puede login Ticketmaster", "Error en checkout",
    "Entradas agotadas", "Navegador se cierra",
    "Error de red/proxy", "CAPTCHA no resuelto",
    "Error de pago", "Dominio no selecciona",
    "OTP no llega", "Otro",
]


def _path():
    return config.ruta("tickets.json")


def create_ticket(title, description, category="Otro", priority="medium"):
    d = json_atomic.load(_path(), default={"tickets": []})
    t = {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "description": description,
        "category": category,
        "priority": priority,
        "status": "open",
        "created_at": datetime.now().isoformat(),
        "responses": [],
    }
    d["tickets"].append(t)
    json_atomic.save(_path(), d)
    return t


def get_all_tickets():
    return json_atomic.load(_path(), default={"tickets": []}).get("tickets", [])


def get_pending_tickets():
    return [t for t in get_all_tickets() if t.get("status") != "resolved"]


def resolve_ticket(tid, resolution=""):
    d = json_atomic.load(_path(), default={"tickets": []})
    for t in d["tickets"]:
        if t["id"] == tid:
            t["status"] = "resolved"
            t["resolution"] = resolution
            t["resolved_at"] = datetime.now().isoformat()
            json_atomic.save(_path(), d)
            return t
    return None


def get_stats():
    all_t = get_all_tickets()
    return {
        "total":    len(all_t),
        "open":     sum(1 for t in all_t if t.get("status") != "resolved"),
        "resolved": sum(1 for t in all_t if t.get("status") == "resolved"),
    }


def get_categories():
    return CATEGORIES
