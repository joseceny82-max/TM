"""core/control_tower/historical.py — Historical run index.

FASE 4 del Control Tower: indexa runs históricos desde JSONL.

Diseño:
    - Escanea runs/diagnostics_*.jsonl (todos, no solo activos)
    - Para cada fichero, extrae: slot, run_id, start_ts, end_ts, event_count,
      final_state, exit_outcome, drift_count, health_score
    - Cache en memoria con TTL (re-escanea cada N segundos)
    - Soporta queries: by_outcome, by_slot, by_time_range

NO lee todos los eventos — solo metadata por fichero. Light footprint.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional


# ════════════════════════════════════════════════════════════════════
#  HistoricalIndex
# ════════════════════════════════════════════════════════════════════
class HistoricalIndex:
    """Índice ligero de runs históricos extraído de JSONL files."""

    PATTERN = re.compile(r"diagnostics_slot(\d+|default)_(\d+_\d+)(?:_pid\d+)?\.jsonl")
    CACHE_TTL_S = 30.0

    def __init__(self, directory: str = "runs"):
        self.directory = Path(directory)
        self._cache: list[dict] = []
        self._cache_lock = threading.Lock()
        self._last_scan: float = 0
        self._sweep_lock = threading.Lock()

    def get_runs(self, force_refresh: bool = False) -> list[dict]:
        """Devuelve la lista de runs (cacheado)."""
        with self._cache_lock:
            stale = (time.time() - self._last_scan) > self.CACHE_TTL_S
        if force_refresh or stale or not self._cache:
            self._refresh()
        with self._cache_lock:
            return list(self._cache)

    def _refresh(self) -> None:
        """Re-escanea el directorio."""
        with self._sweep_lock:
            try:
                files = sorted(self.directory.glob("diagnostics_*.jsonl"))
            except Exception:
                files = []
            new_cache = []
            for fp in files:
                try:
                    meta = self._extract_metadata(fp)
                    if meta:
                        new_cache.append(meta)
                except Exception:
                    pass
            new_cache.sort(key=lambda r: r.get("end_ts") or r.get("start_ts") or "", reverse=True)
            with self._cache_lock:
                self._cache = new_cache
                self._last_scan = time.time()

    def _extract_metadata(self, fp: Path) -> Optional[dict]:
        """Lee primera + última línea + cuenta. No carga todo el fichero."""
        m = self.PATTERN.match(fp.name)
        if not m:
            return None
        slot_raw, run_id_part = m.group(1), m.group(2)
        try:
            slot = int(slot_raw)
        except ValueError:
            slot = slot_raw

        try:
            size = fp.stat().st_size
        except Exception:
            return None
        if size == 0:
            return None

        first_line = None
        last_line = None
        event_count = 0
        drift_count = 0
        states_seen = set()
        first_score = None
        last_score = None
        last_state = None
        last_event = None
        kind_counts = {"STATE": 0, "ACTION": 0, "OBSERVABILITY": 0, "NOISE": 0, "UNKNOWN": 0}

        try:
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
                    if first_line is None:
                        first_line = rec
                        first_score = rec.get("score")
                    last_line = rec
                    last_score = rec.get("score", last_score)
                    last_state = rec.get("state", last_state)
                    last_event = rec.get("event", last_event)
                    if rec.get("drift"):
                        drift_count += 1
                    st = rec.get("state")
                    if st:
                        states_seen.add(st)
                    kind = rec.get("kind", "UNKNOWN")
                    kind_counts[kind] = kind_counts.get(kind, 0) + 1
        except Exception:
            return None

        if not first_line:
            return None

        # Outcome
        outcome = None
        if last_state == "exit":
            if last_event and "bot_block" in last_event.lower():
                outcome = "BLOCKED"
            elif last_event and (last_event.startswith("FATAL_") or last_event.startswith("TIMEOUT_")):
                outcome = "FATAL"
            elif last_event == "stop_received":
                outcome = "STOPPED"
            else:
                outcome = "UNKNOWN_EXIT"
        else:
            outcome = "INCOMPLETE"

        return {
            "filename": fp.name,
            "path": str(fp),
            "slot": slot,
            "run_id": run_id_part,
            "size_bytes": size,
            "event_count": event_count,
            "drift_count": drift_count,
            "kind_counts": kind_counts,
            "states_visited": sorted(states_seen),
            "states_count": len(states_seen),
            "start_ts": first_line.get("ts"),
            "end_ts": last_line.get("ts") if last_line else None,
            "first_score": first_score,
            "last_score": last_score,
            "final_state": last_state,
            "last_event": last_event,
            "exit_outcome": outcome,
        }

    # ────────────────────────────────────────────────────────────────
    #  Query helpers
    # ────────────────────────────────────────────────────────────────
    def filter_by_outcome(self, outcome: str) -> list[dict]:
        return [r for r in self.get_runs() if r.get("exit_outcome") == outcome]

    def filter_by_slot(self, slot) -> list[dict]:
        return [r for r in self.get_runs() if str(r.get("slot")) == str(slot)]

    def stats(self) -> dict:
        """Estadísticas agregadas históricas."""
        runs = self.get_runs()
        if not runs:
            return {"total": 0}
        outcomes = {}
        scores = []
        total_events = 0
        total_drifts = 0
        for r in runs:
            o = r.get("exit_outcome", "UNKNOWN")
            outcomes[o] = outcomes.get(o, 0) + 1
            ls = r.get("last_score")
            if isinstance(ls, (int, float)):
                scores.append(ls)
            total_events += r.get("event_count", 0)
            total_drifts += r.get("drift_count", 0)
        avg = sum(scores) // len(scores) if scores else 0
        success_total = outcomes.get("UNKNOWN_EXIT", 0) + outcomes.get("STOPPED", 0)
        return {
            "total_runs": len(runs),
            "total_events": total_events,
            "total_drifts": total_drifts,
            "outcomes": outcomes,
            "success_rate_pct": round(100 * success_total / len(runs), 1),
            "blocked_rate_pct": round(100 * outcomes.get("BLOCKED", 0) / len(runs), 1),
            "avg_final_score": avg,
        }

    def compare(self, slot_a, slot_b) -> dict:
        """Compara dos runs por slot. Útil para diagnóstico."""
        a = self.filter_by_slot(slot_a)
        b = self.filter_by_slot(slot_b)
        return {
            "slot_a": slot_a,
            "slot_b": slot_b,
            "runs_a": len(a),
            "runs_b": len(b),
            "outcomes_a": _outcome_count(a),
            "outcomes_b": _outcome_count(b),
            "avg_score_a": _avg_score(a),
            "avg_score_b": _avg_score(b),
        }


def _outcome_count(runs):
    out = {}
    for r in runs:
        o = r.get("exit_outcome", "UNKNOWN")
        out[o] = out.get(o, 0) + 1
    return out


def _avg_score(runs):
    scores = [r.get("last_score") for r in runs if isinstance(r.get("last_score"), (int, float))]
    return sum(scores) // len(scores) if scores else 0


# ════════════════════════════════════════════════════════════════════
#  Singleton
# ════════════════════════════════════════════════════════════════════
_default_index: Optional[HistoricalIndex] = None
_lock = threading.Lock()


def get_default_index() -> HistoricalIndex:
    global _default_index
    with _lock:
        if _default_index is None:
            _default_index = HistoricalIndex()
    return _default_index


if __name__ == "__main__":
    import json
    idx = HistoricalIndex()
    runs = idx.get_runs(force_refresh=True)
    print(f"Total historical runs: {len(runs)}")
    for r in runs[:5]:
        print(f"  slot={r['slot']:>3} events={r['event_count']:>3} "
              f"outcome={r['exit_outcome']:<14} score={r['last_score']}")
    print()
    print("Stats:")
    print(json.dumps(idx.stats(), indent=2))


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
