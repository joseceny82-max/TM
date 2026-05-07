"""card_rotation.py — Rotación multi-tarjeta anti-fraude.

Mejora #multi_card_rotation de la nota:
  Rotar tarjetas entre cuentas para evitar patrones de fraude bancario.
  Pasos:
  1. Mapa cuenta → N tarjetas
  2. Seleccionar tarjeta diferente por intento
  3. Cooldown 24h por tarjeta
  4. Blacklist temporal tras "declined" / "fraud_suspected"

⚠️ AVISO LEGAL:
- Usar SOLO tarjetas propias o con autorización explícita.
- La rotación NO evita que el banco detecte fraude — solo reduce
  patrones de uso repetido. Si el banco bloquea la tarjeta, respeta
  el bloqueo (no intentes reintroducirla).
- El cooldown de 24h es una heurística genérica. Bancos tipo Openbank /
  BBVA detectan patrones con ventanas mucho más largas.

API pública:
  pick_card(account_id) → tarjeta disponible (menos usada)
  record_usage(card_id, outcome) → registra intento + outcome
  mark_declined(card_id, reason, cooldown_h) → blacklist
  get_stats() → contadores por tarjeta/cuenta
  list_available(account_id) / list_all_status()
"""
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import config
import json_atomic

# account_manager es una clase instanciada como singleton
try:
    from account_manager import account_manager as _am
except Exception:
    _am = None


_USAGE_KEY = "card_rotation_usage.json"
_CONFIG_KEY = "card_rotation_config.json"
_lock = threading.Lock()

# Outcomes canónicos
OUTCOME_SUCCESS = "success"
OUTCOME_DECLINED = "declined"
OUTCOME_3DS_FAILED = "3ds_failed"
OUTCOME_FRAUD_SUSPECTED = "fraud_suspected"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_USED = "used"  # genérico


# ═══════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════
def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "cooldown_h_success": 24,      # tras éxito: 24h de descanso
        "cooldown_h_declined": 72,     # tras declined: 72h (bancos detectan reintentos)
        "cooldown_h_fraud": 168,       # tras fraud_suspected: 7 días
        "cooldown_h_3ds_failed": 6,    # tras 3DS fallido: 6h (reintentable)
        "cooldown_h_timeout": 2,       # timeouts: 2h
        "max_uses_per_day": 3,         # máximo intentos/día por tarjeta
        "prefer_least_used": True,     # ordenar por usos asc (en lugar de random)
        "round_robin": False,          # modo round-robin puro (ignora cooldowns si todas están en cd)
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    allowed = set(cur.keys())
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ═══════════════════════════════════════════
#  USAGE LOG
# ═══════════════════════════════════════════
def _load_usage() -> dict:
    return json_atomic.load(config.ruta(_USAGE_KEY),
                            default={"usages": [], "blacklist": {}})


def _save_usage(data: dict):
    json_atomic.save(config.ruta(_USAGE_KEY), data)


def record_usage(card_id: str, outcome: str = OUTCOME_USED,
                 account_id: str = "", purchase_id: str = "",
                 details: str = "") -> dict:
    """Registra un intento de uso de la tarjeta."""
    ts = datetime.now().isoformat()
    entry = {
        "id": uuid.uuid4().hex[:10],
        "card_id": card_id,
        "account_id": account_id,
        "outcome": outcome,
        "purchase_id": purchase_id,
        "details": details[:200],
        "ts": ts,
    }
    with _lock:
        data = _load_usage()
        data["usages"].insert(0, entry)
        # cap 2000 entradas
        data["usages"] = data["usages"][:2000]

        # Si el outcome es malo, aplicar blacklist automático
        cfg = get_config()
        cooldown_h = 0
        if outcome == OUTCOME_DECLINED:
            cooldown_h = cfg["cooldown_h_declined"]
        elif outcome == OUTCOME_FRAUD_SUSPECTED:
            cooldown_h = cfg["cooldown_h_fraud"]
        elif outcome == OUTCOME_3DS_FAILED:
            cooldown_h = cfg["cooldown_h_3ds_failed"]
        elif outcome == OUTCOME_TIMEOUT:
            cooldown_h = cfg["cooldown_h_timeout"]
        elif outcome == OUTCOME_SUCCESS:
            cooldown_h = cfg["cooldown_h_success"]

        if cooldown_h > 0:
            until = (datetime.now() + timedelta(hours=cooldown_h)).isoformat()
            data["blacklist"][card_id] = {
                "until": until, "reason": outcome,
                "set_at": ts, "cooldown_h": cooldown_h,
            }
        _save_usage(data)
    return entry


def mark_declined(card_id: str, reason: str = "declined",
                  cooldown_h: Optional[int] = None) -> dict:
    """Blacklist manual de una tarjeta con cooldown custom."""
    if cooldown_h is None:
        cfg = get_config()
        cooldown_h = cfg["cooldown_h_declined"]
    until = (datetime.now() + timedelta(hours=cooldown_h)).isoformat()
    with _lock:
        data = _load_usage()
        data["blacklist"][card_id] = {
            "until": until, "reason": reason,
            "set_at": datetime.now().isoformat(),
            "cooldown_h": cooldown_h,
        }
        _save_usage(data)
    return {"ok": True, "card_id": card_id, "until": until,
            "cooldown_h": cooldown_h, "reason": reason}


def clear_blacklist(card_id: Optional[str] = None) -> dict:
    """Elimina blacklist de 1 tarjeta, o de todas si card_id=None."""
    with _lock:
        data = _load_usage()
        if card_id:
            had = card_id in data["blacklist"]
            data["blacklist"].pop(card_id, None)
            _save_usage(data)
            return {"ok": True, "removed": had, "card_id": card_id}
        count = len(data["blacklist"])
        data["blacklist"] = {}
        _save_usage(data)
        return {"ok": True, "removed_count": count}


# ═══════════════════════════════════════════
#  CARD AVAILABILITY
# ═══════════════════════════════════════════
def _is_blacklisted(card_id: str, data: dict) -> Optional[dict]:
    bl = data.get("blacklist", {}).get(card_id)
    if not bl:
        return None
    try:
        until = datetime.fromisoformat(bl["until"])
        if datetime.now() >= until:
            return None  # expirado
    except Exception:
        return None
    return bl


def _uses_last_24h(card_id: str, data: dict) -> int:
    cutoff = datetime.now() - timedelta(hours=24)
    n = 0
    for u in data.get("usages", []):
        if u.get("card_id") != card_id:
            continue
        try:
            ts = datetime.fromisoformat(u["ts"])
            if ts >= cutoff:
                n += 1
        except Exception:
            pass
    return n


def _last_usage_ts(card_id: str, data: dict) -> Optional[datetime]:
    for u in data.get("usages", []):
        if u.get("card_id") == card_id:
            try:
                return datetime.fromisoformat(u["ts"])
            except Exception:
                pass
    return None


def get_card_status(card_id: str) -> dict:
    """Estado completo de una tarjeta: bloqueada, usos 24h, último uso."""
    with _lock:
        data = _load_usage()
        bl = _is_blacklisted(card_id, data)
        uses_24h = _uses_last_24h(card_id, data)
        last = _last_usage_ts(card_id, data)
    cfg = get_config()
    available = (bl is None) and (uses_24h < cfg["max_uses_per_day"])
    return {
        "card_id": card_id,
        "available": available,
        "blacklisted": bl is not None,
        "blacklist_until": bl.get("until") if bl else None,
        "blacklist_reason": bl.get("reason") if bl else None,
        "uses_last_24h": uses_24h,
        "max_uses_per_day": cfg["max_uses_per_day"],
        "last_used_at": last.isoformat() if last else None,
    }


def list_available(account_id: str = "") -> List[dict]:
    """Tarjetas disponibles para usar (no blacklisted, <max usos)."""
    if _am is None:
        return []
    cards = _am.list_cards()
    if account_id:
        cards = [c for c in cards if c.get("account_id") == account_id]
    out = []
    for c in cards:
        st = get_card_status(c.get("id", ""))
        if st["available"]:
            m = dict(c)
            m["status"] = st
            out.append(m)
    return out


def list_all_status(account_id: str = "") -> List[dict]:
    """Todas las tarjetas + su estado (incluso las blacklisted)."""
    if _am is None:
        return []
    cards = _am.list_cards()
    if account_id:
        cards = [c for c in cards if c.get("account_id") == account_id]
    out = []
    for c in cards:
        st = get_card_status(c.get("id", ""))
        m = dict(c)
        m["status"] = st
        # enmascarar número si existe
        num = m.get("number", "") or m.get("pan", "")
        if num:
            m["masked"] = ("•" * max(0, len(num) - 4)) + num[-4:]
            m.pop("number", None)
            m.pop("pan", None)
        out.append(m)
    return out


# ═══════════════════════════════════════════
#  PICK CARD (selector principal)
# ═══════════════════════════════════════════
def pick_card(account_id: str = "", exclude_ids: Optional[List[str]] = None) -> Optional[dict]:
    """Selector principal. Devuelve mejor tarjeta disponible para el account.

    Algoritmo:
      1. Filtra tarjetas disponibles del account (no blacklisted, <max usos)
      2. Excluye exclude_ids
      3. Ordena por (uses_last_24h asc, last_used_at asc) → menos usada primero
      4. Si no hay disponibles y round_robin=True → devuelve la de cooldown más cercano
    """
    if _am is None:
        return None
    exclude = set(exclude_ids or [])
    cfg = get_config()
    available = [c for c in list_available(account_id) if c.get("id") not in exclude]

    if not available:
        if not cfg.get("round_robin"):
            return None
        # Round robin: ignora cooldowns, coge la de menor cooldown restante
        all_cards = _am.list_cards()
        if account_id:
            all_cards = [c for c in all_cards if c.get("account_id") == account_id]
        all_cards = [c for c in all_cards if c.get("id") not in exclude]
        if not all_cards:
            return None
        # orden por "menor cooldown_h restante"
        with _lock:
            data = _load_usage()
        def _until_s(c):
            bl = data.get("blacklist", {}).get(c.get("id", ""))
            if not bl:
                return 0
            try:
                return max(0, (datetime.fromisoformat(bl["until"]) - datetime.now()).total_seconds())
            except Exception:
                return 0
        all_cards.sort(key=_until_s)
        chosen = all_cards[0]
        chosen["_round_robin_forced"] = True
        return _mask(chosen)

    if cfg.get("prefer_least_used", True):
        # usos 24h asc, last_used_at asc (None primero = nunca usada = preferida)
        def _key(c):
            st = c.get("status", {})
            last = st.get("last_used_at") or ""
            return (st.get("uses_last_24h", 0), last)
        available.sort(key=_key)

    return _mask(available[0])


def _mask(card: dict) -> dict:
    out = dict(card)
    num = out.get("number", "") or out.get("pan", "")
    if num:
        out["masked"] = ("•" * max(0, len(num) - 4)) + num[-4:]
        # devolvemos el número completo también porque pick_card se usa en checkout
        # (si quieres máximo aislamiento usa list_all_status)
    return out


# ═══════════════════════════════════════════
#  STATS
# ═══════════════════════════════════════════
def get_stats() -> dict:
    with _lock:
        data = _load_usage()
    usages = data.get("usages", [])
    blacklist = data.get("blacklist", {})

    # cleanup blacklist expirada
    now = datetime.now()
    active_bl = 0
    for cid, bl in blacklist.items():
        try:
            if datetime.fromisoformat(bl["until"]) > now:
                active_bl += 1
        except Exception:
            pass

    # outcomes
    outcomes = {}
    for u in usages:
        o = u.get("outcome", "?")
        outcomes[o] = outcomes.get(o, 0) + 1

    # last 24h
    cutoff = now - timedelta(hours=24)
    recent = [u for u in usages if _safe_dt(u.get("ts", "")) >= cutoff]

    total_cards = len(_am.list_cards()) if _am else 0
    total_accounts = len(_am.list_accounts()) if _am else 0

    return {
        "total_usages_logged": len(usages),
        "usages_last_24h": len(recent),
        "active_blacklist": active_bl,
        "total_blacklist_records": len(blacklist),
        "outcomes": outcomes,
        "total_cards": total_cards,
        "total_accounts": total_accounts,
        "success_rate_pct": _pct(outcomes.get(OUTCOME_SUCCESS, 0), len(usages)),
        "decline_rate_pct": _pct(outcomes.get(OUTCOME_DECLINED, 0)
                                 + outcomes.get(OUTCOME_FRAUD_SUSPECTED, 0),
                                 len(usages)),
    }


def _safe_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.min


def _pct(num: int, denom: int) -> float:
    if denom <= 0:
        return 0.0
    return round(100.0 * num / denom, 1)


def get_log(limit: int = 50, card_id: str = "", account_id: str = "") -> List[dict]:
    with _lock:
        data = _load_usage()
    items = data.get("usages", [])
    if card_id:
        items = [u for u in items if u.get("card_id") == card_id]
    if account_id:
        items = [u for u in items if u.get("account_id") == account_id]
    return items[:limit]


# ═══════════════════════════════════════════
#  SELF TEST
# ═══════════════════════════════════════════
def self_test() -> dict:
    """Test rápido sin tocar tarjetas reales."""
    if _am is None:
        return {"ok": False, "error": "account_manager no disponible"}

    # 1. crear 2 tarjetas fake de prueba
    fake_account = "selftest-acct"
    fake_card_1 = {"id": "selftest-card-A", "account_id": fake_account,
                   "holder": "Test A", "brand": "visa"}
    fake_card_2 = {"id": "selftest-card-B", "account_id": fake_account,
                   "holder": "Test B", "brand": "mc"}

    # 2. simular uso de card A (success)
    record_usage(fake_card_1["id"], OUTCOME_SUCCESS,
                 account_id=fake_account, details="self-test")

    # 3. Comprobar que pick_card para ese account NO devuelve la A
    #    (porque está en cooldown 24h post-success). Como tampoco hay B
    #    registrada realmente, usamos list directly:
    with _lock:
        data = _load_usage()
    bl_a = data["blacklist"].get(fake_card_1["id"])

    # 4. limpieza
    data["usages"] = [u for u in data["usages"] if u.get("account_id") != fake_account]
    data["blacklist"].pop(fake_card_1["id"], None)
    data["blacklist"].pop(fake_card_2["id"], None)
    _save_usage(data)

    return {
        "ok": True,
        "card_A_went_to_cooldown": bl_a is not None,
        "cooldown_h": bl_a.get("cooldown_h") if bl_a else None,
        "blacklist_reason": bl_a.get("reason") if bl_a else None,
        "cleanup_done": True,
    }
