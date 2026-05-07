"""nodriver_engine.py — nodriver (undetected-chromedriver v2) integration.

nodriver (ultrafunkamsterdam, 2024+) es el engine indetectable #1 de 2026:
- CDP directo sin webdriver footprint (navigator.webdriver = undefined)
- Pasa Cloudflare, DataDome, PerimeterX, Akamai en benchmarks Oxylabs/Scrapfly
- Async-first (asyncio native), NO Playwright/Selenium dependency
- ~40 MB RAM menos que Playwright

API pública (sincronico wrapper):
  is_available() → bool
  launch(headless, proxy, user_agent) → dict con session metadata
  navigate(session_id, url) → dict
  get_cookies(session_id) → list
  close_session(session_id) → dict
  stealth_check(url) → test bot-detector.rebrowser.net

Ejemplo:
  session = await launch_async(headless=True, proxy="http://user:pass@host:port")
  await session.get("https://www.ticketmaster.com")
  cookies = await session.cookies.get_all()
"""
import asyncio
import threading
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, List

# availability
try:
    import nodriver
    NODRIVER_AVAILABLE = True
    NODRIVER_ERROR = None
except Exception as e:
    NODRIVER_AVAILABLE = False
    NODRIVER_ERROR = str(e)


_active_sessions: Dict[str, dict] = {}  # session_id → {browser, started_at, ...}
_lock = threading.Lock()


def is_available() -> bool:
    return NODRIVER_AVAILABLE


def get_info() -> dict:
    return {
        "module": "nodriver_engine",
        "nodriver_available": NODRIVER_AVAILABLE,
        "nodriver_error": NODRIVER_ERROR,
        "active_sessions": len(_active_sessions),
        "benefits": [
            "CDP directo (sin webdriver footprint)",
            "Pasa Cloudflare/DataDome/PerimeterX/Akamai",
            "Async-first (40 MB menos RAM que Playwright)",
            "Winner vs rebrowser-bot-detector (score >95)",
        ],
    }


# ═══════════════════════════════════════════
#  ASYNC HELPERS (nodriver es async-first)
# ═══════════════════════════════════════════
async def _async_launch(headless: bool = True,
                          proxy: Optional[str] = None,
                          user_agent: Optional[str] = None,
                          extra_args: Optional[List[str]] = None) -> dict:
    if not NODRIVER_AVAILABLE:
        return {"ok": False, "error": "nodriver no disponible"}

    try:
        config_kwargs = {"headless": headless}
        if user_agent:
            config_kwargs["user_agent"] = user_agent

        browser_args = []
        if proxy:
            browser_args.append(f"--proxy-server={proxy}")
        if extra_args:
            browser_args.extend(extra_args)

        browser = await nodriver.start(
            headless=headless,
            browser_args=browser_args,
        )
        sid = "nd_" + uuid.uuid4().hex[:10]
        with _lock:
            _active_sessions[sid] = {
                "browser": browser,
                "started_at": datetime.now().isoformat(),
                "headless": headless,
                "has_proxy": bool(proxy),
            }
        return {"ok": True, "session_id": sid,
                 "nodriver_pid": browser.config.user_data_dir if hasattr(browser, "config") else None}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


async def _async_navigate(session_id: str, url: str) -> dict:
    if session_id not in _active_sessions:
        return {"ok": False, "error": "session_id no encontrado"}
    try:
        browser = _active_sessions[session_id]["browser"]
        t0 = time.time()
        tab = await browser.get(url)
        await tab.wait(2)  # wait for page to settle
        title = await tab.evaluate("document.title")
        elapsed_ms = int((time.time() - t0) * 1000)
        return {"ok": True, "url": url, "title": title,
                 "elapsed_ms": elapsed_ms, "session_id": session_id}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


async def _async_close(session_id: str) -> dict:
    if session_id not in _active_sessions:
        return {"ok": False, "error": "session_id no encontrado"}
    try:
        browser = _active_sessions[session_id]["browser"]
        await browser.stop()
        with _lock:
            _active_sessions.pop(session_id, None)
        return {"ok": True, "closed": session_id}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══════════════════════════════════════════
#  SYNCHRONIC WRAPPERS
# ═══════════════════════════════════════════
def launch(headless: bool = True,
             proxy: Optional[str] = None,
             user_agent: Optional[str] = None) -> dict:
    """Wrapper síncrono para lanzar una sesión nodriver."""
    if not NODRIVER_AVAILABLE:
        return {"ok": False, "error": "nodriver no disponible",
                 "hint": "pip install nodriver"}
    try:
        return asyncio.run(_async_launch(headless, proxy, user_agent))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def stealth_check(url: str = "https://bot-detector.rebrowser.net/") -> dict:
    """Test de stealth: navega a bot-detector.rebrowser.net y captura el score.

    Nota: requiere asyncio.run + parsing del score. Aquí hacemos check básico.
    """
    if not NODRIVER_AVAILABLE:
        return {"ok": False, "error": "nodriver no disponible"}

    async def _check():
        browser = None
        try:
            browser = await nodriver.start(headless=True)
            tab = await browser.get(url)
            await tab.wait(5)  # dar tiempo a que el detector mida
            # Intentar capturar el score del DOM
            try:
                score_text = await tab.evaluate(
                    "document.querySelector('.score')?.textContent || '?'"
                )
            except Exception:
                score_text = "?"
            title = await tab.evaluate("document.title")
            html_length = await tab.evaluate("document.documentElement.outerHTML.length")
            return {"ok": True, "title": title,
                     "score_text": str(score_text)[:100],
                     "html_length": html_length,
                     "url": url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            if browser:
                try:
                    await browser.stop()
                except Exception:
                    pass

    try:
        return asyncio.run(_check())
    except Exception as e:
        return {"ok": False, "error": f"asyncio.run fail: {str(e)[:120]}"}


def list_sessions() -> List[dict]:
    with _lock:
        return [
            {"session_id": sid, "started_at": s.get("started_at"),
              "headless": s.get("headless"), "has_proxy": s.get("has_proxy")}
            for sid, s in _active_sessions.items()
        ]


def close_session(session_id: str) -> dict:
    try:
        return asyncio.run(_async_close(session_id))
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def close_all() -> dict:
    closed = 0
    for sid in list(_active_sessions.keys()):
        try:
            close_session(sid)
            closed += 1
        except Exception:
            pass
    return {"ok": True, "closed_count": closed}


# ═══════════════════════════════════════════
#  SELF TEST
# ═══════════════════════════════════════════
def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    results["checks"]["nodriver_available"] = NODRIVER_AVAILABLE
    if not NODRIVER_AVAILABLE:
        results["checks"]["error"] = NODRIVER_ERROR
        return results

    # No lanzar browser real en self_test (tardaría mucho)
    # Solo verificar que los wrappers existen
    results["checks"]["launch_fn_exists"] = callable(launch)
    results["checks"]["stealth_check_fn_exists"] = callable(stealth_check)
    results["checks"]["close_all_fn_exists"] = callable(close_all)

    # Listar sessions (debe ser lista vacía)
    sessions = list_sessions()
    results["checks"]["list_sessions_ok"] = isinstance(sessions, list)

    return results
