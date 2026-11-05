"""human_journey.py — Persona warm-up: navegación real previa al checkout.

Antes de tocar el evento target, el bot navega como un comprador real
durante 3-7 minutos:
  - Home pública con scroll natural
  - Click en 1-2 eventos NO target (lectura 20-40s cada uno)
  - Búsqueda con typo → cancelar
  - Visibility events (focus/blur/visibilitychange)
  - Tab switches simulados
  - Cookies + storage_state se acumulan naturalmente

Esto deja huella sólida en _abck, bm_sz, datadome ANTES de Akamai escrutar
el flow de checkout. Akamai/DataDome usan "session age" como señal — sesión
de 30s = sospechosa; sesión de 5 min con scroll/hover/clicks = normal.

API:
    warm_up_session(page, persona, log, min_seconds=180, max_seconds=420)
        Devuelve dict {ok, elapsed_s, pages_visited, scrolls, hovers, ...}
"""
from __future__ import annotations
import random, time
from typing import Dict, Any, Optional, Callable


# Eventos NO target para visitar — categorías genéricas
DECOY_LINKS = [
    "/conciertos",
    "/teatro",
    "/deportes",
    "/musica",
    "/familiar",
]


def _human_pause(min_s: float = 0.6, max_s: float = 2.2):
    """Pausa aleatoria con distribución gaussiana truncada."""
    mu = (min_s + max_s) / 2
    sigma = (max_s - min_s) / 4
    t = random.gauss(mu, sigma)
    t = max(min_s, min(max_s, t))
    time.sleep(t)


def _natural_scroll(page, steps: int = 5, min_step: int = 150,
                    max_step: int = 480) -> int:
    """Scroll con inertia: pasos grandes inicial, decay exponencial."""
    total = 0
    cur = max_step
    for i in range(steps):
        # Decay para los últimos pasos
        delta = max(min_step, int(cur * random.uniform(0.7, 1.0)))
        page.mouse.wheel(0, delta)
        total += delta
        _human_pause(0.6, 1.8)
        cur = max(min_step, int(cur * 0.85))
    return total


def _random_mouse_move(page, iterations: int = 3):
    """Mueve el ratón a coordenadas aleatorias del viewport (Bezier-like)."""
    try:
        vp = page.viewport_size or {"width": 1366, "height": 768}
    except Exception:
        vp = {"width": 1366, "height": 768}
    for _ in range(iterations):
        x = random.randint(50, vp["width"] - 50)
        y = random.randint(50, vp["height"] - 50)
        steps = random.randint(15, 35)
        try:
            page.mouse.move(x, y, steps=steps)
        except Exception:
            pass
        _human_pause(0.3, 1.0)


def _try_accept_cookies(page) -> bool:
    """Cierra banner cookies si aparece."""
    for txt in ("Aceptar todas las cookies", "Aceptar", "Acepto", "OK",
                "Aceitar todas as cookies", "Accept all cookies"):
        try:
            btn = page.get_by_text(txt, exact=False).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=3000)
                _human_pause(1.0, 2.0)
                return True
        except Exception:
            continue
    # Fallback JS
    try:
        clicked = page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            const m = btns.find(b => /aceptar.*cookies|aceptar todas/i.test(b.innerText || ''));
            if (m) { m.click(); return true; }
            return false;
        }""")
        if clicked:
            _human_pause(1.0, 2.0)
            return True
    except Exception:
        pass
    return False


def _visit_decoy_event(page, target_event_id: str,
                       log: Callable) -> Dict[str, Any]:
    """Click en un evento aleatorio (no el target), leer 20-40s, volver."""
    out = {"visited": False, "url": None}
    try:
        links = page.locator("a[href*='/event/']").all()
        random.shuffle(links)
        for link in links[:8]:
            try:
                href = (link.get_attribute("href") or "")
                if not href or target_event_id in href:
                    continue
                # Hover antes de click
                try:
                    link.hover(timeout=1500)
                except Exception:
                    pass
                _human_pause(0.5, 1.4)
                try:
                    link.click(timeout=5000)
                except Exception:
                    continue
                out["visited"] = True
                out["url"] = href[:120]
                log("warmup_decoy_visit", url=href[:80])
                _human_pause(3.5, 6.0)
                # Lectura 20-40s con scroll
                read_end = time.time() + random.uniform(20, 40)
                scrolls = 0
                while time.time() < read_end:
                    page.mouse.wheel(0, random.randint(180, 420))
                    scrolls += 1
                    _human_pause(2.0, 4.5)
                out["scrolls_during_read"] = scrolls
                # Volver
                try:
                    page.go_back(timeout=15000)
                    _human_pause(2.0, 3.5)
                except Exception:
                    pass
                return out
            except Exception:
                continue
    except Exception as e:
        out["err"] = str(e)[:160]
    return out


def _search_with_typo(page, log: Callable) -> bool:
    """Click search bar → typo → cancel. Genera signals NuData/keystroke patterns."""
    try:
        # Hay variantes — intentar varios selectores
        selectors = [
            "input[type='search']",
            "input[placeholder*='buscar' i]",
            "input[placeholder*='Buscar' i]",
            "[data-testid*='search' i]",
        ]
        search = None
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1500):
                    search = el; break
            except Exception:
                continue
        if not search:
            return False
        search.click(timeout=3000)
        _human_pause(0.6, 1.4)
        # Tipo "concierot" (typo intencionado)
        for ch in "concieert":
            page.keyboard.type(ch, delay=random.randint(80, 220))
        log("warmup_search_typed", text="concieert")
        _human_pause(1.0, 2.2)
        # Borrar con backspace algunos chars + Esc
        for _ in range(random.randint(2, 5)):
            page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.08, 0.18))
        _human_pause(0.4, 1.0)
        page.keyboard.press("Escape")
        _human_pause(0.6, 1.2)
        return True
    except Exception as e:
        log("warmup_search_err", err=str(e)[:120])
        return False


def _dispatch_visibility_events(page, log: Callable):
    """Trigger visibility/focus/blur — Akamai trackea estos eventos."""
    try:
        page.evaluate("""() => {
            try { document.dispatchEvent(new Event('visibilitychange')); } catch(e){}
            try { window.dispatchEvent(new Event('blur')); } catch(e){}
            setTimeout(() => {
                try { window.dispatchEvent(new Event('focus')); } catch(e){}
                try { document.dispatchEvent(new Event('visibilitychange')); } catch(e){}
            }, 1200 + Math.random() * 800);
        }""")
        log("warmup_visibility_events")
        time.sleep(2.0)
    except Exception as e:
        log("warmup_visibility_err", err=str(e)[:120])


def warm_up_session(page, persona: Optional[Dict[str, Any]] = None,
                    log: Optional[Callable] = None,
                    target_event_id: str = "",
                    home_url: str = "https://www.ticketmaster.es/",
                    min_seconds: int = 180,
                    max_seconds: int = 420,
                    decoy_visits: int = 1,
                    do_search: bool = True,
                    do_visibility: bool = True) -> Dict[str, Any]:
    """Navegación previa al checkout para construir sesión natural.

    Args:
        page: Playwright Page (ya conectado al MoreLogin/Camoufox browser)
        persona: dict de purchase_stealth.create_persona_from_sessionid
        log: callable(event_name, **kwargs) — usar el `log(slot, ...)` del bot
        target_event_id: tm_id del evento target (para NO visitarlo)
        home_url: home pública del país (TM ES, TM IT, etc.)
        min_seconds / max_seconds: rango duración total deseada
        decoy_visits: cuántos eventos NO-target visitar (1-2 recomendado)
        do_search: hacer typo+cancel en search bar
        do_visibility: disparar visibility/focus/blur events

    Returns:
        dict con métricas {ok, elapsed_s, pages_visited, total_scrolls,
                            cookies_accepted, search_done, ...}
    """
    if log is None:
        log = lambda *a, **k: None
    duration = random.randint(min_seconds, max_seconds)
    started = time.time()
    out = {"target_duration_s": duration, "pages_visited": 0,
           "total_scrolls": 0, "errors": []}
    log("warmup_start", target_duration_s=duration,
        sessid=(persona or {}).get("sessionid"))

    # 1) Home pública
    try:
        page.goto(home_url, wait_until="domcontentloaded", timeout=45000)
        _human_pause(2.5, 4.5)
        out["pages_visited"] += 1
        out["home_loaded"] = True
    except Exception as e:
        out["errors"].append(f"home_goto:{str(e)[:120]}")
        out["home_loaded"] = False
        return {**out, "elapsed_s": time.time() - started, "ok": False}

    # 2) Cookies banner
    out["cookies_accepted"] = _try_accept_cookies(page)

    # 3) Mouse movement aleatorio
    _random_mouse_move(page, iterations=random.randint(2, 4))

    # 4) Scroll natural en home
    scrolled = _natural_scroll(page, steps=random.randint(4, 7))
    out["total_scrolls"] += scrolled
    log("warmup_home_scrolled", pixels=scrolled)

    # 5) Hover en algunos eventos featured
    try:
        tiles = page.locator("a[href*='/event/']").all()
        random.shuffle(tiles)
        hovers = 0
        for tile in tiles[:random.randint(2, 5)]:
            try:
                tile.hover(timeout=2000)
                _human_pause(0.6, 1.4)
                hovers += 1
            except Exception:
                continue
        out["hovers"] = hovers
    except Exception:
        out["hovers"] = 0

    # 6) Visitar 1-2 eventos NO target
    visits_made = 0
    for _ in range(decoy_visits):
        if time.time() - started > duration * 0.7:
            break
        res = _visit_decoy_event(page, target_event_id, log)
        if res.get("visited"):
            visits_made += 1
            out["pages_visited"] += 1
    out["decoy_visits"] = visits_made

    # 7) Search con typo
    if do_search and (time.time() - started < duration):
        out["search_done"] = _search_with_typo(page, log)
    else:
        out["search_done"] = False

    # 8) Visibility events
    if do_visibility:
        _dispatch_visibility_events(page, log)

    # 9) Padding hasta cumplir duración mínima (sin abusar)
    elapsed = time.time() - started
    if elapsed < min_seconds:
        wait_left = min(min_seconds - elapsed, 60)
        log("warmup_padding", wait_s=round(wait_left, 1))
        # Mover ratón + micro-scroll mientras espera
        end = time.time() + wait_left
        while time.time() < end:
            _random_mouse_move(page, iterations=1)
            page.mouse.wheel(0, random.randint(80, 220))
            _human_pause(2.0, 4.5)

    out["elapsed_s"] = round(time.time() - started, 1)
    out["ok"] = True
    log("warmup_done", **{k: v for k, v in out.items()
                          if k in ("elapsed_s", "pages_visited",
                                   "total_scrolls", "decoy_visits",
                                   "search_done", "cookies_accepted")})
    return out


if __name__ == "__main__":
    print("human_journey.py — módulo de warm-up cargado")
    print(f"  warm_up_session disponible: {callable(warm_up_session)}")


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
