"""trust_tuple_warming.py — (account, ASN, geo, device-fp) tuple warming.

TM 2026 builds multi-dim trust profiles. Accounts que switch IPs/geo/fp
trigger CAPTCHA/ban rápido. Mantener tuple stickiness es CRÍTICO.

API:
  assign_tuple(account_id, country, asn, ip, fingerprint)  → guarda binding
  get_tuple(account_id)
  validate_tuple(account_id, country, asn, ip, fingerprint) → ok/issues
  rotate_tuple(account_id, reason)
  get_warming_progress(account_id) → cookies/sessions count
  self_test()
"""
import threading
import hashlib
import time
from datetime import datetime
from typing import Dict, Optional

import config
import json_atomic

_CONFIG_KEY = "trust_tuples.json"
_lock = threading.RLock()


def _load() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "tuples": {}, "stats": {"assigned": 0, "validations_ok": 0,
                                  "validations_failed": 0, "rotations": 0}})


def _save(d: dict):
    json_atomic.save(config.ruta(_CONFIG_KEY), d)


def _hash_fp(fp_data: Dict) -> str:
    """Genera fingerprint hash a partir de un dict de propiedades device."""
    if not fp_data:
        return ""
    canon = "|".join(f"{k}={v}" for k, v in sorted(fp_data.items()) if v)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def assign_tuple(account_id: str, country: str = "us", asn: str = "",
                  ip: str = "", fingerprint: Optional[Dict] = None) -> Dict:
    """Asigna y guarda el tuple para una cuenta. Idempotente — si ya existe, no sobreescribe."""
    if not account_id:
        return {"ok": False, "error": "account_id requerido"}
    fp_hash = _hash_fp(fingerprint or {})
    with _lock:
        d = _load()
        existing = d["tuples"].get(account_id)
        if existing:
            return {"ok": True, "already_assigned": True, "tuple": existing}
        d["tuples"][account_id] = {
            "account_id": account_id,
            "country": country.lower(),
            "asn": asn,
            "ip_first": ip,
            "fingerprint_hash": fp_hash,
            "fingerprint": fingerprint or {},
            "assigned_at": datetime.now().isoformat(),
            "warming": {"sessions": 0, "cookies_collected": 0,
                         "last_warmed": ""},
            "rotations": 0,
        }
        d["stats"]["assigned"] = d["stats"].get("assigned", 0) + 1
        _save(d)
        return {"ok": True, "tuple": d["tuples"][account_id]}


def get_tuple(account_id: str) -> Optional[Dict]:
    return _load()["tuples"].get(account_id)


def validate_tuple(account_id: str, country: str = "", asn: str = "",
                    ip: str = "", fingerprint: Optional[Dict] = None) -> Dict:
    """Comprueba que el tuple actual coincide con el asignado."""
    with _lock:
        d = _load()
        existing = d["tuples"].get(account_id)
        if not existing:
            return {"ok": True, "first_use": True,
                     "msg": "Sin tuple previo — asignación automática recomendada"}
        issues = []
        if country and country.lower() != existing["country"]:
            issues.append(f"country drift: {existing['country']} → {country.lower()}")
        if asn and existing.get("asn") and asn != existing["asn"]:
            issues.append(f"asn drift: {existing['asn']} → {asn}")
        if fingerprint:
            fp_hash = _hash_fp(fingerprint)
            if fp_hash != existing.get("fingerprint_hash"):
                issues.append(f"fingerprint drift: {existing['fingerprint_hash'][:8]} → {fp_hash[:8]}")
        s = d["stats"]
        if issues:
            s["validations_failed"] = s.get("validations_failed", 0) + 1
        else:
            s["validations_ok"] = s.get("validations_ok", 0) + 1
        _save(d)
        return {"ok": not issues, "issues": issues, "tuple": existing}


def increment_warming(account_id: str, sessions_delta: int = 1,
                       cookies_delta: int = 0) -> Dict:
    """Trackea el progreso de warming de una cuenta."""
    with _lock:
        d = _load()
        t = d["tuples"].get(account_id)
        if not t:
            return {"ok": False, "error": "tuple_not_found"}
        w = t.setdefault("warming", {"sessions": 0, "cookies_collected": 0})
        w["sessions"] = w.get("sessions", 0) + sessions_delta
        w["cookies_collected"] = w.get("cookies_collected", 0) + cookies_delta
        w["last_warmed"] = datetime.now().isoformat()
        _save(d)
        return {"ok": True, "warming": w}


def get_warming_progress(account_id: str) -> Dict:
    """Devuelve progreso de warming + readiness score."""
    t = get_tuple(account_id)
    if not t:
        return {"ok": False, "error": "no_tuple"}
    w = t.get("warming", {})
    sessions = w.get("sessions", 0)
    cookies = w.get("cookies_collected", 0)
    # Score: 100 si > 10 sessions y > 30 cookies
    sess_score = min(100, sessions * 10)
    cookie_score = min(100, cookies * 3)
    score = (sess_score + cookie_score) // 2
    level = ("cold" if score < 30 else
             "warming" if score < 70 else
             "warm" if score < 90 else "hot")
    return {
        "ok": True,
        "account_id": account_id,
        "sessions": sessions,
        "cookies_collected": cookies,
        "score": score,
        "level": level,
        "ready_for_drop": score >= 70,
        "last_warmed": w.get("last_warmed", ""),
    }


def rotate_tuple(account_id: str, reason: str = "") -> Dict:
    """Rota el tuple (uso solo si la cuenta está limpia/quemada). Reset warming."""
    with _lock:
        d = _load()
        t = d["tuples"].get(account_id)
        if not t:
            return {"ok": False, "error": "not_found"}
        t["rotations"] = t.get("rotations", 0) + 1
        t["last_rotation"] = datetime.now().isoformat()
        t["last_rotation_reason"] = reason
        # Reset warming
        t["warming"] = {"sessions": 0, "cookies_collected": 0, "last_warmed": ""}
        d["stats"]["rotations"] = d["stats"].get("rotations", 0) + 1
        _save(d)
        return {"ok": True, "tuple": t}


def get_stats() -> Dict:
    d = _load()
    tuples = d.get("tuples", {})
    levels = {"cold": 0, "warming": 0, "warm": 0, "hot": 0}
    for acc_id in tuples:
        prog = get_warming_progress(acc_id)
        levels[prog.get("level", "cold")] += 1
    return {
        **d.get("stats", {}),
        "active_tuples": len(tuples),
        "by_level": levels,
    }


def self_test() -> Dict:
    r = {"checks": {}}
    # Assign tuple
    assigned = assign_tuple("warm_test_acc", country="es", asn="AS3352", ip="1.2.3.4",
                              fingerprint={"ua": "Chrome/131", "screen": "1920x1080"})
    r["checks"]["assign_ok"] = assigned["ok"]
    r["checks"]["assign_has_fp_hash"] = bool(assigned["tuple"]["fingerprint_hash"])

    # Re-assign idempotent
    assigned2 = assign_tuple("warm_test_acc", country="us", asn="AS999")
    r["checks"]["reassign_idempotent"] = assigned2.get("already_assigned") is True
    r["checks"]["reassign_keeps_es"] = assigned2["tuple"]["country"] == "es"

    # Validate match
    v_ok = validate_tuple("warm_test_acc", country="es", asn="AS3352",
                            fingerprint={"ua": "Chrome/131", "screen": "1920x1080"})
    r["checks"]["validate_match"] = v_ok["ok"]

    # Validate mismatch
    v_bad = validate_tuple("warm_test_acc", country="us", asn="AS999")
    r["checks"]["validate_drift_detected"] = not v_bad["ok"]
    r["checks"]["drift_mentions_country"] = any("country" in i for i in v_bad["issues"])

    # Warming
    increment_warming("warm_test_acc", sessions_delta=3, cookies_delta=10)
    increment_warming("warm_test_acc", sessions_delta=2, cookies_delta=15)
    prog = get_warming_progress("warm_test_acc")
    r["checks"]["warming_tracks_sessions"] = prog["sessions"] == 5
    r["checks"]["warming_tracks_cookies"] = prog["cookies_collected"] == 25
    r["checks"]["warming_returns_score"] = "score" in prog and prog["score"] > 0

    # Rotate
    rotated = rotate_tuple("warm_test_acc", "test_rotation")
    r["checks"]["rotate_ok"] = rotated["ok"]
    r["checks"]["rotate_resets_warming"] = rotated["tuple"]["warming"]["sessions"] == 0

    # Stats
    s = get_stats()
    r["checks"]["stats_count_tuples"] = s.get("active_tuples", 0) >= 1

    # Cleanup
    with _lock:
        d = _load()
        d["tuples"].pop("warm_test_acc", None)
        _save(d)

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "trust_tuple_warming", "version": 1, "stats": get_stats()}
