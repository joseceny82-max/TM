"""api_tokens.py — API tokens para bots externos / automation.

Cada usuario puede generar tokens (headers: Authorization: Bearer tm_...).
Soporta scopes (read, write, admin) y expiración.

Uso desde el dashboard:
  token = create_token(user_id="alice", name="laptop-casa", scopes=["read","write"])
  → {token: "tm_1234..abc", id: "tk_..."}

Uso desde otro equipo / bot:
  curl -H "Authorization: Bearer tm_abc..." http://dashboard/api/purchase-flow/run
"""
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from typing import List, Optional

import config
import json_atomic


_TOKENS_KEY = "api_tokens.json"
_lock = threading.Lock()

PREFIX = "tm_"   # todos los tokens empiezan con tm_

SCOPES = ("read", "write", "admin")


def _load() -> dict:
    return json_atomic.load(config.ruta(_TOKENS_KEY), default={"tokens": []})


def _save(data: dict):
    json_atomic.save(config.ruta(_TOKENS_KEY), data)


def _hash(token: str) -> str:
    """Guardamos solo hash en disco, no el token en plain."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(user_id: str, name: str = "",
                 scopes: Optional[List[str]] = None,
                 expires_in_days: Optional[int] = None) -> dict:
    """Crea un nuevo token. El string plaintext solo se devuelve AHORA,
    luego ya no se puede recuperar."""
    scopes = [s for s in (scopes or ["read"]) if s in SCOPES]
    raw = PREFIX + secrets.token_urlsafe(32)
    tid = "tk_" + secrets.token_hex(6)
    now = datetime.now()
    expires_at = None
    if expires_in_days:
        expires_at = (now + timedelta(days=expires_in_days)).isoformat()
    entry = {
        "id": tid,
        "user_id": user_id,
        "name": name or "api-token",
        "hash": _hash(raw),
        "scopes": scopes,
        "created_at": now.isoformat(),
        "expires_at": expires_at,
        "last_used_at": None,
        "last_used_ip": None,
        "uses_count": 0,
        "revoked": False,
    }
    with _lock:
        d = _load()
        d["tokens"].append(entry)
        _save(d)
    # devolvemos el raw token solo esta vez
    out = dict(entry)
    out.pop("hash", None)
    out["token"] = raw
    return out


def verify_token(raw_token: str, required_scope: str = "read") -> Optional[dict]:
    """Devuelve el entry si el token es válido, None si no."""
    if not raw_token or not raw_token.startswith(PREFIX):
        return None
    h = _hash(raw_token)
    with _lock:
        d = _load()
        for t in d["tokens"]:
            if t.get("hash") != h:
                continue
            if t.get("revoked"):
                return None
            # check expiración
            exp = t.get("expires_at")
            if exp:
                try:
                    if datetime.now() > datetime.fromisoformat(exp):
                        return None
                except Exception:
                    pass
            # check scope
            scopes = t.get("scopes", [])
            if "admin" in scopes:
                pass  # admin puede todo
            elif required_scope not in scopes:
                return None
            return t
    return None


def record_use(token_id: str, ip: str = ""):
    with _lock:
        d = _load()
        for t in d["tokens"]:
            if t.get("id") == token_id:
                t["last_used_at"] = datetime.now().isoformat()
                t["last_used_ip"] = ip
                t["uses_count"] = t.get("uses_count", 0) + 1
                _save(d)
                break


def list_tokens(user_id: Optional[str] = None) -> List[dict]:
    """Nunca devuelve el hash ni el raw token."""
    with _lock:
        d = _load()
    items = d.get("tokens", [])
    if user_id:
        items = [t for t in items if t.get("user_id") == user_id]
    out = []
    for t in items:
        c = {k: v for k, v in t.items() if k != "hash"}
        out.append(c)
    return out


def revoke_token(token_id: str) -> bool:
    with _lock:
        d = _load()
        for t in d["tokens"]:
            if t.get("id") == token_id:
                t["revoked"] = True
                t["revoked_at"] = datetime.now().isoformat()
                _save(d)
                return True
    return False


def delete_token(token_id: str) -> bool:
    with _lock:
        d = _load()
        before = len(d["tokens"])
        d["tokens"] = [t for t in d["tokens"] if t.get("id") != token_id]
        _save(d)
        return len(d["tokens"]) < before


def get_stats() -> dict:
    with _lock:
        d = _load()
    items = d.get("tokens", [])
    active = [t for t in items if not t.get("revoked")]
    return {
        "total": len(items),
        "active": len(active),
        "revoked": len(items) - len(active),
        "total_uses": sum(t.get("uses_count", 0) for t in items),
        "by_scope": {s: sum(1 for t in active if s in t.get("scopes", [])) for s in SCOPES},
    }


# ═══════════════════════════════════════════
#  Flask helper: extrae token de request
# ═══════════════════════════════════════════
def extract_token_from_request():
    """Para usar dentro de endpoints: devuelve (token_entry, raw) o (None, None)."""
    from flask import request
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, None
    raw = auth[7:].strip()
    entry = verify_token(raw, required_scope="read")
    if not entry:
        return None, None
    # record use
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()
    record_use(entry["id"], ip)
    return entry, raw
