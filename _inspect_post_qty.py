"""Inspect TODOS los elementos en el panel post-qty=4 incluyendo checkboxes,
labels, y el HTML del botón 'Continuar comprando' para ver por qué está disabled."""
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
    out = os.path.join(os.path.dirname(__file__), "datos", "compra_test", "_post_qty.png")
    page.screenshot(path=out, full_page=True)
    print("shot:", out)
    # Estado actual stepper
    qty = page.evaluate("""
      () => {
        const s = document.querySelectorAll('[data-testid="quantityStepper"]');
        return Array.from(s).map(x => x.innerText.trim());
      }
    """)
    print(f"steppers: {qty}")

    # Buscar TODOS los botones (habilitados y deshabilitados) con texto "Continuar"
    cont = page.evaluate("""
      () => {
        return Array.from(document.querySelectorAll('button')).map((el,i) => {
            const r = el.getBoundingClientRect();
            return {i, txt:(el.innerText||'').trim().slice(0,80), y:Math.round(r.y+window.scrollY), x:Math.round(r.x), w:Math.round(r.width), tid:el.getAttribute('data-testid')||'', dis:el.disabled, html: el.outerHTML.slice(0,300)};
        }).filter(b => /continuar|comprar|pagar|proceder|checkout|buy|next/i.test(b.txt) || b.tid.match(/buy|cart|checkout|continue/i));
      }
    """)
    print(f"\nBotones relacionados: {len(cont)}")
    for c in cont:
        print(f"  i={c['i']:3} y={c['y']:5} dis={str(c['dis']):5} txt={c['txt']!r}")
        print(f"      HTML: {c['html']}")

    # Checkboxes y labels de "Acepto"
    checks = page.evaluate("""
      () => {
        return Array.from(document.querySelectorAll('input[type="checkbox"], label')).map((el,i) => {
            const r = el.getBoundingClientRect();
            return {tag:el.tagName.toLowerCase(), txt:(el.innerText||el.textContent||'').trim().slice(0,80), y:Math.round(r.y+window.scrollY), checked: el.checked||false, name:el.getAttribute('name')||'', for:el.getAttribute('for')||''};
        }).filter(x => x.txt && x.txt.length>5);
      }
    """)
    print(f"\nCheckboxes/Labels: {len(checks)}")
    for c in checks[:20]:
        print(f"  {c['tag']:8} y={c['y']:5} checked={c['checked']} txt={c['txt']!r}")
