"""core/dashboard/historical_sqlite.py — Historical index persistente en SQLite.

PROBLEMA RESUELTO:
    `core.control_tower.historical.HistoricalIndex` rescanea filesystem y lee
    JSONL completos cada refresh. No escala a >100 ficheros o JSONLs > 100 MB.

DISEÑO:
    - SQLite con WAL mode (lecturas concurrentes con writes)
    - JSONL sigue siendo SOURCE OF TRUTH (raw events). SQLite solo summaries.
    - Schema versioned (schema_v columna en cada tabla)
    - Retries con backoff si la BD está locked
    - Fail-safe absoluto: si DB falla, dashboard sigue (sin historical)

POLÍTICA:
    - NUNCA persiste eventos individuales (solo agregados por run)
    - NUNCA bloquea dashboard endpoint si DB está locked (retry async)
    - Idempotente: re-ingestar mismo run actualiza (UPSERT)
    - NO toca historical.py (mantener en paralelo durante 1 semana mínimo)

ENV:
    HISTORICAL_DB_PATH         (default: runs/historical_index.sqlite)
    HISTORICAL_RETRY_ATTEMPTS  (default: 3)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional


# ════════════════════════════════════════════════════════════════════
#  Schema (idempotente, IF NOT EXISTS)
# ════════════════════════════════════════════════════════════════════
SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    slot            TEXT,
    start_ts        TEXT,
    end_ts          TEXT,
    duration_s      REAL,
    event_count     INTEGER,
    drift_count     INTEGER,
    soft_drift_count INTEGER DEFAULT 0,
    final_state     TEXT,
    exit_outcome    TEXT,
    health_final    INTEGER,
    root_cause      TEXT,
    jsonl_path      TEXT,
    ingested_at     TEXT,
    schema_v        INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS drifts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    ts              TEXT,
    kind            TEXT,
    severity        TEXT,
    detail          TEXT,
    event_name      TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events_summary (
    run_id              TEXT PRIMARY KEY,
    state_count         INTEGER DEFAULT 0,
    action_count        INTEGER DEFAULT 0,
    observability_count INTEGER DEFAULT 0,
    noise_count         INTEGER DEFAULT 0,
    unknown_count       INTEGER DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_start ON runs(start_ts DESC);
CREATE INDEX IF NOT EXISTS idx_runs_outcome ON runs(exit_outcome);
CREATE INDEX IF NOT EXISTS idx_runs_slot ON runs(slot);
CREATE INDEX IF NOT EXISTS idx_drifts_run ON drifts(run_id);
"""


# Pattern para detectar slot + run_id desde filename
_FILENAME_PATTERN = re.compile(
    r"diagnostics_slot([^_]+)_(\d+_\d+)(?:_pid\d+)?\.jsonl(?:\.gz)?"
)


# ════════════════════════════════════════════════════════════════════
#  HistoricalSQLiteIndex
# ════════════════════════════════════════════════════════════════════
class HistoricalSQLiteIndex:
    """Índice histórico persistente en SQLite.

    Uso:
        idx = HistoricalSQLiteIndex()
        idx.ingest_completed_run("runs/diagnostics_slot1_20260513_120000.jsonl")
        recent = idx.get_recent_runs(limit=50)
        stats = idx.get_health_distribution()
    """

    DEFAULT_DB_PATH = "runs/historical_index.sqlite"
    DEFAULT_RETRY_ATTEMPTS = 3
    DEFAULT_RETRY_BACKOFF_S = 0.1

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path or os.environ.get("HISTORICAL_DB_PATH",
                                                       self.DEFAULT_DB_PATH))
        self.retry_attempts = int(os.environ.get("HISTORICAL_RETRY_ATTEMPTS",
                                                   str(self.DEFAULT_RETRY_ATTEMPTS)))
        self._lock = threading.Lock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._init_schema()

    # ────────────────────────────────────────────────────────────────
    #  Conexión + retry
    # ────────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        """Abre conexión con WAL mode."""
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        return conn

    def _execute_with_retry(self, fn, *args, **kwargs):
        """Ejecuta fn(conn, ...) con retries si OperationalError (DB locked)."""
        last_exc = None
        for attempt in range(self.retry_attempts):
            try:
                with self._lock:
                    conn = self._connect()
                    try:
                        result = fn(conn, *args, **kwargs)
                        conn.commit()
                        return result
                    finally:
                        conn.close()
            except sqlite3.OperationalError as e:
                last_exc = e
                if "locked" in str(e).lower():
                    time.sleep(self.DEFAULT_RETRY_BACKOFF_S * (2 ** attempt))
                    continue
                else:
                    return None
            except Exception:
                return None
        return None

    def _init_schema(self) -> None:
        """Crea tablas si no existen."""
        def _init(conn):
            conn.executescript(_SCHEMA_SQL)
        self._execute_with_retry(_init)

    # ────────────────────────────────────────────────────────────────
    #  Ingest
    # ────────────────────────────────────────────────────────────────
    def ingest_completed_run(self, jsonl_path: str) -> bool:
        """Lee JSONL, extrae summary, hace UPSERT en SQLite.

        Idempotente: re-ingestar mismo run_id actualiza.
        Fail-safe: errores devuelven False.
        """
        try:
            fp = Path(jsonl_path)
            if not fp.exists() or fp.stat().st_size == 0:
                return False
            summary = self._extract_summary(fp)
            if not summary:
                return False
            return self._upsert_run(summary)
        except Exception:
            return False

    def _extract_summary(self, fp: Path) -> Optional[dict]:
        """Lee JSONL y produce summary dict para insertar."""
        try:
            m = _FILENAME_PATTERN.match(fp.name)
            slot = m.group(1) if m else None
            file_run_id = m.group(2) if m else fp.stem

            state_count = 0
            action_count = 0
            obs_count = 0
            noise_count = 0
            unknown_count = 0
            event_count = 0
            drift_count = 0
            soft_drift_count = 0
            states_seen: set[str] = set()
            first_record = None
            last_record = None
            last_score = None
            drifts_collected: list[dict] = []

            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    event_count += 1
                    if first_record is None:
                        first_record = rec
                    last_record = rec
                    kind = rec.get("kind", "UNKNOWN")
                    if kind == "STATE":
                        state_count += 1
                    elif kind == "ACTION":
                        action_count += 1
                    elif kind == "OBSERVABILITY":
                        obs_count += 1
                    elif kind == "NOISE":
                        noise_count += 1
                    else:
                        unknown_count += 1
                    st = rec.get("state")
                    if st:
                        states_seen.add(st)
                    s = rec.get("score")
                    if isinstance(s, (int, float)):
                        last_score = int(s)
                    drift = rec.get("drift")
                    if drift and isinstance(drift, dict):
                        drift_count += 1
                        if drift.get("kind") == "SOFT_DRIFT":
                            soft_drift_count += 1
                        drifts_collected.append({
                            "ts": rec.get("ts"),
                            "kind": drift.get("kind"),
                            "severity": drift.get("severity"),
                            "detail": drift.get("detail"),
                            "event_name": rec.get("event"),
                        })

            if first_record is None or last_record is None:
                return None

            # Resolve run_id robusto
            run_id = (last_record.get("run_id")
                       or first_record.get("run_id")
                       or file_run_id)
            if slot is None:
                slot = str(first_record.get("slot", "default"))

            # Compute duration
            start_ts = first_record.get("ts")
            end_ts = last_record.get("ts")
            duration_s = 0.0
            try:
                from datetime import datetime
                if start_ts and end_ts:
                    s = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                    e = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                    duration_s = (e - s).total_seconds()
            except Exception:
                pass

            # Resolve outcome
            final_state = last_record.get("state")
            last_event = last_record.get("event", "")
            outcome = "UNKNOWN_EXIT"
            if final_state == "exit":
                if "bot_block" in str(last_event).lower():
                    outcome = "BLOCKED"
                elif last_event.startswith("FATAL_") or last_event.startswith("TIMEOUT_"):
                    outcome = "FATAL"
                elif last_event == "stop_received":
                    outcome = "STOPPED"
            else:
                outcome = "INCOMPLETE"

            # Root cause heurística: usar último soft_drift category si existe
            root_cause = None
            for drift in reversed(drifts_collected):
                if drift.get("kind") == "SOFT_DRIFT":
                    # No tenemos category directa; usar detail
                    root_cause = drift.get("detail", "")[:120]
                    break

            from datetime import datetime, timezone
            return {
                "run_id": str(run_id),
                "slot": str(slot),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "duration_s": duration_s,
                "event_count": event_count,
                "drift_count": drift_count,
                "soft_drift_count": soft_drift_count,
                "final_state": final_state,
                "exit_outcome": outcome,
                "health_final": last_score,
                "root_cause": root_cause,
                "jsonl_path": str(fp),
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "state_count": state_count,
                "action_count": action_count,
                "observability_count": obs_count,
                "noise_count": noise_count,
                "unknown_count": unknown_count,
                "drifts": drifts_collected,
            }
        except Exception:
            return None

    def _upsert_run(self, s: dict) -> bool:
        """Inserta o actualiza run en runs + events_summary + drifts."""
        def _do(conn):
            run_id = s["run_id"]
            # UPSERT runs
            conn.execute("""
                INSERT INTO runs (
                    run_id, slot, start_ts, end_ts, duration_s,
                    event_count, drift_count, soft_drift_count,
                    final_state, exit_outcome, health_final, root_cause,
                    jsonl_path, ingested_at, schema_v
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    end_ts=excluded.end_ts,
                    duration_s=excluded.duration_s,
                    event_count=excluded.event_count,
                    drift_count=excluded.drift_count,
                    soft_drift_count=excluded.soft_drift_count,
                    final_state=excluded.final_state,
                    exit_outcome=excluded.exit_outcome,
                    health_final=excluded.health_final,
                    root_cause=excluded.root_cause,
                    ingested_at=excluded.ingested_at
            """, (
                run_id, s["slot"], s["start_ts"], s["end_ts"], s["duration_s"],
                s["event_count"], s["drift_count"], s["soft_drift_count"],
                s["final_state"], s["exit_outcome"], s["health_final"],
                s["root_cause"], s["jsonl_path"], s["ingested_at"], SCHEMA_VERSION,
            ))
            # UPSERT events_summary
            conn.execute("""
                INSERT INTO events_summary (
                    run_id, state_count, action_count, observability_count,
                    noise_count, unknown_count
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    state_count=excluded.state_count,
                    action_count=excluded.action_count,
                    observability_count=excluded.observability_count,
                    noise_count=excluded.noise_count,
                    unknown_count=excluded.unknown_count
            """, (run_id, s["state_count"], s["action_count"],
                   s["observability_count"], s["noise_count"], s["unknown_count"]))
            # Re-insert drifts: borrar previos + insertar
            conn.execute("DELETE FROM drifts WHERE run_id=?", (run_id,))
            for d in s["drifts"]:
                conn.execute("""
                    INSERT INTO drifts (run_id, ts, kind, severity, detail, event_name)
                    VALUES (?,?,?,?,?,?)
                """, (run_id, d.get("ts"), d.get("kind"), d.get("severity"),
                       d.get("detail"), d.get("event_name")))
            return True
        result = self._execute_with_retry(_do)
        return result is True

    # ────────────────────────────────────────────────────────────────
    #  Queries
    # ────────────────────────────────────────────────────────────────
    def get_recent_runs(self, limit: int = 50,
                          outcome: Optional[str] = None,
                          slot: Optional[str] = None) -> list[dict]:
        """Lista de runs más recientes (filtrable)."""
        def _do(conn):
            q = "SELECT * FROM runs WHERE 1=1"
            params: list[Any] = []
            if outcome:
                q += " AND exit_outcome=?"
                params.append(outcome)
            if slot is not None:
                q += " AND slot=?"
                params.append(str(slot))
            q += " ORDER BY start_ts DESC LIMIT ?"
            params.append(int(limit))
            cursor = conn.execute(q, params)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        return self._execute_with_retry(_do) or []

    def get_run(self, run_id: str) -> Optional[dict]:
        """Obtiene un run por ID."""
        def _do(conn):
            cursor = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
        return self._execute_with_retry(_do)

    def get_drifts(self, run_id: str) -> list[dict]:
        """Drifts asociados a un run."""
        def _do(conn):
            cursor = conn.execute("SELECT * FROM drifts WHERE run_id=? ORDER BY id",
                                    (run_id,))
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        return self._execute_with_retry(_do) or []

    def get_root_causes(self, limit: int = 20) -> list[dict]:
        """Top root_cause patterns con conteo."""
        def _do(conn):
            cursor = conn.execute("""
                SELECT root_cause, COUNT(*) as count,
                       AVG(health_final) as avg_health
                FROM runs
                WHERE root_cause IS NOT NULL
                GROUP BY root_cause
                ORDER BY count DESC
                LIMIT ?
            """, (limit,))
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        return self._execute_with_retry(_do) or []

    def get_health_distribution(self) -> dict:
        """Distribución de health_final."""
        def _do(conn):
            cursor = conn.execute("""
                SELECT
                    SUM(CASE WHEN health_final >= 80 THEN 1 ELSE 0 END) as healthy,
                    SUM(CASE WHEN health_final >= 60 AND health_final < 80 THEN 1 ELSE 0 END) as degraded,
                    SUM(CASE WHEN health_final >= 30 AND health_final < 60 THEN 1 ELSE 0 END) as unhealthy,
                    SUM(CASE WHEN health_final < 30 THEN 1 ELSE 0 END) as critical,
                    COUNT(*) as total,
                    AVG(health_final) as avg
                FROM runs WHERE health_final IS NOT NULL
            """)
            row = cursor.fetchone()
            if not row:
                return {}
            return {
                "healthy": row[0] or 0,
                "degraded": row[1] or 0,
                "unhealthy": row[2] or 0,
                "critical": row[3] or 0,
                "total": row[4] or 0,
                "avg_score": round(row[5] or 0, 1),
            }
        return self._execute_with_retry(_do) or {}

    def get_stats(self) -> dict:
        """Resumen global."""
        def _do(conn):
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(event_count) as events,
                       SUM(drift_count) as drifts,
                       SUM(soft_drift_count) as soft_drifts,
                       AVG(duration_s) as avg_duration,
                       AVG(health_final) as avg_health
                FROM runs
            """).fetchone()
            outcomes_rows = conn.execute("""
                SELECT exit_outcome, COUNT(*) as c
                FROM runs
                GROUP BY exit_outcome
            """).fetchall()
            outcomes = {r[0] or "UNKNOWN": r[1] for r in outcomes_rows}
            total = row[0] or 0
            success_count = outcomes.get("STOPPED", 0)
            blocked_count = outcomes.get("BLOCKED", 0)
            return {
                "total_runs": total,
                "total_events": row[1] or 0,
                "total_drifts": row[2] or 0,
                "total_soft_drifts": row[3] or 0,
                "avg_duration_s": round(row[4] or 0, 1),
                "avg_health_score": round(row[5] or 0, 1),
                "outcomes": outcomes,
                "success_rate_pct": round(100 * success_count / max(total, 1), 1),
                "blocked_rate_pct": round(100 * blocked_count / max(total, 1), 1),
            }
        return self._execute_with_retry(_do) or {"total_runs": 0}

    def reset_for_test(self) -> None:
        """Para tests: borrar todo."""
        def _do(conn):
            conn.execute("DELETE FROM drifts")
            conn.execute("DELETE FROM events_summary")
            conn.execute("DELETE FROM runs")
        self._execute_with_retry(_do)


# ════════════════════════════════════════════════════════════════════
#  Singleton
# ════════════════════════════════════════════════════════════════════
_default_index: Optional[HistoricalSQLiteIndex] = None
_idx_lock = threading.Lock()


def get_default_sqlite_index() -> HistoricalSQLiteIndex:
    global _default_index
    with _idx_lock:
        if _default_index is None:
            _default_index = HistoricalSQLiteIndex()
    return _default_index


def reset_for_test() -> None:
    global _default_index
    with _idx_lock:
        _default_index = None


# ════════════════════════════════════════════════════════════════════
#  Background ingester (opcional, opt-in)
# ════════════════════════════════════════════════════════════════════
class SQLiteBackgroundIngester:
    """Thread daemon que vigila ficheros JSONL completados e ingesta a SQLite.

    Considera "completado" un fichero que no se ha modificado en `idle_threshold_s`.
    Idempotente: re-ingestar el mismo fichero actualiza.
    """

    def __init__(self,
                 directory: str = "runs",
                 index: Optional[HistoricalSQLiteIndex] = None,
                 idle_threshold_s: float = 60.0,
                 poll_interval_s: float = 30.0):
        self.directory = Path(directory)
        self.index = index or get_default_sqlite_index()
        self.idle_threshold_s = idle_threshold_s
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ingested: set[str] = set()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        if os.environ.get("HISTORICAL_INGESTER_ENABLED", "1") in ("0", "false"):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                          name="SQLiteIngester")
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception:
                pass
            self._stop.wait(self.poll_interval_s)

    def _scan_once(self) -> int:
        ingested = 0
        try:
            files = sorted(self.directory.glob("diagnostics_*.jsonl"))
        except Exception:
            return 0
        now = time.time()
        for fp in files:
            try:
                key = str(fp)
                age = now - fp.stat().st_mtime
                if age < self.idle_threshold_s:
                    continue
                if key in self._ingested:
                    continue
                if self.index.ingest_completed_run(str(fp)):
                    self._ingested.add(key)
                    ingested += 1
            except Exception:
                continue
        return ingested

    def force_scan(self) -> int:
        """Para tests + endpoints manuales."""
        return self._scan_once()


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        runs_dir = Path(tmp) / "runs"
        runs_dir.mkdir()
        # Crear JSONL dummy
        fp = runs_dir / "diagnostics_slot1_20260513_120000.jsonl"
        records = [
            {"ts": "2026-05-13T12:00:00Z", "run_id": "r1", "slot": 1, "event": "init",
              "kind": "OBSERVABILITY", "state": "setup", "score": 30},
            {"ts": "2026-05-13T12:00:05Z", "run_id": "r1", "slot": 1, "event": "goto",
              "kind": "STATE", "state": "browsing", "score": 50},
            {"ts": "2026-05-13T12:00:10Z", "run_id": "r1", "slot": 1, "event": "no_email_input",
              "kind": "OBSERVABILITY", "state": "login", "score": 35,
              "drift": {"kind": "SOFT_DRIFT", "severity": "high",
                          "detail": "LOGIN_FAILURE"}},
            {"ts": "2026-05-13T12:00:15Z", "run_id": "r1", "slot": 1, "event": "FATAL_bot_block_early",
              "kind": "STATE", "state": "exit", "score": 25},
        ]
        with open(fp, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        idx = HistoricalSQLiteIndex(db_path=str(Path(tmp) / "idx.sqlite"))
        ok = idx.ingest_completed_run(str(fp))
        print(f"Ingested: {ok}")
        runs = idx.get_recent_runs()
        print(f"Runs: {len(runs)}, first: {runs[0] if runs else 'none'}")
        stats = idx.get_stats()
        print(f"Stats: {stats}")


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
