"""core/dashboard/ — Self-diagnosing dashboard layer.

Lee del stream JSONL de `runtime_diagnostics` y expone endpoints HTTP.
NO toca el bot. NO escribe en el bot. Solo lee.

Componentes:
    stream      → JSONL tail en background thread
    aggregator  → agrupa runs/sessions en memoria
    api         → Flask Blueprint con endpoints REST + SSE

Activación:
    from core.dashboard import register_diagnostics
    register_diagnostics(flask_app)

Desactivación:
    Si el módulo falla al importar → dashboard original funciona igual.

P8 — Aggregator replay al startup (opt-in, background, non-blocking):
    AGGREGATOR_REPLAY_ENABLED=1     (default ON)
    AGGREGATOR_REPLAY_MINUTES=10    (window de mtime)
    AGGREGATOR_REPLAY_TIMEOUT_S=30  (timeout total background)
    AGGREGATOR_REPLAY_DIR=runs      (directorio JSONLs)
"""
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional


def _replay_enabled() -> bool:
    return os.environ.get("AGGREGATOR_REPLAY_ENABLED", "1") not in (
        "0", "false", "False", "FALSE", "no", "NO"
    )


_replay_thread: Optional[threading.Thread] = None
_replay_completed: bool = False
_replay_lock = threading.Lock()


def _bootstrap_worker():
    """P8 spec v3 fix: chain replay → tailer with offset pre-seeding.

    Steps:
        1) Replay (sync) rehydrates aggregator from existing JSONL files.
        2) Pre-seed tailer file offsets at EOF for already-replayed files.
           This ensures the tailer does NOT re-process lines the replay
           already applied (avoids double ingestion).
        3) Start tailer; it now only sees lines written AFTER bootstrap.

    Both steps run in this background thread. register_diagnostics() returns
    immediately. Tailer subscribers receive only live events from this point.
    """
    global _replay_completed
    # Step 1: replay (skip if disabled by env)
    if _replay_enabled():
        try:
            from .aggregator import get_default_aggregator
            agg = get_default_aggregator()
            since = int(os.environ.get("AGGREGATOR_REPLAY_MINUTES", "10"))
            runs_dir = os.environ.get("AGGREGATOR_REPLAY_DIR", "runs")
            agg.replay_from_jsonl(
                directory=Path(runs_dir),
                since_minutes=since,
                sync=True,  # sync within worker thread
                skip_existing_runs=True,  # run-level idempotency
            )
        except Exception:
            pass  # fail silent
    # Step 2: pre-seed tailer offsets at EOF for existing files.
    # This is the critical bit that avoids tailer re-processing files
    # the replay already covered.
    try:
        from .stream import get_default_tailer
        tailer = get_default_tailer()
        try:
            files = list(tailer.directory.glob(tailer.pattern))
        except Exception:
            files = []
        for fp in files:
            try:
                size = fp.stat().st_size
                with tailer._file_lock:
                    tailer._file_offsets[fp] = size
            except Exception:
                continue
    except Exception:
        pass
    # Step 3: tailer AFTER replay + offset seeding (live events only)
    try:
        from .api import _start_background_tailer
        _start_background_tailer()
    except Exception:
        pass
    # Mark completion
    with _replay_lock:
        _replay_completed = True


# Backward-compat alias (some tests reference the old name)
_replay_worker = _bootstrap_worker


def _replay_aggregator_on_startup() -> bool:
    """P8 spec v3: bootstrap secuencial replay → tailer en BACKGROUND thread.

    Garantías:
        - NUNCA bloquea Flask/API startup (register_diagnostics retorna <500ms)
        - Tailer NO procesa events antes de que replay termine (no dual ingestion)
        - Background thread daemon (muere con proceso)
        - Timeout watchdog (default 30s)
        - Fail-safe: errores silenciados
    """
    global _replay_thread, _replay_completed
    # Si replay desactivado pero tailer debería arrancar igual (legacy path)
    if not _replay_enabled():
        # Sin replay → arrancar tailer directo (no hay riesgo de doble ingesta)
        try:
            from .api import _start_background_tailer
            _start_background_tailer()
        except Exception:
            pass
        return False
    try:
        timeout_s = int(os.environ.get("AGGREGATOR_REPLAY_TIMEOUT_S", "30"))
    except (ValueError, TypeError):
        timeout_s = 30
    try:
        with _replay_lock:
            if _replay_thread is not None and _replay_thread.is_alive():
                return True  # ya en curso
            _replay_completed = False
            _replay_thread = threading.Thread(
                target=_bootstrap_worker,
                daemon=True,
                name="AggregatorReplayThenTailer",
            )
        _replay_thread.start()
        # Watchdog: thread separado que joinea con timeout y abandona
        def _watchdog():
            try:
                _replay_thread.join(timeout=timeout_s)
            except Exception:
                pass
        watchdog = threading.Thread(target=_watchdog, daemon=True,
                                       name="AggregatorReplayWatchdog")
        watchdog.start()
        return True
    except Exception:
        return False


def _replay_status() -> dict:
    """Para tests/diagnostics: estado actual del replay startup."""
    with _replay_lock:
        return {
            "completed": _replay_completed,
            "thread_alive": (_replay_thread is not None
                              and _replay_thread.is_alive()),
            "thread_name": _replay_thread.name if _replay_thread else None,
        }


def _reset_replay_for_test() -> None:
    """Test helper: reset state del replay startup."""
    global _replay_thread, _replay_completed
    with _replay_lock:
        _replay_thread = None
        _replay_completed = False


def register_diagnostics(app: Any) -> bool:
    """Registra el blueprint de diagnostics en una Flask app.

    Fail-safe: si algo falla, devuelve False sin lanzar.

    P8 spec v3: bootstrap secuencial replay → tailer en background thread.
    El tailer es arrancado por el worker DESPUÉS del replay para evitar
    doble ingestión del mismo JSONL en startup window.
    register_diagnostics() retorna inmediato (<500ms).
    """
    try:
        from .api import diagnostics_bp
        app.register_blueprint(diagnostics_bp)
        # P8 spec v3: replay primero, tailer después (ambos en background)
        _replay_aggregator_on_startup()
        return True
    except Exception:
        return False


__all__ = ["register_diagnostics"]


# ════════════════════════════════════════════════════════════════════
#  STEP 15 — Production validation dashboard (additive, opt-in imports)
#  ════════════════════════════════════════════════════════════════════
try:
    from core.dashboard.dashboard_state import build_system_snapshot
    from core.dashboard.health_aggregator import aggregate_health
    from core.dashboard.runtime_health import compute_runtime_health
    from core.dashboard.slo_engine import (
        SLOEvaluation, SLOStatus, SLOTargets, evaluate_slos,
    )
    from core.dashboard.anomaly_detection import (
        Alert, AlertSeverity, AnomalyConfig, scan_for_anomalies, write_alerts,
    )
    from core.dashboard.canary_decision_engine import (
        CanaryDecision, CanaryDecisionResult, decide,
        compute_legacy_readiness_score,
    )
    from core.dashboard.dead_code_detector import (
        build_dead_components_report,
        find_unused_modules, find_unemitted_event_codes,
        find_unused_feature_flags, find_zero_traffic_metrics,
    )
    __all__ += [
        "build_system_snapshot", "aggregate_health",
        "compute_runtime_health",
        "SLOTargets", "SLOStatus", "SLOEvaluation", "evaluate_slos",
        "AnomalyConfig", "Alert", "AlertSeverity",
        "scan_for_anomalies", "write_alerts",
        "CanaryDecision", "CanaryDecisionResult", "decide",
        "compute_legacy_readiness_score",
        "build_dead_components_report",
        "find_unused_modules", "find_unemitted_event_codes",
        "find_unused_feature_flags", "find_zero_traffic_metrics",
    ]
except Exception:
    # Fail-safe: si algún módulo nuevo falla, el dashboard P1-P8
    # sigue funcionando. La capa de validación es opt-in.
    pass
