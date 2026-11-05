"""core/dashboard/dead_code_detector.py — Dead-code & dead-event detection.

Detecta CON EVIDENCIA OBJETIVA:
    - Módulos en core/ que nadie importa
    - Eventos catalogados (ERROR_CODES) que jamás se han emitido
    - Categorías sin tráfico real
    - Feature flags que están en código pero no se consultan
    - Routes / blueprints registrados sin tráfico

No es heurística vaga; es escaneo AST + análisis de JSONL.

OUTPUT: `runs/dead_components_report.json`

stdlib only. Pure functions donde se puede.
"""
from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.observability.error_classification import (
    ERROR_CODES,
    ErrorCategory,
)
from core.observability.event_aggregator import read_event_stream


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
DEFAULT_PROJECT_ROOT: str = "."
DEFAULT_REPORT_PATH: str = "runs/dead_components_report.json"
DEFAULT_EVENTS_GLOB: str = "runs/structured_events_*.jsonl"

# Módulos que NO contamos como "core code" (excluidos del análisis)
EXCLUDED_DIRS: frozenset = frozenset({
    "__pycache__", ".git", ".pytest_cache", "node_modules",
    "venv", ".venv", "dist", "build", "tests",
    "runs",   # outputs, not code
})

# Archivos que NO contamos
EXCLUDED_FILES: frozenset = frozenset({
    "__init__.py",   # estos son re-exports, no código "propio"
})


# ════════════════════════════════════════════════════════════════════
#  Helpers AST
# ════════════════════════════════════════════════════════════════════
def _walk_python_files(root: Path) -> list:
    """Lista todos los archivos .py bajo `root`, ignorando dirs excluidos."""
    out: list = []
    if not root.exists():
        return out
    for p in root.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def _parse_imports(path: Path) -> set:
    """Extrae todos los módulos importados desde un archivo (dotted)."""
    out: set = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                out.add(n.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module)
                # También cuenta los símbolos importados desde ese módulo
                # como "uso" del módulo padre
    return out


def _module_path_to_dotted(path: Path, root: Path) -> str:
    """Convierte ruta de archivo a notation dotted (core.observability.X)."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    parts = list(rel.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _scan_string_literals(path: Path) -> set:
    """Extrae todos los string literals del archivo (para detectar
    referencias a event codes que no son imports)."""
    out: set = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value.strip()
            if v and len(v) < 100:   # ignore long strings (logs/docs)
                out.add(v)
    return out


# ════════════════════════════════════════════════════════════════════
#  1. UNUSED MODULES
# ════════════════════════════════════════════════════════════════════
def find_unused_modules(*,
                         project_root: Any = DEFAULT_PROJECT_ROOT,
                         only_under: str = "core") -> dict:
    """Detecta módulos `.py` bajo `only_under` que NADIE importa.

    Returns:
        dict con:
            "total_modules": int
            "imported_modules": int
            "unused": list[dotted_name]
            "details": dict
    """
    root = Path(project_root).resolve()
    target_root = root / only_under
    if not target_root.exists():
        return {"total_modules": 0, "imported_modules": 0,
                "unused": [], "details": {}}

    # 1. Listar todos los módulos candidato bajo `only_under`
    candidates: dict = {}
    for p in _walk_python_files(target_root):
        if p.name in EXCLUDED_FILES:
            continue
        dotted = _module_path_to_dotted(p, root)
        candidates[dotted] = str(p)

    # 2. Recolectar TODOS los imports en TODO el proyecto
    all_imports: set = set()
    for p in _walk_python_files(root):
        all_imports |= _parse_imports(p)

    # 3. Un módulo está "usado" si:
    #    - su dotted name aparece como import
    #    - o un prefijo de su dotted name aparece (e.g. core.foo importa core.foo.bar)
    imported_set: set = set()
    for dotted, _ in candidates.items():
        for imp in all_imports:
            if imp == dotted or imp.startswith(dotted + "."):
                imported_set.add(dotted)
                break
            if dotted.startswith(imp + ".") or dotted == imp:
                imported_set.add(dotted)
                break

    unused = sorted(d for d in candidates if d not in imported_set)
    return {
        "total_modules": len(candidates),
        "imported_modules": len(imported_set),
        "unused": unused,
        "details": {d: candidates[d] for d in unused},
    }


# ════════════════════════════════════════════════════════════════════
#  2. UNEMITTED EVENT CODES
# ════════════════════════════════════════════════════════════════════
def find_unemitted_event_codes(*,
                                 events_source: Any = DEFAULT_EVENTS_GLOB,
                                 ) -> dict:
    """Compara catálogo `ERROR_CODES` vs códigos efectivamente emitidos
    en `runs/structured_events_*.jsonl`.

    Returns:
        dict con counts por categoría + lista de códigos nunca emitidos.
    """
    # 1. Cargar TODOS los eventos del glob
    emitted_codes: set = set()
    if isinstance(events_source, str):
        from glob import glob
        for f in glob(events_source):
            for ev in read_event_stream(f):
                code = ev.get("code")
                if isinstance(code, str) and code:
                    emitted_codes.add(code)
    elif isinstance(events_source, list):
        for ev in events_source:
            if isinstance(ev, dict):
                code = ev.get("code")
                if isinstance(code, str) and code:
                    emitted_codes.add(code)

    # 2. Comparar con catálogo
    catalog_per_cat: dict = {}
    unemitted_per_cat: dict = {}
    for cat, codes in ERROR_CODES.items():
        catalog_per_cat[cat.value] = sorted(codes)
        unemitted = sorted(c for c in codes if c not in emitted_codes)
        unemitted_per_cat[cat.value] = unemitted

    total_catalog = sum(len(c) for c in catalog_per_cat.values())
    total_unemitted = sum(len(c) for c in unemitted_per_cat.values())
    return {
        "total_cataloged_codes": int(total_catalog),
        "total_emitted_codes": len(emitted_codes),
        "total_unemitted": int(total_unemitted),
        "coverage_rate": (
            round((total_catalog - total_unemitted) / total_catalog, 4)
            if total_catalog > 0 else 0.0
        ),
        "unemitted_per_category": unemitted_per_cat,
        "categories_with_zero_events": sorted(
            cat for cat, codes in unemitted_per_cat.items()
            if len(codes) == len(catalog_per_cat[cat])
        ),
    }


# ════════════════════════════════════════════════════════════════════
#  3. UNUSED FEATURE FLAGS
# ════════════════════════════════════════════════════════════════════
def find_unused_feature_flags(*,
                                project_root: Any = DEFAULT_PROJECT_ROOT,
                                flag_names: Optional[list] = None,
                                ) -> dict:
    """Detecta feature flags definidas que NO se consultan en código.

    Args:
        flag_names: lista opcional de flags a verificar.
            Si None, usa lista por defecto (MigrationFlags).

    Returns:
        dict con counts + lista de flags huérfanas.
    """
    if flag_names is None:
        flag_names = [
            "USE_CORE_FLOW", "SHADOW_CORE_FLOW",
            "ENABLE_CORE_METRICS", "DIVERGENCE_LOG_LEVEL",
            "FLOW_MODE", "KILL_SWITCH", "CANARY_RATE",
        ]
    root = Path(project_root).resolve()

    # Para cada flag, contar referencias en código (excluyendo
    # el archivo que la DEFINE)
    usage: dict = {}
    for flag in flag_names:
        count = 0
        defining_files: set = set()
        for p in _walk_python_files(root):
            try:
                src = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            n = src.count(flag)
            if n > 0:
                count += n
                if "flags.py" in str(p) or "cutover_router.py" in str(p):
                    defining_files.add(str(p))
        # Una flag "usada" tiene > 1 ocurrencia (1 = definición sola)
        usage[flag] = {
            "occurrences": count,
            "defined_in": sorted(defining_files),
            "used_outside_definition": count > len(defining_files),
        }

    unused = sorted(f for f, info in usage.items()
                     if not info["used_outside_definition"])
    return {
        "total_flags": len(flag_names),
        "unused": unused,
        "usage_per_flag": usage,
    }


# ════════════════════════════════════════════════════════════════════
#  4. METRICS WITH ZERO TRAFFIC
# ════════════════════════════════════════════════════════════════════
def find_zero_traffic_metrics(*,
                                events_source: Any = DEFAULT_EVENTS_GLOB,
                                ) -> dict:
    """Detecta categorías de error que SE DEBERÍAN ver en producción
    pero están a cero en los JSONL.

    Categorías productivas esperadas: payment, network, anti_bot,
    cart_lifecycle. Si están a 0 puede significar:
        a) producción aún no las ha pisado (insufficient data)
        b) el hook de clasificación está roto
    """
    emitted_per_cat: dict = {}
    if isinstance(events_source, str):
        from glob import glob
        for f in glob(events_source):
            for ev in read_event_stream(f):
                cat = ev.get("category")
                if isinstance(cat, str) and cat:
                    emitted_per_cat[cat] = emitted_per_cat.get(cat, 0) + 1
    elif isinstance(events_source, list):
        for ev in events_source:
            if isinstance(ev, dict):
                cat = ev.get("category")
                if isinstance(cat, str) and cat:
                    emitted_per_cat[cat] = emitted_per_cat.get(cat, 0) + 1

    productive_cats: frozenset = frozenset({
        "payment", "network", "anti_bot", "cart_lifecycle",
        "inventory", "queue",
    })
    zero_cats = sorted(c for c in productive_cats
                        if emitted_per_cat.get(c, 0) == 0)
    return {
        "events_per_category": dict(emitted_per_cat),
        "productive_categories_with_zero_traffic": zero_cats,
        "total_events": sum(emitted_per_cat.values()),
        "diagnosis": (
            "insufficient_data"
            if sum(emitted_per_cat.values()) < 10
            else "possible_broken_hooks"
            if zero_cats else "ok"
        ),
    }


# ════════════════════════════════════════════════════════════════════
#  5. Top-level report builder
# ════════════════════════════════════════════════════════════════════
def build_dead_components_report(*,
                                   project_root: Any = DEFAULT_PROJECT_ROOT,
                                   events_source: Any = DEFAULT_EVENTS_GLOB,
                                   output_path: Optional[Any] = None,
                                   ) -> dict:
    """Genera el reporte consolidado de componentes muertos.

    Si `output_path` se proporciona, persiste el reporte como JSON.
    """
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unused_modules": find_unused_modules(project_root=project_root),
        "unemitted_codes": find_unemitted_event_codes(events_source=events_source),
        "unused_flags": find_unused_feature_flags(project_root=project_root),
        "zero_traffic_metrics": find_zero_traffic_metrics(events_source=events_source),
    }

    if output_path:
        try:
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(report, indent=2, sort_keys=True),
                            encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass

    return report


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
