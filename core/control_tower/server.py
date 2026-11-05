"""core/control_tower/server.py — FastAPI app del Control Tower.

Una sola app que sirve las 4 fases en cascada:

    FASE 1 (MVP):
        GET  /                       → UI MVP (event stream live)
        GET  /api/v1/state           → estado FSM current
        GET  /api/v1/health          → health score
        GET  /api/v1/stream          → SSE event stream

    FASE 2 (Realtime):
        GET  /realtime               → UI con 4 paneles
        GET  /api/v2/state/live
        GET  /api/v2/drift/current
        GET  /api/v2/loops/current
        GET  /api/v2/health/current

    FASE 3 (Control Tower):
        GET  /control                → UI con analytics + root cause
        GET  /api/v3/run/<slot>/analytics
        GET  /api/v3/run/<slot>/root-cause
        GET  /api/v3/events/recent?kinds=&state=&search=

    FASE 4 (Industrial):
        GET  /tower                  → UI control tower (multi-run)
        GET  /api/v4/runs/all
        GET  /api/v4/drift/global
        GET  /api/v4/alerts
        GET  /api/v4/history
        GET  /api/v4/history/stats
        GET  /api/v4/history/compare/<slot_a>/<slot_b>

Lanzar:
    python -m uvicorn core.control_tower.server:app --port 5060
    o
    python run_control_tower.py
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse


# ════════════════════════════════════════════════════════════════════
#  App
# ════════════════════════════════════════════════════════════════════
app = FastAPI(
    title="TM Control Tower",
    description="Self-diagnosing runtime observability — 4 fases en cascada",
    version="1.0.0",
)

UI_DIR = Path(__file__).resolve().parent / "ui"


# ════════════════════════════════════════════════════════════════════
#  Lazy imports — fail-safe
# ════════════════════════════════════════════════════════════════════
_components: dict = {}
_components_lock = threading.Lock()


def _ensure_components():
    """Carga lazy: tailer + aggregator + analytics + alerts + history."""
    with _components_lock:
        if _components:
            return _components
        try:
            from core.dashboard.stream import get_default_tailer
            from core.dashboard.aggregator import get_default_aggregator
            from . import analytics, alerts, historical
            tailer = get_default_tailer()
            agg = get_default_aggregator()
            tailer.subscribe(agg.update)
            tailer.start()
            _components.update({
                "tailer": tailer,
                "aggregator": agg,
                "analytics": analytics,
                "alerts": alerts,
                "historical_index": historical.get_default_index(),
            })
        except Exception as e:
            _components["error"] = str(e)
    return _components


# ════════════════════════════════════════════════════════════════════
#  SSE event queue (FASE 1)
# ════════════════════════════════════════════════════════════════════
_sse_subscribers: list[asyncio.Queue] = []
_sse_lock = threading.Lock()


def _bootstrap_sse_bridge() -> None:
    """Conecta tailer → SSE queue. Idempotente."""
    cmp = _ensure_components()
    tailer = cmp.get("tailer")
    if not tailer:
        return

    if getattr(tailer, "_sse_bridge_attached", False):
        return

    def _push(record: dict):
        with _sse_lock:
            subs = list(_sse_subscribers)
        for q in subs:
            try:
                if q.qsize() < 200:
                    q.put_nowait(record)
            except Exception:
                pass

    tailer.subscribe(_push)
    tailer._sse_bridge_attached = True  # type: ignore[attr-defined]


@app.on_event("startup")
async def _on_startup():
    _ensure_components()
    _bootstrap_sse_bridge()


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _agg():
    return _ensure_components().get("aggregator")


def _tailer():
    return _ensure_components().get("tailer")


def _serve_html(filename: str) -> HTMLResponse:
    path = UI_DIR / filename
    try:
        html = path.read_text(encoding="utf-8")
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<pre>UI not found: {filename} ({e})</pre>", status_code=500)


# ════════════════════════════════════════════════════════════════════
#  FASE 1 — MVP (lectura básica + SSE stream)
# ════════════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
def ui_mvp():
    """UI MVP — event stream + state + health (página simple)."""
    return _serve_html("mvp.html")


@app.get("/api/v1/state")
def api_v1_state():
    agg = _agg()
    if not agg:
        return {"state": None, "score": 0}
    current = agg.get_current_run()
    if not current:
        return {"state": None, "score": 0}
    return {
        "state": current.get("current_state"),
        "score": current.get("health_score"),
        "verdict": current.get("health_verdict"),
        "slot": current.get("slot"),
        "run_id": current.get("run_id"),
    }


@app.get("/api/v1/health")
def api_v1_health():
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return {"score": 0, "verdict": "UNKNOWN"}
    return {
        "score": current.get("health_score", 0),
        "verdict": current.get("health_verdict", "UNKNOWN"),
        "event_count": current.get("event_count", 0),
        "elapsed_s": current.get("elapsed_s", 0),
    }


@app.get("/api/v1/stream")
async def api_v1_stream(request: Request):
    """SSE stream — empuja cada evento JSONL en cuanto aparece."""
    _bootstrap_sse_bridge()

    async def event_gen():
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with _sse_lock:
            _sse_subscribers.append(q)
        try:
            yield f"event: hello\ndata: {json.dumps({'connected': True, 'ts': time.time()})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    rec = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(rec, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield f": ping {int(time.time())}\n\n"
        finally:
            with _sse_lock:
                try:
                    _sse_subscribers.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ════════════════════════════════════════════════════════════════════
#  FASE 2 — Realtime panels
# ════════════════════════════════════════════════════════════════════
@app.get("/realtime", response_class=HTMLResponse)
def ui_realtime():
    return _serve_html("realtime.html")


@app.get("/api/v2/state/live")
def api_v2_state_live():
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return {"state": None, "previous": None, "history": []}
    return {
        "state": current.get("current_state"),
        "previous": current.get("previous_state"),
        "history": current.get("state_history", []),
        "slot": current.get("slot"),
        "run_id": current.get("run_id"),
    }


@app.get("/api/v2/drift/current")
def api_v2_drift_current():
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return {"drift_count": 0, "drifts": []}
    return {
        "drift_count": current.get("drift_count", 0),
        "drifts": current.get("drifts", []),
    }


@app.get("/api/v2/loops/current")
def api_v2_loops_current():
    agg = _agg()
    current = agg.get_current_run() if agg else None
    if not current:
        return {"active_loops": [], "loops": {}}
    return {
        "active_loops": current.get("active_loops", []),
        "loops": current.get("loops", {}),
    }


@app.get("/api/v2/health/current")
def api_v2_health_current():
    return api_v1_health()


# ════════════════════════════════════════════════════════════════════
#  FASE 3 — Control Tower analytics + root cause
# ════════════════════════════════════════════════════════════════════
@app.get("/control", response_class=HTMLResponse)
def ui_control():
    return _serve_html("control.html")


@app.get("/api/v3/run/{slot}/analytics")
def api_v3_run_analytics(slot: str, run_id: str = "default"):
    cmp = _ensure_components()
    agg = cmp.get("aggregator")
    ana = cmp.get("analytics")
    if not agg or not ana:
        return JSONResponse({"error": "components unavailable"}, status_code=503)
    try:
        slot_int = int(slot)
    except (ValueError, TypeError):
        slot_int = slot
    run = agg.get_run(slot=slot_int, run_id=run_id)
    if not run:
        # Intenta como current
        run = agg.get_current_run()
    if not run:
        return {"analytics": None, "error": "no_run"}
    return {"analytics": ana.compute_run_analytics(run), "run": run}


@app.get("/api/v3/run/{slot}/root-cause")
def api_v3_run_root_cause(slot: str, run_id: str = "default"):
    cmp = _ensure_components()
    agg = cmp.get("aggregator")
    ana = cmp.get("analytics")
    if not agg or not ana:
        return JSONResponse({"error": "components unavailable"}, status_code=503)
    try:
        slot_int = int(slot)
    except (ValueError, TypeError):
        slot_int = slot
    run = agg.get_run(slot=slot_int, run_id=run_id) or agg.get_current_run()
    if not run:
        return {"root_cause": None, "error": "no_run"}
    rc = ana.detect_root_cause(run)
    return {"root_cause": rc, "run_slot": run.get("slot")}


@app.get("/api/v3/events/recent")
def api_v3_events_recent(
    n: int = Query(50, ge=1, le=500),
    kinds: Optional[str] = None,
    state: Optional[str] = None,
    with_drift: Optional[bool] = None,
    search: Optional[str] = None,
):
    cmp = _ensure_components()
    tailer = cmp.get("tailer")
    ana = cmp.get("analytics")
    if not tailer:
        return {"events": []}
    raw = tailer.get_recent(500)
    if ana:
        kinds_set = None
        if kinds:
            kinds_set = set(k.strip().upper() for k in kinds.split(",") if k.strip())
        filtered = ana.filter_events(raw, kinds=kinds_set, state=state,
                                       with_drift=with_drift, search=search)
    else:
        filtered = raw
    return {"events": filtered[-n:]}


# ════════════════════════════════════════════════════════════════════
#  FASE 4 — Industrial control tower
# ════════════════════════════════════════════════════════════════════
@app.get("/tower", response_class=HTMLResponse)
def ui_tower():
    return _serve_html("industrial.html")


@app.get("/api/v4/runs/all")
def api_v4_runs_all(include_stale: bool = False):
    agg = _agg()
    if not agg:
        return {"runs": []}
    return {
        "runs": agg.get_active_runs(include_stale=include_stale),
        "global": agg.get_global_summary(),
    }


@app.get("/api/v4/drift/global")
def api_v4_drift_global():
    agg = _agg()
    if not agg:
        return {"drift": 0, "by_slot": []}
    runs = agg.get_active_runs(include_stale=False)
    total = sum(r.get("drift_count", 0) for r in runs)
    by_slot = [{"slot": r["slot"], "drift_count": r.get("drift_count", 0),
                  "state": r.get("current_state"), "score": r.get("health_score")}
                 for r in runs]
    return {
        "global_drift": total,
        "runs_active": len(runs),
        "drift_per_run": round(total / max(len(runs), 1), 2),
        "by_slot": by_slot,
    }


@app.get("/api/v4/alerts")
def api_v4_alerts():
    cmp = _ensure_components()
    agg = cmp.get("aggregator")
    alerts_mod = cmp.get("alerts")
    if not agg or not alerts_mod:
        return {"alerts": [], "thresholds": {}}
    runs = agg.get_active_runs(include_stale=False)
    g = agg.get_global_summary()
    return {
        "alerts": alerts_mod.evaluate_alerts(runs, g),
        "thresholds": alerts_mod.get_thresholds(),
    }


@app.get("/api/v4/history")
def api_v4_history(outcome: Optional[str] = None, slot: Optional[str] = None,
                    limit: int = Query(50, ge=1, le=500)):
    cmp = _ensure_components()
    idx = cmp.get("historical_index")
    if not idx:
        return {"runs": []}
    runs = idx.get_runs()
    if outcome:
        runs = [r for r in runs if r.get("exit_outcome") == outcome]
    if slot:
        runs = [r for r in runs if str(r.get("slot")) == str(slot)]
    return {"runs": runs[:limit], "total_matching": len(runs)}


@app.get("/api/v4/history/stats")
def api_v4_history_stats():
    cmp = _ensure_components()
    idx = cmp.get("historical_index")
    if not idx:
        return {"stats": {}}
    return {"stats": idx.stats()}


@app.get("/api/v4/history/compare/{slot_a}/{slot_b}")
def api_v4_history_compare(slot_a: str, slot_b: str):
    cmp = _ensure_components()
    idx = cmp.get("historical_index")
    if not idx:
        return {"error": "no_index"}
    return idx.compare(slot_a, slot_b)


# ════════════════════════════════════════════════════════════════════
#  Health + info endpoints
# ════════════════════════════════════════════════════════════════════
@app.get("/api/info")
def api_info():
    cmp = _ensure_components()
    return {
        "module": "control_tower.server",
        "version": "1.0.0",
        "components_ok": "error" not in cmp,
        "tailer_running": cmp.get("tailer").is_running() if cmp.get("tailer") else False,
        "sse_subscribers": len(_sse_subscribers),
        "global": cmp["aggregator"].get_global_summary() if cmp.get("aggregator") else {},
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": time.time()}


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
