"""Test 1: todos los módulos críticos importan sin errores."""
import importlib

CRITICAL_MODULES = [
    # Core
    "config", "json_atomic", "users_manager",
    # Eventos + cuentas
    "event_manager", "event_discovery", "account_manager",
    "account_validator", "account_recovery", "account_warmer",
    # Bot compra (NO _compra_parallel: es script con sys.stdout reasignado)
    "tm_purchase_flow", "tm_real_purchase", "tm_browser_engine",
    "ticket_selector",
    # Queue
    "queue_manager", "onsale_scheduler", "cookie_warmer",
    # Anti-bot
    "captcha_manager", "ja4_spoof", "akamai_sbsd",
    "imperva_bypass", "behavior_chain", "human_motion",
    "session_persistence", "hyper_sdk_chain",
    # Pagos
    "card_rotation", "compliance_gate", "otp_3ds",
    # Notif + sistema
    "notifications", "health_monitor", "backup_system",
    "housekeeping", "venue_scanner", "venue_cache",
]


def test_all_critical_modules_import():
    """Verifica que ningún módulo crítico falla al importar."""
    failed = []
    for mod in CRITICAL_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as e:
            failed.append((mod, f"{type(e).__name__}: {e}"))
    assert not failed, f"Módulos con fallo de import: {failed}"


def test_dashboard_app_starts():
    """Verifica que el módulo dashboard se inicializa con todos los endpoints."""
    import dashboard
    assert dashboard.app is not None
    n_endpoints = len(dashboard.app.url_map._rules)
    assert n_endpoints >= 800, f"Solo {n_endpoints} endpoints registrados (esperado >800)"


def test_all_self_tests_pass():
    """Ejecuta self_test() de cada módulo que lo tenga."""
    import importlib
    fails = []
    for mod_name in ["queue_manager", "human_motion", "tm_real_purchase",
                      "onsale_scheduler", "account_warmer", "housekeeping",
                      "behavior_chain", "imperva_bypass", "akamai_sbsd",
                      "session_persistence"]:
        try:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "self_test"):
                r = mod.self_test()
                if not r.get("ok"):
                    fails.append((mod_name, r))
        except Exception as e:
            fails.append((mod_name, str(e)))
    assert not fails, f"Self-tests fallidos: {fails}"
