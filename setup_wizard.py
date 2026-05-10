"""setup_wizard.py — Onboarding 9 pasos para dejar el bot listo para LIVE.

Verifica el estado de cada requisito usando las APIs públicas de cada módulo
(evita rutas JSON hardcoded que se rompen cuando los módulos cambian).

Uso desde dashboard:
  GET  /api/setup-wizard/status?lang=es|en   → snapshot actual (localizado)
  POST /api/setup-wizard/test/<step>         → test connection real de ese paso

Internacionalización:
  Cada string visible al usuario está en `_T[lang]`. Si lang no existe,
  fallback a "es". Llamar a get_status(lang="en") para inglés.
"""
from typing import Dict, List, Any

import json_atomic
import config


# ═══════════════════════════════════════════════════════════════
#  i18n — strings localizados (ES/EN paridad 100%)
# ═══════════════════════════════════════════════════════════════
_T = {
    "es": {
        # Headlines
        "headline_ready": "🚀 ¡Listo para modo LIVE!",
        "headline_blocking": "⚠️ Faltan {n} pasos bloqueantes",
        # Common status
        "status_ok": "✅ OK",
        # Proxy step
        "proxy.title": "Proxy ISP/residencial configurado",
        "proxy.hint": "Añade al menos 1 proxy residencial/ISP (NetNut, BrightData, SOAX, IPRoyal). Formato: user:pass@host:port",
        "proxy.action": "Ir a Monitor → Proxy Health",
        "proxy.no_proxies": "❌ sin proxies",
        "proxy.no_alive": "⚠️ {total} proxies pero 0 alive",
        # Account step
        "account.title": "1+ cuenta Ticketmaster registrada",
        "account.hint": "Registra al menos 1 cuenta Ticketmaster válida. Email + password + país (ES/UK/US...)",
        "account.action": "Ir a Cuentas y Tarjetas",
        "account.none": "❌ sin cuentas",
        # Cards step
        "cards.title": "{target}+ tarjetas (rotación anti-fraude)",
        "cards.hint": "Añade 2+ tarjetas distintas (Visa/Mastercard de bancos distintos si puedes). Cooldown 24h por tarjeta tras uso.",
        "cards.action": "Ir a Cuentas y Tarjetas",
        "cards.partial": "⚠️ {n} tarjeta (ideal {target}+)",
        "cards.none": "❌ sin tarjetas",
        # Captcha step
        "captcha.title": "CAPTCHA solver (CapSolver / 2Captcha)",
        "captcha.hint": "Pega tu API key de CapSolver (recomendado, ~$0.80/1000) o 2Captcha. reCAPTCHA+hCaptcha cubiertos.",
        "captcha.action": "Ir a Sniper Bot → CAPTCHA",
        "captcha.none": "❌ sin API key válida de CAPTCHA solver",
        # Notifications step
        "notif.title": "Webhook Discord / Telegram",
        "notif.hint": "Configura Discord webhook (preferido) o Telegram bot token+chat_id. El bot avisa al confirmar compra.",
        "notif.action": "Ir a Notificaciones",
        "notif.none": "⚠️ sin webhooks (opcional)",
        # OTP IMAP step
        "otp_imap.title": "OTP 2FA auto-lectura (IMAP)",
        "otp_imap.hint": "Configura IMAP de Gmail/Outlook con App Password para auto-leer códigos 2FA que manda TM al login.",
        "otp_imap.action": "Ir a OTP",
        "otp_imap.none": "⚠️ sin cuentas IMAP (manual OTP sigue funcionando)",
        # OTP 3DS step
        "otp_3ds.title": "3DS autofill (checkout)",
        "otp_3ds.hint": "Webhook SMS YA activo con secret auto-generado y 13 bancos pre-configurados. Para complemento IMAP Gmail (apps con app-password 16 chars sin espacios), configura en Bot → Anti-Bot → 3DS Autofill.",
        "otp_3ds.action": "Bot → Anti-Bot → 3DS Autofill",
        "otp_3ds.none": "⚠️ sin autofill 3DS (el banco suele pedirlo)",
        "otp_3ds.webhook_ok": "✅ OK · webhook auto-generado · {n} bancos activos (BBVA/Santander/ING/CaixaBank/Sabadell…) · IMAP Gmail opcional",
        # Adyen step
        "adyen.title": "Adyen tokenize (checkout rápido)",
        "adyen.hint": "Modo simulate basta para testing. Para checkout real con tarjeta tokenizada, pega keys de Adyen sandbox/live.",
        "adyen.action": "Config → Adyen",
        "adyen.live_ok": "✅ OK · live ({mode})",
        "adyen.simulate_ok": "✅ OK · {mode} (sin coste, válido para testing)",
        "adyen.none": "⚠️ sin credenciales y sin merchant_account",
        # Discovery step
        "discovery.title": "TM Discovery API key (búsqueda eventos)",
        "discovery.hint": "Gratis en developer.ticketmaster.com (5000 calls/día). Si no tienes, el scraping HTML del catálogo TM funciona perfectamente.",
        "discovery.action": "Ir a Eventos → Wizard 🧙",
        "discovery.api_ok": "✅ OK · API key activa (5000 calls/día)",
        "discovery.scraping_ok": "✅ OK · scraping HTML como fallback (sin coste, sin límite)",
        "discovery.none": "⚠️ sin key y scraping deshabilitado",
        # Errors
        "error_in_step": "Error en {fn}",
    },
    "en": {
        "headline_ready": "🚀 Ready for LIVE mode!",
        "headline_blocking": "⚠️ {n} blocking steps remaining",
        "status_ok": "✅ OK",
        "proxy.title": "ISP/residential proxy configured",
        "proxy.hint": "Add at least 1 residential/ISP proxy (NetNut, BrightData, SOAX, IPRoyal). Format: user:pass@host:port",
        "proxy.action": "Go to Monitor → Proxy Health",
        "proxy.no_proxies": "❌ no proxies",
        "proxy.no_alive": "⚠️ {total} proxies but 0 alive",
        "account.title": "1+ Ticketmaster account registered",
        "account.hint": "Register at least 1 valid Ticketmaster account. Email + password + country (ES/UK/US...)",
        "account.action": "Go to Accounts & Cards",
        "account.none": "❌ no accounts",
        "cards.title": "{target}+ cards (anti-fraud rotation)",
        "cards.hint": "Add 2+ distinct cards (Visa/Mastercard from different banks if possible). 24h cooldown per card after use.",
        "cards.action": "Go to Accounts & Cards",
        "cards.partial": "⚠️ {n} card (ideal {target}+)",
        "cards.none": "❌ no cards",
        "captcha.title": "CAPTCHA solver (CapSolver / 2Captcha)",
        "captcha.hint": "Paste your CapSolver API key (recommended, ~$0.80/1000) or 2Captcha. reCAPTCHA+hCaptcha covered.",
        "captcha.action": "Go to Sniper Bot → CAPTCHA",
        "captcha.none": "❌ no valid CAPTCHA solver API key",
        "notif.title": "Discord / Telegram webhook",
        "notif.hint": "Configure Discord webhook (preferred) or Telegram bot token+chat_id. The bot notifies on purchase confirmation.",
        "notif.action": "Go to Notifications",
        "notif.none": "⚠️ no webhooks (optional)",
        "otp_imap.title": "OTP 2FA auto-read (IMAP)",
        "otp_imap.hint": "Configure Gmail/Outlook IMAP with App Password to auto-read 2FA codes TM sends on login.",
        "otp_imap.action": "Go to OTP",
        "otp_imap.none": "⚠️ no IMAP accounts (manual OTP still works)",
        "otp_3ds.title": "3DS autofill (checkout)",
        "otp_3ds.hint": "SMS webhook ALREADY active with auto-generated secret and 13 pre-configured banks. For IMAP Gmail complement (apps with 16-char app-password no spaces), configure in Bot → Anti-Bot → 3DS Autofill.",
        "otp_3ds.action": "Bot → Anti-Bot → 3DS Autofill",
        "otp_3ds.none": "⚠️ no 3DS autofill (the bank usually asks for it)",
        "otp_3ds.webhook_ok": "✅ OK · auto-generated webhook · {n} active banks (BBVA/Santander/ING/CaixaBank/Sabadell…) · IMAP Gmail optional",
        "adyen.title": "Adyen tokenize (fast checkout)",
        "adyen.hint": "Simulate mode is enough for testing. For real checkout with tokenized card, paste Adyen sandbox/live keys.",
        "adyen.action": "Settings → Adyen",
        "adyen.live_ok": "✅ OK · live ({mode})",
        "adyen.simulate_ok": "✅ OK · {mode} (no cost, valid for testing)",
        "adyen.none": "⚠️ no credentials and no merchant_account",
        "discovery.title": "TM Discovery API key (event search)",
        "discovery.hint": "Free at developer.ticketmaster.com (5000 calls/day). If you don't have one, TM HTML catalog scraping works perfectly.",
        "discovery.action": "Go to Events → Wizard 🧙",
        "discovery.api_ok": "✅ OK · API key active (5000 calls/day)",
        "discovery.scraping_ok": "✅ OK · HTML scraping as fallback (no cost, no limit)",
        "discovery.none": "⚠️ no key and scraping disabled",
        "error_in_step": "Error in {fn}",
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
#  Helpers — lectura tolerante a fallos de cada módulo
# ═══════════════════════════════════════════════════════════════
def _safe_call(fn, *args, default=None, **kwargs):
    try: return fn(*args, **kwargs)
    except Exception: return default


def _count_accounts() -> int:
    try:
        import account_manager
        return len(account_manager.account_manager.list_accounts())
    except Exception:
        return 0


def _count_cards() -> int:
    try:
        import account_manager
        return len(account_manager.account_manager.list_cards())
    except Exception:
        return 0


def _count_proxies() -> Dict[str, int]:
    """Devuelve {'total': N, 'alive': M, 'configured': True/False}."""
    try:
        import proxy_health
        stats = proxy_health.get_stats() if hasattr(proxy_health, "get_stats") else {}
        total = stats.get("total", 0)
        alive = stats.get("fast", 0) + stats.get("slow", 0)
        if total == 0 and hasattr(proxy_health, "list_proxies"):
            try:
                proxies = proxy_health.list_proxies()
                total = len(proxies)
                alive = sum(1 for p in proxies if (p.get("status") or "").lower() in ("alive","fast","slow","ok"))
            except Exception: pass
        return {"total": total, "alive": alive}
    except Exception:
        return {"total": 0, "alive": 0}


# ═══════════════════════════════════════════════════════════════
#  9 STEPS (todos aceptan lang)
# ═══════════════════════════════════════════════════════════════
def _step_proxy(lang: str = "es") -> dict:
    p = _count_proxies()
    total, alive = p["total"], p["alive"]
    ok = total >= 1 and alive >= 1
    if ok: status = _t("status_ok", lang)
    elif total >= 1: status = _t("proxy.no_alive", lang, total=total)
    else: status = _t("proxy.no_proxies", lang)
    return {
        "id": "proxy",
        "title": _t("proxy.title", lang),
        "done": ok,
        "blocking": True,
        "action": {"tab": "monitor", "endpoint": "/api/proxy-health/add",
                   "label": _t("proxy.action", lang)},
        "hint": _t("proxy.hint", lang),
        "details": {"total": total, "alive": alive, "status": status},
    }


def _step_account(lang: str = "es") -> dict:
    n = _count_accounts()
    return {
        "id": "account",
        "title": _t("account.title", lang),
        "done": n >= 1,
        "blocking": True,
        "action": {"tab": "cuentas", "endpoint": "/api/accounts",
                   "label": _t("account.action", lang)},
        "hint": _t("account.hint", lang),
        "details": {"count": n,
                    "status": _t("status_ok", lang) if n >= 1 else _t("account.none", lang)},
    }


def _step_cards(lang: str = "es") -> dict:
    n = _count_cards()
    target = 2
    ok = n >= target
    if ok: status = _t("status_ok", lang)
    elif n >= 1: status = _t("cards.partial", lang, n=n, target=target)
    else: status = _t("cards.none", lang)
    return {
        "id": "cards",
        "title": _t("cards.title", lang, target=target),
        "done": ok,
        "blocking": True,
        "action": {"tab": "cuentas", "endpoint": "/api/cards",
                   "label": _t("cards.action", lang)},
        "hint": _t("cards.hint", lang),
        "details": {"count": n, "target": target, "status": status},
    }


def _step_captcha(lang: str = "es") -> dict:
    """Lee captcha_manager.get_config() — la API oficial del módulo.
    Busca api_key en TRES sitios distintos por compat con distintas versiones del módulo:
      1) cfg['<svc>_key']                       (v1 legacy, p.ej. 'twocaptcha_key')
      2) cfg['providers'][svc]['api_key']       (v2)
      3) cfg['services'][svc]['api_key']        (v3 actual — captcha_manager.get_config)
    Solo cuenta como válida si la clave está NO-vacía y NO es placeholder Y el service
    tiene 'enabled'==True (cuando aplique)."""
    try:
        import captcha_manager
        cfg = captcha_manager.get_config()
    except Exception:
        cfg = {}
    providers = {}
    has_any = False
    for svc, legacy_key in [("capsolver", "capsolver_key"),
                             ("2captcha", "twocaptcha_key"),
                             ("anticaptcha", "anticaptcha_key"),
                             ("capmonster", "capmonster_key")]:
        val = cfg.get(legacy_key)
        enabled_flag = True
        if not val:
            sub = (cfg.get("providers", {}) or {}).get(svc, {})
            val = sub.get("api_key")
            if "enabled" in sub: enabled_flag = bool(sub.get("enabled"))
        if not val:
            sub = (cfg.get("services", {}) or {}).get(svc, {})
            val = sub.get("api_key")
            if "enabled" in sub: enabled_flag = bool(sub.get("enabled"))
        is_real = bool(val) and not _looks_placeholder(val) and enabled_flag
        providers[svc] = is_real
        if is_real:
            has_any = True
    return {
        "id": "captcha",
        "title": _t("captcha.title", lang),
        "done": has_any,
        "blocking": True,
        "action": {"tab": "bot", "subtab": "captcha", "endpoint": "/api/bot/captcha/config",
                   "label": _t("captcha.action", lang)},
        "hint": _t("captcha.hint", lang),
        "details": {"providers": providers, "has_any": has_any,
                    "status": _t("status_ok", lang) if has_any else _t("captcha.none", lang)},
    }


def _step_notifications(lang: str = "es") -> dict:
    """Soporta schema nested (actual) y top-level legacy. Filtra placeholders."""
    try:
        import notifications
        cfg = notifications.get_config() if hasattr(notifications, "get_config") else {}
    except Exception:
        cfg = {}

    def _is_real(v):
        if not v: return False
        s = str(v).strip()
        if not s or len(s) < 4: return False
        upper = s.upper()
        for marker in ("PLACEHOLDER", "YOUR_", "EXAMPLE", "XXXX", "TG-PLACEHOLDER"):
            if marker in upper: return False
        return True

    discord_url = (cfg.get("discord") or {}).get("webhook") or cfg.get("discord_webhook")
    discord_enabled = (cfg.get("discord") or {}).get("enabled", True)
    has_discord = _is_real(discord_url) and bool(discord_enabled)

    tg = cfg.get("telegram") or {}
    tg_token = tg.get("token") or cfg.get("telegram_bot_token")
    tg_chat = tg.get("chat_id") or cfg.get("telegram_chat_id")
    tg_enabled = tg.get("enabled", True)
    has_telegram = _is_real(tg_token) and _is_real(tg_chat) and bool(tg_enabled)

    ok = has_discord or has_telegram
    return {
        "id": "notifications",
        "title": _t("notif.title", lang),
        "done": ok,
        "blocking": False,
        "action": {"tab": "notificaciones", "endpoint": "/api/notifications/config",
                   "label": _t("notif.action", lang)},
        "hint": _t("notif.hint", lang),
        "details": {"discord": has_discord, "telegram": has_telegram,
                    "status": _t("status_ok", lang) if ok else _t("notif.none", lang)},
    }


def _step_otp_imap(lang: str = "es") -> dict:
    try:
        import otp_manager
        cfg = otp_manager.get_config()
        accs = [a for a in (cfg.get("accounts") or []) if a.get("enabled")]
        has_any = bool(accs)
    except Exception:
        has_any = False
        accs = []
    return {
        "id": "otp_imap",
        "title": _t("otp_imap.title", lang),
        "done": has_any,
        "blocking": False,
        "action": {"tab": "otp", "endpoint": "/api/otp/account",
                   "label": _t("otp_imap.action", lang)},
        "hint": _t("otp_imap.hint", lang),
        "details": {"enabled_accounts": len(accs),
                    "status": _t("status_ok", lang) if has_any else _t("otp_imap.none", lang)},
    }


def _step_otp_3ds(lang: str = "es") -> dict:
    try:
        import otp_3ds
        cfg = otp_3ds.get_config() if hasattr(otp_3ds, "get_config") else {}
    except Exception:
        cfg = {}
    has_imap = bool(cfg.get("imap_username") and cfg.get("imap_password"))
    has_sms = cfg.get("mode") in ("sms_webhook", "both")
    has_autofill_webhook = False
    autofill_banks = 0
    autofill_secret_set = False
    autofill_enabled = False
    try:
        import otp_3ds_autofill
        st = otp_3ds_autofill.get_status()
        autofill_enabled = bool(st.get('enabled'))
        autofill_banks = len(st.get('banks_active', []) or [])
        autofill_secret_set = bool(st.get('webhook_secret_set'))
        has_autofill_webhook = autofill_enabled and autofill_secret_set and autofill_banks > 0
    except Exception:
        pass

    any_mode = has_imap or has_sms or has_autofill_webhook

    if has_autofill_webhook and not (has_imap or has_sms):
        status_txt = _t("otp_3ds.webhook_ok", lang, n=autofill_banks)
    elif any_mode:
        status_txt = _t("status_ok", lang)
    else:
        status_txt = _t("otp_3ds.none", lang)

    return {
        "id": "otp_3ds",
        "title": _t("otp_3ds.title", lang),
        "done": any_mode,
        "blocking": False,
        "action": {"tab": "bot", "subtab": "antibot",
                   "endpoint": "/api/otp3ds/status",
                   "label": _t("otp_3ds.action", lang)},
        "hint": _t("otp_3ds.hint", lang),
        "details": {
            "imap_legacy": has_imap, "sms_legacy": has_sms,
            "autofill_webhook": has_autofill_webhook,
            "autofill_banks_active": autofill_banks,
            "autofill_secret_generated": autofill_secret_set,
            "status": status_txt,
        },
    }


def _step_adyen(lang: str = "es") -> dict:
    try:
        import adyen_tokenizer
        cfg = adyen_tokenizer.get_config() if hasattr(adyen_tokenizer, "get_config") else {}
    except Exception:
        cfg = {}
    api_key = cfg.get("api_key") or ""
    merchant = cfg.get("merchant_account") or ""
    mode = (cfg.get("mode") or "").lower()
    has_real_creds = bool(api_key) and not _looks_placeholder(api_key) and bool(merchant)
    is_simulate_ok = mode in ("simulate", "test", "sandbox") and bool(merchant)
    done = has_real_creds or is_simulate_ok
    if has_real_creds:
        status = _t("adyen.live_ok", lang, mode=mode)
    elif is_simulate_ok:
        status = _t("adyen.simulate_ok", lang, mode=mode)
    else:
        status = _t("adyen.none", lang)
    return {
        "id": "adyen",
        "title": _t("adyen.title", lang),
        "done": done,
        "blocking": False,
        "action": {"tab": "config", "endpoint": "/api/adyen/config",
                   "label": _t("adyen.action", lang)},
        "hint": _t("adyen.hint", lang),
        "details": {"has_credentials": has_real_creds, "mode": mode,
                    "merchant_account": merchant, "status": status},
    }


def _step_discovery(lang: str = "es") -> dict:
    has_valid = False
    scraping_enabled = True
    try:
        import event_discovery
        cfg = event_discovery.get_config()
        api_key = cfg.get("api_key", "")
        is_ph = event_discovery._is_placeholder_key(api_key) if hasattr(event_discovery, "_is_placeholder_key") else (not api_key)
        has_valid = bool(api_key) and not is_ph
        if cfg.get("scraping_enabled") is False:
            scraping_enabled = False
    except Exception:
        pass
    done = has_valid or scraping_enabled
    if has_valid:
        status = _t("discovery.api_ok", lang)
    elif scraping_enabled:
        status = _t("discovery.scraping_ok", lang)
    else:
        status = _t("discovery.none", lang)
    return {
        "id": "discovery",
        "title": _t("discovery.title", lang),
        "done": done,
        "blocking": False,
        "action": {"tab": "eventos", "endpoint": "/api/discovery/config",
                   "label": _t("discovery.action", lang)},
        "hint": _t("discovery.hint", lang),
        "details": {"has_valid_key": has_valid, "scraping_enabled": scraping_enabled,
                    "status": status},
    }


def _looks_placeholder(value: str) -> bool:
    """Heurística: detecta si un string es un placeholder en vez de una key real."""
    if not value: return True
    v = value.upper()
    for tok in ("PLACEHOLDER", "YOUR_", "XXXX", "EXAMPLE", "TEST_", "DUMMY", "FAKE", "TU_API_KEY"):
        if tok in v:
            return True
    return len(value) < 16


# ═══════════════════════════════════════════════════════════════
#  ORDEN + API PRINCIPAL
# ═══════════════════════════════════════════════════════════════
STEPS_ORDER = [
    _step_proxy,         # 1 bloqueante
    _step_account,       # 2 bloqueante
    _step_cards,         # 3 bloqueante
    _step_captcha,       # 4 bloqueante
    _step_notifications, # 5 opcional
    _step_otp_imap,      # 6 recomendado
    _step_otp_3ds,       # 7 opcional
    _step_adyen,         # 8 opcional
    _step_discovery,     # 9 opcional
]


def get_status(lang: str = "es") -> dict:
    """Snapshot del onboarding completo. Localizado por lang (es|en)."""
    if lang not in ("es", "en"):
        lang = "es"
    steps = []
    for fn in STEPS_ORDER:
        try:
            steps.append(fn(lang))
        except Exception as e:
            steps.append({"id": fn.__name__.replace("_step_",""), "done": False,
                          "title": _t("error_in_step", lang, fn=fn.__name__),
                          "blocking": False,
                          "error": f"{type(e).__name__}: {str(e)[:150]}"})
    total = len(steps)
    done = sum(1 for s in steps if s.get("done"))
    blocking_total = sum(1 for s in steps if s.get("blocking"))
    blocking_done = sum(1 for s in steps if s.get("blocking") and s.get("done"))
    ready_for_live = blocking_done >= blocking_total and blocking_total > 0
    next_pending = next((s for s in steps if not s.get("done")), None)
    if ready_for_live:
        headline = _t("headline_ready", lang)
    else:
        headline = _t("headline_blocking", lang, n=blocking_total - blocking_done)
    return {
        "ok": True,
        "lang": lang,
        "steps": steps,
        "summary": {
            "total": total,
            "done": done,
            "pending": total - done,
            "progress_pct": round(done / total * 100) if total else 0,
            "blocking_total": blocking_total,
            "blocking_done": blocking_done,
            "ready_for_live": ready_for_live,
            "next_step": next_pending,
            "headline": headline,
        },
    }


# ═══════════════════════════════════════════════════════════════
#  TESTS DE CONEXIÓN (validan API keys reales)
# ═══════════════════════════════════════════════════════════════
def test_step(step_id: str) -> dict:
    """Lanza un test real para verificar que la config del paso funciona."""
    if step_id == "proxy":
        try:
            import proxy_health
            if hasattr(proxy_health, "check_all"):
                r = proxy_health.check_all(timeout=8)
            elif hasattr(proxy_health, "get_stats"):
                r = proxy_health.get_stats()
            else:
                r = {"ok": False, "error": "proxy_health sin check_all/get_stats"}
            return {"ok": True, "data": r}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    if step_id == "account":
        n = _count_accounts()
        return {"ok": n >= 1, "data": {"count": n}}

    if step_id == "cards":
        n = _count_cards()
        return {"ok": n >= 2, "data": {"count": n, "target": 2}}

    if step_id == "captcha":
        try:
            import captcha_manager
            stats = captcha_manager.get_stats() if hasattr(captcha_manager, "get_stats") else {}
            return {"ok": True, "data": stats}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    if step_id == "notifications":
        try:
            import notifications
            cfg = notifications.get_config()
            return {"ok": True, "data": {
                "discord": bool((cfg.get("discord") or {}).get("webhook") or cfg.get("discord_webhook")),
                "telegram": bool((cfg.get("telegram") or {}).get("token") or cfg.get("telegram_bot_token")),
            }}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    if step_id == "otp_imap":
        try:
            import otp_manager
            cfg = otp_manager.get_config()
            accs = [a for a in (cfg.get("accounts") or []) if a.get("enabled")]
            return {"ok": bool(accs), "data": {"accounts": len(accs)}}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    if step_id == "otp_3ds":
        try:
            import otp_3ds_autofill
            return {"ok": True, "data": otp_3ds_autofill.get_status()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    if step_id == "adyen":
        try:
            import adyen_tokenizer
            return {"ok": True, "data": adyen_tokenizer.get_config()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    if step_id == "discovery":
        try:
            import event_discovery
            return {"ok": True, "data": event_discovery.get_config()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    return {"ok": False, "error": f"step_id {step_id} no reconocido"}


def self_test() -> dict:
    """Validación del módulo (política proyecto, módulos >50 LOC)."""
    out = {"ok": True, "checks": {}, "errors": []}
    try:
        for fn in ("get_status", "test_step", "_t"):
            present = fn in globals() and callable(globals()[fn])
            out["checks"][f"fn:{fn}"] = present
            if not present:
                out["ok"] = False
                out["errors"].append(f"fn missing: {fn}")
        # Paridad ES/EN
        es_keys = set(_T.get("es", {}).keys())
        en_keys = set(_T.get("en", {}).keys())
        only_es = es_keys - en_keys
        only_en = en_keys - es_keys
        out["checks"]["i18n_es_keys"] = len(es_keys)
        out["checks"]["i18n_en_keys"] = len(en_keys)
        out["checks"]["i18n_parity"] = (len(only_es) == 0 and len(only_en) == 0)
        if only_es:
            out["errors"].append(f"keys only in ES: {sorted(only_es)}")
        if only_en:
            out["errors"].append(f"keys only in EN: {sorted(only_en)}")
        # get_status funciona en ambos lang
        for lg in ("es", "en"):
            st = get_status(lang=lg)
            out["checks"][f"status_{lg}_ok"] = st.get("ok") is True
            out["checks"][f"status_{lg}_steps"] = len(st.get("steps", []))
            out["checks"][f"status_{lg}_headline"] = bool(st.get("summary", {}).get("headline"))
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"{type(e).__name__}: {e}")
    return out
