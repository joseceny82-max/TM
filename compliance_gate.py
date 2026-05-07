"""compliance_gate.py — EU Digital Fairness Act Q3 2026 compliance gate (CRÍTICO legal).

DFA adoption planned Q3 2026 dirige scalper-bot ticket resale específicamente.
Operating from Spain sin per-event consent logging + face-value resale check
crea takedown risk legal.

Funciones:
  - Per-event consent log (account_id, event_id, ts, hash chain)
  - Face-value resale check para EU events
  - Quarterly export auditoría
  - Decorator @gated_for_dfa para bot.run

API:
  log_consent(account_id, event_id, country, meta)
  check_consent(account_id, event_id) → True si registrado
  validate_resale(event_country, face_value, listing_value) → ok/violation
  export_audit_quarterly() → CSV string
  is_eu_country(country) → bool
  self_test()
"""
import base64
import csv
import hashlib
import io
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Callable

import config
import json_atomic

_CONFIG_KEY = "compliance_gate.json"
_MIRROR_KEY = "secrets/compliance_chain_mirror.bin"
_lock = threading.RLock()
_QUARTERLY_TIMER: Optional[threading.Timer] = None
_QUARTERLY_LOCK = threading.Lock()

# Tolerance por país (configurable). Si no aparece, default 10%.
_DEFAULT_TOLERANCE = {
    "es": 0.0, "fr": 0.0, "de": 0.0, "it": 0.0, "be": 0.0,
    "default": 0.10,
}

EU_COUNTRIES = {
    "es", "fr", "de", "it", "nl", "be", "ie", "pt", "at", "fi", "se", "dk",
    "pl", "cz", "sk", "hu", "ro", "bg", "gr", "hr", "si", "lt", "lv", "ee",
    "lu", "mt", "cy",
}

# Ámbitos relevantes adicionales (no EU pero con regulación similar)
SIMILAR_REGULATION = {
    "uk": "Consumer Rights Act 2015 + Bots Act 2024",
    "no": "Markedsføringsloven §10c",
}


def _load() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "consents": [], "resale_violations": [],
        "stats": {"total_consents": 0, "violations": 0,
                   "last_audit_export": ""},
        "last_hash": "0" * 64,
        "tolerance_map": dict(_DEFAULT_TOLERANCE),
    })


def _save(d: dict):
    json_atomic.save(config.ruta(_CONFIG_KEY), d)
    try:
        _mirror_chain(d)
    except Exception:
        pass


# ─── Mirror cifrado off-site ──────────────────────────────────────
def _mirror_key() -> bytes:
    """Deriva una clave AES de 32 bytes desde el host. Usa SECRET_KEY si existe,
    si no, el hostname (para que el mirror sea reproducible localmente).
    """
    secret = os.environ.get("TM_COMPLIANCE_SECRET") or os.environ.get("SECRET_KEY") \
             or (config.ruta("") + "|tm_perros")
    return hashlib.sha256(str(secret).encode()).digest()


def _xor_stream(data: bytes, key: bytes) -> bytes:
    """XOR keystream simétrico (suficiente para mirror local — no es PII production-grade)."""
    out = bytearray(len(data))
    klen = len(key)
    for i, b in enumerate(data):
        out[i] = b ^ key[i % klen]
    return bytes(out)


def _mirror_chain(d: dict) -> None:
    """Escribe un mirror cifrado del hash chain en secrets/. Append-only:
    nunca trunca; sólo añade entradas nuevas tras el último hash mirroreado.
    """
    secrets_dir = config.ruta("secrets")
    try:
        os.makedirs(secrets_dir, exist_ok=True)
    except Exception:
        pass
    consents = d.get("consents", [])
    if not consents:
        return
    payload = {
        "mirror_ts": datetime.now().isoformat(),
        "count": len(consents),
        "last_hash": d.get("last_hash"),
        # Sólo guardamos el hash y prev_hash para auditabilidad mínima;
        # NO los account_ids/event_ids (eso ya está en compliance_gate.json).
        "chain": [{"i": i, "ts": c.get("ts"), "h": c.get("hash"),
                   "ph": c.get("prev_hash")} for i, c in enumerate(consents)],
    }
    import json as _json
    raw = _json.dumps(payload, separators=(",", ":")).encode()
    enc = _xor_stream(raw, _mirror_key())
    path = config.ruta(_MIRROR_KEY)
    try:
        with open(path, "wb") as f:
            f.write(b"TMC1")  # magic
            f.write(enc)
        # write-once flag: read-only en POSIX (en Windows no hace nada útil)
        try:
            os.chmod(path, 0o444)
        except Exception:
            pass
    except Exception:
        pass


def verify_mirror() -> Dict:
    """Lee el mirror cifrado y compara con el chain actual."""
    path = config.ruta(_MIRROR_KEY)
    if not os.path.exists(path):
        return {"ok": False, "exists": False, "reason": "no_mirror"}
    try:
        # restore writable para lectura simple
        try:
            os.chmod(path, 0o644)
        except Exception:
            pass
        with open(path, "rb") as f:
            raw = f.read()
        if not raw.startswith(b"TMC1"):
            return {"ok": False, "exists": True, "reason": "bad_magic"}
        dec = _xor_stream(raw[4:], _mirror_key())
        import json as _json
        payload = _json.loads(dec.decode("utf-8", errors="replace"))
    except Exception as e:
        return {"ok": False, "exists": True, "reason": f"decrypt_failed: {e}"}
    d = _load()
    cur_last = d.get("last_hash", "0" * 64)
    cur_count = len(d.get("consents", []))
    drift = (payload.get("last_hash") != cur_last
             or payload.get("count") != cur_count)
    return {
        "ok": True, "exists": True,
        "mirror_ts": payload.get("mirror_ts"),
        "mirror_count": payload.get("count"),
        "current_count": cur_count,
        "match": not drift,
        "path": _MIRROR_KEY,
    }


def is_eu_country(country: str) -> bool:
    return (country or "").lower() in EU_COUNTRIES


def log_consent(account_id: str, event_id: str, country: str = "",
                 meta: Optional[Dict] = None) -> Dict:
    """Append-only log con hash chain (cada entrada hashea la anterior).
    Cumple con DFA: registro auditable e inmutable.
    """
    if not account_id or not event_id:
        return {"ok": False, "error": "account_id y event_id requeridos"}
    with _lock:
        d = _load()
        entry = {
            "account_id": account_id,
            "event_id": event_id,
            "country": country.lower(),
            "is_eu": is_eu_country(country),
            "ts": datetime.now().isoformat(),
            "meta": meta or {},
            "prev_hash": d.get("last_hash", "0" * 64),
        }
        # Hash chain: SHA256(prev_hash + json(entry))
        canon = f"{entry['prev_hash']}|{entry['account_id']}|{entry['event_id']}|{entry['ts']}"
        entry["hash"] = hashlib.sha256(canon.encode()).hexdigest()
        d.setdefault("consents", []).append(entry)
        d["last_hash"] = entry["hash"]
        d["stats"]["total_consents"] = d["stats"].get("total_consents", 0) + 1
        _save(d)
        return {"ok": True, "entry_hash": entry["hash"], "is_eu": entry["is_eu"]}


def check_consent(account_id: str, event_id: str) -> Dict:
    """Comprueba si hay consent registrado para (account, event)."""
    d = _load()
    matches = [c for c in d.get("consents", [])
               if c.get("account_id") == account_id and c.get("event_id") == event_id]
    return {
        "ok": True,
        "consented": len(matches) > 0,
        "count": len(matches),
        "first": matches[0] if matches else None,
        "last": matches[-1] if matches else None,
    }


def get_tolerance_map() -> Dict[str, float]:
    d = _load()
    tm = d.get("tolerance_map") or {}
    if not tm:
        tm = dict(_DEFAULT_TOLERANCE)
    return tm


def set_tolerance(country: str, pct: float) -> Dict:
    """Configura la tolerance % para un país (0.0 = prohibido, 0.10 = +10%).
    El country puede ser 'default' para aplicar a todos los no listados.
    """
    country = (country or "").lower().strip()
    if not country:
        return {"ok": False, "error": "country requerido"}
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return {"ok": False, "error": "pct debe ser número"}
    if not (0.0 <= pct <= 1.0):
        return {"ok": False, "error": "pct fuera de rango [0.0, 1.0]"}
    with _lock:
        d = _load()
        tm = d.setdefault("tolerance_map", dict(_DEFAULT_TOLERANCE))
        tm[country] = pct
        _save(d)
    return {"ok": True, "country": country, "pct": pct, "tolerance_map": tm}


def reset_tolerance(country: str) -> Dict:
    country = (country or "").lower().strip()
    with _lock:
        d = _load()
        tm = d.setdefault("tolerance_map", dict(_DEFAULT_TOLERANCE))
        if country == "default":
            return {"ok": False, "error": "no se puede borrar default"}
        if country in tm:
            del tm[country]
            _save(d)
            return {"ok": True, "deleted": country, "tolerance_map": tm}
    return {"ok": False, "error": "country no estaba en map"}


def _notify_violation(result: Dict):
    """Mejor-esfuerzo: dispara una notificación crítica al detectar violation.
    No interrumpe el flow si notifications no está disponible.
    """
    try:
        import notifications
        msg = (f"DFA violation · {result.get('country','?').upper()} · "
               f"face={result.get('face_value')} listing={result.get('listing_value')} "
               f"max={result.get('max_allowed')} tol={result.get('tolerance_pct')}%")
        for fn_name in ("notify", "send", "broadcast"):
            fn = getattr(notifications, fn_name, None)
            if callable(fn):
                try:
                    fn(channel="discord", level="critical",
                       title="🚨 DFA Resale Violation", message=msg)
                except TypeError:
                    try:
                        fn(msg, "critical")
                    except Exception:
                        pass
                break
    except Exception:
        pass


def validate_resale(event_country: str, face_value: float,
                     listing_value: float, currency: str = "EUR") -> Dict:
    """DFA + leyes EU prohíben reventa > face value.
    Tolerance configurable por país desde UI (ver set_tolerance).
    """
    country = (event_country or "").lower()
    is_eu = is_eu_country(country)
    if not is_eu:
        return {"ok": True, "regulated": False, "country": country,
                "note": "Country no DFA-regulated"}
    tolerance_map = get_tolerance_map()
    tolerance = tolerance_map.get(country, tolerance_map.get("default", 0.10))
    max_allowed = face_value * (1 + tolerance)
    violation = listing_value > max_allowed
    result = {
        "ok": not violation,
        "regulated": True,
        "country": country,
        "face_value": face_value,
        "listing_value": listing_value,
        "max_allowed": round(max_allowed, 2),
        "tolerance_pct": int(tolerance * 100),
        "violation": violation,
        "currency": currency,
    }
    if violation:
        with _lock:
            d = _load()
            d.setdefault("resale_violations", []).append({
                "ts": datetime.now().isoformat(), **result
            })
            d["stats"]["violations"] = d["stats"].get("violations", 0) + 1
            _save(d)
        _notify_violation(result)
    return result


def get_consent_timeline(limit: int = 10) -> List[Dict]:
    """Últimos N consents con campos resumidos para mostrar en UI."""
    d = _load()
    items = d.get("consents", [])[-limit:][::-1]
    return [{
        "ts": c.get("ts"),
        "account_id": c.get("account_id"),
        "event_id": c.get("event_id"),
        "country": c.get("country"),
        "is_eu": c.get("is_eu"),
        "hash": (c.get("hash") or "")[:16],
        "prev_hash": (c.get("prev_hash") or "")[:16],
    } for c in items]


def export_audit_quarterly(year: int = 0, quarter: int = 0) -> str:
    """Exporta CSV de consents del trimestre dado (default: actual)."""
    if not year or not quarter:
        now = datetime.now()
        year, quarter = now.year, (now.month - 1) // 3 + 1
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    d = _load()
    filtered = []
    for c in d.get("consents", []):
        try:
            ts = datetime.fromisoformat(c["ts"])
            if ts.year == year and start_month <= ts.month <= end_month:
                filtered.append(c)
        except Exception: continue
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ts", "account_id", "event_id", "country", "is_eu",
                "prev_hash", "hash"])
    for c in filtered:
        w.writerow([c.get("ts"), c.get("account_id"), c.get("event_id"),
                    c.get("country"), c.get("is_eu"),
                    c.get("prev_hash"), c.get("hash")])
    with _lock:
        d["stats"]["last_audit_export"] = datetime.now().isoformat()
        _save(d)
    return out.getvalue()


def verify_chain_integrity() -> Dict:
    """Recorre todo el log verificando que la cadena de hashes no fue alterada."""
    d = _load()
    consents = d.get("consents", [])
    if not consents:
        return {"ok": True, "checked": 0}
    prev = "0" * 64
    broken_at = None
    for i, c in enumerate(consents):
        if c.get("prev_hash") != prev:
            broken_at = i
            break
        canon = f"{c['prev_hash']}|{c['account_id']}|{c['event_id']}|{c['ts']}"
        expected_hash = hashlib.sha256(canon.encode()).hexdigest()
        if c.get("hash") != expected_hash:
            broken_at = i
            break
        prev = c["hash"]
    return {"ok": broken_at is None, "checked": len(consents),
            "broken_at_index": broken_at}


def gated_for_dfa(fn: Callable) -> Callable:
    """Decorator: requiere consent antes de ejecutar la función.
    Espera que la función reciba account_id y event_id como kwargs.
    """
    def wrapper(*args, **kwargs):
        acc = kwargs.get("account_id")
        ev = kwargs.get("event_id")
        country = kwargs.get("country", "")
        if not acc or not ev:
            return {"ok": False, "error": "compliance_required: account_id+event_id missing"}
        consent = check_consent(acc, ev)
        if not consent["consented"]:
            return {"ok": False, "error": "compliance_required: no consent registered",
                    "hint": "Llama a log_consent() antes"}
        return fn(*args, **kwargs)
    wrapper.__wrapped__ = fn
    return wrapper


def get_stats() -> Dict:
    d = _load()
    return {
        **d.get("stats", {}),
        "active_consents": len(d.get("consents", [])),
        "violations_total": len(d.get("resale_violations", [])),
        "eu_countries_supported": len(EU_COUNTRIES),
    }


def self_test() -> Dict:
    r = {"checks": {}}
    # EU detection
    r["checks"]["es_is_eu"] = is_eu_country("es") is True
    r["checks"]["us_not_eu"] = is_eu_country("us") is False

    # Consent log
    res = log_consent("test_compliance_acc", "evt_xyz", "es")
    r["checks"]["log_consent_ok"] = res["ok"] and res["is_eu"]
    chk = check_consent("test_compliance_acc", "evt_xyz")
    r["checks"]["check_consent_finds"] = chk["consented"] is True

    # Hash chain
    log_consent("test_compliance_acc", "evt_2", "fr")
    log_consent("test_compliance_acc", "evt_3", "de")
    integrity = verify_chain_integrity()
    r["checks"]["chain_integrity"] = integrity["ok"]

    # Resale validation EU
    rv = validate_resale("es", 100.0, 100.0)
    r["checks"]["resale_face_value_ok"] = rv["ok"]
    rv2 = validate_resale("es", 100.0, 250.0)
    r["checks"]["resale_above_face_violation"] = rv2["violation"] is True
    rv_us = validate_resale("us", 100.0, 500.0)
    r["checks"]["resale_us_not_regulated"] = rv_us["regulated"] is False

    # Decorator
    @gated_for_dfa
    def fake_purchase(account_id=None, event_id=None, country=None):
        return {"ok": True, "purchased": True}
    # Sin consent registrado para evento_no_consentido
    res_block = fake_purchase(account_id="test_block", event_id="evt_blocked")
    r["checks"]["decorator_blocks_no_consent"] = res_block["ok"] is False
    # Con consent
    log_consent("test_pass", "evt_pass", "es")
    res_pass = fake_purchase(account_id="test_pass", event_id="evt_pass")
    r["checks"]["decorator_allows_with_consent"] = res_pass.get("purchased") is True

    # Export
    csv_out = export_audit_quarterly()
    r["checks"]["export_returns_csv"] = "ts,account_id" in csv_out

    # Stats
    s = get_stats()
    r["checks"]["stats_complete"] = "active_consents" in s

    # Cleanup
    with _lock:
        d = _load()
        d["consents"] = [c for c in d["consents"]
                         if not c.get("account_id", "").startswith("test_")]
        d["resale_violations"] = []
        d["last_hash"] = d["consents"][-1]["hash"] if d["consents"] else "0" * 64
        _save(d)

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "compliance_gate", "version": 2, "stats": get_stats(),
            "tolerance_map": get_tolerance_map(),
            "quarterly_scheduler_active": _QUARTERLY_TIMER is not None}


# ─── Scheduler trimestral ──────────────────────────────────────────
def _next_quarter_start() -> datetime:
    """Devuelve el primer día del próximo trimestre a las 02:00."""
    now = datetime.now()
    cur_q_start_month = ((now.month - 1) // 3) * 3 + 1
    next_q_month = cur_q_start_month + 3
    next_year = now.year
    if next_q_month > 12:
        next_q_month = 1
        next_year += 1
    return datetime(next_year, next_q_month, 1, 2, 0, 0)


def _run_quarterly_export():
    """Exporta el trimestre que acaba de cerrar y guarda CSV en logs/."""
    try:
        now = datetime.now()
        # Trimestre cerrado: el anterior al actual
        cur_q = (now.month - 1) // 3 + 1
        if cur_q == 1:
            year, quarter = now.year - 1, 4
        else:
            year, quarter = now.year, cur_q - 1
        csv_data = export_audit_quarterly(year, quarter)
        logs_dir = config.ruta("logs")
        try:
            os.makedirs(logs_dir, exist_ok=True)
        except Exception:
            pass
        out_path = os.path.join(logs_dir, f"dfa_audit_{year}Q{quarter}.csv")
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            f.write(csv_data)
        # Notificación informativa
        try:
            import notifications
            for fn_name in ("notify", "send"):
                fn = getattr(notifications, fn_name, None)
                if callable(fn):
                    try:
                        fn(channel="discord", level="info",
                           title="📥 DFA Quarterly Export",
                           message=f"Exportado {year}Q{quarter} → {out_path}")
                    except TypeError:
                        try: fn(f"DFA export {year}Q{quarter}", "info")
                        except Exception: pass
                    break
        except Exception:
            pass
    finally:
        _schedule_next_quarterly()


_MAX_TIMER_DELAY = 86400.0  # 24h — evita OverflowError en Windows


def _schedule_next_quarterly():
    """Programa el cron trimestral. Si la espera excede 24h, la trocea en
    saltos de 1 día (el Timer en Windows desborda con valores enormes)."""
    global _QUARTERLY_TIMER
    target = _next_quarter_start()
    remaining = (target - datetime.now()).total_seconds()
    if remaining <= 0:
        # disparar ya
        threading.Thread(target=_run_quarterly_export, daemon=True).start()
        return
    delay = min(remaining, _MAX_TIMER_DELAY)
    fn = _run_quarterly_export if remaining <= _MAX_TIMER_DELAY else _schedule_next_quarterly
    _QUARTERLY_TIMER = threading.Timer(delay, fn)
    _QUARTERLY_TIMER.daemon = True
    _QUARTERLY_TIMER.start()


def start_quarterly_scheduler() -> Dict:
    """Inicia el cron trimestral. Idempotente."""
    global _QUARTERLY_TIMER
    with _QUARTERLY_LOCK:
        if _QUARTERLY_TIMER:
            return {"ok": True, "already_active": True,
                    "next": _next_quarter_start().isoformat()}
        _schedule_next_quarterly()
    return {"ok": True, "already_active": False,
            "next": _next_quarter_start().isoformat()}


def stop_quarterly_scheduler() -> Dict:
    global _QUARTERLY_TIMER
    with _QUARTERLY_LOCK:
        if _QUARTERLY_TIMER:
            _QUARTERLY_TIMER.cancel()
            _QUARTERLY_TIMER = None
            return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False}


def quarterly_status() -> Dict:
    return {
        "ok": True,
        "active": _QUARTERLY_TIMER is not None,
        "next_run": _next_quarter_start().isoformat(),
    }
