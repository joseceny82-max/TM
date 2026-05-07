"""queueit_visitorsession.py — Queue-it `Queue-it-visitorsession` cookie handler (SOTA P1-10).

Queue-it (Marzo 2026) anunció el nuevo cookie `Queue-it-visitorsession` que
**cross-correlaciona Queue IDs del mismo visitor**. Sin manejarlo, las cuentas
quemadas (burner) son detectadas como mismo visitor (mismo `vsid`) y bloqueadas.

Patrón:
  1. Cliente entra a queue → Queue-it asigna `Queue-it-visitorsession=<vsid>`
  2. Cliente vuelve a entrar (otro queue, otro evento) → mismo vsid sigue
  3. Queue-it correlaciona: vsid X estuvo en evento A, B, C → posible bot/scalper
  4. Si patrón sospechoso → ban del vsid (todas las cuentas asociadas)

Solución:
  - Generar `vsid` aleatorio único POR ACCOUNT_ID (no compartir entre cuentas)
  - Persistir mapping account_id → vsid (cookie jar separado)
  - Rotar vsid cuando: account suspended, vsid age > 24h, o demand explícito
  - **Aislar storage**: cada cuenta tiene cookie jar separado → no leak

API:
  generate_vsid(account_id) → vsid_str
  get_vsid(account_id) → vsid_str | None
  rotate_vsid(account_id, reason) → new_vsid
  parse_visitorsession_cookie(cookie_str) → {vsid, version, valid}
  detect_visitorsession_in_response(headers, body) → vsid | None
  build_cookie_for_account(account_id) → "Queue-it-visitorsession=...; ..."
  isolate_cookie_jar(account_id) → jar_path
  detect_correlation_risk(account_id) → {risk_level, reasons}
  get_metrics() → dict
  self_test()
"""
from __future__ import annotations
import os, re, time, json, hashlib, secrets, threading, base64
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import config
import json_atomic


_CONFIG_KEY = "queueit_vsid_config.json"
_VSIDS_KEY = "queueit_vsids.json"
_LOG_KEY = "queueit_vsid_log.json"
_lock = threading.RLock()
_metrics = {
    "vsids_generated": 0,
    "vsids_rotated": 0,
    "vsids_extracted_from_response": 0,
    "vsid_lookups_hit": 0,
    "vsid_lookups_miss": 0,
    "correlation_risks_detected": 0,
}

# Vsids persistencia: { account_id: { vsid, generated_at, last_used_at, rotation_count, used_in_queues: [...] } }


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "vsid_max_age_hours": 24,             # rotar a las 24h por safety
        "vsid_format": "uuid_v4",             # uuid_v4 | hex32 | base64
        "max_queues_per_vsid": 5,             # > 5 = correlation risk
        "auto_isolate_jars": True,
        "burn_vsid_on_account_suspend": True,
        "log_max_entries": 500,
        "queueit_domains": [
            "queue-it.net", "queue-ticketmaster.com", "queue.ticketmaster.com",
            "queueitlm.queue-it.com",
        ],
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# VSID GENERATION & PERSISTENCE
# ═══════════════════════════════════════════════════════════════
def _load_vsids() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_VSIDS_KEY), default={"by_account": {}})


def _save_vsids(d: Dict[str, Any]):
    json_atomic.save(config.ruta(_VSIDS_KEY), d)


def _generate_vsid_string(format_type: str = "uuid_v4") -> str:
    """Genera un vsid aleatorio según formato configurado.

    Queue-it usa típicamente formato UUID v4 lowercase.
    """
    if format_type == "uuid_v4":
        # Format: 8-4-4-4-12 hex chars con hyphens
        b = secrets.token_bytes(16)
        # Set version 4 + variant 10
        b = bytearray(b)
        b[6] = (b[6] & 0x0F) | 0x40  # version 4
        b[8] = (b[8] & 0x3F) | 0x80  # variant 10
        h = b.hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    elif format_type == "hex32":
        return secrets.token_hex(16)
    elif format_type == "base64":
        return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")
    else:
        return secrets.token_hex(16)


def generate_vsid(account_id: str,
                   force: bool = False) -> Dict[str, Any]:
    """Genera (o recupera) vsid persistente para un account_id.

    Si force=False y ya existe vsid no expirado → retorna el existente.
    Si force=True → genera uno nuevo (rotation).
    """
    if not account_id: return {"ok": False, "error": "account_id_required"}

    cfg = get_config()
    with _lock:
        d = _load_vsids()
        existing = d["by_account"].get(account_id)

        if existing and not force:
            # Check age
            age_h = (time.time() - existing.get("generated_ts", 0)) / 3600
            if age_h < cfg["vsid_max_age_hours"]:
                # Ok — reuse
                existing["last_used_at"] = datetime.now().isoformat()
                existing["last_used_ts"] = time.time()
                _save_vsids(d)
                _metrics["vsid_lookups_hit"] += 1
                return {"ok": True, "vsid": existing["vsid"],
                        "age_hours": round(age_h, 2),
                        "is_new": False, "rotation_count": existing.get("rotation_count", 0)}

        # Generate new
        vsid = _generate_vsid_string(cfg["vsid_format"])
        rotation_count = (existing.get("rotation_count", 0) + 1) if existing else 0
        d["by_account"][account_id] = {
            "vsid": vsid,
            "format": cfg["vsid_format"],
            "generated_at": datetime.now().isoformat(),
            "generated_ts": time.time(),
            "last_used_at": datetime.now().isoformat(),
            "last_used_ts": time.time(),
            "rotation_count": rotation_count,
            "used_in_queues": [],
        }
        _save_vsids(d)
        _metrics["vsids_generated"] += 1
        if existing:
            _metrics["vsids_rotated"] += 1

    return {"ok": True, "vsid": vsid, "is_new": True,
            "rotation_count": rotation_count}


def get_vsid(account_id: str) -> Optional[str]:
    """Devuelve vsid asociado a account_id, o None si no existe/expirado."""
    cfg = get_config()
    with _lock:
        d = _load_vsids()
        existing = d["by_account"].get(account_id)
        if not existing:
            _metrics["vsid_lookups_miss"] += 1
            return None
        age_h = (time.time() - existing.get("generated_ts", 0)) / 3600
        if age_h >= cfg["vsid_max_age_hours"]:
            _metrics["vsid_lookups_miss"] += 1
            return None
        _metrics["vsid_lookups_hit"] += 1
        return existing["vsid"]


def rotate_vsid(account_id: str, reason: str = "manual") -> Dict[str, Any]:
    """Fuerza rotación del vsid (account suspended, demand explícito, etc)."""
    r = generate_vsid(account_id, force=True)
    if r.get("ok"):
        _log_event({"op": "rotate", "account_id": account_id, "reason": reason,
                    "new_vsid_prefix": r["vsid"][:12]})
    return {**r, "rotated_reason": reason}


def burn_vsid(account_id: str, reason: str = "account_suspended") -> Dict[str, Any]:
    """Elimina completamente el vsid de account (no rotación, BURN)."""
    with _lock:
        d = _load_vsids()
        existing = d["by_account"].pop(account_id, None)
        _save_vsids(d)
    if existing:
        _log_event({"op": "burn", "account_id": account_id, "reason": reason,
                    "vsid_prefix": existing["vsid"][:12]})
    return {"ok": existing is not None, "account_id": account_id}


def record_queue_use(account_id: str, queue_id: str,
                     event_id: Optional[str] = None) -> Dict[str, Any]:
    """Registra que account_id usó este vsid en queue_id."""
    cfg = get_config()
    with _lock:
        d = _load_vsids()
        ent = d["by_account"].get(account_id)
        if not ent: return {"ok": False, "error": "vsid_not_found"}
        ent["used_in_queues"] = ent.get("used_in_queues", [])
        ent["used_in_queues"].append({
            "queue_id": queue_id, "event_id": event_id,
            "ts": datetime.now().isoformat(),
        })
        ent["used_in_queues"] = ent["used_in_queues"][-20:]  # cap
        _save_vsids(d)

        if len(ent["used_in_queues"]) > cfg["max_queues_per_vsid"]:
            _metrics["correlation_risks_detected"] += 1
            return {"ok": True, "warning": "correlation_risk",
                    "queue_count": len(ent["used_in_queues"])}
    return {"ok": True, "queue_count": len(ent["used_in_queues"])}


# ═══════════════════════════════════════════════════════════════
# COOKIE PARSING
# ═══════════════════════════════════════════════════════════════
VSID_COOKIE_REGEX = re.compile(
    r"Queue-it-visitorsession=([A-Za-z0-9\-_]+)", re.IGNORECASE
)


def parse_visitorsession_cookie(cookie_str: str) -> Dict[str, Any]:
    """Parsea Queue-it-visitorsession cookie y valida formato vsid."""
    if not cookie_str: return {"valid": False, "error": "empty"}

    m = VSID_COOKIE_REGEX.search(cookie_str)
    if not m: return {"valid": False, "error": "cookie_not_found"}

    vsid = m.group(1)
    # Validar formato
    is_uuid = bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                              r"[0-9a-f]{4}-[0-9a-f]{12}$", vsid, re.IGNORECASE))
    is_hex32 = bool(re.match(r"^[0-9a-f]{32}$", vsid, re.IGNORECASE))
    is_b64 = bool(re.match(r"^[A-Za-z0-9_\-]{20,}$", vsid)) and not (is_uuid or is_hex32)

    fmt = "uuid_v4" if is_uuid else ("hex32" if is_hex32 else
                                        ("base64" if is_b64 else "unknown"))

    return {
        "valid": fmt != "unknown",
        "vsid": vsid,
        "format": fmt,
        "length": len(vsid),
    }


def detect_visitorsession_in_response(response_headers: Optional[Dict[str, str]] = None,
                                        response_body: Optional[str] = None,
                                        set_cookie_header: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Extrae el vsid de los headers Set-Cookie o body."""
    # 1. Set-Cookie header
    if set_cookie_header is None and response_headers:
        for k, v in (response_headers.items() if hasattr(response_headers, "items") else []):
            if k.lower() == "set-cookie" and "queue-it-visitorsession" in v.lower():
                set_cookie_header = v
                break

    if set_cookie_header:
        candidates = re.split(r"[\r\n,]+", set_cookie_header)
        for c in candidates:
            if "queue-it-visitorsession" in c.lower():
                r = parse_visitorsession_cookie(c)
                if r.get("valid"):
                    with _lock:
                        _metrics["vsids_extracted_from_response"] += 1
                    return {**r, "source": "set_cookie"}

    # 2. Body
    if response_body:
        m = VSID_COOKIE_REGEX.search(response_body)
        if m:
            r = parse_visitorsession_cookie(m.group(0))
            if r.get("valid"):
                with _lock:
                    _metrics["vsids_extracted_from_response"] += 1
                return {**r, "source": "body"}

    return None


# ═══════════════════════════════════════════════════════════════
# COOKIE BUILDER (para inyectar)
# ═══════════════════════════════════════════════════════════════
def build_cookie_for_account(account_id: str,
                                domain: str = "queue-it.net",
                                ttl_s: int = 3600) -> Dict[str, Any]:
    """Construye string cookie listo para Set-Cookie / Cookie header."""
    vsid = get_vsid(account_id)
    if not vsid:
        r = generate_vsid(account_id)
        if not r.get("ok"): return {"ok": False, "error": r.get("error")}
        vsid = r["vsid"]

    expires = (datetime.now() + timedelta(seconds=ttl_s)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")
    cookie_str = (
        f"Queue-it-visitorsession={vsid}; "
        f"Domain={domain}; Path=/; Expires={expires}; "
        "Secure; HttpOnly; SameSite=Lax"
    )
    return {
        "ok": True,
        "vsid": vsid,
        "cookie_string": cookie_str,
        "domain": domain,
        "ttl_s": ttl_s,
    }


# ═══════════════════════════════════════════════════════════════
# COOKIE JAR ISOLATION
# ═══════════════════════════════════════════════════════════════
def isolate_cookie_jar(account_id: str) -> Dict[str, Any]:
    """Devuelve path a un cookie jar exclusivo de este account.

    Usar en el cookie store del HTTP client para que NO se mezclen cookies entre
    cuentas (que es lo que Queue-it usa para correlate).
    """
    if not account_id: return {"ok": False, "error": "account_id_required"}
    safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", account_id)[:60]
    base_dir = os.path.join(
        os.path.dirname(config.ruta(_VSIDS_KEY)),
        "queueit_jars"
    )
    os.makedirs(base_dir, exist_ok=True)
    jar_path = os.path.join(base_dir, f"{safe_id}.jar")
    return {"ok": True, "jar_path": jar_path, "account_id": account_id}


# ═══════════════════════════════════════════════════════════════
# CORRELATION RISK ASSESSMENT
# ═══════════════════════════════════════════════════════════════
def detect_correlation_risk(account_id: str) -> Dict[str, Any]:
    """Analiza si el vsid de account_id está en zona de correlation risk."""
    cfg = get_config()
    with _lock:
        d = _load_vsids()
        ent = d["by_account"].get(account_id)
    if not ent: return {"risk_level": "none", "no_vsid": True}

    reasons = []
    risk_level = "low"

    # 1. Age
    age_h = (time.time() - ent.get("generated_ts", 0)) / 3600
    if age_h > cfg["vsid_max_age_hours"]:
        reasons.append(f"vsid_too_old: {round(age_h, 1)}h > {cfg['vsid_max_age_hours']}h")
        risk_level = "high"

    # 2. Queue count
    queues = ent.get("used_in_queues", [])
    if len(queues) > cfg["max_queues_per_vsid"]:
        reasons.append(f"too_many_queues: {len(queues)} > {cfg['max_queues_per_vsid']}")
        risk_level = "critical" if len(queues) > cfg["max_queues_per_vsid"] * 2 else "high"

    # 3. Distinct events
    distinct_events = list({q.get("event_id") for q in queues if q.get("event_id")})
    if len(distinct_events) > 3:
        reasons.append(f"multi_event_burn: {len(distinct_events)} distinct events")
        if risk_level not in ("critical", "high"):
            risk_level = "medium"

    return {
        "risk_level": risk_level,
        "reasons": reasons,
        "age_hours": round(age_h, 2),
        "queues_used": len(queues),
        "distinct_events": len(distinct_events),
        "rotation_count": ent.get("rotation_count", 0),
        "should_rotate": risk_level in ("high", "critical"),
    }


def auto_rotate_risky_vsids() -> Dict[str, Any]:
    """Recorre todos los account vsids y rota los high/critical."""
    rotated = []
    with _lock:
        d = _load_vsids()
        accounts = list(d["by_account"].keys())
    for acc in accounts:
        risk = detect_correlation_risk(acc)
        if risk.get("should_rotate"):
            r = rotate_vsid(acc, reason=f"auto_rotate_{risk['risk_level']}")
            if r.get("ok"):
                rotated.append({"account_id": acc,
                                  "old_risk": risk["risk_level"],
                                  "rotation_count": r["rotation_count"]})
    return {"ok": True, "rotated_count": len(rotated), "rotated": rotated}


# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
def _log_event(evt: Dict[str, Any]):
    try:
        evt["ts"] = datetime.now().isoformat()
        with _lock:
            d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
            d["log"].insert(0, evt)
            d["log"] = d["log"][:get_config()["log_max_entries"]]
            json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


def get_log(limit: int = 50) -> List[Dict[str, Any]]:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
    return d.get("log", [])[:limit]


# ═══════════════════════════════════════════════════════════════
# METRICS / INFO
# ═══════════════════════════════════════════════════════════════
def get_metrics() -> Dict[str, Any]:
    with _lock:
        d = _load_vsids()
        total_accounts = len(d.get("by_account", {}))
    return {
        **_metrics,
        "total_accounts_with_vsid": total_accounts,
    }


def list_accounts(limit: int = 100) -> List[Dict[str, Any]]:
    d = _load_vsids()
    out = []
    cfg = get_config()
    for acc, ent in list(d.get("by_account", {}).items())[:limit]:
        age_h = (time.time() - ent.get("generated_ts", 0)) / 3600
        out.append({
            "account_id": acc,
            "vsid_short": ent["vsid"][:12] + "...",
            "age_hours": round(age_h, 1),
            "queues_count": len(ent.get("used_in_queues", [])),
            "rotation_count": ent.get("rotation_count", 0),
            "expired": age_h >= cfg["vsid_max_age_hours"],
        })
    return out


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_max_age"] = cfg["vsid_max_age_hours"] == 24
    checks["config_max_queues"] = cfg["max_queues_per_vsid"] >= 5

    # 2. Generate vsid format uuid_v4
    vsid_str = _generate_vsid_string("uuid_v4")
    checks["uuid_format"] = bool(re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        vsid_str, re.IGNORECASE))

    # 3. Generate hex32 + base64
    checks["hex32_format"] = len(_generate_vsid_string("hex32")) == 32
    checks["b64_works"] = len(_generate_vsid_string("base64")) >= 20

    # 4. generate_vsid for account
    acc_id = "test_acc_" + secrets.token_hex(4)
    r = generate_vsid(acc_id)
    checks["generate_ok"] = r.get("ok") is True
    checks["generate_is_new"] = r.get("is_new") is True
    checks["vsid_returned"] = "vsid" in r

    # 5. Get vsid
    v = get_vsid(acc_id)
    checks["get_vsid_works"] = v == r["vsid"]

    # 6. Re-generate same account → reuse
    r2 = generate_vsid(acc_id)
    checks["regenerate_reuse"] = r2.get("vsid") == r["vsid"]
    checks["regenerate_not_new"] = r2.get("is_new") is False

    # 7. Force rotate
    r3 = generate_vsid(acc_id, force=True)
    checks["force_rotate_new"] = r3.get("vsid") != r["vsid"]
    checks["rotation_count_inc"] = r3.get("rotation_count") >= 1

    # 8. rotate_vsid wrapper
    old_vsid = r3["vsid"]
    r4 = rotate_vsid(acc_id, reason="test")
    checks["rotate_wrapper_ok"] = r4.get("ok") is True
    checks["rotate_changed"] = r4.get("vsid") != old_vsid

    # 9. Burn
    r5 = burn_vsid(acc_id, reason="test_burn")
    checks["burn_ok"] = r5.get("ok") is True
    checks["after_burn_no_vsid"] = get_vsid(acc_id) is None

    # 10. Parse cookie valid
    r = parse_visitorsession_cookie(
        "Queue-it-visitorsession=550e8400-e29b-41d4-a716-446655440000; Path=/")
    checks["parse_uuid_valid"] = r.get("valid") is True
    checks["parse_uuid_format"] = r.get("format") == "uuid_v4"

    # 11. Parse hex32
    r = parse_visitorsession_cookie(
        "Queue-it-visitorsession=" + "a" * 32)
    checks["parse_hex32_valid"] = r.get("valid") is True

    # 12. Parse invalid
    r = parse_visitorsession_cookie("garbage cookie")
    checks["parse_invalid_returns_false"] = r.get("valid") is False

    # 13. Detect from headers
    fake_vsid = "550e8400-e29b-41d4-a716-446655440000"
    headers = {"Set-Cookie": f"Queue-it-visitorsession={fake_vsid}; Path=/"}
    r = detect_visitorsession_in_response(response_headers=headers)
    checks["detect_from_headers"] = r is not None and r.get("vsid") == fake_vsid

    # 14. Detect from body
    body = f'document.cookie = "Queue-it-visitorsession={fake_vsid}; Path=/";'
    r = detect_visitorsession_in_response(response_body=body)
    checks["detect_from_body"] = r is not None

    # 15. Detect no cookie
    r = detect_visitorsession_in_response(response_headers={"X": "y"})
    checks["detect_no_cookie_returns_none"] = r is None

    # 16. Build cookie
    acc_b = "test_acc_b_" + secrets.token_hex(4)
    r = build_cookie_for_account(acc_b, domain="queue.test.com")
    checks["build_cookie_ok"] = r.get("ok") is True
    checks["build_cookie_has_string"] = "Queue-it-visitorsession=" in (r.get("cookie_string") or "")
    checks["build_cookie_has_domain"] = "queue.test.com" in (r.get("cookie_string") or "")

    # 17. Isolate jar
    r = isolate_cookie_jar(acc_b)
    checks["isolate_jar_ok"] = r.get("ok") is True
    checks["jar_path_safe"] = r and ".." not in r.get("jar_path", "")

    # 18. Record queue use
    r = record_queue_use(acc_b, "queue_001", "event_alpha")
    checks["record_queue_ok"] = r.get("ok") is True

    # 19. Correlation risk — just one queue, low risk
    r = detect_correlation_risk(acc_b)
    checks["risk_low_initial"] = r.get("risk_level") == "low"

    # 20. Push to high risk: many queues
    for i in range(8):
        record_queue_use(acc_b, f"queue_high_{i}", f"event_{i}")
    r = detect_correlation_risk(acc_b)
    checks["risk_high_or_critical"] = r.get("risk_level") in ("high", "critical")
    checks["risk_should_rotate"] = r.get("should_rotate") is True

    # 21. Auto-rotate risky
    r = auto_rotate_risky_vsids()
    checks["auto_rotate_runs"] = r.get("ok") is True
    checks["auto_rotated_acc_b"] = any(rot["account_id"] == acc_b for rot in r.get("rotated", []))

    # 22. List accounts
    accs = list_accounts(limit=50)
    checks["list_accounts_returns_list"] = isinstance(accs, list)

    # 23. Metrics
    m = get_metrics()
    checks["metrics_generated_count"] = m["vsids_generated"] >= 2
    checks["metrics_rotated_count"] = m["vsids_rotated"] >= 1

    # Cleanup
    burn_vsid(acc_b, reason="self_test_cleanup")

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "queueit_visitorsession",
        "version": 1,
        "metrics": get_metrics(),
        "purpose": "Queue-it visitorsession cookie handler (SOTA P1-10, March 2026)",
    }
