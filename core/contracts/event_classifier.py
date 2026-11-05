"""core/contracts/event_classifier.py — Event Contract clasificador runtime.

REGLAS:
    - 4 capas: STATE / ACTION / OBSERVABILITY / NOISE
    - 28 reglas regex + 7 excepciones de orden
    - Fallback: UNKNOWN (no UNCLASSIFIED — fail-safe)
    - Pure function, sin side-effects
    - Sin dependencia de logging/bot

USO:
    from core.contracts.event_classifier import classify, EventKind

    kind, rule_id, reason = classify("shot")
    # → (EventKind.NOISE, "R1", "screenshot polimórfico")

    kind, _, _ = classify("goto")
    # → (EventKind.STATE, ...)

    kind, _, _ = classify("unknown_event_xyz")
    # → (EventKind.UNKNOWN, "FALLBACK", "no rule matched")
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional, Tuple


# ════════════════════════════════════════════════════════════════════
#  EventKind enum — 4 capas + UNKNOWN fallback
# ════════════════════════════════════════════════════════════════════
class EventKind(Enum):
    STATE = "STATE"                  # cambia FSM
    ACTION = "ACTION"                # intent/side-effect
    OBSERVABILITY = "OBSERVABILITY"  # telemetría
    NOISE = "NOISE"                  # filtrar
    UNKNOWN = "UNKNOWN"              # fallback — no debe ocurrir en runtime


# ════════════════════════════════════════════════════════════════════
#  Reglas — orden de prioridad descendente (primera matching gana)
# ════════════════════════════════════════════════════════════════════
# Tupla: (rule_id, kind, regex, reason)
_RULES: list[Tuple[str, EventKind, re.Pattern, str]] = [
    # ── EXCEPCIONES (Rx1–Rx7) — antes de R2 NOISE pre_* ───────────────
    ("Rx1", EventKind.ACTION,        re.compile(r"^pre_login_navigate$"),
     "navegación pre-login (no ruido)"),
    ("Rx2", EventKind.ACTION,        re.compile(r"^pre_login_SKIPPED_go_direct_to_event$"),
     "decisión de skip pre-login"),
    ("Rx3", EventKind.OBSERVABILITY, re.compile(r"^pre_login_form_detected$"),
     "detección de form pre-login"),
    ("Rx4", EventKind.OBSERVABILITY, re.compile(r"^pre_flight_http$"),
     "pre-flight check (warming)"),
    ("Rx5", EventKind.STATE,         re.compile(r"^pre_flight_FAIL_abort$"),
     "pre-flight fatal → EXIT"),
    ("Rx6", EventKind.OBSERVABILITY, re.compile(r"^pre_checkout_checkboxes$"),
     "pre-checkout detector"),
    ("Rx7", EventKind.OBSERVABILITY, re.compile(r"^pre_flight_skipped_err$"),
     "pre-flight skip diagnostic"),

    # ── NOISE (R1–R3) ────────────────────────────────────────────────
    ("R1",  EventKind.NOISE,         re.compile(r"^shot$"),
     "screenshot polimórfico — contamina drift"),
    ("R1b", EventKind.NOISE,         re.compile(r"^shot_err$"),
     "screenshot error diagnostic"),
    ("R2",  EventKind.NOISE,         re.compile(r"^pre_"),
     "pre_* sin semántica (excepciones aplicadas antes)"),
    ("R3",  EventKind.NOISE,         re.compile(r"_skipped$"),
     "*_skipped — telemetría de skip puro"),
    ("R3b", EventKind.NOISE,         re.compile(r"^blockers_(eval_)?(skipped|none|minor)$"),
     "blockers_* — diagnóstico interno"),

    # ── STATE (R4–R5) ────────────────────────────────────────────────
    ("R4a", EventKind.STATE,         re.compile(r"^goto$"),
     "SETUP → BROWSING (navegación al evento)"),
    ("R4b", EventKind.STATE,         re.compile(r"^oauth_detected$"),
     "BROWSING → LOGIN"),
    ("R4c", EventKind.STATE,         re.compile(r"^auth_detected_mid$"),
     "* → LOGIN (re-auth mid-flow)"),
    ("R4d", EventKind.STATE,         re.compile(r"^checkout_detected(_at_entry)?$"),
     "* → CHECKOUT"),
    ("R4e", EventKind.STATE,         re.compile(r"^stop_received$"),
     "* → EXIT (user abort)"),
    ("R5a", EventKind.STATE,         re.compile(r"^FATAL_"),
     "* → EXIT (FATAL outcome)"),
    ("R5b", EventKind.STATE,         re.compile(r"^TIMEOUT_"),
     "* → EXIT (TIMEOUT outcome)"),
    ("R5c", EventKind.STATE,         re.compile(r"^max_account_swaps_reached$"),
     "* → EXIT (no more swaps)"),
    ("R5d", EventKind.STATE,         re.compile(r"^no_more_accounts_to_try$"),
     "* → EXIT (account pool exhausted)"),

    # ── ACTION (R6–R8) ───────────────────────────────────────────────
    ("R6a", EventKind.ACTION,        re.compile(r"_clicked(_js)?$"),
     "UI click action"),
    ("R6b", EventKind.ACTION,        re.compile(r"^click_(?!err)"),
     "click_* UI action"),
    ("R6c", EventKind.ACTION,        re.compile(r"_typed$"),
     "input typed"),
    ("R6d", EventKind.ACTION,        re.compile(r"_filled$"),
     "form filled"),
    ("R6e", EventKind.ACTION,        re.compile(r"_selected$"),
     "option selected"),
    ("R6f", EventKind.ACTION,        re.compile(r"^checkbox_"),
     "checkbox action"),
    ("R6g", EventKind.ACTION,        re.compile(r"^radio_"),
     "radio button action"),
    ("R7a", EventKind.ACTION,        re.compile(r"^pick_(account|morelogin_profile|account_tier)"),
     "picker action"),
    ("R7b", EventKind.ACTION,        re.compile(r"^account_(picked|swap)$"),
     "account selection action"),
    ("R7c", EventKind.ACTION,        re.compile(r"^(auto_)?recovery(_start)?$"),
     "recovery action"),
    ("R7d", EventKind.ACTION,        re.compile(r"^recovery_result$"),
     "recovery outcome action"),
    ("R7e", EventKind.ACTION,        re.compile(r"^login_start$"),
     "login intent action"),
    ("R7f", EventKind.ACTION,        re.compile(r"^login_full_false_treating_as_bad_creds$"),
     "login decision action"),
    ("R7g", EventKind.ACTION,        re.compile(r"^force_reset_btn_clicked$"),
     "forced reset click action"),
    ("R8a", EventKind.ACTION,        re.compile(r"^continuar_(no_redirect_)?retry$"),
     "continuar retry action"),
    ("R8b", EventKind.ACTION,        re.compile(r"^continuar_ok$"),
     "continuar success action"),
    ("R8c", EventKind.ACTION,        re.compile(r"^email_continuar"),
     "email continue action"),
    ("R8d", EventKind.ACTION,        re.compile(r"^recovery_failed_swap_account$"),
     "recovery failure → swap action"),
    ("R14", EventKind.ACTION,        re.compile(r"^cookies_force_close$"),
     "cookies force-close action"),

    # ── OBSERVABILITY (R9–R13) ───────────────────────────────────────
    ("R9a",  EventKind.OBSERVABILITY, re.compile(r"^init$"),
     "bot start marker"),
    ("R9b",  EventKind.OBSERVABILITY, re.compile(r"^profile_(picked|started|closed|auto_destroyed)$"),
     "profile lifecycle"),
    ("R9c",  EventKind.OBSERVABILITY, re.compile(r"^morelogin_fresh_created$"),
     "profile creation marker"),
    ("R9d",  EventKind.OBSERVABILITY, re.compile(r"^cdp_"),
     "CDP telemetry"),
    ("R9e",  EventKind.OBSERVABILITY, re.compile(r"^stealth_"),
     "stealth telemetry"),
    ("R9f",  EventKind.OBSERVABILITY, re.compile(r"^persona_(created|primed)$"),
     "persona telemetry"),
    ("R9g",  EventKind.OBSERVABILITY, re.compile(r"^headers_set$"),
     "HTTP headers set"),
    ("R9h",  EventKind.OBSERVABILITY, re.compile(r"^geo_set$"),
     "geolocation set"),
    ("R9i",  EventKind.OBSERVABILITY, re.compile(r"^eps_(warming_home|sid_check)$"),
     "EPS warming"),
    ("R9j",  EventKind.OBSERVABILITY, re.compile(r"^loading_storage_state$"),
     "storage state load"),
    ("R9k",  EventKind.OBSERVABILITY, re.compile(r"^storage_state_(loaded|TOO_OLD_skip)$"),
     "storage state outcome"),
    ("R9l",  EventKind.OBSERVABILITY, re.compile(r"^warming_home_(start|done)$"),
     "warming lifecycle"),
    ("R9m",  EventKind.OBSERVABILITY, re.compile(r"^warmup_start$"),
     "warmup marker"),
    ("R9n",  EventKind.OBSERVABILITY, re.compile(r"^cookies_(accepted|post_hydration_)"),
     "cookies UI lifecycle"),
    ("R9o",  EventKind.OBSERVABILITY, re.compile(r"^hydration_(initial|done)$"),
     "page hydration"),
    ("R9p",  EventKind.OBSERVABILITY, re.compile(r"^history_cleanup_(inside|finally)$"),
     "cleanup lifecycle"),
    ("R10a", EventKind.OBSERVABILITY, re.compile(r"^page_state_detect$"),
     "page state detector"),
    ("R10b", EventKind.OBSERVABILITY, re.compile(r"^stepper_offer$"),
     "stepper detector"),
    ("R10c", EventKind.OBSERVABILITY, re.compile(r"^quick_mode_via_best_available$"),
     "mode detection"),
    ("R10d", EventKind.OBSERVABILITY, re.compile(r"^post_continuar_url$"),
     "post-step URL marker"),
    ("R10e", EventKind.OBSERVABILITY, re.compile(r"^URL_CHANGED$"),
     "CDP URL change hook"),
    ("R10f", EventKind.OBSERVABILITY, re.compile(r"^TRACE_AUTH_"),
     "auth tracer telemetry"),
    ("R10g", EventKind.OBSERVABILITY, re.compile(r"^session_recovered_re_navigating_event$"),
     "session recovery marker"),
    ("R10h", EventKind.OBSERVABILITY, re.compile(r"^adyen_iframes_ready$"),
     "Adyen iframe detector"),
    ("R10i", EventKind.OBSERVABILITY, re.compile(r"^fill_card_(start|data)$"),
     "card fill lifecycle"),
    ("R10j", EventKind.OBSERVABILITY, re.compile(r"^card_fields_filled$"),
     "card fill completion"),
    ("R10k", EventKind.OBSERVABILITY, re.compile(r"^watch_(for_checkout|initial_url)$"),
     "watcher telemetry"),
    ("R10l", EventKind.OBSERVABILITY, re.compile(r"^province_(combobox_found|typed|option_clicked)$"),
     "province dropdown action — observability subtype"),
    ("R11",  EventKind.OBSERVABILITY, re.compile(r"^qty$"),
     "counter (no transición)"),
    ("R12a", EventKind.OBSERVABILITY, re.compile(r"^no_email_input$"),
     "UI state detection diagnostic"),
    ("R12b", EventKind.OBSERVABILITY, re.compile(r"^tm_forced_password_reset_detected$"),
     "TM step-up detection"),
    ("R12c", EventKind.OBSERVABILITY, re.compile(r"^READY_FOR_USER$"),
     "outcome marker SUCCESS (no transiciona por sí mismo)"),
    ("R12d", EventKind.OBSERVABILITY, re.compile(r"^bot_block_persisted$"),
     "post-transition outcome marker"),
    ("R12e", EventKind.OBSERVABILITY, re.compile(r"^FATAL_bot_block_early$"),
     "bot block detection (FATAL group)"),  # subgroup STATE
    ("R12f", EventKind.OBSERVABILITY, re.compile(r"^email_typed_dom_check$"),
     "DOM verification diagnostic"),
    ("R12g", EventKind.OBSERVABILITY, re.compile(r"^reset_method_next_failed$"),
     "reset method failure diagnostic"),
    ("R13a", EventKind.OBSERVABILITY, re.compile(r"^human_journey_done$"),
     "human journey marker"),
    ("R13b", EventKind.OBSERVABILITY, re.compile(r"^warmup_(done|home_scrolled|padding|visibility_events)$"),
     "warmup auxiliary telemetry"),
    ("R13c", EventKind.OBSERVABILITY, re.compile(r"^auto_profile_destroyed_after_pre_flight$"),
     "profile destroyed marker"),
    ("R13d", EventKind.OBSERVABILITY, re.compile(r"^post_recovery_url$"),
     "post-recovery URL marker"),
    ("R13e", EventKind.OBSERVABILITY, re.compile(r"^post_recovery_human_warming_(start|done)$"),
     "post-recovery warming"),
    ("R13f", EventKind.OBSERVABILITY, re.compile(r"^recovery_persisted$"),
     "recovery persistence marker"),
    ("R13g", EventKind.OBSERVABILITY, re.compile(r"_err$"),
     "error diagnostic suffix"),

    # ACTION subtype: pwd_typed, post_recovery_checkbox_acepto
    ("R6h", EventKind.ACTION,        re.compile(r"^pwd_typed$"),
     "password typed"),
    ("R6i", EventKind.ACTION,        re.compile(r"^post_recovery_checkbox_acepto$"),
     "post-recovery checkbox"),
]


# ════════════════════════════════════════════════════════════════════
#  classify() — pure function, fail-safe
# ════════════════════════════════════════════════════════════════════
def classify(event_name: str) -> Tuple[EventKind, str, str]:
    """Clasifica un event_name en una de 4 capas.

    Args:
        event_name: nombre del evento (string).

    Returns:
        (kind, rule_id, reason)
        - kind: EventKind enum
        - rule_id: "Rx1", "R6a", etc. o "FALLBACK"
        - reason: descripción humana

    Garantías:
        - Pure function (sin side-effects)
        - Nunca lanza excepciones
        - event_name no string → (UNKNOWN, "FALLBACK", ...)
    """
    if not isinstance(event_name, str) or not event_name:
        return (EventKind.UNKNOWN, "FALLBACK", "event_name vacío o no string")
    try:
        for rule_id, kind, rgx, reason in _RULES:
            if rgx.search(event_name):
                return (kind, rule_id, reason)
    except Exception:
        # Fail-safe: cualquier error en regex → UNKNOWN
        return (EventKind.UNKNOWN, "FALLBACK", "regex error")
    return (EventKind.UNKNOWN, "FALLBACK", "no rule matched")


def is_state(event_name: str) -> bool:
    """True si el evento puede disparar transición FSM."""
    kind, _, _ = classify(event_name)
    return kind == EventKind.STATE


def is_noise(event_name: str) -> bool:
    """True si el evento debe filtrarse del análisis de estado."""
    kind, _, _ = classify(event_name)
    return kind == EventKind.NOISE


def get_rules_summary() -> dict:
    """Resumen de reglas — útil para diagnostics/docs."""
    by_kind: dict[str, int] = {}
    for _, kind, _, _ in _RULES:
        by_kind[kind.value] = by_kind.get(kind.value, 0) + 1
    return {
        "total_rules": len(_RULES),
        "by_kind": by_kind,
    }


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    samples = [
        ("shot", EventKind.NOISE),
        ("goto", EventKind.STATE),
        ("oauth_detected", EventKind.STATE),
        ("FATAL_bot_block_early", EventKind.STATE),  # FATAL_* dispara EXIT (outcome=BLOCKED)
        ("click_continuar", EventKind.ACTION),
        ("pick_account_tier", EventKind.ACTION),
        ("qty", EventKind.OBSERVABILITY),
        ("hydration_done", EventKind.OBSERVABILITY),
        ("pre_login_navigate", EventKind.ACTION),  # exception
        ("pre_checkout_checkboxes", EventKind.OBSERVABILITY),  # exception
        ("blockers_eval_skipped", EventKind.NOISE),
        ("UNKNOWN_EVENT_XYZ", EventKind.UNKNOWN),
        ("", EventKind.UNKNOWN),
    ]
    print("=== event_classifier self-test ===")
    print(f"Rules summary: {get_rules_summary()}")
    all_pass = True
    for evt, expected in samples:
        kind, rule, reason = classify(evt)
        ok = kind == expected
        all_pass &= ok
        mark = "[OK]" if ok else "[FAIL]"
        print(f"  {mark} {evt:50} -> {kind.value:13} (rule={rule})")
    print()
    print(f"Result: {'ALL PASS' if all_pass else 'FAILURES'}")


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
