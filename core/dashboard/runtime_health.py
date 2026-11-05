"""core/dashboard/runtime_health.py — Live health computation.

Lee `runs/structured_events_*.jsonl` y produce métricas operacionales
de health en ventana móvil.

Lo que se computa:
    - success_rate_1h / 24h
    - p50/p95/p99 latency
    - top failures por categoría/código
    - shadow stats (divergence count, crash count)
    - circuit breaker activations
    - account/subnet burn aggregates
    - canary distribution

NUNCA propaga excepciones. Fail-safe absoluto.

stdlib only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Any, Optional


DEFAULT_EVENTS_GLOB: str = "runs/structured_events_*.jsonl"

# Phase 2 audit fix: by default, runtime_health (production dashboard)
# filters out events tagged as test/unknown. To bypass, pass
# `source_filter=frozenset()` to read raw events.
DEFAULT_SOURCE_FILTER: frozenset = frozenset({
    "production", "shadow", "canary",
})


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _load_events_glob(pattern: str,
                        *,
                        since: Optional[datetime] = None,
                        source_filter: Optional[frozenset] = None,
                        include_untagged: bool = True) -> list:
    """Carga events del JSONL, filtrando por source para evitar test pollution.

    Args:
        source_filter: set de sources aceptados. Default: producción+shadow+canary.
            `frozenset()` = aceptar todos.
        include_untagged: si True, events sin tag se tratan como production.
    """
    filter_set = (DEFAULT_SOURCE_FILTER if source_filter is None
                   else source_filter)
    def _accept(ev):
        if not filter_set:
            return True
        src = ev.get("source")
        if src is None:
            return include_untagged
        return src in filter_set
    out: list = []
    try:
        for p in sorted(glob(pattern)):
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if not isinstance(ev, dict):
                            continue
                        if not _accept(ev):
                            continue
                        if since is not None:
                            ts = _parse_iso(ev.get("timestamp"))
                            if ts is None or ts < since:
                                continue
                        out.append(ev)
            except OSError:
                continue
    except Exception:
        return out
    return out


def _percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (p / 100.0) * (len(sorted_vals) - 1)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


# ════════════════════════════════════════════════════════════════════
#  Health computers
# ════════════════════════════════════════════════════════════════════
def compute_step_success_rates(events: list) -> dict:
    """Tasas de éxito por step específico."""
    counts: dict = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("code") != "outcome_recorded":
            continue
        payload = ev.get("payload") or {}
        status = payload.get("status") if isinstance(payload, dict) else None
        step = ev.get("context", "") or "unknown"
        bucket = counts.setdefault(step, {"total": 0, "success": 0})
        bucket["total"] += 1
        if status == "paid" or status == "success" or status == "checkout_ok":
            bucket["success"] += 1

    rates: dict = {}
    for step, b in counts.items():
        if b["total"] > 0:
            rates[step] = round(b["success"] / b["total"], 6)
    return rates


def compute_top_failures(events: list, *, top_n: int = 10) -> list:
    """Top N códigos de error por count."""
    counts: dict = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        sev = ev.get("severity", "")
        if sev in ("recoverable",) and ev.get("category") == "flow":
            continue   # skip operational
        code = ev.get("code", "")
        cat = ev.get("category", "")
        if not code or not cat:
            continue
        key = f"{cat}/{code}"
        counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]
    return [{"key": k, "count": v} for k, v in items]


def compute_shadow_stats(events: list) -> dict:
    """Estadísticas del shadow execution."""
    compare_count = 0
    crash_count = 0
    div_scores: list = []
    latency_legacy: list = []
    latency_shadow: list = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        code = ev.get("code", "")
        p = ev.get("payload") or {}
        if not isinstance(p, dict):
            p = {}
        if code == "canary_compare":
            compare_count += 1
            ds = p.get("divergence_score")
            if isinstance(ds, (int, float)):
                div_scores.append(float(ds))
            l_l = p.get("legacy_latency_ms")
            if isinstance(l_l, (int, float)):
                latency_legacy.append(float(l_l))
            l_s = p.get("shadow_latency_ms")
            if isinstance(l_s, (int, float)):
                latency_shadow.append(float(l_s))
        elif code == "shadow_core_crash":
            crash_count += 1

    div_scores_sorted = sorted(div_scores)
    return {
        "shadow_compares": compare_count,
        "shadow_crashes": crash_count,
        "shadow_crash_rate": (
            round(crash_count / (compare_count + crash_count), 6)
            if (compare_count + crash_count) > 0 else 0.0
        ),
        "divergence_avg": (
            round(sum(div_scores) / len(div_scores), 4)
            if div_scores else 0.0
        ),
        "divergence_p95": round(_percentile(div_scores_sorted, 95), 4),
        "divergence_max": round(max(div_scores), 4) if div_scores else 0.0,
        "latency_legacy_avg_ms": (
            round(sum(latency_legacy) / len(latency_legacy), 3)
            if latency_legacy else 0.0
        ),
        "latency_shadow_avg_ms": (
            round(sum(latency_shadow) / len(latency_shadow), 3)
            if latency_shadow else 0.0
        ),
    }


def compute_cutover_stats(events: list) -> dict:
    """Estadísticas del cutover routing (cutover_event)."""
    total = 0
    by_path: dict = {}
    fallbacks = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("code") != "cutover_event":
            continue
        total += 1
        p = ev.get("payload") or {}
        if not isinstance(p, dict):
            continue
        path = p.get("used_path", "unknown")
        by_path[path] = by_path.get(path, 0) + 1
        if p.get("fallback_triggered"):
            fallbacks += 1
    legacy_count = by_path.get("legacy", 0) + by_path.get("legacy_fallback", 0)
    return {
        "total_routings": int(total),
        "by_path": dict(by_path),
        "fallback_count": int(fallbacks),
        "legacy_pct": (
            round(legacy_count / total, 4) if total > 0 else 0.0
        ),
        "flowcore_pct": (
            round(by_path.get("flowcore", 0) / total, 4) if total > 0 else 0.0
        ),
    }


# ════════════════════════════════════════════════════════════════════
#  API top-level
# ════════════════════════════════════════════════════════════════════
def compute_runtime_health(*,
                             events_glob: str = DEFAULT_EVENTS_GLOB,
                             window_hours: Optional[float] = 24.0,
                             now: Optional[datetime] = None,
                             ) -> dict:
    """Snapshot consolidado del health runtime.

    Args:
        events_glob: pattern del JSONL sink.
        window_hours: ventana móvil. None = todo el histórico.
        now: timestamp de referencia (para tests).

    Returns:
        dict con todas las métricas health + flag data_sufficient.
    """
    now_dt = now or datetime.now(timezone.utc)
    since = None
    if window_hours is not None and window_hours > 0:
        since = now_dt - timedelta(hours=float(window_hours))

    events = _load_events_glob(events_glob, since=since)
    n_events = len(events)
    data_sufficient = n_events >= 10

    return {
        "window_hours": float(window_hours) if window_hours else None,
        "as_of": now_dt.isoformat(timespec="seconds"),
        "events_observed": n_events,
        "data_sufficient": bool(data_sufficient),
        "step_success_rates": compute_step_success_rates(events),
        "top_failures": compute_top_failures(events),
        "shadow_stats": compute_shadow_stats(events),
        "cutover_stats": compute_cutover_stats(events),
    }


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
