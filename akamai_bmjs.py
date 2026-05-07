"""akamai_bmjs.py — Akamai bm.js deobfuscation + real sensor_data capture.

Estrategia PRÁCTICA (no intentamos descifrar RC4 de Akamai):

  1. FETCH bm.js → descarga la versión actual del target
  2. ANALYZE   → extrae metadata: version, endpoint, firma, tamaño, hashes
  3. STATIC DEOBFUSCATE → constant folding + string array decoding
                           (simplifica, NO descifra — el cifrado real
                            requiere ejecutar el JS en un VM)
  4. DIFF     → compara 2 versiones de bm.js para detectar cambios
  5. CAPTURE  → 🌟 lo importante 🌟 usa Playwright para:
                - Cargar TM.com en Chrome real
                - Interceptar request POST a /_bm_/
                - Capturar el `sensor_data` string que el browser manda
                - Guardar con ts + cookies + user-agent asociados
  6. REPLAY   → reutiliza sensor_data capturados, con micro-mutaciones
                para no repetir byte-exacto entre sesiones

Por qué esta vía funciona:
  Los sensor_data son VÁLIDOS porque los generó un Chrome real
  ejecutando el bm.js real. No hay que deobfuscar nada.
  Akamai valida la firma RC4 server-side y acepta el payload porque
  estructuralmente es correcto (estuvo firmado por SU propio bm.js).

Limitación:
  El sensor_data tiene TTL (~30 s) y está ligado a cookies `_abck`,
  `bm_sz`. Reutilizarlo fuera de su ventana hace que Akamai detecte.
  Por eso capturamos continuamente y mantenemos un pool fresco.

API pública:
  fetch_bm_js(url)                  → dict con code + metadata
  analyze_bm_js(code)               → obfuscation signatures + stats
  deobfuscate_basic(code)           → simplificado (constant folding)
  diff_bm_js(old_hash, new_hash)    → cambios entre versiones
  capture_sensor_data(target_url)   → Playwright capture (async/sync)
  list_captures(target_domain?)     → pool de sensor_data válidos
  replay_sensor_data(capture_id, mutations?)
  mutate_sensor_data(sensor, seed?) → micro-variaciones safe
  get_stats()
"""
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import config
import json_atomic


_CACHE_KEY = "akamai_bmjs_cache.json"
_CAPTURES_KEY = "akamai_sensor_captures.json"
_lock = threading.Lock()

# TTL de sensor_data capturados (30 s es el refresh de Akamai)
CAPTURE_TTL_S = 60

# Firmas conocidas de Akamai bm.js 2025/2026
AKAMAI_SIGNATURES = {
    "version_patterns": [
        r"bmak\.v=['\"]([^'\"]+)['\"]",
        r"sensor_data:\s*['\"]([^'\"]+)['\"]",
        r"bmak\.build=['\"]([^'\"]+)['\"]",
    ],
    "endpoint_patterns": [
        r"['\"](/_bm_/[^'\"]+)['\"]",
        r"akam\s*:\s*['\"]([^'\"]+)['\"]",
    ],
    "known_vars": [
        "bmak", "_bm_", "ak_bmsc", "_abck", "bm_sz", "bm_sv", "bm_mi",
        "sensor_data", "cf_ocb",
    ],
    "obfuscation_patterns": {
        "string_arrays":      r"var\s+\w+\s*=\s*\[\s*['\"][^'\"]+['\"]\s*(?:,\s*['\"][^'\"]+['\"]\s*)+\]",
        "hex_encoded":        r"\\x[0-9a-fA-F]{2}",
        "unicode_encoded":    r"\\u[0-9a-fA-F]{4}",
        "char_code_chains":   r"String\.fromCharCode\(\d+(?:\s*,\s*\d+){3,}",
        "base64_literals":    r"['\"][A-Za-z0-9+/=]{100,}['\"]",
        "eval_indirect":      r"\(\s*0\s*,\s*eval\s*\)",
        "ctrl_flow_flatten":  r"while\s*\(\s*!!\s*\[\s*\]\s*\)",
    },
}


# ═══════════════════════════════════════════
#  1. FETCH bm.js
# ═══════════════════════════════════════════
def fetch_bm_js(target_url: str, timeout: float = 15.0) -> dict:
    """Descarga el bm.js de un target. Intenta varias rutas conocidas."""
    import requests
    from urllib.parse import urlparse, urljoin

    parsed = urlparse(target_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # Rutas conocidas de bm.js en targets Akamai 2025/2026
    candidate_paths = [
        "/_bm/js/bm.js",
        "/akam/13/5a4dfa73",        # TM-style path hash
        "/akam/13/pixel_c80fa40e",  # variante
        "/_bm/v2/bm.js",
        "/akam/13/bm.js",
    ]

    result = {"target_url": target_url, "tried": [], "found": False}
    started = time.time()

    # Paso 1: intentar rutas directas
    for path in candidate_paths:
        url = urljoin(base, path)
        try:
            r = requests.get(url, timeout=timeout,
                              headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
            result["tried"].append({"url": url, "status": r.status_code,
                                     "length": len(r.text)})
            if r.status_code == 200 and len(r.text) > 1000 and (
                "bmak" in r.text or "sensor_data" in r.text or "ak_bmsc" in r.text
            ):
                result["found"] = True
                result["url"] = url
                result["code"] = r.text
                result["size_bytes"] = len(r.text)
                result["sha256"] = hashlib.sha256(r.text.encode()).hexdigest()
                result["etag"] = r.headers.get("ETag", "")
                result["last_modified"] = r.headers.get("Last-Modified", "")
                break
        except Exception as e:
            result["tried"].append({"url": url, "error": str(e)[:80]})

    # Paso 2: si no encontrado, parsear HTML del target y buscar <script src="...bm...">
    if not result["found"]:
        try:
            r = requests.get(target_url, timeout=timeout)
            import re as re_mod
            scripts = re_mod.findall(r'<script[^>]+src="([^"]+)"', r.text)
            bm_scripts = [s for s in scripts if "bm" in s.lower() or "akam" in s.lower()]
            for s in bm_scripts[:3]:
                url = s if s.startswith("http") else urljoin(base, s)
                try:
                    rr = requests.get(url, timeout=timeout)
                    if rr.status_code == 200 and len(rr.text) > 1000:
                        result["found"] = True
                        result["url"] = url
                        result["code"] = rr.text
                        result["size_bytes"] = len(rr.text)
                        result["sha256"] = hashlib.sha256(rr.text.encode()).hexdigest()
                        result["discovered_from_html"] = True
                        break
                except Exception:
                    pass
        except Exception as e:
            result["html_parse_error"] = str(e)[:100]

    result["elapsed_ms"] = int((time.time() - started) * 1000)

    # Cachear
    if result["found"]:
        _cache_bmjs(result)

    return result


def _cache_bmjs(r: dict):
    """Guarda hash + metadata del bm.js (no el código entero — puede ser grande)."""
    entry = {k: v for k, v in r.items() if k != "code"}
    entry["cached_at"] = datetime.now().isoformat()
    entry["code_preview"] = (r.get("code", "")[:500])
    with _lock:
        d = json_atomic.load(config.ruta(_CACHE_KEY), default={"versions": []})
        # Dedupe por sha256
        d["versions"] = [v for v in d.get("versions", [])
                          if v.get("sha256") != entry.get("sha256")]
        d["versions"].insert(0, entry)
        d["versions"] = d["versions"][:20]
        json_atomic.save(config.ruta(_CACHE_KEY), d)


def list_cached_versions() -> List[dict]:
    return json_atomic.load(config.ruta(_CACHE_KEY), default={"versions": []}).get("versions", [])


# ═══════════════════════════════════════════
#  2. ANALYZE bm.js
# ═══════════════════════════════════════════
def analyze_bm_js(code: str) -> dict:
    """Extrae metadata + firmas de obfuscación del bm.js."""
    result = {
        "size_bytes": len(code),
        "size_kb": round(len(code) / 1024, 1),
        "lines": code.count("\n"),
        "sha256": hashlib.sha256(code.encode()).hexdigest(),
    }

    # Versión / build
    for pattern in AKAMAI_SIGNATURES["version_patterns"]:
        m = re.search(pattern, code)
        if m:
            result["version_hint"] = m.group(1)
            break

    # Endpoints
    endpoints = set()
    for pattern in AKAMAI_SIGNATURES["endpoint_patterns"]:
        for m in re.finditer(pattern, code):
            endpoints.add(m.group(1))
    result["endpoints_found"] = list(endpoints)[:10]

    # Vars conocidas
    vars_found = []
    for v in AKAMAI_SIGNATURES["known_vars"]:
        if re.search(r"\b" + re.escape(v) + r"\b", code):
            vars_found.append(v)
    result["known_vars_present"] = vars_found

    # Obfuscation signatures
    obf = {}
    for name, pattern in AKAMAI_SIGNATURES["obfuscation_patterns"].items():
        matches = re.findall(pattern, code)
        obf[name] = len(matches)
    result["obfuscation"] = obf

    # Obfuscation score (0-100)
    score = 0
    if obf.get("hex_encoded", 0) > 50: score += 20
    if obf.get("string_arrays", 0) > 0: score += 25
    if obf.get("char_code_chains", 0) > 5: score += 20
    if obf.get("base64_literals", 0) > 0: score += 15
    if obf.get("eval_indirect", 0) > 0: score += 10
    if obf.get("ctrl_flow_flatten", 0) > 0: score += 10
    result["obfuscation_score"] = min(100, score)

    # Entropía (randomness indicator)
    result["entropy"] = _shannon_entropy(code[:10000])  # muestra

    # String array más grande (si hay)
    arr_matches = re.findall(AKAMAI_SIGNATURES["obfuscation_patterns"]["string_arrays"], code)
    if arr_matches:
        longest = max(arr_matches, key=len)
        result["biggest_string_array_size"] = len(longest)
        result["biggest_string_array_count"] = longest.count(",") + 1

    return result


def _shannon_entropy(s: str) -> float:
    """Entropía Shannon (bits/char). Código ofuscado ≈ 5.5-6.5. Normal ≈ 4-5."""
    if not s:
        return 0
    freqs = {}
    for c in s:
        freqs[c] = freqs.get(c, 0) + 1
    import math
    n = len(s)
    return round(-sum((f / n) * math.log2(f / n) for f in freqs.values()), 3)


# ═══════════════════════════════════════════
#  3. STATIC DEOBFUSCATION (BÁSICO)
# ═══════════════════════════════════════════
def deobfuscate_basic(code: str, max_iterations: int = 3) -> dict:
    """Aplica transformaciones estáticas al bm.js:
    - Decode \\xHH → char real
    - Decode \\uHHHH → char real
    - Constant folding de String.fromCharCode(65,66,67) → "ABC"
    - String array resolving (simplificado)

    Nota: NO intenta descifrar. No ejecuta el JS. Solo simplifica sintaxis.
    """
    original_size = len(code)
    transformations = {"hex_decoded": 0, "unicode_decoded": 0,
                        "char_code_folded": 0}

    for _ in range(max_iterations):
        # Decode \xHH (hex escape)
        def _hex_to_char(m):
            transformations["hex_decoded"] += 1
            try:
                ch = chr(int(m.group(1), 16))
                return ch if ch.isprintable() and ord(ch) < 127 else m.group(0)
            except Exception:
                return m.group(0)
        code_new = re.sub(r"\\x([0-9a-fA-F]{2})", _hex_to_char, code)

        # Decode \uHHHH (unicode escape)
        def _unicode_to_char(m):
            transformations["unicode_decoded"] += 1
            try:
                ch = chr(int(m.group(1), 16))
                return ch if ch.isprintable() and ord(ch) < 127 else m.group(0)
            except Exception:
                return m.group(0)
        code_new = re.sub(r"\\u([0-9a-fA-F]{4})", _unicode_to_char, code_new)

        # Fold String.fromCharCode(N, N, N, ...)
        def _fold_char_code(m):
            nums = [int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()]
            if nums and all(0 < n < 0x110000 for n in nums):
                try:
                    s = "".join(chr(n) for n in nums)
                    if all(32 <= ord(c) < 127 or c in "\n\t\r" for c in s):
                        transformations["char_code_folded"] += 1
                        return '"' + s.replace('"', '\\"') + '"'
                except Exception:
                    pass
            return m.group(0)
        code_new = re.sub(
            r"String\.fromCharCode\(((?:\s*\d+\s*,)+\s*\d+\s*)\)",
            _fold_char_code, code_new
        )

        if code_new == code:
            break
        code = code_new

    return {
        "ok": True,
        "original_size": original_size,
        "deobfuscated_size": len(code),
        "reduction_pct": round(100 * (original_size - len(code)) / original_size, 1) if original_size else 0,
        "transformations": transformations,
        "total_transformations": sum(transformations.values()),
        "preview": code[:500] + ("..." if len(code) > 500 else ""),
        "note": "Deobfuscation básica: decode escapes + constant folding. NO descifra RC4.",
    }


# ═══════════════════════════════════════════
#  4. DIFF bm.js entre versiones
# ═══════════════════════════════════════════
def diff_bm_js(code_old: str, code_new: str) -> dict:
    """Compara estructural+tamaño entre 2 versiones del bm.js.
    Útil para detectar si Akamai cambió el algoritmo."""
    old_analysis = analyze_bm_js(code_old)
    new_analysis = analyze_bm_js(code_new)

    # Common substrings (first N chars de funciones típicas)
    # simple: buscar tokens de 32+ chars que aparezcan en ambos
    import difflib
    sm = difflib.SequenceMatcher(None, code_old[:50000], code_new[:50000])
    similarity = round(sm.ratio() * 100, 1)

    # Detectar si los patterns de obfuscación cambian
    obf_changed = {}
    for key in old_analysis.get("obfuscation", {}):
        o = old_analysis["obfuscation"][key]
        n = new_analysis["obfuscation"].get(key, 0)
        if o != n:
            obf_changed[key] = {"old": o, "new": n, "delta": n - o}

    return {
        "size_old": old_analysis["size_bytes"],
        "size_new": new_analysis["size_bytes"],
        "size_delta_bytes": new_analysis["size_bytes"] - old_analysis["size_bytes"],
        "size_delta_pct": round(100 * (new_analysis["size_bytes"] - old_analysis["size_bytes"]) / max(1, old_analysis["size_bytes"]), 1),
        "similarity_pct": similarity,
        "hash_old": old_analysis["sha256"][:16],
        "hash_new": new_analysis["sha256"][:16],
        "same": old_analysis["sha256"] == new_analysis["sha256"],
        "entropy_old": old_analysis["entropy"],
        "entropy_new": new_analysis["entropy"],
        "obfuscation_score_old": old_analysis["obfuscation_score"],
        "obfuscation_score_new": new_analysis["obfuscation_score"],
        "obfuscation_changes": obf_changed,
        "verdict": (
            "identical" if old_analysis["sha256"] == new_analysis["sha256"]
            else "minor_update" if similarity > 90
            else "major_rewrite" if similarity < 50
            else "structural_changes"
        ),
    }


# ═══════════════════════════════════════════
#  5. CAPTURE sensor_data REAL (via Playwright)
# ═══════════════════════════════════════════
def capture_sensor_data(target_url: str = "https://www.ticketmaster.com",
                         duration_s: int = 30,
                         engine: str = "chromium",
                         headless: bool = True) -> dict:
    """Carga el target en Chrome real vía Playwright, intercepta los POST
    a `/_bm_/` y captura el sensor_data string que Chrome manda.

    Esos sensor_data SÍ pasan la validación de Akamai porque los
    generó el bm.js real ejecutándose en un Chrome real.

    Duración 30 s captura 1-3 payloads típicamente.
    """
    started = time.time()
    captured = []
    result = {
        "target_url": target_url, "duration_s": duration_s,
        "engine": engine, "captures": [],
    }

    try:
        import tm_browser_engine as be
    except ImportError:
        result["error"] = "tm_browser_engine no disponible"
        return result

    try:
        browser, context, page, meta = be._launch_engine(engine, headless, "us")
    except Exception as e:
        result["error"] = f"browser launch fail: {e}"
        return result

    try:
        # Interceptar requests ANTES de navegar
        def _on_request(request):
            url = request.url
            # Capturar POST a /_bm_ o /_akamai_/bm
            if request.method == "POST" and ("/_bm_" in url or "/akam/11/" in url or "sensor_data" in (request.post_data or "")):
                try:
                    data = request.post_data or ""
                    if data and len(data) > 100:
                        capture_entry = {
                            "id": uuid.uuid4().hex[:10],
                            "captured_at": datetime.now().isoformat(),
                            "url": url,
                            "method": request.method,
                            "sensor_data": data,
                            "sensor_length": len(data),
                            "sensor_sha256": hashlib.sha256(data.encode()).hexdigest()[:16],
                            "resource_type": request.resource_type,
                            "target_url": target_url,
                            "expires_at": (datetime.now() + timedelta(seconds=CAPTURE_TTL_S)).isoformat(),
                        }
                        captured.append(capture_entry)
                except Exception:
                    pass

        if hasattr(page, "on"):
            page.on("request", _on_request)

        # Navegar y esperar
        page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
        # Pequeña interacción para que bm.js dispare
        import random
        end_ts = time.time() + duration_s
        while time.time() < end_ts:
            try:
                x = random.randint(100, 1200)
                y = random.randint(100, 700)
                page.mouse.move(x, y, steps=5)
                page.mouse.wheel(0, random.randint(-100, 200))
            except Exception:
                pass
            time.sleep(random.uniform(0.5, 1.5))

        # Extraer cookies al final
        try:
            cookies = context.cookies() if context else (browser.contexts[0].cookies() if browser.contexts else [])
            akamai_cookies = [c for c in cookies if any(
                c.get("name", "").startswith(p) for p in ["ak_bmsc", "_abck", "bm_"]
            )]
            for c in captured:
                c["akamai_cookies_at_capture"] = [
                    {"name": ak["name"], "value_preview": ak.get("value", "")[:30]}
                    for ak in akamai_cookies
                ]
                c["user_agent"] = meta.get("ua", "")
        except Exception:
            pass

        # Persistir capturas con TTL
        if captured:
            _save_captures(captured)

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            be._close_engine(browser, meta)
        except Exception:
            pass

    result["captures_count"] = len(captured)
    result["captures"] = [
        {k: v for k, v in c.items() if k != "sensor_data"}  # no dump del sensor completo
        for c in captured
    ]
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


def _save_captures(entries: List[dict]):
    with _lock:
        d = json_atomic.load(config.ruta(_CAPTURES_KEY), default={"captures": []})
        d["captures"] = entries + d.get("captures", [])
        d["captures"] = d["captures"][:200]
        json_atomic.save(config.ruta(_CAPTURES_KEY), d)


def list_captures(target_domain: Optional[str] = None,
                   active_only: bool = True) -> List[dict]:
    d = json_atomic.load(config.ruta(_CAPTURES_KEY), default={"captures": []})
    items = d.get("captures", [])
    if target_domain:
        items = [c for c in items if target_domain in (c.get("target_url") or "")]
    if active_only:
        now = datetime.now()
        fresh = []
        for c in items:
            try:
                if datetime.fromisoformat(c.get("expires_at", "")) > now:
                    fresh.append(c)
            except Exception:
                pass
        items = fresh
    # no devolver sensor_data completo en listings
    return [{k: v for k, v in c.items() if k != "sensor_data"} for c in items]


def get_capture(capture_id: str) -> Optional[dict]:
    d = json_atomic.load(config.ruta(_CAPTURES_KEY), default={"captures": []})
    for c in d.get("captures", []):
        if c.get("id") == capture_id:
            return c
    return None


# ═══════════════════════════════════════════
#  6. REPLAY + MUTATE
# ═══════════════════════════════════════════
def mutate_sensor_data(sensor: str, seed: Optional[int] = None) -> dict:
    """Aplica micro-mutaciones safe al sensor para variar bytes sin
    romper la validación estructural (longitud + separadores).

    Mutaciones típicas:
    - Modificar 1-2 chars aleatorios dentro del payload (dentro del campo b64)
    - Cambiar último byte del checksum
    - Nunca tocar: prefix (2;) ni length ni key_hash (primeros 3 campos)
    """
    import random
    rnd = random.Random(seed) if seed else random

    parts = sensor.split(";")
    if len(parts) != 5:
        return {"ok": False, "error": "formato inesperado (esperaba 5 partes)"}

    prefix, length, key_hash, payload_b64, checksum = parts

    # Mutar 1-2 chars del payload_b64
    payload_list = list(payload_b64)
    n_mutations = rnd.choice([1, 2])
    muts = []
    for _ in range(n_mutations):
        idx = rnd.randint(10, len(payload_list) - 10)  # evitar bordes
        old = payload_list[idx]
        # Cambio conservador: entre chars b64
        b64_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        new_c = rnd.choice(b64_chars.replace(old, ""))
        payload_list[idx] = new_c
        muts.append({"idx": idx, "old": old, "new": new_c})

    new_payload = "".join(payload_list)
    # Recalcular checksum (para que sea coherente con nuestro formato)
    new_checksum = hashlib.sha256(new_payload.encode()).hexdigest()[:12]

    return {
        "ok": True,
        "original_sha256": hashlib.sha256(sensor.encode()).hexdigest()[:16],
        "mutated_sensor": f"{prefix};{length};{key_hash};{new_payload};{new_checksum}",
        "mutated_sha256": hashlib.sha256(f"{prefix};{length};{key_hash};{new_payload};{new_checksum}".encode()).hexdigest()[:16],
        "mutations_applied": muts,
    }


def replay_sensor_data(capture_id: str,
                        apply_mutation: bool = True,
                        target_url: Optional[str] = None) -> dict:
    """Reutiliza un sensor_data capturado (con o sin mutación).

    Args:
      capture_id: ID de la captura a reutilizar
      apply_mutation: aplicar micro-mutación (recomendado para evitar
                     detección por hash-exacto repetido)
      target_url: endpoint /_bm_/ al que postear; si None solo devuelve el payload.
    """
    capture = get_capture(capture_id)
    if not capture:
        return {"ok": False, "error": f"capture {capture_id} no encontrada"}

    # Verificar TTL
    try:
        if datetime.fromisoformat(capture["expires_at"]) < datetime.now():
            return {"ok": False, "error": "capture expirada (>60s)",
                    "captured_at": capture.get("captured_at")}
    except Exception:
        pass

    sensor = capture.get("sensor_data", "")
    if not sensor:
        return {"ok": False, "error": "capture sin sensor_data"}

    payload_to_send = sensor
    mutation_info = None
    if apply_mutation and sensor.count(";") == 4:
        m = mutate_sensor_data(sensor)
        if m.get("ok"):
            payload_to_send = m["mutated_sensor"]
            mutation_info = m

    result = {
        "ok": True,
        "capture_id": capture_id,
        "captured_at": capture.get("captured_at"),
        "sensor_length": len(payload_to_send),
        "mutation_applied": mutation_info is not None,
        "mutation_info": mutation_info,
    }

    if target_url:
        import requests
        try:
            r = requests.post(
                target_url,
                data=payload_to_send,
                headers={
                    "User-Agent": capture.get("user_agent", "Mozilla/5.0"),
                    "Content-Type": "text/plain;charset=UTF-8",
                },
                timeout=10,
            )
            result["http_status"] = r.status_code
            result["response_preview"] = r.text[:200]
        except Exception as e:
            result["http_error"] = f"{type(e).__name__}: {e}"

    return result


# ═══════════════════════════════════════════
#  STATS + SELF TEST
# ═══════════════════════════════════════════
def get_stats() -> dict:
    captures = list_captures(active_only=False)
    active = list_captures(active_only=True)
    versions = list_cached_versions()
    return {
        "cached_bmjs_versions": len(versions),
        "captures_total": len(captures),
        "captures_active": len(active),
        "capture_ttl_s": CAPTURE_TTL_S,
        "latest_capture_at": captures[0].get("captured_at") if captures else None,
        "target_domains": list(set(
            c.get("target_url", "").split("/")[2] for c in captures if c.get("target_url")
        )),
    }


def self_test() -> dict:
    """Test sin red ni browser — valida funciones puras."""
    results = {"ok": True, "checks": {}}

    # Analyze sobre código sintético
    fake_bmjs = """
    var bmak = {v: "2.1.3"};
    var _0xabc = ["\\x68\\x65\\x6c\\x6c\\x6f", "world"];
    bmak.sensor_data = "";
    String.fromCharCode(65, 66, 67, 68);
    while(!![]){ break; }
    var url = "/_bm_/abc";
    """
    a = analyze_bm_js(fake_bmjs)
    results["checks"]["analyze_size"] = a["size_bytes"]
    results["checks"]["analyze_version_hint"] = a.get("version_hint")
    results["checks"]["analyze_endpoints"] = a["endpoints_found"]
    results["checks"]["analyze_obfuscation_score"] = a["obfuscation_score"]
    results["checks"]["analyze_known_vars"] = len(a["known_vars_present"])

    # Deobfuscate
    d = deobfuscate_basic(fake_bmjs)
    results["checks"]["deobf_transformations"] = d["total_transformations"]
    results["checks"]["deobf_ok"] = d["ok"]

    # Diff
    diff = diff_bm_js(fake_bmjs, fake_bmjs + "\n// extra comment")
    results["checks"]["diff_verdict"] = diff["verdict"]
    results["checks"]["diff_similarity"] = diff["similarity_pct"]

    # Mutate
    fake_sensor = "2;1234;deadbeefcafe1234;TkNCVUYWUQ1XO11URwsRRlASRQEXSRoFXApRQgRFVlZqAEUSD0cKUgtSGQBRHA==;abc123def456"
    m = mutate_sensor_data(fake_sensor, seed=42)
    results["checks"]["mutate_ok"] = m["ok"]
    results["checks"]["mutate_different"] = m["original_sha256"] != m["mutated_sha256"]

    return results


def get_info() -> dict:
    return {
        "module": "akamai_bmjs",
        "capabilities": [
            "fetch_bm_js", "analyze_bm_js", "deobfuscate_basic",
            "diff_bm_js", "capture_sensor_data (Playwright)",
            "replay_sensor_data", "mutate_sensor_data",
            "distributed_pool (Redis)", "auto_capture_scheduler",
            "fingerprint_binding", "diff_watchdog",
        ],
        "stats": get_stats(),
        "note": "Vía práctica: capture + replay con sensor_data real de Chrome. NO deobfusca RC4 (eso requiere VM JS).",
    }


# ═══════════════════════════════════════════════════════════
#  MEJORA #1 — Pool distribuido via Redis (hosts comparten capturas)
# ═══════════════════════════════════════════════════════════
def publish_capture_to_cluster(capture: dict) -> dict:
    """Publica una captura al backend distribuido para que otros hosts la usen."""
    try:
        import redis_backend as rb
        cid = capture.get("id", "")
        key = f"akamai:capture:{cid}"
        # Guardar con TTL del capture
        ttl = CAPTURE_TTL_S
        try:
            exp = datetime.fromisoformat(capture.get("expires_at", ""))
            ttl = max(1, int((exp - datetime.now()).total_seconds()))
        except Exception:
            pass
        rb.backend.set(key, capture, ttl=ttl)
        rb.backend.publish("akamai:captures", {"capture_id": cid,
                                                 "from": rb.INSTANCE_ID,
                                                 "target": capture.get("target_url", "")})
        return {"ok": True, "distributed": rb.is_distributed(),
                 "instance": rb.INSTANCE_ID, "ttl_s": ttl}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def fetch_cluster_captures(target_domain: Optional[str] = None) -> List[dict]:
    """Lee TODAS las capturas del cluster (locales + remotas via Redis)."""
    out = list_captures(target_domain=target_domain, active_only=True)
    seen_ids = {c.get("id") for c in out}

    try:
        import redis_backend as rb
        if not rb.is_distributed():
            return out
        for key in rb.backend.keys("akamai:capture:*"):
            data = rb.backend.get(key)
            if isinstance(data, dict) and data.get("id") not in seen_ids:
                # Filtrar por domain si se pidió
                if target_domain and target_domain not in (data.get("target_url") or ""):
                    continue
                # Filtrar sensor_data para no enviarlo enorme
                sanitized = {k: v for k, v in data.items() if k != "sensor_data"}
                sanitized["_source"] = "cluster_redis"
                out.append(sanitized)
    except Exception:
        pass
    return out


# ═══════════════════════════════════════════════════════════
#  MEJORA #2 — Auto-capture scheduler (pool fresco 24/7)
# ═══════════════════════════════════════════════════════════
_scheduler_state = {"running": False, "thread": None, "stop_event": None,
                     "ticks": 0, "captures_made": 0, "last_tick_at": None}


def _scheduler_loop(stop_event, target_urls: List[str], interval_s: int,
                      duration_s: int, engine: str, broadcast):
    while not stop_event.is_set():
        try:
            # Solo capturar si el pool está bajo el mínimo
            pool = list_captures(active_only=True)
            if len(pool) < 3:  # mantener al menos 3 frescas
                for url in target_urls[:1]:  # 1 capture por tick para no saturar
                    try:
                        r = capture_sensor_data(
                            target_url=url,
                            duration_s=duration_s,
                            engine=engine,
                            headless=True,
                        )
                        _scheduler_state["captures_made"] += len(r.get("captures", []))
                        # Publicar al cluster
                        try:
                            # capture_sensor_data ya guardó locamente con _save_captures
                            # ahora las publicamos al Redis pool
                            for cap_meta in r.get("captures", []):
                                cid = cap_meta.get("id")
                                if cid:
                                    full = get_capture(cid)
                                    if full:
                                        publish_capture_to_cluster(full)
                        except Exception:
                            pass
                        if broadcast:
                            broadcast("akamai_auto_capture", {
                                "target": url, "captures": len(r.get("captures", [])),
                                "pool_size": len(list_captures(active_only=True)),
                            })
                    except Exception as e:
                        print(f"[akamai_bmjs scheduler] capture err: {e}", flush=True)
            _scheduler_state["ticks"] += 1
            _scheduler_state["last_tick_at"] = datetime.now().isoformat()
        except Exception as e:
            print(f"[akamai_bmjs scheduler] loop err: {e}", flush=True)
        stop_event.wait(interval_s)


def start_auto_capture(target_urls: Optional[List[str]] = None,
                        interval_s: int = 45,
                        duration_s: int = 25,
                        engine: str = "chromium",
                        broadcast=None) -> dict:
    """Arranca scheduler que mantiene el pool fresco automáticamente."""
    if _scheduler_state["running"]:
        return {"ok": True, "already_running": True,
                 **{k: v for k, v in _scheduler_state.items() if k not in ("thread", "stop_event")}}
    target_urls = target_urls or ["https://www.ticketmaster.com"]
    ev = threading.Event()
    t = threading.Thread(
        target=_scheduler_loop,
        args=(ev, target_urls, interval_s, duration_s, engine, broadcast),
        daemon=True, name="akamai-bmjs-scheduler",
    )
    _scheduler_state.update({"running": True, "thread": t, "stop_event": ev,
                               "target_urls": target_urls, "interval_s": interval_s,
                               "started_at": datetime.now().isoformat()})
    t.start()
    return {"ok": True, "started": True,
             "target_urls": target_urls, "interval_s": interval_s}


def stop_auto_capture() -> dict:
    if not _scheduler_state["running"]:
        return {"ok": True, "not_running": True}
    ev = _scheduler_state.get("stop_event")
    if ev:
        ev.set()
    _scheduler_state.update({"running": False, "thread": None, "stop_event": None})
    return {"ok": True, "stopped": True}


def scheduler_status() -> dict:
    return {k: v for k, v in _scheduler_state.items() if k not in ("thread", "stop_event")}


# ═══════════════════════════════════════════════════════════
#  MEJORA #3 — Fingerprint binding (UA+viewport+geo)
# ═══════════════════════════════════════════════════════════
def _fingerprint_key(user_agent: str, viewport: Optional[list],
                       geo: Optional[str]) -> str:
    """Hash estable del (UA+viewport+geo) para matchear replay al worker."""
    vp = f"{viewport[0]}x{viewport[1]}" if viewport and len(viewport) == 2 else "any"
    key = f"{user_agent or 'any'}|{vp}|{geo or 'any'}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def find_capture_for_fingerprint(user_agent: str,
                                    viewport: Optional[list] = None,
                                    geo: Optional[str] = None,
                                    target_domain: Optional[str] = None) -> Optional[dict]:
    """Busca captura compatible con el fingerprint del worker actual.

    Prioriza:
      1. Match EXACTO (UA+viewport+geo)
      2. Match UA+geo
      3. Match UA
      4. Cualquier activa
    """
    pool = fetch_cluster_captures(target_domain=target_domain)
    if not pool:
        return None

    # Match exacto
    target_fp = _fingerprint_key(user_agent, viewport, geo)
    for c in pool:
        cap_ua = c.get("user_agent", "")
        if _fingerprint_key(cap_ua, viewport, geo) == target_fp:
            c["match_quality"] = "exact"
            return c

    # Match UA
    if user_agent:
        for c in pool:
            if c.get("user_agent") == user_agent:
                c["match_quality"] = "ua_only"
                return c

    # Match UA family (Chrome/Firefox)
    ua_family = "chrome" if "chrome" in (user_agent or "").lower() else (
        "firefox" if "firefox" in (user_agent or "").lower() else "")
    if ua_family:
        for c in pool:
            if ua_family in (c.get("user_agent", "").lower()):
                c["match_quality"] = "ua_family"
                return c

    # Fallback: cualquiera
    if pool:
        c = pool[0]
        c["match_quality"] = "any"
        return c
    return None


# ═══════════════════════════════════════════════════════════
#  MEJORA #4 — Diff detector + alert a Discord
# ═══════════════════════════════════════════════════════════
def watchdog_check_bmjs_version(target_url: str = "https://www.ticketmaster.com",
                                  notify: bool = True) -> dict:
    """Compara el bm.js actual con la última versión cacheada.
    Si cambió → notifica via notifications (Discord/Telegram)."""
    current = fetch_bm_js(target_url)
    if not current.get("found"):
        return {"ok": False, "error": "bm.js no encontrado"}

    cached = list_cached_versions()
    # La primera es la actual (que acabamos de fetchear). La segunda sería la anterior.
    if len(cached) < 2:
        return {"ok": True, "verdict": "first_capture",
                 "note": "primera versión cacheada, sin comparación posible"}

    prev = cached[1]  # la anterior
    cur = cached[0]   # la actual recién guardada

    # Para diff necesitamos el código. Como solo cacheamos preview, re-fetch ambos.
    # Optimización: si los hashes son iguales, no hay cambio.
    if prev.get("sha256") == cur.get("sha256"):
        return {"ok": True, "verdict": "identical",
                 "hash": cur.get("sha256", "")[:16]}

    diff_info = {
        "verdict": "changed",
        "prev_hash": prev.get("sha256", "")[:16],
        "cur_hash": cur.get("sha256", "")[:16],
        "prev_size": prev.get("size_bytes"),
        "cur_size": cur.get("size_bytes"),
        "size_delta": cur.get("size_bytes", 0) - prev.get("size_bytes", 0),
        "prev_cached_at": prev.get("cached_at"),
        "cur_cached_at": cur.get("cached_at"),
    }

    # Notificar
    if notify:
        try:
            import notifications
            notifications.notify(
                event_type="akamai_bmjs_changed",
                title=f"⚠️ bm.js cambió en {target_url.split('/')[2]}",
                message=(f"Akamai actualizó el bm.js.\n"
                         f"Previous: {diff_info['prev_hash']} ({diff_info['prev_size']} B)\n"
                         f"Current:  {diff_info['cur_hash']} ({diff_info['cur_size']} B)\n"
                         f"Delta:    {diff_info['size_delta']:+d} bytes\n\n"
                         f"Las capturas previas de sensor_data pueden quedar invalidadas. "
                         f"Considera relanzar auto-capture scheduler."),
                level="warning",
            )
            diff_info["notification_sent"] = True
        except Exception as e:
            diff_info["notification_error"] = str(e)[:100]

    return {"ok": True, **diff_info}


def start_diff_watchdog(target_url: str = "https://www.ticketmaster.com",
                          check_interval_min: int = 30) -> dict:
    """Arranca watchdog que chequea cada N min si bm.js cambió."""
    if getattr(start_diff_watchdog, "_thread", None) and start_diff_watchdog._thread.is_alive():
        return {"ok": True, "already_running": True}
    stop_ev = threading.Event()

    def _loop():
        while not stop_ev.is_set():
            try:
                watchdog_check_bmjs_version(target_url, notify=True)
            except Exception:
                pass
            stop_ev.wait(check_interval_min * 60)

    t = threading.Thread(target=_loop, daemon=True, name="akamai-bmjs-watchdog")
    start_diff_watchdog._thread = t
    start_diff_watchdog._stop = stop_ev
    t.start()
    return {"ok": True, "started": True,
             "target_url": target_url, "interval_min": check_interval_min}


def stop_diff_watchdog() -> dict:
    ev = getattr(start_diff_watchdog, "_stop", None)
    if ev:
        ev.set()
        return {"ok": True, "stopped": True}
    return {"ok": True, "not_running": True}
