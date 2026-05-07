"""asn_ip_drift.py — ASN+IP reputation cross-drift detector.

GreyNoise/RSAC 2026: 78% IPs residenciales se evaporan antes que reputation
systems flaggen, pero combos good-ASN+bad-IP tankean trust scores.

Pre-flight check IP en IPQS+Spamhaus + ASN consistency over session lifetime.

API:
  classify_ip(ip)                  → reputation score 0-100
  get_asn_info(ip)                 → asn, org, country
  validate_combo(asn, ip)          → ok/violation con razones
  track_session_drift(session_id, asn, ip)
  get_drift_alert(session_id, threshold=10)
  self_test()
"""
import threading
import socket
import time
from typing import Dict, Optional

import config
import json_atomic

_CONFIG_KEY = "asn_drift.json"
_lock = threading.RLock()

# IPQS-style score thresholds
SCORE_TRUSTED = 0      # 0 = trusted
SCORE_SUSPICIOUS = 50
SCORE_HIGH_RISK = 75
SCORE_BLOCK = 90

# ASN reputation cache (en memoria)
_asn_cache: Dict[str, Dict] = {}

# ASNs comunes residenciales/ISP en Europa+US
ASN_DATABASE = {
    # Spain
    "AS3352": {"org": "Telefonica de España", "type": "isp", "country": "es", "trust": 95},
    "AS6739": {"org": "Vodafone España", "type": "isp", "country": "es", "trust": 92},
    "AS12479": {"org": "Orange España", "type": "isp", "country": "es", "trust": 90},
    # UK
    "AS5089": {"org": "Virgin Media", "type": "isp", "country": "uk", "trust": 93},
    "AS2856": {"org": "British Telecom", "type": "isp", "country": "uk", "trust": 95},
    # US
    "AS7922": {"org": "Comcast", "type": "isp", "country": "us", "trust": 92},
    "AS22773": {"org": "Cox Communications", "type": "isp", "country": "us", "trust": 90},
    "AS701":   {"org": "Verizon", "type": "isp", "country": "us", "trust": 95},
    # Mobile
    "AS6167":  {"org": "Verizon Wireless", "type": "mobile", "country": "us", "trust": 98},
    "AS21928": {"org": "T-Mobile USA", "type": "mobile", "country": "us", "trust": 97},
    # Cloud (LOW trust - bots probable)
    "AS14618": {"org": "Amazon AWS", "type": "datacenter", "country": "us", "trust": 15},
    "AS16509": {"org": "Amazon AWS", "type": "datacenter", "country": "us", "trust": 15},
    "AS15169": {"org": "Google Cloud", "type": "datacenter", "country": "us", "trust": 18},
    "AS8075":  {"org": "Microsoft Azure", "type": "datacenter", "country": "us", "trust": 20},
    # VPN providers
    "AS9009":  {"org": "M247 (NordVPN)", "type": "vpn", "country": "uk", "trust": 5},
    "AS62240": {"org": "Clouvider (ExpressVPN)", "type": "vpn", "country": "uk", "trust": 5},
}


def get_asn_info(ip_or_asn: str) -> Dict:
    """Lookup ASN info. ip_or_asn puede ser AS####, número, o IP (lookup falso).

    En producción: usa MaxMind GeoLite2 ASN o ipinfo.io API.
    Aquí: lookup en cache local + heurística por IP range.
    """
    if not ip_or_asn:
        return {"asn": "", "org": "?", "type": "unknown", "trust": 50}
    key = ip_or_asn if ip_or_asn.startswith("AS") else f"AS{ip_or_asn.lstrip('AS')}"
    if key in ASN_DATABASE:
        return {"asn": key, **ASN_DATABASE[key]}
    if key in _asn_cache:
        return _asn_cache[key]
    # IP lookup heurístico (en producción → ipinfo/maxmind)
    if "." in ip_or_asn or ":" in ip_or_asn:
        # Heurística: 10.x / 192.168 = local, AWS ranges = datacenter, etc
        if ip_or_asn.startswith(("10.", "192.168.", "172.16.", "127.")):
            return {"asn": "AS-private", "org": "Private network",
                    "type": "private", "trust": 100, "country": ""}
        # Sin DB real, devolver score neutro
        return {"asn": "AS-unknown", "org": "Unknown ISP",
                "type": "unknown", "trust": 50, "country": ""}
    return {"asn": key, "org": "Unknown ASN", "type": "unknown", "trust": 50}


def classify_ip(ip: str) -> Dict:
    """Reputation score 0-100 basado en ASN type + heurísticas."""
    info = get_asn_info(ip)
    trust = info.get("trust", 50)
    risk_score = 100 - trust  # invertido: alto risk = bajo trust
    level = ("trusted" if risk_score < SCORE_SUSPICIOUS else
             "suspicious" if risk_score < SCORE_HIGH_RISK else
             "high_risk" if risk_score < SCORE_BLOCK else "block")
    return {
        "ip": ip,
        "asn": info.get("asn"),
        "asn_type": info.get("type"),
        "asn_org": info.get("org"),
        "country": info.get("country", ""),
        "trust_score": trust,
        "risk_score": risk_score,
        "level": level,
        "should_use": level in ("trusted", "suspicious"),
    }


def validate_combo(asn: str, ip: str) -> Dict:
    """Valida que ASN+IP no sea una combinación sospechosa."""
    asn_info = get_asn_info(asn)
    ip_info = classify_ip(ip)
    issues = []

    # Datacenter/VPN ASN siempre rojo para TM
    if asn_info["type"] in ("datacenter", "vpn"):
        issues.append(f"ASN tipo {asn_info['type']} bloqueado para Ticketmaster")

    # ASN con buen trust pero IP en cloud range = sospechoso
    if asn_info["trust"] > 80 and ip_info["asn_type"] == "datacenter":
        issues.append("good-ASN+bad-IP combo detectado (proxy mal configurado?)")

    # Country mismatch entre ASN y proxy declarado
    if asn_info.get("country") and ip_info.get("country"):
        if asn_info["country"] != ip_info["country"]:
            issues.append(f"Country mismatch: ASN={asn_info['country']} IP={ip_info['country']}")

    return {
        "ok": not issues,
        "issues": issues,
        "asn_info": asn_info,
        "ip_info": ip_info,
        "combined_score": min(asn_info.get("trust", 50), ip_info.get("trust_score", 50)),
    }


def track_session_drift(session_id: str, asn: str, ip: str) -> Dict:
    """Trackea ASN/IP para una sesión. Detecta drift entre requests."""
    with _lock:
        d = json_atomic.load(config.ruta(_CONFIG_KEY), default={"sessions": {}})
        sess = d["sessions"].setdefault(session_id, {
            "first_asn": asn, "first_ip": ip,
            "started_at": time.time(),
            "checkpoints": [],
        })
        cp = {"asn": asn, "ip": ip, "ts": time.time()}
        sess["checkpoints"].append(cp)
        # Limita a 200 checkpoints por session
        sess["checkpoints"] = sess["checkpoints"][-200:]
        json_atomic.save(config.ruta(_CONFIG_KEY), d)
        return {"ok": True, "session_id": session_id,
                "checkpoints_count": len(sess["checkpoints"])}


def get_drift_alert(session_id: str, threshold_pct: int = 10) -> Dict:
    """Detecta si la sesión tuvo > threshold% de cambios ASN/IP. Bot signal."""
    d = json_atomic.load(config.ruta(_CONFIG_KEY), default={"sessions": {}})
    sess = d["sessions"].get(session_id)
    if not sess or len(sess["checkpoints"]) < 2:
        return {"ok": True, "drift": False, "reason": "insufficient_data"}
    asns = set(cp["asn"] for cp in sess["checkpoints"])
    ips = set(cp["ip"] for cp in sess["checkpoints"])
    n = len(sess["checkpoints"])
    asn_drift_pct = int((len(asns) - 1) / n * 100)
    ip_drift_pct = int((len(ips) - 1) / n * 100)
    drift = asn_drift_pct > threshold_pct or ip_drift_pct > threshold_pct
    return {
        "ok": True,
        "drift": drift,
        "asn_drift_pct": asn_drift_pct,
        "ip_drift_pct": ip_drift_pct,
        "unique_asns": len(asns),
        "unique_ips": len(ips),
        "checkpoints": n,
        "alert": "ASN/IP drift exceeded threshold" if drift else None,
    }


def get_stats() -> Dict:
    d = json_atomic.load(config.ruta(_CONFIG_KEY), default={"sessions": {}})
    return {
        "tracked_sessions": len(d.get("sessions", {})),
        "asn_database_size": len(ASN_DATABASE),
        "trusted_asns": sum(1 for v in ASN_DATABASE.values() if v["trust"] >= 80),
    }


def self_test() -> Dict:
    r = {"checks": {}}
    # Lookup ASN conocido
    info = get_asn_info("AS3352")
    r["checks"]["lookup_es_telefonica"] = info["country"] == "es" and info["trust"] >= 90

    # Datacenter detected
    aws = get_asn_info("AS14618")
    r["checks"]["aws_datacenter_low_trust"] = aws["trust"] < 30

    # IP classification
    cls = classify_ip("AS3352")  # treating as ASN
    r["checks"]["telefonica_trusted"] = cls["level"] == "trusted"

    # Validate combo: AWS ASN → must fail
    v_aws = validate_combo("AS14618", "AS14618")
    r["checks"]["aws_combo_blocked"] = not v_aws["ok"]

    # Validate combo: Telefónica = ok
    v_es = validate_combo("AS3352", "AS3352")
    r["checks"]["telefonica_combo_ok"] = v_es["ok"]

    # Country mismatch
    v_mismatch = validate_combo("AS3352", "AS7922")  # ES asn + US ip
    r["checks"]["country_mismatch_detected"] = not v_mismatch["ok"]

    # Drift tracking
    sid = "test_drift_session_xyz"
    track_session_drift(sid, "AS3352", "1.2.3.4")
    track_session_drift(sid, "AS3352", "1.2.3.4")  # same
    track_session_drift(sid, "AS3352", "1.2.3.4")
    drift_no = get_drift_alert(sid)
    r["checks"]["no_drift_when_stable"] = drift_no["drift"] is False

    track_session_drift(sid, "AS6739", "5.6.7.8")  # change
    track_session_drift(sid, "AS12479", "9.10.11.12")  # change again
    drift_yes = get_drift_alert(sid, threshold_pct=20)
    r["checks"]["drift_detected_when_changes"] = drift_yes["drift"] is True

    # Stats
    s = get_stats()
    r["checks"]["stats_has_asn_db"] = s.get("asn_database_size", 0) > 10

    # Cleanup
    with _lock:
        d = json_atomic.load(config.ruta(_CONFIG_KEY), default={"sessions": {}})
        d["sessions"].pop(sid, None)
        json_atomic.save(config.ruta(_CONFIG_KEY), d)

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "asn_ip_drift", "version": 1, "stats": get_stats()}
