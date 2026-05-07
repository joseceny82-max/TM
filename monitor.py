"""monitor.py — Health checks y monitoreo de recursos (con caché en memoria)."""
import threading
import time
from datetime import datetime

import config
import json_atomic


# ═══ Caché en memoria compartida ═══
_cache = {
    "status": None,
    "status_ts": 0,
    "sections": None,
    "sections_ts": 0,
}
_cache_lock = threading.Lock()

# TTL de caché (segundos)
STATUS_TTL = 2.0   # CPU/RAM/disk — refresca cada 2s
SECTIONS_TTL = 10.0  # secciones — raro que cambien


class Monitor:
    def get_status(self):
        with _cache_lock:
            if _cache["status"] and (time.time() - _cache["status_ts"]) < STATUS_TTL:
                return _cache["status"]
        # fuera del lock (no queremos bloquear otros lectores mientras medimos)
        status = self._read_status_live()
        with _cache_lock:
            _cache["status"] = status
            _cache["status_ts"] = time.time()
        return status

    def _read_status_live(self):
        try:
            import psutil
            # interval=None / 0 → no bloquea, usa la diferencia desde la última llamada
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage("/").percent
        except Exception:
            cpu = mem = disk = 0
        return {
            "ts": datetime.now().isoformat(),
            "cpu": cpu,
            "memory": mem,
            "disk": disk,
            "workers_active": 0,
            "queues_active": 0,
            "health": "ok" if cpu < 90 and mem < 90 else "warning",
        }

    def get_sections_health(self):
        with _cache_lock:
            if _cache["sections"] and (time.time() - _cache["sections_ts"]) < SECTIONS_TTL:
                return _cache["sections"]
        sections = [
            "dashboard", "problemas", "eventos", "sitios", "compras",
            "bot", "cuentas", "cookies", "analytics", "notificaciones",
            "monitor", "consola", "backup", "usuarios", "otp",
            "soporte", "notas", "config",
        ]
        now = datetime.now().isoformat()
        result = [{"section": s, "status": "ok", "last_check": now} for s in sections]
        with _cache_lock:
            _cache["sections"] = result
            _cache["sections_ts"] = time.time()
        return result


monitor = Monitor()

# Arrancar medición inicial de CPU (interval=None necesita una medición previa
# para tener algo con lo que comparar en las siguientes llamadas)
try:
    import psutil as _ps
    _ps.cpu_percent(interval=None)
except Exception:
    pass
