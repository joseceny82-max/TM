"""accounts_csv_sync.py — CSV bidireccional de Cuentas + Tarjetas.

Mantiene un CSV editable en `datos/cuentas/cuentas_tarjetas.csv` sincronizado
con accounts.json y cards.json en AMBAS DIRECCIONES, en tiempo real.

Flujo:
  Dashboard añade/modifica/borra cuenta  → sync_out() reescribe CSV
  Usuario edita el CSV en Excel/VSCode   → watcher detecta mtime, sync_in()
                                            merge-ea cambios al JSON

Estrategia:
  • Polling de mtime cada 1s (sin dependencia de watchdog)
  • Último-en-escribir gana, pero con tolerancia (0.5s) para evitar races
  • Lock-file `.lock` cuando el worker está escribiendo el CSV
  • CSV con header fijo (cambios de columnas no rompen nada)
  • ID oculto en columna `_id` para tracking del record

Columnas del CSV:
  _id, label, status, tm_email, tm_password, contact_email,
  first_name, last_name, dob, nationality, passport, passport_country,
  gender, phone_prefix, phone, address1, address2, city, state, zip,
  country, notes, card_number, card_holder, card_exp (MM/YY),
  card_cvv, created_at

API:
  get_csv_path()          → ruta absoluta del CSV
  init()                  → crea CSV si no existe + arranca watcher
  sync_out()              → fuerza JSON → CSV ahora
  sync_in()               → fuerza CSV → JSON ahora
  start_watcher()         → arranca thread poll (1s)
  stop_watcher()
  get_status()            → {watcher_running, csv_mtime, json_mtime, last_sync}
  self_test()
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple

import config
import json_atomic
import account_manager

_CSV_FILENAME = "cuentas_tarjetas.csv"
_LOCK_FILENAME = "cuentas_tarjetas.lock"
_ACCOUNTS_KEY = "accounts.json"
_CARDS_KEY = "cards.json"

_lock = threading.RLock()
_watcher_state = {
    "running": False, "thread": None, "stop_event": None,
    "last_csv_mtime": 0.0, "last_json_mtime": 0.0,
    "last_sync_in_ts": "", "last_sync_out_ts": "",
    "sync_in_count": 0, "sync_out_count": 0,
    "last_error": "",
}

# Header en orden fijo
CSV_COLUMNS = [
    "_id",
    "label", "status",
    "tm_email", "tm_password", "contact_email", "contact_password",
    "first_name", "last_name", "dob", "gender",
    "nationality", "passport", "passport_country",
    "phone_prefix", "phone",
    "address1", "address2", "city", "state", "zip", "country",
    "notes",
    # Tarjeta asociada (una por cuenta en este CSV; multi-card via dashboard)
    "card_number", "card_holder", "card_exp", "card_cvv",
    "created_at", "purchases", "sessions",
]


# ═══════════════════════════════════════════════════════════════
#  Paths
# ═══════════════════════════════════════════════════════════════
def _cuentas_dir() -> str:
    # Usa la misma carpeta que accounts.json
    acc_path = config.ruta(_ACCOUNTS_KEY)
    return os.path.dirname(acc_path)


def get_csv_path() -> str:
    return os.path.join(_cuentas_dir(), _CSV_FILENAME)


def _lock_path() -> str:
    return os.path.join(_cuentas_dir(), _LOCK_FILENAME)


def _safe_mtime(path: str) -> float:
    try: return os.path.getmtime(path)
    except Exception: return 0.0


# ═══════════════════════════════════════════════════════════════
#  Helpers card_exp (MM/YY)
# ═══════════════════════════════════════════════════════════════
def _format_card_exp(month: Any, year: Any) -> str:
    """Combina mes/año en formato MM/YY (ej: '10/28').

    month: '10' | '1' | '01' | int → siempre 2 dígitos ('01', '10')
    year:  '2028' | '28' | int     → siempre 2 dígitos ('28')
    Si ambos vacíos → ''.
    """
    m = str(month or "").strip()
    y = str(year or "").strip()
    if not m and not y:
        return ""
    # Normaliza mes a 2 dígitos
    if m.isdigit():
        m_pad = f"{int(m):02d}"
    else:
        m_pad = m.zfill(2) if m else "??"
    # Normaliza año: si viene 4 dígitos → toma últimos 2; si 1 → pad a 2
    if len(y) == 4 and y.isdigit():
        y_short = y[2:]
    elif len(y) == 2:
        y_short = y
    elif y.isdigit():
        y_short = y.zfill(2)[-2:]
    elif y:
        y_short = y[-2:]
    else:
        y_short = "??"
    return f"{m_pad}/{y_short}"


def _parse_card_exp(value: str) -> Tuple[str, str]:
    """Parse 'MM/YY' | 'MM/YYYY' | 'MM-YY' | 'MM.YY' → (month_2d, year_4d).

    Devuelve ('', '') si vacío.
    Asume siglo 21 al expandir años de 2 dígitos.
    """
    if not value:
        return "", ""
    s = str(value).strip().replace("-", "/").replace(".", "/").replace(" ", "")
    if "/" not in s:
        # Sólo mes (sin año) o input raro
        if s.isdigit() and 1 <= len(s) <= 2:
            return s.zfill(2), ""
        return "", ""
    parts = s.split("/", 1)
    m_raw = parts[0].strip()
    y_raw = parts[1].strip()
    # Mes
    if m_raw.isdigit():
        m = f"{int(m_raw):02d}"
    else:
        m = m_raw.zfill(2)
    # Año → expandir a 4 dígitos
    if len(y_raw) == 2 and y_raw.isdigit():
        y = "20" + y_raw
    elif len(y_raw) == 1 and y_raw.isdigit():
        y = "200" + y_raw
    elif len(y_raw) == 4 and y_raw.isdigit():
        y = y_raw
    elif len(y_raw) == 3 and y_raw.isdigit():
        y = "2" + y_raw  # 028 → 2028 (improbable pero por si acaso)
    else:
        y = y_raw
    return m, y


# ═══════════════════════════════════════════════════════════════
#  Row <-> dict conversion
# ═══════════════════════════════════════════════════════════════
def _acc_card_to_row(acc: Dict[str, Any], card: Optional[Dict[str, Any]]) -> Dict[str, str]:
    def g(d, k, default=""): return str((d or {}).get(k, default) or "")
    row = {
        "_id": g(acc, "id"),
        "label": g(acc, "label"),
        "status": g(acc, "status", "active"),
        "tm_email": g(acc, "tm_email") or g(acc, "email"),
        "tm_password": g(acc, "tm_password") or g(acc, "password"),
        "contact_email": g(acc, "contact_email"),
        "contact_password": g(acc, "contact_password"),
        "first_name": g(acc, "first_name") or g(acc, "fname"),
        "last_name": g(acc, "last_name") or g(acc, "lname"),
        "dob": g(acc, "dob"),
        "gender": g(acc, "gender"),
        "nationality": g(acc, "nationality"),
        "passport": g(acc, "passport") or g(acc, "dni"),
        "passport_country": g(acc, "passport_country"),
        "phone_prefix": g(acc, "phone_prefix"),
        "phone": g(acc, "phone"),
        "address1": g(acc, "address1") or g(acc, "addr1"),
        "address2": g(acc, "address2") or g(acc, "addr2"),
        "city": g(acc, "city"),
        "state": g(acc, "state"),
        "zip": g(acc, "zip") or g(acc, "postcode"),
        "country": g(acc, "country"),
        "notes": g(acc, "notes"),
        "card_number": g(card, "number"),
        "card_holder": g(card, "holder") or g(card, "card_holder"),
        "card_exp": _format_card_exp(g(card, "exp_month") or g(card, "month"),
                                       g(card, "exp_year") or g(card, "year")),
        "card_cvv": g(card, "cvv"),
        "created_at": g(acc, "created_at"),
        "purchases": g(acc, "purchases", "0"),
        "sessions": g(acc, "sessions", "0"),
    }
    return row


def _row_to_acc_card(row: Dict[str, str]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    acc = {k: (v or "").strip() for k, v in row.items() if not k.startswith("card_") and k not in ("_id", "purchases", "sessions")}
    acc["id"] = (row.get("_id") or "").strip() or uuid.uuid4().hex[:8]
    if row.get("created_at"):
        acc["created_at"] = row["created_at"]
    else:
        acc["created_at"] = datetime.now().isoformat()
    try: acc["purchases"] = int(row.get("purchases") or 0)
    except Exception: acc["purchases"] = 0
    try: acc["sessions"] = int(row.get("sessions") or 0)
    except Exception: acc["sessions"] = 0
    # status default
    if not acc.get("status"): acc["status"] = "active"

    card = None
    card_num = (row.get("card_number") or "").strip()
    if card_num:
        # Parse card_exp formato MM/YY o MM/YYYY (preferido) — fallback a campos viejos
        exp_raw = (row.get("card_exp") or "").strip()
        exp_month, exp_year = _parse_card_exp(exp_raw)
        if not exp_month and not exp_year:
            # Retrocompatibilidad: si CSV viejo tenía las dos columnas separadas
            exp_month = (row.get("card_exp_month") or "").strip()
            exp_year = (row.get("card_exp_year") or "").strip()
        card = {
            "id": uuid.uuid4().hex[:8],
            "account_id": acc["id"],
            "number": card_num,
            "holder": (row.get("card_holder") or "").strip(),
            "exp_month": exp_month,
            "exp_year": exp_year,
            "cvv": (row.get("card_cvv") or "").strip(),
            "created_at": acc["created_at"],
        }
    return acc, card


# ═══════════════════════════════════════════════════════════════
#  Lock-file primitives (para evitar race CSV↔JSON)
# ═══════════════════════════════════════════════════════════════
def _acquire_lock(timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    lp = _lock_path()
    while time.time() < deadline:
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            # Comprueba si está stale (> 10s)
            try:
                if time.time() - os.path.getmtime(lp) > 10:
                    os.unlink(lp)
                    continue
            except Exception:
                pass
            time.sleep(0.05)
    return False


def _release_lock():
    try: os.unlink(_lock_path())
    except Exception: pass


# ═══════════════════════════════════════════════════════════════
#  Sync JSON → CSV (export)
# ═══════════════════════════════════════════════════════════════
def sync_out() -> Dict[str, Any]:
    """Lee accounts.json + cards.json y reescribe el CSV."""
    with _lock:
        if not _acquire_lock():
            return {"ok": False, "error": "lock_busy"}
        try:
            os.makedirs(_cuentas_dir(), exist_ok=True)
            accs = account_manager.account_manager.list_accounts()
            cards = account_manager.account_manager.list_cards()

            # Index cards por account_id (1ª ocurrencia)
            card_by_acc = {}
            for c in cards:
                acc_id = c.get("account_id")
                if acc_id and acc_id not in card_by_acc:
                    card_by_acc[acc_id] = c

            csv_path = get_csv_path()
            # Escribe atomic (tmp + rename)
            tmp_path = csv_path + ".tmp"
            with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                w.writeheader()
                for acc in accs:
                    row = _acc_card_to_row(acc, card_by_acc.get(acc.get("id")))
                    w.writerow(row)
            shutil.move(tmp_path, csv_path)

            _watcher_state["last_csv_mtime"] = _safe_mtime(csv_path)
            _watcher_state["last_json_mtime"] = max(
                _safe_mtime(config.ruta(_ACCOUNTS_KEY)),
                _safe_mtime(config.ruta(_CARDS_KEY)),
            )
            _watcher_state["last_sync_out_ts"] = datetime.now().isoformat()
            _watcher_state["sync_out_count"] = _watcher_state.get("sync_out_count", 0) + 1
            return {
                "ok": True, "csv_path": csv_path,
                "accounts_written": len(accs),
                "cards_written": sum(1 for a in accs if card_by_acc.get(a.get("id"))),
            }
        except Exception as e:
            _watcher_state["last_error"] = f"sync_out: {e}"
            return {"ok": False, "error": str(e)}
        finally:
            _release_lock()


# ═══════════════════════════════════════════════════════════════
#  Sync CSV → JSON (import)
# ═══════════════════════════════════════════════════════════════
def sync_in() -> Dict[str, Any]:
    """Lee el CSV y reconstruye accounts.json + cards.json."""
    csv_path = get_csv_path()
    if not os.path.isfile(csv_path):
        return {"ok": False, "error": "csv_not_found"}
    with _lock:
        if not _acquire_lock():
            return {"ok": False, "error": "lock_busy"}
        try:
            new_accs = []
            new_cards = []
            errors = 0
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Ignora filas sin nada útil
                    if not any((row.get(k) or "").strip() for k in ("tm_email", "label", "first_name", "last_name", "_id")):
                        continue
                    try:
                        acc, card = _row_to_acc_card(row)
                        new_accs.append(acc)
                        if card: new_cards.append(card)
                    except Exception:
                        errors += 1
                        continue

            # Preserva campos no presentes en CSV (ej: cookies, session_data)
            existing_accs = {a["id"]: a for a in account_manager.account_manager.list_accounts() if a.get("id")}
            for a in new_accs:
                old = existing_accs.get(a["id"])
                if old:
                    # merge: campos nuevos del CSV ganan, el resto se preserva
                    merged = dict(old); merged.update(a)
                    a.clear(); a.update(merged)

            # Persist
            json_atomic.save(config.ruta(_ACCOUNTS_KEY), {"accounts": new_accs})
            # Cards: sólo reemplazamos las que aparecen en el CSV (por account_id)
            # Las otras (multi-card, o cards sin account) se preservan
            existing_cards = account_manager.account_manager.list_cards()
            updated_acc_ids = {c["account_id"] for c in new_cards if c.get("account_id")}
            kept_cards = [c for c in existing_cards if c.get("account_id") not in updated_acc_ids]
            merged_cards = kept_cards + new_cards
            json_atomic.save(config.ruta(_CARDS_KEY), {"cards": merged_cards})

            _watcher_state["last_csv_mtime"] = _safe_mtime(csv_path)
            _watcher_state["last_json_mtime"] = max(
                _safe_mtime(config.ruta(_ACCOUNTS_KEY)),
                _safe_mtime(config.ruta(_CARDS_KEY)),
            )
            _watcher_state["last_sync_in_ts"] = datetime.now().isoformat()
            _watcher_state["sync_in_count"] = _watcher_state.get("sync_in_count", 0) + 1
            return {
                "ok": True,
                "accounts_imported": len(new_accs),
                "cards_imported": len(new_cards),
                "parse_errors": errors,
            }
        except Exception as e:
            _watcher_state["last_error"] = f"sync_in: {e}"
            return {"ok": False, "error": str(e)}
        finally:
            _release_lock()


# ═══════════════════════════════════════════════════════════════
#  Watcher background (poll mtime cada 1s)
# ═══════════════════════════════════════════════════════════════
def _watcher_loop(stop_evt: threading.Event, poll_s: float = 1.0):
    while not stop_evt.is_set():
        try:
            csv_path = get_csv_path()
            j_acc = _safe_mtime(config.ruta(_ACCOUNTS_KEY))
            j_card = _safe_mtime(config.ruta(_CARDS_KEY))
            j_mtime = max(j_acc, j_card)
            c_mtime = _safe_mtime(csv_path)

            last_csv = _watcher_state.get("last_csv_mtime", 0)
            last_json = _watcher_state.get("last_json_mtime", 0)

            # CSV más reciente → import (user editó)
            if c_mtime > last_csv + 0.5 and c_mtime >= j_mtime:
                sync_in()
            # JSON más reciente → export (dashboard editó)
            elif j_mtime > last_json + 0.5 and j_mtime > c_mtime:
                sync_out()

        except Exception as e:
            _watcher_state["last_error"] = f"watcher_loop: {e}"
        # Sleep respetando stop_evt
        stop_evt.wait(poll_s)


def start_watcher(poll_s: float = 1.0) -> Dict[str, Any]:
    if _watcher_state.get("running"):
        return {"ok": True, "status": "already_running"}
    init()  # garantiza CSV existe
    evt = threading.Event()
    th = threading.Thread(target=_watcher_loop, args=(evt, poll_s),
                            daemon=True, name="accounts_csv_watcher")
    _watcher_state.update({
        "running": True, "thread": th, "stop_event": evt,
    })
    th.start()
    return {"ok": True, "status": "started", "poll_s": poll_s}


def stop_watcher() -> Dict[str, Any]:
    if not _watcher_state.get("running"):
        return {"ok": True, "status": "not_running"}
    evt = _watcher_state.get("stop_event")
    if evt: evt.set()
    _watcher_state["running"] = False
    return {"ok": True, "status": "stopped"}


def init() -> Dict[str, Any]:
    """Crea el CSV si no existe, con las cuentas actuales."""
    os.makedirs(_cuentas_dir(), exist_ok=True)
    csv_path = get_csv_path()
    if not os.path.isfile(csv_path):
        r = sync_out()
        return {"ok": True, "created": True, **r}
    return {"ok": True, "created": False, "csv_path": csv_path}


def get_status() -> Dict[str, Any]:
    csv_path = get_csv_path()
    acc_path = config.ruta(_ACCOUNTS_KEY)
    card_path = config.ruta(_CARDS_KEY)
    def _iso(t): return datetime.fromtimestamp(t).isoformat() if t else ""
    csv_exists = os.path.isfile(csv_path)
    csv_size = os.path.getsize(csv_path) if csv_exists else 0
    return {
        "watcher_running": _watcher_state.get("running", False),
        "csv_path": csv_path,
        "csv_exists": csv_exists,
        "csv_size_bytes": csv_size,
        "csv_mtime": _iso(_safe_mtime(csv_path)),
        "accounts_json_mtime": _iso(_safe_mtime(acc_path)),
        "cards_json_mtime": _iso(_safe_mtime(card_path)),
        "last_sync_in_ts": _watcher_state.get("last_sync_in_ts", ""),
        "last_sync_out_ts": _watcher_state.get("last_sync_out_ts", ""),
        "sync_in_count": _watcher_state.get("sync_in_count", 0),
        "sync_out_count": _watcher_state.get("sync_out_count", 0),
        "last_error": _watcher_state.get("last_error", ""),
    }


def open_folder() -> Dict[str, Any]:
    """Abre la carpeta que contiene el CSV en el explorador del sistema."""
    path = _cuentas_dir()
    try:
        if os.name == "nt":  # Windows
            os.startfile(path)
        elif os.name == "posix":
            import subprocess
            opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
            subprocess.Popen([opener, path])
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e), "path": path}


# ═══════════════════════════════════════════════════════════════
#  Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    results = {"checks": {}}

    # 1. Paths
    p = get_csv_path()
    results["checks"]["csv_path_valid"] = bool(p and p.endswith(".csv"))
    results["details_csv_path"] = p

    # 2. init creates CSV if missing
    r = init()
    results["checks"]["init_ok"] = bool(r.get("ok"))
    results["checks"]["csv_exists_after_init"] = os.path.isfile(p)

    # 3. sync_out works
    r = sync_out()
    results["checks"]["sync_out_ok"] = bool(r.get("ok"))

    # 4. sync_in works  on the CSV we just wrote
    r = sync_in()
    results["checks"]["sync_in_ok"] = bool(r.get("ok"))

    # 5. Round-trip: add via JSON → sync_out → read CSV → sync_in → still present
    test_acc = {
        "id": "csv_test_xxx",
        "label": "__CSV TEST__",
        "tm_email": "csv@test.invalid",
        "first_name": "Csv",
        "last_name": "Test",
        "status": "active",
        "purchases": 0,
        "sessions": 0,
    }
    # inject directly into JSON to avoid deleting existing
    d = json_atomic.load(config.ruta(_ACCOUNTS_KEY), default={"accounts": []})
    # Clean stale test
    d["accounts"] = [a for a in d["accounts"] if a.get("id") != test_acc["id"]]
    d["accounts"].append(test_acc)
    json_atomic.save(config.ruta(_ACCOUNTS_KEY), d)

    sync_out()
    with open(p, "r", encoding="utf-8-sig") as f:
        csv_text = f.read()
    results["checks"]["roundtrip_has_email"] = "csv@test.invalid" in csv_text
    results["checks"]["roundtrip_has_id"] = "csv_test_xxx" in csv_text

    sync_in()
    after = [a for a in account_manager.account_manager.list_accounts() if a.get("id") == "csv_test_xxx"]
    results["checks"]["roundtrip_survives_import"] = len(after) == 1

    # Cleanup
    d = json_atomic.load(config.ruta(_ACCOUNTS_KEY), default={"accounts": []})
    d["accounts"] = [a for a in d["accounts"] if a.get("id") != test_acc["id"]]
    json_atomic.save(config.ruta(_ACCOUNTS_KEY), d)
    sync_out()

    # 6. Watcher lifecycle
    s = start_watcher(poll_s=0.5)
    results["checks"]["watcher_start"] = bool(s.get("ok"))
    time.sleep(0.2)
    results["checks"]["watcher_status_running"] = get_status()["watcher_running"]
    sp = stop_watcher()
    results["checks"]["watcher_stop"] = bool(sp.get("ok"))

    # 7. Status has all keys
    st = get_status()
    results["checks"]["status_full"] = all(k in st for k in
        ("watcher_running", "csv_path", "csv_exists", "csv_mtime",
         "last_sync_in_ts", "last_sync_out_ts"))

    # 8. Lock primitive
    a = _acquire_lock(0.1)
    results["checks"]["lock_acquire"] = a
    if a:
        b = _acquire_lock(0.1)  # 2ª vez debe fallar
        results["checks"]["lock_second_fails"] = not b
        _release_lock()
    results["checks"]["lock_released"] = not os.path.isfile(_lock_path())

    results["total_checks"] = len(results["checks"])
    results["passed_checks"] = sum(1 for k, v in results["checks"].items() if v)
    results["ok"] = results["passed_checks"] == results["total_checks"]
    return results


def get_info() -> Dict[str, Any]:
    st = get_status()
    return {
        "module": "accounts_csv_sync",
        "version": 1,
        "csv_columns": CSV_COLUMNS,
        "csv_columns_count": len(CSV_COLUMNS),
        "status": st,
    }
