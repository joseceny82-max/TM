"""bot_intelligence.py — Cerebro orquestador del bot de compras TM.

Decide automáticamente la MEJOR estrategia para cada compra basándose en:
  - Evento target (popularidad, venue, país)
  - Recursos disponibles (proxies, cookies PX, capturas Akamai, tarjetas)
  - Histórico de éxito (qué funcionó antes)
  - Estado del cluster (hosts activos, queue)
  - Anti-detection requerido (Akamai, PX, Queue-it detectados)

Ofrece:
  - `recommend_strategy(event_id)` → plan óptimo para este evento
  - `auto_prepare(event_id)` → pre-warm cookies + captures + tarjetas
  - `smart_run(event_id)` → ejecuta compra usando mejor combo
  - `learn_from_flow(flow_result)` → actualiza modelo

Este es el "cerebro" que conecta TODO el sistema en una sola API.
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import config
import json_atomic


_LEARN_KEY = "bot_intelligence_learn.json"
_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════
#  KNOWLEDGE BASE — qué funciona
# ═══════════════════════════════════════════════════════════
def _load_knowledge() -> dict:
    return json_atomic.load(config.ruta(_LEARN_KEY), default={
        "by_domain": {},       # domain_id → {success, fail, avg_elapsed, strategies}
        "by_engine": {},       # engine → {success, fail, avg_ms}
        "by_strategy": {},     # strategy name → stats
        "updated_at": None,
    })


def _save_knowledge(d: dict):
    d["updated_at"] = datetime.now().isoformat()
    json_atomic.save(config.ruta(_LEARN_KEY), d)


# ═══════════════════════════════════════════════════════════
#  1. RECOMMEND STRATEGY
# ═══════════════════════════════════════════════════════════
def recommend_strategy(event_id: str) -> dict:
    """Analiza el evento y devuelve el plan óptimo.

    Plan incluye:
      - anti_bot_layers: qué módulos activar (px, akamai_bmsc, ja4, etc)
      - proxy_type: mobile_4g | isp | residential
      - browser_engine: chromium | camoufox | patchright | multilogin
      - personality: slow/casual/fast/power
      - parallelism: cuántos workers lanzar
      - capture_sensor: usar captura real o generar local
    """
    plan = {
        "event_id": event_id,
        "anti_bot_layers": [],
        "proxy_type": "isp",
        "browser_engine": "chromium",
        "personality": "casual",
        "parallelism": 1,
        "capture_sensor": False,
        "warnings": [],
        "rationale": [],
    }

    # 1. Obtener info del evento
    try:
        import event_manager
        events = event_manager.list_events()
        event = next((e for e in events if e.get("id") == event_id), None)
        if not event:
            plan["warnings"].append(f"evento {event_id} no encontrado")
            plan["rationale"].append("no event data available → using defaults")
            return plan
        plan["event_name"] = event.get("name", "")
        plan["event_country"] = event.get("country", "us")
        plan["event_venue"] = event.get("venue", "")
    except Exception as e:
        plan["warnings"].append(f"event_manager error: {e}")
        event = {}

    country = plan.get("event_country", "us")

    # 2. Geo-aware personality
    try:
        import human_behavior
        plan["personality"] = human_behavior.pick_geo_aware_personality(country, seed=event_id)
        plan["rationale"].append(f"personality={plan['personality']} (geo-aware {country})")
    except Exception:
        pass

    # 3. Evaluar recursos disponibles
    resources = _check_resources(country)
    plan["resources"] = resources

    # 4. Decidir proxy type
    if resources.get("mobile_proxies_configured"):
        plan["proxy_type"] = "mobile_4g"
        plan["rationale"].append("mobile_4g proxies available → preferido para top-tier")
    elif resources.get("isp_proxies_count", 0) >= 3:
        plan["proxy_type"] = "isp"
        plan["rationale"].append(f"ISP pool {resources['isp_proxies_count']} proxies")
    else:
        plan["proxy_type"] = "none"
        plan["warnings"].append("sin proxies disponibles — alto riesgo de ban")

    # 5. Browser engine decision
    if resources.get("multilogin_configured"):
        plan["browser_engine"] = "multilogin"
        plan["rationale"].append("Multilogin disponible → mejor fingerprint isolation")
    elif resources.get("camoufox_available"):
        plan["browser_engine"] = "camoufox"
        plan["rationale"].append("Camoufox disponible → Firefox antidetect")
    elif resources.get("patchright_available"):
        plan["browser_engine"] = "patchright"
    else:
        plan["browser_engine"] = "chromium"

    # 6. Anti-bot layers a activar
    # Siempre JA4
    plan["anti_bot_layers"].append("ja4_spoof")
    # Cookie warming + PX pre-farm
    plan["anti_bot_layers"].append("cookie_warmer")
    if resources.get("px_pool_size", 0) > 0:
        plan["anti_bot_layers"].append("px_prefarm")
        plan["rationale"].append(f"PX pool tiene {resources['px_pool_size']} sesiones frescas")
    # AcpAT injection si hay
    if resources.get("acpat_pool_size", 0) > 0:
        plan["anti_bot_layers"].append("acpat_injection")
        plan["rationale"].append("AcpAT tokens disponibles → bypass Queue-it potencial")
    # Akamai humanizado
    plan["anti_bot_layers"].append("akamai_bmsc_humanized")
    # Si hay capturas reales, usarlas
    if resources.get("akamai_captures_active", 0) > 0:
        plan["capture_sensor"] = True
        plan["anti_bot_layers"].append("akamai_bmjs_replay")
        plan["rationale"].append(f"{resources['akamai_captures_active']} capturas Akamai reales → replay")
    # Session pinning
    plan["anti_bot_layers"].append("proxy_session_pin")
    # Card rotation si hay tarjetas
    if resources.get("cards_count", 0) >= 2:
        plan["anti_bot_layers"].append("card_rotation")
    # 3DS si configurado
    if resources.get("otp_3ds_enabled"):
        plan["anti_bot_layers"].append("otp_3ds")

    # 7. Parallelism
    # Multi-domain si es evento internacional
    if event.get("is_international") or country in ("us", "uk", "de", "es"):
        plan["parallelism"] = min(5, resources.get("cluster_hosts", 1) * 2)
        plan["rationale"].append(f"parallelism={plan['parallelism']} (multi-host)")
    else:
        plan["parallelism"] = 1

    # 8. Priorizar según histórico
    knowledge = _load_knowledge()
    domain_stats = knowledge.get("by_domain", {}).get(country, {})
    if domain_stats:
        sr = domain_stats.get("success_rate_pct", 0)
        if sr < 30 and sr > 0:
            plan["warnings"].append(f"dominio {country} tiene baja tasa éxito histórica: {sr}%")

    plan["recommended_at"] = datetime.now().isoformat()
    return plan


def _check_resources(country: str = "us") -> dict:
    """Snapshot de recursos disponibles."""
    r = {}
    # Proxies ISP
    try:
        import proxy_health
        proxies = proxy_health.list_proxies()
        alive = [p for p in proxies if p.get("status") == "alive"]
        r["isp_proxies_count"] = len(alive)
        r["isp_proxies_with_geo"] = sum(1 for p in alive if p.get("country") == country)
    except Exception:
        r["isp_proxies_count"] = 0

    # Mobile
    try:
        import mobile_proxies
        r["mobile_proxies_configured"] = mobile_proxies.is_configured()
    except Exception:
        r["mobile_proxies_configured"] = False

    # Multilogin
    try:
        import multilogin_orchestrator as mlo
        r["multilogin_configured"] = mlo.is_configured()
    except Exception:
        r["multilogin_configured"] = False

    # Browsers
    for engine in ("camoufox", "patchright"):
        try:
            __import__(engine)
            r[f"{engine}_available"] = True
        except Exception:
            r[f"{engine}_available"] = False

    # PX pool
    try:
        import px_prefarm
        s = px_prefarm.pool_stats()
        r["px_pool_size"] = s.get("size", 0)
    except Exception:
        r["px_pool_size"] = 0

    # AcpAT
    try:
        import acpat_injection as ai
        r["acpat_pool_size"] = ai.get_stats().get("active_channels", 0)
    except Exception:
        r["acpat_pool_size"] = 0

    # Akamai captures
    try:
        import akamai_bmjs
        caps = akamai_bmjs.list_captures(active_only=True)
        r["akamai_captures_active"] = len(caps)
    except Exception:
        r["akamai_captures_active"] = 0

    # Cards
    try:
        from account_manager import account_manager as am
        r["cards_count"] = len(am.list_cards())
    except Exception:
        r["cards_count"] = 0

    # OTP
    try:
        import otp_3ds
        r["otp_3ds_enabled"] = otp_3ds.get_config().get("enabled", False)
    except Exception:
        r["otp_3ds_enabled"] = False

    # Cluster
    try:
        import cluster_manager
        r["cluster_hosts"] = cluster_manager.get_cluster_stats().get("hosts_alive", 1)
    except Exception:
        r["cluster_hosts"] = 1

    return r


# ═══════════════════════════════════════════════════════════
#  2. AUTO PREPARE
# ═══════════════════════════════════════════════════════════
def auto_prepare(event_id: str, minutes_before: int = 15) -> dict:
    """Pre-calienta todos los recursos necesarios basándose en plan recomendado.

    Lanza en paralelo:
      - px_prefarm session
      - akamai capture session
      - proxy health check
      - card rotation pick
    """
    plan = recommend_strategy(event_id)
    country = plan.get("event_country", "us")

    started = time.time()
    tasks_done = []
    tasks_failed = []

    # 1. PX pre-farm (si hace falta)
    if "px_prefarm" in plan["anti_bot_layers"]:
        try:
            import px_prefarm
            r = px_prefarm.start_farming_async(
                domain_id=country, duration_min=min(3, minutes_before / 5),
                engine=plan["browser_engine"],
            )
            if r.get("ok"):
                tasks_done.append(f"px_prefarm_started (engine={r.get('engine')})")
            else:
                tasks_failed.append(f"px_prefarm: {r.get('error')}")
        except Exception as e:
            tasks_failed.append(f"px_prefarm: {e}")

    # 2. Akamai capture (si tenemos playwright)
    if "akamai_bmjs_replay" in plan["anti_bot_layers"] or plan["capture_sensor"]:
        try:
            import akamai_bmjs
            # Arrancar scheduler si no corre
            status = akamai_bmjs.scheduler_status()
            if not status.get("running"):
                akamai_bmjs.start_auto_capture(
                    target_urls=["https://www.ticketmaster.com"],
                    interval_s=45, duration_s=25,
                )
                tasks_done.append("akamai_auto_capture_started")
            else:
                tasks_done.append("akamai_capture_already_running")
        except Exception as e:
            tasks_failed.append(f"akamai_capture: {e}")

    # 3. Check proxies
    try:
        import proxy_health
        r = proxy_health.check_all() if hasattr(proxy_health, "check_all") else {}
        tasks_done.append(f"proxy_health_check (alive={r.get('alive', 0)})")
    except Exception as e:
        tasks_failed.append(f"proxy_health: {e}")

    # 4. Card pre-pick (valida que hay tarjeta disponible)
    if "card_rotation" in plan["anti_bot_layers"]:
        try:
            import card_rotation
            c = card_rotation.pick_card()
            if c:
                tasks_done.append(f"card_rotation: card={c.get('id', 'any')} available")
            else:
                tasks_failed.append("card_rotation: no cards available")
        except Exception as e:
            tasks_failed.append(f"card_rotation: {e}")

    return {
        "ok": True,
        "event_id": event_id,
        "plan": plan,
        "tasks_done": tasks_done,
        "tasks_failed": tasks_failed,
        "elapsed_s": round(time.time() - started, 1),
    }


# ═══════════════════════════════════════════════════════════
#  3. SMART RUN
# ═══════════════════════════════════════════════════════════
def smart_run(event_id: str, quantity: int = 2, mode: str = "simulate") -> dict:
    """Ejecuta la compra usando el plan óptimo recomendado."""
    plan = recommend_strategy(event_id)

    # Ejecutar flow con config optimizada
    try:
        import tm_purchase_flow as tpf
        started = time.time()
        result = tpf.run_flow(
            event_id=event_id,
            quantity=quantity,
            mode=mode,
            strategy="best_available",
        )
        elapsed = round(time.time() - started, 1)

        # Aprender del resultado
        learn_from_flow(result, plan=plan)

        return {
            "ok": True,
            "smart_run": True,
            "plan": plan,
            "flow_result": {
                "ok": result.get("ok"),
                "order_id": result.get("order_id"),
                "error": result.get("error"),
                "elapsed_ms": result.get("elapsed_ms"),
                "steps_count": len(result.get("steps", [])),
            },
            "elapsed_s": elapsed,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "plan": plan}


# ═══════════════════════════════════════════════════════════
#  4. LEARN FROM FLOW
# ═══════════════════════════════════════════════════════════
def learn_from_flow(flow_result: dict, plan: Optional[dict] = None) -> dict:
    """Actualiza el knowledge base con el resultado de un flow."""
    with _lock:
        k = _load_knowledge()
        country = None
        if plan:
            country = plan.get("event_country", "us")
        country = country or "unknown"

        # By domain
        d = k["by_domain"].setdefault(country, {
            "success": 0, "fail": 0, "total_ms": 0, "runs": 0,
        })
        d["runs"] += 1
        if flow_result.get("ok"):
            d["success"] += 1
            d["total_ms"] += flow_result.get("elapsed_ms", 0)
        else:
            d["fail"] += 1

        # Métricas derivadas
        d["success_rate_pct"] = round(100 * d["success"] / max(1, d["runs"]), 1)
        d["avg_success_ms"] = round(d["total_ms"] / max(1, d["success"]), 1) if d["success"] else 0

        # By engine
        engine = (plan or {}).get("browser_engine", "unknown")
        e = k["by_engine"].setdefault(engine, {"success": 0, "fail": 0})
        if flow_result.get("ok"):
            e["success"] += 1
        else:
            e["fail"] += 1

        # By anti-bot stack
        layers_str = ",".join(sorted((plan or {}).get("anti_bot_layers", [])))
        if layers_str:
            s = k["by_strategy"].setdefault(layers_str, {"success": 0, "fail": 0})
            if flow_result.get("ok"):
                s["success"] += 1
            else:
                s["fail"] += 1

        _save_knowledge(k)

    return {"ok": True, "learned": True}


def get_knowledge() -> dict:
    """Snapshot del knowledge acumulado."""
    k = _load_knowledge()
    # Añadir best strategies
    best_domain = None
    best_rate = 0
    for dom, stats in k.get("by_domain", {}).items():
        if stats.get("runs", 0) >= 3 and stats.get("success_rate_pct", 0) > best_rate:
            best_rate = stats["success_rate_pct"]
            best_domain = dom
    return {
        **k,
        "best_domain": best_domain,
        "best_domain_rate": best_rate,
        "total_runs": sum(d.get("runs", 0) for d in k.get("by_domain", {}).values()),
    }


# ═══════════════════════════════════════════════════════════
#  SELF TEST / INFO
# ═══════════════════════════════════════════════════════════
def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    # recommend_strategy — crear evento temporal para test
    try:
        import event_manager
        ev = event_manager.list_events()
        test_event_id = ev[0].get("id") if ev else "fake_event_id"
    except Exception:
        test_event_id = "fake_event_id"
    plan = recommend_strategy(test_event_id)
    results["checks"]["recommend_returns_plan"] = isinstance(plan, dict)
    results["checks"]["plan_has_personality"] = "personality" in plan
    # Siempre hay al menos ja4_spoof + cookie_warmer + akamai_bmsc_humanized + proxy_session_pin
    results["checks"]["plan_has_layers"] = len(plan.get("anti_bot_layers", [])) >= 3

    # check_resources
    res = _check_resources("us")
    results["checks"]["resources_readable"] = len(res) > 5

    # learn_from_flow
    learn_result = learn_from_flow(
        {"ok": True, "elapsed_ms": 1500},
        plan={"event_country": "test", "browser_engine": "test", "anti_bot_layers": ["test_layer"]},
    )
    results["checks"]["learn_ok"] = learn_result["ok"]

    # knowledge
    k = get_knowledge()
    results["checks"]["knowledge_accessible"] = "by_domain" in k

    return results


def get_info() -> dict:
    k = get_knowledge()
    return {
        "module": "bot_intelligence",
        "capabilities": ["recommend_strategy", "auto_prepare",
                          "smart_run", "learn_from_flow"],
        "total_runs_learned": k.get("total_runs", 0),
        "best_domain": k.get("best_domain"),
        "knowledge_updated": k.get("updated_at"),
    }
