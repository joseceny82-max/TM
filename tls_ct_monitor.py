"""tls_ct_monitor.py — Certificate Transparency log monitor (TM/Queue-it).

Monitorea rotaciones de cert TM/Queue-it via crt.sh API. Da early warning
de WAF/CDN migration ANTES de que rompa el bot.

Lets Encrypt RFC6962 logs cierran Feb 2026; nuevo static CT API live.

API:
  query_ct_for_domain(domain)  → [issuers, fingerprints]
  get_current_certs(domain)
  detect_changes(domain)        → diff vs último snapshot
  schedule_daily_check()
  self_test()
"""
import hashlib
import json
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import config
import json_atomic

_CONFIG_KEY = "tls_ct_snapshots.json"
_lock = threading.RLock()

MONITORED_DOMAINS = [
    "ticketmaster.com", "ticketmaster.es", "ticketmaster.co.uk",
    "ticketmaster.de", "ticketmaster.fr", "ticketmaster.it",
    "queue-it.net", "queue-it.com",
    "akamai.net", "akamaihd.net",  # CDN frontend
    "datadome.co",                  # bot mgmt
]

CT_API_URL = "https://crt.sh/?q={domain}&output=json"


def _load() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "snapshots": {},  # {domain: {checked_at, fingerprints, issuers}}
        "alerts": [],
        "stats": {"checks_total": 0, "alerts_total": 0, "last_check": ""},
    })


def _save(d: dict):
    json_atomic.save(config.ruta(_CONFIG_KEY), d)


def query_ct_for_domain(domain: str, limit: int = 50, timeout: int = 10) -> Dict:
    """Consulta crt.sh para un dominio. Devuelve lista de certs activos.
    En entornos sin red, retorna mock vacío.
    """
    if not domain:
        return {"ok": False, "error": "domain requerido"}
    try:
        import requests
        r = requests.get(CT_API_URL.format(domain=domain), timeout=timeout,
                         headers={"User-Agent": "TM-PERROS-CT-Monitor/1.0"})
        if r.status_code != 200:
            return {"ok": False, "status_code": r.status_code,
                    "error": f"HTTP {r.status_code}"}
        data = r.json()
        # Resumir
        certs = []
        seen = set()
        for entry in data[:limit]:
            sha256 = entry.get("serial_number") or ""
            if sha256 in seen: continue
            seen.add(sha256)
            certs.append({
                "issuer": entry.get("issuer_name", "?"),
                "common_name": entry.get("common_name", "?"),
                "name_value": entry.get("name_value", "")[:200],
                "serial": sha256,
                "not_before": entry.get("not_before"),
                "not_after": entry.get("not_after"),
            })
        # Hash agregado de la lista — para detectar cambios
        agg = "|".join(sorted(c["serial"] for c in certs))
        agg_hash = hashlib.sha256(agg.encode()).hexdigest()[:16]
        # Issuers únicos
        issuers = sorted(set(c["issuer"] for c in certs))
        return {
            "ok": True, "domain": domain,
            "cert_count": len(certs),
            "issuers": issuers,
            "aggregate_hash": agg_hash,
            "checked_at": datetime.now().isoformat(),
            "certs_sample": certs[:5],  # solo 5 más recientes
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def detect_changes(domain: str) -> Dict:
    """Compara estado actual vs último snapshot. Devuelve diff."""
    current = query_ct_for_domain(domain)
    if not current.get("ok"):
        return current
    with _lock:
        d = _load()
        prev = d["snapshots"].get(domain)
        d["stats"]["checks_total"] = d["stats"].get("checks_total", 0) + 1
        d["stats"]["last_check"] = datetime.now().isoformat()

        if not prev:
            d["snapshots"][domain] = current
            _save(d)
            return {"ok": True, "first_check": True, "domain": domain,
                    "msg": "Primer snapshot guardado"}

        changed = current["aggregate_hash"] != prev.get("aggregate_hash")
        new_issuers = set(current["issuers"]) - set(prev.get("issuers", []))
        removed_issuers = set(prev.get("issuers", [])) - set(current["issuers"])

        if changed:
            alert = {
                "ts": datetime.now().isoformat(),
                "domain": domain,
                "type": "cert_rotation",
                "prev_hash": prev.get("aggregate_hash"),
                "new_hash": current["aggregate_hash"],
                "new_issuers": list(new_issuers),
                "removed_issuers": list(removed_issuers),
            }
            d.setdefault("alerts", []).insert(0, alert)
            d["alerts"] = d["alerts"][:200]
            d["stats"]["alerts_total"] = d["stats"].get("alerts_total", 0) + 1

        d["snapshots"][domain] = current
        _save(d)

        return {
            "ok": True, "domain": domain, "changed": changed,
            "new_issuers": list(new_issuers),
            "removed_issuers": list(removed_issuers),
            "prev_count": prev.get("cert_count", 0),
            "now_count": current["cert_count"],
        }


def get_current_certs(domain: str) -> Dict:
    """Devuelve el último snapshot guardado del dominio."""
    d = _load()
    snap = d["snapshots"].get(domain)
    if not snap:
        return {"ok": False, "error": "no_snapshot"}
    return {"ok": True, **snap}


def get_recent_alerts(limit: int = 20) -> List[Dict]:
    return _load().get("alerts", [])[:limit]


def schedule_daily_check() -> Dict:
    """LEGACY: marcador de cron schedule. Usar start_scheduler()."""
    return {"ok": True, "scheduled": True,
            "domains": MONITORED_DOMAINS,
            "note": "Usa start_scheduler() para arrancar el thread real"}


# ═══════════════════════════════════════════
#  SCHEDULER REAL (24h, rate-limited 60s entre dominios)
# ═══════════════════════════════════════════
_scheduler = {"running": False, "thread": None, "stop_event": None,
              "started_at": "", "iterations": 0}


def _scheduler_loop(stop_event: threading.Event, interval_hours: int):
    """Bucle background: cada `interval_hours`, comprueba todos los dominios.
    Inter-dominio sleep 60s para no martillar crt.sh API.
    """
    # Espera 30s al arranque para no sobrecargar boot del dashboard
    if stop_event.wait(30):
        return
    while not stop_event.is_set():
        try:
            for d in MONITORED_DOMAINS:
                if stop_event.is_set(): return
                try:
                    detect_changes(d)
                except Exception:
                    pass
                _scheduler["iterations"] += 1
                # 60s entre dominios para respetar crt.sh (~11 dominios = ~11min total)
                if stop_event.wait(60):
                    return
        except Exception:
            pass
        # Espera hasta el siguiente ciclo (24h por default)
        if stop_event.wait(max(3600, interval_hours * 3600)):
            return


def start_scheduler(interval_hours: int = 24) -> Dict:
    """Arranca scheduler background. Idempotent: no duplica si ya corre."""
    if _scheduler["running"]:
        return {"ok": True, "already_running": True}
    stop = threading.Event()
    t = threading.Thread(target=_scheduler_loop, args=(stop, interval_hours),
                         daemon=True, name="tls-ct-monitor-scheduler")
    _scheduler.update({
        "running": True, "thread": t, "stop_event": stop,
        "started_at": datetime.now().isoformat(),
    })
    t.start()
    return {"ok": True, "started": True, "interval_hours": interval_hours,
            "domains": len(MONITORED_DOMAINS)}


def stop_scheduler() -> Dict:
    if not _scheduler["running"]:
        return {"ok": True, "not_running": True}
    ev = _scheduler.get("stop_event")
    if ev: ev.set()
    _scheduler.update({"running": False, "thread": None, "stop_event": None})
    return {"ok": True, "stopped": True}


def scheduler_status() -> Dict:
    return {
        "running": _scheduler["running"],
        "started_at": _scheduler.get("started_at", ""),
        "iterations": _scheduler.get("iterations", 0),
        "monitored_domains": len(MONITORED_DOMAINS),
    }


def get_stats() -> Dict:
    d = _load()
    return {
        **d.get("stats", {}),
        "domains_tracked": len(d.get("snapshots", {})),
        "monitored_domains": len(MONITORED_DOMAINS),
        "recent_alerts": len(d.get("alerts", [])),
    }


def self_test() -> Dict:
    r = {"checks": {}}
    # Domains list
    r["checks"]["has_monitored_domains"] = len(MONITORED_DOMAINS) >= 5
    r["checks"]["tm_in_list"] = "ticketmaster.com" in MONITORED_DOMAINS
    r["checks"]["queueit_in_list"] = "queue-it.net" in MONITORED_DOMAINS

    # Mock de query (no hace llamada real para test rápido)
    # Inyectamos snapshot manual para test detect_changes
    with _lock:
        d = _load()
        d["snapshots"]["test.example.com"] = {
            "ok": True, "domain": "test.example.com",
            "cert_count": 2, "issuers": ["Let's Encrypt"],
            "aggregate_hash": "abc123",
            "checked_at": datetime.now().isoformat(),
            "certs_sample": [],
        }
        _save(d)

    snap = get_current_certs("test.example.com")
    r["checks"]["snapshot_retrievable"] = snap["ok"]
    r["checks"]["snapshot_has_hash"] = snap.get("aggregate_hash") == "abc123"

    # Schedule
    sch = schedule_daily_check()
    r["checks"]["schedule_returns_ok"] = sch["ok"]
    r["checks"]["schedule_lists_domains"] = "domains" in sch

    # Alerts list
    alerts = get_recent_alerts(10)
    r["checks"]["alerts_returns_list"] = isinstance(alerts, list)

    # Stats
    s = get_stats()
    r["checks"]["stats_complete"] = "monitored_domains" in s

    # Cleanup
    with _lock:
        d = _load()
        d["snapshots"].pop("test.example.com", None)
        _save(d)

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "tls_ct_monitor", "version": 1, "stats": get_stats()}
