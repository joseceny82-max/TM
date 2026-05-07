"""Inspecciona el estado actual del browser P-79756 (port 61737)."""
import sys, os, json, requests
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')
from playwright.sync_api import sync_playwright

v = requests.get('http://127.0.0.1:61737/json/version', timeout=5).json()
ws = v['webSocketDebuggerUrl']

with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(ws)
    ctx = b.contexts[0]
    print(f'Tabs ({len(ctx.pages)}):')
    for i, p in enumerate(ctx.pages):
        print(f'  [{i}] {p.url}')
    page = ctx.pages[0] if ctx.pages else None
    if page:
        page.screenshot(path=r'C:\Users\user\TM\proyecto\datos\inspect_now.png', full_page=True)
        print(f'\nURL actual: {page.url}')
        print(f'Title: {page.title()}')
        # Inputs visibles
        print('\n--- Inputs visibles ---')
        try:
            inputs = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('input,textarea,button').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return;
                    const cs = window.getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return;
                    out.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || '',
                        name: el.name || '',
                        id: el.id || '',
                        ph: el.placeholder || '',
                        ac: el.autocomplete || '',
                        text: (el.innerText || el.value || '').slice(0,80),
                        x: Math.round(r.x), y: Math.round(r.y)
                    });
                });
                return out;
            }""")
            for it in inputs:
                print(f'  {it["tag"]:8} type={it["type"]:10} name={it["name"]:20} ac={it["ac"]:20} ph="{it["ph"][:30]}" text="{it["text"]}" @({it["x"]},{it["y"]})')
        except Exception as e:
            print(f'  err: {e}')
    b.close()
