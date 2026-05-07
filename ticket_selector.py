"""Selector de entradas TM — 2 modos:
  - 'quick' (Ver lo mejor disponible): selección rápida sobre el panel de tipos de entrada.
  - 'seatmap' (Ver mapa de asientos): selección manual por asiento (placeholder).

Foco actual: 'quick'. Detecta las cards de oferta vía [data-testid="ticketTypeInfo"]
y opera el quantityStepper [data-testid="quantityStepper"] para subir a la cantidad deseada.

API:
    select_quick(page, qty, offer_index=0) -> {ok, applied_qty, offer_name, price_min, price_max, debug}
    detect_offers(page) -> [{index, name, price_min, price_max, raw_text, stepper_index}]
    switch_to_quick_mode(page) -> bool      # click "Ver lo mejor disponible"
    switch_to_seatmap_mode(page) -> bool    # click "Ver mapa de asientos"
"""
from __future__ import annotations
import re
import time
from typing import Dict, List, Optional, Any

PRICE_RE = re.compile(r'(\d{1,4}[.,]\d{2})\s*€')


def _remove_overlays(page) -> None:
    """Quita OneTrust/cookies banners que tapan los clicks + bloquea prompts de notificaciones.
    También cierra el modal secundario "Tanto nosotros como nuestros asociados..." (Acepto)
    que aparece a media navegación y bloquea clicks sobre tabs."""
    try:
        # 1) Click "Acepto" del modal OneTrust secundario (procesa preferencias data partners)
        page.evaluate("""() => {
            const m = Array.from(document.querySelectorAll('button')).find(b => /^acepto$/i.test((b.innerText||'').trim()));
            if (m) { try { m.click(); } catch(e){} }
        }""")
        # 2) Eliminar contenedor onetrust completo del DOM (todos los selectores)
        page.evaluate("""() => {
            document.getElementById('onetrust-consent-sdk')?.remove();
            document.getElementById('onetrust-banner-sdk')?.remove();
            document.getElementById('onetrust-pc-sdk')?.remove();
            document.querySelectorAll('[id*="onetrust" i],[class*="onetrust" i],[class*="ot-sdk" i]').forEach(e => e.remove());
            // Backdrop genérico que pueda quedar
            document.querySelectorAll('[class*="backdrop" i],[class*="Backdrop" i]').forEach(e => {
                const r = e.getBoundingClientRect();
                if (r.width > 500 && r.height > 300) e.remove();
            });
            document.body.style.overflow = 'auto';
        }""")
    except Exception:
        pass
    _block_notifications(page)


def _block_notifications(page) -> None:
    """Auto-bloquea prompts de notificaciones — JS override + DOM hide + click banners.

    1) Override JS: Notification.requestPermission siempre resuelve 'denied'.
    2) CSS injection que oculta cualquier banner top que mencione "notificaciones".
    3) Click DOM en botones tipo Bloquear/Block/No/Más tarde dentro de banners de notif.
    """
    # 1) Override JS — auto-deny (idempotente)
    try:
        page.evaluate("""() => {
            try {
                if (window.Notification) {
                    Notification.requestPermission = () => Promise.resolve('denied');
                    try { Object.defineProperty(Notification, 'permission', {get:()=>'denied', configurable:true}); } catch(e){}
                }
                if (navigator.serviceWorker && navigator.serviceWorker.register) {
                    navigator.serviceWorker.register = function() { return Promise.reject(new Error('blocked')); };
                }
                if (navigator.permissions && navigator.permissions.query) {
                    const orig = navigator.permissions.query.bind(navigator.permissions);
                    navigator.permissions.query = (q) => {
                        if (q && q.name === 'notifications') return Promise.resolve({state:'denied', onchange:null});
                        return orig(q);
                    };
                }
            } catch(e){}
        }""")
    except Exception:
        pass
    # 2) CSS injection — esconder banners top con texto "notificaciones"
    try:
        page.evaluate("""() => {
            if (document.getElementById('__tm_perros_hide_notif_css')) return;
            const style = document.createElement('style');
            style.id = '__tm_perros_hide_notif_css';
            style.textContent = `
                /* Ocultar cualquier banner top con texto 'notificaciones' o 'Permitir/Bloquear' */
                [class*="notification" i][class*="banner" i],
                [class*="push" i][class*="prompt" i],
                [data-testid*="notification" i][data-testid*="prompt" i],
                [data-bdd*="notification-prompt" i],
                #notifications-banner, .notifications-banner,
                .web-push-prompt, #web-push-prompt
                { display:none !important; visibility:hidden !important; height:0 !important; opacity:0 !important; pointer-events:none !important; }
            `;
            document.head.appendChild(style);
        }""")
    except Exception:
        pass
    # 3) Detectar y eliminar/cerrar banners DOM custom de notif
    try:
        page.evaluate("""() => {
            const re_btn = /^(?:bloquear|block|no(?:,?\\s*gracias)?|ahora no|m[áa]s tarde|later|deny|reject|cerrar|close|x)$/i;
            const re_ctx = /notificaci|notification|push|web push|allow|permitir/i;
            // Estrategia 1: localizar contenedor con texto sobre notif y eliminarlo
            document.querySelectorAll('div, section, aside, [role="dialog"], [role="alert"]').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 100 || r.height < 30) return;
                if (r.y > 200) return;  // solo banners top (no modales centrales)
                const t = (el.innerText||'').toLowerCase();
                if (t.length > 400) return;
                if (re_ctx.test(t) && (t.includes('permitir') || t.includes('bloquear') || t.includes('allow') || t.includes('block'))) {
                    el.remove();
                }
            });
            // Estrategia 2: si el banner sigue, click en el botón Bloquear/Cerrar
            const candidates = Array.from(document.querySelectorAll('button, a[role="button"], [role="button"]'));
            for (const el of candidates) {
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
                if (!t || !re_btn.test(t)) continue;
                const ctx = (el.closest('[class*="notification" i],[id*="notification" i],[class*="push" i],[role="dialog"],[role="alert"]')?.innerText || '');
                if (re_ctx.test(ctx)) {
                    try { el.click(); } catch(e){}
                }
            }
        }""")
    except Exception:
        pass


def _switch_to_tab(page, regex: str, max_wait_s: float = 8.0,
                   already_active_ok: bool = True) -> bool:
    """Helper: encuentra el botón cuyo texto matchea `regex`, click si no está ya activo.
    Polling con backoff (la UI puede tardar en montar el botón tras navigation).
    Si `already_active_ok` y el tab ya está aria-pressed=true → devuelve True sin click.
    En cada iteración se limpian overlays (modal OneTrust puede reaparecer)."""
    deadline = time.time() + max_wait_s
    js = """(rx) => {
        const re = new RegExp(rx, 'i');
        const m = Array.from(document.querySelectorAll('button')).find(b => re.test((b.innerText||'').trim()));
        if (!m) return {found: false};
        const pressed = m.getAttribute('aria-pressed');
        if (pressed === 'true') return {found: true, already_active: true};
        try { m.click(); } catch(e) { return {found: true, click_err: e.message}; }
        return {found: true, clicked: true};
    }"""
    while time.time() < deadline:
        _remove_overlays(page)  # cada iteración — el modal puede reaparecer
        try:
            r = page.evaluate(js, regex)
        except Exception:
            r = {'found': False}
        if r.get('clicked'): return True
        if r.get('already_active') and already_active_ok: return True
        page.wait_for_timeout(400)
    return False


def switch_to_quick_mode(page) -> bool:
    """Click 'Ver lo mejor disponible' (o reconoce que ya está activo). True si tab quick activo."""
    return _switch_to_tab(page, 'ver lo mejor', max_wait_s=8.0)


def switch_to_seatmap_mode(page) -> bool:
    """Click 'Ver mapa de asientos' (o reconoce que ya está activo). True si tab seatmap activo."""
    return _switch_to_tab(page, 'ver mapa', max_wait_s=8.0)


def detect_offers(page) -> List[Dict[str, Any]]:
    """Devuelve lista de ofertas con sus índices sincronizados con el panel.
    Cada offer tiene index, name, price_min, price_max, raw_text, stepper_index, stepper_y."""
    raw = page.evaluate("""() => {
        const items = Array.from(document.querySelectorAll('[data-testid="ticketTypeInfo"]'));
        return items.map((el, idx) => {
            const r = el.getBoundingClientRect();
            return { idx, text: (el.innerText||'').slice(0, 400), y: Math.round(r.y), x: Math.round(r.x) };
        });
    }""")
    out = []
    for r in raw:
        text = r['text']
        prices = [float(m.group(1).replace(',', '.')) for m in PRICE_RE.finditer(text)]
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        name = lines[0] if lines else f'Oferta {r["idx"]}'
        out.append({
            'index': r['idx'],
            'name': name[:100],
            'price_min': min(prices) if prices else None,
            'price_max': max(prices) if prices else None,
            'raw_text': text[:300],
            'card_y': r['y'], 'card_x': r['x'],
        })
    return out


def _get_stepper_state(page, offer_index: int) -> Dict[str, Any]:
    """Lee el estado del quantityStepper de la oferta `offer_index`."""
    return page.evaluate(
        """(idx) => {
            const steppers = document.querySelectorAll('[data-testid="quantityStepper"]');
            if (idx >= steppers.length) return null;
            const s = steppers[idx];
            const buttons = Array.from(s.querySelectorAll('button'));
            // El stepper tiene 2 botones: − (decrement) y + (increment). Identificar por aria/posición.
            const minus = buttons.find(b => /-|−|menos|decrement|decrease/i.test((b.getAttribute('aria-label')||'')+(b.innerText||''))) || buttons[0];
            const plus  = buttons.find(b => /\\+|m[áa]s|increment|increase/i.test((b.getAttribute('aria-label')||'')+(b.innerText||''))) || buttons[buttons.length-1];
            // Cantidad: span/input central con número
            let qty = null;
            const numTxt = (s.innerText||'').match(/\\b\\d+\\b/);
            if (numTxt) qty = parseInt(numTxt[0]);
            const r = s.getBoundingClientRect();
            const rp = plus ? plus.getBoundingClientRect() : null;
            const rm = minus ? minus.getBoundingClientRect() : null;
            return {
                qty,
                stepper_y: Math.round(r.y), stepper_x: Math.round(r.x),
                plus: rp ? {x: Math.round(rp.x+rp.width/2), y: Math.round(rp.y+rp.height/2), disabled: plus.disabled} : null,
                minus: rm ? {x: Math.round(rm.x+rm.width/2), y: Math.round(rm.y+rm.height/2), disabled: minus.disabled} : null,
            };
        }""",
        offer_index
    )


def _click_plus(page, offer_index: int) -> bool:
    """Click programático en el botón + del stepper de offer_index."""
    return bool(page.evaluate(
        """(idx) => {
            const steppers = document.querySelectorAll('[data-testid="quantityStepper"]');
            if (idx >= steppers.length) return false;
            const s = steppers[idx];
            const buttons = Array.from(s.querySelectorAll('button'));
            const plus = buttons.find(b => /\\+|m[áa]s|increment|increase/i.test((b.getAttribute('aria-label')||'')+(b.innerText||''))) || buttons[buttons.length-1];
            if (!plus || plus.disabled) return false;
            plus.click();
            return true;
        }""",
        offer_index
    ))


def select_quick(page, qty: int, offer_index: int = 0,
                 max_retries: int = 3, click_delay_ms: int = 350) -> Dict[str, Any]:
    """Selecciona `qty` entradas en la oferta `offer_index` (0-indexed, según orden de aparición).
    Modo 'Ver lo mejor disponible' — TM auto-asigna asientos disponibles consecutivos.

    Devuelve {ok, applied_qty, offer_name, price_min, price_max, total_price, debug}.
    """
    debug: Dict[str, Any] = {'qty_target': qty, 'offer_index': offer_index, 'attempts': []}

    _remove_overlays(page)

    # Asegurar modo quick
    in_quick = page.evaluate("""() => {
        const t = Array.from(document.querySelectorAll('button')).find(b => /ver lo mejor/i.test(b.innerText||''));
        return t ? t.getAttribute('aria-pressed') === 'true' : false;
    }""")
    if not in_quick:
        if switch_to_quick_mode(page):
            page.wait_for_timeout(2500)
            debug['switched_to_quick'] = True

    # Esperar a que aparezca el panel de tipos
    deadline = time.time() + 15
    while time.time() < deadline:
        page.wait_for_timeout(800)
        n = page.evaluate("""() => document.querySelectorAll('[data-testid="ticketTypeInfo"]').length""")
        if n > 0:
            debug['offers_visible'] = n
            break

    offers = detect_offers(page)
    debug['offers'] = [{'idx': o['index'], 'name': o['name'], 'price_min': o['price_min'], 'price_max': o['price_max']} for o in offers]
    if not offers:
        return {'ok': False, 'error': 'no_offers_visible', 'applied_qty': 0, 'debug': debug}

    if offer_index >= len(offers):
        return {'ok': False, 'error': f'offer_index_{offer_index}_out_of_range_{len(offers)}',
                'applied_qty': 0, 'debug': debug}

    target = offers[offer_index]
    debug['target'] = target['name']

    # Lee estado actual del stepper de la oferta target
    state = _get_stepper_state(page, offer_index)
    debug['stepper_initial'] = state
    if state is None:
        return {'ok': False, 'error': 'stepper_not_found', 'applied_qty': 0, 'debug': debug}

    current = state.get('qty') or 0
    needed = qty - current
    if needed <= 0:
        debug['noop'] = True
        return {'ok': True, 'applied_qty': current, 'offer_name': target['name'],
                'price_min': target['price_min'], 'price_max': target['price_max'],
                'total_price': (target['price_min'] or 0) * current,
                'debug': debug}

    # Click + N veces, con verificación tras cada click
    applied = current
    for i in range(needed):
        attempt_log: Dict[str, Any] = {'i': i+1, 'before_qty': applied}
        clicked = False
        for retry in range(max_retries):
            ok = _click_plus(page, offer_index)
            attempt_log[f'click_retry_{retry}'] = ok
            if ok:
                clicked = True
                page.wait_for_timeout(click_delay_ms)
                # Re-leer stepper state para confirmar que la qty subió
                new_state = _get_stepper_state(page, offer_index)
                new_qty = (new_state or {}).get('qty', applied)
                attempt_log['after_qty'] = new_qty
                if new_qty > applied:
                    applied = new_qty
                    break
                else:
                    # No subió, retry
                    page.wait_for_timeout(200)
        debug['attempts'].append(attempt_log)
        if not clicked or applied >= qty:
            if applied >= qty: break

    # Si no se llegó a qty: intentar limit-detection (algunos eventos limitan a 4 max)
    if applied < qty:
        final_state = _get_stepper_state(page, offer_index)
        debug['stepper_final'] = final_state
        if final_state and final_state.get('plus', {}).get('disabled'):
            debug['hit_limit'] = True

    total_price = None
    if target['price_min'] is not None:
        total_price = round(target['price_min'] * applied, 2)

    return {
        'ok': applied == qty,
        'applied_qty': applied,
        'offer_name': target['name'],
        'price_min': target['price_min'],
        'price_max': target['price_max'],
        'total_price': total_price,
        'partial': applied > 0 and applied < qty,
        'debug': debug,
    }


def detect_offers_with_availability(page) -> List[Dict[str, Any]]:
    """Devuelve ofertas + flag is_available basado en señales DOM:
    - stepper plus.disabled
    - texto 'agotado'/'sold out'/'no disponible' dentro o cerca de la card
    - clase/atributo 'sold-out', 'unavailable'
    - fila visualmente atenuada (opacity < 0.7)"""
    raw = page.evaluate(r"""() => {
        const items = Array.from(document.querySelectorAll('[data-testid="ticketTypeInfo"]'));
        const steppers = Array.from(document.querySelectorAll('[data-testid="quantityStepper"]'));
        const out = [];
        items.forEach((info, idx) => {
            const r = info.getBoundingClientRect();
            // El padre contenedor (li/row) — sube hasta 4 niveles
            let card = info;
            for (let lv=0; lv<4 && card.parentElement; lv++) card = card.parentElement;
            const cardText = (card.innerText||'').toLowerCase();
            const cardClass = (card.getAttribute('class')||'').toLowerCase();

            const stepper = steppers[idx];
            let plus_disabled = false, qty_now = null;
            if (stepper) {
                const buttons = Array.from(stepper.querySelectorAll('button'));
                const plus = buttons[buttons.length-1];
                plus_disabled = !!(plus && plus.disabled);
                const num = (stepper.innerText||'').match(/\b\d+\b/);
                qty_now = num ? parseInt(num[0]) : null;
            }
            const sold_out_text = /agotado|sold\s*out|no\s*disponible|exhausted|no\s*hay\s*disponibilidad/i.test(cardText);
            const sold_out_class = /sold-?out|unavailable|disabled/i.test(cardClass);
            const opacity = parseFloat(getComputedStyle(card).opacity || '1');
            const is_available = !plus_disabled && !sold_out_text && !sold_out_class && opacity >= 0.7;

            const text = (info.innerText||'').slice(0,300);
            const lines = text.split('\n').map(l=>l.trim()).filter(Boolean);
            const name = lines[0] || `Oferta ${idx}`;
            const priceMatches = [...text.matchAll(/(\d+[.,]\d{2})\s*€/g)].map(m => parseFloat(m[1].replace(',','.')));
            out.push({
                index: idx,
                name: name.slice(0,100),
                raw_text: text.slice(0,200),
                price_min: priceMatches.length ? Math.min(...priceMatches) : null,
                price_max: priceMatches.length ? Math.max(...priceMatches) : null,
                qty_now, plus_disabled, sold_out_text, sold_out_class,
                opacity,
                is_available,
                card_y: Math.round(r.y),
            });
        });
        return out;
    }""")
    return raw


def select_quick_smart(page, qty: int,
                       prefer_offer_indices: Optional[List[int]] = None,
                       max_offers_to_try: int = 5) -> Dict[str, Any]:
    """Versión inteligente de select_quick: ITERA ofertas hasta encontrar una con stock real.

    Estrategia:
    1. Detecta ofertas visibles (`detect_offers`).
    2. Intenta en este orden: prefer_offer_indices (si viene), luego el resto en orden natural.
    3. Para cada oferta:
       a. reset_qty (limpia residuos)
       b. Lee stepper inicial. Si plus.disabled antes incluso de clicar → marca SOLD_OUT y salta.
       c. Llama select_quick(qty, offer_index).
       d. Si applied_qty < qty Y stepper.plus.disabled tras último intento → marca SOLD_OUT.
       e. Si applied_qty == qty → devuelve éxito con offer_index_used.
    4. Si ninguna llega a qty → devuelve {ok:False, sold_out_offers:[...], partial_offers:[...]}.

    Devuelve {ok, applied_qty, offer_index_used, offer_name, total_price, sold_out_offers,
              partial_offers, debug}."""
    debug: Dict[str, Any] = {
        'qty_target': qty, 'prefer_offer_indices': prefer_offer_indices,
        'attempts_per_offer': [],
    }
    _remove_overlays(page)

    # Asegurar modo quick
    in_quick = page.evaluate("""() => {
        const t = Array.from(document.querySelectorAll('button')).find(b => /ver lo mejor/i.test(b.innerText||''));
        return t ? t.getAttribute('aria-pressed') === 'true' : false;
    }""")
    if not in_quick:
        switch_to_quick_mode(page)
        page.wait_for_timeout(2500)

    # Esperar panel
    deadline = time.time() + 12
    while time.time() < deadline:
        page.wait_for_timeout(700)
        n = page.evaluate("""() => document.querySelectorAll('[data-testid="ticketTypeInfo"]').length""")
        if n > 0: break

    # Usa detect_offers_with_availability — incluye is_available preciso
    offers = detect_offers_with_availability(page)
    debug['offers'] = [{'idx': o['index'], 'name': o['name'],
                        'price_min': o['price_min'], 'price_max': o['price_max'],
                        'is_available': o['is_available'], 'plus_disabled': o['plus_disabled'],
                        'sold_out_text': o['sold_out_text']} for o in offers]
    if not offers:
        return {'ok': False, 'error': 'no_offers_visible', 'applied_qty': 0, 'debug': debug}

    # Construir orden de prueba: preferidas primero, luego solo las DISPONIBLES
    indices_all = list(range(len(offers)))
    if prefer_offer_indices:
        order = [i for i in prefer_offer_indices if 0 <= i < len(offers)]
        order += [i for i in indices_all if i not in order]
    else:
        order = indices_all
    # Saltarse las claramente no disponibles (pre-filtrado)
    order_filtered = []
    pre_sold_out: List[Dict[str, Any]] = []
    for i in order:
        if offers[i].get('is_available'):
            order_filtered.append(i)
        else:
            pre_sold_out.append({'idx': i, 'name': offers[i]['name'],
                                 'reason': 'plus_disabled' if offers[i]['plus_disabled']
                                            else 'sold_out_text' if offers[i]['sold_out_text']
                                            else 'unavailable'})
    if not order_filtered and order:
        # Si TODO marcadas no disponibles, intentar igual la primera de orden por si fallo de detección
        order_filtered = order[:1]
        debug['fallback_try_first_unavailable'] = True
    order = order_filtered[:max_offers_to_try]
    debug['try_order'] = order
    debug['pre_filtered_sold_out'] = pre_sold_out

    sold_out: List[Dict[str, Any]] = list(pre_sold_out)
    partial: List[Dict[str, Any]] = []
    for idx in order:
        target = offers[idx]
        attempt: Dict[str, Any] = {'offer_index': idx, 'name': target['name']}

        # Reset previo
        try:
            cur = reset_qty(page, idx)
            attempt['reset_to'] = cur
        except Exception as e:
            attempt['reset_err'] = str(e)[:80]

        # Pre-check: stepper plus disabled antes de clicar = sold-out evidente
        st = _get_stepper_state(page, idx)
        if st and st.get('plus', {}).get('disabled'):
            attempt['pre_sold_out'] = True
            sold_out.append({'idx': idx, 'name': target['name'], 'reason': 'plus_disabled_at_start'})
            debug['attempts_per_offer'].append(attempt)
            continue

        # Intentar seleccionar
        res = select_quick(page, qty=qty, offer_index=idx)
        attempt['select_quick'] = {k: v for k, v in res.items() if k != 'debug'}
        attempt['applied_qty'] = res.get('applied_qty', 0)

        # ¿Llegó a qty?
        if res.get('ok') and res.get('applied_qty') == qty:
            debug['attempts_per_offer'].append(attempt)
            # Verificación post-éxito: ¿plus está disabled? (= no más stock)
            st_after = _get_stepper_state(page, idx)
            attempt['stepper_after_success'] = st_after
            return {
                'ok': True,
                'applied_qty': qty,
                'offer_index_used': idx,
                'offer_name': target['name'],
                'price_min': target['price_min'],
                'price_max': target['price_max'],
                'total_price': round((target['price_min'] or 0) * qty, 2) if target['price_min'] else None,
                'sold_out_offers': sold_out,
                'partial_offers': partial,
                'debug': debug,
            }

        # No llegó: ¿plus disabled = sold-out? ¿O fallo de UI?
        st_fail = _get_stepper_state(page, idx)
        attempt['stepper_at_fail'] = st_fail
        if st_fail and st_fail.get('plus', {}).get('disabled'):
            sold_out.append({'idx': idx, 'name': target['name'],
                             'applied_qty': res.get('applied_qty', 0),
                             'reason': 'plus_disabled_after_attempts'})
        else:
            partial.append({'idx': idx, 'name': target['name'],
                            'applied_qty': res.get('applied_qty', 0)})

        # Limpiar antes del siguiente intento
        try: reset_qty(page, idx)
        except Exception: pass

        debug['attempts_per_offer'].append(attempt)

    return {
        'ok': False,
        'applied_qty': 0,
        'sold_out_offers': sold_out,
        'partial_offers': partial,
        'error': 'all_offers_sold_out_or_unavailable' if sold_out else 'no_offer_could_apply_qty',
        'debug': debug,
    }


def reset_qty(page, offer_index: int = 0) -> int:
    """Resetea la cantidad de la oferta `offer_index` a 0 clicando − hasta llegar a 0.
    Devuelve qty final (0 si éxito)."""
    state = _get_stepper_state(page, offer_index)
    if not state: return -1
    cur = state.get('qty') or 0
    while cur > 0:
        ok = page.evaluate(
            """(idx) => {
                const steppers = document.querySelectorAll('[data-testid="quantityStepper"]');
                if (idx >= steppers.length) return false;
                const s = steppers[idx];
                const buttons = Array.from(s.querySelectorAll('button'));
                const minus = buttons.find(b => /-|−|menos|decrement|decrease/i.test((b.getAttribute('aria-label')||'')+(b.innerText||''))) || buttons[0];
                if (!minus || minus.disabled) return false;
                minus.click();
                return true;
            }""",
            offer_index
        )
        if not ok: break
        page.wait_for_timeout(250)
        new_state = _get_stepper_state(page, offer_index)
        new_qty = (new_state or {}).get('qty', cur)
        if new_qty >= cur: break
        cur = new_qty
    return cur


def open_cart(page) -> bool:
    """Click en el FAB inferior 'Entradas' (carrito) — abre el cart sidebar."""
    return bool(page.evaluate("""() => {
        const btns = Array.from(document.querySelectorAll('button')).filter(b => {
            const r = b.getBoundingClientRect();
            return r.y > 700 && r.width > 200 && (b.innerText||'').trim().toLowerCase().endsWith('entradas');
        });
        if (btns.length) { btns[0].click(); return true; }
        return false;
    }"""))


# ════════════════════════════════════════════════════════════════════
#  Modo SEATMAP — selección sobre el mapa SVG
# ════════════════════════════════════════════════════════════════════

def _accept_consent_modals(page) -> None:
    """TM ES a veces lanza un segundo modal OneTrust dentro del flujo seatmap."""
    try:
        page.evaluate("""() => {
            // Click "Acepto" si existe
            const m = Array.from(document.querySelectorAll('button')).find(b => /^acepto$/i.test((b.innerText||'').trim()));
            if (m) m.click();
            // Quita cualquier banner OneTrust residual
            document.querySelectorAll('[id*="onetrust" i],[class*="onetrust" i]').forEach(e => e.remove());
        }""")
        page.wait_for_timeout(1200)
    except Exception:
        pass


def _detect_map_sections(page) -> List[Dict[str, Any]]:
    """Devuelve las secciones del SVG visibles. Selector REAL en TM ES (abr 2026):
    `[data-component="svg__section"]` con atributos `data-section-id`, `data-section-name`,
    `data-active` ('true' = seleccionable, 'false' = no disponible).
    Devuelve todas con su estado (active/inactive). Caller decide cuáles intentar."""
    raw = page.evaluate(r"""() => {
        const out = [];
        document.querySelectorAll('[data-component="svg__section"]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) return;
            const cs = getComputedStyle(el);
            out.push({
                section_id: el.getAttribute('data-section-id') || '',
                section_name: el.getAttribute('data-section-name') || '',
                active: el.getAttribute('data-active') === 'true',
                cx: Math.round(r.x + r.width / 2),
                cy: Math.round(r.y + r.height / 2),
                w: Math.round(r.width), h: Math.round(r.height),
                fill: cs.fill, opacity: cs.opacity,
                cursor: cs.cursor,
                pointer_events: cs.pointerEvents,
            });
        });
        return out;
    }""")
    return raw  # caller filtra; preservamos el listado completo para diagnóstico


def _wait_for_map(page, max_wait_s: int = 20) -> int:
    """Espera a que el SVG del mapa cargue. Devuelve count de [data-component="svg__section"]."""
    deadline = time.time() + max_wait_s
    last = 0
    while time.time() < deadline:
        page.wait_for_timeout(800)
        try:
            n = page.evaluate("""() => document.querySelectorAll('[data-component="svg__section"]').length""")
        except Exception:
            n = 0
        if n >= 3:  # mínimo 3 secciones para considerar mapa cargado
            return n
        last = n
    return last


def _click_section(page, section_id: str) -> bool:
    """Click en la sección por data-section-id ('s_NNN') usando MouseEvent directo."""
    return bool(page.evaluate("""(secId) => {
        const el = document.querySelector(`[data-section-id="${secId}"]`);
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const ev = new MouseEvent('click', {bubbles:true, cancelable:true, view:window,
                                              clientX: r.x + r.width/2, clientY: r.y + r.height/2});
        el.dispatchEvent(ev);
        try { el.click(); } catch(e) {}
        return true;
    }""", section_id))


def select_seatmap(page, qty: int, max_section_attempts: int = 5,
                   fallback_to_quick: bool = True,
                   target_section_ids: Optional[List[str]] = None,
                   strict_targets: bool = False) -> Dict[str, Any]:
    """Modo 'Ver mapa de asientos': click en una sección disponible y selecciona qty.

    Estrategia:
    1. Cambia al tab seatmap.
    2. Cierra modales de consent.
    3. Espera SVG cargado (>= 3 secciones).
    4. Detecta secciones del recinto (activas vs inactivas).
    5. **Si `target_section_ids` viene**: prioriza esos IDs, en el orden dado. Si
       `strict_targets=True` SOLO intenta los marcados (no cae en otros activos).
    6. Por cada candidata: click en la sección y esperar panel de selección.
       - Si aparecen `[data-testid="quantityStepper"]` → delega a select_quick.
       - Si aparecen asientos individuales (svg circle / [data-seat-id]) → click qty asientos.
    7. Si todas fallan y fallback_to_quick: vuelve a tab quick y delega.

    Devuelve {ok, applied_qty, mode_used, section_clicked, debug}."""
    debug: Dict[str, Any] = {
        'qty_target': qty,
        'target_section_ids': list(target_section_ids) if target_section_ids else None,
        'strict_targets': strict_targets,
        'sections_tried': [],
    }

    _remove_overlays(page)

    # Tab seatmap
    if not switch_to_seatmap_mode(page):
        debug['error'] = 'cannot_switch_to_seatmap'
        if fallback_to_quick:
            res = select_quick(page, qty)
            return {**res, 'mode_used': 'quick_fallback', 'debug': {**res.get('debug', {}), **debug}}
        return {'ok': False, 'applied_qty': 0, 'mode_used': 'seatmap', 'debug': debug}
    page.wait_for_timeout(3000)

    _accept_consent_modals(page)

    # Esperar map
    n_sections = _wait_for_map(page)
    debug['svg_sections_loaded'] = n_sections
    if n_sections < 20:
        debug['error'] = 'svg_not_loaded'
        if fallback_to_quick:
            res = select_quick(page, qty)
            return {**res, 'mode_used': 'quick_fallback', 'debug': {**res.get('debug', {}), **debug}}
        return {'ok': False, 'applied_qty': 0, 'mode_used': 'seatmap', 'debug': debug}

    # Detectar secciones — separar activas/inactivas
    all_sections = _detect_map_sections(page)
    active = [s for s in all_sections if s.get('active')]
    inactive = [s for s in all_sections if not s.get('active')]
    debug['total_sections'] = len(all_sections)
    debug['active_sections'] = len(active)
    debug['inactive_sections'] = len(inactive)
    debug['sample_section_names'] = [s.get('section_name') for s in all_sections[:6]]
    if not active:
        debug['error'] = 'no_active_sections_in_map'
        if fallback_to_quick:
            res = select_quick(page, qty)
            return {**res, 'mode_used': 'quick_fallback',
                    'debug': {**res.get('debug', {}), **debug}}
        return {'ok': False, 'applied_qty': 0, 'mode_used': 'seatmap', 'debug': debug}

    # Aplicar prioridad target_section_ids si se especifica
    candidates = active
    if target_section_ids:
        target_set = set(target_section_ids)
        # Mantener orden de target_section_ids (puede que no estén activos todos)
        active_by_id = {s['section_id']: s for s in active}
        prioritized = []
        targets_active, targets_inactive_or_missing = [], []
        for tid in target_section_ids:
            if tid in active_by_id:
                prioritized.append(active_by_id[tid])
                targets_active.append(tid)
            else:
                targets_inactive_or_missing.append(tid)
        debug['targets_active'] = targets_active
        debug['targets_inactive_or_missing'] = targets_inactive_or_missing
        if strict_targets:
            candidates = prioritized
        else:
            # Targets primero, luego el resto de activos como fallback
            others = [s for s in active if s['section_id'] not in target_set]
            candidates = prioritized + others
        if not candidates:
            debug['error'] = 'no_target_sections_active' if strict_targets else 'no_active_at_all'
            if fallback_to_quick:
                res = select_quick(page, qty)
                return {**res, 'mode_used': 'quick_fallback',
                        'debug': {**res.get('debug', {}), **debug}}
            return {'ok': False, 'applied_qty': 0, 'mode_used': 'seatmap',
                    'targets_attempted': target_section_ids, 'debug': debug}

    # Iterar candidatas (priorizadas si hay targets)
    for sec in candidates[:max_section_attempts]:
        attempt = {'section_id': sec['section_id'], 'section_name': sec.get('section_name'), 'fill': sec.get('fill')}
        ok = _click_section(page, sec['section_id'])
        attempt['click_ok'] = ok
        if not ok:
            debug['sections_tried'].append(attempt)
            continue
        page.wait_for_timeout(2500)

        # Cerrar modales que aparezcan tras click
        _accept_consent_modals(page)
        page.wait_for_timeout(800)

        # ¿Aparecieron quantitySteppers? → delegar a select_quick
        steppers = page.evaluate("""() => document.querySelectorAll('[data-testid="quantityStepper"]').length""")
        attempt['steppers_after_click'] = steppers
        if steppers > 0:
            res = select_quick(page, qty)
            debug['sections_tried'].append(attempt)
            return {**res, 'mode_used': 'seatmap_via_section_then_stepper',
                    'section_clicked': sec['section_id'], 'debug': {**res.get('debug', {}), **debug}}

        # ¿Asientos individuales (svg circle, [data-seat-id])? → click qty consecutivos
        seats = page.evaluate(r"""() => {
            const out = [];
            document.querySelectorAll('svg circle, [data-seat-id], [data-seat]').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 3 || r.height < 3) return;
                const cs = getComputedStyle(el);
                if (cs.cursor !== 'pointer') return;
                const id = el.getAttribute('data-seat-id') || el.getAttribute('id') || '';
                out.push({id, cx: Math.round(r.x + r.width/2), cy: Math.round(r.y + r.height/2)});
                if (out.length >= 50) return;
            });
            return out;
        }""")
        attempt['seats_visible'] = len(seats)
        if len(seats) >= qty:
            applied = 0
            for s in seats[:qty]:
                page.mouse.click(s['cx'], s['cy'])
                page.wait_for_timeout(400)
                applied += 1
            attempt['seats_clicked'] = applied
            debug['sections_tried'].append(attempt)
            return {'ok': applied == qty, 'applied_qty': applied,
                    'mode_used': 'seatmap_seat_click', 'section_clicked': sec['section_id'],
                    'debug': debug}

        debug['sections_tried'].append(attempt)
        # Esta sección no llevó a ningún panel utilizable; volver al mapa principal y reintentar otra
        try: page.keyboard.press('Escape')
        except: pass
        page.wait_for_timeout(800)

    # Ningún intento funcionó
    debug['error'] = 'no_section_yielded_selectable_panel'
    if fallback_to_quick:
        res = select_quick(page, qty)
        return {**res, 'mode_used': 'quick_fallback', 'debug': {**res.get('debug', {}), **debug}}
    return {'ok': False, 'applied_qty': 0, 'mode_used': 'seatmap', 'debug': debug}
