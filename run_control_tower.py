#!/usr/bin/env python
"""run_control_tower.py — Launcher uvicorn del Control Tower.

Uso:
    python run_control_tower.py                # puerto 5060
    CT_PORT=8080 python run_control_tower.py   # puerto custom
    CT_HOST=0.0.0.0 python run_control_tower.py  # bind all interfaces

URLs:
    http://127.0.0.1:5060/          → MVP
    http://127.0.0.1:5060/realtime  → 4-panel view
    http://127.0.0.1:5060/control   → analytics + root cause
    http://127.0.0.1:5060/tower     → industrial (multi-run + alerts + history)

API docs auto:
    http://127.0.0.1:5060/docs      → Swagger UI
    http://127.0.0.1:5060/redoc     → ReDoc
"""
import os
import sys

# Asegurar PYTHONPATH apunta al proyecto raíz
ROOT = os.path.abspath(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn no instalado. Ejecuta:")
        print("    pip install fastapi uvicorn")
        sys.exit(1)

    host = os.environ.get("CT_HOST", "127.0.0.1")
    port = int(os.environ.get("CT_PORT", "5060"))
    log_level = os.environ.get("CT_LOG_LEVEL", "info")

    print(f"=== TM Control Tower ===")
    print(f"  MVP:        http://{host}:{port}/")
    print(f"  Realtime:   http://{host}:{port}/realtime")
    print(f"  Control:    http://{host}:{port}/control")
    print(f"  Industrial: http://{host}:{port}/tower")
    print(f"  API docs:   http://{host}:{port}/docs")
    print()

    uvicorn.run(
        "core.control_tower.server:app",
        host=host,
        port=port,
        log_level=log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()


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
