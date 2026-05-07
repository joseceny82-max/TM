"""api_versioning — alias /api/v1/<path> paralelo a /api/<path> + endpoint OpenAPI mínimo.

Para no romper compatibilidad: ambos paths funcionan idénticos. Los nuevos
clientes pueden usar `/api/v1/`. Cuando se rompan endpoints en futuras versiones,
se versionarán a `/api/v2/`.

Endpoint /api/openapi.json devuelve un esquema OpenAPI 3.0 mínimo con todas las
rutas registradas en Flask (auto-discovery sin contratos pesados).
"""
from __future__ import annotations

import re
from typing import Any, Dict


def register_v1_aliases(app):
    """Registra alias /api/v1/<path> que apuntan al mismo handler que /api/<path>.

    Solo afecta endpoints que empiezan con /api/ y que no tienen prefijo v1 ya.
    No genera duplicados en url_map (skip si ya existe).
    """
    aliased = []
    for rule in list(app.url_map.iter_rules()):
        if not rule.rule.startswith("/api/"):
            continue
        if rule.rule.startswith("/api/v1/") or rule.rule.startswith("/api/v2/"):
            continue
        new_path = rule.rule.replace("/api/", "/api/v1/", 1)
        if any(r.rule == new_path for r in app.url_map.iter_rules()):
            continue
        try:
            view_func = app.view_functions[rule.endpoint]
            new_endpoint = "v1_" + rule.endpoint
            if new_endpoint in app.view_functions:
                continue
            app.add_url_rule(
                new_path,
                endpoint=new_endpoint,
                view_func=view_func,
                methods=list(rule.methods - {"HEAD", "OPTIONS"}),
            )
            aliased.append(new_path)
        except Exception:
            pass
    return aliased


def build_openapi_spec(app, info: Dict[str, Any] = None) -> Dict[str, Any]:
    """Construye un esquema OpenAPI 3.0 con todas las rutas /api/* registradas."""
    info = info or {}
    paths: Dict[str, Dict[str, Any]] = {}
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/"):
            continue
        if "static" in rule.rule:
            continue
        # Convertir Flask path /api/x/<param> a OpenAPI /api/x/{param}
        path = re.sub(r"<(?:[\w_]+:)?(\w+)>", r"{\1}", rule.rule)
        methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
        op_dict = paths.setdefault(path, {})
        for method in methods:
            op_dict[method.lower()] = {
                "summary": (rule.endpoint or "").replace("_", " "),
                "operationId": rule.endpoint,
                "tags": [_tag_from_path(path)],
                "responses": {
                    "200": {"description": "OK"},
                    "401": {"description": "Unauthorized"},
                    "429": {"description": "Rate limited"},
                    "500": {"description": "Server error"},
                },
            }
            # Detectar path params
            params = []
            for m in re.finditer(r"\{(\w+)\}", path):
                params.append({
                    "name": m.group(1),
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                })
            if params:
                op_dict[method.lower()]["parameters"] = params
            if method in ("POST", "PUT", "PATCH"):
                op_dict[method.lower()]["requestBody"] = {
                    "content": {"application/json": {"schema": {"type": "object"}}}
                }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": info.get("title", "TM PERROS API"),
            "version": info.get("version", "1.0.0"),
            "description": info.get("description",
                "Dashboard Ticketmaster multi-dominio · 910+ endpoints REST · "
                "Queue Manager · Sniper Mode · Anti-bot stack · Compliance Gate"),
        },
        "servers": [
            {"url": "http://127.0.0.1:5055", "description": "local"},
            {"url": "http://192.168.1.33:5055", "description": "LAN"},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "sessionCookie": {
                    "type": "apiKey", "in": "cookie", "name": "session"
                }
            }
        },
        "security": [{"sessionCookie": []}],
    }


def _tag_from_path(path: str) -> str:
    """Deriva tag OpenAPI desde el path /api/<tag>/..."""
    m = re.match(r"/api(?:/v\d+)?/([\w-]+)", path)
    return m.group(1) if m else "general"
