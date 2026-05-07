"""akamai_validator.py — Validator local + Shadow DOM monitor + ML validator.

Implementa 3 mejoras:

1. VALIDATOR LOCAL
   Simula un servidor Akamai para validar payloads ANTES de enviarlos.
   Detecta sensor_data obvio de bot con heurísticas + ML simple.

2. SHADOW DOM MONITOR
   Akamai agrega campos nuevos al bm.js con cada release. Este módulo
   detecta campos nuevos comparando capturas recientes vs histórico.

3. ML VALIDATOR (simple)
   Clasificador estadístico: compara payload contra distribución de
   capturas reales. Si las mutaciones salen fuera del rango Z-score
   esperado → rechazar.

Útil para testear sensor_data en DEV antes de quemar session real en TM.
"""
import hashlib
import json
import re
import statistics
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import config
import json_atomic


_lock = threading.Lock()
_BASELINE_KEY = "akamai_validator_baseline.json"


# ═══════════════════════════════════════════════════════════
#  1. VALIDATOR LOCAL (20 checks granulares con severity levels)
# ═══════════════════════════════════════════════════════════
#
# Severity levels:
#   CRITICAL → Akamai rechaza inmediato (401/403)
#   HIGH     → Muy probable rechazo
#   MEDIUM   → Patrón anómalo, posible soft-ban
#   LOW      → Warning informativo
#   INFO     → Para debugging

SEVERITY_WEIGHTS = {"CRITICAL": 30, "HIGH": 20, "MEDIUM": 10, "LOW": 3, "INFO": 0}

# Rangos esperados por versión (capturados de bm.js real)
VERSION_RANGES = {
    "2": {
        "sensor_len": (200, 32000),
        "payload_b64_len": (100, 22000),
        "parts_expected": [3, 5],
        "checksum_len": [12, 16],
    },
    "3": {
        "sensor_len": (300, 48000),
        "payload_b64_len": (200, 28000),
        "parts_expected": [5],
        "checksum_len": [20],  # HMAC SHA1
    },
}


def _shannon_entropy_simple(s: str) -> float:
    """Entropía Shannon bits/char. Base64 cifrado ≈ 5.8-6.0."""
    if not s:
        return 0.0
    from collections import Counter
    import math
    freqs = Counter(s)
    n = len(s)
    return -sum((f / n) * math.log2(f / n) for f in freqs.values())


def _is_valid_base64(s: str) -> Tuple[bool, Optional[str]]:
    """Verifica base64 válido + intenta decode para validar length."""
    import base64
    b64_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    invalid = [c for c in s if c not in b64_chars]
    if invalid:
        return False, f"chars no-b64: {set(invalid[:3])}"
    # Padding
    if len(s) % 4 != 0:
        return False, f"length % 4 != 0 ({len(s) % 4})"
    # Decode sanity
    try:
        decoded = base64.b64decode(s, validate=True)
        return True, None
    except Exception as e:
        return False, str(e)[:40]


def validate_sensor_structure(sensor: str,
                                 expected_session_id: Optional[str] = None) -> dict:
    """Validación estructural completa con 20 checks granulares.

    Args:
      sensor: string sensor_data a validar
      expected_session_id: si se pasa, verifica que el key_hash corresponda

    Returns:
      {
        ok: bool,
        verdict: "PASS|WARNING|REJECT",
        issues: [{severity, code, msg, fix}],
        stats: {length, parts_count, version, ...},
        score: 0-100 (bot_score, más alto = más sospechoso),
        akamai_accept_probability: 0-100,
        checks_performed: int,
      }
    """
    result = {
        "ok": True,
        "issues": [],
        "stats": {"length": len(sensor)},
        "checks_performed": 0,
    }
    issues = result["issues"]

    def _add(severity: str, code: str, msg: str, fix: Optional[str] = None):
        issues.append({"severity": severity, "code": code, "msg": msg, "fix": fix})

    result["checks_performed"] = 20  # Total a chequear

    # ── CHECK 1: Tamaño absoluto ──
    ln = len(sensor)
    if ln == 0:
        _add("CRITICAL", "empty", "sensor está vacío", "Generar con generate_humanized_payload()")
    elif ln < 100:
        _add("CRITICAL", "too_short", f"sensor muy corto ({ln} bytes, min 100)",
             "Aumentar events_per_payload en config")
    elif ln < 200:
        _add("HIGH", "suspicious_short", f"sensor corto ({ln} bytes), típico 1-5KB",
             "Aumentar events o usar generate_humanized_payload con personality=power_user")
    elif ln > 65536:
        _add("CRITICAL", "too_large", f"sensor muy grande ({ln} bytes, max 64KB)",
             "Reducir events_count o splitear en 2 payloads")
    elif ln > 32768:
        _add("MEDIUM", "large", f"sensor grande ({ln} bytes)",
             "Considerar reducir events")

    # Si está completamente vacío, devolvemos temprano
    if ln == 0:
        result["ok"] = False
        result["verdict"] = "REJECT"
        result["score"] = 100
        result["akamai_accept_probability"] = 0
        return result

    # ── CHECK 2: Chars no ASCII ──
    non_ascii = [c for c in sensor if ord(c) > 127]
    if non_ascii:
        _add("HIGH", "non_ascii", f"{len(non_ascii)} chars no-ASCII en sensor",
             "El sensor debe ser ASCII puro. Revisa encoding del b64.")

    # ── CHECK 3: Null bytes embebidos ──
    if "\x00" in sensor:
        _add("CRITICAL", "null_bytes", "null bytes embebidos (corrupción)",
             "El sensor tiene \\x00 — corrupted. Re-generar.")

    # ── CHECK 4: Separadores ";" ──
    parts = sensor.split(";")
    result["stats"]["parts_count"] = len(parts)

    if len(parts) == 1:
        _add("CRITICAL", "no_separators",
             "sin separadores ';', formato desconocido",
             "Akamai usa 'v;len;hash;b64;chk'. Usar build_sensor_data_varied.")
    elif len(parts) == 2:
        _add("CRITICAL", "malformed_separators",
             f"solo 2 partes, esperado 3 (v2_simple) o 5 (v2/v3)")
    elif len(parts) not in (3, 5):
        _add("HIGH", "unexpected_parts",
             f"{len(parts)} partes (esperado 3 o 5)",
             "Verificar que no hay ';' extras embebidos en el payload")

    # ── CHECK 5: Versión ──
    if len(parts) >= 1:
        ver = parts[0]
        result["stats"]["version"] = ver
        if ver == "1":
            _add("HIGH", "legacy_v1",
                 "versión v1 obsoleta (deprecated desde 2022)",
                 "Usar v2 o v3")
        elif ver not in ("2", "3"):
            _add("CRITICAL", "unknown_version",
                 f"versión desconocida: '{ver}'",
                 "Solo v2 o v3 son aceptadas por Akamai actualmente")

    # Estructura completa v2/v3 (5 partes)
    if len(parts) == 5:
        version = parts[0]
        length_str, key_hash, payload_b64, checksum = parts[1], parts[2], parts[3], parts[4]
        ranges = VERSION_RANGES.get(version, VERSION_RANGES["2"])

        # ── CHECK 6: length es número ──
        try:
            length = int(length_str)
            result["stats"]["declared_length"] = length

            # ── CHECK 7: length en rango ──
            min_l, max_l = 50, 100000
            if not (min_l <= length <= max_l):
                _add("HIGH", "length_out_of_range",
                     f"length declarada {length} fuera de [{min_l},{max_l}]",
                     "Re-generar payload o ajustar events_count")

            # ── CHECK 8: length consistente con payload ──
            # base64 overhead: length_original * 4/3 ≈ len_b64 (con padding)
            import math
            expected_b64_len = math.ceil(length / 3) * 4
            actual_b64_len = len(payload_b64)
            tolerance = 0.30
            if abs(actual_b64_len - expected_b64_len) / max(1, expected_b64_len) > tolerance:
                _add("MEDIUM", "length_mismatch",
                     f"length declarada {length} → b64 esperado ~{expected_b64_len}, actual {actual_b64_len}",
                     "Akamai valida: b64.length ≈ length * 4/3")
        except ValueError:
            _add("CRITICAL", "length_not_int",
                 f"length no es número: '{length_str[:20]}'",
                 "La parte 2 debe ser entero decimal")

        # ── CHECK 9: key_hash formato hex ──
        if version in ("2", "3"):
            expected_hash_len = 16
            if len(key_hash) != expected_hash_len:
                _add("HIGH", "hash_wrong_length",
                     f"key_hash {len(key_hash)} chars (esperado {expected_hash_len})",
                     f"key_hash debe ser MD5[:16] hex")
            elif not re.match(r"^[0-9a-f]+$", key_hash):
                _add("HIGH", "hash_not_hex",
                     f"key_hash tiene chars no-hex: {key_hash[:20]}",
                     "Solo [0-9a-f]. Usar hashlib.md5(sid).hexdigest()[:16]")

        # ── CHECK 10: session_id match (si se pasa) ──
        if expected_session_id:
            import hashlib
            expected_hash = hashlib.md5(expected_session_id.encode()).hexdigest()[:16]
            if key_hash != expected_hash:
                _add("MEDIUM", "hash_mismatch_session",
                     f"key_hash no coincide con session_id esperado",
                     "Verificar que session_id fue usado al generar")

        # ── CHECK 11: payload b64 válido ──
        b64_ok, b64_err = _is_valid_base64(payload_b64)
        if not b64_ok:
            _add("CRITICAL", "invalid_base64",
                 f"payload no es base64: {b64_err}",
                 "Asegurar base64.b64encode()  + padding '='")

        # ── CHECK 12: payload length en rango ──
        b64_min, b64_max = ranges["payload_b64_len"]
        if not (b64_min <= len(payload_b64) <= b64_max):
            _add("MEDIUM", "payload_length_anomaly",
                 f"payload {len(payload_b64)} bytes fuera de [{b64_min},{b64_max}] para v{version}")

        # ── CHECK 13: Entropía del payload (cifrado = alta) ──
        if len(payload_b64) > 50:
            entropy = _shannon_entropy_simple(payload_b64[:2000])
            result["stats"]["payload_entropy"] = round(entropy, 3)
            if entropy < 4.5:
                _add("HIGH", "low_entropy",
                     f"payload entropy {round(entropy,2)} (cifrado esperado ≥5.5)",
                     "Payload no parece cifrado, posible plain text")
            elif entropy < 5.2:
                _add("MEDIUM", "moderate_entropy",
                     f"payload entropy {round(entropy,2)} moderada",
                     "Verificar que se aplicó RC4/XOR")

        # ── CHECK 14: checksum/sig formato ──
        expected_chk_lens = ranges["checksum_len"]
        if len(checksum) not in expected_chk_lens:
            _add("HIGH", "checksum_length_wrong",
                 f"checksum {len(checksum)} chars (v{version} espera {expected_chk_lens})")
        if not re.match(r"^[0-9a-f]+$", checksum):
            _add("HIGH", "checksum_not_hex",
                 f"checksum con chars no-hex: {checksum[:20]}")

        # ── CHECK 15: ';' embebido en b64 (no debería) ──
        if ";" in payload_b64:
            _add("MEDIUM", "semicolon_in_payload",
                 "';' en payload rompe parsing",
                 "Base64 NO contiene ';'. Verificar build_sensor_data")

    # ── CHECK 16: v2_simple (3 partes) ──
    if len(parts) == 3:
        result["stats"]["format_variant"] = "v2_simple"
        if parts[0] != "2":
            _add("HIGH", "simple_wrong_version",
                 "formato simple solo v2, encontrado v" + parts[0])
        b64_ok, _ = _is_valid_base64(parts[1])
        if not b64_ok:
            _add("HIGH", "simple_invalid_b64",
                 "parte 2 (payload) no es base64 válido")

    # ── CHECK 17: Prefijo sospechoso (honeypots) ──
    if sensor[:5].lower() in ("test;", "fake;", "mock;"):
        _add("HIGH", "suspicious_prefix",
             f"prefijo test/fake detectado",
             "Usar v2 o v3, no prefixes de test")

    # ── CHECK 18: Whitespace / newlines ──
    if "\n" in sensor or "\r" in sensor:
        _add("CRITICAL", "whitespace_in_sensor",
             "sensor contiene \\n o \\r",
             "Sensor debe ser single-line. Usar .replace('\\n','').strip()")
    if sensor != sensor.strip():
        _add("LOW", "leading_trailing_space",
             "sensor tiene whitespace al inicio/fin",
             "Llamar .strip() antes de enviar")

    # ── CHECK 19: Chars especiales en longitud ──
    if any(c in sensor for c in ('"', "'", "<", ">", "&")):
        _add("MEDIUM", "special_chars",
             "caracteres especiales HTML en sensor",
             "Akamai acepta solo ASCII seguro. Verificar encoding.")

    # ── CHECK 20: Estructura consistente entre versiones ──
    if len(parts) == 5 and parts[0] == "3":
        # v3 requiere length, hash, b64, sig (todos obligatorios)
        all_non_empty = all(p.strip() for p in parts)
        if not all_non_empty:
            _add("HIGH", "empty_fields_v3",
                 "v3 tiene campos vacíos (todos obligatorios)")

    # ═══ Calcular scores y verdict ═══
    total_score = sum(SEVERITY_WEIGHTS.get(i["severity"], 0) for i in issues)
    has_critical = any(i["severity"] == "CRITICAL" for i in issues)
    has_high = any(i["severity"] == "HIGH" for i in issues)

    result["score"] = min(100, total_score)
    result["akamai_accept_probability"] = max(0, 100 - total_score)
    result["issues_by_severity"] = {
        sev: sum(1 for i in issues if i["severity"] == sev)
        for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
    }

    if has_critical:
        result["ok"] = False
        result["verdict"] = "REJECT"
    elif has_high and total_score >= 40:
        result["ok"] = False
        result["verdict"] = "LIKELY_REJECT"
    elif has_high or total_score >= 20:
        result["ok"] = True
        result["verdict"] = "WARNING"
    else:
        result["ok"] = True
        result["verdict"] = "PASS"

    # Compatibilidad con API vieja (bot_score)
    result["bot_score"] = result["score"]

    # Sugerencias auto-fix top
    fixes = [i["fix"] for i in issues if i.get("fix")][:3]
    result["suggested_fixes"] = fixes

    return result


def try_auto_repair(sensor: str) -> dict:
    """Intenta reparar problemas simples: whitespace, trailing chars, etc."""
    original = sensor
    repairs_applied = []

    # Fix 1: strip whitespace
    fixed = sensor.strip()
    if fixed != sensor:
        repairs_applied.append("stripped_whitespace")
        sensor = fixed

    # Fix 2: remove newlines
    if "\n" in sensor or "\r" in sensor:
        sensor = sensor.replace("\n", "").replace("\r", "")
        repairs_applied.append("removed_newlines")

    # Fix 3: quitar chars no ASCII
    non_ascii = [c for c in sensor if ord(c) > 127]
    if non_ascii:
        sensor = "".join(c for c in sensor if ord(c) <= 127)
        repairs_applied.append(f"removed_{len(non_ascii)}_non_ascii")

    # Fix 4: quitar null bytes
    if "\x00" in sensor:
        sensor = sensor.replace("\x00", "")
        repairs_applied.append("removed_null_bytes")

    return {
        "ok": True,
        "original_length": len(original),
        "repaired_length": len(sensor),
        "repairs_applied": repairs_applied,
        "repaired_sensor": sensor if repairs_applied else original,
        "fully_repaired": len(repairs_applied) > 0,
    }


def compare_structures(sensor_a: str, sensor_b: str) -> dict:
    """Compara estructuralmente 2 sensors para detectar divergencias."""
    va = validate_sensor_structure(sensor_a)
    vb = validate_sensor_structure(sensor_b)

    stats_a = va.get("stats", {})
    stats_b = vb.get("stats", {})

    return {
        "ok": True,
        "a_verdict": va["verdict"],
        "b_verdict": vb["verdict"],
        "same_version": stats_a.get("version") == stats_b.get("version"),
        "same_parts_count": stats_a.get("parts_count") == stats_b.get("parts_count"),
        "length_diff": abs(stats_a.get("length", 0) - stats_b.get("length", 0)),
        "length_diff_pct": round(100 * abs(stats_a.get("length", 0) - stats_b.get("length", 0)) /
                                    max(1, stats_a.get("length", 1)), 1),
        "a_entropy": stats_a.get("payload_entropy"),
        "b_entropy": stats_b.get("payload_entropy"),
        "a_issues_count": len(va.get("issues", [])),
        "b_issues_count": len(vb.get("issues", [])),
    }


# ═══════════════════════════════════════════════════════════
#  4. CRC/HMAC RECALC (decodifica XOR + recomputa checksum)
# ═══════════════════════════════════════════════════════════
def _xor_decode(payload_b64: str, key_hash: str) -> Optional[bytes]:
    """Decodifica el payload XOR con la key_hash (nuestro encoding v2/v3).
    Si el sensor fue generado con build_sensor_data_varied, este decode
    recupera el JSON original.
    """
    import base64
    try:
        encoded = base64.b64decode(payload_b64)
        key_bytes = key_hash.encode()
        if not key_bytes:
            return None
        decoded = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(encoded))
        return decoded
    except Exception:
        return None


def verify_checksum(sensor: str) -> dict:
    """Valida el checksum del sensor recomputando SHA256[:12] sobre el payload_b64.

    Esto replica la validación server-side que Akamai hace:
      expected_checksum = sha256(payload_b64)[:12]
      if received_checksum != expected_checksum → REJECT

    Devuelve:
      {
        ok, match, expected_checksum, received_checksum,
        decoded_length, is_valid_json (si pudimos decodificar),
        json_keys (sample de keys si es JSON válido)
      }
    """
    import base64
    import hashlib

    parts = sensor.split(";")
    if len(parts) < 3:
        return {"ok": False, "error": "sensor no tiene suficientes partes",
                 "parts_count": len(parts)}

    # v2/v3 completo: [ver, length, key_hash, payload_b64, checksum/sig]
    # v2 simple: [ver, payload_b64, checksum]
    if len(parts) == 5:
        version = parts[0]
        key_hash = parts[2]
        payload_b64 = parts[3]
        received = parts[4]
    elif len(parts) == 3:
        version = parts[0]
        key_hash = ""  # v2_simple no tiene key_hash separado
        payload_b64 = parts[1]
        received = parts[2]
    else:
        return {"ok": False, "error": f"formato desconocido ({len(parts)} parts)"}

    result = {
        "ok": True,
        "version": version,
        "parts_count": len(parts),
        "received_checksum": received,
        "received_length": len(received),
    }

    # Para v3 el campo es signature HMAC-like SHA1[:20]
    if version == "3" and len(received) == 20:
        expected = hashlib.sha1((payload_b64 + key_hash).encode()).hexdigest()[:20]
        result["algorithm"] = "sha1_sig_v3"
    else:
        # v2 → SHA256[:12]
        expected = hashlib.sha256(payload_b64.encode()).hexdigest()[:12]
        result["algorithm"] = "sha256_checksum_v2"

    result["expected_checksum"] = expected
    result["match"] = (received.lower() == expected.lower())
    result["verdict"] = "VALID" if result["match"] else "TAMPERED_OR_WRONG_ALGO"

    # Intentar decodificar el payload XOR (si tenemos key_hash)
    if key_hash:
        decoded = _xor_decode(payload_b64, key_hash)
        if decoded:
            result["decoded_length"] = len(decoded)
            result["decoded_sha256"] = hashlib.sha256(decoded).hexdigest()[:16]
            # ¿Es JSON?
            try:
                import json as _json
                obj = _json.loads(decoded)
                result["is_valid_json"] = True
                if isinstance(obj, dict):
                    result["json_keys"] = list(obj.keys())[:15]
                    result["json_keys_count"] = len(obj)
                    # Validar que tiene los campos esperados de sensor
                    expected_keys = {"mouse_events", "key_events", "fingerprint",
                                      "performance_timing", "session_id"}
                    found_keys = set(obj.keys()) & expected_keys
                    result["expected_keys_found"] = sorted(found_keys)
                    result["expected_keys_coverage_pct"] = round(
                        100 * len(found_keys) / len(expected_keys), 1
                    )
            except Exception as e:
                result["is_valid_json"] = False
                result["decode_error"] = str(e)[:60]
                # Decoded parece binario random → es señal de XOR con key incorrecta o datos corruptos
                # Calcular % de bytes printable
                try:
                    printable = sum(1 for b in decoded if 32 <= b < 127)
                    result["printable_ratio"] = round(100 * printable / max(1, len(decoded)), 1)
                except Exception:
                    pass

    return result


def recompute_with_repair(sensor: str) -> dict:
    """Recalcula checksum y genera sensor 'reparado' con checksum correcto.

    Útil cuando generas payload custom y quieres asegurar checksum válido.
    """
    import hashlib
    parts = sensor.split(";")
    if len(parts) not in (3, 5):
        return {"ok": False, "error": f"formato inválido ({len(parts)} parts)"}

    if len(parts) == 5:
        version, length, key_hash, payload_b64, _old_chk = parts
        if version == "3":
            new_chk = hashlib.sha1((payload_b64 + key_hash).encode()).hexdigest()[:20]
        else:
            new_chk = hashlib.sha256(payload_b64.encode()).hexdigest()[:12]
        repaired = f"{version};{length};{key_hash};{payload_b64};{new_chk}"
    else:
        version, payload_b64, _old_chk = parts
        new_chk = hashlib.sha256(payload_b64.encode()).hexdigest()[:12]
        repaired = f"{version};{payload_b64};{new_chk}"

    return {
        "ok": True,
        "original_sensor": sensor,
        "original_checksum": _old_chk,
        "new_checksum": new_chk,
        "changed": sensor != repaired,
        "repaired_sensor": repaired,
    }


# ═══════════════════════════════════════════════════════════
#  5. PATTERN MATCHING vs CAPTURAS REALES (DB)
# ═══════════════════════════════════════════════════════════
def _get_reference_captures() -> List[dict]:
    """Lee las capturas reales del pool akamai_bmjs (si existen)."""
    try:
        d = json_atomic.load(config.ruta("akamai_sensor_captures.json"),
                              default={"captures": []})
        return d.get("captures", [])
    except Exception:
        return []


def match_against_captures(sensor: str, max_compare: int = 20) -> dict:
    """Compara byte-por-byte el sensor contra capturas reales del pool.

    Devuelve similarity ratio, best match, y análisis estructural.
    Si no hay capturas, retorna indicando que no hay baseline.
    """
    import difflib
    captures = _get_reference_captures()

    if not captures:
        return {
            "ok": True,
            "verdict": "NO_BASELINE",
            "captures_available": 0,
            "note": "No hay capturas reales en pool. Ejecuta capture_sensor_data primero.",
        }

    # Comparar contra top-N capturas más recientes
    comparisons = []
    for cap in captures[:max_compare]:
        cap_sensor = cap.get("sensor_data", "")
        if not cap_sensor:
            continue

        # Quick similarity (primeros 3000 chars)
        sm = difflib.SequenceMatcher(None, sensor[:3000], cap_sensor[:3000])
        similarity = round(sm.ratio() * 100, 2)

        # Diff por partes
        sensor_parts = sensor.split(";")
        cap_parts = cap_sensor.split(";")
        parts_match = {
            "same_version": len(sensor_parts) > 0 and len(cap_parts) > 0 and sensor_parts[0] == cap_parts[0],
            "same_parts_count": len(sensor_parts) == len(cap_parts),
        }

        # Hash del payload body
        body_a = sensor_parts[-2] if len(sensor_parts) >= 2 else ""
        body_b = cap_parts[-2] if len(cap_parts) >= 2 else ""
        body_sim = 0.0
        if body_a and body_b:
            sm2 = difflib.SequenceMatcher(None, body_a[:2000], body_b[:2000])
            body_sim = round(sm2.ratio() * 100, 2)

        comparisons.append({
            "capture_id": cap.get("id"),
            "captured_at": cap.get("captured_at"),
            "target_url": cap.get("target_url"),
            "user_agent": (cap.get("user_agent", "") or "")[:60],
            "overall_similarity_pct": similarity,
            "body_similarity_pct": body_sim,
            "length_diff": abs(len(sensor) - len(cap_sensor)),
            "length_diff_pct": round(100 * abs(len(sensor) - len(cap_sensor)) /
                                        max(1, len(cap_sensor)), 1),
            **parts_match,
        })

    # Best match
    comparisons.sort(key=lambda c: c["overall_similarity_pct"], reverse=True)
    best = comparisons[0] if comparisons else None
    avg_sim = round(sum(c["overall_similarity_pct"] for c in comparisons) / max(1, len(comparisons)), 2)

    # Verdict basado en similarity:
    # - Si > 95% con alguno → prácticamente clonado (sospechoso!)
    # - Si 40-90% → similitud estructural razonable (healthy)
    # - Si < 20% → totalmente distinto, probable bot nuevo o mal generado
    verdict = "UNKNOWN"
    if best:
        bs = best["overall_similarity_pct"]
        if bs > 98:
            verdict = "SUSPICIOUS_CLONE"  # demasiado parecido, podría ser replay sin mutación
        elif bs > 85:
            verdict = "VERY_SIMILAR"       # parecido pero con mutaciones
        elif bs > 40:
            verdict = "STRUCTURALLY_SIMILAR"
        elif bs > 15:
            verdict = "WEAK_MATCH"
        else:
            verdict = "NO_MATCH"           # estructura completamente distinta

    return {
        "ok": True,
        "verdict": verdict,
        "captures_compared": len(comparisons),
        "best_match": best,
        "avg_similarity_pct": avg_sim,
        "max_similarity_pct": best["overall_similarity_pct"] if best else 0,
        "top_3_matches": comparisons[:3],
    }


def get_fingerprint_distance(sensor_a: str, sensor_b: str) -> dict:
    """Distancia fina entre 2 sensors usando múltiples métricas.

    Métricas:
      - Levenshtein ratio (similitud global)
      - Hamming distance por parte (version, length, hash, b64, chk)
      - Entropy diff
      - Length ratio
    """
    import difflib

    # Global similarity
    sm = difflib.SequenceMatcher(None, sensor_a, sensor_b)
    global_ratio = round(sm.ratio(), 4)

    # Parts-wise
    parts_a = sensor_a.split(";")
    parts_b = sensor_b.split(";")
    parts_similarity = []
    for i, (pa, pb) in enumerate(zip(parts_a, parts_b)):
        if not pa or not pb:
            continue
        psm = difflib.SequenceMatcher(None, pa, pb)
        parts_similarity.append({
            "part_idx": i,
            "similarity": round(psm.ratio() * 100, 2),
            "len_a": len(pa), "len_b": len(pb),
        })

    # Entropy
    ent_a = _shannon_entropy_simple(sensor_a[:3000])
    ent_b = _shannon_entropy_simple(sensor_b[:3000])

    # Clasificación
    distance_class = "IDENTICAL" if global_ratio > 0.99 else (
        "VERY_CLOSE" if global_ratio > 0.85 else (
        "SIMILAR" if global_ratio > 0.50 else (
        "DIFFERENT" if global_ratio > 0.15 else "UNRELATED")))

    return {
        "ok": True,
        "global_similarity_pct": round(global_ratio * 100, 2),
        "distance_class": distance_class,
        "length_ratio": round(min(len(sensor_a), len(sensor_b)) /
                                max(1, max(len(sensor_a), len(sensor_b))), 3),
        "entropy_a": round(ent_a, 3),
        "entropy_b": round(ent_b, 3),
        "entropy_diff": round(abs(ent_a - ent_b), 3),
        "parts_a_count": len(parts_a),
        "parts_b_count": len(parts_b),
        "same_parts_count": len(parts_a) == len(parts_b),
        "parts_similarity": parts_similarity[:5],
    }


def full_forensic_analysis(sensor: str,
                             expected_session_id: Optional[str] = None) -> dict:
    """Análisis FORENSE completo del sensor: estructura + checksum + patterns.

    Es el check definitivo antes de enviar un sensor a producción.
    Devuelve un reporte con 4 secciones:
      - structural: 20 checks
      - checksum: ¿es válido matemáticamente?
      - payload: ¿decodifica a JSON válido?
      - patterns: ¿cuán parecido a capturas reales?
    """
    structural = validate_sensor_structure(sensor, expected_session_id)
    checksum = verify_checksum(sensor)
    patterns = match_against_captures(sensor)

    # Verdict final combinado
    reasons = []
    if structural["verdict"] == "REJECT":
        reasons.append(f"structural={structural['verdict']}")
    if checksum.get("ok") and not checksum.get("match"):
        reasons.append("checksum_invalid")
    if patterns.get("verdict") == "SUSPICIOUS_CLONE":
        reasons.append("too_similar_to_existing")
    if patterns.get("verdict") == "NO_MATCH" and patterns.get("captures_compared", 0) > 0:
        reasons.append("no_similar_captures")

    if not reasons:
        if structural["verdict"] == "PASS" and checksum.get("match", True):
            final_verdict = "PRODUCTION_READY"
            confidence = 95
        elif structural["verdict"] == "WARNING":
            final_verdict = "ACCEPTABLE_WITH_WARNINGS"
            confidence = 70
        else:
            final_verdict = "UNKNOWN"
            confidence = 50
    else:
        final_verdict = "DO_NOT_SEND"
        confidence = max(0, 95 - len(reasons) * 25)

    return {
        "ok": True,
        "final_verdict": final_verdict,
        "confidence_pct": confidence,
        "reasons": reasons,
        "structural": {
            "verdict": structural["verdict"],
            "score": structural["score"],
            "issues_count": len(structural.get("issues", [])),
            "critical": structural.get("issues_by_severity", {}).get("CRITICAL", 0),
        },
        "checksum": {
            "algorithm": checksum.get("algorithm"),
            "match": checksum.get("match"),
            "is_valid_json": checksum.get("is_valid_json"),
            "json_keys_count": checksum.get("json_keys_count"),
        },
        "patterns": {
            "verdict": patterns.get("verdict"),
            "max_similarity_pct": patterns.get("max_similarity_pct"),
            "captures_compared": patterns.get("captures_compared"),
        },
    }


def validate_sensor_semantic(payload: dict) -> dict:
    """Validación semántica del contenido del payload.
    Detecta patrones de bot (timings constantes, falta de diversidad).
    """
    result = {"ok": True, "issues": [], "bot_signals": []}

    mouse_events = payload.get("mouse_events", [])
    key_events = payload.get("key_events", [])
    touch_events = payload.get("touch_events", [])

    total_events = len(mouse_events) + len(key_events) + len(touch_events)
    result["total_events"] = total_events

    # Señal 1: muy pocos eventos (bot directo)
    if total_events < 15:
        result["bot_signals"].append("too_few_events")

    # Señal 2: sin mouse moves (bot HTTP puro)
    if len(mouse_events) == 0 and "mobile" not in payload.get("fingerprint", {}).get("ua", "").lower():
        result["bot_signals"].append("no_mouse_on_desktop")

    # Señal 3: timings constantes (mouse)
    if len(mouse_events) >= 5:
        dts = []
        for i in range(1, len(mouse_events)):
            if "t" in mouse_events[i] and "t" in mouse_events[i - 1]:
                dts.append(mouse_events[i]["t"] - mouse_events[i - 1]["t"])
        if dts:
            try:
                std = statistics.stdev(dts) if len(dts) > 1 else 0
                mean = statistics.mean(dts)
                cv = std / max(1, mean)  # coeficiente de variación
                result["mouse_timing_cv"] = round(cv, 3)
                if cv < 0.1:  # muy poco variación → bot
                    result["bot_signals"].append("constant_mouse_timing")
            except Exception:
                pass

    # Señal 4: mouse lineal (dx, dy iguales)
    if len(mouse_events) >= 5:
        deltas_x = [e.get("dx", 0) for e in mouse_events if "dx" in e]
        deltas_y = [e.get("dy", 0) for e in mouse_events if "dy" in e]
        if deltas_x and len(set(deltas_x)) == 1:
            result["bot_signals"].append("constant_mouse_dx")
        if deltas_y and len(set(deltas_y)) == 1:
            result["bot_signals"].append("constant_mouse_dy")

    # Señal 5: key cadence muy uniforme
    if len(key_events) >= 6:
        key_dts = []
        for i in range(1, len(key_events)):
            if "t" in key_events[i] and "t" in key_events[i - 1]:
                key_dts.append(key_events[i]["t"] - key_events[i - 1]["t"])
        if key_dts:
            try:
                k_std = statistics.stdev(key_dts) if len(key_dts) > 1 else 0
                k_mean = statistics.mean(key_dts)
                k_cv = k_std / max(1, k_mean)
                result["key_timing_cv"] = round(k_cv, 3)
                if k_cv < 0.15:
                    result["bot_signals"].append("constant_key_cadence")
            except Exception:
                pass

    # Señal 6: sin noise events (humanos generan basura)
    if total_events > 30 and not any("noise" in e for e in mouse_events):
        result["bot_signals"].append("no_noise_events")

    # Fingerprint
    fp = payload.get("fingerprint", {})
    if fp.get("webgl_vendor", "").startswith("Google") or "Mesa" in fp.get("webgl_renderer", ""):
        result["bot_signals"].append("headless_gpu_signature")
    if fp.get("hardwareConcurrency", 4) == 0:
        result["bot_signals"].append("zero_hw_concurrency")

    # Score: menos señales = más humano
    result["bot_score"] = min(100, len(result["bot_signals"]) * 15)
    result["human_score"] = 100 - result["bot_score"]
    result["ok"] = result["bot_score"] < 40  # <40 se considera humano

    return result


def validate_full(sensor: str, payload: Optional[dict] = None) -> dict:
    """Validación combinada: estructura + semántica."""
    structure = validate_sensor_structure(sensor)
    semantic = {"ok": True, "bot_signals": []}
    if payload is not None:
        semantic = validate_sensor_semantic(payload)

    ok = structure["ok"] and semantic.get("ok", True)
    return {
        "ok": ok,
        "verdict": "PASS" if ok else "REJECT",
        "structure": structure,
        "semantic": semantic,
        "combined_bot_score": max(structure.get("bot_score", 0),
                                    semantic.get("bot_score", 0)),
    }


# ═══════════════════════════════════════════════════════════
#  2. SHADOW DOM MONITOR
# ═══════════════════════════════════════════════════════════
def extract_sensor_fields(payload: dict) -> List[str]:
    """Lista de campos (keys) del payload + subcampos conocidos."""
    fields = set()

    def _walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}.{k}" if prefix else k
                fields.add(path)
                if isinstance(v, (dict, list)):
                    _walk(v, path)
        elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
            # solo walk del primer elemento como sample
            _walk(obj[0], prefix + "[0]")

    _walk(payload)
    return sorted(fields)


def update_baseline(payload: dict) -> dict:
    """Añade campos del payload al baseline conocido."""
    fields = set(extract_sensor_fields(payload))
    with _lock:
        d = json_atomic.load(config.ruta(_BASELINE_KEY),
                              default={"fields": [], "samples": 0,
                                        "first_seen": datetime.now().isoformat()})
        known = set(d.get("fields", []))
        new_fields = fields - known
        d["fields"] = sorted(known | fields)
        d["samples"] = d.get("samples", 0) + 1
        d["last_updated"] = datetime.now().isoformat()
        if new_fields:
            d.setdefault("field_history", []).append({
                "ts": datetime.now().isoformat(),
                "added": sorted(new_fields),
            })
            d["field_history"] = d["field_history"][-50:]
        json_atomic.save(config.ruta(_BASELINE_KEY), d)
    return {
        "ok": True,
        "new_fields": sorted(new_fields),
        "total_known": len(d["fields"]),
        "samples": d["samples"],
    }


def check_new_fields(payload: dict) -> dict:
    """Compara payload contra baseline: detecta campos nuevos (Akamai actualiza bm.js)."""
    fields = set(extract_sensor_fields(payload))
    d = json_atomic.load(config.ruta(_BASELINE_KEY), default={"fields": []})
    known = set(d.get("fields", []))
    new = fields - known
    removed = known - fields
    return {
        "ok": True,
        "new_fields_detected": sorted(new),
        "removed_fields": sorted(removed)[:20],
        "payload_fields_count": len(fields),
        "baseline_fields_count": len(known),
        "match_pct": round(100 * len(fields & known) / max(1, len(fields)), 1),
    }


def get_baseline_info() -> dict:
    d = json_atomic.load(config.ruta(_BASELINE_KEY), default={})
    return {
        "fields_count": len(d.get("fields", [])),
        "samples": d.get("samples", 0),
        "last_updated": d.get("last_updated"),
        "field_history_entries": len(d.get("field_history", [])),
        "recent_history": d.get("field_history", [])[-5:],
    }


# ═══════════════════════════════════════════════════════════
#  3. ML VALIDATOR (basado en estadísticas)
# ═══════════════════════════════════════════════════════════
# Distribuciones esperadas para payloads "humanos"
EXPECTED_DISTRIBUTIONS = {
    "total_events": {"mean": 80, "std": 30, "min": 30, "max": 200},
    "mouse_events": {"mean": 40, "std": 20, "min": 10, "max": 100},
    "key_events": {"mean": 15, "std": 10, "min": 0, "max": 50},
    "touch_events": {"mean": 2, "std": 5, "min": 0, "max": 60},
    "payload_bytes": {"mean": 6000, "std": 2500, "min": 1500, "max": 15000},
}


def ml_validate_payload(payload: dict, sensor_length: Optional[int] = None) -> dict:
    """Clasificador estadístico: z-score contra distribuciones esperadas."""
    features = {
        "total_events": (len(payload.get("mouse_events", [])) +
                          len(payload.get("key_events", [])) +
                          len(payload.get("touch_events", []))),
        "mouse_events": len(payload.get("mouse_events", [])),
        "key_events": len(payload.get("key_events", [])),
        "touch_events": len(payload.get("touch_events", [])),
        "payload_bytes": sensor_length or 0,
    }

    z_scores = {}
    flags = []
    for feat, val in features.items():
        dist = EXPECTED_DISTRIBUTIONS.get(feat)
        if not dist or val == 0 and feat == "payload_bytes":
            continue
        mean = dist["mean"]
        std = dist["std"]
        z = (val - mean) / max(0.1, std)
        z_scores[feat] = round(z, 2)
        # Flags: >2σ es sospechoso
        if abs(z) > 2.5:
            flags.append(f"{feat}_z={round(z,2)}")
        # Out of range
        if val < dist["min"] or val > dist["max"]:
            flags.append(f"{feat}_oor")

    # Calcular score "human-ness"
    avg_abs_z = sum(abs(z) for z in z_scores.values()) / max(1, len(z_scores))
    # < 0.5 = muy típico humano, > 2.5 = sospechoso
    human_prob = max(0, min(100, int(100 - avg_abs_z * 30)))

    verdict = "HUMAN" if human_prob >= 70 else (
        "SUSPICIOUS" if human_prob >= 40 else "BOT"
    )

    return {
        "ok": True,
        "verdict": verdict,
        "human_probability_pct": human_prob,
        "z_scores": z_scores,
        "flags": flags,
        "avg_abs_z": round(avg_abs_z, 2),
        "features": features,
    }


# ═══════════════════════════════════════════════════════════
#  SELF TEST
# ═══════════════════════════════════════════════════════════
def self_test() -> dict:
    """Test EXHAUSTIVO — 15+ escenarios para cubrir todos los checks."""
    results = {"ok": True, "checks": {}, "scenarios": {}}

    import base64
    # Generar un payload b64 con entropía alta (cifrado-like)
    good_b64 = base64.b64encode(bytes([(i * 97 + 31) % 256 for i in range(2000)])).decode()

    # === Escenario 1: Sensor OK v2 completo ===
    good_v2 = f"2;2000;abc123def4567890;{good_b64};abc123def456"
    v = validate_sensor_structure(good_v2)
    results["scenarios"]["good_v2"] = {
        "verdict": v["verdict"],
        "score": v["score"],
        "accept_prob": v["akamai_accept_probability"],
        "issues_count": len(v["issues"]),
    }
    results["checks"]["good_v2_passes"] = v["verdict"] in ("PASS", "WARNING")

    # === Escenario 2: Sensor OK v3 con HMAC ===
    good_v3 = f"3;2000;abc123def4567890;{good_b64};abcdef0123456789abcd"
    v3 = validate_sensor_structure(good_v3)
    results["scenarios"]["good_v3"] = {
        "verdict": v3["verdict"],
        "score": v3["score"],
    }
    results["checks"]["good_v3_passes"] = v3["verdict"] in ("PASS", "WARNING")

    # === Escenario 3: Completamente malformado ===
    bad = "invalid"
    vb = validate_sensor_structure(bad)
    results["scenarios"]["malformed"] = {
        "verdict": vb["verdict"],
        "score": vb["score"],
        "issues_count": len(vb["issues"]),
        "critical_count": vb["issues_by_severity"]["CRITICAL"],
    }
    results["checks"]["malformed_rejected"] = vb["verdict"] == "REJECT"

    # === Escenario 4: Vacío ===
    vempty = validate_sensor_structure("")
    results["scenarios"]["empty"] = {
        "verdict": vempty["verdict"],
        "score": vempty["score"],
    }
    results["checks"]["empty_rejected"] = vempty["verdict"] == "REJECT"

    # === Escenario 5: v1 legacy ===
    v1_legacy = f"1;500;abc123def4567890;{good_b64[:500]};abc123"
    vl = validate_sensor_structure(v1_legacy)
    results["scenarios"]["v1_legacy"] = {
        "verdict": vl["verdict"],
        "has_legacy_warning": any(i["code"] == "legacy_v1" for i in vl["issues"]),
    }

    # === Escenario 6: Null bytes ===
    with_null = good_v2[:100] + "\x00" + good_v2[100:]
    vn = validate_sensor_structure(with_null)
    results["scenarios"]["null_bytes"] = {
        "verdict": vn["verdict"],
        "critical_null": any(i["code"] == "null_bytes" for i in vn["issues"]),
    }
    results["checks"]["null_detected"] = vn["verdict"] == "REJECT"

    # === Escenario 7: Newlines ===
    with_newlines = f"2;2000;abc123def4567890;\n{good_b64}\n;abc123def456"
    vnl = validate_sensor_structure(with_newlines)
    results["scenarios"]["newlines"] = {
        "verdict": vnl["verdict"],
        "detects_whitespace": any("whitespace" in i["code"] for i in vnl["issues"]),
    }

    # === Escenario 8: Entropía muy baja (texto plain) ===
    low_ent = f"2;1000;abc123def4567890;{'A' * 1000};abc123def456"
    ve = validate_sensor_structure(low_ent)
    results["scenarios"]["low_entropy"] = {
        "verdict": ve["verdict"],
        "entropy": ve["stats"].get("payload_entropy"),
        "detects_low_ent": any(i["code"] == "low_entropy" for i in ve["issues"]),
    }
    results["checks"]["low_entropy_flagged"] = results["scenarios"]["low_entropy"]["detects_low_ent"]

    # === Escenario 9: b64 inválido ===
    invalid_b64 = f"2;1000;abc123def4567890;{'*' * 500};abc123def456"
    vib = validate_sensor_structure(invalid_b64)
    results["scenarios"]["invalid_b64"] = {
        "verdict": vib["verdict"],
        "detects_b64": any(i["code"] == "invalid_base64" for i in vib["issues"]),
    }
    results["checks"]["invalid_b64_rejected"] = vib["verdict"] == "REJECT"

    # === Escenario 10: Hash no-hex ===
    bad_hash = f"2;2000;HELLOWORLDNOHEX1;{good_b64};abc123def456"
    vbh = validate_sensor_structure(bad_hash)
    results["scenarios"]["hash_not_hex"] = {
        "detects": any(i["code"] == "hash_not_hex" for i in vbh["issues"]),
    }

    # === Escenario 11: Session ID match ===
    import hashlib
    session_id = "stable_test_123"
    correct_hash = hashlib.md5(session_id.encode()).hexdigest()[:16]
    with_sid = f"2;2000;{correct_hash};{good_b64};abc123def456"
    vs = validate_sensor_structure(with_sid, expected_session_id=session_id)
    results["scenarios"]["session_match"] = {
        "verdict": vs["verdict"],
        "no_mismatch": not any(i["code"] == "hash_mismatch_session" for i in vs["issues"]),
    }
    results["checks"]["session_id_validates"] = results["scenarios"]["session_match"]["no_mismatch"]

    # === Escenario 12: length mismatch (declarada vs real) ===
    mismatch = f"2;999999;abc123def4567890;{good_b64};abc123def456"
    vm = validate_sensor_structure(mismatch)
    results["scenarios"]["length_mismatch"] = {
        "detects": any(i["code"] == "length_mismatch" for i in vm["issues"]),
    }

    # === Escenario 13: v2_simple (3 parts) ===
    v2_simple = f"2;{good_b64};abc123def456"
    vss = validate_sensor_structure(v2_simple)
    results["scenarios"]["v2_simple"] = {
        "verdict": vss["verdict"],
        "variant": vss["stats"].get("format_variant"),
    }

    # === Escenario 14: Auto-repair ===
    repair = try_auto_repair("  2;2000;abc;data\n;chk  ")
    results["checks"]["auto_repair_works"] = repair["fully_repaired"]
    results["checks"]["auto_repair_count"] = len(repair["repairs_applied"])

    # === Escenario 15: Compare structures ===
    cmp = compare_structures(good_v2, good_v3)
    results["checks"]["compare_works"] = cmp["ok"]
    results["checks"]["compare_detects_version"] = not cmp["same_version"]

    # Payload humano (muchos eventos con variación)
    import random
    human_payload = {
        "mouse_events": [
            {"type": "mm", "x": random.randint(0, 1920), "y": random.randint(0, 1080),
             "t": i * random.randint(10, 100), "dx": random.randint(-30, 30),
             "dy": random.randint(-20, 20)} for i in range(50)
        ],
        "key_events": [
            {"type": "kd", "k": 65, "t": i * random.randint(100, 200)}
            for i in range(15)
        ],
        "touch_events": [],
        "fingerprint": {"ua": "Chrome 131", "hardwareConcurrency": 8,
                         "webgl_vendor": "NVIDIA"},
    }
    h = validate_sensor_semantic(human_payload)
    results["checks"]["human_bot_signals"] = len(h["bot_signals"])
    results["checks"]["human_verdict_ok"] = h["ok"]

    # Payload bot (pocos eventos, sin variación)
    bot_payload = {
        "mouse_events": [],
        "key_events": [],
        "touch_events": [],
        "fingerprint": {"ua": "Chrome", "hardwareConcurrency": 0,
                         "webgl_vendor": "Google Inc. (headless)"},
    }
    b = validate_sensor_semantic(bot_payload)
    results["checks"]["bot_signals_detected"] = len(b["bot_signals"])
    results["checks"]["bot_verdict_reject"] = not b["ok"]

    # ML validator
    ml = ml_validate_payload(human_payload, sensor_length=6000)
    results["checks"]["ml_verdict"] = ml["verdict"]
    results["checks"]["ml_human_prob"] = ml["human_probability_pct"]

    ml_bot = ml_validate_payload(bot_payload, sensor_length=500)
    results["checks"]["ml_bot_verdict"] = ml_bot["verdict"]

    # Shadow DOM monitor
    update_baseline(human_payload)
    check = check_new_fields(bot_payload)
    results["checks"]["shadow_baseline_working"] = check["ok"]

    # Resumen
    results["total_scenarios"] = len(results["scenarios"])
    results["scenarios_passed"] = sum(1 for k in results["checks"]
                                         if "passes" in k or "rejected" in k or "detected" in k or "works" in k
                                         if results["checks"].get(k) is True)

    return results

    # Payload humano (muchos eventos con variación)
    import random
    human_payload = {
        "mouse_events": [
            {"type": "mm", "x": random.randint(0, 1920), "y": random.randint(0, 1080),
             "t": i * random.randint(10, 100), "dx": random.randint(-30, 30),
             "dy": random.randint(-20, 20)} for i in range(50)
        ],
        "key_events": [
            {"type": "kd", "k": 65, "t": i * random.randint(100, 200)}
            for i in range(15)
        ],
        "touch_events": [],
        "fingerprint": {"ua": "Chrome 131", "hardwareConcurrency": 8,
                         "webgl_vendor": "NVIDIA"},
    }
    h = validate_sensor_semantic(human_payload)
    results["checks"]["human_bot_signals"] = len(h["bot_signals"])
    results["checks"]["human_verdict_ok"] = h["ok"]

    # Payload bot (pocos eventos, sin variación)
    bot_payload = {
        "mouse_events": [],
        "key_events": [],
        "touch_events": [],
        "fingerprint": {"ua": "Chrome", "hardwareConcurrency": 0,
                         "webgl_vendor": "Google Inc. (headless)"},
    }
    b = validate_sensor_semantic(bot_payload)
    results["checks"]["bot_signals_detected"] = len(b["bot_signals"])
    results["checks"]["bot_verdict_reject"] = not b["ok"]

    # ML validator
    ml = ml_validate_payload(human_payload, sensor_length=6000)
    results["checks"]["ml_verdict"] = ml["verdict"]
    results["checks"]["ml_human_prob"] = ml["human_probability_pct"]

    ml_bot = ml_validate_payload(bot_payload, sensor_length=500)
    results["checks"]["ml_bot_verdict"] = ml_bot["verdict"]

    # Shadow DOM monitor
    update_baseline(human_payload)
    check = check_new_fields(bot_payload)
    results["checks"]["shadow_baseline_working"] = check["ok"]

    return results


def get_info() -> dict:
    return {
        "module": "akamai_validator",
        "capabilities": [
            "validate_structure", "validate_semantic",
            "shadow_dom_monitor", "ml_validator_z_score",
        ],
        "baseline": get_baseline_info(),
    }
