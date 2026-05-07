"""Aplica los resultados de _run149_progress.json a accounts.json.

Usado UNA VEZ después del batch standalone porque el módulo cargado en
memoria del script `_run_149_validation.py` no incluía el fix de persistencia.

Reglas:
- valid=True  → credentials_verified=True, pending_user_review=False, validated_at, validation_reason
- valid=False con razón terminal → pending_user_review=True (NO cambiar status), validated_at, validation_reason
- valid=False por hard_timeout / runner_error → solo registrar last_failure (no marca review)
"""
from __future__ import annotations
import os, sys, json
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import account_manager as am  # noqa: E402

PROGRESS = os.path.join(HERE, "datos", "_run149_progress.json")

INVALID_TERMINAL = (
    "bad_credentials",
    "session_invalid_redirected_to_login",
    "account_mismatch",
    "fresh_login_form_not_reachable",
    "no_password_form",
    "no_login_form_visible",
    "session_persistent_unable_to_force_fresh",
)


def main():
    s = json.load(open(PROGRESS, encoding="utf-8"))
    results = s.get("results", [])
    print(f"[apply] resultados a aplicar: {len(results)} (valid={s['valid']} invalid={s['invalid']} total={s['total']})")

    counters = {
        "valid_persisted": 0,
        "invalid_marked_for_review": 0,
        "infra_failures_skipped": 0,
        "no_id": 0,
    }
    now = datetime.now().isoformat()
    for r in results:
        aid = r.get("account_id")
        if not aid:
            counters["no_id"] += 1
            continue
        valid = r.get("valid")
        reason = r.get("reason") or ""
        if valid is True:
            am.account_manager.update_account(aid, {
                "validated_at": now,
                "validation_reason": reason,
                "credentials_verified": True,
                "pending_user_review": False,
            })
            counters["valid_persisted"] += 1
        elif valid is False and reason in INVALID_TERMINAL:
            am.account_manager.update_account(aid, {
                "validated_at": now,
                "validation_reason": reason,
                "credentials_verified": False,
                "pending_user_review": True,   # marcar SIN mover
            })
            counters["invalid_marked_for_review"] += 1
        else:
            # hard_timeout / runner_error / no_morelogin_profile / etc — infra
            counters["infra_failures_skipped"] += 1

    print("[apply] aplicado:")
    for k, v in counters.items():
        print(f"   {k:30s} = {v}")


if __name__ == "__main__":
    main()
