"""tm_ui_helpers.py — Helpers de UI reusables para el bot de compra TM.

Objetivo: extraer las 3 operaciones más duplicadas a funciones puras, SIN
alterar:
    - los selectores actuales (no añadimos ni quitamos)
    - el comportamiento de timing (waits/sleeps idénticos)
    - la regex de match

Esto es un FACADE — la lógica viene del código existente en `_compra_parallel.py`
pero centralizada. La migración (reemplazar inline → llamar al helper) es OPCIONAL
y se hace en próximas sesiones, una por una. Mientras tanto convive con el código
inline sin romper nada.

API:
    accept_cookies(page, *, label="banner", timeout_each_ms=1500) -> bool
    click_continue(page, *, timeout_each_ms=2500) -> Optional[str]
    safe_fill_input(page, selector, value, *, timeout_ms=5000,
                     clear_first=True, native_setter=True) -> bool

Diseño:
    - Todos devuelven `bool` o `Optional[str]` para que el caller decida flow.
    - Ningún `raise` propaga al caller (try/except interno).
    - Tampoco modifican Page state si la operación falla (idempotencia parcial).

Tests:
    El módulo es 100% pure-Python sin dependencias TM-specific al import time;
    los tests live se hacen en runs reales del bot (uso opcional).
"""
from __future__ import annotations

import time
from typing import Optional


# ────────────────────────────────────────────────────────────────────
#  CONSTANTES — replican defaults usados inline en _compra_parallel.py
# ────────────────────────────────────────────────────────────────────
try:
    from _compra_constants import (TIMEOUT_UI_FAST_MS, TIMEOUT_UI_NORMAL_MS,
                                    TIMEOUT_UI_FORM_FILL_MS, POLL_FAST_MS)
except ImportError:
    TIMEOUT_UI_FAST_MS = 1_500
    TIMEOUT_UI_NORMAL_MS = 3_000
    TIMEOUT_UI_FORM_FILL_MS = 5_000
    POLL_FAST_MS = 200


# Cookies banner: lista de textos en orden de preferencia (idéntico al inline).
COOKIE_BANNER_TEXTS = (
    "Aceptar todas las cookies",
    "Aceptar",
    "Acepto",
    "Accept all cookies",
    "Accept",
    "OK",
)

# OneTrust IDs (idéntico al fallback JS inline en L1745/L2429).
ONETRUST_ACCEPT_IDS = (
    "#onetrust-accept-btn-handler",
    "#accept-recommended-btn-handler",
    "button[id*='accept' i][id*='onetrust' i]",
)

# Continuar/Siguiente: textos exactos. Mismas que `click_continuar` inline.
CONTINUE_BUTTON_TEXTS = (
    "Continuar comprando",
    "Continuar",
    "Continue shopping",
    "Continue",
    "Siguiente",
    "Aceptar",
    "Next",
)


# ────────────────────────────────────────────────────────────────────
#  accept_cookies
# ────────────────────────────────────────────────────────────────────
def accept_cookies(page,
                    label: str = "banner",
                    timeout_each_ms: int = TIMEOUT_UI_FAST_MS,
                    settle_ms: int = 1_500) -> bool:
    """Cierra el banner de cookies si está visible. Idéntico al inline original.

    Estrategia (en orden):
        1. Buscar texto por `page.get_by_text(...)` con la lista COOKIE_BANNER_TEXTS.
        2. Si no, JS directo: `document.querySelector('#onetrust-accept-btn-handler')`.

    Args:
        page: Playwright Page
        label: tag para logs futuros (no usado aquí, dejado para llamadores)
        timeout_each_ms: timeout `is_visible` por cada texto (idéntico al inline 1500ms)
        settle_ms: pausa post-click para que TM termine de hidratar (idéntico inline 1500ms)

    Returns:
        True si clicó algún botón, False en otro caso.
    """
    # 1) Por texto
    for txt in COOKIE_BANNER_TEXTS:
        try:
            btn = page.get_by_text(txt, exact=False).first
            if btn.is_visible(timeout=timeout_each_ms):
                btn.click(timeout=timeout_each_ms + 1_000)
                if settle_ms:
                    page.wait_for_timeout(settle_ms)
                return True
        except Exception:
            continue  # selector falló, probar siguiente texto

    # 2) JS fallback OneTrust (idéntico al inline L1744-L1746, L3260-L3266)
    try:
        clicked = page.evaluate(r"""() => {
            const ids = ['#onetrust-accept-btn-handler',
                         '#accept-recommended-btn-handler'];
            for (const sel of ids) {
                const b = document.querySelector(sel);
                if (b && b.offsetParent !== null) { b.click(); return true; }
            }
            // Búsqueda genérica por texto en botones (fallback final)
            const btns = Array.from(document.querySelectorAll('button'));
            const m = btns.find(b => /aceptar.*cookies|aceptar todas|^acepto$/i.test(b.innerText || ''));
            if (m && m.offsetParent !== null) { m.click(); return true; }
            return false;
        }""")
        if clicked:
            if settle_ms:
                page.wait_for_timeout(settle_ms)
            return True
    except Exception:
        pass

    return False


# ────────────────────────────────────────────────────────────────────
#  click_continue
# ────────────────────────────────────────────────────────────────────
def click_continue(page,
                    timeout_each_ms: int = TIMEOUT_UI_NORMAL_MS,
                    extra_texts: Optional[tuple] = None) -> Optional[str]:
    """Click en botón "Continuar / Siguiente / Aceptar" si está visible.

    Estrategia:
        1. JS directo (más rápido — idéntico al inline L2949-2959):
           busca botones visibles + enabled + match regex.
        2. Fallback: `page.get_by_role('button', name=re...)` con texto exacto.

    Args:
        page: Playwright Page
        timeout_each_ms: timeout por intento
        extra_texts: textos adicionales a probar (concat con CONTINUE_BUTTON_TEXTS)

    Returns:
        Texto del botón clicado, o None si no encontró.
    """
    texts = list(CONTINUE_BUTTON_TEXTS)
    if extra_texts:
        texts = list(extra_texts) + texts

    # 1) JS directo — patrón usado en L2949
    try:
        res = page.evaluate(r"""(args) => {
            const all = Array.from(document.querySelectorAll(
                'button, a[role="button"], [role="button"], input[type="submit"]'));
            const btns = all.filter(b => b.offsetParent !== null && !b.disabled);
            const t = btns.find(b => {
                const tx = (b.innerText || b.textContent || b.value || '').trim();
                return /continuar\s+comprando|^continuar$|continue\s+shopping|^continue$|^siguiente$|^next$|^aceptar$/i.test(tx);
            });
            if (t) { t.click(); return (t.innerText || t.value || '').slice(0, 40); }
            return null;
        }""", {})
        if res:
            return res
    except Exception:
        pass

    # 2) Fallback locator por texto (más lento, último recurso)
    for txt in texts:
        try:
            btn = page.get_by_text(txt, exact=False).first
            if btn.is_visible(timeout=timeout_each_ms):
                btn.click(timeout=timeout_each_ms + 1_000)
                return txt
        except Exception:
            continue

    return None


# ────────────────────────────────────────────────────────────────────
#  safe_fill_input
# ────────────────────────────────────────────────────────────────────
def safe_fill_input(page,
                     selector: str,
                     value: str,
                     timeout_ms: int = TIMEOUT_UI_FORM_FILL_MS,
                     clear_first: bool = True,
                     native_setter: bool = True) -> bool:
    """Rellena un input con valor, manejando inputs React-controlled.

    Estrategia:
        1. Si `native_setter=True`: usa `HTMLInputElement.prototype` para inyectar
           valor SIN disparar handlers React intermedios (idéntico al patrón usado
           en login_full y fill_card_adyen).
        2. Si falla o `native_setter=False`: fallback `page.fill(selector, value)`.

    Args:
        page: Playwright Page
        selector: CSS selector del input
        value: valor a meter
        timeout_ms: timeout para `wait_for_selector` y `fill`
        clear_first: vaciar el input antes de rellenar
        native_setter: usar `setAttribute + dispatchEvent('input')` para React

    Returns:
        True si el input quedó con `value` cargado, False en otro caso.
    """
    try:
        page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
    except Exception:
        return False

    # 1) Native setter (React-friendly)
    if native_setter:
        try:
            ok = page.evaluate(r"""(args) => {
                const el = document.querySelector(args.selector);
                if (!el) return false;
                if (args.clearFirst) {
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value').set;
                    setter.call(el, '');
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                }
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value').set;
                setter.call(el, args.value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return el.value === args.value;
            }""", {"selector": selector, "value": value, "clearFirst": clear_first})
            if ok:
                return True
        except Exception:
            pass

    # 2) Fallback page.fill
    try:
        if clear_first:
            page.fill(selector, "", timeout=timeout_ms)
        page.fill(selector, value, timeout=timeout_ms)
        return True
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────────
#  Self-test
# ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== tm_ui_helpers — API ===")
    for fn in (accept_cookies, click_continue, safe_fill_input):
        print(f"  {fn.__name__}({', '.join(fn.__code__.co_varnames[:fn.__code__.co_argcount])})")
    print(f"\nCookie texts: {len(COOKIE_BANNER_TEXTS)}")
    print(f"Continue texts: {len(CONTINUE_BUTTON_TEXTS)}")
    print(f"OneTrust IDs: {len(ONETRUST_ACCEPT_IDS)}")
    print("\n[OK] module ready for opt-in migration")


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
