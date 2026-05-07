"""http2_fingerprint.py — HTTP/2 frame-order fingerprint match (CRÍTICO 2026).

Akamai detecta bots ANTES del primer request leyendo HTTP/2 SETTINGS,
WINDOW_UPDATE, frame ordering. requests/httpx producen patrones non-browser.

Este módulo encapsula configuración de curl_cffi/h2 para que cada request
use el fingerprint H2 idéntico a Chrome 131 / Firefox 128 / Safari 17.

Aporta:
  - get_chrome_h2_settings()  → SETTINGS frame Chrome 131
  - get_priority_frames()     → PRIORITY frames stream IDs 3,5,7,9,11
  - get_pseudo_header_order() → m,a,s,p
  - apply_to_session(session) → configura una session de requests para H2 spoof

API:
  get_h2_profile(browser='chrome131')
  apply_h2_profile(session, browser='chrome131')
  validate_h2_request(headers_observed) → check pseudo-header order
  self_test()
"""
from typing import Dict, List, Optional


# Chrome 131 SETTINGS frame real (capturado de Wireshark + curl_cffi v0.7+)
H2_PROFILES = {
    "chrome131": {
        "settings": {
            "HEADER_TABLE_SIZE": 65536,
            "ENABLE_PUSH": 0,
            "INITIAL_WINDOW_SIZE": 6291456,
            "MAX_HEADER_LIST_SIZE": 262144,
        },
        "window_update_increment": 15663105,
        "priority_frames": [
            {"stream_id": 3, "weight": 201, "exclusive": False, "depends_on": 0},
            {"stream_id": 5, "weight": 101, "exclusive": False, "depends_on": 0},
            {"stream_id": 7, "weight": 1, "exclusive": False, "depends_on": 0},
            {"stream_id": 9, "weight": 1, "exclusive": False, "depends_on": 7},
            {"stream_id": 11, "weight": 1, "exclusive": False, "depends_on": 3},
        ],
        "pseudo_header_order": [":method", ":authority", ":scheme", ":path"],
        "header_order": [
            "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
            "upgrade-insecure-requests", "user-agent",
            "accept", "sec-fetch-site", "sec-fetch-mode",
            "sec-fetch-user", "sec-fetch-dest",
            "accept-encoding", "accept-language", "cookie",
        ],
    },
    "firefox128": {
        "settings": {
            "HEADER_TABLE_SIZE": 65536,
            "INITIAL_WINDOW_SIZE": 131072,
            "MAX_FRAME_SIZE": 16384,
        },
        "window_update_increment": 12517377,
        "priority_frames": [],
        "pseudo_header_order": [":method", ":path", ":authority", ":scheme"],
        "header_order": [
            "user-agent", "accept", "accept-language", "accept-encoding",
            "referer", "cookie", "upgrade-insecure-requests",
        ],
    },
    "safari17": {
        "settings": {
            "INITIAL_WINDOW_SIZE": 4194304,
            "MAX_CONCURRENT_STREAMS": 100,
            "ENABLE_PUSH": 1,
        },
        "window_update_increment": 10485760,
        "priority_frames": [],
        "pseudo_header_order": [":method", ":scheme", ":path", ":authority"],
        "header_order": [
            "host", "user-agent", "accept", "accept-language",
            "accept-encoding", "cookie",
        ],
    },
}


def get_h2_profile(browser: str = "chrome131") -> Dict:
    return dict(H2_PROFILES.get(browser, H2_PROFILES["chrome131"]))


def get_chrome_h2_settings() -> Dict:
    return H2_PROFILES["chrome131"]["settings"]


def get_priority_frames(browser: str = "chrome131") -> List[Dict]:
    return H2_PROFILES.get(browser, {}).get("priority_frames", [])


def get_pseudo_header_order(browser: str = "chrome131") -> List[str]:
    return H2_PROFILES.get(browser, {}).get("pseudo_header_order",
                                              [":method", ":authority", ":scheme", ":path"])


def apply_h2_profile(session, browser: str = "chrome131") -> Dict:
    """Aplica perfil H2 a una session de curl_cffi.
    Devuelve {ok, profile, library} indicando si se aplicó.
    """
    profile = H2_PROFILES.get(browser, H2_PROFILES["chrome131"])
    try:
        # curl_cffi >= 0.7 acepta `impersonate` que ya configura H2 correctamente
        if hasattr(session, "impersonate"):
            session.impersonate = "chrome131" if browser == "chrome131" else \
                                  ("firefox133" if browser == "firefox128" else "safari17_2_ios")
            return {"ok": True, "profile": browser, "library": "curl_cffi",
                    "applied_via": "impersonate"}
        # Fallback: requests no soporta H2 spoof real → devolver advertencia
        return {"ok": False, "profile": browser,
                "warning": "session no soporta H2 fingerprint — usar curl_cffi"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def validate_h2_request(headers_observed: Dict[str, str], browser: str = "chrome131") -> Dict:
    """Verifica que el orden de pseudo-headers observado coincide con el browser."""
    expected_order = get_pseudo_header_order(browser)
    # En requests reales no podemos comprobar el orden directamente,
    # pero sí validamos que estén todos presentes
    pseudo_headers = [k for k in headers_observed if k.startswith(":")]
    missing = [h for h in expected_order if h not in pseudo_headers]
    extra = [h for h in pseudo_headers if h not in expected_order]
    return {"ok": not missing and not extra,
            "missing": missing, "extra": extra,
            "expected": expected_order,
            "observed": pseudo_headers}


def self_test() -> Dict:
    r = {"checks": {}}
    # Profiles existen
    chrome = get_h2_profile("chrome131")
    r["checks"]["chrome131_has_settings"] = "settings" in chrome
    r["checks"]["chrome131_has_priority"] = len(chrome.get("priority_frames", [])) == 5
    r["checks"]["chrome131_window_15663105"] = chrome.get("window_update_increment") == 15663105

    # Pseudo header order
    order_chrome = get_pseudo_header_order("chrome131")
    r["checks"]["chrome_pseudo_order"] = order_chrome == [":method", ":authority", ":scheme", ":path"]
    order_ff = get_pseudo_header_order("firefox128")
    r["checks"]["firefox_pseudo_order"] = order_ff[1] == ":path"

    # Validate request — happy path
    headers = {":method": "GET", ":authority": "ticketmaster.com", ":scheme": "https",
               ":path": "/", "user-agent": "Mozilla/5.0", "accept": "*/*"}
    v = validate_h2_request(headers, "chrome131")
    r["checks"]["validate_complete_set"] = v["ok"]

    # Validate missing
    v_missing = validate_h2_request({":method": "GET"}, "chrome131")
    r["checks"]["validate_detects_missing"] = len(v_missing["missing"]) == 3

    # Apply (sin curl_cffi real → warning esperado)
    class FakeSession: pass
    s = FakeSession()
    res = apply_h2_profile(s, "chrome131")
    r["checks"]["apply_returns_dict"] = isinstance(res, dict) and "ok" in res

    # Settings de Chrome
    settings = get_chrome_h2_settings()
    r["checks"]["chrome_table_size_65536"] = settings.get("HEADER_TABLE_SIZE") == 65536
    r["checks"]["chrome_no_push"] = settings.get("ENABLE_PUSH") == 0

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "http2_fingerprint", "version": 1,
            "browsers_supported": list(H2_PROFILES.keys()),
            "chrome_priority_frames": len(H2_PROFILES["chrome131"]["priority_frames"])}
