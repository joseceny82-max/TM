"""tests/test_purchase_gate_observability.py — M13.3 read-only endpoint tests.

Verifica que GET /api/diagnostics/purchase_gate:
    1. Devuelve 200 + JSON parseable
    2. Schema cumple con el contrato M13.3
    3. Es read-only (mutaciones cero sobre gate state)
    4. Counters reflejan gate.stats() consistentemente
    5. Es idempotente (calls consecutivas → estado idéntico)
    6. Es thread-safe bajo polling concurrente
"""
import threading
import time

import pytest


@pytest.fixture
def app_with_diagnostics(tmp_path, monkeypatch):
    """Flask app con el blueprint de diagnostics registrado."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    from flask import Flask
    from core.dashboard import register_diagnostics
    from core.diagnostics.purchase_gate import reset_default_gate_for_test

    reset_default_gate_for_test()
    app = Flask(__name__)
    register_diagnostics(app)
    yield app
    reset_default_gate_for_test()


@pytest.fixture
def client(app_with_diagnostics):
    return app_with_diagnostics.test_client()


# ════════════════════════════════════════════════════════════════════
#  1. Endpoint availability + 200
# ════════════════════════════════════════════════════════════════════
def test_m13_3_endpoint_returns_200(client):
    """GET /api/diagnostics/purchase_gate → 200 con JSON parseable."""
    r = client.get("/api/diagnostics/purchase_gate")
    assert r.status_code == 200
    data = r.get_json()
    assert data is not None
    assert data["module"] == "purchase_gate"


def test_m13_3_endpoint_registered_in_blueprint(app_with_diagnostics):
    """La ruta está registrada en el blueprint diagnostics."""
    rules = {str(r) for r in app_with_diagnostics.url_map.iter_rules()}
    assert "/api/diagnostics/purchase_gate" in rules


# ════════════════════════════════════════════════════════════════════
#  2. Schema validation
# ════════════════════════════════════════════════════════════════════
def test_m13_3_schema_structure_complete(client):
    """Response tiene las 7 top-level keys del contrato."""
    r = client.get("/api/diagnostics/purchase_gate")
    data = r.get_json()
    required_keys = {
        "module", "timestamp", "config", "counters",
        "rates", "denial_breakdown", "cache_state",
    }
    missing = required_keys - set(data.keys())
    assert not missing, f"Schema missing keys: {missing}"


def test_m13_3_config_fields_complete(client):
    """config tiene mode + global_enabled + thresholds."""
    r = client.get("/api/diagnostics/purchase_gate")
    data = r.get_json()
    config = data["config"]
    assert "mode" in config
    assert "global_enabled" in config
    assert "thresholds" in config
    # mode debe ser un valor válido del enum-like
    assert config["mode"] in ("disabled", "dry_run", "live")
    # global_enabled debe ser bool
    assert isinstance(config["global_enabled"], bool)
    # thresholds dict tiene los 8 keys esperados
    expected_thresholds = {
        "mismatch_rate_threshold", "critical_crossing_rate_threshold",
        "max_checkout_lifetime_s", "min_cooldown_s",
        "max_retries", "min_health_score",
        "min_consistency", "max_inflation_penalty",
    }
    assert expected_thresholds.issubset(config["thresholds"].keys())


def test_m13_3_counters_block_has_required_keys(client):
    """counters tiene los 6 production counters."""
    r = client.get("/api/diagnostics/purchase_gate")
    counters = r.get_json()["counters"]
    required = {
        "purchase_attempted_count", "purchase_allowed_count",
        "purchase_blocked_count", "purchase_dry_run_count",
        "purchase_live_count", "internal_errors",
    }
    assert required.issubset(counters.keys())
    # Todos los counters son ints
    for k in required:
        assert isinstance(counters[k], int)


def test_m13_3_rates_block_complete(client):
    """rates tiene los 4 derived rates como floats."""
    r = client.get("/api/diagnostics/purchase_gate")
    rates = r.get_json()["rates"]
    required = {
        "purchase_allow_rate", "purchase_block_rate",
        "purchase_dry_run_rate", "purchase_live_rate",
    }
    assert required.issubset(rates.keys())
    for k in required:
        assert isinstance(rates[k], (int, float))


def test_m13_3_denial_breakdown_complete(client):
    """denial_breakdown tiene los 13 deny reason counters."""
    r = client.get("/api/diagnostics/purchase_gate")
    denial = r.get_json()["denial_breakdown"]
    required = {
        "kill_switch_denials", "mode_disabled_denials",
        "duplicate_prevented_count", "stale_checkout_denials",
        "cooldown_denials", "concurrent_denials",
        "health_denials", "mismatch_purchase_denials",
        "critical_crossing_denials", "consistency_denials",
        "inflation_denials", "retry_denials", "invalid_input_denials",
    }
    assert required.issubset(denial.keys())


def test_m13_3_cache_state_present(client):
    """cache_state expone idempotency_size + session_locks_count."""
    r = client.get("/api/diagnostics/purchase_gate")
    cache = r.get_json()["cache_state"]
    assert "idempotency_size" in cache
    assert "session_locks_count" in cache
    assert isinstance(cache["idempotency_size"], int)
    assert isinstance(cache["session_locks_count"], int)


def test_m13_3_timestamp_is_iso8601(client):
    """timestamp es ISO8601 UTC."""
    r = client.get("/api/diagnostics/purchase_gate")
    ts = r.get_json()["timestamp"]
    assert isinstance(ts, str)
    # ISO8601 UTC tiene 'T' y termina con '+00:00' o 'Z'
    assert "T" in ts
    assert ts.endswith("+00:00") or ts.endswith("Z")


# ════════════════════════════════════════════════════════════════════
#  3. Consistency vs gate.stats()
# ════════════════════════════════════════════════════════════════════
def test_m13_3_counters_consistency_with_gate_stats(client):
    """Endpoint counters coinciden con gate.stats() para keys clave."""
    from core.diagnostics.purchase_gate import get_default_gate
    gate = get_default_gate()
    expected_stats = gate.stats()

    r = client.get("/api/diagnostics/purchase_gate")
    data = r.get_json()
    counters = data["counters"]
    for k in counters:
        assert counters[k] == expected_stats.get(k, 0), (
            f"Counter mismatch on {k}: endpoint={counters[k]}, "
            f"gate.stats()={expected_stats.get(k)}"
        )


def test_m13_3_reflects_gate_state_changes_passively(client):
    """Tras un evaluate(), los counters del endpoint reflejan el cambio."""
    from core.diagnostics.purchase_gate import get_default_gate

    # Snapshot inicial
    r0 = client.get("/api/diagnostics/purchase_gate")
    attempted_before = r0.get_json()["counters"]["purchase_attempted_count"]

    # Trigger 1 evaluation (gate is in default state → DENY pero counter sube)
    gate = get_default_gate()
    gate.evaluate(
        session_id="m13_3_test",
        account_id="m13_3@test.com",
        event_id="EVT-1",
        qty=1,
        checkout_opened_at_monotonic=time.monotonic(),
    )

    # Snapshot post
    r1 = client.get("/api/diagnostics/purchase_gate")
    attempted_after = r1.get_json()["counters"]["purchase_attempted_count"]

    assert attempted_after == attempted_before + 1


# ════════════════════════════════════════════════════════════════════
#  4. Immutability — endpoint is read-only
# ════════════════════════════════════════════════════════════════════
def test_m13_3_immutable_across_consecutive_calls(client):
    """2 calls consecutivos sin actividad → mismos counters."""
    r1 = client.get("/api/diagnostics/purchase_gate").get_json()
    r2 = client.get("/api/diagnostics/purchase_gate").get_json()
    # Counters idénticos (no se incrementaron por la llamada al endpoint)
    assert r1["counters"] == r2["counters"]
    assert r1["denial_breakdown"] == r2["denial_breakdown"]
    assert r1["cache_state"]["idempotency_size"] == r2["cache_state"]["idempotency_size"]


def test_m13_3_endpoint_does_not_mutate_gate_state(client):
    """5 GETs consecutivos → gate.stats() pre==post."""
    from core.diagnostics.purchase_gate import get_default_gate
    gate = get_default_gate()
    stats_before = gate.stats()
    for _ in range(5):
        client.get("/api/diagnostics/purchase_gate")
    stats_after = gate.stats()
    # Todos los counters deben ser idénticos (endpoint no muta)
    for k in stats_before:
        assert stats_before[k] == stats_after[k], (
            f"Gate state mutated by endpoint: {k} changed from "
            f"{stats_before[k]} to {stats_after[k]}"
        )


# ════════════════════════════════════════════════════════════════════
#  5. Thread safety — concurrent polling
# ════════════════════════════════════════════════════════════════════
def test_m13_3_concurrent_get_safe(app_with_diagnostics):
    """20 threads polling el endpoint en paralelo → todos 200, sin crash."""
    results = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker():
        local_client = app_with_diagnostics.test_client()
        barrier.wait()
        r = local_client.get("/api/diagnostics/purchase_gate")
        with results_lock:
            results.append((r.status_code, r.get_json() is not None))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 20
    # Todos 200 + JSON válido
    assert all(status == 200 for status, _ in results)
    assert all(json_ok for _, json_ok in results)


def test_m13_3_concurrent_get_does_not_inflate_attempted(app_with_diagnostics):
    """Polling concurrente NO infla purchase_attempted_count del gate."""
    from core.diagnostics.purchase_gate import get_default_gate
    gate = get_default_gate()
    before = gate.stats()["purchase_attempted_count"]

    def worker():
        local_client = app_with_diagnostics.test_client()
        local_client.get("/api/diagnostics/purchase_gate")

    threads = [threading.Thread(target=worker) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    after = gate.stats()["purchase_attempted_count"]
    # GETs del endpoint NO son evaluates → no incrementan attempted_count
    assert after == before


# ════════════════════════════════════════════════════════════════════
#  6. Fail-safe — gate unavailable
# ════════════════════════════════════════════════════════════════════
def test_m13_3_returns_error_dict_when_gate_unavailable(
    app_with_diagnostics, monkeypatch
):
    """Si gate.stats() lanza, endpoint devuelve dict con 'error' key (no 5xx)."""
    from core.diagnostics import purchase_gate as pg_module

    class BrokenGate:
        @property
        def stats(self):
            raise RuntimeError("simulated_stats_failure")

    original = pg_module.get_default_gate
    pg_module.get_default_gate = lambda: BrokenGate()
    try:
        client = app_with_diagnostics.test_client()
        r = client.get("/api/diagnostics/purchase_gate")
        # Patrón consistente con /api/diagnostics/info: 200 + error dict
        assert r.status_code == 200
        data = r.get_json()
        assert data["module"] == "purchase_gate"
        assert "error" in data
        assert "simulated_stats_failure" in data["error"]
    finally:
        pg_module.get_default_gate = original
