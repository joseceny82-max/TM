"""analytics.py — Tracking de eventos y KPIs."""
from datetime import datetime, timedelta
import config
import json_atomic


def _path():
    return config.ruta("analytics_data.json")


def track_event(event_type, data=None):
    d = json_atomic.load(_path(), default={"events": []})
    d["events"].append({
        "type": event_type,
        "ts": datetime.now().isoformat(),
        "data": data or {},
    })
    d["events"] = d["events"][-5000:]
    json_atomic.save(_path(), d)


def get_events(limit=200):
    return json_atomic.load(_path(), default={"events": []}).get("events", [])[-limit:][::-1]


def get_analytics():
    events = json_atomic.load(_path(), default={"events": []}).get("events", [])
    now = datetime.now()
    h24 = now - timedelta(hours=24)
    h7d = now - timedelta(days=7)
    def parse(e):
        try:
            return datetime.fromisoformat(e["ts"])
        except Exception:
            return now
    e24 = [e for e in events if parse(e) >= h24]
    e7d = [e for e in events if parse(e) >= h7d]
    purchases = [e for e in events if e["type"] == "purchase_success"]
    attempts = [e for e in events if e["type"] in ("purchase_attempt", "purchase_success", "purchase_failed")]
    successes = len(purchases)
    total = max(1, len(attempts))
    return {
        "total_events":    len(events),
        "events_24h":      len(e24),
        "events_7d":       len(e7d),
        "purchases":       successes,
        "conversion_rate": round(100 * successes / total, 2),
        "by_type": {
            t: sum(1 for e in events if e["type"] == t)
            for t in {e["type"] for e in events}
        },
    }


def reset_analytics():
    json_atomic.save(_path(), {"events": []})
