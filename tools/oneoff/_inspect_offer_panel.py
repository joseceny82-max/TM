"""Inspecciona el panel de oferta tras 'Ver lo mejor disponible'."""
import sys, io, os, json, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
from playwright.sync_api import sync_playwright
v = requests.get("http://127.0.0.1:60937/json/version", timeout=8).json()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
    ctx = b.contexts[0]
    page = next((p for p in ctx.pages if 'ticketmaster' in (p.url or '').lower()), ctx.pages[0])
    page.bring_to_front()
    print("URL:", page.url)
    out = os.path.join(os.path.dirname(__file__), "datos", "compra_test", "_offer_inspect.png")
    page.screenshot(path=out, full_page=True)
    print("shot:", out)
    # Listar TODOS los elementos clicables en el panel offer
    data = page.evaluate("""
      () => {
        const out = [];
        // testid + aria + role
        document.querySelectorAll('button, [role="button"], [data-testid], [data-bdd]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 12 || r.height < 12) return;
            const t = (el.innerText||el.textContent||'').trim().slice(0,60);
            const tid = el.getAttribute('data-testid') || '';
            const bdd = el.getAttribute('data-bdd') || '';
            const al = el.getAttribute('aria-label') || '';
            // Solo los relevantes para stepper / cta
            if (tid || bdd || /quantity|increment|increase|aumentar|stepper|añadir|comprar|continuar|pagar|ticket/i.test(tid+bdd+al+t)) {
                out.push({tag:el.tagName.toLowerCase(), tid, bdd, al, txt:t,
                          y:Math.round(r.y+window.scrollY), x:Math.round(r.x), w:Math.round(r.width), h:Math.round(r.height),
                          dis: el.disabled || false});
            }
        });
        return out;
      }
    """)
    print(f"Total relevant elements: {len(data)}")
    for d in sorted(data, key=lambda x: x['y'])[:40]:
        print(f"  y={d['y']:5d} x={d['x']:5d} w={d['w']:3d} h={d['h']:2d} {d['tag']:8} tid={d['tid'][:35]:35} bdd={d['bdd'][:20]:20} al={d['al'][:25]:25} txt={d['txt']!r}")
