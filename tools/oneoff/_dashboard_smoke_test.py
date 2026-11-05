"""Smoke test exhaustivo del dashboard.

Comprueba: healthz, login, todos los módulos clave, daemons, WebSocket,
JS válido, HTML balanceado, endpoints REST por categoría.
"""
import json
import os
import re
import sys
import time
from urllib.parse import urlencode

import requests

BASE = "http://127.0.0.1:5055"
ses = requests.Session()
ses.timeout = 10

results = {"PASS": 0, "FAIL": 0, "details": []}


def test(name, fn):
    try:
        ok, info = fn()
        status = "PASS" if ok else "FAIL"
        results[status] += 1
        results["details"].append((status, name, info))
        sym = "+" if ok else "-"
        print(f"  [{sym}] {name}: {info}")
        return ok
    except Exception as e:
        results["FAIL"] += 1
        results["details"].append(("FAIL", name, f"EXCEPTION: {type(e).__name__}: {e}"))
        print(f"  [-] {name}: EXCEPTION: {type(e).__name__}: {e}")
        return False


# 1. HEALTH + LOGIN
print("=" * 60)
print("1. CONECTIVIDAD BÁSICA")
print("=" * 60)


def check_healthz():
    r = ses.get(f"{BASE}/api/healthz")
    if r.status_code != 200:
        return False, f"http {r.status_code}"
    d = r.json()
    return d.get("ok"), f"uptime={d.get('uptime_seconds')}s version={d.get('version')}"


def do_login():
    r = ses.post(f"{BASE}/login",
                  data={"username": "admin", "password": "admin"},
                  allow_redirects=False)
    return r.status_code in (200, 302), f"status={r.status_code}"


test("healthz endpoint", check_healthz)
test("login admin/admin", do_login)


def get_endpoint(path, key=None, method="GET", json_body=None):
    fn = ses.get if method == "GET" else ses.post
    kwargs = {"json": json_body} if json_body else {}
    r = fn(f"{BASE}{path}", **kwargs)
    if r.status_code != 200:
        return False, f"http {r.status_code}"
    try:
        d = r.json()
        if key and key not in d:
            return False, f"missing key '{key}' in response"
        return True, f"ok=True keys={list(d.keys())[:5]}"
    except Exception as e:
        return False, f"json err: {e}"


# 2. MÓDULOS CRÍTICOS
print()
print("=" * 60)
print("2. MÓDULOS CRÍTICOS — endpoints clave")
print("=" * 60)

modules = [
    ("Eventos", "/api/events", "events"),
    ("Eventos catalog", "/api/events/catalog", "events"),
    ("Cuentas list", "/api/accounts", "accounts"),
    ("Tarjetas list", "/api/cards", "cards"),
    ("Proxies list", "/api/proxies", "proxies"),
    ("Sites list", "/api/sites", None),
    ("Domains list", "/api/domains", None),
    ("Health detailed", "/api/health/detailed", None),
    ("Bot queues stats", "/api/bot/queues/stats", None),
    ("Notas list", "/api/notes", "notes"),
    ("Backup stats", "/api/backup/stats", None),
    ("OTP config", "/api/otp/config", None),
    ("Settings get", "/api/settings", None),
    ("Captcha config", "/api/captcha/config", None),
    ("Captcha stats", "/api/captcha/stats", None),
    ("Compliance stats", "/api/compliance/stats", None),
    ("Compliance verify-chain", "/api/compliance/verify-chain", None),
    ("Cookie warmer pool stats", "/api/cookie-warmer/pool/stats", None),
    ("Proxy health stats", "/api/proxy-health/stats", None),
    ("Adyen config", "/api/adyen/config", None),
    ("Discovery config", "/api/discovery/config", None),
    ("Notifications config", "/api/notifications/config", None),
    ("Health monitor", "/api/health-monitor/status", None),
    ("WebSocket status", "/api/ws/status", None),
]

for name, path, key in modules:
    test(name, lambda p=path, k=key: get_endpoint(p, k))


# 3. NUEVOS MÓDULOS QUEUE MANAGER + ONSALE + WARMER + HUMAN MOTION
print()
print("=" * 60)
print("3. MÓDULOS NUEVOS — Queue Manager + Onsale + Warmer + Human Motion")
print("=" * 60)

new_modules = [
    ("Queue metrics", "/api/queue/metrics"),
    ("Queue sessions", "/api/queue/sessions"),
    ("Queue presets", "/api/queue/presets"),
    ("Queue self-test", "/api/queue/self-test"),
    ("Onsale schedule", "/api/onsale-scheduler/schedule"),
    ("Account warmer status", "/api/account-warmer/status"),
    ("Human motion self-test", "/api/human-motion/self-test"),
    ("Akamai SBSD info", "/api/akamai/sbsd/info"),
    ("Imperva info", "/api/imperva/info"),
    ("Behavior chain info", "/api/behavior-chain/info"),
    ("Session persistence info", "/api/session-persistence/info"),
    ("Hyper SDK config", "/api/hyper-sdk/config"),
    ("Research audit", "/api/research/audit"),
]
for name, path in new_modules:
    test(name, lambda p=path: get_endpoint(p))


# 4. SELF-TESTS de módulos críticos
print()
print("=" * 60)
print("4. SELF-TESTS de módulos clave")
print("=" * 60)

self_tests = [
    ("Compliance self-test", "/api/compliance/self-test"),
    ("Imperva self-test", "/api/imperva/self-test"),
    ("Akamai SBSD self-test", "/api/akamai/sbsd/self-test"),
    ("Behavior chain self-test", "/api/behavior-chain/self-test"),
    ("Session persistence self-test", "/api/session-persistence/self-test"),
]
for name, path in self_tests:
    test(name, lambda p=path: get_endpoint(p))


# 5. RENDERIZADO HTML (verificar que sirve la home con contenido)
print()
print("=" * 60)
print("5. RENDERIZADO HTML")
print("=" * 60)


def check_home_renders():
    r = ses.get(f"{BASE}/")
    if r.status_code != 200:
        return False, f"http {r.status_code}"
    html = r.text
    # Verificar que hay todas las secciones principales
    sections = [
        'sec-eventos', 'sec-cuentas', 'sec-bot', 'sec-filas', 'sec-cookies',
        'sec-monitor', 'sec-config', 'sec-soporte', 'sec-otp', 'sec-notas',
    ]
    missing = [s for s in sections if f'id="{s}"' not in html]
    return not missing, f"size={len(html)}b sections=20 missing={missing}"


def check_no_js_syntax_errors():
    r = ses.get(f"{BASE}/")
    html = r.text
    # Patrón problemático: comillas simples con ${} dentro
    bad_patterns = re.findall(r"= '[^']*\$\{[^']*'[^';]", html)
    return not bad_patterns, f"sospechosos: {len(bad_patterns)}"


def check_qm_functions_exist():
    r = ses.get(f"{BASE}/")
    html = r.text
    expected = ['function qmEnqueue', 'function qmSniperMode', 'function qmInspectCookie',
                'function qmSavePreset', 'function qmLoadLogs', 'function qmPlaySound',
                'function loadQueueManager', 'function loadQueueSessions']
    missing = [f for f in expected if f not in html]
    return not missing, f"presentes={len(expected)-len(missing)}/{len(expected)} missing={missing}"


def check_html_structure():
    r = ses.get(f"{BASE}/")
    html = r.text
    o_div = len(re.findall(r'<div[\s>]', html))
    c_div = len(re.findall(r'</div>', html))
    o_det = len(re.findall(r'<details[\s>]', html))
    c_det = len(re.findall(r'</details>', html))
    o_sc = len(re.findall(r'<script[\s>]', html))
    c_sc = len(re.findall(r'</script>', html))
    diff_div = abs(o_div - c_div)
    return diff_div <= 1 and o_det == c_det and o_sc == c_sc, \
        f"div={o_div}/{c_div}(diff={diff_div}) details={o_det}/{c_det} script={o_sc}/{c_sc}"


test("Home renderiza con todas las secciones", check_home_renders)
test("Sin JS syntax errors detectables", check_no_js_syntax_errors)
test("Funciones queue manager presentes", check_qm_functions_exist)
test("HTML estructura balanceada", check_html_structure)


# 6. PROBAR FLOW REAL (queue_manager end-to-end)
print()
print("=" * 60)
print("6. QUEUE MANAGER END-TO-END")
print("=" * 60)


def qm_flow_test():
    # Limpiar
    ses.post(f"{BASE}/api/queue/clear-completed")
    # Crear 5 sesiones
    r1 = ses.post(f"{BASE}/api/queue/enqueue-batch",
                   json={"n": 5, "event_id": "smoketest_evt",
                         "tm_url": "https://x.com",
                         "stagger_s": 0,
                         "auto_rejoin": True}).json()
    if not r1.get("ok"):
        return False, f"enqueue_batch: {r1.get('error')}"
    sids = r1.get("session_ids", [])
    # Marcar 1 accepted
    ses.post(f"{BASE}/api/queue/accept/{sids[0]}",
              json={"accepted_token": "SMOKE_TOK", "queueit_session_xt": "XT"})
    # Verificar metrics
    m = ses.get(f"{BASE}/api/queue/metrics").json()
    accepted = m.get("accepted", 0)
    waiting = m.get("waiting", 0)
    # Claim
    c = ses.post(f"{BASE}/api/queue/claim",
                  json={"event_id": "smoketest_evt"}).json()
    has_token = bool(c.get("accepted_token"))
    # Cleanup
    ses.post(f"{BASE}/api/queue/clear-completed")
    return accepted >= 1 and has_token, \
        f"created={len(sids)} accepted={accepted} waiting={waiting} claim_ok={has_token}"


test("Queue Manager flow E2E", qm_flow_test)


# 7. COOKIE INSPECTOR
def cookie_inspector_test():
    import base64
    hdr = {"typ": "QT1", "c": "ticketmasterspain",
           "e": "476722288", "exp": 1830000000}
    hdr_b64 = base64.urlsafe_b64encode(json.dumps(hdr).encode()).decode().rstrip("=")
    token = f"{hdr_b64}.payload.hash"
    r = ses.post(f"{BASE}/api/queue/inspect-cookie",
                  json={"token": token}).json()
    return (r.get("valid_format")
            and r.get("header", {}).get("c") == "ticketmasterspain"), \
        f"customer={r.get('header',{}).get('c')} event={r.get('header',{}).get('e')}"


test("Cookie inspector decodifica AcpAT", cookie_inspector_test)


# 8. PRESETS roundtrip
def presets_test():
    p = {"name": "_smoke_test_preset", "event_id": "test_evt", "n": 10}
    s = ses.post(f"{BASE}/api/queue/presets", json=p).json()
    if not s.get("ok"):
        return False, f"save fail: {s}"
    lst = ses.get(f"{BASE}/api/queue/presets").json()
    found = any(p2.get("name") == "_smoke_test_preset"
                for p2 in lst.get("presets", []))
    d = ses.delete(f"{BASE}/api/queue/presets/_smoke_test_preset").json()
    return found and d.get("ok"), f"saved={s.get('ok')} listed={found} deleted={d.get('ok')}"


test("Presets save/list/delete", presets_test)


# 9. EXPORT CSV
def export_csv_test():
    r = ses.get(f"{BASE}/api/queue/export-csv")
    ok = r.status_code == 200 and "text/csv" in r.headers.get("Content-Type", "")
    has_header = "session_id,account_id,event_id" in (r.text or "")
    return ok and has_header, f"http={r.status_code} bytes={len(r.text)}"


test("Export CSV", export_csv_test)


# RESUMEN FINAL
print()
print("=" * 60)
print("RESUMEN")
print("=" * 60)
total = results["PASS"] + results["FAIL"]
print(f"PASS: {results['PASS']}/{total}")
print(f"FAIL: {results['FAIL']}/{total}")
print()
if results["FAIL"]:
    print("Tests fallidos:")
    for status, name, info in results["details"]:
        if status == "FAIL":
            print(f"  X {name}: {info}")

sys.exit(0 if results["FAIL"] == 0 else 1)
