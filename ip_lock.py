"""ip_lock.py — Lock GLOBAL cross-process de IPs en uso.

Usado por TODOS los procesos del bot (registros, compras, filas, validador):
una IP NO se puede usar por dos procesos simultáneamente, ni reusar en pocas
horas (TM agrupa fingerprints por IP+ASN en ventanas cortas).

API:
    claim_ip(ip, owner=None) -> bool      # reserva (atómico file-based)
    release_ip(ip)                          # libera al cerrar browser
    list_active() -> [{ip, owner, since}]   # auditoría
    is_recently_used(ip, hours=2) -> bool   # evita reuso reciente
    record_recent_use(ip)                   # llamar al release
    get_recent_uses(hours=24) -> dict
    reset_active()                          # limpieza tras crash
    reset_all()                             # reset total (active + recent)

Archivos en datos/:
    ip_lock_active.json   — {ip: {owner, since}}, IPs en uso AHORA
    ip_lock_recent.json   — {ip: last_used_iso}, todas las IPs usadas reciente
"""
from __future__ import annotations
import os
import json
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

PROYECTO = os.path.dirname(os.path.abspath(__file__))
_ACTIVE_PATH = os.path.join(PROYECTO, "datos", "ip_lock_active.json")
_RECENT_PATH = os.path.join(PROYECTO, "datos", "ip_lock_recent.json")
os.makedirs(os.path.dirname(_ACTIVE_PATH), exist_ok=True)

_lock = threading.Lock()


def _read(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception: pass
    return {}


def _write(path: str, data: dict):
    try:
        try:
            import json_atomic
            json_atomic.save(path, data)
            return
        except Exception:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
        os.replace(tmp, path)
    except Exception: pass


def claim_ip(ip: str, owner: str = "unknown") -> bool:
    """Reserva atómica de una IP. Devuelve False si ya está en uso por otro
    proceso. Usar `with claim_context(ip)` o llamar release_ip al terminar.
    """
    if not ip: return True
    with _lock:
        active = _read(_ACTIVE_PATH)
        if ip in active:
            return False
        active[ip] = {
            "owner": owner,
            "since": datetime.now().isoformat(),
            "pid": os.getpid(),
        }
        _write(_ACTIVE_PATH, active)
        return True


def release_ip(ip: str, mark_recent: bool = True):
    """Libera la IP del lock activo. Si mark_recent=True, registra timestamp
    en `ip_lock_recent.json` para que `is_recently_used` lo detecte."""
    if not ip: return
    with _lock:
        active = _read(_ACTIVE_PATH)
        active.pop(ip, None)
        _write(_ACTIVE_PATH, active)
        if mark_recent:
            recent = _read(_RECENT_PATH)
            recent[ip] = datetime.now().isoformat()
            # Mantener solo últimas 7 días (no acumular indefinido)
            cutoff = datetime.now() - timedelta(days=7)
            cleaned = {}
            for k, v in recent.items():
                try:
                    if datetime.fromisoformat(v) >= cutoff:
                        cleaned[k] = v
                except Exception: pass
            _write(_RECENT_PATH, cleaned)


def is_recently_used(ip: str, hours: float = 2.0) -> bool:
    """¿La IP fue liberada hace MENOS de `hours`? TM agrupa fingerprints por
    IP en ventanas cortas → reusar la misma IP en <2h es señal de patrón bot."""
    if not ip: return False
    recent = _read(_RECENT_PATH)
    iso = recent.get(ip)
    if not iso: return False
    try:
        dt = datetime.fromisoformat(iso)
        elapsed_h = (datetime.now() - dt).total_seconds() / 3600.0
        return elapsed_h < hours
    except Exception:
        return False


def is_ip_available(ip: str, recent_hours: float = 2.0) -> tuple:
    """¿Puedo usar esta IP ahora? Devuelve (allowed, reason).
    Reglas:
        - NO si está activa en otro proceso (active lock).
        - NO si fue usada en las últimas `recent_hours` horas.
    """
    if not ip:
        return True, "no_ip_to_check"
    active = _read(_ACTIVE_PATH)
    if ip in active:
        owner = active[ip].get("owner", "?")
        return False, f"active_in_other_process: owner={owner}"
    if is_recently_used(ip, hours=recent_hours):
        recent = _read(_RECENT_PATH)
        return False, f"recently_used_at_{recent.get(ip,'?')[:19]}"
    return True, "ok"


def list_active() -> List[Dict[str, Any]]:
    active = _read(_ACTIVE_PATH)
    return [{"ip": ip, **info} for ip, info in active.items()]


def get_recent_uses(hours: float = 24.0) -> Dict[str, str]:
    """Devuelve IPs usadas en las últimas X horas con su timestamp."""
    recent = _read(_RECENT_PATH)
    cutoff = datetime.now() - timedelta(hours=hours)
    out = {}
    for ip, iso in recent.items():
        try:
            if datetime.fromisoformat(iso) >= cutoff:
                out[ip] = iso
        except Exception: pass
    return out


def reset_active():
    """Limpia solo el lock active (tras crash)."""
    _write(_ACTIVE_PATH, {})


def reset_all():
    """Limpia active + recent. CUIDADO: tras esto cualquier proceso puede
    reusar IPs sin límite."""
    _write(_ACTIVE_PATH, {})
    _write(_RECENT_PATH, {})


def stats() -> Dict[str, Any]:
    active = _read(_ACTIVE_PATH)
    recent = _read(_RECENT_PATH)
    recent_2h = sum(1 for v in recent.values()
                    if (datetime.now() - datetime.fromisoformat(v)).total_seconds() < 7200)
    return {
        "active_count": len(active),
        "active_ips": list(active.keys()),
        "recent_total": len(recent),
        "recent_last_2h": recent_2h,
    }


if __name__ == "__main__":
    print(json.dumps(stats(), indent=2))
