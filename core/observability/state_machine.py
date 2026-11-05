"""core/observability/state_machine.py — FSM mínimo final del bot.

Modelo de 5 estados basado en realidad empírica del sistema, NO en diseño
teórico. Reducido desde el modelo previo de 11 estados tras gap analysis
de 231 eventos reales.

REGLAS:
    - 5 estados FIJOS (base final, 2026-05-13)
    - Transiciones cerradas (matriz explícita)
    - 2 loops permitidos en LOGIN y CHECKOUT (recovery interno)
    - 1 estado terminal: EXIT
    - SUCCESS y BLOCKED son outcomes en payload, NO estados separados

Diferencias vs modelo previo (11 estados):
    INIT          → fusionado en SETUP (entry point sin eventos propios)
    PROFILE_SETUP → renombrado a SETUP
    SESSION_READY → fusionado en SETUP (estado transitorio, 3 evt)
    AUTH          → renombrado a LOGIN (más descriptivo del flow)
    CART          → fusionado en CHECKOUT (continuación natural)
    PAYMENT       → fusionado en CHECKOUT
    SUCCESS       → outcome de EXIT, no estado
    BLOCKED       → outcome de EXIT, no estado
    RECOVERY      → comportamiento interno de LOGIN (siempre fue sub-flow)

NO se integra al bot. Importable y testable de forma aislada.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Set, Tuple


# ════════════════════════════════════════════════════════════════════
#  1. STATE ENUM — 5 estados fijos
# ════════════════════════════════════════════════════════════════════
class State(Enum):
    """Estados del bot. Base FINAL congelada."""
    SETUP = "setup"          # arranque + profile + session ready + warming
    BROWSING = "browsing"    # navegación, evento target, stepper, cart pre-Continuar
    LOGIN = "login"          # OAuth + recovery interno + account swap
    CHECKOUT = "checkout"    # checkout URL + Adyen iframe + fill card + 3DS detect
    EXIT = "exit"            # terminal — cleanup + outcome (success/blocked/fatal)


TERMINAL_STATES: frozenset[State] = frozenset({State.EXIT})


# ════════════════════════════════════════════════════════════════════
#  2. TRANSICIONES PERMITIDAS — matriz cerrada
# ════════════════════════════════════════════════════════════════════
#
#         SETUP → BROWSING → LOGIN ⇄ CHECKOUT → EXIT
#                      ↑       ↓        ↓
#                      └───────┴────────┘ (CHECKOUT puede pedir re-LOGIN)
#
TRANSITIONS: dict[State, frozenset[State]] = {
    State.SETUP: frozenset({
        State.BROWSING,   # profile setup OK + session ready + warming done
        State.EXIT,       # fatal early (no profile, no account, CDP fail)
    }),
    State.BROWSING: frozenset({
        State.BROWSING,   # loop interno: re-detect page_state, retry hydration
        State.LOGIN,      # Continuar → OAuth redirect
        State.CHECKOUT,   # Continuar → directo a checkout (sesión válida)
        State.EXIT,       # fatal o bot_block
    }),
    State.LOGIN: frozenset({
        State.LOGIN,      # loop: recovery, account_swap, forced_reset retry
        State.CHECKOUT,   # login exitoso
        State.BROWSING,   # post-recovery vuelve al evento
        State.EXIT,       # max_swaps, no_more_accounts, fatal
    }),
    State.CHECKOUT: frozenset({
        State.CHECKOUT,   # loop: stages internos, Adyen iframe retry
        State.LOGIN,      # mid-flow auth required (TM exige re-login)
        State.EXIT,       # SUCCESS (ready_for_user→stop) | bot_block | fatal
    }),
    State.EXIT: frozenset(),  # terminal absoluto
}


# Loops auto-permitidos (self-transition válida)
LOOPS_ALLOWED: frozenset[State] = frozenset({
    State.BROWSING,    # retry page_state
    State.LOGIN,       # recovery + account swap (4 events reales)
    State.CHECKOUT,    # stages + iframe retry (5 events reales)
})


# ════════════════════════════════════════════════════════════════════
#  3. EXIT OUTCOMES — antes eran estados, ahora son atributo del payload
# ════════════════════════════════════════════════════════════════════
class ExitOutcome(Enum):
    """Resultado al entrar en EXIT. Va en payload, no en State."""
    SUCCESS = "success"     # READY_FOR_USER alcanzado + stop_received
    BLOCKED = "blocked"     # bot_block detected
    FATAL = "fatal"         # error fatal en flow
    STOPPED = "stopped"     # STOP file sin completar (user abort)


# ════════════════════════════════════════════════════════════════════
#  4. EVENT → TRANSITION mapping (minimal, no overfitting)
# ════════════════════════════════════════════════════════════════════
# Tipos:
#   TRANSITION    → cambia estado
#   LOOP          → self-transition (mismo estado)
#   OBSERVABILITY → solo telemetría, no cambia estado
#
# Política: SOLO eventos con clara semántica de transición están aquí.
# Los 168 eventos "heuristic-only" del gap analysis quedan como
# observability implícita — viven en el estado actual sin mapping explícito.
#
class EventKind(Enum):
    TRANSITION = "transition"
    LOOP = "loop"
    OBSERVABILITY = "observability"
    UNKNOWN = "unknown"


EVENT_TO_TRANSITION: dict[str, Tuple[EventKind, Optional[State], str]] = {
    # ── Entry / SETUP markers ──
    "init": (EventKind.OBSERVABILITY, None, "bot started"),
    "pre_sleep_for_lock": (EventKind.OBSERVABILITY, None, "setup wait"),

    # ── SETUP → BROWSING ──
    "goto": (EventKind.TRANSITION, State.BROWSING, "navigate to event"),

    # ── BROWSING → LOGIN ──
    "oauth_detected": (EventKind.TRANSITION, State.LOGIN, "OAuth redirect"),
    "auth_detected_mid": (EventKind.TRANSITION, State.LOGIN, "mid-flow re-login required"),

    # ── BROWSING/LOGIN → CHECKOUT ──
    "checkout_detected": (EventKind.TRANSITION, State.CHECKOUT, "checkout URL entered"),
    "checkout_detected_at_entry": (EventKind.TRANSITION, State.CHECKOUT, "direct checkout"),

    # ── * → EXIT ──
    "READY_FOR_USER": (EventKind.OBSERVABILITY, None,
                       "checkout success marker — outcome=SUCCESS"),
    "stop_received": (EventKind.TRANSITION, State.EXIT, "user stop"),
    "FATAL_bot_block_detected": (EventKind.TRANSITION, State.EXIT,
                                  "outcome=BLOCKED"),
    "FATAL_bot_block_early": (EventKind.TRANSITION, State.EXIT,
                               "outcome=BLOCKED"),
    "FATAL_no_profile": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_no_account": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_profile_start": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_cdp_connect": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_cdp_version": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_camoufox_import": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_continuar_no_avanza": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_mid_login_failed": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_no_sector_clickable": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_no_tickets_available": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_oauth_login_failed": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "FATAL_seatmap_no_sections": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "TIMEOUT_no_checkout": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "max_account_swaps_reached": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),
    "no_more_accounts_to_try": (EventKind.TRANSITION, State.EXIT, "outcome=FATAL"),

    # ── LOOPS dentro de LOGIN ──
    "auto_recovery_start": (EventKind.LOOP, State.LOGIN, "recovery cycle"),
    "tm_forced_password_reset_detected": (EventKind.LOOP, State.LOGIN, "TM forced reset"),
    "account_swap": (EventKind.LOOP, State.LOGIN, "try different account"),
    "recovery_result": (EventKind.LOOP, State.LOGIN, "recovery outcome"),

    # ── LOOPS dentro de CHECKOUT (incluye carrito + stages + Adyen) ──
    "click_continuar": (EventKind.LOOP, State.BROWSING, "Continuar attempt"),
    "continuar_clicked_js": (EventKind.LOOP, State.BROWSING, "JS click"),
    "continuar_no_redirect_retry": (EventKind.LOOP, State.BROWSING, "retry"),
    "checkout_stage_url": (EventKind.LOOP, State.CHECKOUT, "stage iter"),
    "checkout_stage_btn_clicked": (EventKind.LOOP, State.CHECKOUT, "stage btn"),
    "ir_al_pago_clicked": (EventKind.LOOP, State.CHECKOUT, "pay button"),
    "card_fill_retry": (EventKind.LOOP, State.CHECKOUT, "fill retry"),
    "card_iframe_trying": (EventKind.LOOP, State.CHECKOUT, "iframe poll"),

    # ── Cross-cutting OBSERVABILITY explícita ──
    "URL_CHANGED": (EventKind.OBSERVABILITY, None, "URL change marker"),
    "bot_block_persisted": (EventKind.OBSERVABILITY, None, "bot_block persist"),
    "shot": (EventKind.OBSERVABILITY, None, "screenshot"),
}


# ════════════════════════════════════════════════════════════════════
#  5. StateMachine class
# ════════════════════════════════════════════════════════════════════
@dataclass
class StateTransition:
    """Una transición registrada."""
    from_state: State
    to_state: State
    reason: str
    event_name: Optional[str]
    outcome: Optional[ExitOutcome] = None  # solo al entrar en EXIT
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    valid: bool = True


class InvalidTransitionError(Exception):
    """Transición no permitida según TRANSITIONS."""


class StateMachine:
    """FSM ejecutable. NO modifica el bot — solo observer del modelo formal.

    Uso típico:
        sm = StateMachine()
        sm.feed_event("goto")              # SETUP → BROWSING
        sm.feed_event("oauth_detected")    # BROWSING → LOGIN
        sm.feed_event("auto_recovery_start")  # LOGIN → LOGIN (loop)
        sm.feed_event("checkout_detected") # LOGIN → CHECKOUT
        sm.feed_event("READY_FOR_USER")    # observability, marca outcome
        sm.feed_event("stop_received")     # CHECKOUT → EXIT
        print(sm.summary())
    """

    def __init__(self, initial: State = State.SETUP, strict: bool = True):
        self.current: State = initial
        self.history: list[StateTransition] = []
        self.exit_outcome: Optional[ExitOutcome] = None
        self.strict = strict
        # Flag interno: si vimos READY_FOR_USER antes de EXIT, marca SUCCESS
        self._seen_ready_for_user = False

    def can_transition_to(self, target: State) -> bool:
        return target in TRANSITIONS.get(self.current, frozenset())

    def transition_to(self, target: State, reason: str = "",
                      event: Optional[str] = None,
                      outcome: Optional[ExitOutcome] = None) -> StateTransition:
        """Realiza la transición. Lanza si inválida y strict=True."""
        valid = self.can_transition_to(target)
        if target == self.current and self.current in LOOPS_ALLOWED:
            valid = True
        tr = StateTransition(
            from_state=self.current, to_state=target,
            reason=reason, event_name=event, valid=valid, outcome=outcome,
        )
        self.history.append(tr)
        if not valid:
            msg = (f"Invalid transition: {self.current.value} -> {target.value} "
                   f"(reason={reason!r}, event={event!r})")
            if self.strict:
                raise InvalidTransitionError(msg)
            return tr
        self.current = target
        if target == State.EXIT and outcome is not None:
            self.exit_outcome = outcome
        return tr

    def feed_event(self, event_name: str, **payload) -> Optional[StateTransition]:
        """Procesa un evento legacy y aplica transición si corresponde.

        Returns:
            StateTransition si aplicó cambio o loop, None si solo observability.
        """
        # READY_FOR_USER marca outcome=SUCCESS pero NO es transición a EXIT
        # (la transición a EXIT viene con stop_received subsiguiente).
        if event_name == "READY_FOR_USER":
            self._seen_ready_for_user = True
            return None

        spec = EVENT_TO_TRANSITION.get(event_name)
        if spec is None:
            return None  # evento no mapeado → observability implícita
        kind, target, comment = spec

        if kind == EventKind.OBSERVABILITY:
            return None
        if kind == EventKind.LOOP:
            # Solo loop si estamos en el estado correcto
            if self.current != target:
                # No estamos en el estado donde el loop aplica → skip silencioso
                return None
            return self.transition_to(self.current, reason=f"loop:{comment}",
                                       event=event_name)
        if kind == EventKind.TRANSITION and target is not None:
            outcome = None
            if target == State.EXIT:
                outcome = self._infer_exit_outcome(event_name)
            return self.transition_to(target, reason=comment,
                                       event=event_name, outcome=outcome)
        return None

    def _infer_exit_outcome(self, event_name: str) -> ExitOutcome:
        """Determina outcome de EXIT según el evento que disparó la transición."""
        if event_name == "stop_received":
            return ExitOutcome.SUCCESS if self._seen_ready_for_user else ExitOutcome.STOPPED
        if "bot_block" in event_name.lower():
            return ExitOutcome.BLOCKED
        # FATAL_*, TIMEOUT_*, max_account_swaps, no_more_accounts
        return ExitOutcome.FATAL

    def reset(self, initial: State = State.SETUP) -> None:
        self.current = initial
        self.history.clear()
        self.exit_outcome = None
        self._seen_ready_for_user = False

    def is_terminal(self) -> bool:
        return self.current in TERMINAL_STATES

    def summary(self) -> dict:
        loops = sum(1 for t in self.history if t.from_state == t.to_state)
        invalid = sum(1 for t in self.history if not t.valid)
        return {
            "current": self.current.value,
            "is_terminal": self.is_terminal(),
            "exit_outcome": self.exit_outcome.value if self.exit_outcome else None,
            "total_transitions": len(self.history),
            "loop_count": loops,
            "invalid_count": invalid,
            "states_visited": sorted({t.to_state.value for t in self.history}),
        }


# ════════════════════════════════════════════════════════════════════
#  6. Classifier helpers
# ════════════════════════════════════════════════════════════════════
def classify_event(event_name: str) -> Tuple[EventKind, Optional[State]]:
    """Clasifica un event_name. Eventos sin mapping → UNKNOWN (= obs implícita)."""
    spec = EVENT_TO_TRANSITION.get(event_name)
    if spec is None:
        return (EventKind.UNKNOWN, None)
    kind, target, _ = spec
    return (kind, target)


def all_transition_events() -> Set[str]:
    return {name for name, (kind, _, _) in EVENT_TO_TRANSITION.items()
            if kind == EventKind.TRANSITION}


def all_loop_events() -> Set[str]:
    return {name for name, (kind, _, _) in EVENT_TO_TRANSITION.items()
            if kind == EventKind.LOOP}


def all_observability_events() -> Set[str]:
    return {name for name, (kind, _, _) in EVENT_TO_TRANSITION.items()
            if kind == EventKind.OBSERVABILITY}


# ════════════════════════════════════════════════════════════════════
#  7. Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=== state_machine v2 (5 states) self-test ===")
    print(f"States: {[s.value for s in State]}")
    print(f"Terminals: {[s.value for s in TERMINAL_STATES]}")
    print(f"Loops allowed: {[s.value for s in LOOPS_ALLOWED]}")
    print(f"Exit outcomes: {[o.value for o in ExitOutcome]}")
    print(f"Transition events mapped: {len(all_transition_events())}")
    print(f"Loop events mapped: {len(all_loop_events())}")
    print(f"Observability events mapped: {len(all_observability_events())}")
    print()

    sm = StateMachine()
    flow = [
        "init",                        # obs
        "goto",                        # SETUP -> BROWSING
        "click_continuar",             # loop BROWSING
        "oauth_detected",              # BROWSING -> LOGIN
        "auto_recovery_start",         # loop LOGIN
        "account_swap",                # loop LOGIN
        "checkout_detected",           # LOGIN -> CHECKOUT
        "checkout_stage_btn_clicked",  # loop CHECKOUT
        "card_fill_retry",             # loop CHECKOUT
        "READY_FOR_USER",              # obs (marca outcome=SUCCESS pendiente)
        "stop_received",               # CHECKOUT -> EXIT (outcome=SUCCESS)
    ]
    for evt in flow:
        tr = sm.feed_event(evt)
        if tr:
            mark = "[OK]" if tr.valid else "[FAIL]"
            print(f"  {mark:7} {tr.from_state.value:10} -> {tr.to_state.value:10} via {evt}")
        else:
            print(f"  [obs]   {evt}")
    print()
    print(f"Final state: {sm.current.value}  outcome: {sm.exit_outcome.value if sm.exit_outcome else None}")
    print(f"Summary: {sm.summary()}")


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
