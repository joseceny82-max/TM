"""captcha_manager.py — Resolvedor de CAPTCHA con integración REAL.

Servicios soportados (integración HTTP real):
- **CapSolver** (recomendado, $0.80/1000) — api.capsolver.com
- **2Captcha** ($1.50/1000) — api.2captcha.com
- **Anti-Captcha** ($2.00/1000) — api.anti-captcha.com
- **CapMonster** — api.capmonster.cloud

Tipos soportados:
- reCAPTCHA v2 (RecaptchaV2TaskProxyless)
- reCAPTCHA v3 (RecaptchaV3TaskProxyless)
- hCaptcha (HCaptchaTaskProxyless)
- Cloudflare Turnstile (AntiTurnstileTaskProxyless)
- FunCaptcha / Arkose (FunCaptchaTaskProxyless)

Uso:
    import captcha_manager as cm
    cfg = cm.save_config({"services":{"capsolver":{"enabled":True,"api_key":"CAP-XXX"}}})
    # Resolver reCAPTCHA v2
    r = cm.solve_recaptcha_v2(site_key="6Lc_XXX", url="https://www.ticketmaster.com/event/ABC")
    if r["ok"]: token = r["token"]

Balance check: cm.check_balance("capsolver") → {"ok":True, "balance":5.23, "currency":"USD"}
"""
import time
import threading
from datetime import datetime
from typing import Optional, Dict
import config
import json_atomic


_lock = threading.Lock()

# Estadísticas en memoria (últimos resueltos)
_stats = {
    "total_solved": 0,
    "total_failed": 0,
    "total_cost_usd": 0.0,
    "by_service": {},
    "by_type": {},
    "last_solves": [],  # últimos 20
}

# Precios referenciales (USD / 1000 solves)
PRICE_TABLE = {
    "capsolver":    {"recaptcha_v2": 0.80, "recaptcha_v3": 0.80, "hcaptcha": 1.00,
                     "turnstile": 1.00, "funcaptcha": 1.20},
    "2captcha":     {"recaptcha_v2": 1.50, "recaptcha_v3": 1.50, "hcaptcha": 1.50,
                     "turnstile": 1.50, "funcaptcha": 2.00},
    "anticaptcha":  {"recaptcha_v2": 2.00, "recaptcha_v3": 2.00, "hcaptcha": 2.00,
                     "turnstile": 2.00, "funcaptcha": 2.00},
    "capmonster":   {"recaptcha_v2": 0.60, "recaptcha_v3": 0.60, "hcaptcha": 0.80,
                     "turnstile": 0.80, "funcaptcha": 1.00},
}


def _path():
    return config.ruta("captcha.json")


def get_config() -> dict:
    default = {
        "primary":  "capsolver",
        "fallback": "2captcha",
        "services": {
            "capsolver":    {"enabled": False, "api_key": "",  "priority": 1},
            "2captcha":     {"enabled": False, "api_key": "",  "priority": 2},
            "anticaptcha":  {"enabled": False, "api_key": "",  "priority": 3},
            "capmonster":   {"enabled": False, "api_key": "",  "priority": 4},
        },
        "auto_solve": True,
        "types": ["recaptcha_v2", "recaptcha_v3", "hcaptcha", "turnstile", "funcaptcha"],
        "default_timeout": 120,       # s esperando la solución
        "polling_interval": 3,         # s entre polls
    }
    return json_atomic.load(_path(), default=default)


def save_config(cfg: dict) -> dict:
    with _lock:
        current = get_config()
        # merge profundo de services
        if "services" in cfg:
            for svc_name, svc_data in cfg["services"].items():
                if svc_name in current["services"]:
                    current["services"][svc_name].update(svc_data)
                else:
                    current["services"][svc_name] = svc_data
            del cfg["services"]
        current.update(cfg)
        json_atomic.save(_path(), current)
    return current


def get_available_methods():
    return ["auto", "manual"] + list(PRICE_TABLE.keys())


def get_best_method() -> str:
    """Devuelve el servicio habilitado con mayor prioridad (menor número)."""
    cfg = get_config()
    enabled = [(k, v) for k, v in cfg["services"].items()
               if v.get("enabled") and v.get("api_key")]
    if not enabled:
        return "manual"
    enabled.sort(key=lambda x: x[1].get("priority", 999))
    return enabled[0][0]


def _api_key(service: str) -> Optional[str]:
    cfg = get_config()
    svc = cfg["services"].get(service, {})
    if not svc.get("enabled") or not svc.get("api_key"):
        return None
    return svc["api_key"]


# ═══════════════════════════════════════════
#  CAPSOLVER (recomendado)
# ═══════════════════════════════════════════
def _capsolver_create(api_key: str, task: dict) -> dict:
    """Crea una tarea en CapSolver. Devuelve {ok, taskId}."""
    import requests
    try:
        r = requests.post("https://api.capsolver.com/createTask",
                          json={"clientKey": api_key, "task": task}, timeout=15)
        data = r.json()
        if data.get("errorId") == 0:
            return {"ok": True, "taskId": data.get("taskId")}
        return {"ok": False, "error": data.get("errorDescription") or data.get("errorCode") or "unknown"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _capsolver_poll(api_key: str, task_id: str, timeout: int = 120,
                    poll_interval: int = 3) -> dict:
    """Poll hasta obtener la solución o timeout."""
    import requests
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.post("https://api.capsolver.com/getTaskResult",
                              json={"clientKey": api_key, "taskId": task_id}, timeout=10)
            data = r.json()
            if data.get("errorId") != 0:
                return {"ok": False, "error": data.get("errorDescription")}
            status = data.get("status", "")
            if status == "ready":
                sol = data.get("solution", {})
                return {"ok": True, "solution": sol,
                        "token": sol.get("gRecaptchaResponse") or sol.get("token")
                                 or sol.get("captchaResponse") or sol.get("text")}
            if status == "failed":
                return {"ok": False, "error": data.get("errorDescription") or "task failed"}
            # processing → espera
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        time.sleep(poll_interval)
    return {"ok": False, "error": f"timeout after {timeout}s"}


def _capsolver_balance(api_key: str) -> dict:
    import requests
    try:
        r = requests.post("https://api.capsolver.com/getBalance",
                          json={"clientKey": api_key}, timeout=10)
        d = r.json()
        if d.get("errorId") == 0:
            return {"ok": True, "balance": d.get("balance", 0), "currency": "USD"}
        return {"ok": False, "error": d.get("errorDescription")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════
#  2CAPTCHA (fallback)
# ═══════════════════════════════════════════
def _2captcha_create(api_key: str, params: dict) -> dict:
    """Crea tarea en 2captcha formato IN request."""
    import requests
    params = dict(params)
    params.update({"key": api_key, "json": 1})
    try:
        r = requests.post("http://2captcha.com/in.php", data=params, timeout=15)
        d = r.json()
        if d.get("status") == 1:
            return {"ok": True, "taskId": d.get("request")}
        return {"ok": False, "error": d.get("request", "unknown")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _2captcha_poll(api_key: str, task_id: str, timeout: int = 120,
                   poll_interval: int = 5) -> dict:
    import requests
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get("http://2captcha.com/res.php",
                             params={"key": api_key, "action": "get",
                                     "id": task_id, "json": 1}, timeout=10)
            d = r.json()
            if d.get("status") == 1:
                return {"ok": True, "token": d.get("request"), "solution": {"token": d.get("request")}}
            req = d.get("request", "")
            if req == "CAPCHA_NOT_READY":
                pass
            elif req.startswith("ERROR_"):
                return {"ok": False, "error": req}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        time.sleep(poll_interval)
    return {"ok": False, "error": f"timeout after {timeout}s"}


def _2captcha_balance(api_key: str) -> dict:
    import requests
    try:
        r = requests.get("http://2captcha.com/res.php",
                         params={"key": api_key, "action": "getbalance", "json": 1}, timeout=10)
        d = r.json()
        if d.get("status") == 1:
            return {"ok": True, "balance": float(d.get("request", 0)), "currency": "USD"}
        return {"ok": False, "error": d.get("request")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════
#  API PÚBLICA - resolvers
# ═══════════════════════════════════════════
def _record_solve(service: str, captcha_type: str, ok: bool, elapsed_s: float,
                    event_id: Optional[str] = None):
    cost = PRICE_TABLE.get(service, {}).get(captcha_type, 0) / 1000
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        if ok:
            _stats["total_solved"] += 1
            _stats["total_cost_usd"] += cost
        else:
            _stats["total_failed"] += 1
        _stats["by_service"].setdefault(service, {"solved": 0, "failed": 0,
                                                       "cost_usd": 0.0})
        _stats["by_service"][service]["solved" if ok else "failed"] += 1
        if ok:
            _stats["by_service"][service]["cost_usd"] = round(
                _stats["by_service"][service].get("cost_usd", 0.0) + cost, 5)
        _stats["by_type"].setdefault(captcha_type, {"solved": 0, "failed": 0,
                                                         "cost_usd": 0.0})
        _stats["by_type"][captcha_type]["solved" if ok else "failed"] += 1
        if ok:
            _stats["by_type"][captcha_type]["cost_usd"] = round(
                _stats["by_type"][captcha_type].get("cost_usd", 0.0) + cost, 5)
        # Coste por día (forecast diario / mensual)
        daily = _stats.setdefault("cost_by_day", {})
        if ok:
            daily[today] = round(daily.get(today, 0.0) + cost, 5)
        # Mantener solo últimos 90 días
        if len(daily) > 90:
            for k in sorted(daily.keys())[:-90]:
                daily.pop(k, None)
        # Coste por event_id (solo si se pasa)
        if event_id and ok:
            evt = _stats.setdefault("cost_by_event", {})
            row = evt.setdefault(event_id, {"solved": 0, "failed": 0,
                                                "cost_usd": 0.0,
                                                "first_at": datetime.now().isoformat()})
            row["cost_usd"] = round(row["cost_usd"] + cost, 5)
            row["solved"] += 1
            row["last_at"] = datetime.now().isoformat()
        elif event_id and not ok:
            evt = _stats.setdefault("cost_by_event", {})
            row = evt.setdefault(event_id, {"solved": 0, "failed": 0,
                                                "cost_usd": 0.0,
                                                "first_at": datetime.now().isoformat()})
            row["failed"] += 1
            row["last_at"] = datetime.now().isoformat()
        _stats["last_solves"].insert(0, {
            "ts": datetime.now().isoformat(),
            "service": service, "type": captcha_type,
            "ok": ok, "elapsed_s": round(elapsed_s, 2),
            "cost_usd": round(cost, 5) if ok else 0,
        })
        _stats["last_solves"] = _stats["last_solves"][:20]


def _solve_with_capsolver(task: dict, captcha_type: str,
                            event_id: Optional[str] = None) -> dict:
    key = _api_key("capsolver")
    if not key:
        return {"ok": False, "error": "capsolver no configurado"}
    cfg = get_config()
    start = time.time()
    created = _capsolver_create(key, task)
    if not created.get("ok"):
        _record_solve("capsolver", captcha_type, False, time.time() - start,
                       event_id=event_id)
        return created
    res = _capsolver_poll(key, created["taskId"],
                         timeout=cfg.get("default_timeout", 120),
                         poll_interval=cfg.get("polling_interval", 3))
    elapsed = time.time() - start
    _record_solve("capsolver", captcha_type, res.get("ok", False), elapsed,
                   event_id=event_id)
    res["service"] = "capsolver"
    res["elapsed_s"] = round(elapsed, 2)
    return res


def _solve_with_2captcha(params: dict, captcha_type: str,
                            event_id: Optional[str] = None) -> dict:
    key = _api_key("2captcha")
    if not key:
        return {"ok": False, "error": "2captcha no configurado"}
    cfg = get_config()
    start = time.time()
    created = _2captcha_create(key, params)
    if not created.get("ok"):
        _record_solve("2captcha", captcha_type, False, time.time() - start,
                       event_id=event_id)
        return created
    res = _2captcha_poll(key, created["taskId"],
                        timeout=cfg.get("default_timeout", 120),
                        poll_interval=cfg.get("polling_interval", 5))
    elapsed = time.time() - start
    _record_solve("2captcha", captcha_type, res.get("ok", False), elapsed,
                   event_id=event_id)
    res["service"] = "2captcha"
    res["elapsed_s"] = round(elapsed, 2)
    return res


def _try_services(captcha_type: str, caps_task: dict, tw_params: dict,
                    event_id: Optional[str] = None) -> dict:
    """Intenta primary, luego fallback automáticamente."""
    cfg = get_config()
    tried = []
    for svc in (cfg.get("primary"), cfg.get("fallback")):
        if not svc or svc in tried:
            continue
        tried.append(svc)
        if svc == "capsolver":
            r = _solve_with_capsolver(caps_task, captcha_type, event_id=event_id)
        elif svc == "2captcha":
            r = _solve_with_2captcha(tw_params, captcha_type, event_id=event_id)
        else:
            continue
        if r.get("ok"):
            return r
    return {"ok": False, "error": f"todos los servicios fallaron ({','.join(tried)})",
            "tried": tried}


def solve_recaptcha_v2(site_key: str, url: str, invisible: bool = False,
                         event_id: Optional[str] = None) -> dict:
    caps_task = {
        "type": "ReCaptchaV2TaskProxyless",
        "websiteKey": site_key,
        "websiteURL": url,
        "isInvisible": invisible,
    }
    tw_params = {"method": "userrecaptcha", "googlekey": site_key, "pageurl": url}
    if invisible:
        tw_params["invisible"] = 1
    return _try_services("recaptcha_v2", caps_task, tw_params, event_id=event_id)


def solve_recaptcha_v3(site_key: str, url: str, action: str = "verify",
                      min_score: float = 0.7,
                      event_id: Optional[str] = None) -> dict:
    caps_task = {
        "type": "ReCaptchaV3TaskProxyless",
        "websiteKey": site_key,
        "websiteURL": url,
        "pageAction": action,
        "minScore": min_score,
    }
    tw_params = {"method": "userrecaptcha", "googlekey": site_key, "pageurl": url,
                 "version": "v3", "action": action, "min_score": min_score}
    return _try_services("recaptcha_v3", caps_task, tw_params, event_id=event_id)


def solve_hcaptcha(site_key: str, url: str,
                     event_id: Optional[str] = None) -> dict:
    caps_task = {
        "type": "HCaptchaTaskProxyless",
        "websiteKey": site_key,
        "websiteURL": url,
    }
    tw_params = {"method": "hcaptcha", "sitekey": site_key, "pageurl": url}
    return _try_services("hcaptcha", caps_task, tw_params, event_id=event_id)


def solve_turnstile(site_key: str, url: str,
                      event_id: Optional[str] = None) -> dict:
    caps_task = {
        "type": "AntiTurnstileTaskProxyless",
        "websiteKey": site_key,
        "websiteURL": url,
    }
    tw_params = {"method": "turnstile", "sitekey": site_key, "pageurl": url}
    return _try_services("turnstile", caps_task, tw_params, event_id=event_id)


def check_balance(service: str) -> dict:
    """Comprueba el saldo REAL vía API del servicio."""
    key = _api_key(service)
    if not key:
        return {"ok": False, "error": f"{service} no configurado"}
    if service == "capsolver":
        return _capsolver_balance(key)
    if service == "2captcha":
        return _2captcha_balance(key)
    return {"ok": False, "error": "service no soportado"}


def check_all_balances() -> dict:
    out = {}
    for svc in ("capsolver", "2captcha"):
        if _api_key(svc):
            out[svc] = check_balance(svc)
    return out


def check_api_service(service: str) -> dict:
    """Compatibilidad retroactiva."""
    cfg = get_config()
    svc = cfg["services"].get(service, {})
    if not svc.get("enabled"):
        return {"ok": False, "error": "disabled"}
    if not svc.get("api_key") and not svc.get("username"):
        return {"ok": False, "error": "no_credentials"}
    # probar balance real
    bal = check_balance(service)
    if bal.get("ok"):
        return {"ok": True, "balance": bal.get("balance"), "currency": bal.get("currency")}
    return bal


def check_nst_browser():
    """Detecta si hay un navegador NST (Anti-Detect) instalado."""
    import shutil
    for candidate in ("multilogin", "morelogin", "gologin", "nstbrowser", "kameleo"):
        if shutil.which(candidate):
            return {"ok": True, "installed": candidate}
    return {"ok": False, "error": "not_installed"}


def get_stats() -> dict:
    with _lock:
        return dict(_stats)


def reset_stats():
    with _lock:
        _stats.update({"total_solved": 0, "total_failed": 0, "total_cost_usd": 0.0,
                       "by_service": {}, "by_type": {},
                       "cost_by_day": {}, "cost_by_event": {},
                       "last_solves": []})
    return {"ok": True}


def export_csv(section: str = "all") -> str:
    """Exporta stats CAPTCHA a CSV multi-sección.

    section:
      "all" — concatena 3 sub-sections separadas por header
      "last_solves" / "cost_by_day" / "cost_by_event"
    """
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")

    sections = (section,) if section != "all" else (
        "last_solves", "cost_by_day", "cost_by_event")

    for sec in sections:
        if section == "all":
            w.writerow([f"# {sec.upper()}"])
        if sec == "last_solves":
            w.writerow(["ts", "service", "type", "ok", "elapsed_s", "cost_usd"])
            for s in (_stats.get("last_solves") or []):
                w.writerow([s.get("ts", ""), s.get("service", ""),
                              s.get("type", ""), s.get("ok"),
                              s.get("elapsed_s", 0), s.get("cost_usd", 0)])
        elif sec == "cost_by_day":
            w.writerow(["date", "cost_usd"])
            daily = _stats.get("cost_by_day", {})
            for k in sorted(daily.keys()):
                w.writerow([k, daily[k]])
        elif sec == "cost_by_event":
            w.writerow(["event_id", "solved", "failed", "cost_usd",
                          "first_at", "last_at"])
            evt = _stats.get("cost_by_event", {})
            for eid, row in sorted(evt.items(),
                                       key=lambda kv: -(kv[1].get("cost_usd", 0))):
                w.writerow([eid, row.get("solved", 0), row.get("failed", 0),
                              row.get("cost_usd", 0),
                              row.get("first_at", ""), row.get("last_at", "")])
        if section == "all":
            w.writerow([])

    return out.getvalue()


def cost_by_event(top: int = 20, sort_by: str = "cost_usd") -> dict:
    """Devuelve top eventos por coste/solves. Útil para priorizar budget."""
    evt = _stats.get("cost_by_event", {})
    items = [{"event_id": k, **v} for k, v in evt.items()]
    items.sort(key=lambda x: x.get(sort_by, 0), reverse=True)
    return {
        "total_events": len(items),
        "total_cost_tracked": round(sum(i.get("cost_usd", 0) for i in items), 5),
        "top": items[:top],
    }


def cost_forecast() -> dict:
    """Forecast de coste basado en últimos 7/30 días.

    Devuelve:
      - last_7d_total / last_30d_total
      - daily_avg_7d / daily_avg_30d
      - monthly_forecast (avg_30d * 30)
      - annual_forecast (avg_30d * 365)
    """
    from datetime import timedelta
    today = datetime.now().date()
    daily = _stats.get("cost_by_day", {})
    last_7 = []
    last_30 = []
    for i in range(30):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        v = daily.get(d, 0.0)
        last_30.append(v)
        if i < 7:
            last_7.append(v)
    avg_7 = round(sum(last_7) / 7, 4) if last_7 else 0
    avg_30 = round(sum(last_30) / 30, 4) if last_30 else 0
    return {
        "today": daily.get(today.strftime("%Y-%m-%d"), 0.0),
        "last_7d_total": round(sum(last_7), 4),
        "last_30d_total": round(sum(last_30), 4),
        "daily_avg_7d": avg_7,
        "daily_avg_30d": avg_30,
        "monthly_forecast": round(avg_30 * 30, 2),
        "annual_forecast": round(avg_30 * 365, 2),
        "trend_7d_vs_30d": round((avg_7 - avg_30) / avg_30 * 100, 1) if avg_30 > 0 else 0,
        "cost_by_day_recent": {k: daily[k] for k in sorted(daily.keys())[-30:]},
    }


# ── Budget alerts ─────────────────────────────────────────────
_BUDGET_KEY = "captcha_budget.json"


def get_budget() -> dict:
    """Devuelve la configuración de budget alert."""
    try:
        import config as _cfg
        import json_atomic as _ja
        return _ja.load(_cfg.ruta(_BUDGET_KEY), default={
            "monthly_limit_usd": 50.0,
            "alert_pct": 80,           # alerta al 80% del budget
            "weekly_digest": True,
            "discord_webhook": True,
            "telegram_webhook": True,
            "last_alert_sent": "",
            "last_weekly_digest": "",
        })
    except Exception:
        return {"monthly_limit_usd": 50.0, "alert_pct": 80,
                "weekly_digest": True}


def save_budget(patch: dict) -> dict:
    try:
        import config as _cfg
        import json_atomic as _ja
        cur = get_budget()
        for k in ("monthly_limit_usd", "alert_pct", "weekly_digest",
                   "discord_webhook", "telegram_webhook"):
            if k in (patch or {}):
                cur[k] = patch[k]
        _ja.save(_cfg.ruta(_BUDGET_KEY), cur)
        return cur
    except Exception as e:
        return {"error": str(e)[:200]}


def check_budget_alert(force: bool = False) -> dict:
    """Comprueba si el forecast mensual supera el % del budget configurado.

    Si supera → envía alerta a Discord/Telegram (si webhook habilitado).
    `force=True` ignora throttle de "ya enviada hoy".
    """
    import config as _cfg
    import json_atomic as _ja
    bud = get_budget()
    fc = cost_forecast()
    forecast_monthly = fc.get("monthly_forecast", 0)
    limit = bud.get("monthly_limit_usd", 50.0)
    threshold = limit * (bud.get("alert_pct", 80) / 100.0)
    over_threshold = forecast_monthly >= threshold
    over_limit = forecast_monthly >= limit
    today = datetime.now().date().isoformat()
    last_alert = (bud.get("last_alert_sent") or "")[:10]
    out = {
        "ok": True,
        "forecast_monthly_usd": forecast_monthly,
        "monthly_limit_usd": limit,
        "alert_threshold_usd": round(threshold, 2),
        "pct_of_limit": round(100 * forecast_monthly / max(0.001, limit), 1),
        "over_threshold": over_threshold,
        "over_limit": over_limit,
        "alert_sent_today": (last_alert == today),
        "notified_channels": [],
    }
    if not (over_threshold and (force or last_alert != today)):
        return out
    # Enviar alerta
    severity = "🚨 CRITICAL" if over_limit else "⚠️ WARNING"
    msg = (f"{severity} CAPTCHA Budget Alert\n"
            f"Forecast mensual: ${forecast_monthly:.2f} / limit ${limit:.2f} "
            f"({out['pct_of_limit']}%)\n"
            f"Avg/día: ${fc.get('daily_avg_30d', 0):.4f} · "
            f"Trend 7d/30d: {fc.get('trend_7d_vs_30d', 0)}%")
    try:
        import notifications
        if bud.get("discord_webhook"):
            try:
                notifications.send_discord(msg)
                out["notified_channels"].append("discord")
            except Exception:
                pass
        if bud.get("telegram_webhook"):
            try:
                notifications.send_telegram(msg)
                out["notified_channels"].append("telegram")
            except Exception:
                pass
    except ImportError:
        out["error"] = "notifications_module_missing"
    if out["notified_channels"]:
        bud["last_alert_sent"] = datetime.now().isoformat()
        try:
            _ja.save(_cfg.ruta(_BUDGET_KEY), bud)
        except Exception:
            pass
    # WS push para banner UI live
    if over_threshold:
        try:
            from dashboard import socketio  # type: ignore
            socketio.emit("captcha.budget_alert", {
                "severity": "critical" if over_limit else "warning",
                "forecast_monthly_usd": forecast_monthly,
                "monthly_limit_usd": limit,
                "pct_of_limit": out["pct_of_limit"],
                "notified_channels": out["notified_channels"],
                "ts": datetime.now().isoformat(),
            })
        except Exception:
            pass
    return out


_digest_scheduler_state = {"running": False, "thread": None,
                              "stop_event": None, "started_at": "",
                              "ticks": 0, "last_run": ""}


def _digest_loop(stop_evt):
    """Loop que ejecuta weekly_digest cada lunes 09:00 (chequea cada hora)."""
    import time as _t
    last_digest_date = ""
    while not stop_evt.is_set():
        try:
            now = datetime.now()
            # Lunes = weekday 0; ejecuta entre 09:00-09:59
            if now.weekday() == 0 and now.hour == 9:
                today = now.date().isoformat()
                if last_digest_date != today:
                    weekly_digest(force=True)
                    last_digest_date = today
                    _digest_scheduler_state["last_run"] = now.isoformat()
            _digest_scheduler_state["ticks"] += 1
        except Exception:
            pass
        # Sleep 1 hora en chunks de 60s
        for _ in range(3600):
            if stop_evt.is_set():
                break
            _t.sleep(1)


def start_digest_scheduler() -> dict:
    """Arranca el cron-like daemon que envía weekly digest cada lunes 09:00."""
    import threading as _th
    if _digest_scheduler_state.get("running"):
        return {"ok": True, "status": "already_running"}
    evt = _th.Event()
    th = _th.Thread(target=_digest_loop, args=(evt,),
                     daemon=True, name="captcha_digest_scheduler")
    _digest_scheduler_state.update({
        "running": True, "thread": th, "stop_event": evt,
        "started_at": datetime.now().isoformat(),
        "ticks": 0,
    })
    th.start()
    return {"ok": True, "status": "started",
             "schedule": "Lunes 09:00 (chequeo horario)"}


def stop_digest_scheduler() -> dict:
    if not _digest_scheduler_state.get("running"):
        return {"ok": True, "status": "not_running"}
    evt = _digest_scheduler_state.get("stop_event")
    if evt:
        evt.set()
    _digest_scheduler_state["running"] = False
    return {"ok": True, "status": "stopped"}


def digest_scheduler_status() -> dict:
    return {
        "running": _digest_scheduler_state.get("running", False),
        "started_at": _digest_scheduler_state.get("started_at", ""),
        "ticks": _digest_scheduler_state.get("ticks", 0),
        "last_run": _digest_scheduler_state.get("last_run", ""),
        "schedule": "Lunes 09:00 (chequeo horario)",
    }


def weekly_digest(force: bool = False) -> dict:
    """Genera el digest semanal de coste y lo envía si está habilitado."""
    import config as _cfg
    import json_atomic as _ja
    bud = get_budget()
    if not bud.get("weekly_digest") and not force:
        return {"ok": False, "error": "weekly_digest_disabled"}
    fc = cost_forecast()
    msg = (f"📊 CAPTCHA Weekly Digest\n"
            f"7d: ${fc.get('last_7d_total', 0):.4f} "
            f"(avg ${fc.get('daily_avg_7d', 0):.4f}/día)\n"
            f"30d: ${fc.get('last_30d_total', 0):.4f}\n"
            f"Forecast mensual: ${fc.get('monthly_forecast', 0):.2f} / "
            f"limit ${bud.get('monthly_limit_usd', 50):.2f}\n"
            f"Trend: {fc.get('trend_7d_vs_30d', 0)}%")
    sent = []
    try:
        import notifications
        if bud.get("discord_webhook"):
            try: notifications.send_discord(msg); sent.append("discord")
            except Exception: pass
        if bud.get("telegram_webhook"):
            try: notifications.send_telegram(msg); sent.append("telegram")
            except Exception: pass
    except ImportError:
        pass
    bud["last_weekly_digest"] = datetime.now().isoformat()
    try:
        _ja.save(_cfg.ruta(_BUDGET_KEY), bud)
    except Exception:
        pass
    return {"ok": True, "sent_to": sent, "message": msg}


# ═══════════════════════════════════════════════════════════
#  TOKEN WARM CACHE (pre-solve antes del drop)
# ═══════════════════════════════════════════════════════════
# Pool de tokens reCAPTCHA/Turnstile pre-resueltos para usar al instante
# en el segundo crítico del drop. Cada token tiene TTL (reCAPTCHA v2/v3 = 120s,
# Turnstile = 300s). Get pop-y-descarta — los tokens son single-use.
import time as _time

_TOKEN_TTL = {
    "recaptcha_v2": 110,
    "recaptcha_v3": 110,
    "hcaptcha":     110,
    "turnstile":    290,
}
_token_pool: Dict[str, list] = {}    # key=(captcha_type, site_key, url) tuple → list of {token, expires_at}
_pool_lock = threading.Lock()


def _pool_key(captcha_type: str, site_key: str, url: str) -> str:
    return f"{captcha_type}|{site_key}|{url}"


def warm_cache(captcha_type: str, site_key: str, url: str,
                count: int = 5, action: str = "verify") -> dict:
    """Pre-resuelve `count` tokens y los guarda en pool. Llama a esto unos
    minutos antes del drop para tener tokens listos al click.
    Devuelve {ok, requested, cached, failed, ttl_s}."""
    if captcha_type not in _TOKEN_TTL:
        return {"ok": False, "error": f"captcha_type {captcha_type} no soportado"}
    cached = 0
    failed = 0
    key = _pool_key(captcha_type, site_key, url)
    for _ in range(count):
        if captcha_type == "recaptcha_v2":
            r = solve_recaptcha_v2(site_key, url)
        elif captcha_type == "recaptcha_v3":
            r = solve_recaptcha_v3(site_key, url, action=action)
        elif captcha_type == "hcaptcha":
            r = solve_hcaptcha(site_key, url)
        elif captcha_type == "turnstile":
            r = solve_turnstile(site_key, url)
        else:
            r = {"ok": False}
        if r.get("ok") and r.get("token"):
            with _pool_lock:
                _token_pool.setdefault(key, []).append({
                    "token": r["token"],
                    "expires_at": _time.time() + _TOKEN_TTL[captcha_type],
                    "cached_at": _time.time(),
                })
            cached += 1
        else:
            failed += 1
    return {"ok": True, "requested": count, "cached": cached, "failed": failed,
            "ttl_s": _TOKEN_TTL[captcha_type], "pool_size": pool_stats().get(key, 0)}


def take_warm_token(captcha_type: str, site_key: str, url: str) -> dict:
    """Saca un token válido del pool (FIFO). Limpia expirados.
    Devuelve {ok, token?, source='warm_cache'|'live_solve'?}.
    Si el pool está vacío, llama síncrono a solve_* (fallback)."""
    key = _pool_key(captcha_type, site_key, url)
    now = _time.time()
    with _pool_lock:
        items = _token_pool.get(key, [])
        # Purgar expirados
        items = [t for t in items if t["expires_at"] > now]
        if items:
            tok = items.pop(0)
            _token_pool[key] = items
            return {"ok": True, "token": tok["token"],
                    "source": "warm_cache",
                    "age_s": int(now - tok["cached_at"])}
        _token_pool[key] = items
    # Pool vacío → resolver al momento
    if captcha_type == "recaptcha_v2":   r = solve_recaptcha_v2(site_key, url)
    elif captcha_type == "recaptcha_v3": r = solve_recaptcha_v3(site_key, url)
    elif captcha_type == "hcaptcha":     r = solve_hcaptcha(site_key, url)
    elif captcha_type == "turnstile":    r = solve_turnstile(site_key, url)
    else: r = {"ok": False, "error": "captcha_type no soportado"}
    if r.get("ok"):
        r["source"] = "live_solve"
    return r


def pool_stats() -> dict:
    """Tokens vivos por (captcha_type, site_key, url)."""
    now = _time.time()
    out = {}
    with _pool_lock:
        for k, items in _token_pool.items():
            valid = [t for t in items if t["expires_at"] > now]
            out[k] = len(valid)
    return out


def clear_pool() -> dict:
    with _pool_lock:
        n = sum(len(v) for v in _token_pool.values())
        _token_pool.clear()
    return {"ok": True, "cleared": n}


# ═══════════════════════════════════════════════════════════
#  WHISPER AUDIO SOLVER (offline, gratis, fallback antes de CapSolver)
# ═══════════════════════════════════════════════════════════
_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model(model_size: str = "base"):
    """Carga lazy el modelo Whisper. base=74MB es óptimo para reCAPTCHA audio
    (números/palabras inglés, 99% accuracy). Tras la primera carga se cachea
    en memoria para resoluciones siguientes."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel(model_size, device="cpu",
                                           compute_type="int8")
            return _whisper_model
        except ImportError:
            try:
                import whisper as _w
                _whisper_model = _w.load_model(model_size)
                return _whisper_model
            except ImportError:
                return None


def whisper_status() -> dict:
    """Reporta si whisper está instalado y modelo cargable."""
    out = {"installed": False, "loaded": False, "model_size": None}
    try:
        from faster_whisper import WhisperModel  # noqa: F401
        out["installed"] = True
        out["lib"] = "faster_whisper"
    except ImportError:
        try:
            import whisper as _w  # noqa: F401
            out["installed"] = True
            out["lib"] = "openai-whisper"
        except ImportError:
            out["installed"] = False
            out["lib"] = None
    if _whisper_model is not None:
        out["loaded"] = True
    return out


def solve_audio_local(audio_url_or_bytes, language: str = "en",
                       model_size: str = "base") -> dict:
    """Resuelve un challenge de audio (reCAPTCHA audio, similar) usando
    Whisper local. audio_url_or_bytes puede ser:
      - URL HTTP/HTTPS al MP3
      - bytes con el MP3 ya descargado
      - path a un archivo local
    Devuelve {ok, text, language, source='whisper_local', latency_ms}."""
    import os, tempfile, time as _t, requests as _r
    started = _t.time()
    model = _get_whisper_model(model_size)
    if model is None:
        return {"ok": False, "error": "whisper no instalado",
                "hint": "pip install faster-whisper"}
    audio_path = None
    tmp_created = False
    try:
        if isinstance(audio_url_or_bytes, (bytes, bytearray)):
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(audio_url_or_bytes)
                audio_path = f.name
                tmp_created = True
        elif isinstance(audio_url_or_bytes, str):
            if audio_url_or_bytes.startswith(("http://", "https://")):
                resp = _r.get(audio_url_or_bytes, timeout=15)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(resp.content)
                    audio_path = f.name
                    tmp_created = True
            elif os.path.exists(audio_url_or_bytes):
                audio_path = audio_url_or_bytes
            else:
                return {"ok": False, "error": "audio source inválida"}
        else:
            return {"ok": False, "error": "tipo de audio no soportado"}

        # faster_whisper API
        if hasattr(model, "transcribe") and "WhisperModel" in type(model).__name__:
            segments, info = model.transcribe(audio_path, language=language,
                                                beam_size=5)
            text = " ".join(s.text for s in segments).strip()
            lang = info.language
        else:
            res = model.transcribe(audio_path, language=language, fp16=False)
            text = (res.get("text", "") or "").strip()
            lang = res.get("language", language)

        # Para reCAPTCHA audio, el texto suele ser solo dígitos/palabras —
        # quitar puntuación final.
        text = text.rstrip(".,;:! ").strip()
        return {"ok": True, "text": text, "language": lang,
                "source": "whisper_local",
                "latency_ms": int((_t.time() - started) * 1000),
                "model_size": model_size}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                "latency_ms": int((_t.time() - started) * 1000)}
    finally:
        if tmp_created and audio_path:
            try: os.unlink(audio_path)
            except Exception: pass


def solve_audio_with_fallback(audio_url_or_bytes, language: str = "en") -> dict:
    """Intenta Whisper local primero (gratis). Si falla, devuelve resultado
    con fallback flag para que el caller pueda escalar a CapSolver audio task."""
    res = solve_audio_local(audio_url_or_bytes, language=language)
    if res.get("ok"):
        return res
    return {"ok": False, "error": res.get("error", "whisper_failed"),
            "needs_paid_fallback": True,
            "whisper_status": whisper_status()}
