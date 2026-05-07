"""sqlite_store.py — Backend SQLite drop-in compatible con `json_atomic`.

Diseñado para reemplazar JSONs grandes (>500KB) sin cambiar APIs públicas
de los módulos que los usan. Mantiene el contrato `load(path, default)` /
`save(path, data)` para que sea sustituible 1:1.

Esquema:
  CREATE TABLE kv_store (
      key         TEXT PRIMARY KEY,    -- filename (ej "purchase_flows")
      data        TEXT NOT NULL,        -- JSON dumps del payload
      updated_at  REAL NOT NULL,
      version     INTEGER DEFAULT 1
  );

Características:
  - 1 SQLite file por proyecto (kv_store.db) — central, atomic transactions
  - WAL mode → lecturas concurrentes sin bloqueos
  - Reentrant lock — varios threads pueden llamar load/save sin races
  - Auto-migración: si key no existe en DB pero el .json sí, lee del JSON
    automáticamente (backward compat sin script explícito)
  - Snapshot al disco JSON cada N writes (opcional, para inspección manual)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

PROYECTO = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROYECTO, "kv_store.db")
_lock = threading.RLock()
_db: Optional[sqlite3.Connection] = None


def _ensure_db() -> sqlite3.Connection:
    """Inicializa la conexión SQLite (singleton thread-safe)."""
    global _db
    with _lock:
        if _db is not None:
            return _db
        # check_same_thread=False permite uso desde múltiples threads
        _db = sqlite3.connect(DB_PATH, check_same_thread=False,
                                isolation_level=None)  # autocommit
        _db.row_factory = sqlite3.Row
        cur = _db.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key         TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                updated_at  REAL NOT NULL,
                version     INTEGER DEFAULT 1
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_kv_updated ON kv_store(updated_at)")
        return _db


def _key_from_path(path: str) -> str:
    """Convierte path absoluto a key (basename sin .json)."""
    bn = os.path.basename(path)
    if bn.endswith(".json"):
        bn = bn[:-5]
    return bn


# ═══════════════════════════════════════════════════════════════
#  API drop-in compatible con json_atomic
# ═══════════════════════════════════════════════════════════════
def load(path: str, default: Any = None) -> Any:
    """Lee desde SQLite. Si la key no existe, intenta leer el JSON original
    (backward compat) y migrarlo a SQLite. Si tampoco hay JSON, devuelve default.
    """
    key = _key_from_path(path)
    with _lock:
        conn = _ensure_db()
        cur = conn.cursor()
        row = cur.execute(
            "SELECT data FROM kv_store WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            try:
                return json.loads(row["data"])
            except json.JSONDecodeError:
                return default if default is not None else {}
        # Backward compat: lee del JSON original si existe
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Auto-migrar a SQLite
                cur.execute(
                    "INSERT OR REPLACE INTO kv_store (key, data, updated_at) "
                    "VALUES (?, ?, ?)",
                    (key, json.dumps(data, ensure_ascii=False), time.time())
                )
                return data
            except Exception:
                pass
        return default if default is not None else {}


def save(path: str, data: Any) -> None:
    """Escribe a SQLite. Atomic via SQLite transaction."""
    key = _key_from_path(path)
    payload = json.dumps(data, ensure_ascii=False)
    with _lock:
        conn = _ensure_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO kv_store (key, data, updated_at) "
            "VALUES (?, ?, ?)",
            (key, payload, time.time())
        )


def delete(path: str) -> bool:
    """Borra la key del store."""
    key = _key_from_path(path)
    with _lock:
        conn = _ensure_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM kv_store WHERE key = ?", (key,))
        return cur.rowcount > 0


def list_keys() -> list:
    """Lista todas las keys + sus tamaños/timestamps."""
    with _lock:
        conn = _ensure_db()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT key, length(data) as size_bytes, updated_at FROM kv_store "
            "ORDER BY size_bytes DESC"
        ).fetchall()
        return [{
            "key": r["key"],
            "size_bytes": r["size_bytes"],
            "updated_at": datetime.fromtimestamp(r["updated_at"]).isoformat(),
        } for r in rows]


def get_db_size() -> int:
    """Devuelve tamaño del archivo SQLite en bytes."""
    return os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0


def vacuum() -> Dict[str, Any]:
    """Compacta la DB (VACUUM SQLite). Útil tras borrar muchas keys."""
    with _lock:
        sz_before = get_db_size()
        conn = _ensure_db()
        # VACUUM no se puede ejecutar dentro de transacción
        conn.execute("VACUUM")
        sz_after = get_db_size()
        return {"ok": True, "size_before": sz_before, "size_after": sz_after,
                 "saved_bytes": sz_before - sz_after}


# ═══════════════════════════════════════════════════════════════
#  Migration helpers
# ═══════════════════════════════════════════════════════════════
def migrate_json(path: str, backup: bool = True) -> Dict[str, Any]:
    """Migra un JSON existente a SQLite. Conserva backup `.json.bak.preSQL`."""
    if not os.path.exists(path):
        return {"ok": False, "error": "json_not_found", "path": path}
    key = _key_from_path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"ok": False, "error": f"json_parse_failed: {e}"}
    save(path, data)
    backup_path = ""
    if backup:
        backup_path = path + ".bak.preSQL"
        try:
            import shutil
            shutil.copy2(path, backup_path)
        except Exception:
            pass
    json_size = os.path.getsize(path)
    return {
        "ok": True,
        "key": key,
        "json_size_bytes": json_size,
        "backup_path": backup_path,
        "migrated_at": datetime.now().isoformat(),
    }


def stats() -> Dict[str, Any]:
    """Resumen del store: keys + total bytes + DB size."""
    keys = list_keys()
    return {
        "db_path": DB_PATH,
        "db_size_bytes": get_db_size(),
        "keys_count": len(keys),
        "total_payload_bytes": sum(k["size_bytes"] for k in keys),
        "keys": keys,
    }


def self_test() -> Dict[str, Any]:
    """Smoke test del store."""
    out = {"ok": True, "checks": {}}
    test_path = os.path.join(PROYECTO, "_sqlite_store_test.json")
    test_data = {"foo": "bar", "n": 42, "items": [1, 2, 3]}
    # 1. Save + load round-trip
    save(test_path, test_data)
    loaded = load(test_path)
    out["checks"]["roundtrip"] = (loaded == test_data)
    # 2. List keys incluye la nueva
    keys = list_keys()
    out["checks"]["key_listed"] = any(k["key"] == "_sqlite_store_test" for k in keys)
    # 3. Delete
    deleted = delete(test_path)
    out["checks"]["delete_works"] = deleted
    loaded2 = load(test_path, default={"empty": True})
    out["checks"]["default_after_delete"] = (loaded2 == {"empty": True})
    # 4. Stats
    s = stats()
    out["checks"]["stats_returns_dict"] = isinstance(s, dict) and "db_size_bytes" in s
    # 5. Concurrencia básica
    import threading
    errors = []
    def writer(i):
        try:
            for j in range(20):
                save(f"/fake/_concurrent_{i}.json", {"i": i, "j": j})
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=5)
    out["checks"]["concurrent_writes"] = (len(errors) == 0)
    # Cleanup test keys
    for i in range(5):
        delete(f"/fake/_concurrent_{i}.json")
    out["passed"] = sum(1 for v in out["checks"].values() if v)
    out["total"] = len(out["checks"])
    out["ok"] = out["passed"] == out["total"]
    return out


def get_info() -> Dict[str, Any]:
    return {
        "module": "sqlite_store",
        "db_path": DB_PATH,
        "stats": stats() if os.path.exists(DB_PATH) else
                  {"db_size_bytes": 0, "keys_count": 0},
        "endpoints": [
            "GET /api/sqlite-store/stats",
            "POST /api/sqlite-store/migrate",
            "POST /api/sqlite-store/vacuum",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
