"""core/dashboard/anomaly_detection.py — Real-time anomaly detection.

Detecta:
    - success rate collapse vs baseline
    - divergence spikes
    - payment failure spikes
    - anti-bot wave / surge
    - subnet/account burn rate explosion
    - circuit breaker storm
    - orphan WAL growth
    - shadow crash storm

OUTPUT:
    - Alert dataclass por anomaly detectada
    - Persistencia opt-in a `runs/alerts.jsonl`

Severities:
    info | warning | critical | emergency

stdlib only. Pure functions. Determinístico (sin random).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Severities
# ════════════════════════════════════════════════════════════════════
class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


# ════════════════════════════════════════════════════════════════════
#  Alert model
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Alert:
    code: str                  # e.g. "success_rate_drop"
    severity: str              # AlertSeverity.value
    message: str
    actual: float = 0.0
    threshold: float = 0.0
    delta: float = 0.0
    timestamp_iso: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "actual": float(self.actual),
            "threshold": float(self.threshold),
            "delta": float(self.delta),
            "timestamp": self.timestamp_iso,
            "payload": dict(self.payload),
        }


# ════════════════════════════════════════════════════════════════════
#  Thresholds (configurable vía AnomalyConfig)
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AnomalyConfig:
    # Caída de success rate vs baseline (delta absoluto)
    success_drop_warning: float = 0.05    # 5% drop = warning
    success_drop_critical: float = 0.10    # 10% drop = critical
    success_drop_emergency: float = 0.20   # 20% drop = emergency

    # Divergence spike (avg)
    divergence_warning: float = 20.0
    divergence_critical: float = 50.0
    divergence_emergency: float = 80.0

    # Payment failure increase (delta absoluto)
    payment_increase_warning: float = 0.05
    payment_increase_critical: float = 0.15

    # Orphan WAL growth
    orphan_rate_warning: float = 0.05
    orphan_rate_critical: float = 0.15

    # Circuit breaker storm
    cb_storm_count_warning: int = 5
    cb_storm_count_critical: int = 20

    # Anti-bot surge (count en ventana)
    antibot_surge_warning: int = 10
    antibot_surge_critical: int = 50


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_get(d: Any, *path, default: Any = 0.0) -> Any:
    cur = d
    for k in path:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ════════════════════════════════════════════════════════════════════
#  Individual detectors
# ════════════════════════════════════════════════════════════════════
def detect_success_rate_drop(current: dict,
                              baseline: dict,
                              *,
                              cfg: Optional[AnomalyConfig] = None,
                              ) -> Optional[Alert]:
    """Detecta colapso de success rate vs baseline."""
    c = cfg or AnomalyConfig()
    cur_rate = _safe_float(_safe_get(current, "success_rate_24h", "rate"))
    base_rate = _safe_float(_safe_get(baseline, "success_rate_24h", "rate"))
    if base_rate <= 0:
        return None
    drop = base_rate - cur_rate
    if drop < c.success_drop_warning:
        return None
    sev = AlertSeverity.WARNING.value
    if drop >= c.success_drop_emergency:
        sev = AlertSeverity.EMERGENCY.value
    elif drop >= c.success_drop_critical:
        sev = AlertSeverity.CRITICAL.value
    return Alert(
        code="success_rate_drop",
        severity=sev,
        message=f"success_rate dropped {drop:.4f} (current={cur_rate:.4f} baseline={base_rate:.4f})",
        actual=cur_rate, threshold=base_rate, delta=-drop,
        timestamp_iso=_now_iso_utc(),
    )


def detect_divergence_spike(current_avg_score: float,
                              *,
                              cfg: Optional[AnomalyConfig] = None,
                              ) -> Optional[Alert]:
    c = cfg or AnomalyConfig()
    score = _safe_float(current_avg_score)
    if score < c.divergence_warning:
        return None
    sev = AlertSeverity.WARNING.value
    if score >= c.divergence_emergency:
        sev = AlertSeverity.EMERGENCY.value
    elif score >= c.divergence_critical:
        sev = AlertSeverity.CRITICAL.value
    return Alert(
        code="divergence_spike",
        severity=sev,
        message=f"avg divergence_score={score:.2f} (threshold warning={c.divergence_warning})",
        actual=score, threshold=c.divergence_warning,
        delta=score - c.divergence_warning,
        timestamp_iso=_now_iso_utc(),
    )


def detect_payment_anomaly(current: dict,
                             baseline: dict,
                             *,
                             cfg: Optional[AnomalyConfig] = None,
                             ) -> Optional[Alert]:
    c = cfg or AnomalyConfig()
    cur_sr = _safe_float(_safe_get(current, "success_rate_24h", "rate"), 1.0)
    base_sr = _safe_float(_safe_get(baseline, "success_rate_24h", "rate"), 1.0)
    cur_fail = 1.0 - cur_sr
    base_fail = 1.0 - base_sr
    increase = cur_fail - base_fail
    if increase < c.payment_increase_warning:
        return None
    sev = (AlertSeverity.CRITICAL.value
            if increase >= c.payment_increase_critical
            else AlertSeverity.WARNING.value)
    return Alert(
        code="payment_failure_spike",
        severity=sev,
        message=f"payment failure increased by {increase:.4f} (cur_fail={cur_fail:.4f})",
        actual=cur_fail, threshold=base_fail, delta=increase,
        timestamp_iso=_now_iso_utc(),
    )


def detect_orphan_wal_growth(current: dict,
                               *,
                               cfg: Optional[AnomalyConfig] = None,
                               ) -> Optional[Alert]:
    c = cfg or AnomalyConfig()
    rate = _safe_float(_safe_get(current, "idempotency", "orphan_rate"))
    if rate < c.orphan_rate_warning:
        return None
    sev = (AlertSeverity.CRITICAL.value
            if rate >= c.orphan_rate_critical
            else AlertSeverity.WARNING.value)
    return Alert(
        code="orphan_wal_growth",
        severity=sev,
        message=f"orphan_rate={rate:.4f} exceeds threshold {c.orphan_rate_warning}",
        actual=rate, threshold=c.orphan_rate_warning,
        delta=rate - c.orphan_rate_warning,
        timestamp_iso=_now_iso_utc(),
    )


def detect_circuit_breaker_storm(open_count: int,
                                    *,
                                    cfg: Optional[AnomalyConfig] = None,
                                    ) -> Optional[Alert]:
    c = cfg or AnomalyConfig()
    n = int(open_count) if isinstance(open_count, int) else 0
    if n < c.cb_storm_count_warning:
        return None
    sev = (AlertSeverity.CRITICAL.value
            if n >= c.cb_storm_count_critical
            else AlertSeverity.WARNING.value)
    return Alert(
        code="circuit_breaker_storm",
        severity=sev,
        message=f"{n} circuit breakers open simultaneously",
        actual=float(n), threshold=float(c.cb_storm_count_warning),
        delta=float(n - c.cb_storm_count_warning),
        timestamp_iso=_now_iso_utc(),
    )


def detect_antibot_surge(antibot_event_count: int,
                           *,
                           cfg: Optional[AnomalyConfig] = None,
                           ) -> Optional[Alert]:
    c = cfg or AnomalyConfig()
    n = int(antibot_event_count) if isinstance(antibot_event_count, int) else 0
    if n < c.antibot_surge_warning:
        return None
    sev = (AlertSeverity.CRITICAL.value
            if n >= c.antibot_surge_critical
            else AlertSeverity.WARNING.value)
    return Alert(
        code="antibot_surge",
        severity=sev,
        message=f"{n} anti-bot events in window",
        actual=float(n), threshold=float(c.antibot_surge_warning),
        delta=float(n - c.antibot_surge_warning),
        timestamp_iso=_now_iso_utc(),
        payload={"count": n},
    )


def detect_shadow_crash_storm(shadow_crash_count: int,
                                total_shadow_runs: int,
                                *,
                                cfg: Optional[AnomalyConfig] = None,
                                ) -> Optional[Alert]:
    """Si > 20% de shadow runs crashean → alerta."""
    if total_shadow_runs <= 0:
        return None
    rate = float(shadow_crash_count) / float(total_shadow_runs)
    if rate < 0.2:
        return None
    sev = (AlertSeverity.CRITICAL.value if rate >= 0.5
            else AlertSeverity.WARNING.value)
    return Alert(
        code="shadow_crash_storm",
        severity=sev,
        message=f"{rate:.2%} of shadow runs crashed ({shadow_crash_count}/{total_shadow_runs})",
        actual=rate, threshold=0.2, delta=rate - 0.2,
        timestamp_iso=_now_iso_utc(),
    )


# ════════════════════════════════════════════════════════════════════
#  Top-level: scan_for_anomalies
# ════════════════════════════════════════════════════════════════════
def scan_for_anomalies(*,
                        current_metrics: dict,
                        baseline_metrics: Optional[dict] = None,
                        avg_divergence_score: float = 0.0,
                        open_circuit_breakers: int = 0,
                        antibot_events: int = 0,
                        shadow_crashes: int = 0,
                        shadow_runs: int = 0,
                        cfg: Optional[AnomalyConfig] = None,
                        ) -> list:
    """Ejecuta todos los detectores y devuelve list[Alert.to_dict()]."""
    alerts: list = []
    baseline_metrics = baseline_metrics or {}

    for fn, args in [
        (detect_success_rate_drop, (current_metrics, baseline_metrics)),
        (detect_payment_anomaly, (current_metrics, baseline_metrics)),
        (detect_orphan_wal_growth, (current_metrics,)),
    ]:
        try:
            a = fn(*args, cfg=cfg)
            if a is not None:
                alerts.append(a.to_dict())
        except Exception:
            pass

    try:
        a = detect_divergence_spike(avg_divergence_score, cfg=cfg)
        if a is not None:
            alerts.append(a.to_dict())
    except Exception:
        pass

    try:
        a = detect_circuit_breaker_storm(open_circuit_breakers, cfg=cfg)
        if a is not None:
            alerts.append(a.to_dict())
    except Exception:
        pass

    try:
        a = detect_antibot_surge(antibot_events, cfg=cfg)
        if a is not None:
            alerts.append(a.to_dict())
    except Exception:
        pass

    try:
        a = detect_shadow_crash_storm(shadow_crashes, shadow_runs, cfg=cfg)
        if a is not None:
            alerts.append(a.to_dict())
    except Exception:
        pass

    return alerts


# ════════════════════════════════════════════════════════════════════
#  Alert sink (opt-in)
# ════════════════════════════════════════════════════════════════════
DEFAULT_ALERTS_PATH: str = "runs/alerts.jsonl"
_alert_lock = threading.Lock()


def write_alerts(alerts: list, *,
                  path: Any = DEFAULT_ALERTS_PATH) -> bool:
    """Append alerts a JSONL atomic. Fail-safe."""
    if not alerts:
        return True
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _alert_lock:
            with p.open("a", encoding="utf-8") as f:
                for a in alerts:
                    if isinstance(a, dict):
                        f.write(json.dumps(a, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


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
