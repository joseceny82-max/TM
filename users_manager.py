"""users_manager.py — Gestión avanzada de usuarios del dashboard.

Features:
- PBKDF2 password hashing
- Tracking por login: IP, user-agent, geo (pasivo), timestamp
- Failed login counter + auto-lockout tras N intentos fallidos (15 min)
- Session tracking (active_sessions_count)
- Role-based: admin / operator / viewer
- Historial por usuario (últimos 20 logins)
"""
import hashlib
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import config
import json_atomic


_lock = threading.Lock()
FAILED_LOCKOUT_THRESHOLD = 5
FAILED_LOCKOUT_MINUTES = 15


def _path():
    return config.ruta("users.json")


def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"pbkdf2:sha256:100000:{salt}:{h.hex()}"


def _verify_password(password, hashed):
    try:
        scheme, algo, iters, salt, h = hashed.split(":")
        new = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), salt.encode("utf-8"), int(iters))
        return new.hex() == h
    except Exception:
        return False


def _migrate(u: dict) -> dict:
    """Añade campos nuevos a usuarios antiguos."""
    u.setdefault("failed_logins", 0)
    u.setdefault("last_failed_at", None)
    u.setdefault("locked_until", None)
    u.setdefault("last_ip", None)
    u.setdefault("last_user_agent", None)
    u.setdefault("login_history", [])  # últimos N logins (dict: ts, ip, ua, success)
    u.setdefault("active_sessions", 0)
    u.setdefault("email", "")
    u.setdefault("notes", "")
    return u


def seed_admin_if_empty():
    with _lock:
        d = json_atomic.load(_path(), default={"users": []})
        if not d.get("users"):
            d["users"].append(_migrate({
                "id": uuid.uuid4().hex[:8],
                "username": "admin",
                "password": _hash_password("admin"),
                "role": "admin",
                "created_at": datetime.now().isoformat(),
                "last_login": None,
                "login_count": 0,
            }))
            json_atomic.save(_path(), d)


def _load():
    d = json_atomic.load(_path(), default={"users": []})
    d["users"] = [_migrate(u) for u in d.get("users", [])]
    return d


def _safe(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "password"}


def list_users() -> List[dict]:
    seed_admin_if_empty()
    d = _load()
    return [_safe(u) for u in d.get("users", [])]


def get_user(uid: str) -> Optional[dict]:
    d = _load()
    u = next((x for x in d["users"] if x["id"] == uid), None)
    return _safe(u) if u else None


def add_user(username: str, password: str, role: str = "user",
             email: str = "", notes: str = "") -> Optional[dict]:
    with _lock:
        d = _load()
        if any(u["username"] == username for u in d.get("users", [])):
            return None
        u = _migrate({
            "id": uuid.uuid4().hex[:8],
            "username": username,
            "password": _hash_password(password),
            "role": role,
            "email": email,
            "notes": notes,
            "created_at": datetime.now().isoformat(),
            "last_login": None,
            "login_count": 0,
        })
        d["users"].append(u)
        json_atomic.save(_path(), d)
    return _safe(u)


def update_user(uid: str, patch: dict) -> Optional[dict]:
    """Actualiza campos permitidos del usuario."""
    allowed = {"role", "email", "notes"}
    with _lock:
        d = _load()
        for u in d["users"]:
            if u["id"] == uid:
                for k, v in (patch or {}).items():
                    if k in allowed:
                        u[k] = v
                json_atomic.save(_path(), d)
                return _safe(u)
    return None


def change_password(uid: str, new_password: str) -> bool:
    if len(new_password) < 6:
        return False
    with _lock:
        d = _load()
        for u in d["users"]:
            if u["id"] == uid:
                u["password"] = _hash_password(new_password)
                u["failed_logins"] = 0
                u["locked_until"] = None
                json_atomic.save(_path(), d)
                return True
    return False


def delete_user(uid: str) -> bool:
    with _lock:
        d = _load()
        before = len(d["users"])
        d["users"] = [u for u in d["users"] if u["id"] != uid]
        json_atomic.save(_path(), d)
    return len(d["users"]) < before


def unlock_user(uid: str) -> bool:
    with _lock:
        d = _load()
        for u in d["users"]:
            if u["id"] == uid:
                u["failed_logins"] = 0
                u["locked_until"] = None
                json_atomic.save(_path(), d)
                return True
    return False


def _is_locked(u: dict) -> bool:
    lu = u.get("locked_until")
    if not lu:
        return False
    try:
        return datetime.fromisoformat(lu) > datetime.now()
    except Exception:
        return False


def verify_login(username: str, password: str,
                 ip: str = "", user_agent: str = "") -> Optional[dict]:
    seed_admin_if_empty()
    with _lock:
        d = _load()
        for u in d.get("users", []):
            if u["username"] != username:
                continue
            # ¿bloqueado?
            if _is_locked(u):
                u["login_history"].insert(0, {
                    "ts": datetime.now().isoformat(),
                    "ip": ip, "ua": user_agent[:150],
                    "success": False, "reason": "locked",
                })
                u["login_history"] = u["login_history"][:20]
                json_atomic.save(_path(), d)
                return {"error": "locked", "locked_until": u.get("locked_until")}
            ok = _verify_password(password, u["password"])
            entry = {
                "ts": datetime.now().isoformat(),
                "ip": ip, "ua": user_agent[:150],
                "success": ok,
            }
            u["login_history"].insert(0, entry)
            u["login_history"] = u["login_history"][:20]
            if ok:
                u["last_login"] = datetime.now().isoformat()
                u["login_count"] = u.get("login_count", 0) + 1
                u["failed_logins"] = 0
                u["locked_until"] = None
                u["last_ip"] = ip or u.get("last_ip")
                u["last_user_agent"] = user_agent[:200] or u.get("last_user_agent")
                u["active_sessions"] = u.get("active_sessions", 0) + 1
                json_atomic.save(_path(), d)
                return _safe(u)
            # login fallido
            u["failed_logins"] = u.get("failed_logins", 0) + 1
            u["last_failed_at"] = datetime.now().isoformat()
            if u["failed_logins"] >= FAILED_LOCKOUT_THRESHOLD:
                u["locked_until"] = (datetime.now() + timedelta(minutes=FAILED_LOCKOUT_MINUTES)).isoformat()
                entry["reason"] = "auto-locked"
            json_atomic.save(_path(), d)
            return None
    return None


def logout(uid: str):
    with _lock:
        d = _load()
        for u in d["users"]:
            if u["id"] == uid and u.get("active_sessions", 0) > 0:
                u["active_sessions"] = max(0, u.get("active_sessions", 0) - 1)
                json_atomic.save(_path(), d)
                return True
    return False


def get_stats() -> dict:
    """Stats globales."""
    d = _load()
    users = d.get("users", [])
    total_logins = sum(u.get("login_count", 0) for u in users)
    total_failed = sum(u.get("failed_logins", 0) for u in users)
    active_now = sum(1 for u in users if u.get("active_sessions", 0) > 0)
    locked = sum(1 for u in users if _is_locked(u))
    by_role = {}
    for u in users:
        by_role[u.get("role", "user")] = by_role.get(u.get("role", "user"), 0) + 1
    # último login global
    last_login = None
    for u in users:
        ll = u.get("last_login")
        if ll and (not last_login or ll > last_login):
            last_login = ll
    return {
        "total": len(users),
        "active_sessions": active_now,
        "locked": locked,
        "total_logins": total_logins,
        "total_failed": total_failed,
        "by_role": by_role,
        "last_login": last_login,
    }


def get_login_history(uid: str, limit: int = 20) -> List[dict]:
    d = _load()
    for u in d["users"]:
        if u["id"] == uid:
            return u.get("login_history", [])[:limit]
    return []
