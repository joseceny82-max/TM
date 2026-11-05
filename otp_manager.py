"""otp_manager.py — Lectura automática de códigos OTP (2FA) para Ticketmaster.

Rewrite completo con IMAP REAL (imap-tools):
  • Multi-account (pool de buzones)
  • Passwords encriptados (Fernet, llave derivada del host)
  • Scheduler background thread (poll cada N segundos)
  • IMAP IDLE helper (push en vez de polling) si se habilita
  • Preset providers (Gmail, Outlook, iCloud, Yahoo, ProtonMail Bridge, Zoho, Yandex, Custom)
  • Multi-sender (5 senders TM conocidos)
  • Multi-regex (4 patrones TM en ES/EN/DE/FR)
  • Stats detalladas (códigos/hora, latencia, errores)
  • Self-test exhaustivo (sin conexión + con conexión opcional)
  • Forward opcional a Telegram/Discord cuando llega un OTP
  • Legacy-compatible con la config antigua (migración automática)

API pública:
  get_config() / save_config(patch)
  add_account(acc)  update_account(acc_id, patch)  remove_account(acc_id)  list_accounts()
  test_connection(account)    → {'ok', 'error', 'elapsed_ms', 'messages_total'}
  fetch_otps(account=None)    → {'ok', 'codes': [...], 'checked': N, 'elapsed_ms': ms}
  start_scheduler(interval_s=10)   stop_scheduler()   scheduler_status()
  manual_otp(code, source='manual', account_id=None)
  get_history(limit=50)   get_last_otp(account_id=None)
  get_stats()
  get_providers()
  self_test()               → {'ok', 'checks': {...}}
  read_otp()                → [LEGACY] mantiene compat con dashboard antiguo
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

import config
import json_atomic

# ---------- dependencias opcionales ----------
try:
    from imap_tools import MailBox, AND, MailMessageFlags
    _HAS_IMAP_TOOLS = True
except ImportError:
    _HAS_IMAP_TOOLS = False

try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

_CONFIG_KEY = "otp.json"
_lock = threading.RLock()  # RLock (reentrant): get_config se llama dentro de save_config

# ═══════════════════════════════════════════════════════════════
#  PROVIDERS — presets conocidos
# ═══════════════════════════════════════════════════════════════
PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "gmail": {
        "label": "Gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "notes": "Requiere App Password (no contraseña normal). Activa 2FA y crea one. Soporta IDLE.",
        "help_url": "https://support.google.com/mail/answer/185833",
        "supports_idle": True,
    },
    "outlook": {
        "label": "Outlook / Hotmail / Live",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "notes": "Requiere App Password si tienes 2FA. Microsoft deshabilita IMAP básico progresivamente.",
        "help_url": "https://support.microsoft.com/account-billing/app-passwords-5896ed9b-4263-e681-128a-a6f2979a7944",
        "supports_idle": True,
    },
    "icloud": {
        "label": "iCloud Mail",
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "notes": "Requiere app-specific password. Formato usuario: xxx@icloud.com",
        "help_url": "https://support.apple.com/en-us/102654",
        "supports_idle": True,
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "notes": "Requiere App Password.",
        "help_url": "https://help.yahoo.com/kb/SLN15241.html",
        "supports_idle": True,
    },
    "protonmail": {
        "label": "ProtonMail (Bridge)",
        "imap_host": "127.0.0.1",
        "imap_port": 1143,
        "notes": "Requiere ProtonMail Bridge corriendo local. Usa contraseña de Bridge.",
        "help_url": "https://proton.me/mail/bridge",
        "supports_idle": False,
    },
    "zoho": {
        "label": "Zoho Mail",
        "imap_host": "imap.zoho.com",
        "imap_port": 993,
        "notes": "Habilita IMAP en Configuración > Correo electrónico > Reenvío POP/IMAP.",
        "help_url": "https://www.zoho.com/mail/help/imap-access.html",
        "supports_idle": True,
    },
    "yandex": {
        "label": "Yandex",
        "imap_host": "imap.yandex.com",
        "imap_port": 993,
        "notes": "Habilita IMAP + usa password de aplicación.",
        "help_url": "https://yandex.com/support/mail/mail-clients/others.html",
        "supports_idle": True,
    },
    "fastmail": {
        "label": "FastMail",
        "imap_host": "imap.fastmail.com",
        "imap_port": 993,
        "notes": "Crea una app-specific password en Settings > Privacy & Security.",
        "help_url": "https://www.fastmail.help/hc/en-us/articles/1500000278342",
        "supports_idle": True,
    },
    "gmx": {
        "label": "GMX / mail.com",
        "imap_host": "imap.gmx.com",
        "imap_port": 993,
        "notes": "Habilita POP/IMAP en Configuración.",
        "help_url": "",
        "supports_idle": True,
    },
    "custom": {
        "label": "Custom IMAP",
        "imap_host": "",
        "imap_port": 993,
        "notes": "Indica host/port manualmente.",
        "help_url": "",
        "supports_idle": False,
    },
}

# ═══════════════════════════════════════════════════════════════
#  SENDERS TM conocidos (ampliar según se descubran nuevos)
# ═══════════════════════════════════════════════════════════════
TM_KNOWN_SENDERS = [
    "customer_support@email.ticketmaster.com",
    "noreply@ticketmaster.com",
    "no-reply@order.ticketmaster.com",
    "customer_support@email.ticketmaster.es",
    "noreply@ticketmaster.es",
    "customer_support@email.ticketmaster.co.uk",
    "noreply@email.ticketmaster.com",
    "ticketmaster@email.ticketmaster.com",
    "no-reply@ticketmaster.com",
    "no_reply@ticketmaster.com",
]

# ═══════════════════════════════════════════════════════════════
#  REGEX patterns (multi-idioma + smart extractor con scoring)
# ═══════════════════════════════════════════════════════════════
TM_OTP_PATTERNS = [
    # Inglés "Your Ticketmaster verification code is 123456"
    r"(?:verification|security|confirm(?:ation)?|access)[^0-9]{0,40}?\b(\d{4,8})\b",
    # Español "Tu código de verificación es 123456"
    r"(?:c[óo]digo|verificaci[óo]n|confirmaci[óo]n)[^0-9]{0,40}?\b(\d{4,8})\b",
    # Alemán "Ihr Verifizierungscode ist 123456"
    r"(?:Verifizierungs|Best[äa]tigungs|Sicherheits)(?:code|schl[üu]ssel)?[^0-9]{0,40}?\b(\d{4,8})\b",
    # Francés "Votre code de vérification est 123456"
    r"(?:code\s+(?:de\s+v[ée]rification|de\s+s[ée]curit[ée]|d.acc[èe]s))[^0-9]{0,40}?\b(\d{4,8})\b",
    # Fallback: "code is 123456" / "code: 123456"
    r"\bcode[\s:]+(\d{4,8})\b",
    # Genérico último recurso
    r"\b(\d{6})\b",
]

# Priority-scored extractor regex (section 11.2 del research report)
_LABEL_RE = re.compile(
    r"(?ix)"
    r"(?:code|c[oó]digo|codice|kode|kod|pin|otp|verification|verificaci[oó]n|"
    r"verifica|best[aä]tigung|v[eé]rification|bekräftelse)\D{0,40}(\d{4,8})\b"
)
# Anti-patterns: rechaza dígitos en URLs, phone numbers, order IDs
_URL_RE = re.compile(r"https?://[^\s]*\b\d{4,8}\b")
_ORDER_RE = re.compile(r"(?i)(?:order|pedido|bestellung|commande|ordine|auftrag|n[uú]mero[\s_-]+de[\s_-]+(?:pedido|orden))\D{0,20}\d{4,8}")
_PHONE_RE = re.compile(r"\+\d{1,3}[\s\-]?\d{2,3}[\s\-]?\d{2,3}[\s\-]?\d{2,4}")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")  # años típicos


# ═══════════════════════════════════════════════════════════════
#  Encriptación (Fernet con llave derivada del host)
# ═══════════════════════════════════════════════════════════════
def _get_fernet_key() -> bytes:
    """Deriva una llave estable del hostname + user — no bulletproof pero
    evita passwords en plano en el JSON. No es cifrado seguro contra atacante
    con acceso al host. Para eso usaría keyring. """
    seed = f"{socket.gethostname()}|{os.environ.get('USER') or os.environ.get('USERNAME') or 'tm'}|tm_otp_v1"
    return base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest())


def _encrypt_password(pw: str) -> str:
    if not pw: return ""
    if not _HAS_CRYPTO:
        # Fallback: base64 obfuscation (mejor que plano, pero NO es seguro)
        return "B64:" + base64.urlsafe_b64encode(pw.encode("utf-8")).decode("ascii")
    try:
        f = Fernet(_get_fernet_key())
        return "FRN:" + f.encrypt(pw.encode("utf-8")).decode("ascii")
    except Exception:
        return "B64:" + base64.urlsafe_b64encode(pw.encode("utf-8")).decode("ascii")


def _decrypt_password(enc: str) -> str:
    if not enc: return ""
    if enc.startswith("FRN:") and _HAS_CRYPTO:
        try:
            f = Fernet(_get_fernet_key())
            return f.decrypt(enc[4:].encode("ascii")).decode("utf-8")
        except Exception:
            return ""
    if enc.startswith("B64:"):
        try:
            return base64.urlsafe_b64decode(enc[4:].encode("ascii")).decode("utf-8")
        except Exception:
            return ""
    # Legacy: texto plano — aceptado para migrar
    return enc


# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════
def _default_config() -> Dict[str, Any]:
    return {
        "version": 2,
        "enabled": False,
        "accounts": [],                          # multi-buzón [{id,email,host,port,password_enc,provider,enabled,folder,use_idle}]
        "scheduler_enabled": False,
        "scheduler_interval_s": 10,              # polling cada 10s cuando está activo
        "senders": list(TM_KNOWN_SENDERS),       # editable desde UI
        "patterns": list(TM_OTP_PATTERNS),       # editable
        "lookback_minutes": 15,                  # sólo procesa correos de los últimos 15 min
        "history": [],                           # últimos 200 OTPs
        "stats": {
            "total_received": 0,
            "total_imap_errors": 0,
            "last_check_ts": "",
            "last_error": "",
            "by_account": {},                    # {acc_id: {received, errors, last_otp_ts, avg_latency_s}}
        },
        "forward_telegram": False,
        "forward_discord": False,
        # legacy fields — para migración
        "imap_host": "",
        "imap_port": 993,
        "username": "",
        "password": "",                          # no se usa, se migra a accounts[0]
        "sender_filter": "",
        "regex": "",
    }


def _migrate_legacy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Migra config v1 (single-account) → v2 (multi-account)."""
    if cfg.get("version", 1) >= 2:
        return cfg
    # Si tenía credenciales legacy, las pasamos a accounts[0]
    legacy_user = cfg.get("username") or ""
    legacy_pw = cfg.get("password") or ""
    legacy_host = cfg.get("imap_host") or "imap.gmail.com"
    legacy_port = cfg.get("imap_port") or 993
    if legacy_user and not cfg.get("accounts"):
        cfg["accounts"] = [{
            "id": "acc_" + secrets.token_hex(4),
            "email": legacy_user,
            "host": legacy_host,
            "port": int(legacy_port),
            "password_enc": _encrypt_password(legacy_pw) if legacy_pw else "",
            "provider": "gmail" if "gmail" in legacy_host else "custom",
            "enabled": bool(cfg.get("enabled")),
            "folder": "INBOX",
            "use_idle": False,
            "created_at": datetime.now().isoformat(),
        }]
    # Limpia legacy fields (los dejamos vacíos para no romper compat)
    cfg["password"] = ""
    # Normaliza campos nuevos
    defaults = _default_config()
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    cfg["version"] = 2
    return cfg


def get_config() -> Dict[str, Any]:
    with _lock:
        cfg = json_atomic.load(config.ruta(_CONFIG_KEY), default=_default_config())
        cfg = _migrate_legacy(cfg)
        return cfg


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        cfg = get_config()
        # Campos aceptados
        for k in ("enabled", "scheduler_enabled", "scheduler_interval_s",
                  "senders", "patterns", "lookback_minutes",
                  "forward_telegram", "forward_discord"):
            if k in (patch or {}):
                cfg[k] = patch[k]
        # Legacy compat — si envían username/password/host/port/regex/sender_filter,
        # actualiza accounts[0] (o lo crea)
        legacy_keys = {"imap_host", "imap_port", "username", "password",
                        "sender_filter", "regex"}
        if any(k in (patch or {}) for k in legacy_keys):
            accs = cfg.get("accounts") or []
            if not accs:
                accs.append({
                    "id": "acc_" + secrets.token_hex(4),
                    "email": "",
                    "host": "imap.gmail.com",
                    "port": 993,
                    "password_enc": "",
                    "provider": "gmail",
                    "enabled": True,
                    "folder": "INBOX",
                    "use_idle": False,
                    "created_at": datetime.now().isoformat(),
                })
            a = accs[0]
            if "imap_host" in patch: a["host"] = patch["imap_host"]
            if "imap_port" in patch:
                try: a["port"] = int(patch["imap_port"])
                except Exception: pass
            if "username" in patch: a["email"] = patch["username"]
            if "password" in patch and patch["password"]:
                a["password_enc"] = _encrypt_password(patch["password"])
            if "sender_filter" in patch and patch["sender_filter"]:
                sf = patch["sender_filter"].strip()
                if sf and sf not in cfg["senders"]:
                    cfg["senders"] = [sf] + cfg["senders"]
            if "regex" in patch and patch["regex"]:
                rx = patch["regex"].strip()
                if rx and rx not in cfg["patterns"]:
                    cfg["patterns"] = [rx] + cfg["patterns"]
            cfg["accounts"] = accs
        json_atomic.save(config.ruta(_CONFIG_KEY), cfg)
        return cfg


# ═══════════════════════════════════════════════════════════════
#  Multi-account CRUD
# ═══════════════════════════════════════════════════════════════
def _normalize_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    """Aplica preset de provider al account si falta host/port."""
    provider = (acc.get("provider") or "custom").lower()
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["custom"])
    acc.setdefault("host", preset["imap_host"])
    try: acc["port"] = int(acc.get("port") or preset["imap_port"])
    except Exception: acc["port"] = preset["imap_port"]
    acc.setdefault("enabled", True)
    acc.setdefault("folder", "INBOX")
    acc.setdefault("use_idle", False)
    acc.setdefault("id", "acc_" + secrets.token_hex(4))
    acc.setdefault("created_at", datetime.now().isoformat())
    return acc


def add_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        cfg = get_config()
        # acc tendrá password en plano — lo ciframos antes de guardar
        pw = acc.pop("password", None)
        if pw is not None:
            acc["password_enc"] = _encrypt_password(pw)
        acc = _normalize_account(acc)
        cfg.setdefault("accounts", []).append(acc)
        json_atomic.save(config.ruta(_CONFIG_KEY), cfg)
        return acc


def update_account(acc_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with _lock:
        cfg = get_config()
        accs = cfg.get("accounts") or []
        target = None
        for a in accs:
            if a.get("id") == acc_id:
                target = a; break
        if not target:
            return None
        # Handle password update
        if "password" in patch:
            pw = patch.pop("password")
            if pw:
                target["password_enc"] = _encrypt_password(pw)
        # Resto de campos permitidos
        for k in ("email", "host", "port", "provider", "enabled", "folder", "use_idle"):
            if k in patch:
                if k == "port":
                    try: target[k] = int(patch[k])
                    except Exception: pass
                else:
                    target[k] = patch[k]
        _normalize_account(target)
        json_atomic.save(config.ruta(_CONFIG_KEY), cfg)
        return target


def remove_account(acc_id: str) -> bool:
    with _lock:
        cfg = get_config()
        accs = cfg.get("accounts") or []
        before = len(accs)
        cfg["accounts"] = [a for a in accs if a.get("id") != acc_id]
        if len(cfg["accounts"]) == before:
            return False
        json_atomic.save(config.ruta(_CONFIG_KEY), cfg)
        return True


def list_accounts(decrypt_passwords: bool = False) -> List[Dict[str, Any]]:
    """Lista cuentas. decrypt_passwords sólo en uso interno."""
    cfg = get_config()
    accs = list(cfg.get("accounts") or [])
    out = []
    for a in accs:
        b = dict(a)
        b.pop("password_enc", None)
        if decrypt_passwords:
            b["_password"] = _decrypt_password(a.get("password_enc") or "")
        b["has_password"] = bool(a.get("password_enc"))
        out.append(b)
    return out


def get_providers() -> Dict[str, Any]:
    return PROVIDER_PRESETS


# ═══════════════════════════════════════════════════════════════
#  Extracción OTP
# ═══════════════════════════════════════════════════════════════
def extract_code(text: str,
                  patterns: Optional[List[str]] = None,
                  smart: bool = True) -> Optional[str]:
    """Extrae código OTP de un texto.

    Con smart=True (default):
      1. Pre-limpia URLs, números de teléfono y años del texto.
      2. Busca labels (code|código|OTP|…) seguidos inmediatamente del código.
      3. Fallback: primer grupo de 6 dígitos que NO esté cerca de "order/pedido".
    Con smart=False:
      Solo aplica los patrones en orden.
    """
    if not text:
        return None
    if smart:
        # Pre-limpieza: elimina ruido obvio
        cleaned = _URL_RE.sub(" ", text)
        cleaned = _PHONE_RE.sub(" ", cleaned)
        cleaned = _YEAR_RE.sub(" ", cleaned)
        # Priority 1: label inmediatamente antes del código
        m = _LABEL_RE.search(cleaned)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if 4 <= len(digits) <= 8:
                return digits
        # Priority 2: cualquier 6 dígitos NO marcado como order id
        for m in re.finditer(r"\b(\d{6})\b", cleaned):
            start = max(0, m.start() - 50)
            ctx = cleaned[start:m.end() + 30]
            if _ORDER_RE.search(ctx):
                continue
            return m.group(1)
        # Priority 3: patrones clásicos sobre el texto limpio
        text_for_patterns = cleaned
    else:
        text_for_patterns = text
    # Patterns clásicos (fallback)
    patterns = patterns or TM_OTP_PATTERNS
    for pat in patterns:
        try:
            m = re.search(pat, text_for_patterns, flags=re.IGNORECASE | re.MULTILINE)
            if m:
                code = m.group(1) if m.groups() else m.group(0)
                digits = re.sub(r"\D", "", code)
                if 4 <= len(digits) <= 8:
                    return digits
        except re.error:
            continue
    return None


def _is_tm_sender(from_addr: str, senders: List[str]) -> bool:
    if not from_addr: return False
    fa = from_addr.lower().strip()
    for s in senders:
        if not s: continue
        s = s.lower().strip()
        # Coincidencia por dominio o igualdad
        if s in fa or fa in s:
            return True
        # comparación de dominio (ticketmaster.com / email.ticketmaster.com)
        if "@" in s and "@" in fa and fa.split("@", 1)[1] == s.split("@", 1)[1]:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
#  IMAP — conexión y fetch (síncrono)
# ═══════════════════════════════════════════════════════════════
def _build_account_cfg(account_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    cfg = get_config()
    accs = cfg.get("accounts") or []
    if not accs: return None
    if account_id:
        for a in accs:
            if a.get("id") == account_id:
                return a
        return None
    # Devuelve primero habilitado
    for a in accs:
        if a.get("enabled"): return a
    return accs[0]


def test_connection(account: Optional[Dict[str, Any]] = None,
                    account_id: Optional[str] = None,
                    timeout_s: int = 15,
                    retries: int = 1) -> Dict[str, Any]:
    """Testea login IMAP + SELECT INBOX sin leer mensajes.
    Aplica socket timeout y un reintento ante errores transitorios (EOF, abort, ConnectionReset)."""
    if not _HAS_IMAP_TOOLS:
        return {"ok": False, "error": "imap-tools no instalado: pip install imap-tools"}
    acc = account or _build_account_cfg(account_id)
    if not acc:
        return {"ok": False, "error": "no_account_configured"}
    pw = _decrypt_password(acc.get("password_enc") or "")
    if not acc.get("email") or not pw:
        return {"ok": False, "error": "missing_credentials"}

    transient = ("EOF", "abort", "timed out", "ConnectionReset", "BrokenPipe", "WSAECONN")
    last_err = None
    started_total = time.time()
    for attempt in range(max(1, retries + 1)):
        prev_to = socket.getdefaulttimeout()
        socket.setdefaulttimeout(int(timeout_s))
        started = time.time()
        try:
            with MailBox(acc["host"], int(acc.get("port") or 993)).login(
                    acc["email"], pw, initial_folder=acc.get("folder") or "INBOX") as mb:
                total = 0
                try:
                    info = mb.folder.status(acc.get("folder") or "INBOX")
                    total = int(info.get("MESSAGES", 0))
                except Exception:
                    try:
                        total = sum(1 for _ in mb.uids())
                    except Exception:
                        pass
            return {
                "ok": True,
                "elapsed_ms": int((time.time() - started_total) * 1000),
                "messages_total": total,
                "host": acc["host"],
                "port": int(acc.get("port") or 993),
                "email": acc["email"],
                "attempts": attempt + 1,
            }
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
            is_transient = any(t.lower() in str(e).lower() for t in transient)
            if attempt < retries and is_transient:
                continue
            return {
                "ok": False,
                "error": last_err,
                "elapsed_ms": int((time.time() - started_total) * 1000),
                "attempts": attempt + 1,
                "transient": is_transient,
            }
        finally:
            socket.setdefaulttimeout(prev_to)
    return {"ok": False, "error": last_err or "unknown",
            "elapsed_ms": int((time.time() - started_total) * 1000)}


def fetch_otps(account_id: Optional[str] = None,
               mark_seen: bool = True,
               max_messages: int = 30) -> Dict[str, Any]:
    """Lee correos recientes del sender TM y extrae códigos.

    Retorna {'ok', 'codes': [{'code','from','subject','received_at','account_id'}], 'checked': N}
    """
    if not _HAS_IMAP_TOOLS:
        return {"ok": False, "error": "imap-tools no instalado"}
    cfg = get_config()
    acc = _build_account_cfg(account_id)
    if not acc:
        return {"ok": False, "error": "no_account_configured"}
    if not acc.get("enabled"):
        return {"ok": False, "error": "account_disabled"}

    pw = _decrypt_password(acc.get("password_enc") or "")
    if not acc.get("email") or not pw:
        return {"ok": False, "error": "missing_credentials"}

    senders = cfg.get("senders") or TM_KNOWN_SENDERS
    patterns = cfg.get("patterns") or TM_OTP_PATTERNS
    lookback = int(cfg.get("lookback_minutes") or 15)
    since_dt = (datetime.now() - timedelta(minutes=lookback)).date()

    started = time.time()
    codes = []
    checked = 0
    new_entries = []
    try:
        with MailBox(acc["host"], int(acc.get("port") or 993)).login(
                acc["email"], pw, initial_folder=acc.get("folder") or "INBOX") as mb:
            # Filtro: desde hoy(+lookback) AND no-leídos AND remitente TM
            # imap-tools soporta AND(date_gte=...) y from_...
            # Como hay múltiples senders usamos OR manual: iteramos
            msgs_seen_ids = set()
            for sender in senders:
                try:
                    query = AND(from_=sender, date_gte=since_dt)
                    for msg in mb.fetch(query, limit=max_messages,
                                          mark_seen=mark_seen, reverse=True):
                        if msg.uid in msgs_seen_ids: continue
                        msgs_seen_ids.add(msg.uid)
                        checked += 1
                        text = (msg.text or "") + "\n" + (msg.html or "") + "\n" + (msg.subject or "")
                        code = extract_code(text, patterns)
                        if code:
                            entry = {
                                "code": code,
                                "from": msg.from_ or sender,
                                "subject": (msg.subject or "")[:200],
                                "received_at": msg.date.isoformat() if msg.date else datetime.now().isoformat(),
                                "extracted_at": datetime.now().isoformat(),
                                "source": "imap",
                                "account_id": acc["id"],
                                "account_email": acc["email"],
                                "uid": str(msg.uid),
                            }
                            codes.append(entry)
                            new_entries.append(entry)
                except Exception as inner:
                    # Continuamos con el siguiente sender
                    continue
        elapsed_ms = int((time.time() - started) * 1000)

        # Actualiza historial + stats
        if new_entries:
            _persist_history(new_entries)
            _update_stats(acc["id"], received=len(new_entries), error=False)
        else:
            _update_stats(acc["id"], received=0, error=False)

        # Forward opcional
        if new_entries and (cfg.get("forward_telegram") or cfg.get("forward_discord")):
            for e in new_entries:
                _forward_otp(e, cfg)

        return {
            "ok": True,
            "codes": codes,
            "checked": checked,
            "elapsed_ms": elapsed_ms,
            "account_id": acc["id"],
            "account_email": acc["email"],
        }
    except Exception as e:
        _update_stats(acc["id"], received=0, error=True, msg=str(e)[:200])
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:200]}",
            "elapsed_ms": int((time.time() - started) * 1000),
            "account_id": acc.get("id"),
        }


def fetch_all_accounts() -> Dict[str, Any]:
    """Lee OTPs en TODAS las cuentas habilitadas en paralelo (thread pool)."""
    cfg = get_config()
    accs = [a for a in (cfg.get("accounts") or []) if a.get("enabled")]
    if not accs:
        return {"ok": False, "error": "no_accounts_enabled", "results": []}
    results = []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(accs))) as ex:
        futs = {ex.submit(fetch_otps, a["id"]): a["id"] for a in accs}
        for fut in futs:
            try:
                results.append(fut.result(timeout=30))
            except Exception as e:
                results.append({"ok": False, "error": str(e)[:200], "account_id": futs[fut]})
    total_codes = sum(len(r.get("codes") or []) for r in results)
    return {
        "ok": True,
        "accounts_checked": len(accs),
        "total_codes": total_codes,
        "results": results,
    }


# ═══════════════════════════════════════════════════════════════
#  Manual lookup por alias (uso manual del OTP)
# ═══════════════════════════════════════════════════════════════
def fetch_otp_for_alias(alias_email: str,
                         account_id: Optional[str] = None,
                         lookback_minutes: int = 30,
                         max_messages: int = 50) -> Dict[str, Any]:
    """Devuelve el OTP de Ticketmaster más reciente destinado al alias.

    Útil cuando el usuario gestiona una cuenta a mano y necesita el código
    que llegó al buzón madre (alias / icloud rotativo).

    Filtros: from ∈ TM_KNOWN_SENDERS · TO/CC contiene alias · fecha ≥ hoy-N min.
    """
    if not _HAS_IMAP_TOOLS:
        return {"ok": False, "error": "imap_tools_missing",
                "hint": "pip install imap-tools"}
    if not alias_email or "@" not in alias_email:
        return {"ok": False, "error": "alias_invalid"}

    cfg = get_config()
    acc = _build_account_cfg(account_id)
    if not acc:
        return {"ok": False, "error": "no_imap_account_configured",
                "hint": "Configura una cuenta IMAP madre primero (panel arriba)."}
    pw = _decrypt_password(acc.get("password_enc") or "")
    if not acc.get("email") or not pw:
        return {"ok": False, "error": "missing_credentials"}

    senders = cfg.get("senders") or TM_KNOWN_SENDERS
    patterns = cfg.get("patterns") or TM_OTP_PATTERNS
    lookback_minutes = max(1, min(int(lookback_minutes or 30), 1440))
    since_dt = (datetime.now() - timedelta(minutes=lookback_minutes)).date()

    started = time.time()
    alias_low = alias_email.lower().strip()
    candidates: List[Dict[str, Any]] = []
    seen_uids: set = set()
    try:
        prev_to = socket.getdefaulttimeout()
        socket.setdefaulttimeout(20)
        try:
            with MailBox(acc["host"], int(acc.get("port") or 993)).login(
                    acc["email"], pw,
                    initial_folder=acc.get("folder") or "INBOX") as mb:
                # Itera por cada sender TM, filtra por alias en TO/CC
                for sender in senders:
                    try:
                        query = AND(from_=sender, date_gte=since_dt)
                        for msg in mb.fetch(query, limit=max_messages,
                                              mark_seen=False, reverse=True):
                            if msg.uid in seen_uids:
                                continue
                            seen_uids.add(msg.uid)
                            to_low = ", ".join(msg.to or []).lower()
                            cc_low = ", ".join(msg.cc or []).lower() if msg.cc else ""
                            if alias_low not in to_low and alias_low not in cc_low:
                                continue
                            text = ((msg.text or "") + "\n" +
                                    (msg.html or "") + "\n" +
                                    (msg.subject or ""))
                            code = extract_code(text, patterns)
                            if not code:
                                continue
                            try:
                                ts_iso = msg.date.isoformat() if msg.date else ""
                                age_s = ((datetime.now(msg.date.tzinfo) - msg.date)
                                          .total_seconds()) if msg.date else None
                            except Exception:
                                ts_iso = ""
                                age_s = None
                            candidates.append({
                                "code": code,
                                "from": msg.from_ or sender,
                                "subject": (msg.subject or "")[:200],
                                "received_at": ts_iso,
                                "age_s": int(age_s) if age_s is not None else None,
                                "uid": str(msg.uid),
                            })
                    except Exception:
                        continue
        finally:
            socket.setdefaulttimeout(prev_to)

        elapsed_ms = int((time.time() - started) * 1000)
        # Más reciente primero
        candidates.sort(key=lambda x: x.get("received_at") or "", reverse=True)

        if not candidates:
            return {"ok": False, "error": "no_otp_found",
                    "alias": alias_email,
                    "lookback_minutes": lookback_minutes,
                    "elapsed_ms": elapsed_ms,
                    "account_email": acc.get("email"),
                    "hint": ("No hay correos de Ticketmaster al alias en la "
                             "ventana indicada. Reenvía el OTP o aumenta el "
                             "lookback.")}

        top = candidates[0]
        # Guarda en historial
        try:
            _persist_history([{
                "code": top["code"],
                "source": "manual_lookup",
                "alias": alias_email,
                "from": top["from"],
                "subject": top["subject"],
                "received_at": top["received_at"],
                "extracted_at": datetime.now().isoformat(),
                "ts": datetime.now().isoformat(),
                "account_id": acc.get("id"),
                "account_email": acc.get("email"),
            }])
        except Exception:
            pass

        return {
            "ok": True,
            "code": top["code"],
            "alias": alias_email,
            "from": top["from"],
            "subject": top["subject"],
            "received_at": top["received_at"],
            "age_s": top.get("age_s"),
            "all_candidates": candidates[:10],
            "elapsed_ms": elapsed_ms,
            "account_email": acc.get("email"),
            "lookback_minutes": lookback_minutes,
        }
    except Exception as e:
        return {"ok": False,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "elapsed_ms": int((time.time() - started) * 1000)}


# ═══════════════════════════════════════════════════════════════
#  Persistencia history + stats
# ═══════════════════════════════════════════════════════════════
def _persist_history(entries: List[Dict[str, Any]]):
    """Persiste OTPs en historial con filtros (norma user 2026-05-12):
      1. Excluir sources de test (`pytest`, `selftest`, `self-test`, `mock`,
         `test`) — solo OTPs REALES de imap/imap_alias/manual.
      2. Deduplicar: si el MISMO `code` aparece de la MISMA `source` en los
         últimos 60s, no añadir copia. Evita el efecto 'todos iguales' del
         usuario cuando varios checks devuelven el mismo OTP fresco."""
    if not entries:
        return
    # Filtrar test sources — cualquier source que contenga estos tokens
    _BLOCKED_TOKENS = ("pytest", "selftest", "self-test", "self_test",
                       "e2e_test", "e2e-test", "e2etest", "smoke",
                       "mock", "stub", "fake", "dummy",
                       "test_", "_test", "unit_test", "unittest")
    def _is_test_source(src: str) -> bool:
        s = (src or "").lower().strip()
        if not s: return False
        if s in ("test",): return True
        return any(t in s for t in _BLOCKED_TOKENS)
    real_entries = [e for e in entries if not _is_test_source(e.get("source"))]
    if not real_entries:
        return
    with _lock:
        cfg = get_config()
        hist = cfg.get("history") or []
        now = datetime.now()
        for e in real_entries:
            # Deduplicar: skip si misma code+source en últimos 60s
            try:
                e_code = e.get("code")
                e_src = e.get("source")
                e_acc = e.get("account_id") or e.get("account_email")
                is_dup = False
                for h in hist[:20]:   # solo chequear las 20 más recientes
                    if (h.get("code") == e_code
                          and h.get("source") == e_src
                          and (h.get("account_id") or h.get("account_email")) == e_acc):
                        try:
                            t = datetime.fromisoformat(h.get("ts")
                                or h.get("extracted_at") or "")
                            if (now - t).total_seconds() < 60:
                                is_dup = True
                                break
                        except Exception: continue
                if is_dup:
                    continue
            except Exception: pass
            hist.insert(0, e)
        cfg["history"] = hist[:200]
        cfg["stats"]["total_received"] = (cfg["stats"].get("total_received", 0)
                                            + len(real_entries))
        json_atomic.save(config.ruta(_CONFIG_KEY), cfg)


def _update_stats(acc_id: str, received: int, error: bool, msg: str = ""):
    with _lock:
        cfg = get_config()
        s = cfg.setdefault("stats", {})
        s["last_check_ts"] = datetime.now().isoformat()
        if error:
            s["total_imap_errors"] = s.get("total_imap_errors", 0) + 1
            s["last_error"] = msg
        ba = s.setdefault("by_account", {}).setdefault(acc_id, {
            "received": 0, "errors": 0, "last_otp_ts": "",
        })
        if received:
            ba["received"] += received
            ba["last_otp_ts"] = datetime.now().isoformat()
        if error:
            ba["errors"] += 1
        json_atomic.save(config.ruta(_CONFIG_KEY), cfg)


# ═══════════════════════════════════════════════════════════════
#  Forward a Telegram/Discord (usa notifications.py si existe)
# ═══════════════════════════════════════════════════════════════
def _forward_otp(entry: Dict[str, Any], cfg: Dict[str, Any]):
    try:
        import notifications
        msg = f"🔐 OTP TM: **{entry['code']}**\n📧 {entry.get('account_email','?')}\n📝 {entry.get('subject','')[:80]}"
        if cfg.get("forward_telegram"):
            try: notifications.send_telegram(msg)
            except Exception: pass
        if cfg.get("forward_discord"):
            try: notifications.send_discord(msg)
            except Exception: pass
    except ImportError:
        pass


# ═══════════════════════════════════════════════════════════════
#  Manual OTP + history
# ═══════════════════════════════════════════════════════════════
def manual_otp(code: str, source: str = "manual",
                account_id: Optional[str] = None,
                subject: str = "") -> Dict[str, Any]:
    code = re.sub(r"\D", "", str(code or ""))
    if not code:
        return {"ok": False, "error": "empty_code"}
    entry = {
        "code": code,
        "source": source,
        "account_id": account_id,
        "subject": subject,
        "ts": datetime.now().isoformat(),
        "extracted_at": datetime.now().isoformat(),
    }
    _persist_history([entry])
    return entry


def get_history(limit: int = 50, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    hist = get_config().get("history") or []
    if account_id:
        hist = [h for h in hist if h.get("account_id") == account_id]
    return hist[:limit]


def get_last_otp(account_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    h = get_history(1, account_id=account_id)
    return h[0] if h else None


def get_stats() -> Dict[str, Any]:
    cfg = get_config()
    s = dict(cfg.get("stats") or {})
    s["accounts_total"] = len(cfg.get("accounts") or [])
    s["accounts_enabled"] = sum(1 for a in (cfg.get("accounts") or []) if a.get("enabled"))
    s["history_size"] = len(cfg.get("history") or [])
    # OTPs última hora / 24h
    now = datetime.now()
    last_1h = 0; last_24h = 0
    for h in (cfg.get("history") or []):
        try:
            t = datetime.fromisoformat(h.get("ts") or h.get("extracted_at") or "")
            dt = (now - t).total_seconds()
            if dt < 3600: last_1h += 1
            if dt < 86400: last_24h += 1
        except Exception:
            continue
    s["last_1h"] = last_1h
    s["last_24h"] = last_24h
    s["scheduler_running"] = _scheduler_state.get("running", False)
    return s


# ═══════════════════════════════════════════════════════════════
#  Scheduler (poll background)
# ═══════════════════════════════════════════════════════════════
_scheduler_state = {
    "running": False,
    "thread": None,
    "stop_event": None,
    "interval_s": 10,
    "started_at": "",
    "ticks": 0,
}


def _scheduler_loop(stop_evt: threading.Event, interval_s: int):
    _scheduler_state["ticks"] = 0
    while not stop_evt.is_set():
        try:
            fetch_all_accounts()
        except Exception:
            pass
        _scheduler_state["ticks"] += 1
        # Sleep en chunks para permitir parada rápida
        for _ in range(max(1, int(interval_s))):
            if stop_evt.is_set(): break
            time.sleep(1)


def start_scheduler(interval_s: Optional[int] = None) -> Dict[str, Any]:
    if _scheduler_state.get("running"):
        return {"ok": True, "status": "already_running",
                 "interval_s": _scheduler_state.get("interval_s")}
    cfg = get_config()
    interval = int(interval_s or cfg.get("scheduler_interval_s") or 10)
    interval = max(5, min(300, interval))  # clamp 5s - 5min
    evt = threading.Event()
    th = threading.Thread(target=_scheduler_loop, args=(evt, interval),
                           daemon=True, name="otp_scheduler")
    _scheduler_state.update({
        "running": True, "thread": th, "stop_event": evt,
        "interval_s": interval,
        "started_at": datetime.now().isoformat(),
    })
    th.start()
    # Persiste en config
    save_config({"scheduler_enabled": True, "scheduler_interval_s": interval})
    return {"ok": True, "status": "started", "interval_s": interval}


def stop_scheduler() -> Dict[str, Any]:
    if not _scheduler_state.get("running"):
        return {"ok": True, "status": "not_running"}
    evt = _scheduler_state.get("stop_event")
    if evt: evt.set()
    _scheduler_state["running"] = False
    save_config({"scheduler_enabled": False})
    return {"ok": True, "status": "stopped"}


def scheduler_status() -> Dict[str, Any]:
    return {
        "running": _scheduler_state.get("running", False),
        "interval_s": _scheduler_state.get("interval_s", 10),
        "started_at": _scheduler_state.get("started_at", ""),
        "ticks": _scheduler_state.get("ticks", 0),
    }


# ═══════════════════════════════════════════════════════════════
#  IMAP IDLE (push) — helper bloqueante, úsalo en un thread dedicado
# ═══════════════════════════════════════════════════════════════
def idle_listener(account_id: str, callback, timeout_s: int = 290) -> Dict[str, Any]:
    """Escucha IMAP IDLE en la cuenta dada. Llama callback(entries) cuando hay nuevos.

    Devuelve cuando expira timeout_s o el server notifica cambios.
    Para uso continuo, envuélvelo en un while loop externo (ver idle_worker_loop).
    """
    try:
        from imapclient import IMAPClient
    except ImportError:
        return {"ok": False, "error": "imapclient no instalado"}
    acc = _build_account_cfg(account_id)
    if not acc: return {"ok": False, "error": "no_account"}
    pw = _decrypt_password(acc.get("password_enc") or "")
    if not pw: return {"ok": False, "error": "no_password"}
    try:
        with IMAPClient(acc["host"], port=int(acc.get("port") or 993),
                          ssl=True, timeout=30) as c:
            c.login(acc["email"], pw)
            c.select_folder(acc.get("folder") or "INBOX")
            c.idle()
            responses = c.idle_check(timeout=timeout_s)
            c.idle_done()
            if responses:
                # Lee nuevos OTPs
                r = fetch_otps(account_id=account_id)
                if r.get("codes") and callback:
                    try: callback(r["codes"])
                    except Exception: pass
                return {"ok": True, "had_activity": True, "codes": r.get("codes", [])}
            return {"ok": True, "had_activity": False}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══════════════════════════════════════════════════════════════
#  IDLE worker loop (auto-renew cada 9 min, reconexión con backoff)
# ═══════════════════════════════════════════════════════════════
_idle_workers: Dict[str, Dict[str, Any]] = {}   # {acc_id: {thread, stop_evt, ...}}
_idle_lock = threading.Lock()

# Research finding: Gmail cierra IDLE después de ~10 min. Renovar cada 9.
IDLE_RENEW_S = 540  # 9 minutos
IDLE_BACKOFF_MIN_S = 5
IDLE_BACKOFF_MAX_S = 300


def _idle_worker_loop(acc_id: str, stop_evt: threading.Event,
                       notify_callback=None):
    """Loop persistente: IDLE → renew cada 9min → reconectar con backoff en errores."""
    try:
        from imapclient import IMAPClient
    except ImportError:
        _idle_workers[acc_id]["last_error"] = "imapclient no instalado"
        return
    backoff = IDLE_BACKOFF_MIN_S
    while not stop_evt.is_set():
        acc = _build_account_cfg(acc_id)
        if not acc or not acc.get("enabled"):
            _idle_workers[acc_id]["last_error"] = "account_disabled_or_missing"
            break
        pw = _decrypt_password(acc.get("password_enc") or "")
        if not pw:
            _idle_workers[acc_id]["last_error"] = "no_password"
            break
        try:
            with IMAPClient(acc["host"], port=int(acc.get("port") or 993),
                              ssl=True, timeout=30) as c:
                c.login(acc["email"], pw)
                c.select_folder(acc.get("folder") or "INBOX")
                _idle_workers[acc_id]["connected"] = True
                _idle_workers[acc_id]["last_error"] = ""
                backoff = IDLE_BACKOFF_MIN_S  # reset en conexión exitosa
                # Fetch inicial (por si hay mensajes pendientes)
                try:
                    r0 = fetch_otps(account_id=acc_id)
                    if r0.get("codes") and notify_callback:
                        try: notify_callback(r0["codes"])
                        except Exception: pass
                except Exception:
                    pass

                # Loop IDLE con renew
                while not stop_evt.is_set():
                    try:
                        c.idle()
                        # idle_check con timeout = renew interval
                        responses = c.idle_check(timeout=IDLE_RENEW_S)
                        c.idle_done()
                        _idle_workers[acc_id]["renewals"] = \
                            _idle_workers[acc_id].get("renewals", 0) + 1
                        if responses:
                            # Mensajes nuevos — lee
                            r = fetch_otps(account_id=acc_id)
                            _idle_workers[acc_id]["last_activity"] = datetime.now().isoformat()
                            if r.get("codes") and notify_callback:
                                try: notify_callback(r["codes"])
                                except Exception: pass
                    except Exception as inner:
                        _idle_workers[acc_id]["last_error"] = f"{type(inner).__name__}: {str(inner)[:150]}"
                        break  # salimos del inner loop → reconectamos
        except Exception as e:
            _idle_workers[acc_id]["connected"] = False
            _idle_workers[acc_id]["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            _idle_workers[acc_id]["errors"] = _idle_workers[acc_id].get("errors", 0) + 1
        # Backoff antes de reconectar
        if stop_evt.is_set(): break
        for _ in range(backoff):
            if stop_evt.is_set(): break
            time.sleep(1)
        backoff = min(backoff * 2, IDLE_BACKOFF_MAX_S)
    _idle_workers[acc_id]["connected"] = False
    _idle_workers[acc_id]["running"] = False


def start_idle_worker(acc_id: str, notify_callback=None) -> Dict[str, Any]:
    """Arranca un worker IDLE persistente para una cuenta concreta."""
    with _idle_lock:
        w = _idle_workers.get(acc_id)
        if w and w.get("running"):
            return {"ok": True, "status": "already_running"}
        evt = threading.Event()
        state = {
            "running": True,
            "connected": False,
            "stop_event": evt,
            "started_at": datetime.now().isoformat(),
            "renewals": 0,
            "errors": 0,
            "last_activity": "",
            "last_error": "",
        }
        th = threading.Thread(target=_idle_worker_loop,
                                args=(acc_id, evt, notify_callback),
                                daemon=True, name=f"otp_idle_{acc_id}")
        state["thread"] = th
        _idle_workers[acc_id] = state
        th.start()
        return {"ok": True, "status": "started"}


def stop_idle_worker(acc_id: str) -> Dict[str, Any]:
    with _idle_lock:
        w = _idle_workers.get(acc_id)
        if not w or not w.get("running"):
            return {"ok": True, "status": "not_running"}
        evt = w.get("stop_event")
        if evt: evt.set()
        w["running"] = False
        return {"ok": True, "status": "stopped"}


def start_all_idle_workers(notify_callback=None) -> Dict[str, Any]:
    """Arranca un worker IDLE para cada cuenta habilitada con use_idle=True."""
    cfg = get_config()
    accs = [a for a in (cfg.get("accounts") or [])
            if a.get("enabled") and a.get("use_idle")]
    started = []
    for a in accs:
        r = start_idle_worker(a["id"], notify_callback)
        if r.get("ok"): started.append(a["id"])
    return {"ok": True, "started_workers": started,
             "count": len(started), "total_enabled_idle": len(accs)}


def stop_all_idle_workers() -> Dict[str, Any]:
    stopped = []
    for acc_id in list(_idle_workers.keys()):
        r = stop_idle_worker(acc_id)
        if r.get("ok"): stopped.append(acc_id)
    return {"ok": True, "stopped_workers": stopped}


def idle_workers_status() -> Dict[str, Any]:
    out = {}
    for acc_id, w in _idle_workers.items():
        out[acc_id] = {
            "running": w.get("running", False),
            "connected": w.get("connected", False),
            "started_at": w.get("started_at", ""),
            "renewals": w.get("renewals", 0),
            "errors": w.get("errors", 0),
            "last_activity": w.get("last_activity", ""),
            "last_error": w.get("last_error", ""),
        }
    return out


# ═══════════════════════════════════════════════════════════════
#  Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    results = {"ok": True, "checks": {}, "details": {}}

    # 1. Libraries
    results["checks"]["imap_tools_available"] = _HAS_IMAP_TOOLS
    results["checks"]["crypto_available"] = _HAS_CRYPTO
    try:
        import imapclient  # noqa
        results["checks"]["imapclient_available"] = True
    except ImportError:
        results["checks"]["imapclient_available"] = False

    # 2. Encryption round-trip
    test_pw = "my_s3cret_pwd!"
    enc = _encrypt_password(test_pw)
    dec = _decrypt_password(enc)
    results["checks"]["encryption_roundtrip"] = (dec == test_pw)
    results["details"]["encryption_prefix"] = enc[:4] if enc else ""

    # 3. Regex extraction (todos los patterns + smart extractor)
    test_cases = [
        ("Your Ticketmaster verification code is 123456", "123456"),
        ("Tu código de verificación es 987654. No lo compartas.", "987654"),
        ("Ihr Verifizierungscode ist 555111", "555111"),
        ("Votre code de vérification est 777888", "777888"),
        ("code: 424242", "424242"),
        ("Subject random noise then 111222 somewhere", "111222"),
        ("No digits at all here", None),
        # Smart extractor: rechaza URL-embedded, phone, order number
        ("Your order #999999 shipped. https://tm.com/confirm?id=111111. Code is 222222.", "222222"),
        ("Call +1 555 333 4444 for help. Your verification code is 876543.", "876543"),
        ("Pedido número 100200. Tu código es 450607.", "450607"),
        ("Bestellung 445566. Ihr Bestätigungscode: 998877", "998877"),
        ("Year 2025. Code 112233", "112233"),
    ]
    regex_ok = 0
    regex_fails = []
    for txt, exp in test_cases:
        got = extract_code(txt)
        if got == exp:
            regex_ok += 1
        else:
            regex_fails.append(f"{txt!r} → got={got!r} exp={exp!r}")
    results["checks"]["regex_extraction"] = (regex_ok == len(test_cases))
    results["details"]["regex_passed"] = f"{regex_ok}/{len(test_cases)}"
    if regex_fails:
        results["details"]["regex_fails"] = regex_fails[:5]

    # 4. Config migration
    cfg = get_config()
    results["checks"]["config_has_v2"] = cfg.get("version", 1) >= 2
    results["checks"]["config_has_accounts_list"] = isinstance(cfg.get("accounts"), list)
    results["checks"]["config_has_patterns"] = isinstance(cfg.get("patterns"), list) and len(cfg["patterns"]) > 0
    results["checks"]["config_has_senders"] = isinstance(cfg.get("senders"), list) and len(cfg["senders"]) > 0

    # 5. Providers list
    p = get_providers()
    results["checks"]["providers_ok"] = isinstance(p, dict) and "gmail" in p

    # 6. Sender matching (contra los defaults hardcoded, no contra cfg persistido)
    default_senders = list(TM_KNOWN_SENDERS)
    results["checks"]["sender_match_basic"] = _is_tm_sender(
        "Customer Support <customer_support@email.ticketmaster.com>", default_senders)
    results["checks"]["sender_match_exact"] = _is_tm_sender(
        "noreply@ticketmaster.com", default_senders)
    results["checks"]["sender_match_reject_unknown"] = not _is_tm_sender(
        "hacker@evil.com", default_senders)

    # 7. Account CRUD smoke test
    test_acc = {"email": "__test__@test.invalid", "provider": "gmail", "password": "x"}
    a = add_account(test_acc)
    results["checks"]["account_add"] = bool(a.get("id"))
    results["checks"]["account_has_encrypted_pw"] = a.get("password_enc", "").startswith(("FRN:", "B64:"))
    u = update_account(a["id"], {"enabled": False})
    results["checks"]["account_update"] = (u is not None and u.get("enabled") is False)
    rm = remove_account(a["id"])
    results["checks"]["account_remove"] = rm

    # 8. Manual OTP
    m = manual_otp("999000", source="selftest")
    results["checks"]["manual_otp_ok"] = (m.get("code") == "999000")
    lo = get_last_otp()
    results["checks"]["last_otp_retrievable"] = (lo is not None and lo.get("code") == "999000")

    # 9. Stats
    s = get_stats()
    results["checks"]["stats_has_all_keys"] = all(k in s for k in
        ("total_received", "accounts_total", "last_1h", "last_24h", "history_size"))

    # 10. Scheduler lifecycle
    st = start_scheduler(interval_s=5)
    results["checks"]["scheduler_start"] = st.get("ok", False)
    time.sleep(0.2)
    stat = scheduler_status()
    results["checks"]["scheduler_status_running"] = stat.get("running", False)
    sp = stop_scheduler()
    results["checks"]["scheduler_stop"] = sp.get("ok", False)

    # 11. fetch_otps sin credenciales devuelve error controlado
    r = fetch_otps()
    results["checks"]["fetch_without_creds_handled"] = (r.get("ok") is False)

    # 12. IDLE worker lifecycle (start/stop sin credenciales no debe explotar)
    iw = start_idle_worker("acc_fake_nonexistent")
    results["checks"]["idle_worker_start_safe"] = iw.get("ok", False)
    time.sleep(0.1)
    iwst = idle_workers_status()
    results["checks"]["idle_worker_status_ok"] = isinstance(iwst, dict)
    isp = stop_idle_worker("acc_fake_nonexistent")
    results["checks"]["idle_worker_stop_safe"] = isp.get("ok", False)

    # 13. Providers presets completos
    providers = get_providers()
    must_have = {"gmail", "outlook", "icloud", "yahoo", "protonmail",
                  "zoho", "yandex", "fastmail", "custom"}
    results["checks"]["providers_complete"] = must_have.issubset(providers.keys())
    results["checks"]["gmail_has_idle_support"] = providers["gmail"].get("supports_idle", False)

    # 14. read_otp() legacy no rompe
    ro = read_otp()
    results["checks"]["read_otp_legacy_handled"] = "ok" in ro

    # Resumen
    results["total_checks"] = len(results["checks"])
    results["passed_checks"] = sum(1 for v in results["checks"].values() if v)
    results["ok"] = results["passed_checks"] == results["total_checks"]
    return results


# ═══════════════════════════════════════════════════════════════
#  Legacy API (compatibilidad con dashboard actual)
# ═══════════════════════════════════════════════════════════════
def read_otp() -> Dict[str, Any]:
    """[LEGACY] read_otp() del dashboard viejo — llama a fetch_all_accounts."""
    cfg = get_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "imap_disabled"}
    accs = cfg.get("accounts") or []
    if not accs:
        return {"ok": False, "error": "no_account_configured"}
    r = fetch_all_accounts()
    if r.get("ok"):
        total = r.get("total_codes", 0)
        return {"ok": True, "codes_found": total, "results": r.get("results")}
    return {"ok": False, "error": r.get("error", "unknown")}


def get_info() -> Dict[str, Any]:
    return {
        "module": "otp_manager",
        "version": 2,
        "libs": {
            "imap_tools": _HAS_IMAP_TOOLS,
            "crypto": _HAS_CRYPTO,
        },
        "stats": get_stats(),
        "providers": list(PROVIDER_PRESETS.keys()),
        "patterns_count": len(TM_OTP_PATTERNS),
        "senders_count": len(TM_KNOWN_SENDERS),
    }
