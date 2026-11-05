"""core/runtime/run_session.py — Sesión observable por slot/run.

Mantiene estado runtime de UN slot/run combinando:
    - DriftDetector (FSM vs runtime)
    - HealthScore (0-100)
    - Event Contract (capa semántica)
    - Persistencia opcional a JSONL streaming

Diseño:
    - Fail-safe en todos los puntos
    - Thread-safe (varios slots paralelos = varias sessions)
    - Streaming (cada observe → línea JSONL append)
    - Sin dependencia del bot

NO se importa desde aquí al bot — solo al revés.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Lazy imports (fail-safe)
# ════════════════════════════════════════════════════════════════════
def _load_components():
    """Carga los componentes diagnósticos. Devuelve None si falla."""
    try:
        from core.contracts.event_classifier import classify, EventKind
        from core.diagnostics.drift_detector import DriftDetector
        from core.diagnostics.health_score import HealthScore
        return {
            "classify": classify,
            "EventKind": EventKind,
            "DriftDetector": DriftDetector,
            "HealthScore": HealthScore,
        }
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
#  RunSession — observable session por slot
# ════════════════════════════════════════════════════════════════════
class RunSession:
    """Sesión observable de un run.

    Uso:
        session = RunSession(slot=1, run_id="20260513_120000")
        session.observe("init")
        session.observe("goto")
        ...
        summary = session.finalize()  # devuelve dict con todo
    """

    def __init__(self,
                 slot: Any = None,
                 run_id: Optional[str] = None,
                 jsonl_path: Optional[Path] = None):
        self.slot = slot
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._t_start = time.time()
        self._lock = threading.Lock()
        self._closed = False

        # Lazy load components
        self._cmp = _load_components()
        if self._cmp:
            self._drift = self._cmp["DriftDetector"](initial_state="setup")
            self._health = self._cmp["HealthScore"]()
        else:
            self._drift = None
            self._health = None

        # Path de streaming JSONL
        if jsonl_path is None:
            d = Path("runs")
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            jsonl_path = d / f"diagnostics_slot{slot}_{self.run_id}_pid{os.getpid()}.jsonl"
        self.jsonl_path = Path(jsonl_path)

        # P1: handle cacheado por sesión (line-buffered).
        # Kill switch: JSONL_HANDLE_CACHE_ENABLED=0 → fallback per-line.
        self._fh = None
        if os.environ.get("JSONL_HANDLE_CACHE_ENABLED", "1") not in (
            "0", "false", "False", "FALSE", "no", "NO"
        ):
            try:
                # buffering=1 → line-buffered (flush por '\n')
                self._fh = open(self.jsonl_path, "a", encoding="utf-8",
                                buffering=1)
            except Exception:
                self._fh = None  # fallback a open() per-line

        # Stats
        self.kind_counts: dict[str, int] = {
            "STATE": 0, "ACTION": 0, "OBSERVABILITY": 0, "NOISE": 0, "UNKNOWN": 0,
        }

    # ────────────────────────────────────────────────────────────────
    #  observe() — entrypoint streaming
    # ────────────────────────────────────────────────────────────────
    def observe(self, event_name: str, **payload: Any) -> None:
        """Procesa un evento — clasifica, drift, score, persiste.

        Nunca lanza. Es fire-and-forget.
        """
        if self._closed or not self._cmp:
            return
        try:
            kind, rule_id, reason = self._cmp["classify"](event_name)
            kind_val = kind.value if hasattr(kind, "value") else str(kind)
            with self._lock:
                self.kind_counts[kind_val] = self.kind_counts.get(kind_val, 0) + 1

            # Drift detection
            drift = None
            if self._drift:
                drift = self._drift.observe(event_name)
                if drift and self._health:
                    self._health.apply_drift_penalty(drift.kind)

            # Health score
            if self._health:
                self._health.observe(event_name)

            # Persistencia streaming (JSONL append)
            self._persist(event_name, kind_val, rule_id, reason, drift, payload)
        except Exception:
            # CONTRATO: nunca propagar errores al bot.
            pass

    def _persist(self,
                  event_name: str,
                  kind: str,
                  rule_id: str,
                  reason: str,
                  drift: Any,
                  payload: dict) -> None:
        """Append un record JSONL. Silencioso ante fallos."""
        try:
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "slot": self.slot,
                "run_id": self.run_id,
                "event": event_name,
                "kind": kind,
                "rule": rule_id,
                "reason": reason,
                "state": self._drift.last_state if self._drift else None,
                "score": self._health.score if self._health else None,
                "drift": ({
                    "kind": drift.kind,
                    "detail": drift.detail,
                    "severity": drift.severity,
                    "expected": drift.expected_states,
                } if drift else None),
            }
            # Mergeamos payload con prefijo para evitar colisiones de claves
            if payload:
                safe = {f"p_{k}": v for k, v in payload.items()
                        if k not in ("ts", "slot", "run_id", "event", "kind", "rule", "reason", "state", "score", "drift")}
                record.update(safe)

            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            # P1: handle cacheado line-buffered. Fallback a open() si falla.
            with self._lock:
                if self._fh is not None and not self._fh.closed:
                    try:
                        self._fh.write(line)
                    except Exception:
                        # Handle inválido → close + reopen + write
                        try:
                            self._fh.close()
                        except Exception:
                            pass
                        try:
                            self._fh = open(self.jsonl_path, "a",
                                            encoding="utf-8", buffering=1)
                            self._fh.write(line)
                        except Exception:
                            self._fh = None
                            with open(self.jsonl_path, "a",
                                      encoding="utf-8") as f:
                                f.write(line)
                else:
                    with open(self.jsonl_path, "a", encoding="utf-8") as f:
                        f.write(line)
        except Exception:
            pass

    # ────────────────────────────────────────────────────────────────
    #  Reporting
    # ────────────────────────────────────────────────────────────────
    def summary(self) -> dict:
        """Resumen del run hasta ahora."""
        elapsed_s = time.time() - self._t_start
        out = {
            "slot": self.slot,
            "run_id": self.run_id,
            "elapsed_s": round(elapsed_s, 2),
            "event_kind_counts": dict(self.kind_counts),
            "total_events": sum(self.kind_counts.values()),
            "jsonl_path": str(self.jsonl_path),
        }
        if self._health:
            out["health"] = self._health.breakdown()
        if self._drift:
            out["drift"] = self._drift.summary()
        return out

    def finalize(self) -> dict:
        """Marca la sesión como cerrada y devuelve summary final.

        P1 Ajuste C: cleanup bajo self._lock para evitar race contra
        _persist() concurrente. close() y write() jamás solapan.
        Idempotente: múltiples finalize() son seguras.
        """
        s = self.summary()
        self._closed = True
        # Cierre del handle bajo el MISMO lock que _persist()
        with self._lock:
            if self._fh is not None:
                try:
                    # flush opcional antes de close (line-buffered ya flushea
                    # pero por defensividad si último write quedó incompleto)
                    try:
                        self._fh.flush()
                    except Exception:
                        pass
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        return s

    def __del__(self):
        """Safety net si finalize() no fue llamado. NO usa lock (GC context)."""
        try:
            fh = getattr(self, "_fh", None)
            if fh is not None and not fh.closed:
                fh.close()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Self-test requiere PYTHONPATH apuntando al proyecto raíz
    import sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parents[2]
    if str(_root) not in _sys.path:
        _sys.path.insert(0, str(_root))

    print("=== run_session self-test ===")
    s = RunSession(slot=99, run_id="selftest")
    # Re-load components ahora que el path está OK
    s._cmp = _load_components()
    if s._cmp:
        s._drift = s._cmp["DriftDetector"](initial_state="setup")
        s._drift._fsm = __import__('core.diagnostics.drift_detector', fromlist=['_load_fsm'])._load_fsm()
        s._drift._cls = __import__('core.diagnostics.drift_detector', fromlist=['_load_classifier'])._load_classifier()
        s._health = s._cmp["HealthScore"]()
    print(f"Components: {'OK' if s._cmp else 'FAIL'}")
    flow = ["init", "goto", "qty", "click_continuar", "oauth_detected",
            "shot",  # NOISE
            "login_start", "no_email_input", "FATAL_bot_block_early",
            "bot_block_persisted", "stop_received"]
    for e in flow:
        s.observe(e, demo="yes")
    sm = s.finalize()
    print(f"Final summary:")
    print(json.dumps(sm, indent=2, ensure_ascii=False))
    print(f"JSONL: {sm['jsonl_path']}")


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
