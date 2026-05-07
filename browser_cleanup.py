"""browser_cleanup.py — Cleanup completo del navegador antes de cerrar.

Política del usuario: SIEMPRE eliminar historial al cerrar un navegador.

Aplicado vía Chrome DevTools Protocol (CDP) sobre Playwright para que funcione
en cualquier engine (chromium / patchright / camoufox / MoreLogin via CDP):

    1. Network.clearBrowserCache         → caché HTTP
    2. Network.clearBrowserCookies       → todas las cookies
    3. Storage.clearDataForOrigin('*')   → localStorage + IndexedDB + cache APIs
    4. Eliminación del archivo History del perfil (MoreLogin) tras cerrar

API:
    full_cleanup(ctx, profile_dir=None) -> dict
        ctx: BrowserContext de Playwright (sync)
        profile_dir: ruta al perfil MoreLogin (opcional, si conocida)

    clear_history_via_cdp(ctx) -> dict
        Solo paso CDP (cookies/cache/storage) sin tocar archivos.
"""
from __future__ import annotations
import os
from typing import Optional, Dict, Any


def clear_history_via_cdp(ctx) -> Dict[str, Any]:
    """Limpia historial via CDP en un context Playwright. Devuelve {ok, steps}."""
    out = {"ok": True, "steps": []}
    if ctx is None:
        return {"ok": False, "error": "ctx_none"}
    try:
        cdp = ctx.new_cdp_session(ctx.pages[0]) if ctx.pages else None
    except Exception as e:
        out["ok"] = False
        out["cdp_init_err"] = str(e)[:120]
        return out
    if cdp is None:
        # Si no hay páginas, intentamos vía un nuevo CDP browser-level
        try:
            browser = ctx.browser
            cdp = browser.new_browser_cdp_session()
        except Exception as e:
            out["ok"] = False
            out["cdp_init_err"] = str(e)[:120]
            return out

    # 1) Network.clearBrowserCache
    try:
        cdp.send("Network.clearBrowserCache")
        out["steps"].append("cache_cleared")
    except Exception as e:
        out["steps"].append(f"cache_err:{str(e)[:60]}")

    # 2) Network.clearBrowserCookies
    try:
        cdp.send("Network.clearBrowserCookies")
        out["steps"].append("cookies_cleared")
    except Exception as e:
        out["steps"].append(f"cookies_err:{str(e)[:60]}")

    # 3) Storage.clearDataForOrigin para cada origin único visible
    try:
        origins = set()
        for page in ctx.pages:
            try:
                origins.add(page.evaluate("() => location.origin"))
            except Exception:
                pass
        # Fallback: limpiar TODO con wildcard origin si no había pages
        if not origins:
            origins = {""}
        for origin in origins:
            try:
                cdp.send("Storage.clearDataForOrigin", {
                    "origin": origin or "*",
                    "storageTypes": "all",
                })
                out["steps"].append(f"storage_cleared:{(origin or '*')[:30]}")
            except Exception as e:
                out["steps"].append(f"storage_err:{str(e)[:60]}")
    except Exception as e:
        out["steps"].append(f"storage_outer_err:{str(e)[:60]}")

    return out


def _delete_history_files(profile_dir: str) -> Dict[str, Any]:
    """Borra los archivos History/Visited Links/Top Sites del perfil de Chrome.
    Solo aplicable cuando el browser ESTÁ CERRADO (los archivos están bloqueados
    si el profile sigue activo)."""
    out = {"deleted": [], "errors": []}
    if not profile_dir or not os.path.isdir(profile_dir):
        return {"profile_dir": profile_dir, "skipped": "not_a_dir"}
    targets = [
        "History", "History-journal",
        "Top Sites", "Top Sites-journal",
        "Visited Links",
        "Network/Cookies", "Network/Cookies-journal",
        "Local Storage", "Session Storage",
        "IndexedDB", "Cache", "Code Cache", "GPUCache",
        "Web Data", "Web Data-journal",
        "Login Data", "Login Data-journal",
    ]
    import shutil
    # El profile_dir puede tener subdirs Default/, Profile 1/, etc.
    sub_profiles = ["Default"] + [d for d in os.listdir(profile_dir)
                                    if d.startswith("Profile ")]
    for sub in sub_profiles:
        base = os.path.join(profile_dir, sub)
        if not os.path.isdir(base):
            continue
        for t in targets:
            p = os.path.join(base, t)
            try:
                if os.path.isfile(p):
                    os.unlink(p)
                    out["deleted"].append(f"{sub}/{t}")
                elif os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    out["deleted"].append(f"{sub}/{t}/")
            except Exception as e:
                out["errors"].append(f"{sub}/{t}:{str(e)[:60]}")
    return out


def full_cleanup(ctx, profile_dir: Optional[str] = None,
                 also_files: bool = False) -> Dict[str, Any]:
    """Pipeline completo: CDP-level (mientras browser vivo) + opcional file-level.
    Llamar JUSTO ANTES de cerrar el browser/profile. Si quieres además purgar los
    archivos de historial del perfil tras cerrar, llama _delete_history_files
    con el browser ya cerrado.
    """
    cdp = clear_history_via_cdp(ctx)
    out = {"cdp": cdp, "ok": cdp.get("ok", False)}
    if also_files and profile_dir:
        out["files"] = _delete_history_files(profile_dir)
    return out


def post_close_purge(profile_dir: str) -> Dict[str, Any]:
    """Llamar TRAS cerrar el browser para borrar archivos History/etc.
    Equivalente a 'borrar historial completo' del perfil — el browser debe
    estar cerrado, si no los archivos estarán bloqueados."""
    return _delete_history_files(profile_dir)
