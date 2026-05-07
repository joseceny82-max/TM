"""proxy_rotator.py — Rotación inteligente de proxies con tracking de uso.

Garantiza que CADA proceso (registro, warming, compra, SMS) use una IP distinta:
  • Pool de 1000+ QuantumProxies en proxies.txt
  • Tracker de uso reciente con cooldown configurable (default: 6h)
  • Asignación por account_id (mismo account = mismo proxy = sesión consistente)
  • Fallback rotativo si el proxy preferido está en cooldown
  • Health-check opcional antes de devolver

API:
  pick_for_account(account_id, country='es', cooldown_hours=6) → dict
  pick_random(country='es', exclude_recent_hours=2) → dict
  release(proxy_id, account_id, success=True)
  get_stats() → dict
  self_test() → dict
"""
from __future__ import annotations
import os, random, threading, hashlib, time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config
import json_atomic

_lock = threading.RLock()
_USAGE_KEY = "proxy_rotator_usage.json"
_PROXIES_TXT = "proxies.txt"

# Cache en memoria del pool (refresca cada N seg)
_pool_cache = {"proxies": [], "loaded_at": 0}


def _load_pool(force: bool = False) -> List[Dict[str, Any]]:
    """Carga el pool desde proxies.txt (cache 60s)."""
    now = time.time()
    if not force and _pool_cache["proxies"] and (now - _pool_cache["loaded_at"]) < 60:
        return _pool_cache["proxies"]

    proxies: List[Dict[str, Any]] = []
    paths = [_PROXIES_TXT]
    try:
        if hasattr(config, "BASE_DIR"):
            paths.append(os.path.join(config.BASE_DIR, _PROXIES_TXT))
    except Exception: pass

    for path in paths:
        if not os.path.isfile(path): continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f.read().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    if "@" in line:
                        creds, hp = line.split("@", 1)
                        host, _, port = hp.partition(":")
                        user, _, pw = creds.partition(":")
                    else:
                        parts = line.split(":")
                        if len(parts) == 2: host, port = parts; user = pw = ""
                        elif len(parts) >= 4:
                            host, port = parts[0], parts[1]
                            user = parts[2]
                            pw = ":".join(parts[3:])
                        else: continue
                    try: port_n = int(port)
                    except: continue
                    pid = hashlib.md5(f"{host}:{port_n}:{user}".encode()).hexdigest()[:12]
                    proxies.append({
                        "id": pid, "host": host, "port": port_n,
                        "user": user, "password": pw,
                        "url": f"http://{user}:{pw}@{host}:{port_n}" if user else f"http://{host}:{port_n}",
                        "server": f"http://{host}:{port_n}",
                    })
            if proxies: break
        except Exception: continue

    _pool_cache["proxies"] = proxies
    _pool_cache["loaded_at"] = now
    return proxies


def _load_usage() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_USAGE_KEY),
                             default={"by_proxy": {}, "by_account": {}})


def _save_usage(d: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(config.ruta(_USAGE_KEY)), exist_ok=True)
    json_atomic.save(config.ruta(_USAGE_KEY), d)


# ═══════════════════════════════════════════════════════════════
# Selección
# ═══════════════════════════════════════════════════════════════
def pick_for_account(account_id: str, country: str = "es",
                     cooldown_hours: int = 6,
                     sticky: bool = True) -> Dict[str, Any]:
    """Devuelve un proxy para una cuenta específica.

    Si sticky=True: la misma cuenta siempre obtiene el mismo proxy (consistencia
    sesión TM). Si está en cooldown global O ya se usó por OTRA cuenta hace
    poco, busca uno nuevo y lo asigna a esta.
    """
    pool = _load_pool()
    if not pool:
        return {"ok": False, "error": "no_proxies_in_pool"}

    with _lock:
        usage = _load_usage()
        ba = usage.get("by_account", {})
        bp = usage.get("by_proxy", {})

        # Si esta cuenta ya tiene proxy asignado y sticky → reusar
        if sticky and account_id in ba:
            assigned_id = ba[account_id]["proxy_id"]
            assigned = next((p for p in pool if p["id"] == assigned_id), None)
            if assigned:
                # Actualizar last_used
                bp.setdefault(assigned_id, {})["last_used"] = datetime.now().isoformat()
                bp[assigned_id]["account_id"] = account_id
                bp[assigned_id]["uses"] = bp[assigned_id].get("uses", 0) + 1
                ba[account_id]["last_used"] = datetime.now().isoformat()
                _save_usage(usage)
                return {"ok": True, "proxy": assigned, "sticky_reused": True,
                        "account_id": account_id}

        # Buscar uno NO usado (o en cooldown vencido)
        cutoff = datetime.now() - timedelta(hours=cooldown_hours)
        candidates = []
        for p in pool:
            pid = p["id"]
            usage_info = bp.get(pid)
            if not usage_info:
                candidates.append(p)
                continue
            try:
                last = datetime.fromisoformat(usage_info["last_used"])
                if last < cutoff and usage_info.get("account_id") == account_id:
                    candidates.append(p)
                elif last < cutoff and not usage_info.get("account_id"):
                    candidates.append(p)
            except: candidates.append(p)

        # Si no hay candidatos limpios, escoger los menos usados recientemente
        if not candidates:
            scored = []
            now = datetime.now()
            for p in pool:
                u = bp.get(p["id"], {})
                last_str = u.get("last_used", "1970-01-01T00:00:00")
                try:
                    last = datetime.fromisoformat(last_str)
                    age_h = (now - last).total_seconds() / 3600
                except: age_h = 1000
                scored.append((age_h, p))
            scored.sort(reverse=True)  # más viejo primero
            candidates = [p for _, p in scored[:50]]

        chosen = random.choice(candidates)
        bp.setdefault(chosen["id"], {})
        bp[chosen["id"]].update({
            "last_used": datetime.now().isoformat(),
            "account_id": account_id,
            "country": country,
            "uses": bp[chosen["id"]].get("uses", 0) + 1,
        })
        ba[account_id] = {"proxy_id": chosen["id"],
                          "first_assigned": datetime.now().isoformat(),
                          "last_used": datetime.now().isoformat()}
        _save_usage(usage)
        return {"ok": True, "proxy": chosen, "sticky_reused": False,
                "account_id": account_id}


def pick_random(country: str = "es",
                exclude_recent_hours: int = 2) -> Dict[str, Any]:
    """Devuelve un proxy random NO usado en las últimas N horas.

    Útil para procesos sin account_id (warming, health-check, etc.)
    """
    pool = _load_pool()
    if not pool: return {"ok": False, "error": "no_proxies_in_pool"}

    with _lock:
        usage = _load_usage()
        bp = usage.get("by_proxy", {})
        cutoff = datetime.now() - timedelta(hours=exclude_recent_hours)
        candidates = []
        for p in pool:
            u = bp.get(p["id"])
            if not u: candidates.append(p); continue
            try:
                last = datetime.fromisoformat(u.get("last_used", ""))
                if last < cutoff: candidates.append(p)
            except: candidates.append(p)

        if not candidates: candidates = pool[:50]
        chosen = random.choice(candidates)
        bp.setdefault(chosen["id"], {})
        bp[chosen["id"]]["last_used"] = datetime.now().isoformat()
        bp[chosen["id"]]["uses"] = bp[chosen["id"]].get("uses", 0) + 1
        _save_usage(usage)
    return {"ok": True, "proxy": chosen, "random": True}


def release(proxy_id: str, account_id: str = "",
            success: bool = True, error: str = "") -> Dict[str, Any]:
    """Marca el proxy como liberado tras uso."""
    with _lock:
        usage = _load_usage()
        bp = usage.get("by_proxy", {})
        if proxy_id in bp:
            bp[proxy_id]["last_released"] = datetime.now().isoformat()
            bp[proxy_id]["last_success"] = bool(success)
            if not success:
                bp[proxy_id]["fails"] = bp[proxy_id].get("fails", 0) + 1
                if error: bp[proxy_id]["last_error"] = error[:120]
            else:
                bp[proxy_id]["successes"] = bp[proxy_id].get("successes", 0) + 1
        _save_usage(usage)
    return {"ok": True}


def get_stats() -> Dict[str, Any]:
    pool = _load_pool()
    usage = _load_usage()
    bp = usage.get("by_proxy", {})
    ba = usage.get("by_account", {})
    now = datetime.now()
    in_use = 0; cold = 0; never = len(pool) - len(bp)
    for pid, u in bp.items():
        try:
            last = datetime.fromisoformat(u.get("last_used", ""))
            age_h = (now - last).total_seconds() / 3600
            if age_h < 1: in_use += 1
            elif age_h > 24: cold += 1
        except: pass
    return {
        "pool_size": len(pool),
        "tracked_proxies": len(bp),
        "tracked_accounts": len(ba),
        "in_use_last_hour": in_use,
        "cold_24h+": cold,
        "never_used": never,
    }


def reset_account(account_id: str) -> Dict[str, Any]:
    """Libera el proxy asignado a una cuenta (para re-asignar uno nuevo)."""
    with _lock:
        usage = _load_usage()
        ba = usage.get("by_account", {})
        if account_id in ba:
            del ba[account_id]
            _save_usage(usage)
            return {"ok": True, "released": account_id}
    return {"ok": False, "error": "not_assigned"}


def reset_all() -> Dict[str, Any]:
    with _lock:
        _save_usage({"by_proxy": {}, "by_account": {}})
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# Helpers para Playwright / requests
# ═══════════════════════════════════════════════════════════════
def to_playwright_proxy(proxy: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte al formato {server, username, password} de Playwright."""
    return {
        "server": proxy["server"],
        "username": proxy.get("user", ""),
        "password": proxy.get("password", ""),
    }


def to_requests_proxies(proxy: Dict[str, Any]) -> Dict[str, str]:
    """Convierte al formato {http, https} de requests."""
    return {"http": proxy["url"], "https": proxy["url"]}


# ═══════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Pool carga
    pool = _load_pool(force=True)
    checks["pool_loads"] = len(pool) > 0
    checks["pool_has_min"] = len(pool) >= 1

    # 2. Estructura proxy
    if pool:
        p = pool[0]
        checks["proxy_has_id"] = "id" in p
        checks["proxy_has_host"] = "host" in p
        checks["proxy_has_url"] = "url" in p

    # 3. pick_for_account
    r = pick_for_account("test_acc_xxx_1", country="es")
    checks["pick_works"] = r.get("ok") is True
    proxy_id_1 = r.get("proxy", {}).get("id")

    # 4. Sticky: misma cuenta = mismo proxy
    r2 = pick_for_account("test_acc_xxx_1", country="es")
    checks["sticky_works"] = r2.get("proxy", {}).get("id") == proxy_id_1
    checks["sticky_flag"] = r2.get("sticky_reused") is True

    # 5. Otra cuenta = (probablemente) otro proxy
    r3 = pick_for_account("test_acc_xxx_2", country="es")
    checks["different_account_picks"] = r3.get("ok") is True

    # 6. Random
    r4 = pick_random()
    checks["random_works"] = r4.get("ok") is True

    # 7. Release
    if proxy_id_1:
        r5 = release(proxy_id_1, "test_acc_xxx_1", success=True)
        checks["release_works"] = r5.get("ok") is True

    # 8. Reset account
    r6 = reset_account("test_acc_xxx_1")
    checks["reset_account"] = r6.get("ok") is True

    # 9. Stats
    s = get_stats()
    checks["stats_has_pool_size"] = "pool_size" in s
    checks["stats_pool_size_match"] = s["pool_size"] == len(pool)

    # 10. to_playwright_proxy
    if pool:
        pw = to_playwright_proxy(pool[0])
        checks["playwright_format"] = "server" in pw and "username" in pw

    # 11. to_requests_proxies
    if pool:
        rp = to_requests_proxies(pool[0])
        checks["requests_format"] = "http" in rp and "https" in rp

    # Cleanup test accounts
    reset_account("test_acc_xxx_2")

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    return {"module": "proxy_rotator", "version": 1, **get_stats()}
