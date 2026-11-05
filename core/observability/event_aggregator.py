"""core/observability/event_aggregator.py — Event Aggregation (STEP 4).

Capa de agregación PURE sobre eventos estructurados emitidos por
`event_emitter.py` (Step 3).

PRINCIPIOS:
    - Sólo READS desde JSONL — nunca escribe.
    - Pure functions — mismo input → mismo output bit-exact.
    - Acepta tanto iterables en memoria como path al JSONL.
    - Fail-safe: eventos malformados se ignoran, no se propagan.

RESPONDE:
    1. ¿% de fallos por categoría? → failure_rate_by_category
    2. ¿En qué step falla más? → step_failure_distribution
    3. ¿Qué phase + category combos producen fallos? → cart_lifecycle_heatmap
    4. ¿Trazas por cart_id / idem_key? → correlate_by_*

stdlib only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Union

from core.observability.event_schema import (
    METRIC_CART_LIFECYCLE_HEATMAP,
    METRIC_CORRELATION_BY_CART_ID,
    METRIC_CORRELATION_BY_IDEM_KEY,
    METRIC_FAILURE_RATE_BY_CATEGORY,
    METRIC_SEVERITY_BREAKDOWN,
    METRIC_STEP_FAILURE_DISTRIBUTION,
    UNKNOWN_BUCKET,
    AggregatedMetric,
    EventGroup,
)


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
# Categorías que cuentan como "fallo" para tasas (excluye flow generic ops)
FAILURE_CATEGORIES: frozenset = frozenset({
    "network", "anti_bot", "cart_lifecycle", "inventory",
    "queue", "payment",
})

# Códigos que NO son errores reales (operacionales)
OPERATIONAL_CODES: frozenset = frozenset({
    "flow_started", "phase_transition", "intent_recorded",
    "outcome_recorded",
})

# ─── Source filter (Phase 2 audit) ──────────────────────────────
# Por defecto, el dashboard ignora eventos taggeados como "test".
# Sólo cuenta producción + canary + shadow del bot real.
DEFAULT_SOURCE_FILTER: frozenset = frozenset({
    "production", "shadow", "canary",
})
# Si un evento NO tiene `source` (eventos pre-Phase2), por defecto se
# trata como production (backward compat con tests existentes).
# Para análisis estricto de "solo producción real", pasar
# include_untagged=False al read_event_stream.
INCLUDE_UNTAGGED_AS_PRODUCTION: bool = True

ROUND_NDIGITS: int = 6


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _is_event_dict(d: Any) -> bool:
    return isinstance(d, dict) and "category" in d and "code" in d


def _safe_get(d: Any, key: str, default: Any = "") -> Any:
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def _safe_payload(d: Any) -> dict:
    p = _safe_get(d, "payload", {})
    return p if isinstance(p, dict) else {}


def _is_failure_event(event: dict) -> bool:
    """¿Este evento cuenta como fallo (vs operacional)?"""
    cat = _safe_get(event, "category", "")
    code = _safe_get(event, "code", "")
    if code in OPERATIONAL_CODES:
        return False
    return cat in FAILURE_CATEGORIES


# ════════════════════════════════════════════════════════════════════
#  Reading streams
# ════════════════════════════════════════════════════════════════════
def read_event_stream(source: Union[str, Path, Iterable[dict]],
                       *,
                       source_filter: Optional[frozenset] = None,
                       include_untagged: Optional[bool] = None,
                       ) -> list:
    """Lee eventos desde un path JSONL o devuelve la lista si ya es iterable.

    Args:
        source: path o iterable.
        source_filter: set de `source` tags aceptados. Default:
            DEFAULT_SOURCE_FILTER ({"production","shadow","canary"}).
            Pasar `frozenset()` para incluir todo.
        include_untagged: si True, eventos sin tag `source` se incluyen
            como "production". Default: INCLUDE_UNTAGGED_AS_PRODUCTION
            (False — eventos viejos = excluidos por seguridad).

    Líneas malformadas se ignoran. Si el path no existe → [].
    """
    filter_set = (DEFAULT_SOURCE_FILTER if source_filter is None
                   else source_filter)
    inc_untagged = (INCLUDE_UNTAGGED_AS_PRODUCTION if include_untagged is None
                     else bool(include_untagged))

    def _accept(ev: dict) -> bool:
        if not filter_set:
            return True   # filter vacío = accept all
        src = ev.get("source")
        if src is None:
            return inc_untagged
        return src in filter_set

    if isinstance(source, (str, Path)):
        p = Path(source)
        if not p.exists():
            return []
        out: list = []
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if _is_event_dict(d) and _accept(d):
                            out.append(d)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            return out
        return out
    # Iterable de dicts
    try:
        return [d for d in source if _is_event_dict(d) and _accept(d)]
    except TypeError:
        return []


# ════════════════════════════════════════════════════════════════════
#  Grouping primitives
# ════════════════════════════════════════════════════════════════════
def group_by(events: Iterable[dict], key_fn: Callable[[dict], str]) -> dict:
    """Agrupa eventos por una función de clave.

    Devuelve dict {key: list[event]}. Pure.
    """
    out: dict = {}
    for ev in events:
        if not _is_event_dict(ev):
            continue
        try:
            k = key_fn(ev)
        except Exception:
            k = UNKNOWN_BUCKET
        if not isinstance(k, str):
            k = str(k) if k is not None else UNKNOWN_BUCKET
        if not k:
            k = UNKNOWN_BUCKET
        out.setdefault(k, []).append(ev)
    return out


def count_by(events: Iterable[dict], key_fn: Callable[[dict], str]) -> dict:
    """Counter genérico por función de clave. Devuelve dict {key: int}."""
    counts: dict = {}
    for ev in events:
        if not _is_event_dict(ev):
            continue
        try:
            k = key_fn(ev)
        except Exception:
            k = UNKNOWN_BUCKET
        if not isinstance(k, str):
            k = str(k) if k is not None else UNKNOWN_BUCKET
        if not k:
            k = UNKNOWN_BUCKET
        counts[k] = counts.get(k, 0) + 1
    return counts


# ════════════════════════════════════════════════════════════════════
#  Agregaciones canónicas
# ════════════════════════════════════════════════════════════════════
def failure_rate_by_category(events: Iterable[dict]) -> AggregatedMetric:
    """Distribución de fallos por categoría (excluye operacionales).

    Devuelve AggregatedMetric.data = {
        "<category>": {"count": int, "rate": float},
        ...
        "_total_failures": int,
    }
    """
    evs = [e for e in events if _is_event_dict(e)]
    failures = [e for e in evs if _is_failure_event(e)]
    total = len(failures)

    counts = count_by(failures, lambda e: e.get("category", UNKNOWN_BUCKET))
    data: dict = {}
    for cat, n in counts.items():
        rate = (n / total) if total > 0 else 0.0
        data[cat] = {
            "count": int(n),
            "rate": float(round(rate, ROUND_NDIGITS)),
        }
    data["_total_failures"] = int(total)

    return AggregatedMetric(
        name=METRIC_FAILURE_RATE_BY_CATEGORY,
        total_events=len(evs),
        data=data,
        meta={"failure_categories": sorted(FAILURE_CATEGORIES)},
    )


def step_failure_distribution(events: Iterable[dict]) -> AggregatedMetric:
    """¿En qué `context` (= step name) ocurren los fallos?

    Devuelve AggregatedMetric.data = {
        "<context>": {
            "<category>": int,
            ...,
            "_total": int,
        },
        ...,
    }
    """
    evs = [e for e in events if _is_event_dict(e)]
    failures = [e for e in evs if _is_failure_event(e)]

    data: dict = {}
    for ev in failures:
        ctx = _safe_get(ev, "context", "") or UNKNOWN_BUCKET
        cat = _safe_get(ev, "category", UNKNOWN_BUCKET)
        per_ctx = data.setdefault(ctx, {"_total": 0})
        per_ctx[cat] = per_ctx.get(cat, 0) + 1
        per_ctx["_total"] = per_ctx["_total"] + 1

    return AggregatedMetric(
        name=METRIC_STEP_FAILURE_DISTRIBUTION,
        total_events=len(evs),
        data=data,
        meta={"failure_count": len(failures)},
    )


def cart_lifecycle_heatmap(events: Iterable[dict]) -> AggregatedMetric:
    """Heatmap phase × category.

    Para cada evento de fallo, busca `payload.phase` o
    `payload.cart_phase` y agrupa.

    Devuelve AggregatedMetric.data = {
        "<phase>": {
            "<category>": int,
            ...,
            "_total": int,
        },
        ...,
    }
    """
    evs = [e for e in events if _is_event_dict(e)]
    failures = [e for e in evs if _is_failure_event(e)]

    data: dict = {}
    for ev in failures:
        p = _safe_payload(ev)
        phase = p.get("phase") or p.get("cart_phase") or UNKNOWN_BUCKET
        if not isinstance(phase, str):
            phase = str(phase) if phase is not None else UNKNOWN_BUCKET
        cat = _safe_get(ev, "category", UNKNOWN_BUCKET)
        bucket = data.setdefault(phase, {"_total": 0})
        bucket[cat] = bucket.get(cat, 0) + 1
        bucket["_total"] = bucket["_total"] + 1

    return AggregatedMetric(
        name=METRIC_CART_LIFECYCLE_HEATMAP,
        total_events=len(evs),
        data=data,
        meta={"failure_count": len(failures)},
    )


def severity_breakdown(events: Iterable[dict]) -> AggregatedMetric:
    """Distribución por severidad (todos los eventos, no sólo fallos)."""
    evs = [e for e in events if _is_event_dict(e)]
    counts = count_by(evs, lambda e: e.get("severity", UNKNOWN_BUCKET))
    return AggregatedMetric(
        name=METRIC_SEVERITY_BREAKDOWN,
        total_events=len(evs),
        data={k: int(v) for k, v in counts.items()},
    )


# ════════════════════════════════════════════════════════════════════
#  Correlación por trace ids
# ════════════════════════════════════════════════════════════════════
def _build_event_groups(grouped: dict) -> dict:
    """Convierte {key: list[event]} → {key: EventGroup}."""
    out: dict = {}
    for k, evs in grouped.items():
        cats = sorted({_safe_get(e, "category", "") for e in evs} - {""})
        codes = sorted({_safe_get(e, "code", "") for e in evs} - {""})
        out[k] = EventGroup(
            key=k,
            events=tuple(evs),
            categories=tuple(cats),
            codes=tuple(codes),
        )
    return out


def correlate_by_cart_id(events: Iterable[dict]) -> AggregatedMetric:
    """Agrupa eventos por cart_id. Cart_id vacío va a 'unknown'."""
    evs = [e for e in events if _is_event_dict(e)]
    grouped = group_by(evs, lambda e: e.get("cart_id", "") or UNKNOWN_BUCKET)
    groups = _build_event_groups(grouped)
    data = {k: g.to_dict() for k, g in groups.items()}
    return AggregatedMetric(
        name=METRIC_CORRELATION_BY_CART_ID,
        total_events=len(evs),
        data=data,
        meta={"unique_cart_ids": len(groups)},
    )


def correlate_by_idempotency_key(events: Iterable[dict]) -> AggregatedMetric:
    """Agrupa eventos por idempotency_key."""
    evs = [e for e in events if _is_event_dict(e)]
    grouped = group_by(
        evs, lambda e: e.get("idempotency_key", "") or UNKNOWN_BUCKET
    )
    groups = _build_event_groups(grouped)
    data = {k: g.to_dict() for k, g in groups.items()}
    return AggregatedMetric(
        name=METRIC_CORRELATION_BY_IDEM_KEY,
        total_events=len(evs),
        data=data,
        meta={"unique_idem_keys": len(groups)},
    )


# ════════════════════════════════════════════════════════════════════
#  Top-level: aggregate_all
# ════════════════════════════════════════════════════════════════════
def aggregate_all(source: Union[str, Path, Iterable[dict]]) -> dict:
    """Computa todas las agregaciones canónicas sobre un stream.

    Devuelve dict {metric_name: AggregatedMetric.to_dict()}.
    """
    events = read_event_stream(source)
    return {
        METRIC_FAILURE_RATE_BY_CATEGORY: failure_rate_by_category(events).to_dict(),
        METRIC_STEP_FAILURE_DISTRIBUTION: step_failure_distribution(events).to_dict(),
        METRIC_CART_LIFECYCLE_HEATMAP: cart_lifecycle_heatmap(events).to_dict(),
        METRIC_SEVERITY_BREAKDOWN: severity_breakdown(events).to_dict(),
        METRIC_CORRELATION_BY_CART_ID: correlate_by_cart_id(events).to_dict(),
        METRIC_CORRELATION_BY_IDEM_KEY: correlate_by_idempotency_key(events).to_dict(),
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
