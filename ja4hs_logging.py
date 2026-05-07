"""ja4hs_logging.py — SOTA P2-20: JA4H + JA4S server-side fingerprint logging.

JA4H: client HTTP fingerprint (extension de JA4 a HTTP/2 headers).
JA4S: server TLS+HTTP fingerprint observado desde el cliente.

Loggear el JA4S del server TM por endpoint nos da early-warning si:
  - TM rota el cipher suite o cambia el ALPN order  (deteccion de A/B test)
  - Aparece un edge node nuevo (Akamai shard)
  - Cambian los SETTINGS frame de HTTP/2 → posible upgrade Bot Manager

Esquema JA4H (cliente):
    JA4H = method(2) + version(2) + cookie_flag(1) + auth_flag(1) +
           num_headers(2) + accept_lang_4chars + truncated_sha(headers_hash)
    Ejemplo: "ge20cn6e4_es-ESa1b2c3d4_a1b2c3d4e5f6"

Esquema JA4S (servidor):
    JA4S = protocol(1) + version(2) + alpn_chosen + cipher_chosen +
           extensions_count + extensions_hash
    Ejemplo: "h2_1.3_h2_1301_07_ab12cd34"

API:
  log_observed_ja4s(endpoint, ja4s, response_metadata) -> dict
  get_ja4s_history(endpoint, days=7)                   -> list
  detect_ja4s_change(endpoint, new_ja4s)               -> dict
  compute_ja4h(request)                                 -> str
  compute_ja4s(server_response)                         -> str
  list_endpoints()                                      -> list
  purge_old_records(days=30)                            -> dict
  self_test()
"""
from __future__ import annotations
import os, time, json, hashlib, threading, re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic


_CONFIG_KEY = "ja4hs_config.json"
_HISTORY_KEY = "ja4hs_history.json"
_LOG_KEY = "ja4hs_log.json"
_lock = threading.RLock()


# ============================================================
# CONFIG
# ============================================================
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "max_records_per_endpoint": 200,
        "history_days_default": 7,
        "alert_on_change": True,
        "alert_min_age_hours": 1,
        "purge_after_days": 30,
        "include_response_metadata": True,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def _load_history() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_HISTORY_KEY),
                             default={"endpoints": {}})


def _save_history(d: Dict[str, Any]) -> None:
    json_atomic.save(config.ruta(_HISTORY_KEY), d)


# ============================================================
# JA4H COMPUTE (client HTTP fingerprint)
# ============================================================
_HTTP_METHODS_MAP = {
    "GET": "ge", "POST": "po", "PUT": "pu", "DELETE": "de",
    "PATCH": "pa", "HEAD": "he", "OPTIONS": "op", "CONNECT": "co",
}


def compute_ja4h(request: Dict[str, Any]) -> Dict[str, Any]:
    """Construye JA4H string desde un request dict.

    request = {"method": "GET", "http_version": "2",
               "headers": {"User-Agent": "...", "Cookie": "...", ...},
               "accept_lang": "es-ES,es"}
    """
    if not isinstance(request, dict):
        return {"ok": False, "error": "request_not_dict"}

    method = (request.get("method") or "GET").upper()
    method_code = _HTTP_METHODS_MAP.get(method, "xx")

    version = str(request.get("http_version", "2"))[:2].zfill(2)

    headers = request.get("headers") or {}
    # Normaliza nombres
    headers_lower = {k.lower(): v for k, v in headers.items()}

    cookie_flag = "c" if "cookie" in headers_lower else "n"
    auth_flag = "r" if "authorization" in headers_lower else "n"

    # Headers no-pseudo (excluir cookie/auth/referer + pseudo-headers que empiezan con :)
    excluded = {"cookie", "authorization", "referer"}
    relevant = [k for k in headers.keys()
                if k.lower() not in excluded and not k.startswith(":")]
    num_headers = min(99, len(relevant))
    nh_str = str(num_headers).zfill(2)

    accept_lang = request.get("accept_lang") or headers_lower.get("accept-language", "")
    al_clean = re.sub(r'[^a-zA-Z\-]', '', accept_lang.split(",")[0])[:6] or "------"

    # Hash de header names en orden (truncado a 12 hex chars)
    hdr_order = ",".join([h.lower() for h in relevant])
    hdr_hash = hashlib.sha256(hdr_order.encode()).hexdigest()[:12]

    ja4h = f"{method_code}{version}{cookie_flag}{auth_flag}{nh_str}_{al_clean}_{hdr_hash}"
    return {"ok": True, "ja4h": ja4h, "method": method,
            "num_headers_counted": num_headers, "accept_lang": al_clean}


# ============================================================
# JA4S COMPUTE (server-side observed fingerprint)
# ============================================================
def compute_ja4s(server_response: Dict[str, Any]) -> Dict[str, Any]:
    """Construye JA4S string desde la respuesta TLS+HTTP del servidor.

    server_response = {"protocol": "h2|h3|h1",
                       "tls_version": "1.3|1.2",
                       "alpn_chosen": "h2",
                       "cipher_chosen_iana": "0x1301",
                       "extensions": ["server_name","key_share",...]}
    """
    if not isinstance(server_response, dict):
        return {"ok": False, "error": "response_not_dict"}

    proto = (server_response.get("protocol") or "h2").lower()
    proto_code = {"h1": "h", "h2": "h", "h3": "q"}.get(proto, "h")

    tls_v = server_response.get("tls_version") or "1.3"
    tls_v_clean = tls_v.replace("TLSv", "").replace(".", "")[:2].zfill(2)

    alpn = (server_response.get("alpn_chosen") or "h2")[:4]

    cipher = str(server_response.get("cipher_chosen_iana") or "0x1301")
    if cipher.startswith("0x"): cipher = cipher[2:]
    cipher_clean = cipher.lower().zfill(4)[:4]

    extensions = server_response.get("extensions") or []
    ext_count = min(99, len(extensions)).__str__().zfill(2)

    ext_str = ",".join([str(e).lower() for e in extensions])
    ext_hash = hashlib.sha256(ext_str.encode()).hexdigest()[:8]

    ja4s = f"{proto_code}{tls_v_clean}_{alpn}_{cipher_clean}_{ext_count}_{ext_hash}"
    return {"ok": True, "ja4s": ja4s, "protocol": proto,
            "tls_version": tls_v, "extension_count": len(extensions)}


# ============================================================
# HISTORY LOGGING
# ============================================================
def log_observed_ja4s(endpoint: str, ja4s: str,
                       response_metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persiste un JA4S observado en `endpoint` con metadata."""
    if not endpoint or not ja4s:
        return {"ok": False, "error": "endpoint_or_ja4s_empty"}

    cfg = get_config()
    rec = {
        "ts": time.time(),
        "iso": datetime.now().isoformat(),
        "ja4s": ja4s,
    }
    if cfg.get("include_response_metadata") and response_metadata:
        rec["meta"] = {
            k: v for k, v in response_metadata.items()
            if k in ("status", "server", "edge", "via", "cf_ray", "akamai_grn", "x_amz_cf_id")
        }

    with _lock:
        d = _load_history()
        endp = d["endpoints"].setdefault(endpoint, {"records": [], "first_seen": rec["iso"]})
        endp["records"].insert(0, rec)
        # Cap
        endp["records"] = endp["records"][:cfg["max_records_per_endpoint"]]
        endp["last_ja4s"] = ja4s
        endp["last_ts"] = rec["ts"]
        _save_history(d)

    return {"ok": True, "endpoint": endpoint, "ja4s": ja4s,
            "records_now": len(d["endpoints"][endpoint]["records"])}


def get_ja4s_history(endpoint: str, days: int = 7) -> Dict[str, Any]:
    if not endpoint:
        return {"ok": False, "error": "endpoint_empty"}

    d = _load_history()
    endp = d["endpoints"].get(endpoint)
    if not endp:
        return {"ok": True, "endpoint": endpoint, "records": [], "count": 0}

    cutoff = time.time() - (days * 86400)
    filtered = [r for r in endp["records"] if r.get("ts", 0) >= cutoff]

    # Distinct ja4s + counts
    distinct: Dict[str, int] = {}
    for r in filtered:
        distinct[r["ja4s"]] = distinct.get(r["ja4s"], 0) + 1

    return {"ok": True, "endpoint": endpoint,
            "count": len(filtered), "records": filtered[:50],
            "distinct_ja4s": distinct,
            "distinct_count": len(distinct)}


def detect_ja4s_change(endpoint: str, new_ja4s: str) -> Dict[str, Any]:
    """Detecta si new_ja4s difiere del ultimo logged (early-warning)."""
    if not endpoint or not new_ja4s:
        return {"ok": False, "error": "args_empty"}

    cfg = get_config()
    d = _load_history()
    endp = d["endpoints"].get(endpoint)
    if not endp or not endp.get("records"):
        return {"ok": True, "changed": False, "reason": "no_baseline",
                "endpoint": endpoint, "new_ja4s": new_ja4s}

    last_ja4s = endp.get("last_ja4s")
    last_ts = endp.get("last_ts", 0)
    age_hours = (time.time() - last_ts) / 3600.0

    if last_ja4s == new_ja4s:
        return {"ok": True, "changed": False, "endpoint": endpoint,
                "current": new_ja4s, "age_hours": round(age_hours, 1)}

    alert = (cfg.get("alert_on_change", True)
             and age_hours >= cfg.get("alert_min_age_hours", 1))

    return {"ok": True, "changed": True, "endpoint": endpoint,
            "previous": last_ja4s, "new": new_ja4s,
            "age_hours": round(age_hours, 1), "alert": alert}


def list_endpoints() -> List[Dict[str, Any]]:
    d = _load_history()
    out = []
    for ep, info in d.get("endpoints", {}).items():
        out.append({
            "endpoint": ep,
            "last_ja4s": info.get("last_ja4s"),
            "first_seen": info.get("first_seen"),
            "records": len(info.get("records", [])),
        })
    return out


def purge_old_records(days: int = 30) -> Dict[str, Any]:
    """Elimina records mas viejos que `days`."""
    cutoff = time.time() - days * 86400
    removed = 0
    with _lock:
        d = _load_history()
        for ep, info in list(d.get("endpoints", {}).items()):
            before = len(info.get("records", []))
            info["records"] = [r for r in info.get("records", [])
                                 if r.get("ts", 0) >= cutoff]
            removed += before - len(info["records"])
            if not info["records"]:
                del d["endpoints"][ep]
        _save_history(d)
    return {"ok": True, "removed": removed,
            "endpoints_now": len(d.get("endpoints", {}))}


# ============================================================
# SELF TEST
# ============================================================
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_has_max_records"] = "max_records_per_endpoint" in cfg
    checks["config_has_purge_days"] = "purge_after_days" in cfg

    # 2. Compute JA4H
    r = compute_ja4h({
        "method": "GET", "http_version": "2",
        "headers": {"User-Agent": "Chrome", "Cookie": "abc=1",
                    "Accept": "text/html", "Accept-Language": "es-ES,es"},
    })
    checks["ja4h_ok"] = r.get("ok") is True
    checks["ja4h_starts_ge"] = r.get("ja4h", "").startswith("ge")  # GET
    checks["ja4h_has_underscore"] = r.get("ja4h", "").count("_") == 2

    # 3. Compute JA4H POST
    r2 = compute_ja4h({
        "method": "POST", "http_version": "2",
        "headers": {"Authorization": "Bearer x", "Content-Type": "application/json"},
    })
    checks["ja4h_post_starts_po"] = r2.get("ja4h", "").startswith("po")

    # 4. Compute JA4S
    r3 = compute_ja4s({
        "protocol": "h2", "tls_version": "1.3", "alpn_chosen": "h2",
        "cipher_chosen_iana": "0x1301",
        "extensions": ["server_name", "key_share", "supported_groups"],
    })
    checks["ja4s_ok"] = r3.get("ok") is True
    checks["ja4s_has_h2"] = "h2" in r3.get("ja4s", "")

    # 5. Log JA4S
    j = r3["ja4s"]
    r4 = log_observed_ja4s("https://www.ticketmaster.com/", j,
                            response_metadata={"status": 200, "server": "AkamaiGHost"})
    checks["log_ok"] = r4.get("ok") is True
    checks["log_records_at_least_1"] = r4.get("records_now", 0) >= 1

    # 6. Get history
    h = get_ja4s_history("https://www.ticketmaster.com/", days=7)
    checks["history_count_ge_1"] = h.get("count", 0) >= 1
    checks["history_distinct_ge_1"] = h.get("distinct_count", 0) >= 1

    # 7. Detect change (no change)
    d_same = detect_ja4s_change("https://www.ticketmaster.com/", j)
    checks["detect_no_change_when_same"] = d_same.get("changed") is False

    # 8. Detect change (different)
    different_ja4s = j[:-4] + "ZZZZ"
    d_diff = detect_ja4s_change("https://www.ticketmaster.com/", different_ja4s)
    checks["detect_change_when_diff"] = d_diff.get("changed") is True

    # 9. List endpoints
    eps = list_endpoints()
    checks["list_endpoints_at_least_1"] = len(eps) >= 1

    # 10. Detect on unknown endpoint
    d_unknown = detect_ja4s_change("https://nope.example.com/", j)
    checks["detect_no_baseline"] = d_unknown.get("reason") == "no_baseline"

    # 11. Compute on bad input
    bad = compute_ja4h(None)
    checks["compute_ja4h_rejects_none"] = bad.get("ok") is False

    bad2 = compute_ja4s("not_a_dict")
    checks["compute_ja4s_rejects_str"] = bad2.get("ok") is False

    # 12. Log + purge old
    # Force an old timestamp into history then purge
    with _lock:
        d_h = _load_history()
        for ep, info in d_h["endpoints"].items():
            for rec in info["records"]:
                rec["ts"] = time.time() - (60 * 86400)  # 60d old
        _save_history(d_h)

    p = purge_old_records(days=30)
    checks["purge_removed_records"] = p.get("removed", 0) >= 1

    # 13. JA4H num_headers cap
    r5 = compute_ja4h({
        "method": "GET",
        "headers": {f"X-H{i}": "v" for i in range(150)},
    })
    checks["ja4h_caps_at_99"] = r5.get("num_headers_counted") == 99

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    d = _load_history()
    return {
        "module": "ja4hs_logging",
        "version": 1,
        "endpoints_tracked": len(d.get("endpoints", {})),
        "config": get_config(),
        "purpose": "JA4H + JA4S server-side fingerprint logging (early-warning)",
    }
