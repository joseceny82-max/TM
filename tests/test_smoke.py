"""tests/test_smoke.py — Smoke test base de la arquitectura.

FASE 2: test trivial que verifica que la estructura nueva es importable
sin depender del legacy. Si este test falla, hay algo roto a nivel de
módulos/packages — NO indica problemas del bot real.

Política: este test debe pasar ANTES de cada commit de migración.
"""


def test_placeholder():
    """Sanity check trivial. Siempre pasa."""
    assert True


def test_core_packages_importable():
    """Los packages core/ están bien formados (no rompen el sys.path).

    core/ui y core/utils fueron eliminados como zombies tras audit SRE
    (cero importadores, placeholder __init__ vacíos)."""
    import core  # noqa: F401
    import core.observability  # noqa: F401
    assert True


def test_bot_package_importable():
    """El package bot/ está bien formado."""
    import bot  # noqa: F401
    assert True


def test_purchase_flow_not_implemented():
    """El placeholder `bot.purchase_flow.run_purchase_flow` debe lanzar
    NotImplementedError. Esto es intencional: garantiza que nadie use el
    path nuevo accidentalmente antes de que esté migrado."""
    from bot.purchase_flow import run_purchase_flow
    import pytest
    with pytest.raises(NotImplementedError):
        run_purchase_flow()


def test_tracer_context_manager():
    """El placeholder `TraceSession` es un context manager NO-OP."""
    from core.observability.tracer import TraceSession
    with TraceSession() as t:
        assert t is not None
    # __exit__ no debe lanzar
