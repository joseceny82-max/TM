"""Scanner DOM real para detectar ofertas TM (paneles de selección de entradas).

Uso:
    from event_offer_scanner import scan_event_offers
    res = scan_event_offers(tm_id, tm_url, port=PORT)
    # res = {ok, offers:[{name,category,price_min,price_max,raw_text}], scan_method, debug}

El scanner:
1. Conecta al CDP del browser MoreLogin abierto en `port`.
2. Navega al `tm_url` (con retries) y elimina overlays OneTrust.
3. Espera a que aparezca el panel de selección.
4. Aplica una cascada de selectores conocidos para cards de oferta.
5. Si ninguno funciona, hace heurística por contenedor (rect: x>500, repetidos, contienen €).
6. Extrae nombre + precio_min + precio_max + categoría inferida.

Persiste vía POST /api/events/<tm_id>/offers (no acoplado: el caller decide).
"""
from __future__ import annotations
import re
import time
import json
import requests
from typing import List, Dict, Any, Optional

# Selectores conocidos en orden de preferencia. Si TM cambia el DOM, añadir uno nuevo aquí.
# Confirmado en TM ES (abr 2026): el panel de tipos de entrada usa data-testid="panelTicketTypes"
# y cada card es un <li> con un hijo data-testid="ticketTypeInfo".
OFFER_CARD_SELECTORS = [
    '[data-testid="panelTicketTypes"] li',
    '[data-testid="ticketTypeInfo"]',
    '[data-bdd*="quick-pick" i]',
    '[data-bdd*="offer-card" i]',
    '[data-bdd*="ticket-list-row" i]',
    '[data-testid*="offer-card" i]',
    '[data-testid*="ticket-row" i]',
    '[class*="QuickPicksTicketRow" i]',
    '[class*="OfferCard" i]',
    '[class*="ticketRow" i]',
    '[class*="TicketRow" i]',
    'li[class*="ticket" i]',
]

CATEGORY_HINTS = [
    # Orden importante: las categorías más específicas van primero (gana la primera coincidencia).
    ('wheelchair', [r'silla\s*de\s*ruedas', r'wheelchair', r'movilidad\s*reducida', r'visibilidad\s*reducida']),
    ('vip',        [r'\bvip\b', r'premium', r'palco premium']),
    ('boxes',      [r'palco', r'\bbox(es)?\b', r'\bsuite']),
    ('floor',      [r'\bpista\b', r'\bfloor\b', r'\bcancha\b']),
    ('ga',         [r'general\s*admission', r'general\s*pista', r'\bga\b', r'general']),
    ('reserved',   [r'asiento\s*reservado', r'reserved', r'asiento\s*numerado', r'numerada']),
]

PRICE_RE = re.compile(r'(\d{1,4}[.,]\d{2})\s*€')


def _infer_category(text: str) -> Optional[str]:
    t = text.lower()
    for cat, patterns in CATEGORY_HINTS:
        for p in patterns:
            if re.search(p, t):
                return cat
    return None


def _extract_offers_via_js(page) -> List[Dict[str, Any]]:
    """Intenta extraer ofertas usando los selectores conocidos en orden."""
    js = """
    (selectors) => {
        function visible(el) {
            const r = el.getBoundingClientRect();
            return r.width >= 50 && r.height >= 30 && getComputedStyle(el).visibility !== 'hidden';
        }
        for (const sel of selectors) {
            let nodes;
            try { nodes = document.querySelectorAll(sel); } catch(e) { continue; }
            const visNodes = Array.from(nodes).filter(visible);
            if (visNodes.length === 0) continue;
            const rows = [];
            const seen = new Set();
            for (const n of visNodes) {
                const txt = (n.innerText || '').trim();
                if (!txt) continue;
                const key = txt.slice(0, 60);
                if (seen.has(key)) continue;
                seen.add(key);
                const r = n.getBoundingClientRect();
                rows.push({
                    selector: sel,
                    text: txt.slice(0, 400),
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height),
                });
            }
            if (rows.length) return {selector_used: sel, rows};
        }
        return {selector_used: null, rows: []};
    }
    """
    return page.evaluate(js, OFFER_CARD_SELECTORS)


def _heuristic_offers(page) -> List[Dict[str, Any]]:
    """Fallback heurístico: busca contenedores en la columna derecha (x>500) con €."""
    js = r"""
    () => {
        function visible(el) {
            const r = el.getBoundingClientRect();
            return r.width >= 80 && r.height >= 30 && getComputedStyle(el).visibility !== 'hidden';
        }
        const re = /\d{1,4}[.,]\d{2}\s*€/;
        const candidates = [];
        document.querySelectorAll('*').forEach(el => {
            if (!visible(el)) return;
            const r = el.getBoundingClientRect();
            if (r.x < 500) return;
            if (r.height > 250 || r.height < 60) return;  // cards entre 60-250 px alto
            const txt = (el.innerText || '').trim();
            if (txt.length > 600) return;
            if (!re.test(txt)) return;
            candidates.push({tag: el.tagName.toLowerCase(), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), text: txt.slice(0, 400)});
        });
        // Dedup por text-prefijo y agrupa elementos solapados (queda con el más pequeño)
        candidates.sort((a, b) => a.y - b.y || a.h - b.h);
        const seen = new Map();
        for (const c of candidates) {
            const key = c.text.slice(0, 50);
            const existing = seen.get(key);
            if (!existing || (c.w * c.h) < (existing.w * existing.h)) seen.set(key, c);
        }
        return Array.from(seen.values()).slice(0, 12);
    }
    """
    return page.evaluate(js)


def _parse_offer_text(text: str) -> Dict[str, Any]:
    """Convierte el texto de una card en {name, price_min, price_max, category, raw_text}."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    name = lines[0] if lines else 'Oferta sin nombre'
    # Si la primera línea es solo un precio o emoji, busca otra
    if PRICE_RE.search(name) or len(name) < 4:
        for ln in lines[1:]:
            if not PRICE_RE.search(ln) and len(ln) > 4:
                name = ln
                break
    prices = [float(m.group(1).replace(',', '.')) for m in PRICE_RE.finditer(text)]
    pmin = min(prices) if prices else None
    pmax = max(prices) if prices else None
    return {
        'name': name[:100],
        'category': _infer_category(text),
        'price_min': pmin,
        'price_max': pmax,
        'raw_text': text[:300],
    }


def scan_event_offers(tm_id: str, tm_url: str, port: int,
                      max_wait_s: int = 30) -> Dict[str, Any]:
    """Conecta al browser en `port` (CDP), navega al `tm_url` y extrae ofertas.
    Retorna {ok, offers, scan_method, selector_used?, debug}."""
    from playwright.sync_api import sync_playwright

    debug = {'tm_id': tm_id, 'tm_url': tm_url, 'port': port}
    try:
        v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=5).json()
        ws = v['webSocketDebuggerUrl']
    except Exception as e:
        return {'ok': False, 'error': f'cdp_unreachable: {e}', 'debug': debug}

    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(ws)
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = next((p for p in ctx.pages if 'ticketmaster' in (p.url or '')), None) or \
               (ctx.pages[0] if ctx.pages else ctx.new_page())
        try: page.bring_to_front()
        except: pass

        # Navegar si no estamos ya en la URL del evento
        if tm_id not in (page.url or ''):
            for attempt in range(3):
                try:
                    page.goto(tm_url, wait_until='domcontentloaded', timeout=30000)
                    break
                except Exception as e:
                    debug.setdefault('goto_errors', []).append(str(e)[:80])
                    time.sleep(3)
            page.wait_for_timeout(7000)

        # Cookies + OneTrust
        try:
            for txt in ['Aceptar todas las cookies', 'Rechazarlas todas', 'Accept All']:
                btn = page.get_by_text(txt, exact=False).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    page.wait_for_timeout(1200)
                    break
        except: pass
        try:
            page.evaluate("""() => {
                document.getElementById('onetrust-consent-sdk')?.remove();
                document.querySelectorAll('[id*="onetrust"], [class*="onetrust"]').forEach(e => e.remove());
            }""")
        except: pass

        # Click "Ver lo mejor disponible" (vía JS para evitar overlays). Si no existe, ya estamos en el flujo correcto.
        try:
            clicked = page.evaluate("""() => {
                const m = Array.from(document.querySelectorAll('button')).find(b => /ver lo mejor/i.test(b.innerText||''));
                if (m) { m.click(); return true; }
                return false;
            }""")
            debug['ver_lo_mejor_clicked'] = bool(clicked)
            if clicked:
                page.wait_for_timeout(3500)
        except Exception as e:
            debug['ver_lo_mejor_err'] = str(e)[:100]

        # Esperar a que cargue el panel de ofertas (heurística: hasta que haya '€' y >= 1 card visible)
        deadline = time.time() + max_wait_s
        last_count = 0
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            try:
                has_eur = page.evaluate("""() => /\\d+[.,]\\d{2}\\s*€/.test(document.body.innerText || '')""")
            except Exception:
                has_eur = False
            if has_eur:
                last_count = page.evaluate("""() => document.querySelectorAll('button').length""")
                break

        # 1) selectores conocidos
        try:
            res = _extract_offers_via_js(page)
        except Exception as e:
            res = {'selector_used': None, 'rows': [], 'error': str(e)[:100]}
        debug['selector_used'] = res.get('selector_used')
        debug['rows_via_selectors'] = len(res.get('rows') or [])

        rows = res.get('rows') or []
        scan_method = 'dom_auto' if rows else None

        # 2) fallback heurístico
        if not rows:
            try:
                rows = _heuristic_offers(page)
                scan_method = 'dom_heuristic'
            except Exception as e:
                debug['heuristic_error'] = str(e)[:100]
                rows = []

        debug['rows_total'] = len(rows)

        # 3) parsing
        offers = []
        seen_names = set()
        for r in rows:
            o = _parse_offer_text(r.get('text', ''))
            if not o['name'] or o['name'] in seen_names:
                continue
            seen_names.add(o['name'])
            # filtra cards sin precio (no son ofertas)
            if o['price_min'] is None:
                continue
            offers.append(o)

        b.close()
        return {
            'ok': bool(offers),
            'offers': offers,
            'scan_method': scan_method or 'no_match',
            'debug': debug,
        }


# CLI: scan + persist via dashboard endpoint
if __name__ == '__main__':
    import sys, os
    if len(sys.argv) < 4:
        print('Usage: python event_offer_scanner.py <tm_id> <tm_url> <port> [dashboard_url]')
        sys.exit(2)
    tm_id = sys.argv[1]; tm_url = sys.argv[2]; port = int(sys.argv[3])
    dash_url = sys.argv[4] if len(sys.argv) > 4 else 'http://127.0.0.1:5055'
    print(f'Scan tm_id={tm_id} url={tm_url[:50]} port={port}')
    res = scan_event_offers(tm_id, tm_url, port)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if res.get('ok'):
        print('\nPOSTing al dashboard...')
        # login first
        s = requests.Session()
        s.post(f'{dash_url}/login', data={'username': 'admin', 'password': 'admin'})
        r = s.post(f'{dash_url}/api/events/{tm_id}/offers',
                   json={'offers': res['offers'], 'tm_url': tm_url, 'scan_method': res['scan_method']})
        print(f'POST status: {r.status_code} body: {r.text[:200]}')
