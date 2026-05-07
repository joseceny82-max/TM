"""bot_infra.py — Infraestructura central del bot de compra.

Primitivas robustas que solucionan los problemas vistos durante el desarrollo:
  - Pick de perfiles MoreLogin con race conditions (mutex file-based atómico).
  - Cleanup CDP que crashea con "Event loop is closed" si se llama fuera del with.
  - Recovery de jobs interrumpidos al restart del dashboard.
  - Dead letter queue para slots que fallan repetidamente.
  - Telemetría JSONL estructurada por job para auditoría.
  - Retry policies con backoff exponencial.
  - Health checks pre-flight (MoreLogin alcanzable, IMAP conectable, etc).

Componentes:
    MoreLoginPool    - lease/release atómico de perfiles ES
    AccountPool      - cuenta TM con bad_credentials skip + lock cross-process
    BrowserSession   - context manager (auto-cleanup CDP + close)
    FlowState        - state machine del flow de compra
    JobLogger        - logging JSONL por slot/job
    RetryPolicy      - backoff configurable
    HealthChecker    - preflight antes de lanzar batches
    DeadLetterQueue  - slots que crashearon (para análisis manual)

Uso:
    from bot_infra import MoreLoginPool, BrowserSession, JobLogger
    pool = MoreLoginPool()
    with pool.lease(country='es') as profile:
        with BrowserSession(profile) as session:
            ...
"""
from __future__ import annotations
import os
import json
import time
import uuid
import random
import threading
import contextlib
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable

import requests

PROYECTO = os.path.dirname(os.path.abspath(__file__))
DATOS = os.path.join(PROYECTO, "datos")
LOCKS = os.path.join(DATOS, "bot_locks")
JOBS_DIR = os.path.join(DATOS, "bot_jobs")
DLQ_DIR = os.path.join(DATOS, "bot_dlq")
HEALTH_LOG = os.path.join(DATOS, "bot_preflight.jsonl")

for d in (DATOS, LOCKS, JOBS_DIR, DLQ_DIR):
    os.makedirs(d, exist_ok=True)


# ═══════════════════════════════════════════════════════════
#  FILE LOCK (mutex cross-process atómico)
# ═══════════════════════════════════════════════════════════
class FileLock:
    """Mutex inter-process via O_EXCL atomic creation. Stale > stale_s lo
    descarta. Usar como context manager: `with FileLock(name):`"""
    def __init__(self, name: str, stale_s: float = 60.0,
                 timeout_s: float = 30.0):
        self.path = os.path.join(LOCKS, f"{name}.lock")
        self.stale_s = stale_s
        self.timeout_s = timeout_s
        self.acquired = False

    def acquire(self) -> bool:
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try: os.write(fd, str(os.getpid()).encode())
                finally: os.close(fd)
                self.acquired = True
                return True
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > self.stale_s:
                        try: os.unlink(self.path)
                        except Exception: pass
                except Exception:
                    pass
                time.sleep(0.3 + random.random() * 0.3)
        return False

    def release(self):
        if self.acquired:
            try: os.unlink(self.path)
            except Exception: pass
            self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"FileLock '{self.path}' timeout")
        return self

    def __exit__(self, *a):
        self.release()


# ═══════════════════════════════════════════════════════════
#  MORELOGIN POOL
# ═══════════════════════════════════════════════════════════
class MoreLoginPool:
    """Pool thread-safe + cross-process de perfiles MoreLogin ES.
    Usa lock atómico file-based + fichero JSON de perfiles en uso.
    """
    API = "http://127.0.0.1:40000"
    ACTIVE_FILE = os.path.join(LOCKS, "morelogin_active.json")
    BURNED_FILE = os.path.join(DATOS, "burned_profiles.json")

    def __init__(self):
        self._sess = requests.Session()

    def is_alive(self) -> bool:
        try:
            r = self._sess.post(f"{self.API}/api/env/page",
                                 json={"pageNo": 1, "pageSize": 1},
                                 timeout=4)
            return r.status_code == 200
        except Exception:
            return False

    def _read_active(self) -> set:
        try:
            if os.path.exists(self.ACTIVE_FILE):
                with open(self.ACTIVE_FILE, encoding="utf-8") as f:
                    return set(json.load(f))
        except Exception: pass
        return set()

    def _write_active(self, names: set):
        try:
            import json_atomic
            json_atomic.save(self.ACTIVE_FILE, list(names))
        except Exception:
            try:
                with open(self.ACTIVE_FILE, "w", encoding="utf-8") as f:
                    json.dump(list(names), f)
            except Exception: pass

    def _read_burned(self) -> set:
        try:
            if os.path.exists(self.BURNED_FILE):
                with open(self.BURNED_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                    return set(d.get("burned", []))
        except Exception: pass
        return set()

    def _pick_profile_from_api(self, country: str = "es",
                                exclude: set = None) -> Optional[Dict]:
        exclude = exclude or set()
        for page_no in range(1, 21):
            try:
                r = self._sess.post(f"{self.API}/api/env/page",
                                     json={"pageNo": page_no,
                                           "pageSize": 100},
                                     timeout=10).json()
            except Exception:
                return None
            items = (r.get("data") or {}).get("dataList") or []
            if not items:
                break
            for it in items:
                p = it.get("proxy") or {}
                name = it.get("envName") or ""
                if not name or name in exclude:
                    continue
                cc = (p.get("countryCode") or "").upper()
                user = p.get("username") or ""
                if country.upper() == "ES":
                    is_match = (cc == "ES" or "cc-ES" in user
                                or "-ES-" in user)
                else:
                    is_match = cc == country.upper()
                if not is_match:
                    continue
                if p.get("proxyCheckStatus") == 2:
                    continue
                return {
                    "profile": name, "env_id": it["id"],
                    "ip": p.get("exportIp", ""),
                    "proxy_user": user[:80],
                    "country": cc or country.upper(),
                }
        return None

    @contextlib.contextmanager
    def lease(self, country: str = "es", timeout_s: float = 60.0):
        """Lease atómico de un perfil ES. Libera al salir del with."""
        prof = None
        with FileLock("morelogin_pick", timeout_s=timeout_s):
            burned = self._read_burned()
            active = self._read_active()
            prof = self._pick_profile_from_api(country=country,
                                                exclude=burned | active)
            if prof:
                active.add(prof["profile"])
                self._write_active(active)
        if not prof:
            raise RuntimeError(f"MoreLoginPool: no profile available for {country} "
                                f"(burned={len(burned)}, active={len(active)})")
        try:
            yield prof
        finally:
            with FileLock("morelogin_pick", timeout_s=15):
                active = self._read_active()
                active.discard(prof["profile"])
                self._write_active(active)

    def start_browser(self, env_id: str) -> int:
        """Arranca perfil → debug port. Errores raise."""
        r = self._sess.post(f"{self.API}/api/env/start",
                             json={"envId": env_id}, timeout=20).json()
        return int(r["data"]["debugPort"])

    def close_browser(self, env_id: str) -> bool:
        try:
            r = self._sess.post(f"{self.API}/api/env/close",
                                 json={"envId": env_id}, timeout=15).json()
            return r.get("code") == 0
        except Exception:
            return False

    def reset_active(self):
        """Resetea el listado de perfiles activos. Útil tras crash global."""
        self._write_active(set())


# ═══════════════════════════════════════════════════════════
#  ACCOUNT POOL
# ═══════════════════════════════════════════════════════════
class AccountPool:
    """Pool de cuentas TM con bad_credentials skip + lock per-account."""
    USED_FILE = os.path.join(LOCKS, "accounts_in_use.json")
    BAD_FILE = os.path.join(DATOS, "bad_credentials_accounts.json")

    def _read_used(self) -> set:
        try:
            if os.path.exists(self.USED_FILE):
                with open(self.USED_FILE, encoding="utf-8") as f:
                    return set(json.load(f))
        except Exception: pass
        return set()

    def _write_used(self, ids: set):
        try:
            import json_atomic
            json_atomic.save(self.USED_FILE, list(ids))
        except Exception:
            try:
                with open(self.USED_FILE, "w", encoding="utf-8") as f:
                    json.dump(list(ids), f)
            except Exception: pass

    def _read_bad(self) -> set:
        try:
            if os.path.exists(self.BAD_FILE):
                with open(self.BAD_FILE, encoding="utf-8") as f:
                    return set(json.load(f))
        except Exception: pass
        return set()

    def mark_bad(self, account_id: str):
        with FileLock("account_bad", timeout_s=10):
            bad = self._read_bad()
            bad.add(account_id)
            try:
                import json_atomic
                json_atomic.save(self.BAD_FILE, list(bad))
            except Exception:
                try:
                    with open(self.BAD_FILE, "w", encoding="utf-8") as f:
                        json.dump(list(bad), f)
                except Exception: pass

    @contextlib.contextmanager
    def lease(self, country: str = "es",
              require_card: bool = True,
              prefer_verified: bool = True):
        """Reserva una cuenta no en uso, no bad, opcionalmente con tarjeta."""
        import account_manager as am
        leased_id = None
        with FileLock("account_pick", timeout_s=30):
            used = self._read_used()
            bad = self._read_bad()
            accs = am.account_manager.list_accounts()
            cards = am.account_manager.list_cards()
            cards_by = {c.get("account_id"): c for c in cards
                        if c.get("account_id")}
            country_match = lambda a: (
                (a.get("country") or "").lower() in (country.lower(), country.lower()+"pain", "spain")
                if country.lower() == "es" else
                (a.get("country") or "").lower() == country.lower()
            )
            cands = []
            for a in accs:
                if a.get("status") not in ("active",): continue
                if a.get("id") in used or a.get("id") in bad: continue
                if not country_match(a): continue
                if require_card and not (cards_by.get(a.get("id"))
                                          or a.get("card_id")):
                    continue
                cands.append(a)
            # Priorizar las verificadas por validator
            if prefer_verified:
                cands.sort(key=lambda a: 0 if a.get("credentials_verified") else 1)
            if not cands:
                raise RuntimeError(f"AccountPool: 0 cuentas disponibles "
                                    f"(used={len(used)}, bad={len(bad)})")
            chosen = cands[0]
            leased_id = chosen.get("id")
            used.add(leased_id)
            self._write_used(used)
        try:
            card = cards_by.get(leased_id)
            yield chosen, card
        finally:
            with FileLock("account_pick", timeout_s=10):
                used = self._read_used()
                used.discard(leased_id)
                self._write_used(used)


# ═══════════════════════════════════════════════════════════
#  BROWSER SESSION (context manager con cleanup garantizado)
# ═══════════════════════════════════════════════════════════
class BrowserSession:
    """Context manager que abre Playwright via CDP MoreLogin, garantiza
    cleanup CDP DENTRO del with sync_playwright (evita Event loop closed)
    y cierra perfil incluso si crashea."""
    def __init__(self, profile: Dict[str, Any], pool: MoreLoginPool):
        self.profile = profile
        self.pool = pool
        self.port = None
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self._cleanup_done = False

    def __enter__(self):
        self.port = self.pool.start_browser(self.profile["env_id"])
        time.sleep(4.5)
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        v = requests.get(f"http://127.0.0.1:{self.port}/json/version",
                          timeout=8).json()
        self.browser = self.pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
        self.ctx = self.browser.contexts[0] if self.browser.contexts \
            else self.browser.new_context()
        try:
            self.ctx.add_init_script("""
                if (window.Notification) Notification.requestPermission = () => Promise.resolve('denied');
            """)
        except Exception: pass
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        try: self.page.bring_to_front()
        except Exception: pass
        return self

    def cleanup_history(self):
        """Limpia historial via CDP. Llamar ANTES de salir del with."""
        if self._cleanup_done or self.ctx is None:
            return
        try:
            import browser_cleanup
            browser_cleanup.clear_history_via_cdp(self.ctx)
            self._cleanup_done = True
        except Exception: pass

    def __exit__(self, *a):
        # 1) Cleanup CDP (mientras event loop sigue activo)
        if not self._cleanup_done:
            self.cleanup_history()
        # 2) Cerrar browser + parar sync_playwright
        try:
            if self.browser: self.browser.close()
        except Exception: pass
        try:
            if self.pw: self.pw.stop()
        except Exception: pass
        # 3) Cerrar perfil MoreLogin
        try:
            self.pool.close_browser(self.profile["env_id"])
        except Exception: pass


# ═══════════════════════════════════════════════════════════
#  FLOW STATE MACHINE
# ═══════════════════════════════════════════════════════════
class FlowState:
    """Estados ordenados del flow de compra. Cada slot avanza a través de
    estos. Útil para reportar progreso granular y reintentos por etapa."""
    INIT = "init"
    PROFILE_LEASED = "profile_leased"
    ACCOUNT_LEASED = "account_leased"
    BROWSER_LAUNCHED = "browser_launched"
    EVENT_LOADED = "event_loaded"
    QTY_SELECTED = "qty_selected"
    CART_REACHED = "cart_reached"
    CONDITIONS_ACCEPTED = "conditions_accepted"
    LOGIN_OK = "login_ok"
    PRECHECKOUT_OK = "precheckout_ok"
    CHECKOUT_REACHED = "checkout_reached"
    CARD_FILLED = "card_filled"
    AWAITING_USER = "awaiting_user"
    DONE = "done"
    FAILED = "failed"
    DLQ = "dlq"

    ORDER = [INIT, PROFILE_LEASED, ACCOUNT_LEASED, BROWSER_LAUNCHED,
             EVENT_LOADED, QTY_SELECTED, CART_REACHED, CONDITIONS_ACCEPTED,
             LOGIN_OK, PRECHECKOUT_OK, CHECKOUT_REACHED, CARD_FILLED,
             AWAITING_USER, DONE]

    @classmethod
    def progress(cls, state: str) -> int:
        """Devuelve % completado (0-100) según estado actual."""
        try:
            idx = cls.ORDER.index(state)
            return int(100 * (idx + 1) / len(cls.ORDER))
        except ValueError:
            return 0


# ═══════════════════════════════════════════════════════════
#  JOB LOGGER (JSONL estructurado)
# ═══════════════════════════════════════════════════════════
class JobLogger:
    """Persiste cada paso del flow como línea JSONL en datos/bot_jobs/<job_id>.jsonl
    y mantiene resumen vivo en datos/bot_jobs/<job_id>.json para query rápida."""
    def __init__(self, job_id: str, slot: int = 0):
        self.job_id = job_id
        self.slot = slot
        self.jsonl_path = os.path.join(JOBS_DIR, f"{job_id}.jsonl")
        self.summary_path = os.path.join(JOBS_DIR, f"{job_id}.summary.json")
        self.state = FlowState.INIT
        self._lock = threading.Lock()

    def log(self, event: str, **fields):
        line = {
            "ts": datetime.now().isoformat(),
            "job_id": self.job_id,
            "slot": self.slot,
            "event": event,
            "state": self.state,
            **fields,
        }
        with self._lock:
            try:
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, default=str, ensure_ascii=False) + "\n")
            except Exception: pass
        # también stdout para tail-f
        print(f"[job{self.job_id[:6]}/s{self.slot}][{event}]"
              f"{' ' + json.dumps(fields, default=str)[:200] if fields else ''}",
              flush=True)

    def transition(self, new_state: str, **fields):
        old = self.state
        self.state = new_state
        self.log("state_transition", from_=old, to=new_state, **fields)
        self._update_summary()

    def _update_summary(self):
        try:
            summary = {
                "job_id": self.job_id, "slot": self.slot,
                "state": self.state,
                "progress_pct": FlowState.progress(self.state),
                "updated_at": datetime.now().isoformat(),
            }
            with open(self.summary_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
        except Exception: pass

    def fail(self, reason: str, **fields):
        self.state = FlowState.FAILED
        self.log("failed", reason=reason, **fields)
        self._update_summary()

    def to_dlq(self, reason: str, snapshot: Dict[str, Any] = None):
        """Mueve este job a la dead-letter queue para análisis manual."""
        self.state = FlowState.DLQ
        path = os.path.join(DLQ_DIR, f"{self.job_id}_slot{self.slot}_{int(time.time())}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "job_id": self.job_id, "slot": self.slot,
                    "reason": reason,
                    "moved_at": datetime.now().isoformat(),
                    "snapshot": snapshot or {},
                }, f, indent=2, default=str)
        except Exception: pass
        self.log("moved_to_dlq", reason=reason)
        self._update_summary()


# ═══════════════════════════════════════════════════════════
#  RETRY POLICY (backoff exponencial + circuit breaker)
# ═══════════════════════════════════════════════════════════
class RetryPolicy:
    """Reintenta una función con backoff exponencial + jitter."""
    def __init__(self, max_attempts: int = 3, initial_delay: float = 2.0,
                 max_delay: float = 30.0, jitter: float = 0.3,
                 retry_on: tuple = (Exception,)):
        self.max_attempts = max_attempts
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.retry_on = retry_on

    def run(self, fn: Callable, *args, **kwargs):
        last_err = None
        delay = self.initial_delay
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn(*args, **kwargs)
            except self.retry_on as e:
                last_err = e
                if attempt == self.max_attempts:
                    raise
                wait = min(delay, self.max_delay)
                wait += wait * self.jitter * (random.random() - 0.5)
                time.sleep(max(0.1, wait))
                delay *= 2
        if last_err:
            raise last_err


# ═══════════════════════════════════════════════════════════
#  HEALTH CHECKER (preflight)
# ═══════════════════════════════════════════════════════════
class HealthChecker:
    """Pre-flight antes de lanzar batches. Cada check devuelve dict con
    {ok, name, detail, latency_ms}."""
    def __init__(self):
        self.checks: List[Dict[str, Any]] = []

    def _check(self, name: str, fn: Callable):
        started = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {str(e)[:100]}"
        c = {"name": name, "ok": ok, "detail": detail,
             "latency_ms": int((time.time() - started) * 1000)}
        self.checks.append(c)
        return c

    def check_morelogin(self):
        return self._check("morelogin", self._morelogin)

    def _morelogin(self):
        try:
            r = requests.post("http://127.0.0.1:40000/api/env/page",
                               json={"pageNo": 1, "pageSize": 1},
                               timeout=4)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            d = r.json()
            total = (d.get("data") or {}).get("total")
            return True, f"{total} profiles available"
        except Exception as e:
            return False, str(e)[:80]

    def check_dashboard(self, port: int = 5055):
        return self._check("dashboard", lambda: self._dashboard(port))

    def _dashboard(self, port):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/healthz",
                              timeout=3)
            return r.status_code < 400, f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)[:80]

    def check_account_pool(self, country: str = "es"):
        return self._check("account_pool",
                            lambda: self._account_pool(country))

    def _account_pool(self, country):
        try:
            import account_manager as am
            pool = AccountPool()
            bad = pool._read_bad()
            used = pool._read_used()
            accs = am.account_manager.list_accounts()
            country_low = country.lower()
            available = sum(1 for a in accs
                            if a.get("status") == "active"
                            and a.get("id") not in bad
                            and a.get("id") not in used
                            and (a.get("country") or "").lower() in
                                (country_low, "spain" if country_low == "es" else country_low))
            return available > 0, (f"{available} cuentas disponibles "
                                    f"(bad={len(bad)}, in_use={len(used)})")
        except Exception as e:
            return False, str(e)[:80]

    def check_event(self, tm_url: str):
        return self._check("event_url", lambda: self._event(tm_url))

    def _event(self, url):
        try:
            r = requests.head(url, timeout=5, allow_redirects=True)
            return r.status_code < 400, f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)[:80]

    def all_ok(self) -> bool:
        return all(c.get("ok") for c in self.checks)

    def report(self) -> Dict[str, Any]:
        rep = {
            "ok": self.all_ok(),
            "checks": self.checks,
            "ts": datetime.now().isoformat(),
        }
        try:
            with open(HEALTH_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rep, default=str) + "\n")
        except Exception: pass
        return rep


# ═══════════════════════════════════════════════════════════
#  RECOVERY de jobs interrumpidos
# ═══════════════════════════════════════════════════════════
def recover_interrupted_jobs() -> Dict[str, Any]:
    """Busca jobs en estado intermedio (no DONE/FAILED/DLQ) y los marca
    interrupted. Llamar al arrancar dashboard."""
    moved = 0
    for fn in os.listdir(JOBS_DIR):
        if not fn.endswith(".summary.json"): continue
        path = os.path.join(JOBS_DIR, fn)
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
            if s.get("state") in (FlowState.DONE, FlowState.FAILED,
                                    FlowState.DLQ, "interrupted"):
                continue
            s["state"] = "interrupted"
            s["interrupted_at"] = datetime.now().isoformat()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
            moved += 1
        except Exception: pass
    return {"ok": True, "interrupted": moved}


# ═══════════════════════════════════════════════════════════
#  DEAD LETTER QUEUE listing
# ═══════════════════════════════════════════════════════════
def list_dlq() -> List[Dict[str, Any]]:
    out = []
    if not os.path.isdir(DLQ_DIR): return out
    for fn in sorted(os.listdir(DLQ_DIR), reverse=True):
        if not fn.endswith(".json"): continue
        try:
            with open(os.path.join(DLQ_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception: pass
    return out


def list_active_jobs() -> List[Dict[str, Any]]:
    """Lista resúmenes de jobs activos (state != done/failed/dlq)."""
    out = []
    for fn in os.listdir(JOBS_DIR):
        if not fn.endswith(".summary.json"): continue
        try:
            with open(os.path.join(JOBS_DIR, fn), encoding="utf-8") as f:
                s = json.load(f)
            if s.get("state") not in (FlowState.DONE, FlowState.FAILED,
                                        FlowState.DLQ, "interrupted"):
                out.append(s)
        except Exception: pass
    return out


def get_job_timeline(job_id: str) -> List[Dict[str, Any]]:
    """Devuelve timeline JSONL de un job."""
    path = os.path.join(JOBS_DIR, f"{job_id}.jsonl")
    out = []
    if not os.path.exists(path): return out
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: out.append(json.loads(line))
                except Exception: pass
    except Exception: pass
    return out


def reset_global_state():
    """RESET TOTAL: borra locks, perfiles activos, used accounts. Para
    arranque limpio tras crash. NO toca bad_credentials ni burned."""
    for f in (MoreLoginPool.ACTIVE_FILE, AccountPool.USED_FILE):
        try: os.unlink(f)
        except Exception: pass
    for fn in os.listdir(LOCKS):
        if fn.endswith(".lock"):
            try: os.unlink(os.path.join(LOCKS, fn))
            except Exception: pass
    return {"ok": True, "reset_at": datetime.now().isoformat()}


def self_test() -> Dict[str, Any]:
    """Test rápido del módulo. NO requiere dashboard ni MoreLogin vivos."""
    r = {"checks": {}}
    # FileLock acquire/release
    try:
        with FileLock("self_test", timeout_s=2):
            pass
        r["checks"]["filelock"] = True
    except Exception as e:
        r["checks"]["filelock"] = False
        r["filelock_err"] = str(e)[:80]
    # Pools instanciables
    try:
        ml = MoreLoginPool()
        ap = AccountPool()
        r["checks"]["pools_instantiable"] = True
        r["checks"]["morelogin_active_file"] = os.path.basename(ml.ACTIVE_FILE)
        r["checks"]["account_used_file"] = os.path.basename(ap.USED_FILE)
    except Exception as e:
        r["checks"]["pools_instantiable"] = False
        r["pools_err"] = str(e)[:80]
    # FlowState progress
    try:
        p = FlowState.progress(FlowState.LOGIN_OK)
        r["checks"]["flow_progress"] = 0 < p < 100
    except Exception:
        r["checks"]["flow_progress"] = False
    # JobLogger crea archivos
    try:
        jl = JobLogger(job_id=f"selftest_{int(time.time())}", slot=0)
        jl.log("self_test")
        jl.transition(FlowState.PROFILE_LEASED)
        r["checks"]["job_logger"] = os.path.exists(jl.jsonl_path) \
            and os.path.exists(jl.summary_path)
    except Exception as e:
        r["checks"]["job_logger"] = False
        r["jl_err"] = str(e)[:80]
    # Retry policy
    try:
        rp = RetryPolicy(max_attempts=2, initial_delay=0.05)
        attempts = [0]
        def fn():
            attempts[0] += 1
            if attempts[0] < 2: raise ValueError("retry me")
            return "ok"
        rp.run(fn)
        r["checks"]["retry_policy"] = attempts[0] == 2
    except Exception:
        r["checks"]["retry_policy"] = False
    r["ok"] = all(r["checks"].values())
    r["passed"] = sum(1 for v in r["checks"].values() if v)
    r["total"] = len(r["checks"])
    return r


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(self_test(), indent=2))
