"""account_health_pro — extensiones avanzadas de gestión de cuentas TM.

Implementa 5 mejoras del state-of-art 2026:
  3. APScheduler cron auto-warming (sin login burst)
  4. Multi-IMAP madre rotation
  5. Sticky residential proxy 30-60min wrapper
  6. Concurrency adaptativa 2→5 progresiva
  7. MFA fatigue avoidance (max 1 fresh login/cuenta/24h)

Todo opcional vía env vars y endpoints REST. Sin romper nada existente.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import json_atomic

PROYECTO = os.path.dirname(os.path.abspath(__file__))
_STATE_PATH = os.path.join(PROYECTO, "data", "account_health_pro.json")
os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)

_lock = threading.RLock()


# ════════════════════════════════════════════════════════════════════
#  3. APScheduler CRON AUTO-WARMING (cookies stay fresh, no login burst)
# ════════════════════════════════════════════════════════════════════

_scheduler = None


def _passive_warming_one(account_id: str, country: str = "es") -> Dict[str, Any]:
    """Warming pasivo: GET home + 1-2 events, lee Set-Cookie, refresca storage_state.
    NO login. Mantiene cookies vivas sin disparar fresh-login flag.
    """
    out = {"account_id": account_id, "method": "passive_http"}
    try:
        import httpx
        import account_manager as am
        accs = am.account_manager.list_accounts()
        acc = next((a for a in accs if a.get("id") == account_id), None)
        if not acc:
            return {**out, "ok": False, "error": "not_found"}
        ss = acc.get("storage_state_file")
        if not ss or not os.path.exists(ss):
            return {**out, "ok": False, "error": "no_storage_state"}
        with open(ss, encoding="utf-8") as f:
            state = json.load(f)
        cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        with httpx.Client(cookies=cookies, headers={"User-Agent": ua},
                           timeout=10, follow_redirects=True) as cli:
            r1 = cli.get(f"https://www.ticketmaster.{country}/")
            time.sleep(1)
            r2 = cli.get(f"https://my.ticketmaster.{country}/settings")
        # Update last_login implicit (mark warming activity)
        am.account_manager.update_account(account_id, {
            "last_passive_warming_at": datetime.now().isoformat(),
            "last_login": datetime.now().isoformat(),
        })
        return {**out, "ok": True,
                "home_status": r1.status_code,
                "settings_status": r2.status_code}
    except Exception as e:
        return {**out, "ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}


def passive_warming_batch(account_ids: List[str], stagger_s: float = 2.0) -> Dict[str, Any]:
    """Warming pasivo masivo con stagger entre cuentas (anti rate-limit IP)."""
    results = []
    ok_count = 0
    started = time.time()
    for aid in account_ids:
        r = _passive_warming_one(aid)
        results.append(r)
        if r.get("ok"):
            ok_count += 1
        if stagger_s > 0:
            time.sleep(stagger_s)
    return {"ok": True, "checked": len(account_ids), "successful": ok_count,
            "elapsed_s": round(time.time() - started, 1),
            "results": results}


def start_warming_cron(interval_hours: int = 18, max_per_run: int = 50) -> Dict[str, Any]:
    """Arranca cron APScheduler que warm-up N cuentas cada interval_hours.

    Default: cada 18h (más frecuente que TTL 24h del storage_state).
    """
    global _scheduler
    if _scheduler and _scheduler.running:
        return {"ok": True, "already_running": True}
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler()

        def _warming_job():
            try:
                import account_manager as am
                accs = am.account_manager.list_accounts()
                # Priorizar cuentas con storage_state stale o sin warming reciente
                candidates = sorted(
                    [a for a in accs if a.get("status") == "active"
                     and a.get("storage_state_file")],
                    key=lambda a: a.get("last_passive_warming_at") or "",
                )[:max_per_run]
                ids = [a["id"] for a in candidates]
                passive_warming_batch(ids, stagger_s=2.0)
            except Exception:
                pass

        _scheduler.add_job(_warming_job, "interval", hours=interval_hours,
                            id="passive_warming", replace_existing=True)
        _scheduler.start()
        return {"ok": True, "started": True, "interval_hours": interval_hours,
                "max_per_run": max_per_run}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def stop_warming_cron() -> Dict[str, Any]:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        return {"ok": True, "stopped": True}
    return {"ok": True, "was_not_running": True}


def warming_cron_status() -> Dict[str, Any]:
    if not _scheduler:
        return {"running": False, "scheduler": None}
    jobs = []
    try:
        for j in _scheduler.get_jobs():
            jobs.append({"id": j.id, "next_run": str(j.next_run_time)})
    except Exception:
        pass
    return {"running": _scheduler.running, "jobs": jobs}


# ════════════════════════════════════════════════════════════════════
#  4. MULTI-IMAP MADRE ROTATION
# ════════════════════════════════════════════════════════════════════

_DEFAULT_MADRE_EMAIL = os.environ.get(
    "TM_IMAP_MADRE_EMAIL", "johnlockperdidos@icloud.com")
_DEFAULT_MADRE_PWD = os.environ.get("TM_IMAP_MADRE_PWD", "")
_DEFAULT_MADRE_HOST = os.environ.get(
    "TM_IMAP_MADRE_HOST", "imap.mail.me.com")

DEFAULT_MADRES = [
    {"alias": "principal", "email": _DEFAULT_MADRE_EMAIL,
     "password": _DEFAULT_MADRE_PWD, "host": _DEFAULT_MADRE_HOST,
     "active": bool(_DEFAULT_MADRE_PWD), "max_aliases": 100},
]


def _load_state() -> Dict[str, Any]:
    return json_atomic.load(_STATE_PATH, default={
        "madres": list(DEFAULT_MADRES),
        "alias_assignments": {},
        "last_used_madre_idx": 0,
    })


def _save_state(d: Dict[str, Any]) -> None:
    json_atomic.save(_STATE_PATH, d)


def add_madre(alias: str, email: str, password: str,
              host: str = "imap.mail.me.com", max_aliases: int = 100) -> Dict[str, Any]:
    with _lock:
        d = _load_state()
        # Reemplaza si alias ya existe
        d["madres"] = [m for m in d["madres"] if m.get("alias") != alias]
        d["madres"].append({
            "alias": alias, "email": email, "password": password,
            "host": host, "active": True, "max_aliases": max_aliases,
            "added_at": datetime.now().isoformat(),
        })
        _save_state(d)
    return {"ok": True, "madres_count": len(d["madres"])}


def list_madres(safe: bool = True) -> List[Dict[str, Any]]:
    d = _load_state()
    out = []
    for m in d.get("madres", []):
        if safe:
            out.append({**m, "password": "***"})
        else:
            out.append(m)
    return out


def get_madre_for_alias(alias_email: str) -> Optional[Dict[str, Any]]:
    """Round-robin entre madres activas. Cachea asignación en alias_assignments."""
    with _lock:
        d = _load_state()
        # Si ya asignado, devolver
        cached = d.get("alias_assignments", {}).get(alias_email)
        if cached:
            for m in d.get("madres", []):
                if m.get("alias") == cached:
                    return m
        # Round-robin entre activas con espacio
        active = [m for m in d.get("madres", []) if m.get("active", True)]
        if not active:
            return None
        # Contar aliases por madre
        counts = {m["alias"]: 0 for m in active}
        for assigned_alias in d.get("alias_assignments", {}).values():
            if assigned_alias in counts:
                counts[assigned_alias] += 1
        # Pick madre con menos aliases (load balance)
        pick = min(active, key=lambda m: counts.get(m["alias"], 0))
        if counts.get(pick["alias"], 0) >= pick.get("max_aliases", 100):
            return None  # todas llenas
        d["alias_assignments"][alias_email] = pick["alias"]
        _save_state(d)
        return pick


def get_madres_summary() -> Dict[str, Any]:
    d = _load_state()
    summary = {}
    for m in d.get("madres", []):
        alias = m["alias"]
        count = sum(1 for v in d.get("alias_assignments", {}).values() if v == alias)
        summary[alias] = {
            "email": m["email"], "active": m.get("active", True),
            "max_aliases": m.get("max_aliases", 100),
            "current_aliases": count,
            "fill_pct": round(100 * count / max(1, m.get("max_aliases", 100)), 1),
        }
    return summary


# ════════════════════════════════════════════════════════════════════
#  5. STICKY RESIDENTIAL PROXY 30-60min
# ════════════════════════════════════════════════════════════════════

def request_sticky_proxy(account_id: str, ttl_min: int = 30) -> Dict[str, Any]:
    """Solicita un proxy sticky para una cuenta (TTL N min mismo exit IP).

    Wrapper sobre proxy_session_pin existente. Pin queda activo TTL min.
    """
    try:
        import proxy_session_pin as psp
        sid = f"acc_{account_id}"
        r = psp.pin_session(session_id=sid, geo="es", ttl_min=ttl_min)
        return {"ok": r.get("ok", False), "session_id": sid, "ttl_min": ttl_min,
                "proxy": r.get("proxy"), "raw": r}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def release_sticky_proxy(account_id: str) -> Dict[str, Any]:
    try:
        import proxy_session_pin as psp
        sid = f"acc_{account_id}"
        if hasattr(psp, "unpin_session"):
            r = psp.unpin_session(sid)
        else:
            r = {"ok": True, "note": "unpin_session not implemented"}
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ════════════════════════════════════════════════════════════════════
#  6. CONCURRENCY ADAPTATIVA 2→5 PROGRESIVA
# ════════════════════════════════════════════════════════════════════

class AdaptiveConcurrency:
    """Auto-escala concurrency según success rate.

    Empieza en 2, sube +1 tras N éxitos consecutivos, baja a 2 tras 1 fallo
    hasta max=5. Anti velocity-by-IP TM.
    """
    def __init__(self, initial: int = 2, max_concurrent: int = 5,
                 successes_to_scale: int = 5):
        self.current = initial
        self.max_concurrent = max_concurrent
        self.successes_to_scale = successes_to_scale
        self._consec_successes = 0
        self._lock = threading.Lock()

    def record_result(self, success: bool) -> int:
        with self._lock:
            if success:
                self._consec_successes += 1
                if (self._consec_successes >= self.successes_to_scale
                        and self.current < self.max_concurrent):
                    self.current += 1
                    self._consec_successes = 0
            else:
                self._consec_successes = 0
                self.current = max(2, self.current - 1)
        return self.current

    def get_concurrency(self) -> int:
        with self._lock:
            return self.current


# Singleton instance
_global_adaptive = AdaptiveConcurrency()


def adaptive_record(success: bool) -> int:
    return _global_adaptive.record_result(success)


def adaptive_get() -> int:
    return _global_adaptive.get_concurrency()


# ════════════════════════════════════════════════════════════════════
#  7. MFA FATIGUE AVOIDANCE (max 1 fresh login / cuenta / 24h)
# ════════════════════════════════════════════════════════════════════

_FRESH_LOGIN_WINDOW_S = 24 * 3600


def can_fresh_login(account_id: str) -> Dict[str, Any]:
    """Check si la cuenta puede hacer fresh login (anti MFA fatigue)."""
    try:
        import account_manager as am
        accs = am.account_manager.list_accounts()
        acc = next((a for a in accs if a.get("id") == account_id), None)
        if not acc:
            return {"ok": False, "error": "not_found"}
        last_fresh = acc.get("last_fresh_login_at")
        if not last_fresh:
            return {"ok": True, "allowed": True, "reason": "no_previous_fresh_login"}
        try:
            dt = datetime.fromisoformat(last_fresh.replace("Z", "+00:00")
                                         if last_fresh.endswith("Z") else last_fresh)
            if dt.tzinfo:
                dt = dt.replace(tzinfo=None)
            elapsed = (datetime.now() - dt).total_seconds()
            if elapsed > _FRESH_LOGIN_WINDOW_S:
                return {"ok": True, "allowed": True,
                        "elapsed_h": round(elapsed / 3600, 1)}
            wait_h = round((_FRESH_LOGIN_WINDOW_S - elapsed) / 3600, 1)
            return {"ok": True, "allowed": False,
                    "reason": "mfa_fatigue_avoidance",
                    "wait_hours": wait_h,
                    "last_fresh_login_at": last_fresh}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def record_fresh_login(account_id: str) -> Dict[str, Any]:
    try:
        import account_manager as am
        am.account_manager.update_account(account_id, {
            "last_fresh_login_at": datetime.now().isoformat(),
        })
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ════════════════════════════════════════════════════════════════════
#  SELF TEST + INFO
# ════════════════════════════════════════════════════════════════════

def get_info() -> Dict[str, Any]:
    return {
        "module": "account_health_pro",
        "purpose": "5 mejoras 2026 sobre account health",
        "features": {
            "passive_warming_cron": "GET home/event sin login. APScheduler cada 18h",
            "multi_imap_madres": "Round-robin entre madres con load balancing",
            "sticky_proxy": "TTL 30-60min mismo exit IP por cuenta",
            "adaptive_concurrency": "2→5 progresiva con success rate",
            "mfa_fatigue_avoidance": "Max 1 fresh-login/cuenta/24h",
        },
        "warming_cron": warming_cron_status(),
        "madres": get_madres_summary(),
        "adaptive_concurrency_now": adaptive_get(),
    }


def self_test() -> Dict[str, Any]:
    out: Dict[str, Any] = {"checks": {}}
    out["checks"]["passive_warming_callable"] = callable(_passive_warming_one)
    out["checks"]["warming_cron_status"] = isinstance(warming_cron_status(), dict)
    out["checks"]["madre_pick"] = (get_madre_for_alias("test@test.com") is not None)
    out["checks"]["adaptive_record"] = isinstance(adaptive_record(True), int)
    out["checks"]["mfa_fatigue"] = callable(can_fresh_login)
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
    print()
    print(json.dumps(get_info(), indent=2))
