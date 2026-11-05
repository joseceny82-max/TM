"""Test integración MoreLogin (Chrome real) + Hyper Solutions Akamai.

Flow:
1. Pick profile MoreLogin ES con proxyCheck=1
2. Start profile via API
3. Conectar Playwright a CDP del profile
4. Navegar a TM event Karol G
5. Esperar 5s a que Akamai inyecte cookies _abck/bm_sz
6. Extraer cookies via ctx.cookies()
7. Llamar hyper_sdk_client.generate_akamai_sensor con cookies REALES
8. Verificar marker resultante (~0~ = human, ~-1~ = bot)
"""
import sys, json, time, requests as rq

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

MORELOGIN_API = "http://127.0.0.1:40000"

# 1. Pick primer profile ES con proxyCheck=1 + proxy Quantum (los Froxy fallan)
print("1. Listando perfiles MoreLogin ES Quantum...")
all_profiles = []
for pageNo in range(1, 50):
    r = rq.post(f"{MORELOGIN_API}/api/env/page",
                json={"pageNo": pageNo, "pageSize": 100}, timeout=10).json()
    items = r.get('data', {}).get('dataList', []) or []
    if not items: break
    all_profiles.extend(items)
    if len(items) < 100: break

profile = None
for p in all_profiles:
    pp = p.get('proxy') or {}
    if pp.get('countryCode') != 'ES': continue
    if pp.get('proxyCheckStatus') != 1: continue
    if 'quantumprox' not in (pp.get('proxyIp') or '').lower(): continue
    profile = p
    break

if not profile:
    print("   ❌ No hay perfil ES Quantum validado")
    sys.exit(1)

env_id = profile.get('id')
env_name = profile.get('envName')
proxy = profile.get('proxy', {})
print(f"   ✅ Profile: {env_name} (id={env_id})")
print(f"   Proxy: cc={proxy.get('countryCode')} ip={proxy.get('exportIp')}")

# 2. Start profile
print(f"\n2. Starting profile {env_name}...")
r = rq.post(f"{MORELOGIN_API}/api/env/start",
            json={"envId": env_id}, timeout=30).json()
print(f"   Response: {json.dumps(r, default=str)[:200]}")

data = r.get('data', {})
ws_endpoint = data.get('ws') or data.get('webSocketDebuggerUrl') or ''
debug_port = data.get('debugPort') or data.get('debuggingPort') or data.get('port')

if not ws_endpoint and not debug_port:
    print("   ❌ No CDP endpoint")
    sys.exit(1)

print(f"   debug_port: {debug_port}")
print(f"   ws_endpoint: {ws_endpoint[:80] if ws_endpoint else 'N/A'}")

# 3. Conectar Playwright via CDP
print(f"\n3. Connecting Playwright CDP...")
from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as pw:
        if ws_endpoint:
            browser = pw.chromium.connect_over_cdp(ws_endpoint)
        else:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(f"   ✅ Connected. Pages: {len(ctx.pages)}")

        # 4. Navegar a TM home primero (warming) → scroll → evento
        print(f"\n4. Warming TM home (warm-up para Akamai)...")
        page.goto("https://www.ticketmaster.es/",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        # Scroll humano (Akamai detecta interacción)
        for sy in (300, 700, 200):
            page.evaluate(f"window.scrollTo({{top:{sy},behavior:'smooth'}})")
            page.wait_for_timeout(1500)

        print(f"5. Cookies tras home + scroll:")
        cookies_home = ctx.cookies()
        print(f"   {len(cookies_home)} cookies. Names: {sorted({c['name'] for c in cookies_home})[:15]}")
        abck_h = next((c['value'] for c in cookies_home if c['name'] == '_abck'), '')
        print(f"   _abck home: {abck_h[:80] if abck_h else '[empty]'}")

        # Ahora event Karol G
        print(f"\n6. Navigating to Karol G event...")
        page.goto("https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour-entradas/476722288",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(7000)
        # Scroll page
        for sy in (400, 800, 200):
            page.evaluate(f"window.scrollTo({{top:{sy}}})")
            page.wait_for_timeout(1500)

        # 5. Extraer cookies
        cookies = ctx.cookies()
        print(f"\n7. Cookies post-evento: {len(cookies)}")
        cookie_names = sorted({c['name'] for c in cookies})
        print(f"   Names: {cookie_names}")
        abck = next((c['value'] for c in cookies if c['name'] == '_abck'), '')
        bmsz = next((c['value'] for c in cookies if c['name'] == 'bm_sz'), '')

        print(f"   _abck ({len(abck)}): {abck[:120]}")
        print(f"   bm_sz ({len(bmsz)}): {bmsz[:80]}")

        marker = "~0~" if "~0~" in abck else ("~-1~" if "~-1~" in abck else "?")
        print(f"   abck marker: {marker}")

        # Buscar DataDome en cookies (TM España usa DataDome, no Akamai)
        datadome_c = next((c['value'] for c in cookies if c['name'] == 'datadome'), '')
        print(f"\n   datadome cookie ({len(datadome_c)}): {datadome_c[:120]}")

        if not abck and datadome_c:
            print("\n   TM España usa DATADOME (no Akamai). Probando Hyper DataDome...")
            sys.path.insert(0, '.')
            import hyper_sdk_client as hc
            ua = page.evaluate("() => navigator.userAgent")

            # Llamar al endpoint DataDome de Hyper
            try:
                r = hc.generate_datadome(
                    target_url='https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour-entradas/476722288',
                    cookie=datadome_c,
                    user_agent=ua,
                ) if hasattr(hc, 'generate_datadome') else None
                print(f"   datadome result: {json.dumps(r, indent=2, default=str)[:600]}")
            except Exception as e:
                print(f"   datadome err: {type(e).__name__}: {e}")
            # Cleanup
            rq.post(f"{MORELOGIN_API}/api/env/close",
                    json={"envId": env_id}, timeout=10)
            sys.exit(0)
        elif not abck:
            print("\n   ❌ NO _abck NI datadome — TM no activó protección en este browser")
            rq.post(f"{MORELOGIN_API}/api/env/close",
                    json={"envId": env_id}, timeout=10)
            sys.exit(1)

        if marker == "~0~":
            print("\n   ✅ abck YA ES ~0~ (human) — TM aceptó sin Hyper")
            rq.post(f"{MORELOGIN_API}/api/env/close",
                    json={"envId": env_id}, timeout=10)
            sys.exit(0)

        # 6. Llamar Hyper Solutions
        print(f"\n6. Calling Hyper Solutions con cookies REALES...")
        ua = page.evaluate("() => navigator.userAgent")
        print(f"   UA: {ua[:80]}")

        sys.path.insert(0, '.')
        import hyper_sdk_client as hc

        result = hc.generate_akamai_sensor(
            abck=abck, bmsz=bmsz, user_agent=ua,
            page_url='https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour-entradas/476722288',
            post_count=3,
        )
        print(f"\n7. Hyper result:")
        print(json.dumps(result, indent=2, default=str)[:1500])

        if result.get('is_human'):
            print("\n🎯 ÉXITO: Hyper Solutions validó cookies → _abck = ~0~ (human)")
        else:
            print(f"\n⚠️ Hyper terminó pero marker = {result.get('abck_marker', '?')}")
            print(f"   posts_done: {result.get('posts_done')}")
            print(f"   posts: {result.get('post_results', [])}")

        # Cleanup
        page.wait_for_timeout(3000)
        rq.post(f"{MORELOGIN_API}/api/env/close",
                json={"envId": env_id}, timeout=10)
        print("\n   profile closed")
except Exception as e:
    print(f"\n❌ Error: {type(e).__name__}: {e}")
    try:
        rq.post(f"{MORELOGIN_API}/api/env/close",
                json={"envId": env_id}, timeout=10)
    except: pass
    raise
