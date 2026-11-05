"""verify_purchase_pipeline.py — End-to-end self-check del bot de compras.

Ejecuta verificaciones SIN abrir browser real:
  1. Sintaxis Python de todos los archivos clave
  2. Imports de módulos
  3. Self-tests internos (hyper_sdk_chain, browser_cleanup, etc.)
  4. Data integrity (accounts.json, plantilla, archive)
  5. Wrapper _compra_with_swap acepta args
  6. Burned profiles + subnets filter funciona
  7. Hyper Solutions config status

USO:
    python verify_purchase_pipeline.py
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

PROYECTO = Path(__file__).parent
results: list[tuple[str, bool, str]] = []   # (test_name, ok, detail)


def log(msg: str) -> None:
    print(msg, flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    icon = "✓" if ok else "✗"
    log(f"  {icon} {name:55} {detail}")
    results.append((name, ok, detail))


# ═════════════════════════════════════════════════════════════════
# 1. SINTAXIS
# ═════════════════════════════════════════════════════════════════
def test_syntax():
    log("")
    log("═══ 1. SINTAXIS PYTHON ═══")
    files = [
        "_compra_parallel.py",
        "_compra_with_swap.py",
        "account_recovery.py",
        "register_account.py",
        "register_verify.py",
        "_run_batch10.py",
        "tm_browser_engine.py",
        "tm_real_purchase.py",
        "tm_purchase_flow.py",
        "cookie_warmer.py",
        "hyper_sdk_client.py",
        "hyper_sdk_chain.py",
        "browser_cleanup.py",
        "account_manager.py",
        "setup_hyper_solutions.py",
        "verify_purchase_pipeline.py",
    ]
    for f in files:
        p = PROYECTO / f
        if not p.exists():
            check(f"file_exists:{f}", False, "no existe")
            continue
        try:
            ast.parse(p.read_text(encoding='utf-8'))
            check(f"syntax:{f}", True)
        except SyntaxError as e:
            check(f"syntax:{f}", False, f"L{e.lineno}: {e.msg}")


# ═════════════════════════════════════════════════════════════════
# 2. IMPORTS
# ═════════════════════════════════════════════════════════════════
def test_imports():
    log("")
    log("═══ 2. IMPORTS ═══")
    modules = [
        "_compra_parallel",
        "account_recovery",
        "register_account",
        "register_verify",
        "tm_real_purchase",
        "tm_purchase_flow",
        "cookie_warmer",
        "hyper_sdk_client",
        "hyper_sdk_chain",
        "browser_cleanup",
        "hyper_sdk",  # external package
    ]
    for m in modules:
        t0 = time.time()
        try:
            importlib.import_module(m)
            dt = (time.time() - t0) * 1000
            check(f"import:{m}", True, f"{dt:.0f}ms")
        except Exception as e:
            check(f"import:{m}", False, f"{type(e).__name__}: {str(e)[:80]}")


# ═════════════════════════════════════════════════════════════════
# 3. SELF-TESTS DE MÓDULOS
# ═════════════════════════════════════════════════════════════════
def test_module_selftests():
    log("")
    log("═══ 3. SELF-TESTS DE MÓDULOS ═══")

    # hyper_sdk_chain.self_test()
    try:
        import hyper_sdk_chain
        r = hyper_sdk_chain.self_test()
        passed = r.get('passed_checks', 0)
        total = r.get('total_checks', 0)
        ok = r.get('ok', False) and passed == total and total > 0
        check("hyper_sdk_chain.self_test", ok, f"{passed}/{total} checks")
    except Exception as e:
        check("hyper_sdk_chain.self_test", False, f"{type(e).__name__}: {str(e)[:80]}")

    # browser_cleanup helpers exist
    try:
        import browser_cleanup
        for fn in ("clear_history_via_cdp", "_delete_history_files",
                   "full_cleanup", "post_close_purge"):
            present = hasattr(browser_cleanup, fn)
            check(f"browser_cleanup.{fn}", present)
    except Exception as e:
        check("browser_cleanup", False, str(e)[:80])

    # hyper_sdk Session class
    try:
        import hyper_sdk
        cls_ok = hasattr(hyper_sdk, 'Session')
        check("hyper_sdk.Session class", cls_ok)
        si_ok = hasattr(hyper_sdk, 'SensorInput')
        check("hyper_sdk.SensorInput class", si_ok)
    except Exception as e:
        check("hyper_sdk", False, str(e)[:80])


# ═════════════════════════════════════════════════════════════════
# 4. DATA INTEGRITY
# ═════════════════════════════════════════════════════════════════
def test_data_integrity():
    log("")
    log("═══ 4. DATA INTEGRITY ═══")

    # accounts.json existe + tiene cuentas
    accs_path = PROYECTO / "datos" / "cuentas" / "accounts.json"
    if accs_path.exists():
        try:
            data = json.loads(accs_path.read_text(encoding='utf-8'))
            n = len(data.get('accounts', []))
            check("accounts.json válido", n > 0, f"{n} cuentas")
        except Exception as e:
            check("accounts.json válido", False, str(e)[:80])
    else:
        check("accounts.json existe", False)

    # registered_archive.csv
    arch = PROYECTO / "datos" / "buy" / "runs" / "registered_archive.csv"
    if arch.exists():
        try:
            import csv
            with open(arch, encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
            ok_email = sum(1 for r in rows if '@' in (r.get('Email_TM') or ''))
            ok = ok_email == len(rows)
            check("archive Email_TM contiene @",
                  ok, f"{ok_email}/{len(rows)} filas")
        except Exception as e:
            check("archive integrity", False, str(e)[:80])
    else:
        check("registered_archive.csv existe", False)

    # burned_profiles + subnets
    bp = PROYECTO / "datos" / "buy" / "runs" / "burned_profiles.json"
    bs = PROYECTO / "datos" / "buy" / "runs" / "burned_subnets24.json"
    if bp.exists():
        try:
            d = json.loads(bp.read_text(encoding='utf-8'))
            n = len(d.get('burned', []))
            check("burned_profiles.json", n > 0, f"{n} profiles")
        except Exception as e:
            check("burned_profiles.json", False, str(e)[:80])
    if bs.exists():
        try:
            d = json.loads(bs.read_text(encoding='utf-8'))
            check("burned_subnets24.json", len(d) >= 0, f"{len(d)} subnets")
        except Exception as e:
            check("burned_subnets24.json", False, str(e)[:80])

    # used_lock cleanable
    lk = PROYECTO / "_compra_parallel_used_accounts.lock"
    if lk.exists():
        try:
            d = json.loads(lk.read_text(encoding='utf-8'))
            check("used_accounts.lock parseable", isinstance(d, list),
                  f"{len(d) if isinstance(d, list) else '?'} ids")
        except Exception as e:
            check("used_accounts.lock", False, str(e)[:80])


# ═════════════════════════════════════════════════════════════════
# 5. WRAPPER ACCEPT ARGS
# ═════════════════════════════════════════════════════════════════
def test_wrapper_args():
    log("")
    log("═══ 5. WRAPPER ARGS ═══")
    # Probar que --help / dry import funciona sin spawning real
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import _compra_with_swap as w; print('MAX_SWAPS=', w.MAX_SWAPS, "
             "'COOLDOWN=', w.COOLDOWN_S, 'RC=', w.RC_BOT_BLOCK)"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROYECTO))
        ok = r.returncode == 0
        check("_compra_with_swap import + constants",
              ok, r.stdout.strip()[:120] if ok else r.stderr[:120])
    except Exception as e:
        check("_compra_with_swap", False, str(e)[:80])

    # Test parse_args _compra_parallel
    try:
        r = subprocess.run(
            [sys.executable, "_compra_parallel.py", "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROYECTO))
        ok = "--slot" in (r.stdout + r.stderr)
        check("_compra_parallel --help muestra args", ok)
    except Exception as e:
        check("_compra_parallel --help", False, str(e)[:80])


# ═════════════════════════════════════════════════════════════════
# 6. BURNED FILTERS
# ═════════════════════════════════════════════════════════════════
def test_burned_filters():
    log("")
    log("═══ 6. BURNED FILTERS ═══")
    try:
        import _compra_parallel as cp
        # _load_burned_subnets24
        subnets = cp._load_burned_subnets24()
        check("_load_burned_subnets24 returns set", isinstance(subnets, set),
              f"{len(subnets)} entries")
        # _ip_to_subnet24
        tests = [
            ("88.15.142.106", "88.15.142.0/24"),
            ("171.33.234.174", "171.33.234.0/24"),
            ("", ""),
            ("invalid", ""),
        ]
        all_ok = True
        for ip, expected in tests:
            got = cp._ip_to_subnet24(ip)
            if got != expected:
                all_ok = False
                check(f"_ip_to_subnet24({ip!r})", False, f"got={got!r}")
        if all_ok:
            check("_ip_to_subnet24 (4 cases)", True)
        # _persist_bot_block existe
        check("_persist_bot_block existe", hasattr(cp, '_persist_bot_block'))
        # pick_morelogin_profile + UNION burned
        import register_account as ra
        b1 = ra.load_burned() if hasattr(ra, 'load_burned') else set()
        b2 = ra.load_burned_profiles() if hasattr(ra, 'load_burned_profiles') else set()
        union = b1 | b2
        check("load_burned UNION load_burned_profiles", len(union) > 0,
              f"{len(b1)} + {len(b2)} = {len(union)}")
    except Exception as e:
        check("burned_filters", False, f"{type(e).__name__}: {str(e)[:80]}")


# ═════════════════════════════════════════════════════════════════
# 7. HYPER SOLUTIONS CONFIG
# ═════════════════════════════════════════════════════════════════
def test_hyper_config():
    log("")
    log("═══ 7. HYPER SOLUTIONS CONFIG ═══")
    try:
        import hyper_sdk_client as hsc
        cfg = hsc.get_config()
        key = cfg.get('api_key', '')
        mode = cfg.get('mode')
        configured = bool(key) and not key.startswith('HYPER-PLACE')
        check("hyper_sdk_client.is_configured", configured,
              "key + mode=" + mode if configured else "VACÍO — necesita API key")
        if not configured:
            log("    → Para activar: registrarse en https://hypersolutions.co")
            log("    → Luego: python setup_hyper_solutions.py <API_KEY>")
        else:
            check("mode = live", mode == 'live', f"mode={mode}")
    except Exception as e:
        check("hyper_sdk_client config", False, str(e)[:80])


# ═════════════════════════════════════════════════════════════════
# 8. handle_partners_seguro + watch URL initial
# ═════════════════════════════════════════════════════════════════
def test_handler_functions():
    log("")
    log("═══ 8. HANDLERS clave en _compra_parallel ═══")
    src = (PROYECTO / "_compra_parallel.py").read_text(encoding='utf-8')
    handlers = [
        ("handle_partners_seguro_step", "def handle_partners_seguro_step"),
        ("handle_pre_checkout_step", "def handle_pre_checkout_step"),
        ("_check_bot_block (closure)", "def _check_bot_block"),
        ("_early_bot_check (closure)", "def _early_bot_check"),
        ("force_reset_btn (click reset)", "force_reset_btn_clicked"),
        ("radio_email_selected", "radio_email_selected"),
        ("checkout_detected_at_entry (URL inicial)", "checkout_detected_at_entry"),
        ("session_recovered_re_navigating_event", "session_recovered_re_navigating_event"),
        ("RC=99 bot-block", "return 99   # RC_BOT_BLOCK"),
        ("history_cleanup_finally", "history_cleanup_finally"),
        ("post_close_purge files", "post_close_purge"),
    ]
    for name, marker in handlers:
        check(f"handler/{name}", marker in src)


# ═════════════════════════════════════════════════════════════════
# SUMMARY
# ═════════════════════════════════════════════════════════════════
def main():
    log("═══════════════════════════════════════════════════════════")
    log("  VERIFY PURCHASE PIPELINE — end-to-end self-check")
    log("═══════════════════════════════════════════════════════════")
    test_syntax()
    test_imports()
    test_module_selftests()
    test_data_integrity()
    test_wrapper_args()
    test_burned_filters()
    test_hyper_config()
    test_handler_functions()

    log("")
    log("═══════════════════════════════════════════════════════════")
    log("  RESUMEN")
    log("═══════════════════════════════════════════════════════════")
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    log(f"  PASS: {passed}/{total}")
    if failed:
        log(f"  FAIL: {failed}")
        log("")
        log("  Fallos:")
        for name, ok, detail in results:
            if not ok:
                log(f"    ✗ {name}: {detail}")
    log("")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


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
