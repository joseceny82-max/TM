"""Lista TODOS los botones/links visibles del DOM con su texto+bbox."""
import sys, io, os, json, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
from playwright.sync_api import sync_playwright
PORT = 60937
v = requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=8).json()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
    ctx = b.contexts[0]
    page = next((p for p in ctx.pages if 'ticketmaster' in (p.url or '').lower()), ctx.pages[0])
    page.bring_to_front()
    print("URL:", page.url)
    print("TITLE:", page.title()[:100])
    # Toma screenshot full-page
    out = os.path.join(os.path.dirname(__file__), "datos", "compra_test", "_inspect_full.png")
    try: page.screenshot(path=out, full_page=True); print(f"Screenshot full-page: {out}")
    except Exception as e: print(f"shoot err: {e}")
    # Listar todos botones con texto (no nav header)
    btns = page.evaluate("""
        () => {
            const out = [];
            document.querySelectorAll('button, a[role="button"], [role="button"]').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 30 || r.height < 15) return;
                const txt = (el.innerText || el.textContent || '').trim().slice(0,80);
                if (!txt) return;
                out.push({txt, x:Math.round(r.x), y:Math.round(r.y+window.scrollY), w:Math.round(r.width), h:Math.round(r.height),
                          testid: el.getAttribute('data-testid') || '',
                          dataBdd: el.getAttribute('data-bdd') || '',
                          ariaLabel: el.getAttribute('aria-label') || '',
                          tag: el.tagName.toLowerCase(),
                          disabled: el.disabled || false,
                          visible: r.width>0 && r.height>0,
                });
            });
            return out;
        }
    """)
    print(f"\nTotal botones DOM (>=30x15): {len(btns)}\n")
    # Ordenar por y desc (los CTA suelen estar abajo)
    for b in sorted(btns, key=lambda x: -x['y'])[:25]:
        print(f"  y={b['y']:5d} x={b['x']:5d} w={b['w']:3d} h={b['h']:3d} testid={b['testid'][:25]:25} bdd={b['dataBdd'][:20]:20} | {b['txt']}")
