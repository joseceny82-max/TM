"""redis_backend.py — Backend distribuido con fallback local.

Permite que varios hosts compartan estado (AcpAT pool, proxy locks,
flow status, cluster heartbeat) sin perder funcionalidad si Redis
no está disponible: todo cae a implementación local threading-safe.

Uso:
  from redis_backend import backend as rb
  rb.set("key", "value", ttl=60)
  rb.get("key")
  rb.publish("channel", {"foo": "bar"})
  rb.subscribe("channel", callback)
  rb.acquire_lock("resource_id", ttl=30)

Configuración por env var:
  REDIS_URL=redis://:password@host:6379/0   (opcional)
  INSTANCE_ID=bot-host-1                     (opcional, auto-gen si falta)
"""
import os
import json
import time
import socket
import threading
import uuid
from typing import Optional, Any, Callable, Dict
from datetime import datetime

_lock = threading.Lock()


def get_instance_id() -> str:
    """ID estable del host actual. Persistido en disco para sobrevivir reinicios."""
    env = os.environ.get("INSTANCE_ID")
    if env:
        return env
    try:
        import config
        path = config.ruta("_instance_id.txt")
        if os.path.exists(path):
            return open(path).read().strip()
        iid = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        with open(path, "w") as fp:
            fp.write(iid)
        return iid
    except Exception:
        return f"host-{uuid.uuid4().hex[:8]}"


class LocalBackend:
    """Fallback in-memory threading-safe cuando Redis no está."""

    def __init__(self):
        self._kv: Dict[str, tuple] = {}      # key -> (value, expires_at)
        self._channels: Dict[str, list] = {} # channel -> [callback,...]
        self._locks: Dict[str, tuple] = {}   # resource -> (owner, expires_at)
        self._counters: Dict[str, int] = {}

    def healthy(self) -> bool:
        return True

    def _gc(self):
        now = time.time()
        for k in list(self._kv.keys()):
            _, exp = self._kv[k]
            if exp and exp < now:
                self._kv.pop(k, None)
        for r in list(self._locks.keys()):
            _, exp = self._locks[r]
            if exp < now:
                self._locks.pop(r, None)

    # ── KV ────────────────────────────────────────
    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        with _lock:
            exp = (time.time() + ttl) if ttl else 0
            self._kv[key] = (self._encode(value), exp)

    def get(self, key: str, default=None):
        with _lock:
            self._gc()
            v = self._kv.get(key)
        if not v:
            return default
        return self._decode(v[0])

    def delete(self, key: str) -> bool:
        with _lock:
            return self._kv.pop(key, None) is not None

    def keys(self, pattern: str = "*") -> list:
        with _lock:
            self._gc()
            if pattern == "*":
                return list(self._kv.keys())
            # soporte básico: prefix*
            if pattern.endswith("*"):
                pref = pattern[:-1]
                return [k for k in self._kv.keys() if k.startswith(pref)]
            return [k for k in self._kv.keys() if k == pattern]

    def incr(self, key: str, amount: int = 1) -> int:
        with _lock:
            v = int(self._counters.get(key, 0)) + amount
            self._counters[key] = v
            return v

    def expire(self, key: str, ttl: int) -> bool:
        with _lock:
            if key not in self._kv:
                return False
            v, _ = self._kv[key]
            self._kv[key] = (v, time.time() + ttl)
            return True

    # ── PUB/SUB ───────────────────────────────────
    def publish(self, channel: str, message: dict) -> int:
        with _lock:
            subs = list(self._channels.get(channel, []))
        sent = 0
        for cb in subs:
            try:
                cb(message)
                sent += 1
            except Exception:
                pass
        return sent

    def subscribe(self, channel: str, callback: Callable) -> str:
        sid = uuid.uuid4().hex[:10]
        with _lock:
            if channel not in self._channels:
                self._channels[channel] = []
            # guardamos tuple (sid, cb)
            self._channels[channel].append(callback)
        return sid

    # ── LOCKS ─────────────────────────────────────
    def acquire_lock(self, resource: str, ttl: int = 30,
                     owner: Optional[str] = None) -> Optional[str]:
        owner = owner or get_instance_id() + "-" + uuid.uuid4().hex[:6]
        with _lock:
            self._gc()
            cur = self._locks.get(resource)
            if cur:
                return None
            self._locks[resource] = (owner, time.time() + ttl)
            return owner

    def release_lock(self, resource: str, owner: str) -> bool:
        with _lock:
            cur = self._locks.get(resource)
            if not cur:
                return False
            if cur[0] != owner:
                return False
            self._locks.pop(resource, None)
            return True

    # ── helpers ───────────────────────────────────
    def _encode(self, v: Any) -> str:
        if isinstance(v, str):
            return v
        try:
            return json.dumps(v)
        except Exception:
            return str(v)

    def _decode(self, s: str) -> Any:
        if not isinstance(s, str):
            return s
        try:
            if s and s[0] in "{[" or s in ("true", "false", "null") or s.isdigit():
                return json.loads(s)
        except Exception:
            pass
        return s


class RedisBackend:
    """Backend real usando redis-py con pubsub thread."""

    def __init__(self, url: str):
        import redis
        self._url = url
        self._r = redis.from_url(url, decode_responses=True, socket_timeout=3,
                                   socket_connect_timeout=2, retry_on_timeout=True)
        self._pubsub = None
        self._pubsub_thread = None
        self._subscribers: Dict[str, list] = {}
        # ping para verificar
        self._r.ping()

    def healthy(self) -> bool:
        try:
            self._r.ping()
            return True
        except Exception:
            return False

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        v = value if isinstance(value, str) else json.dumps(value)
        if ttl:
            self._r.setex(key, ttl, v)
        else:
            self._r.set(key, v)

    def get(self, key: str, default=None):
        v = self._r.get(key)
        if v is None:
            return default
        try:
            if v and v[0] in "{[" or v in ("true","false","null"):
                return json.loads(v)
        except Exception:
            pass
        return v

    def delete(self, key: str) -> bool:
        return bool(self._r.delete(key))

    def keys(self, pattern: str = "*") -> list:
        return self._r.keys(pattern)

    def incr(self, key: str, amount: int = 1) -> int:
        return self._r.incrby(key, amount)

    def expire(self, key: str, ttl: int) -> bool:
        return bool(self._r.expire(key, ttl))

    def publish(self, channel: str, message: dict) -> int:
        return self._r.publish(channel, json.dumps(message))

    def subscribe(self, channel: str, callback: Callable) -> str:
        sid = uuid.uuid4().hex[:10]
        if self._pubsub is None:
            self._pubsub = self._r.pubsub(ignore_subscribe_messages=True)
            self._pubsub_thread = self._pubsub.run_in_thread(sleep_time=0.1, daemon=True)
        self._subscribers.setdefault(channel, []).append(callback)

        def _dispatch(msg):
            try:
                data = json.loads(msg["data"])
            except Exception:
                data = msg.get("data")
            for cb in self._subscribers.get(channel, []):
                try:
                    cb(data)
                except Exception:
                    pass

        self._pubsub.subscribe(**{channel: _dispatch})
        return sid

    def acquire_lock(self, resource: str, ttl: int = 30,
                     owner: Optional[str] = None) -> Optional[str]:
        owner = owner or get_instance_id() + "-" + uuid.uuid4().hex[:6]
        # SET key value NX EX ttl
        ok = self._r.set(f"lock:{resource}", owner, nx=True, ex=ttl)
        return owner if ok else None

    def release_lock(self, resource: str, owner: str) -> bool:
        # lua script atómico: solo borra si sigue siendo del owner
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
        """
        try:
            return bool(self._r.eval(script, 1, f"lock:{resource}", owner))
        except Exception:
            return False


# ═══════════════════════════════════════════
#  Factory
# ═══════════════════════════════════════════
def _make_backend():
    url = os.environ.get("REDIS_URL") or ""
    if url:
        try:
            be = RedisBackend(url)
            print(f"[redis_backend] Conectado a Redis: {url.split('@')[-1]}", flush=True)
            return be
        except Exception as e:
            print(f"[redis_backend] ⚠️ Redis no disponible ({e}), usando LocalBackend", flush=True)
    return LocalBackend()


backend = _make_backend()
INSTANCE_ID = get_instance_id()


def is_distributed() -> bool:
    """True si el backend es Redis (multi-host funcional)."""
    return isinstance(backend, RedisBackend)


def get_info() -> dict:
    return {
        "instance_id": INSTANCE_ID,
        "backend": type(backend).__name__,
        "distributed": is_distributed(),
        "healthy": backend.healthy(),
        "redis_url_set": bool(os.environ.get("REDIS_URL")),
    }
