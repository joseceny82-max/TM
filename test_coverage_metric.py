"""test_coverage_metric.py — Métrica activa de cobertura por self_test.

Para cada módulo del proyecto que tenga `def self_test()`:
  1. Identifica las funciones públicas top-level del módulo
  2. AST-walk del cuerpo de `self_test()` para detectar QUÉ funciones llama
  3. Calcula:
       - covered_fns = funciones llamadas por self_test
       - total_fns   = funciones públicas del módulo
       - coverage_pct = covered_fns / total_fns
       - covered_loc / total_loc (LOC de las funciones cubiertas vs todas)
  4. Reporta resultado JSON consumible por dashboard

Filtros:
  - changed_last_30d: solo módulos con git log --since=30 days
  - module=<nombre>: filtra por uno

Uso:
  python test_coverage_metric.py                     # toda la base
  python test_coverage_metric.py --since-days=30     # último mes
  python test_coverage_metric.py --json              # output JSON
  python test_coverage_metric.py --module=otp_manager
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

PROYECTO = Path(__file__).resolve().parent
EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", "logs", "backups",
                  "datos", "data", "secrets", "node_modules", ".github",
                  "staging_data", "captcha_training_export", "tests", "tools",
                  "staging"}


def list_module_files() -> List[Path]:
    out = []
    for f in sorted(PROYECTO.glob("*.py")):
        if f.name.startswith("_"):
            continue
        if f.name in ("dashboard.py", "test_coverage_metric.py"):
            continue
        out.append(f)
    return out


def parse_module(path: Path):
    """Devuelve (tree, functions, self_test_node)."""
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:
        return None, [], None
    functions = []  # (name, lineno, end_lineno) top-level
    self_test_node = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "self_test":
                self_test_node = node
                continue
            if node.name.startswith("_"):
                continue
            end = getattr(node, "end_lineno", node.lineno)
            functions.append((node.name, node.lineno, end))
    return tree, functions, self_test_node


def extract_calls(node: ast.AST) -> Set[str]:
    """Devuelve nombres de funciones llamadas dentro de un AST."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                # module.fn() — registramos el atributo
                names.add(f.attr)
    return names


def git_changed_recently(path: Path, days: int) -> bool:
    """Verifica si el archivo fue modificado en git en los últimos `days`."""
    try:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        r = subprocess.run(
            ["git", "log", "--since", since, "--oneline", "--", str(path)],
            cwd=PROYECTO, capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except Exception:
        return True  # fallback: incluir si git no está disponible


def compute_coverage(path: Path) -> Dict[str, Any]:
    tree, functions, self_test = parse_module(path)
    if tree is None:
        return {"file": path.name, "error": "parse_failed"}
    if self_test is None:
        return {
            "file": path.name,
            "has_self_test": False,
            "total_fns": len(functions),
            "coverage_pct": 0,
        }
    called_names = extract_calls(self_test)
    fn_names = [f[0] for f in functions]
    covered = [f for f in functions if f[0] in called_names]

    # LOC calc
    src_lines = path.read_text(encoding="utf-8").splitlines()
    total_loc = len(src_lines)
    covered_loc = sum((end - start + 1) for _, start, end in covered)
    total_fn_loc = sum((end - start + 1) for _, start, end in functions)

    return {
        "file": path.name,
        "has_self_test": True,
        "total_fns": len(functions),
        "covered_fns": len(covered),
        "covered_fn_names": [c[0] for c in covered][:20],
        "uncovered_fn_names": [f for f in fn_names if f not in called_names][:20],
        "coverage_pct": round(100 * len(covered) / max(1, len(functions)), 1),
        "total_loc": total_loc,
        "covered_loc": covered_loc,
        "total_fn_loc": total_fn_loc,
        "loc_coverage_pct": round(100 * covered_loc / max(1, total_fn_loc), 1),
    }


def run_audit(since_days: Optional[int] = None,
                only_module: Optional[str] = None) -> Dict[str, Any]:
    files = list_module_files()
    if only_module:
        files = [f for f in files if only_module in f.name]
    if since_days:
        files = [f for f in files if git_changed_recently(f, since_days)]

    results = []
    for f in files:
        r = compute_coverage(f)
        results.append(r)

    # Stats agregadas
    with_test = [r for r in results if r.get("has_self_test")]
    without_test = [r for r in results if not r.get("has_self_test")]
    total_fns_w = sum(r.get("total_fns", 0) for r in with_test)
    covered_fns = sum(r.get("covered_fns", 0) for r in with_test)
    total_fn_loc = sum(r.get("total_fn_loc", 0) for r in with_test)
    covered_loc = sum(r.get("covered_loc", 0) for r in with_test)

    return {
        "generated_at": datetime.now().isoformat(),
        "since_days": since_days,
        "only_module": only_module,
        "totals": {
            "modules": len(results),
            "with_self_test": len(with_test),
            "without_self_test": len(without_test),
            "with_self_test_pct": round(100 * len(with_test) / max(1, len(results)), 1),
            "total_fns": total_fns_w + sum(r.get("total_fns", 0) for r in without_test),
            "covered_fns": covered_fns,
            "fn_coverage_pct": round(100 * covered_fns / max(1, total_fns_w), 1),
            "loc_coverage_pct": round(100 * covered_loc / max(1, total_fn_loc), 1),
        },
        "modules": results,
    }


def self_test() -> Dict[str, Any]:
    """Self-test del propio módulo (irónicamente sí lo tiene)."""
    out = {"ok": True, "checks": {}}
    # 1. parse_module funciona en este propio archivo
    tree, fns, st = parse_module(Path(__file__))
    out["checks"]["parses_self"] = (tree is not None)
    out["checks"]["finds_self_test"] = (st is not None)
    # 2. extract_calls detecta calls dentro de un nodo
    test_src = "def x(): foo(); bar(1, baz())"
    n = ast.parse(test_src).body[0]
    calls = extract_calls(n)
    out["checks"]["extract_calls_basic"] = (
        "foo" in calls and "bar" in calls and "baz" in calls)
    # 3. compute_coverage devuelve estructura válida
    r = compute_coverage(Path(__file__))
    out["checks"]["compute_coverage_keys"] = all(
        k in r for k in ("has_self_test", "total_fns", "coverage_pct"))
    out["checks"]["self_module_has_self_test"] = r.get("has_self_test", False)
    out["passed"] = sum(1 for v in out["checks"].values() if v)
    out["total"] = len(out["checks"])
    out["ok"] = out["passed"] == out["total"]
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since-days", type=int, default=None,
                     help="Solo módulos modificados en últimos N días")
    p.add_argument("--module", default=None, help="Filtrar por nombre")
    p.add_argument("--json", action="store_true", help="Output JSON")
    args = p.parse_args()

    result = run_audit(since_days=args.since_days, only_module=args.module)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    t = result["totals"]
    print("═" * 72)
    print(f"  Cobertura por self_test — {result['generated_at'][:19]}")
    if args.since_days:
        print(f"  Filtro: ultimos {args.since_days} dias (git log)")
    print("═" * 72)
    print(f"  Modulos analizados:     {t['modules']}")
    print(f"  Con self_test:          {t['with_self_test']}/{t['modules']} ({t['with_self_test_pct']}%)")
    print(f"  Funciones cubiertas:    {t['covered_fns']}/{t['total_fns']} ({t['fn_coverage_pct']}%)")
    print(f"  LOC cobertura:          {t['loc_coverage_pct']}%")
    print()
    # Top peor cobertura (con self_test pero baja)
    weak = [r for r in result["modules"]
              if r.get("has_self_test") and r.get("coverage_pct", 0) < 30
              and r.get("total_fns", 0) >= 5]
    weak.sort(key=lambda x: x.get("coverage_pct", 0))
    if weak:
        print(f"  ⚠ Modulos con self_test pero <30% coverage ({len(weak)}):")
        for r in weak[:10]:
            print(f"    {r['file']:40s} {r['coverage_pct']:5.1f}%  "
                    f"({r['covered_fns']}/{r['total_fns']} fns)")
        print()
    # Modulos grandes sin self_test
    no_st = [r for r in result["modules"]
               if not r.get("has_self_test") and r.get("total_fns", 0) >= 10]
    if no_st:
        print(f"  ⚠ Modulos grandes (>10 fns) sin self_test ({len(no_st)}):")
        no_st.sort(key=lambda x: -x.get("total_fns", 0))
        for r in no_st[:10]:
            print(f"    {r['file']:40s} {r.get('total_fns', 0)} fns")


if __name__ == "__main__":
    main()
