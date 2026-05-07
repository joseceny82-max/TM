"""imap_sms_inject.py — IMAP email → SMS code → DOM auto-injection bridge.

Cuando TM envía OTP por **email** en vez de SMS (algunos flows: signup new account,
2FA login, password reset), el bot necesita:
  1. Polear IMAP (Gmail/Outlook/iCloud/etc) buscando email de TM
  2. Extraer el código (regex multi-locale)
  3. Inyectar el código en el campo OTP del DOM via CDP

Este módulo orquesta los 3 pasos via `otp_manager.py` (IMAP) +
`signup_form_helpers.py` (CDP injection).

Uso típico:
  start_inject_bridge(account_id="x@gmail.com", ws_url="ws://localhost:9222/...")
  → returns task_id
  El bridge corre en background, polea IMAP cada 5s, e inyecta cuando recibe code.

API:
  start_inject_bridge(account_id, ws_url, timeout_s=300, selector_hint=None) → task_id
  cancel_inject_bridge(task_id) → True/False
  get_bridge_status(task_id) → dict
  list_active_bridges() → list[task_id]
  fetch_imap_code_sync(account_id, max_wait_s=300) → {ok, code, source}
  parse_otp_from_email_body(body, sender) → str | None
  self_test()
"""
from __future__ import annotations
import os, time, json, re, threading, asyncio, uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import json_atomic

try:
    import otp_manager
    OTP_AVAILABLE = True
except Exception:
    OTP_AVAILABLE = False

try:
    import signup_form_helpers
    SFH_AVAILABLE = True
except Exception:
    SFH_AVAILABLE = False


_BRIDGES_KEY = "imap_inject_bridges.json"
_LOG_KEY = "imap_inject_log.json"
_lock = threading.RLock()
_active_bridges: Dict[str, Dict[str, Any]] = {}


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta("imap_inject_config.json"), default={
        "poll_interval_s": 5,
        "default_timeout_s": 300,
        "max_concurrent_bridges": 20,
        "tm_senders": [
            "noreply@ticketmaster.com",
            "noreply@ticketmaster.es",
            "no-reply@ticketmaster.com",
            "noreply@account.ticketmaster.com",
            "ticketmaster@e.ticketmaster.com",
        ],
        "log_max_entries": 200,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta("imap_inject_config.json"), cur)
    return cur


# ═══════════════════════════════════════════════════════════════
# OTP REGEX (multi-locale)
# ═══════════════════════════════════════════════════════════════
OTP_REGEXES = [
    # Patrón típico inglés: "Your verification code is 123456"
    re.compile(r"verification\s+code\s+is\s*[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    re.compile(r"your\s+code\s+is\s*[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    re.compile(r"security\s+code\s*[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    # Español
    re.compile(r"código\s+de\s+verificación\s+(?:es\s+)?[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    re.compile(r"código\s+es\s*[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    re.compile(r"tu\s+código\s+(?:es\s+)?[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    # Francés
    re.compile(r"code\s+de\s+vérification\s*[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    re.compile(r"votre\s+code\s*[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    # Alemán
    re.compile(r"bestätigungscode\s*[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    re.compile(r"ihr\s+code\s*[:\-]?\s*([0-9]{4,8})", re.IGNORECASE),
    # Genérico — número aislado de 6 dígitos en líneas cortas
    re.compile(r"^\s*([0-9]{6})\s*$", re.MULTILINE),
    # En subject típicamente
    re.compile(r"\b([0-9]{6})\b\s+is\s+your", re.IGNORECASE),
]


def parse_otp_from_email_body(body: str, sender: str = "",
                                subject: str = "") -> Optional[str]:
    """Extrae código OTP de un cuerpo de email."""
    if not body: return None
    # Buscar primero en subject (más fiable)
    text_to_search = (subject + "\n" + body) if subject else body
    text_to_search = text_to_search[:8000]  # limit búsqueda

    for regex in OTP_REGEXES:
        m = regex.search(text_to_search)
        if m:
            code = m.group(1)
            if len(code) >= 4 and len(code) <= 8 and code.isdigit():
                return code
    return None


# ═══════════════════════════════════════════════════════════════
# IMAP FETCH (sync wrapper)
# ═══════════════════════════════════════════════════════════════
def fetch_imap_code_sync(account_id: Optional[str] = None,
                          max_wait_s: int = 300,
                          poll_interval_s: int = 5) -> Dict[str, Any]:
    """Polea IMAP hasta encontrar OTP de TM. Bloquea hasta timeout o code."""
    if not OTP_AVAILABLE:
        return {"ok": False, "error": "otp_manager_not_available"}

    started = time.time()
    last_check = 0

    while (time.time() - started) < max_wait_s:
        if time.time() - last_check < poll_interval_s:
            time.sleep(0.5); continue
        last_check = time.time()
        try:
            r = otp_manager.fetch_otps(account=account_id) if hasattr(otp_manager, "fetch_otps") \
                else otp_manager.read_otp() if hasattr(otp_manager, "read_otp") else None
            if not r: continue
            codes = r.get("codes") if isinstance(r, dict) else None
            if codes:
                first = codes[0]
                code = first.get("code") if isinstance(first, dict) else first
                if code:
                    return {
                        "ok": True, "code": code, "source": "imap",
                        "account_id": account_id,
                        "elapsed_s": round(time.time() - started, 1),
                        "details": first if isinstance(first, dict) else None,
                    }
        except Exception as e:
            return {"ok": False, "error": f"imap_fetch_fail: {str(e)[:120]}"}

    return {"ok": False, "error": "timeout",
            "waited_s": int(time.time() - started)}


# ═══════════════════════════════════════════════════════════════
# BRIDGE LIFECYCLE
# ═══════════════════════════════════════════════════════════════
def start_inject_bridge(account_id: str, ws_url: str,
                          timeout_s: int = 300,
                          selector_hint: Optional[str] = None,
                          on_result=None) -> Dict[str, Any]:
    """Lanza un thread que polea IMAP e inyecta el código en el browser via CDP.

    account_id: identifica la cuenta (email) para filtrar IMAP.
    ws_url: WebSocket CDP del browser target (ws://localhost:9222/...).
    on_result: callback opcional con dict {ok, code, injected, ...}.

    Returns: {ok, task_id}
    """
    if not OTP_AVAILABLE:
        return {"ok": False, "error": "otp_manager_unavailable"}
    if not SFH_AVAILABLE:
        return {"ok": False, "error": "signup_form_helpers_unavailable"}

    cfg = get_config()
    if len(_active_bridges) >= cfg["max_concurrent_bridges"]:
        return {"ok": False, "error": "max_concurrent_bridges_reached"}

    task_id = "imap_inj_" + uuid.uuid4().hex[:12]

    bridge = {
        "task_id": task_id,
        "account_id": account_id,
        "ws_url": ws_url,
        "timeout_s": timeout_s,
        "selector_hint": selector_hint,
        "started_at": datetime.now().isoformat(),
        "started_ts": time.time(),
        "status": "polling",
        "code_received": None,
        "injected": False,
        "result": None,
        "error": None,
    }

    stop_event = threading.Event()
    bridge["_stop_event"] = stop_event

    def _run():
        try:
            # 1. Polear IMAP hasta recibir código
            poll_until = bridge["started_ts"] + timeout_s
            while time.time() < poll_until and not stop_event.is_set():
                r = fetch_imap_code_sync(account_id=account_id,
                                           max_wait_s=min(60, int(poll_until - time.time())))
                if r.get("ok"):
                    bridge["code_received"] = r["code"]
                    bridge["status"] = "code_received"
                    break
                if "timeout" not in (r.get("error") or ""):
                    bridge["error"] = r.get("error")
                    break
            if not bridge["code_received"]:
                bridge["status"] = "timeout"
                _log_bridge(bridge)
                if on_result:
                    try: on_result({"ok": False, "task_id": task_id,
                                       "error": bridge.get("error") or "timeout"})
                    except: pass
                return

            # 2. Inyectar via CDP
            try:
                async def _inject():
                    import websockets as ws_lib
                    async with ws_lib.connect(ws_url, max_size=10*1024*1024) as ws:
                        return await signup_form_helpers.inject_otp_via_cdp(
                            ws, bridge["code_received"], selector_hint=selector_hint)
                inj_result = asyncio.run(_inject())
                bridge["result"] = inj_result
                bridge["injected"] = bool(inj_result.get("ok"))
                bridge["status"] = "injected" if bridge["injected"] else "inject_failed"
            except Exception as e:
                bridge["error"] = f"inject_fail: {str(e)[:120]}"
                bridge["status"] = "inject_failed"

            _log_bridge(bridge)
            if on_result:
                try: on_result({
                    "ok": bridge["injected"], "task_id": task_id,
                    "code": bridge["code_received"],
                    "result": bridge["result"], "error": bridge.get("error"),
                })
                except: pass
        finally:
            # Cleanup auto después de 60s
            def _cleanup():
                time.sleep(60)
                with _lock:
                    _active_bridges.pop(task_id, None)
            threading.Thread(target=_cleanup, daemon=True).start()

    t = threading.Thread(target=_run, daemon=True, name=f"imap-inj-{task_id}")
    bridge["_thread"] = t
    with _lock:
        _active_bridges[task_id] = bridge
    t.start()

    return {"ok": True, "task_id": task_id, "status": "polling"}


def cancel_inject_bridge(task_id: str) -> Dict[str, Any]:
    with _lock:
        b = _active_bridges.get(task_id)
    if not b: return {"ok": False, "error": "task_not_found"}
    evt = b.get("_stop_event")
    if evt: evt.set()
    b["status"] = "cancelled"
    return {"ok": True, "task_id": task_id}


def get_bridge_status(task_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        b = _active_bridges.get(task_id)
    if not b: return None
    return {
        k: v for k, v in b.items()
        if not k.startswith("_")
    }


def list_active_bridges() -> List[Dict[str, Any]]:
    with _lock:
        return [
            {k: v for k, v in b.items() if not k.startswith("_")}
            for b in _active_bridges.values()
        ]


def _log_bridge(bridge: Dict[str, Any]):
    try:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
        d["log"].insert(0, {
            "task_id": bridge["task_id"],
            "account_id": bridge["account_id"],
            "started_at": bridge["started_at"],
            "ended_at": datetime.now().isoformat(),
            "status": bridge["status"],
            "code_received": bridge["code_received"],
            "injected": bridge["injected"],
            "error": bridge.get("error"),
            "duration_s": round(time.time() - bridge["started_ts"], 1),
        })
        d["log"] = d["log"][:get_config()["log_max_entries"]]
        json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


def get_log(limit: int = 30) -> List[Dict[str, Any]]:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
    return d.get("log", [])[:limit]


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_has_poll_interval"] = "poll_interval_s" in cfg
    checks["config_has_tm_senders"] = isinstance(cfg.get("tm_senders"), list)
    checks["config_max_bridges"] = cfg.get("max_concurrent_bridges", 0) > 0

    # 2. OTP regex EN
    code = parse_otp_from_email_body(
        "Hello, your verification code is 123456. It expires in 10min.")
    checks["regex_en_works"] = code == "123456"

    # 3. Regex ES
    code = parse_otp_from_email_body(
        "Hola, tu código de verificación es 654321.")
    checks["regex_es_works"] = code == "654321"

    # 4. Regex FR
    code = parse_otp_from_email_body(
        "Bonjour, votre code de vérification: 987654.")
    checks["regex_fr_works"] = code == "987654"

    # 5. Regex DE
    code = parse_otp_from_email_body(
        "Ihr Bestätigungscode: 246810")
    checks["regex_de_works"] = code == "246810"

    # 6. Regex generic 6-digit
    code = parse_otp_from_email_body("Some text\n135790\nMore text")
    checks["regex_generic_works"] = code == "135790"

    # 7. No code in body
    code = parse_otp_from_email_body("This is just a regular email")
    checks["no_code_returns_none"] = code is None

    # 8. Subject precedence
    code = parse_otp_from_email_body(body="just text",
                                       subject="111111 is your code")
    checks["subject_works"] = code == "111111"

    # 9. fetch_imap_code with no otp_manager → returns error
    if not OTP_AVAILABLE:
        r = fetch_imap_code_sync("x", max_wait_s=1)
        checks["fetch_handles_no_otp_mgr"] = r.get("ok") is False

    # 10. start_bridge fails without ws_url and modules?
    # Solo testeamos el bookkeeping (no abrir CDP real)
    if SFH_AVAILABLE and OTP_AVAILABLE:
        # Cancelar bridge inexistente
        r = cancel_inject_bridge("nonexistent_id")
        checks["cancel_nonexistent_returns_false"] = r.get("ok") is False

    # 11. List active (vacío)
    bridges = list_active_bridges()
    checks["list_returns_list"] = isinstance(bridges, list)

    # 12. Log empty initially
    log = get_log(limit=5)
    checks["log_returns_list"] = isinstance(log, list)

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "imap_sms_inject",
        "version": 1,
        "active_bridges": len(_active_bridges),
        "otp_manager_available": OTP_AVAILABLE,
        "signup_form_helpers_available": SFH_AVAILABLE,
        "regex_count": len(OTP_REGEXES),
        "purpose": "IMAP email → OTP code → DOM auto-injection bridge",
    }
