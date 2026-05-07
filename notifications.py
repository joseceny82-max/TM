"""notifications.py — Sistema de notificaciones (Telegram, Discord, Email)."""
from datetime import datetime
import config
import json_atomic


def _config_path():
    return config.ruta("notifications.json")


# ═══════════════════════════════════════════
#  CATÁLOGO DE EVENTOS (agrupados por área)
# ═══════════════════════════════════════════
EVENT_CATALOG = [
    # Compras y Pagos
    {"group": "purchases", "id": "purchase_success",   "default": True,  "icon": "🎟️", "level": "success", "label": {"es": "Compra completada",        "en": "Purchase completed"}},
    {"group": "purchases", "id": "purchase_failed",    "default": True,  "icon": "❌", "level": "error",   "label": {"es": "Compra fallida",            "en": "Purchase failed"}},
    {"group": "purchases", "id": "purchase_cart",      "default": False, "icon": "🛒", "level": "info",    "label": {"es": "Añadido al carrito",         "en": "Added to cart"}},
    {"group": "purchases", "id": "payment_rejected",   "default": True,  "icon": "💳", "level": "warning", "label": {"es": "Pago rechazado (3DS/fraude)", "en": "Payment rejected (3DS/fraud)"}},
    # Queue-it y Bot
    {"group": "bot",       "id": "queue_entered",      "default": False, "icon": "🏃", "level": "info",    "label": {"es": "Entrada en Queue-it",        "en": "Entered Queue-it"}},
    {"group": "bot",       "id": "queue_passed",       "default": True,  "icon": "✅", "level": "success", "label": {"es": "Queue-it superada",          "en": "Queue-it passed"}},
    {"group": "bot",       "id": "worker_start",       "default": False, "icon": "▶️",  "level": "info",    "label": {"es": "Worker arrancado",            "en": "Worker started"}},
    {"group": "bot",       "id": "worker_end",         "default": False, "icon": "⏹",  "level": "info",    "label": {"es": "Worker finalizado",           "en": "Worker ended"}},
    {"group": "bot",       "id": "worker_error",       "default": True,  "icon": "🤖", "level": "error",   "label": {"es": "Error en worker",            "en": "Worker error"}},
    {"group": "bot",       "id": "flow_ok",            "default": True,  "icon": "🎯", "level": "success", "label": {"es": "Flujo de compra OK",         "en": "Purchase flow OK"}},
    {"group": "bot",       "id": "flow_fail",          "default": True,  "icon": "🎯", "level": "error",   "label": {"es": "Flujo de compra falló",      "en": "Purchase flow failed"}},
    # Anti-detect / Seguridad
    {"group": "security",  "id": "captcha_solved",     "default": False, "icon": "🧠", "level": "info",    "label": {"es": "CAPTCHA resuelto",           "en": "CAPTCHA solved"}},
    {"group": "security",  "id": "captcha_failed",     "default": True,  "icon": "🧠", "level": "warning", "label": {"es": "CAPTCHA falló",              "en": "CAPTCHA failed"}},
    {"group": "security",  "id": "proxy_dead",         "default": True,  "icon": "💀", "level": "warning", "label": {"es": "Proxy caído",                "en": "Proxy dead"}},
    {"group": "security",  "id": "ja4_test",           "default": False, "icon": "🧬", "level": "info",    "label": {"es": "Test TLS JA4+",              "en": "TLS JA4+ test"}},
    {"group": "security",  "id": "browser_launched",   "default": False, "icon": "🌐", "level": "info",    "label": {"es": "Navegador lanzado",          "en": "Browser launched"}},
    # Eventos y Drops
    {"group": "events",    "id": "drop_approaching",   "default": True,  "icon": "🚨", "level": "warning", "label": {"es": "Drop en <1h",                "en": "Drop in <1h"}},
    {"group": "events",    "id": "drop_live",          "default": True,  "icon": "🔴", "level": "success", "label": {"es": "Drop EN VENTA ahora",         "en": "Drop ON SALE now"}},
    {"group": "events",    "id": "event_sold_out",     "default": False, "icon": "🚫", "level": "warning", "label": {"es": "Evento sold out",            "en": "Event sold out"}},
    {"group": "events",    "id": "event_imported",     "default": False, "icon": "📥", "level": "info",    "label": {"es": "Evento importado (Discovery)", "en": "Event imported (Discovery)"}},
    # Sistema
    {"group": "system",    "id": "critical_problem",   "default": True,  "icon": "🔥", "level": "critical","label": {"es": "Problema crítico",           "en": "Critical problem"}},
    {"group": "system",    "id": "module_down",        "default": True,  "icon": "💔", "level": "error",   "label": {"es": "Módulo caído",               "en": "Module down"}},
    {"group": "system",    "id": "backup_ok",          "default": False, "icon": "💾", "level": "success", "label": {"es": "Backup creado",              "en": "Backup created"}},
    {"group": "system",    "id": "backup_fail",        "default": True,  "icon": "💾", "level": "error",   "label": {"es": "Backup falló",               "en": "Backup failed"}},
    # Usuarios y acceso
    {"group": "access",    "id": "login_ok",           "default": False, "icon": "🔐", "level": "info",    "label": {"es": "Login exitoso",              "en": "Login success"}},
    {"group": "access",    "id": "login_fail",         "default": True,  "icon": "🚫", "level": "warning", "label": {"es": "Login fallido",              "en": "Login failed"}},
    {"group": "access",    "id": "user_locked",        "default": True,  "icon": "🔒", "level": "warning", "label": {"es": "Usuario bloqueado (5 fallos)", "en": "User locked (5 failures)"}},
    # Registros TM
    {"group": "registros", "id": "registration_ok",    "default": True,  "icon": "📝", "level": "success", "label": {"es": "Registro TM exitoso",        "en": "TM registration success"}},
    {"group": "registros", "id": "registration_fail",  "default": True,  "icon": "📝", "level": "error",   "label": {"es": "Registro TM fallido",        "en": "TM registration failed"}},
]

GROUPS_META = {
    "purchases": {"icon": "🎟️", "label": {"es": "Compras y pagos",   "en": "Purchases & payments"}},
    "bot":       {"icon": "🤖", "label": {"es": "Bot y workers",      "en": "Bot & workers"}},
    "security":  {"icon": "🛡",  "label": {"es": "Seguridad y stealth","en": "Security & stealth"}},
    "events":    {"icon": "🎭", "label": {"es": "Eventos y drops",    "en": "Events & drops"}},
    "system":    {"icon": "⚙️", "label": {"es": "Sistema",            "en": "System"}},
    "access":    {"icon": "🔐", "label": {"es": "Acceso y usuarios",  "en": "Access & users"}},
    "registros": {"icon": "📋", "label": {"es": "Registros TM",       "en": "TM Registrations"}},
}


def get_event_catalog() -> dict:
    """Retorna catálogo completo agrupado por área con labels ES/EN."""
    grouped = {}
    for ev in EVENT_CATALOG:
        g = ev["group"]
        if g not in grouped:
            meta = GROUPS_META.get(g, {})
            grouped[g] = {
                "group": g,
                "icon": meta.get("icon", "📢"),
                "label": meta.get("label", {"es": g, "en": g}),
                "events": [],
            }
        grouped[g]["events"].append(ev)
    return {"groups": grouped, "all_events": EVENT_CATALOG}


def get_config():
    default = {
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "discord":  {"enabled": False, "webhook": ""},
        "email":    {"enabled": False, "smtp_host": "", "smtp_port": 587,
                     "username": "", "password": "", "from": "", "to": ""},
        "events": {ev["id"]: ev["default"] for ev in EVENT_CATALOG},
        "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
        "history": [],
    }
    cur = json_atomic.load(_config_path(), default=default)
    # Asegurar todos los eventos del catálogo presentes (migración automática)
    cur.setdefault("events", {})
    for ev in EVENT_CATALOG:
        cur["events"].setdefault(ev["id"], ev["default"])
    cur.setdefault("quiet_hours", default["quiet_hours"])
    return cur


def save_config(cfg):
    current = get_config()
    current.update(cfg)
    json_atomic.save(_config_path(), current)
    return current


LEVEL_COLORS = {"info": 0x026CDF, "success": 0x22C55E, "warning": 0xF59E0B, "error": 0xEF4444, "critical": 0xB91C1C}
LEVEL_EMOJI = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌", "critical": "🔥"}


def _send_discord(webhook_url: str, title: str, message: str, level: str = "info", fields: list = None) -> dict:
    """Envía embed real a Discord vía webhook. Retorna dict con ok/status/error."""
    import requests
    try:
        payload = {
            "username": "TM PERROS Bot",
            "embeds": [{
                "title": f"{LEVEL_EMOJI.get(level, 'ℹ️')} {title}",
                "description": message[:2000] if message else "",
                "color": LEVEL_COLORS.get(level, 0x026CDF),
                "timestamp": datetime.now().isoformat(),
                "fields": fields or [],
                "footer": {"text": "TM PERROS · Dashboard"},
            }],
        }
        r = requests.post(webhook_url, json=payload, timeout=10)
        return {"ok": 200 <= r.status_code < 300, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _send_telegram(token: str, chat_id: str, title: str, message: str, level: str = "info") -> dict:
    """Envía mensaje Markdown a Telegram Bot API."""
    import requests
    try:
        text = f"{LEVEL_EMOJI.get(level, 'ℹ️')} *{title}*\n{message}"
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown"},
                          timeout=10)
        return {"ok": 200 <= r.status_code < 300, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _in_quiet_hours(cfg: dict) -> bool:
    qh = cfg.get("quiet_hours", {})
    if not qh.get("enabled"):
        return False
    try:
        now_t = datetime.now().strftime("%H:%M")
        start = qh.get("start", "23:00")
        end = qh.get("end", "08:00")
        if start <= end:
            return start <= now_t < end
        # horario cruzando medianoche (23:00-08:00)
        return now_t >= start or now_t < end
    except Exception:
        return False


def notify(event_type, title, message, level="info", fields: list = None,
           force: bool = False):
    cfg = get_config()
    # Check si el evento está habilitado
    if not cfg.get("events", {}).get(event_type, True) and not force:
        return None
    # Quiet hours (respetadas salvo críticos)
    if _in_quiet_hours(cfg) and level != "critical" and not force:
        return {"ok": False, "skipped": "quiet_hours"}
    entry = {
        "ts": datetime.now().isoformat(),
        "type": event_type,
        "title": title,
        "message": message,
        "level": level,
        "sent_to": [],
        "dispatch": {},
    }
    tg = cfg.get("telegram", {})
    if tg.get("enabled") and tg.get("token") and tg.get("chat_id"):
        res = _send_telegram(tg["token"], tg["chat_id"], title, message, level)
        entry["sent_to"].append("telegram")
        entry["dispatch"]["telegram"] = res
    dc = cfg.get("discord", {})
    if dc.get("enabled") and dc.get("webhook"):
        res = _send_discord(dc["webhook"], title, message, level, fields)
        entry["sent_to"].append("discord")
        entry["dispatch"]["discord"] = res
    if cfg.get("email", {}).get("enabled"):
        entry["sent_to"].append("email")
        entry["dispatch"]["email"] = {"ok": False, "error": "SMTP no implementado"}
    cfg.setdefault("history", []).append(entry)
    cfg["history"] = cfg["history"][-500:]
    json_atomic.save(_config_path(), cfg)
    return entry


def test_channel(channel: str) -> dict:
    """Prueba un canal concreto ('discord'|'telegram') enviando un mensaje de prueba."""
    cfg = get_config()
    title = "🧪 Test de notificación"
    message = f"Canal **{channel}** verificado desde TM PERROS Dashboard"
    if channel == "discord":
        dc = cfg.get("discord", {})
        if not dc.get("webhook"):
            return {"ok": False, "error": "Webhook Discord no configurado"}
        return _send_discord(dc["webhook"], title, message, "info")
    if channel == "telegram":
        tg = cfg.get("telegram", {})
        if not tg.get("token") or not tg.get("chat_id"):
            return {"ok": False, "error": "Token/chat_id Telegram no configurado"}
        return _send_telegram(tg["token"], tg["chat_id"], title, message, "info")
    return {"ok": False, "error": "canal desconocido"}


def get_history(limit=100):
    return get_config().get("history", [])[-limit:][::-1]


def clear_history():
    cfg = get_config()
    cfg["history"] = []
    json_atomic.save(_config_path(), cfg)
