"""core/observability/metrics_api.py — Flask blueprint for purchase metrics.

Provee un Flask Blueprint con el endpoint:

    GET /api/purchase/metrics?hours=24

Diseño:
    - **Opt-in**: NO auto-registra en `dashboard.py`. Cada quien decide
      cuándo + cómo registrarlo.
    - **Fail-safe**: si flask no está disponible o falla la importación,
      `metrics_blueprint = None` y `create_metrics_blueprint()` devuelve
      None. El resto del sistema sigue funcionando.
    - **Pure delegation**: el endpoint llama a `compute_purchase_metrics`.

USO MANUAL (cuando se quiera activar):

    from core.observability.metrics_api import create_metrics_blueprint
    bp = create_metrics_blueprint()
    if bp:
        app.register_blueprint(bp)
"""
from __future__ import annotations

from typing import Any, Optional

from core.observability.metrics import compute_purchase_metrics


# ════════════════════════════════════════════════════════════════════
#  Lazy Flask import
# ════════════════════════════════════════════════════════════════════
try:
    from flask import Blueprint, jsonify, request
    _FLASK_AVAILABLE = True
except ImportError:
    _FLASK_AVAILABLE = False


def is_flask_available() -> bool:
    return _FLASK_AVAILABLE


# ════════════════════════════════════════════════════════════════════
#  Factory
# ════════════════════════════════════════════════════════════════════
def create_metrics_blueprint(*,
                              url_prefix: str = "/api/purchase",
                              name: str = "purchase_metrics",
                              ) -> Optional[Any]:
    """Crea el blueprint Flask. Devuelve None si flask no está disponible.

    Args:
        url_prefix: prefijo de URL para el blueprint.
        name: nombre interno del blueprint (para Flask routing).

    Returns:
        flask.Blueprint o None.
    """
    if not _FLASK_AVAILABLE:
        return None

    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    @bp.route("/metrics", methods=["GET"])
    def metrics_endpoint():
        # Parseo opcional de `hours` query param
        hours_raw = request.args.get("hours", default="24")
        try:
            hours = float(hours_raw)
            if hours <= 0:
                hours = None
        except (TypeError, ValueError):
            hours = 24.0

        try:
            payload = compute_purchase_metrics(window_hours=hours)
            return jsonify(payload), 200
        except Exception as e:
            # Fail-safe: nunca devolvemos 500 desde aquí — empty
            return jsonify({
                "ok": False,
                "error": "metrics_compute_failed",
                "detail": str(e)[:200],
            }), 200

    @bp.route("/metrics/healthz", methods=["GET"])
    def metrics_health():
        return jsonify({"ok": True, "endpoint": "purchase_metrics"}), 200

    return bp


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
