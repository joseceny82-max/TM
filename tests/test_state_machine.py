"""tests/test_state_machine.py — Tests del FSM final (5 estados).

Modelo: SETUP, BROWSING, LOGIN, CHECKOUT, EXIT.
Terminal: solo EXIT con outcome attribute (SUCCESS/BLOCKED/FATAL/STOPPED).
"""
from __future__ import annotations

import pytest

from core.observability.state_machine import (
    EVENT_TO_TRANSITION, EventKind, ExitOutcome, InvalidTransitionError,
    LOOPS_ALLOWED, State, StateMachine, TERMINAL_STATES, TRANSITIONS,
    all_loop_events, all_observability_events, all_transition_events,
    classify_event,
)


# ────────────────────────────────────────────────────────────────────
#  Estructura del modelo
# ────────────────────────────────────────────────────────────────────
def test_five_states_exist():
    """Base final: exactamente 5 estados."""
    assert len(State) == 5
    names = {s.value for s in State}
    assert names == {"setup", "browsing", "login", "checkout", "exit"}


def test_only_exit_is_terminal():
    """SUCCESS/BLOCKED ya no son estados — son outcomes en payload."""
    assert TERMINAL_STATES == frozenset({State.EXIT})


def test_four_exit_outcomes():
    """ExitOutcome enum tiene los 4 valores esperados."""
    assert len(ExitOutcome) == 4
    assert {o.value for o in ExitOutcome} == {"success", "blocked", "fatal", "stopped"}


def test_exit_has_no_outgoing_transitions():
    assert TRANSITIONS[State.EXIT] == frozenset()


def test_transitions_only_to_valid_states():
    for from_state, to_set in TRANSITIONS.items():
        for to_state in to_set:
            assert to_state in State


def test_loops_allowed_match_matrix():
    """Cada LOOP debe permitirse en TRANSITIONS."""
    for state in LOOPS_ALLOWED:
        assert state in TRANSITIONS[state]


def test_all_states_reachable_from_setup():
    visited = set()
    frontier = [State.SETUP]
    while frontier:
        s = frontier.pop()
        if s in visited: continue
        visited.add(s)
        frontier.extend(TRANSITIONS.get(s, frozenset()))
    assert visited == set(State), f"No alcanzables: {set(State) - visited}"


# ────────────────────────────────────────────────────────────────────
#  Classifier
# ────────────────────────────────────────────────────────────────────
def test_classify_transition():
    kind, target = classify_event("oauth_detected")
    assert kind == EventKind.TRANSITION
    assert target == State.LOGIN


def test_classify_loop():
    kind, target = classify_event("auto_recovery_start")
    assert kind == EventKind.LOOP
    assert target == State.LOGIN


def test_classify_observability():
    kind, target = classify_event("URL_CHANGED")
    assert kind == EventKind.OBSERVABILITY
    assert target is None


def test_classify_unknown_is_implicit_obs():
    """Eventos sin mapping → UNKNOWN, que son observabilidad implícita
    (viven en el estado actual sin disparar transición)."""
    kind, target = classify_event("totally_made_up_event")
    assert kind == EventKind.UNKNOWN
    assert target is None


def test_transition_loop_obs_disjoint():
    t = all_transition_events()
    l = all_loop_events()
    o = all_observability_events()
    assert t & l == set()
    assert t & o == set()
    assert l & o == set()


# ────────────────────────────────────────────────────────────────────
#  StateMachine — flow básico
# ────────────────────────────────────────────────────────────────────
def test_initial_state_is_setup():
    sm = StateMachine()
    assert sm.current == State.SETUP
    assert not sm.is_terminal()


def test_setup_to_browsing_via_goto():
    sm = StateMachine()
    sm.feed_event("goto")
    assert sm.current == State.BROWSING


def test_browsing_to_login_via_oauth():
    sm = StateMachine()
    sm.feed_event("goto")
    sm.feed_event("oauth_detected")
    assert sm.current == State.LOGIN


def test_invalid_transition_strict_raises():
    sm = StateMachine(strict=True)
    # SETUP -> CHECKOUT directo es inválido (debe pasar por BROWSING o LOGIN)
    with pytest.raises(InvalidTransitionError):
        sm.transition_to(State.CHECKOUT, reason="bad")


def test_invalid_transition_non_strict_records():
    sm = StateMachine(strict=False)
    tr = sm.transition_to(State.CHECKOUT, reason="bad")
    assert not tr.valid
    assert sm.current == State.SETUP


# ────────────────────────────────────────────────────────────────────
#  LOOPS REALES
# ────────────────────────────────────────────────────────────────────
def test_login_loop_recovery_cycle():
    """auto_recovery_start + account_swap + recovery_result = 3 loops."""
    sm = StateMachine()
    for evt in ["goto", "oauth_detected"]:
        sm.feed_event(evt)
    assert sm.current == State.LOGIN
    sm.feed_event("auto_recovery_start")
    sm.feed_event("account_swap")
    sm.feed_event("recovery_result")
    assert sm.current == State.LOGIN
    assert sm.summary()["loop_count"] == 3


def test_checkout_loop_stages():
    sm = StateMachine()
    for evt in ["goto", "oauth_detected", "checkout_detected"]:
        sm.feed_event(evt)
    assert sm.current == State.CHECKOUT
    sm.feed_event("checkout_stage_btn_clicked")
    sm.feed_event("ir_al_pago_clicked")
    sm.feed_event("card_fill_retry")
    assert sm.current == State.CHECKOUT
    assert sm.summary()["loop_count"] == 3


def test_loop_event_in_wrong_state_ignored():
    """`auto_recovery_start` solo aplica si estado actual = LOGIN."""
    sm = StateMachine()  # estado SETUP
    tr = sm.feed_event("auto_recovery_start")
    # No estamos en LOGIN, el loop NO se aplica
    assert tr is None
    assert sm.current == State.SETUP


# ────────────────────────────────────────────────────────────────────
#  EXIT con outcomes en payload (no estados separados)
# ────────────────────────────────────────────────────────────────────
def test_success_outcome_via_ready_then_stop():
    """READY_FOR_USER es observability + stop_received → EXIT con SUCCESS."""
    sm = StateMachine()
    for evt in ["goto", "oauth_detected", "checkout_detected"]:
        sm.feed_event(evt)
    sm.feed_event("READY_FOR_USER")        # marca seen_ready (obs)
    sm.feed_event("stop_received")          # CHECKOUT → EXIT
    assert sm.current == State.EXIT
    assert sm.exit_outcome == ExitOutcome.SUCCESS


def test_blocked_outcome_via_bot_block():
    sm = StateMachine()
    sm.feed_event("goto")
    sm.feed_event("FATAL_bot_block_early")
    assert sm.current == State.EXIT
    assert sm.exit_outcome == ExitOutcome.BLOCKED


def test_fatal_outcome_via_no_profile():
    sm = StateMachine()
    sm.feed_event("FATAL_no_profile")
    assert sm.current == State.EXIT
    assert sm.exit_outcome == ExitOutcome.FATAL


def test_stopped_outcome_user_abort():
    """Si el user envía STOP sin haber alcanzado READY_FOR_USER → STOPPED."""
    sm = StateMachine()
    sm.feed_event("goto")
    sm.feed_event("stop_received")
    assert sm.current == State.EXIT
    assert sm.exit_outcome == ExitOutcome.STOPPED


# ────────────────────────────────────────────────────────────────────
#  Walkthroughs end-to-end
# ────────────────────────────────────────────────────────────────────
def test_full_success_walkthrough():
    sm = StateMachine()
    flow = [
        "init", "goto", "click_continuar", "oauth_detected",
        "auto_recovery_start", "checkout_detected",
        "checkout_stage_btn_clicked", "card_fill_retry",
        "READY_FOR_USER", "stop_received",
    ]
    for evt in flow:
        sm.feed_event(evt)
    assert sm.current == State.EXIT
    assert sm.exit_outcome == ExitOutcome.SUCCESS
    s = sm.summary()
    assert s["invalid_count"] == 0
    assert s["loop_count"] >= 3


def test_full_blocked_walkthrough():
    sm = StateMachine()
    for evt in ["goto", "oauth_detected", "FATAL_bot_block_detected"]:
        sm.feed_event(evt)
    assert sm.current == State.EXIT
    assert sm.exit_outcome == ExitOutcome.BLOCKED


def test_no_invalid_transitions_in_realistic_flow():
    """Aplicar todos los eventos críticos en orden razonable, esperar 0 inválidas."""
    sm = StateMachine(strict=False)
    realistic = [
        "init", "pre_sleep_for_lock",            # SETUP obs
        "goto", "URL_CHANGED",                    # SETUP → BROWSING
        "click_continuar", "continuar_clicked_js",  # loops BROWSING
        "oauth_detected",                          # BROWSING → LOGIN
        "auto_recovery_start", "account_swap",    # loops LOGIN
        "tm_forced_password_reset_detected",
        "recovery_result",
        "checkout_detected",                       # LOGIN → CHECKOUT
        "checkout_stage_url", "ir_al_pago_clicked",  # loops CHECKOUT
        "card_fill_retry", "card_iframe_trying",
        "shot",                                    # obs
        "READY_FOR_USER",                         # obs
        "stop_received",                          # CHECKOUT → EXIT
    ]
    for evt in realistic:
        sm.feed_event(evt)
    summary = sm.summary()
    assert summary["invalid_count"] == 0
    assert summary["current"] == "exit"
    assert summary["exit_outcome"] == "success"


# ────────────────────────────────────────────────────────────────────
#  Coverage contra 231 eventos reales del legacy
# ────────────────────────────────────────────────────────────────────
def test_classifier_handles_231_real_events():
    """Cargar los 231 eventos reales del legacy y verificar que ninguno crashea."""
    import re
    import pathlib
    src_path = pathlib.Path(__file__).parent.parent / "_compra_parallel.py"
    if not src_path.exists():
        pytest.skip("_compra_parallel.py not present")
    src = src_path.read_text(encoding="utf-8")
    events = sorted(set(re.findall(r'log\(slot,\s*[\"\'](\w+)[\"\']', src)))
    assert len(events) > 200, "Should have 200+ unique events"
    for evt in events:
        kind, target = classify_event(evt)
        # Cada evento debe clasificarse en una de las 4 categorías
        assert kind in EventKind, f"Bad kind for {evt}"
        if kind in (EventKind.TRANSITION, EventKind.LOOP):
            assert target is not None and target in State


def test_minimal_mapping_avoids_overfitting():
    """No más de ~50 eventos en mapping explícito (evitar overfitting)."""
    total = len(EVENT_TO_TRANSITION)
    assert total <= 60, f"Mapping demasiado grande ({total}). Evitar overfitting."
    assert total >= 20, f"Mapping demasiado pequeño ({total}). Cubrir transiciones esenciales."
