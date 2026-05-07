"""register_verify.py — Verificación post-registro de cuentas TM.

Tras un registro exitoso (o sospecha de éxito), verifica que la cuenta
está correctamente persistida en TODOS los lugares donde debe estar:

  1. accounts.json                       — record principal
  2. cuentas_tarjetas.csv                — sync CSV con tarjeta
  3. tm_registrations.json               — log de registros
  4. plantilla_tm.csv                    — debe estar REMOVIDA
  5. registered_archive.csv              — debe estar AÑADIDA al archive
  6. storage_state file                  — sesión Playwright guardada
  7. cookies file                        — cookies persistidas

Uso:
    import register_verify
    r = register_verify.verify_account(email='saucer@icloud.com')
    if not r['ok']:
        print('FALLOS:', r['failures'])

API:
    verify_account(email | acc_id) → {ok, checks, failures, account?}
    verify_batch(emails) → {ok, total, passed, failed_list}
    self_test() → smoke test
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROYECTO = Path(__file__).resolve().parent

# Paths esperados (ajustar si cambia layout)
ACCOUNTS_JSON = PROYECTO / "datos" / "cuentas" / "accounts.json"
CUENTAS_CSV = PROYECTO / "datos" / "cuentas" / "cuentas_tarjetas.csv"
TM_REGS = PROYECTO / "tm_registrations.json"
PLANTILLA = PROYECTO / "plantilla_tm.csv"
ARCHIVE = PROYECTO / "datos" / "buy" / "runs" / "registered_archive.csv"


def _load_accounts() -> List[Dict[str, Any]]:
    if not ACCOUNTS_JSON.exists():
        return []
    try:
        d = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else d.get("accounts", [])
    except Exception:
        return []


def _csv_emails_and_data() -> Dict[str, Dict[str, str]]:
    """Devuelve {tm_email: row_dict}."""
    if not CUENTAS_CSV.exists():
        return {}
    out = {}
    try:
        with open(CUENTAS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                e = (r.get("tm_email") or r.get("email") or "").strip()
                if e:
                    out[e] = r
    except Exception:
        pass
    return out


def _tm_reg_ids() -> set:
    if not TM_REGS.exists():
        return set()
    try:
        d = json.loads(TM_REGS.read_text(encoding="utf-8"))
        items = d.get("items", d) if isinstance(d, dict) else d
        if isinstance(items, list):
            return {i.get("id") for i in items if i.get("id")}
    except Exception:
        pass
    return set()


def _plantilla_emails() -> set:
    if not PLANTILLA.exists():
        return set()
    try:
        with open(PLANTILLA, encoding="utf-8") as f:
            return {(r.get("Email_TM") or "").strip()
                     for r in csv.DictReader(f)}
    except Exception:
        return set()


def _archive_emails() -> set:
    if not ARCHIVE.exists():
        return set()
    try:
        with open(ARCHIVE, encoding="utf-8") as f:
            return {(r.get("Email_TM") or "").strip()
                     for r in csv.DictReader(f)}
    except Exception:
        return set()


def verify_account(email: Optional[str] = None,
                     acc_id: Optional[str] = None,
                     tm_reg_id: Optional[str] = None) -> Dict[str, Any]:
    """Verifica que una cuenta está bien persistida.

    Args:
      email: tm_email
      acc_id: id interno de accounts.json
      tm_reg_id: opcional, id del item en tm_registrations.json

    Devuelve {ok, checks, failures, account, summary}.
    """
    if not email and not acc_id:
        return {"ok": False, "error": "email_or_acc_id_required"}

    accs = _load_accounts()
    csv_data = _csv_emails_and_data()
    reg_ids = _tm_reg_ids()
    plantilla_em = _plantilla_emails()
    archive_em = _archive_emails()

    # Encontrar la cuenta
    account = None
    if acc_id:
        account = next((a for a in accs if a.get("id") == acc_id), None)
    if not account and email:
        account = next((a for a in accs
                          if a.get("tm_email") == email
                          or a.get("email") == email), None)
    if not account:
        return {
            "ok": False,
            "error": "account_not_found",
            "checked": {"email": email, "acc_id": acc_id},
            "failures": [
                f"accounts.json no contiene email={email!r} ni id={acc_id!r}"
            ],
        }
    # Email canónico tras encontrar la cuenta
    em = email or account.get("tm_email") or account.get("email")

    # Storage state file existe?
    ss_file = account.get("storage_state_file") or ""
    has_storage = bool(ss_file and Path(ss_file).exists())

    # Cookies file existe?
    ck_file = account.get("cookies_file") or ""
    has_cookies = bool(ck_file and Path(ck_file).exists())

    # Status
    status = (account.get("status") or "").lower()
    is_active = status in ("active", "")

    checks = {
        "accounts_json_present":      True,  # ya lo encontramos
        "tm_email_field_filled":      bool(account.get("tm_email")),
        "tm_password_field_filled":   bool(account.get("tm_password")),
        "phone_persisted":            bool(account.get("phone")),
        "phone_verified_at":          bool(account.get("phone_verified_at")),
        "registration_completed_at":  bool(account.get("registration_completed_at")),
        "morelogin_profile_linked":   bool(account.get("morelogin_profile")),
        "status_active":              is_active,
        "purchase_ready_flag":        bool(account.get("purchase_ready")),
        "csv_email_present":          em in csv_data,
        "csv_card_number_present":    bool(csv_data.get(em, {}).get("card_number")),
        "csv_card_cvv_present":       bool(csv_data.get(em, {}).get("card_cvv")),
        "plantilla_REMOVED":          em not in plantilla_em,
        "archive_ADDED":              em in archive_em,
        "storage_state_file_exists":  has_storage,
        "cookies_file_exists":        has_cookies,
    }
    if tm_reg_id:
        checks["tm_registrations_logged"] = tm_reg_id in reg_ids

    failures = [k for k, v in checks.items() if not v]
    summary = {
        "email": em,
        "acc_id": account.get("id"),
        "phone": account.get("phone", "")[:14],
        "morelogin_profile": account.get("morelogin_profile"),
        "created_at": (account.get("created_at") or "")[:19],
        "status": status,
    }

    return {
        "ok": len(failures) == 0,
        "checks": checks,
        "failures": failures,
        "passed": len(checks) - len(failures),
        "total": len(checks),
        "account": account,
        "summary": summary,
    }


def verify_batch(emails: List[str]) -> Dict[str, Any]:
    """Verifica una lista de cuentas. Devuelve resumen + detalle de fallos."""
    results = []
    for em in emails:
        r = verify_account(email=em)
        results.append({"email": em, **r})
    passed = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": len(failed) == 0,
        "total": len(emails),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "passed": [{"email": r["email"],
                      "summary": r.get("summary", {})}
                     for r in passed],
        "failed": [{"email": r["email"],
                      "failures": r.get("failures", [r.get("error")])}
                     for r in failed],
    }


def verify_recent(hours: int = 1) -> Dict[str, Any]:
    """Verifica TODAS las cuentas creadas en las últimas N horas.

    Útil tras un batch de registros para auditar persistencia masiva.
    """
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(hours=hours)
    accs = _load_accounts()
    recent_emails = []
    for a in accs:
        ts = a.get("created_at") or a.get("registration_completed_at") or ""
        try:
            # Normalizar TZ — comparamos naive
            t = datetime.fromisoformat(ts.replace("Z", "+00:00").split("+")[0])
            if t > cutoff:
                em = a.get("tm_email") or a.get("email")
                if em:
                    recent_emails.append(em)
        except Exception:
            continue
    return verify_batch(recent_emails)


def self_test() -> Dict[str, Any]:
    """Smoke test del módulo (sin tocar registros reales)."""
    out = {"ok": True, "checks": {}}
    # 1. verify_account sin args devuelve error claro
    r = verify_account()
    out["checks"]["empty_args_returns_error"] = (r.get("ok") is False
                                                     and r.get("error") == "email_or_acc_id_required")
    # 2. cuenta inexistente devuelve account_not_found
    r = verify_account(email="__nonexistent__@test.invalid")
    out["checks"]["nonexistent_returns_not_found"] = (
        r.get("error") == "account_not_found")
    # 3. verify_batch vacío
    r = verify_batch([])
    out["checks"]["empty_batch_ok"] = (r.get("total") == 0)
    # 4. paths existen al menos parcialmente
    out["checks"]["accounts_json_loadable"] = isinstance(_load_accounts(),
                                                            list)
    # 5. csv loadable
    out["checks"]["csv_loadable"] = isinstance(_csv_emails_and_data(), dict)
    out["passed"] = sum(1 for v in out["checks"].values() if v)
    out["total"] = len(out["checks"])
    out["ok"] = out["passed"] == out["total"]
    return out


def get_info() -> Dict[str, Any]:
    return {
        "module": "register_verify",
        "purpose": "Post-register persistence verification",
        "checks_per_account": 17,
        "endpoints": [
            "GET /api/register/verify/<email>",
            "GET /api/register/verify-recent?hours=N",
        ],
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if target == "--recent":
            hours = int(sys.argv[2]) if len(sys.argv) > 2 else 1
            r = verify_recent(hours=hours)
        elif "@" in target:
            r = verify_account(email=target)
        else:
            r = verify_account(acc_id=target)
        print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
    else:
        # Self-test
        r = self_test()
        print(json.dumps(r, indent=2))
