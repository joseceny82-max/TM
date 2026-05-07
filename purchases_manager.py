"""purchases_manager.py — Registro completo de entradas compradas.

Schema enriquecido basado en investigación:
- Event info: name, venue, city, country, date, sale_date
- Seat info: section, row, seat(s) (rango o lista), category (GA/Reserved/VIP/Pit/Floor/Box)
- Pricing: unit_price, quantity, total_price, fees, currency
- Transaction: order_id, confirmation_number, purchase_date, payment_method (last4)
- Delivery: delivery_method (mobile/eTicket/will-call/physical), barcode, qr_code
- Account: account_id, account_email
- Bot metadata: browser_engine, proxy_used, flow_id, checkout_time_ms, mode (simulate/live)
- Status: confirmed / pending / failed / refunded / transferred / used / cancelled
- User tags + notes
"""
import uuid
import csv
import io
import os
from datetime import datetime
from typing import Optional, List, Dict
import config
import json_atomic  # mantener para compat con otros módulos
import sqlite_store


# Estados válidos
STATUS_CHOICES = {"confirmed", "pending", "failed", "refunded", "transferred", "used", "cancelled"}

# Categorías estándar de asientos
SEAT_CATEGORIES = [
    "GA",           # General Admission / Pista de pie
    "Reserved",     # Reserved seating
    "VIP",          # VIP / Meet & Greet
    "Pit",          # Foso / Front pit
    "Floor",        # Pista
    "Box",          # Palco
    "Lower",        # Nivel bajo
    "Upper",        # Nivel alto
    "Suite",        # Suite privada
    "Standing",     # De pie
    "Accessible",   # Silla ruedas / accesible
]

# Métodos de entrega
DELIVERY_METHODS = [
    "mobile",       # TM app / mobile ticket
    "eTicket",      # PDF
    "will-call",    # Recoger en taquilla
    "physical",     # Entrada física por correo
    "wallet",       # Apple/Google Wallet
]


def _path():
    return config.ruta("purchases.json")


def _migrate(p: dict) -> dict:
    """Retrocompatibilidad: añade campos nuevos."""
    p.setdefault("id", uuid.uuid4().hex[:8])
    p.setdefault("ts", datetime.now().isoformat())
    p.setdefault("status", "confirmed")
    # event info
    p.setdefault("event_name", "")
    p.setdefault("venue", "")
    p.setdefault("city", "")
    p.setdefault("country", "")
    p.setdefault("event_date", "")
    # seat info
    p.setdefault("section", "")
    p.setdefault("row", "")
    p.setdefault("seats", [])       # lista de asientos: ["12","13","14"]
    p.setdefault("seat_category", "Reserved")
    # pricing
    p.setdefault("unit_price", 0)
    p.setdefault("quantity", 1)
    p.setdefault("fees", 0)
    p.setdefault("currency", "EUR")
    # transaction
    p.setdefault("order_id", "")
    p.setdefault("confirmation_number", "")
    p.setdefault("payment_method", "")    # "VISA •••• 4242"
    # delivery
    p.setdefault("delivery_method", "mobile")
    p.setdefault("barcode", "")
    # account
    p.setdefault("account_id", "")
    p.setdefault("account_email", "")
    # bot metadata
    p.setdefault("browser_engine", "")
    p.setdefault("proxy_used", "")
    p.setdefault("flow_id", "")
    p.setdefault("checkout_ms", 0)
    p.setdefault("mode", "simulate")
    # user
    p.setdefault("tags", [])
    p.setdefault("notes", "")
    # imagen del evento (del scraper/Discovery API)
    p.setdefault("image_url", "")
    p.setdefault("tm_url", "")
    p.setdefault("event_id", "")
    return p


def _calc_total(p: dict) -> float:
    try:
        unit = float(p.get("unit_price") or 0)
        qty = int(p.get("quantity") or 1)
        fees = float(p.get("fees") or 0)
        return round(unit * qty + fees, 2)
    except Exception:
        return 0


def list_purchases() -> List[dict]:
    d = sqlite_store.load(_path(), default={"purchases": []})
    items = [_migrate(p) for p in d.get("purchases", [])]
    # añadir total calculado
    for p in items:
        p["total_price"] = _calc_total(p)
    return items


def get_purchase(pid: str) -> Optional[dict]:
    for p in list_purchases():
        if p.get("id") == pid:
            return p
    return None


def add_purchase(purchase: dict) -> dict:
    d = sqlite_store.load(_path(), default={"purchases": []})
    p = _migrate(dict(purchase))
    # Compatibilidad retroactiva: si venía 'price' usarlo como unit_price
    if "price" in purchase and not p.get("unit_price"):
        p["unit_price"] = purchase["price"]
    # normalizar seats: acepta str "12-14" o lista
    seats = p.get("seats", [])
    if isinstance(seats, str):
        if "-" in seats:
            try:
                a, b = seats.split("-", 1)
                a, b = int(a.strip()), int(b.strip())
                p["seats"] = [str(x) for x in range(a, b + 1)]
            except Exception:
                p["seats"] = [seats]
        elif "," in seats:
            p["seats"] = [s.strip() for s in seats.split(",") if s.strip()]
        else:
            p["seats"] = [seats]
    # quantity por defecto según seats si hay
    if p["seats"] and p.get("quantity", 1) == 1 and len(p["seats"]) > 1:
        p["quantity"] = len(p["seats"])
    # status válido
    if p.get("status") not in STATUS_CHOICES:
        p["status"] = "confirmed"
    # tags list
    if isinstance(p.get("tags"), str):
        p["tags"] = [t.strip() for t in p["tags"].split(",") if t.strip()]
    d["purchases"].append(p)
    sqlite_store.save(_path(), d)
    p["total_price"] = _calc_total(p)
    return p


def update_purchase(pid: str, patch: dict) -> Optional[dict]:
    """Edita campos permitidos de una compra."""
    allowed = {"event_name", "venue", "city", "country", "event_date",
               "section", "row", "seats", "seat_category",
               "unit_price", "quantity", "fees", "currency",
               "order_id", "confirmation_number", "payment_method",
               "delivery_method", "barcode",
               "account_id", "account_email",
               "browser_engine", "proxy_used", "flow_id",
               "status", "tags", "notes",
               "image_url", "tm_url", "event_id"}
    d = sqlite_store.load(_path(), default={"purchases": []})
    for p in d["purchases"]:
        if p.get("id") == pid:
            for k, v in (patch or {}).items():
                if k in allowed:
                    p[k] = v
            if p.get("status") not in STATUS_CHOICES:
                p["status"] = "confirmed"
            p["updated_at"] = datetime.now().isoformat()
            sqlite_store.save(_path(), d)
            p2 = _migrate(dict(p))
            p2["total_price"] = _calc_total(p2)
            return p2
    return None


def delete_purchase(pid: str) -> bool:
    d = sqlite_store.load(_path(), default={"purchases": []})
    before = len(d["purchases"])
    d["purchases"] = [p for p in d["purchases"] if p.get("id") != pid]
    sqlite_store.save(_path(), d)
    return len(d["purchases"]) < before


def filter_purchases(status: Optional[str] = None, domain_id: Optional[str] = None,
                     event_id: Optional[str] = None, country: Optional[str] = None,
                     search: str = "", seat_category: Optional[str] = None,
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     limit: int = 500) -> List[dict]:
    items = list_purchases()
    if status:
        items = [p for p in items if p.get("status") == status]
    if domain_id:
        items = [p for p in items if p.get("domain_id") == domain_id or p.get("country") == domain_id]
    if country:
        items = [p for p in items if p.get("country") == country]
    if event_id:
        items = [p for p in items if p.get("event_id") == event_id]
    if seat_category:
        items = [p for p in items if p.get("seat_category") == seat_category]
    q = (search or "").lower().strip()
    if q:
        def matches(p):
            blob = " ".join([
                p.get("event_name", ""), p.get("venue", ""), p.get("city", ""),
                p.get("section", ""), p.get("row", ""),
                p.get("order_id", ""), p.get("confirmation_number", ""),
                p.get("account_email", ""), " ".join(p.get("tags", [])),
                " ".join(str(s) for s in p.get("seats", [])),
            ]).lower()
            return q in blob
        items = [p for p in items if matches(p)]
    # ordenar por ts desc
    items.sort(key=lambda p: p.get("ts", ""), reverse=True)
    return items[:limit]


def export_csv() -> str:
    """Exporta a CSV usando path VIVO_COMPRAS."""
    path = config.VIVO_COMPRAS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(export_csv_string())
    return path


def export_csv_string() -> str:
    """Devuelve CSV como string con todos los campos."""
    items = list_purchases()
    if not items:
        return "id,ts,event_name,venue,city,country,event_date,section,row,seats,seat_category,quantity,unit_price,fees,total_price,currency,order_id,confirmation_number,payment_method,delivery_method,status,account_email,browser_engine,mode,tags\n"
    out = io.StringIO()
    fields = ["id", "ts", "event_name", "venue", "city", "country", "event_date",
              "section", "row", "seats", "seat_category",
              "quantity", "unit_price", "fees", "total_price", "currency",
              "order_id", "confirmation_number", "payment_method",
              "delivery_method", "status", "account_email",
              "browser_engine", "mode", "tags"]
    w = csv.DictWriter(out, fieldnames=fields)
    w.writeheader()
    for p in items:
        row = {k: p.get(k, "") for k in fields}
        row["seats"] = ",".join(str(s) for s in (p.get("seats") or []))
        row["tags"] = ",".join(p.get("tags") or [])
        row["total_price"] = _calc_total(p)
        w.writerow(row)
    return out.getvalue()


def get_report() -> dict:
    items = list_purchases()
    total_amount = sum(_calc_total(p) for p in items)
    now = datetime.now()
    upcoming = 0
    past = 0
    for p in items:
        try:
            if p.get("event_date"):
                ed = datetime.fromisoformat(p["event_date"])
                if ed > now:
                    upcoming += 1
                else:
                    past += 1
        except Exception:
            pass
    # breakdown por categoría de asiento
    by_category = {}
    for p in items:
        c = p.get("seat_category", "Reserved")
        by_category[c] = by_category.get(c, 0) + 1
    # breakdown por país
    by_country = {}
    for p in items:
        c = p.get("country", "?")
        by_country[c] = by_country.get(c, 0) + 1
    # breakdown por status
    by_status = {}
    for p in items:
        s = p.get("status", "confirmed")
        by_status[s] = by_status.get(s, 0) + 1
    # tickets totales (contando quantity)
    total_tickets = sum(int(p.get("quantity") or 1) for p in items)
    # unique events
    unique_events = len({p.get("event_name", "") + "|" + p.get("venue", "")
                         for p in items if p.get("event_name")})
    return {
        "total":         len(items),
        "total_tickets": total_tickets,
        "confirmed":     by_status.get("confirmed", 0),
        "pending":       by_status.get("pending", 0),
        "failed":        by_status.get("failed", 0),
        "refunded":      by_status.get("refunded", 0),
        "cancelled":     by_status.get("cancelled", 0),
        "used":          by_status.get("used", 0),
        "upcoming":      upcoming,
        "past":          past,
        "total_amount":  round(total_amount, 2),
        "avg_ticket":    round(total_amount / max(total_tickets, 1), 2),
        "unique_events": unique_events,
        "by_category":   by_category,
        "by_country":    by_country,
        "by_status":     by_status,
    }


def get_categories() -> List[str]:
    return list(SEAT_CATEGORIES)


def get_delivery_methods() -> List[str]:
    return list(DELIVERY_METHODS)


def get_statuses() -> List[str]:
    return sorted(STATUS_CHOICES)
