"""kasada_vm_solver — Devirtualización de Kasada ips.js para emitir x-kpsdk-ct/cd offline.

Flow:
  1. fetch ips.js (path con doble ID dinámico /<UUID1>/<UUID2>/ips.js)
  2. Decrypt array-based string encryption (RC4-like XOR + base64)
  3. Parsear VM opcodes → AST de instrucciones
  4. Resolver PoW embebido (proof-of-work iterativo SHA-256)
  5. Emitir headers x-kpsdk-ct (challenge token) + x-kpsdk-cd (computed data)
  6. Auto-refresh cuando Kasada rota el script (watcher TTL)

Estado actual: SCAFFOLD funcional con detección + parse parcial.
Devirtualización completa requiere reverse engineering iterativo del polimórfico.
Los bots top usan TakionAPI/Hyper Solutions para esto ($pay-as-you-go) o
mantienen patches del solver semanalmente (alta entropy).

API:
  - fetch_ips_js(domain) → script source
  - detect_kasada(html) → bool + signals
  - solve_offline(ips_js_source) → {ct, cd} | {ok:False, fallback}
  - solve_with_browser(page) → fallback browser-based
  - get_cached_solution(target_url, ttl_s=600) → cached headers
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import json_atomic

PROYECTO = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.path.join(PROYECTO, "data", "kasada_vm_cache.json")
_LOG_PATH = os.path.join(PROYECTO, "data", "kasada_vm_log.json")
os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)

CACHE_TTL_S = 600  # 10 min — Kasada típicamente rota cada 30-60 min
KASADA_BASE_PATH = "/149e9513-01fa-4fb0-aad4-566afd725d1b/2d206a39-8ed7-437e-a3be-862e0f06eea3/ips.js"

_lock = threading.RLock()


# ──────────────────────── DETECCIÓN ────────────────────────

KASADA_SIGNALS = (
    "x-kpsdk-ct", "x-kpsdk-cd", "kpsdk", "kasada",
    "ips.js", "kasada.io", "p.js?ar=",
)


def detect_kasada(html: str = "", headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Detecta presencia de Kasada en el HTML/headers de una respuesta."""
    out = {"detected": False, "signals": []}
    headers = headers or {}
    h_lower = " ".join(f"{k.lower()}={v}" for k, v in headers.items())
    body_lower = (html or "")[:50000].lower()
    for sig in KASADA_SIGNALS:
        if sig in body_lower or sig in h_lower:
            out["detected"] = True
            out["signals"].append(sig)
    return out


# ──────────────────────── FETCH ips.js ────────────────────

def fetch_ips_js(domain: str = "ticketmaster.es",
                 path: Optional[str] = None,
                 timeout_s: float = 15) -> Dict[str, Any]:
    """Descarga el ips.js de Kasada. Devuelve {ok, body, length, sha256}."""
    out: Dict[str, Any] = {"domain": domain, "fetched_at": datetime.now().isoformat()}
    try:
        import httpx
        url = f"https://www.{domain}{path or KASADA_BASE_PATH}"
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        r = httpx.get(url, headers={"User-Agent": ua,
                                      "Accept": "*/*",
                                      "Accept-Language": "es-ES,es;q=0.9"},
                      timeout=timeout_s)
        out["status"] = r.status_code
        out["url"] = url
        if r.status_code == 200:
            body = r.text
            out["ok"] = True
            out["body"] = body
            out["length"] = len(body)
            out["sha256"] = hashlib.sha256(body.encode()).hexdigest()[:16]
            out["body_preview"] = body[:300]
        else:
            out["ok"] = False
            out["error"] = f"http_{r.status_code}"
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


# ──────────────────────── PARSE VM (scaffold) ──────────────────

def parse_string_array(js_source: str) -> Dict[str, Any]:
    """Extrae el array de strings ofuscado de Kasada ips.js (RC4-like XOR pattern).

    Patrón típico: `var _0xabcd = ['a','b','c'...]` o `function _0xfunc(){return ['a','b']}`.
    Devuelve {found, count, sample, encryption_hint}.
    """
    out: Dict[str, Any] = {"found": False, "count": 0}
    # Patrón 1: array literal grande
    m = re.search(r"=\s*\[((?:'[^']{0,200}',?){50,})\]", js_source[:200000])
    if m:
        items = re.findall(r"'([^']{0,200})'", m.group(1))
        out["found"] = True
        out["count"] = len(items)
        out["sample"] = items[:5]
        # Detect encryption hint (RC4-like XOR)
        if any(re.match(r"^[A-Za-z0-9+/=]{20,}$", s) for s in items):
            out["encryption_hint"] = "base64"
        elif any(len(s) > 30 and not s.isascii() for s in items[:10]):
            out["encryption_hint"] = "binary_unicode"
        else:
            out["encryption_hint"] = "unknown_or_xor"
    return out


def parse_vm_opcodes(js_source: str) -> Dict[str, Any]:
    """Detecta el handler VM y opcodes (scaffold)."""
    out: Dict[str, Any] = {"detected": False, "opcodes": [], "signals": []}
    # Patrones típicos VM: switch(case), large dispatch table
    if re.search(r"switch\s*\([^)]*\)\s*\{(?:\s*case\s+\d+:[^}]*?break;){10,}", js_source):
        out["detected"] = True
        out["signals"].append("switch_case_dispatch")
    if re.search(r"function\s+\w+\s*\([^)]*\)\s*\{[^{}]*?(?:_0x[a-f0-9]+\[){5,}", js_source):
        out["signals"].append("string_array_lookups")
    # Heurístico: contar referencias a typed arrays (sospechoso de VM)
    if "Uint8Array" in js_source and "DataView" in js_source:
        out["signals"].append("typed_arrays")
    return out


def detect_pow_challenge(js_source: str) -> Dict[str, Any]:
    """Detecta proof-of-work embebido en ips.js."""
    out: Dict[str, Any] = {"detected": False, "type": None, "difficulty": None}
    if "SubtleCrypto" in js_source or "crypto.subtle" in js_source:
        out["signals"] = ["webcrypto_api"]
    if re.search(r"sha-?256", js_source, re.I):
        out["detected"] = True
        out["type"] = "sha256"
    # Heurístico de dificultad: buscar loops grandes
    m = re.search(r"for\s*\([^)]*?<\s*(\d{4,})", js_source)
    if m:
        out["estimated_iterations"] = int(m.group(1))
    return out


def analyze_ips_js(js_source: str) -> Dict[str, Any]:
    """Análisis estático completo del ips.js sin ejecución."""
    out: Dict[str, Any] = {
        "length": len(js_source),
        "sha256": hashlib.sha256(js_source.encode()).hexdigest()[:16],
    }
    out["string_array"] = parse_string_array(js_source)
    out["vm_opcodes"] = parse_vm_opcodes(js_source)
    out["pow_challenge"] = detect_pow_challenge(js_source)
    out["analyzed_at"] = datetime.now().isoformat()
    return out


# ──────────────────────── SOLVE ──────────────────────────

def solve_offline(js_source: str = "",
                  fetch_if_empty: bool = True,
                  domain: str = "ticketmaster.es") -> Dict[str, Any]:
    """Intenta resolver Kasada offline (sin browser).

    Estado actual: scaffold — devirtualización completa requiere mantenimiento
    semanal del polimórfico. Se sugiere fallback a TakionAPI/Hyper Solutions
    o solver browser-based (kasada_solver.py).
    """
    if not js_source and fetch_if_empty:
        f = fetch_ips_js(domain)
        if not f.get("ok"):
            return {"ok": False, "error": "fetch_failed", **f}
        js_source = f["body"]
    analysis = analyze_ips_js(js_source)
    return {
        "ok": False,
        "reason": "offline_solver_scaffold_only",
        "advice": ("Devirtualización VM completa requiere reverse iterativo. "
                    "Usar fallback browser kasada_solver.solve() o servicio "
                    "TakionAPI/Hyper Solutions con api_key."),
        "analysis": analysis,
        "next_steps": [
            "Implementar string array decoder con RC4 reverso",
            "Mapear top-50 opcodes via diff + console.log injection",
            "Compilar PoW SHA256 challenge a Python",
            "Refrescar al detectar nuevo sha256",
        ],
    }


def solve_with_browser_fallback(domain: str = "ticketmaster.es") -> Dict[str, Any]:
    """Fallback: usar kasada_solver.py existente (browser-based)."""
    try:
        import kasada_solver
        if hasattr(kasada_solver, "solve"):
            return kasada_solver.solve(domain)
        if hasattr(kasada_solver, "fetch_ips_js"):
            return {"ok": True, "via": "kasada_solver_legacy",
                    "result": kasada_solver.fetch_ips_js(domain)}
        return {"ok": False, "error": "kasada_solver no expone solve()"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ──────────────────────── CACHE ──────────────────────────

def get_cached_solution(target_url: str = "ticketmaster.es",
                        ttl_s: int = CACHE_TTL_S) -> Optional[Dict[str, Any]]:
    with _lock:
        cache = json_atomic.load(_CACHE_PATH, default={})
    entry = cache.get(target_url)
    if not entry:
        return None
    age = (datetime.now() - datetime.fromisoformat(entry["cached_at"])).total_seconds()
    if age > ttl_s:
        return None
    entry["age_s"] = age
    return entry


def save_solution(target_url: str, solution: Dict[str, Any]) -> None:
    with _lock:
        cache = json_atomic.load(_CACHE_PATH, default={})
        cache[target_url] = {**solution, "cached_at": datetime.now().isoformat()}
        json_atomic.save(_CACHE_PATH, cache)


# ──────────────────────── INFO + SELF TEST ─────────────────

def get_info() -> Dict[str, Any]:
    return {
        "module": "kasada_vm_solver",
        "purpose": "Devirtualizar Kasada ips.js para emitir x-kpsdk-ct/cd offline",
        "status": "scaffold (detection + analysis OK, full solve TODO)",
        "fallbacks": [
            "kasada_solver (browser-based, existente)",
            "TakionAPI Kasada module (commercial)",
            "Hyper Solutions Kasada API (commercial)",
        ],
        "endpoints": [
            "GET /api/kasada-vm/info",
            "POST /api/kasada-vm/fetch-ips",
            "POST /api/kasada-vm/analyze",
            "POST /api/kasada-vm/solve",
            "GET /api/kasada-vm/cache",
            "GET /api/kasada-vm/self-test",
        ],
    }


def self_test() -> Dict[str, Any]:
    out: Dict[str, Any] = {"checks": {}}
    out["checks"]["detect_callable"] = callable(detect_kasada)
    out["checks"]["fetch_callable"] = callable(fetch_ips_js)
    out["checks"]["analyze_callable"] = callable(analyze_ips_js)
    out["checks"]["solve_offline_callable"] = callable(solve_offline)
    # Detection sintética
    fake_html = "<script src='/ips.js'></script>"
    fake_headers = {"X-Kpsdk-Ct": "abc"}
    d = detect_kasada(fake_html, fake_headers)
    out["checks"]["detect_works"] = d["detected"] and len(d["signals"]) >= 1
    # Analyze sintético
    fake_js = "var _0xabc = ['a','b','c'];" * 100
    a = analyze_ips_js(fake_js)
    out["checks"]["analyze_returns_dict"] = isinstance(a, dict) and "string_array" in a
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
    print()
    print(json.dumps(get_info(), indent=2))
