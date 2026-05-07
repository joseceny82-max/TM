"""ja4_spoof.py — JA4+ TLS fingerprint spoofing usando tls-client.

JA3 quedó obsoleto en 2023 cuando los navegadores empezaron a randomizar el
orden de extensiones TLS. JA4+ (Cloudflare/AWS/VirusTotal estándar 2024+)
usa orden alfabético y es resistente. tls-client imita client hello nativo
de Chrome/Firefox/Safari con hasta 30+ perfiles.

Este módulo:
- `get_session(profile)`    → tls_client.Session con perfil concreto
- `pick_profile()`          → rotación simple (random)
- `pick_weighted_profile()` → rotación ponderada por success rate
- `test_handshake()`        → hace request real a tls.peet.ws y extrae JA4
- `verify_against_ja4db()`  → compara el JA4 detectado vs base de datos
                              de JA4 conocidos de Chrome/Firefox/Safari
- `benchmark_profiles()`    → testa todos los perfiles en paralelo y reporta
                              success rate + latencia + fingerprint per perfil
- `get_profile_stats()`     → contadores de uso + éxitos/fallos per perfil
- Historial persistente con stats per-profile para rotación inteligente
"""
import random
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict
import config
import json_atomic


# Perfiles válidos en tls-client 1.0.1+ (representativos y modernos)
PROFILES = [
    "chrome_133", "chrome_131", "chrome_124", "chrome_120",
    "firefox_123", "firefox_120",
    "safari_ios_17_0", "safari_17_0", "safari_ipad_15_6",
    "okhttp4_android_13",
    "opera_91", "opera_90",
]

DEFAULT_PROFILE = "chrome_133"
TEST_LOG_KEY = "ja4_test_log.json"
STATS_KEY    = "ja4_profile_stats.json"

# Base de datos de JA4 fingerprints conocidos 2025/2026 (ja4db.com public).
# Los prefijos deben empezar con "t13d" (TLS 1.3) para Chrome/Firefox/Safari modernos.
# Formato JA4: {version}{sni}{alpn}_{ciphers_hash}_{ext_hash}
# Fuente: https://github.com/FoxIO-LLC/ja4 + ja4db.com
KNOWN_JA4 = {
    # Chrome 120-133 (Windows/Mac/Linux)
    "chrome_stable": [
        "t13d1516h2_8daaf6152771_b0da82dd1658",  # Chrome 131 Windows
        "t13d1517h2_8daaf6152771_b0da82dd1658",  # Chrome 133 Windows/Mac
        "t13d1516h2_8daaf6152771_02713d6af862",  # Chrome 124 variant
        "t13d1515h2_8daaf6152771_b1ff8ab2d16f",  # Chrome 120
    ],
    # Firefox 120-124
    "firefox_stable": [
        "t13d1716h2_5b57614c22b0_93c746dc12af",
        "t13d1715h2_5b57614c22b0_3d5424432f57",
    ],
    # Safari 17 (macOS + iOS)
    "safari_stable": [
        "t13d2014h2_a09f3c656075_14788d8d241b",
        "t13d1617h2_9a8ba6e3f63b_a7c1f0b00e13",
    ],
    # Android OkHTTP
    "android_okhttp": [
        "t13d1311h2_4a5e6b28b22e_3a4d63f4fe0c",
    ],
}

_lock = threading.Lock()


def _path_log():
    return config.ruta(TEST_LOG_KEY)


def available_profiles() -> List[str]:
    return list(PROFILES)


def pick_profile(prefer: Optional[str] = None) -> str:
    """Elige un perfil. Si prefer se indica y es válido, se usa. Si no, aleatorio."""
    if prefer and prefer in PROFILES:
        return prefer
    return random.choice(PROFILES)


def get_session(profile: Optional[str] = None):
    """Devuelve una tls_client.Session con el perfil dado.

    Levanta ImportError si tls-client no está instalado (la UI debe avisar).
    """
    import tls_client  # lazy
    prof = pick_profile(profile)
    session = tls_client.Session(
        client_identifier=prof,
        random_tls_extension_order=True,
    )
    session._ja4_profile = prof  # marcado para diagnóstico
    return session


def test_handshake(profile: Optional[str] = None, url: str = "https://tls.peet.ws/api/all") -> dict:
    """Test real de handshake.

    tls.peet.ws devuelve JA3+JA4+peet fingerprints detectados del cliente.
    Si el endpoint falla, prueba httpbin como fallback.
    """
    started = datetime.now()
    prof = pick_profile(profile)
    result = {
        "profile": prof,
        "url": url,
        "started_at": started.isoformat(),
        "success": False,
    }
    try:
        session = get_session(prof)
        r = session.get(url)
        result["status"] = r.status_code
        elapsed_ms = int((datetime.now() - started).total_seconds() * 1000)
        result["elapsed_ms"] = elapsed_ms
        # intentar parsear JSON
        data = None
        try:
            data = r.json() if hasattr(r, "json") else None
        except Exception:
            pass
        if data:
            ua = data.get("user_agent") or data.get("userAgent") or ""
            tls = data.get("tls") or {}
            peetprint = tls.get("peetprint") or data.get("peetprint")
            ja3 = tls.get("ja3") or data.get("ja3")
            ja3_hash = tls.get("ja3_hash") or data.get("ja3_hash")
            ja4 = tls.get("ja4") or data.get("ja4")
            result["detected"] = {
                "ja4": ja4,
                "ja3_hash": ja3_hash,
                "peetprint": peetprint,
                "user_agent": ua,
            }
            # Verificar contra ja4db
            if ja4:
                result["ja4db_match"] = verify_against_ja4db(ja4)
        result["success"] = 200 <= r.status_code < 400
        record_profile_use(prof, result["success"], elapsed_ms)
    except ImportError:
        result["error"] = "tls-client no instalado (pip install tls-client)"
        record_profile_use(prof, False)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        record_profile_use(prof, False)

    _log_test(result)
    return result


def _log_test(entry: dict):
    d = json_atomic.load(_path_log(), default={"log": []})
    d["log"].insert(0, entry)
    d["log"] = d["log"][:30]
    json_atomic.save(_path_log(), d)


def get_log(limit: int = 20) -> List[dict]:
    return json_atomic.load(_path_log(), default={"log": []}).get("log", [])[:limit]


def self_test() -> dict:
    """Smoke test: verifica que tls-client está disponible y hace 1 request."""
    try:
        import tls_client  # noqa
    except ImportError:
        return {"ok": False, "error": "tls-client no instalado"}
    try:
        s = get_session(DEFAULT_PROFILE)
        r = s.get("https://httpbin.org/headers")
        return {
            "ok": 200 <= r.status_code < 400,
            "profile": DEFAULT_PROFILE,
            "status": r.status_code,
            "available_profiles": len(PROFILES),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════════════════════
#  ESTADÍSTICAS DE PERFILES (para rotación inteligente)
# ═══════════════════════════════════════════════════════════
def _stats_path():
    return config.ruta(STATS_KEY)


def _load_stats() -> dict:
    return json_atomic.load(_stats_path(), default={"profiles": {}})


def _save_stats(data: dict):
    json_atomic.save(_stats_path(), data)


def record_profile_use(profile: str, success: bool, latency_ms: int = 0):
    """Log interno para rotación ponderada. También alimenta el circuit
    breaker que blacklistea perfiles con 2+ fallos consecutivos."""
    with _lock:
        d = _load_stats()
        p = d["profiles"].setdefault(profile, {
            "uses": 0, "successes": 0, "fails": 0,
            "total_latency_ms": 0, "last_used_at": None,
        })
        p["uses"] += 1
        if success:
            p["successes"] += 1
        else:
            p["fails"] += 1
        p["total_latency_ms"] += latency_ms
        p["last_used_at"] = datetime.now().isoformat()
        _save_stats(d)
    # circuit breaker
    _record_result(profile, success)


def get_profile_stats() -> List[Dict]:
    """Contadores + success rate + avg latency per perfil."""
    d = _load_stats()
    out = []
    for prof in PROFILES:
        p = d.get("profiles", {}).get(prof, {})
        uses = p.get("uses", 0)
        succ = p.get("successes", 0)
        out.append({
            "profile": prof,
            "uses": uses,
            "successes": succ,
            "fails": p.get("fails", 0),
            "success_rate_pct": round(100 * succ / uses, 1) if uses else None,
            "avg_latency_ms": round(p.get("total_latency_ms", 0) / uses, 1) if uses else None,
            "last_used_at": p.get("last_used_at"),
        })
    return out


def pick_weighted_profile(min_uses_for_weighting: int = 5,
                           exclude_blacklisted: bool = True) -> str:
    """Rotación ponderada por success_rate. Los perfiles con más éxito
    se escogen más veces. Si un perfil no tiene >=min_uses_for_weighting usos,
    se le da peso neutro (1.0). Si exclude_blacklisted=True, descarta perfiles
    en cooldown por fallos consecutivos."""
    stats = get_profile_stats()
    bl = get_blacklist()
    candidates = []
    weights = []
    for s in stats:
        if exclude_blacklisted and s["profile"] in bl:
            continue
        if s["uses"] >= min_uses_for_weighting and s["success_rate_pct"] is not None:
            w = max(0.1, s["success_rate_pct"] / 100.0)
        else:
            w = 0.5
        candidates.append(s["profile"])
        weights.append(w)
    if not candidates:
        # todos en blacklist → volver a usar todos random
        return random.choice(PROFILES)
    return random.choices(candidates, weights=weights, k=1)[0]


# ═══════════════════════════════════════════════════════════
#  ROTATE ON FAIL (circuit breaker por perfil)
# ═══════════════════════════════════════════════════════════
_FAIL_STREAK: Dict[str, dict] = {}
_BLACKLIST: Dict[str, dict] = {}   # profile → {until_ts, reason, streak}
BLACKLIST_COOLDOWN_S = 600         # 10 min
FAIL_THRESHOLD = 2                 # 2 fallos consecutivos → blacklist


def _record_result(profile: str, success: bool):
    """Tracker de fallos consecutivos. Blacklistea si 2 fallos seguidos."""
    now = datetime.now().timestamp()
    with _lock:
        if success:
            _FAIL_STREAK[profile] = {"streak": 0, "last_ts": now}
            # limpia blacklist si estaba
            _BLACKLIST.pop(profile, None)
            return
        # fallo
        cur = _FAIL_STREAK.get(profile, {"streak": 0})
        cur["streak"] = cur.get("streak", 0) + 1
        cur["last_ts"] = now
        _FAIL_STREAK[profile] = cur
        if cur["streak"] >= FAIL_THRESHOLD:
            _BLACKLIST[profile] = {
                "until_ts": now + BLACKLIST_COOLDOWN_S,
                "reason": f"{cur['streak']} consecutive fails",
                "streak": cur["streak"],
                "blacklisted_at": datetime.now().isoformat(),
            }


def get_blacklist() -> Dict[str, dict]:
    """Perfiles actualmente en blacklist (no expirados)."""
    now = datetime.now().timestamp()
    with _lock:
        out = {}
        for prof, bl in list(_BLACKLIST.items()):
            if bl["until_ts"] > now:
                out[prof] = dict(bl)
                out[prof]["remaining_s"] = int(bl["until_ts"] - now)
            else:
                _BLACKLIST.pop(prof, None)
        return out


def clear_blacklist(profile: Optional[str] = None) -> dict:
    """Limpia la blacklist de un perfil o toda."""
    with _lock:
        if profile:
            removed = _BLACKLIST.pop(profile, None) is not None
            _FAIL_STREAK.pop(profile, None)
            return {"ok": True, "removed": removed, "profile": profile}
        n = len(_BLACKLIST)
        _BLACKLIST.clear()
        _FAIL_STREAK.clear()
        return {"ok": True, "removed_count": n}


# ═══════════════════════════════════════════════════════════
#  HEADER ORDERING (Chrome real order)
# ═══════════════════════════════════════════════════════════
# Orden exacto de Chrome 131+ para un GET a través de tls-client
CHROME_HEADER_ORDER = [
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "upgrade-insecure-requests",
    "user-agent",
    "accept",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-user",
    "sec-fetch-dest",
    "accept-encoding",
    "accept-language",
    "cookie",
]

FIREFOX_HEADER_ORDER = [
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "dnt",
    "connection",
    "upgrade-insecure-requests",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "cookie",
]


def get_header_order(profile: str) -> List[str]:
    """Devuelve el orden de headers esperado para un perfil."""
    if profile.startswith("firefox"):
        return list(FIREFOX_HEADER_ORDER)
    return list(CHROME_HEADER_ORDER)


# ═══════════════════════════════════════════════════════════
#  UA-CH (Sec-CH-UA) coherencia con UA + JA4 profile
# ═══════════════════════════════════════════════════════════
# Mapping profile → (chrome_major, platform). GREASE values rotativos pero
# SIEMPRE en formato (BRAND, "99") para esa versión Chrome.
_GREASE_BRANDS = ['"Not(A:Brand"', '"Not.A/Brand"', '"Not_A Brand"',
                   '"Not-A.Brand"', '"Not/A)Brand"', '"Not?A_Brand"']


def build_sec_ch_ua_for_profile(profile: str, ua: str = "") -> Dict[str, str]:
    """Construye los headers UA-CH (Sec-CH-UA, mobile, platform) coherentes
    con el perfil JA4 y/o el User-Agent. Garantiza match cross-fingerprint:
        JA4 → Chrome 131 ⟺ sec-ch-ua → "Chromium";v="131", "Chrome";v="131"
    Devuelve dict listo para inyectar en headers.
    """
    p = (profile or "").lower()
    # Determinar versión Chrome
    if "chrome_133" in p or "133" in p:    chrome_v = 133
    elif "chrome_131" in p or "131" in p:  chrome_v = 131
    elif "chrome_124" in p or "124" in p:  chrome_v = 124
    elif "chrome_120" in p or "120" in p:  chrome_v = 120
    elif p.startswith("firefox") or p.startswith("safari"):
        # Firefox/Safari NO mandan Sec-CH-UA — devolver vacío
        return {}
    else:
        chrome_v = 131  # default modernizado

    # Plataforma desde profile o UA
    platform = '"Windows"'
    if "mac" in p or "Macintosh" in ua:        platform = '"macOS"'
    elif "linux" in p or "Linux" in ua:        platform = '"Linux"'
    elif "android" in p or "Android" in ua:    platform = '"Android"'
    elif "ios" in p or "iPhone" in ua or "iPad" in ua:
        platform = '"iOS"'

    is_mobile = "?1" if platform in ('"Android"', '"iOS"') else "?0"

    # GREASE rotativo determinístico por chrome_v (mismo perfil → mismo greasy)
    grease = _GREASE_BRANDS[chrome_v % len(_GREASE_BRANDS)]

    sec_ch_ua = f'{grease};v="99", "Chromium";v="{chrome_v}", "Google Chrome";v="{chrome_v}"'
    return {
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": is_mobile,
        "sec-ch-ua-platform": platform,
    }


def validate_uach_consistency(profile: str, ua: str,
                                sec_ch_ua: str) -> Dict[str, bool]:
    """Cross-checkea que (profile, ua, sec_ch_ua) son coherentes.
    Reporta drift si Chrome-major en JA4 no coincide con sec-ch-ua."""
    expected = build_sec_ch_ua_for_profile(profile, ua)
    if not expected:
        # Firefox/Safari → no debería haber sec-ch-ua
        return {"ok": not sec_ch_ua, "expected_empty": True,
                "observed": sec_ch_ua}
    exp_chua = expected.get("sec-ch-ua", "")
    return {"ok": exp_chua == sec_ch_ua,
            "expected": exp_chua, "observed": sec_ch_ua,
            "profile": profile, "ua_summary": ua[:80]}


def validate_header_order(headers: Dict[str, str], profile: str) -> dict:
    """Valida que los headers estén en orden Chrome/Firefox-like.

    Nota: tls-client ya envía en orden correcto por defecto. Esta función
    sirve para verificar headers custom añadidos por el usuario.
    """
    expected = get_header_order(profile)
    received = [h.lower() for h in headers.keys()]
    expected_present = [h for h in expected if h in received]
    # Verificar orden relativo
    order_ok = True
    last_idx = -1
    for h in received:
        if h in expected:
            idx = expected.index(h)
            if idx < last_idx:
                order_ok = False
                break
            last_idx = idx
    extras = [h for h in received if h not in expected]
    return {
        "order_correct": order_ok,
        "expected_present": expected_present,
        "received": received,
        "extra_headers": extras,
        "profile": profile,
    }


# ═══════════════════════════════════════════════════════════
#  VERIFICACIÓN CONTRA JA4DB (ja4db.com)
# ═══════════════════════════════════════════════════════════
def verify_against_ja4db(ja4: str) -> dict:
    """Compara un fingerprint JA4 detectado contra la base de datos local
    de JA4 conocidos de Chrome/Firefox/Safari/Android.

    Devuelve:
      {
        matched: True/False,
        category: "chrome_stable"|"firefox_stable"|...|"unknown",
        confidence: 0-100,
        notes: "explicación",
      }
    """
    if not ja4 or not isinstance(ja4, str):
        return {"matched": False, "category": "unknown",
                "confidence": 0, "notes": "JA4 vacío o inválido"}

    # Match exacto
    for cat, prints in KNOWN_JA4.items():
        if ja4 in prints:
            return {
                "matched": True, "category": cat, "confidence": 100,
                "notes": f"Match exacto con {cat} ({len(prints)} variantes conocidas)",
                "ja4": ja4,
            }

    # Match de prefijo (mismos ciphers, diferentes extensiones)
    # JA4 format: {prefix}_{ciphers_hash}_{ext_hash}
    # Un match de prefix + ciphers = alta probabilidad de ser ese browser
    parts = ja4.split("_")
    if len(parts) >= 3:
        head_ciphers = "_".join(parts[:2])  # "t13d1516h2_8daaf6152771"
        for cat, prints in KNOWN_JA4.items():
            for p in prints:
                p_parts = p.split("_")
                if len(p_parts) >= 3 and "_".join(p_parts[:2]) == head_ciphers:
                    return {
                        "matched": True, "category": cat, "confidence": 75,
                        "notes": f"Match por prefix+ciphers ({cat}), extensiones diferentes",
                        "ja4": ja4,
                    }

    # Match parcial: solo por prefix (versión TLS + ALPN + SNI)
    if len(parts) >= 1:
        prefix = parts[0]  # "t13d1516h2"
        for cat, prints in KNOWN_JA4.items():
            for p in prints:
                if p.split("_")[0] == prefix:
                    return {
                        "matched": True, "category": cat, "confidence": 40,
                        "notes": f"Match débil por prefix TLS ({cat})",
                        "ja4": ja4,
                    }

    return {
        "matched": False, "category": "unknown", "confidence": 0,
        "notes": "JA4 no reconocido — posible navegador exótico o bot detector",
        "ja4": ja4,
    }


def get_current_ja4(profile: Optional[str] = None) -> dict:
    """Hace un handshake real y devuelve el JA4 detectado por tls.peet.ws."""
    r = test_handshake(profile=profile)
    detected = (r.get("detected") or {})
    ja4 = detected.get("ja4")
    verification = verify_against_ja4db(ja4) if ja4 else {"matched": False}
    return {
        "profile": r.get("profile"),
        "ja4": ja4,
        "success": r.get("success"),
        "verification": verification,
        "elapsed_ms": r.get("elapsed_ms"),
        "user_agent": detected.get("user_agent"),
    }


# ═══════════════════════════════════════════════════════════
#  BENCHMARK DE TODOS LOS PERFILES
# ═══════════════════════════════════════════════════════════
def benchmark_profiles(concurrency: int = 4, url: str = "https://tls.peet.ws/api/all",
                        broadcast=None) -> dict:
    """Testa todos los perfiles en paralelo. Reporta success rate + latencia
    + JA4 detectado per perfil. Actualiza stats para rotación ponderada.

    Tarda ~10-30 s dependiendo de tu red.
    """
    started = datetime.now()
    results = {}

    def _run(prof: str):
        t0 = datetime.now()
        try:
            session = get_session(prof)
            r = session.get(url, timeout_seconds=10)
            elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)
            ja4 = None
            try:
                data = r.json()
                tls = data.get("tls") or {}
                ja4 = tls.get("ja4") or data.get("ja4")
            except Exception:
                pass
            ok = 200 <= r.status_code < 400
            record_profile_use(prof, ok, elapsed_ms)
            verify = verify_against_ja4db(ja4) if ja4 else None
            return prof, {
                "ok": ok, "status": r.status_code, "elapsed_ms": elapsed_ms,
                "ja4": ja4,
                "verification": verify,
            }
        except Exception as e:
            record_profile_use(prof, False)
            return prof, {"ok": False, "error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_run, p): p for p in PROFILES}
        for fut in as_completed(futures):
            prof, data = fut.result()
            results[prof] = data
            if broadcast:
                try:
                    broadcast("ja4_benchmark_progress", {"profile": prof, "result": data})
                except Exception:
                    pass

    # Resumen
    ok_count = sum(1 for r in results.values() if r.get("ok"))
    avg_latency = round(
        sum(r.get("elapsed_ms", 0) for r in results.values() if r.get("ok"))
        / max(1, ok_count), 1
    )
    matched = sum(1 for r in results.values()
                   if (r.get("verification") or {}).get("matched"))
    return {
        "ok": True,
        "total_profiles": len(PROFILES),
        "ok_count": ok_count,
        "fail_count": len(PROFILES) - ok_count,
        "success_rate_pct": round(100 * ok_count / len(PROFILES), 1),
        "avg_latency_ms": avg_latency,
        "matched_ja4db": matched,
        "elapsed_s": round((datetime.now() - started).total_seconds(), 1),
        "results": results,
    }


# ═══════════════════════════════════════════════════════════
#  INFO / STATS GLOBAL
# ═══════════════════════════════════════════════════════════
def get_info() -> dict:
    """Resumen del módulo + stats globales."""
    stats = get_profile_stats()
    total_uses = sum(s["uses"] for s in stats)
    tested_profiles = sum(1 for s in stats if s["uses"] > 0)
    avg_success = round(
        sum(s["success_rate_pct"] or 0 for s in stats if s["success_rate_pct"] is not None)
        / max(1, tested_profiles), 1
    )
    try:
        import tls_client
        v = getattr(tls_client, "__version__", "unknown")
        # A veces __version__ es módulo y no string — extraer versión real
        if not isinstance(v, str):
            try:
                import importlib.metadata
                v = importlib.metadata.version("tls_client")
            except Exception:
                v = "installed"
        tls_client_version = v
    except ImportError:
        tls_client_version = "NOT INSTALLED"
    return {
        "tls_client_version": tls_client_version,
        "total_profiles": len(PROFILES),
        "tested_profiles": tested_profiles,
        "total_uses": total_uses,
        "avg_success_rate_pct": avg_success,
        "ja4db_categories": list(KNOWN_JA4.keys()),
        "ja4db_total_prints": sum(len(v) for v in KNOWN_JA4.values()),
        "default_profile": DEFAULT_PROFILE,
    }
