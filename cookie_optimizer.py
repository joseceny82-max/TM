"""cookie_optimizer.py — Gestion, analisis y optimizacion del pool de cookies."""
import uuid
from datetime import datetime
import config
import json_atomic


def _path():
    return config.ruta("cookies.json")


def list_cookies():
    return json_atomic.load(_path(), default={"cookies": []}).get("cookies", [])


def add_cookie(cookie):
    d = json_atomic.load(_path(), default={"cookies": []})
    cookie = dict(cookie)
    cookie.setdefault("id", uuid.uuid4().hex[:8])
    cookie.setdefault("created_at", datetime.now().isoformat())
    cookie.setdefault("status", "active")
    d["cookies"].append(cookie)
    json_atomic.save(_path(), d)
    return cookie


def delete_cookie(cid):
    d = json_atomic.load(_path(), default={"cookies": []})
    d["cookies"] = [c for c in d["cookies"] if c.get("id") != cid]
    json_atomic.save(_path(), d)


def analyze_cookies():
    all_c = list_cookies()
    return {
        "total":   len(all_c),
        "active":  sum(1 for c in all_c if c.get("status") == "active"),
        "expired": sum(1 for c in all_c if c.get("status") == "expired"),
        "bad":     sum(1 for c in all_c if c.get("status") == "bad"),
    }


def clean_cookies():
    d = json_atomic.load(_path(), default={"cookies": []})
    before = len(d["cookies"])
    d["cookies"] = [c for c in d["cookies"] if c.get("status") != "bad"]
    json_atomic.save(_path(), d)
    return {"removed": before - len(d["cookies"])}


def auto_manage_cookies():
    return clean_cookies()


def optimize_for_bot(domain_id=None):
    return {"optimized": len(list_cookies()), "domain": domain_id}


def warm_cookies(count=1):
    return {"warmed": count, "ts": datetime.now().isoformat()}


def get_cookie_pool_stats():
    return analyze_cookies()


def delete_all_bad_cookies():
    d = json_atomic.load(_path(), default={"cookies": []})
    before = len(d["cookies"])
    d["cookies"] = [c for c in d["cookies"] if c.get("status") != "bad"]
    json_atomic.save(_path(), d)
    return before - len(d["cookies"])
