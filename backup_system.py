"""backup_system.py — Snapshots de datos/."""
import os
import zipfile
import shutil
from datetime import datetime
import config


def _backup_dir():
    d = os.path.join(config.BASE_DIR, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def create_backup(label=""):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"backup_{ts}" + (f"_{label}" if label else "") + ".zip"
    path = os.path.join(_backup_dir(), name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(config.DATOS_DIR):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, config.BASE_DIR)
                try:
                    zf.write(full, rel)
                except Exception:
                    continue
    return {"id": name, "path": path, "size": os.path.getsize(path), "created_at": ts}


def list_backups():
    d = _backup_dir()
    items = []
    for f in sorted(os.listdir(d), reverse=True):
        if f.endswith(".zip"):
            full = os.path.join(d, f)
            items.append({
                "id": f,
                "size": os.path.getsize(full),
                "size_mb": round(os.path.getsize(full) / 1024 / 1024, 2),
                "created_at": datetime.fromtimestamp(os.path.getmtime(full)).isoformat(),
            })
    return items


def restore_backup(backup_id):
    path = os.path.join(_backup_dir(), backup_id)
    if not os.path.exists(path):
        return {"ok": False, "error": "not_found"}
    # Hacer backup del estado actual antes de restaurar
    create_backup(label="pre_restore")
    # Restaurar
    with zipfile.ZipFile(path, "r") as zf:
        zf.extractall(config.BASE_DIR)
    return {"ok": True, "restored": backup_id}


def delete_backup(backup_id):
    path = os.path.join(_backup_dir(), backup_id)
    if os.path.exists(path):
        os.remove(path)
        return {"ok": True}
    return {"ok": False}
