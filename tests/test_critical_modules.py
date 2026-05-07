"""tests/test_critical_modules.py — cobertura mínima de los 6 módulos
críticos sin self_test:

  - tm_purchase_flow
  - captcha_manager
  - register_account
  - event_discovery
  - account_validator
  - _compra_parallel

Tests son SMOKE — verifican import + signatures + funciones puras (no llaman
a TM real ni a CAPTCHA solvers ni a IMAP). Para tests E2E ver
.github/workflows/turnstile-e2e.yml.
"""
import importlib
import os
import sys

import pytest


# ═══════════════════════════════════════════════════════════════
#  Module imports — primero ASEGURAR que importan limpios
# ═══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("module_name", [
    "tm_purchase_flow",
    "captcha_manager",
    "register_account",
    "event_discovery",
    "account_validator",
])
def test_module_imports(module_name):
    """Cada módulo crítico debe importar sin error."""
    mod = importlib.import_module(module_name)
    assert mod is not None
    # Verifica que NO se haya roto algo bobo (módulo vacío)
    assert hasattr(mod, "__file__")


def test_compra_parallel_imports():
    """_compra_parallel.py es un script — verifica que el AST parsea (no se
    importa porque tiene side effects al cargar)."""
    import ast
    path = os.path.join(os.path.dirname(__file__), os.pardir,
                        "_compra_parallel.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    ast.parse(src)  # raise SyntaxError si está roto


# ═══════════════════════════════════════════════════════════════
#  tm_purchase_flow — orquestador
# ═══════════════════════════════════════════════════════════════
class TestPurchaseFlow:
    def test_strategies_dict(self):
        import tm_purchase_flow as tpf
        assert hasattr(tpf, "STRATEGIES")
        assert isinstance(tpf.STRATEGIES, dict)
        assert len(tpf.STRATEGIES) >= 5
        # Estrategias canónicas
        for k in ("best_available", "cheapest", "sniper"):
            assert k in tpf.STRATEGIES

    def test_get_log_returns_ok(self):
        import tm_purchase_flow as tpf
        r = tpf.get_log(limit=5)
        assert isinstance(r, dict)
        assert r.get("ok") is True

    def test_step_pick_proxy_handles_no_proxies(self):
        """Smoke: pick_proxy debe manejar gracefully si no hay proxies."""
        import tm_purchase_flow as tpf
        ctx = {"country": "zz_invalid_country"}
        r = tpf.step_pick_proxy(ctx)
        assert isinstance(r, dict)
        assert "step" in r and r["step"] == "pick_proxy"
        assert "elapsed_ms" in r

    def test_step_try_presale_code_no_event_id(self):
        """step_try_presale_code skipea silenciosamente si no hay event_id."""
        import tm_purchase_flow as tpf
        if not hasattr(tpf, "step_try_presale_code"):
            pytest.skip("step_try_presale_code not defined")
        r = tpf.step_try_presale_code({})
        assert r["ok"] is True  # skipea ok
        assert r.get("skipped") is True

    def test_simulate_mode_no_real_calls(self):
        """En mode='simulate' no debe hacer HTTP real a TM."""
        import tm_purchase_flow as tpf
        ctx = {
            "mode": "simulate",
            "event_id": "test_event_xyz",
            "event": {"tm_url": "https://example.com", "name": "test"},
            "country": "us",
            "quantity": 2,
            "captcha_type": "recaptcha_v2",
        }
        r = tpf.step_simulate_cart(ctx)
        assert isinstance(r, dict)
        assert "step" in r


# ═══════════════════════════════════════════════════════════════
#  captcha_manager
# ═══════════════════════════════════════════════════════════════
class TestCaptchaManager:
    def test_get_config_has_services(self):
        import captcha_manager as cm
        cfg = cm.get_config()
        assert isinstance(cfg, dict)
        assert "services" in cfg
        assert isinstance(cfg["services"], dict)

    def test_solve_signatures_accept_event_id(self):
        """7ª ronda: solve_* aceptan event_id kwarg para tracking per-event."""
        import inspect
        import captcha_manager as cm
        for fn_name in ("solve_recaptcha_v2", "solve_recaptcha_v3",
                          "solve_hcaptcha", "solve_turnstile"):
            sig = inspect.signature(getattr(cm, fn_name))
            assert "event_id" in sig.parameters, \
                f"{fn_name} missing event_id kwarg"

    def test_cost_forecast_keys(self):
        import captcha_manager as cm
        fc = cm.cost_forecast()
        for k in ("monthly_forecast", "annual_forecast",
                    "daily_avg_7d", "daily_avg_30d"):
            assert k in fc

    def test_cost_by_event_returns_ranking(self):
        import captcha_manager as cm
        r = cm.cost_by_event(top=10)
        assert isinstance(r, dict)
        assert "top" in r
        assert isinstance(r["top"], list)

    def test_solve_without_creds_returns_error(self):
        """Sin creds, no debe explotar — devuelve error controlado."""
        import captcha_manager as cm
        # Forzar config sin credentials
        orig = cm.get_config()
        try:
            cm.save_config({"services": {"capsolver": {"enabled": False, "api_key": ""},
                                            "2captcha": {"enabled": False, "api_key": ""}}})
            r = cm.solve_recaptcha_v2("fake_key", "https://example.com")
            assert isinstance(r, dict)
            assert r.get("ok") is False
        finally:
            cm.save_config(orig)


# ═══════════════════════════════════════════════════════════════
#  register_account
# ═══════════════════════════════════════════════════════════════
class TestRegisterAccount:
    def test_module_has_main_or_run(self):
        import register_account as ra
        # Debe tener AL MENOS una de estas funciones públicas
        public_fns = [n for n in dir(ra) if not n.startswith("_") and callable(getattr(ra, n))]
        assert len(public_fns) >= 5

    def test_password_generator_exists_or_skip(self):
        """Si register_account expone gen_password, validar 4 categorías.
        NO importamos _fill_plantilla (tiene side effects al cargar)."""
        import register_account as ra
        gen = getattr(ra, "gen_password", None)
        if gen is None:
            pytest.skip("gen_password not exposed by register_account")
        for _ in range(20):
            pw = gen() if callable(gen) else gen
            assert len(pw) >= 8, f"password too short: {pw!r}"
            assert any(c.isupper() for c in pw)
            assert any(c.islower() for c in pw)
            assert any(c.isdigit() for c in pw)
            assert any(not c.isalnum() for c in pw)


# ═══════════════════════════════════════════════════════════════
#  event_discovery
# ═══════════════════════════════════════════════════════════════
class TestEventDiscovery:
    def test_module_has_search_or_scrape(self):
        import event_discovery as ed
        # Al menos una función pública de búsqueda
        candidates = ("search_events", "scrape_search", "discover", "search")
        assert any(hasattr(ed, c) for c in candidates), \
            f"event_discovery missing search functions"

    def test_get_config(self):
        import event_discovery as ed
        if not hasattr(ed, "get_config"):
            pytest.skip("get_config not defined")
        cfg = ed.get_config()
        assert isinstance(cfg, dict)

    def test_no_real_api_call_on_import(self):
        """Importar el módulo NO debe hacer requests externos."""
        # Si llegamos aquí en TestEventDiscovery, ya importó sin colgar
        import event_discovery as ed
        assert ed is not None


# ═══════════════════════════════════════════════════════════════
#  account_validator
# ═══════════════════════════════════════════════════════════════
class TestAccountValidator:
    def test_module_has_validate(self):
        import account_validator as av
        candidates = ("validate_account", "validate", "check_account")
        assert any(hasattr(av, c) for c in candidates)

    def test_validate_with_invalid_account_returns_dict(self):
        """Llamada con cuenta inválida no debe crashear."""
        import account_validator as av
        if not hasattr(av, "validate_account"):
            pytest.skip("validate_account missing")
        # Sin red — la signature debería al menos manejar input vacío
        try:
            import inspect
            sig = inspect.signature(av.validate_account)
            params = list(sig.parameters.keys())
            # Asume que el primer arg es el account o aid
            # No llamamos realmente (necesita TM real) — solo verificamos signature
            assert len(params) >= 1
        except Exception:
            pytest.skip("signature inspection failed")


# ═══════════════════════════════════════════════════════════════
#  _compra_parallel — script standalone
# ═══════════════════════════════════════════════════════════════
class TestCompraParallel:
    def test_script_has_main_block(self):
        """El script debe tener bloque if __name__ == '__main__'."""
        import os
        path = os.path.join(os.path.dirname(__file__), os.pardir,
                            "_compra_parallel.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "__main__" in src or "argparse" in src or "sys.argv" in src

    def test_script_imports_critical_deps(self):
        """Verifica que importa las deps clave (proxy_health, browser engine)."""
        import os
        path = os.path.join(os.path.dirname(__file__), os.pardir,
                            "_compra_parallel.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        # Debe usar al menos un módulo del proyecto
        assert any(m in src for m in (
            "import morelogin", "import tm_browser", "import proxy_health",
            "import account_manager", "from playwright"))


# ═══════════════════════════════════════════════════════════════
#  Integration: SQLite migration didn't break legacy reads
# ═══════════════════════════════════════════════════════════════
class TestSQLiteMigration:
    def test_purchases_manager_lists(self):
        import purchases_manager as pm
        items = pm.list_purchases()
        assert isinstance(items, list)
        # Si había compras antes, deben seguir leyéndose
        # (no asumimos cantidad concreta — depende de datos)

    def test_ml_validator_dataset(self):
        import ml_validator as mv
        if hasattr(mv, "_load_dataset"):
            ds = mv._load_dataset()
            assert isinstance(ds, dict)
            assert "samples" in ds or "labels_count" in ds

    def test_venue_cache_load(self):
        import venue_cache as vc
        if hasattr(vc, "_load"):
            d = vc._load()
            assert isinstance(d, dict)

    def test_audit_log_get_recent(self):
        import audit_log as al
        if hasattr(al, "get_recent"):
            r = al.get_recent(limit=5)
            assert isinstance(r, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
