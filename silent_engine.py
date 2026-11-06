"""silent_engine.py — Adaptador del sistema de registro/compra a Silent.

Drop-in replacement de la superficie de `bot_orchestrator` que usa
`register_account.py`:

    _pick_lock, _active_profiles,
    _pick_fresh_profile(country_code=..., quemados=...) -> {profile, env_id, ip}
    _start_profile(env_id) -> debugPort
    _release_profile(profile_label)
    _close_profile_clean(env_id, port, profile_label)

Silent es la plataforma de navegadores del usuario (C:\\Users\\user\\Silent),
con API HTTP local documentada en src/main/core/api.ts del repo Silent:

    GET  /v1/profiles              -> lista (scope profiles:read)
    POST /v1/profiles/:id/open     -> {cdp: true, headless} -> {sessionId, cdp:{port,...}}
    POST /v1/profiles/:id/close    -> cerrar

Auth: header `Authorization: Bearer <token>`. El token se crea en la UI de
Silent (Ajustes → API) con scopes profiles:read + profiles:write, y hay que
habilitar el ajuste "CDP vía API" para que /open devuelva el puerto CDP.

Config (env vars):
    SILENT_API    default http://127.0.0.1:8788
    SILENT_TOKEN  token en claro (alternativa: secrets/silent_token.txt)
    SILENT_GROUP  si se define, solo perfiles de ese grupo
    SILENT_HEADLESS default "0" (registro: ventana visible, como MoreLogin)

Activación en register_account.py: --engine silent (o TM_ENGINE=silent).
"""
import json
import os
import threading
from typing import Any, Dict, Optional, Set

import requests

SILENT_API = os.environ.get("SILENT_API", "http://127.0.0.1:8788").rstrip("/")
SILENT_GROUP = os.environ.get("SILENT_GROUP", "").strip() or None
SILENT_HEADLESS = os.environ.get("SILENT_HEADLESS", "0") in ("1", "true", "yes")

# Misma superficie que bot_orchestrator para pick atómico cross-proceso.
_pick_lock = threading.Lock()
_active_profiles: Set[str] = set()

_TOKEN_CACHE: Optional[str] = None


def _token() -> str:
    """Token Bearer: env SILENT_TOKEN -> secrets/silent_token.txt -> error claro."""
    global _TOKEN_CACHE
    if _TOKEN_CACHE:
        return _TOKEN_CACHE
    tok = os.environ.get("SILENT_TOKEN", "").strip()
    if not tok:
        for cand in (os.path.join("secrets", "silent_token.txt"),
                     os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "secrets", "silent_token.txt")):
            try:
                with open(cand, encoding="utf-8") as f:
                    tok = f.read().strip()
                if tok:
                    break
            except OSError:
                continue
    if not tok:
        # Descubrimiento local de Silent: ~/.silent/api.json (token rotado en
        # cada arranque de la app, scopes de agente incl. profiles:read/write).
        try:
            home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
            with open(os.path.join(home, ".silent", "api.json"), encoding="utf-8") as f:
                tok = (json.load(f).get("token") or "").strip()
        except (OSError, ValueError):
            tok = ""
    if not tok:
        raise RuntimeError(
            "Silent: falta token de API. Crea uno en Silent (Ajustes → API, "
            "scopes profiles:read+profiles:write) y ponlo en env SILENT_TOKEN "
            "o en secrets/silent_token.txt")
    _TOKEN_CACHE = tok
    return tok


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def _get(path: str, timeout: float = 10) -> Any:
    r = requests.get(f"{SILENT_API}{path}", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: Optional[dict] = None, timeout: float = 30) -> Any:
    r = requests.post(f"{SILENT_API}{path}", headers=_headers(),
                      json=body or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def ping() -> Dict[str, Any]:
    """Sin auth. Devuelve ok/version del servicio Silent."""
    r = requests.get(f"{SILENT_API}/v1/ping", timeout=5)
    r.raise_for_status()
    return r.json()


def list_profiles() -> list:
    return _get("/v1/profiles")


def _pick_fresh_profile(country_code: str = "ES", quemados: Optional[Set[str]] = None,
                        **_ignored) -> Optional[Dict[str, Any]]:
    """Elige un perfil Silent libre. Mapea al dict que espera register_account:
    {profile: <name>, env_id: <id>, ip: <proxy-host o None>}."""
    quemados = quemados or set()
    proxy_by_id: Dict[str, str] = {}
    try:
        for p in _get("/v1/proxies"):
            if p.get("host"):
                proxy_by_id[p.get("id")] = p["host"]
    except Exception:
        proxy_by_id = {}
    for prof in list_profiles():
        if prof.get("status") != "idle":
            continue
        name = prof.get("name") or prof.get("id")
        if not name or name in _active_profiles or name in quemados:
            continue
        if SILENT_GROUP and prof.get("group") != SILENT_GROUP:
            continue
        _active_profiles.add(name)
        return {
            "profile": name,
            "env_id": prof["id"],
            "ip": proxy_by_id.get(prof.get("proxyId") or ""),
        }
    return None


def _start_profile(env_id: str) -> Optional[int]:
    """Abre el perfil con enganche CDP y devuelve el puerto (mismo contrato
    que bot_orchestrator._start_profile). None si no se concedió CDP o falló."""
    try:
        r = _post(f"/v1/profiles/{env_id}/open",
                  {"cdp": True, "headless": SILENT_HEADLESS}, timeout=60)
        cdp = r.get("cdp") or {}
        port = cdp.get("port")
        return int(port) if port else None
    except Exception:
        return None


def _release_profile(profile_label: str) -> None:
    _active_profiles.discard(profile_label)


def _close_profile_clean(env_id: str, port: int, profile_label: str = "") -> Dict[str, Any]:
    """Cierra el navegador vía API. Limpieza CDP de cookies/historial best-effort
    (misma política que MoreLogin: reutiliza cookie_filter y browser_cleanup)."""
    out: Dict[str, Any] = {"closed": False}
    try:
        from playwright.sync_api import sync_playwright
        v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=5).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
            ctx = b.contexts[0] if b.contexts else None
            if ctx:
                try:
                    import cookie_filter
                    cookies = ctx.cookies()
                    keep, drop, unknown = cookie_filter.partition_cookies(cookies)
                    out.update(keep=len(keep), drop=len(drop), unknown=len(unknown))
                    ctx.clear_cookies()
                except Exception:
                    pass
                try:
                    import browser_cleanup as _bc
                    out["history"] = _bc.clear_history_via_cdp(ctx)
                except Exception as _e:
                    out["history_err"] = str(_e)[:120]
            b.close()
    except Exception:
        pass
    finally:
        _release_profile(profile_label)
    try:
        out["closed"] = bool(_post(f"/v1/profiles/{env_id}/close", timeout=30))
    except Exception as e:
        out["close_err"] = str(e)[:120]
    return out


def self_test() -> Dict[str, Any]:
    """Valida conectividad, auth y permisos contra Silent real."""
    out: Dict[str, Any] = {"ok": True, "checks": {}, "errors": []}
    try:
        p = ping()
        out["checks"]["ping"] = p.get("ok") is True
        out["version"] = p.get("version")
    except Exception as e:
        out["checks"]["ping"] = f"err:{e}"
        out["ok"] = False
        out["errors"].append(f"ping: {e}")
        return out
    try:
        profs = list_profiles()
        out["checks"]["profiles_read"] = isinstance(profs, list)
        out["profiles_total"] = len(profs)
        out["profiles_idle"] = sum(1 for x in profs if x.get("status") == "idle")
    except Exception as e:
        out["checks"]["profiles_read"] = f"err:{e}"
        out["ok"] = False
        out["errors"].append(f"profiles: {e}")
    try:
        # No abrimos nada: solo comprobamos que el ajuste CDP está habilitado
        # intentando /open con un id inexistente — debe dar 404/4xx de perfil,
        # no un rechazo de ajuste. Si da 401/403 el token no sirve.
        r = requests.post(f"{SILENT_API}/v1/profiles/__no_existe__/open",
                          headers=_headers(), json={"cdp": True}, timeout=10)
        out["checks"]["profiles_write_scope"] = r.status_code in (404, 400, 422, 200)
        if r.status_code == 401:
            out["ok"] = False
            out["errors"].append("token sin scope profiles:write o inválido")
    except Exception as e:
        out["checks"]["profiles_write_scope"] = f"err:{e}"
    out["ok"] = out["ok"] and all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
