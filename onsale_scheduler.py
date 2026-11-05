"""onsale_scheduler — daemon que dispara compras a la hora exacta del onsale.

Watchea `events.json` y, cuando un evento `enabled=True` con `sale_date` se
acerca al T-0, lanza el flow de compra:
  - T-30s: pre-warming (Akamai sensor + cookie pool refresh)
  - T-3s: arrancar workers paralelos en posición de espera
  - T+0s: trigger sincronizado (sub-segundo precision via wall clock)

Watchdog también:
  - Lanza `account_warmer` 24h antes (si configured)
  - Lanza `cookie_warmer.warm_with_akamai_sensor` 30 min antes
  - Notifica via Discord/Telegram en cada hito (T-24h, T-1h, T-30s, T+0)

Uso:
  - `start_daemon()` arranca el thread loop (1s tick)
  - `register_attack(tm_id, qty, n_workers, account_pool)` programa un ataque
  - `get_schedule()` devuelve los próximos eventos programados
  - `cancel_attack(tm_id)` cancela un ataque pendiente

Persistencia: `data/onsale_attacks.json`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import json_atomic

PROYECTO = os.path.dirname(os.path.abspath(__file__))
_ATTACKS_PATH = os.path.join(PROYECTO, "data", "onsale_attacks.json")
_LOG_PATH = os.path.join(PROYECTO, "data", "onsale_scheduler_log.json")
os.makedirs(os.path.dirname(_ATTACKS_PATH), exist_ok=True)

_lock = threading.Lock()
_daemon_thread: Optional[threading.Thread] = None
_daemon_stop = threading.Event()
_attacks: Dict[str, Dict[str, Any]] = {}

# Hooks por etapa: T-{seconds}: callbacks pre-onsale
PRE_ONSALE_HOOKS = [
    (24 * 3600, "account_warming"),    # 24h antes
    (3600, "akamai_sensor_refresh"),   # 1h antes
    (1800, "cookie_pool_warming"),      # 30min antes
    (300, "captcha_pool_pre_solve"),   # 5min antes
    (30, "workers_launch_standby"),    # 30s antes
    (3, "workers_arm"),                 # 3s antes
    (0, "fire"),                        # T+0
]


def _log(event: Dict[str, Any]) -> None:
    try:
        d = json_atomic.load(_LOG_PATH, default={"log": []})
        event["ts"] = datetime.now().isoformat()
        d["log"].insert(0, event)
        d["log"] = d["log"][:500]
        json_atomic.save(_LOG_PATH, d)
    except Exception:
        pass


def _persist() -> None:
    try:
        json_atomic.save(_ATTACKS_PATH, _attacks)
    except Exception:
        pass


def _load_attacks() -> None:
    global _attacks
    try:
        d = json_atomic.load(_ATTACKS_PATH, default={})
        _attacks.update(d)
    except Exception:
        pass


def register_attack(tm_id: str, qty: int = 2, n_workers: int = 3,
                    account_pool: Optional[List[str]] = None,
                    offer_idx: int = 3, sections: Optional[List[str]] = None,
                    selection_mode: str = "quick",
                    confirm_buy: bool = False,
                    notify_channels: Optional[List[str]] = None) -> Dict[str, Any]:
    """Registra un ataque programado contra un evento.

    Lee `sale_date` del evento en `datos/eventos/events.json`.
    """
    try:
        with open(os.path.join(PROYECTO, "datos", "eventos", "events.json"),
                  encoding="utf-8") as f:
            ev_data = json.load(f)
        events_list = ev_data if isinstance(ev_data, list) else ev_data.get("events", [])
        ev = next((e for e in events_list if str(e.get("tm_id")) == str(tm_id)), None)
        if not ev:
            return {"ok": False, "error": f"event_not_found tm_id={tm_id}"}
        sale_date = ev.get("sale_date")
        if not sale_date:
            return {"ok": False, "error": "event has no sale_date"}
    except Exception as e:
        return {"ok": False, "error": f"events_load:{e}"}

    with _lock:
        _attacks[str(tm_id)] = {
            "tm_id": str(tm_id),
            "tm_url": ev.get("tm_url"),
            "event_name": ev.get("name"),
            "sale_date": sale_date,
            "qty": int(qty),
            "n_workers": int(n_workers),
            "account_pool": account_pool or [],
            "offer_idx": int(offer_idx),
            "sections": sections or [],
            "selection_mode": selection_mode,
            "confirm_buy": bool(confirm_buy),
            "notify_channels": notify_channels or ["telegram", "discord"],
            "registered_at": datetime.now().isoformat(),
            "status": "scheduled",
            "hooks_fired": [],
        }
        _persist()
    _log({"op": "register_attack", "tm_id": tm_id, "sale_date": sale_date,
          "n_workers": n_workers})
    return {"ok": True, "attack": _attacks[str(tm_id)]}


def cancel_attack(tm_id: str) -> Dict[str, Any]:
    with _lock:
        att = _attacks.pop(str(tm_id), None)
        _persist()
    _log({"op": "cancel_attack", "tm_id": tm_id, "found": att is not None})
    return {"ok": att is not None, "tm_id": tm_id}


def get_schedule() -> Dict[str, Any]:
    with _lock:
        return {
            "daemon_running": _daemon_thread is not None and _daemon_thread.is_alive(),
            "attacks": list(_attacks.values()),
        }


def _seconds_until_sale(sale_date_iso: str) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(sale_date_iso.replace("Z", "+00:00")
                                     if sale_date_iso.endswith("Z") else sale_date_iso)
        if dt.tzinfo is None:
            # naive: asumir local (Spain)
            now = datetime.now()
        else:
            now = datetime.now(dt.tzinfo)
        return (dt - now).total_seconds()
    except Exception:
        return None


def _fire_hook(stage: str, attack: Dict[str, Any]) -> None:
    """Ejecuta el hook correspondiente al stage. Best-effort, no bloquea."""
    tm_id = attack["tm_id"]
    _log({"op": "hook_fire", "stage": stage, "tm_id": tm_id})

    if stage == "account_warming":
        try:
            import account_warmer
            pool = attack.get("account_pool") or []
            if pool:
                account_warmer.warm_batch(pool[:8], max_parallel=2,
                                          target_event_url=attack.get("tm_url", ""))
        except Exception as e:
            _log({"op": "hook_err", "stage": stage, "err": str(e)[:120]})

    elif stage == "akamai_sensor_refresh":
        try:
            import cookie_warmer
            cookie_warmer.warm_with_akamai_sensor(
                domain_id="es", page_url=attack.get("tm_url", ""))
        except Exception as e:
            _log({"op": "hook_err", "stage": stage, "err": str(e)[:120]})

    elif stage == "cookie_pool_warming":
        try:
            import cookie_warmer
            cookie_warmer.warm_cookies(domain_id="es", pages=4, client="curl_cffi")
        except Exception as e:
            _log({"op": "hook_err", "stage": stage, "err": str(e)[:120]})

    elif stage == "captcha_pool_pre_solve":
        # Pre-solve N tokens reCAPTCHA si captcha_manager los soporta
        try:
            import captcha_manager
            if hasattr(captcha_manager, "pre_solve_pool"):
                captcha_manager.pre_solve_pool(n=10, type="recaptcha_v2")
        except Exception:
            pass

    elif stage == "workers_launch_standby":
        # Notificar Telegram/Discord
        try:
            import notifications
            notifications.notify(
                f"⏱ T-30s {attack.get('event_name', tm_id)} — workers en standby",
                channels=attack.get("notify_channels"))
        except Exception:
            pass

    elif stage == "workers_arm":
        try:
            import notifications
            notifications.notify(
                f"⚡ T-3s {attack.get('event_name', tm_id)} — ARMADO",
                channels=attack.get("notify_channels"))
        except Exception:
            pass

    elif stage == "fire":
        # Lanzar N workers paralelos via subprocess
        try:
            import subprocess as _sp
            import sys as _sys
            for slot in range(1, attack.get("n_workers", 3) + 1):
                args = [_sys.executable,
                        os.path.join(PROYECTO, "_compra_with_swap.py"),
                        "--slot", str(slot),
                        "--event-url", attack["tm_url"],
                        "--qty", str(attack.get("qty", 2)),
                        "--offer-idx", str(attack.get("offer_idx", 3)),
                        "--mode", attack.get("selection_mode", "quick")]
                if attack.get("sections"):
                    args += ["--sections", ",".join(str(s) for s in attack["sections"])]
                if attack.get("confirm_buy"):
                    args.append("--confirm")
                _sp.Popen(args, cwd=PROYECTO,
                          creationflags=getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0))
            _log({"op": "fire", "tm_id": tm_id, "workers": attack.get("n_workers")})
            try:
                import notifications
                notifications.notify(
                    f"🚀 FIRE {attack.get('event_name', tm_id)} — {attack.get('n_workers')} workers lanzados",
                    channels=attack.get("notify_channels"))
            except Exception:
                pass
        except Exception as e:
            _log({"op": "fire_err", "tm_id": tm_id, "err": str(e)[:160]})


_LAST_PRUNE_AT = 0
_PRUNE_INTERVAL_S = 3600
_PRUNE_AGE_S = 7 * 24 * 3600   # remueve attacks done >7d


def _auto_prune_done_attacks() -> int:
    """Auto-limpia ataques en estado terminal (done/cancelled) >7d. Anti memory leak."""
    now_iso = datetime.now()
    n = 0
    with _lock:
        to_del = []
        for tm_id, att in _attacks.items():
            if att.get("status") not in ("done", "cancelled"):
                continue
            ref_str = (att.get("fired_at") or att.get("registered_at") or "")
            try:
                ref_ts = datetime.fromisoformat(ref_str)
            except Exception:
                continue
            age_s = (now_iso - ref_ts).total_seconds()
            if age_s > _PRUNE_AGE_S:
                to_del.append(tm_id)
        for tm_id in to_del:
            del _attacks[tm_id]
            n += 1
        if n > 0:
            _persist()
    if n > 0:
        _log({"op": "auto_prune", "removed": n})
    return n


def _daemon_loop() -> None:
    global _LAST_PRUNE_AT
    while not _daemon_stop.is_set():
        try:
            # Auto-prune cada PRUNE_INTERVAL_S
            if (time.time() - _LAST_PRUNE_AT) > _PRUNE_INTERVAL_S:
                _auto_prune_done_attacks()
                _LAST_PRUNE_AT = time.time()
            with _lock:
                attacks_snap = list(_attacks.values())
            for att in attacks_snap:
                if att.get("status") in ("done", "cancelled"):
                    continue
                sec = _seconds_until_sale(att["sale_date"])
                if sec is None:
                    continue
                tm_id = att["tm_id"]
                fired = set(att.get("hooks_fired", []))
                # Ejecutar hooks que ya hayan llegado a su umbral
                for delta_s, stage in PRE_ONSALE_HOOKS:
                    if stage in fired:
                        continue
                    # Hook se dispara cuando sec <= delta_s (estamos T-delta_s o menos)
                    # Tolerancia: 1 segundo
                    if sec <= delta_s + 1:
                        _fire_hook(stage, att)
                        fired.add(stage)
                        with _lock:
                            if tm_id in _attacks:
                                _attacks[tm_id]["hooks_fired"] = sorted(fired)
                                if stage == "fire":
                                    _attacks[tm_id]["status"] = "fired"
                                    _attacks[tm_id]["fired_at"] = datetime.now().isoformat()
                                _persist()
                # Si pasaron > 1h tras fire → marcar done
                if "fire" in fired and sec < -3600:
                    with _lock:
                        if tm_id in _attacks:
                            _attacks[tm_id]["status"] = "done"
                            _persist()
        except Exception as e:
            _log({"op": "daemon_err", "err": str(e)[:160]})
        # tick 1s para sub-segundo precision en T-0
        _daemon_stop.wait(1.0)


def start_daemon() -> Dict[str, Any]:
    global _daemon_thread
    if _daemon_thread and _daemon_thread.is_alive():
        return {"ok": True, "already_running": True}
    _load_attacks()
    _daemon_stop.clear()
    _daemon_thread = threading.Thread(target=_daemon_loop, daemon=True,
                                       name="onsale_scheduler")
    _daemon_thread.start()
    _log({"op": "daemon_start"})
    return {"ok": True, "started": True}


def stop_daemon() -> Dict[str, Any]:
    global _daemon_thread
    _daemon_stop.set()
    if _daemon_thread:
        _daemon_thread.join(timeout=3)
    _daemon_thread = None
    _log({"op": "daemon_stop"})
    return {"ok": True}


def self_test() -> Dict[str, Any]:
    out: Dict[str, Any] = {"checks": {}}
    out["checks"]["events_json_loadable"] = os.path.exists(
        os.path.join(PROYECTO, "datos", "eventos", "events.json"))
    s = _seconds_until_sale("2099-12-31T23:59:59")
    out["checks"]["seconds_until_works"] = (s is not None and s > 0)
    out["checks"]["pre_hooks_count"] = (len(PRE_ONSALE_HOOKS) >= 5)
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
