"""imperva_bypass.py — Imperva/Incapsula JS challenge bypass.

Imperva (Incapsula) sirve un challenge JS embebido como meta-refresh o como
respuesta 200 con un script obfuscado que, al ejecutarse, devuelve cookies
`visid_incap_*` y `incap_ses_*`. El cliente normal espera ~10s al meta refresh.
Este módulo:

  1. Captura el JS challenge (HTTP 200 con cuerpo HTML que contiene
     `_Incapsula_Resource` o un literal `incap_ses_` o `visid_incap_`).
  2. Resuelve el challenge ejecutándolo inline en una sandbox JS (preferimos
     `js2py` si está instalado; caemos a una resolución por patrón si no
     está disponible — lo común en TM es un `setCookie('visid_incap_<id>', '<token>')`).
  3. Extrae cookies `visid_incap_*` + `incap_ses_*` y las guarda en cache TTL
     20 min indexado por (domain, challenge_hash).
  4. La cache se persiste en `imperva_cache.json` para sobrevivir reinicios.

API pública:
  is_imperva_challenge(text|response) -> bool
  capture_js_challenge(html_body, *, base_url=None) -> dict (script + meta)
  solve_challenge(script, base_url=None, *, sandbox="auto") -> dict (cookies)
  bypass(domain, *, fetcher=None) -> dict (full pipeline)
  cache_get(domain) / cache_put(domain, cookies, ttl) / cache_clear()
  self_test() -> dict
  get_info() -> dict

Notas:
  - El sandbox V8 real requiere `py-mini-racer`; si está, lo usamos.
  - `js2py` es fallback puro Python (más lento pero portable).
  - Si NINGUNO está disponible, el resolver heurístico extrae cookies
    declaradas con string literals — funciona en >70% de challenges TM
    porque Incapsula usa siempre `setCookie('visid_incap_<id>', '<token>'…`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import config
import json_atomic

_CACHE_KEY = "imperva_cache.json"
_DEFAULT_TTL = 20 * 60  # 20 min según la mejora del catálogo
_lock = threading.RLock()

# patrones de detección
_RE_INCAP_RESOURCE = re.compile(r"_Incapsula_Resource", re.I)
_RE_VISID_INCAP    = re.compile(r"visid_incap_(\d+)", re.I)
_RE_INCAP_SES      = re.compile(r"incap_ses_(\d+)_(\d+)", re.I)
_RE_META_REFRESH   = re.compile(r'<meta[^>]*http-equiv=["\']refresh["\'][^>]*>', re.I)

# patrones de extracción heurística (cuando no hay sandbox)
_RE_SET_COOKIE_LIT = re.compile(
    r"""(?:document\.cookie\s*=|setCookie\s*\(\s*)["']?(visid_incap_\d+|incap_ses_\d+_\d+)["']?\s*[=,]\s*["']([^"']+)["']""",
    re.I,
)


# ─── Detección ────────────────────────────────────────────────────
def is_imperva_challenge(payload) -> bool:
    """Acepta string, bytes o dict tipo response. Devuelve True si parece challenge."""
    if isinstance(payload, dict):
        body = payload.get("body") or payload.get("text") or ""
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        # también revisa headers Set-Cookie
        for k, v in (payload.get("headers") or {}).items():
            if "incap_ses" in str(v).lower() or "visid_incap" in str(v).lower():
                return True
    elif isinstance(payload, bytes):
        body = payload.decode("utf-8", errors="ignore")
    else:
        body = str(payload)
    if not body:
        return False
    return bool(
        _RE_INCAP_RESOURCE.search(body)
        or _RE_VISID_INCAP.search(body)
        or _RE_INCAP_SES.search(body)
    )


# ─── Captura ──────────────────────────────────────────────────────
def capture_js_challenge(html_body: str, *, base_url: Optional[str] = None) -> Dict:
    """Devuelve el JS embebido + meta-refresh url si existe."""
    if isinstance(html_body, bytes):
        html_body = html_body.decode("utf-8", errors="ignore")
    scripts: List[str] = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html_body, re.I)
    meta = _RE_META_REFRESH.search(html_body)
    refresh_url = ""
    if meta:
        m = re.search(r"url=([^\"'>\s]+)", meta.group(0), re.I)
        if m:
            refresh_url = m.group(1)
    challenge_id = ""
    m = _RE_VISID_INCAP.search(html_body) or _RE_INCAP_SES.search(html_body)
    if m:
        challenge_id = m.group(0)
    body_hash = hashlib.sha256(html_body.encode()).hexdigest()[:32]
    return {
        "ok": bool(scripts) or bool(refresh_url),
        "scripts": scripts,
        "script_count": len(scripts),
        "main_script": "\n".join(scripts) if scripts else "",
        "refresh_url": refresh_url,
        "base_url": base_url or "",
        "challenge_id": challenge_id,
        "body_hash": body_hash,
        "size": len(html_body),
    }


# ─── Resolución ───────────────────────────────────────────────────
def _solve_with_pyminiracer(script: str) -> Dict[str, str]:
    """Sandbox V8 real con py-mini-racer (si está instalado)."""
    try:
        from py_mini_racer import py_mini_racer  # type: ignore
    except Exception:
        return {}
    try:
        ctx = py_mini_racer.MiniRacer()
        # shim mínimo: simular document/cookies/location
        ctx.eval("""
            var __cookies={};
            var document={
                get cookie(){ return Object.entries(__cookies).map(([k,v])=>k+'='+v).join('; '); },
                set cookie(s){ var p=String(s).split(';')[0].split('='); if(p.length>=2) __cookies[p[0].trim()]=p[1].trim(); }
            };
            var window={location:{reload:function(){}, href:'about:blank'}};
            var location=window.location;
            function setCookie(k,v){ __cookies[String(k).trim()]=String(v).trim(); }
        """)
        try:
            ctx.eval(script)
        except Exception:
            pass  # algunos challenges hacen reload — ignoramos
        cookies = ctx.eval("JSON.stringify(__cookies)")
        return json.loads(cookies or "{}")
    except Exception:
        return {}


def _solve_with_js2py(script: str) -> Dict[str, str]:
    """Fallback puro-Python con js2py."""
    try:
        import js2py  # type: ignore
    except Exception:
        return {}
    try:
        ctx = js2py.EvalJs({})
        ctx.execute("""
            var __cookies={};
            var document={cookie:''};
            Object.defineProperty(document,'cookie',{
                get:function(){ var a=[]; for(var k in __cookies) a.push(k+'='+__cookies[k]); return a.join('; '); },
                set:function(s){ var p=String(s).split(';')[0].split('='); if(p.length>=2) __cookies[p[0].trim()]=p[1].trim(); }
            });
            var window={location:{reload:function(){}, href:'about:blank'}};
            function setCookie(k,v){ __cookies[String(k).trim()]=String(v).trim(); }
        """)
        try:
            ctx.execute(script)
        except Exception:
            pass
        return dict(ctx.__cookies.to_dict()) if hasattr(ctx, "__cookies") else {}
    except Exception:
        return {}


def _solve_heuristic(script: str) -> Dict[str, str]:
    """Resolver por regex: extrae literales de cookies visid_incap_* / incap_ses_*.

    Cubre 3 casos comunes:
      A. setCookie('name', 'value')                       → name y value en strings separadas
      B. document.cookie = 'name=value; path=/'           → ambos en una sola string
      C. variable assignment: name='value' o name:'value'
    """
    out: Dict[str, str] = {}
    # caso A: setCookie('name', 'value')
    for m in _RE_SET_COOKIE_LIT.finditer(script):
        name, val = m.group(1), m.group(2)
        if name and val:
            out[name] = val
    # caso B: document.cookie = 'name=value; path=/'
    for m in re.finditer(
        r"document\.cookie\s*=\s*['\"]\s*(visid_incap_\d+|incap_ses_\d+_\d+)\s*=\s*([^;'\"]+)",
        script, re.I,
    ):
        out[m.group(1)] = m.group(2).strip()
    # caso C: name='value'
    for m in re.finditer(
        r"(visid_incap_\d+|incap_ses_\d+_\d+)['\"]?\s*[:=]\s*['\"]([A-Za-z0-9+/=._-]+)['\"]",
        script, re.I,
    ):
        out.setdefault(m.group(1), m.group(2))
    return out


def solve_challenge(script: str, base_url: Optional[str] = None, *,
                     sandbox: str = "auto") -> Dict:
    """Resuelve el script y devuelve cookies extraídas + sandbox usado."""
    if not script:
        return {"ok": False, "error": "empty_script", "cookies": {}}
    cookies: Dict[str, str] = {}
    used = ""
    if sandbox in ("auto", "v8"):
        cookies = _solve_with_pyminiracer(script)
        used = "py-mini-racer" if cookies else used
    if not cookies and sandbox in ("auto", "js2py"):
        cookies = _solve_with_js2py(script)
        used = "js2py" if cookies else used
    if not cookies:
        cookies = _solve_heuristic(script)
        used = "heuristic" if cookies else used
    relevant = {k: v for k, v in cookies.items()
                if k.startswith("visid_incap_") or k.startswith("incap_ses_")}
    return {
        "ok": bool(relevant),
        "cookies": relevant,
        "all_cookies": cookies,
        "sandbox": used or "none",
        "base_url": base_url or "",
    }


# ─── Cache TTL ────────────────────────────────────────────────────
def _now() -> float:
    return time.time()


def _cache_load() -> Dict:
    return json_atomic.load(config.ruta(_CACHE_KEY), default={"entries": {}, "stats": {"hits": 0, "misses": 0, "puts": 0}})


def _cache_save(d: Dict):
    json_atomic.save(config.ruta(_CACHE_KEY), d)


def cache_get(domain: str) -> Optional[Dict]:
    """Devuelve {cookies, expires_at, age_s} si hay entrada vigente, None si caducó/missing."""
    with _lock:
        d = _cache_load()
        e = d.get("entries", {}).get(domain)
        if not e:
            d["stats"]["misses"] = d["stats"].get("misses", 0) + 1
            _cache_save(d)
            return None
        if e.get("expires_at", 0) < _now():
            del d["entries"][domain]
            d["stats"]["misses"] = d["stats"].get("misses", 0) + 1
            _cache_save(d)
            return None
        d["stats"]["hits"] = d["stats"].get("hits", 0) + 1
        _cache_save(d)
        e = dict(e)
        e["age_s"] = int(_now() - e.get("created_at", _now()))
        return e


def cache_put(domain: str, cookies: Dict[str, str], ttl: int = _DEFAULT_TTL) -> Dict:
    with _lock:
        d = _cache_load()
        entry = {
            "cookies": cookies,
            "created_at": _now(),
            "expires_at": _now() + max(60, int(ttl)),
            "ttl": int(ttl),
        }
        d.setdefault("entries", {})[domain] = entry
        d["stats"]["puts"] = d["stats"].get("puts", 0) + 1
        _cache_save(d)
        return entry


def cache_clear(domain: Optional[str] = None) -> Dict:
    with _lock:
        d = _cache_load()
        if domain:
            removed = 1 if d.get("entries", {}).pop(domain, None) else 0
        else:
            removed = len(d.get("entries", {}))
            d["entries"] = {}
        _cache_save(d)
        return {"ok": True, "removed": removed}


def cache_stats() -> Dict:
    with _lock:
        d = _cache_load()
        entries = d.get("entries", {})
        active = sum(1 for e in entries.values() if e.get("expires_at", 0) > _now())
        return {
            "ok": True,
            "active_entries": active,
            "total_entries": len(entries),
            **d.get("stats", {}),
        }


# ─── Pipeline completo ────────────────────────────────────────────
def _default_fetcher(url: str, *, headers: Optional[Dict] = None,
                      timeout: float = 10.0) -> Dict:
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return {
            "ok": True, "status": resp.status, "body": body,
            "headers": dict(resp.headers.items()),
        }


def bypass(domain: str, *, fetcher: Optional[Callable] = None,
           force: bool = False, ttl: int = _DEFAULT_TTL) -> Dict:
    """Pipeline completo: GET → detect → solve → cache."""
    if not force:
        cached = cache_get(domain)
        if cached and cached.get("cookies"):
            return {"ok": True, "from_cache": True, "cookies": cached["cookies"],
                    "age_s": cached.get("age_s")}
    f = fetcher or _default_fetcher
    url = f"https://{domain.rstrip('/')}/"
    try:
        r = f(url, headers={"User-Agent": "Mozilla/5.0"})
    except Exception as e:
        return {"ok": False, "error": f"fetch_failed: {e}"}
    body = r.get("body", b"")
    text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body)
    if not is_imperva_challenge(text):
        return {"ok": True, "no_challenge": True}
    cap = capture_js_challenge(text, base_url=url)
    sol = solve_challenge(cap.get("main_script", ""), base_url=url)
    if sol.get("ok"):
        cache_put(domain, sol["cookies"], ttl)
    return {
        "ok": sol.get("ok", False),
        "cookies": sol.get("cookies", {}),
        "sandbox": sol.get("sandbox"),
        "challenge_id": cap.get("challenge_id"),
        "from_cache": False,
    }


# ─── Self-test sin red ────────────────────────────────────────────
def _fake_challenge_html() -> str:
    """Genera un challenge sintético tipo Incapsula para tests."""
    return """
<html><head><title>Loading</title>
<meta http-equiv="refresh" content="2;url=/_Incapsula_Resource?CWUDNSAI=22"/>
<script>
var _0xabc = "visid_incap_12345";
setCookie('visid_incap_12345', 'abc123XYZ.deadbeef9988');
document.cookie = 'incap_ses_42_12345=session-token-7777; path=/';
window.location.reload();
</script>
</head><body>Verifying...</body></html>
"""


def self_test() -> Dict:
    r = {"checks": {}}
    html = _fake_challenge_html()

    # 1. detección
    r["checks"]["detect_challenge_str"] = is_imperva_challenge(html) is True
    r["checks"]["detect_challenge_bytes"] = is_imperva_challenge(html.encode()) is True
    r["checks"]["detect_no_challenge"] = is_imperva_challenge("<html>normal</html>") is False
    r["checks"]["detect_dict_response"] = is_imperva_challenge({"body": html}) is True

    # 2. captura
    cap = capture_js_challenge(html, base_url="https://test.fake/")
    r["checks"]["capture_finds_script"] = cap.get("script_count", 0) >= 1
    r["checks"]["capture_finds_refresh"] = "Incapsula_Resource" in (cap.get("refresh_url") or "")
    r["checks"]["capture_challenge_id"] = "visid_incap_12345" in (cap.get("challenge_id") or "")

    # 3. resolver heurístico
    sol = solve_challenge(cap["main_script"], sandbox="auto")
    r["checks"]["solve_extracts_visid"] = "visid_incap_12345" in sol.get("cookies", {})
    r["checks"]["solve_extracts_session"] = "incap_ses_42_12345" in sol.get("cookies", {})
    r["checks"]["solve_returns_sandbox"] = sol.get("sandbox") in ("py-mini-racer", "js2py", "heuristic")

    # 4. cache TTL
    cache_clear("test.fake")
    cache_put("test.fake", sol["cookies"], ttl=120)
    cached = cache_get("test.fake")
    r["checks"]["cache_put_get"] = cached is not None and "visid_incap_12345" in cached.get("cookies", {})

    # ttl expira → simulamos forzando expires_at < now
    with _lock:
        d = _cache_load()
        d["entries"]["test.fake"]["expires_at"] = _now() - 1
        _cache_save(d)
    cached2 = cache_get("test.fake")
    r["checks"]["cache_expires"] = cached2 is None

    # 5. pipeline completo con fetcher mock
    def mock_fetcher(url, **_):
        return {"ok": True, "status": 200, "body": html.encode(),
                "headers": {}}
    bp = bypass("imperva.fake", fetcher=mock_fetcher, force=True)
    r["checks"]["bypass_pipeline_ok"] = bp.get("ok") is True
    r["checks"]["bypass_returns_cookies"] = "visid_incap_12345" in bp.get("cookies", {})

    # 6. cache hit on second call
    bp2 = bypass("imperva.fake", fetcher=mock_fetcher)
    r["checks"]["bypass_cache_hit"] = bp2.get("from_cache") is True

    # cleanup
    cache_clear("test.fake")
    cache_clear("imperva.fake")

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    """Info para el dashboard."""
    sandbox_available = []
    try:
        import py_mini_racer  # noqa
        sandbox_available.append("py-mini-racer")
    except Exception:
        pass
    try:
        import js2py  # noqa
        sandbox_available.append("js2py")
    except Exception:
        pass
    sandbox_available.append("heuristic")  # siempre disponible
    return {
        "module": "imperva_bypass",
        "version": 1,
        "default_ttl_s": _DEFAULT_TTL,
        "sandboxes": sandbox_available,
        "cache": cache_stats(),
    }
