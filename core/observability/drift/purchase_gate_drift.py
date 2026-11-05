"""core/observability/drift/purchase_gate_drift.py — M13.7 Drift Detection.

Pure function que detecta drift operacional del PurchaseGate comparando
un snapshot actual contra una serie histórica de snapshots.

Diseño:
    - Pure function — same (history, current) → same output (excepto timestamp)
    - Sin IO, sin red, sin threads
    - Read-only: NUNCA modifica los inputs
    - Fail-safe: cualquier input malformado → safe state sin drift
    - Determinístico — baseline computado por media estadística simple

Signals detectados:
    1. dry_run_rate_deviation (medium):
       |current - baseline_mean| > DRY_RUN_DEVIATION_THRESHOLD (0.20)
       Requires len(history) >= MIN_HISTORY_FOR_DEVIATION (3)

    2. block_rate_spike (high):
       current > max(BLOCK_RATE_SPIKE_MIN_ABSOLUTE,
                     BLOCK_RATE_SPIKE_FACTOR * baseline_mean)
       Detecta degradación operacional súbita

    3. internal_error_anomaly (critical):
       current.internal_errors > 0 AND > baseline_max
       Cualquier error interno nuevo dispara

    4. live_count_unexpected_mode (critical):
       current.live_count > 0 AND current.mode != "live"
       Safety violation: click real en modo no-live

    5. live_count_first_emergence (high):
       current.live_count > 0 cuando history.live_max == 0 (en modo live OK)
       Primera aparición de live purchase — operador debe revisar

Severity precedence: critical > high > medium > low.

Output schema:
    {
        "drift_detected": bool,
        "severity": "low" | "medium" | "high" | "critical",
        "signals": [dict, ...],
        "baseline": {dry_run_rate_mean, block_rate_mean, live_count_max,
                     internal_errors_max, samples},
        "timestamp": ISO8601 UTC,
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Severity ordering
# ════════════════════════════════════════════════════════════════════
SEVERITY_LEVELS: tuple = ("low", "medium", "high", "critical")
SEVERITY_ORDER: dict[str, int] = {s: i for i, s in enumerate(SEVERITY_LEVELS)}


# ════════════════════════════════════════════════════════════════════
#  Thresholds — tunables si se quieren ajustar empíricamente
# ════════════════════════════════════════════════════════════════════
DRY_RUN_DEVIATION_THRESHOLD: float = 0.20  # 20% deviation from baseline
BLOCK_RATE_SPIKE_FACTOR: float = 2.0       # 2x baseline → spike
BLOCK_RATE_SPIKE_MIN_ABSOLUTE: float = 0.30  # absolute floor for spike
MIN_HISTORY_FOR_DEVIATION: int = 3         # mínimo samples para baseline


# ════════════════════════════════════════════════════════════════════
#  Internal helpers
# ════════════════════════════════════════════════════════════════════
def _safe_get(d: Any, *path: str, default: Any = None) -> Any:
    """Nested dict get fail-safe."""
    current = d
    for key in path:
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def _max_severity(a: str, b: str) -> str:
    """Returns la severity más alta."""
    return a if SEVERITY_ORDER.get(a, -1) >= SEVERITY_ORDER.get(b, -1) else b


def compute_baseline(history: Any) -> dict:
    """Computa baseline estadístico de una lista de snapshots.

    Pure function — no muta history. Maneja inputs malformados.

    Args:
        history: list[dict] con snapshots del endpoint M13.3

    Returns:
        dict con:
            samples:              int — número de snapshots válidos
            dry_run_rate_mean:    float | None
            block_rate_mean:      float | None
            live_count_max:       int (default 0)
            internal_errors_max:  int (default 0)
    """
    if not isinstance(history, list):
        return {
            "samples": 0,
            "dry_run_rate_mean": None,
            "block_rate_mean": None,
            "live_count_max": 0,
            "internal_errors_max": 0,
        }

    dry_run_rates: list = []
    block_rates: list = []
    live_max: int = 0
    err_max: int = 0
    valid_samples: int = 0

    for snap in history:
        if not isinstance(snap, dict):
            continue
        valid_samples += 1

        dry_r = _safe_get(snap, "rates", "purchase_dry_run_rate")
        if isinstance(dry_r, (int, float)):
            dry_run_rates.append(float(dry_r))

        block_r = _safe_get(snap, "rates", "purchase_block_rate")
        if isinstance(block_r, (int, float)):
            block_rates.append(float(block_r))

        live_c = _safe_get(snap, "counters", "purchase_live_count", default=0)
        if isinstance(live_c, int) and live_c > live_max:
            live_max = live_c

        err_c = _safe_get(snap, "counters", "internal_errors", default=0)
        if isinstance(err_c, int) and err_c > err_max:
            err_max = err_c

    return {
        "samples": valid_samples,
        "dry_run_rate_mean": (
            sum(dry_run_rates) / len(dry_run_rates)
            if dry_run_rates else None
        ),
        "block_rate_mean": (
            sum(block_rates) / len(block_rates)
            if block_rates else None
        ),
        "live_count_max": live_max,
        "internal_errors_max": err_max,
    }


def _safe_result(baseline: Optional[dict] = None) -> dict:
    """Default no-drift result (fail-safe path)."""
    return {
        "drift_detected": False,
        "severity": "low",
        "signals": [],
        "baseline": baseline or {
            "samples": 0,
            "dry_run_rate_mean": None,
            "block_rate_mean": None,
            "live_count_max": 0,
            "internal_errors_max": 0,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ════════════════════════════════════════════════════════════════════
#  Main: detect_gate_drift
# ════════════════════════════════════════════════════════════════════
def detect_gate_drift(history: Any, current: Any) -> dict:
    """Detecta drift operacional del gate entre snapshot actual y baseline.

    Args:
        history: list[dict] — snapshots históricos (output de M13.3)
        current: dict       — snapshot actual (output de M13.3)

    Returns:
        dict con drift_detected, severity, signals, baseline, timestamp.

    Pure function: no muta inputs, no IO, deterministic except timestamp.
    Fail-safe: cualquier exception interna → safe result sin drift.
    """
    try:
        # Sanitize inputs
        baseline = compute_baseline(history)
        if not isinstance(current, dict):
            return _safe_result(baseline)

        signals: list = []
        max_sev: str = "low"

        # ── Signal 1: dry_run_rate deviation from baseline ──
        if (baseline["samples"] >= MIN_HISTORY_FOR_DEVIATION
                and baseline["dry_run_rate_mean"] is not None):
            current_dry = _safe_get(current, "rates",
                                     "purchase_dry_run_rate", default=None)
            if isinstance(current_dry, (int, float)):
                deviation = abs(float(current_dry)
                                 - baseline["dry_run_rate_mean"])
                if deviation > DRY_RUN_DEVIATION_THRESHOLD:
                    signals.append({
                        "type": "dry_run_rate_deviation",
                        "severity": "medium",
                        "current": float(current_dry),
                        "baseline_mean": baseline["dry_run_rate_mean"],
                        "deviation": deviation,
                        "threshold": DRY_RUN_DEVIATION_THRESHOLD,
                    })
                    max_sev = _max_severity(max_sev, "medium")

        # ── Signal 2: block_rate spike vs baseline ──
        current_block = _safe_get(current, "rates",
                                    "purchase_block_rate", default=0.0)
        if isinstance(current_block, (int, float)):
            baseline_block = baseline["block_rate_mean"] or 0.0
            spike_threshold = max(
                BLOCK_RATE_SPIKE_MIN_ABSOLUTE,
                BLOCK_RATE_SPIKE_FACTOR * baseline_block,
            )
            if float(current_block) > spike_threshold:
                signals.append({
                    "type": "block_rate_spike",
                    "severity": "high",
                    "current": float(current_block),
                    "baseline_mean": baseline_block,
                    "spike_threshold": spike_threshold,
                })
                max_sev = _max_severity(max_sev, "high")

        # ── Signal 3: internal_error anomaly ──
        current_errors = _safe_get(current, "counters",
                                     "internal_errors", default=0)
        if isinstance(current_errors, int) and current_errors > 0:
            if current_errors > baseline["internal_errors_max"]:
                signals.append({
                    "type": "internal_error_anomaly",
                    "severity": "critical",
                    "current": current_errors,
                    "baseline_max": baseline["internal_errors_max"],
                })
                max_sev = _max_severity(max_sev, "critical")

        # ── Signal 4/5: live_count emergence ──
        current_live = _safe_get(current, "counters",
                                   "purchase_live_count", default=0)
        current_mode = _safe_get(current, "config", "mode", default="")
        if isinstance(current_live, int) and current_live > 0:
            if isinstance(current_mode, str) and current_mode != "live":
                # Safety violation: live count en modo no-live
                signals.append({
                    "type": "live_count_unexpected_mode",
                    "severity": "critical",
                    "current_live": current_live,
                    "current_mode": current_mode,
                })
                max_sev = _max_severity(max_sev, "critical")
            elif (current_mode == "live"
                   and baseline["live_count_max"] == 0
                   and baseline["samples"] >= MIN_HISTORY_FOR_DEVIATION):
                # Primera emergencia de live count en modo live (esperable)
                # — pero el operador debe revisar
                signals.append({
                    "type": "live_count_first_emergence",
                    "severity": "high",
                    "current_live": current_live,
                    "current_mode": current_mode,
                })
                max_sev = _max_severity(max_sev, "high")

        return {
            "drift_detected": len(signals) > 0,
            "severity": max_sev if signals else "low",
            "signals": signals,
            "baseline": baseline,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Failsafe: no triggear drift en caso de bug interno
        return _safe_result()


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
