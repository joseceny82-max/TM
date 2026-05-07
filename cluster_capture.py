"""cluster_capture.py — Distributed capture fleet across N machines.

Coordina N hosts del cluster para capturar sensor_data en paralelo.
Cada host corre su propio Playwright, captura desde su IP distinta,
y publica al pool Redis compartido.

Ventajas vs single-host:
  - N veces más capturas por unidad de tiempo
  - Diversidad geográfica (si hosts están en distintos países)
  - Resiliencia: si 1 host cae, los demás siguen
  - Load balancing: coordinator distribuye targets

Requiere: redis_backend + cluster_manager + akamai_bmjs

API pública:
  start_fleet(target_urls, hosts_wanted, captures_per_host_per_hour)
  stop_fleet()
  fleet_status()
  register_as_capture_host()           ← cada host registra capacidad
  elect_coordinator()                   ← elige 1 host como coordinator
  distribute_targets()                  ← coordinator asigna targets
  collect_captures()                    ← agrega capturas de todos los hosts
"""
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import config
import json_atomic


_CONFIG_KEY = "cluster_capture_config.json"
_FLEET_STATE_KEY = "cluster:capture:fleet"  # en redis_backend
_HOST_STATE_KEY = "cluster:capture:hosts"

_lock = threading.Lock()
_state = {
    "running": False, "thread": None, "stop_event": None,
    "is_coordinator": False, "registered_as_host": False,
    "captures_contributed": 0,
    "last_tick_at": None,
}


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "enabled": False,
        "target_urls": ["https://www.ticketmaster.com"],
        "captures_per_host_per_hour": 40,   # 1 captura cada 90s
        "coordinator_ttl_s": 120,
        "capture_duration_s": 25,
        "engine": "chromium",
        "min_hosts": 1,
        "max_hosts": 10,
        "publish_to_redis": True,           # compartir con cluster
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════════════════════
#  HOST REGISTRATION (cada host del cluster se anuncia)
# ═══════════════════════════════════════════════════════════
def register_as_capture_host(capacity_per_hour: int = 40,
                                geo: Optional[str] = None,
                                engine: str = "chromium") -> dict:
    """Este host se registra como capturador disponible en el cluster."""
    try:
        import redis_backend as rb
        import cluster_manager

        instance = rb.INSTANCE_ID
        info = {
            "instance_id": instance,
            "registered_at": datetime.now().isoformat(),
            "capacity_per_hour": capacity_per_hour,
            "geo": geo or "unknown",
            "engine": engine,
            "captures_contributed": _state.get("captures_contributed", 0),
            "last_heartbeat": datetime.now().isoformat(),
        }
        # TTL 300s, el heartbeat del cluster_manager se refresca cada 10s
        rb.backend.set(f"{_HOST_STATE_KEY}:{instance}", info, ttl=300)
        _state["registered_as_host"] = True
        return {"ok": True, "registered": True, "host_info": info}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def unregister_host() -> dict:
    try:
        import redis_backend as rb
        rb.backend.delete(f"{_HOST_STATE_KEY}:{rb.INSTANCE_ID}")
        _state["registered_as_host"] = False
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def list_registered_hosts() -> List[dict]:
    """Lista hosts del cluster disponibles para captura."""
    try:
        import redis_backend as rb
        keys = rb.backend.keys(f"{_HOST_STATE_KEY}:*")
        out = []
        for key in keys:
            data = rb.backend.get(key)
            if isinstance(data, dict):
                # Verificar que el heartbeat es fresco
                try:
                    hb = datetime.fromisoformat(data.get("last_heartbeat", ""))
                    age_s = (datetime.now() - hb).total_seconds()
                    data["heartbeat_age_s"] = round(age_s, 1)
                    data["status"] = "alive" if age_s < 120 else "stale"
                except Exception:
                    data["status"] = "unknown"
                out.append(data)
        return out
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════
#  COORDINATOR ELECTION (1 host elegido como lider)
# ═══════════════════════════════════════════════════════════
def elect_coordinator() -> dict:
    """Usa distributed lock de redis_backend para elegir coordinator.
    Solo 1 host del cluster actúa como tal a la vez (TTL 120s).
    """
    try:
        import redis_backend as rb
        owner = rb.backend.acquire_lock("cluster:capture:coordinator",
                                          ttl=120, owner=rb.INSTANCE_ID)
        if owner == rb.INSTANCE_ID:
            _state["is_coordinator"] = True
            return {"ok": True, "is_coordinator": True,
                     "instance": rb.INSTANCE_ID,
                     "ttl_s": 120}
        _state["is_coordinator"] = False
        return {"ok": True, "is_coordinator": False,
                 "owner": owner}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def is_coordinator() -> bool:
    return _state.get("is_coordinator", False)


def renew_coordinator() -> dict:
    """Renueva el lock del coordinator (usar cada 60s si lo eres)."""
    try:
        import redis_backend as rb
        if not _state.get("is_coordinator"):
            return {"ok": False, "reason": "not_coordinator"}
        # Re-acquire (mismo owner, extiende TTL)
        rb.backend.set(f"lock:cluster:capture:coordinator", rb.INSTANCE_ID, ttl=120)
        return {"ok": True, "renewed": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


# ═══════════════════════════════════════════════════════════
#  TARGET DISTRIBUTION (el coordinator asigna URLs a hosts)
# ═══════════════════════════════════════════════════════════
def distribute_targets() -> dict:
    """Coordinator: asigna target_urls a hosts según geo/engine.
    Publica asignaciones via pub/sub."""
    if not is_coordinator():
        return {"ok": False, "error": "not coordinator"}

    hosts = [h for h in list_registered_hosts() if h.get("status") == "alive"]
    if not hosts:
        return {"ok": False, "error": "no alive hosts"}

    cfg = get_config()
    targets = cfg.get("target_urls", [])
    assignments = []

    # Round-robin: cada host coge N/total targets
    for i, target in enumerate(targets):
        assigned_host = hosts[i % len(hosts)]
        assignments.append({
            "target_url": target,
            "assigned_host": assigned_host["instance_id"],
            "ts": datetime.now().isoformat(),
        })

    # Publicar via cluster_manager
    try:
        import cluster_manager
        cluster_manager.broadcast("capture:assignments", {
            "assignments": assignments,
            "issued_at": datetime.now().isoformat(),
            "coordinator": _state.get("is_coordinator") and "self" or "other",
        })
    except Exception:
        pass

    return {"ok": True, "assignments_count": len(assignments),
             "assignments": assignments}


# ═══════════════════════════════════════════════════════════
#  FLEET LOOP (orquestación continua)
# ═══════════════════════════════════════════════════════════
def _fleet_loop(stop_event):
    cfg = get_config()
    interval = max(30, int(3600 / max(1, cfg.get("captures_per_host_per_hour", 40))))

    while not stop_event.is_set():
        try:
            # 1. Registrar como host disponible
            register_as_capture_host(
                capacity_per_hour=cfg.get("captures_per_host_per_hour", 40),
                engine=cfg.get("engine", "chromium"),
            )

            # 2. Intentar ser coordinator
            elect_coordinator()
            if _state.get("is_coordinator"):
                distribute_targets()
                renew_coordinator()

            # 3. Ejecutar captura (todos los hosts capturan)
            # Los hosts registrados + distributed task publican al pool
            try:
                import akamai_bmjs
                for target in cfg.get("target_urls", [])[:1]:  # 1 target per tick
                    r = akamai_bmjs.capture_sensor_data(
                        target_url=target,
                        duration_s=cfg.get("capture_duration_s", 25),
                        engine=cfg.get("engine", "chromium"),
                        headless=True,
                    )
                    if r.get("captures"):
                        _state["captures_contributed"] += len(r["captures"])
                        # Auto-publish al pool cluster
                        if cfg.get("publish_to_redis"):
                            for cap_meta in r["captures"]:
                                cid = cap_meta.get("id")
                                full = akamai_bmjs.get_capture(cid) if cid else None
                                if full:
                                    akamai_bmjs.publish_capture_to_cluster(full)
                        # Auto-añadir al dataset ML
                        try:
                            import ml_validator
                            from akamai_validator import _xor_decode
                            import json as _json
                            for cap_meta in r["captures"]:
                                cid = cap_meta.get("id")
                                full = akamai_bmjs.get_capture(cid) if cid else None
                                if full and full.get("sensor_data"):
                                    sensor = full["sensor_data"]
                                    parts = sensor.split(";")
                                    if len(parts) == 5:
                                        decoded = _xor_decode(parts[3], parts[2])
                                        if decoded:
                                            try:
                                                payload = _json.loads(decoded)
                                                ml_validator.add_to_dataset(
                                                    payload, sensor_length=len(sensor),
                                                    label="human_confirmed",
                                                    source="cluster_captured",
                                                )
                                            except Exception:
                                                pass
                        except Exception:
                            pass
            except Exception as e:
                print(f"[cluster_capture] capture err: {e}", flush=True)

            _state["last_tick_at"] = datetime.now().isoformat()
        except Exception as e:
            print(f"[cluster_capture] loop err: {e}", flush=True)

        stop_event.wait(interval)


def start_fleet(target_urls: Optional[List[str]] = None,
                 captures_per_host_per_hour: Optional[int] = None,
                 engine: Optional[str] = None) -> dict:
    """Arranca el fleet loop en este host."""
    if _state["running"]:
        return {"ok": True, "already_running": True}

    # Actualizar config
    patch = {"enabled": True}
    if target_urls:
        patch["target_urls"] = target_urls
    if captures_per_host_per_hour is not None:
        patch["captures_per_host_per_hour"] = int(captures_per_host_per_hour)
    if engine:
        patch["engine"] = engine
    save_config(patch)

    ev = threading.Event()
    t = threading.Thread(target=_fleet_loop, args=(ev,),
                          daemon=True, name="cluster-capture-fleet")
    _state.update({"running": True, "thread": t, "stop_event": ev})
    t.start()
    return {"ok": True, "started": True, "config": get_config()}


def stop_fleet() -> dict:
    if not _state["running"]:
        return {"ok": True, "not_running": True}
    ev = _state.get("stop_event")
    if ev:
        ev.set()
    _state.update({"running": False, "thread": None, "stop_event": None,
                    "is_coordinator": False})
    unregister_host()
    save_config({"enabled": False})
    return {"ok": True, "stopped": True}


def fleet_status() -> dict:
    hosts = list_registered_hosts()
    alive = [h for h in hosts if h.get("status") == "alive"]
    total_capacity = sum(h.get("capacity_per_hour", 0) for h in alive)
    total_contributed = sum(h.get("captures_contributed", 0) for h in hosts)
    return {
        "running": _state["running"],
        "is_coordinator": _state.get("is_coordinator", False),
        "registered_as_host": _state.get("registered_as_host", False),
        "captures_contributed_by_self": _state.get("captures_contributed", 0),
        "total_captures_by_fleet": total_contributed,
        "last_tick_at": _state.get("last_tick_at"),
        "hosts_total": len(hosts),
        "hosts_alive": len(alive),
        "total_capacity_per_hour": total_capacity,
        "hosts": hosts,
        "config": get_config(),
    }


def self_test() -> dict:
    """Smoke test sin arrancar el loop."""
    results = {"ok": True, "checks": {}}

    # Config
    cfg = get_config()
    results["checks"]["config_ok"] = bool(cfg.get("target_urls"))

    # Registration
    r = register_as_capture_host(capacity_per_hour=40)
    results["checks"]["register_ok"] = r.get("ok", False)

    # Coordinator election
    ce = elect_coordinator()
    results["checks"]["coordinator_election_ok"] = ce.get("ok", False)

    # List hosts
    hosts = list_registered_hosts()
    results["checks"]["hosts_visible"] = len(hosts) >= 1

    # Fleet status
    fs = fleet_status()
    results["checks"]["status_readable"] = "running" in fs

    # Cleanup
    unregister_host()

    return results


def get_info() -> dict:
    return {
        "module": "cluster_capture",
        "capabilities": [
            "host_registration", "coordinator_election",
            "target_distribution", "cluster_publish",
            "auto_dataset_feed",
        ],
        "fleet_status": fleet_status(),
    }
