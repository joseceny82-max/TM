"""tm_registrations.py — Registros masivos TM con cola + workers + CSV.

⚠️ AVISO LEGAL (lee antes de usar):
- La creación masiva de cuentas en Ticketmaster viola sus TOS.
- Usar identidades fabricadas puede constituir fraude/suplantación (delito).
- Usuario es responsable de que los datos del CSV sean reales y legítimos
  (cuentas propias, de su equipo con consentimiento, o negocio propio).
- Este módulo funciona por defecto en modo SIMULACIÓN para testing seguro.
- El modo LIVE debe activarse EXPLÍCITAMENTE por el usuario bajo su responsabilidad.

Arquitectura:
- CSV import → validate → queue (JSON persistido)
- Workers concurrentes con rate limit por país
- Delay humano entre campos y registros
- Retry exponencial en errores transitorios
- Integración opcional con ja4_spoof (TLS) y proxy pool
- WebSocket broadcasts para UI en tiempo real
"""
import csv
import io
import json
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Callable

import config
import json_atomic


# ═══════════════════════════════════════════
#  CONSTANTES
# ═══════════════════════════════════════════
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# URLs reales de las páginas de registro TM por dominio
TM_REGISTER_URLS = {
    "us": "https://identity.ticketmaster.com/sign-up",
    "ca": "https://identity.ticketmaster.ca/sign-up",
    "mx": "https://identity.ticketmaster.com.mx/sign-up",
    "uk": "https://identity.ticketmaster.co.uk/sign-up",
    "ie": "https://identity.ticketmaster.ie/sign-up",
    "es": "https://identity.ticketmaster.es/sign-up",
    "de": "https://identity.ticketmaster.de/sign-up",
    "fr": "https://identity.ticketmaster.fr/sign-up",
    "it": "https://identity.ticketmaster.it/sign-up",
    "nl": "https://identity.ticketmaster.nl/sign-up",
    "at": "https://identity.ticketmaster.at/sign-up",
    "be": "https://identity.ticketmaster.be/sign-up",
    "se": "https://identity.ticketmaster.se/sign-up",
    "no": "https://identity.ticketmaster.no/sign-up",
    "dk": "https://identity.ticketmaster.dk/sign-up",
    "fi": "https://identity.ticketmaster.fi/sign-up",
    "pl": "https://identity.ticketmaster.pl/sign-up",
    "cz": "https://identity.ticketmaster.cz/sign-up",
    "ch": "https://identity.ticketmaster.ch/sign-up",
    "au": "https://identity.ticketmaster.com.au/sign-up",
    "nz": "https://identity.ticketmaster.co.nz/sign-up",
    "ae": "https://identity.ticketmaster.ae/sign-up",
}

# Columnas aceptadas del CSV (flexibles; las opcionales pueden faltar)
REQUIRED_COLUMNS = ["email", "password", "first_name", "last_name"]
OPTIONAL_COLUMNS = ["country", "phone", "dob", "zip", "address", "city", "proxy"]

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

_lock = threading.Lock()
_worker_state_lock = threading.Lock()  # protege _worker_state mutations entre workers

# Worker state (en memoria)
_worker_state = {
    "running": False,
    "threads": [],
    "stop_event": None,
    "started_at": None,
    "mode": "simulate",      # "simulate" o "live"
    "concurrency": 3,
    "delay_min_s": 3.0,      # delay aleatorio mínimo entre registros (mismo país)
    "delay_max_s": 8.0,
    "max_retries": 2,
    "use_ja4_spoof": True,
    "use_proxies": False,
    "last_per_country": {},  # {country: last_attempt_iso}
    "processed": 0,
    "errors_last": [],
}


# ═══════════════════════════════════════════
#  PERSISTENCIA
# ═══════════════════════════════════════════
def _path_queue():
    return config.ruta("tm_registrations.json")


def _path_config():
    return config.ruta("tm_reg_config.json")


def _load():
    return json_atomic.load(_path_queue(), default={"items": []})


def _save(d):
    json_atomic.save(_path_queue(), d)


def get_config() -> dict:
    return json_atomic.load(_path_config(), default={
        "mode": "simulate",
        "concurrency": 3,
        "delay_min_s": 3.0,
        "delay_max_s": 8.0,
        "max_retries": 2,
        "use_ja4_spoof": True,
        "use_proxies": False,
        "per_country_min_interval_s": 30.0,  # entre registros mismo país
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    allowed = {"mode", "concurrency", "delay_min_s", "delay_max_s", "max_retries",
               "use_ja4_spoof", "use_proxies", "per_country_min_interval_s"}
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    if cur.get("mode") not in ("simulate", "live"):
        cur["mode"] = "simulate"
    cur["concurrency"] = max(1, min(int(cur.get("concurrency", 3)), 20))
    cur["delay_min_s"] = max(0.5, float(cur.get("delay_min_s", 3.0)))
    cur["delay_max_s"] = max(cur["delay_min_s"], float(cur.get("delay_max_s", 8.0)))
    cur["max_retries"] = max(0, min(int(cur.get("max_retries", 2)), 10))
    cur["per_country_min_interval_s"] = max(5.0, float(cur.get("per_country_min_interval_s", 30.0)))
    json_atomic.save(_path_config(), cur)
    return cur


# ═══════════════════════════════════════════
#  CSV IMPORT + VALIDATION
# ═══════════════════════════════════════════
def csv_template() -> str:
    """Devuelve una plantilla CSV con todas las columnas soportadas."""
    header = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
    example1 = {
        "email": "usuario1@example.com", "password": "Pass1234!",
        "first_name": "Juan", "last_name": "García",
        "country": "es", "phone": "+34600000001", "dob": "1990-01-15",
        "zip": "28001", "address": "Calle Falsa 123", "city": "Madrid", "proxy": "",
    }
    example2 = {
        "email": "user2@example.com", "password": "Pass5678!",
        "first_name": "John", "last_name": "Smith",
        "country": "us", "phone": "+15551234567", "dob": "1985-05-20",
        "zip": "10001", "address": "5th Ave 100", "city": "New York", "proxy": "",
    }
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header)
    writer.writeheader()
    writer.writerow(example1)
    writer.writerow(example2)
    return out.getvalue()


def _validate_row(row: dict) -> Optional[str]:
    """Valida una fila. Devuelve None si OK, mensaje de error si inválida."""
    for col in REQUIRED_COLUMNS:
        if not (row.get(col) or "").strip():
            return f"falta campo requerido: {col}"
    email = row["email"].strip()
    if not EMAIL_RE.match(email):
        return f"email inválido: {email}"
    if len(row["password"]) < 6:
        return "password muy corto (min 6)"
    country = (row.get("country") or "us").strip().lower()
    if country and country not in TM_REGISTER_URLS:
        return f"país no soportado: {country} (usa: {', '.join(sorted(TM_REGISTER_URLS.keys()))})"
    return None


def import_csv(csv_text: str) -> dict:
    """Importa CSV. Deduplica por email. Devuelve stats."""
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
    except Exception as e:
        return {"ok": False, "error": f"CSV inválido: {e}"}

    with _lock:
        d = _load()
        existing_emails = {it["email"].lower() for it in d["items"] if it.get("email")}
        added, skipped_dup, skipped_bad = 0, 0, []
        for i, raw in enumerate(reader, start=2):  # línea 2 = primera data
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            err = _validate_row(row)
            if err:
                skipped_bad.append({"line": i, "error": err, "email": row.get("email", "")})
                continue
            email_low = row["email"].lower()
            if email_low in existing_emails:
                skipped_dup += 1
                continue
            existing_emails.add(email_low)
            item = {
                "id": uuid.uuid4().hex[:10],
                "email": row["email"],
                "password": row["password"],
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "country": (row.get("country") or "us").lower(),
                "phone": row.get("phone", ""),
                "dob": row.get("dob", ""),
                "zip": row.get("zip", ""),
                "address": row.get("address", ""),
                "city": row.get("city", ""),
                "proxy": row.get("proxy", ""),
                "status": STATUS_PENDING,
                "created_at": datetime.now().isoformat(),
                "attempts": 0,
                "last_error": "",
                "last_attempt_at": None,
                "completed_at": None,
                "tm_url": TM_REGISTER_URLS.get((row.get("country") or "us").lower(), ""),
                "mode_used": None,
                "history": [],
            }
            d["items"].append(item)
            added += 1
        _save(d)
    return {
        "ok": True,
        "added": added,
        "skipped_duplicates": skipped_dup,
        "skipped_invalid": len(skipped_bad),
        "errors": skipped_bad[:50],
    }


def add_manual(data: dict) -> dict:
    """Añade un registro manual (vía form). Devuelve {ok, id, ...}."""
    row = {k: (data.get(k) or "").strip() for k in
           REQUIRED_COLUMNS + OPTIONAL_COLUMNS}
    err = _validate_row(row)
    if err:
        return {"ok": False, "error": err}
    # Capturar id del item recién añadido
    emails_before = {it["email"].lower() for it in list_items(limit=10000)
                     if it.get("email")}
    result = import_csv(_dict_to_csv_row(row))
    # Si fue duplicado, el UI debe verlo claramente
    if result.get("ok") and result.get("added", 0) == 0:
        if result.get("skipped_duplicates", 0) > 0:
            return {"ok": False, "error": f"email duplicado: {row.get('email', '')}",
                    "duplicate": True}
        if result.get("skipped_invalid", 0) > 0:
            errs = result.get("errors") or []
            msg = errs[0].get("error") if errs else "fila invalida"
            return {"ok": False, "error": msg}
    # Buscar el item nuevo para devolver su id
    if result.get("ok") and result.get("added", 0) > 0:
        new_email = row.get("email", "").lower()
        for it in list_items(limit=10000):
            if it.get("email", "").lower() == new_email and \
               it.get("email", "").lower() not in emails_before:
                result["id"] = it.get("id")
                result["item"] = it
                break
    return result


def _dict_to_csv_row(row: dict) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=REQUIRED_COLUMNS + OPTIONAL_COLUMNS)
    w.writeheader()
    w.writerow({k: row.get(k, "") for k in REQUIRED_COLUMNS + OPTIONAL_COLUMNS})
    return out.getvalue()


# ═══════════════════════════════════════════
#  CRUD
# ═══════════════════════════════════════════
def list_all(limit: int = 500) -> List[dict]:
    """Alias de list_items() sin filtros — para compat con API de otros módulos."""
    return list_items(limit=limit)


def list_by_status(status: str, limit: int = 500) -> List[dict]:
    """Alias: lista registros filtrados por status (pending/done/failed/retry)."""
    return list_items(status=status, limit=limit)


def list_by_country(country: str, limit: int = 500) -> List[dict]:
    """Alias: lista registros por país."""
    return list_items(country=country, limit=limit)


def list_items(search: str = "", status: Optional[str] = None,
               country: Optional[str] = None, limit: int = 500) -> List[dict]:
    d = _load()
    items = d.get("items", [])
    if status:
        items = [it for it in items if it.get("status") == status]
    if country:
        items = [it for it in items if it.get("country") == country]
    q = (search or "").lower().strip()
    if q:
        items = [it for it in items
                 if q in (it.get("email", "") + " " + it.get("first_name", "") + " " +
                          it.get("last_name", "")).lower()]
    # ordenar: pending primero, luego por created_at desc
    order = {STATUS_PENDING: 0, STATUS_PROCESSING: 1, STATUS_FAILED: 2,
             STATUS_DONE: 3, STATUS_SKIPPED: 4}
    items.sort(key=lambda it: (order.get(it.get("status"), 5),
                               -(datetime.fromisoformat(it.get("created_at", "2000-01-01")).timestamp()
                                 if it.get("created_at") else 0)))
    # enmascarar password en respuesta
    out = []
    for it in items[:limit]:
        c = dict(it)
        if c.get("password"):
            c["password_preview"] = "*" * min(len(c["password"]), 10)
            del c["password"]
        out.append(c)
    return out


def get_stats() -> dict:
    d = _load()
    items = d.get("items", [])
    by_status = {}
    by_country = {}
    for it in items:
        s = it.get("status", STATUS_PENDING)
        c = it.get("country", "?")
        by_status[s] = by_status.get(s, 0) + 1
        by_country[c] = by_country.get(c, 0) + 1
    return {
        "total": len(items),
        "pending": by_status.get(STATUS_PENDING, 0),
        "processing": by_status.get(STATUS_PROCESSING, 0),
        "done": by_status.get(STATUS_DONE, 0),
        "failed": by_status.get(STATUS_FAILED, 0),
        "skipped": by_status.get(STATUS_SKIPPED, 0),
        "flaky": by_status.get("flaky", 0),
        "abandoned": by_status.get("abandoned", 0),
        "by_country": by_country,
        "worker": {
            "running": _worker_state["running"],
            "mode": _worker_state["mode"],
            "concurrency": _worker_state["concurrency"],
            "processed": _worker_state["processed"],
            "started_at": _worker_state["started_at"],
        },
    }


def delete_item(reg_id: str) -> bool:
    with _lock:
        d = _load()
        before = len(d["items"])
        d["items"] = [it for it in d["items"] if it["id"] != reg_id]
        _save(d)
    return len(d["items"]) < before


# Campos editables vía PUT — protege id/ts/status/history del frontend.
_EDITABLE_FIELDS = (
    "email", "password", "first_name", "last_name",
    "country", "phone", "dob", "zip", "address", "city", "proxy",
    "notes",
)


def update_item(reg_id: str, patch: dict) -> Optional[dict]:
    """Actualiza un registro existente. Solo permite campos en _EDITABLE_FIELDS.
    Devuelve el item actualizado o None si no existe."""
    if not isinstance(patch, dict): return None
    with _lock:
        d = _load()
        for it in d["items"]:
            if it.get("id") == reg_id:
                for k in _EDITABLE_FIELDS:
                    if k in patch:
                        v = patch[k]
                        # Sanitize email
                        if k == "email" and v:
                            v = str(v).strip().lower()
                            if not EMAIL_RE.match(v):
                                continue  # ignora email inválido
                        if k == "country" and v:
                            v = str(v).strip().lower()
                        if v is None:
                            it.pop(k, None)
                        else:
                            it[k] = v
                it["last_edited_at"] = datetime.now().isoformat()
                _save(d)
                return it
    return None


def bulk_update(updates: List[dict]) -> dict:
    """Actualiza múltiples items a la vez. Cada update debe tener `id` y los campos editables.
    Devuelve {ok, updated, failed:[...], total}."""
    if not isinstance(updates, list):
        return {"ok": False, "error": "updates must be list"}
    updated, failed = [], []
    for u in updates:
        rid = (u or {}).get("id")
        if not rid:
            failed.append({"id": None, "reason": "missing_id"})
            continue
        res = update_item(rid, u)
        if res:
            updated.append(rid)
        else:
            failed.append({"id": rid, "reason": "not_found_or_invalid"})
    return {"ok": True, "updated": updated, "updated_count": len(updated),
            "failed": failed, "total": len(updates)}


def clear_by_status(status: str) -> int:
    with _lock:
        d = _load()
        before = len(d["items"])
        d["items"] = [it for it in d["items"] if it.get("status") != status]
        _save(d)
    return before - len(d["items"])


def retry_item(reg_id: str) -> Optional[dict]:
    with _lock:
        d = _load()
        for it in d["items"]:
            if it["id"] == reg_id:
                it["status"] = STATUS_PENDING
                it["last_error"] = ""
                _save(d)
                return _safe(it)
        return None


def retry_all_failed() -> int:
    with _lock:
        d = _load()
        n = 0
        for it in d["items"]:
            if it.get("status") == STATUS_FAILED:
                it["status"] = STATUS_PENDING
                it["last_error"] = ""
                n += 1
        _save(d)
    return n


def _safe(it: dict) -> dict:
    c = dict(it)
    if "password" in c:
        c["password_preview"] = "*" * min(len(c.get("password", "")), 10)
        c.pop("password", None)
    return c


# ═══════════════════════════════════════════
#  SUBMISSION: simulate vs live
# ═══════════════════════════════════════════
def _perform_submit_simulate(item: dict) -> dict:
    """Modo SIMULACIÓN: no hace peticiones reales.

    Respuesta aleatoria ponderada para probar toda la cadena sin tocar TM.
    Éxito 70%, fallo captcha 12%, fallo network 8%, fallo email duplicado 10%.
    """
    # simular trabajo
    time.sleep(random.uniform(0.4, 1.2))
    roll = random.random()
    if roll < 0.70:
        return {"ok": True, "message": "simulated OK (mode=simulate)", "status_code": 201}
    elif roll < 0.82:
        return {"ok": False, "message": "simulated CAPTCHA blocked", "status_code": 403,
                "error_type": "captcha"}
    elif roll < 0.90:
        return {"ok": False, "message": "simulated network timeout", "status_code": 0,
                "error_type": "network", "retriable": True}
    else:
        return {"ok": False, "message": "simulated email already exists", "status_code": 409,
                "error_type": "duplicate"}


def _perform_submit_live(item: dict, cfg: dict) -> dict:
    """Modo LIVE: petición HTTP real al sign-up de TM.

    Nota: TM usa SPA + CAPTCHA + protección de PerimeterX. Este método:
    - Hace GET a la página de registro (warming + cookies)
    - POST a la API interna de sign-up
    - Si hay CAPTCHA → falla y marca para revisión manual
    - Respeta rate limit por país

    El usuario es responsable de la legitimidad de los datos y del cumplimiento
    de los TOS de Ticketmaster.
    """
    url = TM_REGISTER_URLS.get(item.get("country", "us"))
    if not url:
        return {"ok": False, "message": f"país no soportado: {item.get('country')}",
                "error_type": "country"}
    try:
        session = _get_http_session(cfg, item)
    except Exception as e:
        return {"ok": False, "message": f"no session: {e}", "error_type": "session"}

    # 1) GET página de registro (warming cookies)
    # timeout explícito para evitar cuelgues del worker. tls_client usa
    # `timeout_seconds`, requests usa `timeout`. Probamos ambos.
    try:
        try:
            r = session.get(url, timeout_seconds=20)
        except TypeError:
            r = session.get(url, timeout=20)
        if not (200 <= getattr(r, "status_code", 0) < 400):
            return {"ok": False, "message": f"GET {url}: HTTP {r.status_code}",
                    "error_type": "http_get", "status_code": r.status_code,
                    "retriable": r.status_code in (429, 502, 503, 504)}
    except Exception as e:
        return {"ok": False, "message": f"GET error: {e}",
                "error_type": "network", "retriable": True}

    # 2) Human delay antes de enviar el form
    time.sleep(random.uniform(1.5, 3.5))

    # 3) POST al endpoint de creación — TM usa múltiples endpoints según dominio.
    # No construyo el payload exacto aquí porque cambia frecuentemente y requiere
    # CAPTCHA token. El integrador debe completar esto con su infraestructura.
    # Si detectamos CAPTCHA challenge en la página, marcamos como needs_captcha.
    if hasattr(r, "text") and ("captcha" in (r.text or "").lower() or
                                "px-captcha" in (r.text or "").lower() or
                                "hcaptcha" in (r.text or "").lower()):
        return {"ok": False, "message": "Página requiere CAPTCHA (resuélvelo manualmente o integra solver)",
                "error_type": "captcha_required", "status_code": getattr(r, "status_code", 0),
                "retriable": False}

    # Placeholder: sin solver captcha, no podemos completar registro real automáticamente
    return {"ok": False, "message": "LIVE mode: completar integración con CAPTCHA solver + payload exacto",
            "error_type": "needs_integration", "status_code": 200,
            "note": "Este paso requiere que el usuario conecte captcha_manager + formPayload personalizado"}


def _get_http_session(cfg: dict, item: dict):
    """Devuelve una session HTTP: tls_client si ja4 activado, requests si no."""
    if cfg.get("use_ja4_spoof"):
        try:
            import ja4_spoof
            s = ja4_spoof.get_session()
            # user-agent ya viene del perfil
            if cfg.get("use_proxies") and item.get("proxy"):
                s.proxies = {"http": item["proxy"], "https": item["proxy"]}
            return s
        except Exception:
            pass
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    if cfg.get("use_proxies") and item.get("proxy"):
        s.proxies = {"http": item["proxy"], "https": item["proxy"]}
    return s


def _process_one(item: dict, cfg: dict) -> dict:
    """Procesa un registro. Aplica retries + rate limit por país."""
    # Rate limit por país (atomic check+set para evitar que dos workers
    # del mismo país pasen al mismo tiempo)
    country = item.get("country", "us")
    min_interval = cfg.get("per_country_min_interval_s", 30.0)
    wait_s = 0.0
    with _worker_state_lock:
        last = _worker_state["last_per_country"].get(country)
        if last:
            try:
                since = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
                if since < min_interval:
                    wait_s = min_interval - since
            except Exception:
                pass
        # Reservamos slot ya con el timestamp futuro (post-wait) para
        # que otros workers vean la reserva y se pongan en cola.
        reserved_ts = datetime.now() + timedelta(seconds=max(0.0, wait_s))
        _worker_state["last_per_country"][country] = reserved_ts.isoformat()
    if wait_s > 0:
        time.sleep(wait_s)

    # Ejecutar submit según modo
    mode = cfg.get("mode", "simulate")
    if mode == "live":
        result = _perform_submit_live(item, cfg)
    else:
        result = _perform_submit_simulate(item)
    result["mode"] = mode
    return result


# ═══════════════════════════════════════════
#  WORKER POOL
# ═══════════════════════════════════════════
def _pick_next_pending() -> Optional[dict]:
    """Extrae el siguiente pending y lo marca como processing."""
    with _lock:
        d = _load()
        for it in d["items"]:
            if it.get("status") == STATUS_PENDING:
                it["status"] = STATUS_PROCESSING
                it["last_attempt_at"] = datetime.now().isoformat()
                _save(d)
                return dict(it)
        return None


def _update_item(reg_id: str, patch: dict):
    with _lock:
        d = _load()
        for it in d["items"]:
            if it["id"] == reg_id:
                for k, v in patch.items():
                    it[k] = v
                _save(d)
                return


def _append_history(reg_id: str, entry: dict):
    with _lock:
        d = _load()
        for it in d["items"]:
            if it["id"] == reg_id:
                it.setdefault("history", []).append(entry)
                it["history"] = it["history"][-20:]
                _save(d)
                return


def _worker_loop(stop_event: threading.Event, worker_id: int,
                 broadcast: Optional[Callable] = None):
    """Loop principal de un worker."""
    while not stop_event.is_set():
        cfg = get_config()
        item = _pick_next_pending()
        if not item:
            stop_event.wait(2.0)
            continue

        attempts = item.get("attempts", 0) + 1
        _update_item(item["id"], {"attempts": attempts})

        try:
            result = _process_one(item, cfg)
        except Exception as e:
            result = {"ok": False, "message": f"worker exception: {e}",
                      "error_type": "exception", "retriable": True}

        now = datetime.now().isoformat()
        _append_history(item["id"], {"ts": now, "worker": worker_id, "result": result})

        if result.get("ok"):
            _update_item(item["id"], {
                "status": STATUS_DONE,
                "completed_at": now,
                "last_error": "",
                "mode_used": result.get("mode"),
            })
            with _worker_state_lock:
                _worker_state["processed"] += 1
            if broadcast:
                broadcast("registration_done", {"id": item["id"], "email": item.get("email")})
            # AUTO-PROMOTE: trasladar la cuenta recién registrada al account_manager
            # (pestaña Cuentas y Tarjetas) para que el bot pueda usarla. Idempotente.
            try:
                import registration_promoter
                # re-leer el item actualizado (con status=done) para que el promoter lo vea
                fresh = next((x for x in _load().get("items", []) if x.get("id") == item["id"]), item)
                promo = registration_promoter.promote_to_account_manager(fresh)
                if broadcast:
                    broadcast("registration_promoted", {
                        "reg_id": item["id"], "email": item.get("email"),
                        "account_id": promo.get("account_id"),
                        "ok": promo.get("ok"), "skipped": promo.get("skipped_reason"),
                    })
            except Exception:
                pass
        else:
            # decidir si re-encolar o marcar failed
            retriable = result.get("retriable", False)
            if retriable and attempts < cfg.get("max_retries", 2) + 1:
                # backoff exponencial 2^n
                backoff = min(60, 2 ** attempts + random.uniform(0, 2))
                _update_item(item["id"], {
                    "status": STATUS_PENDING,
                    "last_error": result.get("message", ""),
                })
                time.sleep(backoff)
            else:
                _update_item(item["id"], {
                    "status": STATUS_FAILED,
                    "last_error": result.get("message", ""),
                    "mode_used": result.get("mode"),
                })
                with _worker_state_lock:
                    _worker_state["errors_last"].insert(0, {
                        "id": item["id"], "email": item.get("email"),
                        "error": result.get("message", ""), "ts": now,
                    })
                    _worker_state["errors_last"] = _worker_state["errors_last"][:20]
                if broadcast:
                    broadcast("registration_failed", {"id": item["id"],
                                                      "email": item.get("email"),
                                                      "error": result.get("message", "")})

        # delay humano antes del siguiente
        time.sleep(random.uniform(cfg.get("delay_min_s", 3.0),
                                    cfg.get("delay_max_s", 8.0)))


def start_worker(broadcast: Optional[Callable] = None) -> dict:
    if _worker_state["running"]:
        return {"ok": True, "already_running": True, "concurrency": _worker_state["concurrency"]}
    cfg = get_config()
    stop = threading.Event()
    concurrency = cfg.get("concurrency", 3)
    threads = []
    for i in range(concurrency):
        t = threading.Thread(target=_worker_loop, args=(stop, i + 1, broadcast),
                             daemon=True, name=f"tm-reg-worker-{i+1}")
        t.start()
        threads.append(t)
    _worker_state.update({
        "running": True,
        "threads": threads,
        "stop_event": stop,
        "started_at": datetime.now().isoformat(),
        "mode": cfg.get("mode", "simulate"),
        "concurrency": concurrency,
        "processed": 0,
        "errors_last": [],
        "last_per_country": {},
    })
    return {"ok": True, "started": True, "concurrency": concurrency,
            "mode": cfg.get("mode", "simulate")}


def stop_worker() -> dict:
    if not _worker_state["running"]:
        return {"ok": True, "not_running": True}
    ev = _worker_state.get("stop_event")
    if ev:
        ev.set()
    # marcar cualquier 'processing' como pending otra vez
    with _lock:
        d = _load()
        n_restored = 0
        for it in d["items"]:
            if it.get("status") == STATUS_PROCESSING:
                it["status"] = STATUS_PENDING
                n_restored += 1
        _save(d)
    _worker_state.update({
        "running": False,
        "threads": [],
        "stop_event": None,
    })
    return {"ok": True, "stopped": True, "restored_pending": n_restored}


def worker_status() -> dict:
    return {
        "running": _worker_state["running"],
        "mode": _worker_state["mode"],
        "concurrency": _worker_state["concurrency"],
        "processed": _worker_state["processed"],
        "started_at": _worker_state["started_at"],
        "errors_last": list(_worker_state["errors_last"])[:5],
        "last_per_country": dict(_worker_state["last_per_country"]),
    }


def get_supported_countries() -> List[dict]:
    return [{"id": k, "url": v} for k, v in sorted(TM_REGISTER_URLS.items())]
