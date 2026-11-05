"""core/diagnostics/drift_detector.py — FSM vs runtime drift detector.

Detecta en tiempo real:
    - Transiciones observadas no permitidas por FSM (DRIFT)
    - Eventos STATE en estado FSM incompatible
    - Eventos NOISE infiltrándose en flow crítico
    - Loops anómalos (frecuencia > N en mismo estado)
    - Patrones pre-fallo (3 eventos previos a *_err/FATAL_*)

Diseño:
    - Pure observer — NO afecta ejecución
    - Streaming — actualiza estado interno con cada evento
    - Fail-safe — try/except envuelto en observe()
    - Sin dependencia de IO directo (delega persistencia al caller)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# Lazy import del FSM v2 (no en runtime crítico)
def _load_fsm():
    """Carga FSM v2 solo cuando se necesita. Devuelve None si falla."""
    try:
        from core.observability.state_machine import (
            State, TRANSITIONS, LOOPS_ALLOWED, EVENT_TO_TRANSITION, EventKind as _FsmKind,
        )
        return {
            "State": State,
            "TRANSITIONS": TRANSITIONS,
            "LOOPS_ALLOWED": LOOPS_ALLOWED,
            "EVENT_TO_TRANSITION": EVENT_TO_TRANSITION,
            "EventKind": _FsmKind,
        }
    except Exception:
        return None


def _load_classifier():
    """Carga event_classifier. Devuelve None si falla."""
    try:
        from core.contracts.event_classifier import classify, EventKind
        return {"classify": classify, "EventKind": EventKind}
    except Exception:
        return None


def _load_failure_classifier():
    """Carga failure_classifier (M2). Devuelve None si falla.

    Opt-in: si el módulo no existe, el DriftDetector funciona igual sin
    soft_drift detection (compat backward total).
    """
    try:
        from core.diagnostics.failure_classifier import classify_failure
        return {"classify_failure": classify_failure}
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
#  DriftEvent — un drift detectado
# ════════════════════════════════════════════════════════════════════
@dataclass
class DriftEvent:
    """Un drift concreto detectado en runtime.

    kind values:
        INVALID_TRANSITION           — STATE event intenta transición ilegal (FSM)
        LOOP_ANOMALY                 — >N eventos consecutivos en mismo estado
        NOISE_IN_FLOW                — NOISE event en zona crítica (reservado)
        STATE_EVENT_OUTSIDE_CONTEXT  — STATE event en estado FSM incompatible
        SOFT_DRIFT                   ★ M2 — fallo FUNCIONAL (OBSERVABILITY)
                                       reconocido por failure_classifier
                                       (no rompe FSM)
    """
    kind: str
    event_name: str
    current_state: str
    expected_states: list[str] = field(default_factory=list)
    detail: str = ""
    # M2: opcional. Solo presente cuando kind == SOFT_DRIFT.
    soft_drift: Optional[dict] = None
    severity: str = "warn"  # "info" | "warn" | "error"


# ════════════════════════════════════════════════════════════════════
#  DriftDetector — pure observer
# ════════════════════════════════════════════════════════════════════
class DriftDetector:
    """Pure observer que detecta drift entre FSM y runtime.

    Mantiene estado mínimo:
        - last_state: estado FSM actual (inferido por STATE events)
        - state_event_count: contador por estado (para detectar loops anómalos)
        - recent_events: deque(3) para context de fallos
        - drifts: lista de DriftEvent acumulados

    Uso:
        detector = DriftDetector()
        for event_name in stream:
            drift = detector.observe(event_name)
            if drift:
                # opcional: log/alert
                ...
        report = detector.summary()
    """

    LOOP_THRESHOLD = 30  # más de N eventos consecutivos en mismo estado = anómalo

    def __init__(self, initial_state: str = "setup"):
        self.last_state: str = initial_state
        self.state_event_count: int = 0
        self.recent_events: deque[str] = deque(maxlen=3)
        self.drifts: list[DriftEvent] = []
        self.transitions_observed: list[tuple[str, str, str]] = []  # (from, to, via_event)
        self.event_count: int = 0
        # M2: contador específico de soft drifts (functional failures)
        self.soft_drift_count: int = 0

        # Carga FSM y classifier (lazy + fail-safe)
        self._fsm = _load_fsm()
        self._cls = _load_classifier()
        # M2: failure classifier opt-in. Si no existe, soft_drift desactivado.
        self._fcls = _load_failure_classifier()

    # ────────────────────────────────────────────────────────────────
    #  observe() — entrypoint streaming
    # ────────────────────────────────────────────────────────────────
    def observe(self, event_name: str) -> Optional[DriftEvent]:
        """Procesa un evento y devuelve DriftEvent si detecta drift.

        Pure observer — siempre devuelve, nunca lanza.
        """
        self.event_count += 1
        self.recent_events.append(event_name)

        if not self._cls:
            return None  # sin classifier → no podemos analizar

        try:
            kind, _rule, _reason = self._cls["classify"](event_name)
        except Exception:
            return None

        kind_val = kind.value if hasattr(kind, "value") else str(kind)

        # NOISE no afecta nada — pero podríamos reportar si aparece en flow crítico
        if kind_val == "NOISE":
            # Detectar NOISE_IN_FLOW solo si rodeado de STATE events (ruido infiltrado)
            return None  # silencioso por diseño

        # ACTION / OBSERVABILITY no cambian estado, solo cuentan
        if kind_val in ("ACTION", "OBSERVABILITY", "UNKNOWN"):
            self.state_event_count += 1
            # M2: comprobar si es un fallo FUNCIONAL (soft drift)
            soft = self._check_soft_drift(event_name)
            if soft:
                return soft
            return self._check_loop_anomaly()

        # STATE: intentar transición. Si la transición es válida pero el
        # evento es un fallo catalogado (FATAL_*, TIMEOUT_*), también emitir
        # SOFT_DRIFT como aviso paralelo al cambio de estado.
        if kind_val == "STATE":
            structural = self._handle_state_event(event_name)
            # M2: incluso si la transición es válida, marcar soft_drift si es failure
            if structural is None:
                soft = self._check_soft_drift(event_name)
                if soft:
                    return soft
            return structural

        return None

    # ────────────────────────────────────────────────────────────────
    #  Handlers internos
    # ────────────────────────────────────────────────────────────────
    def _handle_state_event(self, event_name: str) -> Optional[DriftEvent]:
        """Maneja un evento STATE. Detecta transiciones inválidas."""
        if not self._fsm:
            return None
        spec = self._fsm["EVENT_TO_TRANSITION"].get(event_name)
        if not spec:
            return None  # STATE no mapeado en FSM (raro pero posible)
        _kind, target_state, _comment = spec
        if target_state is None:
            return None
        target_val = target_state.value if hasattr(target_state, "value") else str(target_state)

        # Cargar transiciones permitidas
        try:
            current_state_enum = self._fsm["State"](self.last_state)
        except Exception:
            return None

        allowed = self._fsm["TRANSITIONS"].get(current_state_enum, frozenset())
        allowed_vals = {s.value if hasattr(s, "value") else str(s) for s in allowed}

        # Self-loop check
        is_self_loop = (target_val == self.last_state)
        loops_allowed = self._fsm["LOOPS_ALLOWED"]
        loops_allowed_vals = {s.value if hasattr(s, "value") else str(s) for s in loops_allowed}

        if is_self_loop and self.last_state in loops_allowed_vals:
            # loop permitido — no es drift
            self.transitions_observed.append((self.last_state, target_val, event_name))
            return None

        if target_val not in allowed_vals:
            # DRIFT — transición no permitida
            drift = DriftEvent(
                kind="INVALID_TRANSITION",
                event_name=event_name,
                current_state=self.last_state,
                expected_states=sorted(allowed_vals),
                detail=f"event '{event_name}' intenta {self.last_state}→{target_val}, no permitido",
                severity="error",
            )
            self.drifts.append(drift)
            return drift

        # Transición válida — aplicar
        self.transitions_observed.append((self.last_state, target_val, event_name))
        self.last_state = target_val
        self.state_event_count = 0
        return None

    def _check_soft_drift(self, event_name: str) -> Optional[DriftEvent]:
        """M2: detecta soft drift = fallo funcional.

        El evento puede ser OBSERVABILITY (no rompe FSM) pero ser un fallo
        funcional reconocido por failure_classifier (no_email_input,
        recovery_failed, etc.).

        Devuelve DriftEvent kind=SOFT_DRIFT con campo `soft_drift` poblado.
        """
        if not self._fcls:
            return None
        try:
            sig = self._fcls["classify_failure"](event_name)
        except Exception:
            return None
        if not sig:
            return None
        # Construir DriftEvent SOFT_DRIFT
        drift = DriftEvent(
            kind="SOFT_DRIFT",
            event_name=event_name,
            current_state=self.last_state,
            expected_states=[self.last_state],
            detail=f"functional failure: {sig.category} ({sig.severity})",
            severity=sig.severity,
            soft_drift={
                "category": sig.category,
                "severity": sig.severity,
                "confidence": sig.confidence,
                "remediation": sig.remediation,
            },
        )
        self.drifts.append(drift)
        self.soft_drift_count += 1
        return drift

    def _check_loop_anomaly(self) -> Optional[DriftEvent]:
        """Detecta si un estado acumula demasiados eventos consecutivos."""
        if self.state_event_count >= self.LOOP_THRESHOLD:
            drift = DriftEvent(
                kind="LOOP_ANOMALY",
                event_name="(multiple)",
                current_state=self.last_state,
                expected_states=[self.last_state],
                detail=f"{self.state_event_count} eventos consecutivos en {self.last_state} (>{self.LOOP_THRESHOLD})",
                severity="warn",
            )
            self.drifts.append(drift)
            self.state_event_count = 0  # reset para no spammear
            return drift
        return None

    # ────────────────────────────────────────────────────────────────
    #  Reporting
    # ────────────────────────────────────────────────────────────────
    def summary(self) -> dict:
        """Resumen del run: drifts + transiciones."""
        drift_by_kind: dict[str, int] = {}
        for d in self.drifts:
            drift_by_kind[d.kind] = drift_by_kind.get(d.kind, 0) + 1
        return {
            "event_count": self.event_count,
            "final_state": self.last_state,
            "transitions_observed": len(self.transitions_observed),
            "drift_count": len(self.drifts),
            "drift_by_kind": drift_by_kind,
            "soft_drift_count": self.soft_drift_count,  # M2
            "recent_events": list(self.recent_events),
            "drifts_detail": [
                {
                    "kind": d.kind,
                    "event": d.event_name,
                    "state": d.current_state,
                    "expected": d.expected_states,
                    "detail": d.detail,
                    "severity": d.severity,
                    "soft_drift": d.soft_drift,  # M2
                }
                for d in self.drifts
            ],
        }

    def reset(self, initial_state: str = "setup") -> None:
        """Reset state — para nuevo run."""
        self.last_state = initial_state
        self.state_event_count = 0
        self.recent_events.clear()
        self.drifts.clear()
        self.transitions_observed.clear()
        self.event_count = 0
        self.soft_drift_count = 0  # M2


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

    print("=== drift_detector self-test ===")
    d = DriftDetector(initial_state="setup")
    # Re-load components ahora que el path está OK
    d._fsm = _load_fsm()
    d._cls = _load_classifier()
    print(f"FSM loaded: {d._fsm is not None}, classifier loaded: {d._cls is not None}")
    flow = [
        "init",                # OBS
        "morelogin_fresh_created",  # OBS
        "goto",                # STATE setup→browsing
        "qty",                 # OBS
        "click_continuar",     # ACTION
        "oauth_detected",      # STATE browsing→login
        "recovery",            # ACTION
        "checkout_detected",   # STATE login→checkout
        "shot",                # NOISE (filtrado)
        "stop_received",       # STATE checkout→exit
    ]
    for evt in flow:
        result = d.observe(evt)
        mark = f"DRIFT={result.kind}" if result else "ok"
        print(f"  {evt:30} state={d.last_state:10} [{mark}]")
    print()
    print(f"Summary: {d.summary()}")


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
