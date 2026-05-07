"""startup_cleanup.py — Limpieza de temporales y .bak corruptos al arrancar."""
import os
import glob
import config


def run_startup_cleanup():
    count = 0
    for pattern in ("*.tmp", "*.bak.old"):
        for f in glob.glob(os.path.join(config.DATOS_DIR, "**", pattern), recursive=True):
            try:
                if os.path.getsize(f) == 0:
                    os.remove(f)
                    count += 1
            except Exception:
                continue
    return count
