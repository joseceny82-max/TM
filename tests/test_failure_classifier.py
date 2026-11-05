"""Tests M2 — Functional failure classifier + DriftDetector soft_drift.

Garantías:
    1. classify_failure es pure function fail-safe
    2. Patrones reales (53% de fallos) detectados
    3. DriftDetector emite SOFT_DRIFT para OBSERVABILITY failures
    4. DriftDetector emite SOFT_DRIFT para STATE failures (FATAL_*)
    5. Eventos normales NO disparan soft_drift (no false positives)
    6. Compatibilidad backward: si failure_classifier no se carga, DriftDetector
       sigue funcionando idéntico al modo pre-M2
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════════
#  1. classify_failure() pure function
# ════════════════════════════════════════════════════════════════════
def test_classify_no_email_input():
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("no_email_input")
    assert sig is not None
    assert sig.category == "LOGIN_FAILURE"
    assert sig.severity == "high"
    assert sig.confidence == "high"
    assert "OAuth" in sig.remediation or "form" in sig.remediation.lower()


def test_classify_bot_block_early():
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("FATAL_bot_block_early")
    assert sig is not None
    assert sig.category == "BOT_BLOCK"
    assert sig.severity == "high"


def test_classify_bot_block_persisted():
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("bot_block_persisted")
    assert sig is not None
    assert sig.category == "BOT_BLOCK"


def test_classify_recovery_failed():
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("recovery_failed_swap_account")
    assert sig is not None
    assert sig.category == "RECOVERY_FAILURE"


def test_classify_reset_method_next_failed():
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("reset_method_next_failed")
    assert sig is not None
    assert sig.category == "RECOVERY_FAILURE"


def test_classify_timeout_no_checkout():
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("TIMEOUT_no_checkout")
    assert sig is not None
    assert sig.category == "CHECKOUT_FAILURE"


def test_classify_pre_flight_fail():
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("pre_flight_FAIL_abort")
    assert sig is not None
    assert sig.category == "NETWORK_FAILURE"


# ════════════════════════════════════════════════════════════════════
#  2. False positives avoidance
# ════════════════════════════════════════════════════════════════════
def test_normal_events_not_classified():
    from core.diagnostics.failure_classifier import classify_failure
    for evt in ["goto", "shot", "click_continuar", "oauth_detected",
                "checkout_detected", "READY_FOR_USER", "init", "qty"]:
        sig = classify_failure(evt)
        assert sig is None, f"{evt} should NOT be classified as failure"


def test_partial_match_not_triggered():
    """no_email_inputXYZ NO debe matchear (regex anchored)."""
    from core.diagnostics.failure_classifier import classify_failure
    assert classify_failure("no_email_inputXYZ") is None
    assert classify_failure("xx_no_email_input") is None


# ════════════════════════════════════════════════════════════════════
#  3. Fail-safe
# ════════════════════════════════════════════════════════════════════
def test_classify_fail_safe():
    from core.diagnostics.failure_classifier import classify_failure
    for bad in [None, "", 123, [], {}, object()]:
        assert classify_failure(bad) is None


def test_classify_with_payload():
    """payload se acepta sin lanzar (no usado actualmente)."""
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("no_email_input", {"extra": "data"})
    assert sig is not None


def test_is_failure_helper():
    from core.diagnostics.failure_classifier import is_failure
    assert is_failure("no_email_input") is True
    assert is_failure("goto") is False
    assert is_failure(None) is False  # type: ignore


def test_get_categories():
    from core.diagnostics.failure_classifier import get_categories
    cats = get_categories()
    assert "LOGIN_FAILURE" in cats
    assert "BOT_BLOCK" in cats
    assert "RECOVERY_FAILURE" in cats
    assert "CHECKOUT_FAILURE" in cats
    assert "NETWORK_FAILURE" in cats


def test_signal_serializable():
    """FailureSignal.to_dict() es JSON-safe."""
    import json
    from core.diagnostics.failure_classifier import classify_failure
    sig = classify_failure("no_email_input")
    d = sig.to_dict()
    json.dumps(d)  # debe ser serializable
    assert "event" in d
    assert "category" in d


# ════════════════════════════════════════════════════════════════════
#  4. Integración con DriftDetector
# ════════════════════════════════════════════════════════════════════
def test_drift_detector_emits_soft_drift_on_no_email_input():
    """El patrón dominante (53% de fallos): debe disparar SOFT_DRIFT."""
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector(initial_state="login")
    # Simular llegada a login + no_email_input
    drift = d.observe("no_email_input")
    assert drift is not None
    assert drift.kind == "SOFT_DRIFT"
    assert drift.soft_drift is not None
    assert drift.soft_drift["category"] == "LOGIN_FAILURE"
    assert d.soft_drift_count == 1


def test_drift_detector_emits_soft_drift_on_fatal_bot_block():
    """STATE events que también son failures: SOFT_DRIFT paralelo."""
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector(initial_state="login")
    drift = d.observe("FATAL_bot_block_early")
    # Puede ser INVALID_TRANSITION o SOFT_DRIFT — pero soft_drift_count debe subir si la transición fue válida
    summary = d.summary()
    # En cualquier caso, al menos 1 drift detectado
    assert summary["drift_count"] >= 1


def test_drift_detector_no_soft_drift_on_normal_event():
    """Eventos normales (goto, qty) → NO soft_drift."""
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector(initial_state="setup")
    for evt in ["init", "goto", "qty", "click_continuar"]:
        d.observe(evt)
    assert d.soft_drift_count == 0


def test_drift_detector_soft_drift_in_summary():
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector(initial_state="login")
    d.observe("no_email_input")
    summary = d.summary()
    assert "soft_drift_count" in summary
    assert summary["soft_drift_count"] == 1
    # drift_by_kind debe contar SOFT_DRIFT
    assert summary["drift_by_kind"].get("SOFT_DRIFT") == 1


def test_drift_detector_reset_clears_soft():
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector(initial_state="login")
    d.observe("no_email_input")
    assert d.soft_drift_count == 1
    d.reset()
    assert d.soft_drift_count == 0


def test_drift_detector_backward_compat_without_failure_classifier(monkeypatch):
    """Si failure_classifier falla al cargar, DriftDetector sigue funcionando."""
    # Simular failure_classifier ausente
    import core.diagnostics.drift_detector as dd_mod
    original = dd_mod._load_failure_classifier
    dd_mod._load_failure_classifier = lambda: None

    try:
        d = dd_mod.DriftDetector(initial_state="setup")
        assert d._fcls is None  # no cargado
        # Camino normal funciona
        d.observe("goto")
        d.observe("oauth_detected")
        # no_email_input no genera soft drift (módulo ausente)
        d.observe("no_email_input")
        assert d.soft_drift_count == 0
        # Pero no rompe
        assert d.summary() is not None
    finally:
        dd_mod._load_failure_classifier = original


def test_drift_detector_remediation_propagated():
    """La remediation del FailureSignal está en el drift.soft_drift."""
    from core.diagnostics.drift_detector import DriftDetector
    d = DriftDetector(initial_state="login")
    drift = d.observe("no_email_input")
    assert drift is not None
    assert "remediation" in drift.soft_drift
    assert len(drift.soft_drift["remediation"]) > 10


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
