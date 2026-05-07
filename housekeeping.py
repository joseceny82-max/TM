"""housekeeping — tareas mantenimiento automático para producción.

Tareas:
  1. Backup diario (vía backup_system) con retención 30 días
  2. Log rotation: archiva *.log >7d en logs/archive/, comprimidos
  3. Stale lock reaper: limpia *.lock huérfanos al boot (PID muerto)
  4. Storage_state freshness: marca caducados >7d
  5. Old screenshots cleanup: borra screenshots >30d

Arranca con `start_daemon()` desde dashboard.py al boot.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

PROYECTO = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("housekeeping")

_daemon_thread = None
_daemon_stop = threading.Event()
_TICK_INTERVAL_S = 3600  # 1h tick

LOG_RETENTION_DAYS = int(os.environ.get("TM_LOG_RETENTION_DAYS", "7"))
BACKUP_RETENTION_DAYS = int(os.environ.get("TM_BACKUP_RETENTION_DAYS", "30"))
SCREENSHOT_RETENTION_DAYS = int(os.environ.get("TM_SCREENSHOT_RETENTION_DAYS", "30"))
STORAGE_STATE_TTL_DAYS = int(os.environ.get("TM_STORAGE_TTL_DAYS", "7"))

stats = {
    "logs_archived": 0,
    "logs_deleted": 0,
    "backups_created": 0,
    "backups_deleted": 0,
    "locks_reaped": 0,
    "storage_states_stale": 0,
    "screenshots_deleted": 0,
    "last_tick": None,
}


def _is_pid_alive(pid: int) -> bool:
    """Comprueba si un PID está vivo en Windows."""
    try:
        import subprocess
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="ignore")
        return str(pid) in out
    except Exception:
        return True  # default: asume vivo (no borrar)


def reap_stale_locks() -> int:
    """Limpia *.lock files huérfanos en raíz del proyecto si su PID está muerto."""
    n = 0
    for f in os.listdir(PROYECTO):
        if not f.endswith(".lock"):
            continue
        path = os.path.join(PROYECTO, f)
        try:
            mtime = os.path.getmtime(path)
            age_h = (time.time() - mtime) / 3600
            # Si el lock tiene >2h y aún existe, asumir orfano
            if age_h > 2:
                with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                    content = fp.read().strip()
                # Try parse PID
                pid = None
                try:
                    pid = int(content) if content.isdigit() else None
                except Exception:
                    pass
                if pid and _is_pid_alive(pid):
                    continue  # vivo, dejarlo
                os.remove(path)
                n += 1
                log.info(f"reaped stale lock: {f} (age={age_h:.1f}h)")
        except Exception as e:
            log.warning(f"lock reap err on {f}: {e}")
    stats["locks_reaped"] += n
    return n


_LOG_SIZE_ROTATE_BYTES = 10 * 1024 * 1024  # rotate cuando log activo > 10 MB


def rotate_logs(retention_days: int = None) -> Dict[str, int]:
    """Rota logs:
      - Si logs/dashboard.log >10MB → mueve a dashboard.log.{ts} y comprime
      - Comprime *.log y *.log.* viejos a logs/archive/
      - Borra .gz >2× retention
    """
    days = retention_days or LOG_RETENTION_DAYS
    out = {"archived": 0, "deleted": 0, "rotated_active": 0}
    logs_dir = os.path.join(PROYECTO, "logs")
    archive_dir = os.path.join(logs_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    cutoff_archive = time.time() - days * 86400
    cutoff_delete = time.time() - days * 86400 * 2

    # 1. Rotación por TAMAÑO del log activo (dashboard.log)
    active_log = os.path.join(logs_dir, "dashboard.log")
    if os.path.exists(active_log):
        try:
            sz = os.path.getsize(active_log)
            if sz > _LOG_SIZE_ROTATE_BYTES:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                rotated = os.path.join(logs_dir, f"dashboard.log.{ts}")
                # truncate via copy+open (no podemos mover si Flask logger lo tiene)
                with open(active_log, "rb") as fin, open(rotated, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                # Truncar el activo
                with open(active_log, "w", encoding="utf-8") as f:
                    f.write(f"--- log rotated at {ts} ({sz} bytes) ---\n")
                out["rotated_active"] = 1
                # Comprimir el rotado
                with open(rotated, "rb") as fin, gzip.open(rotated + ".gz", "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                os.remove(rotated)
                out["archived"] += 1
        except Exception as e:
            log.warning(f"active log rotate err: {e}")

    # 2. Buscar en proyecto raíz Y dentro de logs/ (cubre ambos casos)
    targets = [PROYECTO, logs_dir]
    for src_dir in targets:
        if not os.path.exists(src_dir):
            continue
        for fname in os.listdir(src_dir):
            if not (fname.endswith(".log") or
                     ".log." in fname):
                continue
            if fname.endswith(".gz"):
                continue
            # No archivar el log activo aún en uso
            if fname == "dashboard.log" and src_dir == logs_dir:
                continue
            path = os.path.join(src_dir, fname)
            try:
                mtime = os.path.getmtime(path)
                if mtime < cutoff_archive:
                    gz_path = os.path.join(archive_dir, fname + ".gz")
                    if not os.path.exists(gz_path):
                        with open(path, "rb") as fin, gzip.open(gz_path, "wb") as fout:
                            shutil.copyfileobj(fin, fout)
                    os.remove(path)
                    out["archived"] += 1
            except Exception as e:
                log.warning(f"log rotate err {fname}: {e}")
    # Borrar viejos comprimidos
    for fname in os.listdir(archive_dir):
        if not fname.endswith(".gz"):
            continue
        path = os.path.join(archive_dir, fname)
        try:
            if os.path.getmtime(path) < cutoff_delete:
                os.remove(path)
                out["deleted"] += 1
        except Exception:
            pass
    stats["logs_archived"] += out["archived"]
    stats["logs_deleted"] += out["deleted"]
    return out


def auto_backup(retention_days: int = None) -> Dict[str, Any]:
    """Crea backup diario (1 vez al día) y limpia los antiguos."""
    days = retention_days or BACKUP_RETENTION_DAYS
    out = {"created": False, "deleted": 0, "error": None}
    try:
        import backup_system
        # crea backup solo si último >20h (mtime del .zip más nuevo en backups/)
        backup_dir = os.path.join(PROYECTO, "backups")
        recent = False
        if os.path.exists(backup_dir):
            cutoff_recent = time.time() - 20 * 3600
            for fname in os.listdir(backup_dir):
                if not fname.endswith(".zip"):
                    continue
                p = os.path.join(backup_dir, fname)
                try:
                    if os.path.getmtime(p) > cutoff_recent:
                        recent = True
                        break
                except Exception:
                    continue
        if recent:
            out["skip"] = "recent_backup_<20h"
        else:
            # backup_system.create_backup tiene signature: (label="")
            r = backup_system.create_backup(label="housekeeping_daily")
            if isinstance(r, dict) and r.get("ok"):
                out["created"] = True
                out["path"] = r.get("path")
                stats["backups_created"] += 1
                log.info(f"daily backup created: {r.get('path')}")
            elif isinstance(r, dict):
                out["error"] = r.get("error", "unknown_backup_error")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        log.warning(f"backup err: {e}")

    # Limpiar backups antiguos
    backup_dir = os.path.join(PROYECTO, "backups")
    if os.path.exists(backup_dir):
        cutoff = time.time() - days * 86400
        for fname in os.listdir(backup_dir):
            path = os.path.join(backup_dir, fname)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    out["deleted"] += 1
                    stats["backups_deleted"] += 1
            except Exception:
                pass
    return out


def cleanup_old_screenshots(retention_days: int = None) -> int:
    """Borra screenshots >retention_days."""
    days = retention_days or SCREENSHOT_RETENTION_DAYS
    n = 0
    ss_dir = os.path.join(PROYECTO, "datos", "buy", "runs", "screenshots")
    if not os.path.exists(ss_dir):
        return 0
    cutoff = time.time() - days * 86400
    for fname in os.listdir(ss_dir):
        path = os.path.join(ss_dir, fname)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                n += 1
        except Exception:
            pass
    stats["screenshots_deleted"] += n
    return n


def check_storage_state_freshness(ttl_days: int = None) -> Dict[str, Any]:
    """Marca storage_state files >ttl_days como stale en metadata."""
    days = ttl_days or STORAGE_STATE_TTL_DAYS
    out = {"checked": 0, "stale": 0}
    ss_dir = os.path.join(PROYECTO, "datos", "sessions")
    if not os.path.exists(ss_dir):
        return out
    cutoff = time.time() - days * 86400
    for fname in os.listdir(ss_dir):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(ss_dir, fname)
        try:
            out["checked"] += 1
            if os.path.getmtime(path) < cutoff:
                out["stale"] += 1
                # Mark stale by renaming to .stale (no borra, opt-in)
                marker = path + ".stale_marker"
                if not os.path.exists(marker):
                    with open(marker, "w") as f:
                        f.write(datetime.now().isoformat())
        except Exception:
            pass
    stats["storage_states_stale"] = out["stale"]
    return out


def run_all_tasks() -> Dict[str, Any]:
    """Ejecuta todas las tareas en serie. Devuelve resumen."""
    out = {"ts": datetime.now().isoformat()}
    out["locks_reaped"] = reap_stale_locks()
    out["logs"] = rotate_logs()
    out["backup"] = auto_backup()
    out["screenshots_deleted"] = cleanup_old_screenshots()
    out["storage_states"] = check_storage_state_freshness()
    stats["last_tick"] = out["ts"]
    return out


def _loop():
    while not _daemon_stop.is_set():
        try:
            r = run_all_tasks()
            log.info(f"housekeeping tick: locks={r.get('locks_reaped',0)} "
                     f"logs_arch={r.get('logs',{}).get('archived',0)} "
                     f"backup={r.get('backup',{}).get('created',False)}")
        except Exception as e:
            log.warning(f"housekeeping tick err: {e}")
        _daemon_stop.wait(_TICK_INTERVAL_S)


def start_daemon() -> Dict[str, Any]:
    global _daemon_thread
    if _daemon_thread and _daemon_thread.is_alive():
        return {"ok": True, "already_running": True}
    _daemon_stop.clear()
    # Boot pass: reap stale locks inmediatamente (no esperar 1h)
    try:
        reap_stale_locks()
    except Exception:
        pass
    _daemon_thread = threading.Thread(target=_loop, daemon=True, name="housekeeping")
    _daemon_thread.start()
    return {"ok": True, "started": True}


def stop_daemon() -> Dict[str, Any]:
    global _daemon_thread
    _daemon_stop.set()
    if _daemon_thread:
        _daemon_thread.join(timeout=3)
    _daemon_thread = None
    return {"ok": True}


def get_stats() -> Dict[str, Any]:
    return {
        **stats,
        "daemon_running": _daemon_thread is not None and _daemon_thread.is_alive(),
        "config": {
            "log_retention_days": LOG_RETENTION_DAYS,
            "backup_retention_days": BACKUP_RETENTION_DAYS,
            "screenshot_retention_days": SCREENSHOT_RETENTION_DAYS,
            "storage_state_ttl_days": STORAGE_STATE_TTL_DAYS,
            "tick_interval_s": _TICK_INTERVAL_S,
        }
    }


def self_test() -> Dict[str, Any]:
    out = {"checks": {}}
    out["checks"]["proyecto_dir"] = os.path.isdir(PROYECTO)
    out["checks"]["pid_alive_self"] = _is_pid_alive(os.getpid())
    out["checks"]["pid_dead_999999"] = not _is_pid_alive(999999)
    out["checks"]["stats_callable"] = isinstance(get_stats(), dict)
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=2))
