"""tm_cookie_store.py — Persiste/carga cookies de sesión TM por usuario.

Flow:
- harvest_cookies(ctx, label): captura cookies del contexto + storage TM, guarda en JSON
- load_cookies(label): devuelve cookies + storage del usuario (None si no hay)
- inject_cookies(ctx, data): inyecta cookies y localStorage en un contexto fresco
- has_valid_session(label): True si hay cookies guardadas y no expiraron
"""
from __future__ import annotations
import os, json, time
from datetime import datetime
from typing import Optional, Dict, Any, List

COOKIE_DIR = os.path.join(os.path.dirname(__file__), "datos", "tm_cookies")
os.makedirs(COOKIE_DIR, exist_ok=True)

# Dominios TM que nos interesan preservar
TM_DOMAINS = (
    "ticketmaster.es", "ticketmaster.com", "auth.ticketmaster.com",
    "identity.ticketmaster.es", "my.ticketmaster.es", "live.ticketmaster.es",
    "ticketmaster.co.uk", "ticketmaster.de", "ticketmaster.fr"
)

# Cookies CRÍTICAS para la sesión TM (login + queue + anti-bot)
SESSION_COOKIES = {
    "TMUO", "tm-session", "eps-session", "tmps-session",
    "_abck", "bm_sv", "bm_so", "bm_mi", "bm_sz", "bm_ss",
    "BIGipServer", "akavpau", "akacd_phased", "ak_bmsc",
    "_px", "_px2", "_px3", "_pxhd",  # Perimeterx/HUMAN
    "cf_clearance", "__cf_bm",  # Cloudflare
    "reese84",  # F5/Incapsula
    "eps-onesnap", "tmsnpid", "tmppd",
    "TM_USER", "TM_LOGIN", "tm-login-state",
}


def _path_for(label: str) -> str:
    safe = label.replace('/', '_').replace('\\', '_').replace(' ', '_')
    return os.path.join(COOKIE_DIR, f"{safe}.json")


def _is_tm_cookie(c: Dict[str, Any]) -> bool:
    """True si el cookie pertenece a un dominio TM."""
    domain = (c.get("domain") or "").lower().lstrip('.')
    if not domain: return False
    return any(domain == d or domain.endswith("." + d) for d in TM_DOMAINS)


def harvest_cookies(ctx, label: str, page=None) -> Dict[str, Any]:
    """Captura cookies + localStorage TM del contexto y guarda en disco.
    Devuelve metadata: {ok, total_cookies, tm_cookies, has_session, file}"""
    out = {"ok": False, "label": label, "captured_at": datetime.now().isoformat()}
    try:
        all_cookies = ctx.cookies()
    except Exception as e:
        out["error"] = f"cookies_err: {e}"
        return out
    tm_cookies = [c for c in all_cookies if _is_tm_cookie(c)]
    has_session = any(c.get("name") in SESSION_COOKIES or c.get("name", "").startswith("TM")
                      for c in tm_cookies)
    out["total_cookies"] = len(all_cookies)
    out["tm_cookies"] = len(tm_cookies)
    out["has_session_marker"] = has_session

    # Capturar localStorage de páginas TM si tenemos page
    storage = {}
    if page is not None:
        try:
            for p in ctx.pages:
                url = (p.url or "").lower()
                if any(d in url for d in ["ticketmaster", "livenation"]):
                    try:
                        ls = p.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
                        if ls:
                            domain = url.split('/')[2] if '/' in url else url
                            storage[domain] = ls
                    except Exception: pass
        except Exception: pass

    data = {
        "label": label,
        "captured_at": out["captured_at"],
        "cookies": tm_cookies,
        "localStorage": storage,
    }
    fpath = _path_for(label)
    try:
        try:
            import json_atomic
            json_atomic.save(fpath, data)
        except Exception:
            with open(fpath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        out["file"] = fpath
        out["ok"] = True
    except Exception as e: out["error"] = f"save_err: {e}"
    return out


def load_cookies(label: str) -> Optional[Dict[str, Any]]:
    """Carga cookies guardadas. None si no existe o corrupto."""
    fpath = _path_for(label)
    if not os.path.exists(fpath): return None
    try:
        with open(fpath, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def has_valid_session(label: str, max_age_hours: int = 12) -> bool:
    """True si hay cookies guardadas, no expiradas, con marker de sesión TM."""
    data = load_cookies(label)
    if not data: return False
    captured = data.get("captured_at")
    if not captured: return False
    try:
        captured_dt = datetime.fromisoformat(captured)
        age_h = (datetime.now() - captured_dt).total_seconds() / 3600
        if age_h > max_age_hours: return False
    except: return False
    cookies = data.get("cookies") or []
    has_session = any(c.get("name") in SESSION_COOKIES or (c.get("name") or "").startswith("TM")
                      for c in cookies)
    return has_session


def inject_cookies(ctx, data: Dict[str, Any]) -> Dict[str, int]:
    """Inyecta cookies + localStorage en el contexto fresco. Devuelve count."""
    out = {"cookies_added": 0, "storage_set": 0, "errors": 0}
    cookies = data.get("cookies") or []
    norm = []
    for c in cookies:
        nc = {k: v for k, v in c.items() if v is not None and k in
              ("name", "value", "domain", "path", "expires", "httpOnly",
               "secure", "sameSite", "url")}
        if "sameSite" in nc:
            ss = str(nc["sameSite"]).capitalize()
            nc["sameSite"] = ss if ss in ("Strict", "Lax", "None") else "Lax"
        # Asegurar dominio o url presente
        if "domain" not in nc and "url" not in nc:
            continue
        norm.append(nc)
    if norm:
        try:
            ctx.add_cookies(norm)
            out["cookies_added"] = len(norm)
        except Exception:
            # Algunos cookies pueden fallar — añadirlos uno a uno
            for c in norm:
                try: ctx.add_cookies([c])
                except: out["errors"] += 1
                else: out["cookies_added"] += 1
    return out


def inject_localstorage(page, data: Dict[str, Any]) -> int:
    """Inyecta localStorage de la página actual si dominio matches."""
    storage = data.get("localStorage") or {}
    if not storage: return 0
    try:
        url = (page.url or "").lower()
        domain = url.split('/')[2] if '/' in url else url
        items = storage.get(domain) or {}
        if not items: return 0
        page.evaluate("""(items) => {
            for (const [k, v] of Object.entries(items)) {
                try { localStorage.setItem(k, v); } catch(e) {}
            }
        }""", items)
        return len(items)
    except Exception:
        return 0


def status_summary() -> Dict[str, Any]:
    """Resumen de qué cuentas tienen sesión guardada y su edad."""
    out = {"accounts": []}
    for fname in os.listdir(COOKIE_DIR):
        if not fname.endswith('.json'): continue
        label = fname[:-5]
        data = load_cookies(label)
        if not data: continue
        captured = data.get("captured_at")
        try:
            age_h = (datetime.now() - datetime.fromisoformat(captured)).total_seconds() / 3600
        except: age_h = -1
        out["accounts"].append({
            "label": label,
            "cookies": len(data.get("cookies") or []),
            "captured_at": captured,
            "age_hours": round(age_h, 1),
            "valid": has_valid_session(label),
        })
    return out


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    print(json.dumps(status_summary(), indent=2, ensure_ascii=False))
