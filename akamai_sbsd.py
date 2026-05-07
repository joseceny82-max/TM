"""akamai_sbsd.py — Akamai SBSD (Sentinel Bot Signal Data) payload generator.

Akamai Bot Manager v3 usa cookies `sbsd_o` y `bm_so` derivadas de un payload
SBSD que el cliente JS construye con datos del navegador y un hash dinámico
extraído del propio script `_sbsd`. Sin payload válido, los endpoints de alto
riesgo (checkout, login) responden 403/blocked en TM.

Pipeline implementado:
  1. fetch_sbsd_script()       → descarga main script y extrae fileHash
  2. build_sbsd_payload()      → ensambla {ua, uuid, pageUrl, oCookie, ip,
                                   acceptLanguage, index, ts}
  3. encrypt_sbsd_payload()    → XOR con PRNG sembrado por (cookieHash|fileHash)
  4. submit_sbsd_payload()     → POST a /_sbsd, retorna cookie sbsd_o
  5. SBSDSession.refresh()     → re-genera token cada `ttl_seconds` (def. 120s)

API pública:
  fetch_sbsd_script(domain, *, fetcher=None) -> dict
  build_sbsd_payload(ctx) -> dict
  encrypt_sbsd_payload(payload, cookie_hash, file_hash) -> str
  submit_sbsd_payload(domain, encrypted, *, fetcher=None) -> dict
  SBSDSession(domain, ctx).start() / .refresh() / .cookie()
  self_test() -> dict
  get_info() -> dict

Notas:
  - El "hash dinámico" se aproxima con SHA256(script_body) trimmed; la imple-
    mentación real puede requerir ejecutar el VM del script para extraer el
    valor exacto, pero este shim permite probar la pipeline end-to-end.
  - El fetcher se inyecta para tests deterministas. Por defecto usa
    `urllib.request` con TLS standard.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, Dict, Optional

import config
import json_atomic

_LOG_KEY = "akamai_sbsd_log.json"
_DEFAULT_TTL = 120  # 2 min, alineado con la tarjeta-mejora
_lock = threading.RLock()


# ─── Fetch / parse del script SBSD ────────────────────────────────
def _default_fetcher(url: str, *, headers: Optional[Dict] = None,
                      data: Optional[bytes] = None,
                      method: str = "GET", timeout: float = 10.0) -> Dict:
    import urllib.request
    req = urllib.request.Request(url, data=data, method=method,
                                  headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return {
            "ok": True,
            "status": resp.status,
            "body": body,
            "headers": dict(resp.headers.items()),
        }


def fetch_sbsd_script(domain: str, *, fetcher: Optional[Callable] = None,
                      script_path: str = "/_sbsd/main.js") -> Dict:
    """Descarga el script principal SBSD y extrae file_hash + version."""
    f = fetcher or _default_fetcher
    url = f"https://{domain.rstrip('/')}{script_path}"
    try:
        r = f(url, headers={"User-Agent": "Mozilla/5.0",
                             "Accept": "*/*"})
    except Exception as e:
        return {"ok": False, "error": f"fetch_failed: {e}"}
    body = r.get("body", b"")
    if not body:
        return {"ok": False, "error": "empty_body"}
    text = body if isinstance(body, str) else body.decode("utf-8", errors="ignore")
    file_hash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    # heurística: busca un literal de versión tipo `"v":"3.x.y"` o `version=3.x`
    m = re.search(r"version[\"']?\s*[:=]\s*[\"']([\d.]+)[\"']", text)
    version = m.group(1) if m else "unknown"
    return {
        "ok": True,
        "url": url,
        "file_hash": file_hash,
        "version": version,
        "size": len(text),
    }


# ─── Payload construction ─────────────────────────────────────────
def build_sbsd_payload(ctx: Dict) -> Dict:
    """Ensambla el payload SBSD con los campos canónicos."""
    return {
        "ua": ctx.get("user_agent", "Mozilla/5.0"),
        "uuid": ctx.get("uuid") or uuid.uuid4().hex,
        "pageUrl": ctx.get("page_url", ""),
        "oCookie": ctx.get("o_cookie", ""),
        "ip": ctx.get("ip", ""),
        "acceptLanguage": ctx.get("accept_language", "en-US"),
        "index": int(ctx.get("index", 0)),
        "ts": int(ctx.get("ts") or time.time() * 1000),
    }


# ─── Encrypt / decrypt con PRNG sembrado ──────────────────────────
def _seeded_keystream(seed: bytes, length: int) -> bytes:
    """PRNG simple basado en SHA256 encadenado. Determinista por seed."""
    out = bytearray()
    block = seed
    while len(out) < length:
        block = hashlib.sha256(block).digest()
        out.extend(block)
    return bytes(out[:length])


def encrypt_sbsd_payload(payload: Dict, cookie_hash: str, file_hash: str) -> str:
    """Serializa payload y aplica XOR con keystream sembrado por
    SHA256(cookie_hash | file_hash). Devuelve hex.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    seed = hashlib.sha256((cookie_hash + "|" + file_hash).encode()).digest()
    ks = _seeded_keystream(seed, len(raw))
    enc = bytes(b ^ k for b, k in zip(raw, ks))
    return enc.hex()


def decrypt_sbsd_payload(hex_str: str, cookie_hash: str, file_hash: str) -> Dict:
    """Inverso de encrypt_sbsd_payload — usado por self_test."""
    enc = bytes.fromhex(hex_str)
    seed = hashlib.sha256((cookie_hash + "|" + file_hash).encode()).digest()
    ks = _seeded_keystream(seed, len(enc))
    raw = bytes(b ^ k for b, k in zip(enc, ks))
    return json.loads(raw.decode("utf-8", errors="replace"))


# ─── Submit / cookie extraction ──────────────────────────────────
def submit_sbsd_payload(domain: str, encrypted: str, *,
                         fetcher: Optional[Callable] = None,
                         endpoint: str = "/_sbsd") -> Dict:
    """POST encrypted hex al endpoint SBSD; extrae cookie sbsd_o de Set-Cookie."""
    f = fetcher or _default_fetcher
    url = f"https://{domain.rstrip('/')}{endpoint}"
    try:
        r = f(url, method="POST", data=encrypted.encode(),
              headers={"Content-Type": "application/octet-stream",
                        "User-Agent": "Mozilla/5.0"})
    except Exception as e:
        return {"ok": False, "error": f"submit_failed: {e}"}
    set_cookie = ""
    headers = r.get("headers") or {}
    for k, v in headers.items():
        if k.lower() == "set-cookie":
            set_cookie = v
            break
    sbsd_o = ""
    m = re.search(r"sbsd_o=([^;]+)", set_cookie or "")
    if m:
        sbsd_o = m.group(1)
    bm_so = ""
    m2 = re.search(r"bm_so=([^;]+)", set_cookie or "")
    if m2:
        bm_so = m2.group(1)
    return {"ok": bool(sbsd_o or bm_so),
            "status": r.get("status"),
            "sbsd_o": sbsd_o, "bm_so": bm_so,
            "set_cookie": set_cookie}


# ─── Sesión con auto-refresh ──────────────────────────────────────
class SBSDSession:
    """Mantiene una sesión SBSD con refresh automático del token cada `ttl`s."""

    def __init__(self, domain: str, ctx: Dict, *,
                 ttl_seconds: int = _DEFAULT_TTL,
                 fetcher: Optional[Callable] = None):
        self.domain = domain
        self.ctx = dict(ctx)
        self.ttl = max(30, int(ttl_seconds))
        self.fetcher = fetcher
        self._cookie_o: str = ""
        self._cookie_so: str = ""
        self._last_refresh: float = 0.0
        self._file_hash: str = ""
        self._lock = threading.Lock()

    def start(self) -> Dict:
        return self.refresh()

    def cookie(self) -> Dict:
        return {"sbsd_o": self._cookie_o, "bm_so": self._cookie_so,
                "age_s": int(time.time() - self._last_refresh) if self._last_refresh else None}

    def is_fresh(self) -> bool:
        return self._last_refresh and (time.time() - self._last_refresh < self.ttl)

    def refresh(self) -> Dict:
        with self._lock:
            scr = fetch_sbsd_script(self.domain, fetcher=self.fetcher)
            if not scr.get("ok"):
                return scr
            self._file_hash = scr["file_hash"]
            payload = build_sbsd_payload(self.ctx)
            cookie_hash = hashlib.sha256(
                (self.ctx.get("o_cookie", "") + str(payload["index"])).encode()
            ).hexdigest()
            enc = encrypt_sbsd_payload(payload, cookie_hash, self._file_hash)
            res = submit_sbsd_payload(self.domain, enc, fetcher=self.fetcher)
            if res.get("ok"):
                self._cookie_o = res.get("sbsd_o", "") or self._cookie_o
                self._cookie_so = res.get("bm_so", "") or self._cookie_so
                self._last_refresh = time.time()
                _log_event({
                    "ts": datetime.now().isoformat(),
                    "domain": self.domain,
                    "version": scr.get("version"),
                    "file_hash": self._file_hash[:16],
                    "ok": True,
                })
            return {**res, "file_hash": self._file_hash, "version": scr.get("version")}


# ─── Logging ──────────────────────────────────────────────────────
def _log_event(entry: Dict, max_keep: int = 200):
    with _lock:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"events": []})
        d.setdefault("events", []).append(entry)
        d["events"] = d["events"][-max_keep:]
        json_atomic.save(config.ruta(_LOG_KEY), d)


def get_log(limit: int = 30) -> Dict:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"events": []})
    items = d.get("events", [])[-limit:][::-1]
    return {"ok": True, "events": items}


# ─── Self-test sin red (fetcher mock) ─────────────────────────────
def _mock_fetcher_factory():
    """Devuelve fetcher determinista que simula un Akamai SBSD endpoint."""
    state = {"hits": 0}

    def f(url: str, *, headers=None, data=None, method="GET", timeout=10.0):
        state["hits"] += 1
        if method == "GET":
            body = b"// fake sbsd script v3.1.4 version='3.1.4' var x=1;"
            return {"ok": True, "status": 200, "body": body, "headers": {}}
        if method == "POST":
            tok = hashlib.sha1(data or b"").hexdigest()[:24]
            return {"ok": True, "status": 200, "body": b"{}",
                    "headers": {"Set-Cookie": f"sbsd_o={tok}; Path=/; Secure; bm_so={tok[::-1]}"}}
        return {"ok": False, "status": 405, "body": b""}
    return f, state


def self_test() -> Dict:
    r = {"checks": {}}
    fetcher, state = _mock_fetcher_factory()

    # 1. fetch_sbsd_script
    scr = fetch_sbsd_script("tm-fake.test", fetcher=fetcher)
    r["checks"]["fetch_ok"] = scr.get("ok") is True
    r["checks"]["fetch_extracts_version"] = scr.get("version") == "3.1.4"
    r["checks"]["fetch_returns_hash"] = bool(scr.get("file_hash"))

    # 2. build_payload
    p = build_sbsd_payload({"user_agent": "UA1", "page_url": "/x", "ip": "1.2.3.4",
                              "o_cookie": "abcd", "index": 7, "ts": 1234567})
    r["checks"]["payload_complete"] = all(k in p for k in
        ("ua", "uuid", "pageUrl", "oCookie", "ip", "acceptLanguage", "index", "ts"))
    r["checks"]["payload_index_int"] = p["index"] == 7

    # 3. encrypt + decrypt roundtrip
    enc = encrypt_sbsd_payload(p, "cookie_h", scr["file_hash"])
    dec = decrypt_sbsd_payload(enc, "cookie_h", scr["file_hash"])
    r["checks"]["encrypt_decrypt_roundtrip"] = dec.get("ip") == "1.2.3.4"
    r["checks"]["encrypt_is_hex"] = all(c in "0123456789abcdef" for c in enc)

    # 4. submit
    sub = submit_sbsd_payload("tm-fake.test", enc, fetcher=fetcher)
    r["checks"]["submit_ok"] = sub.get("ok") is True
    r["checks"]["submit_extracts_sbsd_o"] = bool(sub.get("sbsd_o"))
    r["checks"]["submit_extracts_bm_so"] = bool(sub.get("bm_so"))

    # 5. SBSDSession + refresh + ttl
    s = SBSDSession("tm-fake.test", {"o_cookie": "abc", "user_agent": "UA"},
                     ttl_seconds=60, fetcher=fetcher)
    s.start()
    r["checks"]["session_starts"] = bool(s.cookie().get("sbsd_o"))
    r["checks"]["session_is_fresh"] = s.is_fresh() is True

    # 6. log
    log = get_log(5)
    r["checks"]["log_records_event"] = len(log.get("events", [])) >= 1

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    log = get_log(1).get("events", [])
    return {
        "module": "akamai_sbsd",
        "version": 1,
        "default_ttl_s": _DEFAULT_TTL,
        "last_event": log[0] if log else None,
    }
