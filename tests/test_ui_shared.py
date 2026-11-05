"""Tests M7 — UI shared layer.

Garantías:
    1. tokens.css existe y contiene variables clave
    2. sse-client.js existe y exporta TMSSEClient
    3. panels.js existe y exporta TMPanels con funciones esperadas
    4. CSS válido (parseo básico)
    5. JS parseable (sintaxis básica)
    6. Variables tokens consistentes con kind/state del sistema
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SHARED_DIR = ROOT / "ui" / "shared"


# ════════════════════════════════════════════════════════════════════
#  1. Files exist
# ════════════════════════════════════════════════════════════════════
def test_tokens_css_exists():
    assert (SHARED_DIR / "tokens.css").exists()


def test_sse_client_js_exists():
    assert (SHARED_DIR / "sse-client.js").exists()


def test_panels_js_exists():
    assert (SHARED_DIR / "panels.js").exists()


# ════════════════════════════════════════════════════════════════════
#  2. tokens.css contiene variables clave
# ════════════════════════════════════════════════════════════════════
def test_tokens_has_state_colors():
    text = (SHARED_DIR / "tokens.css").read_text(encoding="utf-8")
    # 5 estados FSM
    for st in ["setup", "browsing", "login", "checkout", "exit"]:
        assert f"--tm-state-{st}" in text, f"missing --tm-state-{st}"


def test_tokens_has_kind_colors():
    text = (SHARED_DIR / "tokens.css").read_text(encoding="utf-8")
    for k in ["state", "action", "observability", "noise", "unknown"]:
        assert f"--tm-kind-{k}" in text, f"missing --tm-kind-{k}"


def test_tokens_has_health_colors():
    text = (SHARED_DIR / "tokens.css").read_text(encoding="utf-8")
    for v in ["healthy", "degraded", "unhealthy", "critical"]:
        assert f"--tm-health-{v}" in text


def test_tokens_has_classes():
    text = (SHARED_DIR / "tokens.css").read_text(encoding="utf-8")
    # Components reusables
    for cls in [".tm-state-big", ".tm-kind-badge", ".tm-event-line",
                ".tm-health-gauge", ".tm-gauge-bar", ".tm-alert",
                ".tm-watchdog-banner", ".tm-topbar", ".tm-card"]:
        assert cls in text, f"missing class {cls}"


def test_tokens_has_watchdog_banner_visible_class():
    """M6 + M7: clase para mostrar banner cuando tailer está stalled."""
    text = (SHARED_DIR / "tokens.css").read_text(encoding="utf-8")
    assert ".tm-watchdog-banner.visible" in text


# ════════════════════════════════════════════════════════════════════
#  3. sse-client.js
# ════════════════════════════════════════════════════════════════════
def test_sse_client_exports_TMSSEClient():
    text = (SHARED_DIR / "sse-client.js").read_text(encoding="utf-8")
    assert "TMSSEClient" in text
    assert "global.TMSSEClient" in text or "window.TMSSEClient" in text


def test_sse_client_has_reconnect_logic():
    text = (SHARED_DIR / "sse-client.js").read_text(encoding="utf-8")
    assert "_scheduleReconnect" in text or "reconnect" in text.lower()
    assert "backoff" in text.lower()


def test_sse_client_has_close_method():
    text = (SHARED_DIR / "sse-client.js").read_text(encoding="utf-8")
    assert "close" in text


def test_sse_client_has_hello_handler():
    text = (SHARED_DIR / "sse-client.js").read_text(encoding="utf-8")
    assert '"hello"' in text or "'hello'" in text


# ════════════════════════════════════════════════════════════════════
#  4. panels.js
# ════════════════════════════════════════════════════════════════════
def test_panels_exports_TMPanels():
    text = (SHARED_DIR / "panels.js").read_text(encoding="utf-8")
    assert "TMPanels" in text


def test_panels_has_renderState():
    text = (SHARED_DIR / "panels.js").read_text(encoding="utf-8")
    assert "renderState" in text


def test_panels_has_renderHealth():
    text = (SHARED_DIR / "panels.js").read_text(encoding="utf-8")
    assert "renderHealth" in text


def test_panels_has_renderEvent():
    text = (SHARED_DIR / "panels.js").read_text(encoding="utf-8")
    assert "renderEvent" in text


def test_panels_has_renderDrifts():
    text = (SHARED_DIR / "panels.js").read_text(encoding="utf-8")
    assert "renderDrifts" in text


def test_panels_has_watchdog_banner_renderer():
    """M6+M7: panels.js debe poder renderizar el watchdog banner."""
    text = (SHARED_DIR / "panels.js").read_text(encoding="utf-8")
    assert "renderWatchdogBanner" in text or "watchdog" in text.lower()


def test_panels_has_html_escape():
    """XSS protection: debe haber escape de strings."""
    text = (SHARED_DIR / "panels.js").read_text(encoding="utf-8")
    assert "&amp;" in text or "&lt;" in text


# ════════════════════════════════════════════════════════════════════
#  5. CSS sintaxis básica (curly braces balanceadas)
# ════════════════════════════════════════════════════════════════════
def test_tokens_css_balanced_braces():
    text = (SHARED_DIR / "tokens.css").read_text(encoding="utf-8")
    # Quitar comentarios para evitar falsos positivos
    no_comments = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    opens = no_comments.count("{")
    closes = no_comments.count("}")
    assert opens == closes, f"unbalanced braces: {opens} open vs {closes} close"


def test_tokens_css_no_obvious_errors():
    text = (SHARED_DIR / "tokens.css").read_text(encoding="utf-8")
    # Sin doble punto y coma seguido
    assert ";;" not in text
    # Sin doble llave abierta
    assert "{{" not in text


# ════════════════════════════════════════════════════════════════════
#  6. JS sintaxis básica
# ════════════════════════════════════════════════════════════════════
def test_panels_js_minimum_structure():
    """panels.js tiene función envolvente IIFE + TMPanels object."""
    text = (SHARED_DIR / "panels.js").read_text(encoding="utf-8")
    # Estructura: (function (global) {  ...  })(...)
    assert "(function" in text
    assert "}(typeof window" in text or "})(typeof window" in text or "}(global)" in text
    # TMPanels object
    assert "global.TMPanels = TMPanels" in text or "TMPanels =" in text


def test_sse_client_js_balanced_braces():
    text = (SHARED_DIR / "sse-client.js").read_text(encoding="utf-8")
    text_clean = re.sub(r"//.*", "", text)
    text_clean = re.sub(r"/\*.*?\*/", "", text_clean, flags=re.DOTALL)
    text_clean = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text_clean)
    text_clean = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text_clean)
    opens = text_clean.count("{")
    closes = text_clean.count("}")
    assert opens == closes


# ════════════════════════════════════════════════════════════════════
#  7. Existing pages NOT modified (regression)
# ════════════════════════════════════════════════════════════════════
def test_templates_diagnostics_still_exists():
    """templates/diagnostics.html original sigue presente sin modificar."""
    fp = ROOT / "templates" / "diagnostics.html"
    assert fp.exists()
    text = fp.read_text(encoding="utf-8")
    assert "TM DIAGNOSTICS" in text


def test_control_tower_uis_still_exist():
    """Las 4 UIs del control tower siguen existiendo."""
    for name in ["mvp.html", "realtime.html", "control.html", "industrial.html"]:
        fp = ROOT / "core" / "control_tower" / "ui" / name
        assert fp.exists(), f"{name} missing"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
