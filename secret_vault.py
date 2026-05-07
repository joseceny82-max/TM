"""secret_vault — cifrado at-rest de campos sensibles (PCI-DSS / GDPR).

Cifra campos críticos (PAN, CVV, password TM, IMAP password, API keys) usando
Fernet (AES-128-CBC + HMAC-SHA256) con clave derivada de TM_SECRET_KEY.

Uso transparente:
  from secret_vault import encrypt_field, decrypt_field, vault_encrypt_dict

  # Al guardar:
  card['number'] = encrypt_field(card['number'])

  # Al leer:
  card['number'] = decrypt_field(card['number'])  # si no está cifrado, devuelve igual

Migración:
  - `migrate_cards_to_encrypted()` y `migrate_accounts_to_encrypted()`
  - Backup automático del archivo original con sufijo .pre_encrypt_<timestamp>
  - Idempotente: si ya está cifrado, no re-cifra

Activación: env var `TM_ENCRYPT_AT_REST=1` (default 0 = backward compat).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

PROYECTO = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("secret_vault")

# Prefijo identificador (los valores cifrados empiezan así)
_PREFIX = "ENC1::"

# Lista de claves sensibles por archivo (PCI-DSS + GDPR)
SENSITIVE_KEYS_CARDS = ["number", "cvv", "card_number", "card_cvv", "pan"]
SENSITIVE_KEYS_ACCOUNTS = ["tm_password", "password", "contact_password",
                            "email_password", "imap_password",
                            "api_key", "consumer_secret"]


def _get_master_key() -> bytes:
    """Deriva la clave Fernet desde el SECRET_KEY persistente del dashboard.

    Si TM_SECRET_KEY env existe, la usa; sino lee `secrets/.session_key`.
    """
    env = os.environ.get("TM_SECRET_KEY", "").strip()
    src = env
    if not src:
        path = os.path.join(PROYECTO, "secrets", ".session_key")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    src = f.read().strip()
            except Exception:
                pass
    if not src or len(src) < 16:
        raise RuntimeError("secret_vault: no master key disponible")
    # Derive Fernet key (32 bytes b64) desde HKDF
    derived = hashlib.sha256(src.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(derived)


_FERNET = None


def _fernet():
    global _FERNET
    if _FERNET is None:
        try:
            from cryptography.fernet import Fernet
            _FERNET = Fernet(_get_master_key())
        except ImportError:
            raise RuntimeError("cryptography package required (pip install cryptography)")
    return _FERNET


def is_encrypted(v: Any) -> bool:
    """True si el valor ya está cifrado por este módulo."""
    return isinstance(v, str) and v.startswith(_PREFIX)


def encrypt_field(value: Any) -> str:
    """Cifra un valor (cualquier tipo serializable a string)."""
    if value is None or value == "":
        return value
    if is_encrypted(value):
        return value
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    token = _fernet().encrypt(s.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt_field(value: Any) -> Any:
    """Descifra. Si no está cifrado o falla, devuelve original (graceful)."""
    if not is_encrypted(value):
        return value
    try:
        token = value[len(_PREFIX):]
        plain = _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
        return plain
    except Exception:
        return value


def vault_encrypt_dict(d: Dict[str, Any], sensitive_keys: List[str]) -> Dict[str, Any]:
    """Cifra in-place las claves sensibles de un dict."""
    for k in sensitive_keys:
        if k in d and d[k] not in (None, ""):
            d[k] = encrypt_field(d[k])
    return d


def vault_decrypt_dict(d: Dict[str, Any], sensitive_keys: List[str]) -> Dict[str, Any]:
    """Descifra in-place las claves sensibles."""
    for k in sensitive_keys:
        if k in d and is_encrypted(d[k]):
            d[k] = decrypt_field(d[k])
    return d


# ──────────────── MIGRACIONES ────────────────

def _backup_file(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    bak = path + f".pre_encrypt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy(path, bak)
    return bak


def migrate_cards_to_encrypted(cards_path: Optional[str] = None) -> Dict[str, Any]:
    """Cifra cards.json in-place (backup automático)."""
    p = cards_path or os.path.join(PROYECTO, "datos", "cuentas", "cards.json")
    if not os.path.exists(p):
        return {"ok": False, "error": "cards.json not found"}
    bak = _backup_file(p)
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    cards = d.get("cards", []) if isinstance(d, dict) else d
    n_encrypted = 0
    for c in cards:
        for k in SENSITIVE_KEYS_CARDS:
            if k in c and not is_encrypted(c[k]):
                c[k] = encrypt_field(c[k])
                n_encrypted += 1
    try:
        import json_atomic
        json_atomic.save(p, d)
    except Exception:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    return {"ok": True, "cards": len(cards), "encrypted_fields": n_encrypted, "backup": bak}


def migrate_accounts_to_encrypted(accounts_path: Optional[str] = None) -> Dict[str, Any]:
    """Cifra passwords en accounts.json in-place."""
    p = accounts_path or os.path.join(PROYECTO, "datos", "cuentas", "accounts.json")
    if not os.path.exists(p):
        return {"ok": False, "error": "accounts.json not found"}
    bak = _backup_file(p)
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    accounts = d.get("accounts", []) if isinstance(d, dict) else d
    n_encrypted = 0
    for a in accounts:
        for k in SENSITIVE_KEYS_ACCOUNTS:
            if k in a and not is_encrypted(a[k]):
                a[k] = encrypt_field(a[k])
                n_encrypted += 1
    try:
        import json_atomic
        json_atomic.save(p, d)
    except Exception:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    return {"ok": True, "accounts": len(accounts), "encrypted_fields": n_encrypted, "backup": bak}


def decrypt_all_cards(cards_path: Optional[str] = None) -> Dict[str, Any]:
    """Reverso: descifra cards.json. Para emergencias o desactivación."""
    p = cards_path or os.path.join(PROYECTO, "datos", "cuentas", "cards.json")
    if not os.path.exists(p):
        return {"ok": False, "error": "not_found"}
    bak = _backup_file(p)
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    cards = d.get("cards", []) if isinstance(d, dict) else d
    n = 0
    for c in cards:
        for k in SENSITIVE_KEYS_CARDS:
            if k in c and is_encrypted(c[k]):
                c[k] = decrypt_field(c[k])
                n += 1
    try:
        import json_atomic
        json_atomic.save(p, d)
    except Exception:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    return {"ok": True, "decrypted": n, "backup": bak}


def vault_status() -> Dict[str, Any]:
    """Reporta el estado del vault: cuántos campos están cifrados."""
    out = {"enabled": os.environ.get("TM_ENCRYPT_AT_REST", "0") == "1"}
    for name, fname, sensitive in [
        ("cards", "datos/cuentas/cards.json", SENSITIVE_KEYS_CARDS),
        ("accounts", "datos/cuentas/accounts.json", SENSITIVE_KEYS_ACCOUNTS),
    ]:
        path = os.path.join(PROYECTO, fname)
        info = {"path": fname, "exists": os.path.exists(path)}
        if info["exists"]:
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                items = d.get(name, []) if isinstance(d, dict) else d
                if not isinstance(items, list):
                    items = []
                total = 0
                encrypted = 0
                for it in items:
                    for k in sensitive:
                        if k in it:
                            total += 1
                            if is_encrypted(it[k]):
                                encrypted += 1
                info["total_sensitive_fields"] = total
                info["encrypted_fields"] = encrypted
                info["coverage"] = round(100.0 * encrypted / max(1, total), 1)
                info["items_count"] = len(items)
            except Exception as e:
                info["error"] = str(e)[:120]
        out[name] = info
    return out


def self_test() -> Dict[str, Any]:
    out = {"checks": {}}
    try:
        # Test cifrar/descifrar
        plain = "TEST_VALUE_xyz_123!@#"
        ct = encrypt_field(plain)
        out["checks"]["encrypts"] = is_encrypted(ct) and ct != plain
        out["checks"]["decrypts"] = decrypt_field(ct) == plain
        out["checks"]["idempotent_encrypt"] = encrypt_field(ct) == ct
        out["checks"]["graceful_decrypt_plain"] = decrypt_field(plain) == plain
        # Dict
        d = {"number": "4111-1111-1111-1111", "cvv": "123", "name": "test"}
        d2 = vault_encrypt_dict(d.copy(), SENSITIVE_KEYS_CARDS)
        out["checks"]["dict_encrypt"] = is_encrypted(d2["number"]) and is_encrypted(d2["cvv"])
        out["checks"]["dict_keep_non_sensitive"] = d2["name"] == "test"
        d3 = vault_decrypt_dict(d2, SENSITIVE_KEYS_CARDS)
        out["checks"]["dict_decrypt"] = d3["number"] == "4111-1111-1111-1111"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["ok"] = all(v is True for v in out["checks"].values()) and not out.get("error")
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
    print()
    print("Status:")
    print(json.dumps(vault_status(), indent=2))
