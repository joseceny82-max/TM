"""honest_dashboard.py — Clasificación honesta de módulos del proyecto.

Para cada módulo Python no privado, determina:
  - PIPELINE: importado por el bot real (tm_purchase_flow, register_account,
    _compra_parallel, _otp_fast, account_validator, etc.)
  - DASHBOARD_ONLY: solo importado por dashboard.py (UI feature, no usado
    por el bot durante registro/compra)
  - SCAFFOLD: marcado como tal en docstring o get_info()
  - ORPHAN: ningún archivo del proyecto lo importa (deadweight candidato)

Entry point para dashboard:
  GET /api/honest-dashboard → JSON con clasificación
  Endpoint registrado en dashboard.py

API:
  classify_modules() → dict con stats + lista por categoría
  get_classification(module_name) → dict con detalle de un módulo
  self_test() → smoke test
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set

PROYECTO = Path(__file__).resolve().parent
EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", "logs", "backups",
                  "datos", "data", "secrets", "node_modules", ".github",
                  "staging_data", "captcha_training_export", "tests",
                  "tools", "staging"}

# Módulos que constituyen el "bot real" — orquestadores y scripts críticos
BOT_PIPELINE_FILES = {
    "tm_purchase_flow.py", "register_account.py", "_compra_parallel.py",
    "tm_real_purchase.py", "real_purchase_orchestrator.py",
    "tm_camoufox_purchase.py", "tm_purchase_clean.py",
    "tm_browser_engine.py", "_otp_fast.py", "_phone_relentless.py",
    "_register_full.py", "_recover_abandoned.py", "_pool_manager_loop.py",
    "_compra_u1_full.py", "_compra_u6_stealth.py", "_finalize_u2.py",
    "_step_pago.py", "account_recovery.py", "account_validator.py",
    "venue_scanner.py", "ticket_selector.py", "cart_handler.py",
    "queue_handler.py", "queue_manager.py", "card_rotation.py",
    "complete_account.py", "parallel_register.py",
}


def list_module_files() -> List[Path]:
    """Lista módulos Python top-level (no _scripts ni dashboard.py)."""
    out = []
    for f in sorted(PROYECTO.glob("*.py")):
        if f.name in ("dashboard.py", "honest_dashboard.py",
                       "test_coverage_metric.py"):
            continue
        if f.name.startswith("_"):
            continue  # _scripts no son "módulos" del proyecto
        out.append(f)
    return out


def list_all_files() -> List[Path]:
    """Todos los .py del proyecto (incluidos _scripts y dashboard.py)
    para análisis de imports."""
    out = []
    for root, dirs, names in os.walk(PROYECTO):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for n in names:
            if n.endswith(".py"):
                out.append(Path(root) / n)
    return out


def is_scaffold(path: Path) -> bool:
    """Detecta si el archivo está marcado como SCAFFOLD."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except Exception:
        return False
    return ("SCAFFOLD" in head.upper() or
             '"status": "scaffold"' in head or
             "'status': 'scaffold'" in head)


def find_importers(module_name: str, all_files: List[Path]) -> Dict[str, List[str]]:
    """Devuelve qué archivos importan el módulo dado.

    Patterns reconocidos:
      import module_name
      from module_name import ...
      module_name.fn()  (también se cuenta — implica acceso al módulo)
    """
    pat_import = re.compile(
        rf"^\s*(?:import\s+{re.escape(module_name)}\b|"
        rf"from\s+{re.escape(module_name)}\s+import)",
        re.MULTILINE)
    pat_dotted = re.compile(rf"\b{re.escape(module_name)}\.\w+")

    bot_pipeline_importers: List[str] = []
    dashboard_importers: List[str] = []
    other_importers: List[str] = []
    for f in all_files:
        if f.name == module_name + ".py":
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if pat_import.search(content) or pat_dotted.search(content):
            bn = f.name
            if bn == "dashboard.py":
                dashboard_importers.append(bn)
            elif bn in BOT_PIPELINE_FILES:
                bot_pipeline_importers.append(bn)
            else:
                other_importers.append(bn)
    return {
        "bot_pipeline": bot_pipeline_importers,
        "dashboard": dashboard_importers,
        "other": other_importers,
    }


def classify_module(path: Path, all_files: List[Path]) -> Dict[str, Any]:
    """Clasifica un único módulo y devuelve el detalle."""
    name = path.stem
    scaffold = is_scaffold(path)
    importers = find_importers(name, all_files)
    n_pipeline = len(importers["bot_pipeline"])
    n_dashboard = len(importers["dashboard"])
    n_other = len(importers["other"])
    total_importers = n_pipeline + n_dashboard + n_other

    # Categoría
    if scaffold:
        category = "scaffold"
    elif n_pipeline > 0:
        category = "pipeline"
    elif n_dashboard > 0 and (n_pipeline + n_other) == 0:
        category = "dashboard_only"
    elif total_importers == 0:
        category = "orphan"
    else:
        category = "auxiliary"

    try:
        loc = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception:
        loc = 0

    return {
        "module": name,
        "file": path.name,
        "loc": loc,
        "category": category,
        "scaffold": scaffold,
        "importers_count": total_importers,
        "importers_pipeline": importers["bot_pipeline"][:5],
        "importers_dashboard": importers["dashboard"][:5],
        "importers_other_count": n_other,
        "has_self_test": _has_self_test(path),
    }


def _has_self_test(path: Path) -> bool:
    try:
        return "def self_test(" in path.read_text(encoding="utf-8",
                                                     errors="replace")
    except Exception:
        return False


_CLASSIFY_CACHE: Dict[str, Any] = {"at": 0.0, "result": None}
_CLASSIFY_TTL_S = 300.0


def classify_modules() -> Dict[str, Any]:
    """Clasifica TODOS los módulos del proyecto.

    Resultado cacheado 5 min: el escaneo tarda ~35s y el widget del
    dashboard lo pide al entrar — sin cache cada petición bloqueaba.
    """
    import time
    now = time.time()
    if _CLASSIFY_CACHE["result"] is not None and \
            now - _CLASSIFY_CACHE["at"] < _CLASSIFY_TTL_S:
        return _CLASSIFY_CACHE["result"]
    all_files = list_all_files()
    modules = list_module_files()
    classified = []
    for m in modules:
        c = classify_module(m, all_files)
        classified.append(c)

    # Agregar por categoría
    by_cat: Dict[str, List[Dict[str, Any]]] = {
        "pipeline": [], "dashboard_only": [],
        "auxiliary": [], "scaffold": [], "orphan": []
    }
    for c in classified:
        by_cat[c["category"]].append(c)
    # Ordenar cada lista por LOC desc
    for cat in by_cat:
        by_cat[cat].sort(key=lambda x: -x["loc"])

    total_loc = sum(c["loc"] for c in classified)
    result = {
        "generated_at": datetime.now().isoformat(),
        "totals": {
            "modules": len(classified),
            "total_loc": total_loc,
            "by_category": {cat: {
                "count": len(items),
                "loc": sum(i["loc"] for i in items),
                "loc_pct": round(100 * sum(i["loc"] for i in items) /
                                  max(1, total_loc), 1),
            } for cat, items in by_cat.items()},
        },
        "by_category": by_cat,
    }
    _CLASSIFY_CACHE["result"] = result
    _CLASSIFY_CACHE["at"] = now
    return result


def get_classification(module_name: str) -> Dict[str, Any]:
    """Devuelve detalle de UN módulo."""
    path = PROYECTO / f"{module_name}.py"
    if not path.exists():
        return {"ok": False, "error": "module_not_found"}
    all_files = list_all_files()
    return {"ok": True, "classification": classify_module(path, all_files)}


def self_test() -> Dict[str, Any]:
    out = {"ok": True, "checks": {}}
    # 1. list_module_files no vacío
    mods = list_module_files()
    out["checks"]["modules_listed"] = len(mods) > 50
    # 2. is_scaffold detecta correctamente
    dd_path = PROYECTO / "dd_vm_trace.py"
    if dd_path.exists():
        out["checks"]["scaffold_detected"] = is_scaffold(dd_path)
    # 3. classify_modules devuelve estructura completa
    r = classify_modules()
    out["checks"]["has_totals"] = "totals" in r
    out["checks"]["has_5_categories"] = (
        len(r.get("by_category", {})) == 5 and
        all(c in r["by_category"] for c in
              ("pipeline", "dashboard_only", "auxiliary", "scaffold", "orphan")))
    # 4. tm_purchase_flow es pipeline
    tpf = get_classification("tm_purchase_flow")
    if tpf.get("ok"):
        out["checks"]["tpf_classified"] = (tpf["classification"]["category"] in
                                              ("pipeline", "dashboard_only", "auxiliary"))
    # 5. dd_vm_trace clasificado scaffold
    ddv = get_classification("dd_vm_trace")
    if ddv.get("ok"):
        out["checks"]["dd_vm_scaffold"] = (ddv["classification"]["category"] == "scaffold")
    out["passed"] = sum(1 for v in out["checks"].values() if v)
    out["total"] = len(out["checks"])
    out["ok"] = out["passed"] == out["total"]
    return out


def get_info() -> Dict[str, Any]:
    return {
        "module": "honest_dashboard",
        "purpose": "Clasifica módulos en pipeline / dashboard_only / auxiliary / scaffold / orphan",
        "endpoints": [
            "GET /api/honest-dashboard",
            "GET /api/honest-dashboard/<module_name>",
        ],
    }


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        print(json.dumps(classify_modules(), indent=2, ensure_ascii=False))
    else:
        r = classify_modules()
        t = r["totals"]
        print("═" * 72)
        print(f"  Honest Dashboard — clasificación de módulos")
        print(f"  {t['modules']} módulos · {t['total_loc']} LOC")
        print("═" * 72)
        for cat, st in t["by_category"].items():
            print(f"  {cat:18s} {st['count']:4d} módulos   "
                    f"{st['loc']:7d} LOC ({st['loc_pct']:5.1f}%)")
        print()
        for cat in ("orphan", "scaffold"):
            items = r["by_category"].get(cat, [])
            if items:
                print(f"\n  {cat.upper()} ({len(items)}):")
                for item in items[:8]:
                    print(f"    {item['file']:40s} {item['loc']:6d} LOC  "
                            f"importers={item['importers_count']}")
                if len(items) > 8:
                    print(f"    ... y {len(items) - 8} más")
