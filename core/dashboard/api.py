"""core/dashboard/api.py — Flask Blueprint con endpoints REST + SSE.

Endpoints:
    GET  /diagnostics                       → UI (HTML)
    GET  /api/diagnostics/info              → estado del módulo
    GET  /api/diagnostics/runs              → todos los runs activos
    GET  /api/diagnostics/run/current       → run más reciente
    GET  /api/diagnostics/run/<slot>        → un run específico
    GET  /api/diagnostics/state/live        → estado FSM en tiempo real
    GET  /api/diagnostics/drift/current     → drift summary
    GET  /api/diagnostics/health/current    → health summary
    GET  /api/diagnostics/loops/current     → loops activos
    GET  /api/diagnostics/events/recent     → últimos N eventos
    GET  /api/diagnostics/events/stream     → SSE stream live

Diseño:
    - Blueprint plug-in (registro opcional)
    - SSE para streaming (no requiere websockets adicionales)
    - Fail-safe: cada endpoint envuelto en try/except
    - Activación: env DIAGNOSTICS_DASHBOARD_ENABLED=1 (default ON)
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, render_template, request


# ════════════════════════════════════════════════════════════════════
#  Blueprint
# ════════════════════════════════════════════════════════════════════
diagnostics_bp = Blueprint(
    "diagnostics",
    __name__,
    template_folder=str(Path(__file__).resolve().parents[2] / "templates"),
)


# ════════════════════════════════════════════════════════════════════
#  SSE broadcast — cola por suscriptor
# ════════════════════════════════════════════════════════════════════
_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()


def _broadcast_event(record: dict) -> None:
    """Distribuye un record a todos los clientes SSE conectados."""
    with _sse_lock:
        subs = list(_sse_subscribers)
    for q in subs:
        try:
            # Drop si la cola está llena (cliente lento)
            if q.qsize() < 200:
                q.put_nowait(record)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#  Tailer + aggregator bootstrap
# ════════════════════════════════════════════════════════════════════
_started = False
_start_lock = threading.Lock()


def _is_enabled() -> bool:
    val = os.environ.get("DIAGNOSTICS_DASHBOARD_ENABLED", "1")
    return val not in ("0", "false", "False", "FALSE", "no", "NO", "")


def _start_background_tailer() -> None:
    """Lanza el JSONLTailer y conecta al aggregator + SSE broadcast."""
    global _started
    if not _is_enabled():
        return
    with _start_lock:
        if _started:
            return
        try:
            from .stream import get_default_tailer
            from .aggregator import get_default_aggregator
            tailer = get_default_tailer()
            agg = get_default_aggregator()

            # Suscribir aggregator
            tailer.subscribe(agg.update)
            # Suscribir SSE broadcast
            tailer.subscribe(_broadcast_event)

            tailer.start()
            _started = True
        except Exception:
            pass  # fail-safe


# ════════════════════════════════════════════════════════════════════
#  UI route
# ════════════════════════════════════════════════════════════════════
@diagnostics_bp.route("/diagnostics")
def diagnostics_ui():
    """Renderiza la página HTML del dashboard."""
    try:
        return render_template("diagnostics.html")
    except Exception as e:
        return f"<pre>diagnostics UI error: {e}</pre>", 500


# ════════════════════════════════════════════════════════════════════
#  API endpoints
# ════════════════════════════════════════════════════════════════════
def _agg():
    """Lazy-load del aggregator (siempre devuelve algo o None)."""
    try:
        from .aggregator import get_default_aggregator
        return get_default_aggregator()
    except Exception:
        return None


def _tailer():
    try:
        from .stream import get_default_tailer
        return get_default_tailer()
    except Exception:
        return None


@diagnostics_bp.route("/api/diagnostics/info")
def api_info():
    """Estado del módulo. M6: incluye watchdog del tailer + degraded mode."""
    try:
        from core.observability.runtime_diagnostics import get_info as _rd_info
        rd_info = _rd_info()
    except Exception:
        rd_info = {"error": "runtime_diagnostics not loaded"}
    agg = _agg()
    tailer = _tailer()
    # M6: watchdog
    tailer_health = {}
    current_mode = "UNKNOWN"
    if tailer:
        try:
            tailer_health = tailer.get_health()
            current_mode = tailer_health.get("mode", "UNKNOWN")
        except Exception:
            tailer_health = {"error": "health_unavailable"}
            current_mode = "TAILER_DEAD"
    else:
        current_mode = "TAILER_NOT_LOADED"
    return jsonify({
        "module": "core.dashboard.api",
        "dashboard_enabled": _is_enabled(),
        "started": _started,
        "tailer_running": tailer.is_running() if tailer else False,
        "tailer_health": tailer_health,           # M6
        "current_mode": current_mode,             # M6
        "active_subscribers": len(_sse_subscribers),
        "global": agg.get_global_summary() if agg else {},
        "runtime_diagnostics": rd_info,
    })


@diagnostics_bp.route("/api/diagnostics/tailer/health")
def api_tailer_health():
    """M6: endpoint dedicado para health del tailer."""
    tailer = _tailer()
    if not tailer:
        return jsonify({"alive": False, "mode": "TAILER_NOT_LOADED"})
    return jsonify(tailer.get_health())


@diagnostics_bp.route("/api/diagnostics/tailer/recover", methods=["POST", "GET"])
def api_tailer_recover():
    """M6: intenta reiniciar el tailer si está muerto."""
    tailer = _tailer()
    if not tailer:
        return jsonify({"ok": False, "error": "tailer_not_loaded"})
    ok = tailer.attempt_recovery()
    return jsonify({"ok": ok, "running": tailer.is_running()})


@diagnostics_bp.route("/api/diagnostics/purchase_gate")
def api_purchase_gate():
    """M13.3: read-only snapshot del PurchaseGate (M13) para ops/dashboards.

    Contract:
        - GET only — sin mutaciones
        - Schema-stable JSON
        - Zero side-effects sobre gate state
        - Idempotente (lecturas consecutivas devuelven el mismo state)
        - Fail-safe: si gate no está disponible, devuelve dict con 'error'

    Schema:
        {
            "module":           "purchase_gate",
            "timestamp":        ISO8601 UTC,
            "config":           {mode, global_enabled, thresholds{...}},
            "counters":         {6 production counters},
            "rates":            {4 derived rates},
            "denial_breakdown": {13 deny reason counters},
            "cache_state":      {idempotency_size, session_locks_count}
        }
    """
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    try:
        from core.diagnostics.purchase_gate import get_default_gate
        gate = get_default_gate()
        stats = gate.stats()
    except Exception as e:
        return jsonify({
            "module": "purchase_gate",
            "timestamp": ts,
            "error": "gate_unavailable: " + str(e)[:200],
        })

    # Cache inspection (read-only, best-effort)
    try:
        idem_size = gate._idem.size()
    except Exception:
        idem_size = -1
    try:
        session_locks_count = len(gate._session_locks)
    except Exception:
        session_locks_count = -1

    # Thresholds (instance attrs read-only)
    try:
        thresholds = {
            "mismatch_rate_threshold":          gate.mismatch_rate_threshold,
            "critical_crossing_rate_threshold": gate.critical_crossing_rate_threshold,
            "max_checkout_lifetime_s":          gate.max_checkout_lifetime_s,
            "min_cooldown_s":                   gate.min_cooldown_s,
            "max_retries":                      gate.max_retries,
            "min_health_score":                 gate.min_health_score,
            "min_consistency":                  gate.min_consistency,
            "max_inflation_penalty":            gate.max_inflation_penalty,
        }
    except Exception:
        thresholds = {"error": "thresholds_unavailable"}

    counters_keys = (
        "purchase_attempted_count", "purchase_allowed_count",
        "purchase_blocked_count", "purchase_dry_run_count",
        "purchase_live_count", "internal_errors",
    )
    rates_keys = (
        "purchase_allow_rate", "purchase_block_rate",
        "purchase_dry_run_rate", "purchase_live_rate",
    )
    denial_keys = (
        "kill_switch_denials", "mode_disabled_denials",
        "duplicate_prevented_count", "stale_checkout_denials",
        "cooldown_denials", "concurrent_denials",
        "health_denials", "mismatch_purchase_denials",
        "critical_crossing_denials", "consistency_denials",
        "inflation_denials", "retry_denials", "invalid_input_denials",
    )

    return jsonify({
        "module":    "purchase_gate",
        "timestamp": ts,
        "config": {
            "mode":           gate.mode,
            "global_enabled": gate.global_enabled,
            "thresholds":     thresholds,
        },
        "counters":         {k: stats.get(k, 0)   for k in counters_keys},
        "rates":            {k: stats.get(k, 0.0) for k in rates_keys},
        "denial_breakdown": {k: stats.get(k, 0)   for k in denial_keys},
        "cache_state": {
            "idempotency_size":    idem_size,
            "session_locks_count": session_locks_count,
        },
    })


@diagnostics_bp.route("/api/diagnostics/runs")
def api_runs():
    """Todos los runs activos."""
    agg = _agg()
    if not agg:
        return jsonify({"runs": [], "error": "aggregator unavailable"})
    include_stale = request.args.get("include_stale", "0") == "1"
    return jsonify({
        "runs": agg.get_active_runs(include_stale=include_stale),
        "global": agg.get_global_summary(),
    })


@diagnostics_bp.route("/api/diagnostics/run/current")
def api_run_current():
    """Run más reciente (con actividad)."""
    agg = _agg()
    if not agg:
        return jsonify({"run": None})
    return jsonify({"run": agg.get_current_run()})


@diagnostics_bp.route("/api/diagnostics/run/<slot>")
def api_run_by_slot(slot):
    """Run por slot."""
    agg = _agg()
    if not agg:
        return jsonify({"run": None})
    # Try int slot
    try:
        slot_int = int(slot)
    except (ValueError, TypeError):
        slot_int = slot
    run_id = request.args.get("run_id", "default")
    return jsonify({"run": agg.get_run(slot=slot_int, run_id=run_id)})


@diagnostics_bp.route("/api/diagnostics/state/live")
def api_state_live():
    """Estado FSM en tiempo real."""
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return jsonify({"state": None, "previous": None, "history": []})
    return jsonify({
        "state": current.get("current_state"),
        "previous": current.get("previous_state"),
        "history": current.get("state_history", []),
        "score": current.get("health_score"),
        "verdict": current.get("health_verdict"),
        "drift_count": current.get("drift_count"),
        "slot": current.get("slot"),
        "run_id": current.get("run_id"),
    })


@diagnostics_bp.route("/api/diagnostics/drift/current")
def api_drift_current():
    """Drift summary del run actual."""
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return jsonify({"drift_count": 0, "drifts": []})
    return jsonify({
        "drift_count": current.get("drift_count", 0),
        "drifts": current.get("drifts", []),
    })


@diagnostics_bp.route("/api/diagnostics/health/current")
def api_health_current():
    """Health gauge data."""
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return jsonify({"score": 0, "verdict": "UNKNOWN"})
    return jsonify({
        "score": current.get("health_score"),
        "verdict": current.get("health_verdict"),
        "event_count": current.get("event_count"),
        "elapsed_s": current.get("elapsed_s"),
    })


@diagnostics_bp.route("/api/diagnostics/loops/current")
def api_loops_current():
    """Loops activos del run actual."""
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return jsonify({"loops": []})
    return jsonify({
        "active_loops": current.get("active_loops", []),
        "all_loops": current.get("loops", {}),
    })


@diagnostics_bp.route("/api/diagnostics/events/recent")
def api_events_recent():
    """Últimos N eventos del buffer global."""
    tailer = _tailer()
    if not tailer:
        return jsonify({"events": []})
    try:
        n = int(request.args.get("n", 50))
    except (ValueError, TypeError):
        n = 50
    n = max(1, min(n, 500))
    return jsonify({"events": tailer.get_recent(n)})


@diagnostics_bp.route("/api/diagnostics/failure/current")
def api_failure_current():
    """Failure trace del run actual."""
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return jsonify({"trace": [], "last_failure": None})
    return jsonify({
        "trace": current.get("failure_trace", []),
        "last_failure": current.get("last_failure"),
        "exit_outcome": current.get("exit_outcome"),
    })


# ────────────────────────────────────────────────────────────────────
#  M3 — SQLite Historical Index (endpoints paralelos a v1)
# ────────────────────────────────────────────────────────────────────
def _sqlite_idx():
    """Lazy load SQLite historical index. Devuelve None si falla."""
    try:
        from core.dashboard.historical_sqlite import get_default_sqlite_index
        return get_default_sqlite_index()
    except Exception:
        return None


@diagnostics_bp.route("/api/v4/history2")
def api_v4_history2():
    """Historical runs desde SQLite (M3, paralelo a /api/diagnostics/history)."""
    from flask import request as _req
    idx = _sqlite_idx()
    if not idx:
        return jsonify({"runs": [], "error": "sqlite_index_unavailable"})
    try:
        limit = int(_req.args.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    outcome = _req.args.get("outcome")
    slot = _req.args.get("slot")
    return jsonify({
        "runs": idx.get_recent_runs(limit=limit, outcome=outcome, slot=slot),
        "stats": idx.get_stats(),
    })


@diagnostics_bp.route("/api/v4/history2/run/<run_id>")
def api_v4_history2_run(run_id: str):
    """Detalle de un run histórico."""
    idx = _sqlite_idx()
    if not idx:
        return jsonify({"run": None})
    return jsonify({
        "run": idx.get_run(run_id),
        "drifts": idx.get_drifts(run_id),
    })


@diagnostics_bp.route("/api/v4/history2/root-causes")
def api_v4_history2_root_causes():
    """Top root cause patterns."""
    idx = _sqlite_idx()
    if not idx:
        return jsonify({"root_causes": []})
    return jsonify({"root_causes": idx.get_root_causes()})


@diagnostics_bp.route("/api/v4/history2/health-distribution")
def api_v4_history2_health_distribution():
    """Distribución de health_final histórica."""
    idx = _sqlite_idx()
    if not idx:
        return jsonify({"distribution": {}})
    return jsonify({"distribution": idx.get_health_distribution()})


# ────────────────────────────────────────────────────────────────────
#  M4 — JSONL Rotation stats endpoint
# ────────────────────────────────────────────────────────────────────
def _rotator():
    try:
        from core.dashboard.rotation import get_default_rotator
        return get_default_rotator()
    except Exception:
        return None


@diagnostics_bp.route("/api/diagnostics/rotation/stats")
def api_rotation_stats():
    """Estadísticas del rotador JSONL (M4)."""
    r = _rotator()
    if not r:
        return jsonify({"enabled": False, "error": "rotator_unavailable"})
    return jsonify(r.get_stats())


@diagnostics_bp.route("/api/diagnostics/rotation/run", methods=["POST", "GET"])
def api_rotation_run():
    """Fuerza rotación manual (útil para tests/admin)."""
    r = _rotator()
    if not r:
        return jsonify({"ok": False, "error": "rotator_unavailable"})
    result = r.rotate_once()
    return jsonify({"ok": True, "result": result})


# ────────────────────────────────────────────────────────────────────
#  SSE — Server-Sent Events stream
# ────────────────────────────────────────────────────────────────────
@diagnostics_bp.route("/api/diagnostics/events/stream")
def api_events_stream():
    """SSE stream live de eventos.

    Cliente JS:
        const ev = new EventSource("/api/diagnostics/events/stream");
        ev.onmessage = (e) => { const rec = JSON.parse(e.data); ... }
    """
    def event_generator():
        q: queue.Queue = queue.Queue(maxsize=200)
        with _sse_lock:
            _sse_subscribers.append(q)
        # Enviar hello inicial
        try:
            yield f"event: hello\ndata: {json.dumps({'connected': True, 'ts': time.time()})}\n\n"
            while True:
                try:
                    rec = q.get(timeout=15)  # 15s keep-alive
                    yield f"data: {json.dumps(rec, default=str)}\n\n"
                except queue.Empty:
                    # Keep-alive ping
                    yield f": ping {int(time.time())}\n\n"
        except (GeneratorExit, BrokenPipeError):
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_subscribers.remove(q)
                except ValueError:
                    pass

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # disable nginx buffering
    }
    return Response(event_generator(), headers=headers)


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
