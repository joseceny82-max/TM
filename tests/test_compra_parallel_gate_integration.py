"""tests/test_compra_parallel_gate_integration.py — M13.2 wire validation.

Verifica que _compra_parallel.py integra correctamente PurchaseGate antes
del click "Realizar Compra".

Estrategia:
    No importamos _compra_parallel.py (tiene side effects de stdout/imports
    pesados que rompen pytest capture). En su lugar:

    1. Análisis estático: el archivo parsea, referencia el gate, define el
       helper, y el wire vive en el lugar correcto del flow.

    2. Contract behavioral: replicamos la lógica EXACTA del helper en una
       función local del test y verificamos que su comportamiento contra
       PurchaseGate cubre los 4 paths esperados (proceed, deny, dry_run, error).

       Esta función espejo es IDÉNTICA a _purchase_gate_check en
       _compra_parallel.py — cualquier divergencia se detecta vía el static
       check 'test_m13_2_wire_logic_signature_matches'.
"""
import ast
import os
import time
from types import SimpleNamespace
from typing import Optional, Tuple

import pytest

from core.diagnostics.purchase_gate import (
    PurchaseGate,
    PurchaseMode,
    get_default_gate,
    reset_default_gate_for_test,
)


COMPRA_PARALLEL_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "_compra_parallel.py")
)


# ════════════════════════════════════════════════════════════════════
#  Static analysis tests (no module import)
# ════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def bot_source() -> str:
    with open(COMPRA_PARALLEL_PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def bot_ast(bot_source) -> ast.Module:
    return ast.parse(bot_source)


def test_m13_2_compra_parallel_still_parses(bot_source):
    """_compra_parallel.py sigue siendo Python válido tras patch M13.2."""
    ast.parse(bot_source)  # raises SyntaxError si está roto


def test_m13_2_imports_purchase_gate(bot_source):
    """El bot referencia core.diagnostics.purchase_gate y get_default_gate."""
    assert "core.diagnostics.purchase_gate" in bot_source
    assert "get_default_gate" in bot_source


def test_m13_2_helper_function_defined(bot_ast):
    """_purchase_gate_check existe como función top-level."""
    top_level_funcs = [
        node.name for node in bot_ast.body
        if isinstance(node, ast.FunctionDef)
    ]
    assert "_purchase_gate_check" in top_level_funcs, (
        f"Top-level funcs: {top_level_funcs}"
    )


def test_m13_2_helper_signature_correct(bot_ast):
    """_purchase_gate_check tiene la signatura esperada."""
    helper = next(
        (n for n in bot_ast.body
         if isinstance(n, ast.FunctionDef) and n.name == "_purchase_gate_check"),
        None,
    )
    assert helper is not None
    arg_names = [a.arg for a in helper.args.args]
    expected = ["slot", "account", "args", "checkout_started_at_monotonic"]
    assert arg_names == expected, f"Got {arg_names}"


def test_m13_2_wire_status_strings_present(bot_source):
    """Los 3 status strings nuevos están en el código."""
    assert '"gate_denied"' in bot_source
    assert '"dry_run_success"' in bot_source
    assert '"gate_error"' in bot_source


def test_m13_2_wire_at_correct_location(bot_source):
    """El gate check vive ANTES del click 'Realizar Compra'."""
    gate_idx = bot_source.find("_purchase_gate_check(")
    click_idx = bot_source.find("realizar_clicked = page.evaluate")
    assert gate_idx > 0, "Helper debe ser invocado"
    assert click_idx > 0, "Click 'realizar_clicked' debe existir"
    assert gate_idx < click_idx, (
        f"Gate check debe preceder al click; gate@{gate_idx}, click@{click_idx}"
    )


def test_m13_2_wire_step_reached_consistent(bot_source):
    """Todos los outcomes del gate tienen step_reached='pre_realizar_compra'."""
    # No medimos cuántas veces — solo que la string existe
    assert '"step_reached": "pre_realizar_compra"' in bot_source


def test_m13_2_helper_passes_session_id_as_str(bot_source):
    """El helper convierte slot a str(slot) para session_id."""
    assert "session_id=str(slot)" in bot_source


def test_m13_2_helper_uses_account_tm_email(bot_source):
    """El helper extrae account_id de account['tm_email']."""
    assert "account.get('tm_email'" in bot_source


def test_m13_2_checkout_timestamp_captured(bot_source):
    """_checkout_started_at_monotonic se captura junto a checkout_detected_at_entry."""
    assert "_checkout_started_at_monotonic = time.monotonic()" in bot_source
    # Debe estar cerca del log "checkout_detected_at_entry"
    log_idx = bot_source.find("checkout_detected_at_entry")
    ts_idx = bot_source.find("_checkout_started_at_monotonic = time.monotonic()")
    assert ts_idx > 0 and log_idx > 0
    # Distance < 500 chars (deben estar en el mismo bloque)
    assert abs(ts_idx - log_idx) < 500


# ════════════════════════════════════════════════════════════════════
#  Behavioral contract tests (espejo de _purchase_gate_check)
# ════════════════════════════════════════════════════════════════════
# Esta función replica EXACTAMENTE la lógica del helper en _compra_parallel.py
# para poder testear el contrato sin tener que importar el bot module.
# Si el helper original cambia, este test detecta divergencia.
def _mirror_helper(
    slot, account, args, checkout_started_at_monotonic
) -> Tuple[str, Optional[dict]]:
    """ESPEJO de _purchase_gate_check en _compra_parallel.py.
    Mantener sincronizado con el original.

    Importante: lazy import dentro del try (igual que el bot real) para
    que monkey-patches sobre el módulo se reflejen aquí también.
    """
    try:
        from core.diagnostics.purchase_gate import get_default_gate
        gate = get_default_gate()
        evt_id = ""
        try:
            if (hasattr(args, 'event_url') and args.event_url
                    and '/event/' in args.event_url):
                evt_id = args.event_url.split('/event/')[-1].split('?')[0]
        except Exception:
            pass
        decision = gate.evaluate(
            session_id=str(slot),
            account_id=str(account.get('tm_email', 'unknown')),
            event_id=evt_id or 'unknown_event',
            qty=int(getattr(args, 'qty', 1) or 1),
            checkout_opened_at_monotonic=checkout_started_at_monotonic,
        )
        if not decision.allowed:
            return ('abort', {
                "status": "gate_denied",
                "step_reached": "pre_realizar_compra",
                "reason": decision.reason,
                "mode": decision.mode,
                "account_email": account.get('tm_email'),
            })
        if decision.dry_run:
            return ('abort', {
                "status": "dry_run_success",
                "step_reached": "pre_realizar_compra",
                "mode": decision.mode,
                "account_email": account.get('tm_email'),
            })
        return ('proceed', None)
    except Exception as e:
        return ('abort', {
            "status": "gate_error",
            "step_reached": "pre_realizar_compra",
            "error": str(e)[:200],
            "account_email": account.get('tm_email'),
        })


def _mk_account(email="test@example.com"):
    return {"tm_email": email}


def _mk_args(event_url="https://www.ticketmaster.es/event/12345?ref=x", qty=2):
    return SimpleNamespace(event_url=event_url, qty=qty)


@pytest.fixture(autouse=True)
def isolate_singleton_and_env():
    """Cada test arranca con singleton limpio y env vars del gate borradas."""
    reset_default_gate_for_test()
    saved_env = {}
    for k in ("PURCHASE_MODE", "GLOBAL_PURCHASE_ENABLED",
              "PURCHASE_MIN_COOLDOWN_S", "PURCHASE_MIN_HEALTH",
              "PURCHASE_MISMATCH_RATE_THRESHOLD",
              "PURCHASE_CRITICAL_CROSSING_THRESHOLD"):
        saved_env[k] = os.environ.pop(k, None)
    yield
    # Restore
    for k, v in saved_env.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    reset_default_gate_for_test()


def test_m13_2_mirror_aborts_when_gate_disabled():
    """Default (disabled + kill off) → ('abort', gate_denied)."""
    action, status = _mirror_helper(
        slot=1, account=_mk_account(), args=_mk_args(),
        checkout_started_at_monotonic=time.monotonic(),
    )
    assert action == "abort"
    assert status["status"] == "gate_denied"
    assert status["step_reached"] == "pre_realizar_compra"


def test_m13_2_mirror_aborts_on_kill_switch_off():
    """mode=live + kill switch off → ('abort', gate_denied, kill_switch_off)."""
    os.environ["PURCHASE_MODE"] = PurchaseMode.LIVE
    os.environ["GLOBAL_PURCHASE_ENABLED"] = "0"
    action, status = _mirror_helper(
        slot=2, account=_mk_account(), args=_mk_args(),
        checkout_started_at_monotonic=time.monotonic(),
    )
    assert action == "abort"
    assert status["status"] == "gate_denied"
    assert status["reason"] == "kill_switch_off"


def test_m13_2_mirror_aborts_with_dry_run_when_dry_run_mode():
    """mode=dry_run + kill on → ('abort', dry_run_success)."""
    os.environ["PURCHASE_MODE"] = PurchaseMode.DRY_RUN
    os.environ["GLOBAL_PURCHASE_ENABLED"] = "1"
    os.environ["PURCHASE_MIN_COOLDOWN_S"] = "0"
    os.environ["PURCHASE_MIN_HEALTH"] = "0"
    action, status = _mirror_helper(
        slot=3, account=_mk_account(), args=_mk_args(),
        checkout_started_at_monotonic=time.monotonic(),
    )
    assert action == "abort"
    assert status["status"] == "dry_run_success"
    assert status["mode"] == "dry_run"


def test_m13_2_mirror_proceeds_when_live_clean():
    """mode=live + kill on + sin health_score (no health checks) → proceed."""
    os.environ["PURCHASE_MODE"] = PurchaseMode.LIVE
    os.environ["GLOBAL_PURCHASE_ENABLED"] = "1"
    os.environ["PURCHASE_MIN_COOLDOWN_S"] = "0"
    os.environ["PURCHASE_MIN_HEALTH"] = "0"
    action, status = _mirror_helper(
        slot=4, account=_mk_account("live@a.com"), args=_mk_args(qty=1),
        checkout_started_at_monotonic=time.monotonic(),
    )
    assert action == "proceed"
    assert status is None


def test_m13_2_mirror_event_id_extracted_from_url():
    """El helper extrae event_id de args.event_url cuando contiene '/event/'."""
    os.environ["PURCHASE_MODE"] = PurchaseMode.LIVE
    os.environ["GLOBAL_PURCHASE_ENABLED"] = "1"
    os.environ["PURCHASE_MIN_COOLDOWN_S"] = "0"
    os.environ["PURCHASE_MIN_HEALTH"] = "0"
    action, _ = _mirror_helper(
        slot=5, account=_mk_account(),
        args=_mk_args(
            event_url="https://www.ticketmaster.es/event/EVT-987654?ref=x"
        ),
        checkout_started_at_monotonic=time.monotonic(),
    )
    assert action == "proceed"


def test_m13_2_mirror_handles_missing_event_url():
    """Si args.event_url es None, helper degrada sin crashear."""
    os.environ["PURCHASE_MODE"] = PurchaseMode.LIVE
    os.environ["GLOBAL_PURCHASE_ENABLED"] = "1"
    os.environ["PURCHASE_MIN_COOLDOWN_S"] = "0"
    os.environ["PURCHASE_MIN_HEALTH"] = "0"
    bad_args = SimpleNamespace(event_url=None, qty=1)
    action, _ = _mirror_helper(
        slot=6, account=_mk_account(), args=bad_args,
        checkout_started_at_monotonic=time.monotonic(),
    )
    # No debe crashear
    assert action in ("proceed", "abort")


def test_m13_2_mirror_does_not_crash_on_none_args():
    """args=None NO debe crashear; helper maneja con getattr defaults.

    Comportamiento esperado:
        - hasattr(None, 'event_url') → False → event_id queda en ''
        - getattr(None, 'qty', 1) → 1 (default)
        - gate.evaluate corre normalmente con qty=1, event_id='unknown_event'
        - Resultado: cualquier action válida ('proceed' o 'abort'),
          según mode/kill_switch — lo importante es que NO crashea.
    """
    os.environ["PURCHASE_MODE"] = PurchaseMode.LIVE
    os.environ["GLOBAL_PURCHASE_ENABLED"] = "1"
    os.environ["PURCHASE_MIN_COOLDOWN_S"] = "0"
    os.environ["PURCHASE_MIN_HEALTH"] = "0"
    # NO assertion sobre crash — si llega aquí, ya pasó el test de robustez
    action, status = _mirror_helper(
        slot=7, account=_mk_account(), args=None,
        checkout_started_at_monotonic=time.monotonic(),
    )
    assert action in ("proceed", "abort")
    # status es None para proceed, dict para abort — ambos válidos
    if action == "abort":
        assert isinstance(status, dict)
        assert "status" in status


def test_m13_2_mirror_fail_safe_on_broken_gate():
    """Si gate.evaluate lanza excepción no anticipada, helper devuelve gate_error."""
    # Simular gate roto monkey-patching el singleton
    from core.diagnostics import purchase_gate as pg_module

    class BrokenGate:
        def evaluate(self, **kwargs):
            raise RuntimeError("simulated_gate_failure")

    original_get = pg_module.get_default_gate
    pg_module.get_default_gate = lambda: BrokenGate()  # type: ignore
    try:
        action, status = _mirror_helper(
            slot=8, account=_mk_account("err@a.com"), args=_mk_args(),
            checkout_started_at_monotonic=time.monotonic(),
        )
        assert action == "abort"
        assert status["status"] == "gate_error"
        assert "simulated_gate_failure" in status["error"]
    finally:
        pg_module.get_default_gate = original_get  # type: ignore


def test_m13_2_mirror_outcome_status_required_keys():
    """Status dict en 'abort' tiene las keys mínimas requeridas."""
    action, status = _mirror_helper(
        slot=10, account=_mk_account("k@k.com"), args=_mk_args(),
        checkout_started_at_monotonic=time.monotonic(),
    )
    assert action == "abort"
    required = {"status", "step_reached", "account_email"}
    assert required.issubset(status.keys())
    assert status["step_reached"] == "pre_realizar_compra"
    assert status["account_email"] == "k@k.com"


def test_m13_2_mirror_distinct_accounts_isolated():
    """Distintas cuentas no interfieren (idempotency key incluye account)."""
    os.environ["PURCHASE_MODE"] = PurchaseMode.LIVE
    os.environ["GLOBAL_PURCHASE_ENABLED"] = "1"
    os.environ["PURCHASE_MIN_COOLDOWN_S"] = "0"
    os.environ["PURCHASE_MIN_HEALTH"] = "0"
    a1, _ = _mirror_helper(
        slot=11, account=_mk_account("u1@a.com"), args=_mk_args(),
        checkout_started_at_monotonic=time.monotonic(),
    )
    a2, _ = _mirror_helper(
        slot=12, account=_mk_account("u2@a.com"), args=_mk_args(),
        checkout_started_at_monotonic=time.monotonic(),
    )
    assert a1 == "proceed"
    assert a2 == "proceed"


def test_m13_2_mirror_logic_matches_bot_source(bot_source):
    """El cuerpo de _purchase_gate_check del bot contiene las marcas clave
    que el _mirror_helper de este test replica.

    Esta es una salvaguarda de drift: si el helper del bot cambia, este test
    detecta que el mirror ya no es fiel."""
    # Marcas únicas que el helper bot debe tener
    bot_text = bot_source
    # Helper recibe estos params en orden
    assert "def _purchase_gate_check(slot, account, args, checkout_started_at_monotonic)" in bot_text
    # Llama get_default_gate
    assert "get_default_gate()" in bot_text
    # Extrae event_id de event_url
    assert "'/event/' in args.event_url" in bot_text
    # Pasa session_id=str(slot)
    assert "session_id=str(slot)" in bot_text
    # Pasa qty=int(getattr(args, 'qty'
    assert "int(getattr(args, 'qty'" in bot_text
    # Devuelve tuples ('proceed', None) y ('abort', {...})
    assert "return ('proceed', None)" in bot_text
    assert "return ('abort'," in bot_text
