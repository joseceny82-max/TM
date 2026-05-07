"""tools/scope_creep_audit.py — Detecta funciones definidas pero no llamadas
fuera de su propio módulo o de tests.

Heurística:
  1. Para cada `def fn_name(...)` en un módulo, busca `fn_name(` o
     `module.fn_name` en TODO el proyecto.
  2. Si la única referencia está en el archivo donde se define + tests/
     y no es un endpoint Flask, marca "CANDIDATO scope creep".
  3. Excluye dunder methods, self_test, get_info, run_*, helpers privados (_).

Uso:
  python tools/scope_creep_audit.py [--module=<nombre>]

Output: tabla con candidatos para revisión manual antes de borrar.
ESTE SCRIPT NUNCA BORRA NADA — solo lista.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", "logs", "backups",
                  "datos", "data", "secrets", "node_modules", ".github",
                  "staging_data", "captcha_training_export"}
EXCLUDED_FILES_PREFIX = ("_", "test_")
INFRA_FNS = {
    "self_test", "get_info", "main", "run", "start", "stop",
    "check", "fetch", "load", "save", "init",
    "__init__", "__str__", "__repr__", "__call__",
}


def iter_py_files() -> List[Path]:
    files = []
    for root, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for n in names:
            if not n.endswith(".py"):
                continue
            if n.startswith("_") and n not in ("_compra_parallel.py",):
                continue
            files.append(Path(root) / n)
    return files


def extract_functions(path: Path) -> List[Tuple[str, int]]:
    """Devuelve (fn_name, lineno) de funciones top-level + métodos públicos."""
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:
        return []
    out = []
    for node in ast.iter_child_nodes(tree):
        # Top-level functions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue  # private — ignore
            if node.name in INFRA_FNS:
                continue
            out.append((node.name, node.lineno))
        # Class methods públicos
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name.startswith("_") or sub.name in INFRA_FNS:
                        continue
                    qualified = f"{node.name}.{sub.name}"
                    out.append((qualified, sub.lineno))
    return out


def count_external_refs(fn_name: str, defining_file: Path,
                          all_files: List[Path]) -> Tuple[int, List[str]]:
    """Cuenta referencias a fn_name fuera del archivo definiente (+ tests).

    Considera:
      - Llamada directa: `fn_name(`
      - Vía import: `module.fn_name(` o `from module import fn_name`
    """
    base_name = fn_name.split(".")[-1]  # para métodos de clase
    pattern_call = re.compile(rf"\b{re.escape(base_name)}\s*\(")
    pattern_import = re.compile(rf"from\s+\w+\s+import\s+[^#\n]*\b{re.escape(base_name)}\b")
    pattern_attr = re.compile(rf"\.{re.escape(base_name)}\s*\(")

    callers: List[str] = []
    defining_str = str(defining_file)
    for f in all_files:
        if str(f) == defining_str:
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Ignorar tests si solo aparece ahí (pueden ser smoke tests)
        is_test = "tests" in str(f).lower() or f.name.startswith("test_")
        has_match = (pattern_call.search(content) or
                      pattern_import.search(content) or
                      pattern_attr.search(content))
        if has_match:
            tag = "test" if is_test else "user"
            callers.append(f"{tag}:{f.name}")
    # Contar refs solo de tipo "user" (excluye tests)
    user_refs = sum(1 for c in callers if c.startswith("user:"))
    return user_refs, callers


def is_endpoint(fn_name: str, file_content: str) -> bool:
    """Detecta si la función es un endpoint Flask (@app.route encima)."""
    pattern = rf"@app\.route\([^)]*\)[^@]*?def\s+{re.escape(fn_name)}\b"
    return bool(re.search(pattern, file_content, re.DOTALL))


def main():
    only_module = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("--module="):
        only_module = sys.argv[1].split("=", 1)[1]

    all_files = iter_py_files()
    print(f"Scaneando {len(all_files)} archivos Python...")
    print()

    candidates: List[Dict] = []
    for f in all_files:
        if only_module and only_module not in f.name:
            continue
        if f.name == "dashboard.py":
            continue  # endpoints — todos llamados via Flask
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        functions = extract_functions(f)
        for fn_name, lineno in functions:
            base = fn_name.split(".")[-1]
            if is_endpoint(base, content):
                continue
            user_refs, callers = count_external_refs(fn_name, f, all_files)
            if user_refs == 0:
                candidates.append({
                    "file": f.relative_to(ROOT).as_posix(),
                    "function": fn_name,
                    "line": lineno,
                    "test_refs": [c for c in callers if c.startswith("test:")],
                })

    # Agrupar por archivo
    by_file = defaultdict(list)
    for c in candidates:
        by_file[c["file"]].append(c)

    print("═" * 70)
    print(f"  Funciones SIN llamadas externas (excl. tests/dashboard endpoints)")
    print(f"  Total candidatas: {len(candidates)}")
    print(f"  Archivos involucrados: {len(by_file)}")
    print("═" * 70)
    for fname, items in sorted(by_file.items(), key=lambda x: -len(x[1]))[:30]:
        print(f"\n{fname} ({len(items)} candidatas)")
        for c in items[:8]:
            test_count = len(c["test_refs"])
            tag = f"({test_count} tests)" if test_count else "(ZERO refs)"
            print(f"  L{c['line']:5d}  {c['function']:40s}  {tag}")
        if len(items) > 8:
            print(f"  ... y {len(items) - 8} más")
    print()
    print("REVISAR MANUALMENTE — este script NO borra nada.")
    print("Para revisar 1 módulo: python tools/scope_creep_audit.py --module=<name>")


if __name__ == "__main__":
    main()
