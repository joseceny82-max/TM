"""Smoke test del Centro de Problemas."""
import requests, json
BASE = "http://127.0.0.1:5055"
ses = requests.Session()
ses.post(f"{BASE}/login", data={"username": "admin", "password": "admin"})

results = {"PASS": 0, "FAIL": 0}

def tst(name, ok, info=""):
    sym = "+" if ok else "-"
    if ok: results["PASS"] += 1
    else: results["FAIL"] += 1
    print(f"  [{sym}] {name}: {info}")

print("=" * 60)
print("CENTRO DE PROBLEMAS — smoke test")
print("=" * 60)

# 1. Listar problems
r = ses.get(f"{BASE}/api/problems")
ok = r.status_code == 200
data = r.json() if ok else {}
problems = data.get("problems", [])
stats = data.get("stats", {})
tst("GET /api/problems", ok, f"http={r.status_code} problems={len(problems)} stats_keys={list(stats.keys())}")

# 2. Listar con filtros
r2 = ses.get(f"{BASE}/api/problems?sev=critical&sort=recent")
tst("GET /api/problems?sev=critical&sort=recent", r2.status_code == 200,
    f"problems={len(r2.json().get('problems', []))}")

r3 = ses.get(f"{BASE}/api/problems?include_resolved=1")
tst("GET /api/problems?include_resolved=1", r3.status_code == 200,
    f"problems={len(r3.json().get('problems', []))}")

# 3. Reportar problema manual
r4 = ses.post(f"{BASE}/api/problems/report", json={
    "category": "test",
    "severity": "low",
    "title": "Smoke test problem",
    "description": "Esto es un problema de prueba que se va a auto-resolver",
})
ok = r4.status_code == 200 and r4.json().get("ok")
new_pid = r4.json().get("problem_id") or r4.json().get("id") or r4.json().get("problem", {}).get("id")
tst("POST /api/problems/report", ok, f"created pid={new_pid}")

# 4. Ver el nuevo problema en la lista
if new_pid:
    r5 = ses.get(f"{BASE}/api/problems")
    plist = r5.json().get("problems", [])
    found = any(p.get("id") == new_pid for p in plist)
    tst("Nuevo problema visible en lista", found, f"found={found}")

# 5. Resolver el problema manualmente
if new_pid:
    r6 = ses.post(f"{BASE}/api/problems/{new_pid}/resolve")
    tst("POST /api/problems/<pid>/resolve", r6.status_code == 200, f"resp={r6.json()}")

# 6. Auto-resolver low
r7 = ses.post(f"{BASE}/api/problems/auto-resolve")
ok = r7.status_code == 200
data7 = r7.json() if ok else {}
tst("POST /api/problems/auto-resolve", ok, f"resolved={data7.get('resolved', '?')}")

# 7. Auto-resolver stale
r8 = ses.post(f"{BASE}/api/problems/auto-resolve-stale")
ok = r8.status_code == 200
tst("POST /api/problems/auto-resolve-stale", ok, f"resolved={r8.json().get('resolved', '?')}")

# 8. Limpiar resueltos
r9 = ses.post(f"{BASE}/api/problems/clear")
ok = r9.status_code == 200
tst("POST /api/problems/clear", ok, f"removed={r9.json().get('removed', '?')}")

# 9. Resolve-batch
r10 = ses.post(f"{BASE}/api/problems/resolve-batch", json={"ids": []})
tst("POST /api/problems/resolve-batch (empty)", r10.status_code == 200, f"resp={r10.json()}")

# 10. Stats global
r11 = ses.get(f"{BASE}/api/problems")
data11 = r11.json()
stats_keys = list(data11.get("stats", {}).keys())
tst("Stats keys presentes", set(["total", "open", "categories"]).issubset(stats_keys),
    f"keys={stats_keys}")

# 11. Verificar timeline 24h
r12 = ses.get(f"{BASE}/api/problems")
timeline = r12.json().get("timeline_24h", [])
tst("timeline_24h presente", isinstance(timeline, list), f"len={len(timeline)}")

print()
print("=" * 60)
print(f"PASS: {results['PASS']}/{results['PASS']+results['FAIL']}")
print(f"FAIL: {results['FAIL']}/{results['PASS']+results['FAIL']}")
print("=" * 60)
