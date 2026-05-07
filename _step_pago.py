"""Test paso a paso: 1 bot Vis.Reducida qty=4 → siguiente → ver hasta dónde llega.
Deja el browser abierto al final con resumen de CTAs disponibles.
"""
import sys, time, json, os
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')
import requests

import bot_orchestrator as bo
import ticket_selector
import cart_handler

EVENT_URL = 'https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961'
SHOTS = r'datos\buy\pago'
os.makedirs(SHOTS, exist_ok=True)

# 1) Arrancar perfil + browser
prof = bo._pick_fresh_profile(country_code='ES')
print(f'[1] perfil: {prof}')
port = bo._start_profile(prof['env_id'])
print(f'    port: {port}')
time.sleep(5)

from playwright.sync_api import sync_playwright
v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
ctx = b.contexts[0]
ctx.add_init_script("""
    if (window.Notification) {
        Notification.requestPermission = () => Promise.resolve('denied');
        try { Object.defineProperty(Notification, 'permission', {get:()=>'denied', configurable:true}); } catch(e){}
    }
""")
page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.bring_to_front()

def shot(n):
    p = os.path.join(SHOTS, f'{n}.png')
    page.screenshot(path=p, full_page=False)
    print(f'    shot {n}')

# 2) Goto
print('\n[2] goto evento')
page.goto(EVENT_URL, wait_until='domcontentloaded', timeout=30000)
page.wait_for_timeout(7000)
for txt in ['Aceptar todas las cookies', 'Rechazarlas todas']:
    try:
        btn = page.get_by_text(txt, exact=False).first
        if btn.is_visible(timeout=1500): btn.click(); page.wait_for_timeout(1500); break
    except: pass
ticket_selector._block_notifications(page)
shot('01_loaded')

# 3) Selección Vis.Reducida qty=4
print('\n[3] select_quick_smart prefer=[2] qty=4')
res = ticket_selector.select_quick_smart(page, qty=4, prefer_offer_indices=[2])
print(f'    ok={res["ok"]} qty={res.get("applied_qty")} offer={res.get("offer_name")}')
shot('02_selected')

# 4) Click "Siguiente"/"Entradas" verde
print('\n[4] proceed_to_checkout')
proc = cart_handler.proceed_to_checkout(page, click_attempts=2, post_click_wait_s=10)
print(f'    destination={proc.get("destination")} url={proc.get("url","")[:80]}')
shot('03_after_proceed')

# 5) Inspeccionar TODOS los CTAs visibles ahora
print('\n[5] CTAs disponibles tras proceed:')
ctas = page.evaluate(r"""() => {
    const out = [];
    const re_cta = /^(continuar|continue|siguiente|next|comprar|pagar|iniciar sesi[oó]n|sign in|log\s*in|registrarse|crear|checkout|finalizar|proceder)/i;
    document.querySelectorAll('button, a[role="button"], a').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width < 60 || r.height < 18) return;
        const t = (el.innerText || '').trim();
        if (!t || t.length > 80) return;
        if (re_cta.test(t.split('\n')[0])) {
            const cs = getComputedStyle(el);
            out.push({tag:el.tagName, text:t.slice(0,60).replace(/\n/g,'|'), x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), bg:cs.backgroundColor.slice(0,30)});
        }
    });
    return out.sort((a,b)=>a.y-b.y);
}""")
for c in ctas[:15]: print(f'  y={c["y"]:4} x={c["x"]:4} {c["w"]}px tag={c["tag"]} bg={c["bg"]:30} \"{c["text"]}\"')

# 6) Buscar específicamente botón verde con texto que avance al pago
print('\n[6] click CTA "Iniciar sesión" o "Continuar al pago" si existe')
clicked_text = page.evaluate(r"""() => {
    const re_buy = /^(iniciar sesi[oó]n|sign in|log\s*in|registrarse|continuar al pago|proceder al pago|finalizar compra|pagar)/i;
    const candidates = Array.from(document.querySelectorAll('button, a[role="button"], a')).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width >= 60 && r.height >= 18 && r.y > 100 && r.y < 1000;
    });
    for (const el of candidates) {
        const t = (el.innerText||'').trim().split('\n')[0];
        if (re_buy.test(t)) {
            try { el.click(); return t; } catch(e) { return 'click_err: '+e.message; }
        }
    }
    return null;
}""")
print(f'    clicked: {clicked_text}')
page.wait_for_timeout(7000)
shot('04_after_signin_click')

print(f'\n[7] estado tras click avanzar')
print(f'    URL: {page.url}')
print(f'    Title: {page.title()}')
state = page.evaluate(r"""() => {
    const out = {h:[],in:[],btn:[]};
    document.querySelectorAll('h1,h2,h3').forEach(h=>{
        const r=h.getBoundingClientRect();
        if (r.width>5) out.h.push((h.innerText||'').slice(0,80));
    });
    document.querySelectorAll('input').forEach(i=>{
        const r=i.getBoundingClientRect();
        if (r.width<5) return;
        out.in.push({type:i.type,name:i.name,ph:i.placeholder,ac:i.autocomplete});
    });
    document.querySelectorAll('button').forEach(b=>{
        const r=b.getBoundingClientRect();
        if (r.width<60) return;
        const t=(b.innerText||'').trim();
        if (t && t.length<60) out.btn.push(t.slice(0,50).replace(/\n/g,'|'));
    });
    return out;
}""")
print(f'    headings: {state["h"][:5]}')
print(f'    inputs: {state["in"][:8]}')
print(f'    botones: {list(set(state["btn"]))[:10]}')

# Persist state
with open('_step_pago_state.json', 'w', encoding='utf-8') as f:
    json.dump({'profile':prof['profile'],'env_id':prof['env_id'],'port':port,
               'last_url':page.url,'last_title':page.title()}, f, indent=2)

print(f'\n>>> Browser ABIERTO en port={port}, perfil={prof["profile"]} (env_id={prof["env_id"]})')
print(f'>>> Próximo script puede continuar el flujo desde aquí.')

b.close()
pw.stop()
