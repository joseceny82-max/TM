"""tm_real_purchase — wrapper que delega la compra REAL en _compra_parallel.py.

Conecta el orquestador `tm_purchase_flow.py` (steps 7-9) con el script standalone
`_compra_parallel.py` que ya realiza el flow real contra TM (login, captcha, cart,
checkout, pago) en modo live. El orquestador llama a `run_real_purchase(ctx)` UNA
vez y este módulo:
  1. Lanza `_compra_parallel.py` como subprocess con args derivados de ctx
  2. Espera al término (timeout configurable)
  3. Parsea el log slotN para extraer outcome (success/error/code)

Devuelve estructura compatible con los step_* del orquestador.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

PROYECTO = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(PROYECTO, "_compra_with_swap.py")
DEFAULT_TIMEOUT_S = 600  # 10 min máximo por intento


def _slot_log_path(slot: int) -> str:
    return os.path.join(PROYECTO, f"_compra_parallel_slot{slot}.log")


def _slot_outcome_path(slot: int) -> str:
    return os.path.join(PROYECTO, f"_compra_parallel_slot{slot}.outcome.json")


def _read_outcome_json(slot: int) -> Optional[Dict[str, Any]]:
    """Lee `_compra_parallel_slot{N}.outcome.json` si existe.

    Este es el camino preferido — IPC estructurado entre wrapper y subprocess.
    """
    path = _slot_outcome_path(slot)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _parse_outcome_from_log(slot: int, started_at: float) -> Dict[str, Any]:
    """[FALLBACK] Lee últimas líneas del log si outcome.json no existe.

    Mantenido por compatibilidad: si el subprocess crashea sin escribir
    outcome.json, intentamos extraer información del log.
    """
    path = _slot_log_path(slot)
    out: Dict[str, Any] = {"order_id": None, "step_reached": None, "error": None}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()[-32000:]
    except Exception as e:
        out["error"] = f"log_read_err:{type(e).__name__}"
        return out
    lines = [l for l in text.splitlines() if l.strip()]
    for line in lines[::-1]:
        ts_match = re.match(r"^\[([\d\-T:.]+)\]", line)
        if ts_match:
            try:
                ts = datetime.fromisoformat(ts_match.group(1)).timestamp()
                if ts < started_at - 5:
                    break
            except Exception:
                pass
        low = line.lower()
        if "order_id" in low or "order_confirmed" in low:
            m = re.search(r"order[_-]?id[=:\s]+([A-Z0-9\-]{6,})", line, re.I)
            if m:
                out["order_id"] = m.group(1)
                out["step_reached"] = "order_confirmed"
                return out
        if "fatal" in low or "bad_credentials" in low or "captcha_unsolvable" in low:
            out["error"] = line[-300:]
            out["step_reached"] = "fatal"
            return out
        if "checkout_ok" in low or "pago_ok" in low or "purchase_ok" in low:
            out["step_reached"] = "checkout_ok"
        elif "cart_ok" in low or "carrito_ok" in low:
            out["step_reached"] = out["step_reached"] or "cart_ok"
        elif "login_ok" in low:
            out["step_reached"] = out["step_reached"] or "login_ok"
    return out


def run_real_purchase(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Lanza _compra_parallel.py para el evento + cuenta indicados en ctx.

    ctx requerido:
      - event.tm_url   (str)  URL del evento TM
      - quantity       (int)  default 2
      - offer_idx      (int)  default 3 (Asiento Reservado)
      - selection_mode (str)  'quick' | 'seatmap'
      - sections       (list) sólo si seatmap
      - allow_separated(bool)
      - confirm_buy    (bool) si False, NO se pulsa Pagar (safety)
      - engine         (str)  'silent' | 'camoufox' | ausente → morelogin

    Retorna:
      {ok, step, elapsed_ms, data?, error?, error_type?, retriable?}
    """
    started = time.time()
    event = ctx.get("event") or {}
    event_url = event.get("tm_url") or event.get("url") or ""
    if not event_url:
        return {"step": "real_purchase", "ok": False,
                "elapsed_ms": 0, "error": "missing_event_url"}

    slot = int(ctx.get("slot", 1))
    qty = int(ctx.get("quantity", 2))
    offer_idx = int(ctx.get("offer_idx", 3))
    sel_mode = ctx.get("selection_mode") or "quick"
    sections = ctx.get("sections") or []
    allow_sep = bool(ctx.get("allow_separated", False))
    confirm = bool(ctx.get("confirm_buy", False))
    timeout_s = int(ctx.get("real_purchase_timeout_s", DEFAULT_TIMEOUT_S))
    engine = ctx.get("engine")  # 'silent' | 'camoufox' | None → default morelogin

    args = [
        sys.executable, SCRIPT,
        "--slot", str(slot),
        "--event-url", event_url,
        "--offer-idx", str(offer_idx),
        "--qty", str(qty),
        "--mode", sel_mode,
    ]
    if sections:
        args += ["--sections", ",".join(str(s) for s in sections)]
    if allow_sep:
        args.append("--allow-separated")
    if confirm:
        args.append("--confirm")
    if engine:
        args += ["--engine", str(engine)]

    log_path = _slot_log_path(slot)
    try:
        if os.path.exists(log_path):
            os.rename(log_path, log_path + ".prev")
    except Exception:
        pass

    proc = None
    try:
        proc = subprocess.Popen(args, cwd=PROYECTO,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            proc.wait(timeout=timeout_s)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            rc = -1
            return {"step": "real_purchase", "ok": False,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "error": "timeout", "error_type": "timeout", "retriable": True}
    except Exception as e:
        return {"step": "real_purchase", "ok": False,
                "elapsed_ms": int((time.time()-started)*1000),
                "error": f"{type(e).__name__}: {e}", "error_type": "subprocess_fail"}

    # Estrategia: leer outcome.json (IPC estructurado) PRIMERO, log como fallback
    outcome_struct = _read_outcome_json(slot)
    if outcome_struct:
        outcome = {
            "order_id": outcome_struct.get("order_id"),
            "step_reached": outcome_struct.get("step_reached"),
            "error": outcome_struct.get("error"),
            "status": outcome_struct.get("status"),
            "via": "outcome_json",
        }
    else:
        outcome = _parse_outcome_from_log(slot, started)
        outcome["via"] = "log_parse"
    elapsed = int((time.time()-started)*1000)

    if rc == 0 and outcome.get("order_id"):
        ctx["order_id"] = outcome["order_id"]
        return {"step": "real_purchase", "ok": True, "elapsed_ms": elapsed,
                "data": {"order_id": outcome["order_id"], "rc": rc, "via": outcome["via"]}}
    if rc == 0 and outcome.get("step_reached") in ("checkout_ok", "cart_ok"):
        return {"step": "real_purchase", "ok": True, "elapsed_ms": elapsed,
                "data": {"step_reached": outcome.get("step_reached"),
                         "via": outcome["via"],
                         "note": "confirm_buy=False — no se cargó pago" if not confirm else "",
                         "rc": rc}}

    err = outcome.get("error") or f"subprocess_rc={rc} step_reached={outcome.get('step_reached')}"
    err_type = "fatal" if "fatal" in (err or "").lower() else "unknown"
    retriable = err_type != "fatal" and "bad_credentials" not in (err or "").lower()
    return {"step": "real_purchase", "ok": False, "elapsed_ms": elapsed,
            "error": err[:300], "error_type": err_type, "retriable": retriable,
            "data": {"step_reached": outcome.get("step_reached"), "rc": rc}}


def self_test() -> Dict[str, Any]:
    """Smoke test del wrapper sin lanzar compra real."""
    out: Dict[str, Any] = {"checks": {}}
    out["checks"]["script_exists"] = os.path.exists(SCRIPT)
    out["checks"]["proyecto_writable"] = os.access(PROYECTO, os.W_OK)
    try:
        r = run_real_purchase({"event": {}, "quantity": 0,
                                "real_purchase_timeout_s": 1})
        out["checks"]["empty_url_rejected"] = (r.get("error") == "missing_event_url")
    except Exception as e:
        out["checks"]["empty_url_rejected"] = f"err:{e}"
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(self_test(), indent=2))
