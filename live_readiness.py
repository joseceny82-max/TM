"""live_readiness.py — Comprueba qué falta para activar el bot en modo LIVE.

Audita 8 dimensiones y devuelve un checklist con score y acciones recomendadas.
También provee setup helpers para automatizar lo automatizable.

API:
    check() -> dict      # checklist completo + score
    auto_setup() -> dict # setup automatizado (proxies demo, AcpAT seed)
    set_captcha_key(provider, key) -> dict
    set_telegram(token, chat_id) -> dict
    seed_acpat(n=5) -> dict
    self_test() -> dict
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
import time as _time_mod
_CHECK_CACHE = {"data": None, "ts": 0, "ttl_s": 30}
_check_cache_lock = threading.RLock()


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
def _check_accounts() -> Dict[str, Any]:
    try:
        import account_manager
        accs = account_manager.account_manager.list_accounts()
        cards = account_manager.account_manager.list_cards()
        active = [a for a in accs if a.get("status") == "active"]
        ok = len(active) >= 1 and len(cards) >= 1
        return {
            "name": "Cuentas y Tarjetas",
            "ok": ok,
            "weight": 15,
            "stats": {
                "total_accounts": len(accs),
                "active_accounts": len(active),
                "cards": len(cards),
            },
            "msg": (f"{len(active)} cuentas activas · {len(cards)} tarjetas"
                    if ok else "Necesitas al menos 1 cuenta activa con 1 tarjeta"),
            "fix_url": "/dashboard#cuentas",
            "fix_action": "Añadir cuentas en pestaña Cuentas y Tarjetas",
        }
    except Exception as e:
        return {"name": "Cuentas y Tarjetas", "ok": False, "weight": 15,
                "msg": f"Error: {e}", "fix_action": "Revisar account_manager"}


def _read_proxies_txt() -> List[Dict[str, Any]]:
    """Lee proxies.txt (formato bulk del usuario) y devuelve entries normalizadas."""
    paths = []
    try:
        if hasattr(config, "BASE_DIR"):
            paths.append(os.path.join(config.BASE_DIR, "proxies.txt"))
    except Exception:
        pass
    paths.append("proxies.txt")
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


def _check_proxies() -> Dict[str, Any]:
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
            msg = f"{len(alive_real)} proxies disponibles"
            if txt_proxies:
                msg += f" · {len(txt_proxies)} en proxies.txt"
            if json_proxies and not all(p.get("is_placeholder") for p in json_proxies):
                json_real = sum(1 for p in json_proxies if not p.get("is_placeholder"))
                if json_real > 0:
                    msg += f" · {json_real} en proxies.json"
        elif placeholders:
            msg = f"⚠️ Solo {len(placeholders)} proxies placeholder — sustitúyelos por reales"
        else:
            msg = "Sin proxies alive — CRÍTICO para Queue-it"

        return {
            "name": "Proxies",
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
            "fix_action": "Añadir proxies reales (BrightData, IPRoyal, SOAX, ProxyEmpire) — NO placeholders",
            "fix_url": "/dashboard#bot/proxies",
        }
    except Exception as e:
        return {"name": "Proxies", "ok": False, "weight": 20,
                "msg": f"Error: {e}", "fix_action": "Revisar proxy_health"}


def _check_captcha() -> Dict[str, Any]:
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
        msg = f"✅ Configurados: {', '.join(configured)}"
    elif is_placeholder_only:
        msg = f"⚠️ Hay key placeholder en '{primary_provider}' — sustitúyela por la real"
    else:
        msg = "Sin API key — el bot no podrá resolver CAPTCHAs"

    return {
        "name": "CAPTCHA Solvers",
        "ok": ok,
        "weight": 20,
        "stats": services,
        "msg": msg,
        "fix_action": "Añadir API key REAL (CapSolver $1=2k captchas) en pestaña CAPTCHA o aquí mismo",
        "fix_url": "/dashboard#bot/captcha",
    }


def _check_acpat() -> Dict[str, Any]:
    try:
        d = _safe_load("acpat_pool.json", default={"tokens": []})
        tokens = d.get("tokens", [])
        # Filtra expirados (>30min)
        import time
        now = time.time()
        valid = [t for t in tokens if (now - (t.get("captured_at_ts", now))) < 1800]
        ok = len(valid) >= 1
        return {
            "name": "AcpAT Pool (Queue-it)",
            "ok": ok,
            "weight": 10,
            "stats": {"total": len(tokens), "valid": len(valid)},
            "msg": (f"{len(valid)} tokens válidos en pool"
                    if ok else "Pool vacío — Queue-it bypass desactivado"),
            "fix_action": "Captura tokens AcpAT manualmente o lanza warming",
            "fix_url": "/dashboard#bot/bypass",
        }
    except Exception as e:
        return {"name": "AcpAT Pool (Queue-it)", "ok": False, "weight": 10,
                "msg": f"Error: {e}", "fix_action": "Revisar acpat_pool.json"}


def _check_notifications() -> Dict[str, Any]:
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
    return {
        "name": "Notificaciones",
        "ok": ok,
        "weight": 5,
        "stats": {"discord": discord, "telegram": telegram},
        "msg": (f"Configurado: {'Discord' if discord else ''}{' + ' if discord and telegram else ''}{'Telegram' if telegram else ''}"
                if ok else "Sin Discord ni Telegram — no recibirás avisos"),
        "fix_action": "Añadir webhook Discord o token Telegram",
        "fix_url": "/dashboard#notif",
    }


def _check_browser() -> Dict[str, Any]:
    try:
        import tm_browser_engine
        chk = tm_browser_engine.check_playwright()
        if chk.get("ok"):
            browsers = chk.get("browsers", {})
            available = [b for b, info in browsers.items() if info.get("available")]
            antidetect = [b for b, info in browsers.items() if info.get("antidetect")]
            return {
                "name": "Browser Engine",
                "ok": len(available) >= 1,
                "weight": 10,
                "stats": {"available": available, "antidetect": antidetect},
                "msg": f"{len(available)} navegadores · {len(antidetect)} con anti-detect",
                "fix_action": "Instalar Playwright: python -m playwright install",
            }
        return {"name": "Browser Engine", "ok": False, "weight": 10,
                "msg": "Playwright no instalado",
                "fix_action": "python -m playwright install chromium firefox"}
    except Exception as e:
        return {"name": "Browser Engine", "ok": False, "weight": 10,
                "msg": f"Error: {e}", "fix_action": "pip install playwright"}


def _check_critical_2026() -> Dict[str, Any]:
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
        "name": "Módulos críticos 2026",
        "ok": ok,
        "weight": 10,
        "stats": {"loaded": len(loaded), "expected": len(mods)},
        "msg": f"{len(loaded)}/{len(mods)} módulos cargados",
        "fix_action": "Reinstalar módulos críticos faltantes",
    }


def _check_csv_sync() -> Dict[str, Any]:
    try:
        import accounts_csv_sync
        st = accounts_csv_sync.get_status()
        ok = bool(st.get("watcher_running") and st.get("csv_exists"))
        return {
            "name": "CSV Sync (cuentas/tarjetas)",
            "ok": ok,
            "weight": 5,
            "stats": st,
            "msg": "Watcher activo · CSV sincronizado" if ok else "Watcher inactivo",
            "fix_action": "Reinicia dashboard para activar el watcher",
        }
    except Exception as e:
        return {"name": "CSV Sync", "ok": False, "weight": 5, "msg": f"Error: {e}"}


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
    """Fuerza recomputo del próximo check()."""
    with _check_cache_lock:
        _CHECK_CACHE["data"] = None
        _CHECK_CACHE["ts"] = 0


def _check_uncached() -> Dict[str, Any]:
    """Ejecuta todas las verificaciones y devuelve el checklist."""
    items = [c() for c in ALL_CHECKS]
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


def check(force: bool = False) -> Dict[str, Any]:
    """Versión cacheada del check (TTL 30s). Para forzar recomputo: force=True."""
    now = _time_mod.time()
    with _check_cache_lock:
        cached = _CHECK_CACHE.get("data")
        cached_ts = _CHECK_CACHE.get("ts", 0)
        ttl = _CHECK_CACHE.get("ttl_s", 30)
        if (not force) and cached and (now - cached_ts) < ttl:
            # Hit caché — añadir hint de que viene de cache
            return {**cached, "_cache_hit": True,
                    "_cache_age_s": round(now - cached_ts, 1)}

    # Miss caché → recomputar (puede tardar 1-2s)
    fresh = _check_uncached()
    with _check_cache_lock:
        _CHECK_CACHE["data"] = fresh
        _CHECK_CACHE["ts"] = now
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


def auto_setup() -> Dict[str, Any]:
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

    # 3. Re-check
    final = check()
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

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {
        "ok": passed == total,
        "total_checks": total,
        "passed_checks": passed,
        "checks": checks,
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
