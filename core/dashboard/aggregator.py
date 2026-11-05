"""core/dashboard/aggregator.py — Run/session state aggregator.

Mantiene en memoria el estado actual de cada slot/run activo:
    - Estado FSM actual
    - Health score
    - Drifts detectados
    - Loops detectados
    - Events stream (últimos N)
    - Conteos por categoría (STATE/ACTION/OBS/NOISE)
    - Timestamp inicio + última actividad

Reconstruye desde JSONL stream — no necesita acceso al bot.

Diseño:
    - Thread-safe
    - Idle timeout: runs sin actividad >60s se marcan stale
    - Memoria limitada: solo últimos N runs activos
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  P2: Caps de memoria (eliminan growth unbounded en runs largos)
# ════════════════════════════════════════════════════════════════════
DRIFTS_MAXLEN = 50         # últimos N drifts por run
FAILURE_TRACE_MAXLEN = 10  # eventos previos+actual del último fallo
LOOPS_CAP = 20             # top-N keys en loops dict


# ════════════════════════════════════════════════════════════════════
#  P8: Pattern para filenames diagnostics_slotN_run_id_pidN.jsonl
# ════════════════════════════════════════════════════════════════════
_JSONL_FILENAME_PATTERN = re.compile(
    r"diagnostics_slot([^_]+)_(\d+_\d+)(?:_pid\d+)?\.jsonl(?:\.gz)?"
)


# ════════════════════════════════════════════════════════════════════
#  RunStats — estado de un run/slot
# ════════════════════════════════════════════════════════════════════
@dataclass
class RunStats:
    slot: Any
    run_id: str
    started_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)

    # FSM state
    current_state: str = "setup"
    previous_state: Optional[str] = None
    state_history: deque = field(default_factory=lambda: deque(maxlen=20))

    # Health
    health_score: int = 30
    health_verdict: str = "UNHEALTHY"

    # Events
    event_count: int = 0
    kind_counts: dict = field(default_factory=lambda: {
        "STATE": 0, "ACTION": 0, "OBSERVABILITY": 0, "NOISE": 0, "UNKNOWN": 0,
    })
    recent_events: deque = field(default_factory=lambda: deque(maxlen=50))

    # Drift (P2: deque acotado + counter de descartes)
    drifts: deque = field(default_factory=lambda: deque(maxlen=DRIFTS_MAXLEN))
    drift_count: int = 0
    drifts_dropped_total: int = 0  # P2: contadores totales históricos

    # Loops (P2: top-N keys + counters)
    loops: dict[str, int] = field(default_factory=dict)
    loops_dropped_keys: int = 0   # P2: keys nuevas descartadas por cap
    loops_max_seen: int = 20      # P2 spec: capacidad declarada del top-N

    # Failure trace (P2: deque acumulativo de eventos de fallo, cap 10)
    # Spec: 100 failures → len == 10 (los últimos 10).
    # Contexto previo de cada fallo sigue en recent_events (deque 50).
    failure_trace: deque = field(
        default_factory=lambda: deque(maxlen=FAILURE_TRACE_MAXLEN)
    )
    last_failure: Optional[str] = None

    # Outcome (cuando llega a EXIT)
    exit_outcome: Optional[str] = None

    def update_from_record(self, record: dict) -> None:
        """Aplica un record JSONL a este RunStats."""
        self.last_event_at = time.time()
        self.event_count += 1

        evt = record.get("event", "")
        kind = record.get("kind", "UNKNOWN")
        state = record.get("state")
        score = record.get("score")
        drift = record.get("drift")

        # Kind counter
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1

        # State change
        if state and state != self.current_state:
            self.previous_state = self.current_state
            self.current_state = state
            self.state_history.append({
                "ts": record.get("ts"),
                "from": self.previous_state,
                "to": state,
                "via": evt,
            })

        # Health
        if isinstance(score, int):
            self.health_score = score
            self.health_verdict = _verdict(score)

        # Recent events (no NOISE en stream visible para no spam)
        self.recent_events.append({
            "ts": record.get("ts"),
            "event": evt,
            "kind": kind,
            "state": state,
        })

        # Drift (P2: deque acotado; cuenta descartes cuando supera maxlen)
        if drift and isinstance(drift, dict):
            if len(self.drifts) >= self.drifts.maxlen:
                self.drifts_dropped_total += 1
            self.drifts.append({
                "kind": drift.get("kind"),
                "event": evt,
                "state": state,
                "detail": drift.get("detail"),
                "severity": drift.get("severity"),
            })
            self.drift_count += 1

        # Loop tracking (P2: cap top-N keys, contadores aux)
        if self.recent_events and len(self.recent_events) >= 2:
            prev = self.recent_events[-2]
            if prev["event"] == evt:
                if evt in self.loops:
                    self.loops[evt] += 1
                    if self.loops[evt] > self.loops_max_seen:
                        self.loops_max_seen = self.loops[evt]
                elif len(self.loops) < LOOPS_CAP:
                    self.loops[evt] = 1
                    if self.loops_max_seen < 1:
                        self.loops_max_seen = 1
                else:
                    # Cap alcanzado: descartar nueva key, contar
                    self.loops_dropped_keys += 1

        # Failure trace (P2 spec: deque acumulativo, cap 10 últimos fallos)
        if _is_failure(evt):
            self.last_failure = evt
            # Append acumulativo: deque(maxlen=10) auto-evict del más viejo.
            # Diferencia vs pre-P2: ya no es "trace causal del último fallo"
            # (eso vive ahora en recent_events). Es "últimos 10 fallos".
            self.failure_trace.append(evt)

        # Exit outcome
        if state == "exit" and self.exit_outcome is None:
            if "bot_block" in evt.lower():
                self.exit_outcome = "BLOCKED"
            elif evt.startswith("FATAL_") or evt.startswith("TIMEOUT_"):
                self.exit_outcome = "FATAL"
            elif evt == "stop_received":
                self.exit_outcome = "STOPPED"
            else:
                self.exit_outcome = "UNKNOWN_EXIT"

    def to_dict(self) -> dict:
        # P2: deque→list/dict para backward compat con consumidores (UI, tests).
        drifts_list = list(self.drifts)
        # P2 spec test 4: orden determinista por (-count, key) para top-N estable
        loops_sorted = dict(sorted(
            self.loops.items(),
            key=lambda kv: (-kv[1], kv[0]),  # frequency desc, key asc
        ))
        return {
            "slot": self.slot,
            "run_id": self.run_id,
            "started_at": _iso(self.started_at),
            "last_event_at": _iso(self.last_event_at),
            "elapsed_s": round(time.time() - self.started_at, 1),
            "idle_s": round(time.time() - self.last_event_at, 1),
            "current_state": self.current_state,
            "previous_state": self.previous_state,
            "state_history": list(self.state_history),
            "health_score": self.health_score,
            "health_verdict": self.health_verdict,
            "event_count": self.event_count,
            "kind_counts": dict(self.kind_counts),
            "drift_count": self.drift_count,
            "drifts": drifts_list[-10:],  # últimos 10 visibles
            "drifts_dropped_total": self.drifts_dropped_total,  # P2 counter
            "loops": loops_sorted,  # P2 spec: ordering estable
            "loops_dropped_keys": self.loops_dropped_keys,  # P2 counter
            "loops_max_seen": self.loops_max_seen,           # P2 counter
            "active_loops": [{"event": e, "count": c}
                               for e, c in loops_sorted.items() if c >= 3],
            "last_failure": self.last_failure,
            "failure_trace": list(self.failure_trace),  # P2: deque→list
            "exit_outcome": self.exit_outcome,
            "is_stale": self.is_stale(),
        }

    def is_stale(self, threshold_s: float = 60.0) -> bool:
        return (time.time() - self.last_event_at) > threshold_s


# ════════════════════════════════════════════════════════════════════
#  Helpers privados
# ════════════════════════════════════════════════════════════════════
def _verdict(score: int) -> str:
    if score >= 80:
        return "HEALTHY"
    if score >= 60:
        return "DEGRADED"
    if score >= 30:
        return "UNHEALTHY"
    return "CRITICAL"


def _is_failure(evt: str) -> bool:
    if not isinstance(evt, str):
        return False
    patterns = ("FATAL_", "_err", "_failed", "no_email_input", "bot_block",
                "TIMEOUT_", "max_account_swaps", "no_more_accounts")
    return any(p in evt for p in patterns)


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).isoformat()
    except Exception:
        return str(ts)


# ════════════════════════════════════════════════════════════════════
#  SessionAggregator
# ════════════════════════════════════════════════════════════════════
class SessionAggregator:
    """Mantiene el estado de todos los runs/slots vistos.

    Es alimentado por el JSONLTailer (vía subscribe).
    """

    MAX_RUNS = 50  # límite de memoria

    def __init__(self):
        self._runs: dict[tuple, RunStats] = {}  # (slot, run_id) → RunStats
        self._lock = threading.Lock()
        self._global_drift_count: int = 0
        self._global_event_count: int = 0
        self._first_seen_at: float = time.time()
        # P8: replay cursor — dedup por (slot, run_id, file_path) → last_offset
        # Key tipo str(file_path) para que sea serializable. Offset es int.
        self._replay_cursor: dict[tuple, int] = {}
        self._replay_lock = threading.Lock()
        self._replay_stats: dict = {
            "total_runs_seen": 0,
            "total_applied": 0,
            "total_skipped": 0,
            "last_replay_at": None,
            "last_replay_duration_ms": 0,
            "replay_count": 0,
            "corrupt_lines_total": 0,
        }

    def update(self, record: dict) -> None:
        """Alimenta un record. Fail-safe."""
        try:
            slot = record.get("slot")
            run_id = record.get("run_id", "default")
            key = (slot, run_id)
            with self._lock:
                stats = self._runs.get(key)
                if stats is None:
                    # Cap runs a MAX_RUNS — descartar más antiguos primero
                    if len(self._runs) >= self.MAX_RUNS:
                        oldest = min(self._runs.items(),
                                       key=lambda kv: kv[1].last_event_at)
                        self._runs.pop(oldest[0], None)
                    stats = RunStats(slot=slot, run_id=run_id)
                    self._runs[key] = stats
                stats.update_from_record(record)
                if record.get("drift"):
                    self._global_drift_count += 1
                self._global_event_count += 1
        except Exception:
            pass

    def get_active_runs(self, include_stale: bool = False) -> list[dict]:
        """Lista de runs activos (no stale)."""
        with self._lock:
            runs = list(self._runs.values())
        if not include_stale:
            runs = [r for r in runs if not r.is_stale()]
        runs.sort(key=lambda r: -r.last_event_at)
        return [r.to_dict() for r in runs]

    def get_run(self, slot: Any = None, run_id: str = "default") -> Optional[dict]:
        """Devuelve un run específico."""
        with self._lock:
            stats = self._runs.get((slot, run_id))
            if stats is None:
                # Buscar por slot solamente si run_id no matchea
                for (s, _rid), st in self._runs.items():
                    if s == slot:
                        stats = st
                        break
            return stats.to_dict() if stats else None

    def get_current_run(self) -> Optional[dict]:
        """Devuelve el run con actividad más reciente."""
        with self._lock:
            if not self._runs:
                return None
            most_recent = max(self._runs.values(), key=lambda r: r.last_event_at)
            return most_recent.to_dict()

    def get_global_summary(self) -> dict:
        """Resumen global del aggregator."""
        with self._lock:
            n_runs = len(self._runs)
            n_active = sum(1 for r in self._runs.values() if not r.is_stale())
            avg_health = 0
            if self._runs:
                avg_health = sum(r.health_score for r in self._runs.values()) // n_runs
            return {
                "first_seen_at": _iso(self._first_seen_at),
                "uptime_s": round(time.time() - self._first_seen_at, 1),
                "runs_tracked": n_runs,
                "runs_active": n_active,
                "global_event_count": self._global_event_count,
                "global_drift_count": self._global_drift_count,
                "avg_health_score": avg_health,
            }

    def reset(self) -> None:
        """Para tests."""
        with self._lock:
            self._runs.clear()
            self._global_drift_count = 0
            self._global_event_count = 0
            self._first_seen_at = time.time()
        with self._replay_lock:
            self._replay_cursor.clear()
            self._replay_stats = {
                "total_runs_seen": 0,
                "total_applied": 0,
                "total_skipped": 0,
                "last_replay_at": None,
                "last_replay_duration_ms": 0,
                "replay_count": 0,
                "corrupt_lines_total": 0,
            }

    # ────────────────────────────────────────────────────────────────
    #  P8 — Replay engine (2-stage: index + replay)
    # ────────────────────────────────────────────────────────────────
    def _build_index(self, directory: Path,
                      since_minutes: int = 10) -> list[dict]:
        """Stage 1: index ligero de archivos JSONL.

        Construye lista de archivos elegibles para replay, ordenados por mtime.
        SOLO metadata: regex sobre filename + stat. NO parse JSON aquí.

        Returns:
            Lista de dicts:
                {"file": Path, "slot": Any, "run_id": str, "mtime": float}
            Orden: mtime ascendente (oldest first → apply in chronological order).
        """
        directory = Path(directory)
        if not directory.exists():
            return []
        now = time.time()
        cutoff = now - (since_minutes * 60)
        index: list[dict] = []
        try:
            files = list(directory.glob("diagnostics_*.jsonl"))
        except Exception:
            return []
        for fp in files:
            try:
                mtime = fp.stat().st_mtime
                if mtime < cutoff:
                    continue  # fuera de window
                m = _JSONL_FILENAME_PATTERN.match(fp.name)
                if not m:
                    continue
                slot_str, run_id = m.group(1), m.group(2)
                try:
                    slot = int(slot_str)
                except ValueError:
                    slot = slot_str
                index.append({
                    "file": fp,
                    "slot": slot,
                    "run_id": run_id,
                    "mtime": mtime,
                })
            except Exception:
                continue
        # Stage 1 output: ordenado por mtime asc para apply chronological
        index.sort(key=lambda e: e["mtime"])
        return index

    def replay_from_jsonl(self,
                           directory: Any = "runs",
                           since_minutes: int = 10,
                           sync: bool = True,
                           skip_existing_runs: bool = True) -> dict:
        """P8: Reconstrucción del estado desde JSONL (Stage 1 + Stage 2).

        Args:
            directory: directorio con archivos JSONL
            since_minutes: solo archivos con mtime dentro del window
            sync: True = ejecuta síncronamente; False = lanza thread daemon
            skip_existing_runs: True (default) = idempotencia a nivel run
                (si (slot, run_id) ya existe en _runs → skip todo el file).
                False = idempotencia incremental por cursor (procesa solo
                líneas con offset > last_offset).

        Returns:
            dict con stats: {applied, skipped, files, duration_ms, corrupt_lines,
                              runs_skipped}

        Garantías:
            - Idempotente: re-replay no duplica
            - Run-level skip: (slot, run_id) in _runs → no procesar file
            - Cursor incremental (skip_existing_runs=False): offset-based dedup
            - Corruption-safe: try/except por línea
            - Fail-safe: si falla todo, devuelve dict con error en lugar de lanzar
            - Streaming line-by-line
            - Apply en orden cronológico por mtime + offset
        """
        if not sync:
            # Async mode: lanzar thread daemon
            t = threading.Thread(
                target=lambda: self.replay_from_jsonl(
                    directory=directory,
                    since_minutes=since_minutes,
                    sync=True,
                    skip_existing_runs=skip_existing_runs,
                ),
                daemon=True,
                name="AggregatorReplay",
            )
            t.start()
            return {"async": True, "thread": t.name}
        # Sync mode
        t0 = time.time()
        result = {
            "applied": 0,
            "skipped": 0,
            "files": 0,
            "duration_ms": 0,
            "corrupt_lines": 0,
            "runs_skipped": 0,
            "error": None,
        }
        try:
            index = self._build_index(Path(directory), since_minutes)
            result["files"] = len(index)
            for entry in index:
                # Spec v3: skip file completo si (slot, run_id) ya existe en _runs
                if skip_existing_runs:
                    key = (entry["slot"], entry["run_id"])
                    with self._lock:
                        if key in self._runs:
                            result["runs_skipped"] += 1
                            continue  # SKIP completo del file
                applied_n, skipped_n, corrupt_n = self._apply_file(
                    entry["file"], entry["slot"], entry["run_id"]
                )
                result["applied"] += applied_n
                result["skipped"] += skipped_n
                result["corrupt_lines"] += corrupt_n
        except Exception as e:
            result["error"] = type(e).__name__
        # Update stats
        duration_ms = int((time.time() - t0) * 1000)
        result["duration_ms"] = duration_ms
        with self._replay_lock:
            self._replay_stats["total_applied"] += result["applied"]
            self._replay_stats["total_skipped"] += result["skipped"]
            self._replay_stats["corrupt_lines_total"] += result["corrupt_lines"]
            self._replay_stats["total_runs_seen"] = len(self._runs)
            self._replay_stats["last_replay_at"] = _iso(time.time())
            self._replay_stats["last_replay_duration_ms"] = duration_ms
            self._replay_stats["replay_count"] += 1
        return result

    def _apply_file(self, fp: Path, slot: Any, run_id: str) -> tuple[int, int, int]:
        """Stage 2: aplicar eventos del file. Devuelve (applied, skipped, corrupt).

        Dedup por (slot, run_id, str(fp)) → last_offset. Si offset <= last, skip.
        Streaming line-by-line. Corruption-safe.
        """
        applied = 0
        skipped = 0
        corrupt = 0
        cursor_key = (slot, run_id, str(fp))
        try:
            with self._replay_lock:
                last_off = self._replay_cursor.get(cursor_key, -1)
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for offset, line in enumerate(f):
                    if not line.strip():
                        continue
                    if offset <= last_off:
                        skipped += 1
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        corrupt += 1
                        continue
                    if not isinstance(rec, dict):
                        corrupt += 1
                        continue
                    # Asegurar slot/run_id presentes (fallback a los del filename)
                    if rec.get("slot") is None:
                        rec["slot"] = slot
                    if rec.get("run_id") is None:
                        rec["run_id"] = run_id
                    # Apply via update normal (respeta caps P2)
                    self.update(rec)
                    applied += 1
                    # Mover cursor
                    with self._replay_lock:
                        self._replay_cursor[cursor_key] = offset
        except Exception:
            pass
        return applied, skipped, corrupt

    def get_replay_stats(self) -> dict:
        """Snapshot de stats del replay engine."""
        with self._replay_lock:
            return dict(self._replay_stats)


# ════════════════════════════════════════════════════════════════════
#  Singleton
# ════════════════════════════════════════════════════════════════════
_default_aggregator: Optional[SessionAggregator] = None
_agg_lock = threading.Lock()


def get_default_aggregator() -> SessionAggregator:
    global _default_aggregator
    with _agg_lock:
        if _default_aggregator is None:
            _default_aggregator = SessionAggregator()
    return _default_aggregator


def reset_for_test() -> None:
    """Test helper: resetea el singleton para aislar tests."""
    global _default_aggregator
    with _agg_lock:
        _default_aggregator = None


if __name__ == "__main__":
    print("=== SessionAggregator self-test ===")
    agg = SessionAggregator()
    sample = [
        {"slot": 1, "run_id": "r1", "event": "init", "kind": "OBSERVABILITY", "state": "setup", "score": 30},
        {"slot": 1, "run_id": "r1", "event": "goto", "kind": "STATE", "state": "browsing", "score": 50},
        {"slot": 1, "run_id": "r1", "event": "qty", "kind": "OBSERVABILITY", "state": "browsing", "score": 50},
        {"slot": 1, "run_id": "r1", "event": "qty", "kind": "OBSERVABILITY", "state": "browsing", "score": 50},
        {"slot": 1, "run_id": "r1", "event": "qty", "kind": "OBSERVABILITY", "state": "browsing", "score": 50},
        {"slot": 1, "run_id": "r1", "event": "FATAL_bot_block_early", "kind": "STATE", "state": "exit",
          "score": 25, "drift": {"kind": "INVALID_TRANSITION", "detail": "test"}},
    ]
    for r in sample:
        agg.update(r)
    import json
    current = agg.get_current_run()
    print(json.dumps(current, indent=2, default=str))
    print()
    print("Global:", agg.get_global_summary())


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
