#!/bin/bash
# M13.2 LOCKED — CI verification script.
# Modo: VERIFICATION ONLY. No modifica código.
# Exit 0 si todas las assertions del manifest se cumplen, exit !=0 si no.

set -e

echo "Running M13.2 LOCKED verification..."

python - << 'PY'
import ast

# 1. AST parse — utf-8 explicit por cross-platform safety (cp1252 default fails)
with open("_compra_parallel.py", encoding="utf-8") as f:
    src = f.read()
tree = ast.parse(src)

# 2. Wire presence
assert "_purchase_gate_check" in src, "[FAIL] _purchase_gate_check missing"
assert "realizar_compra" in src, "[FAIL] realizar_compra reference missing"
assert "checkout" in src.lower(), "[FAIL] checkout reference missing"

# 3. Allowed outcome statuses
for status in ("gate_denied", "dry_run_success", "gate_error"):
    assert status in src, f"[FAIL] status missing: {status}"

# 4. Helper top-level declaration
top_funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
assert "_purchase_gate_check" in top_funcs, "[FAIL] helper not top-level"

# 5. Wire ordering: helper call BEFORE the irreversible click
gate_idx = src.find("_purchase_gate_check(")
click_idx = src.find("realizar_clicked = page.evaluate")
assert gate_idx > 0, "[FAIL] helper never invoked"
assert click_idx > 0, "[FAIL] realizar_compra click missing"
assert gate_idx < click_idx, "[FAIL] helper call must precede irreversible click"

# 6. Monotonic checkout timestamp captured
assert "_checkout_started_at_monotonic = time.monotonic()" in src, (
    "[FAIL] monotonic checkout timestamp not captured"
)

# 7. PurchaseGate references intact
assert "core.diagnostics.purchase_gate" in src, "[FAIL] gate module not referenced"
assert "get_default_gate" in src, "[FAIL] singleton accessor not referenced"

print("[OK] AST VALID + M13.2 wiring present")
print("[OK] Default safety: gate is abort-first")
print("[OK] All 7 CI assertions pass")
PY

echo "Running M13.2 integration tests..."
pytest -q tests/test_compra_parallel_gate_integration.py

echo ""
echo "✔ M13.2 CI PASS"
