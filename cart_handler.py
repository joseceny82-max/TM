"""cart_handler.py — Procede del panel de selección al siguiente paso del flujo TM.

Tras `ticket_selector.select_quick`/`select_seatmap` queda una selección lista en el
right panel. Este módulo intenta avanzar y clasifica el destino según observación
real en TM ES (abr 2026):

- 'queue'      → URL contiene queue-it.net o body indica "Tu posición en la cola..."
- 'auth'       → URL en auth.ticketmaster.com / identity.ticketmaster.* (login requerido)
- 'checkout'   → URL contiene /checkout, /purchase, /cart o título "Pago" / "Checkout"
- 'same_page'  → no hubo navegación; probablemente requiere login antes (intentar
                 click "Iniciar sesión" si el FAB sigue siendo solo "Entradas" verde)
- 'unknown'    → dominio TM pero no reconocido — devolver para diagnóstico

API:
    proceed_to_checkout(page, click_attempts=3, post_click_wait_s=12) -> dict
"""
from __future__ import annotations
import time
from typing import Dict, Any, Optional

# Selectores de los CTAs del cart panel observados en TM ES (en orden de preferencia)
_CTA_PRIORITY = (
    # 1) Botón verde grande "Entradas" en el FAB inferior con counter
    {'selector': 'button',
     'matcher_js': """(b) => {
        const r = b.getBoundingClientRect();
        if (r.y < 700 || r.width < 400) return false;
        const t = (b.innerText||'').trim();
        if (!/^entradas/i.test(t)) return false;
        const bg = getComputedStyle(b).backgroundColor;
        const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
        if (!m) return false;
        return parseInt(m[2]) > 100 && parseInt(m[2]) > parseInt(m[1]) + 20;  // verde dominante
     }"""},
    # 2) "Siguiente" exact en el right panel (x>1280) — el del cart, NO chevron
    {'selector': 'button,a[role="button"]',
     'matcher_js': """(b) => {
        const r = b.getBoundingClientRect();
        if (r.x < 1280 || r.width < 50) return false;
        const t = (b.innerText||'').trim();
        return /^siguiente$/i.test(t);
     }"""},
    # 3) Cualquier "Continuar" / "Checkout" / "Pagar" prominente
    {'selector': 'button,a[role="button"]',
     'matcher_js': """(b) => {
        const r = b.getBoundingClientRect();
        if (r.width < 80 || r.height < 25) return false;
        const t = (b.innerText||'').trim();
        return /^(continuar|checkout|comprar|pagar|finalizar|proceder|continue|pay)$/i.test(t);
     }"""},
)


def _click_first_matching(page, selector: str, matcher_js: str) -> Optional[Dict[str, Any]]:
    """Encuentra el primer elemento que cumple matcher_js (ejecuta en JS) y lo clica.
    Devuelve dict con info del click o None si no encuentra."""
    js = """(args) => {
        const els = Array.from(document.querySelectorAll(args.sel));
        const m = new Function('b', 'return (' + args.matcher + ')(b);');
        const found = els.find(m);
        if (!found) return null;
        const r = found.getBoundingClientRect();
        const text = (found.innerText||'').trim().slice(0,60).replace(/\\n/g, '|');
        try { found.click(); } catch(e) { return {error: e.message}; }
        return {y: Math.round(r.y), x: Math.round(r.x), w: Math.round(r.width), text};
    }"""
    try:
        return page.evaluate(js, {'sel': selector, 'matcher': matcher_js})
    except Exception:
        return None


def detect_destination(page) -> Dict[str, Any]:
    """Inspecciona la página actual y clasifica el destino del flujo."""
    url = (page.url or '').lower()
    title = ''
    try: title = (page.title() or '').lower()
    except Exception: pass

    if 'queue-it.net' in url or 'queue-it.com' in url:
        return {'destination': 'queue', 'url': page.url, 'title': title}
    if 'auth.ticketmaster' in url or 'identity.ticketmaster' in url \
            or '/login' in url or 'sign-in' in url:
        return {'destination': 'auth', 'url': page.url, 'title': title}
    if '/checkout' in url or '/purchase' in url or '/cart' in url \
            or 'checkout' in title or 'pago' in title or 'pay' in title:
        return {'destination': 'checkout', 'url': page.url, 'title': title}

    # Heurística DOM: ¿hay banner "Tu turno en la cola"?
    try:
        body = (page.evaluate("() => (document.body.innerText||'').slice(0,1500)") or '').lower()
    except Exception:
        body = ''
    if 'tu posición en la cola' in body or 'esperando turno' in body \
            or 'en la cola' in body or 'queue position' in body:
        return {'destination': 'queue', 'url': page.url, 'title': title}
    if 'iniciar sesión' in body and 'cuenta' in body and 'continúa' in body:
        return {'destination': 'auth', 'url': page.url, 'title': title}

    # Si seguimos en /event/ y no hay cambio: same_page
    if '/event/' in url:
        return {'destination': 'same_page', 'url': page.url, 'title': title}

    return {'destination': 'unknown', 'url': page.url, 'title': title}


def proceed_to_checkout(page, click_attempts: int = 3,
                        post_click_wait_s: int = 12) -> Dict[str, Any]:
    """Intenta proceder del cart al siguiente paso. Devuelve {ok, destination, ...}."""
    debug: Dict[str, Any] = {'cta_clicked': None, 'attempts': []}

    initial_url = page.url
    debug['initial_url'] = initial_url

    clicked = None
    for cta in _CTA_PRIORITY:
        for _ in range(click_attempts):
            r = _click_first_matching(page, cta['selector'], cta['matcher_js'])
            debug['attempts'].append({'selector': cta['selector'][:30], 'result': r})
            if r and 'error' not in r:
                clicked = r
                break
        if clicked: break

    debug['cta_clicked'] = clicked
    if not clicked:
        return {'ok': False, 'error': 'no_cta_found', 'destination': 'same_page',
                'debug': debug}

    # Esperar transición (URL change o body change)
    deadline = time.time() + post_click_wait_s
    last_url = initial_url
    while time.time() < deadline:
        try:
            cur = page.url
        except Exception:
            cur = last_url
        if cur != last_url:
            debug['url_changed_at'] = round(time.time() - (deadline - post_click_wait_s), 1)
            break
        page.wait_for_timeout(500)

    page.wait_for_timeout(2000)  # estabilizar
    dest = detect_destination(page)
    debug.update(dest)

    return {'ok': True, **dest, 'cta_clicked_text': (clicked or {}).get('text'),
            'debug': debug}
