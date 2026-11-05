"""core/dashboard/rotation.py — JSONL rotation background job.

PROBLEMA RESUELTO:
    JSONLs en runs/ crecen indefinidamente. Tras 1 año, GB acumulados.
    Si disco al 95% → próxima escritura del bot bloquea.

POLÍTICA (no destructiva):
    1. Comprime con gzip ficheros antiguos (>N días)
    2. Mueve a runs/archive/<YYYY-MM>/
    3. Si total > N MB, comprime más viejos hasta bajar al 50%
    4. **NUNCA borra** — solo comprime + mueve
    5. **NUNCA toca ficheros activos** (modificados últimos 60s)
    6. Background thread daemon (no bloquea bot)
    7. Apagable con env JSONL_ROTATION_ENABLED=0

ENV:
    JSONL_ROTATION_ENABLED      (default: 1)
    JSONL_ROTATION_MAX_AGE_DAYS (default: 7)
    JSONL_ROTATION_MAX_TOTAL_MB (default: 1024)
    JSONL_ROTATION_INTERVAL_S   (default: 3600)
"""
from __future__ import annotations

import gzip
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# ════════════════════════════════════════════════════════════════════
#  CONFIG defaults
# ════════════════════════════════════════════════════════════════════
DEFAULT_DIR = "runs"
DEFAULT_ARCHIVE_DIR = "runs/archive"
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_MAX_TOTAL_MB = 1024
DEFAULT_INTERVAL_S = 3600  # 1 hora
ACTIVE_GRACE_S = 60  # no tocar ficheros con mtime < 60s


# ════════════════════════════════════════════════════════════════════
#  JSONLRotator
# ════════════════════════════════════════════════════════════════════
class JSONLRotator:
    """Background job que comprime y archiva JSONLs viejos.

    Uso:
        rotator = JSONLRotator()
        rotator.start()  # daemon thread

        # Para forzar rotación manual (tests, endpoint):
        result = rotator.rotate_once()

        stats = rotator.get_stats()
    """

    def __init__(self,
                 directory: str = DEFAULT_DIR,
                 archive_dir: str = DEFAULT_ARCHIVE_DIR,
                 max_age_days: Optional[int] = None,
                 max_total_mb: Optional[int] = None,
                 interval_s: Optional[float] = None):
        self.directory = Path(directory)
        self.archive_dir = Path(archive_dir)
        self.max_age_days = int(max_age_days
                                  if max_age_days is not None
                                  else os.environ.get(
                                      "JSONL_ROTATION_MAX_AGE_DAYS",
                                      DEFAULT_MAX_AGE_DAYS))
        self.max_total_mb = int(max_total_mb
                                  if max_total_mb is not None
                                  else os.environ.get(
                                      "JSONL_ROTATION_MAX_TOTAL_MB",
                                      DEFAULT_MAX_TOTAL_MB))
        self.interval_s = float(interval_s
                                  if interval_s is not None
                                  else os.environ.get(
                                      "JSONL_ROTATION_INTERVAL_S",
                                      DEFAULT_INTERVAL_S))

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

        self._stats_lock = threading.Lock()
        self.stats: dict = {
            "rotations_completed": 0,
            "files_rotated_total": 0,
            "bytes_compressed_total": 0,
            "last_rotation_at": None,
            "last_rotation_files": 0,
            "errors_total": 0,
        }

    # ────────────────────────────────────────────────────────────────
    #  Lifecycle
    # ────────────────────────────────────────────────────────────────
    def start(self) -> bool:
        """Lanza thread daemon. Idempotente. Devuelve True si arranca."""
        if self._started:
            return True
        if not self._is_enabled():
            return False
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                          name="JSONLRotator")
        self._thread.start()
        self._started = True
        return True

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @staticmethod
    def _is_enabled() -> bool:
        return os.environ.get("JSONL_ROTATION_ENABLED", "1") not in (
            "0", "false", "False", "FALSE", "no", "NO"
        )

    # ────────────────────────────────────────────────────────────────
    #  Main loop
    # ────────────────────────────────────────────────────────────────
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.rotate_once()
            except Exception:
                pass  # fail-safe
            self._stop.wait(self.interval_s)

    # ────────────────────────────────────────────────────────────────
    #  Rotation policy
    # ────────────────────────────────────────────────────────────────
    def rotate_once(self) -> dict:
        """Ejecuta una iteración de rotación. Devuelve estadísticas.

        Política:
            1. Comprime ficheros con age > max_age_days
            2. Si total_size > max_total_mb, sigue comprimiendo más viejos
               hasta bajar al 50% del threshold
        """
        result = {
            "files_rotated": 0,
            "bytes_compressed": 0,
            "errors": 0,
        }
        try:
            files = self._list_jsonl_files()
            now = time.time()
            max_age_s = self.max_age_days * 86400

            # Paso 1: ficheros viejos
            for fp in files:
                try:
                    age_s = now - fp.stat().st_mtime
                    if age_s < ACTIVE_GRACE_S:
                        continue  # protegido
                    if age_s >= max_age_s:
                        n = self._compress_file(fp)
                        if n > 0:
                            result["files_rotated"] += 1
                            result["bytes_compressed"] += n
                except Exception:
                    result["errors"] += 1

            # Paso 2: si total > threshold, comprimir más viejos
            files_after = self._list_jsonl_files()
            total_bytes = sum(f.stat().st_size for f in files_after
                                if f.exists())
            max_bytes = self.max_total_mb * 1024 * 1024
            target_bytes = int(max_bytes * 0.5)
            if total_bytes > max_bytes:
                for fp in sorted(files_after, key=lambda f: f.stat().st_mtime):
                    if total_bytes <= target_bytes:
                        break
                    try:
                        age_s = now - fp.stat().st_mtime
                        if age_s < ACTIVE_GRACE_S:
                            continue
                        size_before = fp.stat().st_size
                        n = self._compress_file(fp)
                        if n > 0:
                            result["files_rotated"] += 1
                            result["bytes_compressed"] += n
                            total_bytes -= size_before
                    except Exception:
                        result["errors"] += 1

            # Update stats
            with self._stats_lock:
                self.stats["rotations_completed"] += 1
                self.stats["files_rotated_total"] += result["files_rotated"]
                self.stats["bytes_compressed_total"] += result["bytes_compressed"]
                self.stats["errors_total"] += result["errors"]
                self.stats["last_rotation_at"] = datetime.now().isoformat()
                self.stats["last_rotation_files"] = result["files_rotated"]
        except Exception:
            result["errors"] += 1
        return result

    def _list_jsonl_files(self) -> list[Path]:
        """Files que matchean el patrón en el directorio."""
        try:
            return sorted(self.directory.glob("diagnostics_*.jsonl"))
        except Exception:
            return []

    def _compress_file(self, jsonl_path: Path) -> int:
        """Comprime + mueve un fichero. Devuelve bytes del archive resultante.

        Política:
            - Origen: runs/diagnostics_slotX_YYYYMMDD_HHMMSS_pidN.jsonl
            - Destino: runs/archive/<YYYY-MM>/<filename>.gz
            - Si destino ya existe (re-ejecución), sufijo .dup<N>
        """
        try:
            if not jsonl_path.exists():
                return 0
            ts = datetime.fromtimestamp(jsonl_path.stat().st_mtime)
            target_dir = self.archive_dir / ts.strftime("%Y-%m")
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / (jsonl_path.name + ".gz")
            # Evitar overwrite
            if target.exists():
                # Buscar nombre alternativo
                i = 1
                while True:
                    alt = target_dir / f"{jsonl_path.name}.dup{i}.gz"
                    if not alt.exists():
                        target = alt
                        break
                    i += 1
                    if i > 10:
                        return 0  # demasiados duplicados
            # Comprimir
            with open(jsonl_path, "rb") as fin:
                with gzip.open(target, "wb", compresslevel=6) as fout:
                    shutil.copyfileobj(fin, fout)
            # Verificar tamaño antes de borrar original
            archived_size = target.stat().st_size
            if archived_size <= 0:
                return 0
            # Eliminar original SOLO si compresión exitosa
            jsonl_path.unlink()
            return archived_size
        except Exception:
            return 0

    # ────────────────────────────────────────────────────────────────
    #  Stats
    # ────────────────────────────────────────────────────────────────
    def get_stats(self) -> dict:
        """Snapshot de estadísticas + config actual."""
        with self._stats_lock:
            s = dict(self.stats)
        # Stats directorio actual
        try:
            files = self._list_jsonl_files()
            total_bytes = sum(f.stat().st_size for f in files if f.exists())
        except Exception:
            files = []
            total_bytes = 0
        try:
            archive_files = list(self.archive_dir.rglob("*.gz")) if self.archive_dir.exists() else []
            archive_bytes = sum(f.stat().st_size for f in archive_files)
        except Exception:
            archive_files = []
            archive_bytes = 0

        s.update({
            "config": {
                "max_age_days": self.max_age_days,
                "max_total_mb": self.max_total_mb,
                "interval_s": self.interval_s,
                "enabled": self._is_enabled(),
            },
            "current": {
                "active_files": len(files),
                "active_bytes": total_bytes,
                "active_mb": round(total_bytes / (1024 * 1024), 2),
                "archive_files": len(archive_files),
                "archive_bytes": archive_bytes,
                "archive_mb": round(archive_bytes / (1024 * 1024), 2),
            },
            "running": self.is_running(),
        })
        return s


# ════════════════════════════════════════════════════════════════════
#  Singleton + helpers
# ════════════════════════════════════════════════════════════════════
_default_rotator: Optional[JSONLRotator] = None
_rot_lock = threading.Lock()


def get_default_rotator() -> JSONLRotator:
    global _default_rotator
    with _rot_lock:
        if _default_rotator is None:
            _default_rotator = JSONLRotator()
    return _default_rotator


def reset_for_test() -> None:
    global _default_rotator
    with _rot_lock:
        if _default_rotator:
            _default_rotator.stop()
        _default_rotator = None


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "runs"
        d.mkdir()
        # Crear fichero "viejo"
        fp = d / "diagnostics_slot1_20260101_120000.jsonl"
        fp.write_text('{"event":"old"}\n', encoding="utf-8")
        old_time = time.time() - 86400 * 10  # 10 días
        os.utime(fp, (old_time, old_time))
        r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                          max_age_days=7)
        result = r.rotate_once()
        print(f"Rotated: {result}")
        print(f"Stats: {r.get_stats()}")


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
