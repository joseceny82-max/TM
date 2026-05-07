"""account_warmer — calienta cuentas TM 24-48h antes de un onsale.

Por qué: TM bloquea cuentas "verdes" (recién creadas, 0 actividad) en checkout,
incluso con login válido. Las cuentas warmed (con histórico de browse, login,
wishlist) pasan los filtros heurísticos que TM aplica el día del onsale.

Plan de calentamiento por cuenta:
  1. Login normal (NO checkout) — TM marca la sesión "engaged"
  2. Browse 2-3 eventos del país (consume cookies, _abck genuino)
  3. Add 1 evento a wishlist / favoritos (TM lo loguea como interés)
  4. Logout limpio

Flujos:
  - warm_account(account_id, target_event_url='', dry_run=False) -> dict
  - warm_batch(account_ids, max_parallel=2, target_event_url='') -> {job_id}
  - get_warm_status(job_id?) -> dict

NOTA: requiere `_compra_parallel.login_full(slot, page, account)` para reusar
el login que ya funciona. La extensión a wishlist se hace aquí (no toca el
flow de compra real).
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import json_atomic

PROYECTO = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(PROYECTO, "data", "account_warm_log.json")
_JOBS_PATH = os.path.join(PROYECTO, "data", "account_warm_jobs.json")
os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)

_jobs_lock = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}


def _persist_jobs() -> None:
    try:
        json_atomic.save(_JOBS_PATH, _JOBS)
    except Exception:
        pass


def _log_event(event: Dict[str, Any]) -> None:
    try:
        d = json_atomic.load(_LOG_PATH, default={"log": []})
        event["ts"] = datetime.now().isoformat()
        d["log"].insert(0, event)
        d["log"] = d["log"][:500]
        json_atomic.save(_LOG_PATH, d)
    except Exception:
        pass


# Eventos de "browse" sintético — usar listings populares, no compras
_DEFAULT_BROWSE_PATHS = [
    "/discover/concerts",
    "/discover/sports",
    "/discover/arts-theater",
    "/discover/family",
]


def _pick_browse_events(country: str, n: int = 2) -> List[str]:
    """Devuelve URLs de eventos cacheados del país, para 'browse' realista."""
    try:
        import event_manager
        evs = [e for e in event_manager.list_events()
               if e.get("country", "").lower() == country.lower()
               and e.get("tm_url")]
        random.shuffle(evs)
        return [e["tm_url"] for e in evs[:n]]
    except Exception:
        return []


def warm_account(account_id: str, target_event_url: str = "",
                 dry_run: bool = False, slot: int = 0) -> Dict[str, Any]:
    """Caliéntate una cuenta. Devuelve dict con stages OK/fail y elapsed.

    `target_event_url` (opcional): si se pasa, además se hace browse del evento
    target para que TM tenga señal de interés específico cuando haga el onsale.

    `dry_run=True` solo valida prerequisitos sin lanzar browser.
    """
    started = time.time()
    out: Dict[str, Any] = {"account_id": account_id, "stages": [],
                            "started_at": datetime.now().isoformat(),
                            "dry_run": dry_run}

    # 1. cargar cuenta
    try:
        import account_manager as am
        acc = next((a for a in am.account_manager.list_accounts()
                    if a.get("id") == account_id), None)
        if not acc:
            out["error"] = "account_not_found"
            return out
        out["email"] = acc.get("tm_email")
        out["country"] = acc.get("country") or "es"
    except Exception as e:
        out["error"] = f"load_acc:{e}"
        return out

    if dry_run:
        out["stages"].append({"stage": "preflight_ok", "ok": True,
                               "browse_count": len(_pick_browse_events(out["country"], 2))})
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        out["ok"] = True
        return out

    # 2. lanzar perfil + browser usando primitivas de _compra_parallel
    browse_urls = _pick_browse_events(out["country"], 2)
    if target_event_url:
        browse_urls = [target_event_url] + browse_urls
    out["browse_urls"] = browse_urls

    # Stage REAL — delega al módulo standalone si está disponible
    try:
        import _compra_parallel as cp
        import browser_orchestrator as bo
    except Exception as e:
        out["error"] = f"deps_missing:{e}"
        return out

    # Pick perfil ES fresco
    prof = None
    for retry in range(3):
        if cp._acquire_pick_mutex(timeout=20):
            try:
                prof = cp.pick_morelogin_profile(slot)
            finally:
                cp._release_pick_mutex()
            if prof:
                break
        time.sleep(2)
    if not prof:
        out["error"] = "no_profile"
        return out
    out["profile"] = prof.get("env_id")

    try:
        import requests as _rq
        r = _rq.post(f"{bo.MORELOGIN_API}/api/env/start",
                     json={"envId": prof["env_id"]}, timeout=20).json()
        port = int(r["data"]["debugPort"])
        out["port"] = port
    except Exception as e:
        out["error"] = f"profile_start:{e}"
        return out

    time.sleep(5)
    try:
        from playwright.sync_api import sync_playwright
        v = _rq.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.bring_to_front()

            # 3. Login
            t = time.time()
            login_ok = False
            try:
                lr = cp.login_full(slot, page, acc)
                login_ok = bool(lr) if isinstance(lr, bool) else lr.get("ok") if isinstance(lr, dict) else False
            except Exception as e:
                out["stages"].append({"stage": "login", "ok": False, "err": str(e)[:160]})
            else:
                out["stages"].append({"stage": "login", "ok": login_ok,
                                       "elapsed_ms": int((time.time()-t)*1000)})
            if not login_ok:
                out["error"] = "login_failed"
                return out

            # 4. Browse 2-3 eventos
            for url in browse_urls[:3]:
                t = time.time()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    # scroll + dwell humano
                    try:
                        import human_motion as hm
                        page.wait_for_timeout(int(random.uniform(1500, 3000)))
                        page.mouse.wheel(0, random.randint(200, 600))
                        page.wait_for_timeout(int(random.uniform(1200, 2400)))
                        page.mouse.wheel(0, random.randint(150, 400))
                    except Exception:
                        page.wait_for_timeout(2500)
                    out["stages"].append({"stage": "browse", "ok": True,
                                           "url": url[:80],
                                           "elapsed_ms": int((time.time()-t)*1000)})
                    time.sleep(random.uniform(2.0, 4.5))
                except Exception as e:
                    out["stages"].append({"stage": "browse", "ok": False,
                                           "url": url[:80], "err": str(e)[:120]})

            # 5. Wishlist (best-effort): si TM tiene botón "Add to favorites"
            try:
                wishlist_clicked = page.evaluate("""() => {
                    const b = Array.from(document.querySelectorAll('button,a'))
                              .find(el => /(favorito|wishlist|guardar|save)/i.test(el.innerText || el.getAttribute('aria-label') || ''));
                    if (b) { b.click(); return true; }
                    return false;
                }""")
                out["stages"].append({"stage": "wishlist", "ok": bool(wishlist_clicked),
                                       "note": "best-effort"})
            except Exception:
                out["stages"].append({"stage": "wishlist", "ok": False,
                                       "note": "no botón visible"})

            # 6. Logout limpio
            try:
                page.goto(f"https://my.ticketmaster.{out['country']}/logout",
                          timeout=15000)
                out["stages"].append({"stage": "logout", "ok": True})
            except Exception:
                pass

    except Exception as e:
        out["error"] = f"flow:{e}"
    finally:
        # cerrar perfil
        try:
            import requests as _rq
            _rq.post(f"{bo.MORELOGIN_API}/api/env/close",
                     json={"envId": prof["env_id"]}, timeout=15)
        except Exception:
            pass

    elapsed = int((time.time() - started) * 1000)
    out["elapsed_ms"] = elapsed
    out["ok"] = all(s.get("ok") for s in out["stages"]
                    if s.get("stage") in ("login", "browse"))

    # Persistencia: marcar warming en el account
    try:
        import account_manager as am
        am.account_manager.update_account(account_id, {
            "warmed_at": datetime.now().isoformat(),
            "warmed_for_event": target_event_url or "",
        })
    except Exception:
        pass

    _log_event({"op": "warm_account", "id": account_id, "ok": out.get("ok"),
                "elapsed_ms": elapsed})
    return out


def warm_batch(account_ids: List[str], max_parallel: int = 2,
               target_event_url: str = "") -> Dict[str, Any]:
    """Lanza job background warming. Devuelve {job_id, total}."""
    if not account_ids:
        return {"ok": False, "error": "account_ids required"}
    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _JOBS[job_id] = {
            "id": job_id, "total": len(account_ids), "done": 0,
            "ok_count": 0, "fail_count": 0,
            "status": "starting", "started_at": datetime.now().isoformat(),
            "results": [], "max_parallel": max_parallel,
            "target_event_url": target_event_url,
        }
    _persist_jobs()

    def _worker():
        for acc_id in account_ids:
            r = warm_account(acc_id, target_event_url=target_event_url, slot=0)
            with _jobs_lock:
                _JOBS[job_id]["done"] += 1
                if r.get("ok"):
                    _JOBS[job_id]["ok_count"] += 1
                else:
                    _JOBS[job_id]["fail_count"] += 1
                _JOBS[job_id]["results"].append(
                    {"id": acc_id, "ok": r.get("ok"),
                     "email": r.get("email"), "error": r.get("error")})
            _persist_jobs()
        with _jobs_lock:
            _JOBS[job_id]["status"] = "done"
            _JOBS[job_id]["finished_at"] = datetime.now().isoformat()
        _persist_jobs()

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    return {"ok": True, "job_id": job_id, "total": len(account_ids)}


def get_warm_status(job_id: Optional[str] = None) -> Dict[str, Any]:
    if job_id:
        return _JOBS.get(job_id, {"error": "job_not_found"})
    return {"jobs": list(_JOBS.values())}


def self_test() -> Dict[str, Any]:
    out: Dict[str, Any] = {"checks": {}}
    try:
        import account_manager
        accs = account_manager.account_manager.list_accounts()
        out["checks"]["accounts_loadable"] = len(accs) > 0
    except Exception as e:
        out["checks"]["accounts_loadable"] = f"err:{e}"
    out["checks"]["browse_paths"] = isinstance(_DEFAULT_BROWSE_PATHS, list)
    if accs:
        first = accs[0]
        r = warm_account(first["id"], dry_run=True)
        out["checks"]["dry_run_ok"] = r.get("ok") is True
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
