"""live_readiness.py — Comprueba qué falta para activar el bot en modo LIVE.

Audita 8 dimensiones y devuelve un checklist con score y acciones recomendadas.
También provee setup helpers para automatizar lo automatizable.

API:
    check(lang='es') -> dict   # checklist completo + score (localizado ES/EN)
    auto_setup() -> dict # setup automatizado (proxies demo, AcpAT seed)
    set_captcha_key(provider, key) -> dict
    set_telegram(token, chat_id) -> dict
    seed_acpat(n=5) -> dict
    self_test() -> dict

i18n:
    Cada string visible en `_T[lang]`. Llama a check(lang='en') para inglés.
    Cache TTL 30s separado por lang.
"""
from __future__ import annotations
import os
import json
import threading
from datetime import datetime
from typing import Any, Dict, List

import config
import json_atomic

_lock = threading.RLock()

# Caché de check() para reducir el latency a <50ms
# El check real hace HTTP a Discord/MoreLogin/2captcha (~1.5s)
# Cache por idioma (es/en) para que el toggle EN no devuelva contenido cached ES
import time as _time_mod
_CHECK_CACHE = {"es": {"data": None, "ts": 0}, "en": {"data": None, "ts": 0}, "ttl_s": 30}
_check_cache_lock = threading.RLock()


# ═══════════════════════════════════════════════════════════════
#  i18n — strings localizados (ES/EN paridad 100%)
# ═══════════════════════════════════════════════════════════════
_T = {
    "es": {
        # Names (cards/headers visibles en pestaña Sniper Bot)
        "name.accounts": "Cuentas y Tarjetas",
        "name.proxies": "Proxies",
        "name.captcha": "CAPTCHA Solvers",
        "name.acpat": "AcpAT Pool (Queue-it)",
        "name.notifications": "Notificaciones",
        "name.browser": "Browser Engine",
        "name.critical_2026": "Módulos críticos 2026",
        "name.csv_sync": "CSV Sync (cuentas/tarjetas)",
        # Accounts
        "accounts.ok": "{active} cuentas activas · {cards} tarjetas",
        "accounts.need_one": "Necesitas al menos 1 cuenta activa con 1 tarjeta",
        "accounts.fix": "Añadir cuentas en pestaña Cuentas y Tarjetas",
        "accounts.error": "Error: {err}",
        "accounts.error_fix": "Revisar account_manager",
        # Proxies
        "proxies.ok": "{n} proxies disponibles",
        "proxies.from_txt": " · {n} en proxies.txt",
        "proxies.from_json": " · {n} en proxies.json",
        "proxies.placeholders_only": "⚠️ Solo {n} proxies placeholder — sustitúyelos por reales",
        "proxies.no_alive": "Sin proxies alive — CRÍTICO para Queue-it",
        "proxies.fix": "Añadir proxies reales (BrightData, IPRoyal, SOAX, ProxyEmpire) — NO placeholders",
        "proxies.error_fix": "Revisar proxy_health",
        # Captcha
        "captcha.ok": "✅ Configurados: {providers}",
        "captcha.placeholder_only": "⚠️ Hay key placeholder en '{provider}' — sustitúyela por la real",
        "captcha.no_key": "Sin API key — el bot no podrá resolver CAPTCHAs",
        "captcha.fix": "Añadir API key REAL (CapSolver $1=2k captchas) en pestaña CAPTCHA o aquí mismo",
        # AcpAT
        "acpat.ok": "{n} tokens válidos en pool",
        "acpat.empty": "Pool vacío — Queue-it bypass desactivado",
        "acpat.fix": "Captura tokens AcpAT manualmente o lanza warming",
        "acpat.error_fix": "Revisar acpat_pool.json",
        # Notifications
        "notif.configured_both": "Configurado: Discord + Telegram",
        "notif.configured_discord": "Configurado: Discord",
        "notif.configured_telegram": "Configurado: Telegram",
        "notif.none": "Sin Discord ni Telegram — no recibirás avisos",
        "notif.fix": "Añadir webhook Discord o token Telegram",
        # Browser
        "browser.ok": "{available} navegadores · {antidetect} con anti-detect",
        "browser.no_playwright": "Playwright no instalado",
        "browser.fix_install": "Instalar Playwright: python -m playwright install",
        "browser.fix_install_full": "python -m playwright install chromium firefox",
        "browser.error_fix": "pip install playwright",
        # Critical 2026
        "critical.status": "{loaded}/{expected} módulos cargados",
        "critical.fix": "Reinstalar módulos críticos faltantes",
        # CSV sync
        "csv.ok": "Watcher activo · CSV sincronizado",
        "csv.inactive": "Watcher inactivo",
        "csv.fix": "Reinicia dashboard para activar el watcher",
        "csv.error": "Error: {err}",
        # generic error
        "error": "Error: {err}",
    },
    "en": {
        "name.accounts": "Accounts & Cards",
        "name.proxies": "Proxies",
        "name.captcha": "CAPTCHA Solvers",
        "name.acpat": "AcpAT Pool (Queue-it)",
        "name.notifications": "Notifications",
        "name.browser": "Browser Engine",
        "name.critical_2026": "Critical 2026 modules",
        "name.csv_sync": "CSV Sync (accounts/cards)",
        "accounts.ok": "{active} active accounts · {cards} cards",
        "accounts.need_one": "You need at least 1 active account with 1 card",
        "accounts.fix": "Add accounts in Accounts & Cards tab",
        "accounts.error": "Error: {err}",
        "accounts.error_fix": "Check account_manager",
        "proxies.ok": "{n} proxies available",
        "proxies.from_txt": " · {n} in proxies.txt",
        "proxies.from_json": " · {n} in proxies.json",
        "proxies.placeholders_only": "⚠️ Only {n} placeholder proxies — replace them with real ones",
        "proxies.no_alive": "No alive proxies — CRITICAL for Queue-it",
        "proxies.fix": "Add real proxies (BrightData, IPRoyal, SOAX, ProxyEmpire) — NO placeholders",
        "proxies.error_fix": "Check proxy_health",
        "captcha.ok": "✅ Configured: {providers}",
        "captcha.placeholder_only": "⚠️ Placeholder key in '{provider}' — replace it with the real one",
        "captcha.no_key": "No API key — the bot can't solve CAPTCHAs",
        "captcha.fix": "Add a REAL API key (CapSolver $1=2k captchas) in CAPTCHA tab or here",
        "acpat.ok": "{n} valid tokens in pool",
        "acpat.empty": "Empty pool — Queue-it bypass disabled",
        "acpat.fix": "Capture AcpAT tokens manually or run warming",
        "acpat.error_fix": "Check acpat_pool.json",
        "notif.configured_both": "Configured: Discord + Telegram",
        "notif.configured_discord": "Configured: Discord",
        "notif.configured_telegram": "Configured: Telegram",
        "notif.none": "No Discord nor Telegram — you won't receive alerts",
        "notif.fix": "Add Discord webhook or Telegram token",
        "browser.ok": "{available} browsers · {antidetect} with anti-detect",
        "browser.no_playwright": "Playwright not installed",
        "browser.fix_install": "Install Playwright: python -m playwright install",
        "browser.fix_install_full": "python -m playwright install chromium firefox",
        "browser.error_fix": "pip install playwright",
        "critical.status": "{loaded}/{expected} modules loaded",
        "critical.fix": "Reinstall missing critical modules",
        "csv.ok": "Watcher active · CSV synced",
        "csv.inactive": "Watcher inactive",
        "csv.fix": "Restart dashboard to activate the watcher",
        "csv.error": "Error: {err}",
        "error": "Error: {err}",
    },
}


def _t(key: str, lang: str = "es", **fmt) -> str:
    """Resuelve key en el lang dado. Fallback a ES si no existe en EN."""
    s = _T.get(lang, _T["es"]).get(key) or _T["es"].get(key, key)
    try:
        return s.format(**fmt) if fmt else s
    except Exception:
        return s


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════
def _safe_load(key: str, default: Any) -> Any:
    try:
        return json_atomic.load(config.ruta(key), default=default)
    except Exception:
        return default


def _safe_save(key: str, data: Any) -> bool:
    try:
        path = config.ruta(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json_atomic.save(path, data)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# Individual checks
# ═══════════════════════════════════════════════════════════════
def _check_accounts(lang: str = "es") -> Dict[str, Any]:
    try:
        import account_manager
        accs = account_manager.account_manager.list_accounts()
        cards = account_manager.account_manager.list_cards()
        active = [a for a in accs if a.get("status") == "active"]
        ok = len(active) >= 1 and len(cards) >= 1
        return {
            "name": _t("name.accounts", lang),
            "ok": ok,
            "weight": 15,
            "stats": {
                "total_accounts": len(accs),
                "active_accounts": len(active),
                "cards": len(cards),
            },
            "msg": (_t("accounts.ok", lang, active=len(active), cards=len(cards))
                    if ok else _t("accounts.need_one", lang)),
            "fix_url": "/dashboard#cuentas",
            "fix_action": _t("accounts.fix", lang),
        }
    except Exception as e:
        return {"name": _t("name.accounts", lang), "ok": False, "weight": 15,
                "msg": _t("accounts.error", lang, err=e), "fix_action": _t("accounts.error_fix", lang)}


def _read_proxies_txt() -> List[Dict[str, Any]]:
    """Lee proxies.txt (formato bulk del usuario) y devuelve entries normalizadas."""
    paths = [config.ruta("proxies.txt")]
    items: List[Dict[str, Any]] = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f.read().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Formatos: host:port  |  host:port:user:pass  |  user:pass@host:port
                    if "@" in line:
                        creds, hp = line.split("@", 1)
                        host, _, port = hp.partition(":")
                        user, _, password = creds.partition(":")
                    else:
                        parts = line.split(":")
                        if len(parts) == 2:
                            host, port = parts; user = password = ""
                        elif len(parts) >= 4:
                            host, port, user = parts[0], parts[1], parts[2]
                            password = ":".join(parts[3:])
                        else:
                            continue
                    try:
                        port_n = int(port)
                    except Exception:
                        continue
                    items.append({
                        "host": host, "port": port_n, "user": user, "password": password,
                        "type": "http", "alive": None, "source": "proxies.txt",
                    })
            if items:
                break  # primer path con contenido
        except Exception:
            continue
    return items


def _check_proxies(lang: str = "es") -> Dict[str, Any]:
    try:
        # Fuente 1: proxies.json
        json_proxies: List[Dict[str, Any]] = []
        try:
            import proxy_health
            if hasattr(proxy_health, "list_all"):
                json_proxies = proxy_health.list_all() or []
        except Exception:
            pass
        if not json_proxies:
            d = _safe_load("proxies.json", default={"proxies": []})
            json_proxies = d.get("proxies", [])

        # Fuente 2: proxies.txt (formato bulk de la UI Sniper Bot)
        txt_proxies = _read_proxies_txt()

        # Mergear sin duplicados (por host:port:user — para soportar
        # servicios residenciales tipo QuantumProxies/BrightData/IPRoyal donde
        # todas las sesiones van al mismo gateway con distintas credenciales)
        seen = set()
        all_proxies: List[Dict[str, Any]] = []
        for p in json_proxies + txt_proxies:
            key = f"{p.get('host','')}:{p.get('port','')}:{p.get('user','')}:{p.get('password','')}"
            if key in seen:
                continue
            seen.add(key)
            all_proxies.append(p)

        real = [p for p in all_proxies if not p.get("is_placeholder")]
        placeholders = [p for p in all_proxies if p.get("is_placeholder")]
        # Cuenta como "alive" si está marcado o si viene de proxies.txt (asumimos válido salvo prueba contraria)
        alive_real = [p for p in real
                      if p.get("alive") or p.get("status") == "alive"
                      or (p.get("source") == "proxies.txt" and p.get("alive") is not False)]
        ok = len(alive_real) >= 1

        if ok:
            msg = _t("proxies.ok", lang, n=len(alive_real))
            if txt_proxies:
                msg += _t("proxies.from_txt", lang, n=len(txt_proxies))
            if json_proxies and not all(p.get("is_placeholder") for p in json_proxies):
                json_real = sum(1 for p in json_proxies if not p.get("is_placeholder"))
                if json_real > 0:
                    msg += _t("proxies.from_json", lang, n=json_real)
        elif placeholders:
            msg = _t("proxies.placeholders_only", lang, n=len(placeholders))
        else:
            msg = _t("proxies.no_alive", lang)

        return {
            "name": _t("name.proxies", lang),
            "ok": ok,
            "weight": 20,
            "stats": {
                "total": len(all_proxies),
                "real": len(real),
                "placeholders": len(placeholders),
                "alive": len(alive_real),
                "from_txt": len(txt_proxies),
                "from_json": len(json_proxies),
            },
            "msg": msg,
            "fix_action": _t("proxies.fix", lang),
            "fix_url": "/dashboard#bot/proxies",
        }
    except Exception as e:
        return {"name": _t("name.proxies", lang), "ok": False, "weight": 20,
                "msg": _t("error", lang, err=e), "fix_action": _t("proxies.error_fix", lang)}


def _check_captcha(lang: str = "es") -> Dict[str, Any]:
    """Detecta API keys en MÚLTIPLES estructuras posibles (compat amplia)."""
    services = {"capsolver": False, "2captcha": False, "anticaptcha": False, "capmonster": False}
    primary_key = ""
    primary_provider = ""
    is_placeholder_only = False
    try:
        import captcha_manager
        cfg = captcha_manager.get_config() if hasattr(captcha_manager, "get_config") else {}

        # Forma 1: cfg["services"]["capsolver"]["api_key"]  (estructura nueva)
        svcs_dict = cfg.get("services") or {}
        for svc in services:
            entry = svcs_dict.get(svc, {}) if isinstance(svcs_dict, dict) else {}
            if isinstance(entry, dict) and entry.get("api_key"):
                services[svc] = True

        # Forma 2: cfg["capsolver"]["api_key"]  (estructura por servicio plana)
        for svc in services:
            entry = cfg.get(svc, {})
            if isinstance(entry, dict) and entry.get("api_key"):
                services[svc] = True

        # Forma 3: cfg["api_key"] + cfg["provider"]  (estructura clásica)
        flat_key = (cfg.get("api_key") or "").strip()
        flat_provider = (cfg.get("provider") or cfg.get("primary") or "").strip().lower()
        primary_key = flat_key
        primary_provider = flat_provider
        if flat_key and flat_provider in services:
            # Detecta placeholders típicos
            placeholder_markers = ["PLACEHOLDER", "REPLACE", "YOUR-KEY", "TEST-", "DEMO-", "XXXX"]
            is_placeholder_now = any(m.lower() in flat_key.lower() for m in placeholder_markers)
            if not is_placeholder_now and len(flat_key) >= 12:
                services[flat_provider] = True
            else:
                is_placeholder_only = True

        # Forma 4: cfg["{svc}_key"]
        for svc in services:
            if cfg.get(f"{svc}_key"):
                services[svc] = True
    except Exception:
        pass

    configured = [s for s, v in services.items() if v]
    ok = len(configured) >= 1
    if ok:
        msg = _t("captcha.ok", lang, providers=", ".join(configured))
    elif is_placeholder_only:
        msg = _t("captcha.placeholder_only", lang, provider=primary_provider)
    else:
        msg = _t("captcha.no_key", lang)

    return {
        "name": _t("name.captcha", lang),
        "ok": ok,
        "weight": 20,
        "stats": services,
        "msg": msg,
        "fix_action": _t("captcha.fix", lang),
        "fix_url": "/dashboard#bot/captcha",
    }


def _check_acpat(lang: str = "es") -> Dict[str, Any]:
    try:
        d = _safe_load("acpat_pool.json", default={"tokens": []})
        tokens = d.get("tokens", [])
        # Filtra expirados (>30min)
        import time
        now = time.time()
        valid = [t for t in tokens if (now - (t.get("captured_at_ts", now))) < 1800]
        ok = len(valid) >= 1
        return {
            "name": _t("name.acpat", lang),
            "ok": ok,
            "weight": 10,
            "stats": {"total": len(tokens), "valid": len(valid)},
            "msg": (_t("acpat.ok", lang, n=len(valid))
                    if ok else _t("acpat.empty", lang)),
            "fix_action": _t("acpat.fix", lang),
            "fix_url": "/dashboard#bot/bypass",
        }
    except Exception as e:
        return {"name": _t("name.acpat", lang), "ok": False, "weight": 10,
                "msg": _t("error", lang, err=e), "fix_action": _t("acpat.error_fix", lang)}


def _check_notifications(lang: str = "es") -> Dict[str, Any]:
    discord = telegram = False
    try:
        import notifications
        if hasattr(notifications, "get_config"):
            cfg = notifications.get_config() or {}
        else:
            cfg = _safe_load("notifications.json", default={})
        discord = bool(cfg.get("discord_webhook") or
                       (cfg.get("discord", {}) or {}).get("webhook"))
        telegram = bool(cfg.get("telegram_token") or
                        (cfg.get("telegram", {}) or {}).get("token"))
    except Exception:
        pass
    ok = discord or telegram
    if ok:
        if discord and telegram:
            msg = _t("notif.configured_both", lang)
        elif discord:
            msg = _t("notif.configured_discord", lang)
        else:
            msg = _t("notif.configured_telegram", lang)
    else:
        msg = _t("notif.none", lang)
    return {
        "name": _t("name.notifications", lang),
        "ok": ok,
        "weight": 5,
        "stats": {"discord": discord, "telegram": telegram},
        "msg": msg,
        "fix_action": _t("notif.fix", lang),
        "fix_url": "/dashboard#notif",
    }


def _check_browser(lang: str = "es") -> Dict[str, Any]:
    try:
        import tm_browser_engine
        chk = tm_browser_engine.check_playwright()
        if chk.get("ok"):
            browsers = chk.get("browsers", {})
            available = [b for b, info in browsers.items() if info.get("available")]
            antidetect = [b for b, info in browsers.items() if info.get("antidetect")]
            return {
                "name": _t("name.browser", lang),
                "ok": len(available) >= 1,
                "weight": 10,
                "stats": {"available": available, "antidetect": antidetect},
                "msg": _t("browser.ok", lang, available=len(available), antidetect=len(antidetect)),
                "fix_action": _t("browser.fix_install", lang),
            }
        return {"name": _t("name.browser", lang), "ok": False, "weight": 10,
                "msg": _t("browser.no_playwright", lang),
                "fix_action": _t("browser.fix_install_full", lang)}
    except Exception as e:
        return {"name": _t("name.browser", lang), "ok": False, "weight": 10,
                "msg": _t("error", lang, err=e), "fix_action": _t("browser.error_fix", lang)}


def _check_critical_2026(lang: str = "es") -> Dict[str, Any]:
    """Los 10 módulos críticos 2026 instalados y operativos."""
    mods = ["ja4_pinning", "http2_fingerprint", "safetix_handler",
            "compliance_gate", "datadome_proxy_hooks", "uach_grease_validator",
            "asn_ip_drift", "psd3_payment_retry", "trust_tuple_warming",
            "tls_ct_monitor"]
    loaded = []
    for m in mods:
        try:
            __import__(m)
            loaded.append(m)
        except Exception:
            pass
    ok = len(loaded) == len(mods)
    return {
        "name": _t("name.critical_2026", lang),
        "ok": ok,
        "weight": 10,
        "stats": {"loaded": len(loaded), "expected": len(mods)},
        "msg": _t("critical.status", lang, loaded=len(loaded), expected=len(mods)),
        "fix_action": _t("critical.fix", lang),
    }


def _check_csv_sync(lang: str = "es") -> Dict[str, Any]:
    try:
        import accounts_csv_sync
        st = accounts_csv_sync.get_status()
        ok = bool(st.get("watcher_running") and st.get("csv_exists"))
        return {
            "name": _t("name.csv_sync", lang),
            "ok": ok,
            "weight": 5,
            "stats": st,
            "msg": _t("csv.ok", lang) if ok else _t("csv.inactive", lang),
            "fix_action": _t("csv.fix", lang),
        }
    except Exception as e:
        return {"name": _t("name.csv_sync", lang), "ok": False, "weight": 5,
                "msg": _t("csv.error", lang, err=e)}


# ═══════════════════════════════════════════════════════════════
# Master check
# ═══════════════════════════════════════════════════════════════
ALL_CHECKS = [
    _check_accounts,
    _check_proxies,
    _check_captcha,
    _check_acpat,
    _check_notifications,
    _check_browser,
    _check_critical_2026,
    _check_csv_sync,
]


def invalidate_cache() -> None:
    """Fuerza recomputo del próximo check() para todos los idiomas."""
    with _check_cache_lock:
        for lg in ("es", "en"):
            _CHECK_CACHE[lg]["data"] = None
            _CHECK_CACHE[lg]["ts"] = 0


def _check_uncached(lang: str = "es") -> Dict[str, Any]:
    """Ejecuta todas las verificaciones y devuelve el checklist (localizado)."""
    items = [c(lang) for c in ALL_CHECKS]
    total_weight = sum(it.get("weight", 0) for it in items)
    earned = sum(it.get("weight", 0) for it in items if it.get("ok"))
    score = round(100 * earned / total_weight, 1) if total_weight > 0 else 0
    blockers = [it["name"] for it in items
                if (not it.get("ok")) and it.get("weight", 0) >= 15]
    warnings = [it["name"] for it in items
                if (not it.get("ok")) and it.get("weight", 0) < 15]
    ready_for_live = score >= 75 and len(blockers) == 0
    return {
        "ok": True,
        "lang": lang,
        "score": score,
        "ready_for_live": ready_for_live,
        "blockers": blockers,
        "warnings": warnings,
        "items": items,
        "summary": {
            "total_checks": len(items),
            "passing": sum(1 for it in items if it.get("ok")),
            "failing": sum(1 for it in items if not it.get("ok")),
        },
        "checked_at": datetime.now().isoformat(),
    }


def check(force: bool = False, lang: str = "es") -> Dict[str, Any]:
    """Versión cacheada del check (TTL 30s, separado por lang).
    Para forzar recomputo: force=True. Soporta lang='es'|'en'."""
    if lang not in ("es", "en"):
        lang = "es"
    now = _time_mod.time()
    with _check_cache_lock:
        bucket = _CHECK_CACHE.get(lang) or {"data": None, "ts": 0}
        cached = bucket.get("data")
        cached_ts = bucket.get("ts", 0)
        ttl = _CHECK_CACHE.get("ttl_s", 30)
        if (not force) and cached and (now - cached_ts) < ttl:
            # Hit caché — añadir hint de que viene de cache
            return {**cached, "_cache_hit": True,
                    "_cache_age_s": round(now - cached_ts, 1)}

    # Miss caché → recomputar (puede tardar 1-2s)
    fresh = _check_uncached(lang)
    with _check_cache_lock:
        _CHECK_CACHE[lang]["data"] = fresh
        _CHECK_CACHE[lang]["ts"] = now
    return {**fresh, "_cache_hit": False}


# ═══════════════════════════════════════════════════════════════
# Setup helpers
# ═══════════════════════════════════════════════════════════════
def set_captcha_key(provider: str, api_key: str) -> Dict[str, Any]:
    """Guarda la API key de un proveedor de CAPTCHA en la estructura real de captcha_manager."""
    if provider not in ("capsolver", "2captcha", "anticaptcha", "capmonster"):
        return {"ok": False, "error": "invalid_provider"}
    if not api_key or len(api_key.strip()) < 10:
        return {"ok": False, "error": "invalid_key"}
    api_key = api_key.strip()
    # Bloquea explícitamente placeholders
    if any(m in api_key.upper() for m in ("PLACEHOLDER", "REPLACE", "YOUR-KEY")):
        return {"ok": False, "error": "looks_like_placeholder"}
    try:
        import captcha_manager
        # Usa save_config si existe (formato real)
        if hasattr(captcha_manager, "save_config"):
            cfg = captcha_manager.get_config() or {}
            cfg["api_key"] = api_key
            cfg["provider"] = provider
            cfg["primary"] = provider
            cfg.setdefault("services", {})
            cfg["services"].setdefault(provider, {})
            cfg["services"][provider]["api_key"] = api_key
            cfg["services"][provider]["enabled"] = True
            cfg["mode"] = "live"  # con key real, modo live
            r = captcha_manager.save_config(cfg)
            return {"ok": True, "provider": provider, "saved": True, "result": r}
        # Fallback directo a fichero
        path = config.ruta("captcha.json")
        cfg = json_atomic.load(path, default={})
        cfg["api_key"] = api_key
        cfg["provider"] = provider
        cfg["primary"] = provider
        cfg.setdefault("services", {}).setdefault(provider, {})["api_key"] = api_key
        cfg["mode"] = "live"
        json_atomic.save(path, cfg)
        return {"ok": True, "provider": provider, "saved": True, "fallback": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_telegram(token: str, chat_id: str) -> Dict[str, Any]:
    """Configura Telegram bot token + chat_id."""
    if not token or not chat_id:
        return {"ok": False, "error": "token_and_chat_id_required"}
    try:
        cfg = _safe_load("notifications.json", default={})
        cfg.setdefault("telegram", {})
        cfg["telegram"]["token"] = token.strip()
        cfg["telegram"]["chat_id"] = chat_id.strip()
        cfg["telegram"]["enabled"] = True
        _safe_save("notifications.json", cfg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def seed_acpat(n: int = 5) -> Dict[str, Any]:
    """Genera N tokens AcpAT placeholder para testing del pool.
    NOTA: estos son tokens de prueba — para LIVE necesitas tokens reales
    capturados del browser tras pasar Queue-it manualmente."""
    import time, secrets
    tokens = []
    now_ts = time.time()
    for i in range(max(1, min(n, 20))):
        tokens.append({
            "id": secrets.token_hex(6),
            "token": "PLACEHOLDER_" + secrets.token_urlsafe(48),
            "domain": ["us", "es", "uk", "de", "fr"][i % 5],
            "captured_at_ts": now_ts,
            "captured_at": datetime.now().isoformat(),
            "ttl_min": 30,
            "is_placeholder": True,
        })
    try:
        d = _safe_load("acpat_pool.json", default={"tokens": []})
        d["tokens"] = (d.get("tokens", []) + tokens)[-50:]  # cap a 50
        _safe_save("acpat_pool.json", d)
        return {"ok": True, "added": len(tokens), "total": len(d["tokens"])}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def add_real_proxy(spec: str, country: str = "", proxy_type: str = "http") -> Dict[str, Any]:
    """Añade un proxy REAL al sistema y opcionalmente hace health-check.

    spec admite varios formatos:
        "host:port"
        "host:port:user:pass"
        "user:pass@host:port"
        "http://user:pass@host:port"
        "https://host:port"
    """
    if not spec or not spec.strip():
        return {"ok": False, "error": "spec_required"}
    s = spec.strip()
    # Extrae user:pass si los hay
    user = password = host = port = ""
    if "://" in s:
        proto, s = s.split("://", 1)
        if proto in ("http", "https", "socks5", "socks4"):
            proxy_type = proto
    if "@" in s:
        creds, hp = s.split("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
        s = hp
    # Ahora s es "host:port" o "host:port:user:pass"
    parts = s.split(":")
    if len(parts) == 2:
        host, port = parts
    elif len(parts) >= 4:
        host, port, user, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
    else:
        return {"ok": False, "error": "format_invalid",
                "hint": "Usa host:port o host:port:user:pass o user:pass@host:port"}
    try:
        port_n = int(port)
    except Exception:
        return {"ok": False, "error": "port_invalid"}
    if not host or port_n < 1 or port_n > 65535:
        return {"ok": False, "error": "host_or_port_invalid"}

    # Health-check: hacer request real a través del proxy
    alive = False
    latency_ms = None
    err = None
    try:
        import requests
        proxy_url = f"{proxy_type}://"
        if user:
            proxy_url += f"{user}:{password}@"
        proxy_url += f"{host}:{port_n}"
        proxies_dict = {"http": proxy_url, "https": proxy_url}
        import time as _t
        t0 = _t.time()
        r = requests.get("https://api.ipify.org?format=json",
                         proxies=proxies_dict, timeout=8)
        latency_ms = int((_t.time() - t0) * 1000)
        alive = r.status_code == 200
    except Exception as e:
        err = str(e)[:80]

    # Persistir
    import secrets
    entry = {
        "id": secrets.token_hex(6),
        "host": host,
        "port": port_n,
        "user": user,
        "password": password,
        "type": proxy_type,
        "country": country.lower() if country else "",
        "alive": alive,
        "status": "alive" if alive else "dead",
        "latency_ms": latency_ms,
        "last_check": datetime.now().isoformat(),
        "is_placeholder": False,
        "created_at": datetime.now().isoformat(),
    }
    if err:
        entry["last_error"] = err
    try:
        d = _safe_load("proxies.json", default={"proxies": []})
        d["proxies"] = d.get("proxies", []) + [entry]
        _safe_save("proxies.json", d)
        return {
            "ok": True,
            "alive": alive,
            "latency_ms": latency_ms,
            "host": host,
            "port": port_n,
            "error": err,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def remove_placeholder_proxies() -> Dict[str, Any]:
    """Elimina todos los proxies marcados como placeholder."""
    try:
        d = _safe_load("proxies.json", default={"proxies": []})
        before = len(d.get("proxies", []))
        d["proxies"] = [p for p in d.get("proxies", []) if not p.get("is_placeholder")]
        _safe_save("proxies.json", d)
        removed = before - len(d["proxies"])
        return {"ok": True, "removed": removed, "remaining": len(d["proxies"])}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def add_demo_proxies(n: int = 3) -> Dict[str, Any]:
    """Añade N proxies placeholder al sistema (NO funcionarán para LIVE,
    son sólo para que el dashboard tenga entradas visibles).
    Para LIVE real, usa proxies de pago: BrightData, IPRoyal, SOAX..."""
    import secrets
    items = []
    for i in range(max(1, min(n, 10))):
        items.append({
            "id": secrets.token_hex(6),
            "host": f"placeholder-{i+1}.demo.invalid",
            "port": 8080 + i,
            "user": "user_demo",
            "password": "PASSWORD_PLACEHOLDER",
            "type": "http",
            "country": ["us", "es", "uk"][i % 3],
            "alive": False,
            "is_placeholder": True,
            "created_at": datetime.now().isoformat(),
            "note": "PLACEHOLDER — sustituir por proxy real de BrightData/IPRoyal/SOAX",
        })
    try:
        d = _safe_load("proxies.json", default={"proxies": []})
        d["proxies"] = (d.get("proxies", []) + items)
        _safe_save("proxies.json", d)
        return {"ok": True, "added": len(items),
                "note": "Placeholders insertados — sustitúyelos por proxies reales antes de LIVE"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def auto_setup(lang: str = "es") -> Dict[str, Any]:
    """Configuración automatizada de lo que se puede hacer sin credenciales del usuario."""
    actions = []
    # 1. Seed AcpAT pool si está vacío
    d = _safe_load("acpat_pool.json", default={"tokens": []})
    if len(d.get("tokens", [])) < 3:
        r = seed_acpat(5)
        actions.append({"step": "seed_acpat", "result": r})

    # 2. Sembrar proxies placeholder si no hay
    pd = _safe_load("proxies.json", default={"proxies": []})
    if len(pd.get("proxies", [])) == 0:
        r = add_demo_proxies(3)
        actions.append({"step": "add_demo_proxies", "result": r})

    # 3. Re-check (forzar recomputo y respetar lang)
    final = check(force=True, lang=lang)
    return {"ok": True, "actions": actions, "score_after": final["score"],
            "items": final["items"]}


# ═══════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}

    # 1. check() devuelve el shape correcto
    r = check()
    checks["check_runs"] = r.get("ok") is True
    checks["check_has_score"] = isinstance(r.get("score"), (int, float))
    checks["check_has_items"] = len(r.get("items", [])) == len(ALL_CHECKS)
    checks["check_has_summary"] = isinstance(r.get("summary"), dict)

    # 2. set_captcha_key valida proveedor
    r2 = set_captcha_key("invalid_provider", "x" * 20)
    checks["captcha_rejects_bad_provider"] = not r2.get("ok")
    r3 = set_captcha_key("capsolver", "short")
    checks["captcha_rejects_short_key"] = not r3.get("ok")

    # 3. seed_acpat
    r4 = seed_acpat(2)
    checks["seed_acpat_works"] = bool(r4.get("ok"))
    checks["seed_acpat_added"] = r4.get("added") == 2

    # 4. auto_setup
    r5 = auto_setup()
    checks["auto_setup_runs"] = bool(r5.get("ok"))
    checks["auto_setup_has_score"] = isinstance(r5.get("score_after"), (int, float))

    # 5. Item shape
    items = r["items"]
    checks["items_have_name"] = all("name" in it for it in items)
    checks["items_have_ok"] = all("ok" in it for it in items)
    checks["items_have_weight"] = all("weight" in it for it in items)
    checks["items_have_msg"] = all("msg" in it for it in items)

    # 6. Score range
    checks["score_in_range"] = 0 <= r["score"] <= 100

    # 7. Telegram requires both
    r6 = set_telegram("", "chat")
    checks["telegram_requires_token"] = not r6.get("ok")

    # 8. i18n parity ES/EN
    es_keys = set(_T.get("es", {}).keys())
    en_keys = set(_T.get("en", {}).keys())
    only_es = es_keys - en_keys
    only_en = en_keys - es_keys
    checks["i18n_parity_es_en"] = (len(only_es) == 0 and len(only_en) == 0)
    checks["i18n_es_keys_present"] = len(es_keys) >= 30
    checks["i18n_en_keys_present"] = len(en_keys) >= 30

    # 9. check(lang='en') devuelve strings localizados
    r_en = check(force=True, lang="en")
    checks["check_en_runs"] = r_en.get("ok") is True
    # Sample: msg de accounts en EN no debe contener 'cuentas activas'
    items_en = r_en.get("items", [])
    en_strings = " ".join((it.get("name", "") + " " + it.get("msg", "") + " " + it.get("fix_action", "")) for it in items_en)
    checks["check_en_no_spanish_leak"] = ("cuentas activas" not in en_strings.lower()) and ("añadir" not in en_strings.lower())

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {
        "ok": passed == total,
        "total_checks": total,
        "passed_checks": passed,
        "checks": checks,
        "i18n_only_es": sorted(only_es),
        "i18n_only_en": sorted(only_en),
    }


def get_info() -> Dict[str, Any]:
    r = check()
    return {
        "module": "live_readiness",
        "version": 1,
        "score": r["score"],
        "ready_for_live": r["ready_for_live"],
        "blockers": r["blockers"],
    }
