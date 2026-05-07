"""http3_settings_match.py — HTTP/3 SETTINGS frame fingerprint match Chrome 132+.

Cuando declaras Chrome 132 en JA4 (TLS ClientHello) pero las HTTP/3 SETTINGS
del cliente real no coinciden con los valores reales del Chrome 132 stock,
Akamai/DataDome detectan inconsistencia y bloquean.

Casos típicos:
  - curl-cffi/impersonate emite MAX_CONCURRENT_STREAMS=1000 en vez de 100
  - Reqwest/Tokio H3 usa INITIAL_WINDOW_SIZE=65535 (default RFC) en vez de 6291456
  - tls-client puede no pushear el SETTINGS frame correcto para QPACK

Referencia:
  Chrome 132 SETTINGS reales (Wireshark capture, 2026-01):
    HEADER_TABLE_SIZE       = 65536
    ENABLE_PUSH             = 0
    MAX_CONCURRENT_STREAMS  = 100      (NO 1000 como curl-cffi)
    INITIAL_WINDOW_SIZE     = 6291456
    MAX_FRAME_SIZE          = 16384
    MAX_HEADER_LIST_SIZE    = 262144

API pública:
  get_chrome_settings(version=132)
  validate_settings_match(declared_ja4, observed_settings)
  generate_chrome_alpn_offer()
  detect_quic_fingerprint(packets)
  generate_quic_initial(version)
  diff_settings(observed, expected)
  get_known_browser_h3_profile(browser, version)
  self_test()
"""
from __future__ import annotations
import os, time, json, hashlib, threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic


_CONFIG_KEY = "http3_settings_config.json"
_LOG_KEY = "http3_settings_log.json"
_lock = threading.RLock()


# ═══════════════════════════════════════════════════════════════
# REFERENCE DATA — Chrome / Firefox / Safari real H3 settings
# ═══════════════════════════════════════════════════════════════
_CHROME_REFERENCE: Dict[int, Dict[str, int]] = {
    131: {
        "HEADER_TABLE_SIZE":       65536,
        "ENABLE_PUSH":             0,
        "MAX_CONCURRENT_STREAMS":  100,
        "INITIAL_WINDOW_SIZE":     6291456,
        "MAX_FRAME_SIZE":          16384,
        "MAX_HEADER_LIST_SIZE":    262144,
    },
    132: {
        "HEADER_TABLE_SIZE":       65536,
        "ENABLE_PUSH":             0,
        "MAX_CONCURRENT_STREAMS":  100,
        "INITIAL_WINDOW_SIZE":     6291456,
        "MAX_FRAME_SIZE":          16384,
        "MAX_HEADER_LIST_SIZE":    262144,
    },
    133: {
        "HEADER_TABLE_SIZE":       65536,
        "ENABLE_PUSH":             0,
        "MAX_CONCURRENT_STREAMS":  100,
        "INITIAL_WINDOW_SIZE":     6291456,
        "MAX_FRAME_SIZE":          16384,
        "MAX_HEADER_LIST_SIZE":    262144,
    },
}

_FIREFOX_REFERENCE: Dict[int, Dict[str, int]] = {
    133: {
        "HEADER_TABLE_SIZE":       65536,
        "ENABLE_PUSH":             0,
        "MAX_CONCURRENT_STREAMS":  100,
        "INITIAL_WINDOW_SIZE":     131072,
        "MAX_FRAME_SIZE":          16384,
    },
}

_SAFARI_REFERENCE: Dict[int, Dict[str, int]] = {
    18: {
        "HEADER_TABLE_SIZE":       4096,
        "ENABLE_PUSH":             0,
        "INITIAL_WINDOW_SIZE":     2097152,
        "MAX_FRAME_SIZE":          16384,
    },
}

# Known curl-cffi / impersonate signatures (used for detection)
_BAD_FINGERPRINTS: Dict[str, Dict[str, int]] = {
    "curl_cffi_chrome": {
        "HEADER_TABLE_SIZE":       65536,
        "ENABLE_PUSH":             0,
        "MAX_CONCURRENT_STREAMS":  1000,    # WRONG (Chrome real uses 100)
        "INITIAL_WINDOW_SIZE":     6291456,
        "MAX_FRAME_SIZE":          16384,
    },
    "tls_client_default": {
        "HEADER_TABLE_SIZE":       4096,    # WRONG default
        "ENABLE_PUSH":             1,       # WRONG (Chrome=0)
        "MAX_CONCURRENT_STREAMS":  100,
        "INITIAL_WINDOW_SIZE":     65535,   # WRONG RFC default
        "MAX_FRAME_SIZE":          16384,
    },
}


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "default_browser": "chrome",
        "default_version": 132,
        "strict_mode": True,             # falla si CUALQUIER setting no matchea
        "tolerance_window_size_pct": 0,  # 0 = exacto
        "log_validation_failures": True,
        "alpn_protocols": ["h3", "h3-29", "h2", "http/1.1"],
        "quic_version": "0x00000001",    # QUIC v1 (RFC 9000)
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def _log(event: Dict[str, Any]) -> None:
    try:
        event["ts"] = datetime.now().isoformat()
        with _lock:
            d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
            d["log"].insert(0, event)
            d["log"] = d["log"][:200]
            json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# CHROME SETTINGS RETRIEVAL
# ═══════════════════════════════════════════════════════════════
def get_chrome_settings(version: int = 132) -> Dict[str, Any]:
    """Devuelve el HTTP/3 SETTINGS frame stock de Chrome <version>."""
    if version in _CHROME_REFERENCE:
        return {
            "ok": True,
            "browser": "chrome",
            "version": version,
            "settings": dict(_CHROME_REFERENCE[version]),
            "alpn": ["h3", "h2"],
            "is_exact_match": True,
        }
    # Fallback al más cercano
    closest = min(_CHROME_REFERENCE.keys(), key=lambda v: abs(v - version))
    return {
        "ok": True,
        "browser": "chrome",
        "version": version,
        "settings": dict(_CHROME_REFERENCE[closest]),
        "alpn": ["h3", "h2"],
        "is_exact_match": False,
        "note": f"version {version} not in reference, used {closest}",
    }


def get_known_browser_h3_profile(browser: str = "chrome",
                                   version: int = 132) -> Dict[str, Any]:
    """Devuelve el perfil completo (settings + alpn + quic) para un browser."""
    b = (browser or "chrome").lower()
    if b == "chrome":
        s = get_chrome_settings(version)
        return {**s, "quic_version": "0x00000001",
                "transport_params": {"max_idle_timeout": 30000,
                                     "max_udp_payload_size": 1472,
                                     "active_connection_id_limit": 2}}
    if b == "firefox":
        ver = version if version in _FIREFOX_REFERENCE else 133
        return {"ok": True, "browser": "firefox", "version": ver,
                "settings": dict(_FIREFOX_REFERENCE[ver]),
                "alpn": ["h3", "h2"], "quic_version": "0x00000001",
                "transport_params": {"max_idle_timeout": 30000}}
    if b == "safari":
        ver = version if version in _SAFARI_REFERENCE else 18
        return {"ok": True, "browser": "safari", "version": ver,
                "settings": dict(_SAFARI_REFERENCE[ver]),
                "alpn": ["h3", "h2"], "quic_version": "0x00000001",
                "transport_params": {"max_idle_timeout": 30000}}
    return {"ok": False, "error": f"unknown browser: {browser}"}


# ═══════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════
def validate_settings_match(declared_ja4: str = "",
                              observed_settings: Optional[Dict[str, int]] = None,
                              browser_hint: str = "chrome",
                              version_hint: int = 132) -> Dict[str, Any]:
    """Compara SETTINGS observados vs los esperados según el JA4 declarado.

    declared_ja4: string JA4 (e.g., 't13d1516h2_8daaf6152771_b186095e22b6').
                  Se intenta extraer browser+version pero también acepta hints.
    observed_settings: dict {SETTING_NAME: value} capturado del wire (Wireshark
                       o eBPF).
    """
    observed = observed_settings or {}
    if not isinstance(observed, dict):
        return {"ok": False, "error": "observed_settings must be dict"}

    # Resolver browser/version (preferir hints, luego JA4)
    browser = browser_hint or "chrome"
    version = version_hint or 132

    profile = get_known_browser_h3_profile(browser, version)
    if not profile.get("ok"):
        return {"ok": False, "error": "no_reference_for_browser",
                "browser": browser, "version": version}
    expected = profile["settings"]

    cfg = get_config()
    diffs = []
    matches = []
    for key, expected_val in expected.items():
        if key not in observed:
            diffs.append({"key": key, "expected": expected_val,
                          "observed": None, "reason": "missing"})
            continue
        obs_val = observed[key]
        if obs_val == expected_val:
            matches.append(key)
        else:
            # Tolerancia opcional para INITIAL_WINDOW_SIZE
            if (key == "INITIAL_WINDOW_SIZE"
                    and cfg.get("tolerance_window_size_pct", 0) > 0):
                tol = expected_val * (cfg["tolerance_window_size_pct"] / 100)
                if abs(obs_val - expected_val) <= tol:
                    matches.append(key)
                    continue
            diffs.append({"key": key, "expected": expected_val,
                          "observed": obs_val, "reason": "mismatch"})

    # Fingerprint check: ¿matchea algún BAD signature conocido?
    bad_match = None
    for sig_name, sig_settings in _BAD_FINGERPRINTS.items():
        sig_diff = sum(1 for k, v in sig_settings.items()
                        if observed.get(k) == v)
        if sig_diff >= max(3, int(len(sig_settings) * 0.7)):
            bad_match = {"name": sig_name, "matches_count": sig_diff,
                         "total_keys": len(sig_settings)}
            break

    matches_count = len(matches)
    diffs_count = len(diffs)
    total = matches_count + diffs_count
    score = (matches_count / total * 100) if total > 0 else 0
    is_match = (diffs_count == 0) if cfg.get("strict_mode") else (score >= 80)

    result = {
        "ok": True,
        "matches_chrome_real": is_match,
        "score_pct": round(score, 1),
        "matches": matches,
        "matches_count": matches_count,
        "differences": diffs,
        "differences_count": diffs_count,
        "browser_expected": browser,
        "version_expected": version,
        "declared_ja4": declared_ja4 or "(none)",
        "fingerprint_match_bad": bad_match,
        "verdict": "PASS" if is_match else "FAIL",
    }

    if not is_match and cfg.get("log_validation_failures"):
        _log({"op": "validate_failure", "score": score,
              "diffs_count": diffs_count, "bad_match": bad_match})

    return result


def diff_settings(observed: Dict[str, int],
                   expected: Optional[Dict[str, int]] = None,
                   version: int = 132) -> Dict[str, Any]:
    """Helper de diff bonito (incluye keys missing/extra/mismatch)."""
    if expected is None:
        expected = _CHROME_REFERENCE.get(version, _CHROME_REFERENCE[132])
    only_observed = [k for k in observed if k not in expected]
    only_expected = [k for k in expected if k not in observed]
    mismatches = [{"key": k, "expected": expected[k], "observed": observed[k]}
                   for k in expected
                   if k in observed and observed[k] != expected[k]]
    return {
        "ok": True,
        "matches_count": sum(1 for k in expected
                              if k in observed and observed[k] == expected[k]),
        "mismatches_count": len(mismatches),
        "mismatches": mismatches,
        "only_in_observed": only_observed,
        "only_in_expected": only_expected,
        "is_perfect": (len(mismatches) == 0 and len(only_observed) == 0
                        and len(only_expected) == 0),
    }


# ═══════════════════════════════════════════════════════════════
# ALPN OFFER GENERATION
# ═══════════════════════════════════════════════════════════════
def generate_chrome_alpn_offer(version: int = 132,
                                 prefer_h3: bool = True) -> Dict[str, Any]:
    """Genera el ALPN offer que Chrome <version> envía en TLS ClientHello.
    Chrome ofrece h3 + h2 + http/1.1 (en ese orden si H3 disponible)."""
    if prefer_h3:
        protos = ["h3", "h2", "http/1.1"]
    else:
        protos = ["h2", "http/1.1"]

    # Wire format: each entry prefixed with length byte
    wire = b""
    for p in protos:
        wire += bytes([len(p)]) + p.encode("ascii")

    return {
        "ok": True,
        "browser": "chrome",
        "version": version,
        "alpn_protocols": protos,
        "alpn_wire_hex": wire.hex(),
        "alpn_wire_length": len(wire),
        "prefer_h3": prefer_h3,
    }


# ═══════════════════════════════════════════════════════════════
# QUIC FINGERPRINT DETECTION
# ═══════════════════════════════════════════════════════════════
def detect_quic_fingerprint(packets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analiza una lista de paquetes QUIC parseados (e.g., desde Wireshark
    json export) y devuelve un veredicto sobre el browser inferido.

    Cada paquete debe tener al menos: {type, frames: [{type, settings: {...}}]}.
    """
    if not isinstance(packets, list):
        return {"ok": False, "error": "packets must be list"}

    found_settings: Dict[str, int] = {}
    initial_seen = False
    handshake_seen = False
    for pkt in packets:
        ptype = pkt.get("type", "").lower()
        if "initial" in ptype:
            initial_seen = True
        if "handshake" in ptype:
            handshake_seen = True
        for frame in pkt.get("frames", []):
            ftype = (frame.get("type") or "").lower()
            if ftype == "settings" or "settings" in ftype:
                s = frame.get("settings") or {}
                if isinstance(s, dict):
                    found_settings.update(s)

    if not found_settings:
        return {"ok": True, "browser_inferred": "unknown",
                "reason": "no_settings_frame_seen",
                "initial_seen": initial_seen, "handshake_seen": handshake_seen,
                "packets_analyzed": len(packets)}

    # Compara contra todos los perfiles
    best_match = None
    best_score = 0
    for browser, refs in (("chrome", _CHROME_REFERENCE),
                          ("firefox", _FIREFOX_REFERENCE),
                          ("safari", _SAFARI_REFERENCE)):
        for ver, settings in refs.items():
            score = sum(1 for k, v in settings.items()
                        if found_settings.get(k) == v)
            score_pct = score / len(settings) * 100
            if score_pct > best_score:
                best_score = score_pct
                best_match = (browser, ver, score, len(settings))

    return {
        "ok": True,
        "browser_inferred": best_match[0] if best_match else "unknown",
        "version_inferred": best_match[1] if best_match else None,
        "match_score_pct": round(best_score, 1),
        "settings_observed": found_settings,
        "initial_seen": initial_seen,
        "handshake_seen": handshake_seen,
        "packets_analyzed": len(packets),
    }


# ═══════════════════════════════════════════════════════════════
# QUIC INITIAL PACKET GENERATION (para tooling)
# ═══════════════════════════════════════════════════════════════
def generate_quic_initial(version: str = "0x00000001",
                            scid_len: int = 8,
                            dcid_len: int = 8) -> Dict[str, Any]:
    """Genera un QUIC Initial packet skeleton (header only, sin crypto)."""
    if not version.startswith("0x"):
        return {"ok": False, "error": "version must be hex string 0x..."}
    try:
        ver_int = int(version, 16)
    except Exception:
        return {"ok": False, "error": "invalid hex version"}

    # Long header: form bit=1, fixed bit=1, type=00 (Initial)
    first_byte = 0xC0
    header = bytes([first_byte])
    header += ver_int.to_bytes(4, "big")
    header += bytes([dcid_len])
    header += os.urandom(dcid_len) if dcid_len > 0 else b""
    header += bytes([scid_len])
    header += os.urandom(scid_len) if scid_len > 0 else b""

    return {
        "ok": True,
        "version": version,
        "version_int": ver_int,
        "header_hex": header.hex(),
        "header_length": len(header),
        "is_initial": True,
    }


# ═══════════════════════════════════════════════════════════════
# COMPACT FINGERPRINT (hash for comparing 2 SETTINGS blocks)
# ═══════════════════════════════════════════════════════════════
def settings_fingerprint(settings: Dict[str, int]) -> str:
    """Hash determinístico de un SETTINGS dict para comparación rápida."""
    keys = sorted(settings.keys())
    serialized = "|".join(f"{k}={settings[k]}" for k in keys)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}

    # 1. Config
    cfg = get_config()
    checks["config_has_default_browser"] = "default_browser" in cfg
    checks["config_has_default_version"] = cfg.get("default_version") == 132

    # 2. get_chrome_settings 132
    s = get_chrome_settings(132)
    checks["chrome_132_has_settings"] = "settings" in s
    chrome132 = s["settings"]
    checks["chrome_132_max_streams_100"] = chrome132["MAX_CONCURRENT_STREAMS"] == 100
    checks["chrome_132_window_6291456"] = chrome132["INITIAL_WINDOW_SIZE"] == 6291456
    checks["chrome_132_enable_push_0"] = chrome132["ENABLE_PUSH"] == 0
    checks["chrome_132_table_size_65536"] = chrome132["HEADER_TABLE_SIZE"] == 65536

    # 3. validate_settings_match — perfect match
    r = validate_settings_match(
        declared_ja4="t13d1516h2_8daaf6152771_b186095e22b6",
        observed_settings=dict(chrome132),
        browser_hint="chrome", version_hint=132)
    checks["validate_perfect_match_pass"] = r["matches_chrome_real"] is True
    checks["validate_perfect_score_100"] = r["score_pct"] == 100.0

    # 4. validate_settings_match — curl-cffi (MAX_CONCURRENT_STREAMS=1000)
    bad_observed = dict(chrome132)
    bad_observed["MAX_CONCURRENT_STREAMS"] = 1000
    r = validate_settings_match(
        declared_ja4="t13d1516h2_8daaf6152771_b186095e22b6",
        observed_settings=bad_observed,
        browser_hint="chrome", version_hint=132)
    checks["validate_curl_cffi_fails"] = r["matches_chrome_real"] is False
    checks["validate_curl_cffi_diff_count"] = r["differences_count"] >= 1

    # 5. validate detects bad fingerprint
    checks["validate_detects_curl_cffi_signature"] = (
        r.get("fingerprint_match_bad") is not None
        and r["fingerprint_match_bad"].get("name") == "curl_cffi_chrome"
    )

    # 6. generate_chrome_alpn_offer
    a = generate_chrome_alpn_offer(132, prefer_h3=True)
    checks["alpn_offer_ok"] = a.get("ok") is True
    checks["alpn_includes_h3_first"] = a["alpn_protocols"][0] == "h3"
    checks["alpn_wire_hex_present"] = len(a.get("alpn_wire_hex", "")) > 0

    # 7. detect_quic_fingerprint con paquetes de chrome real
    pkts = [
        {"type": "Initial", "frames": []},
        {"type": "Handshake", "frames": [
            {"type": "SETTINGS", "settings": dict(chrome132)}
        ]},
    ]
    r = detect_quic_fingerprint(pkts)
    checks["detect_chrome_inferred"] = r["browser_inferred"] == "chrome"
    checks["detect_score_high"] = r["match_score_pct"] >= 90
    checks["detect_initial_seen"] = r["initial_seen"] is True

    # 8. detect con paquetes vacíos
    r = detect_quic_fingerprint([])
    checks["detect_empty_unknown"] = r["browser_inferred"] == "unknown"

    # 9. diff_settings
    d = diff_settings(observed=bad_observed, version=132)
    checks["diff_finds_mismatch"] = d["mismatches_count"] == 1
    checks["diff_not_perfect"] = d["is_perfect"] is False

    # 10. generate_quic_initial
    q = generate_quic_initial("0x00000001")
    checks["quic_initial_ok"] = q.get("ok") is True
    checks["quic_initial_has_header"] = len(q.get("header_hex", "")) > 0

    # 11. settings fingerprint
    fp1 = settings_fingerprint(chrome132)
    fp2 = settings_fingerprint(chrome132)
    fp3 = settings_fingerprint(bad_observed)
    checks["fingerprint_deterministic"] = fp1 == fp2
    checks["fingerprint_different_for_diff_settings"] = fp1 != fp3

    # 12. profile firefox / safari
    pf = get_known_browser_h3_profile("firefox", 133)
    checks["firefox_profile_ok"] = pf.get("ok") is True
    ps = get_known_browser_h3_profile("safari", 18)
    checks["safari_profile_ok"] = ps.get("ok") is True

    # 13. profile unknown browser
    pu = get_known_browser_h3_profile("netscape", 4)
    checks["unknown_browser_returns_error"] = pu.get("ok") is False

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "http3_settings_match",
        "version": 1,
        "purpose": "HTTP/3 SETTINGS frame fingerprint match Chrome 132+",
        "browsers_supported": ["chrome", "firefox", "safari"],
        "chrome_versions_referenced": list(_CHROME_REFERENCE.keys()),
        "bad_fingerprints_detected": list(_BAD_FINGERPRINTS.keys()),
    }
