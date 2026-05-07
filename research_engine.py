"""research_engine.py — Motor inteligente del Estudio de Mejoras.

Analiza el estado real del proyecto y prioriza dinámicamente qué mejoras
del catálogo (research_catalog.CATALOG) aplicar AHORA. Combina tres señales:

  1. **Implementación detectada** — escanea módulos .py del proyecto y
     decide para cada mejora si está `missing | stub | partial | implemented`,
     basándose en presencia de archivos y funciones esperadas.

  2. **Señales de urgencia** — lee logs reales (problem_center, audit_log,
     error_manager, health_monitor) en una ventana de 24h y cuenta cuántos
     eventos coinciden con keywords ligados a cada mejora (ej. "akamai",
     "_abck", "captcha_fail" → suben el score de las mejoras anti-Akamai
     y captcha respectivamente).

  3. **Score** — fórmula:
        score = (impact / 5) * (1 - implementation_pct) * 0.6
              + min(urgency, 10)/10 * 0.4
        × 100   (escala 0-100)

API pública:
  run_audit(top_n=10)        → audit completo {top, all, by_status, urgency_signals}
  get_audit(force=False)     → versión cacheada (TTL 600s) + persistida en disco
  start_scheduler(times)     → re-ejecuta el audit a cada hora HH:MM en `times`
  stop_scheduler()           → cancela timers
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import config
import json_atomic
import research_catalog


# ═══════════════════════════════════════════════════════════════════
#  MAPEO DE SEÑALES POR MEJORA
# ═══════════════════════════════════════════════════════════════════
# Para cada improvement_id, declaramos:
#   files: módulos .py cuya presencia indica implementación
#   fns:   patrones de nombre de función esperados (case-insensitive)
#
# La detección es heurística (no garantiza que el módulo funcione, sólo
# que está escrito y tiene las funciones clave).
IMPLEMENTATION_HINTS: Dict[str, Dict[str, List[str]]] = {
    # === Anti-Bot ===
    "qit_rotation":         {"files": ["queueit_visitorsession.py"], "fns": ["rotate", "queueit"]},
    "px_prefarm":           {"files": ["px_prefarm.py"], "fns": ["prefarm", "warm"]},
    "imperva_bypass":       {"files": ["imperva_bypass.py"],
                             "fns": ["is_imperva_challenge", "capture_js_challenge",
                                      "solve_challenge", "bypass", "cache_get"]},
    "shape_ja4":            {"files": ["ja4_spoof.py", "ja4_pinning.py", "ja4hs_logging.py"],
                             "fns": ["spoof", "ja4"]},
    "akamai_ab_bypass":     {"files": ["akamai_sensor_v3.py", "akamai_validator.py", "sensor_js_watcher.py"],
                             "fns": ["sensor", "validator"]},
    "akamai_bmjs_deobf":    {"files": ["akamai_bmjs.py", "akamai_bmsc.py", "akamai_v3_hash_refresh.py"],
                             "fns": ["deobf", "capture", "replay", "analyze"]},
    "akamai_sbsd_gen":      {"files": ["akamai_sbsd.py"],
                             "fns": ["fetch_sbsd_script", "build_sbsd_payload",
                                      "encrypt_sbsd_payload", "submit_sbsd_payload",
                                      "SBSDSession"]},
    "datadome_bypass":      {"files": ["datadome_emulator.py", "datadome_proxy_hooks.py"],
                             "fns": ["bypass", "emulate", "hook"]},
    "kasada_bypass":        {"files": ["kasada_solver.py"], "fns": ["solve", "kasada"]},
    "shape_vm":             {"files": ["shape_vm_solver.py"], "fns": ["disasm", "vm"]},
    "tls_ct_monitor":       {"files": ["tls_ct_monitor.py"], "fns": ["monitor", "ct"]},
    "h2_frame_match":       {"files": ["http2_frame_match.py", "http2_fingerprint.py"], "fns": ["frame", "fingerprint"]},
    "h3_quic":              {"files": ["http3_quic.py", "http3_settings_match.py"], "fns": ["quic", "h3"]},

    # === CAPTCHA ===
    "captcha_capsolver":    {"files": ["captcha_manager.py"], "fns": ["capsolver", "solve"]},
    "captcha_2captcha":     {"files": ["captcha_manager.py"], "fns": ["twocaptcha", "_2captcha", "solve"]},
    "captcha_anticaptcha":  {"files": ["captcha_manager.py"], "fns": ["anticaptcha"]},
    "captcha_capmonster":   {"files": ["captcha_manager.py"], "fns": ["capmonster"]},
    "captcha_cv":           {"files": ["captcha_cv_solver.py"], "fns": ["solve", "cv"]},
    "captcha_llm":          {"files": ["llm_captcha.py"], "fns": ["solve_llm", "vision"]},

    # === Cookies / Warming ===
    "cookie_warmer":        {"files": ["cookie_warmer.py"], "fns": ["warm", "inject_acpat"]},
    "cookie_optimizer":     {"files": ["cookie_optimizer.py"], "fns": ["optimize", "filter"]},
    "trust_tuple_warming":  {"files": ["trust_tuple_warming.py", "cookie_warmer.py"], "fns": ["warm", "tuple"]},
    "session_persistence":  {"files": ["session_persistence.py"],
                             "fns": ["save", "load", "invalidate", "mark_failed",
                                      "apply_to_playwright_context"]},
    "acpat_pool":           {"files": ["acpat_pool.json", "acpat_sync.py", "acpat_injection.py"],
                             "fns": ["sync", "inject", "broadcast"]},

    # === Proxies ===
    "mobile_proxies":       {"files": ["mobile_proxies.py"], "fns": ["pick", "rotate"]},
    "proxy_health":         {"files": ["proxy_health.py"], "fns": ["check", "score"]},
    "proxy_intelligence":   {"files": ["proxy_intelligence.py"], "fns": ["intelligence", "rate"]},
    "proxy_ipqs":           {"files": ["proxy_ipqs.py"], "fns": ["ipqs", "score"]},
    "proxy_session_pin":    {"files": ["proxy_session_pin.py", "proxy_per_account.py"], "fns": ["pin", "session"]},
    "proxy_rotator":        {"files": ["proxy_rotator.py"], "fns": ["rotate", "pick"]},
    "asn_drift":            {"files": ["asn_ip_drift.py"], "fns": ["drift", "asn"]},

    # === Browser / Fingerprint ===
    "browser_pool":         {"files": ["browser_pool.py"], "fns": ["acquire", "release"]},
    "advanced_fingerprint": {"files": ["advanced_fingerprint.py"], "fns": ["spoof", "canvas", "webgl"]},
    "rebrowser_stealth":    {"files": ["rebrowser_stealth.py"], "fns": ["patch", "stealth"]},
    "nodriver_engine":      {"files": ["nodriver_engine.py"], "fns": ["launch", "navigate"]},
    "human_behavior":       {"files": ["human_behavior.py"], "fns": ["click", "scroll", "type"]},
    "behavior_chain":       {"files": ["behavior_chain.py"],
                             "fns": ["build_default_matrix", "sample_path",
                                      "next_step", "dwell_time", "learn_from_session"]},
    "continuous_biometrics":{"files": ["continuous_biometrics.py"], "fns": ["track", "biometric"]},
    "uach_grease":          {"files": ["uach_grease_validator.py"], "fns": ["validate", "grease"]},

    # === Pago / 3DS ===
    "adyen_tokenizer":      {"files": ["adyen_tokenizer.py", "adyen_merchant_token.py"],
                             "fns": ["tokenize", "merchant"]},
    "apple_google_pay":     {"files": ["apple_google_pay.py"], "fns": ["apple", "google", "pay"]},
    "emv_3ds_v23":          {"files": ["emv_3ds_v23.py"], "fns": ["challenge", "frictionless"]},
    "otp_3ds_autofill":     {"files": ["otp_3ds_autofill.py", "otp_3ds.py"], "fns": ["autofill", "otp"]},
    "psd3_payment_retry":   {"files": ["psd3_payment_retry.py", "psd3_mandate_setup.py"], "fns": ["retry", "mandate"]},
    "card_rotation":        {"files": ["card_rotation.py"], "fns": ["rotate", "pick"]},

    # === Anti-detect avanzado ===
    "hyper_sdk":            {"files": ["hyper_sdk_chain.py", "hyper_sdk_client.py"], "fns": ["chain", "client"]},
    "tm_mobile_sdk":        {"files": ["tm_mobile_sdk.py"], "fns": ["mobile", "sdk"]},
    "agent_attestation":    {"files": ["agent_attestation.py"], "fns": ["attest", "verify"]},
    "nudata_emulator":      {"files": ["nudata_emulator.py"], "fns": ["emulate", "session"]},
    "live_readiness":       {"files": ["live_readiness.py"], "fns": ["check", "ready"]},

    # === Observabilidad / Performance / Compliance ===
    "compliance_gate":         {"files": ["compliance_gate.py"], "fns": ["check", "gate"]},
    "eu_dfa_compliance_gate":  {"files": ["compliance_gate.py"],
                                "fns": ["log_consent", "validate_resale",
                                         "verify_chain_integrity", "gated_for_dfa",
                                         "export_audit_quarterly"]},
    "bots_compliance":      {"files": ["bots_compliance.py"], "fns": ["compliance", "waiver"]},
    "rate_limit":           {"files": ["rate_limit.py"], "fns": ["check", "throttle"]},
    "health_monitor":       {"files": ["health_monitor.py"], "fns": ["uptime", "probe"]},
    "ml_validator":         {"files": ["ml_validator.py"], "fns": ["predict", "train"]},
    "discovery_adaptive":   {"files": ["discovery_adaptive.py"], "fns": ["adapt", "discover"]},
    "presale_farming":      {"files": ["presale_farming.py"], "fns": ["farm", "presale"]},
    "registration_promoter":{"files": ["registration_promoter.py"], "fns": ["promote", "register"]},
}


# Keywords que cuando aparecen en logs/audit/problem_center suben la urgencia
# de una mejora específica. Cada keyword se busca en LOWERCASE.
URGENCY_SIGNALS: Dict[str, List[str]] = {
    "akamai_bmjs_deobf":    ["akamai", "bm_block", "_abck", "sensor_data", "bm_sz"],
    "akamai_ab_bypass":     ["akamai", "_abck", "bm_sz", "akam"],
    "datadome_bypass":      ["datadome", "_dd", "captcha_dd", "dd_block"],
    "px_prefarm":           ["perimeterx", "_px", "px_block", "_pxhd", "pxhd_missing"],
    "kasada_bypass":        ["kasada", "kpsdk", "x-kpsdk"],
    "qit_rotation":         ["queue-it", "queueit", "acpat", "qsid"],
    "shape_ja4":            ["tls_fp", "ja3_blocked", "ja4_drift", "fingerprint_drift", "tls_block"],
    "shape_vm":             ["shape", "f5_shape"],
    "captcha_capsolver":    ["captcha_fail", "captcha_timeout", "captcha_unsolved", "recaptcha_fail"],
    "captcha_2captcha":     ["2captcha", "captcha_fail"],
    "captcha_cv":           ["captcha_cv_fail", "image_captcha"],
    "captcha_llm":          ["llm_captcha", "vision_fail"],
    "imperva_bypass":       ["imperva", "incapsula", "_incap_", "visid_incap"],
    "trust_tuple_warming":  ["trust_drift", "geo_mismatch", "asn_drift", "fp_mismatch"],
    "mobile_proxies":       ["proxy_block", "ip_blocked", "proxy_dead", "datacenter_blocked"],
    "proxy_health":         ["proxy_dead", "proxy_timeout", "proxy_error"],
    "proxy_ipqs":           ["ipqs_low", "fraud_score"],
    "asn_drift":            ["asn_change", "asn_drift", "asn_mismatch"],
    "browser_pool":         ["browser_crash", "playwright_timeout", "page_closed"],
    "human_behavior":       ["bot_detected", "behavior_flag", "humanity_check"],
    "advanced_fingerprint": ["canvas_fp", "webgl_fp", "fp_collision"],
    "adyen_tokenizer":      ["adyen", "payment_decline", "card_decline"],
    "emv_3ds_v23":          ["3ds", "challenge_fail", "3ds2"],
    "otp_3ds_autofill":     ["3ds_otp", "otp_3ds_fail", "challenge_otp"],
    "psd3_payment_retry":   ["psd2", "psd3", "soft_decline"],
    "rate_limit":           ["429", "rate_limit", "too_many_requests"],
    "health_monitor":       ["health_fail", "uptime_drop"],
}

# Peso por nivel de impacto declarado en CATALOG
IMPACT_WEIGHT = {"Bajo": 1, "Medio": 2, "Alto": 3, "Muy alto": 4, "Máximo": 5}

# Una señal cuenta como "urgente" si ocurrió en esta ventana
URGENCY_WINDOW_HOURS = 24

# Estado de implementación → factor de penalización (1.0 = totalmente implementado, no urge)
IMPL_PCT = {"missing": 0.0, "stub": 0.25, "partial": 0.6, "implemented": 1.0}


# ═══════════════════════════════════════════════════════════════════
#  DETECCIÓN DE IMPLEMENTACIÓN
# ═══════════════════════════════════════════════════════════════════
_SCAN_CACHE: Dict[str, Dict] = {}


def _scan_module(name: str) -> Dict:
    """Lee un archivo del proyecto y extrae métricas básicas. Cacheado por path+mtime."""
    p = config.ruta(name)
    if not os.path.exists(p):
        return {"present": False, "loc": 0, "fns": []}
    mtime = os.path.getmtime(p)
    cache_key = f"{p}|{mtime}"
    if cache_key in _SCAN_CACHE:
        return _SCAN_CACHE[cache_key]
    try:
        src = Path(p).read_text(encoding="utf-8", errors="ignore")
        info = {
            "present": True,
            "loc": len(src.splitlines()),
            "fns": re.findall(r"^\s*def\s+(\w+)\s*\(", src, flags=re.MULTILINE),
        }
    except Exception:
        info = {"present": True, "loc": 0, "fns": []}
    _SCAN_CACHE[cache_key] = info
    return info


# Aliases para fallback heurístico: token corto del id → palabra completa que
# aparece en nombres de archivo del proyecto.
_TOKEN_ALIAS = {
    "qit": "queueit", "cf": "cloudflare", "dd": "datadome",
    "px": "perimeterx", "cap": "captcha", "fp": "fingerprint",
    "ja4": "ja4", "h2": "http2", "h3": "http3",
    "cv": "cv", "ml": "ml", "ai": "ai",
    "isp": "proxy", "asn": "asn",
}

# Stopwords muy genéricas que dan falsos positivos
_STOPWORDS = {"bypass", "rotation", "warm", "spoof", "match", "solve", "rotate",
              "sticky", "monitor", "predict", "watch", "binding", "block",
              "leak", "enum", "drop", "chain", "pool", "deploy", "ha",
              "actions", "encryption", "log", "enrich", "allowlist", "ent",
              "flow", "flows", "edge", "cache", "metrics", "tracking",
              "trajectory", "shape", "mode", "prefarm", "inject", "send",
              "send", "pay", "checkout", "hold", "press", "v3", "v4",
              "behavior", "biometric", "biometrics", "entropy", "image",
              "audio", "text", "grid", "vm", "wasm", "worker", "ml",
              "early", "access", "presale", "screenshot", "handler",
              "challenge", "integrity", "probe", "proxy", "trust", "tuple",
              "drift", "consistency", "consistency", "warming",
              "session", "persistence", "warm", "fwd", "signal",
              "behavior", "bezier", "intent", "pattern",
              "compliance", "gate", "tra", "aware", "retry",
              "ip", "ct", "log", "monitor",  "consistency",
              "grease", "mobile", "5g", "4g", "ipv6"}


def _heuristic_files_for(imp_id: str) -> List[str]:
    """Si no hay hint manual, intenta encontrar archivos del proyecto cuyo
    nombre contenga tokens significativos del imp_id.

    p.ej. 'akamai_v3_cookiehash_chain' → ['akamai_v3_hash_refresh.py', ...]
    """
    proyecto = config.ruta("")  # carpeta del proyecto
    tokens = [t.lower() for t in imp_id.split("_") if len(t) >= 2]
    # expandir aliases
    expanded = set()
    for t in tokens:
        expanded.add(_TOKEN_ALIAS.get(t, t))
    # quitar stopwords
    candidates_tokens = [t for t in expanded if t not in _STOPWORDS and len(t) >= 3]
    if not candidates_tokens:
        return []

    found = []
    try:
        for f in os.listdir(proyecto):
            if not f.endswith(".py"):
                continue
            name = f[:-3].lower()
            # exigir AL MENOS 1 token >= 4 chars que matchee
            for tok in candidates_tokens:
                if len(tok) >= 4 and tok in name:
                    found.append(f)
                    break
    except Exception:
        pass
    return found[:5]  # cap


def detect_implementation(imp_id: str) -> Dict:
    """Para una mejora del catálogo, determina si está implementada en el proyecto.

    Devuelve:
      status: missing | stub | partial | implemented
      evidence: lista de archivos detectados con su LOC
      loc: total LOC sumadas
      fns_match: cuántas funciones esperadas se encontraron
      fns_expected: cuántas se esperaban (si 0, sólo se usa LOC)
    """
    hints = IMPLEMENTATION_HINTS.get(imp_id, {})
    files = list(hints.get("files", []) or [])
    fns_pat = hints.get("fns", []) or []

    # Fallback heurístico cuando no hay hint manual
    heuristic_used = False
    if not files:
        files = _heuristic_files_for(imp_id)
        heuristic_used = bool(files)

    if not files:
        return {"status": "missing", "evidence": [], "loc": 0,
                "fns_match": 0, "fns_expected": len(fns_pat),
                "heuristic": heuristic_used}

    evidence, loc_total, fns_match = [], 0, 0
    for f in files:
        s = _scan_module(f)
        if s["present"]:
            evidence.append({"file": f, "loc": s["loc"]})
            loc_total += s["loc"]
            for pat in fns_pat:
                if any(pat.lower() in fn.lower() for fn in s["fns"]):
                    fns_match += 1

    if not evidence:
        status = "missing"
    else:
        # umbrales: pct funciones encontradas + LOC total
        expected = max(1, len(fns_pat))
        pct = fns_match / expected if fns_pat else (1.0 if loc_total > 100 else 0.5)
        if pct >= 0.7 and loc_total >= 200:
            status = "implemented"
        elif pct >= 0.3 or loc_total >= 80:
            status = "partial"
        elif loc_total > 0:
            status = "stub"
        else:
            status = "missing"

    return {
        "status": status,
        "evidence": evidence,
        "loc": loc_total,
        "fns_match": fns_match,
        "fns_expected": len(fns_pat),
        "heuristic": heuristic_used,
    }


# ═══════════════════════════════════════════════════════════════════
#  RECOLECCIÓN DE SEÑALES DE URGENCIA
# ═══════════════════════════════════════════════════════════════════
def _parse_ts(v) -> float:
    """Acepta float, int, ISO string. Devuelve epoch seconds. 0 si no se puede."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        # heurística: ms si > 10^11
        return float(v) / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            s = v.replace("Z", "").split("+")[0].split(".")[0]
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0
    return 0.0


def _match_signals(text: str) -> List[str]:
    """Devuelve la lista de improvement_ids cuyos keywords matchean en `text`."""
    text_lc = text.lower()
    out = []
    for imp_id, kws in URGENCY_SIGNALS.items():
        if any(kw in text_lc for kw in kws):
            out.append(imp_id)
    return out


def collect_urgency() -> Dict[str, int]:
    """Cuenta señales recientes para cada mejora desde múltiples fuentes.

    Fuentes: problem_center, audit_log.json, error_manager (si existe),
    health_monitor.json, dashboard.log (cola).
    """
    now = time.time()
    window_start = now - URGENCY_WINDOW_HOURS * 3600
    counts: Dict[str, int] = {}

    def _bump(imp_id: str):
        counts[imp_id] = counts.get(imp_id, 0) + 1

    # 1) problem_center
    try:
        import problem_center as pc
        problems = []
        for fn_name in ("get_problems", "list_problems", "all_problems"):
            fn = getattr(pc, fn_name, None)
            if callable(fn):
                problems = fn() or []
                break
        for p in problems:
            ts = _parse_ts(p.get("last_seen") or p.get("created_at") or p.get("ts") or 0)
            if ts and ts < window_start:
                continue
            text = " ".join(str(p.get(k, "")) for k in ("title", "desc", "description", "category", "fingerprint"))
            for imp_id in _match_signals(text):
                _bump(imp_id)
    except Exception:
        pass

    # 2) audit_log.json
    try:
        audit = json_atomic.load(config.ruta("audit_log.json"), default=[])
        if isinstance(audit, dict):
            audit = audit.get("entries") or audit.get("log") or []
        for entry in audit[-1000:]:
            ts = _parse_ts(entry.get("ts") if isinstance(entry, dict) else 0)
            if ts and ts < window_start:
                continue
            text = json.dumps(entry, ensure_ascii=False)
            for imp_id in _match_signals(text):
                _bump(imp_id)
    except Exception:
        pass

    # 3) error_manager
    try:
        import error_manager as em
        errs = []
        for fn_name in ("get_recent", "get_recent_errors", "list_errors"):
            fn = getattr(em, fn_name, None)
            if callable(fn):
                try:
                    errs = fn(URGENCY_WINDOW_HOURS) or []
                except TypeError:
                    errs = fn() or []
                break
        for e in errs[-500:]:
            ts = _parse_ts(e.get("ts") if isinstance(e, dict) else 0)
            if ts and ts < window_start:
                continue
            text = json.dumps(e, ensure_ascii=False) if isinstance(e, dict) else str(e)
            for imp_id in _match_signals(text):
                _bump(imp_id)
    except Exception:
        pass

    # 4) health_monitor.json (si tiene módulos con failures)
    try:
        hm = json_atomic.load(config.ruta("health_monitor.json"), default={})
        if isinstance(hm, dict):
            text = json.dumps(hm, ensure_ascii=False)
            # solo si hay alguna señal de fallo reciente
            if any(k in text.lower() for k in ("fail", "error", "down", "blocked")):
                for imp_id in _match_signals(text):
                    _bump(imp_id)
    except Exception:
        pass

    return counts


# ═══════════════════════════════════════════════════════════════════
#  SCORING + AUDIT
# ═══════════════════════════════════════════════════════════════════
def score_improvement(imp: Dict, impl: Dict, urgency_count: int) -> Dict:
    impact = IMPACT_WEIGHT.get(imp.get("impact") or imp.get("impacto") or "Medio", 2)
    pct = IMPL_PCT.get(impl["status"], 0.0)
    gap = 1.0 - pct
    urgency_norm = min(urgency_count, 10) / 10.0
    base = (impact / 5.0) * gap
    score = round((base * 0.6 + urgency_norm * 0.4) * 100, 1)

    return {
        "score": score,
        "impact_weight": impact,
        "implementation_pct": pct,
        "urgency_count": urgency_count,
        "reason": _reason(imp, impl, urgency_count),
    }


def _reason(imp: Dict, impl: Dict, urg: int) -> str:
    parts = []
    if urg > 0:
        parts.append(f"{urg} eventos relacionados en últimas 24h")
    status_label = {
        "missing":     "módulo no detectado en el proyecto",
        "stub":        "stub presente · faltan funciones clave",
        "partial":     "implementación parcial · faltan piezas",
        "implemented": "ya implementado · sólo monitorizar",
    }
    parts.append(status_label.get(impl["status"], impl["status"]))
    impact = imp.get("impact") or imp.get("impacto", "")
    if impact in ("Muy alto", "Máximo"):
        parts.append(f"impacto {impact}")
    return " · ".join(parts)


def run_audit(top_n: int = 10) -> Dict:
    """Ejecuta una auditoría completa del catálogo contra el estado del proyecto."""
    urgency = collect_urgency()
    enriched = []
    for imp in research_catalog.CATALOG:
        impl = detect_implementation(imp["id"])
        urg = urgency.get(imp["id"], 0)
        score_data = score_improvement(imp, impl, urg)
        entry = {
            "id": imp["id"],
            "cat": imp.get("cat"),
            "cat_icon": imp.get("cat_icon"),
            "name": imp.get("name"),
            "desc": imp.get("desc"),
            "impact": imp.get("impact") or imp.get("impacto"),
            "difficulty": imp.get("difficulty") or imp.get("dificultad"),
            "effort": imp.get("effort"),
            "time": imp.get("time"),
            "audit": {
                "implementation": impl,
                **score_data,
                "audited_at": datetime.now().isoformat(),
            },
        }
        enriched.append(entry)

    enriched.sort(key=lambda x: x["audit"]["score"], reverse=True)

    done_ids = {t["improvement_id"] for t in research_catalog.list_tasks(status="done")}
    actionable = [e for e in enriched if e["id"] not in done_ids]

    by_status = {"missing": 0, "stub": 0, "partial": 0, "implemented": 0}
    for e in enriched:
        s = e["audit"]["implementation"]["status"]
        by_status[s] = by_status.get(s, 0) + 1

    return {
        "audited_at": datetime.now().isoformat(),
        "total": len(enriched),
        "actionable": len(actionable),
        "top": actionable[:top_n],
        "all": enriched,
        "by_status": by_status,
        "urgency_signals": urgency,
        "urgency_window_hours": URGENCY_WINDOW_HOURS,
    }


# ═══════════════════════════════════════════════════════════════════
#  CACHE + SCHEDULER
# ═══════════════════════════════════════════════════════════════════
_AUDIT_CACHE: Dict = {"data": None, "ts": 0.0}
_AUDIT_TTL = 600  # 10 min
_SCHED_TIMER: Optional[threading.Timer] = None
_SCHED_LOCK = threading.Lock()
_SCHED_TIMES: List[str] = []


def get_audit(force: bool = False, top_n: int = 10) -> Dict:
    """Devuelve el último audit (cacheado 10 min). force=True ignora cache."""
    now = time.time()
    if not force and _AUDIT_CACHE["data"] and (now - _AUDIT_CACHE["ts"] < _AUDIT_TTL):
        return _AUDIT_CACHE["data"]
    data = run_audit(top_n=top_n)
    _AUDIT_CACHE.update({"data": data, "ts": now})
    try:
        json_atomic.save(config.ruta("research_audit.json"), data)
    except Exception:
        pass
    return data


def last_audit_time() -> Optional[str]:
    if _AUDIT_CACHE["data"]:
        return _AUDIT_CACHE["data"].get("audited_at")
    try:
        prev = json_atomic.load(config.ruta("research_audit.json"), default=None)
        if isinstance(prev, dict):
            return prev.get("audited_at")
    except Exception:
        pass
    return None


def _schedule_next(times: List[str]):
    """Programa el próximo Timer al siguiente HH:MM en `times`."""
    global _SCHED_TIMER
    now = datetime.now()
    targets = []
    for t in times:
        if not t or ":" not in t:
            continue
        try:
            hh, mm = t.split(":")
            target = now.replace(hour=int(hh), minute=int(mm),
                                  second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            targets.append(target)
        except Exception:
            continue
    if not targets:
        return
    next_target = min(targets)
    delay = max(1.0, (next_target - now).total_seconds())

    def _fire():
        try:
            get_audit(force=True)
        except Exception:
            pass
        # re-schedule sólo si scheduler sigue activo
        with _SCHED_LOCK:
            if _SCHED_TIMES:
                _schedule_next(_SCHED_TIMES)

    _SCHED_TIMER = threading.Timer(delay, _fire)
    _SCHED_TIMER.daemon = True
    _SCHED_TIMER.start()


def start_scheduler(times: List[str]):
    """Inicia el scheduler con la lista de horas HH:MM (cualquier número)."""
    global _SCHED_TIMER, _SCHED_TIMES
    with _SCHED_LOCK:
        if _SCHED_TIMER:
            _SCHED_TIMER.cancel()
            _SCHED_TIMER = None
        _SCHED_TIMES = [t for t in (times or []) if t]
        if _SCHED_TIMES:
            _schedule_next(_SCHED_TIMES)


def stop_scheduler():
    global _SCHED_TIMER, _SCHED_TIMES
    with _SCHED_LOCK:
        _SCHED_TIMES = []
        if _SCHED_TIMER:
            _SCHED_TIMER.cancel()
            _SCHED_TIMER = None


def next_run() -> Optional[str]:
    """ISO timestamp de la próxima ejecución programada, o None."""
    if not _SCHED_TIMES:
        return None
    now = datetime.now()
    targets = []
    for t in _SCHED_TIMES:
        try:
            hh, mm = t.split(":")
            target = now.replace(hour=int(hh), minute=int(mm),
                                  second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            targets.append(target)
        except Exception:
            continue
    return min(targets).isoformat() if targets else None
