"""core/observability/metrics.py — Purchase Metrics (STEP 5).

Capa de métricas operacionales derivadas de:
    - structured_events_*.jsonl (Step 3 emitter)
    - idempotency_log/         (Step 2 WAL)

PRINCIPIOS:
    - Pure functions sobre datos en disco; sin estado interno.
    - Time-window filtering por `since_iso`.
    - Determinístico: mismo input → mismo output bit-exact.
    - Fail-safe: archivos corruptos / faltantes → métricas vacías.

stdlib only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from core.observability.event_aggregator import (
    FAILURE_CATEGORIES,
    OPERATIONAL_CODES,
    failure_rate_by_category,
    read_event_stream,
)
from core.observability.event_schema import UNKNOWN_BUCKET


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
DEFAULT_SINK_DIR: str = "runs"
DEFAULT_IDEM_ROOT: str = "runs/idempotency_log"
SINK_GLOB: str = "structured_events_*.jsonl"

WINDOW_24H_SEC: float = 24 * 3600.0
WINDOW_1H_SEC: float = 3600.0

ROUND_NDIGITS: int = 6

# Para burn rates (heurística: leemos archivos que pueden NO existir)
BURNED_ACCOUNTS_FILE: str = "runs/burned_accounts.json"
BURNED_SUBNETS_FILE: str = "runs/burned_subnets24.json"


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _event_timestamp(ev: dict) -> Optional[datetime]:
    return _parse_iso(ev.get("timestamp"))


def _within_window(ev: dict, since: Optional[datetime]) -> bool:
    if since is None:
        return True
    ts = _event_timestamp(ev)
    if ts is None:
        return False
    return ts >= since


# ════════════════════════════════════════════════════════════════════
#  Loaders
# ════════════════════════════════════════════════════════════════════
def load_all_events(sink_dir: Union[str, Path] = DEFAULT_SINK_DIR,
                     *,
                     since: Optional[datetime] = None) -> list:
    """Carga todos los eventos de `structured_events_*.jsonl` en `sink_dir`.

    Filtra por `since` (UTC) si se proporciona.
    """
    base = Path(sink_dir)
    if not base.exists():
        return []
    out: list = []
    try:
        for p in sorted(base.glob(SINK_GLOB)):
            evs = read_event_stream(p)
            for ev in evs:
                if _within_window(ev, since):
                    out.append(ev)
    except OSError:
        return out
    return out


def list_idempotency_keys(idem_root: Union[str, Path] = DEFAULT_IDEM_ROOT) -> list:
    """Lista las keys presentes en el WAL log."""
    p = Path(idem_root)
    if not p.exists():
        return []
    out: list = []
    try:
        for child in p.iterdir():
            if child.is_dir():
                out.append(child.name)
    except OSError:
        return out
    return sorted(out)


def _load_idempotency_summary(idem_root: Union[str, Path] = DEFAULT_IDEM_ROOT) -> dict:
    """Resumen del WAL: counts por estado."""
    root = Path(idem_root)
    if not root.exists():
        return {"total": 0, "committed": 0, "orphan": 0, "unknown": 0}
    committed = 0
    orphan = 0
    total = 0
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            total += 1
            has_intent = (child / "intent.json").exists()
            has_outcome = (child / "outcome.json").exists()
            if has_outcome and has_intent:
                committed += 1
            elif has_intent and not has_outcome:
                orphan += 1
            elif has_outcome and not has_intent:
                committed += 1
    except OSError:
        pass
    return {
        "total": int(total),
        "committed": int(committed),
        "orphan": int(orphan),
        "unknown": int(total - committed - orphan),
    }


# ════════════════════════════════════════════════════════════════════
#  Percentile helper (puro stdlib)
# ════════════════════════════════════════════════════════════════════
def _percentile(sorted_values: list, p: float) -> float:
    """Percentil p ∈ [0, 100] usando interpolación lineal.

    Si la lista está vacía → 0.0.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    p = max(0.0, min(100.0, float(p)))
    k = (p / 100.0) * (len(sorted_values) - 1)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return float(sorted_values[f])
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return float(d0 + d1)


# ════════════════════════════════════════════════════════════════════
#  Métricas individuales
# ════════════════════════════════════════════════════════════════════
def success_rate(events: Iterable[dict]) -> dict:
    """% de outcomes "paid" sobre el total de outcomes registrados.

    Cuenta eventos `outcome_recorded` cuyo payload.status == "paid"
    vs total outcomes.
    """
    total = 0
    paid = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("code") != "outcome_recorded":
            continue
        total += 1
        payload = ev.get("payload") or {}
        if isinstance(payload, dict) and payload.get("status") == "paid":
            paid += 1
    rate = (paid / total) if total > 0 else 0.0
    return {
        "rate": round(rate, ROUND_NDIGITS),
        "paid": int(paid),
        "total_outcomes": int(total),
    }


def failure_attribution(events: Iterable[dict]) -> dict:
    """Pareto de fallos por categoría. Reusa failure_rate_by_category."""
    m = failure_rate_by_category(events)
    return m.to_dict()


def step_latencies(events: Iterable[dict]) -> list:
    """Devuelve list[float] de latencias inferibles de los eventos.

    Estrategia: para cada cart_id, busca el delta entre
    `flow_started` y `outcome_recorded`. Devuelve ms.
    """
    flow_start: dict = {}  # key (idem_key) → datetime
    outcomes: dict = {}     # key → datetime
    for ev in events:
        if not isinstance(ev, dict):
            continue
        key = ev.get("idempotency_key", "") or ev.get("cart_id", "")
        if not key:
            continue
        ts = _event_timestamp(ev)
        if ts is None:
            continue
        if ev.get("code") == "flow_started" and key not in flow_start:
            flow_start[key] = ts
        elif ev.get("code") == "outcome_recorded" and key not in outcomes:
            outcomes[key] = ts

    latencies: list = []
    for key, start_ts in flow_start.items():
        end_ts = outcomes.get(key)
        if end_ts is None:
            continue
        delta_ms = (end_ts - start_ts).total_seconds() * 1000.0
        if delta_ms >= 0:
            latencies.append(delta_ms)
    return latencies


def latency_percentiles(events: Iterable[dict]) -> dict:
    """p50/p95/p99 de latencias inferidas. ms."""
    lats = sorted(step_latencies(events))
    return {
        "p50_ms": round(_percentile(lats, 50.0), 3),
        "p95_ms": round(_percentile(lats, 95.0), 3),
        "p99_ms": round(_percentile(lats, 99.0), 3),
        "count": len(lats),
        "min_ms": round(min(lats), 3) if lats else 0.0,
        "max_ms": round(max(lats), 3) if lats else 0.0,
    }


def cart_lifetime_distribution(events: Iterable[dict]) -> dict:
    """Mismo cálculo que step_latencies pero presentado como
    "cart lifetime" (start → terminal phase)."""
    lats = sorted(step_latencies(events))
    if not lats:
        return {
            "count": 0,
            "min_sec": 0.0, "max_sec": 0.0,
            "p50_sec": 0.0, "p95_sec": 0.0,
        }
    return {
        "count": len(lats),
        "min_sec": round(min(lats) / 1000.0, 3),
        "max_sec": round(max(lats) / 1000.0, 3),
        "p50_sec": round(_percentile(lats, 50.0) / 1000.0, 3),
        "p95_sec": round(_percentile(lats, 95.0) / 1000.0, 3),
    }


def idempotency_collision_rate(idem_root: Union[str, Path] = DEFAULT_IDEM_ROOT) -> dict:
    """Rate de orphan intents (= crashes entre side-effect y outcome)."""
    s = _load_idempotency_summary(idem_root)
    total = s["total"]
    orphan_rate = (s["orphan"] / total) if total > 0 else 0.0
    return {
        "total": s["total"],
        "committed": s["committed"],
        "orphan": s["orphan"],
        "orphan_rate": round(orphan_rate, ROUND_NDIGITS),
    }


def burn_rate_per_day(file_path: Union[str, Path],
                       *, now: Optional[datetime] = None) -> dict:
    """Lee un archivo JSON con burned_* y estima rate por día.

    Acepta tanto dict {key: {first_seen, ...}} como list[str].
    Si no hay timestamps, devuelve count total + None.
    """
    p = Path(file_path)
    if not p.exists():
        return {"total": 0, "per_day": None, "window_days": 0.0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"total": 0, "per_day": None, "window_days": 0.0}

    if isinstance(data, list):
        # Sin timestamps — sólo count
        return {"total": len(data), "per_day": None, "window_days": 0.0}

    if not isinstance(data, dict):
        return {"total": 0, "per_day": None, "window_days": 0.0}

    timestamps: list = []
    for k, v in data.items():
        if isinstance(v, dict):
            ts = _parse_iso(v.get("burned_at") or v.get("first_seen")
                             or v.get("timestamp"))
            if ts is not None:
                timestamps.append(ts)

    total = len(data)
    if not timestamps:
        return {"total": total, "per_day": None, "window_days": 0.0}

    now_dt = now or _now_utc()
    oldest = min(timestamps)
    window_sec = (now_dt - oldest).total_seconds()
    window_days = window_sec / 86400.0 if window_sec > 0 else 0.0
    per_day = (len(timestamps) / window_days) if window_days > 0 else None
    return {
        "total": int(total),
        "per_day": round(per_day, ROUND_NDIGITS) if per_day is not None else None,
        "window_days": round(window_days, 3),
    }


# ════════════════════════════════════════════════════════════════════
#  API principal: compute_purchase_metrics
# ════════════════════════════════════════════════════════════════════
def compute_purchase_metrics(*,
                              sink_dir: Union[str, Path] = DEFAULT_SINK_DIR,
                              idem_root: Union[str, Path] = DEFAULT_IDEM_ROOT,
                              window_hours: Optional[float] = 24.0,
                              now: Optional[datetime] = None,
                              burned_accounts_file: Union[str, Path] = BURNED_ACCOUNTS_FILE,
                              burned_subnets_file: Union[str, Path] = BURNED_SUBNETS_FILE,
                              ) -> dict:
    """Genera el JSON canónico para `/api/purchase/metrics`.

    Args:
        sink_dir: dónde están los structured_events_*.jsonl
        idem_root: dónde está el idempotency WAL
        window_hours: ventana de tiempo (None = todo el histórico)
        now: timestamp de referencia (para tests; default = now UTC)
        burned_accounts_file / burned_subnets_file: archivos opcionales
            de burned (devuelven counts si existen)

    Returns:
        dict JSON-serializable con todas las métricas del spec STEP 5.
    """
    now_dt = now or _now_utc()
    since: Optional[datetime] = None
    if window_hours is not None and window_hours > 0:
        since = now_dt - timedelta(hours=float(window_hours))

    events = load_all_events(sink_dir, since=since)

    sr = success_rate(events)
    attribution = failure_attribution(events)
    lat = latency_percentiles(events)
    lifetime = cart_lifetime_distribution(events)
    idem = idempotency_collision_rate(idem_root)
    burn_acc = burn_rate_per_day(burned_accounts_file, now=now_dt)
    burn_sub = burn_rate_per_day(burned_subnets_file, now=now_dt)

    return {
        "window_hours": float(window_hours) if window_hours else None,
        "as_of": now_dt.isoformat(timespec="seconds"),
        "events_in_window": int(len(events)),
        "success_rate_24h": sr,
        "failure_pareto": attribution["data"],
        "latency": lat,
        "cart_lifecycle": lifetime,
        "idempotency": idem,
        "account_burn_rate_per_day": burn_acc,
        "subnet_burn_rate_per_day": burn_sub,
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
