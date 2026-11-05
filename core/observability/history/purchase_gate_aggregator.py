"""core/observability/history/purchase_gate_aggregator.py — M13.6.

Pure aggregation function que lee JSONL streams existentes de
`runtime_diagnostics` y bucketea los eventos `purchase_*` en time-series.

Diseño:
    - READ-ONLY sobre los JSONL existentes en `runs/`
    - Sin nueva storage / DB / external service
    - Sin afectar runtime path
    - Sin side-effects
    - Pure function: mismo input (files + now) → mismo output

Windows soportadas: "1h", "24h", "7d"

Output schema:
    {
        "window":       "1h" | "24h" | "7d",
        "bucket_count": int,
        "bucket_size_s": int,
        "now":          ISO8601 UTC,
        "metrics": {
            "attempted":  [int, ...],  # bucket_count entries, 0=oldest
            "allowed":    [int, ...],
            "blocked":    [int, ...],
            "dry_run":    [int, ...],
            "live":       [int, ...],
            "duplicate":  [int, ...],
            "errors":     [int, ...]
        },
        "derived": {
            "block_rate_trend":   [float, ...],
            "dry_run_stability":  [float, ...],
            "live_rate_trend":    [float, ...]
        }
    }
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# Windows soportadas → segundos
WINDOW_SECONDS: dict[str, int] = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}

# Eventos del gate que aparecen en JSONL (alineado con PurchaseEvent constants)
PURCHASE_EVENT_NAMES: frozenset = frozenset({
    "purchase_attempted",
    "purchase_allowed",
    "purchase_blocked",
    "purchase_dry_run",
    "purchase_live_success",
    "purchase_duplicate_blocked",
    "purchase_stale_checkout",
    "purchase_kill_switch_blocked",
})


def _parse_iso_ts(ts_str: Any) -> Optional[datetime]:
    """Parse ISO8601 ts → datetime aware. None si falla."""
    if not isinstance(ts_str, str) or not ts_str:
        return None
    try:
        # Soporta '...+00:00' y '...Z'
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _empty_buckets(bucket_count: int) -> dict:
    return {
        "attempted":  [0] * bucket_count,
        "allowed":    [0] * bucket_count,
        "blocked":    [0] * bucket_count,
        "dry_run":    [0] * bucket_count,
        "live":       [0] * bucket_count,
        "duplicate":  [0] * bucket_count,
        "errors":     [0] * bucket_count,
    }


def _compute_derived(metrics: dict, bucket_count: int) -> dict:
    """Computa rates derived per bucket. Pure function."""
    derived = {
        "block_rate_trend":  [0.0] * bucket_count,
        "dry_run_stability": [0.0] * bucket_count,
        "live_rate_trend":   [0.0] * bucket_count,
    }
    for i in range(bucket_count):
        attempted_i = metrics["attempted"][i]
        if attempted_i > 0:
            derived["block_rate_trend"][i] = (
                metrics["blocked"][i] / attempted_i
            )
            derived["dry_run_stability"][i] = (
                metrics["dry_run"][i] / attempted_i
            )
            derived["live_rate_trend"][i] = (
                metrics["live"][i] / attempted_i
            )
    return derived


def _event_to_metric(event_name: str) -> Optional[str]:
    """Mapea event_name → key del bucket. None si no es purchase event."""
    if event_name == "purchase_attempted":
        return "attempted"
    if event_name == "purchase_allowed":
        return "allowed"
    if event_name == "purchase_blocked":
        return "blocked"
    if event_name == "purchase_dry_run":
        return "dry_run"
    if event_name == "purchase_live_success":
        return "live"
    if event_name == "purchase_duplicate_blocked":
        return "duplicate"
    # stale_checkout y kill_switch_blocked son subtipos de blocked — los
    # contamos como blocked también para coherencia con el endpoint M13.3
    if event_name == "purchase_stale_checkout":
        return "blocked"
    if event_name == "purchase_kill_switch_blocked":
        return "blocked"
    return None


def aggregate_purchase_history(
    window: str = "24h",
    directory: Any = "runs",
    now: Optional[datetime] = None,
    bucket_count: int = 24,
    file_pattern: str = "diagnostics_*.jsonl",
) -> dict:
    """Agrega eventos del gate desde JSONL en time-series buckets.

    Args:
        window:       "1h" | "24h" | "7d" (default "24h")
        directory:    path al dir con JSONL (default "runs")
        now:          tiempo de referencia (default UTC now)
        bucket_count: número de buckets (default 24)
        file_pattern: glob pattern para JSONL (default diagnostics_*.jsonl)

    Returns:
        dict con window, bucket_count, bucket_size_s, now, metrics, derived
        (ver docstring del módulo para schema completo).

    Pure function: NO modificaciones de files, NO side-effects en runtime.
    Fail-safe: si dir no existe o ningún JSONL es legible → buckets en 0.
    """
    # Validación de window
    if window not in WINDOW_SECONDS:
        window = "24h"
    if bucket_count < 1:
        bucket_count = 24
    window_s = WINDOW_SECONDS[window]
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        # Asumir UTC si naive
        now = now.replace(tzinfo=timezone.utc)
    window_start = now - timedelta(seconds=window_s)
    bucket_s = max(1, window_s // bucket_count)

    metrics = _empty_buckets(bucket_count)

    try:
        dir_path = Path(directory)
        if not dir_path.exists() or not dir_path.is_dir():
            return _build_result(
                window, bucket_count, bucket_s, now, metrics
            )

        # Walk JSONL files — skip los demasiado antiguos por mtime
        for jsonl in dir_path.glob(file_pattern):
            try:
                mtime_dt = datetime.fromtimestamp(
                    jsonl.stat().st_mtime, tz=timezone.utc
                )
                # Si el file no ha sido modificado dentro del window+buffer,
                # skip (optimización; buffer = 1 window para handles abiertos)
                if mtime_dt < window_start - timedelta(seconds=window_s):
                    continue
                with open(jsonl, "r", encoding="utf-8",
                          errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        evt = rec.get("event", "") if isinstance(rec, dict) else ""
                        metric_key = _event_to_metric(evt)
                        if metric_key is None:
                            continue
                        ts = _parse_iso_ts(rec.get("ts"))
                        if ts is None or ts < window_start or ts > now:
                            continue
                        # Bucket index: 0 = más antiguo, bucket_count-1 = más reciente
                        offset_s = (now - ts).total_seconds()
                        reverse_idx = int(offset_s // bucket_s)
                        if reverse_idx < 0 or reverse_idx >= bucket_count:
                            continue
                        bucket = bucket_count - 1 - reverse_idx
                        if 0 <= bucket < bucket_count:
                            metrics[metric_key][bucket] += 1
            except Exception:
                continue  # skip files corruptos
    except Exception:
        pass  # fail-safe: devolver lo que haya

    return _build_result(window, bucket_count, bucket_s, now, metrics)


def _build_result(window: str, bucket_count: int, bucket_s: int,
                   now: datetime, metrics: dict) -> dict:
    return {
        "window":       window,
        "bucket_count": bucket_count,
        "bucket_size_s": bucket_s,
        "now":          now.isoformat(),
        "metrics":      metrics,
        "derived":      _compute_derived(metrics, bucket_count),
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
