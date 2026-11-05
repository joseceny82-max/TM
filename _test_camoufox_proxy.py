"""Test minimal: verificar que Camoufox aplica el proxy Quantum."""
import random
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
    "proxy": {
        "server": "http://residential.quantumproxies.net:8888",
        "username": f"Quantum_927evhgx-cc-ES-pool-qntm-sessionid-{sessid}-sesstime-60",
        "password": "ycp4e57m",
    },
}

print("proxy server:", cf_kwargs["proxy"]["server"])
print("proxy username:", cf_kwargs["proxy"]["username"])

with Camoufox(**cf_kwargs) as b:
    ctx = b.contexts[0] if b.contexts else b.new_context(no_viewport=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    # Test 1: IP via api.ipify.org
    page.goto("https://api.ipify.org?format=json", timeout=20000)
    page.wait_for_timeout(2000)
    body = page.evaluate('() => document.body.innerText')
    print(f"[Camoufox] IP detected: {body}")

    # Test 2: IP via httpbin.org
    page.goto("https://httpbin.org/ip", timeout=20000)
    page.wait_for_timeout(2000)
    body2 = page.evaluate('() => document.body.innerText')
    print(f"[Camoufox] httpbin: {body2}")

    # Test 3: TM check
    page.goto("https://www.ticketmaster.es/", timeout=30000)
    page.wait_for_timeout(5000)
    print(f"[Camoufox] TM home url: {page.url}")
    print(f"[Camoufox] TM body chars: {len(page.evaluate('() => document.body.innerText'))}")

    page.wait_for_timeout(3000)
