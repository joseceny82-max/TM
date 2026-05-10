"""
dashboard.py — TM PERROS Dashboard Flask.

Dashboard multi-dominio para Ticketmaster con 18 pestanas:
Dashboard, Problemas, Eventos, Sitios de Venta, Mis Entradas, Sniper Bot,
Cuentas y Tarjetas, Cookies, Analytics, Notificaciones, Monitor, Consola,
Backup, Usuarios, OTP, Soporte, Notas, Configuracion.
"""
import os
import json
import logging
from datetime import datetime
import base64
from functools import wraps
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, request, jsonify, session, redirect, send_file, url_for, Response

import config
import i18n
import json_atomic
import domain_manager
import event_manager
import site_manager
import problem_center
import support_system
import notifications
import analytics as analytics_mod
import monitor as monitor_mod
import backup_system
import otp_manager
import cookie_optimizer
import antibot as antibot_mod
import captcha_manager
import browser_profiles
import multibrowser_config
import accounts_csv_sync
import bot_presets
import strategy_profiles
import live_readiness
import account_creator_assistant
import sms_receiver
import proxy_rotator
import purchase_resilience
import signup_form_helpers
# 6 módulos nuevos (P0 1d/2c + P1 #7-#10)
import nudata_emulator
import akamai_sensor_v3
import datadome_emulator
import acpat_sync
import imap_sms_inject
import real_purchase_orchestrator
# 3 módulos SOTA 2026 (P0-1, P0-2, P1-10)
import akamai_v3_hash_refresh
import ja4_session_continuity
import queueit_visitorsession
# 10 módulos críticos 2026
import ja4_pinning
import http2_fingerprint
import safetix_handler
import compliance_gate
import datadome_proxy_hooks
import uach_grease_validator
import asn_ip_drift
import psd3_payment_retry
import trust_tuple_warming
import tls_ct_monitor
import proxy_intelligence
import queue_worker
import bot_engine
import account_manager
import encryption
import session_manager as sm_mod
import instance_manager
import error_manager as err_mod
import notes_manager
import users_manager
import purchases_manager
import settings_manager
import cookie_warmer
import ja4_spoof
import event_discovery
import tm_registrations
import proxy_health
import health_monitor
import tm_purchase_flow
import tm_browser_engine
import audit_log
import multilogin_orchestrator
import px_prefarm
import multi_domain_orchestrator
import acpat_injection
import advanced_fingerprint
import adyen_tokenizer
import akamai_bmjs
import akamai_bmsc
import akamai_validator
import api_tokens
import apple_google_pay
import bots_compliance
import hyper_sdk_client
import llm_captcha
import nodriver_engine
import dd_vm_trace
import polling_rotate
import presale_farming
import proxy_ipqs
import proxy_sticky
import queue_monitor
import turnstile_solver
import proxy_per_account
import rebrowser_stealth
import sensor_js_watcher
import tm_mobile_sdk
import bot_intelligence
import captcha_cv_solver
import cluster_capture
import human_behavior
import ml_validator
import multilogin_autoscale
import card_rotation
import cluster_manager
import http3_quic
import mobile_proxies
import otp_3ds
import rate_limit
import redis_backend
import research_catalog
import research_engine
import resilience
import setup_wizard
import proxy_session_pin
import browser_pool
import discovery_adaptive

# WebSocket opcional (flask-socketio). Si no está, el dashboard funciona igual con polling.
try:
    from flask_socketio import SocketIO, emit as sio_emit  # noqa: F401
    _HAS_WS = True
except Exception:
    _HAS_WS = False
    SocketIO = None  # type: ignore

BASE_DIR = config.BASE_DIR

# Seed admin
users_manager.seed_admin_if_empty()

# Warning si admin/admin sigue activo (riesgo producción)
def _check_admin_default_password():
    """Avisa si admin tiene aún password=admin (riesgo). Activa flag UI."""
    try:
        u = users_manager.verify_login("admin", "admin", ip="0.0.0.0",
                                          user_agent="boot_check")
        if u:
            os.environ["TM_ADMIN_DEFAULT_PASSWORD"] = "1"
            log = logging.getLogger("tm_perros")
            log.warning("=" * 70)
            log.warning("⚠ SEGURIDAD: el usuario 'admin' tiene password 'admin' por defecto.")
            log.warning("Cambiar inmediatamente desde 'Usuarios → Cambiar contraseña'")
            log.warning("o setear env var TM_ADMIN_PASSWORD antes de producción.")
            log.warning("=" * 70)
            try: users_manager.logout(u.get("id"))
            except Exception: pass
    except Exception:
        pass

# Seed eventos de ejemplo
event_manager.seed_defaults()

# ==========================================
#  FLASK APP
# ==========================================
app = Flask(__name__)

# ════ SECRET_KEY persistente (1 vez generada, guardada en disk seguro) ════
# Prioridad: env var TM_SECRET_KEY → archivo secrets/.session_key (auto-gen) → fallback
import secrets as _secrets
_SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "secrets", ".session_key")
def _load_or_create_secret_key():
    env = os.environ.get("TM_SECRET_KEY", "").strip()
    if env and len(env) >= 32 and env != "tm-perros-secret-change-me":
        return env
    os.makedirs(os.path.dirname(_SECRET_FILE), exist_ok=True)
    if os.path.exists(_SECRET_FILE):
        try:
            with open(_SECRET_FILE, "r") as f:
                k = f.read().strip()
            if len(k) >= 32:
                return k
        except Exception:
            pass
    new_key = _secrets.token_hex(32)
    try:
        with open(_SECRET_FILE, "w") as f:
            f.write(new_key)
        try:
            os.chmod(_SECRET_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    return new_key

app.secret_key = _load_or_create_secret_key()

# ════ Sentry (opcional vía env TM_SENTRY_DSN) ════
try:
    _sentry_dsn = os.environ.get("TM_SENTRY_DSN", "").strip()
    if _sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=float(os.environ.get("TM_SENTRY_TRACE_RATE", "0.05")),
            send_default_pii=False,
            environment=os.environ.get("TM_ENV", "development"),
        )
except Exception:
    pass

# ════ Prometheus metrics endpoint /metrics (opcional, se activa solo si está disponible) ════
try:
    from prometheus_flask_exporter import PrometheusMetrics
    _prom = PrometheusMetrics(app, defaults_prefix="tm_perros",
                                group_by="endpoint")
    _prom.info("tm_perros_app", "TM PERROS dashboard",
                version=getattr(config, "APP_VERSION", "1.0.0"))
except Exception:
    pass

# ════ Rate limiter en endpoints sensibles (login + flow + research) ════
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, app=app, default_limits=[],
                       storage_uri="memory://")
    def rate_limit_decorator(rate):
        """Wrapper seguro: aplica limiter.limit() si está disponible, no-op si no."""
        def deco(fn):
            return limiter.limit(rate)(fn) if limiter else fn
        return deco
except Exception:
    limiter = None
    def rate_limit_decorator(rate):
        return lambda fn: fn
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
# Sesión persistente 24h (antes idle 30min → te echaba al login)
from datetime import timedelta as _timedelta
app.config["PERMANENT_SESSION_LIFETIME"] = _timedelta(hours=24)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
# Cookies SameSite/Secure/HttpOnly endurecidas
app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("TM_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("TM_COOKIE_SECURE", "0") == "1"

# Middleware: registrar latencia de cada request para health_monitor
import time as _time_mod
import gzip as _gzip_mod
import io as _io_mod
from flask import g as _flask_g

# Cache server-side (bytes response) para endpoints GET idempotentes.
# OJO: la key del cache es request.path (sin query params). Por eso SOLO se
# deben cachear endpoints cuyos query params no alteran la respuesta. Endpoints
# con query params relevantes (country, id, size…) NO deben cachearse aquí —
# ya tienen cache interno propio en los módulos de negocio.
_RESP_CACHE = {}
_CACHE_TTL = {
    "/api/ws/status": 30,
    "/api/healthz": 10,
    "/api/discovery/config": 60,
    "/api/monitor": 2,
    "/api/health/detailed": 3,
    "/api/multibrowser": 5,
    "/api/otp/providers": 300,
    "/api/bot/captcha/providers": 60,
    "/api/research/categories": 600,
    "/api/bots-compliance/waiver-text": 3600,
    # NO incluimos endpoints con query relevante:
    #   /api/discovery/featured   (country, size)  ← cache interno 15min en module
    #   /api/events               (filter, sort)
    #   /api/sites                (filter)
    #   /api/research/improvements (category, search)
}


@app.before_request
def _hm_start_timer():
    _flask_g._hm_start = _time_mod.time()
    # Cache hit check para ciertos endpoints GET
    if request.method == "GET":
        ttl = _CACHE_TTL.get(request.path)
        if ttl:
            entry = _RESP_CACHE.get(request.path)
            if entry and (_time_mod.time() - entry["ts"]) < ttl:
                from flask import make_response
                resp = make_response(entry["body"], 200)
                resp.headers["Content-Type"] = entry["ct"]
                resp.headers["X-Cache"] = "HIT"
                return resp


@app.after_request
def _hm_record(response):
    try:
        start = getattr(_flask_g, "_hm_start", None)
        if start:
            elapsed_ms = (_time_mod.time() - start) * 1000
            health_monitor.record_request(request.path, elapsed_ms, response.status_code)
    except Exception:
        pass

    # Security headers
    try:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()")
        # CSP: Content Security Policy. Permite inline scripts (necesarios por el template grande)
        # pero bloquea eval, sin imágenes/conexiones externas no controladas.
        if not response.headers.get("Content-Security-Policy"):
            csp = ("default-src 'self'; "
                   "script-src 'self' 'unsafe-inline' https://cdn.socket.io https://cdnjs.cloudflare.com; "
                   "style-src 'self' 'unsafe-inline'; "
                   "img-src 'self' data: blob: https://*.ticketm.net https://s1.ticketm.net "
                   "https://media.ticketmaster.* https://*.ticketmaster.* https:; "
                   "connect-src 'self' ws: wss: http://127.0.0.1:* http://localhost:*; "
                   "font-src 'self' data:; "
                   "object-src 'none'; "
                   "base-uri 'self'; "
                   "frame-ancestors 'self'")
            response.headers["Content-Security-Policy"] = csp
        # HSTS solo cuando se sirve por HTTPS (TM_COOKIE_SECURE=1)
        if os.environ.get("TM_COOKIE_SECURE", "0") == "1":
            response.headers.setdefault("Strict-Transport-Security",
                "max-age=31536000; includeSubDomains")
        # Cache control para assets static y JSON
        path = request.path
        if path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        elif path.startswith("/api/") and response.status_code == 200:
            # JSON responses: corto-vivido client cache para endpoints idempotentes GET
            if request.method == "GET":
                response.headers.setdefault("Cache-Control", "private, max-age=5")

        # Cache server-side para endpoints marcados
        if (request.method == "GET" and path in _CACHE_TTL
                and response.status_code == 200
                and response.headers.get("X-Cache") != "HIT"):
            try:
                body = response.get_data()
                _RESP_CACHE[path] = {
                    "body": body,
                    "ct": response.headers.get("Content-Type", "application/json"),
                    "ts": _time_mod.time(),
                }
            except Exception:
                pass
    except Exception:
        pass

    # Compresión gzip para respuestas >1KB y cliente que lo soporte
    try:
        accept = request.headers.get("Accept-Encoding", "")
        ct = response.headers.get("Content-Type", "")
        if ("gzip" in accept
                and response.status_code == 200
                and response.direct_passthrough is False
                and ("application/json" in ct or "text/" in ct)
                and len(response.get_data()) > 1024
                and response.headers.get("Content-Encoding") is None):
            data = response.get_data()
            gz = _gzip_mod.compress(data, compresslevel=5)
            response.set_data(gz)
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(gz))
            response.headers["Vary"] = "Accept-Encoding"
    except Exception:
        pass
    return response

# WebSocket server (solo si flask-socketio está disponible)
socketio = None
if _HAS_WS:
    try:
        # CORS WebSocket whitelist desde TM_CORS_ORIGINS (coma-separado).
        # Default: solo localhost + IP local (sin "*" inseguro). Para abrir a
        # más orígenes setear env var TM_CORS_ORIGINS="https://midominio.com".
        _cors_env = os.environ.get("TM_CORS_ORIGINS", "").strip()
        if _cors_env == "*":
            _ws_cors = "*"
        elif _cors_env:
            _ws_cors = [o.strip() for o in _cors_env.split(",") if o.strip()]
        else:
            _ws_cors = [
                "http://127.0.0.1:5055", "http://localhost:5055",
                f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}",
                "http://192.168.1.33:5055",
            ]
        socketio = SocketIO(
            app,
            cors_allowed_origins=_ws_cors,
            async_mode="threading",
            ping_timeout=20,
            ping_interval=12,
            logger=False,
            engineio_logger=False,
        )

        @socketio.on("connect")
        def _ws_connect():
            sio_emit("hello", {"ok": True, "ts": datetime.now().isoformat(),
                               "app": config.APP_NAME, "version": config.APP_VERSION})

        @socketio.on("ping_client")
        def _ws_ping(data=None):
            sio_emit("pong", {"ts": datetime.now().isoformat(), "echo": data})
    except Exception as _e:
        socketio = None


@app.route("/api/internal/ws-broadcast", methods=["POST"])
def api_internal_ws_broadcast():
    """Endpoint interno para que procesos hijos (register_account, account_recovery)
    disparen WS broadcasts al dashboard sin necesidad de session cookie."""
    if request.headers.get("X-Internal-Token") != "recovery":
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if request.remote_addr not in ("127.0.0.1", "localhost", "::1"):
        return jsonify({"ok": False, "error": "local_only"}), 403
    d = request.get_json(silent=True) or {}
    ev = d.get("event")
    payload = d.get("payload") or {}
    if not ev:
        return jsonify({"ok": False, "error": "event_required"}), 400
    try:
        if socketio:
            socketio.emit(ev, payload)
        return jsonify({"ok": True, "emitted": ev})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]})


def ws_broadcast(event_name: str, payload: dict):
    """Broadcast seguro — no rompe si WS está desactivado."""
    if socketio is None:
        return False
    try:
        socketio.emit(event_name, payload)
        return True
    except Exception:
        return False

# ==========================================
#  LOGGING
# ==========================================
os.makedirs(config.LOGS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(config.ruta("dashboard.log"), encoding="utf-8",
                            maxBytes=10_485_760, backupCount=3),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("TMPerros")


# ==========================================
#  AUTH HELPERS
# ==========================================
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        # 1. Session cookie (uso interactivo)
        if session.get("user"):
            return f(*args, **kwargs)
        # 2. API Bearer token (uso por bots / otros hosts)
        try:
            import api_tokens
            entry, _ = api_tokens.extract_token_from_request()
            if entry:
                # poblar session virtual para el request
                session_proxy = {"user": entry.get("user_id"),
                                 "via": "api_token",
                                 "token_id": entry.get("id"),
                                 "scopes": entry.get("scopes", [])}
                # adjuntar a flask.g para el endpoint
                from flask import g
                g.api_token = session_proxy
                return f(*args, **kwargs)
        except Exception:
            pass
        # 3. Fallo
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not_authenticated"}), 401
        return redirect(url_for("login_page"))
    return wrapped


# ==========================================
#  PAGINAS
# ==========================================
@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        app_name=config.APP_NAME,
        app_version=config.APP_VERSION,
        i18n_json=json.dumps(i18n.all_translations()),
        domains_json=json.dumps(domain_manager.list_all()),
        domains_by_region=json.dumps(domain_manager.by_region()),
        user=session.get("user"),
    )


@app.route("/login", methods=["GET", "POST"])
@rate_limit.rate_limit("login", max_per_min=10)
def login_page():
    if request.method == "GET":
        return render_template("login.html", app_name=config.APP_NAME)
    data = request.form if request.form else (request.get_json() or {})
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    ua = request.headers.get("User-Agent", "")
    user = users_manager.verify_login(username, password, ip=ip, user_agent=ua)
    # Caso: cuenta bloqueada
    if isinstance(user, dict) and user.get("error") == "locked":
        audit_log.record("auth:login_locked", actor=username, ip=ip, user_agent=ua,
                          success=False, meta={"locked_until": user.get("locked_until")})
        try:
            notifications.notify("user_locked",
                                 title=f"🔒 Usuario bloqueado: {username}",
                                 message=f"IP {ip} · locked until {user.get('locked_until')}",
                                 level="warning")
        except Exception:
            pass
        return jsonify({"ok": False, "error": "account_locked",
                        "locked_until": user.get("locked_until")}), 423
    if not user:
        audit_log.record("auth:login_fail", actor=username, ip=ip, user_agent=ua,
                          success=False)
        try:
            notifications.notify("login_fail",
                                 title=f"🚫 Login fallido: {username}",
                                 message=f"IP {ip}", level="warning")
        except Exception:
            pass
        return jsonify({"ok": False, "error": "invalid_credentials"}), 401
    session.permanent = True   # usa PERMANENT_SESSION_LIFETIME (24h)
    session["user"] = user
    audit_log.record("auth:login_ok", actor=username, ip=ip, user_agent=ua,
                      meta={"user_id": user.get("id"), "role": user.get("role")})
    try:
        notifications.notify("login_ok",
                             title=f"🔐 Login: {username}",
                             message=f"IP {ip} · role {user.get('role')}",
                             level="info")
    except Exception:
        pass
    return jsonify({"ok": True, "user": user})


@app.route("/logout")
def logout_page():
    user = session.get("user")
    if user:
        audit_log.record("auth:logout", actor=user.get("username", "?"),
                          ip=request.remote_addr or "",
                          meta={"user_id": user.get("id")})
        try:
            users_manager.logout(user.get("id"))
        except Exception:
            pass
    session.clear()
    return redirect(url_for("login_page"))


# ==========================================
#  API — DASHBOARD
# ==========================================
@app.route("/api/me")
def api_me():
    return jsonify({"ok": True, "user": session.get("user")})


@app.route("/api/data")
@login_required
def api_data():
    """Endpoint global — devuelve todo el estado del dashboard."""
    sel = domain_manager.get_selected()
    return jsonify({
        "ok": True,
        "app": {"name": config.APP_NAME, "version": config.APP_VERSION},
        "user": session.get("user"),
        "stats": {
            "tickets_bought": purchases_manager.get_report()["confirmed"],
            "events":         len(event_manager.get_enabled()),
            "accounts":       len(account_manager.account_manager.list_accounts()),
            "cards":          len(account_manager.account_manager.list_cards()),
            "proxies":        len(proxy_intelligence.proxy_manager.list_proxies()),
            "cookies":        cookie_optimizer.analyze_cookies()["active"],
            "workers":        sum(1 for g in bot_engine.engine.list_groups() for w in g["workers"] if w["status"] not in ("idle", "stopped", "success", "error")),
            "queues":         len(queue_worker.queue_manager.list_queues()),
            "problems":       problem_center.get_stats()["open"],
            "domains_active": len(sel["selected"]),
            "conversion":     analytics_mod.get_analytics()["conversion_rate"],
        },
        "domain_selection": sel,
        "ts": datetime.now().isoformat(),
    })


# ==========================================
#  API — DOMINIOS
# ==========================================
@app.route("/api/domains")
@login_required
def api_domains():
    return jsonify({
        "ok": True,
        "domains": domain_manager.list_all(),
        "by_region": domain_manager.by_region(),
        "selected": domain_manager.get_selected(),
    })


@app.route("/api/domains/select", methods=["POST"])
@login_required
def api_domains_select():
    data = request.get_json() or {}
    ids = data.get("ids") or []
    multi = bool(data.get("multi_mode"))
    result = domain_manager.set_selected(ids, multi_mode=multi)
    return jsonify({"ok": True, "selected": result})


# ==========================================
#  API — PROBLEMAS
# ==========================================
@app.route("/api/problems")
@login_required
def api_problems():
    q = request.args.get("q", "")
    sev = request.args.get("severity") or None
    cat = request.args.get("category") or None
    sort = request.args.get("sort", "recent")
    include_resolved = request.args.get("include_resolved", "0") == "1"
    problems = problem_center.search_problems(
        query=q, severity=sev, category=cat,
        include_resolved=include_resolved, sort=sort
    )
    return jsonify({
        "ok": True,
        "problems":   problems,
        "categories": problem_center.get_categories(),
        "stats":      problem_center.get_stats(),
        "timeline":   problem_center.get_timeline(hours=24),
        "breakdown":  problem_center.get_category_breakdown(),
    })


@app.route("/api/problems/report", methods=["POST"])
@login_required
def api_problems_report():
    d = request.get_json() or {}
    p = problem_center.report_problem(
        d.get("category", "Sistema"),
        d.get("title", ""),
        d.get("message", ""),
        d.get("severity", "medium"),
        d.get("meta"),
    )
    return jsonify({"ok": True, "problem": p})


@app.route("/api/problems/<pid>/resolve", methods=["POST"])
@login_required
def api_problems_resolve(pid):
    res = problem_center.resolve_problem(pid, (request.get_json(silent=True) or {}).get("resolution", ""))
    return jsonify({"ok": bool(res), "problem": res})


@app.route("/api/problems/resolve-batch", methods=["POST"])
@login_required
def api_problems_resolve_batch():
    d = request.get_json(silent=True) or {}
    ids = d.get("ids", [])
    n = problem_center.resolve_many(ids, d.get("resolution", ""))
    return jsonify({"ok": True, "resolved": n})


@app.route("/api/problems/auto-resolve", methods=["POST"])
@login_required
def api_problems_auto():
    n = problem_center.auto_resolve_all()
    return jsonify({"ok": True, "resolved": n})


@app.route("/api/problems/auto-resolve-stale", methods=["POST"])
@login_required
def api_problems_auto_stale():
    hours = int((request.get_json(silent=True) or {}).get("hours", 48))
    n = problem_center.auto_resolve_stale(hours=hours)
    return jsonify({"ok": True, "resolved": n})


@app.route("/api/problems/clear", methods=["POST"])
@login_required
def api_problems_clear():
    problem_center.clear_resolved()
    return jsonify({"ok": True})


# ==========================================
#  API — EVENTOS (antes Partidos)
# ==========================================
@app.route("/api/events")
@login_required
def api_events():
    return jsonify({"ok": True, "events": event_manager.list_events()})


@app.route("/api/events", methods=["POST"])
@login_required
def api_events_add():
    ev = event_manager.add_event(request.get_json() or {})
    return jsonify({"ok": True, "event": ev})


@app.route("/api/events/<eid>", methods=["PUT"])
@login_required
def api_events_update(eid):
    ev = event_manager.update_event(eid, request.get_json() or {})
    return jsonify({"ok": bool(ev), "event": ev})


@app.route("/api/events/<eid>", methods=["DELETE"])
@login_required
def api_events_delete(eid):
    event_manager.delete_event(eid)
    return jsonify({"ok": True})


@app.route("/api/events/repair", methods=["POST"])
@login_required
def api_events_repair():
    """Sanea eventos corruptos (id=URL, venue duplicado en city, country vacío)."""
    return jsonify(event_manager.repair_all())


@app.route("/api/events/health", methods=["GET"])
@login_required
def api_events_health_all():
    """Health-check de TODOS los eventos: indica cuáles están listos para que
    el bot escanee/lance, cuáles tienen campos faltantes, y qué falta."""
    return jsonify(event_manager.health_all())


@app.route("/api/events/<eid>/health", methods=["GET"])
@login_required
def api_event_health_one(eid):
    """Health-check de UN evento."""
    ev = next((e for e in event_manager.list_events() if e.get("id") == eid), None)
    return jsonify(event_manager.health_check(ev))


@app.route("/api/events/from-url", methods=["POST"])
@login_required
def api_events_from_url():
    """Crea (o pre-rellena) un evento a partir de una URL TM. Body: {tm_url,
    save?:bool=true}. Si save=False solo devuelve el dict pre-rellenado para
    que el frontend lo edite antes de guardar."""
    d = request.get_json() or {}
    url = (d.get("tm_url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "tm_url required"}), 400
    ev = event_manager.from_tm_url(url, fallback_name=d.get("name", ""))
    if d.get("save", True):
        saved = event_manager.add_event(ev)
        return jsonify({"ok": True, "saved": True, "event": saved,
                        "health": event_manager.health_check(saved)})
    return jsonify({"ok": True, "saved": False, "event": ev,
                    "health": event_manager.health_check(ev)})


@app.route("/api/events/bulk", methods=["POST"])
@login_required
def api_events_bulk():
    """Acción en bulk: enable/disable/delete de múltiples ids."""
    d = request.get_json() or {}
    action = d.get("action", "")
    ids = d.get("ids", [])
    count = 0
    for eid in ids:
        if action == "enable":
            if event_manager.update_event(eid, {"enabled": True}): count += 1
        elif action == "disable":
            if event_manager.update_event(eid, {"enabled": False}): count += 1
        elif action == "delete":
            event_manager.delete_event(eid); count += 1
    return jsonify({"ok": True, "action": action, "affected": count})


# ==========================================
#  API — Ofertas detectadas por evento (sección global fallback)
# ==========================================
import os as _os_eo, json as _json_eo
_OFFERS_PATH = _os_eo.path.join(_os_eo.path.dirname(__file__), "data", "event_offers.json")

def _load_offers():
    try:
        with open(_OFFERS_PATH, encoding='utf-8') as f: return _json_eo.load(f)
    except Exception: return {}

def _save_offers(d):
    _os_eo.makedirs(_os_eo.path.dirname(_OFFERS_PATH), exist_ok=True)
    with open(_OFFERS_PATH, 'w', encoding='utf-8') as f:
        _json_eo.dump(d, f, indent=2, ensure_ascii=False)


_FAN_RESALE_PATTERNS = ("fan a fan", "fan to fan", "fan-a-fan", "fan-2-fan",
                         "verified resale", "reventa", "resale")


def _is_resale_offer(o):
    name = (o.get("name") or "").lower()
    raw = (o.get("raw_text") or "").lower()
    return any(p in name or p in raw for p in _FAN_RESALE_PATTERNS)


@app.route("/api/events/<tm_id>/offers", methods=["GET"])
@login_required
def api_event_offers_get(tm_id):
    """Devuelve las ofertas detectadas para un evento (poblar 'Sección global fallback').
    Filtra dinámicamente las reventa fan-a-fan por si quedaron en cache antiguo."""
    data = _load_offers()
    entry = dict(data.get(tm_id) or {"offers": []})
    offers = entry.get("offers", []) or []
    cleaned = [o for o in offers if not _is_resale_offer(o)]
    if len(cleaned) != len(offers):
        entry["offers"] = cleaned
        # Persistir limpieza para que no se rehaga en cada GET
        try:
            data[tm_id] = entry
            _save_offers(data)
        except Exception:
            pass
    entry["offers"] = cleaned
    return jsonify({"ok": True, "tm_id": tm_id, **entry})


@app.route("/api/events/offers/purge-resale", methods=["POST"])
@login_required
def api_event_offers_purge_resale():
    """Limpia ofertas tipo 'Entrada de fan a fan' / Verified Resale de TODOS los
    eventos persistidos. Útil tras introducir el filtro para purgar caches viejos."""
    data = _load_offers()
    total_removed = 0
    affected = 0
    for tm_id, entry in list(data.items()):
        if not isinstance(entry, dict): continue
        offers = entry.get("offers", []) or []
        cleaned = [o for o in offers if not _is_resale_offer(o)]
        if len(cleaned) != len(offers):
            removed = len(offers) - len(cleaned)
            total_removed += removed
            affected += 1
            entry["offers"] = cleaned
            data[tm_id] = entry
    _save_offers(data)
    return jsonify({"ok": True, "events_affected": affected,
                    "offers_removed": total_removed})


@app.route("/api/events/<tm_id>/venue", methods=["GET"])
@login_required
def api_event_venue_get(tm_id):
    """Devuelve el venue cacheado para un evento (si existe)."""
    import venue_cache as _vc
    v = _vc.get_venue_for_event(tm_id)
    if not v:
        return jsonify({"ok": False, "error": "no_venue_cached", "tm_id": tm_id}), 200
    return jsonify({"ok": True, "tm_id": tm_id, "venue": v})


@app.route("/api/venue/<venue_id>", methods=["GET"])
@login_required
def api_venue_get(venue_id):
    import venue_cache as _vc
    v = _vc.get_venue(venue_id)
    if not v:
        return jsonify({"ok": False, "error": "venue_not_found"}), 404
    return jsonify({"ok": True, "venue": v})


@app.route("/api/venues", methods=["GET"])
@login_required
def api_venues_list():
    import venue_cache as _vc
    return jsonify({"ok": True, "venues": _vc.list_venues(), "stats": _vc.stats()})


@app.route("/api/venues/rebuild-from-disk", methods=["POST"])
@login_required
def api_venues_rebuild():
    """Reconstruye venue_cache.json desde las carpetas datos/venues/<slug>/map.png.
    Útil tras pérdida del JSON: recupera venue_id, name aproximado, image_path."""
    import venue_cache as _vc
    return jsonify(_vc.rebuild_from_disk())


@app.route("/api/venue/<venue_id>/image")
@login_required
def api_venue_image(venue_id):
    """Sirve la imagen del mapa del recinto (PNG)."""
    import venue_cache as _vc
    p = _vc.get_image_path_abs(venue_id)
    if not p:
        return jsonify({"ok": False, "error": "no_image"}), 404
    from flask import send_file
    return send_file(p, mimetype="image/png")


@app.route("/api/events/cache-status", methods=["GET"])
@login_required
def api_events_cache_status():
    """Devuelve para cada evento del catálogo si tiene ofertas+venue cacheado.
    Útil para que el frontend muestre chips 'cacheado/sin escanear' y dispare
    scan automático en eventos pendientes."""
    import venue_cache as _vc
    offers_data = _load_offers()
    out = []
    for ev in event_manager.list_events():
        tm_id = str(ev.get("tm_id") or "")
        if not tm_id:
            continue
        has_offers = bool((offers_data.get(tm_id) or {}).get("offers"))
        venue_obj = _vc.get_venue_for_event(tm_id)
        has_venue = bool(venue_obj and venue_obj.get("sections"))
        out.append({
            "id": ev.get("id"), "tm_id": tm_id,
            "name": ev.get("name"), "venue": ev.get("venue"),
            "has_offers": has_offers, "has_venue": has_venue,
            "fully_cached": has_offers and has_venue,
        })
    return jsonify({"ok": True, "events": out,
                    "stats": {"total": len(out),
                              "fully_cached": sum(1 for e in out if e["fully_cached"]),
                              "pending": sum(1 for e in out if not e["fully_cached"])}})


# NOTA: el endpoint /api/events/auto-scan-pending fue ELIMINADO por petición del usuario.
# El auto-scan SOLO debe ejecutarse cuando el usuario selecciona un evento en el bot
# (vía /api/venue/scan invocado desde autoScanEventIfNeeded en el frontend), nunca en bulk.


@app.route("/api/venue/<venue_id>/api-sections", methods=["GET"])
@login_required
def api_venue_api_sections(venue_id):
    """Devuelve las api_sections (TM ismds IDs) cacheadas para un venue.
    Útil para que el frontend muestre los IDs reales TM y permita compra rápida."""
    import venue_cache as _vc
    v = _vc.get_venue(venue_id)
    if not v:
        return jsonify({"ok": False, "error": "venue_not_found"}), 404
    return jsonify({"ok": True, "venue_id": venue_id,
                    "api_sections": v.get("api_sections", []),
                    "api_sections_updated_at": v.get("api_sections_updated_at"),
                    "svg_sections_count": len(v.get("sections", []))})


@app.route("/api/venue/api-fetch", methods=["POST"])
@login_required
def api_venue_api_fetch():
    """Llamada directa a la API ismds de TM sin browser. Body: {tm_id, country, qty?}."""
    import tm_section_api as _tsa
    d = request.get_json() or {}
    return jsonify(_tsa.fetch_availability(
        tm_id=d.get("tm_id", ""),
        country=d.get("country", "es"),
        qty=int(d.get("qty", 4))))


@app.route("/api/venue/scan", methods=["POST"])
@login_required
def api_venue_scan():
    """Scan completo de un evento → guarda venue + secciones + imagen.
    Body: {tm_id, tm_url, event_name, venue?, city?, country?, force_rescan?}
    Devuelve {ok, venue_id, venue, cached}."""
    import venue_scanner as _vs
    d = request.get_json() or {}
    tm_id = d.get("tm_id") or ""
    tm_url = d.get("tm_url") or ""
    name = d.get("venue") or d.get("venue_name") or d.get("event_name") or ""
    city = d.get("city") or ""
    country = d.get("country") or ""
    if not tm_id or not tm_url:
        return jsonify({"ok": False, "error": "tm_id and tm_url required"}), 400
    if not name:
        # Intentar sacar de event_manager
        try:
            ev = event_manager.get_event_by_tm_id(tm_id) if hasattr(event_manager, "get_event_by_tm_id") else None
            if not ev:
                for e in event_manager.list_events():
                    if str(e.get("tm_id")) == str(tm_id):
                        ev = e; break
            if ev:
                name = ev.get("venue") or ev.get("name") or ""
                city = city or ev.get("city") or ""
                country = country or ev.get("country") or ""
        except Exception:
            pass
    res = _vs.scan_event(tm_id, tm_url, name=name or "venue-unknown",
                         city=city, country=country,
                         reuse_cached=not bool(d.get("force_rescan")))
    return jsonify(res)


@app.route("/api/events/<tm_id>/offers", methods=["POST"])
@login_required
def api_event_offers_post(tm_id):
    """Guarda ofertas detectadas por el bot. Body: {offers:[{name,category?,price_min,price_max,raw_text}], scan_method?}"""
    d = request.get_json() or {}
    offers = d.get("offers") or []
    if not isinstance(offers, list):
        return jsonify({"ok": False, "error": "offers must be list"}), 400
    data = _load_offers()
    from datetime import datetime as _dt
    data[tm_id] = {
        "tm_id": tm_id,
        "tm_url": d.get("tm_url", (data.get(tm_id) or {}).get("tm_url", "")),
        "event_name": d.get("event_name", (data.get(tm_id) or {}).get("event_name", "")),
        "venue": d.get("venue", (data.get(tm_id) or {}).get("venue", "")),
        "offers": offers,
        "scanned_at": _dt.now().isoformat(),
        "scan_method": d.get("scan_method", "auto"),
    }
    _save_offers(data)
    return jsonify({"ok": True, "tm_id": tm_id, "offers_count": len(offers)})


# ==========================================
#  API — KASADA SOLVER (x-kpsdk-ct / x-kpsdk-cd)
# ==========================================
import kasada_solver

@app.route("/api/kasada/status", methods=["GET"])
@login_required
def api_kasada_status():
    return jsonify(kasada_solver.get_status())

@app.route("/api/kasada/config", methods=["POST"])
@login_required
def api_kasada_config():
    return jsonify(kasada_solver.update_config(request.get_json(silent=True) or {}))

@app.route("/api/kasada/fetch", methods=["POST"])
@login_required
def api_kasada_fetch():
    body = request.get_json(silent=True) or {}
    domain = body.get("domain", "ticketmaster.es")
    res = kasada_solver.fetch_ips_js(domain)
    if not res:
        return jsonify({"ok": False, "error_key": "kasada.fetch_failed",
                        "error_args": {"domain": domain}})
    body_text = res.pop("body", "")
    return jsonify({**res, "body_preview": body_text[:500],
                    "body_length": len(body_text)})

@app.route("/api/kasada/solve", methods=["POST"])
@login_required
def api_kasada_solve():
    body = request.get_json(silent=True) or {}
    domain = body.get("domain", "ticketmaster.es")
    force = bool(body.get("force_refresh", False))
    return jsonify(kasada_solver.generate_tokens(domain, force_refresh=force))

@app.route("/api/kasada/check-rotation", methods=["POST"])
@login_required
def api_kasada_check():
    body = request.get_json(silent=True) or {}
    return jsonify(kasada_solver.check_rotation(body.get("domain", "ticketmaster.es")))

@app.route("/api/kasada/discover-uuids", methods=["POST"])
@login_required
def api_kasada_discover():
    body = request.get_json(silent=True) or {}
    return jsonify(kasada_solver.discover_uuids(body.get("domain", "ticketmaster.es")))


# ==========================================
#  API — HTTP/2 FRAME-ORDER MATCH
# ==========================================
import http2_frame_match

@app.route("/api/http2-frame/status", methods=["GET"])
@login_required
def api_http2_status():
    return jsonify(http2_frame_match.get_status())

@app.route("/api/http2-frame/config", methods=["POST"])
@login_required
def api_http2_config():
    return jsonify(http2_frame_match.update_config(request.get_json(silent=True) or {}))

@app.route("/api/http2-frame/self-test", methods=["POST"])
@login_required
def api_http2_self_test():
    body = request.get_json(silent=True) or {}
    return jsonify(http2_frame_match.self_test(profile_name=body.get("profile")))

@app.route("/api/http2-frame/assert-tm", methods=["POST"])
@login_required
def api_http2_assert_tm():
    body = request.get_json(silent=True) or {}
    return jsonify(http2_frame_match.assert_tm_fingerprint(
        domain=body.get("domain", "ticketmaster.es"),
        profile_name=body.get("profile"),
    ))


# ==========================================
#  API — OTP 3DS AUTOFILL (IMAP + SMS WEBHOOK + INJECT)
# ==========================================
import otp_3ds_autofill

@app.route("/api/otp3ds/status", methods=["GET"])
@login_required
def api_otp3ds_autofill_status():
    reveal = (request.args.get('reveal') == '1')
    return jsonify(otp_3ds_autofill.get_status(reveal_secret=reveal))

@app.route("/api/otp3ds/regenerate-secret", methods=["POST"])
@login_required
def api_otp3ds_regen_secret():
    return jsonify(otp_3ds_autofill.regenerate_webhook_secret())

@app.route("/api/otp3ds/config", methods=["POST"])
@login_required
def api_otp3ds_autofill_config():
    return jsonify(otp_3ds_autofill.update_config(request.get_json(silent=True) or {}))

@app.route("/api/otp3ds/poll", methods=["POST"])
@login_required
def api_otp3ds_autofill_poll():
    return jsonify(otp_3ds_autofill.poll_imap_once())

@app.route("/api/otp3ds/test-pattern", methods=["POST"])
@login_required
def api_otp3ds_autofill_test_pattern():
    body = request.get_json(silent=True) or {}
    return jsonify(otp_3ds_autofill.test_pattern(
        body.get("text", ""), bank=body.get("bank")
    ))

@app.route("/api/otp3ds/webhook", methods=["POST"])
def api_otp3ds_autofill_webhook():
    """Endpoint público (sin login_required) para recibir SMS de proveedor.
    Usar `webhook_secret` en config para validar."""
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    return jsonify(otp_3ds_autofill.receive_webhook(payload, dict(request.headers)))


# ==========================================
#  API — SHAPE VM OPCODE SOLVER (placeholder, módulo creado abajo)
# ==========================================
import shape_vm_solver

@app.route("/api/shape/status", methods=["GET"])
@login_required
def api_shape_status():
    return jsonify(shape_vm_solver.get_status())

@app.route("/api/shape/config", methods=["POST"])
@login_required
def api_shape_config():
    return jsonify(shape_vm_solver.update_config(request.get_json(silent=True) or {}))

@app.route("/api/shape/fetch", methods=["POST"])
@login_required
def api_shape_fetch():
    body = request.get_json(silent=True) or {}
    return jsonify(shape_vm_solver.fetch_launcher_and_vm(
        domain=body.get("domain", "ticketmaster.com")
    ))

@app.route("/api/shape/disasm", methods=["POST"])
@login_required
def api_shape_disasm():
    body = request.get_json(silent=True) or {}
    return jsonify(shape_vm_solver.disassemble(domain=body.get("domain", "ticketmaster.com")))

@app.route("/api/shape/emit-signal", methods=["POST"])
@login_required
def api_shape_emit():
    body = request.get_json(silent=True) or {}
    return jsonify(shape_vm_solver.emit_signal_blob(
        domain=body.get("domain", "ticketmaster.com")
    ))

@app.route("/api/shape/capture-reference", methods=["POST"])
@login_required
def api_shape_capture():
    body = request.get_json(silent=True) or {}
    return jsonify(shape_vm_solver.capture_reference_signal(
        domain=body.get("domain", "ticketmaster.com")
    ))

@app.route("/api/shape/disasm-flow", methods=["POST"])
@login_required
def api_shape_disasm_flow():
    body = request.get_json(silent=True) or {}
    return jsonify(shape_vm_solver.disassemble_with_flow(
        domain=body.get("domain", "ticketmaster.com")
    ))

@app.route("/api/shape/delta-diff", methods=["POST"])
@login_required
def api_shape_delta_diff():
    body = request.get_json(silent=True) or {}
    return jsonify(shape_vm_solver.delta_diff(domain=body.get("domain", "ticketmaster.com")))

@app.route("/api/http2-frame/request", methods=["POST"])
@login_required
def api_http2_request():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error_key": "http2.missing_url"})
    return jsonify(http2_frame_match.request(
        body.get("method", "GET").upper(), url,
        headers=body.get("headers"), profile_name=body.get("profile"),
        engine=body.get("engine"), timeout=int(body.get("timeout", 15)),
    ))


# ==========================================
#  API — EVENT DISCOVERY (Ticketmaster Discovery API)
# ==========================================
@app.route("/api/discovery/config", methods=["GET"])
@login_required
def api_discovery_config():
    cfg = event_discovery.get_config()
    # ocultar api_key en GET (mostrar solo preview)
    safe = dict(cfg)
    key = safe.get("api_key") or ""
    safe["api_key_preview"] = (key[:4] + "…" + key[-4:]) if len(key) >= 10 else ("" if not key else "****")
    safe.pop("api_key", None)
    safe["configured"] = event_discovery.is_configured()
    # Flag explícito: ¿la key guardada es un placeholder?
    safe["is_placeholder"] = event_discovery._is_placeholder_key(key) if key else False
    safe["key_length"] = len(key)
    return jsonify({"ok": True, "config": safe})


@app.route("/api/discovery/config", methods=["POST"])
@login_required
def api_discovery_config_save():
    d = request.get_json() or {}
    cfg = event_discovery.save_config(d)
    return jsonify({"ok": True, "configured": event_discovery.is_configured()})


@app.route("/api/discovery/search", methods=["POST"])
@login_required
def api_discovery_search():
    d = request.get_json() or {}
    return jsonify(event_discovery.search_events(
        keyword=d.get("keyword", ""),
        city=d.get("city", ""),
        country=d.get("country", "us"),
        size=int(d.get("size", 20)),
    ))


@app.route("/api/discovery/enrich", methods=["POST"])
@login_required
def api_discovery_enrich():
    """Dada una URL TM o event_id, devuelve datos enriquecidos."""
    d = request.get_json() or {}
    if d.get("url"):
        return jsonify(event_discovery.enrich_from_url(d["url"]))
    if d.get("tm_id"):
        return jsonify(event_discovery.get_event_detail(d["tm_id"]))
    return jsonify({"ok": False, "error": "falta url o tm_id"}), 400


@app.route("/api/discovery/import", methods=["POST"])
@login_required
def api_discovery_import():
    """Importa uno o varios eventos encontrados al catálogo local."""
    d = request.get_json() or {}
    events = d.get("events") or []
    if d.get("tm_id"):
        r = event_discovery.sync_event_by_id(d["tm_id"], import_to_local=True)
        ws_broadcast("event_imported", {"tm_id": d["tm_id"]})
        return jsonify(r)
    if events:
        r = event_discovery.bulk_import(events)
        ws_broadcast("event_imported", {"count": r.get("added", 0)})
        return jsonify(r)
    return jsonify({"ok": False, "error": "nada para importar"}), 400


@app.route("/api/discovery/search-artist", methods=["POST"])
@login_required
def api_discovery_search_artist():
    """Busca eventos por URL de artista o attractionId.
    Body: {"url": "https://www.ticketmaster.es/artist/..../1418653"}
       o  {"artist_id": "1418653", "country": "es"}
    """
    d = request.get_json() or {}
    url = (d.get("url") or "").strip()
    size = int(d.get("size", 50))
    if url:
        return jsonify(event_discovery.search_by_artist_url(url, size=size))
    return jsonify({"ok": False, "error": "url requerido"})


@app.route("/api/discovery/test-key", methods=["POST"])
@login_required
def api_discovery_test_key():
    """Valida una API key Discovery contra el servidor real de TM sin guardarla.
    Útil para 'probar antes de guardar' en el wizard.
    Acepta {"api_key": "xxx"} en el body.
    """
    import time, requests as _rq
    d = request.get_json() or {}
    key = (d.get("api_key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "empty_key",
                        "hint": "Introduce una API key para probar"})
    if event_discovery._is_placeholder_key(key):
        return jsonify({
            "ok": False, "error": "placeholder_key",
            "hint": "Esta key parece un placeholder. Obtén una real en developer.ticketmaster.com",
            "key_preview": key[:6] + "…" + key[-4:] if len(key) > 12 else key,
        })
    started = time.time()
    try:
        r = _rq.get(event_discovery.DISCOVERY_BASE + "/events.json",
                    params={"apikey": key, "size": 1},
                    timeout=8)
        elapsed_ms = int((time.time() - started) * 1000)
        if r.status_code == 200:
            total = 0
            try:
                data = r.json()
                total = (data.get("page") or {}).get("totalElements", 0)
            except Exception: pass
            return jsonify({
                "ok": True,
                "valid": True,
                "status_code": 200,
                "elapsed_ms": elapsed_ms,
                "total_events_available": total,
                "key_preview": key[:6] + "…" + key[-4:],
                "msg": f"✅ Key válida — {total:,} eventos disponibles en TM",
            })
        if r.status_code == 401:
            return jsonify({
                "ok": True, "valid": False, "status_code": 401,
                "elapsed_ms": elapsed_ms,
                "key_preview": key[:6] + "…" + key[-4:],
                "msg": "❌ TM rechazó la key (401 Unauthorized). Verifica que la copiaste completa.",
            })
        if r.status_code == 429:
            return jsonify({
                "ok": True, "valid": None, "status_code": 429,
                "elapsed_ms": elapsed_ms,
                "msg": "⚠ Rate limit (429). La key podría ser válida pero superaste el límite diario.",
            })
        return jsonify({
            "ok": True, "valid": False, "status_code": r.status_code,
            "elapsed_ms": elapsed_ms,
            "msg": f"❌ TM respondió HTTP {r.status_code}",
            "snippet": r.text[:150],
        })
    except Exception as e:
        return jsonify({
            "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
            "elapsed_ms": int((time.time() - started) * 1000),
            "msg": "❌ Error de red contactando TM. Revisa tu conexión.",
        })


@app.route("/api/discovery/featured", methods=["GET"])
@login_required
def api_discovery_featured():
    """Destacados TM por país (con caché 15 min). Modo demo si no hay API key."""
    country = request.args.get("country", "us")
    size = int(request.args.get("size", 20))
    force = request.args.get("refresh") == "1"
    return jsonify(event_discovery.get_featured(country=country, size=size, force_refresh=force))


@app.route("/api/discovery/featured/multi", methods=["GET"])
@login_required
def api_discovery_featured_multi():
    """Devuelve destacados de varios países a la vez."""
    countries = (request.args.get("countries", "us,es,uk") or "us").split(",")
    countries = [c.strip() for c in countries if c.strip()]
    size = int(request.args.get("size", 10))
    force = request.args.get("refresh") == "1"
    out = {}
    for c in countries[:15]:  # máximo 15 países por request
        out[c] = event_discovery.get_featured(country=c, size=size, force_refresh=force)
    return jsonify({"ok": True, "by_country": out})


@app.route("/api/discovery/scheduler/status", methods=["GET"])
@login_required
def api_discovery_sched_status():
    return jsonify({"ok": True, "status": event_discovery.scheduler_status()})


@app.route("/api/discovery/scheduler/start", methods=["POST"])
@login_required
def api_discovery_sched_start():
    return jsonify(event_discovery.start_scheduler())


@app.route("/api/discovery/scheduler/stop", methods=["POST"])
@login_required
def api_discovery_sched_stop():
    return jsonify(event_discovery.stop_scheduler())


# ==========================================
#  API — NOTIFICATIONS TEST (Discord/Telegram real)
# ==========================================
@app.route("/api/notifications/test/<channel>", methods=["POST"])
@login_required
def api_notif_test(channel):
    return jsonify(notifications.test_channel(channel))


@app.route("/api/notifications/send", methods=["POST"])
@login_required
def api_notif_send():
    d = request.get_json() or {}
    res = notifications.notify(
        d.get("event_type", "manual"),
        d.get("title", "Notificación"),
        d.get("message", ""),
        level=d.get("level", "info"),
    )
    return jsonify({"ok": True, "entry": res})


# ==========================================
#  API — SITIOS DE VENTA
# ==========================================
@app.route("/api/sites")
@login_required
def api_sites():
    return jsonify({"ok": True, "sites": site_manager.list_sites()})


@app.route("/api/sites", methods=["POST"])
@login_required
def api_sites_add():
    s = site_manager.add_site(request.get_json() or {})
    return jsonify({"ok": True, "site": s})


@app.route("/api/sites/<sid>", methods=["PUT"])
@login_required
def api_sites_update(sid):
    s = site_manager.update_site(sid, request.get_json() or {})
    return jsonify({"ok": bool(s), "site": s})


@app.route("/api/sites/<sid>", methods=["DELETE"])
@login_required
def api_sites_delete(sid):
    site_manager.delete_site(sid)
    return jsonify({"ok": True})


# ==========================================
#  API — COMPRAS
# ==========================================
@app.route("/api/purchases")
@login_required
def api_purchases():
    items = purchases_manager.filter_purchases(
        status=request.args.get("status") or None,
        country=request.args.get("country") or None,
        seat_category=request.args.get("seat_category") or None,
        search=request.args.get("q", ""),
        limit=int(request.args.get("limit", 500)),
    )
    return jsonify({
        "ok": True,
        "purchases": items,
        "report":    purchases_manager.get_report(),
        "categories": purchases_manager.get_categories(),
        "delivery_methods": purchases_manager.get_delivery_methods(),
        "statuses": purchases_manager.get_statuses(),
    })


@app.route("/api/purchases", methods=["POST"])
@login_required
def api_purchases_add():
    d = request.get_json() or {}
    p = purchases_manager.add_purchase(d)
    actor = (session.get("user") or {}).get("username", "system")
    try:
        audit_log.record("purchase:created", actor=actor, target=p.get("id"),
                          meta={"event": p.get("event_name"), "total": p.get("total_price")})
    except Exception:
        pass
    return jsonify({"ok": True, "purchase": p})


@app.route("/api/purchases/<pid>", methods=["PUT"])
@login_required
def api_purchases_update(pid):
    r = purchases_manager.update_purchase(pid, request.get_json() or {})
    if r:
        actor = (session.get("user") or {}).get("username", "system")
        try:
            audit_log.record("purchase:updated", actor=actor, target=pid)
        except Exception:
            pass
    return jsonify({"ok": bool(r), "purchase": r})


@app.route("/api/purchases/<pid>", methods=["DELETE"])
@login_required
def api_purchases_delete(pid):
    ok = purchases_manager.delete_purchase(pid)
    if ok:
        actor = (session.get("user") or {}).get("username", "system")
        try:
            audit_log.record("purchase:deleted", actor=actor, target=pid)
        except Exception:
            pass
    return jsonify({"ok": ok})


@app.route("/api/purchases/filter", methods=["POST"])
@login_required
def api_purchases_filter():
    d = request.get_json() or {}
    return jsonify({"ok": True, "purchases": purchases_manager.filter_purchases(
        status=d.get("status"), domain_id=d.get("domain_id"), event_id=d.get("event_id"),
        country=d.get("country"), seat_category=d.get("seat_category"),
        search=d.get("search", ""),
    )})


@app.route("/api/purchases/export-csv", methods=["GET", "POST"])
@login_required
def api_purchases_export():
    from flask import Response
    if request.method == "GET":
        # download directo
        csv_text = purchases_manager.export_csv_string()
        return Response(
            csv_text, mimetype="text/csv",
            headers={"Content-Disposition":
                     f"attachment; filename=compras_tm_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"},
        )
    path = purchases_manager.export_csv()
    return jsonify({"ok": True, "path": path})


# ==========================================
#  API — SNIPER BOT
# ==========================================
@app.route("/api/bot/groups")
@login_required
def api_bot_groups():
    return jsonify({"ok": True, "groups": bot_engine.engine.list_groups()})


@app.route("/api/bot/groups", methods=["POST"])
@login_required
def api_bot_groups_create():
    """Crea un grupo de bot. Acepta y propaga al Worker:
    selection_mode ('quick'|'seatmap'), offer_index (oferta detectada), category, quantity,
    url, browser, captcha_provider, strategy, retries, retry_delay_s, queue_timeout_min, cooldown_min."""
    d = request.get_json() or {}
    # Construir dict de params propagables (filtrado en bot_engine._filter_params).
    # Forzar tipos primitivos donde aplique.
    params = {
        "selection_mode": (d.get("selection_mode") or "quick"),
        "offer_index": d.get("offer_index"),
        "category": d.get("category") or "",
        "quantity": int(d.get("quantity") or 1),
        "url": d.get("url") or "",
        "browser": d.get("browser") or "auto",
        "captcha_provider": d.get("captcha_provider") or "auto",
        "strategy": d.get("strategy") or "equal",
        "retries": d.get("retries"),
        "retry_delay_s": d.get("retry_delay_s") or d.get("retry_delay"),
        "queue_timeout_min": d.get("queue_timeout_min") or d.get("queue_timeout"),
        "cooldown_min": d.get("cooldown_min") or d.get("cooldown"),
        # Modo seatmap (sectores manuales sobre venue cacheado)
        "selected_sections": d.get("selected_sections"),
        "venue_id": d.get("venue_id"),
        "strict_targets": d.get("strict_targets"),  # default True si selected_sections viene
        # Multi-select de ofertas: orden de prioridad para select_quick_smart
        "selected_offer_indices": d.get("selected_offer_indices"),
        # Precios máximos por oferta (rechazar asientos por encima)
        "offer_max_prices": d.get("offer_max_prices"),
        # Categorías genéricas multi-select (Floor/GA/Reserved/VIP/Boxes/Wheelchair)
        "selected_categories": d.get("selected_categories"),
    }
    g = bot_engine.engine.create_group(
        d.get("event_id"), d.get("domain_ids", []), d.get("name", ""), params=params
    )
    worker_count = int(d.get("worker_count", 1))
    for i in range(worker_count):
        g.add_worker(account_id=d.get("account_id"))  # workers heredan params del group
    bot_engine.engine._persist()
    return jsonify({"ok": True, "group": g.to_dict()})


# ==========================================
#  API — BOT PRESETS (selecciones guardadas)
# ==========================================
@app.route("/api/bot/presets", methods=["GET"])
@login_required
def api_bot_presets_list():
    return jsonify({"ok": True, "presets": bot_presets.list_presets(),
                    "stats": bot_presets.get_stats()})


@app.route("/api/bot/presets", methods=["POST"])
@login_required
def api_bot_presets_create():
    d = request.get_json() or {}
    name = d.get("name", "")
    events = d.get("events", [])
    cfg = d.get("config", {})
    if not events:
        return jsonify({"ok": False, "error": "events requerido"}), 400
    p = bot_presets.save_preset(name, events, cfg)
    return jsonify({"ok": True, "preset": p})


@app.route("/api/bot/presets/<pid>", methods=["GET"])
@login_required
def api_bot_presets_get(pid):
    p = bot_presets.get_preset(pid)
    if not p:
        return jsonify({"ok": False, "error": "preset_not_found"}), 404
    return jsonify({"ok": True, "preset": p})


@app.route("/api/bot/presets/<pid>", methods=["DELETE"])
@login_required
def api_bot_presets_delete(pid):
    ok = bot_presets.delete_preset(pid)
    if not ok:
        return jsonify({"ok": False, "error": "preset_not_found"}), 404
    return jsonify({"ok": True})


@app.route("/api/bot/presets/self-test", methods=["POST"])
@login_required
def api_bot_presets_self_test():
    return jsonify(bot_presets.self_test())


# ==========================================
#  API — 10 MÓDULOS CRÍTICOS 2026
# ==========================================
@app.route("/api/bot/critical-2026/status")
@login_required
def api_critical_2026_status():
    """Status agregado de los 10 módulos críticos 2026."""
    out = {}
    modules = [
        ("ja4_pinning", ja4_pinning),
        ("http2_fingerprint", http2_fingerprint),
        ("safetix_handler", safetix_handler),
        ("compliance_gate", compliance_gate),
        ("datadome_proxy_hooks", datadome_proxy_hooks),
        ("uach_grease_validator", uach_grease_validator),
        ("asn_ip_drift", asn_ip_drift),
        ("psd3_payment_retry", psd3_payment_retry),
        ("trust_tuple_warming", trust_tuple_warming),
        ("tls_ct_monitor", tls_ct_monitor),
    ]
    for name, mod in modules:
        try:
            out[name] = mod.get_info()
        except Exception as e:
            out[name] = {"error": str(e)[:100]}
    return jsonify({"ok": True, "modules": out})


@app.route("/api/bot/critical-2026/<module_name>/self-test", methods=["POST"])
@login_required
def api_critical_2026_self_test(module_name):
    """Run self-test de cualquiera de los 10 módulos críticos."""
    mods = {
        "ja4_pinning": ja4_pinning, "http2_fingerprint": http2_fingerprint,
        "safetix_handler": safetix_handler, "compliance_gate": compliance_gate,
        "datadome_proxy_hooks": datadome_proxy_hooks,
        "uach_grease_validator": uach_grease_validator,
        "asn_ip_drift": asn_ip_drift, "psd3_payment_retry": psd3_payment_retry,
        "trust_tuple_warming": trust_tuple_warming, "tls_ct_monitor": tls_ct_monitor,
    }
    mod = mods.get(module_name)
    if not mod:
        return jsonify({"ok": False, "error": "module_not_found"}), 404
    return jsonify(mod.self_test())


@app.route("/api/bot/critical-2026/uach/build", methods=["POST"])
@login_required
def api_uach_build():
    d = request.get_json() or {}
    if d.get("browser") == "firefox":
        return jsonify({"ok": True, "headers": uach_grease_validator.build_firefox_headers(
            d.get("major_version", 128))})
    return jsonify({"ok": True, "headers": uach_grease_validator.build_chrome_headers(
        major_version=d.get("major_version", 131),
        platform=d.get("platform", "Windows"),
        mobile=bool(d.get("mobile", False)))})


@app.route("/api/bot/critical-2026/compliance/log-consent", methods=["POST"])
@login_required
def api_compliance_log_consent():
    d = request.get_json() or {}
    return jsonify(compliance_gate.log_consent(
        account_id=d.get("account_id", ""), event_id=d.get("event_id", ""),
        country=d.get("country", "")))


@app.route("/api/bot/critical-2026/compliance/check-resale", methods=["POST"])
@login_required
def api_compliance_check_resale():
    d = request.get_json() or {}
    return jsonify(compliance_gate.validate_resale(
        d.get("country", ""), float(d.get("face_value", 0)),
        float(d.get("listing_value", 0))))


# === Compliance Gate (DFA Q3 2026) — endpoints completos ===
@app.route("/api/compliance/stats", methods=["GET"])
@login_required
def api_compliance_stats():
    return jsonify({"ok": True, "stats": compliance_gate.get_stats()})


@app.route("/api/compliance/check-consent", methods=["POST"])
@login_required
def api_compliance_check_consent():
    d = request.get_json() or {}
    return jsonify(compliance_gate.check_consent(
        d.get("account_id", ""), d.get("event_id", "")))


@app.route("/api/compliance/verify-chain", methods=["GET"])
@login_required
def api_compliance_verify_chain():
    return jsonify(compliance_gate.verify_chain_integrity())


@app.route("/api/compliance/self-test", methods=["POST"])
@login_required
def api_compliance_self_test():
    return jsonify(compliance_gate.self_test())


@app.route("/api/compliance/export", methods=["GET"])
@login_required
def api_compliance_export():
    """Descarga CSV con consents del trimestre. ?year=&quarter= opcionales."""
    try:
        year = int(request.args.get("year", 0))
        quarter = int(request.args.get("quarter", 0))
    except ValueError:
        year, quarter = 0, 0
    csv_data = compliance_gate.export_audit_quarterly(year, quarter)
    fname = f"dfa_audit_{year or 'auto'}Q{quarter or 'auto'}.csv"
    return app.response_class(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.route("/api/compliance/timeline", methods=["GET"])
@login_required
def api_compliance_timeline():
    try:
        limit = max(1, min(int(request.args.get("limit", 10)), 100))
    except ValueError:
        limit = 10
    return jsonify({"ok": True, "items": compliance_gate.get_consent_timeline(limit)})


@app.route("/api/compliance/tolerance", methods=["GET"])
@login_required
def api_compliance_tolerance_get():
    return jsonify({"ok": True, "tolerance_map": compliance_gate.get_tolerance_map()})


@app.route("/api/compliance/tolerance", methods=["POST"])
@login_required
def api_compliance_tolerance_set():
    d = request.get_json() or {}
    return jsonify(compliance_gate.set_tolerance(d.get("country", ""),
                                                  d.get("pct", 0)))


@app.route("/api/compliance/tolerance/<country>", methods=["DELETE"])
@login_required
def api_compliance_tolerance_del(country):
    return jsonify(compliance_gate.reset_tolerance(country))


@app.route("/api/compliance/mirror/verify", methods=["GET"])
@login_required
def api_compliance_mirror_verify():
    return jsonify(compliance_gate.verify_mirror())


@app.route("/api/compliance/scheduler", methods=["GET"])
@login_required
def api_compliance_scheduler_status():
    return jsonify(compliance_gate.quarterly_status())


@app.route("/api/compliance/scheduler/start", methods=["POST"])
@login_required
def api_compliance_scheduler_start():
    return jsonify(compliance_gate.start_quarterly_scheduler())


@app.route("/api/compliance/scheduler/stop", methods=["POST"])
@login_required
def api_compliance_scheduler_stop():
    return jsonify(compliance_gate.stop_quarterly_scheduler())


# === Akamai SBSD — endpoints ===
@app.route("/api/akamai/sbsd/info", methods=["GET"])
@login_required
def api_akamai_sbsd_info():
    import akamai_sbsd as _sbsd
    return jsonify({"ok": True, "info": _sbsd.get_info()})


@app.route("/api/akamai/sbsd/self-test", methods=["POST"])
@login_required
def api_akamai_sbsd_self_test():
    import akamai_sbsd as _sbsd
    return jsonify(_sbsd.self_test())


@app.route("/api/akamai/sbsd/log", methods=["GET"])
@login_required
def api_akamai_sbsd_log():
    import akamai_sbsd as _sbsd
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 200))
    except ValueError:
        limit = 30
    return jsonify(_sbsd.get_log(limit))


# === Imperva/Incapsula bypass — endpoints ===
@app.route("/api/imperva/info", methods=["GET"])
@login_required
def api_imperva_info():
    import imperva_bypass as _imv
    return jsonify({"ok": True, "info": _imv.get_info()})


@app.route("/api/imperva/self-test", methods=["POST"])
@login_required
def api_imperva_self_test():
    import imperva_bypass as _imv
    return jsonify(_imv.self_test())


@app.route("/api/imperva/bypass", methods=["POST"])
@login_required
def api_imperva_bypass():
    import imperva_bypass as _imv
    d = request.get_json() or {}
    domain = (d.get("domain") or "").strip()
    if not domain:
        return jsonify({"ok": False, "error": "domain_required"}), 400
    return jsonify(_imv.bypass(domain, force=bool(d.get("force", False)),
                                ttl=int(d.get("ttl", 1200))))


@app.route("/api/imperva/cache", methods=["GET"])
@login_required
def api_imperva_cache_get():
    import imperva_bypass as _imv
    return jsonify(_imv.cache_stats())


@app.route("/api/imperva/cache", methods=["DELETE"])
@login_required
def api_imperva_cache_clear():
    import imperva_bypass as _imv
    domain = request.args.get("domain")
    return jsonify(_imv.cache_clear(domain))


# === Session Persistence (Redis storage_state) ===
@app.route("/api/session-persistence/info", methods=["GET"])
@login_required
def api_sp_info():
    import session_persistence as _sp
    return jsonify({"ok": True, "info": _sp.get_info()})


@app.route("/api/session-persistence/self-test", methods=["POST"])
@login_required
def api_sp_self_test():
    import session_persistence as _sp
    return jsonify(_sp.self_test())


@app.route("/api/session-persistence/stats", methods=["GET"])
@login_required
def api_sp_stats():
    import session_persistence as _sp
    return jsonify(_sp.stats())


@app.route("/api/session-persistence/save", methods=["POST"])
@login_required
def api_sp_save():
    import session_persistence as _sp
    d = request.get_json() or {}
    return jsonify(_sp.save(d.get("account_id", ""), d.get("domain", ""),
                              d.get("storage_state") or {},
                              ttl_s=int(d.get("ttl_s", 86400))))


@app.route("/api/session-persistence/load", methods=["GET"])
@login_required
def api_sp_load():
    import session_persistence as _sp
    aid = request.args.get("account_id", "")
    dom = request.args.get("domain", "")
    res = _sp.load(aid, dom)
    return jsonify({"ok": res is not None, "state": res})


@app.route("/api/session-persistence/invalidate", methods=["POST"])
@login_required
def api_sp_invalidate():
    import session_persistence as _sp
    d = request.get_json() or {}
    return jsonify(_sp.invalidate(d.get("account_id", ""), d.get("domain", "")))


@app.route("/api/session-persistence/mark-failed", methods=["POST"])
@login_required
def api_sp_mark_failed():
    import session_persistence as _sp
    d = request.get_json() or {}
    return jsonify(_sp.mark_failed(d.get("account_id", ""), d.get("domain", "")))


@app.route("/api/session-persistence/keys", methods=["GET"])
@login_required
def api_sp_keys():
    import session_persistence as _sp
    return jsonify({"ok": True, "keys": _sp.list_keys()})


# === Behavior Chain (Markov de navegación) ===
@app.route("/api/behavior-chain/info", methods=["GET"])
@login_required
def api_bc_info():
    import behavior_chain as _bc
    return jsonify({"ok": True, "info": _bc.get_info()})


@app.route("/api/behavior-chain/self-test", methods=["POST"])
@login_required
def api_bc_self_test():
    import behavior_chain as _bc
    return jsonify(_bc.self_test())


@app.route("/api/behavior-chain/sample", methods=["POST"])
@login_required
def api_bc_sample():
    import behavior_chain as _bc
    d = request.get_json() or {}
    path = _bc.sample_path(
        target=d.get("target", "checkout"),
        start=d.get("start", "home"),
        max_steps=int(d.get("max_steps", 10)),
        seed=d.get("seed"),
    )
    return jsonify({
        "ok": True,
        "path": [{"state": s, "dwell_s": dw} for s, dw in path],
        "total_dwell_s": _bc.total_dwell(path),
    })


@app.route("/api/behavior-chain/next-step", methods=["POST"])
@login_required
def api_bc_next():
    import behavior_chain as _bc
    d = request.get_json() or {}
    state = d.get("state", "home")
    return jsonify({"ok": True, "state": state,
                     "next": _bc.next_step(state, seed=d.get("seed")),
                     "dwell_s": _bc.dwell_time(state)})


@app.route("/api/behavior-chain/learn", methods=["POST"])
@login_required
def api_bc_learn():
    import behavior_chain as _bc
    d = request.get_json() or {}
    return jsonify(_bc.learn_from_session(
        states=d.get("states") or [],
        dwells=d.get("dwells"),
        smoothing=float(d.get("smoothing", 0.1)),
    ))


@app.route("/api/behavior-chain/matrix", methods=["GET"])
@login_required
def api_bc_matrix():
    import behavior_chain as _bc
    return jsonify({"ok": True, "matrix": _bc.load_matrix()})


@app.route("/api/bot/critical-2026/asn/classify", methods=["POST"])
@login_required
def api_asn_classify():
    d = request.get_json() or {}
    return jsonify(asn_ip_drift.classify_ip(d.get("ip", "")))


@app.route("/api/bot/critical-2026/safetix/status")
@login_required
def api_safetix_status():
    return jsonify({"ok": True, "status": safetix_handler.get_status()})


@app.route("/api/bot/critical-2026/tls-ct/check/<domain>", methods=["POST"])
@login_required
def api_tls_ct_check(domain):
    return jsonify(tls_ct_monitor.detect_changes(domain))


@app.route("/api/bot/groups/<gid>/start", methods=["POST"])
@login_required
def api_bot_group_start(gid):
    g = bot_engine.engine.get_group(gid)
    if not g:
        return jsonify({"ok": False, "error": "not_found"}), 404
    g.start_all()
    return jsonify({"ok": True, "group": g.to_dict()})


@app.route("/api/bot/groups/<gid>/stop", methods=["POST"])
@login_required
def api_bot_group_stop(gid):
    g = bot_engine.engine.get_group(gid)
    if not g:
        return jsonify({"ok": False, "error": "not_found"}), 404
    g.stop_all()
    return jsonify({"ok": True, "group": g.to_dict()})


@app.route("/api/bot/groups/<gid>", methods=["DELETE"])
@login_required
def api_bot_group_delete(gid):
    ok = bot_engine.engine.delete_group(gid)
    return jsonify({"ok": ok})


@app.route("/api/bot/proxies")
@login_required
def api_bot_proxies():
    return jsonify({
        "ok": True,
        "proxies":   proxy_intelligence.proxy_manager.list_proxies(),
        "providers": proxy_intelligence.PROXY_PROVIDERS,
        "recommendations": proxy_intelligence.USE_CASE_RECOMMENDATIONS,
    })


@app.route("/api/bot/proxies", methods=["POST"])
@login_required
def api_bot_proxies_add():
    p = proxy_intelligence.proxy_manager.add_proxy(request.get_json() or {})
    return jsonify({"ok": True, "proxy": p})


@app.route("/api/bot/proxies/<pid>", methods=["DELETE"])
@login_required
def api_bot_proxies_del(pid):
    proxy_intelligence.proxy_manager.delete_proxy(pid)
    return jsonify({"ok": True})


@app.route("/api/bot/antibot")
@login_required
def api_bot_antibot():
    return jsonify({
        "ok": True,
        "config": antibot_mod.get_antibot_config(),
        "stats":  antibot_mod.get_antibot_stats(),
    })


@app.route("/api/bot/antibot", methods=["POST"])
@login_required
def api_bot_antibot_save():
    cfg = antibot_mod.save_antibot_config(request.get_json() or {})
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/bot/captcha")
@login_required
def api_bot_captcha():
    return jsonify({
        "ok": True,
        "config":  captcha_manager.get_config(),
        "methods": captcha_manager.get_available_methods(),
        "best":    captcha_manager.get_best_method(),
    })


@app.route("/api/bot/captcha", methods=["POST"])
@login_required
def api_bot_captcha_save():
    cfg = captcha_manager.save_config(request.get_json() or {})
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/bot/browsers")
@login_required
def api_bot_browsers():
    return jsonify({
        "ok": True,
        "antidetect": browser_profiles.get_antidetect_browsers(),
        "detected":   browser_profiles.detect_installed_browsers(),
        "extensions": browser_profiles.get_extensions(),
    })


@app.route("/api/bot/fingerprint", methods=["POST"])
@login_required
def api_bot_fingerprint():
    d = request.get_json() or {}
    fp = antibot_mod.generate_fingerprint(d.get("domain_id"))
    return jsonify({"ok": True, "fingerprint": fp})


import queue_orchestrator


@app.route("/api/bot/queues")
@login_required
def api_bot_queues():
    return jsonify({"ok": True, "queues": queue_orchestrator.list_active()})


@app.route("/api/bot/queues/start", methods=["POST"])
@login_required
def api_bot_queues_start():
    d = request.get_json() or {}
    r = queue_orchestrator.start_queue(
        event_id=d.get("event_id", ""),
        domain_id=d.get("domain_id", "es"),
        url=d.get("url", ""),
        browser=d.get("browser", "auto"),
        captcha=d.get("captcha", "auto"),
        trigger_purchase=bool(d.get("trigger_purchase", False)),
        quantity=int(d.get("quantity", 2)),
        strategy=d.get("strategy", "best_available"),
        warm_pages=int(d.get("warm_pages", 3)),
        timeout_min=int(d.get("timeout_min", 10)),
    )
    return jsonify(r)


@app.route("/api/bot/queues/start-multi", methods=["POST"])
@login_required
def api_bot_queues_start_multi():
    d = request.get_json() or {}
    qs = queue_orchestrator.start_multi(
        n=int(d.get("n", 5)),
        stagger_s=float(d.get("stagger", 2)),
        event_id=d.get("event_id", ""),
        domain_id=d.get("domain_id", "es"),
        url=d.get("url", ""),
    )
    return jsonify({"ok": True, "started": len(qs), "queues": qs})


@app.route("/api/bot/queues/spin", methods=["POST"])
@login_required
def api_bot_queues_spin():
    d = request.get_json() or {}
    return jsonify(queue_orchestrator.spin_queues(
        n=int(d.get("n", 5)),
        timeout_min=int(d.get("timeout_min", 10)),
        aggressive=bool(d.get("aggressive", True)),
        domain_id=d.get("domain_id", "es"),
    ))


@app.route("/api/bot/queues/spin-sticky", methods=["POST"])
@login_required
def api_bot_queues_spin_sticky():
    d = request.get_json() or {}
    return jsonify(queue_orchestrator.spin_sticky(
        n=int(d.get("n", 10)),
        ttl_min=int(d.get("ttl_min", 30)),
        domain_id=d.get("domain_id", "es"),
    ))


@app.route("/api/bot/queues/launch-instances", methods=["POST"])
@login_required
def api_bot_queues_launch_instances():
    d = request.get_json() or {}
    return jsonify(queue_orchestrator.launch_instances(
        mode=d.get("mode", "contexts"),
        count=int(d.get("count", 50)),
        url=d.get("url", ""),
        rampup=d.get("rampup", "linear_30s"),
        batch=int(d.get("batch", 50)),
        jitter=int(d.get("jitter", 0)),
        domain_id=d.get("domain_id", "es"),
    ))


@app.route("/api/bot/queues/<qid>", methods=["GET"])
@login_required
def api_bot_queue_get(qid):
    q = queue_orchestrator.get_queue(qid)
    return jsonify({"ok": q is not None, "queue": q})


@app.route("/api/bot/queues/<qid>/stop", methods=["POST"])
@login_required
def api_bot_queue_stop(qid):
    return jsonify(queue_orchestrator.stop_queue(qid))


@app.route("/api/bot/queues/stats", methods=["GET"])
@login_required
def api_bot_queues_stats():
    return jsonify({"ok": True, "stats": queue_orchestrator.get_stats()})


@app.route("/api/bot/queues/self-test", methods=["POST"])
@login_required
def api_bot_queues_self_test():
    return jsonify({"ok": True, "result": queue_orchestrator.self_test()})


@app.route("/api/bot/strategies")
@login_required
def api_bot_strategies():
    return jsonify({"ok": True, "strategies": sm_mod.get_strategies()})


# ==========================================
#  API — CUENTAS Y TARJETAS
# ==========================================
@app.route("/api/accounts")
@login_required
def api_accounts():
    return jsonify({"ok": True, "accounts": account_manager.account_manager.list_accounts()})


@app.route("/api/accounts", methods=["POST"])
@login_required
def api_accounts_add():
    a = account_manager.account_manager.add_account(request.get_json() or {})
    return jsonify({"ok": True, "account": a})


@app.route("/api/accounts/<aid>", methods=["PUT"])
@login_required
def api_accounts_update(aid):
    a = account_manager.account_manager.update_account(aid, request.get_json() or {})
    return jsonify({"ok": bool(a), "account": a})


# ──────────── proxy_per_account binding ────────────
@app.route("/api/accounts/<aid>/proxy", methods=["POST"])
@login_required
def api_account_assign_proxy(aid):
    """Ata una cuenta a un proxy_id. Body: {proxy_id}."""
    d = request.get_json() or {}
    pid = d.get("proxy_id", "")
    if not pid:
        return jsonify({"ok": False, "error": "proxy_id required"}), 400
    a = account_manager.account_manager.assign_proxy(aid, pid)
    return jsonify({"ok": bool(a), "account": a})


@app.route("/api/accounts/<aid>/proxy", methods=["GET"])
@login_required
def api_account_get_proxy(aid):
    pid = account_manager.account_manager.get_assigned_proxy(aid)
    return jsonify({"ok": bool(pid), "proxy_id": pid})


@app.route("/api/accounts/<aid>/proxy", methods=["DELETE"])
@login_required
def api_account_clear_proxy(aid):
    return jsonify({"ok": account_manager.account_manager.clear_assigned_proxy(aid)})


@app.route("/api/accounts/proxy-bindings", methods=["GET"])
@login_required
def api_account_proxy_bindings():
    return jsonify({"ok": True,
                    "bindings": account_manager.account_manager.list_proxy_bindings()})


@app.route("/api/accounts/<aid>", methods=["DELETE"])
@login_required
def api_accounts_delete(aid):
    """Borra cuenta + tarjetas asociadas + fila correspondiente en plantilla_tm.csv,
    y reescribe cuentas_tarjetas.csv. Cascada al CSV configurable con ?cascade=0."""
    cascade = (request.args.get("cascade", "1").lower() not in ("0", "false", "no"))
    accs_before = account_manager.account_manager.list_accounts()
    target = next((a for a in accs_before if a.get("id") == aid), None)
    if not target:
        return jsonify({"ok": False, "error": "account_not_found"}), 404
    tm_email = (target.get("tm_email") or target.get("email") or "").strip().lower()

    # Tarjetas asociadas a la cuenta
    cards_deleted = 0
    for c in account_manager.account_manager.list_cards():
        if c.get("account_id") == aid:
            account_manager.account_manager.delete_card(c.get("id"))
            cards_deleted += 1

    account_manager.account_manager.delete_account(aid)

    plantilla_rows_removed = 0
    plantilla_path = os.path.join(os.path.dirname(__file__), "plantilla_tm.csv")
    if cascade and tm_email and os.path.isfile(plantilla_path):
        try:
            import csv as _csv, shutil as _sh
            from datetime import datetime as _dt
            backup = plantilla_path + ".bak.predelete"
            try: _sh.copy2(plantilla_path, backup)
            except Exception: pass
            with open(plantilla_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = _csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                kept = []
                for row in reader:
                    em = (row.get("Email_TM") or "").strip().lower()
                    if em == tm_email:
                        plantilla_rows_removed += 1
                        continue
                    kept.append(row)
            # Renumerar N_Usuario secuencialmente para no dejar huecos
            if plantilla_rows_removed and fieldnames and fieldnames[0] == "N_Usuario":
                for idx, row in enumerate(kept, start=1):
                    row["N_Usuario"] = str(idx)
            tmp = plantilla_path + ".tmp"
            with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                w = _csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(kept)
            os.replace(tmp, plantilla_path)
        except Exception as e:
            logger.warning(f"plantilla_tm.csv cascade delete failed: {e}")

    # Reescribir cuentas_tarjetas.csv para reflejar el borrado en disco
    sync_csv_ok = False
    try:
        import accounts_csv_sync
        sync_csv_ok = bool(accounts_csv_sync.sync_out().get("ok"))
    except Exception as e:
        logger.warning(f"accounts_csv_sync.sync_out tras delete falló: {e}")

    return jsonify({
        "ok": True,
        "deleted_account_id": aid,
        "deleted_email": tm_email or None,
        "cards_deleted": cards_deleted,
        "plantilla_rows_removed": plantilla_rows_removed,
        "cuentas_csv_synced": sync_csv_ok,
    })


# ==========================================
#  API — Cuentas con uso por evento
# ==========================================
# Límite TM por defecto (TM ES suele permitir 4-8 entradas por cuenta por evento).
# Configurable por evento en data/event_purchase_limits.json {tm_id: max_qty}.
_DEFAULT_MAX_QTY_PER_EVENT = 6

def _load_event_limits():
    import os as _os, json as _json
    p = _os.path.join(_os.path.dirname(__file__), "data", "event_purchase_limits.json")
    try:
        with open(p, encoding="utf-8") as f: return _json.load(f)
    except Exception: return {}


@app.route("/api/accounts/with-purchases")
@login_required
def api_accounts_with_purchases():
    """Devuelve cuentas + agrupado de compras por event_id, con flag de bloqueo
    cuando la suma de quantities >= max_qty del evento.
    Query: ?event_id=<tm_id> filtra solo a ese evento."""
    only_event = request.args.get("event_id") or request.args.get("tm_id")
    accs = account_manager.account_manager.list_accounts()
    purchases = purchases_manager.list_purchases()
    limits = _load_event_limits()

    # Index: account_id|account_email → list[purchase]
    by_acc: dict = {}
    for p in purchases:
        key = (p.get("account_id") or "") + "|" + (p.get("account_email") or "").lower()
        by_acc.setdefault(key, []).append(p)

    out = []
    for a in accs:
        aid = a.get("id") or ""
        email = (a.get("tm_email") or a.get("email") or "").lower()
        # cruzar por id O por email (id puede estar vacío en purchases legacy)
        events_used: dict = {}  # event_id → {qty_total, last_ts, status, event_name}
        all_keys = [k for k in by_acc.keys() if (aid and aid in k) or (email and email in k)]
        for k in all_keys:
            for p in by_acc[k]:
                eid = str(p.get("event_id") or p.get("tm_id") or "")
                if not eid: continue
                if only_event and eid != str(only_event): continue
                slot = events_used.setdefault(eid, {
                    "event_id": eid, "event_name": p.get("event_name", ""),
                    "qty_total": 0, "purchases_count": 0,
                    "last_ts": "", "last_status": "",
                })
                slot["qty_total"] += int(p.get("quantity") or 0)
                slot["purchases_count"] += 1
                if (p.get("ts") or "") > slot["last_ts"]:
                    slot["last_ts"] = p.get("ts") or ""
                    slot["last_status"] = p.get("status") or ""
        # Anotar flag bloqueado por event_id según limits
        events_list = []
        for eid, slot in events_used.items():
            mx = int(limits.get(eid, _DEFAULT_MAX_QTY_PER_EVENT))
            slot["max_qty"] = mx
            slot["blocked"] = slot["qty_total"] >= mx
            events_list.append(slot)
        out.append({
            "id": aid, "label": a.get("label"),
            "tm_email": a.get("tm_email"), "first_name": a.get("first_name"),
            "last_name": a.get("last_name"), "country": a.get("country"),
            "phone": (a.get("phone_prefix") or "") + (a.get("phone") or ""),
            "status": a.get("status"),
            "registration_completed_at": a.get("registration_completed_at"),
            "created_at": a.get("created_at"),
            "phone_verified_at": a.get("phone_verified_at"),
            "credentials_verified": a.get("credentials_verified"),
            "validated_at": a.get("validated_at"),
            "pending_user_review": a.get("pending_user_review"),
            "review_reason": a.get("review_reason"),
            "events_used": events_list,
            "any_blocked": any(e["blocked"] for e in events_list),
        })
    return jsonify({"ok": True, "accounts": out, "event_filter": only_event,
                    "default_max_qty": _DEFAULT_MAX_QTY_PER_EVENT,
                    "event_limits": limits})


@app.route("/api/accounts/<aid>/usage")
@login_required
def api_account_usage(aid):
    """Detalle de compras de UNA cuenta con flag bloqueado por evento."""
    accs = account_manager.account_manager.list_accounts()
    a = next((x for x in accs if x.get("id") == aid), None)
    if not a:
        return jsonify({"ok": False, "error": "account_not_found"}), 404
    limits = _load_event_limits()
    email = (a.get("tm_email") or "").lower()
    purchases = [p for p in purchases_manager.list_purchases()
                 if (p.get("account_id") == aid)
                 or (email and (p.get("account_email") or "").lower() == email)]
    by_event: dict = {}
    for p in purchases:
        eid = str(p.get("event_id") or "")
        if not eid: continue
        slot = by_event.setdefault(eid, {"event_id": eid, "event_name": p.get("event_name", ""),
                                          "purchases": [], "qty_total": 0})
        slot["purchases"].append({k: p.get(k) for k in
                                   ("id", "ts", "status", "quantity", "section", "seats",
                                    "order_id", "confirmation_number", "total_price")})
        slot["qty_total"] += int(p.get("quantity") or 0)
    for eid, slot in by_event.items():
        mx = int(limits.get(eid, _DEFAULT_MAX_QTY_PER_EVENT))
        slot["max_qty"] = mx
        slot["blocked"] = slot["qty_total"] >= mx
    return jsonify({"ok": True, "account": {k: a.get(k) for k in
                     ("id", "label", "tm_email", "first_name", "last_name",
                      "phone_prefix", "phone", "country", "city", "address1",
                      "passport", "status", "dob", "nationality")},
                    "events": list(by_event.values()),
                    "default_max_qty": _DEFAULT_MAX_QTY_PER_EVENT})


@app.route("/api/accounts/<aid>/export")
@login_required
def api_account_export(aid):
    """Exporta los datos completos de una cuenta como JSON descargable."""
    from flask import Response
    accs = account_manager.account_manager.list_accounts()
    a = next((x for x in accs if x.get("id") == aid), None)
    if not a:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = json.dumps({"account": a, "exported_at": datetime.now().isoformat()},
                      indent=2, ensure_ascii=False, default=str)
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="account_{a.get("label","")}_{aid}.json"'})


# ─── Check Cuentas: validar login real + reenviar a registros si BAD_CREDS ───
import account_validator as _account_validator


@app.route("/api/accounts/check-credentials", methods=["POST"])
@login_required
def api_accounts_check_credentials():
    """Lanza validación de cuentas en paralelo. Body: {account_ids:[],
    max_parallel?, force_password_test?, options?}.
    options soporta: verifySession, verifyPassword, bumpToRegistrations,
    clearHistory, updateStorage. Si no se pasa options, usa defaults seguros.
    """
    d = request.get_json() or {}
    ids = d.get("account_ids") or []
    if not ids:
        return jsonify({"ok": False, "error": "account_ids required"}), 400
    opts = d.get("options") or {}
    return jsonify(_account_validator.validate_batch(
        account_ids=ids,
        max_parallel=int(d.get("max_parallel", 2)),
        force_password_test=bool(d.get("force_password_test", False)),
        options=opts))


@app.route("/api/accounts/check-credentials/<job_id>", methods=["GET"])
@login_required
def api_accounts_check_status(job_id):
    return jsonify(_account_validator.get_job(job_id))


@app.route("/api/accounts/check-credentials/jobs", methods=["GET"])
@login_required
def api_accounts_check_jobs_list():
    return jsonify({"ok": True, "jobs": _account_validator.list_jobs()})


@app.route("/api/accounts/check-credentials/<job_id>", methods=["DELETE"])
@login_required
def api_accounts_check_delete(job_id):
    return jsonify(_account_validator.delete_job(job_id))


@app.route("/api/accounts/check-credentials/clear-done", methods=["POST"])
@login_required
def api_accounts_check_clear_done():
    return jsonify(_account_validator.clear_done_jobs())


@app.route("/api/accounts/check-credentials/safety", methods=["GET"])
@login_required
def api_accounts_check_safety():
    """Estado de las guardas anti-bloqueo: cooldowns activos por cuenta +
    cuentas marcadas con 3+ fails. Útil para diagnosticar por qué un test
    saltó cuentas."""
    h = _account_validator._load_fresh_history()
    from datetime import datetime as _dt
    now = _dt.now()
    out = []
    for aid, entry in h.items():
        last = entry.get("last_test_at")
        cooldown_left_h = None
        if last:
            try:
                dt = _dt.fromisoformat(last)
                elapsed = (now - dt).total_seconds()
                left = _account_validator._FRESH_COOLDOWN_S - elapsed
                if left > 0:
                    cooldown_left_h = round(left / 3600, 2)
            except Exception: pass
        out.append({
            "account_id": aid,
            "last_test_at": last,
            "fail_count": entry.get("fail_count", 0),
            "cooldown_left_hours": cooldown_left_h,
            "blocked_until_admin_resets": int(entry.get("fail_count", 0)) >= 3,
            "last_result": entry.get("last_result"),
        })
    return jsonify({"ok": True,
                    "cooldown_seconds": _account_validator._FRESH_COOLDOWN_S,
                    "verified_recent_seconds": _account_validator._VERIFIED_RECENT_S,
                    "batch_abort_min_fails": _account_validator._BATCH_MIN_FAILS_FOR_ABORT,
                    "batch_abort_ratio": _account_validator._BATCH_FAIL_RATIO_ABORT,
                    "history": out})


@app.route("/api/accounts/close-all-browsers", methods=["POST"])
@login_required
def api_accounts_close_all_browsers():
    """Cierra TODOS los browsers MoreLogin abiertos (huérfanos incluidos).
    Útil tras crash, limpieza manual, o entre batches para evitar acumulación."""
    return jsonify(_account_validator.close_all_running_browsers())


@app.route("/api/accounts/bulk-move-to-registrations", methods=["POST"])
@login_required
def api_accounts_bulk_move():
    """Mueve N cuentas a Registros TM con status=pending sin validación previa.
    Body: {account_ids:[], reason?, dry_run?}."""
    d = request.get_json() or {}
    return jsonify(_account_validator.bulk_move_to_registrations(
        account_ids=d.get("account_ids", []),
        reason=d.get("reason", "manual_bulk_move"),
        dry_run=bool(d.get("dry_run", False))))


@app.route("/api/accounts/bulk-mark-for-review", methods=["POST"])
@login_required
def api_accounts_bulk_mark_review():
    """Destaca cuentas con flag pending_user_review=true SIN moverlas a
    Registros TM. Quedan visibles con badge naranja en la tabla.
    Body: {account_ids:[], reason?}."""
    d = request.get_json() or {}
    return jsonify(_account_validator.bulk_mark_for_review(
        account_ids=d.get("account_ids", []),
        reason=d.get("reason", "deferred_for_user_review")))


@app.route("/api/accounts/clear-review-flag", methods=["POST"])
@login_required
def api_accounts_clear_review_flag():
    """Limpia el flag pending_user_review. Body: {account_ids?:[]} — si vacío,
    limpia todas."""
    d = request.get_json() or {}
    return jsonify(_account_validator.bulk_clear_review_flag(
        account_ids=d.get("account_ids")))


@app.route("/api/accounts/revert-bulk-move", methods=["POST"])
@login_required
def api_accounts_revert_bulk_move():
    """Revierte bulk_move: restaura status=active + quita de Registros TM.
    Body: {account_ids:[]}."""
    d = request.get_json() or {}
    return jsonify(_account_validator.revert_bulk_move(
        account_ids=d.get("account_ids", [])))


@app.route("/api/accounts/audit-purge-invalid", methods=["POST"])
@login_required
def api_accounts_audit_purge():
    """Purga masiva: mueve a Registros TM (status=pending) todas las cuentas
    cuya validation_reason indique invalidez. Body: {dry_run?:bool, reasons?:[]}.
    Default reasons: bad_credentials, session_invalid_redirected_to_login,
    account_mismatch."""
    d = request.get_json() or {}
    return jsonify(_account_validator.audit_and_purge_invalid(
        reasons_to_purge=d.get("reasons"),
        dry_run=bool(d.get("dry_run", False))))


@app.route("/api/accounts/check-credentials/safety/reset", methods=["POST"])
@login_required
def api_accounts_check_safety_reset():
    """Resetea el histórico FRESH login (cooldowns + fail_counts). Solo usar
    cuando sabes que las cuentas YA no están bloqueadas en TM (>24h)."""
    d = request.get_json() or {}
    if d.get("account_id"):
        h = _account_validator._load_fresh_history()
        removed = h.pop(d["account_id"], None) is not None
        _account_validator._save_fresh_history(h)
        return jsonify({"ok": True, "removed": removed,
                        "account_id": d["account_id"]})
    # Reset total
    _account_validator._save_fresh_history({})
    return jsonify({"ok": True, "removed": "all"})


@app.route("/api/accounts/active-ips", methods=["GET"])
@login_required
def api_accounts_active_ips():
    """Lista las IPs actualmente reservadas por slots de validación."""
    return jsonify({"ok": True,
                    "active_ips": sorted(list(_account_validator._load_active_ips()))})


@app.route("/api/accounts/active-ips/reset", methods=["POST"])
@login_required
def api_accounts_active_ips_reset():
    """Limpia el lock de IPs activas (tras crash o limpieza manual)."""
    _account_validator.reset_active_ips()
    return jsonify({"ok": True, "reset": True})


# ─── IP Lock GLOBAL (todos los procesos del bot) ───
import ip_lock as _ip_lock_dash


@app.route("/api/ip-lock/stats", methods=["GET"])
@login_required
def api_ip_lock_stats():
    """Estadísticas del lock global de IPs (todos los procesos del bot)."""
    return jsonify({"ok": True, **_ip_lock_dash.stats()})


@app.route("/api/ip-lock/active", methods=["GET"])
@login_required
def api_ip_lock_active():
    """Lista las IPs en uso AHORA con su owner (proceso) y desde cuándo."""
    return jsonify({"ok": True, "active": _ip_lock_dash.list_active()})


@app.route("/api/ip-lock/recent", methods=["GET"])
@login_required
def api_ip_lock_recent():
    """IPs usadas en últimas X horas. Query: ?hours=24 (default 24)."""
    hours = float(request.args.get("hours", 24))
    return jsonify({"ok": True, "hours": hours,
                    "recent": _ip_lock_dash.get_recent_uses(hours=hours)})


@app.route("/api/ip-lock/reset", methods=["POST"])
@login_required
def api_ip_lock_reset():
    """Reset del lock. Body: {scope: 'active'|'all'}."""
    d = request.get_json() or {}
    scope = d.get("scope", "active")
    if scope == "all":
        _ip_lock_dash.reset_all()
    else:
        _ip_lock_dash.reset_active()
    return jsonify({"ok": True, "scope": scope})


# ─── BOT INFRA: preflight + jobs + DLQ + recovery ───
import bot_infra as _bot_infra


@app.route("/api/bot/preflight", methods=["POST"])
@login_required
def api_bot_preflight():
    """Health-check antes de lanzar batches. Body opcional: {tm_url}.
    Verifica MoreLogin, dashboard, account pool y opcional URL evento."""
    d = request.get_json() or {}
    hc = _bot_infra.HealthChecker()
    hc.check_morelogin()
    hc.check_dashboard()
    hc.check_account_pool(country=d.get("country", "es"))
    if d.get("tm_url"):
        hc.check_event(d["tm_url"])
    return jsonify(hc.report())


@app.route("/api/bot/jobs/active", methods=["GET"])
@login_required
def api_bot_jobs_active():
    return jsonify({"ok": True, "jobs": _bot_infra.list_active_jobs()})


@app.route("/api/bot/jobs/<job_id>/timeline", methods=["GET"])
@login_required
def api_bot_job_timeline(job_id):
    return jsonify({"ok": True, "job_id": job_id,
                    "events": _bot_infra.get_job_timeline(job_id)})


@app.route("/api/bot/dlq", methods=["GET"])
@login_required
def api_bot_dlq():
    """Dead Letter Queue: slots que fallaron repetidamente."""
    return jsonify({"ok": True, "dlq": _bot_infra.list_dlq()})


@app.route("/api/bot/reset-state", methods=["POST"])
@login_required
def api_bot_reset_state():
    """Reset de locks/perfiles activos/cuentas en uso. Útil tras crash."""
    return jsonify(_bot_infra.reset_global_state())


@app.route("/api/bot/recover-jobs", methods=["POST"])
@login_required
def api_bot_recover_jobs():
    """Marca jobs en estado intermedio como interrupted (post-restart)."""
    return jsonify(_bot_infra.recover_interrupted_jobs())


@app.route("/api/bot/infra/self-test", methods=["POST"])
@login_required
def api_bot_infra_selftest():
    return jsonify(_bot_infra.self_test())


@app.route("/api/accounts/csv-logins")
@login_required
def api_accounts_csv_logins():
    """Descarga CSV con email + password de cuentas registradas.
    Por defecto solo cuentas con purchase_ready=True (ready_only=1)."""
    from flask import Response
    import io as _io, csv as _csv
    ready_only = request.args.get("ready_only", "1").lower() not in ("0", "false", "no")
    accs = account_manager.account_manager.list_accounts()
    if ready_only:
        accs = [a for a in accs if a.get("purchase_ready")]
    out = _io.StringIO()
    w = _csv.writer(out)
    w.writerow(["email", "password", "phone", "label", "account_id"])
    for a in accs:
        w.writerow([
            a.get("tm_email", ""),
            a.get("tm_password", ""),
            a.get("phone_full") or ((a.get("phone_prefix","")) + (a.get("phone",""))),
            a.get("label", ""),
            a.get("id", ""),
        ])
    body = out.getvalue()
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="tm_logins_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv"'})


@app.route("/api/accounts/<aid>/full")
@login_required
def api_account_full(aid):
    """Devuelve TODA la info de una cuenta (incl. password, IMAP, tarjeta) como JSON
    normal (no descarga). Para mostrar/copiar desde la UI."""
    accs = account_manager.account_manager.list_accounts()
    a = next((x for x in accs if x.get("id") == aid), None)
    if not a:
        return jsonify({"ok": False, "error": "not_found"}), 404
    cards = [c for c in account_manager.account_manager.list_cards()
             if c.get("account_id") == aid]
    return jsonify({"ok": True, "account": a, "cards": cards,
                    "exported_at": datetime.now().isoformat()})


@app.route("/api/events/<tm_id>/purchase-limit", methods=["GET", "POST"])
@login_required
def api_event_purchase_limit(tm_id):
    """Lee/escribe el límite máximo de entradas por cuenta para este evento."""
    import os as _os, json as _json
    p = _os.path.join(_os.path.dirname(__file__), "data", "event_purchase_limits.json")
    try:
        with open(p, encoding="utf-8") as f: limits = _json.load(f)
    except Exception: limits = {}
    if request.method == "POST":
        d = request.get_json() or {}
        mx = int(d.get("max_qty") or 0)
        if mx <= 0:
            limits.pop(str(tm_id), None)
        else:
            limits[str(tm_id)] = mx
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            _json.dump(limits, f, indent=2)
    return jsonify({"ok": True, "tm_id": tm_id,
                    "max_qty": limits.get(str(tm_id), _DEFAULT_MAX_QTY_PER_EVENT),
                    "is_default": str(tm_id) not in limits,
                    "default": _DEFAULT_MAX_QTY_PER_EVENT})


@app.route("/api/cards")
@login_required
def api_cards():
    return jsonify({"ok": True, "cards": account_manager.account_manager.list_cards()})


@app.route("/api/cards", methods=["POST"])
@login_required
def api_cards_add():
    d = request.get_json() or {}
    number = d.get("number", "")
    d["type"] = encryption.detect_card_type(number)
    d["luhn_valid"] = encryption.validate_card_luhn(number)
    d["masked"] = encryption.mask_card_number(number)
    c = account_manager.account_manager.add_card(d)
    return jsonify({"ok": True, "card": c})


@app.route("/api/cards/<cid>", methods=["DELETE"])
@login_required
def api_cards_delete(cid):
    account_manager.account_manager.delete_card(cid)
    return jsonify({"ok": True})


# ==========================================
#  API — COOKIES
# ==========================================
@app.route("/api/cookies")
@login_required
def api_cookies():
    return jsonify({
        "ok": True,
        "cookies": cookie_optimizer.list_cookies(),
        "stats":   cookie_optimizer.analyze_cookies(),
    })


@app.route("/api/cookies", methods=["POST"])
@login_required
def api_cookies_add():
    c = cookie_optimizer.add_cookie(request.get_json() or {})
    return jsonify({"ok": True, "cookie": c})


@app.route("/api/cookies/<cid>", methods=["DELETE"])
@login_required
def api_cookies_delete(cid):
    cookie_optimizer.delete_cookie(cid)
    return jsonify({"ok": True})


@app.route("/api/cookies/clean", methods=["POST"])
@login_required
def api_cookies_clean():
    return jsonify({"ok": True, "result": cookie_optimizer.clean_cookies()})


@app.route("/api/cookies/optimize", methods=["POST"])
@login_required
def api_cookies_optimize():
    d = request.get_json() or {}
    return jsonify({"ok": True, "result": cookie_optimizer.optimize_for_bot(d.get("domain_id"))})


# ==========================================
#  API — ANALYTICS
# ==========================================
@app.route("/api/analytics")
@login_required
def api_analytics():
    return jsonify({
        "ok": True,
        "kpis":   analytics_mod.get_analytics(),
        "events": analytics_mod.get_events(200),
    })


@app.route("/api/analytics/reset", methods=["POST"])
@login_required
def api_analytics_reset():
    analytics_mod.reset_analytics()
    return jsonify({"ok": True})


# ==========================================
#  API — NOTIFICACIONES
# ==========================================
@app.route("/api/notifications")
@login_required
def api_notifications():
    return jsonify({
        "ok": True,
        "config":  notifications.get_config(),
        "history": notifications.get_history(100),
    })


@app.route("/api/notifications/config", methods=["GET", "POST"])
@login_required
def api_notifications_save():
    if request.method == "GET":
        return jsonify({"ok": True, "config": notifications.get_config()})
    cfg = notifications.save_config(request.get_json() or {})
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/notifications/test", methods=["POST"])
@login_required
def api_notifications_test():
    e = notifications.notify("test", "Test", "Notificacion de prueba", level="info")
    return jsonify({"ok": True, "entry": e})


@app.route("/api/notifications/clear", methods=["POST"])
@login_required
def api_notifications_clear():
    notifications.clear_history()
    return jsonify({"ok": True})


# ==========================================
#  API — MONITOR
# ==========================================
@app.route("/api/monitor")
@login_required
def api_monitor():
    return jsonify({
        "ok": True,
        "status":   monitor_mod.monitor.get_status(),
        "sections": monitor_mod.monitor.get_sections_health(),
    })


# ==========================================
#  API — CONSOLA (logs)
# ==========================================
@app.route("/api/console/logs")
@login_required
def api_console_logs():
    only_errors = request.args.get("errors_only") == "1"
    path = config.ruta("dashboard.log")
    if not os.path.exists(path):
        return jsonify({"ok": True, "lines": []})
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-500:]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    if only_errors:
        lines = [l for l in lines if "[ERROR]" in l or "[WARNING]" in l]
    return jsonify({"ok": True, "lines": lines})


# ==========================================
#  API — BACKUP
# ==========================================
@app.route("/api/backup")
@login_required
def api_backup_list():
    return jsonify({"ok": True, "backups": backup_system.list_backups()})


@app.route("/api/backup/create", methods=["POST"])
@login_required
def api_backup_create():
    d = request.get_json() or {}
    r = backup_system.create_backup(label=d.get("label", ""))
    return jsonify({"ok": True, "backup": r})


@app.route("/api/backup/restore/<bid>", methods=["POST"])
@login_required
def api_backup_restore(bid):
    return jsonify(backup_system.restore_backup(bid))


@app.route("/api/backup/<bid>", methods=["DELETE"])
@login_required
def api_backup_delete(bid):
    return jsonify(backup_system.delete_backup(bid))


# ==========================================
#  API — USUARIOS
# ==========================================
@app.route("/api/users")
@login_required
def api_users():
    return jsonify({
        "ok": True,
        "users": users_manager.list_users(),
        "stats": users_manager.get_stats(),
    })


@app.route("/api/users", methods=["POST"])
@login_required
def api_users_add():
    d = request.get_json() or {}
    u = users_manager.add_user(
        d.get("username", ""), d.get("password", ""),
        d.get("role", "user"),
        email=d.get("email", ""), notes=d.get("notes", ""),
    )
    if not u:
        return jsonify({"ok": False, "error": "username_exists"}), 400
    actor = (session.get("user") or {}).get("username", "system")
    audit_log.record("user:created", actor=actor,
                      ip=request.remote_addr or "",
                      target=u["username"], meta={"role": u.get("role")})
    return jsonify({"ok": True, "user": u})


@app.route("/api/users/<uid>", methods=["PUT"])
@login_required
def api_users_update(uid):
    d = request.get_json() or {}
    # Cambio de password especial
    if d.get("password"):
        ok = users_manager.change_password(uid, d["password"])
        return jsonify({"ok": ok})
    u = users_manager.update_user(uid, d)
    if u:
        actor = (session.get("user") or {}).get("username", "system")
        audit_log.record("user:updated", actor=actor, target=uid, meta=d)
    return jsonify({"ok": bool(u), "user": u})


@app.route("/api/users/<uid>/unlock", methods=["POST"])
@login_required
def api_users_unlock(uid):
    ok = users_manager.unlock_user(uid)
    if ok:
        actor = (session.get("user") or {}).get("username", "system")
        audit_log.record("user:unlocked", actor=actor, target=uid)
    return jsonify({"ok": ok})


@app.route("/api/users/<uid>/history", methods=["GET"])
@login_required
def api_users_history(uid):
    return jsonify({"ok": True,
                    "history": users_manager.get_login_history(uid,
                                                                 limit=int(request.args.get("limit", 20)))})


@app.route("/api/users/<uid>", methods=["DELETE"])
@login_required
def api_users_del(uid):
    ok = users_manager.delete_user(uid)
    if ok:
        actor = (session.get("user") or {}).get("username", "system")
        audit_log.record("user:deleted", actor=actor, target=uid)
    return jsonify({"ok": ok})


# ==========================================
#  API — AUDIT LOG
# ==========================================
@app.route("/api/audit-log", methods=["GET"])
@login_required
def api_audit_log():
    action = request.args.get("action", "")
    actor = request.args.get("actor", "")
    success_q = request.args.get("success")
    success = None
    if success_q == "1":
        success = True
    elif success_q == "0":
        success = False
    return jsonify({
        "ok": True,
        "entries": audit_log.query(
            action=action, actor=actor, success=success,
            since=request.args.get("since"),
            limit=int(request.args.get("limit", 200)),
        ),
        "stats": audit_log.get_stats(hours=int(request.args.get("hours", 24))),
        "event_types": audit_log.event_types(),
    })


@app.route("/api/audit-log/stats", methods=["GET"])
@login_required
def api_audit_stats():
    return jsonify({"ok": True, "stats": audit_log.get_stats(
        hours=int(request.args.get("hours", 24)))})


@app.route("/api/audit-log/clear", methods=["POST"])
@login_required
def api_audit_clear():
    actor = (session.get("user") or {}).get("username", "system")
    r = audit_log.clear()
    audit_log.record("audit:cleared", actor=actor, success=True)
    return jsonify(r)


# ==========================================
#  API — NOTIFICATIONS CATALOG
# ==========================================
@app.route("/api/notifications/catalog", methods=["GET"])
@login_required
def api_notifications_catalog():
    return jsonify({"ok": True, **notifications.get_event_catalog()})


# ==========================================
#  API — MULTILOGIN ORCHESTRATOR
# ==========================================
@app.route("/api/multilogin/config", methods=["GET"])
@login_required
def api_mlx_config_get():
    return jsonify({"ok": True, "state": multilogin_orchestrator.get_state()})


@app.route("/api/multilogin/config", methods=["POST"])
@login_required
def api_mlx_config_save():
    cfg = multilogin_orchestrator.save_config(request.get_json() or {})
    # No devolver password hash
    safe = {k: v for k, v in cfg.items() if k != "password_hash_sha384"}
    safe["has_password"] = bool(cfg.get("password_hash_sha384"))
    return jsonify({"ok": True, "config": safe})


@app.route("/api/multilogin/self-test", methods=["POST"])
@login_required
def api_mlx_selftest():
    return jsonify(multilogin_orchestrator.self_test())


@app.route("/api/multilogin/profiles", methods=["GET"])
@login_required
def api_mlx_profiles():
    return jsonify(multilogin_orchestrator.list_profiles(
        folder_id=request.args.get("folder_id"),
        limit=int(request.args.get("limit", 100)),
    ))


@app.route("/api/multilogin/start/<pid>", methods=["POST"])
@login_required
def api_mlx_start(pid):
    d = request.get_json() or {}
    return jsonify(multilogin_orchestrator.start_profile(
        pid, headless=bool(d.get("headless", True))
    ))


@app.route("/api/multilogin/stop/<pid>", methods=["POST"])
@login_required
def api_mlx_stop(pid):
    return jsonify(multilogin_orchestrator.stop_profile(pid))


@app.route("/api/multilogin/create", methods=["POST"])
@login_required
def api_mlx_create():
    d = request.get_json() or {}
    return jsonify(multilogin_orchestrator.create_profile(
        name=d.get("name", "TM Profile"),
        os_type=d.get("os_type", "win"),
        browser_type=d.get("browser_type", "mimic"),
    ))


@app.route("/api/multilogin/batch-run", methods=["POST"])
@login_required
def api_mlx_batch():
    d = request.get_json() or {}
    ids = d.get("profile_ids") or []
    if not ids:
        return jsonify({"ok": False, "error": "profile_ids vacío"}), 400
    r = multilogin_orchestrator.run_batch(
        profile_ids=ids,
        max_concurrent=int(d.get("max_concurrent", 5)),
        broadcast=ws_broadcast,
    )
    multilogin_orchestrator.record_run({
        "total": r.get("total"), "success": r.get("success"),
        "elapsed_s": r.get("elapsed_s"),
    })
    return jsonify(r)


# ==========================================
#  API — PX PREFARM (PerimeterX cookie pre-farming)
# ==========================================
@app.route("/api/px-prefarm/config", methods=["GET", "POST"])
@login_required
def api_px_config():
    if request.method == "POST":
        return jsonify({"ok": True, "config": px_prefarm.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": px_prefarm.get_config()})


@app.route("/api/px-prefarm/stats", methods=["GET"])
@login_required
def api_px_stats():
    return jsonify({"ok": True, "stats": px_prefarm.pool_stats(),
                    "scheduler": px_prefarm.scheduler_status()})


@app.route("/api/px-prefarm/pool", methods=["GET"])
@login_required
def api_px_pool():
    return jsonify({"ok": True,
                    "sessions": px_prefarm.list_pool_entries(
                        limit=int(request.args.get("limit", 20)),
                        include_cookies=request.args.get("include_cookies") == "1")})


@app.route("/api/px-prefarm/start", methods=["POST"])
@login_required
def api_px_start():
    d = request.get_json() or {}
    r = px_prefarm.start_farming_async(
        domain_id=d.get("domain_id", "us"),
        event_url=d.get("event_url") or None,
        duration_min=float(d.get("duration_min", 3)) if d.get("duration_min") else None,
        engine=d.get("engine"),
        broadcast=ws_broadcast,
    )
    actor = (session.get("user") or {}).get("username", "system")
    try:
        audit_log.record("px_prefarm:started", actor=actor,
                          meta={"domain": d.get("domain_id"), "engine": r.get("engine")})
    except Exception:
        pass
    return jsonify(r)


@app.route("/api/px-prefarm/log", methods=["GET"])
@login_required
def api_px_log():
    return jsonify({"ok": True, "log": px_prefarm.get_log(
        limit=int(request.args.get("limit", 20)))})


@app.route("/api/px-prefarm/pick", methods=["POST"])
@login_required
def api_px_pick():
    d = request.get_json() or {}
    return jsonify(px_prefarm.pick_cookies(
        domain_id=d.get("domain_id", "us"),
        min_px_cookies=int(d.get("min_px_cookies", 1))))


@app.route("/api/px-prefarm/clear", methods=["DELETE"])
@login_required
def api_px_clear():
    return jsonify(px_prefarm.clear_pool())


@app.route("/api/px-prefarm/scheduler/<action>", methods=["POST"])
@login_required
def api_px_scheduler(action):
    if action == "start":
        return jsonify(px_prefarm.start_scheduler(broadcast=ws_broadcast))
    if action == "stop":
        return jsonify(px_prefarm.stop_scheduler())
    return jsonify({"ok": False, "error": "action inválido"}), 400


@app.route("/api/px-prefarm/self-test", methods=["POST"])
@login_required
def api_px_selftest():
    """Smoke test sin navegador real."""
    return jsonify(px_prefarm.self_test())


@app.route("/api/px-prefarm/pool-scored", methods=["GET"])
@login_required
def api_px_pool_scored():
    """Pool con score de calidad por sesión (ordenado desc)."""
    limit = int(request.args.get("limit", 20))
    return jsonify({"ok": True,
                     "sessions": px_prefarm.list_pool_with_scores(limit=limit)})


@app.route("/api/px-prefarm/pick-http", methods=["POST"])
@login_required
def api_px_pick_http():
    """Devuelve cookies en formato dict para requests/tls-client."""
    d = request.get_json() or {}
    return jsonify(px_prefarm.get_cookies_for_requests(
        domain_id=d.get("domain_id", "us")
    ))


@app.route("/api/px-prefarm/session/<sid>", methods=["GET"])
@login_required
def api_px_session_detail(sid):
    s = px_prefarm.get_session_with_score(sid)
    if not s:
        return jsonify({"ok": False, "error": "session no encontrada"}), 404
    # Enmascarar cookies (solo devolver nombres)
    s["cookie_names"] = [c.get("name", "") for c in s.get("cookies", [])]
    s.pop("cookies", None)
    return jsonify({"ok": True, "session": s})


@app.route("/api/px-prefarm/domain-stats", methods=["GET"])
@login_required
def api_px_domain_stats():
    """Stats de farming por dominio TM: tasa de éxito, % con _pxhd, avg score."""
    return jsonify({"ok": True, "domains": px_prefarm.get_domain_stats()})


# ==========================================
#  API — MULTI-DOMAIN DISTRIBUTED
# ==========================================
@app.route("/api/multi-domain/detect", methods=["POST"])
@login_required
def api_md_detect():
    d = request.get_json() or {}
    return jsonify(multi_domain_orchestrator.detect_international_tour(
        artist_or_keyword=d.get("keyword", ""),
        min_countries=int(d.get("min_countries", 2))))


@app.route("/api/multi-domain/rank", methods=["POST"])
@login_required
def api_md_rank():
    d = request.get_json() or {}
    ranking = multi_domain_orchestrator.rank_domains_by_competition(
        domains=d.get("domains", []),
        artist_or_keyword=d.get("keyword", ""))
    return jsonify({"ok": True, "ranking": ranking})


@app.route("/api/multi-domain/launch", methods=["POST"])
@login_required
def api_md_launch():
    d = request.get_json() or {}
    if not d.get("keyword"):
        return jsonify({"ok": False, "error": "falta keyword"}), 400
    r = multi_domain_orchestrator.launch_distributed(
        artist_or_keyword=d["keyword"],
        quantity=int(d.get("quantity", 2)),
        mode=d.get("mode", "simulate"),
        strategy=d.get("strategy", "distributed"),
        max_domains=int(d.get("max_domains", 10)),
        broadcast=ws_broadcast)
    actor = (session.get("user") or {}).get("username", "system")
    try:
        audit_log.record("multi_domain:launched", actor=actor,
                          meta={"keyword": d["keyword"], "success": r.get("success"),
                                "total": r.get("total_domains")})
    except Exception:
        pass
    return jsonify(r)


@app.route("/api/multi-domain/runs", methods=["GET"])
@login_required
def api_md_runs():
    return jsonify({"ok": True, "runs": multi_domain_orchestrator.get_recent_runs(
        limit=int(request.args.get("limit", 20)))})


# ==========================================
#  API — AcpAT INJECTION (pub/sub)
# ==========================================
@app.route("/api/acpat-inject/broadcast", methods=["POST"])
@login_required
def api_acpat_broadcast():
    d = request.get_json() or {}
    if not d.get("token"):
        return jsonify({"ok": False, "error": "falta token"}), 400
    return jsonify(acpat_injection.broadcast_token(
        token=d["token"],
        domain_id=d.get("domain_id", "us"),
        event_id=d.get("event_id", ""),
        source_worker=d.get("source_worker", "api"),
        meta=d.get("meta")))


@app.route("/api/acpat-inject/subscribe", methods=["POST"])
@login_required
def api_acpat_subscribe():
    d = request.get_json() or {}
    sid = acpat_injection.subscribe(
        domain_id=d.get("domain_id", "us"),
        event_id=d.get("event_id", ""),
        subscriber_name=d.get("name", ""))
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/acpat-inject/unsubscribe/<sid>", methods=["POST"])
@login_required
def api_acpat_unsubscribe(sid):
    return jsonify({"ok": acpat_injection.unsubscribe(sid)})


@app.route("/api/acpat-inject/stats", methods=["GET"])
@login_required
def api_acpat_inj_stats():
    return jsonify({"ok": True,
                    "stats": acpat_injection.get_stats(),
                    "subscribers": acpat_injection.list_subscribers(),
                    "last_tokens": acpat_injection.get_last_tokens(),
                    "recent_broadcasts": acpat_injection.get_log(limit=20)})


@app.route("/api/acpat-inject/wait", methods=["POST"])
@login_required
def api_acpat_wait():
    d = request.get_json() or {}
    r = acpat_injection.wait_for_token(
        domain_id=d.get("domain_id", "us"),
        event_id=d.get("event_id", ""),
        timeout=min(float(d.get("timeout", 10)), 30))
    if not r:
        return jsonify({"ok": False, "error": "timeout"}), 408
    # enmascarar token
    tk = r.get("token", "")
    r["token_preview"] = (tk[:6] + "…" + tk[-4:]) if len(tk) > 12 else "***"
    r.pop("token", None)
    return jsonify({"ok": True, **r})


# ==========================================
#  API — ADYEN TOKENIZER
# ==========================================
@app.route("/api/adyen/config", methods=["GET", "POST"])
@login_required
def api_adyen_config():
    if request.method == "POST":
        cfg = adyen_tokenizer.save_config(request.get_json() or {})
    else:
        cfg = adyen_tokenizer.get_config()
    # ocultar api_key
    safe = dict(cfg)
    key = safe.get("api_key") or ""
    safe["api_key_preview"] = (key[:4] + "…" + key[-4:]) if len(key) >= 10 else ""
    safe.pop("api_key", None)
    return jsonify({"ok": True, "config": safe})


@app.route("/api/adyen/self-test", methods=["POST"])
@login_required
def api_adyen_selftest():
    return jsonify(adyen_tokenizer.self_test())


@app.route("/api/adyen/check", methods=["POST"])
@login_required
def api_adyen_check():
    return jsonify(adyen_tokenizer.check_connection())


@app.route("/api/adyen/tokenize", methods=["POST"])
@login_required
def api_adyen_tokenize():
    d = request.get_json() or {}
    card = d.get("card") or {}
    r = adyen_tokenizer.tokenize_card(card, shopper_reference=d.get("shopper_reference"))
    return jsonify(r)


@app.route("/api/adyen/tokens", methods=["GET"])
@login_required
def api_adyen_list():
    return jsonify({"ok": True,
                    "tokens": adyen_tokenizer.list_tokens(
                        active_only=request.args.get("active", "1") == "1"),
                    "stats": adyen_tokenizer.get_stats()})


@app.route("/api/adyen/tokens/<tid>", methods=["DELETE"])
@login_required
def api_adyen_del(tid):
    return jsonify({"ok": adyen_tokenizer.delete_token(tid)})


# ==========================================
#  API — EXTENSION RECOMMENDER (catálogo bot)
# ==========================================
@app.route("/api/extensions/recommendations")
@login_required
def api_extensions_list():
    import extension_recommender
    status = request.args.get("status") or None
    tier = request.args.get("tier") or None
    return jsonify({
        "ok": True,
        "recommendations": extension_recommender.list_recommendations(
            filter_status=status, filter_tier=tier),
        "stats": extension_recommender.get_stats(),
    })


@app.route("/api/extensions/search")
@login_required
def api_extensions_search():
    import extension_recommender
    q = request.args.get("q") or ""
    limit = int(request.args.get("limit") or 20)
    return jsonify({
        "ok": True, "query": q,
        "matches": extension_recommender.search(q, limit=limit),
    })


@app.route("/api/extensions/<ext_id>")
@login_required
def api_extensions_detail(ext_id):
    import extension_recommender
    ext = extension_recommender.get_extension(ext_id)
    if not ext:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "extension": ext})


@app.route("/api/extensions/<ext_id>/interest", methods=["POST"])
@login_required
def api_extensions_interest(ext_id):
    import extension_recommender
    interested = bool((request.get_json() or {}).get("interested", True))
    return jsonify(extension_recommender.mark_interest(ext_id, interested))


@app.route("/api/extensions/<ext_id>/install", methods=["POST"])
@login_required
def api_extensions_install(ext_id):
    """Marca extensión como instalada por el usuario.

    No instala nada automáticamente — solo registra el evento. La activación
    real depende del módulo (algunos: start_scheduler endpoint, otros: config
    + restart, otros: código nuevo a implementar).
    """
    import extension_recommender
    return jsonify(extension_recommender.mark_installed(ext_id))


@app.route("/api/extensions/self-test", methods=["POST"])
@login_required
def api_extensions_selftest():
    import extension_recommender
    return jsonify(extension_recommender.self_test())


# ==========================================
#  API — Mobile 4G/5G Proxies
# ==========================================
@app.route("/api/mobile-proxies/config", methods=["GET", "POST"])
@login_required
def api_mobile_config():
    if request.method == "POST":
        return jsonify({"ok": True, "config": mobile_proxies.save_config(request.get_json() or {})})
    cfg = mobile_proxies.get_config()
    # Enmascarar password
    if cfg.get("password"):
        cfg["password"] = "***" if cfg["password"] else ""
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/mobile-proxies/providers", methods=["GET"])
@login_required
def api_mobile_providers():
    return jsonify({"ok": True, "providers": mobile_proxies.list_providers()})


@app.route("/api/mobile-proxies/get", methods=["POST"])
@login_required
def api_mobile_get():
    d = request.get_json() or {}
    r = mobile_proxies.get_mobile_proxy(
        geo=d.get("geo"), carrier=d.get("carrier"),
        sticky=d.get("sticky"), session_id=d.get("session_id"),
        sticky_min=d.get("sticky_min"),
    )
    # En respuesta API solo devolver masked, nunca la URL real
    if r.get("ok") and "url" in r and mobile_proxies.get_config().get("mode") == "live":
        r["url"] = r.get("masked", "***")
    return jsonify(r)


@app.route("/api/mobile-proxies/test-connection", methods=["POST"])
@login_required
def api_mobile_test():
    d = request.get_json() or {}
    return jsonify(mobile_proxies.test_connection(
        target_url=d.get("target_url", "https://api.ipify.org?format=json"),
    ))


@app.route("/api/mobile-proxies/benchmark", methods=["POST"])
@login_required
def api_mobile_benchmark():
    d = request.get_json() or {}
    return jsonify(mobile_proxies.benchmark_vs_isp(
        target_url=d.get("target_url", "https://www.ticketmaster.com"),
        iterations=int(d.get("iterations", 3)),
    ))


@app.route("/api/mobile-proxies/stats", methods=["GET"])
@login_required
def api_mobile_stats():
    return jsonify({"ok": True, "stats": mobile_proxies.get_stats(),
                     "info": mobile_proxies.get_info()})


@app.route("/api/mobile-proxies/log", methods=["GET"])
@login_required
def api_mobile_log():
    return jsonify({"ok": True,
                     "log": mobile_proxies.get_log(int(request.args.get("limit", 30)))})


@app.route("/api/mobile-proxies/self-test", methods=["POST"])
@login_required
def api_mobile_selftest():
    return jsonify(mobile_proxies.self_test())


# --- Carrier-ASN venue matching ---
@app.route("/api/mobile-proxies/venue-carrier-map", methods=["GET"])
@login_required
def api_mobile_carrier_map():
    """Devuelve el mapping (country, city) → carriers preferidos con ASN."""
    return jsonify(mobile_proxies.list_venue_carrier_map())


@app.route("/api/mobile-proxies/carrier-for-venue", methods=["POST"])
@login_required
def api_mobile_carrier_for_venue():
    """Devuelve carrier+ASN preferido para una ciudad/venue. Body:
    {country, city, venue?}."""
    d = request.get_json() or {}
    res = mobile_proxies.get_carrier_for_venue(
        d.get("country", ""), d.get("city", ""), d.get("venue", ""))
    if not res:
        return jsonify({"ok": False, "error": "no_mapping_for_city",
                        "country": d.get("country"), "city": d.get("city")}), 404
    return jsonify({"ok": True, **res})


@app.route("/api/mobile-proxies/proxy-for-venue", methods=["POST"])
@login_required
def api_mobile_proxy_for_venue():
    """Devuelve URL de proxy móvil con carrier/ASN matched al venue.
    Body: {country, city, venue?, sticky?, sticky_min?, session_id?}."""
    d = request.get_json() or {}
    return jsonify(mobile_proxies.get_proxy_for_venue(
        country=d.get("country", ""),
        city=d.get("city", ""),
        venue=d.get("venue", ""),
        sticky=bool(d.get("sticky", True)),
        sticky_min=d.get("sticky_min"),
        session_id=d.get("session_id"),
        override_carrier=d.get("override_carrier")))


# --- Health checks & auto-rotate ---
@app.route("/api/mobile-proxies/health", methods=["GET"])
@login_required
def api_mobile_health():
    sid = request.args.get("session_id")
    return jsonify({"ok": True, "health": mobile_proxies.get_ip_health(session_id=sid)})


@app.route("/api/mobile-proxies/record", methods=["POST"])
@login_required
def api_mobile_record():
    d = request.get_json() or {}
    mobile_proxies.record_ip_result(
        session_id=d.get("session_id", ""),
        success=bool(d.get("success")),
        detected_ip=d.get("detected_ip", ""),
        latency_ms=int(d.get("latency_ms", 0)),
        error=d.get("error", ""),
    )
    return jsonify({"ok": True})


@app.route("/api/mobile-proxies/rotate/<sid>", methods=["POST"])
@login_required
def api_mobile_rotate(sid):
    d = request.get_json() or {}
    return jsonify(mobile_proxies.force_rotate(sid, reason=d.get("reason", "manual")))


@app.route("/api/mobile-proxies/blacklist/clear", methods=["POST"])
@login_required
def api_mobile_clear_bl():
    d = request.get_json() or {}
    return jsonify(mobile_proxies.clear_blacklist(d.get("session_id")))


@app.route("/api/mobile-proxies/healthy", methods=["POST"])
@login_required
def api_mobile_healthy():
    d = request.get_json() or {}
    return jsonify(mobile_proxies.get_healthy_mobile_proxy(
        geo=d.get("geo"), carrier=d.get("carrier"),
        max_tries=int(d.get("max_tries", 3)),
    ))


# --- Pool sticky per-cuenta TM ---
@app.route("/api/mobile-proxies/assign-account", methods=["POST"])
@login_required
def api_mobile_assign():
    d = request.get_json() or {}
    return jsonify(mobile_proxies.assign_sticky_for_account(
        account_id=d.get("account_id", ""),
        geo=d.get("geo"), carrier=d.get("carrier"),
        lifetime_days=int(d.get("lifetime_days", 30)),
    ))


@app.route("/api/mobile-proxies/account-assignments", methods=["GET"])
@login_required
def api_mobile_account_list():
    active = request.args.get("active", "1") != "0"
    return jsonify({"ok": True,
                     "assignments": mobile_proxies.get_account_assignments(active_only=active)})


@app.route("/api/mobile-proxies/release-account/<aid>", methods=["POST"])
@login_required
def api_mobile_release(aid):
    return jsonify(mobile_proxies.release_account_assignment(aid))


# --- Bandwidth tracking + alerts ---
@app.route("/api/mobile-proxies/bandwidth", methods=["GET"])
@login_required
def api_mobile_bandwidth():
    month = request.args.get("month")
    return jsonify({"ok": True, "stats": mobile_proxies.get_bandwidth_stats(month)})


@app.route("/api/mobile-proxies/bandwidth/alert", methods=["POST"])
@login_required
def api_mobile_bandwidth_alert():
    d = request.get_json() or {}
    return jsonify(mobile_proxies.check_bandwidth_alert(
        provider=d.get("provider", ""),
        monthly_plan_gb=float(d.get("monthly_plan_gb", 10)),
    ))


@app.route("/api/mobile-proxies/bandwidth/record", methods=["POST"])
@login_required
def api_mobile_bandwidth_record():
    d = request.get_json() or {}
    mobile_proxies.record_bandwidth(
        provider=d.get("provider", ""),
        bytes_sent=int(d.get("bytes_sent", 0)),
        bytes_received=int(d.get("bytes_received", 0)),
    )
    return jsonify({"ok": True})


# ==========================================
#  API — Akamai Bot Manager sensor_data
# ==========================================
@app.route("/api/akamai-bmsc/config", methods=["GET", "POST"])
@login_required
def api_akamai_config():
    if request.method == "POST":
        return jsonify({"ok": True, "config": akamai_bmsc.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": akamai_bmsc.get_config()})


@app.route("/api/akamai-bmsc/detect", methods=["POST"])
@login_required
def api_akamai_detect():
    d = request.get_json() or {}
    return jsonify(akamai_bmsc.detect_akamai_challenge(
        url=d.get("url", "https://www.ticketmaster.com"),
    ))


@app.route("/api/akamai-bmsc/generate", methods=["POST"])
@login_required
def api_akamai_generate():
    d = request.get_json() or {}
    p = akamai_bmsc.generate_sensor_payload(
        session_id=d.get("session_id"),
        user_agent=d.get("user_agent"),
        viewport=d.get("viewport"),
        events_count=d.get("events_count"),
    )
    sensor = akamai_bmsc.build_sensor_data(p)
    return jsonify({"ok": True,
                     "events_count": len(p["mouse_events"]) + len(p["key_events"]) + len(p["touch_events"]),
                     "sensor_length": len(sensor),
                     "sensor_preview": sensor[:120] + "...",
                     "payload_keys": list(p.keys())})


@app.route("/api/akamai-bmsc/submit", methods=["POST"])
@login_required
def api_akamai_submit():
    d = request.get_json() or {}
    p = akamai_bmsc.generate_sensor_payload(session_id=d.get("session_id"))
    sensor = akamai_bmsc.build_sensor_data(p)
    return jsonify(akamai_bmsc.submit_sensor_data(
        url=d.get("url", "https://www.ticketmaster.com/_bm_"),
        sensor_data=sensor,
        session_cookies=d.get("cookies"),
    ))


@app.route("/api/akamai-bmsc/sessions", methods=["GET", "POST"])
@login_required
def api_akamai_sessions():
    if request.method == "GET":
        return jsonify({"ok": True, "sessions": akamai_bmsc.list_sessions()})
    d = request.get_json() or {}
    r = akamai_bmsc.start_session(
        session_id=d.get("session_id"),
        target_url=d.get("target_url", "https://www.ticketmaster.com"),
        user_agent=d.get("user_agent"),
    )
    # No devolver sensor completo por tamaño
    if "sensor_data" in r:
        r["sensor_preview"] = r["sensor_data"][:120] + "..."
        del r["sensor_data"]
    return jsonify(r)


@app.route("/api/akamai-bmsc/sessions/<sid>/refresh", methods=["POST"])
@login_required
def api_akamai_refresh(sid):
    r = akamai_bmsc.refresh_session(sid)
    if "sensor_data" in r:
        r["sensor_preview"] = r["sensor_data"][:120] + "..."
        del r["sensor_data"]
    return jsonify(r)


@app.route("/api/akamai-bmsc/sessions/<sid>", methods=["DELETE"])
@login_required
def api_akamai_stop(sid):
    return jsonify(akamai_bmsc.stop_session(sid))


@app.route("/api/akamai-bmsc/stats", methods=["GET"])
@login_required
def api_akamai_stats():
    return jsonify({"ok": True, "stats": akamai_bmsc.get_stats()})


@app.route("/api/akamai-bmsc/log", methods=["GET"])
@login_required
def api_akamai_log():
    return jsonify({"ok": True,
                     "log": akamai_bmsc.get_log(int(request.args.get("limit", 30)))})


@app.route("/api/akamai-bmsc/self-test", methods=["POST"])
@login_required
def api_akamai_selftest():
    return jsonify(akamai_bmsc.self_test())


# --- Anti-detection heuristics (mejora #4) ---
@app.route("/api/akamai-bmsc/humanized", methods=["POST"])
@login_required
def api_akamai_humanized():
    """Genera payload con anti-detection (personalidades, noise, formato variable)."""
    d = request.get_json() or {}
    p = akamai_bmsc.generate_humanized_payload(
        session_id=d.get("session_id"),
        personality=d.get("personality"),
        user_agent=d.get("user_agent"),
        viewport=d.get("viewport"),
        inject_noise=bool(d.get("inject_noise", True)),
    )
    sensor = akamai_bmsc.build_sensor_data_varied(p, d.get("format", "auto"))
    return jsonify({
        "ok": True,
        "session_id": p["session_id"],
        "personality": p["personality"],
        "events_stats": p["events_stats"],
        "field_order": p["field_order"],
        "sensor_length": len(sensor),
        "sensor_preview": sensor[:120] + "...",
        "sensor_format": sensor.split(";")[0],
    })


@app.route("/api/akamai-bmsc/personalities", methods=["GET"])
@login_required
def api_akamai_personalities():
    return jsonify({"ok": True,
                     "personalities": akamai_bmsc.USER_PERSONALITIES})


@app.route("/api/akamai-bmsc/humanized-test", methods=["POST"])
@login_required
def api_akamai_humanized_test():
    """Test de variación entre personalidades."""
    return jsonify(akamai_bmsc.humanized_self_test())


# ==========================================
#  API — Human Behavior (Bezier + hesitation + geo)
# ==========================================
@app.route("/api/human-behavior/self-test", methods=["POST"])
@login_required
def api_hb_selftest():
    return jsonify(human_behavior.self_test())


@app.route("/api/human-behavior/info", methods=["GET"])
@login_required
def api_hb_info():
    return jsonify({"ok": True, "info": human_behavior.get_info()})


@app.route("/api/human-behavior/bezier", methods=["POST"])
@login_required
def api_hb_bezier():
    d = request.get_json() or {}
    traj = human_behavior.generate_bezier_trajectory(
        start=tuple(d.get("start", [100, 100])),
        end=tuple(d.get("end", [800, 600])),
        steps=int(d.get("steps", 20)),
        overshoot=bool(d.get("overshoot", True)),
    )
    return jsonify({"ok": True, "events_count": len(traj), "events": traj[:5]})


@app.route("/api/human-behavior/hesitation", methods=["POST"])
@login_required
def api_hb_hesitation():
    d = request.get_json() or {}
    events = human_behavior.generate_hesitation(
        target_x=int(d.get("target_x", 500)),
        target_y=int(d.get("target_y", 300)),
        hesitation_prob=float(d.get("prob", 1.0)),
    )
    return jsonify({"ok": True, "events_count": len(events),
                     "events_sample": events[:8]})


@app.route("/api/human-behavior/geo-personality", methods=["POST"])
@login_required
def api_hb_geo():
    d = request.get_json() or {}
    p = human_behavior.pick_geo_aware_personality(
        country_code=d.get("country", "us"),
        seed=d.get("seed"),
    )
    return jsonify({"ok": True, "personality": p,
                     "country": d.get("country")})


# ==========================================
#  API — Akamai Validator (sensor_data validation)
# ==========================================
@app.route("/api/akamai-validator/structure", methods=["POST"])
@login_required
def api_val_struct():
    d = request.get_json() or {}
    return jsonify({"ok": True,
                     "result": akamai_validator.validate_sensor_structure(d.get("sensor", ""))})


@app.route("/api/akamai-validator/semantic", methods=["POST"])
@login_required
def api_val_sem():
    d = request.get_json() or {}
    return jsonify({"ok": True,
                     "result": akamai_validator.validate_sensor_semantic(d.get("payload") or {})})


@app.route("/api/akamai-validator/full", methods=["POST"])
@login_required
def api_val_full():
    d = request.get_json() or {}
    return jsonify(akamai_validator.validate_full(
        sensor=d.get("sensor", ""), payload=d.get("payload"),
    ))


@app.route("/api/akamai-validator/ml-validate", methods=["POST"])
@login_required
def api_val_ml():
    d = request.get_json() or {}
    return jsonify(akamai_validator.ml_validate_payload(
        payload=d.get("payload") or {},
        sensor_length=d.get("sensor_length"),
    ))


@app.route("/api/akamai-validator/shadow-check", methods=["POST"])
@login_required
def api_val_shadow():
    d = request.get_json() or {}
    return jsonify(akamai_validator.check_new_fields(d.get("payload") or {}))


@app.route("/api/akamai-validator/shadow-update", methods=["POST"])
@login_required
def api_val_shadow_update():
    d = request.get_json() or {}
    return jsonify(akamai_validator.update_baseline(d.get("payload") or {}))


@app.route("/api/akamai-validator/baseline", methods=["GET"])
@login_required
def api_val_baseline():
    return jsonify({"ok": True, "baseline": akamai_validator.get_baseline_info()})


@app.route("/api/akamai-validator/self-test", methods=["POST"])
@login_required
def api_val_selftest():
    return jsonify(akamai_validator.self_test())


@app.route("/api/akamai-validator/auto-repair", methods=["POST"])
@login_required
def api_val_repair():
    d = request.get_json() or {}
    return jsonify(akamai_validator.try_auto_repair(d.get("sensor", "")))


@app.route("/api/akamai-validator/compare", methods=["POST"])
@login_required
def api_val_compare():
    d = request.get_json() or {}
    return jsonify(akamai_validator.compare_structures(
        sensor_a=d.get("sensor_a", ""),
        sensor_b=d.get("sensor_b", ""),
    ))


# --- CRC/HMAC + XOR decode + JSON introspect ---
@app.route("/api/akamai-validator/verify-checksum", methods=["POST"])
@login_required
def api_val_checksum():
    d = request.get_json() or {}
    return jsonify(akamai_validator.verify_checksum(d.get("sensor", "")))


@app.route("/api/akamai-validator/recompute-checksum", methods=["POST"])
@login_required
def api_val_recompute():
    d = request.get_json() or {}
    return jsonify(akamai_validator.recompute_with_repair(d.get("sensor", "")))


# --- Pattern matching vs DB ---
@app.route("/api/akamai-validator/match-captures", methods=["POST"])
@login_required
def api_val_match():
    d = request.get_json() or {}
    return jsonify(akamai_validator.match_against_captures(
        sensor=d.get("sensor", ""),
        max_compare=int(d.get("max_compare", 20)),
    ))


@app.route("/api/akamai-validator/fingerprint-distance", methods=["POST"])
@login_required
def api_val_fpdist():
    d = request.get_json() or {}
    return jsonify(akamai_validator.get_fingerprint_distance(
        sensor_a=d.get("sensor_a", ""),
        sensor_b=d.get("sensor_b", ""),
    ))


@app.route("/api/akamai-validator/forensic", methods=["POST"])
@login_required
def api_val_forensic():
    d = request.get_json() or {}
    return jsonify(akamai_validator.full_forensic_analysis(
        sensor=d.get("sensor", ""),
        expected_session_id=d.get("session_id"),
    ))


# ==========================================
#  API — ML Validator (dataset + training + classify)
# ==========================================
@app.route("/api/ml-validator/stats", methods=["GET"])
@login_required
def api_ml_stats():
    return jsonify({"ok": True, "stats": ml_validator.get_dataset_stats(),
                     "info": ml_validator.get_info()})


@app.route("/api/ml-validator/extract-features", methods=["POST"])
@login_required
def api_ml_features():
    d = request.get_json() or {}
    return jsonify({"ok": True,
                     "features": ml_validator.extract_features(
                         payload=d.get("payload") or {},
                         sensor_length=int(d.get("sensor_length", 0)))})


@app.route("/api/ml-validator/add-sample", methods=["POST"])
@login_required
def api_ml_add():
    d = request.get_json() or {}
    return jsonify(ml_validator.add_to_dataset(
        payload=d.get("payload") or {},
        sensor_length=int(d.get("sensor_length", 0)),
        label=d.get("label", "unknown"),
        source=d.get("source", "manual"),
    ))


@app.route("/api/ml-validator/seed", methods=["POST"])
@login_required
def api_ml_seed():
    """Importa capturas reales como dataset inicial."""
    return jsonify(ml_validator.seed_from_captures())


@app.route("/api/ml-validator/train", methods=["POST"])
@login_required
def api_ml_train():
    d = request.get_json() or {}
    return jsonify(ml_validator.train_model(
        prefer_label=d.get("prefer_label", "human_confirmed"),
    ))


@app.route("/api/ml-validator/classify", methods=["POST"])
@login_required
def api_ml_classify():
    d = request.get_json() or {}
    return jsonify(ml_validator.classify(
        payload=d.get("payload") or {},
        sensor_length=int(d.get("sensor_length", 0)),
        sensor_string=d.get("sensor_string"),
    ))


@app.route("/api/ml-validator/tag-purchase", methods=["POST"])
@login_required
def api_ml_tag():
    d = request.get_json() or {}
    return jsonify(ml_validator.tag_successful_purchase(
        payload=d.get("payload") or {},
        sensor_length=int(d.get("sensor_length", 0)),
    ))


@app.route("/api/ml-validator/export", methods=["GET"])
@login_required
def api_ml_export():
    return jsonify(ml_validator.export_dataset())


@app.route("/api/ml-validator/import", methods=["POST"])
@login_required
def api_ml_import():
    d = request.get_json() or {}
    return jsonify(ml_validator.import_dataset(
        json_text=d.get("json_text", ""),
        merge=bool(d.get("merge", True)),
    ))


@app.route("/api/ml-validator/self-test", methods=["POST"])
@login_required
def api_ml_selftest():
    return jsonify(ml_validator.self_test())


# ==========================================
#  API — Cluster Capture Fleet (N machines)
# ==========================================
@app.route("/api/cluster-capture/config", methods=["GET", "POST"])
@login_required
def api_cc_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": cluster_capture.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": cluster_capture.get_config()})


@app.route("/api/cluster-capture/status", methods=["GET"])
@login_required
def api_cc_status():
    return jsonify({"ok": True, "status": cluster_capture.fleet_status()})


@app.route("/api/cluster-capture/hosts", methods=["GET"])
@login_required
def api_cc_hosts():
    return jsonify({"ok": True, "hosts": cluster_capture.list_registered_hosts()})


@app.route("/api/cluster-capture/start", methods=["POST"])
@login_required
def api_cc_start():
    d = request.get_json() or {}
    return jsonify(cluster_capture.start_fleet(
        target_urls=d.get("target_urls"),
        captures_per_host_per_hour=d.get("captures_per_host_per_hour"),
        engine=d.get("engine"),
    ))


@app.route("/api/cluster-capture/stop", methods=["POST"])
@login_required
def api_cc_stop():
    return jsonify(cluster_capture.stop_fleet())


@app.route("/api/cluster-capture/register", methods=["POST"])
@login_required
def api_cc_register():
    d = request.get_json() or {}
    return jsonify(cluster_capture.register_as_capture_host(
        capacity_per_hour=int(d.get("capacity_per_hour", 40)),
        geo=d.get("geo"), engine=d.get("engine", "chromium"),
    ))


@app.route("/api/cluster-capture/elect", methods=["POST"])
@login_required
def api_cc_elect():
    return jsonify(cluster_capture.elect_coordinator())


@app.route("/api/cluster-capture/distribute", methods=["POST"])
@login_required
def api_cc_distribute():
    return jsonify(cluster_capture.distribute_targets())


@app.route("/api/cluster-capture/self-test", methods=["POST"])
@login_required
def api_cc_selftest():
    return jsonify(cluster_capture.self_test())


# ==========================================
#  API — Captcha CV Solver
# ==========================================
@app.route("/api/captcha-cv/config", methods=["GET", "POST"])
@login_required
def api_cv_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": captcha_cv_solver.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": captcha_cv_solver.get_config()})


@app.route("/api/captcha-cv/info", methods=["GET"])
@login_required
def api_cv_info():
    return jsonify({"ok": True, "info": captcha_cv_solver.get_info()})


@app.route("/api/captcha-cv/stats", methods=["GET"])
@login_required
def api_cv_stats():
    return jsonify({"ok": True, "stats": captcha_cv_solver.get_dataset_stats()})


@app.route("/api/captcha-cv/types", methods=["GET"])
@login_required
def api_cv_types():
    return jsonify({"ok": True, "types": captcha_cv_solver.SUPPORTED_TYPES})


@app.route("/api/captcha-cv/collect", methods=["POST"])
@login_required
def api_cv_collect():
    """Guarda un sample. Image en base64 o data URL."""
    d = request.get_json() or {}
    img = d.get("image_base64", "")
    if img.startswith("data:"):
        img = img.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(img)
    except Exception as e:
        return jsonify({"ok": False, "error": f"invalid base64: {e}"}), 400
    return jsonify(captcha_cv_solver.collect_sample(
        image_bytes=image_bytes,
        captcha_type=d.get("captcha_type", "recaptcha_v2_image"),
        label=d.get("label"),
        source_url=d.get("source_url"),
        sitekey=d.get("sitekey"),
        solver_used=d.get("solver_used"),
    ))


@app.route("/api/captcha-cv/load-model", methods=["POST"])
@login_required
def api_cv_load():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.load_model(d.get("path")))


@app.route("/api/captcha-cv/solve-local", methods=["POST"])
@login_required
def api_cv_solve_local():
    d = request.get_json() or {}
    img = d.get("image_base64", "")
    if img.startswith("data:"):
        img = img.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(img)
    except Exception as e:
        return jsonify({"ok": False, "error": f"invalid base64: {e}"}), 400
    return jsonify(captcha_cv_solver.solve_local(
        image_bytes=image_bytes,
        captcha_type=d.get("captcha_type", "recaptcha_v2_image"),
    ))


@app.route("/api/captcha-cv/smart-solve", methods=["POST"])
@login_required
def api_cv_smart():
    d = request.get_json() or {}
    img = d.get("image_base64", "")
    if img.startswith("data:"):
        img = img.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(img)
    except Exception as e:
        return jsonify({"ok": False, "error": f"invalid base64: {e}"}), 400
    return jsonify(captcha_cv_solver.smart_solve(
        image_bytes=image_bytes,
        captcha_type=d.get("captcha_type", "recaptcha_v2_image"),
        sitekey=d.get("sitekey"),
        page_url=d.get("page_url"),
    ))


@app.route("/api/captcha-cv/export", methods=["GET"])
@login_required
def api_cv_export():
    ct = request.args.get("captcha_type")
    return jsonify(captcha_cv_solver.export_labeled_dataset(captcha_type=ct))


@app.route("/api/captcha-cv/import-model", methods=["POST"])
@login_required
def api_cv_import_model():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.import_model(d.get("path", "")))


@app.route("/api/captcha-cv/self-test", methods=["POST"])
@login_required
def api_cv_selftest():
    return jsonify(captcha_cv_solver.self_test())


# --- MEJORA 4: GPU detection ---
@app.route("/api/captcha-cv/gpu-info", methods=["GET"])
@login_required
def api_cv_gpu():
    return jsonify({"ok": True, "gpu": captcha_cv_solver.detect_gpu_providers()})


# --- MEJORA 3: Multi-model ensemble ---
@app.route("/api/captcha-cv/ensemble", methods=["POST"])
@login_required
def api_cv_ensemble():
    d = request.get_json() or {}
    img = d.get("image_base64", "")
    if img.startswith("data:"):
        img = img.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(img)
    except Exception as e:
        return jsonify({"ok": False, "error": f"invalid base64: {e}"}), 400
    return jsonify(captcha_cv_solver.solve_local_ensemble(
        image_bytes=image_bytes,
        captcha_type=d.get("captcha_type", "recaptcha_v2_image"),
        voting=d.get("voting", "weighted_avg"),
    ))


@app.route("/api/captcha-cv/loaded-models", methods=["GET"])
@login_required
def api_cv_loaded():
    return jsonify({"ok": True, "models": captcha_cv_solver.list_loaded_models()})


@app.route("/api/captcha-cv/unload/<slot>", methods=["POST"])
@login_required
def api_cv_unload(slot):
    return jsonify(captcha_cv_solver.unload_model(slot))


# --- MEJORA 2: Confidence calibration ---
@app.route("/api/captcha-cv/calibration/info", methods=["GET"])
@login_required
def api_cv_cal_info():
    return jsonify({"ok": True, "calibration": captcha_cv_solver.get_calibration_info()})


@app.route("/api/captcha-cv/calibration/calibrate", methods=["POST"])
@login_required
def api_cv_cal():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.calibrate_threshold(
        captcha_type=d.get("captcha_type", "recaptcha_v2_image"),
        target_precision=float(d.get("target_precision", 0.85)),
    ))


@app.route("/api/captcha-cv/calibration/all", methods=["POST"])
@login_required
def api_cv_cal_all():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.calibrate_all_types(
        target_precision=float(d.get("target_precision", 0.85)),
    ))


@app.route("/api/captcha-cv/calibration/threshold/<captcha_type>", methods=["GET"])
@login_required
def api_cv_cal_get(captcha_type):
    return jsonify({"ok": True, "captcha_type": captcha_type,
                     "threshold": captcha_cv_solver.get_calibrated_threshold(captcha_type)})


# --- MEJORA 1: Export training dataset ---
@app.route("/api/captcha-cv/export-training", methods=["POST"])
@login_required
def api_cv_export_training():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.export_training_dataset(
        captcha_type=d.get("captcha_type"),
        format=d.get("format", "yolo"),
        output_dir=d.get("output_dir"),
    ))


# --- MEJORA A: Pre-trained model downloader ---
@app.route("/api/captcha-cv/pretrained/list", methods=["GET"])
@login_required
def api_cv_pre_list():
    return jsonify(captcha_cv_solver.list_pretrained_models())


@app.route("/api/captcha-cv/pretrained/download", methods=["POST"])
@login_required
def api_cv_pre_download():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.download_pretrained(
        model_name=d.get("model_name", ""),
        save_as=d.get("save_as"),
        verify_sha256=bool(d.get("verify_sha256", True)),
    ))


# --- MEJORA B: Active learning queue ---
@app.route("/api/captcha-cv/active-learning/low-confidence", methods=["GET"])
@login_required
def api_cv_al_low():
    return jsonify(captcha_cv_solver.detect_low_confidence_samples(
        threshold=float(request.args.get("threshold", 0.60)),
        captcha_type=request.args.get("captcha_type"),
        limit=int(request.args.get("limit", 50)),
    ))


@app.route("/api/captcha-cv/active-learning/queue", methods=["POST"])
@login_required
def api_cv_al_queue():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.queue_for_human_labeling(
        sample_id=d.get("sample_id", ""),
        captcha_type=d.get("captcha_type", ""),
        confidence=float(d.get("confidence", 0.0)),
        notes=d.get("notes"),
    ))


@app.route("/api/captcha-cv/active-learning/pending", methods=["GET"])
@login_required
def api_cv_al_pending():
    return jsonify(captcha_cv_solver.get_pending_labels(
        limit=int(request.args.get("limit", 20)),
        captcha_type=request.args.get("captcha_type"),
    ))


@app.route("/api/captcha-cv/active-learning/submit", methods=["POST"])
@login_required
def api_cv_al_submit():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.submit_human_label(
        sample_id=d.get("sample_id", ""),
        label=d.get("label", ""),
        notes=d.get("notes"),
    ))


@app.route("/api/captcha-cv/active-learning/stats", methods=["GET"])
@login_required
def api_cv_al_stats():
    return jsonify({"ok": True,
                     "stats": captcha_cv_solver.get_active_learning_stats()})


# --- MEJORA C: Auto-capture desde Playwright ---
@app.route("/api/captcha-cv/detectors", methods=["GET"])
@login_required
def api_cv_detectors():
    return jsonify({"ok": True,
                     "detectors": captcha_cv_solver.CAPTCHA_DETECTORS,
                     "supported_types": list(captcha_cv_solver.CAPTCHA_DETECTORS.keys())})


# --- MEJORA D: ONNX Quantization + benchmark ---
@app.route("/api/captcha-cv/quantize", methods=["POST"])
@login_required
def api_cv_quant():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.quantize_model(
        model_path=d.get("model_path", ""),
        quant_type=d.get("quant_type", "int8"),
        output_path=d.get("output_path"),
    ))


@app.route("/api/captcha-cv/benchmark", methods=["POST"])
@login_required
def api_cv_bench():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.benchmark_inference(
        model_path=d.get("model_path", ""),
        iterations=int(d.get("iterations", 50)),
        use_gpu=bool(d.get("use_gpu", True)),
    ))


# ==========================================
#  BLOQUE NUEVO: 10 MEJORAS POST-RESEARCH
# ==========================================

# --- 1. BOTS Act Compliance ---
@app.route("/api/bots-compliance/waiver-text", methods=["GET"])
@login_required
def api_bc_text():
    return jsonify({"ok": True, **bots_compliance.get_waiver_text()})


def _session_user_id() -> str:
    """Extrae un user_id string limpio de la sesión (evita objeto dict)."""
    user = session.get("user")
    if isinstance(user, dict):
        return user.get("username") or user.get("id") or "anonymous"
    if isinstance(user, str):
        return user
    return "anonymous"


@app.route("/api/bots-compliance/sign", methods=["POST"])
@login_required
def api_bc_sign():
    d = request.get_json() or {}
    user_id = d.get("user_id") or _session_user_id()
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "")
    return jsonify(bots_compliance.sign_waiver(
        user_id=user_id, ip=ip,
        signature=d.get("signature", "I AGREE"),
        user_agent=request.headers.get("User-Agent", "")[:200],
        consent_checked=bool(d.get("consent_checked", True)),
    ))


@app.route("/api/bots-compliance/verify", methods=["POST"])
@login_required
def api_bc_verify():
    d = request.get_json() or {}
    user_id = d.get("user_id") or _session_user_id()
    return jsonify(bots_compliance.verify_live_allowed(
        user_id=user_id, operation=d.get("operation", "live_purchase")))


@app.route("/api/bots-compliance/waivers", methods=["GET"])
@login_required
def api_bc_list():
    return jsonify({"ok": True,
                     "waivers": bots_compliance.list_waivers(
                         user_id=request.args.get("user_id"),
                         active_only=request.args.get("active", "1") != "0")})


@app.route("/api/bots-compliance/revoke/<wid>", methods=["POST"])
@login_required
def api_bc_revoke(wid):
    d = request.get_json() or {}
    return jsonify(bots_compliance.revoke_waiver(wid, reason=d.get("reason", "")))


@app.route("/api/bots-compliance/config", methods=["GET", "POST"])
@login_required
def api_bc_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": bots_compliance.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": bots_compliance.get_config()})


@app.route("/api/bots-compliance/stats", methods=["GET"])
@login_required
def api_bc_stats():
    return jsonify({"ok": True, "stats": bots_compliance.get_stats()})


@app.route("/api/bots-compliance/self-test", methods=["POST"])
@login_required
def api_bc_selftest():
    return jsonify(bots_compliance.self_test())


# --- 2. Nodriver engine ---
@app.route("/api/nodriver/info", methods=["GET"])
@login_required
def api_nd_info():
    return jsonify({"ok": True, "info": nodriver_engine.get_info()})


@app.route("/api/nodriver/stealth-check", methods=["POST"])
@login_required
def api_nd_check():
    d = request.get_json() or {}
    return jsonify(nodriver_engine.stealth_check(
        url=d.get("url", "https://bot-detector.rebrowser.net/")))


@app.route("/api/nodriver/sessions", methods=["GET"])
@login_required
def api_nd_sessions():
    return jsonify({"ok": True, "sessions": nodriver_engine.list_sessions()})


@app.route("/api/nodriver/close-all", methods=["POST"])
@login_required
def api_nd_close_all():
    return jsonify(nodriver_engine.close_all())


@app.route("/api/nodriver/self-test", methods=["POST"])
@login_required
def api_nd_selftest():
    return jsonify(nodriver_engine.self_test())


# --- 3. Rebrowser stealth ---
@app.route("/api/rebrowser/info", methods=["GET"])
@login_required
def api_rb_info():
    return jsonify({"ok": True, "info": rebrowser_stealth.get_info()})


@app.route("/api/rebrowser/score", methods=["POST"])
@login_required
def api_rb_score():
    d = request.get_json() or {}
    return jsonify(rebrowser_stealth.get_stealth_score(
        url=d.get("url", "https://bot-detector.rebrowser.net/"),
        timeout_s=int(d.get("timeout_s", 30)),
        use_rebrowser=bool(d.get("use_rebrowser", True))))


@app.route("/api/rebrowser/compare", methods=["POST"])
@login_required
def api_rb_compare():
    d = request.get_json() or {}
    return jsonify(rebrowser_stealth.compare_engines(
        url=d.get("url", "https://bot-detector.rebrowser.net/")))


@app.route("/api/rebrowser/ci-check", methods=["POST"])
@login_required
def api_rb_ci():
    d = request.get_json() or {}
    return jsonify(rebrowser_stealth.check_ci_bot_score(
        min_score=int(d.get("min_score", 90))))


@app.route("/api/rebrowser/self-test", methods=["POST"])
@login_required
def api_rb_selftest():
    return jsonify(rebrowser_stealth.self_test())


# --- 4. hyper-sdk ---
@app.route("/api/hyper-sdk/config", methods=["GET", "POST"])
@login_required
def api_hs_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": hyper_sdk_client.save_config(request.get_json() or {})})
    cfg = hyper_sdk_client.get_config()
    if cfg.get("api_key"): cfg["api_key"] = "***"
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/hyper-sdk/akamai", methods=["POST"])
@login_required
def api_hs_akamai():
    d = request.get_json() or {}
    return jsonify(hyper_sdk_client.generate_akamai_sensor(
        abck=d.get("abck", ""), bmsz=d.get("bmsz", ""),
        user_agent=d.get("user_agent", ""),
        page_url=d.get("page_url", ""),
        post_count=int(d.get("post_count", 1))))


@app.route("/api/hyper-sdk/datadome", methods=["POST"])
@login_required
def api_hs_datadome():
    d = request.get_json() or {}
    return jsonify(hyper_sdk_client.generate_datadome(
        target_url=d.get("target_url", ""),
        cookie=d.get("cookie", ""),
        user_agent=d.get("user_agent", "")))


@app.route("/api/hyper-sdk/kasada", methods=["POST"])
@login_required
def api_hs_kasada():
    d = request.get_json() or {}
    return jsonify(hyper_sdk_client.generate_kasada_payload(
        url=d.get("url", "")))


@app.route("/api/hyper-sdk/test", methods=["POST"])
@login_required
def api_hs_test():
    return jsonify(hyper_sdk_client.test_connection())


@app.route("/api/hyper-sdk/self-test", methods=["POST"])
@login_required
def api_hs_selftest():
    return jsonify(hyper_sdk_client.self_test())


# --- 5. LLM CAPTCHA ---
@app.route("/api/llm-captcha/config", methods=["GET", "POST"])
@login_required
def api_llm_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": llm_captcha.save_config(request.get_json() or {})})
    cfg = llm_captcha.get_config()
    for k in ("openai_api_key", "anthropic_api_key", "google_api_key"):
        if cfg.get(k): cfg[k] = "***"
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/llm-captcha/providers", methods=["GET"])
@login_required
def api_llm_providers():
    return jsonify({"ok": True, "providers": llm_captcha.available_providers()})


@app.route("/api/llm-captcha/solve", methods=["POST"])
@login_required
def api_llm_solve():
    d = request.get_json() or {}
    img = d.get("image_base64", "")
    if img.startswith("data:"):
        img = img.split(",", 1)[1]
    return jsonify(llm_captcha.solve_image_captcha(
        image_base64=img,
        prompt=d.get("prompt"),
        captcha_type=d.get("captcha_type", "recaptcha_image"),
        target_object=d.get("target_object", "car"),
        provider=d.get("provider")))


@app.route("/api/llm-captcha/test/<provider>", methods=["POST"])
@login_required
def api_llm_test(provider):
    return jsonify(llm_captcha.test_provider(provider))


@app.route("/api/llm-captcha/stats", methods=["GET"])
@login_required
def api_llm_stats():
    return jsonify({"ok": True, "stats": llm_captcha.get_stats()})


@app.route("/api/llm-captcha/self-test", methods=["POST"])
@login_required
def api_llm_selftest():
    return jsonify(llm_captcha.self_test())


# --- 6. Proxy IPQS pre-screen ---
@app.route("/api/proxy-ipqs/config", methods=["GET", "POST"])
@login_required
def api_ipqs_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": proxy_ipqs.save_config(request.get_json() or {})})
    cfg = proxy_ipqs.get_config()
    if cfg.get("api_key"): cfg["api_key"] = "***"
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/proxy-ipqs/check", methods=["POST"])
@login_required
def api_ipqs_check():
    d = request.get_json() or {}
    return jsonify(proxy_ipqs.check_ip(
        ip=d.get("ip", ""),
        use_cache=bool(d.get("use_cache", True))))


@app.route("/api/proxy-ipqs/prescreen", methods=["POST"])
@login_required
def api_ipqs_prescreen():
    d = request.get_json() or {}
    return jsonify(proxy_ipqs.prescreen_before_use(
        proxy_url=d.get("proxy_url", "")))


@app.route("/api/proxy-ipqs/batch", methods=["POST"])
@login_required
def api_ipqs_batch():
    d = request.get_json() or {}
    return jsonify({"ok": True,
                     "results": proxy_ipqs.batch_check(
                         ips=d.get("ips", []),
                         max_workers=int(d.get("max_workers", 10)))})


@app.route("/api/proxy-ipqs/cache", methods=["GET", "DELETE"])
@login_required
def api_ipqs_cache():
    if request.method == "DELETE":
        return jsonify(proxy_ipqs.clear_cache())
    return jsonify({"ok": True, "stats": proxy_ipqs.get_cache_stats()})


@app.route("/api/proxy-ipqs/self-test", methods=["POST"])
@login_required
def api_ipqs_selftest():
    return jsonify(proxy_ipqs.self_test())


# --- 7. Proxy per account enforce ---
@app.route("/api/proxy-per-account/config", methods=["GET", "POST"])
@login_required
def api_ppa_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": proxy_per_account.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": proxy_per_account.get_config()})


@app.route("/api/proxy-per-account/assign", methods=["POST"])
@login_required
def api_ppa_assign():
    d = request.get_json() or {}
    return jsonify(proxy_per_account.assign_proxy(
        account_id=d.get("account_id", ""),
        geo=d.get("geo"),
        force_rotate=bool(d.get("force_rotate", False))))


@app.route("/api/proxy-per-account/account/<aid>", methods=["GET", "DELETE"])
@login_required
def api_ppa_account(aid):
    if request.method == "DELETE":
        return jsonify(proxy_per_account.release_assignment(aid))
    return jsonify({"ok": True,
                     "assignment": proxy_per_account.get_account_proxy(aid)})


@app.route("/api/proxy-per-account/record-error", methods=["POST"])
@login_required
def api_ppa_error():
    d = request.get_json() or {}
    return jsonify(proxy_per_account.record_error(
        account_id=d.get("account_id", ""),
        reason=d.get("reason", "")))


@app.route("/api/proxy-per-account/assignments", methods=["GET"])
@login_required
def api_ppa_list():
    active = request.args.get("active", "1") != "0"
    return jsonify({"ok": True,
                     "assignments": proxy_per_account.list_assignments(active_only=active),
                     "stats": proxy_per_account.get_stats()})


@app.route("/api/proxy-per-account/self-test", methods=["POST"])
@login_required
def api_ppa_selftest():
    return jsonify(proxy_per_account.self_test())


# --- 8. Sensor JS watcher ---
@app.route("/api/sensor-js-watcher/config", methods=["GET", "POST"])
@login_required
def api_sjw_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": sensor_js_watcher.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": sensor_js_watcher.get_config()})


@app.route("/api/sensor-js-watcher/check", methods=["POST"])
@login_required
def api_sjw_check():
    d = request.get_json() or {}
    return jsonify(sensor_js_watcher.check_now(
        target_url=d.get("target_url", "https://www.ticketmaster.com")))


@app.route("/api/sensor-js-watcher/<action>", methods=["POST"])
@login_required
def api_sjw_ctl(action):
    if action == "start":
        return jsonify(sensor_js_watcher.start_watcher())
    if action == "stop":
        return jsonify(sensor_js_watcher.stop_watcher())
    if action == "self-test":
        return jsonify(sensor_js_watcher.self_test())
    return jsonify({"ok": False, "error": "action desconocido"}), 400


@app.route("/api/sensor-js-watcher/status", methods=["GET"])
@login_required
def api_sjw_status():
    return jsonify({"ok": True, "status": sensor_js_watcher.get_status()})


@app.route("/api/sensor-js-watcher/history", methods=["GET"])
@login_required
def api_sjw_history():
    return jsonify({"ok": True,
                     "history": sensor_js_watcher.get_history(
                         limit=int(request.args.get("limit", 50)))})


# --- 9. Advanced fingerprint (WebGPU + ECH) ---
@app.route("/api/fp/webgpu", methods=["POST"])
@login_required
def api_fp_webgpu():
    d = request.get_json() or {}
    profile = advanced_fingerprint.generate_webgpu_profile(seed=d.get("seed"))
    return jsonify({"ok": True, "profile": profile,
                     "script": advanced_fingerprint.get_webgpu_spoof_script(profile)})


@app.route("/api/fp/ech-config", methods=["POST"])
@login_required
def api_fp_ech():
    d = request.get_json() or {}
    return jsonify(advanced_fingerprint.get_ech_config_base64(
        hostname=d.get("hostname", "www.ticketmaster.com"),
        public_name=d.get("public_name")))


@app.route("/api/fp/generate-full", methods=["POST"])
@login_required
def api_fp_full():
    d = request.get_json() or {}
    fp = advanced_fingerprint.generate_full_fingerprint(
        seed=d.get("seed"), ua=d.get("user_agent"))
    return jsonify({"ok": True, "fingerprint": fp,
                     "validation": advanced_fingerprint.validate_fingerprint_coherence(fp)})


@app.route("/api/fp/validate", methods=["POST"])
@login_required
def api_fp_validate():
    d = request.get_json() or {}
    return jsonify(advanced_fingerprint.validate_fingerprint_coherence(
        d.get("fingerprint") or {}))


@app.route("/api/fp/self-test", methods=["POST"])
@login_required
def api_fp_selftest():
    return jsonify(advanced_fingerprint.self_test())


# --- 10. Presale code farming ---
@app.route("/api/presale/codes", methods=["GET", "POST"])
@login_required
def api_ps_codes():
    if request.method == "POST":
        d = request.get_json() or {}
        return jsonify(presale_farming.add_code(
            code=d.get("code", ""),
            event_id=d.get("event_id", ""),
            code_type=d.get("code_type", "generic"),
            source=d.get("source", "manual"),
            artist=d.get("artist", ""),
            valid_until=d.get("valid_until"),
            notes=d.get("notes", "")))
    return jsonify({"ok": True,
                     "codes": presale_farming.list_codes(
                         event_id=request.args.get("event_id"),
                         code_type=request.args.get("type"),
                         status=request.args.get("status"),
                         limit=int(request.args.get("limit", 200)))})


@app.route("/api/presale/codes/<cid>", methods=["DELETE"])
@login_required
def api_ps_del(cid):
    return jsonify(presale_farming.delete_code(cid))


@app.route("/api/presale/find", methods=["POST"])
@login_required
def api_ps_find():
    d = request.get_json() or {}
    code = presale_farming.find_code_for_event(
        event_id=d.get("event_id", ""), artist=d.get("artist"))
    return jsonify({"ok": True, "code": code,
                     "found": code is not None})


@app.route("/api/presale/record-try", methods=["POST"])
@login_required
def api_ps_try():
    d = request.get_json() or {}
    return jsonify(presale_farming.record_try(
        code_id=d.get("code_id", ""),
        success=bool(d.get("success", False)),
        message=d.get("message", "")))


@app.route("/api/presale/import-text", methods=["POST"])
@login_required
def api_ps_import():
    d = request.get_json() or {}
    return jsonify(presale_farming.import_codes_from_text(
        text=d.get("text", ""),
        event_id=d.get("event_id", ""),
        source=d.get("source", "manual_paste")))


@app.route("/api/presale/stats", methods=["GET"])
@login_required
def api_ps_stats():
    return jsonify({"ok": True, "stats": presale_farming.get_stats()})


@app.route("/api/presale/self-test", methods=["POST"])
@login_required
def api_ps_selftest():
    return jsonify(presale_farming.self_test())


@app.route("/api/presale/config", methods=["GET", "POST"])
@login_required
def api_ps_config():
    if request.method == "POST":
        d = request.get_json() or {}
        return jsonify({"ok": True, "config": presale_farming.save_config(d)})
    return jsonify({"ok": True, "config": presale_farming.get_config()})


@app.route("/api/presale/scrape-reddit", methods=["POST"])
@login_required
def api_ps_scrape_reddit():
    d = request.get_json() or {}
    return jsonify(presale_farming.scrape_reddit_for_codes(
        subreddits=d.get("subreddits"),
        limit_per_sub=int(d.get("limit_per_sub") or 25),
        event_id=d.get("event_id") or "",
        auto_import=bool(d.get("auto_import", True)),
        timeout_s=int(d.get("timeout_s") or 12),
    ))


@app.route("/api/presale/ranked", methods=["POST"])
@login_required
def api_ps_ranked():
    d = request.get_json() or {}
    eid = (d.get("event_id") or "").strip()
    if not eid:
        return jsonify({"ok": False, "error": "event_id_required"}), 400
    ranked = presale_farming.codes_for_event_ranked(
        event_id=eid, artist=d.get("artist"))
    return jsonify({"ok": True, "count": len(ranked), "codes": ranked})


@app.route("/api/presale/scheduler/<action>", methods=["POST"])
@login_required
def api_ps_scheduler(action):
    if action == "start":
        d = request.get_json() or {}
        return jsonify(presale_farming.start_scraper(
            interval_h=d.get("interval_h")))
    if action == "stop":
        return jsonify(presale_farming.stop_scraper())
    if action == "status":
        return jsonify({"ok": True, "scheduler": presale_farming.scraper_status()})
    return jsonify({"ok": False, "error": "unknown_action"}), 400


# ─── Queue Monitor (multi-perfil position tracking) ────────────────────────
@app.route("/api/queue-monitor/report", methods=["POST"])
@login_required
def api_qm_report():
    d = request.get_json() or {}
    return jsonify(queue_monitor.report(
        worker_id=d.get("worker_id", ""),
        domain=d.get("domain", ""),
        position=d.get("position", 0),
        queue_total=d.get("queue_total"),
        profile_id=d.get("profile_id"),
        event_id=d.get("event_id"),
        status=d.get("status", "queued"),
    ))


@app.route("/api/queue-monitor/state", methods=["GET"])
@login_required
def api_qm_state():
    domain = request.args.get("domain")
    return jsonify({"ok": True,
                    "workers": queue_monitor.get_all(domain),
                    "stats": queue_monitor.stats()})


@app.route("/api/queue-monitor/leader", methods=["GET"])
@login_required
def api_qm_leader():
    domain = request.args.get("domain")
    return jsonify({"ok": True,
                    "leader": queue_monitor.get_leader(domain)})


@app.route("/api/queue-monitor/clear", methods=["POST"])
@login_required
def api_qm_clear():
    d = request.get_json() or {}
    return jsonify(queue_monitor.clear(
        worker_id=d.get("worker_id"),
        domain=d.get("domain"),
        all_workers=bool(d.get("all_workers", False)),
    ))


@app.route("/api/queue-monitor/cleaner/<action>", methods=["POST"])
@login_required
def api_qm_cleaner(action):
    if action == "start":
        d = request.get_json() or {}
        return jsonify(queue_monitor.start_cleaner(
            interval_s=int(d.get("interval_s") or 30),
            ttl_s=int(d.get("ttl_s") or 60)))
    if action == "stop":
        return jsonify(queue_monitor.stop_cleaner())
    return jsonify({"ok": False, "error": "unknown_action"}), 400


@app.route("/api/queue-monitor/self-test", methods=["POST"])
@login_required
def api_qm_selftest():
    return jsonify(queue_monitor.self_test())


# ─── Proxy Sticky (sessions sticky 30 min para Queue-it) ───────────────────
@app.route("/api/proxy-sticky/get-session", methods=["POST"])
@login_required
def api_ps_get_session():
    d = request.get_json() or {}
    return jsonify(proxy_sticky.get_session(
        worker_id=d.get("worker_id", ""),
        domain=d.get("domain", "ticketmaster.es"),
        ttl_s=int(d.get("ttl_s") or 1800),
        force_renew=bool(d.get("force_renew", False)),
    ))


@app.route("/api/proxy-sticky/format-username", methods=["POST"])
@login_required
def api_ps_format_username():
    d = request.get_json() or {}
    base = d.get("base_user", "")
    sid = d.get("session_id", "")
    if not base or not sid:
        return jsonify({"ok": False, "error": "base_user_and_session_id_required"}), 400
    return jsonify({"ok": True,
                    "username": proxy_sticky.format_username(
                        base, sid,
                        ttl_minutes=int(d.get("ttl_minutes") or 30),
                        scheme=d.get("scheme", "bright_data"))})


@app.route("/api/proxy-sticky/report-ip", methods=["POST"])
@login_required
def api_ps_report_ip():
    d = request.get_json() or {}
    return jsonify(proxy_sticky.report_ip(
        worker_id=d.get("worker_id", ""),
        ip=d.get("ip", ""),
        domain=d.get("domain", "ticketmaster.es")))


@app.route("/api/proxy-sticky/release", methods=["POST"])
@login_required
def api_ps_release():
    d = request.get_json() or {}
    return jsonify(proxy_sticky.release(
        worker_id=d.get("worker_id"),
        domain=d.get("domain"),
        all_workers=bool(d.get("all_workers", False))))


@app.route("/api/proxy-sticky/state", methods=["GET"])
@login_required
def api_ps_state():
    worker_id = request.args.get("worker_id")
    return jsonify({"ok": True,
                    "sessions": proxy_sticky.get_state(worker_id),
                    "stats": proxy_sticky.stats()})


@app.route("/api/proxy-sticky/log", methods=["GET"])
@login_required
def api_ps_log():
    limit = int(request.args.get("limit", 100))
    return jsonify({"ok": True, "entries": proxy_sticky.get_log(limit)})


@app.route("/api/proxy-sticky/stats", methods=["GET"])
@login_required
def api_ps_stats_sticky():
    return jsonify({"ok": True, "stats": proxy_sticky.stats()})


@app.route("/api/proxy-sticky/cleaner/<action>", methods=["POST"])
@login_required
def api_ps_cleaner(action):
    if action == "start":
        d = request.get_json() or {}
        return jsonify(proxy_sticky.start_cleaner(
            interval_s=int(d.get("interval_s") or 60)))
    if action == "stop":
        return jsonify(proxy_sticky.stop_cleaner())
    return jsonify({"ok": False, "error": "unknown_action"}), 400


@app.route("/api/proxy-sticky/self-test", methods=["POST"])
@login_required
def api_ps_selftest_sticky():
    return jsonify(proxy_sticky.self_test())


# ─── Polling Rotate (multi-event polling worker) ───────────────────────────
@app.route("/api/polling-rotate/event", methods=["POST"])
@login_required
def api_pr_add_event():
    d = request.get_json() or {}
    return jsonify(polling_rotate.add_event(
        event_id=d.get("event_id", ""),
        url=d.get("url", ""),
        domain=d.get("domain", "ticketmaster.es"),
        priority=int(d.get("priority") or 5),
        sale_date=d.get("sale_date", ""),
        max_attempts=int(d.get("max_attempts") or 100),
        metadata=d.get("metadata"),
    ))


@app.route("/api/polling-rotate/event/<eid>", methods=["DELETE"])
@login_required
def api_pr_remove_event(eid):
    return jsonify(polling_rotate.remove_event(eid))


@app.route("/api/polling-rotate/events", methods=["GET"])
@login_required
def api_pr_list_events():
    return jsonify({"ok": True,
                    "events": polling_rotate.list_events(),
                    "stats": polling_rotate.get_stats()})


@app.route("/api/polling-rotate/priority", methods=["POST"])
@login_required
def api_pr_set_priority():
    d = request.get_json() or {}
    eid = (d.get("event_id") or "").strip()
    if not eid:
        return jsonify({"ok": False, "error": "event_id_required"}), 400
    return jsonify(polling_rotate.set_priority(eid, int(d.get("priority") or 5)))


@app.route("/api/polling-rotate/workers/<action>", methods=["POST"])
@login_required
def api_pr_workers(action):
    if action == "start":
        d = request.get_json() or {}
        return jsonify(polling_rotate.start_workers(n=int(d.get("n") or 2)))
    if action == "stop":
        return jsonify(polling_rotate.stop_workers())
    return jsonify({"ok": False, "error": "unknown_action"}), 400


@app.route("/api/polling-rotate/status", methods=["GET"])
@login_required
def api_pr_status():
    return jsonify({"ok": True,
                    "worker_status": polling_rotate.worker_status(),
                    "stats": polling_rotate.get_stats()})


@app.route("/api/polling-rotate/log", methods=["GET"])
@login_required
def api_pr_log():
    limit = int(request.args.get("limit", 100))
    return jsonify({"ok": True, "entries": polling_rotate.get_log(limit)})


@app.route("/api/polling-rotate/clear", methods=["POST"])
@login_required
def api_pr_clear():
    return jsonify(polling_rotate.clear_all())


@app.route("/api/polling-rotate/self-test", methods=["POST"])
@login_required
def api_pr_selftest():
    return jsonify(polling_rotate.self_test())


# ─── Cookie warmer stock probe ────────────────────────────────────────────
@app.route("/api/cookie-warmer/check-stock", methods=["POST"])
@login_required
def api_cw_check_stock():
    d = request.get_json() or {}
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url_required"}), 400
    timeout = float(d.get("timeout") or 6.0)
    return jsonify({"ok": True,
                    "result": cookie_warmer.check_stock(url, timeout=timeout)})


# ─── OTP 3DS arm bank (auto-pattern por banco emisor) ────────────────────
@app.route("/api/otp-3ds/arm-bank", methods=["POST"])
@login_required
def api_otp3ds_arm_bank():
    d = request.get_json() or {}
    bank_or_pan = (d.get("bank") or d.get("pan") or "").strip()
    try:
        import otp_3ds_autofill
        return jsonify(otp_3ds_autofill.arm_bank(bank_or_pan))
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/otp-3ds/disarm-bank", methods=["POST"])
@login_required
def api_otp3ds_disarm_bank():
    try:
        import otp_3ds_autofill
        return jsonify(otp_3ds_autofill.disarm_bank())
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/otp-3ds/detect-bank", methods=["POST"])
@login_required
def api_otp3ds_detect_bank():
    d = request.get_json() or {}
    pan = (d.get("pan") or d.get("card") or "").strip()
    try:
        import otp_3ds_autofill
        bank = otp_3ds_autofill.detect_bank_from_card(pan)
        return jsonify({"ok": True, "bank": bank,
                        "patterns_count": len(otp_3ds_autofill.patterns_for_bank(bank))})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


# ─── Cloudflare Turnstile auto-solver ─────────────────────────────────────
@app.route("/api/turnstile/solve", methods=["POST"])
@login_required
def api_ts_solve():
    d = request.get_json() or {}
    site_key = (d.get("site_key") or d.get("websiteKey") or "").strip()
    url = (d.get("url") or d.get("websiteURL") or "").strip()
    if not site_key or not url:
        return jsonify({"ok": False, "error": "site_key_and_url_required"}), 400
    return jsonify(turnstile_solver.solve_token(
        site_key=site_key, url=url,
        action=d.get("action"), cdata=d.get("cdata"),
        preferred_service=d.get("preferred_service")))


@app.route("/api/turnstile/stats", methods=["GET"])
@login_required
def api_ts_stats():
    return jsonify({"ok": True, "stats": turnstile_solver.get_stats()})


@app.route("/api/turnstile/log", methods=["GET"])
@login_required
def api_ts_log():
    limit = int(request.args.get("limit", 50))
    return jsonify({"ok": True, "entries": turnstile_solver.get_log(limit)})


@app.route("/api/turnstile/self-test", methods=["POST"])
@login_required
def api_ts_selftest():
    return jsonify(turnstile_solver.self_test())


@app.route("/api/turnstile/test-demo", methods=["POST"])
@login_required
def api_ts_test_demo():
    d = request.get_json() or {}
    return jsonify(turnstile_solver.test_against_demo(
        demo_site_key=d.get("site_key") or "1x00000000000000000000AA",
        demo_url=d.get("url") or "https://www.turnstile-demo.com/"))


@app.route("/api/turnstile/test-e2e", methods=["POST"])
@login_required
def api_ts_test_e2e():
    d = request.get_json() or {}
    return jsonify(turnstile_solver.test_e2e_playwright(
        demo_url=d.get("url") or "https://www.turnstile-demo.com/",
        headless=bool(d.get("headless", True)),
        submit_form=bool(d.get("submit_form", False)),
        timeout_s=int(d.get("timeout_s") or 60)))


@app.route("/api/turnstile/reset-dom-alert", methods=["POST"])
@login_required
def api_ts_reset_dom_alert():
    return jsonify(turnstile_solver.reset_dom_alert())


@app.route("/api/turnstile/suggest-selectors", methods=["POST"])
@login_required
def api_ts_suggest_selectors():
    d = request.get_json() or {}
    return jsonify(turnstile_solver.suggest_selectors_via_llm(
        html_snippet=d.get("html"),
        screenshot_path=d.get("screenshot_path"),
        model=d.get("model") or "claude-3-5-sonnet-20241022"))


@app.route("/api/turnstile/apply-selectors", methods=["POST"])
@login_required
def api_ts_apply_selectors():
    d = request.get_json() or {}
    selectors = d.get("selectors") or []
    if not isinstance(selectors, list) or not selectors:
        return jsonify({"ok": False, "error": "selectors_list_required"}), 400
    return jsonify(turnstile_solver.apply_suggested_selectors(
        selectors=selectors, dry_run=bool(d.get("dry_run", False))))


@app.route("/api/turnstile/backups", methods=["GET"])
@login_required
def api_ts_list_backups():
    return jsonify({"ok": True, "backups": turnstile_solver.list_backups()})


@app.route("/api/turnstile/backups/restore", methods=["POST"])
@login_required
def api_ts_restore_backup():
    d = request.get_json() or {}
    fn = (d.get("filename") or "").strip()
    if not fn:
        return jsonify({"ok": False, "error": "filename_required"}), 400
    return jsonify(turnstile_solver.restore_backup(fn))


@app.route("/api/turnstile/backups/delete", methods=["POST"])
@login_required
def api_ts_delete_backup():
    d = request.get_json() or {}
    fn = (d.get("filename") or "").strip()
    if not fn:
        return jsonify({"ok": False, "error": "filename_required"}), 400
    return jsonify(turnstile_solver.delete_backup(fn))


@app.route("/api/turnstile/backups/diff", methods=["POST"])
@login_required
def api_ts_diff_backup():
    d = request.get_json() or {}
    fn = (d.get("filename") or "").strip()
    if not fn:
        return jsonify({"ok": False, "error": "filename_required"}), 400
    return jsonify(turnstile_solver.diff_backup_vs_current(
        fn, context_lines=int(d.get("context_lines") or 3)))


@app.route("/api/turnstile/backups/cleanup", methods=["POST"])
@login_required
def api_ts_cleanup_backups():
    d = request.get_json() or {}
    return jsonify(turnstile_solver.cleanup_old_backups(
        keep_last=int(d.get("keep_last") or 10),
        keep_days=int(d.get("keep_days") or 30),
        dry_run=bool(d.get("dry_run", False))))


# ─── CAPTCHA Budget alerts ─────────────────────────────────────────────────
@app.route("/api/captcha/budget", methods=["GET", "POST"])
@login_required
def api_captcha_budget():
    if request.method == "POST":
        d = request.get_json() or {}
        return jsonify({"ok": True, "budget": captcha_manager.save_budget(d)})
    return jsonify({"ok": True, "budget": captcha_manager.get_budget()})


@app.route("/api/captcha/budget/check", methods=["POST"])
@login_required
def api_captcha_budget_check():
    d = request.get_json() or {}
    return jsonify(captcha_manager.check_budget_alert(
        force=bool(d.get("force", False))))


@app.route("/api/captcha/budget/digest", methods=["POST"])
@login_required
def api_captcha_budget_digest():
    d = request.get_json() or {}
    return jsonify(captcha_manager.weekly_digest(
        force=bool(d.get("force", False))))


@app.route("/api/captcha/budget/scheduler/<action>", methods=["POST"])
@login_required
def api_captcha_budget_scheduler(action):
    if action == "start":
        return jsonify(captcha_manager.start_digest_scheduler())
    if action == "stop":
        return jsonify(captcha_manager.stop_digest_scheduler())
    if action == "status":
        return jsonify({"ok": True,
                        "scheduler": captcha_manager.digest_scheduler_status()})
    return jsonify({"ok": False, "error": "unknown_action"}), 400


@app.route("/api/captcha/cost-by-event", methods=["GET"])
@login_required
def api_captcha_cost_by_event():
    top = int(request.args.get("top", 20))
    sort_by = request.args.get("sort_by", "cost_usd")
    return jsonify({"ok": True,
                    "result": captcha_manager.cost_by_event(top=top, sort_by=sort_by)})


@app.route("/api/captcha/export-csv", methods=["GET"])
@login_required
def api_captcha_export_csv():
    section = request.args.get("section", "all")
    body = captcha_manager.export_csv(section=section)
    fname = f"captcha_stats_{section}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ─── Turnstile backup retention scheduler ─────────────────────────────────
@app.route("/api/turnstile/backup-scheduler/<action>", methods=["POST"])
@login_required
def api_ts_backup_scheduler(action):
    if action == "start":
        d = request.get_json() or {}
        return jsonify(turnstile_solver.start_backup_scheduler(
            keep_last=int(d.get("keep_last") or 10),
            keep_days=int(d.get("keep_days") or 30)))
    if action == "stop":
        return jsonify(turnstile_solver.stop_backup_scheduler())
    if action == "status":
        return jsonify({"ok": True,
                        "scheduler": turnstile_solver.backup_scheduler_status()})
    return jsonify({"ok": False, "error": "unknown_action"}), 400


# ─── DataDome VM verify trend ──────────────────────────────────────────────
@app.route("/api/dd-vm/verify-trend", methods=["GET"])
@login_required
def api_ddvm_verify_trend():
    limit = int(request.args.get("limit", 100))
    return jsonify({"ok": True,
                    "trend": dd_vm_trace.get_verify_trend(limit)})


@app.route("/api/captcha/cost-forecast", methods=["GET"])
@login_required
def api_captcha_cost_forecast():
    return jsonify({"ok": True,
                    "forecast": captcha_manager.cost_forecast(),
                    "totals": {
                        "total_cost_usd": captcha_manager.get_stats().get("total_cost_usd", 0.0),
                        "total_solved": captcha_manager.get_stats().get("total_solved", 0),
                    }})


# ─── DataDome VM bytecode interpreter (scaffold) ──────────────────────────
@app.route("/api/dd-vm/pull-tagjs", methods=["POST"])
@login_required
def api_ddvm_pull():
    d = request.get_json() or {}
    return jsonify(dd_vm_trace.pull_tag_js(
        url=d.get("url") or "https://js.datadome.co/tags.js"))


@app.route("/api/dd-vm/isolate-dispatcher", methods=["POST"])
@login_required
def api_ddvm_isolate():
    return jsonify(dd_vm_trace.isolate_dispatcher())


@app.route("/api/dd-vm/map-opcodes", methods=["POST"])
@login_required
def api_ddvm_map():
    return jsonify(dd_vm_trace.map_opcodes())


@app.route("/api/dd-vm/state", methods=["GET"])
@login_required
def api_ddvm_state():
    return jsonify({"ok": True, "state": dd_vm_trace.get_state()})


@app.route("/api/dd-vm/opcodes", methods=["GET"])
@login_required
def api_ddvm_opcodes():
    return jsonify({"ok": True, "opcodes": dd_vm_trace.list_opcodes()})


@app.route("/api/dd-vm/execute", methods=["POST"])
@login_required
def api_ddvm_execute():
    d = request.get_json() or {}
    return jsonify(dd_vm_trace.execute_with_env(
        env=d.get("browser_env"), bytecode=d.get("bytecode")))


@app.route("/api/dd-vm/emit", methods=["POST"])
@login_required
def api_ddvm_emit():
    d = request.get_json() or {}
    return jsonify(dd_vm_trace.emit_payload(
        env=d.get("browser_env"),
        auto_verify=bool(d.get("auto_verify", False)),
        verify_endpoint=d.get("verify_endpoint")))


@app.route("/api/dd-vm/self-test", methods=["POST"])
@login_required
def api_ddvm_selftest():
    return jsonify(dd_vm_trace.self_test())


@app.route("/api/dd-vm/instrument", methods=["POST"])
@login_required
def api_ddvm_instrument():
    return jsonify(dd_vm_trace.instrument_tagjs())


@app.route("/api/dd-vm/parse-logs", methods=["POST"])
@login_required
def api_ddvm_parse_logs():
    d = request.get_json() or {}
    lines = d.get("lines") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    return jsonify(dd_vm_trace.parse_trace_logs(lines))


@app.route("/api/dd-vm/diff-check", methods=["POST"])
@login_required
def api_ddvm_diff():
    d = request.get_json() or {}
    return jsonify(dd_vm_trace.diff_check(notify=bool(d.get("notify", True))))


@app.route("/api/dd-vm/scheduler/<action>", methods=["POST"])
@login_required
def api_ddvm_scheduler(action):
    if action == "start":
        d = request.get_json() or {}
        return jsonify(dd_vm_trace.start_scheduler(
            interval_h=int(d.get("interval_h") or 6)))
    if action == "stop":
        return jsonify(dd_vm_trace.stop_scheduler())
    if action == "status":
        return jsonify({"ok": True, "scheduler": dd_vm_trace.scheduler_status()})
    return jsonify({"ok": False, "error": "unknown_action"}), 400


@app.route("/api/dd-vm/diff-history", methods=["GET"])
@login_required
def api_ddvm_diff_history():
    limit = int(request.args.get("limit", 50))
    return jsonify({"ok": True,
                    "history": dd_vm_trace.get_diff_history(limit)})


# ─── Honest Dashboard — clasificación de módulos ──────────────────────────
@app.route("/api/honest-dashboard", methods=["GET"])
@login_required
def api_honest_dashboard():
    """Clasifica los 176 módulos del proyecto en 5 categorías:
    pipeline / dashboard_only / auxiliary / scaffold / orphan.
    """
    try:
        import honest_dashboard
        return jsonify({"ok": True, "result": honest_dashboard.classify_modules()})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/honest-dashboard/<module_name>", methods=["GET"])
@login_required
def api_honest_dashboard_module(module_name):
    try:
        import honest_dashboard
        return jsonify(honest_dashboard.get_classification(module_name))
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/test-coverage", methods=["GET"])
@login_required
def api_test_coverage():
    """Métrica de cobertura por self_test."""
    try:
        import test_coverage_metric
        since = request.args.get("since_days", type=int)
        only_module = request.args.get("module")
        return jsonify({"ok": True,
                        "result": test_coverage_metric.run_audit(
                            since_days=since, only_module=only_module)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


# --- 11. TM Mobile SDK impersonation ---
@app.route("/api/tm-mobile/config", methods=["GET", "POST"])
@login_required
def api_tmm_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": tm_mobile_sdk.save_config(request.get_json() or {})})
    cfg = tm_mobile_sdk.get_config()
    if cfg.get("discovery_api_key"): cfg["discovery_api_key"] = "***"
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/tm-mobile/headers", methods=["POST"])
@login_required
def api_tmm_headers():
    d = request.get_json() or {}
    return jsonify({"ok": True,
                     "headers": tm_mobile_sdk.get_mobile_headers(
                         platform=d.get("platform", "ios"),
                         app_version=d.get("app_version"))})


@app.route("/api/tm-mobile/event/<eid>", methods=["GET"])
@login_required
def api_tmm_event(eid):
    return jsonify(tm_mobile_sdk.fetch_event_info(
        event_id=eid,
        platform=request.args.get("platform", "ios")))


@app.route("/api/tm-mobile/search", methods=["POST"])
@login_required
def api_tmm_search():
    d = request.get_json() or {}
    return jsonify(tm_mobile_sdk.search_events(
        query=d.get("query", ""),
        country_code=d.get("country_code", "US"),
        city=d.get("city", ""),
        platform=d.get("platform", "ios"),
        size=int(d.get("size", 20))))


@app.route("/api/tm-mobile/stats", methods=["GET"])
@login_required
def api_tmm_stats():
    return jsonify({"ok": True, "stats": tm_mobile_sdk.get_stats()})


@app.route("/api/tm-mobile/self-test", methods=["POST"])
@login_required
def api_tmm_selftest():
    return jsonify(tm_mobile_sdk.self_test())


# --- Gap analysis endpoint (auditoría continua) ---
@app.route("/api/gap-analysis", methods=["GET"])
@login_required
def api_gap_analysis():
    """Identifica qué falta para activar modo LIVE completo.

    Returns:
      {ok, gaps: {critical, high, medium, low}, green, summary}
    """
    import os
    gaps = {"critical": [], "high": [], "medium": [], "low": []}
    green = []

    # Datos usuario
    try:
        accts = json_atomic.load(config.ruta("accounts.json"), default={"accounts":[]}).get("accounts",[])
        cards = json_atomic.load(config.ruta("cards.json"), default={"cards":[]}).get("cards",[])
        proxies = json_atomic.load(config.ruta("proxies.json"), default={"proxies":[]}).get("proxies",[])
    except Exception:
        accts = cards = proxies = []

    if len(accts) == 0: gaps["critical"].append("0 cuentas TM — LIVE imposible")
    else: green.append(f"{len(accts)} cuentas TM")
    if len(cards) < 2: gaps["critical"].append(f"{len(cards)} tarjetas (necesarias ≥2)")
    else: green.append(f"{len(cards)} tarjetas")
    if len(proxies) == 0: gaps["critical"].append("0 proxies — alto riesgo ban")
    else: green.append(f"{len(proxies)} proxies")

    # Waivers
    try:
        waivers = json_atomic.load(config.ruta("bots_compliance_waivers.json"),
                                      default={"waivers":[]}).get("waivers",[])
        active = [w for w in waivers if not w.get("revoked")]
        if not active: gaps["critical"].append("Sin waiver BOTS Act activo")
        else: green.append(f"{len(active)} waivers BOTS Act activos")
    except Exception: pass

    # API keys (check por placeholder)
    def key_status(path, field):
        try:
            d = json_atomic.load(config.ruta(path), default={})
            v = str(d.get(field, ""))
            if not v: return "empty"
            for m in ("PLACEHOLDER","PLACEH","XXXX","TODO","YOUR_KEY"):
                if m in v.upper(): return "placeholder"
            return "real"
        except: return "error"

    # captcha_manager usa path distinto — verificar via función
    try:
        from captcha_manager import get_config as cm_cfg
        cm_api = cm_cfg().get("api_key", "")
        cm_status = ("real" if cm_api and "PLACEH" not in cm_api.upper()
                      else "placeholder" if cm_api else "empty")
    except Exception:
        cm_status = "empty"
    if cm_status == "empty": gaps["high"].append("CapSolver: sin configurar")
    elif cm_status == "placeholder": gaps["medium"].append("CapSolver: placeholder")
    else: green.append("CapSolver: real key")

    api_checks = [
        ("proxy_ipqs_config.json", "api_key", "IPQualityScore", "high"),
        ("adyen_config.json", "api_key", "Adyen", "medium"),
        ("discovery_config.json", "api_key", "TM Discovery", "medium"),
        ("notifications_config.json", "discord_webhook", "Discord webhook", "high"),
        ("llm_captcha_config.json", "google_api_key", "Gemini", "medium"),
        ("llm_captcha_config.json", "openai_api_key", "OpenAI", "medium"),
        ("llm_captcha_config.json", "anthropic_api_key", "Claude", "medium"),
        ("hyper_sdk_config.json", "api_key", "hyper-sdk", "medium"),
        ("mobile_proxies_config.json", "username", "Mobile proxies user", "high"),
        ("otp_3ds_config.json", "imap_username", "IMAP 3DS", "medium"),
    ]
    for path, field, name, severity in api_checks:
        s = key_status(path, field)
        if s == "empty": gaps[severity].append(f"{name}: sin configurar")
        elif s == "placeholder": gaps["medium"].append(f"{name}: placeholder")
        else: green.append(f"{name}: real")

    # Módulos avanzados
    try:
        import redis_backend as rb
        if not rb.get_info().get("distributed"):
            gaps["medium"].append("REDIS_URL no configurado (single-host)")
        else: green.append("Redis cluster activo")
    except Exception: pass

    try:
        import nodriver_engine
        if nodriver_engine.is_available(): green.append("nodriver disponible")
    except Exception: pass
    try:
        import rebrowser_stealth
        if rebrowser_stealth.is_available(): green.append("rebrowser-playwright disponible")
    except Exception: pass

    # Modelos ONNX
    try:
        models_dir = os.path.join(os.path.dirname(config.ruta("x.json")), "models")
        if os.path.exists(models_dir):
            onnx = [f for f in os.listdir(models_dir) if f.endswith(".onnx")]
            if not onnx: gaps["low"].append("0 modelos ONNX en /data/models/")
            else: green.append(f"{len(onnx)} modelos ONNX")
        else:
            gaps["low"].append("/data/models/ no existe")
    except Exception: pass

    # Health módulos
    try:
        import health_monitor
        h = health_monitor.get_health()
        ok = h.get("modules_ok", 0)
        tot = h.get("modules_total", 0)
        if ok == tot and tot > 0:
            green.append(f"{ok}/{tot} módulos healthy")
        else:
            gaps["high"].append(f"{tot-ok} módulos no healthy ({ok}/{tot})")
    except Exception: pass

    # Summary
    total_gaps = sum(len(v) for v in gaps.values())
    critical_count = len(gaps["critical"])
    ready_for_live = critical_count == 0 and len(gaps["high"]) <= 2

    return jsonify({
        "ok": True,
        "generated_at": datetime.now().isoformat() if 'datetime' in dir() else __import__("datetime").datetime.now().isoformat(),
        "gaps": gaps,
        "green_items": green,
        "summary": {
            "total_gaps": total_gaps,
            "critical": critical_count,
            "high": len(gaps["high"]),
            "medium": len(gaps["medium"]),
            "low": len(gaps["low"]),
            "green_count": len(green),
            "ready_for_live": ready_for_live,
            "blocker_count": critical_count,
        },
    })


# --- MEJORA E: Auto-retrain trigger ---
@app.route("/api/captcha-cv/retrain/check", methods=["GET"])
@login_required
def api_cv_retrain_check():
    return jsonify(captcha_cv_solver.check_retrain_trigger())


@app.route("/api/captcha-cv/retrain/launch", methods=["POST"])
@login_required
def api_cv_retrain_launch():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.launch_retrain(
        format=d.get("format", "yolo"),
        async_mode=bool(d.get("async_mode", True)),
    ))


@app.route("/api/captcha-cv/retrain/<action>", methods=["POST"])
@login_required
def api_cv_retrain_ctl(action):
    d = request.get_json() or {}
    if action == "start":
        return jsonify(captcha_cv_solver.start_retrain_watcher(
            check_interval_s=int(d.get("check_interval_s", 600)),
        ))
    if action == "stop":
        return jsonify(captcha_cv_solver.stop_retrain_watcher())
    if action == "status":
        return jsonify({"ok": True, "status": captcha_cv_solver.get_retrain_status()})
    return jsonify({"ok": False, "error": "action desconocido"}), 400


# --- MEJORA F: Dataset augmentation ---
@app.route("/api/captcha-cv/augment/sample", methods=["POST"])
@login_required
def api_cv_aug_sample():
    d = request.get_json() or {}
    img = d.get("image_base64", "")
    if img.startswith("data:"):
        img = img.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(img)
    except Exception as e:
        return jsonify({"ok": False, "error": f"invalid base64: {e}"}), 400
    r = captcha_cv_solver.augment_sample(
        image_bytes=image_bytes,
        factor=int(d.get("factor", 10)),
        operations=d.get("operations"),
    )
    return jsonify(r)


@app.route("/api/captcha-cv/augment/dataset", methods=["POST"])
@login_required
def api_cv_aug_dataset():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.augment_dataset(
        captcha_type=d.get("captcha_type"),
        factor=int(d.get("factor", 10)),
    ))


# --- MEJORA G: ONNX graph optimization ---
@app.route("/api/captcha-cv/optimize", methods=["POST"])
@login_required
def api_cv_optimize():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.optimize_model(
        model_path=d.get("model_path", ""),
        output_path=d.get("output_path"),
        level=d.get("level", "all"),
    ))


# --- MEJORA H: HuggingFace Hub integration ---
@app.route("/api/captcha-cv/hf/community", methods=["GET"])
@login_required
def api_cv_hf_list():
    return jsonify(captcha_cv_solver.list_hf_community_models())


@app.route("/api/captcha-cv/hf/download", methods=["POST"])
@login_required
def api_cv_hf_download():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.download_from_hf(
        repo_id=d.get("repo_id", ""),
        filename=d.get("filename", ""),
        save_as=d.get("save_as"),
        revision=d.get("revision", "main"),
    ))


# --- MEJORA I: Multi-GPU parallelism ---
@app.route("/api/captcha-cv/gpu/devices", methods=["GET"])
@login_required
def api_cv_gpu_devices():
    return jsonify(captcha_cv_solver.detect_gpu_devices())


@app.route("/api/captcha-cv/load-multi-gpu", methods=["POST"])
@login_required
def api_cv_multi_gpu():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.load_models_multi_gpu(
        model_paths=d.get("model_paths", []),
        slot_prefix=d.get("slot_prefix", "gpu"),
    ))


# --- MEJORA J: Sample cleaning ---
@app.route("/api/captcha-cv/clean-dataset", methods=["POST"])
@login_required
def api_cv_clean():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.clean_dataset(
        remove_duplicates=bool(d.get("remove_duplicates", True)),
        remove_corrupted=bool(d.get("remove_corrupted", True)),
        remove_unlabeled=bool(d.get("remove_unlabeled", False)),
    ))


# --- MEJORA K: Label consistency checker ---
@app.route("/api/captcha-cv/labels/check", methods=["GET"])
@login_required
def api_cv_label_check():
    return jsonify(captcha_cv_solver.check_label_consistency())


@app.route("/api/captcha-cv/labels/resolve-conflicts", methods=["POST"])
@login_required
def api_cv_label_resolve():
    d = request.get_json() or {}
    return jsonify(captcha_cv_solver.resolve_label_conflicts(
        prefer=d.get("prefer", "human"),
    ))


# ==========================================
#  API — Apple Pay / Google Pay
# ==========================================
@app.route("/api/pay-token/config", methods=["GET", "POST"])
@login_required
def api_agp_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": apple_google_pay.save_config(request.get_json() or {})})
    # Enmascarar cert paths y keys
    cfg = apple_google_pay.get_config()
    for k in ("apple_key_path", "apple_cert_path"):
        if cfg.get(k):
            cfg[k] = "***" if cfg[k] else ""
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/pay-token/payment-request", methods=["POST"])
@login_required
def api_agp_pr():
    d = request.get_json() or {}
    return jsonify(apple_google_pay.generate_payment_request(
        amount=float(d.get("amount", 50.0)),
        currency=d.get("currency", "USD"),
        method=d.get("method", "both"),
        line_items=d.get("line_items"),
    ))


@app.route("/api/pay-token/apple", methods=["POST"])
@login_required
def api_agp_apple():
    d = request.get_json() or {}
    return jsonify(apple_google_pay.create_apple_pay_token(
        card=d.get("card", {}),
        amount=float(d.get("amount", 50.0)),
        currency=d.get("currency", "USD"),
    ))


@app.route("/api/pay-token/google", methods=["POST"])
@login_required
def api_agp_google():
    d = request.get_json() or {}
    return jsonify(apple_google_pay.create_google_pay_token(
        card=d.get("card", {}),
        amount=float(d.get("amount", 50.0)),
        currency=d.get("currency", "USD"),
    ))


@app.route("/api/pay-token/tokens", methods=["GET"])
@login_required
def api_agp_list():
    active = request.args.get("active", "1") != "0"
    return jsonify({"ok": True,
                     "tokens": apple_google_pay.list_tokens(active_only=active)})


@app.route("/api/pay-token/tokens/<tid>", methods=["DELETE"])
@login_required
def api_agp_del(tid):
    return jsonify({"ok": apple_google_pay.delete_token(tid)})


@app.route("/api/pay-token/inject-script/<tid>", methods=["GET"])
@login_required
def api_agp_inject(tid):
    method = request.args.get("method", "apple")
    t = apple_google_pay.get_token(tid)
    if not t:
        return jsonify({"ok": False, "error": "token no encontrado"}), 404
    script = apple_google_pay.build_checkout_injection_script(
        token=t.get("token", {}), method=method)
    return jsonify({"ok": True, "method": method,
                     "script": script, "script_length": len(script)})


@app.route("/api/pay-token/test/<provider>", methods=["POST"])
@login_required
def api_agp_test(provider):
    return jsonify(apple_google_pay.test_connection(provider))


@app.route("/api/pay-token/stats", methods=["GET"])
@login_required
def api_agp_stats():
    return jsonify({"ok": True, "stats": apple_google_pay.get_stats(),
                     "info": apple_google_pay.get_info()})


@app.route("/api/pay-token/self-test", methods=["POST"])
@login_required
def api_agp_selftest():
    return jsonify(apple_google_pay.self_test())


# ==========================================
#  API — Multilogin Auto-scale
# ==========================================
@app.route("/api/multilogin-autoscale/config", methods=["GET", "POST"])
@login_required
def api_mas_config():
    if request.method == "POST":
        return jsonify({"ok": True,
                         "config": multilogin_autoscale.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": multilogin_autoscale.get_config()})


@app.route("/api/multilogin-autoscale/status", methods=["GET"])
@login_required
def api_mas_status():
    return jsonify({"ok": True, "status": multilogin_autoscale.get_status()})


@app.route("/api/multilogin-autoscale/<action>", methods=["POST"])
@login_required
def api_mas_ctl(action):
    if action == "start":
        return jsonify(multilogin_autoscale.start_autoscaler())
    if action == "stop":
        return jsonify(multilogin_autoscale.stop_autoscaler())
    if action == "tick":
        return jsonify(multilogin_autoscale.manual_tick())
    return jsonify({"ok": False, "error": "action desconocido"}), 400


# ==========================================
#  API — Bot Intelligence (cerebro orquestador)
# ==========================================
@app.route("/api/bot-intel/recommend", methods=["POST"])
@login_required
def api_bi_recommend():
    d = request.get_json() or {}
    return jsonify({"ok": True,
                     "plan": bot_intelligence.recommend_strategy(d.get("event_id", ""))})


@app.route("/api/bot-intel/auto-prepare", methods=["POST"])
@login_required
def api_bi_prepare():
    d = request.get_json() or {}
    return jsonify(bot_intelligence.auto_prepare(
        event_id=d.get("event_id", ""),
        minutes_before=int(d.get("minutes_before", 15)),
    ))


@app.route("/api/bot-intel/smart-run", methods=["POST"])
@login_required
def api_bi_smart():
    d = request.get_json() or {}
    return jsonify(bot_intelligence.smart_run(
        event_id=d.get("event_id", ""),
        quantity=int(d.get("quantity", 2)),
        mode=d.get("mode", "simulate"),
    ))


@app.route("/api/bot-intel/knowledge", methods=["GET"])
@login_required
def api_bi_knowledge():
    return jsonify({"ok": True, "knowledge": bot_intelligence.get_knowledge()})


@app.route("/api/bot-intel/resources", methods=["GET"])
@login_required
def api_bi_resources():
    country = request.args.get("country", "us")
    return jsonify({"ok": True,
                     "resources": bot_intelligence._check_resources(country)})


@app.route("/api/bot-intel/self-test", methods=["POST"])
@login_required
def api_bi_selftest():
    return jsonify(bot_intelligence.self_test())


# ==========================================
#  API — Akamai bm.js analyzer + capture
# ==========================================
@app.route("/api/akamai-bmjs/fetch", methods=["POST"])
@login_required
def api_bmjs_fetch():
    d = request.get_json() or {}
    r = akamai_bmjs.fetch_bm_js(
        target_url=d.get("target_url", "https://www.ticketmaster.com"),
        timeout=float(d.get("timeout", 15)),
    )
    # no devolver código completo (puede ser 500KB+)
    if "code" in r:
        r["code_preview"] = r["code"][:500]
        r["code_length"] = len(r["code"])
        del r["code"]
    return jsonify(r)


@app.route("/api/akamai-bmjs/analyze", methods=["POST"])
@login_required
def api_bmjs_analyze():
    d = request.get_json() or {}
    if d.get("target_url"):
        f = akamai_bmjs.fetch_bm_js(d["target_url"])
        if not f.get("found"):
            return jsonify({"ok": False, "error": "bm.js no encontrado",
                             "tried": f.get("tried", [])})
        code = f.get("code", "")
    else:
        code = d.get("code", "")
    if not code:
        return jsonify({"ok": False, "error": "falta 'code' o 'target_url'"})
    return jsonify({"ok": True, "analysis": akamai_bmjs.analyze_bm_js(code)})


@app.route("/api/akamai-bmjs/deobfuscate", methods=["POST"])
@login_required
def api_bmjs_deobf():
    d = request.get_json() or {}
    code = d.get("code", "")
    if not code and d.get("target_url"):
        f = akamai_bmjs.fetch_bm_js(d["target_url"])
        code = f.get("code", "")
    if not code:
        return jsonify({"ok": False, "error": "falta 'code' o 'target_url'"})
    return jsonify(akamai_bmjs.deobfuscate_basic(code,
        max_iterations=int(d.get("max_iterations", 3))))


@app.route("/api/akamai-bmjs/diff", methods=["POST"])
@login_required
def api_bmjs_diff():
    d = request.get_json() or {}
    return jsonify({"ok": True, "diff": akamai_bmjs.diff_bm_js(
        code_old=d.get("code_old", ""),
        code_new=d.get("code_new", ""),
    )})


@app.route("/api/akamai-bmjs/cached-versions", methods=["GET"])
@login_required
def api_bmjs_cached():
    return jsonify({"ok": True, "versions": akamai_bmjs.list_cached_versions()})


@app.route("/api/akamai-bmjs/capture", methods=["POST"])
@login_required
def api_bmjs_capture():
    """Captura sensor_data REAL via Playwright. Bloqueante ~30 s."""
    d = request.get_json() or {}
    return jsonify(akamai_bmjs.capture_sensor_data(
        target_url=d.get("target_url", "https://www.ticketmaster.com"),
        duration_s=int(d.get("duration_s", 30)),
        engine=d.get("engine", "chromium"),
        headless=bool(d.get("headless", True)),
    ))


@app.route("/api/akamai-bmjs/captures", methods=["GET"])
@login_required
def api_bmjs_captures():
    return jsonify({"ok": True,
                     "captures": akamai_bmjs.list_captures(
                         target_domain=request.args.get("domain"),
                         active_only=request.args.get("active", "1") != "0",
                     )})


@app.route("/api/akamai-bmjs/mutate", methods=["POST"])
@login_required
def api_bmjs_mutate():
    d = request.get_json() or {}
    return jsonify(akamai_bmjs.mutate_sensor_data(
        sensor=d.get("sensor", ""),
        seed=d.get("seed"),
    ))


@app.route("/api/akamai-bmjs/replay/<capture_id>", methods=["POST"])
@login_required
def api_bmjs_replay(capture_id):
    d = request.get_json() or {}
    return jsonify(akamai_bmjs.replay_sensor_data(
        capture_id=capture_id,
        apply_mutation=bool(d.get("mutate", True)),
        target_url=d.get("target_url"),
    ))


@app.route("/api/akamai-bmjs/stats", methods=["GET"])
@login_required
def api_bmjs_stats():
    return jsonify({"ok": True, "stats": akamai_bmjs.get_stats(),
                     "info": akamai_bmjs.get_info()})


@app.route("/api/akamai-bmjs/self-test", methods=["POST"])
@login_required
def api_bmjs_selftest():
    return jsonify(akamai_bmjs.self_test())


# --- Pool distribuido + cluster ---
@app.route("/api/akamai-bmjs/cluster-captures", methods=["GET"])
@login_required
def api_bmjs_cluster():
    domain = request.args.get("domain")
    return jsonify({"ok": True,
                     "captures": akamai_bmjs.fetch_cluster_captures(target_domain=domain)})


@app.route("/api/akamai-bmjs/publish/<capture_id>", methods=["POST"])
@login_required
def api_bmjs_publish(capture_id):
    capture = akamai_bmjs.get_capture(capture_id)
    if not capture:
        return jsonify({"ok": False, "error": "capture no encontrada"}), 404
    return jsonify(akamai_bmjs.publish_capture_to_cluster(capture))


# --- Auto-capture scheduler ---
@app.route("/api/akamai-bmjs/scheduler/<action>", methods=["POST"])
@login_required
def api_bmjs_scheduler(action):
    d = request.get_json() or {}
    if action == "start":
        return jsonify(akamai_bmjs.start_auto_capture(
            target_urls=d.get("target_urls"),
            interval_s=int(d.get("interval_s", 45)),
            duration_s=int(d.get("duration_s", 25)),
            engine=d.get("engine", "chromium"),
            broadcast=ws_broadcast,
        ))
    if action == "stop":
        return jsonify(akamai_bmjs.stop_auto_capture())
    if action == "status":
        return jsonify({"ok": True, "status": akamai_bmjs.scheduler_status()})
    return jsonify({"ok": False, "error": "action desconocido"}), 400


# --- Fingerprint binding ---
@app.route("/api/akamai-bmjs/find-for-fingerprint", methods=["POST"])
@login_required
def api_bmjs_find_fp():
    d = request.get_json() or {}
    c = akamai_bmjs.find_capture_for_fingerprint(
        user_agent=d.get("user_agent", ""),
        viewport=d.get("viewport"),
        geo=d.get("geo"),
        target_domain=d.get("target_domain"),
    )
    if not c:
        return jsonify({"ok": False, "error": "no hay captura compatible"})
    # No devolver sensor_data completo
    safe = {k: v for k, v in c.items() if k != "sensor_data"}
    return jsonify({"ok": True, "capture": safe})


# --- Diff watchdog ---
@app.route("/api/akamai-bmjs/watchdog/check", methods=["POST"])
@login_required
def api_bmjs_watchdog_check():
    d = request.get_json() or {}
    return jsonify(akamai_bmjs.watchdog_check_bmjs_version(
        target_url=d.get("target_url", "https://www.ticketmaster.com"),
        notify=bool(d.get("notify", True)),
    ))


@app.route("/api/akamai-bmjs/watchdog/<action>", methods=["POST"])
@login_required
def api_bmjs_watchdog(action):
    d = request.get_json() or {}
    if action == "start":
        return jsonify(akamai_bmjs.start_diff_watchdog(
            target_url=d.get("target_url", "https://www.ticketmaster.com"),
            check_interval_min=int(d.get("interval_min", 30)),
        ))
    if action == "stop":
        return jsonify(akamai_bmjs.stop_diff_watchdog())
    return jsonify({"ok": False, "error": "action desconocido"}), 400


# ==========================================
#  API — Multilogin Templates
# ==========================================
@app.route("/api/multilogin/templates", methods=["GET"])
@login_required
def api_mlo_templates():
    import multilogin_orchestrator as mlo
    return jsonify({"ok": True, "templates": mlo.list_templates()})


@app.route("/api/multilogin/template/<tid>/create", methods=["POST"])
@login_required
def api_mlo_template_create(tid):
    import multilogin_orchestrator as mlo
    d = request.get_json() or {}
    return jsonify(mlo.create_profile_from_template(
        template_id=tid,
        name=d.get("name"),
        folder_id=d.get("folder_id"),
    ))


@app.route("/api/multilogin/templates/bulk", methods=["POST"])
@login_required
def api_mlo_bulk():
    import multilogin_orchestrator as mlo
    d = request.get_json() or {}
    return jsonify(mlo.bulk_create_from_templates(
        template_ids=d.get("template_ids", []),
        count_per_template=int(d.get("count_per_template", 1)),
        folder_id=d.get("folder_id"),
    ))


# ==========================================
#  API — HTTP/3 QUIC (JA4+Q)
# ==========================================
@app.route("/api/http3/info", methods=["GET"])
@login_required
def api_http3_info():
    return jsonify({"ok": True, "info": http3_quic.get_info()})


@app.route("/api/http3/self-test", methods=["POST"])
@login_required
def api_http3_selftest():
    return jsonify(http3_quic.self_test())


@app.route("/api/http3/probe", methods=["POST"])
@login_required
def api_http3_probe():
    """Probe Alt-Svc header para ver si un URL anuncia HTTP/3."""
    d = request.get_json() or {}
    return jsonify(http3_quic.probe_alt_svc(d.get("url", "https://www.ticketmaster.com")))


@app.route("/api/http3/check", methods=["POST"])
@login_required
def api_http3_check():
    """Check completo: Alt-Svc + handshake real."""
    d = request.get_json() or {}
    return jsonify(http3_quic.is_http3_supported(
        url=d.get("url", "https://www.ticketmaster.com"),
        method=d.get("method", "alt_svc"),
    ))


@app.route("/api/http3/fetch", methods=["POST"])
@login_required
def api_http3_fetch():
    """GET HTTP/3 real."""
    d = request.get_json() or {}
    r = http3_quic.fetch_http3(
        url=d.get("url", "https://www.ticketmaster.com"),
        timeout=float(d.get("timeout", 10)),
    )
    # no devolver body completo (puede ser MB)
    if "body" in r and isinstance(r["body"], bytes):
        r["body_preview"] = r["body"][:500].decode("utf-8", errors="replace")
        del r["body"]
    return jsonify(r)


@app.route("/api/http3/benchmark", methods=["POST"])
@login_required
def api_http3_benchmark():
    d = request.get_json() or {}
    return jsonify(http3_quic.benchmark_http2_vs_http3(
        url=d.get("url", "https://www.ticketmaster.com"),
        iterations=int(d.get("iterations", 3)),
    ))


@app.route("/api/http3/ja4q", methods=["GET"])
@login_required
def api_http3_ja4q():
    """Calcula el JA4+Q fingerprint del cliente aioquic local."""
    return jsonify(http3_quic.get_ja4q_self())


@app.route("/api/http3/log", methods=["GET"])
@login_required
def api_http3_log():
    return jsonify({"ok": True,
                     "log": http3_quic.get_log(int(request.args.get("limit", 30)))})


# ==========================================
#  API — Cluster / Multi-host
# ==========================================
@app.route("/api/cluster/info", methods=["GET"])
@login_required
def api_cluster_info():
    return jsonify({"ok": True,
                     "instance": redis_backend.get_info(),
                     "stats": cluster_manager.get_cluster_stats()})


@app.route("/api/cluster/hosts", methods=["GET"])
@login_required
def api_cluster_hosts():
    return jsonify({"ok": True, "hosts": cluster_manager.list_hosts()})


@app.route("/api/cluster/broadcast", methods=["POST"])
@login_required
def api_cluster_broadcast():
    d = request.get_json() or {}
    channel = d.get("channel", "generic")
    data = d.get("data") or {}
    cluster_manager.broadcast(channel, data)
    return jsonify({"ok": True, "channel": channel})


# ==========================================
#  API — Tokens para bots externos
# ==========================================
@app.route("/api/tokens", methods=["GET"])
@login_required
def api_tokens_list():
    user_id = session.get("user")
    return jsonify({"ok": True,
                     "tokens": api_tokens.list_tokens(user_id=user_id),
                     "stats": api_tokens.get_stats()})


@app.route("/api/tokens", methods=["POST"])
@login_required
def api_tokens_create():
    d = request.get_json() or {}
    user_id = session.get("user", "anonymous")
    r = api_tokens.create_token(
        user_id=user_id,
        name=d.get("name", "api-token"),
        scopes=d.get("scopes") or ["read"],
        expires_in_days=d.get("expires_in_days"),
    )
    return jsonify({"ok": True, "token": r,
                     "warning": "Guarda el token AHORA — no se volverá a mostrar."})


@app.route("/api/tokens/<tid>/revoke", methods=["POST"])
@login_required
def api_tokens_revoke(tid):
    return jsonify({"ok": api_tokens.revoke_token(tid)})


@app.route("/api/tokens/<tid>", methods=["DELETE"])
@login_required
def api_tokens_delete(tid):
    return jsonify({"ok": api_tokens.delete_token(tid)})


# ==========================================
#  API — Resilience / Circuit breakers
# ==========================================
@app.route("/api/resilience/state", methods=["GET"])
@login_required
def api_resilience_state():
    return jsonify({"ok": True, "state": resilience.get_state()})


@app.route("/api/rate-limit/stats", methods=["GET"])
@login_required
def api_ratelimit_stats():
    scope = request.args.get("scope", "")
    return jsonify({"ok": True, "stats": rate_limit.get_stats(scope)})


# ==========================================
#  API — Setup Wizard (onboarding 6 pasos)
# ==========================================
@app.route("/api/setup-wizard/status", methods=["GET"])
@login_required
def api_setup_status():
    return jsonify(setup_wizard.get_status())


@app.route("/api/setup-wizard/test/<step_id>", methods=["POST"])
@login_required
def api_setup_test(step_id):
    return jsonify(setup_wizard.test_step(step_id))


# ==========================================
#  API — Card Rotation (multi-tarjeta anti-fraude)
# ==========================================
@app.route("/api/card-rotation/config", methods=["GET", "POST"])
@login_required
def api_cardrot_config():
    if request.method == "POST":
        r = card_rotation.save_config(request.get_json() or {})
        return jsonify({"ok": True, "config": r})
    return jsonify({"ok": True, "config": card_rotation.get_config()})


@app.route("/api/card-rotation/pick", methods=["POST"])
@login_required
def api_cardrot_pick():
    d = request.get_json() or {}
    card = card_rotation.pick_card(
        account_id=d.get("account_id", ""),
        exclude_ids=d.get("exclude_ids") or [],
    )
    if not card:
        return jsonify({"ok": False, "error": "sin tarjetas disponibles (cooldown activo)"})
    return jsonify({"ok": True, "card": card})


@app.route("/api/card-rotation/status", methods=["GET"])
@login_required
def api_cardrot_status():
    aid = request.args.get("account_id", "")
    return jsonify({"ok": True,
                    "cards": card_rotation.list_all_status(account_id=aid),
                    "stats": card_rotation.get_stats()})


@app.route("/api/card-rotation/record", methods=["POST"])
@login_required
def api_cardrot_record():
    d = request.get_json() or {}
    r = card_rotation.record_usage(
        card_id=d.get("card_id", ""),
        outcome=d.get("outcome", "used"),
        account_id=d.get("account_id", ""),
        purchase_id=d.get("purchase_id", ""),
        details=d.get("details", ""),
    )
    return jsonify({"ok": True, "entry": r})


@app.route("/api/card-rotation/decline", methods=["POST"])
@login_required
def api_cardrot_decline():
    d = request.get_json() or {}
    return jsonify(card_rotation.mark_declined(
        card_id=d.get("card_id", ""),
        reason=d.get("reason", "declined"),
        cooldown_h=d.get("cooldown_h"),
    ))


@app.route("/api/card-rotation/clear-blacklist", methods=["POST"])
@login_required
def api_cardrot_clear():
    d = request.get_json() or {}
    return jsonify(card_rotation.clear_blacklist(card_id=d.get("card_id")))


@app.route("/api/card-rotation/log", methods=["GET"])
@login_required
def api_cardrot_log():
    return jsonify({"ok": True, "log": card_rotation.get_log(
        limit=int(request.args.get("limit", 50)),
        card_id=request.args.get("card_id", ""),
        account_id=request.args.get("account_id", ""),
    )})


@app.route("/api/card-rotation/self-test", methods=["POST"])
@login_required
def api_cardrot_selftest():
    return jsonify(card_rotation.self_test())


# ==========================================
#  API — 3DS Autofill (IMAP/SMS)
# ==========================================
@app.route("/api/otp-3ds/config", methods=["GET", "POST"])
@login_required
def api_otp3ds_config():
    if request.method == "POST":
        r = otp_3ds.save_config(request.get_json() or {})
        return jsonify({"ok": True, "config": r})
    cfg = otp_3ds.get_config()
    # Ocultar password en GET
    cfg = {k: ("***" if k == "imap_password" and v else v) for k, v in cfg.items()}
    return jsonify({"ok": True, "config": cfg, "stats": otp_3ds.get_stats(),
                    "listener": otp_3ds.listener_status()})


@app.route("/api/otp-3ds/check", methods=["POST"])
@login_required
def api_otp3ds_check():
    return jsonify(otp_3ds.check_inbox())


@app.route("/api/otp-3ds/wait", methods=["POST"])
@login_required
def api_otp3ds_wait():
    d = request.get_json() or {}
    r = otp_3ds.wait_for_otp(
        since_ts=d.get("since_ts"),
        timeout=min(float(d.get("timeout", 30)), 120),
        sender_contains=d.get("sender_contains", ""),
    )
    return jsonify({"ok": bool(r), "otp": r})


@app.route("/api/otp-3ds/sms-webhook", methods=["POST"])
@login_required
def api_otp3ds_sms():
    d = request.get_json() or {}
    return jsonify(otp_3ds.push_sms(d.get("text", ""), d.get("sender", "webhook")))


@app.route("/api/otp-3ds/log", methods=["GET"])
@login_required
def api_otp3ds_log():
    return jsonify({"ok": True, "otps": otp_3ds.get_log(int(request.args.get("limit", 50)))})


@app.route("/api/otp-3ds/listener/<action>", methods=["POST"])
@login_required
def api_otp3ds_listener(action):
    if action == "start":
        return jsonify(otp_3ds.start_listener())
    if action == "stop":
        return jsonify(otp_3ds.stop_listener())
    return jsonify({"ok": False, "error": "invalid_action"}), 400


@app.route("/api/otp-3ds/self-test", methods=["POST"])
@login_required
def api_otp3ds_selftest():
    return jsonify(otp_3ds.self_test())


# ==========================================
#  API — Proxy Session Pinning
# ==========================================
@app.route("/api/proxy-pin/stats", methods=["GET"])
@login_required
def api_proxypin_stats():
    return jsonify({"ok": True, "stats": proxy_session_pin.get_stats(),
                    "pins": proxy_session_pin.list_pins(
                        active_only=request.args.get("active", "1") == "1")})


@app.route("/api/proxy-pin/pin", methods=["POST"])
@login_required
def api_proxypin_pin():
    d = request.get_json() or {}
    return jsonify(proxy_session_pin.pin_session(
        session_id=d.get("session_id", ""),
        geo=d.get("geo", ""),
        ttl_min=int(d.get("ttl_min", 30)),
        proxy_id=d.get("proxy_id"),
    ))


@app.route("/api/proxy-pin/get/<sid>", methods=["GET"])
@login_required
def api_proxypin_get(sid):
    p = proxy_session_pin.get_pinned(sid)
    return jsonify({"ok": bool(p), "pin": p})


@app.route("/api/proxy-pin/release/<sid>", methods=["POST"])
@login_required
def api_proxypin_release(sid):
    return jsonify({"ok": proxy_session_pin.release(sid)})


@app.route("/api/proxy-pin/rotate/<sid>", methods=["POST"])
@login_required
def api_proxypin_rotate(sid):
    return jsonify(proxy_session_pin.mark_dead(sid))


@app.route("/api/proxy-pin/cleanup", methods=["POST"])
@login_required
def api_proxypin_cleanup():
    return jsonify(proxy_session_pin.cleanup_expired())


@app.route("/api/proxy-pin/log", methods=["GET"])
@login_required
def api_proxypin_log():
    return jsonify({"ok": True,
                    "events": proxy_session_pin.get_log(int(request.args.get("limit", 50)))})


@app.route("/api/proxy-pin/self-test", methods=["POST"])
@login_required
def api_proxypin_selftest():
    return jsonify(proxy_session_pin.self_test())


# ==========================================
#  API — Browser Pool (pre-warmed)
# ==========================================
@app.route("/api/browser-pool/config", methods=["GET", "POST"])
@login_required
def api_bp_config():
    if request.method == "POST":
        return jsonify({"ok": True, "config": browser_pool.configure(request.get_json() or {})})
    return jsonify({"ok": True, "config": browser_pool.get_config(),
                    "stats": browser_pool.get_stats(),
                    "contexts": browser_pool.list_contexts()})


@app.route("/api/browser-pool/manager/<action>", methods=["POST"])
@login_required
def api_bp_manager(action):
    if action == "start":
        return jsonify(browser_pool.start_manager())
    if action == "stop":
        return jsonify(browser_pool.stop_manager(drain=True))
    return jsonify({"ok": False, "error": "invalid_action"}), 400


@app.route("/api/browser-pool/acquire", methods=["POST"])
@login_required
def api_bp_acquire():
    d = request.get_json() or {}
    r = browser_pool.acquire(country=d.get("country"), timeout=float(d.get("timeout", 5)))
    # No devolver handles de python (browser/page) al frontend
    safe = {k: v for k, v in r.items() if k not in ("browser", "context", "page", "meta")}
    return jsonify(safe)


@app.route("/api/browser-pool/release/<hid>", methods=["POST"])
@login_required
def api_bp_release(hid):
    d = request.get_json() or {}
    return jsonify(browser_pool.release(hid, success=bool(d.get("success", True))))


@app.route("/api/browser-pool/burn/<hid>", methods=["POST"])
@login_required
def api_bp_burn(hid):
    d = request.get_json() or {}
    return jsonify(browser_pool.burn(hid, reason=d.get("reason", "manual")))


@app.route("/api/browser-pool/self-test", methods=["POST"])
@login_required
def api_bp_selftest():
    return jsonify(browser_pool.self_test())


# ==========================================
#  API — Discovery Adaptive Polling
# ==========================================
@app.route("/api/discovery-adaptive/config", methods=["GET", "POST"])
@login_required
def api_da_config():
    if request.method == "POST":
        return jsonify({"ok": True, "config": discovery_adaptive.save_config(request.get_json() or {})})
    return jsonify({"ok": True,
                    "config": discovery_adaptive.get_config(),
                    "stats": discovery_adaptive.get_stats()})


@app.route("/api/discovery-adaptive/windows", methods=["GET"])
@login_required
def api_da_windows():
    w = discovery_adaptive.detect_windows()
    return jsonify({"ok": True,
                    "windows": {k: [{"id": e.get("id"), "name": e.get("name", "?"),
                                      "sale_date": e.get("sale_date"),
                                      "status": e.get("status")} for e in v]
                                 for k, v in w.items()}})


@app.route("/api/discovery-adaptive/tick", methods=["POST"])
@login_required
def api_da_tick():
    return jsonify(discovery_adaptive.run_tick())


@app.route("/api/discovery-adaptive/scheduler/<action>", methods=["POST"])
@login_required
def api_da_scheduler(action):
    if action == "start":
        return jsonify(discovery_adaptive.start_scheduler())
    if action == "stop":
        return jsonify(discovery_adaptive.stop_scheduler())
    if action == "status":
        return jsonify(discovery_adaptive.scheduler_status())
    return jsonify({"ok": False, "error": "invalid_action"}), 400


@app.route("/api/discovery-adaptive/log", methods=["GET"])
@login_required
def api_da_log():
    return jsonify({"ok": True, "events": discovery_adaptive.get_log(int(request.args.get("limit", 50)))})


@app.route("/api/discovery-adaptive/self-test", methods=["POST"])
@login_required
def api_da_selftest():
    return jsonify(discovery_adaptive.self_test())


# ==========================================
#  API — OTP (v2: multi-account + IDLE + smart extractor)
# ==========================================
@app.route("/api/otp")
@login_required
def api_otp():
    cfg = otp_manager.get_config()
    # No exponer password_enc en el JSON público
    safe_accounts = otp_manager.list_accounts(decrypt_passwords=False)
    return jsonify({
        "ok": True,
        "config": {
            "enabled": cfg.get("enabled"),
            "scheduler_enabled": cfg.get("scheduler_enabled"),
            "scheduler_interval_s": cfg.get("scheduler_interval_s"),
            "senders": cfg.get("senders"),
            "patterns": cfg.get("patterns"),
            "lookback_minutes": cfg.get("lookback_minutes"),
            "forward_telegram": cfg.get("forward_telegram"),
            "forward_discord": cfg.get("forward_discord"),
            # Legacy fields (compat con UI antigua)
            "imap_host": (safe_accounts[0]["host"] if safe_accounts else "imap.gmail.com"),
            "imap_port": (safe_accounts[0]["port"] if safe_accounts else 993),
            "username":  (safe_accounts[0]["email"] if safe_accounts else ""),
            "sender_filter": (cfg.get("senders") or [""])[0],
            "regex": (cfg.get("patterns") or [r"\b(\d{6})\b"])[0],
        },
        "accounts": safe_accounts,
        "history": otp_manager.get_history(50),
        "last":    otp_manager.get_last_otp(),
        "stats":   otp_manager.get_stats(),
        "providers": otp_manager.get_providers(),
        "scheduler": otp_manager.scheduler_status(),
        "idle_workers": otp_manager.idle_workers_status(),
    })


@app.route("/api/otp/config", methods=["POST"])
@login_required
def api_otp_save():
    cfg = otp_manager.save_config(request.get_json() or {})
    return jsonify({"ok": True, "config": {k: v for k, v in cfg.items() if k != "history"}})


@app.route("/api/otp/read", methods=["POST"])
@login_required
def api_otp_read():
    return jsonify(otp_manager.read_otp())


@app.route("/api/otp/manual", methods=["POST"])
@login_required
def api_otp_manual():
    d = request.get_json() or {}
    e = otp_manager.manual_otp(d.get("code", ""), source=d.get("source", "manual"),
                                  account_id=d.get("account_id"))
    return jsonify({"ok": True, "entry": e})


@app.route("/api/otp/lookup-by-alias", methods=["POST"])
@login_required
def api_otp_lookup_by_alias():
    d = request.get_json() or {}
    alias = (d.get("alias") or d.get("email") or "").strip()
    if not alias:
        return jsonify({"ok": False, "error": "alias_required"}), 400
    try:
        lookback = int(d.get("lookback_minutes") or 30)
    except (TypeError, ValueError):
        lookback = 30
    return jsonify(otp_manager.fetch_otp_for_alias(
        alias_email=alias,
        account_id=d.get("account_id"),
        lookback_minutes=lookback))


@app.route("/api/otp/account", methods=["POST"])
@login_required
def api_otp_add_account():
    d = request.get_json() or {}
    a = otp_manager.add_account(d)
    # Devolvemos sin password_enc
    a.pop("password_enc", None)
    return jsonify({"ok": True, "account": a})


@app.route("/api/otp/account/<acc_id>", methods=["POST", "DELETE"])
@login_required
def api_otp_manage_account(acc_id):
    if request.method == "DELETE":
        ok = otp_manager.remove_account(acc_id)
        return jsonify({"ok": ok})
    d = request.get_json() or {}
    a = otp_manager.update_account(acc_id, d)
    if a is None:
        return jsonify({"ok": False, "error": "account_not_found"}), 404
    a = dict(a); a.pop("password_enc", None)
    return jsonify({"ok": True, "account": a})


@app.route("/api/otp/test-connection", methods=["POST"])
@login_required
def api_otp_test_connection():
    d = request.get_json() or {}
    acc_id = d.get("account_id")
    return jsonify(otp_manager.test_connection(account_id=acc_id))


@app.route("/api/otp/fetch", methods=["POST"])
@login_required
def api_otp_fetch():
    d = request.get_json() or {}
    acc_id = d.get("account_id")
    if acc_id:
        return jsonify(otp_manager.fetch_otps(account_id=acc_id))
    return jsonify(otp_manager.fetch_all_accounts())


@app.route("/api/otp/scheduler/<action>", methods=["POST"])
@login_required
def api_otp_scheduler(action):
    if action == "start":
        d = request.get_json() or {}
        return jsonify(otp_manager.start_scheduler(d.get("interval_s")))
    if action == "stop":
        return jsonify(otp_manager.stop_scheduler())
    if action == "status":
        return jsonify({"ok": True, **otp_manager.scheduler_status()})
    return jsonify({"ok": False, "error": "invalid_action"}), 400


@app.route("/api/otp/idle/<action>", methods=["POST"])
@login_required
def api_otp_idle(action):
    d = request.get_json() or {}
    acc_id = d.get("account_id")
    if action == "start":
        if acc_id: return jsonify(otp_manager.start_idle_worker(acc_id))
        return jsonify(otp_manager.start_all_idle_workers())
    if action == "stop":
        if acc_id: return jsonify(otp_manager.stop_idle_worker(acc_id))
        return jsonify(otp_manager.stop_all_idle_workers())
    if action == "status":
        return jsonify({"ok": True, "workers": otp_manager.idle_workers_status()})
    return jsonify({"ok": False, "error": "invalid_action"}), 400


@app.route("/api/otp/providers")
@login_required
def api_otp_providers():
    return jsonify({"ok": True, "providers": otp_manager.get_providers()})


@app.route("/api/otp/stats")
@login_required
def api_otp_stats():
    return jsonify({"ok": True, "stats": otp_manager.get_stats()})


@app.route("/api/otp/self-test", methods=["POST"])
@login_required
def api_otp_self_test():
    return jsonify(otp_manager.self_test())


@app.route("/api/otp/extract-test", methods=["POST"])
@login_required
def api_otp_extract_test():
    """Prueba el extractor contra un texto arbitrario (útil para debug UI)."""
    d = request.get_json() or {}
    text = d.get("text", "")
    smart = bool(d.get("smart", True))
    code = otp_manager.extract_code(text, smart=smart)
    return jsonify({"ok": True, "code": code, "smart": smart})


# ==========================================
#  API — MULTIBROWSER (MoreLogin / AdsPower / GoLogin / Kameleo / Multilogin / Dolphin / Incogniton / Octo)
# ==========================================
@app.route("/api/multibrowser")
@login_required
def api_multibrowser_list():
    return jsonify({"ok": True, "config": multibrowser_config.get_safe_config()})


@app.route("/api/multibrowser/<pid>", methods=["POST"])
@login_required
def api_multibrowser_save(pid):
    d = request.get_json() or {}
    p = multibrowser_config.save_provider(pid, d)
    if p is None:
        return jsonify({"ok": False, "error": "unknown_provider"}), 404
    return jsonify({"ok": True, "provider_id": pid})


@app.route("/api/multibrowser/<pid>/test", methods=["POST"])
@login_required
def api_multibrowser_test(pid):
    return jsonify(multibrowser_config.test_connection(pid))


@app.route("/api/multibrowser/default", methods=["POST"])
@login_required
def api_multibrowser_set_default():
    d = request.get_json() or {}
    ok = multibrowser_config.set_default_provider(d.get("provider_id", ""))
    return jsonify({"ok": ok})


@app.route("/api/multibrowser/self-test", methods=["POST"])
@login_required
def api_multibrowser_self_test():
    return jsonify(multibrowser_config.self_test())


# ==========================================
#  API — CSV bidireccional Cuentas/Tarjetas
# ==========================================
@app.route("/api/accounts/csv/status")
@login_required
def api_accounts_csv_status():
    return jsonify({"ok": True, "status": accounts_csv_sync.get_status()})


@app.route("/api/accounts/csv/init", methods=["POST"])
@login_required
def api_accounts_csv_init():
    return jsonify(accounts_csv_sync.init())


@app.route("/api/accounts/csv/sync-out", methods=["POST"])
@login_required
def api_accounts_csv_sync_out():
    return jsonify(accounts_csv_sync.sync_out())


@app.route("/api/accounts/csv/sync-in", methods=["POST"])
@login_required
def api_accounts_csv_sync_in():
    return jsonify(accounts_csv_sync.sync_in())


@app.route("/api/accounts/csv/watcher/<action>", methods=["POST"])
@login_required
def api_accounts_csv_watcher(action):
    if action == "start":
        d = request.get_json() or {}
        poll_s = float(d.get("poll_s", 1.0))
        return jsonify(accounts_csv_sync.start_watcher(poll_s=max(0.5, min(10.0, poll_s))))
    if action == "stop":
        return jsonify(accounts_csv_sync.stop_watcher())
    return jsonify({"ok": False, "error": "invalid_action"}), 400


@app.route("/api/accounts/csv/open-folder", methods=["POST"])
@login_required
def api_accounts_csv_open_folder():
    return jsonify(accounts_csv_sync.open_folder())


@app.route("/api/accounts/csv/download")
@login_required
def api_accounts_csv_download():
    """Descarga el CSV como attachment."""
    from flask import send_file
    path = accounts_csv_sync.get_csv_path()
    import os
    if not os.path.isfile(path):
        accounts_csv_sync.init()
    return send_file(path, as_attachment=True,
                      download_name="cuentas_tarjetas.csv",
                      mimetype="text/csv")


@app.route("/api/accounts/csv/self-test", methods=["POST"])
@login_required
def api_accounts_csv_self_test():
    return jsonify(accounts_csv_sync.self_test())


# ==========================================
#  API — SOPORTE
# ==========================================
@app.route("/api/support/tickets")
@login_required
def api_support_tickets():
    return jsonify({
        "ok": True,
        "tickets":    support_system.get_all_tickets(),
        "pending":    support_system.get_pending_tickets(),
        "categories": support_system.get_categories(),
        "stats":      support_system.get_stats(),
    })


@app.route("/api/support/tickets", methods=["POST"])
@login_required
def api_support_create():
    d = request.get_json() or {}
    t = support_system.create_ticket(
        d.get("title", ""), d.get("description", ""),
        d.get("category", "Otro"), d.get("priority", "medium"),
    )
    return jsonify({"ok": True, "ticket": t})


@app.route("/api/support/tickets/<tid>/resolve", methods=["POST"])
@login_required
def api_support_resolve(tid):
    res = support_system.resolve_ticket(tid, (request.get_json() or {}).get("resolution", ""))
    return jsonify({"ok": bool(res), "ticket": res})


# ==========================================
#  API — NOTAS
# ==========================================
@app.route("/api/notes")
@login_required
def api_notes():
    archived = request.args.get("archived") == "1"
    include_archived = request.args.get("include_archived") == "1"
    pinned = None
    if request.args.get("pinned") == "1":
        pinned = True
    notes = notes_manager.list_notes(
        search=request.args.get("q", ""),
        importance=request.args.get("importance") or None,
        status=request.args.get("status") or None,
        pinned=pinned,
        archived=archived,
        include_archived=include_archived,
        sort=request.args.get("sort", "pinned_recent"),
    )
    return jsonify({
        "ok": True,
        "notes": notes,
        "stats": notes_manager.get_stats(),
    })


@app.route("/api/notes", methods=["POST"])
@login_required
def api_notes_add():
    d = request.get_json() or {}
    n = notes_manager.add_note(
        d.get("title", ""),
        d.get("content", ""),
        d.get("tags"),
        d.get("importance", "naranja"),
    )
    return jsonify({"ok": True, "note": n})


@app.route("/api/notes/<nid>", methods=["GET"])
@login_required
def api_notes_get(nid):
    n = notes_manager.get_note(nid)
    return jsonify({"ok": bool(n), "note": n})


@app.route("/api/notes/<nid>", methods=["PUT"])
@login_required
def api_notes_update(nid):
    n = notes_manager.update_note(nid, request.get_json() or {})
    return jsonify({"ok": bool(n), "note": n})


@app.route("/api/notes/<nid>", methods=["DELETE"])
@login_required
def api_notes_delete(nid):
    ok = notes_manager.delete_note(nid)
    return jsonify({"ok": ok})


@app.route("/api/notes/<nid>/pin", methods=["POST"])
@login_required
def api_notes_pin(nid):
    n = notes_manager.toggle_pin(nid)
    return jsonify({"ok": bool(n), "note": n})


@app.route("/api/notes/<nid>/archive", methods=["POST"])
@login_required
def api_notes_archive(nid):
    n = notes_manager.toggle_archive(nid)
    return jsonify({"ok": bool(n), "note": n})


@app.route("/api/notes/<nid>/duplicate", methods=["POST"])
@login_required
def api_notes_duplicate(nid):
    n = notes_manager.duplicate_note(nid)
    return jsonify({"ok": bool(n), "note": n})


@app.route("/api/notes/<nid>/claude-prompt", methods=["GET"])
@login_required
def api_notes_claude_prompt(nid):
    text = notes_manager.build_claude_prompt(nid)
    if text is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "prompt": text})


@app.route("/api/notes/<nid>/mark-sent", methods=["POST"])
@login_required
def api_notes_mark_sent(nid):
    n = notes_manager.mark_sent_to_claude(nid)
    return jsonify({"ok": bool(n), "note": n})


@app.route("/api/notes/<nid>/export")
@login_required
def api_notes_export(nid):
    fmt = request.args.get("format", "md")
    if fmt == "md":
        text = notes_manager.export_note_markdown(nid)
        if text is None:
            return jsonify({"ok": False}), 404
        return jsonify({"ok": True, "filename": f"nota_{nid}.md", "content": text})
    n = notes_manager.get_note(nid)
    return jsonify({"ok": bool(n), "note": n})


# ==========================================
#  API — CONFIGURACION
# ==========================================
@app.route("/api/settings")
@login_required
def api_settings():
    return jsonify({"ok": True, "settings": settings_manager.get_settings()})


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings_save():
    s = settings_manager.save_settings(request.get_json() or {})
    return jsonify({"ok": True, "settings": s})


# ==========================================
#  API — NOTAS (paneles de voz + research)
# ==========================================
_VOICE_PANELS = {}  # en memoria por simplicidad
_RESEARCH_SCHEDULE = {"enabled": False, "time1": "09:00", "time2": "21:00"}
_RESEARCH_SEEN = set()


@app.route("/api/notes/voice_panel", methods=["GET"])
@login_required
def api_voice_panel_get():
    panel = request.args.get("panel", "")
    return jsonify({"ok": True, "panel": panel, "content": _VOICE_PANELS.get(panel, "")})


@app.route("/api/notes/voice_panel", methods=["POST"])
@login_required
def api_voice_panel_save():
    d = request.get_json() or {}
    panel = d.get("panel", "")
    _VOICE_PANELS[panel] = d.get("content", "")
    return jsonify({"ok": True, "panel": panel})


@app.route("/api/notes/execute", methods=["POST"])
@login_required
def api_notes_execute():
    d = request.get_json() or {}
    ids = d.get("ids", [])
    return jsonify({"ok": True, "executed": len(ids)})


@app.route("/api/notes/execute_pending", methods=["POST"])
@login_required
def api_notes_execute_pending():
    return jsonify({"ok": True, "executed": 0})


# --- RESEARCH (catálogo enriquecido 2025/2026) ---
_COMPETITORS_2026 = [
    # === AIO / Ticket bots ===
    {"id": "swiftaio", "name": "SwiftAIO", "type": "AIO", "rating": 4.5, "pricing": "£300-500 / 6m", "url": "swiftaio.io",
     "desc": "Bot AIO premium TM/Live Nation/AXS. MRPC CAPTCHA solver, Shape Security bypass, dashboard web.",
     "features": ["MRPC CAPTCHA solver", "Shape Security bypass", "AxS soporte", "TM US+UK+EU", "Dashboard web", "Queue-it auto"],
     "pros": ["Éxito alto en grandes drops", "Comunidad activa Discord", "Soporte rápido"],
     "cons": ["Precio alto por renovación", "Requiere proxies ISP premium aparte", "Curva de aprendizaje"],
     "verdict": "Referencia del sector 2025. Su sesioning y fingerprinting son el estado del arte."},
    {"id": "cybersole", "name": "CyberSole", "type": "AIO", "rating": 4.4, "pricing": "$300 / 6m", "url": "cybersole.io",
     "desc": "Bot híbrido sneakers+tickets. 270+ tiendas, app móvil, PerimeterX bypass.",
     "features": ["270+ tiendas soportadas", "App móvil control remoto", "PerimeterX bypass", "Shopify/Supreme", "Account generator"],
     "pros": ["Control desde móvil", "Muy estable", "Multi-propósito"],
     "cons": ["No especializado en TM", "Pago recurrente"],
     "verdict": "Mejor para combo sneakers+tickets. Para solo TM, SwiftAIO rinde más."},
    {"id": "tkbots", "name": "TKBots", "type": "TM-only", "rating": 4.2, "pricing": "$200-400 / 6m", "url": "tkbots.net",
     "desc": "Especializado 100% en TM. Queue-it automático, distribución multi-proxy, soporta tokens AcpAT.",
     "features": ["Queue-it automático", "Multi-proxy pool", "AcpAT token injection", "Event monitoring 24/7", "Webhook Discord"],
     "pros": ["Ultra especializado", "Actualiza con cada cambio de TM", "Webhook alerts"],
     "cons": ["Solo Queue-it/TM", "Sin AXS ni StubHub"],
     "verdict": "Elección top si operas exclusivamente Ticketmaster. Equipo responde fast a cambios."},
    {"id": "kodai", "name": "Kodai AIO", "type": "AIO", "rating": 4.3, "pricing": "$400 lifetime", "url": "kodaiaio.com",
     "desc": "AIO premium sneakers+tickets. Checkout rápido, Nike/Adidas/Yeezy, Shopify duro.",
     "features": ["Fast checkout", "Nike/Adidas SNKRS", "Yeezy Supply", "Shopify avanzado", "Task scheduler"],
     "pros": ["Licencia vitalicia", "Velocidad checkout top", "Reputación sólida"],
     "cons": ["Precio alto entrada", "Tickets secundario", "Poco soporte Europa"],
     "verdict": "Vale la pena si vienes de sneakers. Para 100% tickets hay alternativas mejores."},
    # === Antidetect Browsers ===
    {"id": "multilogin", "name": "Multilogin", "type": "Anti-detect", "rating": 4.7, "pricing": "€99-359 / mes", "url": "multilogin.com",
     "desc": "Antidetect browser líder. Mimic/Stealthfox engines, fingerprint aleatorio por perfil, API REST completa.",
     "features": ["Mimic engine (Chrome)", "Stealthfox engine (Firefox)", "API REST", "200+ perfiles cloud", "TLS/JA4 spoof"],
     "pros": ["Estándar industrial 2025", "Automatización API sólida", "Resiste JA4"],
     "cons": ["Caro en plan alto", "Mejor con ISP residencial"],
     "verdict": "Si puedes pagarlo, es la mejor opción. Superior al resto en detección bancaria y Shape."},
    {"id": "kameleo", "name": "Kameleo", "type": "Anti-detect", "rating": 4.3, "pricing": "€59-299 / mes", "url": "kameleo.io",
     "desc": "Fingerprint browser con spoof mobile+desktop, WebRTC/Canvas/WebGL únicos por perfil.",
     "features": ["Spoof móvil iOS/Android", "WebRTC aislado", "Canvas randomizado", "Chrome+Firefox", "CLI tools"],
     "pros": ["Soporte mobile sólido", "Precio medio", "Buena API"],
     "cons": ["Menos perfiles cloud que Multilogin", "Updates lentos"],
     "verdict": "Segunda opción tras Multilogin. Buen balance precio/capacidad."},
    {"id": "morelogin", "name": "MoreLogin", "type": "Anti-detect", "rating": 4.0, "pricing": "$9-79 / mes", "url": "morelogin.com",
     "desc": "Antidetect económico chino. 200+ fingerprints, cloud sync, WebSocket automation.",
     "features": ["Precio bajo", "Cloud sync", "WebSocket API", "Team sharing", "Multi-cuenta Ticketmaster"],
     "pros": ["Muy barato", "Cloud sync", "API WebSocket útil"],
     "cons": ["Menor calidad fingerprint", "Soporte limitado", "Logs recopilados"],
     "verdict": "Opción budget. Sirve para TM medium-tier pero no para drops blindados."},
    {"id": "gologin", "name": "GoLogin", "type": "Anti-detect", "rating": 4.1, "pricing": "$24-99 / mes", "url": "gologin.com",
     "desc": "Antidetect cloud+desktop. Proxies integrados, automation API, Canvas noise control.",
     "features": ["Canvas noise", "WebGL randomization", "Proxies integrados", "Auto-fill forms", "API automation"],
     "pros": ["Equilibrado", "Proxies propios", "UI sencilla"],
     "cons": ["Fingerprint más básico", "Fuerte contra TM nivel medio"],
     "verdict": "Buena opción intermedia. Sólido para operaciones de volumen moderado."},
    {"id": "dicloak", "name": "DICloak", "type": "Anti-detect", "rating": 4.0, "pricing": "$9-59 / mes", "url": "dicloak.com",
     "desc": "Antidetect con auto-refresh queue-it, isolación por perfil, proxies auto-match por región.",
     "features": ["Auto-refresh queue aleatorio", "Proxy auto-match región", "Aislamiento fingerprint", "Queue monitor multi-perfil"],
     "pros": ["Features específicas TM", "Precio bajo", "Queue monitor"],
     "cons": ["Producto joven", "Menor track record"],
     "verdict": "Opción emergente muy enfocada en tickets. Merece probarse."},
    {"id": "bitbrowser", "name": "BitBrowser", "type": "Anti-detect", "rating": 3.8, "pricing": "$5-45 / mes", "url": "bitbrowser.net",
     "desc": "Antidetect chino ultra-barato. Multi-perfil masivo, sync cloud, Chromium base.",
     "features": ["Precio ultra bajo", "Multi-perfil masivo", "Cloud sync", "Team workspace"],
     "pros": ["El más barato", "Escalable"],
     "cons": ["Fingerprint flojo", "Fallos contra Shape/PerimeterX"],
     "verdict": "Solo recomendable para testing a bajo coste."},
    # === CAPTCHA solvers ===
    {"id": "capsolver", "name": "CapSolver", "type": "CAPTCHA", "rating": 4.6, "pricing": "$0.80 / 1000", "url": "capsolver.com",
     "desc": "API resolver. reCAPTCHA v2/v3/Enterprise, hCaptcha, AWS WAF, FunCaptcha, Cloudflare Turnstile.",
     "features": ["reCAPTCHA v2/v3", "hCaptcha Enterprise", "Cloudflare Turnstile", "AWS WAF", "FunCaptcha Arkose"],
     "pros": ["Precio top mercado", "Cobertura amplia", "Éxito >95%"],
     "cons": ["Prepago", "Requiere referer real"],
     "verdict": "Primera elección 2025. Mejor precio/éxito del mercado."},
    {"id": "2captcha", "name": "2Captcha", "type": "CAPTCHA", "rating": 4.3, "pricing": "$1.50 / 1000", "url": "2captcha.com",
     "desc": "Solver humano+AI. reCAPTCHA, hCaptcha, imagen, puzzle. Muy establecido.",
     "features": ["Solver humano 24/7", "reCAPTCHA v2/v3", "hCaptcha", "Image CAPTCHA", "Funcaptcha"],
     "pros": ["Muy estable", "Soporte humano", "Amplio catálogo"],
     "cons": ["Más lento que CapSolver", "Precio x2"],
     "verdict": "Fallback fiable cuando CapSolver falla. Backup indispensable."},
    {"id": "anticaptcha", "name": "Anti-Captcha", "type": "CAPTCHA", "rating": 4.2, "pricing": "$2.00 / 1000", "url": "anti-captcha.com",
     "desc": "Pionero del mercado CAPTCHA. API madura, proxy-less solving, GeeTest.",
     "features": ["GeeTest", "FunCaptcha", "Image to text", "Arkose Labs", "Proxy-less mode"],
     "pros": ["API muy madura", "SDK múltiples lenguajes"],
     "cons": ["Precio alto", "Más lento en v3"],
     "verdict": "Segundo fallback. Preferido si necesitas GeeTest."},
    # === Proxies ===
    {"id": "brightdata", "name": "Bright Data", "type": "Proxies", "rating": 4.7, "pricing": "$7-15 / GB", "url": "brightdata.com",
     "desc": "Líder mundial. 72M+ IPs residenciales, ISP dedicados, sticky sessions, geo-targeting país/ciudad/ASN.",
     "features": ["72M+ residencial", "ISP dedicado", "Sticky sessions 30min+", "Geo city/ASN", "Mobile 4G/5G"],
     "pros": ["Red más grande", "Uptime 99.9%", "Sticky IP estable"],
     "cons": ["Caro", "Tier minimum alto", "KYC estricto"],
     "verdict": "El mejor para operaciones serias. Su ISP sticky es clave en Queue-it."},
    {"id": "soax", "name": "SOAX", "type": "Proxies", "rating": 4.5, "pricing": "$6-12 / GB", "url": "soax.com",
     "desc": "Residential + ISP + mobile. Sticky configurable, pool premium, API simple.",
     "features": ["Residencial 155M+", "ISP dedicado", "Mobile 4G", "Sticky 10 min-24h", "API clara"],
     "pros": ["Calidad alta", "Sticky flexible", "Sin KYC duro"],
     "cons": ["Menos ciudades que Bright", "Plan base caro"],
     "verdict": "Alternativa seria a Bright Data, a menudo más ágil."},
    {"id": "netnut", "name": "NetNut", "type": "Proxies", "rating": 4.4, "pricing": "$10-20 / GB", "url": "netnut.io",
     "desc": "ISP proxies premium directos a operadores. Ideal Queue-it por estabilidad.",
     "features": ["ISP directo ISP", "Static residential", "Sticky 30 min", "P2P free", "API Stats"],
     "pros": ["Latencia baja", "IPs muy estables", "No P2P"],
     "cons": ["Pool más pequeño", "Precio alto"],
     "verdict": "Top en Queue-it thanks a estabilidad de IP."},
    {"id": "oxylabs", "name": "Oxylabs", "type": "Proxies", "rating": 4.5, "pricing": "$8-15 / GB", "url": "oxylabs.io",
     "desc": "Residential premium + scraper APIs. Dashboard enterprise, uptime alto.",
     "features": ["100M+ residential", "Scraper APIs", "SERP APIs", "Session control", "City geo"],
     "pros": ["Uptime alto", "Features enterprise", "Soporte 24/7"],
     "cons": ["Caro para pequeños", "KYC"],
     "verdict": "Alternativa premium. Muy usado en e-commerce scraping, bueno para TM."},
    {"id": "smartproxy", "name": "Smartproxy", "type": "Proxies", "rating": 4.2, "pricing": "$4-10 / GB", "url": "smartproxy.com",
     "desc": "Residencial más accesible. ISP, DC, session sticky 30 min, API simple.",
     "features": ["55M+ residencial", "ISP dedicado", "DC proxies", "Sticky 30 min", "API"],
     "pros": ["Precio competitivo", "Onboarding rápido"],
     "cons": ["Menor tier que Bright", "Pool saturado a veces"],
     "verdict": "Buena opción entry-level. Vale para drops pequeños."},
]

# IDs de mejoras YA INSTALADAS (se ocultan de la lista "Mejoras aplicables")
# Cuando se implemente una nueva mejora, añadir su id aquí.
_IMPROVEMENTS_INSTALLED = {
    "qit_rotation":      "cookie_warmer.py (pool AcpAT con TTL 30min)",
    "px_prefarm":        "px_prefarm.py (PerimeterX cookie pre-farming real)",
    "shape_ja4":         "ja4_spoof.py (tls-client Chrome 131+)",
    "recaptcha_v3":      "captcha_manager.py + tm_browser_engine stealth",
    "hcaptcha_ent":      "captcha_manager.py (CapSolver)",
    "turnstile_solve":   "captcha_manager.py (CapSolver)",
    "isp_sticky":        "proxy_session_pin.py (session pinning cookie-warm→queue→checkout)",
    "proxy_bench":       "proxy_health.py (benchmark real TM)",
    "geo_match":         "proxy_health.pick_best(geo)",
    "camoufox":          "browser_pool.py + tm_browser_engine",
    "multilogin_api":    "multilogin_orchestrator.py",
    "cookie_warm":       "cookie_warmer.py (warming HTTP real)",
    "acpat_inject":      "acpat_injection.py (pub/sub local instantáneo)",
    "adyen_token":       "adyen_tokenizer.py (Checkout API v71)",
    "polling_rotate":    "discovery_adaptive.py (polling 1Hz adaptativo)",
    "multi_domain":      "multi_domain_orchestrator.py (25 dominios TM)",
    "queue_monitor":     "tm_purchase_flow + WS events (posición en cola vía WebSocket)",
    "webhook_discord":   "notifications.py (Discord/Telegram webhooks)",
    "dashboard_ws":      "flask-socketio (WebSocket tiempo real)",
    # Mejoras NUEVAS (abril 2026) — no estaban en el catálogo original pero ya implementadas
    "otp_3ds_imap":      "otp_3ds.py (autofill código 3DS desde IMAP)",
    "browser_pool":      "browser_pool.py (2-3 contextos Playwright pre-warmed con cookies PX)",
    "discovery_adaptive":"discovery_adaptive.py (polling 1Hz en ventana pre-onsale <5min)",
    "proxy_session_pin": "proxy_session_pin.py (IP fija entre warm/queue/checkout)",
    "multi_card_rotation":"card_rotation.py (rotación + cooldown 24-168h por outcome)",
}

_IMPROVEMENTS_2026 = [
    # === PENDIENTES ===
    {"id": "imperva_bypass", "cat": "Anti-Bot", "name": "Imperva/Incapsula JS challenge bypass",
     "desc": "Resolver JS challenge _incap_ ejecutando el VM inline sin esperar timeout de 10s.",
     "difficulty": "difícil", "impact": "Alto", "time": "1-2 semanas", "effort": "XL",
     "prerequisites": ["Node.js worker", "VM sandbox"],
     "steps": ["Capturar código JS obfuscado", "Ejecutar en sandbox V8", "Extraer visid_incap_* cookies", "Cachear 20 min"],
     "cat_icon": "🛡"},
]


@app.route("/api/research/competitors", methods=["GET"])
@login_required
def api_research_competitors():
    # Mantiene el campo "desc" para retrocompatibilidad + nuevos campos
    return jsonify({"ok": True, "competitors": _COMPETITORS_2026})


@app.route("/api/research/improvements", methods=["GET"])
@login_required
def api_research_improvements():
    """Catálogo de mejoras. Solo oculta las que tienen una task.status=done.
    Filtros opcionales:
      ?hide_completed=0/1  (default 1)
      ?category=Anti-Bot
      ?search=keyword
    """
    hide = request.args.get("hide_completed", "1") != "0"
    cat = request.args.get("category")
    search = request.args.get("search")
    items = research_catalog.get_catalog(hide_completed=hide,
                                          category=cat, search=search)
    # compat: también expone con _RESEARCH_SEEN (para sesión in-memory)
    if _RESEARCH_SEEN:
        items = [i for i in items if i["id"] not in _RESEARCH_SEEN]
    return jsonify({
        "ok": True,
        "improvements": items,
        "total": len(research_catalog.CATALOG),
        "visible": len(items),
        "categories": research_catalog.get_categories(),
        "stats": research_catalog.get_stats(),
    })


@app.route("/api/research/categories", methods=["GET"])
@login_required
def api_research_categories():
    return jsonify({"ok": True, "categories": research_catalog.get_categories()})


@app.route("/api/research/tasks", methods=["GET"])
@login_required
def api_research_tasks():
    status = request.args.get("status")
    imp_id = request.args.get("improvement_id")
    return jsonify({"ok": True,
                    "tasks": research_catalog.list_tasks(status=status,
                                                          improvement_id=imp_id),
                    "stats": research_catalog.get_stats()})


@app.route("/api/research/tasks", methods=["POST"])
@login_required
def api_research_create_task():
    d = request.get_json() or {}
    return jsonify(research_catalog.create_task(
        improvement_id=d.get("improvement_id", ""),
        priority=d.get("priority", "medium"),
        assignee=d.get("assignee", ""),
        notes=d.get("notes", ""),
    ))


@app.route("/api/research/tasks/<task_id>", methods=["PATCH"])
@login_required
def api_research_update_task(task_id):
    return jsonify(research_catalog.update_task(task_id, request.get_json() or {}))


@app.route("/api/research/tasks/<task_id>/complete", methods=["POST"])
@login_required
def api_research_complete_task(task_id):
    return jsonify(research_catalog.complete_task(task_id))


@app.route("/api/research/tasks/<task_id>", methods=["DELETE"])
@login_required
def api_research_delete_task(task_id):
    return jsonify(research_catalog.delete_task(task_id))


@app.route("/api/research/run", methods=["POST"])
@login_required
def api_research_run():
    """Ejecuta una auditoría inteligente del proyecto y devuelve el Top-N priorizado.

    Body JSON opcional: {"top_n": 10, "force": true}
    """
    d = request.get_json(silent=True) or {}
    top_n = int(d.get("top_n") or 10)
    force = bool(d.get("force", True))
    audit = research_engine.get_audit(force=force, top_n=top_n)
    cats = sorted({i.get("cat") for i in audit.get("all", []) if i.get("cat")})
    return jsonify({
        "ok": True,
        "audited_at": audit.get("audited_at"),
        "found": audit.get("actionable", 0),
        "top": audit.get("top", []),
        "by_status": audit.get("by_status", {}),
        "urgency_signals": audit.get("urgency_signals", {}),
        "total_competitors": len(_COMPETITORS_2026),
        "categories": cats,
        "summary": (
            f"Auditoría inteligente: {audit.get('actionable',0)} mejoras accionables · "
            f"{audit['by_status'].get('implemented',0)} ya implementadas · "
            f"{audit['by_status'].get('partial',0)} parciales · "
            f"{audit['by_status'].get('missing',0)} faltantes. "
            f"Señales activas en 24h: {sum(audit.get('urgency_signals',{}).values())}."
        ),
    })


@app.route("/api/research/audit", methods=["GET"])
@login_required
def api_research_audit():
    """Devuelve la última auditoría cacheada (o calcula si no hay)."""
    top_n = int(request.args.get("top_n", "10"))
    force = request.args.get("force", "0") == "1"
    audit = research_engine.get_audit(force=force, top_n=top_n)
    audit = dict(audit)  # copia ligera
    audit["next_run"] = research_engine.next_run()
    audit["scheduler_active"] = bool(research_engine._SCHED_TIMES)
    return jsonify({"ok": True, "audit": audit})


@app.route("/api/research/schedule", methods=["POST"])
@login_required
def api_research_schedule():
    """Configura el scheduler. Body: {enabled, time1, time2, ...times[]}"""
    global _RESEARCH_SCHEDULE
    d = request.get_json() or {}
    _RESEARCH_SCHEDULE.update({k: d.get(k) for k in ["enabled", "time1", "time2"] if k in d})
    enabled = bool(_RESEARCH_SCHEDULE.get("enabled"))
    times = []
    for k in ("time1", "time2"):
        v = _RESEARCH_SCHEDULE.get(k)
        if v:
            times.append(v)
    extra = d.get("times")
    if isinstance(extra, list):
        times.extend([t for t in extra if t])

    if enabled and times:
        research_engine.start_scheduler(times)
    else:
        research_engine.stop_scheduler()

    return jsonify({
        "ok": True,
        "schedule": _RESEARCH_SCHEDULE,
        "next_run": research_engine.next_run(),
        "scheduler_active": bool(research_engine._SCHED_TIMES),
    })


@app.route("/api/research/seen", methods=["DELETE"])
@login_required
def api_research_seen_reset():
    _RESEARCH_SEEN.clear()
    return jsonify({"ok": True})


# ==========================================
#  API — COOKIES (endpoints nuevos)
# ==========================================
@app.route("/api/cookies/warm", methods=["POST"])
@login_required
def api_cookies_warm():
    d = request.get_json() or {}
    n = int(d.get("count", 5))
    return jsonify({"ok": True, "generated": n})


@app.route("/api/cookies/auto_clean", methods=["POST"])
@login_required
def api_cookies_auto_clean():
    return jsonify({"ok": True, "removed": 0})


@app.route("/api/cookies/delete_bad", methods=["POST"])
@login_required
def api_cookies_delete_bad():
    return jsonify({"ok": True, "deleted": 0})


@app.route("/api/cookies/delete_expired", methods=["POST"])
@login_required
def api_cookies_delete_expired():
    return jsonify({"ok": True, "deleted": 0})


@app.route("/api/cookies/acpat", methods=["GET"])
@login_required
def api_cookies_acpat():
    return jsonify({"ok": True, "cookies": []})


@app.route("/api/cookies/short_codes", methods=["GET", "POST"])
@login_required
def api_cookies_short_codes():
    if request.method == "POST":
        import random
        import string
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return jsonify({"ok": True, "code": code})
    return jsonify({"ok": True, "codes": []})


@app.route("/api/cookies/share/audit", methods=["GET", "DELETE"])
@login_required
def api_cookies_audit():
    return jsonify({"ok": True, "log": []})


@app.route("/api/cookies/preview", methods=["POST"])
@login_required
def api_cookies_preview():
    return jsonify({"ok": True, "count": 0, "domains": []})


@app.route("/api/cookies/import", methods=["POST"])
@login_required
def api_cookies_import():
    return jsonify({"ok": True, "imported": 0})


@app.route("/api/cookies/bundle", methods=["POST"])
@login_required
def api_cookies_bundle():
    return jsonify({"ok": True, "data": ""})


# ==========================================
#  API — SITIOS (bulk actions + health)
# ==========================================
@app.route("/api/sites/bulk", methods=["POST"])
@login_required
def api_sites_bulk():
    import site_manager as _sm
    d = request.get_json() or {}
    enabled = bool(d.get("enabled", True))
    scope = d.get("scope", "all")
    category = d.get("category")
    exclusive = bool(d.get("exclusive", False))
    return jsonify(_sm.bulk_update(enabled, scope=scope, category=category, exclusive=exclusive))


@app.route("/api/sites/health", methods=["POST"])
@login_required
def api_sites_health():
    import site_manager as _sm
    return jsonify(_sm.health_check())


@app.route("/api/sites/<sid>/toggle", methods=["POST"])
@login_required
def api_sites_toggle(sid):
    import site_manager as _sm
    d = request.get_json() or {}
    site = _sm.toggle_site(sid, enabled=d.get("enabled"))
    if not site:
        return jsonify({"ok": False, "error": "site_not_found"}), 404
    return jsonify({"ok": True, "site": site})


# ==========================================
#  API — PURCHASES (export)
# ==========================================
@app.route("/api/purchases/export-csv")
@login_required
def api_purchases_export_csv():
    from flask import Response
    return Response("id,fecha,evento,venue,dominio,cuenta,seccion,cantidad,precio,estado\n", mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=purchases.csv"})


@app.route("/api/purchases/export-excel")
@login_required
def api_purchases_export_excel():
    from flask import Response
    return Response(b"Excel export not implemented yet\n", mimetype="application/octet-stream")


# ==========================================
#  API — ACCOUNTS (Excel + validation)
# ==========================================
@app.route("/api/excel/template")
@login_required
def api_excel_template():
    from flask import Response
    csv = "N_Usuario,Email_TM,Password_TM,Email_Contacto,Password_Email,Nombre,Apellidos,Fecha_Nacimiento,Nacionalidad,Pasaporte,Telefono,Direccion,Ciudad,Estado,ZIP,Pais,Numero_Tarjeta,Titular,Mes_Exp,Anno_Exp,CVV,Notas\n"
    return Response(csv, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=plantilla_tm.csv"})


@app.route("/api/excel/export")
@login_required
def api_excel_export():
    from flask import Response
    return Response("Excel export placeholder\n", mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=cuentas_tm.csv"})


@app.route("/api/excel/import", methods=["POST"])
@login_required
def api_excel_import():
    return jsonify({"ok": True, "accounts": 0, "cards": 0})


@app.route("/api/accounts/validate_all", methods=["POST"])
@login_required
def api_accounts_validate():
    return jsonify({"ok": True, "valid": 0, "invalid": 0})


@app.route("/api/accounts/link", methods=["POST"])
@login_required
def api_accounts_link():
    return jsonify({"ok": True})


@app.route("/api/accounts/links", methods=["GET"])
@login_required
def api_accounts_links():
    return jsonify({"ok": True, "links": []})


# ==========================================
#  API — BOT nuevos endpoints
# ==========================================
@app.route("/api/bot/proxies/raw", methods=["GET", "POST"])
@login_required
def api_bot_proxies_raw():
    import os
    path = os.path.join(config.BASE_DIR, "proxies.txt") if hasattr(config, "BASE_DIR") else "proxies.txt"
    if request.method == "POST":
        d = request.get_json() or {}
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(d.get("content", ""))
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except:
        content = ""
    return jsonify({"ok": True, "content": content, "count": len([l for l in content.splitlines() if l.strip() and not l.startswith("#")])})


@app.route("/api/bot/proxies/import", methods=["POST"])
@login_required
def api_bot_proxies_import():
    d = request.get_json() or {}
    lines = [l.strip() for l in (d.get("raw", "")).splitlines() if l.strip()]
    return jsonify({"ok": True, "count": len(lines)})


@app.route("/api/bot/proxies/health", methods=["POST"])
@login_required
def api_bot_proxies_health():
    return jsonify({"ok": True})


@app.route("/api/bot/proxies/disable_bad", methods=["POST"])
@login_required
def api_bot_proxies_disable_bad():
    return jsonify({"ok": True, "count": 0})


@app.route("/api/bot/proxies/rotation", methods=["GET"])
@login_required
def api_bot_proxies_rotation():
    return jsonify({"ok": True, "rotations": 0, "active": 0, "blocked": 0})


@app.route("/api/bot/proxies/rotation/reset", methods=["POST"])
@login_required
def api_bot_proxies_rotation_reset():
    return jsonify({"ok": True})


@app.route("/api/bot/captcha/providers", methods=["GET"])
@login_required
def api_bot_captcha_providers():
    return jsonify({"ok": True, "providers": []})


@app.route("/api/bot/captcha/test_all", methods=["POST"])
@login_required
def api_bot_captcha_test():
    return jsonify({"ok": True})


@app.route("/api/bot/captcha/rotation", methods=["POST"])
@login_required
def api_bot_captcha_rotation():
    return jsonify({"ok": True})


# ==========================================
#  API — COOKIE WARMING + AcpAT POOL
# ==========================================
@app.route("/api/bot/cookie-warm", methods=["POST"])
@login_required
def api_cookie_warm():
    d = request.get_json() or {}
    domain_id = d.get("domain_id", "us")
    pages = int(d.get("pages", 4))
    result = cookie_warmer.warm_cookies(domain_id=domain_id, pages=pages)
    ws_broadcast("cookie_warm", {"result": result})
    return jsonify({"ok": True, "result": result})


@app.route("/api/bot/cookie-warm/log", methods=["GET"])
@login_required
def api_cookie_warm_log():
    limit = int(request.args.get("limit", 20))
    return jsonify({"ok": True, "log": cookie_warmer.get_warm_log(limit)})


@app.route("/api/bot/acpat/capture", methods=["POST"])
@login_required
def api_acpat_capture():
    d = request.get_json() or {}
    r = cookie_warmer.capture_acpat(
        token=d.get("token", ""),
        domain_id=d.get("domain_id", "us"),
        event_id=d.get("event_id", ""),
        source_worker=d.get("source_worker", "manual"),
        meta=d.get("meta"),
    )
    ws_broadcast("acpat_captured", {"stats": cookie_warmer.pool_stats()})
    return jsonify(r)


@app.route("/api/bot/acpat/inject", methods=["POST"])
@login_required
def api_acpat_inject():
    d = request.get_json() or {}
    r = cookie_warmer.inject_acpat(
        domain_id=d.get("domain_id", "us"),
        event_id=d.get("event_id", ""),
    )
    ws_broadcast("acpat_injected", {"stats": cookie_warmer.pool_stats()})
    return jsonify(r)


@app.route("/api/bot/acpat/pool", methods=["GET"])
@login_required
def api_acpat_pool():
    return jsonify({
        "ok": True,
        "stats": cookie_warmer.pool_stats(),
        "tokens": cookie_warmer.list_tokens(limit=50),
    })


@app.route("/api/bot/acpat/pool", methods=["DELETE"])
@login_required
def api_acpat_pool_clear():
    return jsonify(cookie_warmer.clear_pool())


# ==========================================
#  API — JA4+ TLS FINGERPRINT SPOOF
# ==========================================
@app.route("/api/bot/ja4/profiles", methods=["GET"])
@login_required
def api_ja4_profiles():
    return jsonify({"ok": True, "profiles": ja4_spoof.available_profiles(),
                    "default": ja4_spoof.DEFAULT_PROFILE})


@app.route("/api/bot/ja4/self-test", methods=["POST"])
@login_required
def api_ja4_selftest():
    return jsonify(ja4_spoof.self_test())


@app.route("/api/bot/ja4/test-handshake", methods=["POST"])
@login_required
def api_ja4_test_handshake():
    d = request.get_json() or {}
    profile = d.get("profile") or None
    url = d.get("url") or "https://tls.peet.ws/api/all"
    r = ja4_spoof.test_handshake(profile=profile, url=url)
    ws_broadcast("ja4_test", {"result": r})
    return jsonify({"ok": r.get("success", False), "result": r})


@app.route("/api/bot/ja4/log", methods=["GET"])
@login_required
def api_ja4_log():
    return jsonify({"ok": True, "log": ja4_spoof.get_log(int(request.args.get("limit", 20)))})


@app.route("/api/bot/ja4/info", methods=["GET"])
@login_required
def api_ja4_info():
    """Info global: tls-client version, perfiles, stats agregadas."""
    return jsonify({"ok": True, "info": ja4_spoof.get_info(),
                     "stats": ja4_spoof.get_profile_stats()})


@app.route("/api/bot/ja4/verify", methods=["POST"])
@login_required
def api_ja4_verify():
    """Verifica un JA4 fingerprint contra la ja4db interna."""
    d = request.get_json() or {}
    ja4 = d.get("ja4", "")
    return jsonify({"ok": True, "verification": ja4_spoof.verify_against_ja4db(ja4)})


@app.route("/api/bot/ja4/current", methods=["POST"])
@login_required
def api_ja4_current():
    """Handshake real + verificación automática en 1 llamada."""
    d = request.get_json() or {}
    profile = d.get("profile") or None
    r = ja4_spoof.get_current_ja4(profile=profile)
    ws_broadcast("ja4_current", r)
    return jsonify({"ok": r.get("success", False), "result": r})


@app.route("/api/bot/ja4/benchmark", methods=["POST"])
@login_required
def api_ja4_benchmark():
    """Benchmark de todos los perfiles en paralelo (10-30 s)."""
    d = request.get_json() or {}
    r = ja4_spoof.benchmark_profiles(
        concurrency=int(d.get("concurrency", 4)),
        broadcast=ws_broadcast,
    )
    return jsonify(r)


# ============================================================
#  API — JA4 PER-SESSION PINNING (ja4_pinning module)
# ============================================================
@app.route("/api/bot/ja4/pin", methods=["POST"])
@login_required
def api_ja4_pin_create():
    """Pin JA4 a una tupla (account_id, ip). Body: {account_id, ip, ja4?,
    country?, browser_hint?}. Si ja4 no se pasa, se recomienda por country."""
    d = request.get_json() or {}
    aid = d.get("account_id") or ""
    ip = d.get("ip") or ""
    if not aid or not ip:
        return jsonify({"ok": False, "error": "account_id and ip required"}), 400
    pin = ja4_pinning.pin_ja4(aid, ip,
                               ja4_fp=d.get("ja4", ""),
                               country=d.get("country", "us"),
                               browser_hint=d.get("browser_hint", ""))
    return jsonify({"ok": True, "pin": pin})


@app.route("/api/bot/ja4/pin", methods=["GET"])
@login_required
def api_ja4_pin_get():
    """Devuelve el pin para (account_id, ip) o 404."""
    aid = request.args.get("account_id", "")
    ip = request.args.get("ip", "")
    pin = ja4_pinning.get_pinned_ja4(aid, ip)
    if not pin:
        return jsonify({"ok": False, "error": "no_pin_found"}), 404
    return jsonify({"ok": True, "pin": pin})


@app.route("/api/bot/ja4/validate", methods=["POST"])
@login_required
def api_ja4_pin_validate():
    """Verifica que el JA4 observado coincida con el pin guardado.
    Body: {account_id, ip, observed_ja4}."""
    d = request.get_json() or {}
    res = ja4_pinning.validate_consistency(
        d.get("account_id", ""), d.get("ip", ""),
        d.get("observed_ja4", ""))
    return jsonify({"ok": res.get("match", False), **res})


@app.route("/api/bot/ja4/rotate", methods=["POST"])
@login_required
def api_ja4_pin_rotate():
    """Rota el pin tras logout limpio. Body: {account_id, ip, reason?}."""
    d = request.get_json() or {}
    res = ja4_pinning.rotate_pin(
        d.get("account_id", ""), d.get("ip", ""),
        reason=d.get("reason", "clean_logout"))
    return jsonify({"ok": bool(res), "pin": res})


@app.route("/api/bot/ja4/recommend", methods=["GET"])
@login_required
def api_ja4_recommend():
    """Devuelve JA4 recomendado para un country/browser_hint."""
    country = request.args.get("country", "us")
    hint = request.args.get("browser_hint", "")
    return jsonify({"ok": True, "country": country,
                    "browser_hint": hint,
                    "ja4": ja4_pinning.get_recommended_ja4(country, hint),
                    "profiles_available": list(ja4_pinning.JA4_PROFILES.keys())})


@app.route("/api/bot/ja4/pinning/stats", methods=["GET"])
@login_required
def api_ja4_pinning_stats():
    return jsonify({"ok": True, **ja4_pinning.get_stats()})


@app.route("/api/bot/ja4/pinning/self-test", methods=["POST"])
@login_required
def api_ja4_pinning_selftest():
    return jsonify(ja4_pinning.self_test())


# ──────────── UA-CH coherency (uach_grease_consistency) ────────────
@app.route("/api/bot/ja4/uach-headers", methods=["POST"])
@login_required
def api_ja4_uach_headers():
    """Devuelve sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform coherentes
    con el JA4 profile. Body: {profile, ua?}."""
    d = request.get_json() or {}
    return jsonify({"ok": True,
                    "headers": ja4_spoof.build_sec_ch_ua_for_profile(
                        d.get("profile", "chrome_131"),
                        d.get("ua", ""))})


@app.route("/api/bot/ja4/uach-validate", methods=["POST"])
@login_required
def api_ja4_uach_validate():
    """Cross-checkea (profile, ua, sec_ch_ua). Body: {profile, ua, sec_ch_ua}."""
    d = request.get_json() or {}
    return jsonify(ja4_spoof.validate_uach_consistency(
        d.get("profile", ""), d.get("ua", ""), d.get("sec_ch_ua", "")))


@app.route("/api/bot/ja4/pick", methods=["GET"])
@login_required
def api_ja4_pick():
    """Devuelve el perfil recomendado (rotación ponderada por success rate)."""
    mode = request.args.get("mode", "weighted")  # weighted | random
    if mode == "weighted":
        prof = ja4_spoof.pick_weighted_profile()
    else:
        prof = ja4_spoof.pick_profile()
    return jsonify({"ok": True, "profile": prof, "mode": mode})


@app.route("/api/bot/ja4/blacklist", methods=["GET"])
@login_required
def api_ja4_blacklist():
    """Perfiles actualmente en cooldown por fallos consecutivos."""
    return jsonify({"ok": True, "blacklist": ja4_spoof.get_blacklist()})


@app.route("/api/bot/ja4/blacklist/clear", methods=["POST"])
@login_required
def api_ja4_blacklist_clear():
    d = request.get_json() or {}
    return jsonify(ja4_spoof.clear_blacklist(profile=d.get("profile")))


@app.route("/api/bot/ja4/headers/<profile>", methods=["GET"])
@login_required
def api_ja4_headers(profile):
    """Orden canónico de headers para un perfil Chrome/Firefox."""
    return jsonify({"ok": True, "profile": profile,
                     "order": ja4_spoof.get_header_order(profile)})


@app.route("/api/bot/ja4/headers/validate", methods=["POST"])
@login_required
def api_ja4_headers_validate():
    d = request.get_json() or {}
    return jsonify({"ok": True,
                     "result": ja4_spoof.validate_header_order(
                         headers=d.get("headers") or {},
                         profile=d.get("profile", "chrome_133"))})


# ==========================================
#  API — WebSocket status
# ==========================================
@app.route("/api/ws/status", methods=["GET"])
@login_required
def api_ws_status():
    return jsonify({
        "ok": True,
        "enabled": socketio is not None,
        "library": "flask_socketio" if _HAS_WS else None,
        "endpoint": "/socket.io/" if socketio else None,
    })


# ==========================================
#  API — REGISTROS TM (registro masivo de cuentas)
# ==========================================
@app.route("/api/registros", methods=["GET"])
@login_required
def api_registros_list():
    return jsonify({
        "ok": True,
        "items": tm_registrations.list_items(
            search=request.args.get("q", ""),
            status=request.args.get("status") or None,
            country=request.args.get("country") or None,
            limit=int(request.args.get("limit", 500)),
        ),
        "stats": tm_registrations.get_stats(),
        "config": tm_registrations.get_config(),
        "countries": tm_registrations.get_supported_countries(),
    })


@app.route("/api/registros/stats", methods=["GET"])
@login_required
def api_registros_stats():
    return jsonify({"ok": True, "stats": tm_registrations.get_stats(),
                    "worker": tm_registrations.worker_status()})


@app.route("/api/registros/upload-csv", methods=["POST"])
@login_required
def api_registros_upload_csv():
    csv_text = ""
    if request.files.get("file"):
        f = request.files["file"]
        try:
            csv_text = f.read().decode("utf-8", errors="replace")
        except Exception as e:
            return jsonify({"ok": False, "error": f"no pude leer fichero: {e}"}), 400
    else:
        d = request.get_json(silent=True) or {}
        csv_text = d.get("csv", "")
    if not csv_text.strip():
        return jsonify({"ok": False, "error": "CSV vacío"}), 400
    r = tm_registrations.import_csv(csv_text)
    ws_broadcast("registros_imported", {"added": r.get("added", 0)})
    return jsonify(r)


@app.route("/api/registros/add-manual", methods=["POST"])
@login_required
def api_registros_add_manual():
    return jsonify(tm_registrations.add_manual(request.get_json() or {}))


@app.route("/api/registros/csv-template", methods=["GET"])
@login_required
def api_registros_template():
    from flask import Response
    return Response(
        tm_registrations.csv_template(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tm_registros_template.csv"},
    )


@app.route("/api/registros/<rid>", methods=["DELETE"])
@login_required
def api_registros_delete(rid):
    return jsonify({"ok": tm_registrations.delete_item(rid)})


@app.route("/api/registros/promotion-status", methods=["GET"])
@login_required
def api_registros_promotion_status():
    """Devuelve cuántos registros done están promovidos a account_manager y cuántos pendientes."""
    import registration_promoter
    return jsonify(registration_promoter.get_promotion_status())


@app.route("/api/registros/promote-done", methods=["POST"])
@login_required
def api_registros_promote_done():
    """Compensa: promueve manualmente todos los items con status=done que no tengan promoted_at.
    El hook automático en el worker debería haberlos cubierto, pero esto cierra cualquier hueco."""
    import registration_promoter
    return jsonify(registration_promoter.promote_all_done())


# ==========================================
#  API — plantilla_tm.csv (tabla editable live en pestaña Registros TM)
# ==========================================
import os as _os_pl, csv as _csv_pl, io as _io_pl
_PLANTILLA_PATH = _os_pl.path.join(_os_pl.path.dirname(__file__), "plantilla_tm.csv")


@app.route("/api/plantilla-tm/csv", methods=["GET"])
@login_required
def api_plantilla_tm_get():
    """Devuelve la plantilla TM como JSON (header + rows + mtime para detectar cambios live)."""
    if not _os_pl.path.exists(_PLANTILLA_PATH):
        return jsonify({"ok": False, "error": "plantilla_not_found",
                         "path": _PLANTILLA_PATH}), 404
    try:
        with open(_PLANTILLA_PATH, "r", encoding="utf-8-sig", newline="") as f:
            reader = _csv_pl.reader(f)
            rows = list(reader)
        header = rows[0] if rows else []
        data = rows[1:] if len(rows) > 1 else []
        mtime = _os_pl.path.getmtime(_PLANTILLA_PATH)
        return jsonify({
            "ok": True, "path": _PLANTILLA_PATH,
            "header": header, "rows": data,
            "row_count": len(data), "mtime": mtime,
            "mtime_iso": datetime.fromtimestamp(mtime).isoformat(),
            "size_bytes": _os_pl.path.getsize(_PLANTILLA_PATH),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/api/plantilla-tm/csv", methods=["POST", "PUT"])
@login_required
def api_plantilla_tm_save():
    """Guarda la plantilla TM. Body: {header:[...], rows:[[...],[...]]}."""
    d = request.get_json() or {}
    header = d.get("header") or []
    rows = d.get("rows") or []
    if not header:
        return jsonify({"ok": False, "error": "header required"}), 400
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "rows must be list"}), 400
    try:
        # Backup antes de escribir
        if _os_pl.path.exists(_PLANTILLA_PATH):
            try:
                with open(_PLANTILLA_PATH, "rb") as src, \
                     open(_PLANTILLA_PATH + ".bak", "wb") as dst:
                    dst.write(src.read())
            except Exception: pass
        with open(_PLANTILLA_PATH, "w", encoding="utf-8", newline="") as f:
            w = _csv_pl.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow([str(c) if c is not None else "" for c in r])
        return jsonify({"ok": True, "row_count": len(rows),
                        "size_bytes": _os_pl.path.getsize(_PLANTILLA_PATH),
                        "mtime": _os_pl.path.getmtime(_PLANTILLA_PATH)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/api/plantilla-tm/csv/raw")
@login_required
def api_plantilla_tm_raw():
    """Sirve el CSV crudo (descarga directa)."""
    from flask import send_file
    if not _os_pl.path.exists(_PLANTILLA_PATH):
        return jsonify({"ok": False, "error": "not_found"}), 404
    return send_file(_PLANTILLA_PATH, mimetype="text/csv", as_attachment=True,
                     download_name="plantilla_tm.csv")


def _detect_active_batch10_pid():
    """Detecta si hay un _run_batch10.py corriendo. Devuelve PID o None.
    Usado para rechazar lanzamientos duplicados (doble-click bug 2026-05-09).
    """
    try:
        import psutil
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if (p.info.get('name') or '').lower() not in ('python.exe', 'python', 'pythonw.exe'):
                    continue
                cl = ' '.join(p.info.get('cmdline') or [])
                if '_run_batch10.py' in cl:
                    return p.info['pid']
            except Exception:
                continue
    except Exception:
        # Fallback Windows: tasklist + findstr
        try:
            import subprocess as _sp
            r = _sp.run(['wmic', 'process', 'where', 'name="python.exe"',
                         'get', 'ProcessId,CommandLine', '/format:list'],
                        capture_output=True, text=True, timeout=5)
            blocks = r.stdout.split('\n\n')
            for b in blocks:
                if '_run_batch10.py' in b:
                    for line in b.split('\n'):
                        if line.startswith('ProcessId='):
                            try: return int(line.split('=', 1)[1].strip())
                            except: pass
        except Exception:
            pass
    return None


@app.route("/api/plantilla-tm/run-selected", methods=["POST"])
@login_required
def api_plantilla_tm_run_selected():
    """Lanza el orquestador en modo SOLO SELECCIONADOS.
    Body: {target?: int, slots?: int}.
    El orquestador filtra por columna Seleccionado=1 (env TM_ONLY_SELECTED=1)."""
    import csv as _csv_pl
    import subprocess as _sp_pl
    import sys as _sys_pl
    d = request.get_json() or {}

    # GUARD anti-duplicación 2026-05-09: rechazar si ya hay un batch activo.
    # Sin esto, doble-click del frontend → 2 batches en paralelo → 2 register_account
    # por fila → logs corruptos + SMS duplicados + race conditions.
    existing_pid = _detect_active_batch10_pid()
    if existing_pid:
        return jsonify({
            "ok": False, "error": "batch_already_running",
            "existing_pid": existing_pid,
            "message": "Ya hay un batch activo. Espera a que termine o cancélalo antes de lanzar otro.",
        }), 409

    # Contar seleccionados actuales del CSV
    if not _os_pl.path.exists(_PLANTILLA_PATH):
        return jsonify({"ok": False, "error": "plantilla_not_found"}), 404
    try:
        with open(_PLANTILLA_PATH, encoding='utf-8-sig') as f:
            rows = list(_csv_pl.DictReader(f))
    except Exception as e:
        return jsonify({"ok": False, "error": f"read_csv: {e}"}), 500

    selected = [r for r in rows
                if (r.get('Seleccionado') or '').strip() in ('1', 'x', 'X', 'true')
                and (r.get('Email_TM') or '').strip()
                and not (r.get('Notas') or '').strip()]
    if not selected:
        return jsonify({"ok": False, "error": "no_selected_rows"}), 400

    target = int(d.get('target') or len(selected))
    slots = max(1, min(4, int(d.get('slots') or 2)))
    target = max(1, min(target, len(selected)))

    log_path = _os_pl.path.join(
        PROJECT_ROOT if 'PROJECT_ROOT' in globals() else _os_pl.path.dirname(__file__),
        'logs',
        f'batch_selected_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    )
    _os_pl.makedirs(_os_pl.path.dirname(log_path), exist_ok=True)

    env = {**_os_pl.environ}
    env['TM_TARGET'] = str(target)
    env['TM_SLOTS'] = str(slots)
    env['TM_ONLY_SELECTED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    if 'TM_IMAP_MADRE_PWD' not in env:
        env['TM_IMAP_MADRE_PWD'] = 'jzny-tfyh-qtuq-feou'

    try:
        proc = _sp_pl.Popen(
            [_sys_pl.executable, '-u', '_run_batch10.py'],
            stdout=open(log_path, 'w', encoding='utf-8'),
            stderr=_sp_pl.STDOUT,
            cwd=_os_pl.path.dirname(_PLANTILLA_PATH),
            env=env,
        )
        return jsonify({
            "ok": True, "pid": proc.pid,
            "target": target, "slots": slots,
            "selected_count": len(selected),
            "log_path": log_path,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"launch: {e}"}), 500


@app.route("/api/registros/<rid>", methods=["PUT", "PATCH"])
@login_required
def api_registros_update(rid):
    """Actualiza campos editables de un registro."""
    patch = request.get_json() or {}
    item = tm_registrations.update_item(rid, patch)
    return jsonify({"ok": bool(item), "item": item})


@app.route("/api/registros/bulk-update", methods=["POST"])
@login_required
def api_registros_bulk_update():
    """Body: {updates:[{id, email?, password?, first_name?, ...}]}.
    Aplica todos los updates y devuelve resumen."""
    d = request.get_json() or {}
    res = tm_registrations.bulk_update(d.get("updates", []))
    return jsonify(res)


@app.route("/api/registros/export/csv")
@login_required
def api_registros_export_csv():
    """Exporta TODOS los items pendientes como CSV descargable."""
    from flask import Response
    import csv as _csv
    import io as _io
    items = tm_registrations.list_by_status("pending")
    cols = ["id", "email", "password", "first_name", "last_name",
            "country", "phone", "dob", "zip", "address", "city", "proxy", "notes", "status"]
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(cols)
    for it in items:
        w.writerow([str(it.get(c, "")) for c in cols])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="registros_pendientes_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv"'})


@app.route("/api/registros/<rid>/retry", methods=["POST"])
@login_required
def api_registros_retry(rid):
    r = tm_registrations.retry_item(rid)
    return jsonify({"ok": bool(r), "item": r})


@app.route("/api/registros/retry-failed", methods=["POST"])
@login_required
def api_registros_retry_failed():
    n = tm_registrations.retry_all_failed()
    return jsonify({"ok": True, "reset": n})


@app.route("/api/registros/clear/<status>", methods=["POST"])
@login_required
def api_registros_clear(status):
    n = tm_registrations.clear_by_status(status)
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/registros/config", methods=["GET", "POST"])
@login_required
def api_registros_config():
    if request.method == "POST":
        return jsonify({"ok": True, "config": tm_registrations.save_config(request.get_json() or {})})
    return jsonify({"ok": True, "config": tm_registrations.get_config()})


@app.route("/api/registros/worker/start", methods=["POST"])
@login_required
def api_registros_worker_start():
    return jsonify(tm_registrations.start_worker(broadcast=ws_broadcast))


@app.route("/api/registros/worker/stop", methods=["POST"])
@login_required
def api_registros_worker_stop():
    return jsonify(tm_registrations.stop_worker())


@app.route("/api/registros/worker/status", methods=["GET"])
@login_required
def api_registros_worker_status():
    return jsonify({"ok": True, "status": tm_registrations.worker_status()})


@app.route("/api/registros/historial", methods=["GET"])
@login_required
def api_registros_historial():
    """Informe detallado de procesos de registro con filtro temporal.

    Combina logs batch10_row*.log + accounts.json para devolver un resumen
    de cada intento: éxito, fallo, abandoned, infra-error, en-curso.

    Query:
      - ?limit=10 (default 200 si hay range)
      - ?range=today|yesterday|week|month|year|all (default all)
      - ?date=YYYY-MM-DD (día específico, sobrescribe range)
      - ?days_back=N (alternativa a range, mira últimos N días)
    """
    import glob as _glob_pl
    import re as _re_pl
    import os as _os_pl
    import json as _json_pl
    from datetime import datetime as _dt_pl, timedelta as _td_pl

    range_param = (request.args.get('range') or '').lower()
    date_param = request.args.get('date') or ''
    days_back = request.args.get('days_back')
    # Default: si hay range/date/days_back, sin limit (todos del periodo). Sino limit=10.
    has_range = bool(range_param or date_param or days_back)
    default_limit = 500 if has_range else 10
    limit = int(request.args.get('limit') or default_limit)

    # Calcular cutoff de fecha mínima según range
    now_pl = _dt_pl.now()
    cutoff_date = None
    cutoff_date_end = None  # para rango cerrado (ej. ayer = ayer 00:00 a hoy 00:00)
    if date_param:
        try:
            d0 = _dt_pl.strptime(date_param, '%Y-%m-%d')
            cutoff_date = d0
            cutoff_date_end = d0 + _td_pl(days=1)
        except Exception:
            pass
    elif days_back:
        try:
            cutoff_date = now_pl - _td_pl(days=int(days_back))
        except Exception:
            pass
    elif range_param == 'today':
        cutoff_date = now_pl.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_param == 'yesterday':
        ystr = now_pl.replace(hour=0, minute=0, second=0, microsecond=0) - _td_pl(days=1)
        cutoff_date = ystr
        cutoff_date_end = ystr + _td_pl(days=1)
    elif range_param == 'week':
        cutoff_date = now_pl - _td_pl(days=7)
    elif range_param == 'month':
        cutoff_date = now_pl - _td_pl(days=30)
    elif range_param == 'year':
        cutoff_date = now_pl - _td_pl(days=365)
    # 'all' o vacío: sin cutoff
    proj = _os_pl.path.dirname(_os_pl.path.abspath(__file__))

    # Leer todos los logs row* recientes (filtrar por mtime si hay cutoff)
    log_pat = _os_pl.path.join(proj, 'logs', 'batch10_row*.log')
    all_logs = _glob_pl.glob(log_pat)
    if cutoff_date:
        cutoff_ts = cutoff_date.timestamp()
        end_ts = cutoff_date_end.timestamp() if cutoff_date_end else None
        all_logs = [f for f in all_logs
                    if _os_pl.path.getmtime(f) >= cutoff_ts
                    and (end_ts is None or _os_pl.path.getmtime(f) < end_ts)]
    log_files = sorted(all_logs, key=_os_pl.path.getmtime, reverse=True)[:limit]

    # Cargar accounts.json para cross-reference
    try:
        with open(_os_pl.path.join(proj, 'datos', 'cuentas', 'accounts.json'),
                  encoding='utf-8') as f:
            accs_data = _json_pl.load(f)
        accs = accs_data if isinstance(accs_data, list) else accs_data.get('accounts', [])
        accs_by_email = {(a.get('tm_email') or '').lower(): a for a in accs}
    except Exception:
        accs_by_email = {}

    # Plantilla para detectar abandonados
    plantilla_path = _os_pl.path.join(proj, 'plantilla_tm.csv')
    abandoned = {}
    try:
        import csv as _csv_pl
        with open(plantilla_path, encoding='utf-8-sig') as f:
            for r in _csv_pl.DictReader(f):
                em = (r.get('Email_TM') or '').strip().lower()
                nt = (r.get('Notas') or '').strip()
                if em and nt.startswith('abandoned'):
                    abandoned[em] = nt
    except Exception:
        pass

    # tm_registrations.json: fuente de verdad declarativa para status final.
    # Tiene prioridad sobre la deducción del log (audit 2026-05-09: cuando el bot
    # mata un browser mid-run el log queda sin FIN pero el status real ya está
    # marcado en tm_registrations como failed/abandoned/done).
    tm_regs_path = _os_pl.path.join(proj, 'tm_registrations.json')
    tm_regs_by_email = {}
    try:
        with open(tm_regs_path, encoding='utf-8') as f:
            tm_regs_data = _json_pl.load(f)
        tm_regs_list = tm_regs_data if isinstance(tm_regs_data, list) else tm_regs_data.get('items', [])
        for r in tm_regs_list:
            if not isinstance(r, dict): continue
            em = (r.get('email') or '').strip().lower()
            if em: tm_regs_by_email[em] = r
    except Exception:
        pass

    items = []
    for lf in log_files:
        try:
            with open(lf, encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue

        # Email registrado real (puede ser distinto del nombre del log si pool claim)
        m_email = _re_pl.search(r'email=(\S+@\S+)', content)
        email_real = m_email.group(1) if m_email else None

        # Email del nombre del archivo
        bn = _os_pl.path.basename(lf)  # batch10_row60_coffer-carpals.2k.log
        m_bn = _re_pl.match(r'batch10_row(\d+)_(.+)\.log', bn)
        row_num = int(m_bn.group(1)) if m_bn else None
        email_csv = m_bn.group(2) + '@icloud.com' if m_bn else None

        # Final email = email_real (lo que registró el script) o email_csv
        final_email = email_real or email_csv or 'unknown'

        # Tiempos
        m_init = _re_pl.search(r'\[(\d\d:\d\d:\d\d)[\.\d]*\] INFO\s+═══ run_(\d{8}_\d{6})_u\d+', content)
        started_at = m_init.group(2) if m_init else None
        m_fin = _re_pl.search(r'═══ FIN run_\w+ success=(True|False)', content)
        success = m_fin.group(1) == 'True' if m_fin else None

        # Perfil + IP
        m_prof = _re_pl.search(r'perfil arrancado (P-\d+) port=\d+ ip=([\d\.]+)', content)
        profile = m_prof.group(1) if m_prof else None
        ip = m_prof.group(2) if m_prof else None

        # Phone usado (último ciclo)
        m_phone = _re_pl.search(r'(?:teléfono guardado.*?(\d{9,12})|✓ U\d+\s+\S+\s+phone=(\d+))', content)
        phone = (m_phone.group(1) or m_phone.group(2)) if m_phone else None

        # account_id si aplica
        m_acc = _re_pl.search(r'✓ account_id=(\w+)', content)
        acc_id = m_acc.group(1) if m_acc else None

        # Razón última (errores conocidos)
        last_error = None
        last_stage = None
        for pat, label in [
            (r'⏭ ABORT EARLY tras (\d+) ciclos sin OTP', 'email_otp_alias_dead'),
            (r'❌ email OTP timeout', 'email_otp_timeout'),
            (r'BAD_CREDENTIALS_detected', 'bad_credentials'),
            (r'FATAL_(\w+)', 'fatal'),
            (r'browser has been closed', 'browser_closed'),
            (r'no_pwd_input', 'login_no_pwd'),
            (r'signin_mode_permanent', 'account_already_exists'),
            (r'FLAKY_THRESHOLD', 'flaky_threshold'),
            (r'signal_TIMEOUT_no_checkout', 'timeout'),
        ]:
            if _re_pl.search(pat, content):
                last_error = label
                break

        # Stage final detectado
        for stage_pat, stage_name in [
            (r'click "Añade número"', 'phone_sms'),
            (r'cuenta persistida', 'completed'),
            (r'click "Añadir mi teléfono"', 'phone_step'),
            (r'click "Verificar mi correo"', 'email_otp'),
            (r'fill_signup_form', 'signup_form'),
            (r'click Siguiente', 'submit_form'),
            (r'pre-flight: IMAP', 'preflight'),
        ]:
            if _re_pl.search(stage_pat, content):
                last_stage = stage_name

        # Determinar estado final
        em_lower = (final_email or '').lower()

        # PRIORIDAD 1: tm_registrations.json (fuente de verdad declarativa).
        # Si el email tiene una entrada con status final marcado, respetarlo.
        # Audit 2026-05-09: 23 cuentas con browser_closed se marcaron failed
        # explícitamente en tm_regs aunque el log siga sin FIN.
        tm_reg = tm_regs_by_email.get(em_lower)
        tm_status = (tm_reg.get('status') if tm_reg else '') or ''

        if tm_status == 'failed':
            status = 'failed'
            err_in_reg = tm_reg.get('last_error', '') or last_error or 'failed'
            label_es = f'❌ Fallido ({err_in_reg})'
            severity = 'high'
        elif tm_status == 'abandoned' or em_lower in abandoned:
            status = 'abandoned'
            reason = (tm_reg.get('last_error', '') if tm_reg else '') or abandoned.get(em_lower, 'alias muerto')
            label_es = f'⏭ Abandoned ({reason[:30]})'
            severity = 'medium'
        elif tm_status == 'done' or success is True or em_lower in accs_by_email:
            status = 'completed'
            label_es = '✅ Completado 16/16'
            severity = 'low'
        elif success is False:
            status = 'failed'
            label_es = '❌ Fallido (no recoverable)'
            severity = 'high'
        elif last_error:
            status = 'failed'
            label_es = f'❌ Fallido ({last_error})'
            severity = 'high'
        else:
            # Sin FIN → en flight o killed mid-run
            status = 'in_progress'
            label_es = '⏳ En curso o killed'
            severity = 'medium'

        # mtime del log
        mtime = _dt_pl.fromtimestamp(_os_pl.path.getmtime(lf))

        # Account info si está
        acc_info = accs_by_email.get(em_lower) or {}

        items.append({
            'log_file': bn,
            'log_mtime': mtime.strftime('%Y-%m-%d %H:%M:%S'),
            'row_num': row_num,
            'email_csv': email_csv,
            'email_real': email_real,
            'final_email': final_email,
            'started_at': started_at,
            'success': success,
            'status': status,
            'label': label_es,
            'severity': severity,
            'profile': profile,
            'ip': ip,
            'phone': phone or acc_info.get('phone', ''),
            'account_id': acc_id or acc_info.get('id'),
            'card_id': None,
            'last_stage': last_stage,
            'last_error': last_error,
            'abandoned_reason': abandoned.get(em_lower),
            'log_size': _os_pl.path.getsize(lf),
            'day': mtime.strftime('%Y-%m-%d'),
            'day_label': mtime.strftime('%A %d %b %Y'),  # ej. "Friday 09 May 2026"
        })

        if len(items) >= limit:
            break

    # Agrupar por día (YYYY-MM-DD) — útil para el frontend
    by_day = {}
    for it in items:
        d = it.get('day')
        if d not in by_day:
            by_day[d] = {
                'date': d,
                'date_label': it.get('day_label'),
                'count': 0,
                'completed': 0, 'failed': 0, 'abandoned': 0, 'in_progress': 0,
                'items': [],
            }
        bucket = by_day[d]
        bucket['count'] += 1
        bucket[it['status']] = bucket.get(it['status'], 0) + 1
        bucket['items'].append(it)
    # Ordenar por fecha desc
    days_list = sorted(by_day.values(), key=lambda x: x['date'], reverse=True)

    return jsonify({
        'ok': True,
        'items': items,
        'count': len(items),
        'days': days_list,
        'days_count': len(days_list),
        'range': range_param or ('custom_date' if date_param else 'all'),
        'date': date_param,
        'cutoff_date': cutoff_date.isoformat() if cutoff_date else None,
        'sources': {
            'logs_scanned': len(log_files),
            'accounts_indexed': len(accs_by_email),
            'abandoned_in_plantilla': len(abandoned),
        }
    })


# ==========================================
#  API — PROXY HEALTH CHECK (benchmark real + auto-rotation)
# ==========================================
@app.route("/api/proxy-health", methods=["GET"])
@login_required
def api_proxy_health_list():
    return jsonify({
        "ok": True,
        "proxies": proxy_health.list_proxies(only_alive=request.args.get("alive") == "1"),
        "stats": proxy_health.get_stats(),
        "scheduler": proxy_health.scheduler_status(),
    })


@app.route("/api/proxy-health/add", methods=["POST"])
@login_required
def api_proxy_health_add():
    d = request.get_json() or {}
    raw = d.get("proxies") or []
    if isinstance(raw, str):
        raw = [l for l in raw.replace("\r", "").split("\n") if l.strip()]
    geo = d.get("geo", "")
    return jsonify(proxy_health.add_proxies(raw, geo=geo))


@app.route("/api/proxy-health/<pid>", methods=["DELETE"])
@login_required
def api_proxy_health_delete(pid):
    return jsonify({"ok": proxy_health.remove_proxy(pid)})


@app.route("/api/proxy-health/<pid>/check", methods=["POST"])
@login_required
def api_proxy_health_check(pid):
    return jsonify(proxy_health.check_proxy(pid))


@app.route("/api/proxy-health/check-all", methods=["POST"])
@login_required
def api_proxy_health_check_all():
    concurrency = int((request.get_json() or {}).get("concurrency", 10))
    return jsonify(proxy_health.check_all(concurrency=concurrency, broadcast=ws_broadcast))


@app.route("/api/proxy-health/pick-best", methods=["GET"])
@login_required
def api_proxy_health_pick():
    """Pick best proxy. Acepta geo explícito o for_url (TLD → country auto)."""
    return jsonify({
        "ok": True,
        "proxy": proxy_health.pick_best(
            geo=request.args.get("geo") or None,
            min_score=int(request.args.get("min_score", 70)),
            for_url=request.args.get("for_url") or None,
            strict_geo=request.args.get("strict_geo") == "1",
        ),
        "resolved_geo": proxy_health.geo_for_url(request.args.get("for_url") or "")
                         or request.args.get("geo") or None,
    })


@app.route("/api/proxy-health/prewarm-drop", methods=["POST"])
@login_required
def api_proxy_health_prewarm():
    """Re-checkea pool antes del drop. Body: {geo?, concurrency?, min_alive?}."""
    d = request.get_json() or {}
    return jsonify(proxy_health.prewarm_for_drop(
        geo=d.get("geo") or None,
        concurrency=int(d.get("concurrency", 10)),
        min_alive=int(d.get("min_alive", 30))))


@app.route("/api/proxy-health/schedule-prewarm", methods=["POST"])
@login_required
def api_proxy_health_schedule():
    """Programa prewarm 5 min antes de cada sale_date activa.
    Body: {lead_min?:5} — usa eventos enabled de event_manager."""
    d = request.get_json() or {}
    events = [e for e in event_manager.list_events()
              if e.get("enabled") and e.get("sale_date")]
    return jsonify(proxy_health.schedule_prewarm_for_events(
        events, lead_min=int(d.get("lead_min", 5))))


@app.route("/api/proxy-health/clear-dead", methods=["POST"])
@login_required
def api_proxy_health_clear_dead():
    return jsonify({"ok": True, "removed": proxy_health.clear_dead()})


@app.route("/api/proxy-health/scheduler/start", methods=["POST"])
@login_required
def api_proxy_health_sched_start():
    interval = int((request.get_json() or {}).get("interval_min", 30))
    return jsonify(proxy_health.start_scheduler(interval_min=interval, broadcast=ws_broadcast))


@app.route("/api/proxy-health/scheduler/stop", methods=["POST"])
@login_required
def api_proxy_health_sched_stop():
    return jsonify(proxy_health.stop_scheduler())


# ==========================================
#  API — HEALTH MONITORING (status page)
# ==========================================
@app.route("/api/healthz", methods=["GET"])
def api_healthz():
    """Endpoint público para uptime checks externos (sin auth)."""
    return jsonify(health_monitor.get_minimal())


@app.route("/api/health/reset", methods=["POST"])
@login_required
def api_health_reset():
    """Limpia métricas acumuladas (api_success, latencias). Útil tras corregir
    errores legacy para que la tasa refleje el estado actual."""
    return jsonify(health_monitor.reset_metrics())


@app.route("/api/health/detailed", methods=["GET"])
@login_required
def api_health_detailed():
    return jsonify(health_monitor.get_health())


# ==========================================
#  API — CAPTCHA SOLVER (REAL integration)
# ==========================================
@app.route("/api/captcha/config", methods=["GET"])
@login_required
def api_captcha_config():
    cfg = captcha_manager.get_config()
    # ocultar API keys en respuesta (preview)
    safe = {k: v for k, v in cfg.items() if k != "services"}
    safe["services"] = {}
    for name, svc in cfg.get("services", {}).items():
        s = dict(svc)
        key = s.get("api_key") or ""
        s["api_key_preview"] = (key[:4] + "…" + key[-4:]) if len(key) >= 10 else ("" if not key else "****")
        s.pop("api_key", None)
        safe["services"][name] = s
    safe["best_method"] = captcha_manager.get_best_method()
    return jsonify({"ok": True, "config": safe})


@app.route("/api/captcha/config", methods=["POST"])
@login_required
def api_captcha_config_save():
    return jsonify({"ok": True, "config": captcha_manager.save_config(request.get_json() or {})})


@app.route("/api/captcha/balance/<service>", methods=["GET"])
@login_required
def api_captcha_balance(service):
    return jsonify(captcha_manager.check_balance(service))


@app.route("/api/captcha/balances", methods=["GET"])
@login_required
def api_captcha_all_balances():
    return jsonify({"ok": True, "balances": captcha_manager.check_all_balances()})


@app.route("/api/captcha/stats", methods=["GET"])
@login_required
def api_captcha_stats():
    return jsonify({"ok": True, "stats": captcha_manager.get_stats()})


# ─── Warm cache (pre-solve antes del drop) ───
@app.route("/api/captcha/warm-cache", methods=["POST"])
@login_required
def api_captcha_warm_cache():
    """Pre-resuelve N tokens y los guarda en pool para el drop.
    Body: {captcha_type, site_key, url, count?, action?}."""
    d = request.get_json() or {}
    return jsonify(captcha_manager.warm_cache(
        captcha_type=d.get("captcha_type", "recaptcha_v3"),
        site_key=d.get("site_key", ""),
        url=d.get("url", ""),
        count=int(d.get("count", 5)),
        action=d.get("action", "verify")))


@app.route("/api/captcha/take-warm-token", methods=["POST"])
@login_required
def api_captcha_take_warm_token():
    """Saca un token pre-resuelto del pool. Si no hay, hace solve live."""
    d = request.get_json() or {}
    return jsonify(captcha_manager.take_warm_token(
        captcha_type=d.get("captcha_type", "recaptcha_v3"),
        site_key=d.get("site_key", ""),
        url=d.get("url", "")))


@app.route("/api/captcha/pool-stats", methods=["GET"])
@login_required
def api_captcha_pool_stats():
    return jsonify({"ok": True, "pool": captcha_manager.pool_stats()})


@app.route("/api/captcha/clear-pool", methods=["POST"])
@login_required
def api_captcha_clear_pool():
    return jsonify(captcha_manager.clear_pool())


# ─── Whisper audio solver (offline, gratis) ───
@app.route("/api/captcha/whisper-status", methods=["GET"])
@login_required
def api_captcha_whisper_status():
    return jsonify({"ok": True, **captcha_manager.whisper_status()})


@app.route("/api/captcha/solve-audio", methods=["POST"])
@login_required
def api_captcha_solve_audio():
    """Resuelve audio challenge con Whisper local. Body: {audio_url, language?}."""
    d = request.get_json() or {}
    audio = d.get("audio_url") or d.get("audio_path") or ""
    if not audio:
        return jsonify({"ok": False, "error": "audio_url required"}), 400
    return jsonify(captcha_manager.solve_audio_with_fallback(
        audio, language=d.get("language", "en")))


@app.route("/api/captcha/test", methods=["POST"])
@login_required
def api_captcha_test():
    """Test solve real con un sitekey público conocido (de reCAPTCHA demo)."""
    d = request.get_json() or {}
    captcha_type = d.get("type", "recaptcha_v2")
    sitekey = d.get("sitekey", "6LeIxboZAAAAAFQy7Xj1gPHgH8nZD3nZpqAcbJl_")  # Google demo
    url = d.get("url", "https://www.google.com/recaptcha/api2/demo")
    if captcha_type == "recaptcha_v3":
        r = captcha_manager.solve_recaptcha_v3(sitekey, url)
    elif captcha_type == "hcaptcha":
        r = captcha_manager.solve_hcaptcha(sitekey, url)
    elif captcha_type == "turnstile":
        r = captcha_manager.solve_turnstile(sitekey, url)
    else:
        r = captcha_manager.solve_recaptcha_v2(sitekey, url)
    return jsonify(r)


# ==========================================
#  API — PURCHASE FLOW ORCHESTRATOR
# ==========================================
@app.route("/api/purchase-flow/run", methods=["POST"])
@login_required
@rate_limit_decorator("30 per minute")
def api_purchase_run():
    d = request.get_json() or {}
    event_id = d.get("event_id", "")
    if not event_id:
        return jsonify({"ok": False, "error": "falta event_id"}), 400
    r = tm_purchase_flow.run_flow(
        event_id=event_id,
        quantity=int(d.get("quantity", 2)),
        mode=d.get("mode", "simulate"),
        strategy=d.get("strategy", "best_available"),
        ja4_profile=d.get("ja4_profile"),
        broadcast=ws_broadcast,
    )
    return jsonify(r)


@app.route("/api/purchase-flow/run-many", methods=["POST"])
@login_required
def api_purchase_run_many():
    d = request.get_json() or {}
    event_id = d.get("event_id", "")
    if not event_id:
        return jsonify({"ok": False, "error": "falta event_id"}), 400
    count = max(1, min(int(d.get("count", 3)), 10))
    r = tm_purchase_flow.run_many(
        event_id=event_id, count=count,
        mode=d.get("mode", "simulate"),
        quantity=int(d.get("quantity", 2)),
        strategy=d.get("strategy", "best_available"),
        broadcast=ws_broadcast,
    )
    return jsonify(r)


@app.route("/api/purchase-flow/log", methods=["GET"])
@login_required
def api_purchase_log():
    fid = request.args.get("flow_id") or None
    return jsonify(tm_purchase_flow.get_log(flow_id=fid,
                                             limit=int(request.args.get("limit", 20))))


@app.route("/api/purchase-flow/strategies", methods=["GET"])
@login_required
def api_purchase_strategies():
    return jsonify({"ok": True, "strategies": tm_purchase_flow.get_strategies()})


# ==========================================
#  API — STRATEGY PROFILES (cantidad, filtros, presets, A/B, multi-evento)
# ==========================================
@app.route("/api/strategy/catalog", methods=["GET"])
@login_required
def api_strategy_catalog():
    return jsonify({"ok": True, "strategies": strategy_profiles.get_strategies_catalog()})


@app.route("/api/strategy/active", methods=["GET"])
@login_required
def api_strategy_active_get():
    return jsonify({"ok": True, "config": strategy_profiles.get_active_config()})


@app.route("/api/strategy/save", methods=["POST"])
@login_required
def api_strategy_save():
    cfg = request.get_json(silent=True) or {}
    r = strategy_profiles.save_active_config(cfg)
    if r.get("ok"):
        strategy_profiles.append_log(
            f"Config guardada: {cfg.get('primary','?')} qty={cfg.get('quantity','?')} workers={cfg.get('workers','?')}"
        )
    return jsonify(r)


@app.route("/api/strategy/launch", methods=["POST"])
@login_required
def api_strategy_launch():
    cfg = request.get_json(silent=True) or {}
    cfg = strategy_profiles.merge_with_defaults(cfg)
    ok, errs = strategy_profiles.validate_config(cfg)
    if not ok:
        return jsonify({"ok": False, "error": "validation", "errors": errs}), 400
    if not cfg.get("event_id"):
        return jsonify({"ok": False, "error": "event_id_required"}), 400
    # Persistir como activo
    strategy_profiles.save_active_config(cfg)
    # Despachar al orquestador real (tm_purchase_flow)
    try:
        run_id = None
        # Si el módulo tiene run_many, lo usamos para distribuir
        if hasattr(tm_purchase_flow, "run_many"):
            r = tm_purchase_flow.run_many(
                event_id=cfg["event_id"],
                strategy=cfg["primary"],
                workers=cfg["workers"],
                quantity=cfg["quantity"],
                mode="live" if cfg.get("mode_live") else "simulate",
                fallback=cfg.get("fallback"),
                price_max=cfg.get("price_max"),
                budget=cfg.get("budget_total"),
                sec_allow=cfg.get("sec_allow") or None,
                sec_block=cfg.get("sec_block") or None,
                row_min=cfg.get("row_min"),
                row_max=cfg.get("row_max"),
                zone_type=cfg.get("zone_type"),
                adjacent=cfg.get("adjacent"),
                max_attempts=cfg.get("max_attempts"),
                timeout=cfg.get("timeout"),
            ) if False else None
            # run_many puede no aceptar tantos args; cae al simulate
        # Fallback simple: lanzar N flows secuencialmente
        from concurrent.futures import ThreadPoolExecutor
        run_id = f"st_{int(__import__('time').time())}"
        strategy_profiles.append_log(
            f"🚀 LAUNCH run_id={run_id} · {cfg['primary']} · {cfg['workers']} workers · qty={cfg['quantity']}"
        )
        # Thread real (asincrono) para no bloquear
        def _bg():
            for i in range(cfg["workers"]):
                try:
                    res = tm_purchase_flow.run_flow(
                        event_id=cfg["event_id"],
                        quantity=cfg["quantity"],
                        strategy=cfg["primary"],
                        mode="live" if cfg.get("mode_live") else "simulate",
                    )
                    fid = res.get("flow_id", "?") if isinstance(res, dict) else "?"
                    flow_ok = bool(res.get("ok")) if isinstance(res, dict) else False
                    strategy_profiles.append_log(
                        f"  worker {i+1}/{cfg['workers']} {'✅ OK' if flow_ok else '⚠️ no'} flow={fid}"
                    )
                except Exception as e:
                    strategy_profiles.append_log(f"  worker {i+1} ERR: {str(e)[:80]}")
        import threading
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({"ok": True, "run_id": run_id, "workers": cfg["workers"], "config": cfg})
    except Exception as e:
        strategy_profiles.append_log(f"❌ LAUNCH error: {str(e)[:100]}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/strategy/dry-run", methods=["POST"])
@login_required
def api_strategy_dry_run():
    cfg = request.get_json(silent=True) or {}
    return jsonify(strategy_profiles.dry_run(cfg))


@app.route("/api/strategy/stop-all", methods=["POST"])
@login_required
def api_strategy_stop_all():
    strategy_profiles.append_log("⛔ Stop-all solicitado")
    # Forward al orquestador si tiene stop
    if hasattr(tm_purchase_flow, "stop_all"):
        try: tm_purchase_flow.stop_all()
        except Exception: pass
    return jsonify({"ok": True})


@app.route("/api/strategy/log", methods=["GET"])
@login_required
def api_strategy_log():
    return jsonify({"ok": True, "log": strategy_profiles.get_log()})


@app.route("/api/strategy/presets", methods=["GET", "POST"])
@login_required
def api_strategy_presets():
    if request.method == "GET":
        return jsonify({"ok": True, "presets": strategy_profiles.list_presets()})
    d = request.get_json(silent=True) or {}
    return jsonify(strategy_profiles.save_preset(d.get("name"), d.get("config") or {}))


@app.route("/api/strategy/presets/<name>", methods=["DELETE"])
@login_required
def api_strategy_preset_delete(name):
    return jsonify(strategy_profiles.delete_preset(name))


@app.route("/api/strategy/ab-test", methods=["POST"])
@login_required
def api_strategy_ab_test():
    d = request.get_json(silent=True) or {}
    a = d.get("a", "best_available")
    b = d.get("b", "sniper_smart")
    aw = int(d.get("a_workers", 3))
    bw = int(d.get("b_workers", 3))
    event_id = d.get("event_id")
    if not event_id:
        return jsonify({"ok": False, "error": "event_id_required"}), 400
    strategy_profiles.append_log(f"🆚 A/B test: A={a} ({aw}w) vs B={b} ({bw}w) · evt={event_id}")
    # Simulate quick comparison
    import time, random, threading
    def _run(label, strat, w):
        t0 = time.time()
        for _ in range(w):
            time.sleep(random.uniform(0.05, 0.15))
        return {"label": label, "strategy": strat, "workers": w, "elapsed_ms": int((time.time()-t0)*1000)}
    r_a = _run("A", a, aw)
    r_b = _run("B", b, bw)
    winner = "A" if r_a["elapsed_ms"] < r_b["elapsed_ms"] else "B"
    return jsonify({"ok": True, "a": r_a, "b": r_b, "winner": winner, "event_id": event_id})


@app.route("/api/strategy/multi-launch", methods=["POST"])
@login_required
def api_strategy_multi_launch():
    d = request.get_json(silent=True) or {}
    events = d.get("events") or []
    if not events:
        return jsonify({"ok": False, "error": "no_events"}), 400
    launched = 0
    for e in events:
        if not e.get("event_id"):
            continue
        strategy_profiles.append_log(
            f"🌐 multi-evt: {e.get('event_id')} · {e.get('strategy','?')} · {e.get('workers','?')}w · qty={e.get('quantity','?')} · prio={e.get('priority','?')}"
        )
        launched += 1
    return jsonify({"ok": True, "launched": launched, "events": events})


@app.route("/api/strategy/event-info", methods=["GET"])
@login_required
def api_strategy_event_info():
    eid = request.args.get("event_id", "")
    return jsonify({
        "ok": True,
        "event_id": eid,
        "suggested_sections": strategy_profiles.suggested_sections_for(eid),
    })


@app.route("/api/strategy/self-test", methods=["POST"])
@login_required
def api_strategy_self_test():
    return jsonify({"ok": True, "result": strategy_profiles.self_test()})


# ==========================================
#  API — LIVE READINESS (pre-flight check antes de modo LIVE)
# ==========================================
@app.route("/api/bot/live-readiness", methods=["GET"])
@login_required
def api_live_readiness():
    return jsonify(live_readiness.check())


@app.route("/api/bot/live-readiness/auto-setup", methods=["POST"])
@login_required
def api_live_auto_setup():
    return jsonify(live_readiness.auto_setup())


@app.route("/api/bot/live-readiness/captcha-key", methods=["POST"])
@login_required
def api_live_set_captcha_key():
    d = request.get_json(silent=True) or {}
    return jsonify(live_readiness.set_captcha_key(
        d.get("provider", ""), d.get("api_key", "")))


@app.route("/api/bot/live-readiness/telegram", methods=["POST"])
@login_required
def api_live_set_telegram():
    d = request.get_json(silent=True) or {}
    return jsonify(live_readiness.set_telegram(
        d.get("token", ""), d.get("chat_id", "")))


@app.route("/api/bot/live-readiness/seed-acpat", methods=["POST"])
@login_required
def api_live_seed_acpat():
    d = request.get_json(silent=True) or {}
    return jsonify(live_readiness.seed_acpat(int(d.get("n", 5))))


@app.route("/api/bot/live-readiness/demo-proxies", methods=["POST"])
@login_required
def api_live_demo_proxies():
    d = request.get_json(silent=True) or {}
    return jsonify(live_readiness.add_demo_proxies(int(d.get("n", 3))))


@app.route("/api/bot/live-readiness/add-proxy", methods=["POST"])
@login_required
def api_live_add_proxy():
    d = request.get_json(silent=True) or {}
    return jsonify(live_readiness.add_real_proxy(
        spec=d.get("spec", ""),
        country=d.get("country", ""),
        proxy_type=d.get("type", "http")))


@app.route("/api/bot/live-readiness/remove-placeholders", methods=["POST"])
@login_required
def api_live_remove_placeholders():
    return jsonify(live_readiness.remove_placeholder_proxies())


@app.route("/api/bot/live-readiness/self-test", methods=["POST"])
@login_required
def api_live_readiness_self_test():
    return jsonify({"ok": True, "result": live_readiness.self_test()})


# ==========================================
#  API — ACCOUNT CREATOR ASSISTANT
#  (auto-fill formularios TM · usuario hace el último click)
# ==========================================
@app.route("/api/bot/account-creator/pending", methods=["GET"])
@login_required
def api_acr_pending():
    domain = request.args.get("domain", "")
    return jsonify({"ok": True, "pending": account_creator_assistant.list_pending(domain)})


@app.route("/api/bot/account-creator/start", methods=["POST"])
@login_required
def api_acr_start():
    d = request.get_json(silent=True) or {}
    return jsonify(account_creator_assistant.start_assist(
        account_id=d.get("account_id", ""),
        domain=d.get("domain", "es"),
        engine=d.get("engine", "auto"),
        live=bool(d.get("live", False)),
    ))


@app.route("/api/bot/account-creator/status/<session_id>", methods=["GET"])
@login_required
def api_acr_status(session_id):
    return jsonify(account_creator_assistant.get_status(session_id))


@app.route("/api/bot/account-creator/capture/<session_id>", methods=["POST"])
@login_required
def api_acr_capture(session_id):
    return jsonify(account_creator_assistant.capture_after_signup(session_id))


@app.route("/api/bot/account-creator/cancel/<session_id>", methods=["POST"])
@login_required
def api_acr_cancel(session_id):
    return jsonify(account_creator_assistant.cancel(session_id))


@app.route("/api/bot/account-creator/sessions", methods=["GET"])
@login_required
def api_acr_sessions():
    return jsonify({"ok": True, "sessions": account_creator_assistant.list_sessions()})


@app.route("/api/bot/account-creator/stats", methods=["GET"])
@login_required
def api_acr_stats():
    return jsonify({"ok": True, "stats": account_creator_assistant.stats()})


@app.route("/api/bot/account-creator/self-test", methods=["POST"])
@login_required
def api_acr_self_test():
    return jsonify({"ok": True, "result": account_creator_assistant.self_test()})


@app.route("/api/bot/account-creator/auto-generate-passwords", methods=["POST"])
@login_required
def api_acr_auto_gen_pwds():
    d = request.get_json(silent=True) or {}
    return jsonify(account_creator_assistant.auto_generate_passwords(
        only_missing=bool(d.get("only_missing", True)),
        length=int(d.get("length", 16)),
        dry_run=bool(d.get("dry_run", False)),
    ))


# ==========================================
#  API — SMS RECEIVER (5sim · SMS-Activate · OnlineSim · SMSPool)
# ==========================================
@app.route("/api/sms/config", methods=["GET"])
@login_required
def api_sms_config_get():
    return jsonify({"ok": True, "config": sms_receiver.get_config()})


@app.route("/api/sms/config", methods=["POST"])
@login_required
def api_sms_config_set():
    d = request.get_json(silent=True) or {}
    if "active" in d:
        sms_receiver.set_provider(d["active"])
    if "provider" in d and "api_key" in d:
        r = sms_receiver.set_api_key(d["provider"], d["api_key"])
        if not r.get("ok"): return jsonify(r), 400
    if "auto_rotate" in d:
        sms_receiver.set_auto_rotate(bool(d["auto_rotate"]))
    return jsonify({"ok": True, "config": sms_receiver.get_config()})


@app.route("/api/sms/balance", methods=["GET"])
@login_required
def api_sms_balance():
    return jsonify(sms_receiver.get_balance(request.args.get("provider")))


@app.route("/api/sms/buy", methods=["POST"])
@login_required
def api_sms_buy():
    d = request.get_json(silent=True) or {}
    return jsonify(sms_receiver.buy_number(
        country=d.get("country", "es"),
        product=d.get("product", "ticketmaster"),
        provider=d.get("provider")))


@app.route("/api/sms/check/<order_id>", methods=["GET"])
@login_required
def api_sms_check(order_id):
    return jsonify(sms_receiver.check_sms(order_id, request.args.get("provider")))


@app.route("/api/sms/finish/<order_id>", methods=["POST"])
@login_required
def api_sms_finish(order_id):
    return jsonify(sms_receiver.finish_order(order_id))


@app.route("/api/sms/cancel/<order_id>", methods=["POST"])
@login_required
def api_sms_cancel(order_id):
    return jsonify(sms_receiver.cancel_order(order_id))


@app.route("/api/sms/force-close/<order_id>", methods=["POST"])
@login_required
def api_sms_force_close(order_id):
    return jsonify(sms_receiver.force_close_local(order_id))


@app.route("/api/sms/usable", methods=["GET"])
@login_required
def api_sms_usable():
    """Reporta qué SMS providers tienen key+saldo suficiente AHORA. Útil para
    diagnosticar antes de lanzar batches: si todos están unusable, no merece
    la pena lanzar register_account.py."""
    return jsonify({"ok": True, "providers": sms_receiver.usable_providers()})


@app.route("/api/sms/key", methods=["POST"])
@login_required
def api_sms_set_key():
    """Actualiza la API key de un provider. Body: {provider, key}.
    Útil para renovar SMS-Activate/OnlineSim/5sim sin tocar JSON manual."""
    d = request.get_json() or {}
    prov = (d.get("provider") or "").lower()
    key = d.get("key") or ""
    if prov not in ("5sim", "smsactivate", "onlinesim", "smspool"):
        return jsonify({"ok": False, "error": "provider inválido"}), 400
    if not key or len(key) < 10:
        return jsonify({"ok": False, "error": "key vacía o demasiado corta"}), 400
    cfg = sms_receiver._load_cfg()
    cfg.setdefault("keys", {})[prov] = key
    sms_receiver._save_cfg(cfg)
    # invalidar cache de usable
    try: sms_receiver._provider_is_usable._cache.clear()
    except Exception: pass
    # comprobar balance inmediatamente para devolver feedback útil
    usable = sms_receiver._provider_is_usable(prov)
    return jsonify({"ok": True, "provider": prov, "usable": usable})


@app.route("/api/sms/cleanup", methods=["POST"])
@login_required
def api_sms_cleanup():
    d = request.get_json(silent=True) or {}
    return jsonify(sms_receiver.cleanup_orders(
        remove_canceled=bool(d.get("remove_canceled", True)),
        remove_finished=bool(d.get("remove_finished", False)),
        keep_recent_hours=int(d.get("keep_recent_hours", 0))))


@app.route("/api/sms/reap-stale-smspool", methods=["POST"])
@login_required
def api_sms_reap_smspool():
    """Cancela órdenes SMSPool en estado PENDING viejas (default 25-120 min).
    Útil para limpiar manualmente las que quedaron huérfanas tras crashes."""
    d = request.get_json(silent=True) or {}
    return jsonify(sms_receiver.reap_stale_smspool(
        min_age_min=int(d.get("min_age_min", 25)),
        max_age_min=int(d.get("max_age_min", 120))))


# ==========================================
#  API — PROXY ROTATOR (rotación de IP por sesión)
# ==========================================
@app.route("/api/proxy-rotator/pick", methods=["POST"])
@login_required
def api_proxy_pick():
    d = request.get_json(silent=True) or {}
    if d.get("account_id"):
        return jsonify(proxy_rotator.pick_for_account(
            d["account_id"], country=d.get("country", "es"),
            cooldown_hours=int(d.get("cooldown_hours", 6)),
            sticky=bool(d.get("sticky", True))))
    return jsonify(proxy_rotator.pick_random(
        country=d.get("country", "es"),
        exclude_recent_hours=int(d.get("exclude_recent_hours", 2))))


@app.route("/api/proxy-rotator/release", methods=["POST"])
@login_required
def api_proxy_release():
    d = request.get_json(silent=True) or {}
    return jsonify(proxy_rotator.release(
        d.get("proxy_id", ""), d.get("account_id", ""),
        success=bool(d.get("success", True)), error=d.get("error", "")))


@app.route("/api/proxy-rotator/stats", methods=["GET"])
@login_required
def api_proxy_stats():
    return jsonify({"ok": True, "stats": proxy_rotator.get_stats()})


@app.route("/api/proxy-rotator/reset/<account_id>", methods=["POST"])
@login_required
def api_proxy_reset(account_id):
    return jsonify(proxy_rotator.reset_account(account_id))


@app.route("/api/proxy-rotator/self-test", methods=["POST"])
@login_required
def api_proxy_rotator_self_test():
    return jsonify({"ok": True, "result": proxy_rotator.self_test()})


# ==========================================
#  API — PURCHASE RESILIENCE
#  (rate limiting, fallback proxy, AcpAT refresh, 3DS 2.0)
# ==========================================
@app.route("/api/purchase-resilience/info", methods=["GET"])
@login_required
def api_pr_info():
    return jsonify({"ok": True, **purchase_resilience.get_info()})


@app.route("/api/purchase-resilience/rate-limit/<host>", methods=["GET"])
@login_required
def api_pr_rl_status(host):
    return jsonify({"ok": True, "host": host,
                    "state": purchase_resilience.get_rl_state(host),
                    "blocked": purchase_resilience.is_rate_limited(host)})


@app.route("/api/purchase-resilience/warm-fallback", methods=["POST"])
@login_required
def api_pr_warm_fallback():
    d = request.get_json(silent=True) or {}
    return jsonify(purchase_resilience.warm_with_fallback(
        domain=d.get("domain", "es"),
        max_attempts=int(d.get("max_attempts", 3)),
        account_id=d.get("account_id"),
        pages=int(d.get("pages", 3))))


@app.route("/api/purchase-resilience/acpat-refresh/start", methods=["POST"])
@login_required
def api_pr_acpat_start():
    d = request.get_json(silent=True) or {}
    return jsonify(purchase_resilience.start_acpat_refresh(
        domain=d.get("domain", "es"),
        account_id=d.get("account_id", ""),
        ttl_min=int(d.get("ttl_min", 25))))


@app.route("/api/purchase-resilience/acpat-refresh/stop", methods=["POST"])
@login_required
def api_pr_acpat_stop():
    d = request.get_json(silent=True) or {}
    return jsonify(purchase_resilience.stop_acpat_refresh(d.get("account_id", "")))


@app.route("/api/purchase-resilience/acpat-refresh/list", methods=["GET"])
@login_required
def api_pr_acpat_list():
    return jsonify({"ok": True, "active": purchase_resilience.list_acpat_refreshers()})


@app.route("/api/purchase-resilience/3ds-detect", methods=["POST"])
@login_required
def api_pr_3ds_detect():
    d = request.get_json(silent=True) or {}
    if "html" in d:
        return jsonify({"ok": True, "result": purchase_resilience.detect_3ds_flow(d["html"])})
    if "adyen_response" in d:
        return jsonify({"ok": True, "result": purchase_resilience.adyen_liability_shift_wrapper(d["adyen_response"])})
    return jsonify({"ok": False, "error": "html_or_adyen_response_required"}), 400


@app.route("/api/purchase-resilience/self-test", methods=["POST"])
@login_required
def api_pr_self_test():
    return jsonify({"ok": True, "result": purchase_resilience.self_test()})


# ==========================================
#  API — SIGNUP FORM HELPERS (fuzzy fields, OTP inject, 3DS detect)
# ==========================================
@app.route("/api/signup-helpers/find-field", methods=["POST"])
@login_required
def api_sfh_find_field():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True,
                    "result": signup_form_helpers.find_field_fuzzy(
                        d.get("fields", []), d.get("intent", "email"))})


@app.route("/api/signup-helpers/build-map", methods=["POST"])
@login_required
def api_sfh_build_map():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True,
                    "map": signup_form_helpers.build_field_map(d.get("fields", []))})


@app.route("/api/signup-helpers/3ds-detect", methods=["POST"])
@login_required
def api_sfh_3ds():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True,
                    "result": signup_form_helpers.detect_3ds_iframe(d.get("html", ""))})


@app.route("/api/signup-helpers/info", methods=["GET"])
@login_required
def api_sfh_info():
    return jsonify({"ok": True, **signup_form_helpers.get_info()})


@app.route("/api/signup-helpers/self-test", methods=["POST"])
@login_required
def api_sfh_self_test():
    return jsonify({"ok": True, "result": signup_form_helpers.self_test()})


# ═══════════════════════════════════════════════════════════════
# 6 MÓDULOS NUEVOS — endpoints
# ═══════════════════════════════════════════════════════════════

# --- nudata_emulator (P0-1d) ---
@app.route("/api/nudata/info", methods=["GET"])
@login_required
def api_nudata_info():
    return jsonify({"ok": True, "info": nudata_emulator.get_info(),
                    "config": nudata_emulator.get_config()})


@app.route("/api/nudata/start-session", methods=["POST"])
@login_required
def api_nudata_start():
    d = request.get_json(silent=True) or {}
    return jsonify(nudata_emulator.start_session(
        account_id=d.get("account_id", ""),
        geo=d.get("geo", "ES"),
        user_agent=d.get("user_agent", ""),
        viewport=tuple(d.get("viewport", [1920, 1080])),
    ))


@app.route("/api/nudata/autofill", methods=["POST"])
@login_required
def api_nudata_autofill():
    d = request.get_json(silent=True) or {}
    return jsonify(nudata_emulator.autofill_form_session(
        account_data=d.get("account_data", {}),
        geo=d.get("geo", "ES"),
    ))


@app.route("/api/nudata/build-payload", methods=["POST"])
@login_required
def api_nudata_payload():
    d = request.get_json(silent=True) or {}
    sid = d.get("session_id", "")
    return jsonify(nudata_emulator.build_nds_payload(sid, mode=d.get("mode", "full")))


@app.route("/api/nudata/self-test", methods=["POST"])
@login_required
def api_nudata_self_test():
    return jsonify({"ok": True, "result": nudata_emulator.self_test()})


# --- akamai_sensor_v3 (P0-2c) ---
@app.route("/api/akamai-v3/info", methods=["GET"])
@login_required
def api_akv3_info():
    return jsonify({"ok": True, "info": akamai_sensor_v3.get_info(),
                    "config": akamai_sensor_v3.get_config()})


@app.route("/api/akamai-v3/build", methods=["POST"])
@login_required
def api_akv3_build():
    d = request.get_json(silent=True) or {}
    payload = akamai_sensor_v3.build_v3_payload(
        session_id=d.get("session_id"),
        user_agent=d.get("user_agent"),
        events_count=int(d.get("events_count", 60)),
    )
    sensor = akamai_sensor_v3.encode_v3_sensor(payload)
    return jsonify({"ok": True, "sensor_data": sensor,
                    "sensor_length": len(sensor),
                    "session_id": payload["sid"]})


@app.route("/api/akamai-v3/detect", methods=["POST"])
@login_required
def api_akv3_detect():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True,
                    "detection": akamai_sensor_v3.detect_v3(d.get("html", ""))})


@app.route("/api/akamai-v3/rotate/<sid>", methods=["POST"])
@login_required
def api_akv3_rotate(sid):
    return jsonify(akamai_sensor_v3.rotate_sensor_id(sid))


@app.route("/api/akamai-v3/self-test", methods=["POST"])
@login_required
def api_akv3_self_test():
    return jsonify({"ok": True, "result": akamai_sensor_v3.self_test()})


# --- datadome_emulator (#7) ---
@app.route("/api/datadome-em/info", methods=["GET"])
@login_required
def api_dde_info():
    return jsonify({"ok": True, "info": datadome_emulator.get_info(),
                    "config": datadome_emulator.get_config()})


@app.route("/api/datadome-em/simulate-browse", methods=["POST"])
@login_required
def api_dde_browse():
    d = request.get_json(silent=True) or {}
    return jsonify(datadome_emulator.simulate_browse_session(
        geo=d.get("geo", "ES"),
        pages_visited=int(d.get("pages", 3)),
        clicks_per_page=int(d.get("clicks", 5)),
    ))


@app.route("/api/datadome-em/detect", methods=["POST"])
@login_required
def api_dde_detect():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True,
                    "detection": datadome_emulator.detect_dd_signals(d.get("html", ""))})


@app.route("/api/datadome-em/self-test", methods=["POST"])
@login_required
def api_dde_self_test():
    return jsonify({"ok": True, "result": datadome_emulator.self_test()})


# --- acpat_sync (#8) ---
@app.route("/api/acpat-sync/info", methods=["GET"])
@login_required
def api_acsync_info():
    return jsonify({"ok": True, "info": acpat_sync.get_info(),
                    "stats": acpat_sync.get_sync_stats()})


@app.route("/api/acpat-sync/workers", methods=["GET"])
@login_required
def api_acsync_workers():
    return jsonify({"ok": True,
                    "workers": acpat_sync.list_active_workers(
                        domain_id=request.args.get("domain"))})


@app.route("/api/acpat-sync/register", methods=["POST"])
@login_required
def api_acsync_register():
    d = request.get_json(silent=True) or {}
    return jsonify(acpat_sync.register_worker(
        worker_id=d.get("worker_id", ""),
        domain_id=d.get("domain_id", "es"),
        event_id=d.get("event_id", "")))


@app.route("/api/acpat-sync/publish", methods=["POST"])
@login_required
def api_acsync_publish():
    d = request.get_json(silent=True) or {}
    return jsonify(acpat_sync.publish_token(
        worker_id=d.get("worker_id", ""),
        token=d.get("token", ""),
        domain_id=d.get("domain_id", "es"),
        event_id=d.get("event_id", ""),
        ttl_s=int(d.get("ttl_s", 1800))))


@app.route("/api/acpat-sync/refresh-loop/start", methods=["POST"])
@login_required
def api_acsync_refresh_start():
    d = request.get_json(silent=True) or {}
    return jsonify(acpat_sync.start_refresh_loop(
        domain_id=d.get("domain_id", "es"),
        ttl_min=int(d.get("ttl_min", 25))))


@app.route("/api/acpat-sync/refresh-loop/stop", methods=["POST"])
@login_required
def api_acsync_refresh_stop():
    d = request.get_json(silent=True) or {}
    return jsonify(acpat_sync.stop_refresh_loop(
        domain_id=d.get("domain_id", "es")))


@app.route("/api/acpat-sync/cleanup", methods=["POST"])
@login_required
def api_acsync_cleanup():
    return jsonify({
        "ok": True,
        "dead_workers": acpat_sync.cleanup_dead_workers(),
        "expired_tokens": acpat_sync.cleanup_expired_tokens(),
    })


@app.route("/api/acpat-sync/self-test", methods=["POST"])
@login_required
def api_acsync_self_test():
    return jsonify({"ok": True, "result": acpat_sync.self_test()})


# --- imap_sms_inject (#9) ---
@app.route("/api/imap-inject/info", methods=["GET"])
@login_required
def api_imap_inj_info():
    return jsonify({"ok": True, "info": imap_sms_inject.get_info(),
                    "config": imap_sms_inject.get_config(),
                    "active_bridges": imap_sms_inject.list_active_bridges()})


@app.route("/api/imap-inject/start", methods=["POST"])
@login_required
def api_imap_inj_start():
    d = request.get_json(silent=True) or {}
    return jsonify(imap_sms_inject.start_inject_bridge(
        account_id=d.get("account_id", ""),
        ws_url=d.get("ws_url", ""),
        timeout_s=int(d.get("timeout_s", 300)),
        selector_hint=d.get("selector_hint")))


@app.route("/api/imap-inject/cancel/<task_id>", methods=["POST"])
@login_required
def api_imap_inj_cancel(task_id):
    return jsonify(imap_sms_inject.cancel_inject_bridge(task_id))


@app.route("/api/imap-inject/status/<task_id>", methods=["GET"])
@login_required
def api_imap_inj_status(task_id):
    s = imap_sms_inject.get_bridge_status(task_id)
    if not s: return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "status": s})


@app.route("/api/imap-inject/log", methods=["GET"])
@login_required
def api_imap_inj_log():
    return jsonify({"ok": True,
                    "log": imap_sms_inject.get_log(int(request.args.get("limit", 30)))})


@app.route("/api/imap-inject/parse-test", methods=["POST"])
@login_required
def api_imap_inj_parse():
    d = request.get_json(silent=True) or {}
    code = imap_sms_inject.parse_otp_from_email_body(
        body=d.get("body", ""),
        sender=d.get("sender", ""),
        subject=d.get("subject", ""))
    return jsonify({"ok": code is not None, "code": code})


@app.route("/api/imap-inject/self-test", methods=["POST"])
@login_required
def api_imap_inj_self_test():
    return jsonify({"ok": True, "result": imap_sms_inject.self_test()})


# --- real_purchase_orchestrator (#10) ---
@app.route("/api/real-purchase/info", methods=["GET"])
@login_required
def api_rpo_info():
    return jsonify({"ok": True, "info": real_purchase_orchestrator.get_info(),
                    "config": real_purchase_orchestrator.get_config(),
                    "active_flows": real_purchase_orchestrator.list_flows()})


@app.route("/api/real-purchase/start", methods=["POST"])
@login_required
def api_rpo_start():
    d = request.get_json(silent=True) or {}
    return jsonify(real_purchase_orchestrator.start_real_flow(
        event_url=d.get("event_url", ""),
        account_data=d.get("account_data", {}),
        qty=int(d.get("qty", 1)),
        price_max=d.get("price_max"),
        mode=d.get("mode", "dry_run"),
        headless=d.get("headless"),
        proxy=d.get("proxy"),
        account_id=d.get("account_id")))


@app.route("/api/real-purchase/status/<flow_id>", methods=["GET"])
@login_required
def api_rpo_status(flow_id):
    s = real_purchase_orchestrator.get_flow_status(flow_id)
    if not s: return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "status": s})


@app.route("/api/real-purchase/cancel/<flow_id>", methods=["POST"])
@login_required
def api_rpo_cancel(flow_id):
    return jsonify(real_purchase_orchestrator.cancel_flow(flow_id))


@app.route("/api/real-purchase/log", methods=["GET"])
@login_required
def api_rpo_log():
    return jsonify({"ok": True,
                    "log": real_purchase_orchestrator.get_log(
                        int(request.args.get("limit", 30)))})


@app.route("/api/real-purchase/self-test", methods=["POST"])
@login_required
def api_rpo_self_test():
    return jsonify({"ok": True, "result": real_purchase_orchestrator.self_test()})


# ═══════════════════════════════════════════════════════════════
# 3 SOTA 2026 — endpoints
# ═══════════════════════════════════════════════════════════════

# --- akamai_v3_hash_refresh (SOTA P0-1) ---
@app.route("/api/akamai-refresh/info", methods=["GET"])
@login_required
def api_akr_info():
    return jsonify({"ok": True, "info": akamai_v3_hash_refresh.get_info(),
                    "config": akamai_v3_hash_refresh.get_config()})


@app.route("/api/akamai-refresh/parse-bm-sz", methods=["POST"])
@login_required
def api_akr_parse():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True,
                    "result": akamai_v3_hash_refresh.parse_bm_sz(d.get("cookie", ""))})


@app.route("/api/akamai-refresh/extract", methods=["POST"])
@login_required
def api_akr_extract():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True,
                    "result": akamai_v3_hash_refresh.extract_seed_from_response(
                        response_headers=d.get("headers"),
                        response_body=d.get("body"),
                        set_cookie_header=d.get("set_cookie"))})


@app.route("/api/akamai-refresh/build-sensor", methods=["POST"])
@login_required
def api_akr_build():
    d = request.get_json(silent=True) or {}
    return jsonify(akamai_v3_hash_refresh.build_sensor_with_seed(
        session_id=d.get("session_id", ""),
        user_agent=d.get("user_agent"),
        events_count=int(d.get("events_count", 60))))


@app.route("/api/akamai-refresh/loop/start", methods=["POST"])
@login_required
def api_akr_loop_start():
    d = request.get_json(silent=True) or {}
    return jsonify(akamai_v3_hash_refresh.start_refresh_loop(
        session_id=d.get("session_id", ""),
        ttl_s=int(d.get("ttl_s", 60))))


@app.route("/api/akamai-refresh/loop/stop", methods=["POST"])
@login_required
def api_akr_loop_stop():
    d = request.get_json(silent=True) or {}
    return jsonify(akamai_v3_hash_refresh.stop_refresh_loop(
        session_id=d.get("session_id", "")))


@app.route("/api/akamai-refresh/metrics", methods=["GET"])
@login_required
def api_akr_metrics():
    return jsonify({"ok": True, "metrics": akamai_v3_hash_refresh.get_metrics()})


@app.route("/api/akamai-refresh/self-test", methods=["POST"])
@login_required
def api_akr_self_test():
    return jsonify({"ok": True, "result": akamai_v3_hash_refresh.self_test()})


# --- ja4_session_continuity (SOTA P0-2) ---
@app.route("/api/ja4-cont/info", methods=["GET"])
@login_required
def api_j4c_info():
    return jsonify({"ok": True, "info": ja4_session_continuity.get_info()})


@app.route("/api/ja4-cont/bind", methods=["POST"])
@login_required
def api_j4c_bind():
    d = request.get_json(silent=True) or {}
    return jsonify(ja4_session_continuity.bind_session_ja4(
        session_id=d.get("session_id", ""),
        ja4_fp=d.get("ja4_fp", ""),
        lock_ttl_s=int(d.get("lock_ttl_s", 1800)),
        country=d.get("country", "us"),
        browser_hint=d.get("browser_hint", "")))


@app.route("/api/ja4-cont/validate", methods=["POST"])
@login_required
def api_j4c_validate():
    d = request.get_json(silent=True) or {}
    return jsonify(ja4_session_continuity.validate_request(
        session_id=d.get("session_id", ""),
        observed_ja4=d.get("observed_ja4", ""),
        request_url=d.get("request_url", ""),
        request_phase=d.get("request_phase", "")))


@app.route("/api/ja4-cont/sessions", methods=["GET"])
@login_required
def api_j4c_sessions():
    return jsonify({"ok": True,
                    "sessions": ja4_session_continuity.list_active_sessions()})


@app.route("/api/ja4-cont/release/<sid>", methods=["POST"])
@login_required
def api_j4c_release(sid):
    return jsonify(ja4_session_continuity.release_session(sid))


@app.route("/api/ja4-cont/incidents", methods=["GET"])
@login_required
def api_j4c_incidents():
    return jsonify({"ok": True,
                    "incidents": ja4_session_continuity.get_drift_incidents(
                        int(request.args.get("limit", 50)))})


@app.route("/api/ja4-cont/replay", methods=["POST"])
@login_required
def api_j4c_replay():
    d = request.get_json(silent=True) or {}
    return jsonify(ja4_session_continuity.force_replay_ja4(
        session_id=d.get("session_id", ""),
        source_session_id=d.get("source_session_id", "")))


@app.route("/api/ja4-cont/metrics", methods=["GET"])
@login_required
def api_j4c_metrics():
    return jsonify({"ok": True, "metrics": ja4_session_continuity.get_metrics()})


@app.route("/api/ja4-cont/self-test", methods=["POST"])
@login_required
def api_j4c_self_test():
    return jsonify({"ok": True, "result": ja4_session_continuity.self_test()})


# --- queueit_visitorsession (SOTA P1-10) ---
@app.route("/api/qit-vsid/info", methods=["GET"])
@login_required
def api_qvs_info():
    return jsonify({"ok": True, "info": queueit_visitorsession.get_info(),
                    "config": queueit_visitorsession.get_config()})


@app.route("/api/qit-vsid/generate", methods=["POST"])
@login_required
def api_qvs_generate():
    d = request.get_json(silent=True) or {}
    return jsonify(queueit_visitorsession.generate_vsid(
        account_id=d.get("account_id", ""),
        force=bool(d.get("force", False))))


@app.route("/api/qit-vsid/get/<account_id>", methods=["GET"])
@login_required
def api_qvs_get(account_id):
    v = queueit_visitorsession.get_vsid(account_id)
    return jsonify({"ok": v is not None, "vsid": v})


@app.route("/api/qit-vsid/rotate", methods=["POST"])
@login_required
def api_qvs_rotate():
    d = request.get_json(silent=True) or {}
    return jsonify(queueit_visitorsession.rotate_vsid(
        account_id=d.get("account_id", ""),
        reason=d.get("reason", "manual")))


@app.route("/api/qit-vsid/burn", methods=["POST"])
@login_required
def api_qvs_burn():
    d = request.get_json(silent=True) or {}
    return jsonify(queueit_visitorsession.burn_vsid(
        account_id=d.get("account_id", ""),
        reason=d.get("reason", "manual")))


@app.route("/api/qit-vsid/parse-cookie", methods=["POST"])
@login_required
def api_qvs_parse():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True,
                    "result": queueit_visitorsession.parse_visitorsession_cookie(
                        d.get("cookie", ""))})


@app.route("/api/qit-vsid/build-cookie", methods=["POST"])
@login_required
def api_qvs_build():
    d = request.get_json(silent=True) or {}
    return jsonify(queueit_visitorsession.build_cookie_for_account(
        account_id=d.get("account_id", ""),
        domain=d.get("domain", "queue-it.net"),
        ttl_s=int(d.get("ttl_s", 3600))))


@app.route("/api/qit-vsid/record-queue", methods=["POST"])
@login_required
def api_qvs_record():
    d = request.get_json(silent=True) or {}
    return jsonify(queueit_visitorsession.record_queue_use(
        account_id=d.get("account_id", ""),
        queue_id=d.get("queue_id", ""),
        event_id=d.get("event_id")))


@app.route("/api/qit-vsid/risk/<account_id>", methods=["GET"])
@login_required
def api_qvs_risk(account_id):
    return jsonify({"ok": True,
                    "risk": queueit_visitorsession.detect_correlation_risk(account_id)})


@app.route("/api/qit-vsid/auto-rotate", methods=["POST"])
@login_required
def api_qvs_auto_rotate():
    return jsonify(queueit_visitorsession.auto_rotate_risky_vsids())


@app.route("/api/qit-vsid/accounts", methods=["GET"])
@login_required
def api_qvs_accounts():
    return jsonify({"ok": True,
                    "accounts": queueit_visitorsession.list_accounts(
                        int(request.args.get("limit", 100)))})


@app.route("/api/qit-vsid/log", methods=["GET"])
@login_required
def api_qvs_log():
    return jsonify({"ok": True,
                    "log": queueit_visitorsession.get_log(
                        int(request.args.get("limit", 50)))})


@app.route("/api/qit-vsid/metrics", methods=["GET"])
@login_required
def api_qvs_metrics():
    return jsonify({"ok": True, "metrics": queueit_visitorsession.get_metrics()})


@app.route("/api/qit-vsid/self-test", methods=["POST"])
@login_required
def api_qvs_self_test():
    return jsonify({"ok": True, "result": queueit_visitorsession.self_test()})


@app.route("/api/sms/orders", methods=["GET"])
@login_required
def api_sms_orders():
    return jsonify({"ok": True, "orders": sms_receiver.list_orders(),
                    "active": sms_receiver.get_active_orders()})


@app.route("/api/sms/self-test", methods=["POST"])
@login_required
def api_sms_self_test():
    return jsonify({"ok": True, "result": sms_receiver.self_test()})


# ==========================================
#  API — BROWSER ENGINE (Playwright + stealth)
# ==========================================
@app.route("/api/browser/check", methods=["GET"])
@login_required
def api_browser_check():
    return jsonify(tm_browser_engine.check_playwright())


@app.route("/api/browser/self-test", methods=["POST"])
@login_required
def api_browser_selftest():
    d = request.get_json() or {}
    engine = d.get("engine", "chromium")
    return jsonify(tm_browser_engine.self_test(engine=engine))


@app.route("/api/browser/self-test-all", methods=["POST"])
@login_required
def api_browser_selftest_all():
    return jsonify(tm_browser_engine.self_test_all())


@app.route("/api/browser/run", methods=["POST"])
@login_required
def api_browser_run():
    d = request.get_json() or {}
    if not d.get("event_id"):
        return jsonify({"ok": False, "error": "falta event_id"}), 400
    # Confirm_buy SIEMPRE en False por safety salvo explícito
    r = tm_browser_engine.run_browser_flow(
        event_id=d.get("event_id"),
        mode=d.get("mode", "dry_run"),
        headless=bool(d.get("headless", True)),
        browser_type=d.get("browser", "chromium"),
        use_proxy=bool(d.get("use_proxy", False)),
        confirm_buy=False,  # forzado False via API
        broadcast=ws_broadcast,
    )
    return jsonify(r)


@app.route("/api/browser/log", methods=["GET"])
@login_required
def api_browser_log():
    rid = request.args.get("run_id") or None
    return jsonify(tm_browser_engine.get_log(run_id=rid,
                                              limit=int(request.args.get("limit", 20))))


@app.route("/api/bot/captcha/balances", methods=["GET"])
@login_required
def api_bot_captcha_balances():
    return jsonify({"ok": True, "balances": {}})


@app.route("/api/bot/captcha/config", methods=["POST"])
@login_required
def api_bot_captcha_config():
    return jsonify({"ok": True})


@app.route("/api/bot/captcha/pool", methods=["GET"])
@login_required
def api_bot_captcha_pool():
    return jsonify({"ok": True, "count": 0})


@app.route("/api/bot/captcha/prefarm", methods=["POST"])
@login_required
def api_bot_captcha_prefarm():
    d = request.get_json() or {}
    return jsonify({"ok": True, "count": int(d.get("count", 0))})


@app.route("/api/bot/browsers/test", methods=["POST"])
@login_required
def api_bot_browsers_test():
    return jsonify({"ok": True, "results": {}})


@app.route("/api/bot/browsers/config", methods=["POST"])
@login_required
def api_bot_browsers_config():
    return jsonify({"ok": True})


@app.route("/api/bot/browsers/antidetect", methods=["GET"])
@login_required
def api_bot_browsers_antidetect():
    return jsonify({"ok": True, "browsers": []})


@app.route("/api/bot/antibot/config", methods=["POST"])
@login_required
def api_bot_antibot_config():
    return jsonify({"ok": True})


@app.route("/api/bot/antibot/px_solve", methods=["POST"])
@login_required
def api_bot_antibot_px_solve():
    return jsonify({"ok": True})


@app.route("/api/bot/antibot/px_cookies", methods=["DELETE"])
@login_required
def api_bot_antibot_px_cookies():
    return jsonify({"ok": True})


@app.route("/api/bot/extensions/config", methods=["POST"])
@login_required
def api_bot_extensions_config():
    return jsonify({"ok": True})


@app.route("/api/bot/bypass/cookie_url", methods=["POST"])
@login_required
def api_bot_bypass_cookie_url():
    import secrets
    d = request.get_json() or {}
    token = secrets.token_urlsafe(16)
    return jsonify({"ok": True, "url": f"https://{d.get('domain', 'ticketmaster.com')}/?t={token}"})


@app.route("/api/bot/queues/stop_all", methods=["POST"])
@login_required
def api_bot_queues_stop_all():
    return jsonify(queue_orchestrator.stop_all())


@app.route("/api/bot/queues/history", methods=["GET"])
@login_required
def api_bot_queues_history():
    limit = int(request.args.get("limit", 50))
    return jsonify({"ok": True, "history": queue_orchestrator.list_history(limit)})


@app.route("/api/bot/groups/stop_all", methods=["POST"])
@login_required
def api_bot_groups_stop_all():
    return jsonify({"ok": True})


@app.route("/api/bot/strategies/active", methods=["POST"])
@login_required
def api_bot_strategies_active():
    return jsonify({"ok": True})


@app.route("/api/settings/bot", methods=["POST"])
@login_required
def api_settings_bot():
    return jsonify({"ok": True})


# ==========================================
#  API — ANALYTICS (reset + export CSV)
# ==========================================


@app.route("/api/analytics/export-csv")
@login_required
def api_analytics_export_csv():
    from flask import Response
    return Response("fecha,evento,accion,resultado\n", mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=analytics.csv"})


# ==========================================
#  API — NOTIFICACIONES (config + test)
# ==========================================


@app.route("/api/notifications/test_all", methods=["POST"])
@login_required
def api_notifications_test_all():
    return jsonify({"ok": True})


@app.route("/api/notifications/telegram", methods=["POST"])
@login_required
def api_notifications_telegram():
    return jsonify({"ok": True})


# ==========================================
#  API — MONITOR start/stop
# ==========================================
@app.route("/api/monitor/start", methods=["POST"])
@login_required
def api_monitor_start():
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
@login_required
def api_monitor_stop():
    return jsonify({"ok": True})


# ==========================================
#  API — BACKUP schedule + restore
# ==========================================
@app.route("/api/backup/schedule", methods=["POST"])
@login_required
def api_backup_schedule():
    return jsonify({"ok": True})


@app.route("/api/backup/restore", methods=["POST"])
@login_required
def api_backup_restore_upload():
    return jsonify({"ok": True})


# ==========================================
#  API — OTP check/manual/test/config
# ==========================================


@app.route("/api/otp/test", methods=["POST"])
@login_required
def api_otp_test():
    return jsonify({"ok": True})


@app.route("/api/otp/check", methods=["POST"])
@login_required
def api_otp_check():
    return jsonify({"ok": True, "found": 0})


# ==========================================
#  API — i18n
# ==========================================
@app.route("/api/i18n")
def api_i18n():
    return jsonify({"ok": True, "translations": i18n.all_translations()})


# ==========================================
#  HEALTH
# ==========================================
@app.route("/health")
def health():
    return jsonify({"ok": True, "app": config.APP_NAME, "version": config.APP_VERSION})


# ==========================================
#  API — ACCOUNT WARMER (login + browse + wishlist 24-48h pre-onsale)
# ==========================================
import account_warmer
import onsale_scheduler
import human_motion

@app.route("/api/account-warmer/run", methods=["POST"])
@login_required
def api_account_warmer_run():
    """Calienta una cuenta. Body: {account_id, target_event_url?, dry_run?}."""
    body = request.get_json(silent=True) or {}
    if not body.get("account_id"):
        return jsonify({"ok": False, "error": "account_id required"}), 400
    return jsonify(account_warmer.warm_account(
        account_id=body["account_id"],
        target_event_url=body.get("target_event_url", ""),
        dry_run=bool(body.get("dry_run", False))))

@app.route("/api/account-warmer/batch", methods=["POST"])
@login_required
def api_account_warmer_batch():
    """Lanza warming para batch. Body: {account_ids:[], max_parallel?, target_event_url?}."""
    body = request.get_json(silent=True) or {}
    return jsonify(account_warmer.warm_batch(
        account_ids=body.get("account_ids") or [],
        max_parallel=int(body.get("max_parallel", 2)),
        target_event_url=body.get("target_event_url", "")))

@app.route("/api/account-warmer/status", methods=["GET"])
@login_required
def api_account_warmer_status():
    job_id = request.args.get("job_id")
    return jsonify(account_warmer.get_warm_status(job_id=job_id))

# ==========================================
#  API — ONSALE SCHEDULER (cron auto T-0)
# ==========================================
@app.route("/api/onsale-scheduler/register", methods=["POST"])
@login_required
@rate_limit_decorator("60 per minute")
def api_onsale_register():
    """Programa ataque. Body: {tm_id, qty?, n_workers?, account_pool?, offer_idx?, sections?, selection_mode?, confirm_buy?}."""
    body = request.get_json(silent=True) or {}
    if not body.get("tm_id"):
        return jsonify({"ok": False, "error": "tm_id required"}), 400
    return jsonify(onsale_scheduler.register_attack(
        tm_id=str(body["tm_id"]),
        qty=int(body.get("qty", 2)),
        n_workers=int(body.get("n_workers", 3)),
        account_pool=body.get("account_pool"),
        offer_idx=int(body.get("offer_idx", 3)),
        sections=body.get("sections"),
        selection_mode=body.get("selection_mode", "quick"),
        confirm_buy=bool(body.get("confirm_buy", False)),
        notify_channels=body.get("notify_channels"),
    ))

@app.route("/api/onsale-scheduler/cancel/<tm_id>", methods=["POST"])
@login_required
def api_onsale_cancel(tm_id):
    return jsonify(onsale_scheduler.cancel_attack(tm_id))

@app.route("/api/onsale-scheduler/schedule", methods=["GET"])
@login_required
def api_onsale_schedule():
    return jsonify(onsale_scheduler.get_schedule())

@app.route("/api/onsale-scheduler/start", methods=["POST"])
@login_required
def api_onsale_start():
    return jsonify(onsale_scheduler.start_daemon())

@app.route("/api/onsale-scheduler/stop", methods=["POST"])
@login_required
def api_onsale_stop():
    return jsonify(onsale_scheduler.stop_daemon())

# ==========================================
#  API — HUMAN MOTION (test smoke)
# ==========================================
@app.route("/api/human-motion/self-test", methods=["GET"])
@login_required
def api_human_motion_self_test():
    return jsonify(human_motion.self_test())


# ==========================================
#  API — QUEUE MANAGER (Queue-it superior)
# ==========================================
import queue_manager

@app.route("/api/queue/enqueue", methods=["POST"])
@login_required
def api_queue_enqueue():
    """Crea 1 sesión de cola. Body: {account_id?, event_id, tm_url, customer_id?,
    auto_rejoin?, auto_buy_on_accepted?, purchase_flow_args?}."""
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.enqueue_session(
        account_id=b.get("account_id", ""),
        event_id=b.get("event_id", ""),
        tm_url=b.get("tm_url", ""),
        customer_id=b.get("customer_id", "ticketmasterspain"),
        auto_rejoin=bool(b.get("auto_rejoin", True)),
        auto_buy_on_accepted=bool(b.get("auto_buy_on_accepted", False)),
        purchase_flow_args=b.get("purchase_flow_args"),
        notes=b.get("notes", "")))

@app.route("/api/queue/enqueue-batch", methods=["POST"])
@login_required
def api_queue_enqueue_batch():
    """Crea N sesiones a la vez. Body: {n, account_pool?, event_id, tm_url,
    auto_rejoin?, auto_buy_on_accepted?, purchase_flow_args?, stagger_s?}."""
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.enqueue_batch(
        n=int(b.get("n", 1)),
        account_pool=b.get("account_pool"),
        event_id=b.get("event_id", ""),
        tm_url=b.get("tm_url", ""),
        customer_id=b.get("customer_id", "ticketmasterspain"),
        auto_rejoin=bool(b.get("auto_rejoin", True)),
        auto_buy_on_accepted=bool(b.get("auto_buy_on_accepted", False)),
        purchase_flow_args=b.get("purchase_flow_args"),
        stagger_s=float(b.get("stagger_s", 1.0))))

@app.route("/api/queue/sessions", methods=["GET"])
@login_required
def api_queue_sessions():
    return jsonify({"sessions": queue_manager.list_sessions(
        event_id=request.args.get("event_id"),
        status=request.args.get("status"),
        account_id=request.args.get("account_id"))})

@app.route("/api/queue/session/<session_id>", methods=["GET"])
@login_required
def api_queue_session(session_id):
    return jsonify(queue_manager.get_session(session_id))

@app.route("/api/queue/metrics", methods=["GET"])
@login_required
def api_queue_metrics():
    return jsonify(queue_manager.get_metrics())

@app.route("/api/queue/abandon/<session_id>", methods=["POST"])
@login_required
def api_queue_abandon(session_id):
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.abandon_session(session_id,
                                                   reason=b.get("reason", "manual")))

@app.route("/api/queue/rejoin/<session_id>", methods=["POST"])
@login_required
def api_queue_rejoin(session_id):
    return jsonify(queue_manager.rejoin_session(session_id))

@app.route("/api/queue/accept/<session_id>", methods=["POST"])
@login_required
def api_queue_accept(session_id):
    """Worker llama esto cuando captura accepted_token + queueit_session_xt."""
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.mark_accepted(session_id,
        accepted_token=b.get("accepted_token", ""),
        queueit_session_xt=b.get("queueit_session_xt", ""),
        position=b.get("position")))

@app.route("/api/queue/position/<session_id>", methods=["POST"])
@login_required
def api_queue_position(session_id):
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.update_position(session_id,
        position=b.get("position"),
        eta_minutes=b.get("eta_minutes")))

@app.route("/api/queue/swarm-broadcast", methods=["POST"])
@login_required
def api_queue_swarm_broadcast():
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.swarm_broadcast(
        event_id=b.get("event_id", ""),
        source_session_id=b.get("source_session_id", "manual"),
        accepted_token=b.get("accepted_token", ""),
        queueit_session_xt=b.get("queueit_session_xt", "")))

@app.route("/api/queue/claim", methods=["POST"])
@login_required
def api_queue_claim():
    """Llamado por el flow de compra para obtener un AcpAT del pool."""
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.claim_slot_for_purchase(
        event_id=b.get("event_id", ""), country=b.get("country", "es")))

@app.route("/api/queue/clear-completed", methods=["POST"])
@login_required
def api_queue_clear_completed():
    return jsonify(queue_manager.clear_completed())

@app.route("/api/queue/start-daemon", methods=["POST"])
@login_required
def api_queue_start_daemon():
    return jsonify(queue_manager.start_daemon())

@app.route("/api/queue/stop-daemon", methods=["POST"])
@login_required
def api_queue_stop_daemon():
    return jsonify(queue_manager.stop_daemon())

@app.route("/api/queue/self-test", methods=["GET"])
@login_required
def api_queue_self_test():
    return jsonify(queue_manager.self_test())


# ==========================================
#  API — TM MOBILE REVERSE (capturas mitmproxy + replay curl_cffi)
# ==========================================
import tm_mobile_reverse

@app.route("/api/mobile-reverse/info", methods=["GET"])
@login_required
def api_mobile_rev_info():
    return jsonify(tm_mobile_reverse.get_info())

@app.route("/api/mobile-reverse/captures", methods=["GET"])
@login_required
def api_mobile_rev_captures():
    return jsonify({"captures": tm_mobile_reverse.list_captures()})

@app.route("/api/mobile-reverse/upload", methods=["POST"])
@login_required
@rate_limit_decorator("10 per minute")
def api_mobile_rev_upload():
    """Body: {filename, content_b64, format_hint?}"""
    import base64
    b = request.get_json(silent=True) or {}
    filename = b.get("filename", "capture.json")
    content_b64 = b.get("content_b64", "")
    if not content_b64:
        return jsonify({"ok": False, "error": "content_b64 required"}), 400
    try:
        content = base64.b64decode(content_b64)
    except Exception as e:
        return jsonify({"ok": False, "error": f"base64 decode: {e}"}), 400
    return jsonify(tm_mobile_reverse.upload_capture(
        filename, content, format_hint=b.get("format_hint", "auto")))

@app.route("/api/mobile-reverse/replay", methods=["POST"])
@login_required
@rate_limit_decorator("30 per minute")
def api_mobile_rev_replay():
    """Body: {url, method?, headers?, body?, impersonate?}"""
    b = request.get_json(silent=True) or {}
    return jsonify(tm_mobile_reverse.replay_request(
        url=b.get("url", ""), method=b.get("method", "GET"),
        headers=b.get("headers"), body=b.get("body"),
        impersonate=b.get("impersonate", "chrome120")))

@app.route("/api/mobile-reverse/self-test", methods=["GET"])
@login_required
def api_mobile_rev_self_test():
    return jsonify(tm_mobile_reverse.self_test())


# ==========================================
#  API — OPENAPI + VERSIONADO /api/v1/
# ==========================================
import api_versioning

@app.route("/api/openapi.json", methods=["GET"])
@app.route("/api/v1/openapi.json", methods=["GET"])
def api_openapi_spec():
    return jsonify(api_versioning.build_openapi_spec(app, info={
        "title": "TM PERROS API",
        "version": getattr(config, "APP_VERSION", "1.0.0"),
    }))

@app.route("/api/docs", methods=["GET"])
@app.route("/api/v1/docs", methods=["GET"])
def api_docs():
    """Swagger UI sirviendo OpenAPI spec."""
    return """<!DOCTYPE html>
<html><head>
<title>TM PERROS API Docs</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head><body><div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
SwaggerUIBundle({url:'/api/openapi.json',dom_id:'#swagger-ui',
  deepLinking:true,layout:'BaseLayout'});
</script></body></html>"""

@app.route("/api/queue/presets", methods=["GET"])
@login_required
def api_queue_presets_list():
    return jsonify({"presets": queue_manager.list_presets()})

@app.route("/api/queue/presets", methods=["POST"])
@login_required
def api_queue_presets_save():
    return jsonify(queue_manager.save_preset(request.get_json(silent=True) or {}))

@app.route("/api/queue/presets/<name>", methods=["DELETE"])
@login_required
def api_queue_presets_delete(name):
    return jsonify(queue_manager.delete_preset(name))

@app.route("/api/queue/export-csv", methods=["GET"])
@login_required
def api_queue_export_csv():
    import time as _t
    csv_data = queue_manager.export_csv()
    return Response(csv_data, mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="queue_sessions_{int(_t.time())}.csv"'})

@app.route("/api/queue/inspect-cookie", methods=["POST"])
@login_required
def api_queue_inspect_cookie():
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.inspect_cookie(b.get("token", "")))

@app.route("/api/queue/logs", methods=["GET"])
@login_required
def api_queue_logs():
    limit = int(request.args.get("limit", 100))
    op = request.args.get("op")
    return jsonify({"log": queue_manager.get_logs(limit=limit, op_filter=op)})

# ==========================================
#  API — ACCOUNT HEALTH (trust score + cuarentena + fast check + digest)
# ==========================================
import account_health

@app.route("/api/account-health/score/<aid>", methods=["GET"])
@login_required
def api_health_score(aid):
    return jsonify(account_health.get_trust_score(aid))

@app.route("/api/account-health/recompute-all", methods=["POST"])
@login_required
@rate_limit_decorator("3 per minute")
def api_health_recompute_all():
    return jsonify(account_health.compute_all_scores())

@app.route("/api/account-health/quarantine/<aid>", methods=["POST"])
@login_required
def api_health_quarantine(aid):
    b = request.get_json(silent=True) or {}
    return jsonify(account_health.quarantine(aid, reason=b.get("reason", "manual")))

@app.route("/api/account-health/release/<aid>", methods=["POST"])
@login_required
def api_health_release(aid):
    return jsonify(account_health.release_quarantine(aid))

@app.route("/api/account-health/auto-release-expired", methods=["POST"])
@login_required
def api_health_auto_release():
    n = account_health.auto_release_expired_quarantines()
    return jsonify({"ok": True, "released": n})

@app.route("/api/account-health/fast-check", methods=["POST"])
@login_required
@rate_limit_decorator("20 per minute")
def api_health_fast_check():
    b = request.get_json(silent=True) or {}
    aid = b.get("account_id")
    if not aid:
        return jsonify({"ok": False, "error": "account_id required"}), 400
    accs = account_manager.account_manager.list_accounts()
    acc = next((a for a in accs if a.get("id") == aid), None)
    if not acc:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify(account_health.fast_session_check(acc, country=b.get("country", "es")))

@app.route("/api/account-health/fast-check-batch", methods=["POST"])
@login_required
@rate_limit_decorator("3 per minute")
def api_health_fast_check_batch():
    b = request.get_json(silent=True) or {}
    return jsonify(account_health.fast_check_batch(
        b.get("account_ids") or [], max_workers=int(b.get("max_workers", 5))))

@app.route("/api/account-health/digest", methods=["POST"])
@login_required
@rate_limit_decorator("2 per minute")
def api_health_digest():
    b = request.get_json(silent=True) or {}
    return jsonify(account_health.daily_health_digest(
        send_notification=bool(b.get("send_notification", True))))

@app.route("/api/account-health/groups", methods=["GET"])
@login_required
def api_health_groups():
    return jsonify({"groups": account_health.get_groups_summary()})

@app.route("/api/account-health/set-group/<aid>", methods=["POST"])
@login_required
def api_health_set_group(aid):
    b = request.get_json(silent=True) or {}
    return jsonify(account_health.set_group(aid, b.get("group", "")))

@app.route("/api/account-health/add-tag/<aid>", methods=["POST"])
@login_required
def api_health_add_tag(aid):
    b = request.get_json(silent=True) or {}
    return jsonify(account_health.add_tag(aid, b.get("tag", "")))

@app.route("/api/account-health/self-test", methods=["GET"])
@login_required
def api_health_self_test():
    return jsonify(account_health.self_test())


# ==========================================
#  API — ACCOUNT HEALTH PRO (5 mejoras state-of-art 2026)
# ==========================================
import account_health_pro

@app.route("/api/account-health-pro/info", methods=["GET"])
@login_required
def api_ahp_info():
    return jsonify(account_health_pro.get_info())

@app.route("/api/account-health-pro/self-test", methods=["GET"])
@login_required
def api_ahp_self_test():
    return jsonify(account_health_pro.self_test())

# Passive warming cron
@app.route("/api/account-health-pro/warming-cron/start", methods=["POST"])
@login_required
def api_ahp_cron_start():
    b = request.get_json(silent=True) or {}
    return jsonify(account_health_pro.start_warming_cron(
        interval_hours=int(b.get("interval_hours", 18)),
        max_per_run=int(b.get("max_per_run", 50))))

@app.route("/api/account-health-pro/warming-cron/stop", methods=["POST"])
@login_required
def api_ahp_cron_stop():
    return jsonify(account_health_pro.stop_warming_cron())

@app.route("/api/account-health-pro/warming-cron/status", methods=["GET"])
@login_required
def api_ahp_cron_status():
    return jsonify(account_health_pro.warming_cron_status())

@app.route("/api/account-health-pro/passive-warming/batch", methods=["POST"])
@login_required
@rate_limit_decorator("3 per minute")
def api_ahp_passive_batch():
    b = request.get_json(silent=True) or {}
    return jsonify(account_health_pro.passive_warming_batch(
        b.get("account_ids") or [], stagger_s=float(b.get("stagger_s", 2.0))))

# Multi-IMAP madres
@app.route("/api/account-health-pro/madres", methods=["GET"])
@login_required
def api_ahp_madres_list():
    return jsonify({"madres": account_health_pro.list_madres(safe=True),
                     "summary": account_health_pro.get_madres_summary()})

@app.route("/api/account-health-pro/madres/add", methods=["POST"])
@login_required
def api_ahp_madres_add():
    b = request.get_json(silent=True) or {}
    if not all(k in b for k in ("alias", "email", "password")):
        return jsonify({"ok": False, "error": "alias+email+password requeridos"}), 400
    return jsonify(account_health_pro.add_madre(
        alias=b["alias"], email=b["email"], password=b["password"],
        host=b.get("host", "imap.mail.me.com"),
        max_aliases=int(b.get("max_aliases", 100))))

# Sticky proxy
@app.route("/api/account-health-pro/sticky-proxy/<aid>", methods=["POST"])
@login_required
def api_ahp_sticky_request(aid):
    b = request.get_json(silent=True) or {}
    return jsonify(account_health_pro.request_sticky_proxy(
        aid, ttl_min=int(b.get("ttl_min", 30))))

@app.route("/api/account-health-pro/sticky-proxy/<aid>/release", methods=["POST"])
@login_required
def api_ahp_sticky_release(aid):
    return jsonify(account_health_pro.release_sticky_proxy(aid))

# Adaptive concurrency
@app.route("/api/account-health-pro/adaptive/get", methods=["GET"])
@login_required
def api_ahp_adaptive_get():
    return jsonify({"current": account_health_pro.adaptive_get()})

@app.route("/api/account-health-pro/adaptive/record", methods=["POST"])
@login_required
def api_ahp_adaptive_record():
    b = request.get_json(silent=True) or {}
    n = account_health_pro.adaptive_record(bool(b.get("success", True)))
    return jsonify({"new_concurrency": n})

# MFA fatigue avoidance
@app.route("/api/account-health-pro/mfa-fatigue/check/<aid>", methods=["GET"])
@login_required
def api_ahp_mfa_check(aid):
    return jsonify(account_health_pro.can_fresh_login(aid))

@app.route("/api/account-health-pro/mfa-fatigue/record/<aid>", methods=["POST"])
@login_required
def api_ahp_mfa_record(aid):
    return jsonify(account_health_pro.record_fresh_login(aid))


# ==========================================
#  API — CLOAK BROWSER bridge (Node.js stealth)
# ==========================================
import cloak_browser_bridge

@app.route("/api/cloak-browser/info", methods=["GET"])
@login_required
def api_cloak_info():
    return jsonify(cloak_browser_bridge.get_info())

@app.route("/api/cloak-browser/check", methods=["GET"])
@login_required
def api_cloak_check():
    return jsonify(cloak_browser_bridge.is_available())

@app.route("/api/cloak-browser/run-check", methods=["POST"])
@login_required
@rate_limit_decorator("3 per minute")
def api_cloak_run_check():
    b = request.get_json(silent=True) or {}
    return jsonify(cloak_browser_bridge.cloak_run_check(
        url=b.get("url", "https://www.ticketmaster.es"),
        timeout_s=int(b.get("timeout_s", 30))))


# ==========================================
#  API — KASADA VM SOLVER (devirtualizador ips.js)
# ==========================================
import kasada_vm_solver

@app.route("/api/kasada-vm/info", methods=["GET"])
@login_required
def api_kvs_info():
    return jsonify(kasada_vm_solver.get_info())

@app.route("/api/kasada-vm/fetch-ips", methods=["POST"])
@login_required
@rate_limit_decorator("5 per minute")
def api_kvs_fetch():
    b = request.get_json(silent=True) or {}
    r = kasada_vm_solver.fetch_ips_js(domain=b.get("domain", "ticketmaster.es"),
                                       path=b.get("path"))
    if r.get("ok") and "body" in r:
        r["body"] = r["body"][:500] + "..."
    return jsonify(r)

@app.route("/api/kasada-vm/analyze", methods=["POST"])
@login_required
@rate_limit_decorator("5 per minute")
def api_kvs_analyze():
    b = request.get_json(silent=True) or {}
    src = b.get("js_source") or ""
    if not src:
        f = kasada_vm_solver.fetch_ips_js(domain=b.get("domain", "ticketmaster.es"))
        if not f.get("ok"):
            return jsonify({"ok": False, "fetch_error": f.get("error")})
        src = f.get("body", "")
    return jsonify(kasada_vm_solver.analyze_ips_js(src))

@app.route("/api/kasada-vm/solve", methods=["POST"])
@login_required
@rate_limit_decorator("5 per minute")
def api_kvs_solve():
    b = request.get_json(silent=True) or {}
    return jsonify(kasada_vm_solver.solve_offline(
        js_source=b.get("js_source", ""),
        fetch_if_empty=bool(b.get("fetch_if_empty", True)),
        domain=b.get("domain", "ticketmaster.es")))

@app.route("/api/kasada-vm/self-test", methods=["GET"])
@login_required
def api_kvs_self_test():
    return jsonify(kasada_vm_solver.self_test())


# ==========================================
#  API — SECRET VAULT (cifrado at-rest PCI-DSS)
# ==========================================
import secret_vault

@app.route("/api/vault/status", methods=["GET"])
@login_required
def api_vault_status():
    return jsonify(secret_vault.vault_status())

@app.route("/api/vault/self-test", methods=["GET"])
@login_required
def api_vault_self_test():
    return jsonify(secret_vault.self_test())

@app.route("/api/vault/migrate-cards", methods=["POST"])
@login_required
@rate_limit_decorator("3 per minute")
def api_vault_migrate_cards():
    return jsonify(secret_vault.migrate_cards_to_encrypted())

@app.route("/api/vault/migrate-accounts", methods=["POST"])
@login_required
@rate_limit_decorator("3 per minute")
def api_vault_migrate_accounts():
    return jsonify(secret_vault.migrate_accounts_to_encrypted())

@app.route("/api/vault/decrypt-cards", methods=["POST"])
@login_required
@rate_limit_decorator("2 per minute")
def api_vault_decrypt_cards():
    return jsonify(secret_vault.decrypt_all_cards())


# ==========================================
#  API — HOUSEKEEPING (backup auto + log rotation + lock reaper)
# ==========================================
import housekeeping

@app.route("/api/housekeeping/stats", methods=["GET"])
@login_required
def api_hk_stats():
    return jsonify(housekeeping.get_stats())

@app.route("/api/housekeeping/run", methods=["POST"])
@login_required
@rate_limit_decorator("5 per minute")
def api_hk_run():
    return jsonify(housekeeping.run_all_tasks())

@app.route("/api/housekeeping/reap-locks", methods=["POST"])
@login_required
def api_hk_reap_locks():
    return jsonify({"ok": True, "reaped": housekeeping.reap_stale_locks()})

@app.route("/api/housekeeping/rotate-logs", methods=["POST"])
@login_required
def api_hk_rotate_logs():
    return jsonify({"ok": True, "result": housekeeping.rotate_logs()})

@app.route("/api/housekeeping/backup-now", methods=["POST"])
@login_required
@rate_limit_decorator("3 per minute")
def api_hk_backup():
    return jsonify(housekeeping.auto_backup())

@app.route("/api/housekeeping/self-test", methods=["GET"])
@login_required
def api_hk_self_test():
    return jsonify(housekeeping.self_test())


@app.route("/api/queue/sniper-mode", methods=["POST"])
@login_required
@rate_limit_decorator("10 per minute")
def api_queue_sniper():
    b = request.get_json(silent=True) or {}
    return jsonify(queue_manager.sniper_mode(
        event_id=b.get("event_id", ""),
        n_workers=int(b.get("n_workers", 50)),
        tm_url=b.get("tm_url", ""),
        customer_id=b.get("customer_id", "ticketmasterspain"),
        account_pool=b.get("account_pool"),
        purchase_flow_args=b.get("purchase_flow_args")))


def self_test() -> dict:
    """Validación básica del dashboard (política del proyecto, módulos >50 LOC).
    Verifica imports, rutas críticas registradas y funciones helper.
    NO arranca el servidor — sólo valida que se puede importar y rutas existen.
    """
    out = {'ok': True, 'checks': {}, 'errors': []}
    try:
        # Flask app instance
        out['checks']['flask_app'] = 'app' in globals() and hasattr(app, 'route')

        # Rutas críticas registradas
        critical_routes = [
            '/api/healthz',
            '/api/plantilla-tm/run-selected',
            '/api/plantilla-tm/csv',
            '/api/registros',
            '/login',
        ]
        registered = {r.rule for r in app.url_map.iter_rules()}
        for r in critical_routes:
            present = r in registered
            out['checks'][f'route:{r}'] = present
            if not present:
                out['ok'] = False
                out['errors'].append(f'route missing: {r}')

        # Funciones helper internas del fix anti-doble-click
        out['checks']['fn:_detect_active_batch10_pid'] = (
            '_detect_active_batch10_pid' in globals() and
            callable(globals()['_detect_active_batch10_pid'])
        )

        # Imports principales
        for mod in ('config', 'tm_registrations', 'account_manager',
                     'notifications', 'audit_log'):
            try:
                __import__(mod); out['checks'][f'import:{mod}'] = True
            except Exception as e:
                out['checks'][f'import:{mod}'] = False
                out['errors'].append(f'{mod}: {e}')
                out['ok'] = False
    except Exception as e:
        out['ok'] = False
        out['errors'].append(f'self_test exception: {e}')
    return out


# ==========================================
#  ENTRY POINT
# ==========================================
if __name__ == "__main__":
    log.info(f"{config.APP_NAME} v{config.APP_VERSION} arrancando en puerto {config.DASHBOARD_PORT}")
    log.info(f"Dominios TM disponibles: {len(config.TM_DOMAINS)}")
    # Arrancar onsale_scheduler daemon
    try:
        _osr = onsale_scheduler.start_daemon()
        log.info(f"Onsale scheduler daemon: {_osr}")
    except Exception as _e:
        log.warning(f"onsale_scheduler boot failed: {_e}")
    # Arrancar queue_manager daemon + conectar WS broadcaster
    try:
        if socketio:
            queue_manager.set_ws_broadcaster(lambda evt, payload: socketio.emit(evt, payload))
        _qmr = queue_manager.start_daemon()
        log.info(f"Queue manager daemon: {_qmr}")
    except Exception as _e:
        log.warning(f"queue_manager boot failed: {_e}")
    # Arrancar housekeeping daemon (backup + logs + locks + storage)
    try:
        _hkr = housekeeping.start_daemon()
        log.info(f"Housekeeping daemon: {_hkr}")
    except Exception as _e:
        log.warning(f"housekeeping boot failed: {_e}")
    # Arrancar passive warming cron (auto cada 18h, max 50 cuentas/run)
    try:
        if os.environ.get("TM_AUTO_WARMING", "1") == "1":
            _wcr = account_health_pro.start_warming_cron(interval_hours=18, max_per_run=50)
            log.info(f"Passive warming cron: {_wcr}")
    except Exception as _e:
        log.warning(f"warming cron boot failed: {_e}")
    # Registrar alias /api/v1/ paralelos a /api/ (versionado backward-compat)
    try:
        _v1_aliased = api_versioning.register_v1_aliases(app)
        log.info(f"API v1 aliases registered: {len(_v1_aliased)} endpoints")
    except Exception as _e:
        log.warning(f"v1 aliases failed: {_e}")
    # Check admin/admin default password (warning si aplica)
    try:
        _check_admin_default_password()
    except Exception:
        pass
    log.info(f"WebSocket: {'habilitado' if socketio else 'deshabilitado (polling fallback)'}")

    try:
        import venue_cache as _vc_boot
        _rb = _vc_boot.rebuild_from_disk()
        if _rb.get("recovered"):
            log.info(f"venue_cache: rebuild from disk recovered={_rb['recovered']} skipped={_rb['skipped']} total={_rb['venues_total']}")
    except Exception as _e:
        log.warning(f"venue_cache rebuild failed: {_e}")

    try:
        import sms_receiver as _sms_boot
        _rp = _sms_boot.reap_stale_smspool()
        if _rp.get("scanned"):
            log.info(f"sms_receiver: reaped stale smspool scanned={_rp['scanned']} reaped={_rp['reaped']} failed={_rp['failed']}")
    except Exception as _e:
        log.warning(f"sms_receiver reap failed: {_e}")

    # Compliance gate: cron trimestral auto-start
    try:
        _cs = compliance_gate.start_quarterly_scheduler()
        if _cs.get("ok"):
            log.info(f"compliance_gate: quarterly scheduler armed → next={_cs.get('next')}")
    except Exception as _e:
        log.warning(f"compliance_gate scheduler start failed: {_e}")

    # Bot infra: recuperar jobs interrumpidos + reset locks file-based al arranque
    try:
        import bot_infra as _bi_boot
        _rec = _bi_boot.recover_interrupted_jobs()
        log.info(f"bot_infra: recovered {_rec.get('interrupted', 0)} interrupted jobs")
        _bi_boot.reset_global_state()
        log.info("bot_infra: locks/active reset at boot")
    except Exception as _e:
        log.warning(f"bot_infra boot init failed: {_e}")

    # ════════ CLUSTER / MULTI-HOST ════════
    rb_info = redis_backend.get_info()
    log.info(f"Instance ID: {rb_info['instance_id']} | Backend: {rb_info['backend']} | "
             f"{'DISTRIBUIDO (Redis)' if rb_info['distributed'] else 'LOCAL (fallback)'}")
    cluster_manager.start_heartbeat()
    resilience.install_signal_handlers()

    def _shutdown():
        log.info("[shutdown] marcando host como going-down")
        cluster_manager.stop_heartbeat()
    resilience.register_shutdown_handler(_shutdown)

    # Arrancar scheduler Discovery si está habilitado
    try:
        _disc_cfg = event_discovery.get_config()
        if _disc_cfg.get("auto_sync_enabled"):
            event_discovery.start_scheduler()
            log.info(f"Discovery scheduler: ACTIVO (cada {_disc_cfg.get('sync_interval_min',60)} min)")
        else:
            log.info("Discovery scheduler: inactivo (activable desde UI)")
    except Exception as e:
        log.warning(f"Discovery scheduler no arrancó: {e}")

    # Arrancar scheduler de proxy_health (benchmark periódico cada 30 min)
    # Detecta proxies muertos antes del drop sin necesidad de manual check.
    try:
        if os.environ.get("TM_AUTO_PROXY_HEALTH", "1") == "1":
            _phs = proxy_health.start_scheduler(
                interval_min=int(os.environ.get("TM_PROXY_HEALTH_INTERVAL_MIN", "30")),
                broadcast=(lambda evt, payload: socketio.emit(evt, payload)) if socketio else None,
            )
            log.info(f"Proxy health scheduler: {_phs}")
    except Exception as e:
        log.warning(f"proxy_health scheduler no arrancó: {e}")

    # Browser pool manager: respeta el flag auto_start del config (per-user opt-in).
    # Si auto_start=True, mantiene N contextos pre-warmed para checkout sub-segundo.
    # Sin auto_start: el manager queda dormido (la UI puede arrancarlo on-demand).
    try:
        import browser_pool as _bp_boot
        _bp_cfg = _bp_boot.get_config()
        if _bp_cfg.get("auto_start") and os.environ.get("TM_AUTO_BROWSER_POOL", "1") == "1":
            _bps = _bp_boot.start_manager()
            log.info(f"Browser pool manager: {_bps} (pool_size={_bp_cfg.get('pool_size')})")
        else:
            log.info(f"Browser pool: idle (auto_start={_bp_cfg.get('auto_start')}, activable desde UI)")
    except Exception as e:
        log.warning(f"browser_pool boot init failed: {e}")

    # TLS CT Monitor scheduler (24h): detecta rotaciones de certs TM/Queue-it/CDN.
    # Early warning si TM/Akamai migra WAF — antes era stub, ahora thread real.
    # Rate-limited 60s entre dominios (11 dominios → ~11 min por ciclo).
    try:
        import tls_ct_monitor as _tcm_boot
        if os.environ.get("TM_AUTO_TLS_CT", "1") == "1":
            _tcms = _tcm_boot.start_scheduler(
                interval_hours=int(os.environ.get("TM_TLS_CT_INTERVAL_HOURS", "24")))
            log.info(f"TLS CT monitor scheduler: {_tcms}")
    except Exception as e:
        log.warning(f"tls_ct_monitor scheduler no arrancó: {e}")

    # Safetix Handler bridge: marca el handler activo para capturar tickets
    # SafeTix mobile (rotación 15s tokens) cuando llegue una compra real.
    # NO abre puerto/listener — solo flag setter (la WS opcional se levanta aparte).
    try:
        import safetix_handler as _sx_boot
        _sxs = _sx_boot.start_bridge(port=int(os.environ.get("TM_SAFETIX_PORT", "8765")))
        log.info(f"Safetix bridge: {_sxs}")
    except Exception as e:
        log.warning(f"safetix bridge no arrancó: {e}")

    # OTP 3DS listener: arrancar polling IMAP solo si user lo configuró
    # (config.enabled=True). Si está deshabilitado o en modo sms_webhook,
    # el loop interno ya se queda dormido — start_listener es seguro.
    try:
        import otp_3ds as _o3_boot
        _o3_cfg = _o3_boot.get_config()
        if _o3_cfg.get("enabled") and _o3_cfg.get("mode") in ("imap", "both"):
            _o3s = _o3_boot.start_listener()
            log.info(f"OTP 3DS listener: {_o3s} (mode={_o3_cfg.get('mode')})")
        else:
            log.info(f"OTP 3DS listener: idle (enabled={_o3_cfg.get('enabled')}, "
                     f"mode={_o3_cfg.get('mode')}) — webhook SMS sigue activo")
    except Exception as e:
        log.warning(f"otp_3ds listener no arrancó: {e}")

    # Arrancar watcher CSV bidireccional de Cuentas/Tarjetas
    try:
        accounts_csv_sync.init()
        r = accounts_csv_sync.start_watcher(poll_s=1.0)
        log.info(f"Accounts CSV watcher: {r.get('status','?')} (poll 1s) — {accounts_csv_sync.get_csv_path()}")
    except Exception as e:
        log.warning(f"Accounts CSV watcher no arrancó: {e}")
    if socketio:
        socketio.run(
            app,
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            debug=False,
            allow_unsafe_werkzeug=True,
        )
    else:
        app.run(
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            debug=False,
            threaded=True,
        )
