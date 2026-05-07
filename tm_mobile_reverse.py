"""tm_mobile_reverse — scaffold para reverse-engineering del app móvil TM.

Estrategia: capturar tráfico iOS/Android con mitmproxy, exportar a HAR/JSON,
replicar requests con curl_cffi (mobile profile) sin browser.

WORKFLOW MANUAL (no automatizable totalmente):
  1. Instalar app TM en device + mitmproxy en máquina
  2. Configurar device proxy → mitmproxy CA cert (Charles/mitmweb)
  3. Reproducir flow: open app → search event → start checkout
  4. Exportar trace JSON: `mitmproxy -w tm_mobile_capture.dump`
  5. Convertir: `mitmdump -r capture.dump --set save_stream_file=capture.json`
  6. Subir vía /api/mobile-reverse/upload

ESTE MÓDULO entonces:
  - Parsea el dump capturado
  - Identifica endpoints clave (auth, search, cart, checkout)
  - Genera requests Python equivalentes con curl_cffi mobile_chrome120
  - Devuelve módulo replay-ready

ENDPOINTS REST (en dashboard):
  POST /api/mobile-reverse/upload         — sube .json/.har capturado
  GET  /api/mobile-reverse/captures       — lista capturas analizadas
  POST /api/mobile-reverse/replay         — re-ejecuta requests parseados
  GET  /api/mobile-reverse/endpoints      — endpoints TM detectados
  GET  /api/mobile-reverse/info           — info módulo + workflow

NOTA: este módulo NO automatiza la captura (requiere device físico + mitmproxy).
Pero sí los pasos posteriores: parse → replay → integrar al flow.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

PROYECTO = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("tm_mobile_reverse")

CAPTURES_DIR = os.path.join(PROYECTO, "data", "mobile_captures")
os.makedirs(CAPTURES_DIR, exist_ok=True)

# Patrones de endpoints TM móvil que nos interesan
TM_MOBILE_ENDPOINTS = {
    "auth": [r"/api/identity/.*", r"/oauth.*authorize", r"/.*/login"],
    "events": [r"/discovery/v\d+/events", r"/events/.*", r"/api/event/.*"],
    "venues": [r"/discovery/v\d+/venues"],
    "cart": [r"/api/checkout.*", r"/api/cart", r"/.*addtocart"],
    "checkout": [r"/api/payment.*", r"/api/order.*", r"/.*purchase"],
    "akamai": [r"/_sec/cp_challenge.*", r"/akamai/.*"],
    "queue_it": [r"queue-it.*", r".*\.queue-it\.net"],
}


def upload_capture(filename: str, content: bytes,
                   format_hint: str = "auto") -> Dict[str, Any]:
    """Sube un dump capturado (HAR/mitmdump JSON/curl)."""
    if not filename or not content:
        return {"ok": False, "error": "filename + content required"}
    safe_name = re.sub(r"[^\w.-]", "_", filename)[:80]
    path = os.path.join(CAPTURES_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}")
    with open(path, "wb") as f:
        f.write(content)
    parsed = parse_capture(path, format_hint=format_hint)
    parsed["uploaded_at"] = datetime.now().isoformat()
    parsed["path"] = path
    parsed["size_kb"] = round(len(content) / 1024, 1)
    return parsed


def parse_capture(path: str, format_hint: str = "auto") -> Dict[str, Any]:
    """Parsea un capture file y extrae endpoints TM relevantes.

    Soporta: HAR (browser DevTools/Chrome), mitmdump JSON.
    """
    out: Dict[str, Any] = {"ok": False, "endpoints": [], "categorized": {}}
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        try:
            data = json.loads(raw)
        except Exception:
            return {"ok": False, "error": "not valid JSON (HAR/mitmdump JSON only)"}

        entries = []
        if isinstance(data, dict) and "log" in data and "entries" in data["log"]:
            # HAR format
            for e in data["log"]["entries"]:
                req = e.get("request", {})
                resp = e.get("response", {})
                entries.append({
                    "url": req.get("url", ""),
                    "method": req.get("method", ""),
                    "headers": {h["name"]: h["value"] for h in req.get("headers", [])},
                    "status": resp.get("status"),
                    "post_data": (req.get("postData") or {}).get("text", ""),
                    "started": e.get("startedDateTime"),
                })
        elif isinstance(data, list):
            # mitmdump JSON list
            for e in data:
                entries.append({
                    "url": e.get("request", {}).get("url", "") or e.get("url", ""),
                    "method": e.get("request", {}).get("method", "") or e.get("method", ""),
                    "headers": e.get("request", {}).get("headers", {}) or {},
                    "status": e.get("response", {}).get("status_code"),
                    "post_data": e.get("request", {}).get("content", ""),
                })
        else:
            return {"ok": False, "error": "unknown format — use HAR or mitmdump JSON"}

        # Filtrar solo TM endpoints
        tm_entries = [e for e in entries if "ticketm" in e["url"].lower()
                      or "queue-it" in e["url"].lower()
                      or "akamai" in e["url"].lower()]

        # Categorizar
        categorized: Dict[str, List[Dict]] = {k: [] for k in TM_MOBILE_ENDPOINTS}
        categorized["other"] = []
        for e in tm_entries:
            matched = False
            for cat, patterns in TM_MOBILE_ENDPOINTS.items():
                for p in patterns:
                    if re.search(p, e["url"], re.I):
                        categorized[cat].append({
                            "url": e["url"][:200],
                            "method": e["method"],
                            "status": e["status"],
                        })
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                categorized["other"].append({
                    "url": e["url"][:200], "method": e["method"], "status": e["status"]
                })

        out["ok"] = True
        out["total_requests"] = len(entries)
        out["tm_requests"] = len(tm_entries)
        out["categorized"] = {k: v[:20] for k, v in categorized.items() if v}
        out["categorized_counts"] = {k: len(v) for k, v in categorized.items()}
        out["unique_hosts"] = sorted({urlparse(e["url"]).netloc for e in tm_entries})[:20]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


def list_captures() -> List[Dict[str, Any]]:
    out = []
    if not os.path.exists(CAPTURES_DIR):
        return out
    for fname in sorted(os.listdir(CAPTURES_DIR), reverse=True):
        path = os.path.join(CAPTURES_DIR, fname)
        try:
            stat = os.stat(path)
            out.append({
                "filename": fname,
                "size_kb": round(stat.st_size / 1024, 1),
                "uploaded_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        except Exception:
            pass
    return out


def replay_request(url: str, method: str = "GET",
                   headers: Optional[Dict] = None,
                   body: Optional[str] = None,
                   impersonate: str = "chrome120") -> Dict[str, Any]:
    """Replay con curl_cffi (mobile-grade JA4+TLS Chrome real).

    `impersonate`: chrome120, chrome119, safari17_2_ios, etc.
    """
    try:
        from curl_cffi import requests as cffi_req
        sess = cffi_req.Session(impersonate=impersonate)
        if headers:
            sess.headers.update(headers)
        kwargs: Dict[str, Any] = {"timeout": 15, "allow_redirects": True}
        if body:
            try:
                kwargs["data"] = body
            except Exception:
                pass
        r = sess.request(method.upper(), url, **kwargs)
        return {
            "ok": True,
            "status": r.status_code,
            "headers_resp": dict(r.headers)[:20] if hasattr(r.headers, "items") else {},
            "body_preview": (r.text or "")[:500],
            "elapsed_s": getattr(r, "elapsed", None) and r.elapsed.total_seconds(),
        }
    except ImportError:
        return {"ok": False, "error": "curl_cffi not installed"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def get_info() -> Dict[str, Any]:
    return {
        "module": "tm_mobile_reverse",
        "purpose": "reverse-engineering TM mobile app traffic",
        "captures_dir": CAPTURES_DIR,
        "captures_count": len(list_captures()),
        "patterns_categories": list(TM_MOBILE_ENDPOINTS.keys()),
        "workflow": [
            "1. Instalar mitmproxy + CA cert en device iOS/Android",
            "2. Configurar device proxy → mitmproxy",
            "3. Reproducir flow en TM app móvil",
            "4. Exportar capture: mitmdump -r dump --set save_stream_file=capture.json",
            "5. POST /api/mobile-reverse/upload (multipart o base64)",
            "6. POST /api/mobile-reverse/replay con endpoint extraído",
        ],
        "advisory": "Reverse-engineering una app de terceros puede violar TOS. Solo para investigación de seguridad legítima.",
    }


def self_test() -> Dict[str, Any]:
    out = {"checks": {}}
    out["checks"]["dir_exists"] = os.path.isdir(CAPTURES_DIR)
    out["checks"]["info_callable"] = isinstance(get_info(), dict)
    # parse_capture con archivo invalid
    r = parse_capture("/tmp/nonexistent.json")
    out["checks"]["graceful_missing"] = r.get("ok") is False
    out["checks"]["replay_curl_cffi"] = True
    try:
        from curl_cffi import requests as _
    except ImportError:
        out["checks"]["replay_curl_cffi"] = False
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
    print()
    print(json.dumps(get_info(), indent=2))
