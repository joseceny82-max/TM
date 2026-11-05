"""Test integración Hyper Solutions con Camoufox.

Flow:
1. Lanzar Camoufox + proxy Quantum
2. Navegar a TM home → obtener cookies _abck/bm_sz REALES
3. Llamar hyper_sdk.generate_akamai_sensor con esas cookies
4. POST sensor_data al endpoint TM via page.evaluate (fetch)
5. Verificar que _abck se actualiza a "~0~" (human marker)
"""
import sys, random, json, time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from camoufox.sync_api import Camoufox

sessid = random.randint(100000, 999999)
print(f"sessionid: {sessid}")

cf_kwargs = {
    "headless": False,
    "geoip": True,
    "locale": "es-ES",
    "humanize": True,
    "i_know_what_im_doing": True,
    "os": "windows",
    "window": (1280, 800),
    "disable_coop": True,
    "block_webrtc": True,
    "proxy": {
        "server": "http://residential.quantumproxies.net:8888",
        "username": f"Quantum_927evhgx-cc-ES-pool-qntm-sessionid-{sessid}-sesstime-60",
        "password": "ycp4e57m",
    },
}

with Camoufox(**cf_kwargs) as b:
    ctx = b.contexts[0] if b.contexts else b.new_context(no_viewport=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    print("\n1. Navigating to TM EVENT (Karol G) - Akamai active here...")
    page.goto("https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour-entradas/476722288",
              wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(7000)   # esperar Akamai inject _abck

    # Extraer todas las cookies
    cookies = ctx.cookies()
    print(f"\n2. Cookies received: {len(cookies)}")

    abck = next((c['value'] for c in cookies if c['name'] == '_abck'), '')
    bmsz = next((c['value'] for c in cookies if c['name'] == 'bm_sz'), '')

    print(f"   _abck ({len(abck)}): {abck[:120]}")
    print(f"   bm_sz ({len(bmsz)}): {bmsz[:80]}")

    if not abck:
        print("\n❌ NO _abck cookie received. Akamai not active or TM didn't serve it.")
        sys.exit(1)

    # Detectar marker actual del abck
    marker = "~0~" if "~0~" in abck else ("~-1~" if "~-1~" in abck else "?")
    print(f"   abck marker: {marker}")

    if marker == "~0~":
        print("\n✅ abck ALREADY ~0~ — TM lo aceptó sin Hyper (human marker).")
        sys.exit(0)

    # 3. Llamar Hyper Solutions con cookies REALES
    print(f"\n3. Calling Hyper Solutions with REAL cookies...")
    import hyper_sdk_client as hc

    user_agent = page.evaluate("() => navigator.userAgent")
    print(f"   browser UA: {user_agent[:80]}")

    result = hc.generate_akamai_sensor(
        abck=abck,
        bmsz=bmsz,
        user_agent=user_agent,
        page_url='https://www.ticketmaster.es/',
        post_count=3,
    )
    print(f"\n4. Hyper result:")
    print(json.dumps(result, indent=2, default=str)[:1500])

    if not result.get('ok'):
        print(f"\n❌ Hyper failed: {result.get('error')}")
        sys.exit(1)

    # 5. Verificar cookies actualizadas (Hyper hace POST internamente)
    print(f"\n5. Final marker: {result.get('abck_marker')}")
    print(f"   is_human: {result.get('is_human')}")
    print(f"   posts_done: {result.get('posts_done')}")

    if result.get('is_human'):
        print("\n🎯 SUCCESS: abck = ~0~ (human marker via Hyper)")
    else:
        print(f"\n⚠️ abck still ~-1~ after {result.get('posts_done')} POSTs")

    page.wait_for_timeout(5000)
