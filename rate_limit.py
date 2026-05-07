"""rate_limit.py — Rate limiting por IP / user / token.

Usa redis_backend (distribuido si Redis disponible, si no local).

Uso:
  @rate_limit("login", max_per_min=10)
  def api_login():
      ...

  # o manual
  ok, remaining = check_limit("api:user:alice", max=100, window_s=60)
"""
import time
from functools import wraps
from typing import Callable, Tuple

from flask import request, jsonify
import redis_backend as rb


def check_limit(key: str, limit: int, window_s: int = 60) -> Tuple[bool, int]:
    """Sliding window counter. Devuelve (allowed, remaining)."""
    bucket = f"rl:{key}:{int(time.time() // window_s)}"
    count = rb.backend.incr(bucket)
    rb.backend.expire(bucket, window_s + 5)
    remaining = limit - count
    allowed = count <= limit
    return (allowed, remaining if remaining > 0 else 0)


def get_client_key(scope: str) -> str:
    """Identifica al cliente por user-session > api-token > IP."""
    # 1. session user
    try:
        from flask import session
        if session.get("user_id"):
            return f"{scope}:u:{session['user_id']}"
    except Exception:
        pass
    # 2. Bearer token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return f"{scope}:t:{auth[7:20]}"
    # 3. IP
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "?").split(",")[0].strip()
    return f"{scope}:ip:{ip}"


def rate_limit(scope: str, max_per_min: int = 60):
    """Decorator para proteger endpoints Flask."""
    def deco(fn: Callable):
        @wraps(fn)
        def inner(*a, **kw):
            key = get_client_key(scope)
            ok, rem = check_limit(key, limit=max_per_min, window_s=60)
            if not ok:
                return jsonify({
                    "ok": False,
                    "error": "rate limit exceeded",
                    "retry_after_s": 60,
                    "max_per_min": max_per_min,
                }), 429
            return fn(*a, **kw)
        return inner
    return deco


def get_stats(scope: str = "") -> dict:
    """Snapshot contadores activos."""
    pattern = f"rl:{scope}*" if scope else "rl:*"
    keys = rb.backend.keys(pattern)
    out = {"keys": len(keys), "by_scope": {}}
    for k in keys[:200]:
        parts = k.split(":")
        if len(parts) >= 2:
            s = parts[1]
            out["by_scope"][s] = out["by_scope"].get(s, 0) + 1
    return out
