"""Avanza al form de teléfono haciendo click en "Verificar tu número de teléfono"."""
import sys, requests
sys.stdout.reconfigure(encoding='utf-8')
PORT = 57448
v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=8).json()
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
page = b.contexts[0].pages[0]
page.bring_to_front()
print(f'URL antes: {page.url[:120]}')

clicked = page.evaluate(r"""() => {
  const btns = Array.from(document.querySelectorAll('button,a'));
  // Botón con texto "Verificar" / "Añadir teléfono" del bloque del teléfono
  const cand = btns.find(b => /verificar.*tel[eé]fono|a[ñn]adir.*tel[eé]fono|verificar mi tel|verificar mi n[uú]mero|verify your phone/i.test((b.innerText||'').trim()) && !b.disabled)
            || btns.find(b => /^verificar$/i.test((b.innerText||'').trim()) && /tel|n[uú]m/i.test(b.closest('section,div,article')?.innerText||'') && !b.disabled);
  if (cand) { cand.click(); return cand.innerText.trim(); }
  return null;
}""")
print(f'click: {clicked or "NO ENCONTRADO"}')
page.wait_for_timeout(4000)
import os
os.makedirs(r'datos\buy\register_u1', exist_ok=True)
page.screenshot(path=r'datos\buy\register_u1\20_phone_form.png')
print(f'URL después: {page.url[:120]}')
print(f'Title: {page.title()}')
print(f'BODY 600c:')
print(page.evaluate('()=>document.body.innerText.slice(0,600)'))
b.close(); pw.stop()
