"""account_health — sistema de salud de cuentas TM con trust score 0-100.

Reemplaza el modelo binario válido/inválido del account_validator por un score
ponderado con desglose. Implementa también:
  - Cuarentena 24h (estado intermedio antes de invalidar)
  - Fast session check vía httpx (sin browser, ~1s/cuenta)
  - Daily health digest (Telegram/Discord)
  - Account groups/tags para filtrado

Score factors (peso en %):
  - storage_state_fresh (TTL 24h)         25%
  - has_recent_login_24h                  20%
  - account_age_days (>30 mejor)          10%
  - has_warming_event                     10%
  - phone_verified                         5%
  - card_associated_count                  5%
  - login_failures_last_7d (negativo)    -10%
  - is_quarantined (negativo)            -15%
  - bumped_to_pending (negativo)         -50%
  - has_storage_state                     10%
  - country_match                          5%
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
_HEALTH_PATH = os.path.join(PROYECTO, "data", "account_health.json")
_QUARANTINE_PATH = os.path.join(PROYECTO, "data", "account_quarantine.json")
os.makedirs(os.path.dirname(_HEALTH_PATH), exist_ok=True)

QUARANTINE_TTL_HOURS = 24
STORAGE_TTL_HOURS = 24
MIN_LOGIN_AGE_HOURS_FOR_TRUST = 1   # cuentas con login <1h no suman extra (warming sospechoso)

_lock = threading.RLock()


def _load_health_db() -> Dict[str, Any]:
    return json_atomic.load(_HEALTH_PATH, default={"accounts": {}, "history": []})


def _save_health_db(d: Dict[str, Any]) -> None:
    json_atomic.save(_HEALTH_PATH, d)


def _hours_ago(iso_str: Optional[str]) -> Optional[float]:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str)
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return (datetime.now() - dt).total_seconds() / 3600.0
    except Exception:
        return None


def compute_trust_score(account: Dict[str, Any]) -> Dict[str, Any]:
    """Calcula trust score 0-100 con desglose por factor.

    Devuelve: {score: int 0-100, factors: {nombre: pts}, reasons: [str]}.
    """
    factors: Dict[str, int] = {}
    reasons: List[str] = []

    # Factor 1: storage_state_fresh (25 pts si <24h)
    ss_path = account.get("storage_state_file") or ""
    if ss_path and os.path.exists(ss_path):
        ss_age_h = (time.time() - os.path.getmtime(ss_path)) / 3600
        if ss_age_h < STORAGE_TTL_HOURS:
            factors["storage_fresh_24h"] = 25
            reasons.append(f"storage_state OK ({ss_age_h:.1f}h)")
        else:
            factors["storage_stale"] = 5
            reasons.append(f"storage_state stale ({ss_age_h:.0f}h >24h)")
    else:
        factors["no_storage_state"] = 0
        reasons.append("sin storage_state")

    # Factor 2: has_recent_login_24h (20 pts si login <24h)
    last_login_h = _hours_ago(account.get("last_login"))
    if last_login_h is not None and last_login_h < 24 and last_login_h > MIN_LOGIN_AGE_HOURS_FOR_TRUST:
        factors["recent_login_24h"] = 20
        reasons.append(f"login reciente ({last_login_h:.1f}h)")
    elif last_login_h is None:
        factors["no_login_record"] = 0
    else:
        factors["login_outdated"] = 3 if last_login_h < 168 else 0
        reasons.append(f"login {last_login_h:.0f}h ago")

    # Factor 3: account_age_days (>30 mejor, max 10 pts)
    created_h = _hours_ago(account.get("created_at"))
    if created_h is not None:
        age_days = created_h / 24
        if age_days > 30:
            factors["age_30plus"] = 10
            reasons.append(f"age {age_days:.0f}d (>30 ideal)")
        elif age_days > 7:
            factors["age_7plus"] = 6
        elif age_days > 1:
            factors["age_recent"] = 3
        else:
            factors["age_brandnew"] = 1
            reasons.append("cuenta brand-new (<1d)")

    # Factor 4: warming
    if account.get("warmed_at"):
        warming_h = _hours_ago(account.get("warmed_at"))
        if warming_h is not None and warming_h < 168:  # <7d
            factors["warmed"] = 10
            reasons.append(f"warming {warming_h:.0f}h ago")

    # Factor 5: phone verified (campo real `phone` o `phone_verified_at`)
    if account.get("phone") or account.get("phone_verified_at"):
        factors["phone_verified"] = 5

    # Factor 6: card associated (campo real `cards` lista o numero_tarjeta)
    cards = account.get("cards") or []
    if isinstance(cards, list) and cards:
        factors["has_card"] = 5

    # Factor 6b: registration completed (cuenta cerrada con éxito en TM)
    if account.get("registration_completed_at"):
        factors["registration_completed"] = 8
        reasons.append("registration_completed")

    # Factor 6c: validated_at recent (validator ha pasado por la cuenta)
    val_at = account.get("validated_at")
    if val_at:
        val_h = _hours_ago(val_at)
        if val_h is not None and val_h < 168:  # <7d
            factors["validator_passed_7d"] = 5
            reasons.append(f"validator OK {val_h:.0f}h ago")

    # Factor 7: login_failures recent (negativo)
    fails = account.get("login_failures_last_7d", 0) or 0
    if fails > 0:
        factors["recent_failures"] = -min(fails * 3, 10)
        reasons.append(f"{fails} login fails 7d")

    # Factor 8: cuarentena (negativo)
    if account.get("quarantined"):
        factors["quarantined"] = -15
        reasons.append("EN CUARENTENA 24h")

    # Factor 9: bumped to pending (BAD_CREDENTIALS) — penaliza fuerte
    if account.get("bumped_to_pending"):
        factors["bumped_pending"] = -50
        reasons.append("BUMPED to pending (BAD_CREDS)")

    # Factor 10: tiene storage state aunque sea viejo
    if ss_path and os.path.exists(ss_path):
        factors.setdefault("has_storage_file", 10)

    # Factor 11: country match
    if (account.get("country") or "").lower() in ("es", "spain", "españa"):
        factors["country_es"] = 5

    # Sumar y clamp 0-100
    raw = sum(factors.values())
    score = max(0, min(100, raw))

    return {
        "score": score,
        "factors": factors,
        "reasons": reasons,
        "computed_at": datetime.now().isoformat(),
    }


def get_trust_score(account_id: str) -> Dict[str, Any]:
    """Lee trust score cacheado o lo recomputa."""
    db = _load_health_db()
    cached = db.get("accounts", {}).get(account_id)
    if cached:
        age_min = _hours_ago(cached.get("computed_at"))
        if age_min is not None and age_min < 1:  # 1h cache
            return cached
    # Recomputar
    try:
        import account_manager
        accs = account_manager.account_manager.list_accounts()
        acc = next((a for a in accs if a.get("id") == account_id), None)
        if not acc:
            return {"error": "account_not_found", "account_id": account_id}
        score = compute_trust_score(acc)
        with _lock:
            db = _load_health_db()
            db["accounts"][account_id] = score
            _save_health_db(db)
        return score
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def compute_all_scores() -> Dict[str, Any]:
    """Recomputa scores para todas las cuentas activas. Devuelve resumen."""
    try:
        import account_manager
        accs = account_manager.account_manager.list_accounts()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    db = _load_health_db()
    distribution = {"excellent_85+": 0, "good_70-84": 0, "fair_50-69": 0,
                    "poor_30-49": 0, "critical_0-29": 0}
    total = 0
    for a in accs:
        if a.get("status") not in ("active", "registered"):
            continue
        s = compute_trust_score(a)
        db["accounts"][a["id"]] = s
        score = s["score"]
        total += 1
        if score >= 85:
            distribution["excellent_85+"] += 1
        elif score >= 70:
            distribution["good_70-84"] += 1
        elif score >= 50:
            distribution["fair_50-69"] += 1
        elif score >= 30:
            distribution["poor_30-49"] += 1
        else:
            distribution["critical_0-29"] += 1

    db["last_full_recompute_at"] = datetime.now().isoformat()
    _save_health_db(db)
    return {"ok": True, "total_accounts": total, "distribution": distribution,
            "computed_at": db["last_full_recompute_at"]}


# ───────────────────────── CUARENTENA 24h ─────────────────────────

def quarantine(account_id: str, reason: str = "login_failure") -> Dict[str, Any]:
    """Marca cuenta en cuarentena 24h (entre fallo login y bumpear a pending)."""
    try:
        import account_manager as am
        am.account_manager.update_account(account_id, {
            "quarantined": True,
            "quarantine_reason": reason[:200],
            "quarantined_at": datetime.now().isoformat(),
            "quarantine_until": (datetime.now() + timedelta(hours=QUARANTINE_TTL_HOURS)).isoformat(),
        })
        return {"ok": True, "account_id": account_id, "until_hours": QUARANTINE_TTL_HOURS}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def release_quarantine(account_id: str) -> Dict[str, Any]:
    """Sale de cuarentena (auto-recovery o manual)."""
    try:
        import account_manager as am
        am.account_manager.update_account(account_id, {
            "quarantined": False,
            "quarantine_reason": "",
            "quarantine_released_at": datetime.now().isoformat(),
        })
        return {"ok": True, "account_id": account_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def auto_release_expired_quarantines() -> int:
    """Cron: libera cuentas cuya cuarentena ya expiró."""
    try:
        import account_manager as am
        accs = am.account_manager.list_accounts()
        n = 0
        for a in accs:
            if not a.get("quarantined"):
                continue
            until = a.get("quarantine_until")
            if until and _hours_ago(until) is not None and _hours_ago(until) > 0:
                release_quarantine(a["id"])
                n += 1
        return n
    except Exception:
        return 0


# ───────────────────────── FAST SESSION CHECK ─────────────────────

def fast_session_check(account: Dict[str, Any], country: str = "es") -> Dict[str, Any]:
    """Validación rápida via httpx GET sin browser. ~1-2s por cuenta.

    Lee storage_state, monta cookies, GET /my.ticketmaster.es/settings.
    Si HTTP 200 → cookies válidas. Si 302 a login → expiradas.
    """
    out: Dict[str, Any] = {"account_id": account.get("id"), "method": "fast_httpx"}
    try:
        import httpx
        ss_path = account.get("storage_state_file")
        if not ss_path or not os.path.exists(ss_path):
            return {**out, "ok": False, "error": "no_storage_state"}
        with open(ss_path, encoding="utf-8") as f:
            state = json.load(f)
        cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
        url = f"https://my.ticketmaster.{country}/settings"
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        r = httpx.get(url, cookies=cookies, headers={"User-Agent": ua},
                      timeout=10, follow_redirects=False)
        out["status_code"] = r.status_code
        out["session_valid"] = r.status_code == 200
        out["redirect_to_login"] = (r.status_code in (301, 302) and
                                      "login" in r.headers.get("Location", "").lower())
        out["ok"] = out["session_valid"]
        if out["session_valid"]:
            out["last_login"] = datetime.now().isoformat()
        return out
    except Exception as e:
        return {**out, "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def fast_check_batch(account_ids: List[str], max_workers: int = 5) -> Dict[str, Any]:
    """Validación rápida de N cuentas en paralelo via httpx (sin browser)."""
    try:
        import account_manager as am
        accs = {a["id"]: a for a in am.account_manager.list_accounts()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    results: List[Dict[str, Any]] = []
    valid = 0
    invalid = 0
    started = time.time()

    def _check(aid):
        a = accs.get(aid)
        if not a:
            return {"account_id": aid, "ok": False, "error": "not_found"}
        return fast_session_check(a)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(_check, account_ids):
            results.append(r)
            if r.get("ok"):
                valid += 1
            else:
                invalid += 1

    return {
        "ok": True,
        "checked": len(account_ids),
        "valid": valid,
        "invalid": invalid,
        "elapsed_s": round(time.time() - started, 2),
        "results": results,
    }


# ───────────────────────── DAILY HEALTH DIGEST ─────────────────────

def daily_health_digest(send_notification: bool = True) -> Dict[str, Any]:
    """Genera resumen diario de salud + opcionalmente envía a Discord/Telegram."""
    summary = compute_all_scores()
    if not summary.get("ok"):
        return summary

    total = summary["total_accounts"]
    dist = summary["distribution"]
    excellent = dist["excellent_85+"]
    good = dist["good_70-84"]
    fair = dist["fair_50-69"]
    poor = dist["poor_30-49"]
    critical = dist["critical_0-29"]

    msg = (
        f"📊 *Health digest TM PERROS — {datetime.now():%Y-%m-%d %H:%M}*\n\n"
        f"Total: {total}\n"
        f"  🟢 Excelente (85+): {excellent}  ({excellent*100//max(1,total)}%)\n"
        f"  🟡 Bueno (70-84):   {good}\n"
        f"  🟠 Aceptable (50-69): {fair}\n"
        f"  🔴 Pobre (30-49):    {poor}\n"
        f"  ⚫ Crítico (0-29):   {critical}\n"
    )

    sent = []
    if send_notification:
        try:
            import notifications
            r = notifications.notify(msg, channels=["discord", "telegram"])
            sent = list(r.keys()) if isinstance(r, dict) else ["sent"]
        except Exception:
            pass

    return {**summary, "message": msg, "notification_sent": sent}


# ───────────────────────── GROUPS / TAGS ──────────────────────────

def set_group(account_id: str, group: str) -> Dict[str, Any]:
    try:
        import account_manager as am
        am.account_manager.update_account(account_id, {"group": group})
        return {"ok": True, "account_id": account_id, "group": group}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def add_tag(account_id: str, tag: str) -> Dict[str, Any]:
    try:
        import account_manager as am
        accs = am.account_manager.list_accounts()
        acc = next((a for a in accs if a.get("id") == account_id), None)
        if not acc:
            return {"ok": False, "error": "not_found"}
        tags = list(acc.get("tags") or [])
        if tag not in tags:
            tags.append(tag)
        am.account_manager.update_account(account_id, {"tags": tags})
        return {"ok": True, "tags": tags}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_by_group(group: str) -> List[Dict[str, Any]]:
    try:
        import account_manager as am
        return [a for a in am.account_manager.list_accounts()
                if (a.get("group") or "") == group]
    except Exception:
        return []


def list_by_tag(tag: str) -> List[Dict[str, Any]]:
    try:
        import account_manager as am
        return [a for a in am.account_manager.list_accounts()
                if tag in (a.get("tags") or [])]
    except Exception:
        return []


def get_groups_summary() -> Dict[str, int]:
    try:
        import account_manager as am
        accs = am.account_manager.list_accounts()
        groups: Dict[str, int] = {}
        for a in accs:
            g = a.get("group") or "ungrouped"
            groups[g] = groups.get(g, 0) + 1
        return groups
    except Exception:
        return {}


def self_test() -> Dict[str, Any]:
    out = {"checks": {}}
    try:
        # Compute score para cuenta sintética
        synth = {"id": "test", "tm_email": "test@test.com",
                 "status": "active", "country": "es",
                 "created_at": datetime.now().isoformat()}
        s = compute_trust_score(synth)
        out["checks"]["compute_score"] = isinstance(s.get("score"), int) and 0 <= s["score"] <= 100
        out["checks"]["score_factors"] = isinstance(s.get("factors"), dict)
        # Quarantine roundtrip dry
        out["checks"]["quarantine_module_load"] = callable(quarantine)
        out["checks"]["fast_check_callable"] = callable(fast_session_check)
        out["checks"]["digest_callable"] = callable(daily_health_digest)
    except Exception as e:
        out["error"] = str(e)
    out["ok"] = all(v is True for v in out["checks"].values()) and not out.get("error")
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
