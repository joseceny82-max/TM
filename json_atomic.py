"""json_atomic.py — Escritura atomica de JSON con .bak y .tmp.

Multiproceso-seguro en Windows: el archivo .tmp incluye PID + thread + nanosegundos
para evitar WinError 32 cuando varios procesos escriben el mismo JSON a la vez.
`os.replace` se reintenta hasta 5 veces con backoff si Windows tiene el fichero
locked momentáneamente."""
import os
import json
import shutil
import threading
import time


def load(path, default=None):
    """Carga JSON con fallback a .bak si el principal esta corrupto."""
    if default is None:
        default = {}
    for p in (path, path + ".bak"):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return default


def save(path, data):
    """Escritura ATOMICA con backup .bak. Multiproceso-safe."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # PID + thread + nanos → tmp único entre procesos paralelos
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}"
    bak = path + ".bak"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as vf:
                json.load(vf)
            shutil.copy2(path, bak)
        except Exception:
            pass
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    # Reintenta os.replace hasta 5 veces con backoff (WinError 32 transitorio).
    last_err = None
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            last_err = e
            time.sleep(0.05 * (attempt + 1))
    # Si tras 5 intentos sigue locked → limpiar tmp y propagar
    try: os.unlink(tmp)
    except Exception: pass
    if last_err: raise last_err
