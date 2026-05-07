"""otp_3ds_autofill.py — Autofill OTP 3DS bancarios desde IMAP/SMS sin intervención.

Wrapper sobre `otp_3ds.py` con:
  - Patterns específicos por banco español (BBVA, Santander, ING, Sabadell, CaixaBank, etc.).
  - Receptor de SMS-webhook (entrante de Twilio/SMSPool/proveedor SMS real del usuario).
  - Inyector Playwright en form 3DS (Visa/Mastercard challenge page).
  - Orquestador end-to-end: poll → match → inject → confirm.
"""
from __future__ import annotations
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import json_atomic
import otp_3ds  # módulo IMAP existente

ROOT = os.path.dirname(os.path.abspath(__file__))
WEBHOOK_PATH = os.path.join(ROOT, 'otp_3ds_webhook_inbox.json')
STATS_PATH = os.path.join(ROOT, 'otp_3ds_autofill_stats.json')
CONFIG_PATH = os.path.join(ROOT, 'otp_3ds_autofill_config.json')

# Patterns por banco (regex case-insensitive aplicado a subject + body).
# Cada banco tiene su frase típica + extracción del código de 6 dígitos.
BANK_PATTERNS: Dict[str, List[str]] = {
    'BBVA': [
        r'codigo[\s:]+(\d{6})',
        r'firma\s+digital[\s\S]{0,80}?(\d{6})',
        r'BBVA[\s\S]{0,80}?(\d{6})',
    ],
    'Santander': [
        r'(?:clave|codigo)[\s\S]{0,40}?(\d{6})',
        r'Santander[\s\S]{0,80}?(\d{6})',
        r'pago[\s\S]{0,40}?(\d{6})',
    ],
    'ING': [
        r'codigo[\s\S]{0,30}?(\d{6})',
        r'ING[\s\S]{0,80}?(\d{6})',
    ],
    'Sabadell': [
        r'Sabadell[\s\S]{0,80}?(\d{6})',
        r'autoriz[\s\S]{0,40}?(\d{6})',
    ],
    'CaixaBank': [
        r'CaixaBank[\s\S]{0,80}?(\d{6})',
        r'firma[\s\S]{0,40}?(\d{6})',
        r'compra[\s\S]{0,40}?(\d{6})',
    ],
    'Cajamar': [
        r'Cajamar[\s\S]{0,80}?(\d{6})',
    ],
    'Unicaja': [
        r'Unicaja[\s\S]{0,80}?(\d{6})',
    ],
    'Bankinter': [
        r'Bankinter[\s\S]{0,80}?(\d{6})',
        r'codigo[\s\S]{0,30}?(\d{6})',
    ],
    'OpenBank': [
        r'Openbank[\s\S]{0,80}?(\d{6})',
    ],
    'Revolut': [
        r'Revolut[\s\S]{0,80}?(\d{6,8})',
        r'verification\s+code[\s\S]{0,30}?(\d{6,8})',
    ],
    'N26': [
        r'N26[\s\S]{0,80}?(\d{6})',
    ],
    # Genéricos 3DS (Visa Secure / Mastercard ID Check)
    '3DS_VISA': [
        r'Visa[\s\S]{0,40}?(\d{6})',
        r'autenticaci[oó]n[\s\S]{0,40}?(\d{6})',
    ],
    '3DS_MC': [
        r'Mastercard[\s\S]{0,40}?(\d{6})',
        r'ID\s+Check[\s\S]{0,40}?(\d{6})',
    ],
}

DEFAULT_CONFIG = {
    'enabled': True,
    'mode': 'imap',  # imap | webhook | both
    'imap_poll_interval_s': 5,
    'banks_active': list(BANK_PATTERNS.keys()),
    'webhook_secret': '',  # opcional, para validar X-Webhook-Secret entrante
    'auto_inject': True,
    'inject_timeout_s': 60,
    'log_max': 100,
}

_lock = threading.Lock()


def _load_config() -> dict:
    cfg = json_atomic.load(CONFIG_PATH, default=DEFAULT_CONFIG)
    # Generar webhook_secret UUID4 al primer arranque si está vacío
    if not cfg.get('webhook_secret'):
        import uuid
        cfg['webhook_secret'] = uuid.uuid4().hex
        json_atomic.save(CONFIG_PATH, cfg)
    return cfg


def _save_config(cfg: dict) -> None:
    json_atomic.save(CONFIG_PATH, cfg)


def _load_webhook_inbox() -> List[dict]:
    return json_atomic.load(WEBHOOK_PATH, default=[])


def _save_webhook_inbox(d: List[dict]) -> None:
    json_atomic.save(WEBHOOK_PATH, d)


def _load_stats() -> dict:
    return json_atomic.load(STATS_PATH, default={
        'imap_polls': 0, 'imap_codes_found': 0,
        'webhook_received': 0, 'webhook_codes_found': 0,
        'injections_attempted': 0, 'injections_ok': 0,
        'last_at': None, 'errors': [],
    })


def _save_stats(d: dict) -> None:
    json_atomic.save(STATS_PATH, d)


def _bump(key: str, by: int = 1) -> None:
    with _lock:
        s = _load_stats()
        s[key] = (s.get(key, 0) or 0) + by
        s['last_at'] = datetime.now(timezone.utc).isoformat()
        _save_stats(s)


# ─────────────────── BIN → BANCO (mapping para auto-pattern) ───────────────────
# BIN ranges (primeros 6 dígitos de la tarjeta) → banco español típico.
# Source: EU/ES BIN database snapshot — ampliar al detectar nuevos.
BIN_TO_BANK = {
    # Santander
    "454617": "Santander", "454677": "Santander", "493764": "Santander",
    # BBVA
    "454393": "BBVA", "415625": "BBVA", "517397": "BBVA",
    # CaixaBank / La Caixa
    "454681": "CaixaBank", "517234": "CaixaBank", "549014": "CaixaBank",
    # Sabadell
    "454697": "Sabadell", "517231": "Sabadell",
    # ING España
    "454649": "ING", "454757": "ING",
    # Bankinter
    "454675": "Bankinter",
    # Cajamar
    "454601": "Cajamar",
    # Unicaja
    "454650": "Unicaja",
    # Openbank
    "454720": "OpenBank", "454721": "OpenBank",
    # Revolut
    "537818": "Revolut", "535122": "Revolut",
    # N26
    "535316": "N26",
}


def detect_bank_from_card(card_pan_or_bin: str) -> Optional[str]:
    """Devuelve el banco a partir del PAN completo o BIN (primeros 6).

    Útil al rellenar la tarjeta en checkout: detecta el banco emisor antes
    de que llegue el OTP, para usar SOLO los patterns del banco correcto y
    evitar falsos positivos cuando hay emails de varios bancos en la madre.
    """
    if not card_pan_or_bin:
        return None
    digits = re.sub(r"\D", "", str(card_pan_or_bin))
    if len(digits) < 6:
        return None
    bin6 = digits[:6]
    if bin6 in BIN_TO_BANK:
        return BIN_TO_BANK[bin6]
    # Fallback: probar BIN de 4 dígitos (algunos bancos comparten gama)
    bin4 = digits[:4]
    for full_bin, bank in BIN_TO_BANK.items():
        if full_bin.startswith(bin4):
            return bank
    return None


def patterns_for_bank(bank: Optional[str]) -> List[str]:
    """Devuelve los patterns del banco indicado, o todos si no se identifica."""
    if not bank:
        # Concatena todos
        out: List[str] = []
        for b, pats in BANK_PATTERNS.items():
            out.extend(pats)
        return out
    return BANK_PATTERNS.get(bank, [])


# Aliases por banco para detección segura (dominios + nombres alternativos)
BANK_ALIASES = {
    "BBVA": ["bbva"],
    "Santander": ["santander"],
    "ING": ["ing.es", "ing direct", "ing españa", "ingdirect"],
    "Sabadell": ["sabadell", "bsabadell"],
    "CaixaBank": ["caixabank", "lacaixa", "la caixa"],
    "Cajamar": ["cajamar"],
    "Unicaja": ["unicaja"],
    "Bankinter": ["bankinter"],
    "OpenBank": ["openbank"],
    "Revolut": ["revolut"],
    "N26": ["n26"],
    "3DS_VISA": ["visa secure", "visa.com", "verified by visa"],
    "3DS_MC": ["mastercard", "id check", "securecode"],
}


def _text_belongs_to_bank(text: str, bank: str) -> bool:
    """Heurística: el texto contiene el nombre del banco o un alias conocido."""
    if not text or not bank:
        return False
    low = text.lower()
    for alias in BANK_ALIASES.get(bank, [bank.lower()]):
        if alias.lower() in low:
            return True
    return False


def extract_code_for_bank(text: str, bank: Optional[str] = None,
                            strict: bool = True) -> Optional[str]:
    """Extrae el código aplicando SOLO los patterns del banco indicado.

    Args:
      text: cuerpo + subject del email (o SMS).
      bank: banco activo. Si None usa todos los patterns.
      strict: si True (default) requiere que el texto mencione el banco
        (evita falsos positivos cuando llegan emails de varios bancos a la
        misma cuenta IMAP). Si False acepta el primer match aunque no
        mencione el banco.
    """
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", " ", text)
    if bank and strict and not _text_belongs_to_bank(cleaned, bank):
        return None  # Texto no pertenece al banco armado
    pats = patterns_for_bank(bank)
    for pat in pats:
        try:
            m = re.search(pat, cleaned, re.IGNORECASE | re.DOTALL)
            if m:
                code = re.sub(r"\D", "", m.group(1))
                if 4 <= len(code) <= 8:
                    return code
        except re.error:
            continue
    return None


# Caché del último banco "armado" (set por el flow al rellenar PAN)
_armed_bank: Optional[str] = None


def arm_bank(bank_or_pan: str) -> Dict[str, Any]:
    """Indica qué banco/tarjeta se está usando en el checkout activo.

    Llama esto justo antes del 3DS challenge (el flow conoce la tarjeta).
    Acepta nombre de banco directo o PAN/BIN para auto-detect.
    """
    global _armed_bank
    if not bank_or_pan:
        _armed_bank = None
        return {"ok": True, "armed": None}
    if bank_or_pan in BANK_PATTERNS:
        _armed_bank = bank_or_pan
    else:
        _armed_bank = detect_bank_from_card(bank_or_pan)
    return {"ok": True, "armed": _armed_bank,
             "patterns_count": len(patterns_for_bank(_armed_bank))}


def get_armed_bank() -> Optional[str]:
    return _armed_bank


def disarm_bank() -> Dict[str, Any]:
    """Limpia el banco armado (vuelve a usar todos los patterns)."""
    global _armed_bank
    prev = _armed_bank
    _armed_bank = None
    return {"ok": True, "previous": prev}


# ─────────────────── 1. IMAP POLL CON PATTERNS BANCARIOS ───────────────────

def get_active_patterns() -> List[str]:
    """Devuelve la lista plana de patterns activos según `banks_active` config."""
    cfg = _load_config()
    active = set(cfg.get('banks_active', list(BANK_PATTERNS.keys())))
    out: List[str] = []
    for bank, pats in BANK_PATTERNS.items():
        if bank in active:
            out.extend(pats)
    return out


def poll_imap_once() -> Dict[str, Any]:
    """Una pasada IMAP combinando patterns bancarios. Reusa otp_3ds.check_inbox()
    pero sobrescribiendo `patterns` runtime con los activos."""
    cfg = otp_3ds.get_config()
    base_patterns = cfg.get('patterns', [])
    # Combina los patterns bancarios + los del usuario configurados en otp_3ds.json
    extended = list(set(base_patterns) | set(get_active_patterns()))
    # No alterar config persistente — usar override en memoria
    cfg_backup = dict(cfg)
    cfg['patterns'] = extended
    otp_3ds.save_config({'patterns': extended})
    try:
        res = otp_3ds.check_inbox()
        _bump('imap_polls')
        if res.get('found'):
            _bump('imap_codes_found', len(res['found']))
        return res
    finally:
        otp_3ds.save_config({'patterns': cfg_backup.get('patterns', [])})


# ─────────────────── 2. SMS WEBHOOK RECEIVER ───────────────────

def receive_webhook(payload: dict, headers: Optional[dict] = None) -> Dict[str, Any]:
    """Recibe un POST entrante de Twilio/SMSPool/usuario. Extrae el código de SMS body
    aplicando patterns activos + cualquier patrón de 6 dígitos como fallback.

    Payload esperado (formato unificado, mapear desde Twilio si aplica):
      {From: "+34xxx", Body: "Tu código BBVA: 123456", To: "+34yyy"}
    También Twilio formato: {'From': ..., 'Body': ...}
    """
    cfg = _load_config()
    secret = cfg.get('webhook_secret', '')
    if secret:
        provided = (headers or {}).get('X-Webhook-Secret') or (headers or {}).get('x-webhook-secret')
        if provided != secret:
            return {'ok': False, 'error_key': 'otp3ds.webhook_unauthorized'}

    body_text = (payload.get('Body') or payload.get('body') or
                 payload.get('text') or payload.get('message') or '')
    sender = (payload.get('From') or payload.get('from') or
              payload.get('sender') or '').strip()
    if not body_text:
        return {'ok': False, 'error_key': 'otp3ds.webhook_no_body'}

    code = None
    bank_matched = None
    for bank in cfg.get('banks_active', list(BANK_PATTERNS.keys())):
        for pat in BANK_PATTERNS.get(bank, []):
            try:
                m = re.search(pat, body_text, re.I)
                if m:
                    code = m.group(1); bank_matched = bank; break
            except re.error: continue
        if code: break
    # Fallback: cualquier secuencia 6 dígitos
    if not code:
        m = re.search(r'\b(\d{6})\b', body_text)
        if m: code = m.group(1)

    entry = {
        'code': code, 'source': 'webhook', 'bank': bank_matched,
        'from': sender[:120], 'body': body_text[:300],
        'ts': datetime.now(timezone.utc).isoformat(),
    }
    inbox = _load_webhook_inbox()
    inbox.append(entry)
    inbox = inbox[-(cfg.get('log_max', 100)):]
    _save_webhook_inbox(inbox)
    _bump('webhook_received')
    if code: _bump('webhook_codes_found')

    # También guardar en log compartido de otp_3ds para que `wait_for_code()` lo vea
    if code:
        otp_3ds._log({  # type: ignore
            'code': code, 'source': 'sms_webhook', 'bank': bank_matched,
            'from': sender[:120], 'subject': '(SMS)', 'ts': entry['ts'],
            'uid': f"webhook_{int(time.time()*1000)}",
        })
        with otp_3ds._cond:  # type: ignore
            otp_3ds._cond.notify_all()  # type: ignore

    return {'ok': True, 'code_found': bool(code), 'code': code,
            'bank': bank_matched, 'sender': sender[:80]}


# ─────────────────── 3. INJECT EN FORM 3DS (Playwright) ───────────────────

# Selectores comunes en challenge pages 3DS (Visa Secure / MC ID Check).
_3DS_INPUT_SELECTORS = [
    'input[name*="otp" i]',
    'input[name*="code" i]',
    'input[name*="challenge" i]',
    'input[id*="otp" i]',
    'input[id*="code" i]',
    'input[autocomplete="one-time-code"]',
    'input[type="tel"][maxlength="6"]',
    'input[type="text"][maxlength="6"]',
    'input[inputmode="numeric"]',
]

_3DS_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button[id*="submit" i]',
    'button[id*="continue" i]',
    'button:has-text("Continuar")',
    'button:has-text("Aceptar")',
    'button:has-text("Confirmar")',
    'button:has-text("Submit")',
    'button:has-text("Verify")',
]


def inject_into_3ds_form(page, code: str, *,
                         submit: bool = True, timeout_ms: int = 8000) -> Dict[str, Any]:
    """Inyecta `code` en el form 3DS visible en `page` (Playwright Page).
    Probaba múltiples selectores hasta encontrar input visible.
    Returns: {ok, selector_used, submitted}."""
    if not page or not code:
        return {'ok': False, 'error_key': 'otp3ds.inject_no_page_or_code'}
    inp_used = None
    for sel in _3DS_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout_ms // len(_3DS_INPUT_SELECTORS)):
                loc.click()
                loc.fill('')
                loc.fill(str(code))
                inp_used = sel
                break
        except Exception:
            continue
    if not inp_used:
        _bump('injections_attempted')
        return {'ok': False, 'error_key': 'otp3ds.inject_input_not_found'}

    submitted = False
    if submit:
        try:
            page.wait_for_timeout(400)
            for s in _3DS_SUBMIT_SELECTORS:
                try:
                    btn = page.locator(s).first
                    if btn.is_visible(timeout=300):
                        btn.click()
                        submitted = True
                        break
                except Exception: continue
            if not submitted:
                # fallback: Enter en el input
                try:
                    page.locator(inp_used).first.press('Enter')
                    submitted = True
                except Exception: pass
        except Exception: pass

    _bump('injections_attempted')
    if submitted: _bump('injections_ok')
    return {'ok': True, 'selector_used': inp_used, 'submitted': submitted}


# ─────────────────── 4. ORCHESTRATOR END-TO-END ───────────────────

def wait_and_inject(page, *, timeout_s: int = 60,
                    bank_hint: Optional[str] = None) -> Dict[str, Any]:
    """Espera el OTP (IMAP poll + webhook inbox) y lo inyecta en `page`.
    Returns: {ok, code, source, bank, inject_meta, elapsed_s}."""
    started = time.time()
    cfg = _load_config()
    poll_interval = cfg.get('imap_poll_interval_s', 5)
    deadline = started + timeout_s

    last_uids = set(x.get('uid') for x in otp_3ds.get_log(50))

    while time.time() < deadline:
        # IMAP poll
        if cfg.get('mode') in ('imap', 'both'):
            try:
                res = poll_imap_once()
                for entry in (res.get('found') or []):
                    if entry.get('uid') in last_uids: continue
                    code = entry.get('code')
                    if not code: continue
                    inject = inject_into_3ds_form(page, code)
                    return {
                        'ok': True, 'code': code, 'source': 'imap',
                        'bank': bank_hint, 'inject': inject,
                        'elapsed_s': round(time.time() - started, 2),
                    }
            except Exception: pass

        # Webhook inbox poll (solo entries nuevos con código)
        if cfg.get('mode') in ('webhook', 'both'):
            inbox = _load_webhook_inbox()
            for entry in reversed(inbox):
                if entry.get('code') and entry['ts'] > datetime.fromtimestamp(
                        started, tz=timezone.utc).isoformat():
                    code = entry['code']
                    inject = inject_into_3ds_form(page, code)
                    return {
                        'ok': True, 'code': code, 'source': 'webhook',
                        'bank': entry.get('bank'), 'inject': inject,
                        'elapsed_s': round(time.time() - started, 2),
                    }
        time.sleep(poll_interval)

    return {'ok': False, 'error_key': 'otp3ds.timeout',
            'error_args': {'timeout_s': timeout_s},
            'elapsed_s': round(time.time() - started, 2)}


# ─────────────────── 5. MANAGEMENT ───────────────────

def get_status(reveal_secret: bool = False) -> Dict[str, Any]:
    cfg = _load_config()
    stats = _load_stats()
    inbox = _load_webhook_inbox()
    secret = cfg.get('webhook_secret', '')
    return {
        'ok': True,
        'enabled': cfg.get('enabled', True),
        'mode': cfg.get('mode', 'imap'),
        'banks_active': cfg.get('banks_active', []),
        'banks_available': list(BANK_PATTERNS.keys()),
        'imap_poll_interval_s': cfg.get('imap_poll_interval_s', 5),
        'auto_inject': cfg.get('auto_inject', True),
        'webhook_endpoint': '/api/otp3ds/webhook',
        'webhook_secret_set': bool(secret),
        'webhook_secret_masked': (secret[:4] + '…' + secret[-4:]) if secret else '',
        'webhook_secret': secret if reveal_secret else None,
        'webhook_inbox_size': len(inbox),
        'webhook_last': inbox[-3:] if inbox else [],
        'stats': stats,
    }


def regenerate_webhook_secret() -> Dict[str, Any]:
    """Regenera el secret UUID4 (rotación). Devuelve el nuevo en claro."""
    import uuid
    cfg = _load_config()
    new = uuid.uuid4().hex
    cfg['webhook_secret'] = new
    _save_config(cfg)
    return {'ok': True, 'webhook_secret': new}


def update_config(patch: dict) -> Dict[str, Any]:
    cfg = _load_config()
    allowed = {'enabled', 'mode', 'imap_poll_interval_s', 'banks_active',
               'webhook_secret', 'auto_inject', 'inject_timeout_s', 'log_max'}
    cfg.update({k: v for k, v in patch.items() if k in allowed})
    _save_config(cfg)
    return {'ok': True, 'config': cfg}


def test_pattern(text: str, bank: Optional[str] = None) -> Dict[str, Any]:
    """Test helper: aplica los patterns de un banco (o todos) sobre `text`."""
    matches = []
    pools = {bank: BANK_PATTERNS[bank]} if bank in BANK_PATTERNS else BANK_PATTERNS
    for b, pats in pools.items():
        for p in pats:
            try:
                m = re.search(p, text, re.I)
                if m: matches.append({'bank': b, 'pattern': p, 'code': m.group(1)})
            except re.error: pass
    return {'ok': True, 'matches': matches, 'count': len(matches)}
