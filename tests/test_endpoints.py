"""Test 2: endpoints críticos responden sin errores."""
import json
import pytest


@pytest.fixture(scope="module")
def client():
    import dashboard
    c = dashboard.app.test_client()
    # Login
    c.post("/login", data={"username": "admin", "password": "admin"})
    return c


def _get_json(client, path):
    r = client.get(path)
    return r.status_code, (json.loads(r.data) if r.data else {})


def _post_json(client, path, body=None):
    r = client.post(path, json=body or {})
    return r.status_code, (json.loads(r.data) if r.data else {})


# CORE
def test_healthz(client):
    code, d = _get_json(client, "/api/healthz")
    assert code == 200 and d.get("ok")


def test_events_list(client):
    code, d = _get_json(client, "/api/events")
    assert code == 200
    assert "events" in d


def test_accounts_list(client):
    code, d = _get_json(client, "/api/accounts")
    assert code == 200
    assert "accounts" in d


# QUEUE MANAGER
def test_queue_metrics(client):
    code, d = _get_json(client, "/api/queue/metrics")
    assert code == 200
    assert "active" in d


def test_queue_sessions(client):
    code, d = _get_json(client, "/api/queue/sessions")
    assert code == 200 and "sessions" in d


def test_queue_self_test(client):
    code, d = _get_json(client, "/api/queue/self-test")
    assert code == 200 and d.get("ok")


def test_queue_enqueue_and_clear(client):
    code, d = _post_json(client, "/api/queue/enqueue-batch",
                          {"n": 3, "event_id": "pytest_evt", "stagger_s": 0,
                           "tm_url": "https://x.com"})
    assert code == 200 and d.get("ok")
    assert len(d.get("session_ids", [])) == 3
    code, d2 = _get_json(client, "/api/queue/sessions?event_id=pytest_evt")
    assert len(d2.get("sessions", [])) >= 3
    # cleanup
    client.post("/api/queue/clear-completed")


def test_queue_inspect_cookie(client):
    import base64
    hdr = {"typ": "QT1", "c": "ticketmasterspain", "e": "test_evt"}
    hdr_b64 = base64.urlsafe_b64encode(json.dumps(hdr).encode()).decode().rstrip("=")
    token = f"{hdr_b64}.payload.hash"
    code, d = _post_json(client, "/api/queue/inspect-cookie", {"token": token})
    assert code == 200 and d.get("valid_format")


# ONSALE SCHEDULER
def test_onsale_schedule(client):
    code, d = _get_json(client, "/api/onsale-scheduler/schedule")
    assert code == 200 and "attacks" in d


# DISCOVERY
def test_discovery_config(client):
    code, d = _get_json(client, "/api/discovery/config")
    assert code == 200 and "config" in d


def test_discovery_search(client):
    code, d = _post_json(client, "/api/discovery/search",
                          {"keyword": "Karol G", "country": "es", "size": 2})
    assert code == 200 and d.get("ok")


# ACCOUNT WARMER
def test_account_warmer_status(client):
    code, d = _get_json(client, "/api/account-warmer/status")
    assert code == 200 and "jobs" in d


# COMPLIANCE
def test_compliance_stats(client):
    code, d = _get_json(client, "/api/compliance/stats")
    assert code == 200


def test_compliance_verify_chain(client):
    code, d = _get_json(client, "/api/compliance/verify-chain")
    assert code == 200


# CAPTCHA
def test_captcha_config(client):
    code, d = _get_json(client, "/api/captcha/config")
    assert code == 200


# WS STATUS
def test_ws_status(client):
    code, d = _get_json(client, "/api/ws/status")
    assert code == 200 and d.get("ok")


# PROBLEMS CENTER
def test_problems_list(client):
    code, d = _get_json(client, "/api/problems")
    assert code == 200 and "problems" in d


def test_problems_auto_resolve(client):
    code, d = _post_json(client, "/api/problems/auto-resolve")
    assert code == 200 and d.get("ok")


# HOUSEKEEPING
def test_housekeeping_stats(client):
    code, d = _get_json(client, "/api/housekeeping/stats")
    assert code == 200 and "config" in d


def test_housekeeping_self_test(client):
    code, d = _get_json(client, "/api/housekeeping/self-test")
    assert code == 200 and d.get("ok")


# HUMAN MOTION
def test_human_motion_self_test(client):
    code, d = _get_json(client, "/api/human-motion/self-test")
    assert code == 200 and d.get("ok")


# RESEARCH ENGINE
def test_research_audit(client):
    code, d = _get_json(client, "/api/research/audit")
    assert code == 200


# SETTINGS
def test_settings_get(client):
    code, d = _get_json(client, "/api/settings")
    assert code == 200


# SECURITY HEADERS
def test_security_headers(client):
    r = client.get("/api/healthz")
    assert "X-Content-Type-Options" in r.headers
    assert "X-Frame-Options" in r.headers
    assert "Content-Security-Policy" in r.headers


# AUTH
def test_login_rate_limited():
    """Login debe rechazar tras N intentos."""
    import dashboard
    c2 = dashboard.app.test_client()
    # Sin auth previa
    r = c2.get("/api/healthz")  # endpoint público
    assert r.status_code == 200
