"""session_persistence.py — Persistencia de Playwright storage_state en Redis con fallback local.

Guarda el `storage_state` (cookies + localStorage + sessionStorage + IndexedDB
metadata) que Playwright entrega tras un login exitoso, indexado por
(account_id, domain). Otros workers pueden cargar el mismo state en su
launch para arrancar autenticados sin re-login.

  - Backend: usa `redis_backend.backend` (se auto-eleva a Redis real si
    `REDIS_URL` está; si no, LocalBackend in-memory threading-safe).
  - TTL por defecto: 24h (alineado con la tarjeta-mejora).
  - Invalida automáticamente si una llamada `mark_failed(key)` es invocada
    tras un login fallido — útil cuando el state caducó por server-side.

API pública:
  save(account_id, domain, storage_state, *, ttl_s=86400) -> dict
  load(account_id, domain) -> dict | None
  invalidate(account_id, domain) -> dict
  mark_failed(account_id, domain) -> dict   # invalida + incrementa contador
  list_keys() -> list[str]
  stats() -> dict
  apply_to_playwright_context(context, account_id, domain) -> bool
  capture_from_playwright_context(context, account_id, domain, *, ttl_s) -> dict
  self_test() -> dict
  get_info() -> dict
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import json_atomic

try:
    from redis_backend import backend as _rb
except Exception:
    _rb = None

_KEY_PREFIX = "session_state:"
_FAIL_PREFIX = "session_fail:"
_INDEX_KEY = "session_state:_index"
_DEFAULT_TTL = 86400  # 24h
_lock = threading.RLock()
_LOG_KEY = "session_persistence_log.json"


def _ckey(account_id: str, domain: str) -> str:
    return f"{_KEY_PREFIX}{account_id}|{domain}"


def _fkey(account_id: str, domain: str) -> str:
    return f"{_FAIL_PREFIX}{account_id}|{domain}"


def _index_add(key: str):
    if not _rb:
        return
    try:
        idx = _rb.get(_INDEX_KEY) or {}
        if not isinstance(idx, dict):
            idx = {}
        idx[key] = time.time()
        _rb.set(_INDEX_KEY, idx)
    except Exception:
        pass


def _index_remove(key: str):
    if not _rb:
        return
    try:
        idx = _rb.get(_INDEX_KEY) or {}
        if isinstance(idx, dict):
            idx.pop(key, None)
            _rb.set(_INDEX_KEY, idx)
    except Exception:
        pass


def _log_event(action: str, account_id: str, domain: str, **extra):
    with _lock:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"events": []})
        d.setdefault("events", []).append({
            "ts": datetime.now().isoformat(),
            "action": action,
            "account_id": account_id,
            "domain": domain,
            **extra,
        })
        d["events"] = d["events"][-300:]
        json_atomic.save(config.ruta(_LOG_KEY), d)


# ─── API pública ──────────────────────────────────────────────────
def save(account_id: str, domain: str, storage_state: Dict,
          *, ttl_s: int = _DEFAULT_TTL) -> Dict:
    """Guarda storage_state. Devuelve {ok, key, expires_in_s, size_bytes}."""
    if not _rb:
        return {"ok": False, "error": "redis_backend_unavailable"}
    if not account_id or not domain:
        return {"ok": False, "error": "account_id y domain requeridos"}
    if not isinstance(storage_state, dict):
        return {"ok": False, "error": "storage_state must be dict"}
    key = _ckey(account_id, domain)
    payload = {
        "storage_state": storage_state,
        "saved_at": datetime.now().isoformat(),
        "saved_ts": time.time(),
        "account_id": account_id,
        "domain": domain,
    }
    raw = json.dumps(payload, ensure_ascii=False)
    _rb.set(key, payload, ttl=int(max(60, ttl_s)))
    _index_add(key)
    _log_event("save", account_id, domain, ttl_s=ttl_s, size_bytes=len(raw))
    return {"ok": True, "key": key, "expires_in_s": int(ttl_s),
            "size_bytes": len(raw)}


def load(account_id: str, domain: str) -> Optional[Dict]:
    """Devuelve {storage_state, saved_at, age_s} o None si missing/expirado."""
    if not _rb:
        return None
    if not account_id or not domain:
        return None
    key = _ckey(account_id, domain)
    payload = _rb.get(key)
    if not payload or not isinstance(payload, dict):
        return None
    age = int(time.time() - payload.get("saved_ts", time.time()))
    return {
        "storage_state": payload.get("storage_state"),
        "saved_at": payload.get("saved_at"),
        "age_s": age,
        "account_id": account_id,
        "domain": domain,
    }


def invalidate(account_id: str, domain: str) -> Dict:
    """Borra el state cacheado."""
    if not _rb:
        return {"ok": False, "error": "redis_backend_unavailable"}
    key = _ckey(account_id, domain)
    removed = bool(_rb.delete(key))
    if removed:
        _index_remove(key)
        _log_event("invalidate", account_id, domain)
    return {"ok": True, "removed": removed, "key": key}


def mark_failed(account_id: str, domain: str) -> Dict:
    """Invalida tras un fallo + incrementa contador de fallos por par."""
    if not _rb:
        return {"ok": False, "error": "redis_backend_unavailable"}
    key = _ckey(account_id, domain)
    fkey = _fkey(account_id, domain)
    _rb.delete(key)
    _index_remove(key)
    fails = _rb.incr(fkey, 1) if hasattr(_rb, "incr") else 1
    _rb.expire(fkey, 7 * 86400) if hasattr(_rb, "expire") else None
    _log_event("mark_failed", account_id, domain, fails=fails)
    return {"ok": True, "invalidated": True, "fails": fails, "key": key}


def list_keys() -> List[str]:
    """Lista todas las keys gestionadas (vía índice + scan keys)."""
    if not _rb:
        return []
    keys = set()
    try:
        idx = _rb.get(_INDEX_KEY) or {}
        if isinstance(idx, dict):
            keys.update(idx.keys())
    except Exception:
        pass
    try:
        if hasattr(_rb, "keys"):
            scan = _rb.keys(_KEY_PREFIX + "*") or []
            keys.update(k for k in scan if k != _INDEX_KEY)
    except Exception:
        pass
    # purgar las que ya no existen
    alive = []
    for k in keys:
        if _rb.get(k):
            alive.append(k)
        else:
            _index_remove(k)
    return sorted(alive)


def stats() -> Dict:
    keys = list_keys()
    sample = []
    for k in keys[:5]:
        v = _rb.get(k) if _rb else None
        if isinstance(v, dict):
            sample.append({
                "account_id": v.get("account_id"),
                "domain": v.get("domain"),
                "saved_at": v.get("saved_at"),
            })
    return {
        "ok": True,
        "backend": "redis" if (_rb and getattr(_rb, "_r", None)) else "local",
        "active_states": len(keys),
        "default_ttl_s": _DEFAULT_TTL,
        "sample": sample,
    }


# ─── Integración Playwright (best-effort, no rompe si no está) ────
def apply_to_playwright_context(context, account_id: str, domain: str) -> bool:
    """Inyecta el storage_state cacheado a un Playwright BrowserContext.

    `context` es un `playwright.async_api.BrowserContext` o `sync_api`. Si el
    contexto ya tiene cookies, no se duplican (Playwright merge). Devuelve
    True si se aplicó algo, False si no había cache.
    """
    cached = load(account_id, domain)
    if not cached or not cached.get("storage_state"):
        return False
    state = cached["storage_state"]
    try:
        cookies = state.get("cookies") or []
        origins = state.get("origins") or []
        if cookies and hasattr(context, "add_cookies"):
            context.add_cookies(cookies)
        # localStorage por origen
        for origin in origins:
            url = origin.get("origin")
            if not url:
                continue
            for kv in origin.get("localStorage") or []:
                k, v = kv.get("name"), kv.get("value")
                if not k:
                    continue
                try:
                    page = context.new_page() if hasattr(context, "new_page") else None
                    if page:
                        page.goto(url, timeout=8000)
                        page.evaluate(f"() => localStorage.setItem({json.dumps(k)}, {json.dumps(v)})")
                        page.close()
                except Exception:
                    continue
        return True
    except Exception:
        return False


def capture_from_playwright_context(context, account_id: str, domain: str,
                                     *, ttl_s: int = _DEFAULT_TTL) -> Dict:
    """Llama a `context.storage_state()` y lo guarda."""
    try:
        if hasattr(context, "storage_state"):
            state = context.storage_state()
        else:
            return {"ok": False, "error": "context lacks storage_state()"}
        if isinstance(state, str):
            state = json.loads(state)
        return save(account_id, domain, state, ttl_s=ttl_s)
    except Exception as e:
        return {"ok": False, "error": f"capture_failed: {e}"}


# ─── Self-test sin Playwright ─────────────────────────────────────
def self_test() -> Dict:
    r = {"checks": {}}
    if not _rb:
        return {"ok": False, "error": "redis_backend not importable",
                "checks": {"redis_backend_loaded": False}, "total_checks": 1, "passed_checks": 0}

    aid, dom = "test_sp_acc", "test.example"
    fake_state = {
        "cookies": [
            {"name": "auth", "value": "tok123", "domain": dom, "path": "/"},
            {"name": "csrf", "value": "csrfXYZ", "domain": dom, "path": "/"},
        ],
        "origins": [
            {"origin": f"https://{dom}",
             "localStorage": [{"name": "user_id", "value": "42"}]},
        ],
    }

    # 1. save
    s = save(aid, dom, fake_state, ttl_s=300)
    r["checks"]["save_ok"] = s.get("ok") is True
    r["checks"]["save_returns_size"] = s.get("size_bytes", 0) > 0

    # 2. load
    ld = load(aid, dom)
    r["checks"]["load_ok"] = ld is not None
    r["checks"]["load_has_cookies"] = bool(
        ld and ld["storage_state"]["cookies"][0]["value"] == "tok123"
    )
    r["checks"]["load_returns_age"] = ld is not None and ld.get("age_s") is not None

    # 3. list_keys
    keys = list_keys()
    r["checks"]["list_keys_finds_test"] = _ckey(aid, dom) in keys

    # 4. mark_failed → invalidate
    mf = mark_failed(aid, dom)
    r["checks"]["mark_failed_ok"] = mf.get("ok") is True
    r["checks"]["mark_failed_invalidates"] = load(aid, dom) is None

    # 5. invalidate idempotente
    inv = invalidate(aid, dom)
    r["checks"]["invalidate_idempotent"] = inv.get("ok") is True

    # 6. ttl real (corto) — guardamos con ttl=60 y vemos que load funciona
    save(aid, dom, fake_state, ttl_s=60)
    ld2 = load(aid, dom)
    r["checks"]["ttl_alive_immediately"] = ld2 is not None

    # cleanup
    invalidate(aid, dom)

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {
        "module": "session_persistence",
        "version": 1,
        "default_ttl_s": _DEFAULT_TTL,
        "backend_available": _rb is not None,
        "stats": stats(),
    }
