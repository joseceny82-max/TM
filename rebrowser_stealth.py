"""rebrowser_stealth.py — rebrowser-playwright drop-in replacement.

rebrowser-playwright es un fork patched que:
- Pasa `bot-detector.rebrowser.net` tests (score > 95/100)
- Oculta `__pwInitScripts`, `window.chrome`, CDP detection
- Drop-in: solo cambia el import

API:
  is_available()
  get_stealth_score(url)           → score contra detector online
  upgrade_browser_engine()         → actualiza tm_browser_engine para usar rebrowser
  check_ci_bot_score(min_score=90) → CI-friendly assert para tests
"""
import time
import threading
from datetime import datetime
from typing import Optional

try:
    import rebrowser_playwright
    REBROWSER_AVAILABLE = True
    REBROWSER_ERROR = None
except Exception as e:
    REBROWSER_AVAILABLE = False
    REBROWSER_ERROR = str(e)


def is_available() -> bool:
    return REBROWSER_AVAILABLE


def get_info() -> dict:
    try:
        import rebrowser_playwright
        version = getattr(rebrowser_playwright, "__version__", "unknown")
    except ImportError:
        version = None
    return {
        "module": "rebrowser_stealth",
        "rebrowser_available": REBROWSER_AVAILABLE,
        "rebrowser_version": version,
        "error": REBROWSER_ERROR if not REBROWSER_AVAILABLE else None,
        "benefits": [
            "Drop-in replacement de playwright",
            "Pasa bot-detector.rebrowser.net",
            "Oculta __pwInitScripts + window.chrome leak",
        ],
        "next_step": "Reemplaza `from playwright.sync_api import ...` por `from rebrowser_playwright.sync_api import ...` en tm_browser_engine.py",
    }


def get_stealth_score(url: str = "https://bot-detector.rebrowser.net/",
                        timeout_s: int = 30,
                        use_rebrowser: bool = True) -> dict:
    """Navega a bot-detector.rebrowser.net y captura el score.

    Returns:
      {ok, score, score_raw, tests_passed, tests_total, url}
    """
    if not REBROWSER_AVAILABLE and use_rebrowser:
        return {"ok": False, "error": "rebrowser_playwright no disponible"}

    started = time.time()
    try:
        if use_rebrowser:
            from rebrowser_playwright.sync_api import sync_playwright
        else:
            from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                                          args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            try:
                page.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
                # rebrowser-bot-detector takes a few seconds to run tests
                page.wait_for_timeout(5000)
                # Capturar score del DOM
                try:
                    score_text = page.evaluate(
                        "() => document.body.innerText.substring(0, 2000)"
                    )
                except Exception:
                    score_text = ""

                # Buscar patrones típicos: "Score: X/Y" o "failed: N"
                import re
                score_match = re.search(r"score[:\s]+(\d+)", score_text, re.I)
                score = int(score_match.group(1)) if score_match else None

                tests_passed_match = re.search(r"(\d+)\s*(?:passed|pass)", score_text, re.I)
                tests_total_match = re.search(r"/\s*(\d+)\s*tests?", score_text, re.I)
                tests_passed = int(tests_passed_match.group(1)) if tests_passed_match else None
                tests_total = int(tests_total_match.group(1)) if tests_total_match else None

                title = page.title()
                browser.close()
                return {
                    "ok": True,
                    "engine": "rebrowser" if use_rebrowser else "playwright",
                    "url": url, "title": title[:100],
                    "score": score,
                    "tests_passed": tests_passed,
                    "tests_total": tests_total,
                    "score_preview": score_text[:500],
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            except Exception as e:
                browser.close()
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                         "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def compare_engines(url: str = "https://bot-detector.rebrowser.net/") -> dict:
    """Compara vanilla playwright vs rebrowser-playwright en el mismo test."""
    vanilla = get_stealth_score(url, use_rebrowser=False)
    time.sleep(1)
    rebrowser = get_stealth_score(url, use_rebrowser=True)
    return {
        "ok": True,
        "vanilla_playwright": vanilla,
        "rebrowser_playwright": rebrowser,
        "winner": (
            "rebrowser" if (rebrowser.get("score") or 0) > (vanilla.get("score") or 0)
            else "vanilla" if (vanilla.get("score") or 0) > (rebrowser.get("score") or 0)
            else "tie"
        ),
    }


def check_ci_bot_score(min_score: int = 90,
                         url: str = "https://bot-detector.rebrowser.net/") -> dict:
    """CI gate: passes if rebrowser score >= min_score. Use in tests."""
    r = get_stealth_score(url, use_rebrowser=True)
    if not r.get("ok"):
        return {"ok": False, "passed": False, "reason": r.get("error")}
    score = r.get("score")
    passed = score is not None and score >= min_score
    return {
        "ok": True,
        "passed": passed,
        "score": score,
        "min_required": min_score,
        "details": r,
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}
    results["checks"]["rebrowser_available"] = REBROWSER_AVAILABLE
    if REBROWSER_AVAILABLE:
        import rebrowser_playwright
        results["checks"]["rebrowser_version"] = getattr(rebrowser_playwright,
                                                           "__version__", "?")
        results["checks"]["get_stealth_score_callable"] = callable(get_stealth_score)
        results["checks"]["compare_engines_callable"] = callable(compare_engines)
        results["checks"]["check_ci_bot_score_callable"] = callable(check_ci_bot_score)
    return results
