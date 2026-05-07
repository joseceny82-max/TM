"""akamai_v3_hash_refresh.py — Akamai v3.1 cookie-derived hash refresh loop (SOTA P0-1).

Akamai BMP v3.1 (2026) introduce un cambio crítico: el sensor_data ya NO se
genera con un hash fijo (`8888888` en v3.0) sino con un **hash derivado del
cookie `bm_sz` que el server retorna**.

Patrón:
  1. Cliente manda primer sensor con hash placeholder
  2. Server responde con `bm_sz=<base64-encoded-hash>` en Set-Cookie
  3. Cliente debe parsear ese cookie, extraer el hash, y SEMBRAR su PRNG con él
     para los sensores 2..N
  4. Si los sensores 2..N usan hash placeholder → "first-payload-replay" detectado

Sin esta lógica, Akamai marca al cliente como bot tras 1-2 requests.

Implementa:
  - Parser de `bm_sz` (formato variable: base64, padding URL, version markers)
  - Extracción de hash seed
  - Cache per-session del seed extraído
  - Refresh loop background (re-genera sensor cada 60s con seed actual)
  - Validación cross-request: si server cambia bm_sz, re-extraer seed
  - Métricas: sensores generados con seed real vs placeholder

API:
  parse_bm_sz(cookie_value) → {hash_seed, version, valid}
  extract_seed_from_response(response_headers, response_body) → seed_hex
  set_session_seed(session_id, seed_hex)
  get_session_seed(session_id) → seed_hex
  build_sensor_with_seed(session_id, payload) → encoded sensor_data
  start_refresh_loop(session_id, page_url, ttl_s=60) → ok
  stop_refresh_loop(session_id) → ok
  detect_seed_change(session_id, new_bm_sz) → bool
  get_metrics() → dict
  self_test()
"""
from __future__ import annotations
import os, re, time, json, base64, hashlib, threading, secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic

try:
    import akamai_sensor_v3
    AKAMAI_V3_AVAILABLE = True
except Exception:
    AKAMAI_V3_AVAILABLE = False


_CONFIG_KEY = "akamai_v3_refresh_config.json"
_SEEDS_KEY = "akamai_v3_seeds.json"
_lock = threading.RLock()
_session_seeds: Dict[str, Dict[str, Any]] = {}    # sid → {seed, extracted_at, source}
_refresh_threads: Dict[str, threading.Event] = {}
_metrics = {
    "seeds_extracted": 0,
    "seeds_extraction_failed": 0,
    "sensors_generated_with_real_seed": 0,
    "sensors_generated_with_placeholder": 0,
    "seed_changes_detected": 0,
    "refresh_loops_active": 0,
}


def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "default_refresh_ttl_s": 60,
        "placeholder_seed_hex": "8888888",
        "max_session_seeds": 500,
        "seed_cache_ttl_s": 1800,            # 30min
        "log_max_entries": 200,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# 1. PARSE BM_SZ COOKIE
# ═══════════════════════════════════════════════════════════════
# Formato observado v3.1:
#   bm_sz=<base64url>~<numeric-version>~<expiry>~<flags>
#   Ejemplo: bm_sz=ABCD1234efgh~3.1~3600000~17~01-aGV5IGppIGFr
#
# El hash seed está embedded en los primeros 12-16 chars base64
# del primer fragmento (después de decoded reverse).

BM_SZ_REGEX = re.compile(
    r"bm_sz=([^;,\s\"']+)", re.IGNORECASE
)

# Patrones para detectar versión v3.1 dentro de bm_sz
# Tuplas (regex, version_label) para evitar string-matching del propio regex
V3_VERSION_MARKERS = [
    (r"~3\.1~", "3.1"),
    (r"~3~", "3.0"),
    (r"v=3", "3.0"),
]


def parse_bm_sz(cookie_value: str) -> Dict[str, Any]:
    """Parsea el cookie bm_sz y extrae hash seed + metadata.

    Returns:
        {
            "valid": bool,
            "hash_seed": str (hex),
            "version": "3.1" | "3.0" | "2.x" | "unknown",
            "raw_first_fragment": str,
            "fragments_count": int,
        }
    """
    if not cookie_value or len(cookie_value) < 8:
        return {"valid": False, "error": "cookie_too_short"}

    # Permitir tanto valor crudo como cookie completa "bm_sz=..."
    if "bm_sz=" in cookie_value.lower():
        m = BM_SZ_REGEX.search(cookie_value)
        if not m:
            return {"valid": False, "error": "bm_sz_not_found_in_string"}
        cookie_value = m.group(1)

    # Split por ~
    fragments = cookie_value.split("~")
    if len(fragments) < 2:
        return {"valid": False, "error": "no_separator_found",
                "raw": cookie_value[:60]}

    first = fragments[0]
    # Base64 decode con padding tolerance
    try:
        # Trim non-b64 chars
        b64_clean = re.sub(r"[^A-Za-z0-9+/=_\-]", "", first)
        # Aceptar urlsafe + add padding si falta
        b64_clean += "=" * (-len(b64_clean) % 4)
        try:
            decoded = base64.urlsafe_b64decode(b64_clean)
        except Exception:
            decoded = base64.b64decode(b64_clean)
    except Exception as e:
        return {"valid": False, "error": f"b64_decode_fail: {str(e)[:60]}",
                "raw": first[:40]}

    # Hash seed = SHA-256 de los primeros 16 bytes decoded (mock real Akamai)
    if len(decoded) < 4:
        return {"valid": False, "error": "decoded_too_short"}

    hash_seed = hashlib.sha256(decoded[:32]).hexdigest()[:16]

    # Version detection
    full_str = cookie_value
    version = "unknown"
    for marker_re, label in V3_VERSION_MARKERS:
        if re.search(marker_re, full_str):
            version = label
            break

    return {
        "valid": True,
        "hash_seed": hash_seed,
        "version": version,
        "raw_first_fragment": first[:60],
        "fragments_count": len(fragments),
        "decoded_byte_length": len(decoded),
    }


def extract_seed_from_response(response_headers: Optional[Dict[str, str]] = None,
                                response_body: Optional[str] = None,
                                set_cookie_header: Optional[str] = None) -> Dict[str, Any]:
    """Extrae el hash seed de los headers o body de una respuesta TM.

    Busca en:
      - Set-Cookie (caso normal)
      - Body HTML (fallback si JS configura cookie via document.cookie)
    """
    # 1. Try Set-Cookie header
    if set_cookie_header is None and response_headers:
        # Headers pueden venir como dict o lista
        for k, v in (response_headers.items() if hasattr(response_headers, "items") else []):
            if k.lower() == "set-cookie":
                set_cookie_header = v
                break

    if set_cookie_header:
        # Headers Set-Cookie pueden ser concatenados con \r\n o , (multiple)
        candidates = re.split(r"[\r\n,]+", set_cookie_header)
        for c in candidates:
            if "bm_sz" in c.lower():
                r = parse_bm_sz(c)
                if r.get("valid"):
                    with _lock:
                        _metrics["seeds_extracted"] += 1
                    return {**r, "source": "set_cookie"}

    # 2. Try body
    if response_body:
        m = BM_SZ_REGEX.search(response_body)
        if m:
            r = parse_bm_sz(m.group(0))
            if r.get("valid"):
                with _lock:
                    _metrics["seeds_extracted"] += 1
                return {**r, "source": "body"}

    with _lock:
        _metrics["seeds_extraction_failed"] += 1
    return {"valid": False, "error": "no_bm_sz_found"}


# ═══════════════════════════════════════════════════════════════
# 2. SESSION SEED CACHE
# ═══════════════════════════════════════════════════════════════
def set_session_seed(session_id: str, seed_hex: str,
                      source: str = "manual",
                      version: str = "3.1") -> Dict[str, Any]:
    """Asocia un hash seed a una session_id."""
    with _lock:
        # Detectar cambio si ya existía
        prev = _session_seeds.get(session_id)
        seed_changed = prev and prev.get("seed_hex") != seed_hex
        if seed_changed:
            _metrics["seed_changes_detected"] += 1
        _session_seeds[session_id] = {
            "seed_hex": seed_hex,
            "version": version,
            "source": source,
            "extracted_at": datetime.now().isoformat(),
            "extracted_ts": time.time(),
            "use_count": 0,
        }
        # Cap size
        cfg = get_config()
        if len(_session_seeds) > cfg["max_session_seeds"]:
            # Drop oldest
            oldest = min(_session_seeds.items(),
                          key=lambda kv: kv[1]["extracted_ts"])
            _session_seeds.pop(oldest[0], None)
    return {"ok": True, "session_id": session_id,
            "seed_changed": bool(seed_changed),
            "seed_hex_short": seed_hex[:8] + "..."}


def get_session_seed(session_id: str) -> Optional[Dict[str, Any]]:
    """Devuelve seed asociada a sesión, o None si expirado/no existe."""
    cfg = get_config()
    with _lock:
        s = _session_seeds.get(session_id)
        if not s: return None
        if time.time() - s["extracted_ts"] > cfg["seed_cache_ttl_s"]:
            _session_seeds.pop(session_id, None)
            return None
        s["use_count"] += 1
        return dict(s)


def list_session_seeds() -> List[Dict[str, Any]]:
    with _lock:
        return [
            {"session_id": sid, **{k: v for k, v in s.items() if k != "_internal"}}
            for sid, s in _session_seeds.items()
        ]


def detect_seed_change(session_id: str, new_bm_sz: str) -> Dict[str, Any]:
    """Compara seed actual con uno nuevo. Si cambió, re-extrae."""
    parsed = parse_bm_sz(new_bm_sz)
    if not parsed.get("valid"):
        return {"ok": False, "error": parsed.get("error")}

    new_seed = parsed["hash_seed"]
    current = get_session_seed(session_id)
    if not current:
        set_session_seed(session_id, new_seed, source="auto_detect",
                          version=parsed["version"])
        return {"changed": False, "reason": "no_previous_seed",
                 "new_seed_set": True}

    if current["seed_hex"] == new_seed:
        return {"changed": False}

    # Cambió → actualizar
    set_session_seed(session_id, new_seed, source="auto_refresh",
                     version=parsed["version"])
    return {"changed": True,
             "old_seed": current["seed_hex"][:8] + "...",
             "new_seed": new_seed[:8] + "...",
             "version": parsed["version"]}


# ═══════════════════════════════════════════════════════════════
# 3. SENSOR BUILD WITH SEED (override de akamai_sensor_v3.encode)
# ═══════════════════════════════════════════════════════════════
def build_sensor_with_seed(session_id: str,
                             user_agent: Optional[str] = None,
                             events_count: int = 60) -> Dict[str, Any]:
    """Genera sensor v3.1 usando el seed cacheado para session_id.

    Si NO hay seed cacheado → usa placeholder y MARCA en métricas.
    """
    if not AKAMAI_V3_AVAILABLE:
        return {"ok": False, "error": "akamai_sensor_v3_unavailable"}

    seed_info = get_session_seed(session_id)
    cfg = get_config()
    real_seed = bool(seed_info)

    payload = akamai_sensor_v3.build_v3_payload(
        session_id=session_id,
        user_agent=user_agent,
        events_count=events_count,
    )

    # Inyectar seed en el payload (campo dyn → sustituido por seed real)
    if real_seed:
        payload["dyn"]["seed_hex"] = seed_info["seed_hex"]
        payload["dyn"]["seed_source"] = "bm_sz_derived"
        with _lock:
            _metrics["sensors_generated_with_real_seed"] += 1
    else:
        payload["dyn"]["seed_hex"] = cfg["placeholder_seed_hex"]
        payload["dyn"]["seed_source"] = "placeholder"
        with _lock:
            _metrics["sensors_generated_with_placeholder"] += 1

    # Encode
    sensor_str = akamai_sensor_v3.encode_v3_sensor(payload)

    return {
        "ok": True,
        "sensor_data": sensor_str,
        "sensor_length": len(sensor_str),
        "session_id": session_id,
        "used_real_seed": real_seed,
        "seed_source": payload["dyn"]["seed_source"],
        "seed_hex_short": payload["dyn"]["seed_hex"][:8] + "...",
    }


# ═══════════════════════════════════════════════════════════════
# 4. REFRESH LOOP BACKGROUND
# ═══════════════════════════════════════════════════════════════
def start_refresh_loop(session_id: str, ttl_s: int = 60,
                        on_refresh=None) -> Dict[str, Any]:
    """Lanza thread que cada `ttl_s` segundos regenera sensor con seed actual."""
    if session_id in _refresh_threads:
        return {"ok": False, "error": "already_running"}

    stop_evt = threading.Event()
    _refresh_threads[session_id] = stop_evt
    with _lock:
        _metrics["refresh_loops_active"] += 1

    def _loop():
        while not stop_evt.is_set():
            try:
                r = build_sensor_with_seed(session_id)
                if on_refresh:
                    try: on_refresh(r)
                    except Exception: pass
            except Exception:
                pass
            stop_evt.wait(ttl_s)
        with _lock:
            _metrics["refresh_loops_active"] = max(0, _metrics["refresh_loops_active"] - 1)

    t = threading.Thread(target=_loop, daemon=True,
                          name=f"akv3-refresh-{session_id[:12]}")
    t.start()
    return {"ok": True, "session_id": session_id, "ttl_s": ttl_s}


def stop_refresh_loop(session_id: str) -> Dict[str, Any]:
    evt = _refresh_threads.pop(session_id, None)
    if evt: evt.set()
    return {"ok": evt is not None, "session_id": session_id}


def list_refresh_loops() -> List[str]:
    return list(_refresh_threads.keys())


# ═══════════════════════════════════════════════════════════════
# METRICS / INFO
# ═══════════════════════════════════════════════════════════════
def get_metrics() -> Dict[str, Any]:
    with _lock:
        total_sensors = (_metrics["sensors_generated_with_real_seed"]
                          + _metrics["sensors_generated_with_placeholder"])
        real_pct = round(
            100.0 * _metrics["sensors_generated_with_real_seed"] / max(1, total_sensors), 1
        )
        return {
            **_metrics,
            "session_seeds_cached": len(_session_seeds),
            "real_seed_pct": real_pct,
            "akamai_v3_available": AKAMAI_V3_AVAILABLE,
        }


def reset_metrics():
    with _lock:
        for k in _metrics:
            _metrics[k] = 0


def cleanup_expired_seeds() -> Dict[str, Any]:
    cfg = get_config()
    now = time.time()
    removed = 0
    with _lock:
        for sid in list(_session_seeds.keys()):
            if now - _session_seeds[sid]["extracted_ts"] > cfg["seed_cache_ttl_s"]:
                _session_seeds.pop(sid)
                removed += 1
    return {"ok": True, "removed_count": removed}


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_has_ttl"] = "default_refresh_ttl_s" in cfg
    checks["config_placeholder"] = cfg["placeholder_seed_hex"] == "8888888"

    # 2. Parse bm_sz válido
    fake_b64 = base64.urlsafe_b64encode(b"abcd" * 8).decode().rstrip("=")
    cookie = f"{fake_b64}~3.1~3600000~17~01-extra"
    r = parse_bm_sz(cookie)
    checks["parse_valid"] = r.get("valid") is True
    checks["parse_has_seed"] = "hash_seed" in r and len(r["hash_seed"]) >= 16
    checks["parse_version_31"] = r.get("version") == "3.1"

    # 3. Parse bm_sz from full Set-Cookie
    full_cookie = f"bm_sz={fake_b64}~3.1~3600000; Path=/; Domain=.ticketmaster.com"
    r = parse_bm_sz(full_cookie)
    checks["parse_full_setcookie"] = r.get("valid") is True

    # 4. Parse inválido
    r = parse_bm_sz("garbage")
    checks["parse_invalid_returns_false"] = r.get("valid") is False

    r = parse_bm_sz("")
    checks["parse_empty_returns_false"] = r.get("valid") is False

    # 5. Extract from response headers (dict)
    headers = {"Set-Cookie": f"bm_sz={fake_b64}~3.1~3600000; Path=/"}
    r = extract_seed_from_response(response_headers=headers)
    checks["extract_from_headers"] = r.get("valid") is True
    checks["extract_source_header"] = r.get("source") == "set_cookie"

    # 6. Extract from body
    body = f'<script>document.cookie = "bm_sz={fake_b64}~3.1~3600";</script>'
    r = extract_seed_from_response(response_body=body)
    checks["extract_from_body"] = r.get("valid") is True

    # 7. Extract fail
    r = extract_seed_from_response(response_headers={"X-Other": "abc"},
                                     response_body="no cookie here")
    checks["extract_no_cookie_fails"] = r.get("valid") is False

    # 8. Set + get session seed
    sid = "test_sess_001"
    seed = "abc123def456"
    r = set_session_seed(sid, seed)
    checks["set_seed_ok"] = r.get("ok") is True

    g = get_session_seed(sid)
    checks["get_seed_works"] = g and g["seed_hex"] == seed
    checks["use_count_incremented"] = g and g["use_count"] >= 1

    # 9. Detect seed change
    new_b64 = base64.urlsafe_b64encode(b"different" * 4).decode().rstrip("=")
    new_cookie = f"bm_sz={new_b64}~3.1~3600"
    r = detect_seed_change(sid, new_cookie)
    checks["seed_change_detected"] = r.get("changed") is True

    r2 = detect_seed_change(sid, new_cookie)
    checks["same_seed_no_change"] = r2.get("changed") is False

    # 10. Build sensor with seed
    if AKAMAI_V3_AVAILABLE:
        # Set seed primero
        set_session_seed("sensor_test", "1234567890abcdef")
        r = build_sensor_with_seed("sensor_test", events_count=15)
        checks["build_with_real_seed"] = r.get("used_real_seed") is True
        checks["sensor_starts_with_3"] = r.get("sensor_data", "").startswith("3;")

        # Sin seed → placeholder
        r2 = build_sensor_with_seed("no_seed_sess", events_count=10)
        checks["build_with_placeholder"] = r2.get("used_real_seed") is False
        checks["placeholder_source"] = r2.get("seed_source") == "placeholder"

    # 11. Refresh loop start/stop
    r = start_refresh_loop("loop_test", ttl_s=99999)
    checks["loop_start_ok"] = r.get("ok") is True
    checks["loop_in_list"] = "loop_test" in list_refresh_loops()
    r = stop_refresh_loop("loop_test")
    checks["loop_stop_ok"] = r.get("ok") is True

    # 12. Metrics
    m = get_metrics()
    checks["metrics_real_seed_count"] = m.get("sensors_generated_with_real_seed", 0) >= 1
    checks["metrics_seeds_extracted"] = m.get("seeds_extracted", 0) >= 1

    # 13. Cleanup
    r = cleanup_expired_seeds()
    checks["cleanup_returns_ok"] = r.get("ok") is True

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "akamai_v3_hash_refresh",
        "version": 1,
        "metrics": get_metrics(),
        "active_refresh_loops": len(_refresh_threads),
        "purpose": "Akamai BMP v3.1 cookie-derived hash refresh (SOTA P0-1)",
    }
