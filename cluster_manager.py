"""cluster_manager.py — Coordinación multi-host del bot.

Cada instancia (host) se registra con heartbeat cada 10 s.
Permite:
  - Ver qué hosts están activos en el dashboard
  - Distribuir carga: cada flow se asigna a 1 host (evita duplicados)
  - Detectar hosts caídos automáticamente (TTL 30 s sin heartbeat = down)
  - Broadcasts cluster-wide via Redis pub/sub

API:
  start_heartbeat()          → thread en background
  list_hosts()               → hosts activos
  acquire_flow(flow_id)      → lock distribuido (solo 1 host ejecuta un flow)
  broadcast(channel, data)   → pub a todos los hosts
  get_cluster_stats()
"""
import os
import time
import threading
import platform
from datetime import datetime
from typing import Optional, List

import redis_backend as rb


_heartbeat_thread = None
_heartbeat_stop = False
_hb_interval = 10   # segundos
_hb_ttl      = 30   # si no hay heartbeat en 30 s → host down


def _host_info() -> dict:
    """Info estática del host."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_count(logical=True)
        mem_gb = round(mem.total / (1024**3), 1)
    except ImportError:
        cpu = os.cpu_count()
        mem_gb = 0
    return {
        "instance_id": rb.INSTANCE_ID,
        "hostname":  platform.node(),
        "system":    platform.system(),
        "release":   platform.release(),
        "python":    platform.python_version(),
        "cpu_count": cpu,
        "mem_gb":    mem_gb,
        "pid":       os.getpid(),
        "started_at":datetime.now().isoformat(),
    }


def _heartbeat_loop():
    global _heartbeat_stop
    info = _host_info()
    while not _heartbeat_stop:
        try:
            payload = dict(info)
            payload["last_heartbeat"] = datetime.now().isoformat()
            payload["backend"] = type(rb.backend).__name__
            # stats vivas
            try:
                import health_monitor
                h = health_monitor.get_health()
                payload["modules_ok"] = h.get("modules_ok", 0)
                payload["modules_total"] = h.get("modules_total", 0)
                payload["uptime_s"] = h.get("uptime_seconds", 0)
            except Exception:
                pass
            # flow activos
            try:
                import tm_purchase_flow
                flows = tm_purchase_flow.get_log(limit=1)
                payload["last_flow"] = flows.get("flows", [{}])[0].get("id") if flows else None
            except Exception:
                pass

            rb.backend.set(f"cluster:host:{rb.INSTANCE_ID}", payload, ttl=_hb_ttl)
        except Exception as e:
            print(f"[cluster_manager] heartbeat err: {e}", flush=True)
        time.sleep(_hb_interval)


def start_heartbeat():
    """Arranca el heartbeat en background thread. Idempotente."""
    global _heartbeat_thread, _heartbeat_stop
    if _heartbeat_thread and _heartbeat_thread.is_alive():
        return False
    _heartbeat_stop = False
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True,
                                           name="cluster-heartbeat")
    _heartbeat_thread.start()
    return True


def stop_heartbeat():
    global _heartbeat_stop
    _heartbeat_stop = True
    # marca este host como going-down
    try:
        rb.backend.delete(f"cluster:host:{rb.INSTANCE_ID}")
    except Exception:
        pass


def list_hosts() -> List[dict]:
    """Todos los hosts vivos (heartbeat <30 s)."""
    hosts = []
    for key in rb.backend.keys("cluster:host:*"):
        data = rb.backend.get(key)
        if isinstance(data, dict):
            # calcular age del heartbeat
            try:
                last = datetime.fromisoformat(data.get("last_heartbeat", ""))
                age = (datetime.now() - last).total_seconds()
                data["heartbeat_age_s"] = round(age, 1)
                data["status"] = "alive" if age < _hb_ttl else "stale"
            except Exception:
                data["status"] = "unknown"
            data["is_self"] = (data.get("instance_id") == rb.INSTANCE_ID)
            hosts.append(data)
    hosts.sort(key=lambda h: h.get("instance_id", ""))
    return hosts


# ═══════════════════════════════════════════
#  FLOW DEDUPE (evita que 2 hosts ejecuten el mismo flow)
# ═══════════════════════════════════════════
def acquire_flow(flow_id: str, ttl: int = 600) -> Optional[str]:
    """Lock distribuido de un flow. Devuelve owner si adquirido, None si ya está."""
    return rb.backend.acquire_lock(f"flow:{flow_id}", ttl=ttl)


def release_flow(flow_id: str, owner: str) -> bool:
    return rb.backend.release_lock(f"flow:{flow_id}", owner)


# ═══════════════════════════════════════════
#  CROSS-HOST BROADCAST (para AcpAT, etc.)
# ═══════════════════════════════════════════
_subs = {}
def broadcast(channel: str, data: dict):
    """Publica a todos los hosts del cluster."""
    rb.backend.publish(f"cluster:{channel}", {
        "from": rb.INSTANCE_ID,
        "ts": datetime.now().isoformat(),
        "data": data,
    })


def listen(channel: str, callback):
    """Suscribe a mensajes de un canal del cluster."""
    sid = rb.backend.subscribe(f"cluster:{channel}", callback)
    _subs[sid] = channel
    return sid


# ═══════════════════════════════════════════
#  STATS
# ═══════════════════════════════════════════
def get_cluster_stats() -> dict:
    hosts = list_hosts()
    alive = [h for h in hosts if h.get("status") == "alive"]
    total_cpu = sum(h.get("cpu_count", 0) for h in alive)
    total_mem = sum(h.get("mem_gb", 0) for h in alive)
    # flow locks activos
    flow_locks = len(rb.backend.keys("lock:flow:*"))
    return {
        "instance_id": rb.INSTANCE_ID,
        "backend": type(rb.backend).__name__,
        "distributed": rb.is_distributed(),
        "hosts_total": len(hosts),
        "hosts_alive": len(alive),
        "hosts_stale": len(hosts) - len(alive),
        "cluster_cpu": total_cpu,
        "cluster_mem_gb": round(total_mem, 1),
        "active_flow_locks": flow_locks,
        "hosts": hosts,
    }
