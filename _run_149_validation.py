"""Standalone batch validator para las 149 cuentas restauradas.

Bypass del dashboard (cuyo runner está colgado). Ejecuta `validate_account`
de forma directa con `ThreadPoolExecutor` y persiste resultados en
`accounts.json` mediante `account_manager`.

Opciones aplicadas (alineadas con la última instrucción del usuario):
    verifySession=True
    verifyPassword=False
    bumpToRegistrations=False    ← user pidió NO mover
    markForReview=True            ← marcar pending_user_review
    clearHistory=True             ← limpiar historial al cerrar
    updateStorage=False
"""
from __future__ import annotations
import os, sys, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime

# Asegurar import desde proyecto/
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import account_validator as av           # noqa: E402
import account_manager as am             # noqa: E402

PROYECTO = HERE
JOBS_DIR = os.path.join(PROYECTO, "datos")
RESULT_PATH = os.path.join(JOBS_DIR, "_run149_progress.json")
PER_ACCOUNT_TIMEOUT_S = 180


def main():
    ids = json.load(open("C:/Users/user/_split.json", encoding="utf-8"))["bulk_move"]
    accs = am.account_manager.list_accounts()
    by_id = {a.get("id"): a for a in accs}
    targets = [by_id[i] for i in ids if i in by_id and by_id[i].get("status") == "active"]
    print(f"[run149] starting validation. targets={len(targets)} (de {len(ids)})")

    options = {
        "verifySession": True,
        "verifyPassword": False,
        "clearHistory": True,
        "bumpToRegistrations": False,
        "updateStorage": False,
        "skipRecentlyVerified": False,
        "markForReview": True,
    }
    used_ips = set()
    used_profiles = set()
    state = {
        "total": len(targets),
        "done": 0,
        "valid": 0,
        "invalid": 0,
        "started_at": datetime.now().isoformat(),
        "results": [],
    }

    def save_state():
        try:
            with open(RESULT_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"[run149] save_state ERR: {e}")

    save_state()

    def _validate_one(acc, slot):
        try:
            return av.validate_account(
                acc, slot=slot,
                force_password_test=False,
                options=options,
                used_ips_in_job=used_ips,
                used_profiles_in_job=used_profiles,
            )
        except Exception as e:
            return {
                "account_id": acc.get("id"),
                "tm_email": acc.get("tm_email"),
                "valid": False,
                "reason": f"runner_error: {type(e).__name__}: {str(e)[:80]}",
                "elapsed_s": 0,
            }

    max_parallel = 2
    with ThreadPoolExecutor(max_workers=max_parallel,
                              thread_name_prefix="run149") as ex:
        future_to_acc = {}
        for i, acc in enumerate(targets):
            slot = (i % max_parallel) + 1
            fut = ex.submit(_validate_one, acc, slot)
            future_to_acc[fut] = acc
        print(f"[run149] all_submitted={len(future_to_acc)}")

        for fut in as_completed(future_to_acc):
            acc = future_to_acc[fut]
            try:
                res = fut.result(timeout=PER_ACCOUNT_TIMEOUT_S)
            except FuturesTimeout:
                res = {
                    "account_id": acc.get("id"),
                    "tm_email": acc.get("tm_email"),
                    "valid": False,
                    "reason": f"hard_timeout_{PER_ACCOUNT_TIMEOUT_S}s",
                    "elapsed_s": PER_ACCOUNT_TIMEOUT_S,
                }
            except Exception as e:
                res = {
                    "account_id": acc.get("id"),
                    "tm_email": acc.get("tm_email"),
                    "valid": False,
                    "reason": f"future_error: {type(e).__name__}",
                }
            state["results"].append(res)
            state["done"] += 1
            if res.get("valid"):
                state["valid"] += 1
            else:
                state["invalid"] += 1
            print(f"[run149] {state['done']}/{state['total']} "
                  f"slot~ valid={res.get('valid')} reason={res.get('reason')} "
                  f"acc={res.get('tm_email')}")
            if state["done"] % 5 == 0:
                save_state()

    state["finished_at"] = datetime.now().isoformat()
    save_state()
    print(f"[run149] DONE valid={state['valid']} invalid={state['invalid']} "
          f"of total={state['total']}")


if __name__ == "__main__":
    main()
