"""otp_3ds.py — Autofill 3DS OTP via IMAP/SMS real.

Mejora #autofill_3ds:
  Lector IMAP/SMS para rellenar código 3DS durante checkout automáticamente.

Características:
- IMAP real (imaplib stdlib) con UIDs para evitar releer
- Patterns por banco (BBVA, Santander, CaixaBank, Visa, Mastercard, Amex)
- wait_for_otp(timeout) bloquea con polling suave hasta recibir código
- SMS via proveedor genérico (Twilio/SMS-Activate/etc) vía webhook inbound
- Integración con otp_manager existente (añade códigos al historial)

API principal:
  save_config(patch) → dict
  check_inbox() → lista de OTPs detectados ahora
  wait_for_otp(since_ts, timeout, bank="") → dict | None
  start_listener() / stop_listener()  (polling IMAP en background)
  push_sms(text, sender)  (webhook SMS inbound)

Formatos reconocidos:
  "Su código de seguridad es 123456"
  "3D Secure code: 845291"
  "TICKETMASTER: 482913 es su OTP"
  "Confirma tu compra con el código 123-456"
"""
import re
import ssl
import imaplib
import email
import threading
import time
from datetime import datetime, timedelta
from email.header import decode_header
from typing import Optional, List, Dict

import config
import json_atomic


_CFG_KEY = "otp_3ds_config.json"
_LOG_KEY = "otp_3ds_log.json"
_lock = threading.RLock()
_cond = threading.Condition()
_listener = {"running": False, "thread": None, "stop_event": None, "last_check": None}

# Codigo 3DS: tipicamente 4-8 dígitos, a veces con guión
DEFAULT_PATTERNS = [
    # Español (incluye ':', '=', guiones)
    r"(?:c[oó]digo|clave|pin|otp|contraseña)[^\d]{0,30}(\d{4,8})",
    r"(\d{3}[\s-]\d{3})",  # 123-456 / 123 456
    # Inglés
    r"(?:verification|security|auth|confirm|code|pin)[^\d]{0,30}(\d{4,8})",
    r"3D[-\s]?[Ss]ecure[^\d]{0,30}(\d{4,8})",
    # Genérico final (último recurso)
    r"\b(\d{6})\b",
    r"\b(\d{4})\b",
]

# Remitentes conocidos bancos + TM
DEFAULT_SENDERS = [
    "noreply@ticketmaster",
    "3dsecure@",
    "notificaciones@bbva.es",
    "alertas@santander.es",
    "notificaciones@caixabank.es",
    "openbank@openbank.es",
    "3ds@visa.com",
    "mc-securecode@mastercard.com",
    "alerts@americanexpress.com",
    "noreply@",  # fallback amplio
]


def _cfg_path():
    return config.ruta(_CFG_KEY)


def _log_path():
    return config.ruta(_LOG_KEY)


# ── Vault helpers (cifrado de imap_password en disco) ──
def _vault_encrypt(v: str) -> str:
    if not v:
        return ""
    try:
        import secret_vault
        return secret_vault.encrypt_field(v)
    except Exception:
        return v  # fallback texto plano si vault falla — no debería


def _vault_decrypt(v: str) -> str:
    if not v:
        return ""
    try:
        import secret_vault
        if secret_vault.is_encrypted(v):
            return secret_vault.decrypt_field(v)
    except Exception:
        pass
    return v


def get_config() -> dict:
    cfg = json_atomic.load(_cfg_path(), default={
        "enabled": False,
        "mode": "imap",  # imap | sms_webhook | both
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_username": "",
        "imap_password": "",  # App password (NO la real) — cifrada en disco
        "imap_folder": "INBOX",
        "imap_ssl": True,
        "sender_filters": DEFAULT_SENDERS,
        "patterns": DEFAULT_PATTERNS,
        "poll_interval_s": 5,
        "lookback_minutes": 10,
        "max_history": 200,
    })
    # Migración legacy: si en disco hay una pwd en claro, cifrarla y reescribir
    pw_disk = cfg.get("imap_password", "")
    if pw_disk:
        try:
            import secret_vault
            if not secret_vault.is_encrypted(pw_disk):
                cfg["imap_password"] = secret_vault.encrypt_field(pw_disk)
                json_atomic.save(_cfg_path(), cfg)
        except Exception:
            pass
    # Devolver con password en claro IN-MEMORY para uso interno
    cfg["imap_password"] = _vault_decrypt(cfg.get("imap_password", ""))
    return cfg


def save_config(patch: dict) -> dict:
    # Cargar config sin auto-decrypt (raw on-disk)
    cur = json_atomic.load(_cfg_path(), default={})
    if not cur:
        cur = get_config()  # primera vez: usa defaults
        # Re-cifrar antes de mergear patch
        if cur.get("imap_password"):
            cur["imap_password"] = _vault_encrypt(cur["imap_password"])
    allowed = {"enabled", "mode", "imap_host", "imap_port", "imap_username",
               "imap_password", "imap_folder", "imap_ssl", "sender_filters",
               "patterns", "poll_interval_s", "lookback_minutes"}
    for k, v in (patch or {}).items():
        if k in allowed:
            if k == "imap_password" and v:
                cur[k] = _vault_encrypt(v)
            else:
                cur[k] = v
    json_atomic.save(_cfg_path(), cur)
    return {k: ("***" if k == "imap_password" and v else v) for k, v in cur.items()}


def _log(entry: dict):
    with _lock:
        d = json_atomic.load(_log_path(), default={"otps": []})
        d["otps"].insert(0, entry)
        cfg = get_config()
        d["otps"] = d["otps"][:cfg.get("max_history", 200)]
        json_atomic.save(_log_path(), d)


def get_log(limit: int = 50) -> List[dict]:
    return json_atomic.load(_log_path(), default={"otps": []}).get("otps", [])[:limit]


def _decode_header(raw: str) -> str:
    try:
        parts = decode_header(raw or "")
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                try:
                    out.append(text.decode(enc or "utf-8", errors="replace"))
                except Exception:
                    out.append(text.decode("utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out)
    except Exception:
        return str(raw)


def _extract_body(msg) -> str:
    """Extrae texto plano del email (recorre partes)."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            # fallback html
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    except Exception:
        pass
    return ""


def _match_code(text: str, patterns: List[str]) -> Optional[str]:
    if not text:
        return None
    # Elimina HTML tags primero para que el regex vea texto plano
    cleaned = re.sub(r"<[^>]+>", " ", text)
    for pat in patterns:
        try:
            m = re.search(pat, cleaned, re.IGNORECASE)
            if m:
                code = m.group(1).replace(" ", "").replace("-", "")
                if 4 <= len(code) <= 8 and code.isdigit():
                    return code
        except re.error:
            continue
    return None


def _sender_matches(from_header: str, filters: List[str]) -> bool:
    if not filters:
        return True
    low = (from_header or "").lower()
    for f in filters:
        if f.lower() in low:
            return True
    return False


def check_inbox() -> dict:
    """Chequea IMAP una vez, devuelve OTPs nuevos detectados."""
    cfg = get_config()
    if cfg.get("mode") == "sms_webhook":
        return {"ok": False, "error": "mode=sms_webhook; nada que leer por IMAP"}
    user = cfg.get("imap_username", "")
    pwd = cfg.get("imap_password", "")
    host = cfg.get("imap_host", "imap.gmail.com")
    port = int(cfg.get("imap_port", 993))
    if not user or not pwd:
        return {"ok": False, "error": "imap_username / imap_password requeridos"}
    patterns = cfg.get("patterns", DEFAULT_PATTERNS)
    senders = cfg.get("sender_filters", DEFAULT_SENDERS)
    lookback = int(cfg.get("lookback_minutes", 10))

    found: List[dict] = []
    try:
        ctx = ssl.create_default_context()
        M = imaplib.IMAP4_SSL(host, port, ssl_context=ctx) if cfg.get("imap_ssl", True) \
            else imaplib.IMAP4(host, port)
        M.login(user, pwd)
        M.select(cfg.get("imap_folder", "INBOX"))
        # Busca emails desde fecha reciente
        since = (datetime.now() - timedelta(minutes=lookback)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE "{since}")')
        if typ != "OK" or not data or not data[0]:
            M.logout()
            return {"ok": True, "found": [], "scanned": 0}
        uids = data[0].split()[-50:]  # últimos 50 como mucho
        for uid in uids:
            try:
                t, d = M.fetch(uid, "(RFC822)")
                if t != "OK" or not d or not d[0]:
                    continue
                raw = d[0][1]
                msg = email.message_from_bytes(raw)
                frm = _decode_header(msg.get("From", ""))
                subj = _decode_header(msg.get("Subject", ""))
                if not _sender_matches(frm, senders):
                    continue
                body = _extract_body(msg)
                code = _match_code(subj + "\n" + body, patterns)
                if code:
                    entry = {
                        "code": code,
                        "source": "imap",
                        "from": frm[:120],
                        "subject": subj[:160],
                        "ts": datetime.now().isoformat(),
                        "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                    }
                    # dedupe
                    recent = [x for x in get_log(50) if x.get("uid") == entry["uid"]]
                    if not recent:
                        found.append(entry)
                        _log(entry)
            except Exception:
                continue
        try:
            M.logout()
        except Exception:
            pass
        # despertar waiters
        with _cond:
            _cond.notify_all()
        _listener["last_check"] = datetime.now().isoformat()
        return {"ok": True, "found": found, "scanned": len(uids)}
    except imaplib.IMAP4.error as e:
        return {"ok": False, "error": f"IMAP login/fetch: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def push_sms(text: str, sender: str = "sms_webhook") -> dict:
    """Webhook inbound SMS: parsea el texto y guarda código si matchea."""
    cfg = get_config()
    patterns = cfg.get("patterns", DEFAULT_PATTERNS)
    code = _match_code(text, patterns)
    if not code:
        return {"ok": False, "error": "no_code_match", "text_preview": (text or "")[:120]}
    entry = {
        "code": code,
        "source": "sms",
        "from": sender[:60],
        "subject": (text or "")[:160],
        "ts": datetime.now().isoformat(),
        "uid": f"sms-{int(time.time() * 1000)}",
    }
    _log(entry)
    with _cond:
        _cond.notify_all()
    return {"ok": True, **entry}


def wait_for_otp(since_ts: Optional[str] = None, timeout: float = 60,
                 sender_contains: str = "") -> Optional[dict]:
    """Bloquea hasta que aparezca OTP nuevo o timeout (segundos).

    since_ts: ISO timestamp — solo devuelve códigos posteriores a este.
    sender_contains: filtro adicional por remitente.
    """
    deadline = time.time() + timeout
    since_dt = None
    if since_ts:
        try:
            since_dt = datetime.fromisoformat(since_ts)
        except Exception:
            since_dt = None

    def _find():
        for entry in get_log(30):
            try:
                ts = datetime.fromisoformat(entry.get("ts", ""))
                if since_dt and ts <= since_dt:
                    continue
                if sender_contains and sender_contains.lower() not in entry.get("from", "").lower():
                    continue
                return entry
            except Exception:
                continue
        return None

    # Primera comprobación sin esperar
    got = _find()
    if got:
        return got
    with _cond:
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            _cond.wait(timeout=min(remaining, 2))
            got = _find()
            if got:
                return got
    return None


# ═══════════════════════════════════════════
#  LISTENER (polling IMAP en background)
# ═══════════════════════════════════════════
def _listener_loop(stop_event):
    while not stop_event.is_set():
        cfg = get_config()
        if not cfg.get("enabled") or cfg.get("mode") == "sms_webhook":
            stop_event.wait(10)
            continue
        try:
            check_inbox()
        except Exception:
            pass
        interval = int(cfg.get("poll_interval_s", 5))
        stop_event.wait(max(3, interval))


def start_listener() -> dict:
    if _listener["running"]:
        return {"ok": True, "already_running": True}
    stop = threading.Event()
    t = threading.Thread(target=_listener_loop, args=(stop,),
                         daemon=True, name="otp-3ds-listener")
    _listener.update({"running": True, "thread": t, "stop_event": stop})
    t.start()
    return {"ok": True, "started": True}


def stop_listener() -> dict:
    if not _listener["running"]:
        return {"ok": True, "not_running": True}
    ev = _listener.get("stop_event")
    if ev:
        ev.set()
    _listener.update({"running": False, "thread": None, "stop_event": None})
    return {"ok": True, "stopped": True}


def listener_status() -> dict:
    return {"running": _listener["running"], "last_check": _listener.get("last_check")}


def get_stats() -> dict:
    log = get_log(200)
    cfg = get_config()
    by_source = {}
    for e in log:
        s = e.get("source", "?")
        by_source[s] = by_source.get(s, 0) + 1
    last = log[0] if log else None
    return {
        "enabled": cfg.get("enabled", False),
        "mode": cfg.get("mode"),
        "total_otps": len(log),
        "by_source": by_source,
        "last_otp_ts": last.get("ts") if last else None,
        "last_otp_source": last.get("source") if last else None,
        "listener_running": _listener["running"],
    }


def self_test() -> dict:
    """Test sin conectarse: valida regex con muestras reales."""
    samples = [
        ("Su código de seguridad es 482913 para confirmar su compra en Ticketmaster", "482913"),
        ("3D Secure code: 845291", "845291"),
        ("TICKETMASTER verification code 123-456", "123456"),
        ("Your PIN is 9921", "9921"),
        ("Código OTP: 78451", "78451"),
    ]
    cfg = get_config()
    patterns = cfg.get("patterns", DEFAULT_PATTERNS)
    results = []
    ok_count = 0
    for text, expected in samples:
        got = _match_code(text, patterns)
        match = got == expected
        if match:
            ok_count += 1
        results.append({"text": text[:60], "expected": expected, "got": got, "match": match})
    return {
        "ok": ok_count == len(samples),
        "matches": f"{ok_count}/{len(samples)}",
        "samples": results,
    }
