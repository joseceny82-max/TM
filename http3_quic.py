"""http3_quic.py — HTTP/3 QUIC support con JA4+Q fingerprint.

Mejora #http3_quic del catálogo:
  HTTP/3 QUIC reduce latencia 20-40% vs TCP. TM usa HTTP/3 en edge Akamai.
  Akamai desplegó JA4+Q fingerprinting en 2026 → detecta clientes que
  solo hablan TCP cuando el navegador real habla QUIC.

Este módulo:
  - probe_alt_svc(url)           → detecta si el endpoint anuncia h3 via Alt-Svc
  - is_http3_supported(host)     → prueba directa QUIC handshake
  - fetch_http3(url, headers)    → GET HTTP/3 real con aioquic
  - benchmark_http2_vs_http3(url)→ compara latencia TCP+H2 vs QUIC+H3
  - get_ja4q(host)               → calcula JA4+Q fingerprint del cliente
  - self_test()

Requiere `pip install aioquic`. Si no está disponible, funciones devuelven
{"ok":False, "error":"aioquic no instalado"}.
"""
import asyncio
import hashlib
import time
import socket
from typing import Optional, Dict, List
from datetime import datetime
from urllib.parse import urlparse

import config
import json_atomic


# ═══════════════════════════════════════════
#  DISPONIBILIDAD
# ═══════════════════════════════════════════
try:
    from aioquic.asyncio import connect as quic_connect
    from aioquic.asyncio.client import connect as _quic_connect_client
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import HeadersReceived, DataReceived
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import HandshakeCompleted, StreamDataReceived
    AIOQUIC_AVAILABLE = True
    AIOQUIC_ERROR = None
except Exception as e:
    AIOQUIC_AVAILABLE = False
    AIOQUIC_ERROR = str(e)


LOG_KEY = "http3_quic_log.json"


def _log(entry: dict):
    try:
        d = json_atomic.load(config.ruta(LOG_KEY), default={"log": []})
        d["log"].insert(0, entry)
        d["log"] = d["log"][:100]
        json_atomic.save(config.ruta(LOG_KEY), d)
    except Exception:
        pass


def get_log(limit: int = 30) -> List[dict]:
    return json_atomic.load(config.ruta(LOG_KEY), default={"log": []}).get("log", [])[:limit]


# ═══════════════════════════════════════════
#  1. ALT-SVC PROBE (sin QUIC — usa HTTP/2)
# ═══════════════════════════════════════════
def probe_alt_svc(url: str, timeout: float = 8.0) -> dict:
    """Hace GET HTTP/1.1 o HTTP/2 y mira el header Alt-Svc.
    Si contiene 'h3=' o 'h3-XX=', el endpoint soporta HTTP/3."""
    import requests
    started = time.time()
    result = {"url": url, "alt_svc_found": False, "h3_supported": False,
              "alt_svc_raw": "", "status": None}
    try:
        r = requests.get(url, timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0 (TM-Perros HTTP3 prober)"})
        result["status"] = r.status_code
        alt_svc = r.headers.get("Alt-Svc", "") or r.headers.get("alt-svc", "")
        if alt_svc:
            result["alt_svc_raw"] = alt_svc[:500]
            result["alt_svc_found"] = True
            if "h3=" in alt_svc or "h3-" in alt_svc:
                result["h3_supported"] = True
                # extract port
                for part in alt_svc.split(","):
                    part = part.strip()
                    if part.startswith("h3="):
                        port_str = part[3:].split(";")[0].strip('"').lstrip(":")
                        result["h3_port"] = port_str or "443"
                        break
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    _log({"type": "alt_svc", "ts": datetime.now().isoformat(), **result})
    return result


# ═══════════════════════════════════════════
#  2. HTTP/3 GET directo con aioquic
# ═══════════════════════════════════════════
class _H3Client(asyncio.Protocol):
    """Minimal HTTP/3 client protocol usando aioquic.
    Basado en el ejemplo canónico `examples/http3_client.py` del repo."""
    pass


async def _http3_get(host: str, port: int, path: str,
                      timeout: float = 10.0,
                      extra_headers: Optional[Dict[str, str]] = None) -> dict:
    """GET HTTP/3 minimalista. Si falla el handshake → descarta."""
    if not AIOQUIC_AVAILABLE:
        return {"ok": False, "error": f"aioquic no disponible: {AIOQUIC_ERROR}"}

    import ssl as ssl_mod
    config_quic = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
    config_quic.verify_mode = ssl_mod.CERT_NONE
    config_quic.idle_timeout = timeout

    started = time.time()
    response_data = {"ok": False, "body": b"", "headers": [],
                      "handshake_ms": None}

    try:
        async with quic_connect(
            host, port,
            configuration=config_quic,
        ) as client:
            response_data["handshake_ms"] = int((time.time() - started) * 1000)
            # aioquic 1.x tiene H3Client wrapper
            from aioquic.h3.connection import H3Connection
            h3 = H3Connection(client._quic)

            headers = [
                (b":method", b"GET"),
                (b":scheme", b"https"),
                (b":authority", host.encode()),
                (b":path", path.encode()),
                (b"user-agent", b"Mozilla/5.0 (TM-Perros H3)"),
                (b"accept", b"*/*"),
            ]
            for k, v in (extra_headers or {}).items():
                headers.append((k.lower().encode(), v.encode()))

            stream_id = client._quic.get_next_available_stream_id()
            h3.send_headers(stream_id=stream_id, headers=headers, end_stream=True)
            client.transmit()

            # Espera simple: polling del buffer H3
            loop_end = time.time() + timeout
            while time.time() < loop_end:
                await asyncio.sleep(0.1)
                # Process any pending QUIC events
                qevent = client._quic.next_event()
                while qevent is not None:
                    for h3event in h3.handle_event(qevent):
                        if isinstance(h3event, HeadersReceived):
                            response_data["headers"] = [
                                (h[0].decode("utf-8", errors="replace"),
                                 h[1].decode("utf-8", errors="replace"))
                                for h in h3event.headers
                            ]
                        elif isinstance(h3event, DataReceived):
                            response_data["body"] += h3event.data
                            if h3event.stream_ended:
                                response_data["ok"] = True
                                response_data["body_len"] = len(response_data["body"])
                                response_data["elapsed_ms"] = int((time.time() - started) * 1000)
                                return response_data
                    qevent = client._quic.next_event()
                client.transmit()

        response_data["error"] = "timeout esperando respuesta H3"
    except Exception as e:
        response_data["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    response_data["elapsed_ms"] = int((time.time() - started) * 1000)
    return response_data


def fetch_http3(url: str, timeout: float = 10.0,
                 extra_headers: Optional[Dict[str, str]] = None) -> dict:
    """GET HTTP/3 síncrono (wrap async)."""
    if not AIOQUIC_AVAILABLE:
        return {"ok": False, "error": f"aioquic no disponible: {AIOQUIC_ERROR}"}
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        # asyncio.run crea nuevo event loop
        r = asyncio.run(_http3_get(host, port, path, timeout, extra_headers))
        r["url"] = url
        # No incluir body completo en log (puede ser grande)
        log_entry = {k: v for k, v in r.items() if k != "body"}
        if "body" in r:
            log_entry["body_preview"] = r["body"][:200].decode("utf-8", errors="replace") if isinstance(r["body"], bytes) else str(r["body"])[:200]
        _log({"type": "h3_get", "ts": datetime.now().isoformat(), **log_entry})
        return r
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                  "url": url}
        _log({"type": "h3_get", "ts": datetime.now().isoformat(), **result})
        return result


def is_http3_supported(url: str, method: str = "alt_svc") -> dict:
    """Combina Alt-Svc probe + (opcional) HS real.
    method: 'alt_svc' (solo header) | 'handshake' (intenta conectar QUIC)

    El handshake se considera éxito si `handshake_ms` está poblado
    (conexión QUIC establecida) aunque el body GET timeout.
    """
    alt = probe_alt_svc(url)
    if method == "alt_svc":
        return {"ok": True, "http3": alt.get("h3_supported", False),
                 "details": alt}

    # handshake real
    if not alt.get("h3_supported"):
        return {"ok": True, "http3": False, "details": alt,
                 "reason": "Alt-Svc no anuncia h3"}
    parsed = urlparse(url)
    host = parsed.hostname
    port = int(alt.get("h3_port", "443"))
    try:
        r = fetch_http3(f"https://{host}:{port}/", timeout=5.0)
        # Handshake completado = QUIC funciona, aunque el body no llegue
        quic_ok = bool(r.get("handshake_ms") is not None) or r.get("ok", False)
        return {"ok": True, "http3": quic_ok,
                 "details": alt, "handshake": {
                     "ms": r.get("handshake_ms"),
                     "body_ok": r.get("ok", False),
                     "elapsed_ms": r.get("elapsed_ms"),
                 }}
    except Exception as e:
        return {"ok": True, "http3": False, "error": str(e)[:100]}


# ═══════════════════════════════════════════
#  3. BENCHMARK H2 vs H3
# ═══════════════════════════════════════════
def benchmark_http2_vs_http3(url: str, iterations: int = 3) -> dict:
    """Compara latencia TCP+HTTP/2 vs QUIC+HTTP/3 para misma URL.
    Devuelve delta de latencia y % mejora."""
    import requests
    started = time.time()
    h2_latencies = []
    h3_latencies = []
    errors = {"h2": [], "h3": []}

    for i in range(iterations):
        # H2 (o H1 si servidor no negocia h2)
        t0 = time.time()
        try:
            r = requests.get(url, timeout=10,
                              headers={"User-Agent": "Mozilla/5.0 (H2 bench)"})
            h2_latencies.append(int((time.time() - t0) * 1000))
        except Exception as e:
            errors["h2"].append(str(e)[:80])

        # H3
        if AIOQUIC_AVAILABLE:
            t0 = time.time()
            try:
                r = fetch_http3(url, timeout=10)
                if r.get("ok"):
                    h3_latencies.append(int((time.time() - t0) * 1000))
                else:
                    errors["h3"].append(r.get("error", "fail")[:80])
            except Exception as e:
                errors["h3"].append(str(e)[:80])
        else:
            errors["h3"].append("aioquic no disponible")

    def _avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else None

    h2_avg = _avg(h2_latencies)
    h3_avg = _avg(h3_latencies)
    delta_ms = None
    pct_improvement = None
    if h2_avg and h3_avg:
        delta_ms = round(h2_avg - h3_avg, 1)
        pct_improvement = round(100 * delta_ms / h2_avg, 1)

    result = {
        "url": url, "iterations": iterations,
        "h2_latencies_ms": h2_latencies,
        "h3_latencies_ms": h3_latencies,
        "h2_avg_ms": h2_avg, "h3_avg_ms": h3_avg,
        "delta_ms": delta_ms,
        "pct_improvement_h3": pct_improvement,
        "errors": errors,
        "total_elapsed_s": round(time.time() - started, 1),
        "aioquic_available": AIOQUIC_AVAILABLE,
    }
    _log({"type": "bench", "ts": datetime.now().isoformat(), **result})
    return result


# ═══════════════════════════════════════════
#  4. JA4+Q fingerprint
# ═══════════════════════════════════════════
# JA4+Q format: q{version}{sni}{alpn}_{ciphers_hash}_{ext_hash}
# Similar a JA4 para TLS, pero específico del Client Hello de QUIC.
# En 2026 Akamai usa JA4+Q para detectar clientes TCP que fingen ser browsers.

def calculate_ja4q(client_hello_params: dict) -> str:
    """Calcula JA4+Q fingerprint a partir de los parámetros del QUIC Initial.

    Args:
      client_hello_params: dict con:
        - version: str de QUIC version (e.g. "v1")
        - sni_count: int
        - alpn: str ('h3')
        - cipher_suites: list[str]
        - extensions: list[str]
        - transport_params: list[str]
    """
    version = client_hello_params.get("version", "v1")
    sni = "s" if client_hello_params.get("sni_count", 1) > 0 else "0"
    alpn = client_hello_params.get("alpn", "h3")[:2]
    cipher_count = len(client_hello_params.get("cipher_suites", []))
    ext_count = len(client_hello_params.get("extensions", []))

    # Version prefix: q13 (QUIC v1 TLS 1.3)
    prefix = f"q13{sni}{cipher_count:02d}{ext_count:02d}{alpn}"

    # Ciphers hash (primeros 12 hex)
    ciphers_str = ",".join(sorted(client_hello_params.get("cipher_suites", [])))
    ciphers_hash = hashlib.sha256(ciphers_str.encode()).hexdigest()[:12]

    # Extensions hash (primeros 12 hex)
    exts_str = ",".join(sorted(client_hello_params.get("extensions", [])))
    exts_hash = hashlib.sha256(exts_str.encode()).hexdigest()[:12]

    return f"{prefix}_{ciphers_hash}_{exts_hash}"


def get_ja4q_self() -> dict:
    """Calcula el JA4+Q del cliente local (aioquic) actual."""
    if not AIOQUIC_AVAILABLE:
        return {"ok": False, "error": "aioquic no disponible"}

    # Parámetros canónicos de aioquic para h3
    params = {
        "version": "v1",
        "sni_count": 1,
        "alpn": "h3",
        "cipher_suites": [
            "TLS_AES_128_GCM_SHA256",
            "TLS_AES_256_GCM_SHA384",
            "TLS_CHACHA20_POLY1305_SHA256",
        ],
        "extensions": [
            "server_name", "supported_groups", "ec_point_formats",
            "signature_algorithms", "application_layer_protocol_negotiation",
            "key_share", "supported_versions", "psk_key_exchange_modes",
            "quic_transport_parameters",
        ],
        "transport_params": [
            "initial_max_data", "initial_max_stream_data_bidi_local",
            "initial_max_stream_data_bidi_remote", "initial_max_stream_data_uni",
            "initial_max_streams_bidi", "initial_max_streams_uni",
        ],
    }
    ja4q = calculate_ja4q(params)
    return {
        "ok": True,
        "ja4q": ja4q,
        "params": params,
        "note": "JA4+Q fingerprint aproximado del cliente aioquic (para comparar con browser real)",
    }


# ═══════════════════════════════════════════
#  SELF TEST
# ═══════════════════════════════════════════
def self_test() -> dict:
    """Test mínimo — no requiere red."""
    r = {
        "ok": True,
        "aioquic_available": AIOQUIC_AVAILABLE,
        "aioquic_error": AIOQUIC_ERROR,
    }
    # Test calculate_ja4q
    fake = calculate_ja4q({
        "version": "v1", "sni_count": 1, "alpn": "h3",
        "cipher_suites": ["A", "B"], "extensions": ["X", "Y", "Z"],
    })
    r["ja4q_format_ok"] = fake.startswith("q13s")
    r["ja4q_sample"] = fake

    if AIOQUIC_AVAILABLE:
        r["self_ja4q"] = get_ja4q_self().get("ja4q")

    return r


def get_info() -> dict:
    try:
        import importlib.metadata
        ver = importlib.metadata.version("aioquic") if AIOQUIC_AVAILABLE else None
    except Exception:
        ver = None
    return {
        "aioquic_available": AIOQUIC_AVAILABLE,
        "aioquic_version": ver,
        "error": AIOQUIC_ERROR if not AIOQUIC_AVAILABLE else None,
        "supported_features": ["alt_svc_probe", "http3_get", "benchmark_h2_vs_h3",
                                "ja4q_fingerprint"] if AIOQUIC_AVAILABLE else ["alt_svc_probe"],
    }
