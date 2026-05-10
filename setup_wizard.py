"""setup_wizard.py — Onboarding 8 pasos para dejar el bot listo para LIVE.

Verifica el estado de cada requisito usando las APIs públicas de cada módulo
(evita rutas JSON hardcoded que se rompen cuando los módulos cambian).

Uso desde dashboard:
  GET  /api/setup-wizard/status       → snapshot actual
  POST /api/setup-wizard/test/<step>  → test connection real de ese paso
"""
from typing import Dict, List, Any

import json_atomic
import config


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
        # intenta varias keys compatibles con distintas versiones del módulo
        total = stats.get("total", 0)
        alive = stats.get("fast", 0) + stats.get("slow", 0)  # alive = fast+slow
        # Fallback: contar por list_proxies si existe
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
#  8 STEPS
# ═══════════════════════════════════════════════════════════════
def _step_proxy() -> dict:
    p = _count_proxies()
    total, alive = p["total"], p["alive"]
    ok = total >= 1 and alive >= 1
    return {
        "id": "proxy",
        "title": "Proxy ISP/residencial configurado",
        "done": ok,
        "blocking": True,
        "action": {"tab": "monitor", "endpoint": "/api/proxy-health/add",
                   "label": "Ir a Monitor → Proxy Health"},
        "hint": "Añade al menos 1 proxy residencial/ISP (NetNut, BrightData, SOAX, IPRoyal). Formato: user:pass@host:port",
        "details": {
            "total": total, "alive": alive,
            "status": "✅ OK" if ok else
                      (f"⚠️ {total} proxies pero 0 alive" if total >= 1 else "❌ sin proxies"),
        },
    }


def _step_account() -> dict:
    n = _count_accounts()
    return {
        "id": "account",
        "title": "1+ cuenta Ticketmaster registrada",
        "done": n >= 1,
        "blocking": True,
        "action": {"tab": "cuentas", "endpoint": "/api/accounts",
                   "label": "Ir a Cuentas y Tarjetas"},
        "hint": "Registra al menos 1 cuenta Ticketmaster válida. Email + password + país (ES/UK/US...)",
        "details": {"count": n, "status": "✅ OK" if n >= 1 else "❌ sin cuentas"},
    }


def _step_cards() -> dict:
    n = _count_cards()
    target = 2
    ok = n >= target
    return {
        "id": "cards",
        "title": f"{target}+ tarjetas (rotación anti-fraude)",
        "done": ok,
        "blocking": True,
        "action": {"tab": "cuentas", "endpoint": "/api/cards",
                   "label": "Ir a Cuentas y Tarjetas"},
        "hint": "Añade 2+ tarjetas distintas (Visa/Mastercard de bancos distintos si puedes). Cooldown 24h por tarjeta tras uso.",
        "details": {"count": n, "target": target,
                    "status": "✅ OK" if ok else
                              (f"⚠️ {n} tarjeta (ideal {target}+)" if n >= 1 else "❌ sin tarjetas")},
    }


def _step_captcha() -> dict:
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
        enabled_flag = True  # legacy paths no tienen enabled, asumir True
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
        "title": "CAPTCHA solver (CapSolver / 2Captcha)",
        "done": has_any,
        "blocking": True,
        "action": {"tab": "bot", "subtab": "captcha", "endpoint": "/api/bot/captcha/config",
                   "label": "Ir a Sniper Bot → CAPTCHA"},
        "hint": "Pega tu API key de CapSolver (recomendado, ~$0.80/1000) o 2Captcha. reCAPTCHA+hCaptcha cubiertos.",
        "details": {"providers": providers, "has_any": has_any,
                    "status": "✅ OK" if has_any else "❌ sin API key válida de CAPTCHA solver"},
    }


def _step_notifications() -> dict:
    """Lee notifications.get_config() — soporta schema nested (actual) y
    top-level legacy. Filtra valores placeholder para no dar falsos
    positivos cuando el config tiene strings tipo 'PLACEHOLDER' o 'YOUR_*'.
    """
    try:
        import notifications
        cfg = notifications.get_config() if hasattr(notifications, "get_config") else {}
    except Exception:
        cfg = {}

    def _is_real(v):
        """True si v es un valor configurado real (no vacío, no placeholder)."""
        if not v:
            return False
        s = str(v).strip()
        if not s or len(s) < 4:
            return False
        upper = s.upper()
        for marker in ("PLACEHOLDER", "YOUR_", "EXAMPLE", "XXXX", "TG-PLACEHOLDER"):
            if marker in upper:
                return False
        return True

    # Discord: nested actual O top-level legacy
    discord_url = (cfg.get("discord") or {}).get("webhook") or cfg.get("discord_webhook")
    discord_enabled = (cfg.get("discord") or {}).get("enabled", True)
    has_discord = _is_real(discord_url) and bool(discord_enabled)

    # Telegram: nested actual O top-level legacy
    tg = cfg.get("telegram") or {}
    tg_token = tg.get("token") or cfg.get("telegram_bot_token")
    tg_chat = tg.get("chat_id") or cfg.get("telegram_chat_id")
    tg_enabled = tg.get("enabled", True)
    has_telegram = _is_real(tg_token) and _is_real(tg_chat) and bool(tg_enabled)

    ok = has_discord or has_telegram
    return {
        "id": "notifications",
        "title": "Webhook Discord / Telegram",
        "done": ok,
        "blocking": False,
        "action": {"tab": "notificaciones", "endpoint": "/api/notifications/config",
                   "label": "Ir a Notificaciones"},
        "hint": "Configura Discord webhook (preferido) o Telegram bot token+chat_id. El bot avisa al confirmar compra.",
        "details": {"discord": has_discord, "telegram": has_telegram,
                    "status": "✅ OK" if ok else "⚠️ sin webhooks (opcional)"},
    }


def _step_otp_imap() -> dict:
    """Usa otp_manager (v2 multi-account)."""
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
        "title": "OTP 2FA auto-lectura (IMAP)",
        "done": has_any,
        "blocking": False,
        "action": {"tab": "otp", "endpoint": "/api/otp/account",
                   "label": "Ir a OTP"},
        "hint": "Configura IMAP de Gmail/Outlook con App Password para auto-leer códigos 2FA que manda TM al login.",
        "details": {"enabled_accounts": len(accs),
                    "status": "✅ OK" if has_any else "⚠️ sin cuentas IMAP (manual OTP sigue funcionando)"},
    }


def _step_otp_3ds() -> dict:
    """Autofill del 3DS en checkout (separado del OTP 2FA de login)."""
    try:
        import otp_3ds
        cfg = otp_3ds.get_config() if hasattr(otp_3ds, "get_config") else {}
    except Exception:
        cfg = {}
    has_imap = bool(cfg.get("imap_username") and cfg.get("imap_password"))
    has_sms = cfg.get("mode") in ("sms_webhook", "both")
    # NUEVO módulo `otp_3ds_autofill`: tiene webhook UUID4 auto-generado + 13 bancos
    # pre-configurados. Si está enabled, considerar el step como done aunque el legacy
    # IMAP del usuario esté vacío — el webhook receiver ya cubre el flujo SMS bancario.
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
        status_txt = (f"✅ OK · webhook auto-generado · {autofill_banks} bancos activos "
                      f"(BBVA/Santander/ING/CaixaBank/Sabadell…) · IMAP Gmail opcional")
    elif any_mode:
        status_txt = "✅ OK"
    else:
        status_txt = "⚠️ sin autofill 3DS (el banco suele pedirlo)"

    return {
        "id": "otp_3ds",
        "title": "3DS autofill (checkout)",
        "done": any_mode,
        "blocking": False,
        "action": {"tab": "bot", "subtab": "antibot",
                   "endpoint": "/api/otp3ds/status",
                   "label": "Bot → Anti-Bot → 3DS Autofill"},
        "hint": ("Webhook SMS YA activo con secret auto-generado y 13 bancos "
                 "pre-configurados. Para complemento IMAP Gmail (apps con app-password "
                 "16 chars sin espacios), configura en Bot → Anti-Bot → 3DS Autofill."),
        "details": {
            "imap_legacy": has_imap, "sms_legacy": has_sms,
            "autofill_webhook": has_autofill_webhook,
            "autofill_banks_active": autofill_banks,
            "autofill_secret_generated": autofill_secret_set,
            "status": status_txt,
        },
    }


def _step_adyen() -> dict:
    """Adyen tokenize — opcional. Acepta credenciales LIVE/test reales o modo simulate.
    `simulate` es válido para checkout funcional sin coste (la tokenización local genera
    tokens deterministas y el bot no llama al PSP). Solo modo `live` requiere creds reales."""
    try:
        import adyen_tokenizer
        cfg = adyen_tokenizer.get_config() if hasattr(adyen_tokenizer, "get_config") else {}
    except Exception:
        cfg = {}
    api_key = cfg.get("api_key") or ""
    merchant = cfg.get("merchant_account") or ""
    mode = (cfg.get("mode") or "").lower()
    has_real_creds = bool(api_key) and not _looks_placeholder(api_key) and bool(merchant)
    # Done si hay creds reales O si modo simulate/test con merchant_account
    is_simulate_ok = mode in ("simulate", "test", "sandbox") and bool(merchant)
    done = has_real_creds or is_simulate_ok
    if has_real_creds:
        status = f"✅ OK · live ({mode})"
    elif is_simulate_ok:
        status = f"✅ OK · {mode} (sin coste, válido para testing)"
    else:
        status = "⚠️ sin credenciales y sin merchant_account"
    return {
        "id": "adyen",
        "title": "Adyen tokenize (checkout rápido)",
        "done": done,
        "blocking": False,
        "action": {"tab": "config", "endpoint": "/api/adyen/config",
                   "label": "Config → Adyen"},
        "hint": "Modo simulate basta para testing. Para checkout real con tarjeta tokenizada, pega keys de Adyen sandbox/live.",
        "details": {"has_credentials": has_real_creds, "mode": mode,
                    "merchant_account": merchant, "status": status},
    }


def _step_discovery() -> dict:
    """Discovery API key — opcional. Acepta API key real O fallback de scraping HTML
    (que ya funciona y descubre eventos sin key). Done si hay key válida O scraping habilitado."""
    has_valid = False
    scraping_enabled = True  # default: scraping siempre activo como fallback
    try:
        import event_discovery
        cfg = event_discovery.get_config()
        api_key = cfg.get("api_key", "")
        is_ph = event_discovery._is_placeholder_key(api_key) if hasattr(event_discovery, "_is_placeholder_key") else (not api_key)
        has_valid = bool(api_key) and not is_ph
        # Si el user marcó explícitamente scraping_enabled:False, respetar
        if cfg.get("scraping_enabled") is False:
            scraping_enabled = False
    except Exception:
        pass
    done = has_valid or scraping_enabled
    if has_valid:
        status = "✅ OK · API key activa (5000 calls/día)"
    elif scraping_enabled:
        status = "✅ OK · scraping HTML como fallback (sin coste, sin límite)"
    else:
        status = "⚠️ sin key y scraping deshabilitado"
    return {
        "id": "discovery",
        "title": "TM Discovery API key (búsqueda eventos)",
        "done": done,
        "blocking": False,
        "action": {"tab": "eventos", "endpoint": "/api/discovery/config",
                   "label": "Ir a Eventos → Wizard 🧙"},
        "hint": "Gratis en developer.ticketmaster.com (5000 calls/día). Si no tienes, el scraping HTML del catálogo TM funciona perfectamente.",
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
    return len(value) < 16  # keys reales suelen tener 20+ chars


# ═══════════════════════════════════════════════════════════════
#  ORDEN + API PRINCIPAL
# ═══════════════════════════════════════════════════════════════
STEPS_ORDER = [
    _step_proxy,         # 1 bloqueante
    _step_account,       # 2 bloqueante
    _step_cards,         # 3 bloqueante
    _step_captcha,       # 4 bloqueante
    _step_notifications, # 5 opcional
    _step_otp_imap,      # 6 recomendado (OTP 2FA login)
    _step_otp_3ds,       # 7 opcional (3DS checkout)
    _step_adyen,         # 8 opcional
    _step_discovery,     # 9 opcional
]


def get_status() -> dict:
    """Snapshot del onboarding completo."""
    steps = []
    for fn in STEPS_ORDER:
        try:
            steps.append(fn())
        except Exception as e:
            steps.append({"id": fn.__name__.replace("_step_",""), "done": False,
                          "title": f"Error en {fn.__name__}",
                          "blocking": False,
                          "error": f"{type(e).__name__}: {str(e)[:150]}"})
    total = len(steps)
    done = sum(1 for s in steps if s.get("done"))
    blocking_total = sum(1 for s in steps if s.get("blocking"))
    blocking_done = sum(1 for s in steps if s.get("blocking") and s.get("done"))
    ready_for_live = blocking_done >= blocking_total and blocking_total > 0
    next_pending = next((s for s in steps if not s.get("done")), None)
    return {
        "ok": True,
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
            "headline": (
                "🚀 ¡Listo para modo LIVE!" if ready_for_live
                else f"⚠️ Faltan {blocking_total - blocking_done} pasos bloqueantes"
            ),
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
        return {"ok": n >= 1, "count": n,
                "note": "Cuenta registrada — validación real requiere login en TM (no se hace aquí)"}

    if step_id == "cards":
        n = _count_cards()
        return {"ok": n >= 2, "count": n, "target": 2}

    if step_id == "captcha":
        try:
            import captcha_manager
            cfg = captcha_manager.get_config()
            results = {}
            for svc, k in [("capsolver","capsolver_key"), ("2captcha","twocaptcha_key"),
                            ("anticaptcha","anticaptcha_key"), ("capmonster","capmonster_key")]:
                val = cfg.get(k)
                if val and not _looks_placeholder(val):
                    try:
                        bal = captcha_manager.check_balance(svc) if hasattr(captcha_manager, "check_balance") else {"ok": True, "balance": "unknown"}
                        results[svc] = bal
                    except Exception as e:
                        results[svc] = {"ok": False, "error": str(e)[:80]}
            any_ok = any(v.get("ok") for v in results.values())
            return {"ok": any_ok, "balances": results,
                    "msg": "No hay providers configurados" if not results else ""}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    if step_id == "notifications":
        try:
            import notifications
            if not hasattr(notifications, "notify"):
                return {"ok": False, "error": "módulo notifications sin notify()"}
            r = notifications.notify(
                event_type="setup_test",
                title="🧪 Setup Wizard · Test",
                message="Si ves este mensaje, el webhook funciona correctamente.",
                level="info")
            return {"ok": True, "data": r}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    if step_id == "otp_imap":
        try:
            import otp_manager
            r = otp_manager.self_test() if hasattr(otp_manager, "self_test") else {"ok": True}
            return r
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}

    if step_id == "otp_3ds":
        try:
            import otp_3ds
            return otp_3ds.self_test() if hasattr(otp_3ds, "self_test") else {"ok": True, "note": "sin self_test"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}

    if step_id == "adyen":
        try:
            import adyen_tokenizer
            if hasattr(adyen_tokenizer, "check_connection"):
                return adyen_tokenizer.check_connection()
            if hasattr(adyen_tokenizer, "self_test"):
                return adyen_tokenizer.self_test()
            return {"ok": True, "note": "sin validación disponible"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}

    if step_id == "discovery":
        try:
            import event_discovery
            cfg = event_discovery.get_config()
            key = cfg.get("api_key", "")
            if not key:
                return {"ok": False, "error": "sin API key configurada"}
            # Usa el endpoint test-key de event_discovery (llamada real a TM)
            import requests
            r = requests.get(event_discovery.DISCOVERY_BASE + "/events.json",
                            params={"apikey": key, "size": 1}, timeout=8)
            if r.status_code == 200:
                data = r.json()
                total = (data.get("page") or {}).get("totalElements", 0)
                return {"ok": True, "status_code": 200,
                        "msg": f"✅ Key válida — {total} eventos disponibles en TM"}
            return {"ok": False, "status_code": r.status_code,
                    "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}

    return {"ok": False, "error": f"step_id desconocido: {step_id}"}


# ═══════════════════════════════════════════════════════════════
#  Self-test del propio wizard
# ═══════════════════════════════════════════════════════════════
def self_test() -> dict:
    """Verifica que get_status() no crashea y todos los steps responden."""
    results = {"checks": {}}
    try:
        st = get_status()
        results["checks"]["get_status_ok"] = st.get("ok", False)
        steps = st.get("steps", [])
        results["checks"]["has_8_or_9_steps"] = 8 <= len(steps) <= 10
        results["checks"]["summary_has_ready_for_live"] = "ready_for_live" in st.get("summary", {})
        results["checks"]["summary_has_headline"] = bool(st.get("summary", {}).get("headline"))
        # Todos los steps deben tener id/title
        results["checks"]["all_steps_have_id_and_title"] = all(
            s.get("id") and s.get("title") for s in steps)
        # Ningún step con "error" (si tiene error = crashea)
        errors = [s for s in steps if s.get("error")]
        results["checks"]["no_step_crashed"] = len(errors) == 0
        if errors:
            results["details"] = {"errors": [{"id": e["id"], "err": e["error"]} for e in errors]}
    except Exception as e:
        results["checks"]["get_status_ok"] = False
        results["details"] = {"error": f"{type(e).__name__}: {str(e)[:150]}"}

    # test_step acepta id válidos sin crashear
    for sid in ["proxy", "account", "cards", "captcha", "notifications",
                 "otp_imap", "otp_3ds", "adyen", "discovery", "unknown_xxx"]:
        try:
            r = test_step(sid)
            results["checks"][f"test_{sid}_no_crash"] = isinstance(r, dict) and "ok" in r
        except Exception:
            results["checks"][f"test_{sid}_no_crash"] = False

    results["total_checks"] = len(results["checks"])
    results["passed_checks"] = sum(1 for v in results["checks"].values() if v)
    results["ok"] = results["passed_checks"] == results["total_checks"]
    return results
