"""multibrowser_config.py — Configuración centralizada de API keys de multibrowsers antidetect.

Soporta:
  • MoreLogin        (local API :40000, requires API ID + KEY)
  • AdsPower         (local API :50325, API key)
  • GoLogin          (cloud https://api.gologin.com, token)
  • Kameleo          (local API :5050, no auth / opcional)
  • Multilogin X     (cloud api.multilogin.com, email+password hash sha384)
  • Dolphin Anty     (cloud https://dolphin-anty-api.com, token)
  • Incogniton       (local API :35000, no auth)
  • Octo Browser     (cloud https://app.octobrowser.net, api token)

Features:
  - Fernet encryption de passwords / tokens
  - test_connection(id) → prueba API real si la URL responde
  - Integración con multilogin_orchestrator.py existente (Multilogin X)
  - Config persistente en JSON (datos/multibrowser.json)

API:
  get_providers()          → lista de providers con metadata
  get_config()             → dict completo con credenciales (encrypt)
  get_safe_config()        → sin credenciales (para API)
  save_provider(id, patch) → actualiza un provider
  test_connection(id)      → prueba API real
  get_enabled_providers()  → lista de los enabled=True
  self_test()              → checks internos sin conexión
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import threading
import time
from datetime import datetime
from typing import Optional, Dict, List, Any

import config
import json_atomic

try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

_CONFIG_KEY = "multibrowser.json"
_lock = threading.RLock()


# ═══════════════════════════════════════════════════════════════
#  Providers — catálogo estático
# ═══════════════════════════════════════════════════════════════
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "morelogin": {
        "id": "morelogin",
        "label": "MoreLogin",
        "logo": "🦊",
        "default_host": "http://127.0.0.1:40000",
        "type": "local",
        "fields": [
            {"key": "host",    "label": "API host",  "type": "text", "placeholder": "http://127.0.0.1:40000", "default": "http://127.0.0.1:40000"},
            {"key": "api_id",  "label": "API ID",    "type": "text", "placeholder": "Your API ID"},
            {"key": "api_key", "label": "API Key",   "type": "password", "placeholder": "Your API Key"},
        ],
        "help_url": "https://www.morelogin.com/api-doc/localapi",
        "test_endpoint": "/api/env/list",   # MoreLogin GET con headers X-Api-Id / X-Api-Key
        "description": "Multi-navegador anti-detect freemium. API local en :40000. Requiere MoreLogin Desktop abierto.",
    },
    "adspower": {
        "id": "adspower",
        "label": "AdsPower",
        "logo": "🛡️",
        "default_host": "http://local.adspower.net:50325",
        "type": "local",
        "fields": [
            {"key": "host",    "label": "Local API URL", "type": "text", "placeholder": "http://local.adspower.net:50325", "default": "http://local.adspower.net:50325"},
            {"key": "api_key", "label": "API Key",       "type": "password", "placeholder": "Your AdsPower API Key"},
        ],
        "help_url": "https://localapi-doc-en.adspower.com/",
        "test_endpoint": "/status",
        "description": "AdsPower browser. API local en :50325. Activa 'API' en Settings → API.",
    },
    "gologin": {
        "id": "gologin",
        "label": "GoLogin",
        "logo": "🕵️",
        "default_host": "https://api.gologin.com",
        "type": "cloud",
        "fields": [
            {"key": "host",  "label": "API host", "type": "text", "default": "https://api.gologin.com"},
            {"key": "token", "label": "API Token", "type": "password", "placeholder": "Bearer token"},
        ],
        "help_url": "https://docs.gologin.com/",
        "test_endpoint": "/browser/v2",
        "description": "Browser cloud. Genera un token en tu dashboard de GoLogin.",
    },
    "kameleo": {
        "id": "kameleo",
        "label": "Kameleo",
        "logo": "🦎",
        "default_host": "http://127.0.0.1:5050",
        "type": "local",
        "fields": [
            {"key": "host",    "label": "Local API", "type": "text", "default": "http://127.0.0.1:5050"},
            {"key": "api_key", "label": "API Key (opcional)", "type": "password", "placeholder": "Vacío si no requerido"},
        ],
        "help_url": "https://help.kameleo.io/hc/en-us/articles/4415331283473-Local-API",
        "test_endpoint": "/profiles",
        "description": "Kameleo browser. API local en :5050. Inicia Kameleo.CLI.exe para habilitarla.",
    },
    "multilogin": {
        "id": "multilogin",
        "label": "Multilogin X",
        "logo": "🔐",
        "default_host": "https://api.multilogin.com",
        "type": "cloud",
        "fields": [
            {"key": "email",        "label": "Email",       "type": "email"},
            {"key": "password",     "label": "Password",    "type": "password"},
            {"key": "workspace_id", "label": "Workspace ID","type": "text"},
            {"key": "folder_id",    "label": "Folder ID",   "type": "text"},
        ],
        "help_url": "https://docs.multilogin.com/",
        "test_endpoint": "/user/signin",
        "description": "Multilogin X (€99-359/mes). Delega al módulo multilogin_orchestrator para operar perfiles.",
        "delegates_to": "multilogin_orchestrator",
    },
    "dolphin": {
        "id": "dolphin",
        "label": "Dolphin Anty",
        "logo": "🐬",
        "default_host": "https://dolphin-anty-api.com",
        "type": "cloud",
        "fields": [
            {"key": "host",         "label": "API host",  "type": "text", "default": "https://dolphin-anty-api.com"},
            {"key": "token",        "label": "API Token", "type": "password"},
            {"key": "local_host",   "label": "Local API (opcional)", "type": "text", "placeholder": "http://localhost:3001", "default": "http://localhost:3001"},
        ],
        "help_url": "https://dolphin-anty-api.com/docs/",
        "test_endpoint": "/browser_profiles",
        "description": "Dolphin Anty freemium. Usa cloud API + Local API en :3001 para lanzar perfiles.",
    },
    "incogniton": {
        "id": "incogniton",
        "label": "Incogniton",
        "logo": "🥷",
        "default_host": "http://127.0.0.1:35000",
        "type": "local",
        "fields": [
            {"key": "host", "label": "Local API", "type": "text", "default": "http://127.0.0.1:35000"},
        ],
        "help_url": "https://incogniton.com/documentation/api/",
        "test_endpoint": "/profile/all",
        "description": "Incogniton browser freemium. API local en :35000. Requiere app abierta.",
    },
    "octo": {
        "id": "octo",
        "label": "Octo Browser",
        "logo": "🐙",
        "default_host": "https://app.octobrowser.net",
        "type": "cloud",
        "fields": [
            {"key": "host",       "label": "API host", "type": "text", "default": "https://app.octobrowser.net"},
            {"key": "api_token",  "label": "API Token", "type": "password"},
        ],
        "help_url": "https://docs.octobrowser.net/api-documentation/",
        "test_endpoint": "/api/v2/profiles",
        "description": "Octo Browser — fingerprinting + cloud sync. Token desde cuenta.",
    },
}


# ═══════════════════════════════════════════════════════════════
#  Encriptación (Fernet con llave derivada del host — compat con otp_manager)
# ═══════════════════════════════════════════════════════════════
def _fernet_key() -> bytes:
    seed = f"{socket.gethostname()}|{os.environ.get('USER') or os.environ.get('USERNAME') or 'tm'}|tm_mbrowser_v1"
    return base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest())


def _encrypt(s: str) -> str:
    if not s: return ""
    if _HAS_CRYPTO:
        try:
            return "FRN:" + Fernet(_fernet_key()).encrypt(s.encode()).decode()
        except Exception:
            pass
    return "B64:" + base64.urlsafe_b64encode(s.encode()).decode()


def _decrypt(enc: str) -> str:
    if not enc: return ""
    if enc.startswith("FRN:") and _HAS_CRYPTO:
        try:
            return Fernet(_fernet_key()).decrypt(enc[4:].encode()).decode()
        except Exception:
            return ""
    if enc.startswith("B64:"):
        try:
            return base64.urlsafe_b64decode(enc[4:].encode()).decode()
        except Exception:
            return ""
    return enc  # legacy plain


# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════
_SENSITIVE_FIELDS = {"api_key", "api_id", "token", "password", "api_token"}


def _default_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "providers": {pid: {"enabled": False, "config": {}, "updated_at": ""}
                      for pid in PROVIDERS.keys()},
        "default_provider": "",
        "stats": {"tests_run": 0, "tests_ok": 0, "last_test": ""},
    }


def get_config() -> Dict[str, Any]:
    with _lock:
        cfg = json_atomic.load(config.ruta(_CONFIG_KEY), default=_default_config())
        # Asegura que todos los providers están presentes (si se añade uno nuevo)
        prov = cfg.setdefault("providers", {})
        for pid in PROVIDERS.keys():
            prov.setdefault(pid, {"enabled": False, "config": {}, "updated_at": ""})
        cfg.setdefault("stats", {"tests_run": 0, "tests_ok": 0, "last_test": ""})
        return cfg


def get_safe_config() -> Dict[str, Any]:
    """Versión para exponer al frontend: passwords/tokens/api_keys redactados."""
    cfg = get_config()
    out = {
        "version": cfg.get("version", 1),
        "default_provider": cfg.get("default_provider", ""),
        "stats": cfg.get("stats", {}),
        "providers": {},
    }
    for pid, pmeta in PROVIDERS.items():
        p = cfg["providers"].get(pid, {})
        pconf = dict(p.get("config") or {})
        # Redact sensibles
        safe = {}
        for k, v in pconf.items():
            if k in _SENSITIVE_FIELDS:
                safe[k] = "***" if v else ""
                safe[k + "_set"] = bool(v)
            else:
                safe[k] = v
        out["providers"][pid] = {
            **pmeta,
            "enabled": bool(p.get("enabled")),
            "config": safe,
            "updated_at": p.get("updated_at", ""),
        }
    return out


def get_provider_creds(pid: str) -> Dict[str, str]:
    """Devuelve credenciales DECIFRADAS de un provider (uso interno)."""
    cfg = get_config()
    p = cfg.get("providers", {}).get(pid, {})
    pconf = dict(p.get("config") or {})
    out = {}
    for k, v in pconf.items():
        if k in _SENSITIVE_FIELDS and isinstance(v, str) and v.startswith(("FRN:", "B64:")):
            out[k] = _decrypt(v)
        else:
            out[k] = v
    return out


def save_provider(pid: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if pid not in PROVIDERS:
        return None
    with _lock:
        cfg = get_config()
        p = cfg["providers"].setdefault(pid, {"enabled": False, "config": {}, "updated_at": ""})
        # enabled
        if "enabled" in patch:
            p["enabled"] = bool(patch["enabled"])
        # config fields
        new_config = dict(p.get("config") or {})
        pconfig = patch.get("config") or {}
        # Merge: los campos sensibles se encriptan, el resto en plano
        for k, v in pconfig.items():
            if v is None or v == "":
                # Si pasa vacío en un campo sensible = no tocar (user no lo rellenó)
                if k in _SENSITIVE_FIELDS:
                    continue
                new_config[k] = v
            else:
                if k in _SENSITIVE_FIELDS:
                    new_config[k] = _encrypt(str(v))
                else:
                    new_config[k] = v
        p["config"] = new_config
        p["updated_at"] = datetime.now().isoformat()
        json_atomic.save(config.ruta(_CONFIG_KEY), cfg)

        # Integración con multilogin_orchestrator: sincroniza cuando es Multilogin X
        if pid == "multilogin":
            try:
                import multilogin_orchestrator
                ml_patch = {}
                creds = get_provider_creds(pid)
                if "email" in creds: ml_patch["email"] = creds["email"]
                if "workspace_id" in creds: ml_patch["workspace_id"] = creds["workspace_id"]
                if "folder_id" in creds: ml_patch["folder_id"] = creds["folder_id"]
                if creds.get("password"):
                    ml_patch["password"] = creds["password"]
                if ml_patch:
                    multilogin_orchestrator.save_config(ml_patch)
            except Exception:
                pass
        return p


def get_enabled_providers() -> List[str]:
    cfg = get_config()
    return [pid for pid, p in cfg.get("providers", {}).items() if p.get("enabled")]


def set_default_provider(pid: str) -> bool:
    if pid and pid not in PROVIDERS:
        return False
    with _lock:
        cfg = get_config()
        cfg["default_provider"] = pid or ""
        json_atomic.save(config.ruta(_CONFIG_KEY), cfg)
        return True


# ═══════════════════════════════════════════════════════════════
#  Test connection — por provider
# ═══════════════════════════════════════════════════════════════
def test_connection(pid: str, timeout: int = 5) -> Dict[str, Any]:
    if pid not in PROVIDERS:
        return {"ok": False, "error": "unknown_provider"}
    meta = PROVIDERS[pid]
    creds = get_provider_creds(pid)
    started = time.time()

    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests library missing"}

    host = creds.get("host") or meta["default_host"]
    endpoint = meta.get("test_endpoint", "/")
    url = host.rstrip("/") + endpoint
    headers = {"Accept": "application/json"}

    # Construye auth por provider
    try:
        if pid == "morelogin":
            # MoreLogin: X-Api-Id + X-Api-Key
            if not creds.get("api_id") or not creds.get("api_key"):
                return {"ok": False, "error": "api_id y api_key requeridos"}
            headers["X-Api-Id"] = creds["api_id"]
            # MoreLogin firma con hmac-sha1(timestamp+api_id) usando api_key
            ts = str(int(time.time() * 1000))
            import hmac as _hmac
            sign = _hmac.new(creds["api_key"].encode(), (ts + creds["api_id"]).encode(), hashlib.sha1).hexdigest()
            headers["X-Timestamp"] = ts
            headers["X-Sign"] = sign
            r = requests.get(url, headers=headers, timeout=timeout)
        elif pid == "adspower":
            # AdsPower: api_key opcional como query param
            params = {}
            if creds.get("api_key"):
                params["api_key"] = creds["api_key"]
            r = requests.get(url, params=params, timeout=timeout)
        elif pid == "gologin":
            if not creds.get("token"):
                return {"ok": False, "error": "token requerido"}
            headers["Authorization"] = f"Bearer {creds['token']}"
            r = requests.get(url, headers=headers, timeout=timeout)
        elif pid == "kameleo":
            if creds.get("api_key"):
                headers["Authorization"] = f"Bearer {creds['api_key']}"
            r = requests.get(url, headers=headers, timeout=timeout)
        elif pid == "multilogin":
            # Multilogin X: POST signin con email + pwd sha384
            if not creds.get("email") or not creds.get("password"):
                return {"ok": False, "error": "email y password requeridos"}
            pwd_hash = hashlib.sha384(creds["password"].encode()).hexdigest()
            r = requests.post(url, json={"email": creds["email"], "password": pwd_hash}, timeout=timeout)
        elif pid == "dolphin":
            if not creds.get("token"):
                return {"ok": False, "error": "token requerido"}
            headers["Authorization"] = f"Bearer {creds['token']}"
            r = requests.get(url, headers=headers, timeout=timeout)
        elif pid == "incogniton":
            r = requests.get(url, timeout=timeout)
        elif pid == "octo":
            if not creds.get("api_token"):
                return {"ok": False, "error": "api_token requerido"}
            headers["X-Octo-Api-Token"] = creds["api_token"]
            r = requests.get(url, headers=headers, timeout=timeout)
        else:
            return {"ok": False, "error": "no_test_impl"}

        elapsed_ms = int((time.time() - started) * 1000)
        _bump_stats(ok=r.status_code < 400)
        return {
            "ok": r.status_code < 400,
            "status_code": r.status_code,
            "elapsed_ms": elapsed_ms,
            "provider": pid,
            "url": url,
            "snippet": (r.text or "")[:200],
        }
    except requests.exceptions.ConnectionError as e:
        _bump_stats(ok=False)
        hint = ""
        if meta.get("type") == "local":
            hint = f" (¿está {meta['label']} desktop abierto?)"
        return {"ok": False, "error": f"ConnectionError{hint}: {str(e)[:120]}",
                "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as e:
        _bump_stats(ok=False)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                "elapsed_ms": int((time.time() - started) * 1000)}


def _bump_stats(ok: bool):
    try:
        with _lock:
            cfg = get_config()
            s = cfg.setdefault("stats", {"tests_run": 0, "tests_ok": 0, "last_test": ""})
            s["tests_run"] = s.get("tests_run", 0) + 1
            if ok: s["tests_ok"] = s.get("tests_ok", 0) + 1
            s["last_test"] = datetime.now().isoformat()
            json_atomic.save(config.ruta(_CONFIG_KEY), cfg)
    except Exception:
        pass


def get_providers() -> Dict[str, Any]:
    return dict(PROVIDERS)


# ═══════════════════════════════════════════════════════════════
#  Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    results = {"checks": {}, "details": {}}
    # 1. Libs
    results["checks"]["crypto_available"] = _HAS_CRYPTO
    try:
        import requests; results["checks"]["requests_available"] = True
    except ImportError:
        results["checks"]["requests_available"] = False

    # 2. Encryption round-trip
    secret = "my_test_token_1234"
    enc = _encrypt(secret)
    results["checks"]["encryption_roundtrip"] = (_decrypt(enc) == secret)

    # 3. Todos los providers están en el catálogo
    expected = {"morelogin", "adspower", "gologin", "kameleo", "multilogin",
                 "dolphin", "incogniton", "octo"}
    results["checks"]["providers_complete"] = expected.issubset(PROVIDERS.keys())
    results["details"]["providers_count"] = len(PROVIDERS)

    # 4. save + load flujo
    save_provider("morelogin", {"config": {"api_id": "TEST_ID", "api_key": "TEST_KEY_xxx"}})
    cfg = get_config()
    ml = cfg["providers"].get("morelogin", {}).get("config", {})
    # api_id y api_key son sensibles → se guardan encriptados (FRN: o B64:)
    results["checks"]["save_provider_ok"] = bool(ml.get("api_id", "").startswith(("FRN:", "B64:")))
    results["checks"]["save_encrypts_sensitive"] = ml.get("api_key", "").startswith(("FRN:", "B64:"))

    # 5. safe config redacta
    safe = get_safe_config()
    ml_safe = safe["providers"]["morelogin"]["config"]
    results["checks"]["safe_config_redacts"] = (ml_safe.get("api_key") == "***")
    results["checks"]["safe_config_has_flag"] = ml_safe.get("api_key_set") is True

    # 6. creds decrypt
    creds = get_provider_creds("morelogin")
    results["checks"]["creds_decrypt_ok"] = (creds.get("api_key") == "TEST_KEY_xxx")

    # 7. enabled list
    save_provider("morelogin", {"enabled": True})
    en = get_enabled_providers()
    results["checks"]["enabled_list_includes"] = "morelogin" in en
    save_provider("morelogin", {"enabled": False})

    # 8. Limpia test values
    save_provider("morelogin", {"config": {"api_id": "", "api_key": ""}, "enabled": False})
    # Los sensibles no se limpian con "" (por diseño). Limpiamos manualmente
    with _lock:
        raw = get_config()
        raw["providers"]["morelogin"]["config"] = {"api_id": "", "api_key": ""}
        json_atomic.save(config.ruta(_CONFIG_KEY), raw)

    # 9. Unknown provider → None
    results["checks"]["unknown_provider_handled"] = (save_provider("nope_fake", {}) is None)

    # Resumen
    results["total_checks"] = len(results["checks"])
    results["passed_checks"] = sum(1 for v in results["checks"].values() if v)
    results["ok"] = (results["passed_checks"] == results["total_checks"])
    return results


def get_info() -> Dict[str, Any]:
    cfg = get_config()
    return {
        "module": "multibrowser_config",
        "version": 1,
        "providers_total": len(PROVIDERS),
        "providers_enabled": len(get_enabled_providers()),
        "crypto_available": _HAS_CRYPTO,
        "stats": cfg.get("stats", {}),
    }
