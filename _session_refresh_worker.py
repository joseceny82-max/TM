"""_session_refresh_worker.py — Worker dedicado a refrescar storage_state.

Wrapper especializado sobre `account_validator.validate_batch()` con flags
configurados para SOLO hacer login y guardar storage_state (sin compras).

Diferencia vs validate_batch genérico:
  - Force `updateStorage=True`  (refresca/crea storage_state canónico)
  - Force `verifyPassword=True` (login fresh, no fía de cookies viejas)
  - Force `clearHistory=False`  (preservar storage_state recién guardado)
  - Force `bumpToRegistrations=False` (no penalizar al fallar)
  - Selector inteligente: filtra cuentas que REALMENTE necesitan refresh
    (missing / warning / expired), saltando las active.

Engine actual: MoreLogin (vía bot_orchestrator/account_validator).
TODO: integrar fallback Camoufox cuando MoreLogin queda bloqueado por Akamai.
       Por ahora, `engine_strategy='auto'` solo usa MoreLogin pero deja hook
       para Camoufox en futura iteración.

Public API:
    start_refresh_batch(account_ids, max_parallel=3, engine='auto') -> {job_id}
    pick_candidates(status_filter='auto', limit=None) -> [account_id]
    get_job(job_id) -> dict
    list_jobs() -> list
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional, Literal

import account_manager as am
import account_validator
import session_status
import camoufox_login_only

# Estados de sesión que JUSTIFICAN un refresh
_STATUS_NEEDS_REFRESH = ("missing", "expired", "warning")

EngineStrategy = Literal["auto", "morelogin", "camoufox"]


def pick_candidates(status_filter: str = "auto",
                    limit: Optional[int] = None,
                    only_purchase_ready: bool = True) -> List[str]:
    """Devuelve IDs de cuentas que necesitan refresh.

    Args:
      status_filter:
        - "auto"     → missing + expired + warning (default)
        - "missing"  → solo cuentas sin sesión guardada
        - "expired"  → solo cuentas con sesión >14d
        - "warning"  → solo cuentas con sesión 7-14d
        - "all"      → todas (incluso active, fuerza refresh)
      limit: máximo de candidatas (None = todas)
      only_purchase_ready: si True, filtra a cuentas con purchase_ready=True
                          (cuentas registradas exitosamente y validadas).

    Returns: list[account_id]
    """
    accs = am.account_manager.list_accounts()

    # Filtro purchase_ready primero (más restrictivo)
    if only_purchase_ready:
        accs = [a for a in accs if a.get("purchase_ready") is True
                or a.get("credentials_verified") is True]

    # Selección por status
    if status_filter == "all":
        target = accs
    else:
        if status_filter == "auto":
            allowed = set(_STATUS_NEEDS_REFRESH)
        elif status_filter in ("missing", "expired", "warning", "active"):
            allowed = {status_filter}
        else:
            allowed = set(_STATUS_NEEDS_REFRESH)
        target = []
        for a in accs:
            info = session_status.session_info_for(a)
            if info["session_status"] in allowed:
                target.append(a)

    # Orden: missing primero, luego expired (más urgente), luego warning
    def _priority(acc):
        info = session_status.session_info_for(acc)
        return {"missing": 0, "expired": 1, "warning": 2, "active": 3}.get(
            info["session_status"], 9)
    target.sort(key=_priority)

    ids = [a["id"] for a in target if a.get("id")]
    if limit:
        ids = ids[:limit]
    return ids


def start_refresh_batch(account_ids: List[str],
                         max_parallel: int = 3,
                         engine: EngineStrategy = "auto",
                         headless: bool = True) -> Dict[str, Any]:
    """Arranca un job de refresh de sesiones.

    engine:
      - 'morelogin'  → motor MoreLogin (vía account_validator.validate_batch)
      - 'camoufox'   → motor Camoufox (vía camoufox_login_only.refresh_sessions_batch_camoufox)
      - 'auto'       → Camoufox por defecto (MoreLogin queda bloqueado por
                       Akamai en /auth desde 2026-05-14, según memoria proyecto).

    Args:
      account_ids: IDs de cuentas a procesar
      max_parallel: workers concurrentes (default 3)
      engine: estrategia (ver arriba)
      headless: solo aplica a Camoufox (default True)

    Returns: {ok, job_id, total, engine}
    """
    if not account_ids:
        return {"ok": False, "error": "account_ids_empty"}

    # Resolver 'auto' → preferir Camoufox (MoreLogin bloqueado por Akamai)
    engine_used = engine
    if engine == "auto":
        engine_used = "camoufox"

    if engine_used == "camoufox":
        result = camoufox_login_only.refresh_sessions_batch_camoufox(
            account_ids, max_parallel=max_parallel, headless=headless)
        if result.get("ok"):
            result["purpose"] = "session_refresh"
        return result

    # engine_used == "morelogin"
    options = {
        "verifySession": True,
        "verifyPassword": True,
        "bumpToRegistrations": False,
        "clearHistory": False,
        "updateStorage": True,
        "skipRecentlyVerified": False,
        "markForReview": False,
    }
    result = account_validator.validate_batch(
        account_ids,
        max_parallel=max_parallel,
        force_password_test=True,
        options=options,
    )
    if result.get("ok"):
        result["engine"] = "morelogin"
        result["purpose"] = "session_refresh"
    return result


def get_job(job_id: str) -> Dict[str, Any]:
    """Busca job por ID en AMBOS motores (Camoufox primero, MoreLogin después)."""
    # 1) Camoufox
    cf_job = camoufox_login_only.get_job(job_id)
    if cf_job and not cf_job.get("error"):
        cf_job["job_type"] = "session_refresh"
        return cf_job
    # 2) MoreLogin (validate_batch)
    ml_job = account_validator.get_job(job_id)
    if isinstance(ml_job, dict) and not ml_job.get("error"):
        opts = ml_job.get("options", {}) or {}
        if opts.get("updateStorage") and not opts.get("bumpToRegistrations"):
            ml_job["job_type"] = "session_refresh"
            ml_job.setdefault("engine", "morelogin")
        return ml_job
    return {"ok": False, "error": "job_not_found"}


def list_jobs() -> List[Dict[str, Any]]:
    """Lista jobs de refresh de AMBOS motores."""
    out: List[Dict[str, Any]] = []
    # Camoufox jobs (todos son session_refresh)
    for j in camoufox_login_only.list_jobs():
        j2 = dict(j)
        j2["job_type"] = "session_refresh"
        out.append(j2)
    # MoreLogin jobs (filtrar por options)
    for j in account_validator.list_jobs():
        opts = j.get("options", {}) or {}
        if opts.get("updateStorage") and not opts.get("bumpToRegistrations"):
            j2 = dict(j)
            j2["job_type"] = "session_refresh"
            j2.setdefault("engine", "morelogin")
            out.append(j2)
    return out


# ════════════════════════════════════════════════════════════════════
#  Smoke CLI
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    """Test: lista candidatas (sin lanzar batch)."""
    ids = pick_candidates(status_filter="auto", limit=20)
    print(f"Candidates needing refresh (auto): {len(ids)}")
    if ids:
        # Mostrar primeros 5 con detalle
        accs = am.account_manager.list_accounts()
        idx = {a["id"]: a for a in accs}
        for aid in ids[:5]:
            a = idx.get(aid, {})
            info = session_status.session_info_for(a)
            email = a.get("tm_email", "?")[:40]
            st = info["session_status"]
            dl = info["session_days_left"]
            print(f"  - {aid}  {email:40s}  {st:8s}  left={dl}d")
    print()

    summ = session_status.summary(am.account_manager.list_accounts())
    print(f"Status summary: {summ}")
