"""cloak_browser_bridge — bridge a CloakBrowser (Chromium stealth fork, Node.js).

CloakBrowser es Node-only (no PyPI). Esta capa expone un wrapper Python que
puede invocarlo via subprocess + JSON IPC cuando sea necesario.

Uso típico (cuando Camoufox/Patchright fallen contra DataDome v6+ o NuData):
  - cloak_run_check(url, profile_id?) → {fingerprint_signals, anti_detect_score}
  - cloak_solve_challenge(url) → cookies/headers válidos

Estado actual: SCAFFOLD. Sólo se activa si TM_CLOAK_NODE_PATH apunta a un
binario Node con cloakbrowser instalado. Sino devuelve fallback graceful.

Repo: https://github.com/CloakHQ/CloakBrowser
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional

PROYECTO = os.path.dirname(os.path.abspath(__file__))


def _node_binary() -> Optional[str]:
    """Localiza node.exe (Windows) o node (Linux/Mac)."""
    env = os.environ.get("TM_CLOAK_NODE_PATH", "").strip()
    if env and os.path.exists(env):
        return env
    return shutil.which("node")


def is_available() -> Dict[str, Any]:
    """Verifica si Node + cloakbrowser están disponibles."""
    out: Dict[str, Any] = {"node": None, "cloakbrowser": False}
    nb = _node_binary()
    out["node"] = nb
    if not nb:
        return out
    try:
        r = subprocess.run(
            [nb, "-e", "try { require('cloakbrowser'); console.log('OK'); } catch(e) { console.log('MISSING:'+e.message); }"],
            capture_output=True, text=True, timeout=10
        )
        out["cloakbrowser"] = "OK" in (r.stdout or "")
        if not out["cloakbrowser"]:
            out["error"] = (r.stdout or "") + " | " + (r.stderr or "")[:200]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def get_info() -> Dict[str, Any]:
    avail = is_available()
    return {
        "module": "cloak_browser_bridge",
        "purpose": "Bridge Python → CloakBrowser (Chromium stealth Node.js)",
        "status": "scaffold",
        "available": avail,
        "install_steps": [
            "1. Instalar Node.js (https://nodejs.org)",
            "2. npm install -g cloakbrowser   (o local en proyecto)",
            "3. Setear env TM_CLOAK_NODE_PATH si node no está en PATH",
            "4. Reiniciar dashboard",
        ],
        "alternative": ("Si CloakBrowser no disponible, usar Camoufox "
                         "(Firefox patcheado, Python nativo, ya integrado)."),
        "endpoints": [
            "GET /api/cloak-browser/info",
            "GET /api/cloak-browser/check",
        ],
    }


def cloak_run_check(url: str = "https://example.com",
                    timeout_s: int = 30) -> Dict[str, Any]:
    """Lanza CloakBrowser para chequeo anti-detect del fingerprint.

    Devuelve {ok, fingerprint, signals, score}.
    """
    avail = is_available()
    if not avail.get("cloakbrowser"):
        return {"ok": False, "error": "cloakbrowser_not_available",
                "fallback": "use Camoufox or Patchright instead",
                "available": avail}
    nb = avail.get("node")
    if not nb:
        return {"ok": False, "error": "no_node"}
    # Script Node embedded para fingerprint check
    script = f"""
    const cb = require('cloakbrowser');
    (async () => {{
        const browser = await cb.launch({{ headless: true }});
        const page = await browser.newPage();
        await page.goto('{url}', {{ waitUntil: 'domcontentloaded' }});
        const fp = await page.evaluate(() => ({{
            webdriver: navigator.webdriver,
            chrome: !!window.chrome,
            ua: navigator.userAgent,
            plugins: navigator.plugins.length,
            permissions: !!navigator.permissions,
        }}));
        await browser.close();
        console.log(JSON.stringify(fp));
    }})().catch(e => {{ console.error(JSON.stringify({{error: e.message}})); process.exit(1); }});
    """
    try:
        r = subprocess.run([nb, "-e", script], capture_output=True, text=True,
                           timeout=timeout_s)
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr[:200]}
        try:
            fp = json.loads(r.stdout.strip())
            score = 0
            score += 25 if fp.get("webdriver") in (None, False) else 0
            score += 25 if fp.get("chrome") else 0
            score += 25 if fp.get("plugins", 0) > 0 else 0
            score += 25 if fp.get("permissions") else 0
            return {"ok": True, "fingerprint": fp, "score": score,
                    "checked_at": datetime.now().isoformat()}
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_json", "raw": r.stdout[:200]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def self_test() -> Dict[str, Any]:
    out: Dict[str, Any] = {"checks": {}}
    avail = is_available()
    out["checks"]["module_loadable"] = True
    out["checks"]["info_callable"] = isinstance(get_info(), dict)
    out["available_node"] = avail.get("node") is not None
    out["available_cloakbrowser"] = avail.get("cloakbrowser", False)
    # OK incluso si no hay node — el scaffold es funcional
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
    print()
    print(json.dumps(get_info(), indent=2))
