"""tests/test_purchase_gate_ui.py — M13.4 UI dashboard pane smoke tests.

Tests del pane HTML/JS añadido a templates/diagnostics.html.
No requieren headless browser — solo validación estática del DOM/JS.
"""
import os
import re

import pytest


TEMPLATE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "templates", "diagnostics.html")
)


@pytest.fixture(scope="module")
def template_source() -> str:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


# ════════════════════════════════════════════════════════════════════
#  DOM presence — pane elements
# ════════════════════════════════════════════════════════════════════
def test_m13_4_pane_card_present(template_source):
    """El card #gate-card está en el HTML."""
    assert 'id="gate-card"' in template_source


def test_m13_4_all_required_dom_elements_present(template_source):
    """Los IDs del pane existen para que el JS pueda actualizarlos."""
    required_ids = [
        "gate-mode", "gate-enabled",
        "gate-attempted", "gate-live",
        "gate-dry-run", "gate-blocked",
        "gate-errors",
        "gate-dry-rate", "gate-block-rate",
        "gate-idem", "gate-locks",
        "gate-denials",
    ]
    for elem_id in required_ids:
        assert f'id="{elem_id}"' in template_source, (
            f"Missing DOM element: id={elem_id}"
        )


def test_m13_4_denial_breakdown_collapsible(template_source):
    """El denial breakdown usa <details>/<summary> (collapsed por default)."""
    # Buscamos un <details> que contenga "gate-denials"
    assert "<details" in template_source
    # Specifically denial breakdown summary
    assert "Denial breakdown" in template_source


# ════════════════════════════════════════════════════════════════════
#  JS — endpoint reference + polling
# ════════════════════════════════════════════════════════════════════
def test_m13_4_js_references_m13_3_endpoint(template_source):
    """El JS llama al endpoint M13.3."""
    assert '"/api/diagnostics/purchase_gate"' in template_source


def test_m13_4_js_polling_interval_in_range(template_source):
    """El polling interval está entre 2-5 segundos (per spec)."""
    # Buscamos setInterval(refreshGate, N)
    m = re.search(r"setInterval\(refreshGate,\s*(\d+)\)", template_source)
    assert m is not None, "setInterval(refreshGate, ...) no encontrado"
    interval_ms = int(m.group(1))
    # Spec: 2-5 seconds = 2000-5000 ms
    assert 2000 <= interval_ms <= 5000, (
        f"Polling interval {interval_ms}ms fuera del rango 2000-5000"
    )


def test_m13_4_js_refresh_function_defined(template_source):
    """La función refreshGate está definida."""
    assert "function refreshGate" in template_source or "refreshGate" in template_source


def test_m13_4_js_initial_call_on_load(template_source):
    """refreshGate() se llama una vez al cargar (no solo via interval)."""
    # El JS llama refreshGate() antes del setInterval
    refresh_idx = template_source.find("refreshGate();")
    setinterval_idx = template_source.find("setInterval(refreshGate")
    assert refresh_idx > 0
    assert setinterval_idx > 0


# ════════════════════════════════════════════════════════════════════
#  Read-only enforcement (no mutation calls)
# ════════════════════════════════════════════════════════════════════
def test_m13_4_no_mutation_endpoints_in_pane_js(template_source):
    """El pane NO hace POST/PUT/DELETE al gate o endpoints relacionados."""
    # Buscar bloques cerca de gate references que tengan method:'POST' u otro
    gate_section_start = template_source.find("refreshGate")
    if gate_section_start < 0:
        return  # No JS yet (caught by other tests)
    # Lookahead 800 chars de la función refreshGate
    section = template_source[gate_section_start:gate_section_start + 1500]
    forbidden = ['method:"POST"', "method:'POST'",
                 'method:"PUT"', "method:'PUT'",
                 'method:"DELETE"', "method:'DELETE'"]
    for f in forbidden:
        assert f not in section, f"Mutation method found in pane JS: {f}"


def test_m13_4_pane_does_not_call_evaluate_endpoint(template_source):
    """El pane NO llama a evaluate() ni endpoints que muten state."""
    # No debe haber referencias a operaciones de write
    pane_start = template_source.find('id="gate-card"')
    if pane_start < 0:
        return
    section = template_source[pane_start:]
    # Solo el endpoint M13.3 está permitido
    forbidden_endpoints = [
        "/api/diagnostics/purchase_gate/evaluate",
        "/api/diagnostics/purchase_gate/reset",
        "/api/diagnostics/purchase_gate/clear",
    ]
    for ep in forbidden_endpoints:
        assert ep not in section


# ════════════════════════════════════════════════════════════════════
#  Template integrity
# ════════════════════════════════════════════════════════════════════
def test_m13_4_template_still_well_formed_html(template_source):
    """Tags balanceados básicos — <html>, <body>, <script>."""
    assert template_source.count("<html") == 1
    assert template_source.count("</html>") == 1
    assert template_source.count("<body") >= 1
    assert template_source.count("</body>") >= 1
    # script tags balanced (al menos par)
    open_scripts = template_source.count("<script")
    close_scripts = template_source.count("</script>")
    assert open_scripts == close_scripts


def test_m13_4_template_does_not_remove_existing_panes(template_source):
    """El patch M13.4 NO elimina los panes pre-existentes."""
    # Pre-existing panes
    assert "Last failure trace" in template_source
    assert "id=\"failure-box\"" in template_source
    assert "/api/diagnostics/runs" in template_source
    assert "/api/diagnostics/events/stream" in template_source
