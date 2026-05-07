"""tests/fakes/playwright_fake.py — Factory de Playwright pages fake.

Provee `make_fake_page(html, url)` que devuelve un objeto compatible con la
API de Playwright Page que usa `tm_browser_engine.py` y módulos similares.

API soportada (las que el módulo realmente llama):
  page.url                        → str
  page.title()                    → str
  page.content()                  → str (HTML)
  page.evaluate(js, *args)        → resultado mockeado por callback
  page.goto(url, ...)             → simula navegación
  page.locator(selector)          → FakeLocator
  page.screenshot(path=, ...)     → no-op (devuelve bytes vacíos)
  page.wait_for_timeout(ms)       → sleep
  page.context                    → FakeContext
  page.mouse                      → FakeMouse

  Locator.first / .nth(n)         → FakeLocator
  Locator.is_visible(timeout)     → bool
  Locator.click(...)              → no-op
  Locator.fill(text)              → registra valor
  Locator.type(text, delay)       → registra valor
  Locator.text_content()          → str
  Locator.get_attribute(name)     → str | None
  Locator.count()                 → int
  Locator.all()                   → list[FakeLocator]

Uso:
  from tests.fakes.playwright_fake import make_fake_page

  page = make_fake_page(
      html='<html><div class="cf-turnstile" data-sitekey="x"></div></html>',
      url='https://www.ticketmaster.es/event/123',
      eval_responses={
          # Para específicos page.evaluate(js): return value
          'document.body.innerText': 'select your seats',
      })
  ok = tm_browser_engine.try_presale_code(page, 'CODE2026')
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional


class FakeLocator:
    """Fake de Playwright Locator. Comportamiento configurable por atributo."""

    def __init__(self, page: "FakePage", selector: str,
                  exists: Optional[bool] = None,
                  visible: bool = True,
                  text: str = "",
                  attrs: Optional[Dict[str, str]] = None):
        self._page = page
        self._selector = selector
        # exists=None → infiere del HTML; True/False fuerza
        self._exists_override = exists
        self._visible = visible
        self._text = text
        self._attrs = attrs or {}
        self._fill_value: Optional[str] = None
        self._typed_value: str = ""
        self._clicked = False

    @property
    def first(self):
        return self

    def nth(self, idx: int):
        return self

    def _exists(self) -> bool:
        if self._exists_override is not None:
            return self._exists_override
        return self._page._selector_matches(self._selector)

    def is_visible(self, timeout: int = 0) -> bool:
        if not self._exists():
            # Playwright lanza TimeoutError si timeout>0; aquí simulamos
            return False
        return self._visible

    def is_hidden(self, timeout: int = 0) -> bool:
        return not self.is_visible(timeout)

    def click(self, timeout: int = 0, force: bool = False, **kwargs):
        if not self._exists():
            raise TimeoutError(f"Element not found: {self._selector}")
        self._clicked = True
        self._page._record_click(self._selector)

    def fill(self, value: str, timeout: int = 0):
        self._fill_value = value
        self._page._record_fill(self._selector, value)

    def type(self, text: str, delay: int = 0, timeout: int = 0):
        self._typed_value += text
        self._page._record_type(self._selector, text)

    def text_content(self, timeout: int = 0) -> str:
        return self._text

    def inner_text(self, timeout: int = 0) -> str:
        return self._text

    def get_attribute(self, name: str, timeout: int = 0) -> Optional[str]:
        return self._attrs.get(name)

    def count(self) -> int:
        return 1 if self._exists() else 0

    def all(self) -> List["FakeLocator"]:
        return [self] if self._exists() else []

    def wait_for(self, state: str = "visible", timeout: int = 0):
        if not self._exists():
            raise TimeoutError(f"Locator wait failed: {self._selector}")


class FakeMouse:
    def __init__(self, page):
        self._page = page
        self._moves: List[tuple] = []

    def move(self, x: float, y: float, steps: int = 1):
        self._moves.append(("move", x, y))

    def click(self, x: float, y: float, button: str = "left",
              click_count: int = 1, delay: int = 0):
        self._moves.append(("click", x, y))

    def down(self, button: str = "left"):
        self._moves.append(("down", button))

    def up(self, button: str = "left"):
        self._moves.append(("up", button))

    def wheel(self, dx: float, dy: float):
        self._moves.append(("wheel", dx, dy))


class FakeContext:
    def __init__(self, pages: Optional[List["FakePage"]] = None,
                  cookies: Optional[List[Dict]] = None):
        self.pages = pages or []
        self._cookies = cookies or []
        self._init_scripts: List[str] = []

    def new_page(self) -> "FakePage":
        p = FakePage()
        p.context = self
        self.pages.append(p)
        return p

    def cookies(self, urls: Optional[List[str]] = None) -> List[Dict]:
        return list(self._cookies)

    def add_cookies(self, cookies: List[Dict]):
        self._cookies.extend(cookies)

    def add_init_script(self, script: str = "", path: str = ""):
        self._init_scripts.append(script or path)

    def set_default_timeout(self, timeout: int):
        pass

    def close(self):
        pass

    def clear_cookies(self):
        self._cookies.clear()


class FakePage:
    """Fake de Playwright Page configurable.

    Args:
      html:           HTML que devuelve `content()` y se inspecciona para selectors
      url:            URL inicial
      title:          Título
      eval_responses: dict {js_substring: return_value} — si el JS de evaluate
                      contiene cualquier substring de la key, devuelve el value.
                      Si no hay match, devuelve None.
      visible_selectors: set de selectors que se reportan visibles (default: detect del HTML)
      missing_selectors: set de selectors que NO existen (override)
    """

    def __init__(self,
                  html: str = "<html><body></body></html>",
                  url: str = "https://example.com",
                  title: str = "Fake Page",
                  eval_responses: Optional[Dict[str, Any]] = None,
                  eval_callback: Optional[Callable[[str], Any]] = None,
                  visible_selectors: Optional[set] = None,
                  missing_selectors: Optional[set] = None):
        self._html = html
        self.url = url
        self._title = title
        self._eval_responses = eval_responses or {}
        self._eval_callback = eval_callback
        self._visible_selectors = visible_selectors
        self._missing_selectors = missing_selectors or set()
        self.context = FakeContext()
        self.mouse = FakeMouse(self)
        self._navigations: List[str] = []
        self._clicks: List[str] = []
        self._fills: List[tuple] = []
        self._types: List[tuple] = []
        self._screenshots: List[str] = []
        self._evals: List[str] = []
        self._waited_ms: int = 0
        self._routes: Dict[str, Callable] = {}
        self._console_handlers: List[Callable] = []
        self._page_error_handlers: List[Callable] = []
        self._listeners: Dict[str, List[Callable]] = {}

    # ─── Navegación / contenido ──────────────────────────────────
    def goto(self, url: str, timeout: int = 0,
              wait_until: str = "load", **kwargs):
        self.url = url
        self._navigations.append(url)
        return None  # Playwright devuelve Response, aquí None

    def reload(self, timeout: int = 0, wait_until: str = "load"):
        self._navigations.append(self.url + "#reload")

    def go_back(self):
        pass

    def go_forward(self):
        pass

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self._html

    def set_content(self, html: str):
        self._html = html

    def close(self, **kwargs):
        pass

    # ─── Eval / scripts ──────────────────────────────────────────
    def evaluate(self, js: str, *args) -> Any:
        self._evals.append(js)
        if self._eval_callback:
            return self._eval_callback(js)
        # Buscar substring match
        for marker, value in self._eval_responses.items():
            if marker in js:
                return value
        # Defaults inteligentes para comportamientos comunes
        if "document.body.innerText" in js or "document.body.textContent" in js:
            # Strip HTML tags y devolver texto
            text = re.sub(r"<[^>]+>", " ", self._html)
            return re.sub(r"\s+", " ", text).strip()
        if "data-sitekey" in js or "getAttribute" in js:
            m = re.search(r'data-sitekey=["\']([^"\']+)["\']', self._html)
            return m.group(1) if m else None
        if "navigator." in js:
            return None
        return None

    def add_init_script(self, script: str = "", path: str = ""):
        self.context.add_init_script(script, path)

    # ─── Locators ────────────────────────────────────────────────
    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector,
                              visible=(selector not in self._missing_selectors))

    def query_selector(self, selector: str) -> Optional[FakeLocator]:
        if self._selector_matches(selector):
            return FakeLocator(self, selector)
        return None

    def query_selector_all(self, selector: str) -> List[FakeLocator]:
        if self._selector_matches(selector):
            return [FakeLocator(self, selector)]
        return []

    def _selector_matches(self, selector: str) -> bool:
        """Heurística: chequea si el selector aparece (parcial) en el HTML."""
        if selector in self._missing_selectors:
            return False
        if self._visible_selectors and selector in self._visible_selectors:
            return True
        # Match básico de class/id/attr en HTML
        # .cf-turnstile → busca class="cf-turnstile"
        m = re.match(r"\.([\w-]+)", selector)
        if m and f'class="{m.group(1)}"' in self._html:
            return True
        if m and f"class='{m.group(1)}'" in self._html:
            return True
        if m and m.group(1) in self._html:
            return True
        # input[name="foo"]
        m = re.match(r'\w+\[(\w+)=["\']([^"\']+)["\']', selector)
        if m and f'{m.group(1)}="{m.group(2)}"' in self._html:
            return True
        # iframe[src*="..."]
        m = re.search(r'\[src\*=["\']([^"\']+)["\']', selector)
        if m and m.group(1).lower() in self._html.lower():
            return True
        # #id
        m = re.match(r"#([\w-]+)", selector)
        if m and f'id="{m.group(1)}"' in self._html:
            return True
        return False

    # ─── Screenshots / waits ─────────────────────────────────────
    def screenshot(self, path: Optional[str] = None,
                    full_page: bool = False, type: str = "png",
                    **kwargs) -> bytes:
        if path:
            self._screenshots.append(path)
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # fake PNG

    def wait_for_timeout(self, ms: int):
        self._waited_ms += ms

    def wait_for_url(self, url: str, timeout: int = 0, **kwargs):
        pass

    def wait_for_selector(self, selector: str, timeout: int = 0,
                            state: str = "visible") -> Optional[FakeLocator]:
        if self._selector_matches(selector):
            return FakeLocator(self, selector)
        raise TimeoutError(f"wait_for_selector failed: {selector}")

    def wait_for_load_state(self, state: str = "load", timeout: int = 0):
        pass

    def wait_for_function(self, expression: str, timeout: int = 0, **kwargs):
        pass

    # ─── Eventos ─────────────────────────────────────────────────
    def on(self, event: str, handler: Callable):
        self._listeners.setdefault(event, []).append(handler)
        if event == "console":
            self._console_handlers.append(handler)
        elif event == "pageerror":
            self._page_error_handlers.append(handler)

    def remove_listener(self, event: str, handler: Callable):
        if event in self._listeners and handler in self._listeners[event]:
            self._listeners[event].remove(handler)

    # ─── Routing (para route fulfill) ────────────────────────────
    def route(self, pattern: str, handler: Callable):
        self._routes[pattern] = handler

    def unroute(self, pattern: str, handler: Optional[Callable] = None):
        self._routes.pop(pattern, None)

    # ─── Bring / focus ───────────────────────────────────────────
    def bring_to_front(self):
        pass

    def focus(self, selector: str, timeout: int = 0):
        pass

    def keyboard_type(self, text: str, delay: int = 0):
        pass

    @property
    def keyboard(self):
        return _FakeKeyboard()

    # ─── Helpers de inspección para tests ─────────────────────────
    def _record_click(self, selector: str):
        self._clicks.append(selector)

    def _record_fill(self, selector: str, value: str):
        self._fills.append((selector, value))

    def _record_type(self, selector: str, value: str):
        self._types.append((selector, value))

    def get_recorded_actions(self) -> Dict[str, Any]:
        """Inspect helper para tests."""
        return {
            "navigations": list(self._navigations),
            "clicks": list(self._clicks),
            "fills": list(self._fills),
            "types": list(self._types),
            "screenshots": list(self._screenshots),
            "waited_ms": self._waited_ms,
            "evals_count": len(self._evals),
        }


class _FakeKeyboard:
    def type(self, text: str, delay: int = 0):
        pass

    def press(self, key: str):
        pass

    def down(self, key: str):
        pass

    def up(self, key: str):
        pass


class FakeBrowser:
    def __init__(self):
        self.contexts: List[FakeContext] = []
        self._closed = False

    def new_context(self, **kwargs) -> FakeContext:
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx

    def new_page(self, **kwargs) -> FakePage:
        ctx = self.new_context()
        return ctx.new_page()

    def close(self):
        self._closed = True

    def is_connected(self) -> bool:
        return not self._closed


class FakeBrowserType:
    """Mocks p.chromium / p.firefox / p.webkit."""

    def __init__(self, name: str = "chromium"):
        self.name = name

    def launch(self, headless: bool = True, **kwargs) -> FakeBrowser:
        return FakeBrowser()

    def launch_persistent_context(self, user_data_dir: str,
                                     headless: bool = True, **kwargs) -> FakeContext:
        return FakeContext()


class FakeSyncPlaywright:
    """Mocks `sync_playwright()` context manager."""

    def __init__(self):
        self.chromium = FakeBrowserType("chromium")
        self.firefox = FakeBrowserType("firefox")
        self.webkit = FakeBrowserType("webkit")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def stop(self):
        pass


def fake_sync_playwright():
    """Replacement para `playwright.sync_api.sync_playwright()`."""
    return FakeSyncPlaywright()


# ═══════════════════════════════════════════════════════════════
#  Factory pública
# ═══════════════════════════════════════════════════════════════
def make_fake_page(html: str = "<html><body></body></html>",
                     url: str = "https://example.com",
                     title: str = "Fake Page",
                     eval_responses: Optional[Dict[str, Any]] = None,
                     missing_selectors: Optional[set] = None) -> FakePage:
    """Construye una página fake lista para usar en tests.

    Args:
      html: contenido HTML
      url: URL inicial
      title: título
      eval_responses: dict {js_substring: return_value}
      missing_selectors: set de selectors a forzar como NO existentes
    """
    return FakePage(
        html=html, url=url, title=title,
        eval_responses=eval_responses or {},
        missing_selectors=missing_selectors or set(),
    )


def make_fake_browser_stack():
    """Devuelve (sync_playwright_fn, browser_factory, page_factory)
    para tests que necesiten todo el stack."""
    return (fake_sync_playwright, FakeBrowserType, make_fake_page)
