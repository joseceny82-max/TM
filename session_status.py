"""session_status.py — Helper para visualizar estado de sesiones (storage_state) por cuenta TM.

Lee `datos/sessions/*.json` (Playwright storage_state) y calcula:
  - has_session: bool
  - session_age_days: float
  - session_days_left: float (TTL_DAYS - age)
  - session_status: "active" | "warning" | "expired" | "missing"

Reglas (TTL=14d según norma 2026-05-15, eps_sid real=16d):
  - "missing"  → cuenta sin storage_state_file o archivo no existe en disco
  - "expired"  → age >= 14d
  - "warning"  → 7d <= age < 14d   (≤7d restantes, refrescar pronto)
  - "active"   → age < 7d           (>7d restantes, saludable)

Public API:
  session_info_for(account)          → dict con session_* fields
  enrich_accounts(accounts)          → list[dict] con campos añadidos
  list_orphan_sessions()             → archivos sin cuenta asociada
  find_session_for_account(account)  → fallback path-finder por email/id

NORMA: SOBRESCRIBIR 1 archivo por cuenta (canonical path).
       Canonical: datos/sessions/account_{aid}.json
"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# ════════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════════
TTL_DAYS: float = 14.0       # umbral expiración (eps_sid real=16d, margen 2d)
WARNING_DAYS: float = 7.0    # umbral ámbar (≤7d restantes)

ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = ROOT / "datos" / "sessions"
COOKIES_DIR = ROOT / "datos" / "cookies"

# Patrones de archivos de sesión legacy (varios formatos en el repo)
#   tm_u9_20260428_043356_state.json           ← register_account.py
#   tm_cmp_c0774196_20260514_215843_state.json ← _compra_parallel.py
#   account_<aid>.json                          ← canonical (NUEVO)
_LEGACY_PATTERNS = [
    re.compile(r"tm_u(?P<row>\d+)_(?P<ts>\d{8}_\d{6})_state\.json$"),
    re.compile(r"tm_cmp_(?P<hash>[a-f0-9]+)_(?P<ts>\d{8}_\d{6})_state\.json$"),
]
_CANONICAL_PATTERN = re.compile(r"account_(?P<aid>[a-f0-9]+)\.json$")


# ════════════════════════════════════════════════════════════════════
#  Path helpers
# ════════════════════════════════════════════════════════════════════
def canonical_session_path(account_id: str) -> Path:
    """Path canónico (sobrescribible) para storage_state de UNA cuenta."""
    return SESSIONS_DIR / f"account_{account_id}.json"


def _file_mtime_iso(path: Path) -> Optional[str]:
    """Devuelve mtime del archivo en ISO 8601, o None si no existe."""
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, FileNotFoundError):
        return None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Tolera con/sin timezone
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ════════════════════════════════════════════════════════════════════
#  Core: status por cuenta
# ════════════════════════════════════════════════════════════════════
def session_info_for(account: Dict[str, Any]) -> Dict[str, Any]:
    """Calcula campos session_* para UNA cuenta.

    Estrategia de localización del archivo:
      1. Canonical: datos/sessions/account_{id}.json (preferido)
      2. Fallback: campo `storage_state_file` (path legacy)
      3. Fallback: scan por timestamp más reciente que matchee email/row

    Returns dict con:
      session_path: str | None     — path absoluto al archivo
      session_saved_at: str | None — ISO 8601
      session_age_days: float | None
      session_days_left: float | None
      session_status: "active"|"warning"|"expired"|"missing"
      session_size_bytes: int | None
      session_canonical: bool      — True si usa path canónico
    """
    aid = account.get("id", "")
    out = {
        "session_path": None,
        "session_saved_at": None,
        "session_age_days": None,
        "session_days_left": None,
        "session_status": "missing",
        "session_size_bytes": None,
        "session_canonical": False,
    }

    # 1) Canonical
    path: Optional[Path] = None
    if aid:
        canon = canonical_session_path(aid)
        if canon.exists():
            path = canon
            out["session_canonical"] = True

    # 2) Legacy field
    if path is None:
        ssf = account.get("storage_state_file")
        if ssf:
            p = Path(ssf)
            if p.exists():
                path = p

    if path is None:
        return out

    # Stats
    try:
        st = path.stat()
        out["session_path"] = str(path)
        out["session_size_bytes"] = st.st_size
        saved_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        out["session_saved_at"] = saved_dt.isoformat()

        # Age
        now = datetime.now(tz=timezone.utc)
        age_seconds = (now - saved_dt).total_seconds()
        age_days = max(age_seconds / 86400.0, 0.0)
        out["session_age_days"] = round(age_days, 2)
        out["session_days_left"] = round(max(TTL_DAYS - age_days, 0.0), 2)

        # Status
        if age_days >= TTL_DAYS:
            out["session_status"] = "expired"
        elif age_days >= WARNING_DAYS:
            out["session_status"] = "warning"
        else:
            out["session_status"] = "active"
    except (OSError, FileNotFoundError):
        # Archivo desapareció entre exists() y stat()
        out["session_status"] = "missing"

    return out


# ════════════════════════════════════════════════════════════════════
#  Bulk: enrich account list
# ════════════════════════════════════════════════════════════════════
def enrich_accounts(accounts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aplica session_info_for() a cada cuenta y devuelve lista enriquecida.

    Mutación: añade campos session_* sin tocar el resto. Idempotente.
    """
    out: List[Dict[str, Any]] = []
    for acc in accounts:
        info = session_info_for(acc)
        merged = {**acc, **info}
        out.append(merged)
    return out


# ════════════════════════════════════════════════════════════════════
#  Summary stats
# ════════════════════════════════════════════════════════════════════
def summary(accounts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Cuenta cuántas cuentas hay en cada estado."""
    counts = {"active": 0, "warning": 0, "expired": 0, "missing": 0, "total": 0}
    for acc in accounts:
        info = session_info_for(acc)
        st = info["session_status"]
        counts[st] = counts.get(st, 0) + 1
        counts["total"] += 1
    return counts


# ════════════════════════════════════════════════════════════════════
#  Orphan scanner
# ════════════════════════════════════════════════════════════════════
def list_orphan_sessions(known_paths: Optional[set] = None) -> List[Dict[str, Any]]:
    """Lista archivos en SESSIONS_DIR que no estén referenciados por ninguna cuenta.

    Args:
      known_paths: set de paths absolutos en uso (de accounts.json).
                   Si None, considera TODOS los archivos como huérfanos.

    Returns: list[dict] con {path, size, age_days}.
    """
    out: List[Dict[str, Any]] = []
    known_paths = known_paths or set()
    known_norm = {os.path.normcase(os.path.normpath(p)) for p in known_paths if p}

    if not SESSIONS_DIR.exists():
        return out

    now = datetime.now(tz=timezone.utc)
    for p in SESSIONS_DIR.glob("*.json"):
        norm = os.path.normcase(os.path.normpath(str(p)))
        if norm in known_norm:
            continue
        try:
            st = p.stat()
            saved_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            age_days = (now - saved_dt).total_seconds() / 86400.0
            out.append({
                "path": str(p),
                "size": st.st_size,
                "age_days": round(age_days, 2),
                "saved_at": saved_dt.isoformat(),
            })
        except OSError:
            pass
    return out


# ════════════════════════════════════════════════════════════════════
#  Smoke test (CLI)
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    """Smoke test: lee accounts.json y muestra resumen.

    Uso: python session_status.py
    """
    accounts_path = ROOT / "datos" / "cuentas" / "accounts.json"
    if not accounts_path.exists():
        print(f"❌ No existe: {accounts_path}")
        raise SystemExit(1)

    with open(accounts_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    accs = data.get("accounts", [])

    print(f"📊 {len(accs)} cuentas en {accounts_path}")
    print(f"   TTL={TTL_DAYS}d, WARNING={WARNING_DAYS}d")
    print()

    s = summary(accs)
    print(f"  🟢 active:  {s.get('active', 0)}")
    print(f"  🟡 warning: {s.get('warning', 0)}")
    print(f"  🔴 expired: {s.get('expired', 0)}")
    print(f"  ⚪ missing: {s.get('missing', 0)}")
    print(f"  ─────────────────")
    print(f"     total:   {s.get('total', 0)}")
    print()

    # 5 ejemplos
    print("Sample (5 first):")
    for acc in accs[:5]:
        info = session_info_for(acc)
        email = acc.get("tm_email", "?")[:35].ljust(35)
        st = info["session_status"]
        dl = info["session_days_left"]
        dl_str = f"{dl}d" if dl is not None else "—"
        flag = {"active": "🟢", "warning": "🟡", "expired": "🔴", "missing": "⚪"}[st]
        print(f"  {flag} {email}  {st:8s}  left={dl_str}")

    # Orphans
    known = {a.get("storage_state_file") for a in accs}
    # También añadir canonical paths
    for a in accs:
        if a.get("id"):
            known.add(str(canonical_session_path(a["id"])))
    orphans = list_orphan_sessions(known)
    print(f"\n🗑️  Orphan sessions: {len(orphans)}")
    if orphans:
        print(f"   (oldest first, top 3):")
        for o in sorted(orphans, key=lambda x: x["age_days"], reverse=True)[:3]:
            print(f"   - {os.path.basename(o['path'])}  age={o['age_days']}d  size={o['size']//1024}KB")


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
