"""multilogin_orchestrator.py — Orquestación REST de perfiles Multilogin.

Multilogin X API (2024+):
- auth: api.multilogin.com/user/signin  (email+password → token)
- profiles: launcher.mlx.yt/profile/f/{folder}/p/{profile_id}/start?headless_mode=true
- CDP port: devuelve `status.message.ws` WebSocket + `automation_type`
- Connect Playwright: browser.connect_over_cdp(ws_url)

Este módulo proporciona:
- Config persistente: email/password/api_key/workspace_id/folder_id
- signin(): autenticación y cache de token
- list_profiles(): lista perfiles disponibles
- start_profile(id, headless): lanza perfil y devuelve CDP URL
- stop_profile(id): detiene perfil
- create_profile(name, os_type): crea perfil nuevo
- run_batch(profiles, callback): orquesta N perfiles en paralelo
- connect_playwright(cdp_url): helper para conectar Playwright al CDP

⚠️ AVISO: Multilogin X es un servicio comercial (€99-359/mes).
Sin credenciales válidas el módulo corre en modo SIMULATE.

Docs: https://docs.multilogin.com/
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import json_atomic


_CONFIG_KEY = "multilogin.json"
_STATE_KEY = "multilogin_state.json"
_lock = threading.Lock()

SIGNIN_URL = "https://api.multilogin.com/user/signin"
API_V2 = "https://api.multilogin.com"
LAUNCHER = "https://launcher.mlx.yt:45001/api/v2"
DEFAULT_TIMEOUT = 25

# Cache token en memoria con expiración
_token_cache = {"token": None, "exp": 0}


def _config_path():
    return config.ruta(_CONFIG_KEY)


def _state_path():
    return config.ruta(_STATE_KEY)


def get_config() -> dict:
    default = {
        "email": "",
        "password_hash_sha384": "",  # MLX requires sha384 hash of password
        "workspace_id": "",
        "folder_id": "",
        "mode": "simulate",           # simulate | live
        "default_headless": True,
        "default_os": "win",
        "max_concurrent": 5,
        "session_timeout_min": 60,
    }
    return json_atomic.load(_config_path(), default=default)


def save_config(patch: dict) -> dict:
    import hashlib
    cur = get_config()
    allowed = {"email", "workspace_id", "folder_id", "mode",
               "default_headless", "default_os", "max_concurrent", "session_timeout_min"}
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    # Password recibido en claro → hash sha384 (requisito Multilogin)
    if patch.get("password"):
        cur["password_hash_sha384"] = hashlib.sha384(patch["password"].encode("utf-8")).hexdigest()
    if cur.get("mode") not in ("simulate", "live"):
        cur["mode"] = "simulate"
    cur["max_concurrent"] = max(1, min(int(cur.get("max_concurrent", 5)), 50))
    json_atomic.save(_config_path(), cur)
    # Invalidar token cache al cambiar credenciales
    _token_cache["token"] = None
    return cur


def is_configured() -> bool:
    cfg = get_config()
    return bool(cfg.get("email") and cfg.get("password_hash_sha384")
                and cfg.get("workspace_id"))


# ═══════════════════════════════════════════
#  SIGNIN + TOKEN
# ═══════════════════════════════════════════
def _signin() -> dict:
    """Autentica contra Multilogin. Cachea token 55 min."""
    if _token_cache["token"] and time.time() < _token_cache["exp"]:
        return {"ok": True, "token": _token_cache["token"], "cached": True}
    cfg = get_config()
    if not cfg.get("email") or not cfg.get("password_hash_sha384"):
        return {"ok": False, "error": "credenciales no configuradas"}
    import requests
    try:
        r = requests.post(
            SIGNIN_URL,
            json={"email": cfg["email"], "password": cfg["password_hash_sha384"],
                  "workspace_id": cfg.get("workspace_id", "")},
            timeout=DEFAULT_TIMEOUT,
        )
        data = r.json()
        if r.status_code == 200 and data.get("status", {}).get("http_code") == 200:
            token = data.get("data", {}).get("token")
            if token:
                _token_cache["token"] = token
                _token_cache["exp"] = time.time() + 55 * 60
                return {"ok": True, "token": token}
        # Error típico: bad credentials
        err = data.get("status", {}).get("message", f"HTTP {r.status_code}")
        return {"ok": False, "error": err, "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def check_connection() -> dict:
    """Comprueba credenciales haciendo signin real."""
    cfg = get_config()
    if cfg.get("mode") == "simulate" or not is_configured():
        return {
            "ok": True,
            "mode": "simulate",
            "note": "Configura email/password/workspace para pasar a live",
        }
    r = _signin()
    return {
        "ok": r.get("ok", False),
        "mode": "live",
        "token_cached": r.get("cached", False),
        "error": r.get("error"),
    }


def _auth_headers() -> dict:
    s = _signin()
    if not s.get("ok"):
        return {}
    return {"Authorization": f"Bearer {s['token']}"}


# ═══════════════════════════════════════════
#  PROFILE MANAGEMENT
# ═══════════════════════════════════════════
def list_profiles(folder_id: Optional[str] = None, limit: int = 100) -> dict:
    """Lista perfiles en el folder. En simulate devuelve fake."""
    cfg = get_config()
    if cfg.get("mode") == "simulate":
        return {"ok": True, "mode": "simulate",
                "profiles": _demo_profiles(limit=min(limit, 10))}
    import requests
    headers = _auth_headers()
    if not headers:
        return {"ok": False, "error": "no auth"}
    fid = folder_id or cfg.get("folder_id", "")
    if not fid:
        return {"ok": False, "error": "folder_id no configurado"}
    try:
        r = requests.get(
            f"{API_V2}/profile/search",
            headers=headers,
            params={"limit": limit, "folder_id": fid},
            timeout=DEFAULT_TIMEOUT,
        )
        data = r.json()
        if r.status_code == 200:
            return {"ok": True, "mode": "live",
                    "profiles": data.get("data", {}).get("profiles", [])}
        return {"ok": False, "error": data.get("status", {}).get("message", f"HTTP {r.status_code}")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _demo_profiles(limit: int = 10) -> List[dict]:
    """Perfiles de demo para modo simulate."""
    import uuid
    out = []
    oss = ["win", "mac", "linux"]
    browsers = ["stealthfox", "mimic"]
    for i in range(limit):
        pid = f"demo-{uuid.uuid4().hex[:8]}"
        out.append({
            "id": pid,
            "name": f"Profile Demo {i+1}",
            "os": oss[i % len(oss)],
            "browser_type": browsers[i % len(browsers)],
            "status": "stopped",
            "created_at": datetime.now().isoformat(),
            "notes": "Demo (sin API key configurada)",
        })
    return out


def start_profile(profile_id: str, headless: bool = True,
                  timeout: int = 45) -> dict:
    """Lanza el perfil. Devuelve CDP WebSocket URL y automation_type."""
    cfg = get_config()
    if cfg.get("mode") == "simulate":
        # Simulación: devuelve CDP fake
        return {"ok": True, "mode": "simulate",
                "profile_id": profile_id,
                "cdp_url": f"ws://127.0.0.1:0/devtools/simulated/{profile_id}",
                "automation_type": "playwright",
                "port": 0,
                "note": "Simulated — no browser real launched"}
    import requests
    headers = _auth_headers()
    if not headers:
        return {"ok": False, "error": "no auth"}
    fid = cfg.get("folder_id", "")
    headless_str = "true" if headless else "false"
    try:
        r = requests.get(
            f"{LAUNCHER}/profile/f/{fid}/p/{profile_id}/start",
            headers=headers,
            params={"headless_mode": headless_str, "automation_type": "playwright"},
            timeout=timeout,
            verify=True,
        )
        data = r.json()
        if r.status_code == 200 and data.get("status", {}).get("http_code") == 200:
            msg = data.get("data", {}) or data.get("status", {}).get("message", {})
            if isinstance(msg, str):
                # Legacy: msg contiene port
                return {"ok": True, "mode": "live", "raw": msg}
            return {
                "ok": True, "mode": "live",
                "profile_id": profile_id,
                "cdp_url": msg.get("ws", "").strip(),
                "port": msg.get("port"),
                "automation_type": "playwright",
                "data": msg,
            }
        return {"ok": False, "error": data.get("status", {}).get("message",
                                                                  f"HTTP {r.status_code}")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def stop_profile(profile_id: str) -> dict:
    cfg = get_config()
    if cfg.get("mode") == "simulate":
        return {"ok": True, "mode": "simulate", "stopped": profile_id}
    import requests
    headers = _auth_headers()
    if not headers:
        return {"ok": False, "error": "no auth"}
    fid = cfg.get("folder_id", "")
    try:
        r = requests.get(
            f"{LAUNCHER}/profile/stop/p/{profile_id}",
            headers=headers, timeout=DEFAULT_TIMEOUT,
        )
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def create_profile(name: str, os_type: str = "win",
                   browser_type: str = "mimic") -> dict:
    """Crea perfil con fingerprint aleatorio generado por MLX."""
    cfg = get_config()
    if cfg.get("mode") == "simulate":
        import uuid
        return {"ok": True, "mode": "simulate", "profile_id": f"demo-{uuid.uuid4().hex[:8]}",
                "name": name}
    import requests
    headers = _auth_headers()
    if not headers:
        return {"ok": False, "error": "no auth"}
    fid = cfg.get("folder_id", "")
    payload = {
        "name": name,
        "os_type": os_type,
        "browser_type": browser_type,
        "folder_id": fid,
        "parameters": {
            "fingerprint": {},  # MLX auto-genera
            "storage": {"is_local": False, "save_service_worker": True},
        },
    }
    try:
        r = requests.post(f"{API_V2}/profile/create", headers=headers,
                          json=payload, timeout=DEFAULT_TIMEOUT)
        data = r.json()
        if r.status_code == 200:
            return {"ok": True, "mode": "live",
                    "profile_id": data.get("data", {}).get("ids", [""])[0]}
        return {"ok": False, "error": data.get("status", {}).get("message")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════
#  BATCH ORCHESTRATION
# ═══════════════════════════════════════════
def run_batch(profile_ids: List[str], callback: Optional[Callable] = None,
              max_concurrent: Optional[int] = None,
              broadcast: Optional[Callable] = None) -> dict:
    """Lanza N perfiles en paralelo y opcionalmente ejecuta callback(cdp_url, profile_id).

    Stops automáticamente cada perfil al terminar.
    Respeta max_concurrent del config.
    """
    cfg = get_config()
    mx = max_concurrent or cfg.get("max_concurrent", 5)
    mx = max(1, min(mx, 20))
    headless = cfg.get("default_headless", True)

    results = []
    started = time.time()

    def _one(pid):
        t0 = time.time()
        r = start_profile(pid, headless=headless)
        entry = {"profile_id": pid, "started_ok": r.get("ok", False),
                 "cdp_url": r.get("cdp_url"),
                 "port": r.get("port"),
                 "elapsed_ms": int((time.time() - t0) * 1000)}
        if broadcast:
            try:
                broadcast("multilogin_started", {"profile_id": pid,
                                                   "ok": r.get("ok", False)})
            except Exception:
                pass
        if r.get("ok") and callback:
            try:
                cb_result = callback(r.get("cdp_url"), pid)
                entry["callback_result"] = cb_result
            except Exception as e:
                entry["callback_error"] = f"{type(e).__name__}: {e}"
        # stop al terminar
        if r.get("ok"):
            try:
                stop_profile(pid)
                entry["stopped_ok"] = True
            except Exception:
                entry["stopped_ok"] = False
        return entry

    with ThreadPoolExecutor(max_workers=mx) as ex:
        futures = {ex.submit(_one, pid): pid for pid in profile_ids}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"profile_id": futures[fut],
                                "error": f"{type(e).__name__}: {e}"})

    return {
        "ok": True,
        "total": len(profile_ids),
        "success": sum(1 for r in results if r.get("started_ok")),
        "elapsed_s": round(time.time() - started, 1),
        "results": results,
        "mode": cfg.get("mode", "simulate"),
    }


def connect_playwright(cdp_url: str):
    """Helper: conecta Playwright al CDP URL del perfil."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright no instalado"}
    if not cdp_url:
        return {"ok": False, "error": "cdp_url vacío"}
    if cdp_url.startswith("ws://127.0.0.1:0"):
        return {"ok": False, "error": "simulate mode — conecta en live para CDP real"}
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp_url)
        return {"ok": True, "browser": browser, "pw": pw,
                "contexts": len(browser.contexts)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════
#  ESTADO / HISTORIAL
# ═══════════════════════════════════════════
def get_state() -> dict:
    """Devuelve estado actual: configurado, perfiles activos conocidos, últimas ejecuciones."""
    cfg = get_config()
    d = json_atomic.load(_state_path(), default={"recent_runs": []})
    return {
        "configured": is_configured(),
        "mode": cfg.get("mode", "simulate"),
        "email_preview": (cfg.get("email", "")[:3] + "…" +
                           cfg.get("email", "")[-5:]) if cfg.get("email") else "",
        "workspace_configured": bool(cfg.get("workspace_id")),
        "folder_configured": bool(cfg.get("folder_id")),
        "max_concurrent": cfg.get("max_concurrent", 5),
        "default_headless": cfg.get("default_headless", True),
        "recent_runs": d.get("recent_runs", [])[:10],
    }


def record_run(run: dict):
    with _lock:
        d = json_atomic.load(_state_path(), default={"recent_runs": []})
        d["recent_runs"].insert(0, {
            "ts": datetime.now().isoformat(),
            **run,
        })
        d["recent_runs"] = d["recent_runs"][:30]
        json_atomic.save(_state_path(), d)


def self_test() -> dict:
    """Test rápido: verifica config + signin en modo live, o simulate si no."""
    started = time.time()
    cfg = get_config()
    if cfg.get("mode") == "simulate" or not is_configured():
        profiles = list_profiles()
        return {
            "ok": True,
            "mode": "simulate",
            "elapsed_ms": int((time.time() - started) * 1000),
            "profiles_count": len(profiles.get("profiles", [])),
            "note": "Modo simulate — configura credenciales para test real",
        }
    r = check_connection()
    elapsed = int((time.time() - started) * 1000)
    if not r.get("ok"):
        return {"ok": False, "mode": "live", "elapsed_ms": elapsed,
                "error": r.get("error")}
    pr = list_profiles(limit=5)
    return {
        "ok": pr.get("ok", False),
        "mode": "live",
        "elapsed_ms": elapsed,
        "profiles_count": len(pr.get("profiles", [])),
        "error": pr.get("error"),
    }


# ═══════════════════════════════════════════════════════════
#  PROFILE TEMPLATES — predefinidos por combo (browser + país)
# ═══════════════════════════════════════════════════════════
PROFILE_TEMPLATES = {
    "chrome_us_desktop": {
        "name": "Chrome-US Desktop",
        "browser_type": "mimic",
        "os_type": "windows",
        "navigator": {
            "user_agent_vendor": "chrome",
            "language": "en-US",
            "platform": "Win32",
            "hardware_concurrency": 8,
        },
        "proxy": {"country": "us"},
        "tags": ["chrome", "us", "desktop"],
    },
    "chrome_es_desktop": {
        "name": "Chrome-ES Desktop",
        "browser_type": "mimic",
        "os_type": "windows",
        "navigator": {
            "user_agent_vendor": "chrome",
            "language": "es-ES",
            "platform": "Win32",
            "hardware_concurrency": 8,
        },
        "proxy": {"country": "es"},
        "tags": ["chrome", "es", "desktop"],
    },
    "chrome_uk_desktop": {
        "name": "Chrome-UK Desktop",
        "browser_type": "mimic",
        "os_type": "windows",
        "navigator": {
            "user_agent_vendor": "chrome",
            "language": "en-GB",
            "platform": "Win32",
        },
        "proxy": {"country": "gb"},
        "tags": ["chrome", "uk", "desktop"],
    },
    "chrome_de_desktop": {
        "name": "Chrome-DE Desktop",
        "browser_type": "mimic",
        "os_type": "windows",
        "navigator": {"user_agent_vendor": "chrome", "language": "de-DE"},
        "proxy": {"country": "de"},
        "tags": ["chrome", "de", "desktop"],
    },
    "chrome_fr_desktop": {
        "name": "Chrome-FR Desktop",
        "browser_type": "mimic",
        "os_type": "windows",
        "navigator": {"user_agent_vendor": "chrome", "language": "fr-FR"},
        "proxy": {"country": "fr"},
        "tags": ["chrome", "fr", "desktop"],
    },
    "firefox_uk_desktop": {
        "name": "Firefox-UK Desktop",
        "browser_type": "stealthfox",
        "os_type": "windows",
        "navigator": {"user_agent_vendor": "firefox", "language": "en-GB"},
        "proxy": {"country": "gb"},
        "tags": ["firefox", "uk", "desktop"],
    },
    "firefox_us_desktop": {
        "name": "Firefox-US Desktop",
        "browser_type": "stealthfox",
        "os_type": "windows",
        "navigator": {"user_agent_vendor": "firefox", "language": "en-US"},
        "proxy": {"country": "us"},
        "tags": ["firefox", "us", "desktop"],
    },
    "chrome_us_mac": {
        "name": "Chrome-US MacBook",
        "browser_type": "mimic",
        "os_type": "macos",
        "navigator": {
            "user_agent_vendor": "chrome",
            "language": "en-US",
            "platform": "MacIntel",
        },
        "proxy": {"country": "us"},
        "tags": ["chrome", "us", "mac"],
    },
    "chrome_mobile_ios": {
        "name": "Chrome-iOS Mobile",
        "browser_type": "mimic",
        "os_type": "ios",
        "navigator": {
            "user_agent_vendor": "chrome",
            "language": "en-US",
            "platform": "iPhone",
        },
        "proxy": {"country": "us"},
        "tags": ["chrome", "mobile", "ios"],
    },
    "chrome_mobile_android": {
        "name": "Chrome-Android Mobile",
        "browser_type": "mimic",
        "os_type": "android",
        "navigator": {
            "user_agent_vendor": "chrome",
            "language": "en-US",
        },
        "proxy": {"country": "us"},
        "tags": ["chrome", "mobile", "android"],
    },
}


def list_templates() -> List[Dict]:
    """Devuelve templates disponibles con su metadata."""
    return [{"id": tid, **tpl} for tid, tpl in PROFILE_TEMPLATES.items()]


def get_template(template_id: str) -> Optional[Dict]:
    return PROFILE_TEMPLATES.get(template_id)


def create_profile_from_template(template_id: str,
                                    name: Optional[str] = None,
                                    folder_id: Optional[str] = None) -> dict:
    """Crea un perfil Multilogin a partir de un template predefinido."""
    tpl = get_template(template_id)
    if not tpl:
        return {"ok": False, "error": f"template {template_id} no existe",
                "available": list(PROFILE_TEMPLATES.keys())}

    profile_name = name or f"{tpl['name']} {datetime.now().strftime('%y%m%d%H%M%S')}"
    cfg = get_config()
    if cfg.get("mode") == "simulate":
        import uuid
        return {"ok": True, "mode": "simulate",
                "profile_id": "sim-" + uuid.uuid4().hex[:10],
                "name": profile_name,
                "template": template_id,
                "browser": tpl.get("browser_type"),
                "os": tpl.get("os_type"),
                "proxy_country": tpl.get("proxy", {}).get("country"),
                "note": "modo simulate — no crea perfil real"}

    # Modo live: llamada a create_profile con los parámetros del template
    r = create_profile(
        name=profile_name,
        browser_type=tpl["browser_type"],
        os_type=tpl["os_type"],
        folder_id=folder_id,
        navigator=tpl.get("navigator"),
        proxy=tpl.get("proxy"),
    )
    if isinstance(r, dict):
        r["template"] = template_id
    return r


def bulk_create_from_templates(template_ids: List[str],
                                  count_per_template: int = 1,
                                  folder_id: Optional[str] = None) -> dict:
    """Crea N perfiles de cada template. Útil para batch inicial."""
    results = {"created": [], "failed": [], "templates_used": template_ids,
                "total_attempts": len(template_ids) * count_per_template}
    for tid in template_ids:
        for i in range(count_per_template):
            name = f"{tid}-{i+1}"
            r = create_profile_from_template(tid, name=name, folder_id=folder_id)
            if r.get("ok"):
                results["created"].append({"template": tid, "name": name,
                                             "profile_id": r.get("profile_id")})
            else:
                results["failed"].append({"template": tid, "error": r.get("error")})
    results["ok"] = len(results["created"]) > 0
    results["success_count"] = len(results["created"])
    results["fail_count"] = len(results["failed"])
    return results
