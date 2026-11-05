"""_trace_auth.py — Instrumentación CDP aislada para diagnóstico de /auth.

Diseño:
    - `AuthTracer` encapsula sesión CDP + listeners + buffers + exports.
    - `attach()` activa listeners y snapshot pre.
    - `detach()` desacopla listeners, hace snapshot post y exporta a disco.
    - Cleanup garantizado vía `__enter__`/`__exit__` o `detach()` en finally.
    - Activación por feature flag `TRACE_AUTH=1` (env var).

Uso típico:

    from _trace_auth import AuthTracer

    if os.environ.get('TRACE_AUTH') == '1':
        with AuthTracer(ctx, page, label='auth_diagnose') as tracer:
            # ...flow del bot que llega a /auth...
            time.sleep(40)
        # tracer.detach() llamado automático por __exit__
        # exports guardados en datos/diagnose/trace_auth_<ts>/

Garantías:
    - Listeners CDP siempre se desacoplan (incluso con exception).
    - Buffers se vacían a disco antes de retornar.
    - No leak de memoria si trace dura horas (capped a MAX_BUFFER por categoría).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ────────────────────────────────────────────────────────────────────────
#  CONSTANTES
# ────────────────────────────────────────────────────────────────────────
DEFAULT_OUT_BASE = "datos/diagnose"
MAX_BUFFER_REQUESTS = 5000        # cap para evitar leak en sesiones largas
MAX_BUFFER_RESPONSES = 5000
MAX_BUFFER_CONSOLE = 2000
MAX_BUFFER_EXCEPTIONS = 500
MAX_BUFFER_FAILED = 500
TRACE_ENV_FLAG = "TRACE_AUTH"


def trace_enabled() -> bool:
    """True si la flag TRACE_AUTH=1 está activa."""
    return os.environ.get(TRACE_ENV_FLAG) == "1"


# ────────────────────────────────────────────────────────────────────────
#  AuthTracer
# ────────────────────────────────────────────────────────────────────────
class AuthTracer:
    """Captura CDP aislada con cleanup garantizado.

    Args:
        ctx: BrowserContext de Playwright (CDP sesión se abrirá desde aquí).
        page: Page principal (se usa para snapshot pre/post).
        label: Prefijo del directorio de salida (`datos/diagnose/<label>_<ts>/`).
        out_base: Override del directorio base (default `datos/diagnose`).
        on_event: Callback opcional `(kind: str, data: dict) -> None`
                  para emitir cada evento al stream de logs del bot.
    """

    def __init__(self,
                 ctx,
                 page,
                 label: str = "trace_auth",
                 out_base: str = DEFAULT_OUT_BASE,
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.ctx = ctx
        self.page = page
        self.label = label
        self.on_event = on_event or (lambda kind, data: None)
        self._ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.out_dir = Path(out_base) / f"{label}_{self._ts}"

        # Buffers (capped)
        self.requests: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []
        self.console: List[Dict[str, Any]] = []
        self.exceptions: List[Dict[str, Any]] = []
        self.failed: List[Dict[str, Any]] = []
        self.redirects: List[Dict[str, Any]] = []

        # State
        self._cdp = None
        self._attached = False
        self._snap_pre: Optional[Dict[str, Any]] = None
        self._snap_post: Optional[Dict[str, Any]] = None
        self._handlers: List[tuple] = []  # [(event_name, handler), ...] para detach

    # ── Context manager ──────────────────────────────────────────────
    def __enter__(self) -> "AuthTracer":
        self.attach()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Cleanup garantizado incluso con exception
        try:
            self.detach()
        except Exception:
            # Ya estamos cleanup-ando; no podemos fallar más
            pass

    # ── Public API ───────────────────────────────────────────────────
    def attach(self) -> Dict[str, Any]:
        """Activa listeners CDP y captura snapshot pre.

        Returns:
            dict con `{ok: bool, out_dir: str, err: str?}`.
        """
        if self._attached:
            return {"ok": True, "out_dir": str(self.out_dir),
                    "note": "already_attached"}
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._cdp = self.ctx.new_cdp_session(self.page)
            self._cdp.send("Network.enable")
            self._cdp.send("Runtime.enable")

            # Registrar handlers (guardamos refs para detach correcto)
            handlers = [
                ("Network.requestWillBeSent", self._on_request),
                ("Network.responseReceived", self._on_response),
                ("Network.loadingFinished", self._on_finished),
                ("Network.loadingFailed", self._on_failed),
                ("Runtime.consoleAPICalled", self._on_console),
                ("Runtime.exceptionThrown", self._on_exception),
            ]
            for event_name, handler in handlers:
                self._cdp.on(event_name, handler)
                self._handlers.append((event_name, handler))

            self._snap_pre = self._capture_state("pre")
            self._save_json("pre_snapshot.json", self._snap_pre)

            self._attached = True
            self.on_event("trace_attached", {"out_dir": str(self.out_dir)})
            return {"ok": True, "out_dir": str(self.out_dir)}
        except Exception as e:
            return {"ok": False, "err": str(e)[:200],
                    "out_dir": str(self.out_dir)}

    def detach(self) -> Dict[str, Any]:
        """Desacopla listeners CDP, snapshot post + exports a disco.

        Idempotente: si ya está detached, no hace nada.

        Returns:
            dict con métricas del export y rutas de los ficheros.
        """
        if not self._attached:
            return {"ok": True, "note": "not_attached"}

        # 1) Capturar snapshot post ANTES de detach (page puede cerrarse después)
        try:
            self._snap_post = self._capture_state("post")
            self._save_json("post_snapshot.json", self._snap_post)
        except Exception as e:
            self.on_event("trace_post_snap_err", {"err": str(e)[:200]})

        # 2) HTML final + screenshot (best-effort)
        try:
            body = self.page.content()
            (self.out_dir / "post_body.html").write_text(body, encoding="utf-8")
        except Exception:
            pass
        try:
            self.page.screenshot(path=str(self.out_dir / "post.png"),
                                  full_page=True)
        except Exception:
            pass

        # 3) Desacoplar handlers (importante para evitar leaks)
        for event_name, handler in self._handlers:
            try:
                # Playwright CDP no tiene off() directo, pero asignar a None
                # rompe la ref. Como fallback, marcamos detached y los handlers
                # checkearán este flag.
                pass
            except Exception:
                pass
        self._attached = False

        # 4) Cerrar la sesión CDP (libera recursos browser-side)
        try:
            if self._cdp:
                self._cdp.detach()
        except Exception:
            # Algunos backends de Playwright no implementan detach() en CDPSession
            pass
        self._cdp = None

        # 5) Exports
        exports = {
            "requests": self.export_requests(),
            "responses": self.export_responses(),
            "console": self.export_console(),
            "exceptions": self.export_exceptions(),
            "failed_requests": self.export_failed_requests(),
            "redirects": self.export_redirects(),
        }
        # HAR opcional (combinación reqs+resps en formato HAR-like)
        try:
            har = self.export_har()
            self._save_json("export.har", har)
            exports["har"] = str(self.out_dir / "export.har")
        except Exception as e:
            exports["har_err"] = str(e)[:200]

        result = {
            "ok": True,
            "out_dir": str(self.out_dir),
            "counts": {
                "requests": len(self.requests),
                "responses": len(self.responses),
                "console": len(self.console),
                "exceptions": len(self.exceptions),
                "failed": len(self.failed),
                "redirects": len(self.redirects),
            },
            "exports": exports,
        }
        self.on_event("trace_detached", result["counts"])
        return result

    # ── Exports individuales ─────────────────────────────────────────
    def export_requests(self) -> str:
        return str(self._save_json("requests.json", self.requests))

    def export_responses(self) -> str:
        return str(self._save_json("responses.json", self.responses))

    def export_console(self) -> str:
        return str(self._save_json("console.json", self.console))

    def export_exceptions(self) -> str:
        return str(self._save_json("exceptions.json", self.exceptions))

    def export_failed_requests(self) -> str:
        return str(self._save_json("failed.json", self.failed))

    def export_redirects(self) -> str:
        return str(self._save_json("redirects.json", self.redirects))

    def export_har(self) -> Dict[str, Any]:
        """Combina requests + responses en formato HAR 1.2 minimal.

        No es HAR 100% compatible con DevTools (faltan timings detallados),
        pero es parseable por herramientas estándar (har-viewer, charles).
        """
        # Index responses by requestId for fast lookup
        resp_idx = {r["requestId"]: r for r in self.responses}
        entries = []
        for req in self.requests:
            rid = req["requestId"]
            resp = resp_idx.get(rid, {})
            ts_iso = datetime.fromtimestamp(req["ts_local"]).isoformat() + "Z"
            entries.append({
                "startedDateTime": ts_iso,
                "time": 0,
                "request": {
                    "method": req.get("method"),
                    "url": req.get("url"),
                    "httpVersion": "HTTP/2",
                    "headers": [{"name": k, "value": str(v)}
                                for k, v in (req.get("headers") or {}).items()],
                    "queryString": [],
                    "cookies": [],
                    "headersSize": -1,
                    "bodySize": -1,
                },
                "response": {
                    "status": resp.get("status", 0),
                    "statusText": resp.get("statusText", ""),
                    "httpVersion": resp.get("protocol", "HTTP/2"),
                    "headers": [{"name": k, "value": str(v)}
                                for k, v in (resp.get("headers") or {}).items()],
                    "cookies": [],
                    "content": {
                        "size": resp.get("encodedDataLength", 0),
                        "mimeType": resp.get("mimeType", ""),
                    },
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": resp.get("encodedDataLength", -1),
                },
                "cache": {},
                "timings": {"send": 0, "wait": 0, "receive": 0},
            })
        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "_trace_auth.py", "version": "1.0"},
                "pages": [],
                "entries": entries,
            }
        }

    # ── CDP handlers (privados) ──────────────────────────────────────
    def _on_request(self, p: Dict[str, Any]) -> None:
        if not self._attached:
            return
        try:
            rq = p.get("request") or {}
            # Detectar redirect
            if p.get("redirectResponse"):
                rr = p["redirectResponse"]
                if len(self.redirects) < MAX_BUFFER_FAILED:
                    self.redirects.append({
                        "requestId": p.get("requestId"),
                        "from_url": rr.get("url"),
                        "to_url": rq.get("url"),
                        "status": rr.get("status"),
                        "set_cookie": str((rr.get("headers") or {}).get("set-cookie", ""))[:500],
                        "location": (rr.get("headers") or {}).get("location", ""),
                        "ts": time.time(),
                    })
            if len(self.requests) >= MAX_BUFFER_REQUESTS:
                return
            self.requests.append({
                "requestId": p.get("requestId"),
                "url": rq.get("url"),
                "method": rq.get("method"),
                "headers": rq.get("headers") or {},
                "type": p.get("type"),
                "initiator_type": (p.get("initiator") or {}).get("type"),
                "documentURL": p.get("documentURL"),
                "ts_local": time.time(),
            })
        except Exception:
            pass  # nunca propagar excepción desde handler CDP

    def _on_response(self, p: Dict[str, Any]) -> None:
        if not self._attached:
            return
        try:
            if len(self.responses) >= MAX_BUFFER_RESPONSES:
                return
            r = p.get("response") or {}
            self.responses.append({
                "requestId": p.get("requestId"),
                "url": r.get("url"),
                "status": r.get("status"),
                "statusText": r.get("statusText"),
                "headers": r.get("headers") or {},
                "mimeType": r.get("mimeType"),
                "protocol": r.get("protocol"),
                "encodedDataLength": r.get("encodedDataLength"),
                "ts_local": time.time(),
            })
        except Exception:
            pass

    def _on_finished(self, p: Dict[str, Any]) -> None:
        # No bufferamos cada finished por defecto (mucho ruido).
        # Si necesitas tamaños, mira `encodedDataLength` en _on_response.
        pass

    def _on_failed(self, p: Dict[str, Any]) -> None:
        if not self._attached:
            return
        try:
            if len(self.failed) >= MAX_BUFFER_FAILED:
                return
            rid = p.get("requestId")
            url = None
            for r in reversed(self.requests):
                if r["requestId"] == rid:
                    url = r["url"]
                    break
            self.failed.append({
                "requestId": rid,
                "url": url,
                "errorText": p.get("errorText"),
                "canceled": p.get("canceled"),
                "blockedReason": p.get("blockedReason"),
                "ts_local": time.time(),
            })
        except Exception:
            pass

    def _on_console(self, p: Dict[str, Any]) -> None:
        if not self._attached:
            return
        try:
            if len(self.console) >= MAX_BUFFER_CONSOLE:
                return
            args = p.get("args", [])
            vals = []
            for a in args[:5]:
                v = a.get("value") if "value" in a else a.get("description", "")
                vals.append(str(v)[:200])
            self.console.append({
                "type": p.get("type"),
                "text": " ".join(vals),
                "url": (p.get("stackTrace", {}).get("callFrames", [{}])[0]).get("url", "")[:120],
                "ts": time.time(),
            })
        except Exception:
            pass

    def _on_exception(self, p: Dict[str, Any]) -> None:
        if not self._attached:
            return
        try:
            if len(self.exceptions) >= MAX_BUFFER_EXCEPTIONS:
                return
            ex = p.get("exceptionDetails", {})
            self.exceptions.append({
                "text": str(ex.get("text", ""))[:300],
                "url": str(ex.get("url", ""))[:200],
                "lineNumber": ex.get("lineNumber"),
                "description": str((ex.get("exception") or {}).get("description", ""))[:300],
                "ts": time.time(),
            })
        except Exception:
            pass

    # ── Snapshot helpers ─────────────────────────────────────────────
    def _capture_state(self, phase: str) -> Dict[str, Any]:
        snap: Dict[str, Any] = {"phase": phase, "ts": time.time()}
        try:
            snap["url"] = self.page.url
            snap["title"] = self.page.title()
        except Exception as e:
            snap["page_err"] = str(e)[:200]
        try:
            snap["cookies"] = self.ctx.cookies()
        except Exception as e:
            snap["cookies_err"] = str(e)[:200]
        try:
            snap["localStorage"] = self.page.evaluate("""() => {
                const r = {};
                try { for (let i=0; i<localStorage.length; i++) {
                    const k = localStorage.key(i); r[k] = localStorage.getItem(k);
                }} catch(e) { r._err = e.message; }
                return r;
            }""")
            snap["sessionStorage"] = self.page.evaluate("""() => {
                const r = {};
                try { for (let i=0; i<sessionStorage.length; i++) {
                    const k = sessionStorage.key(i); r[k] = sessionStorage.getItem(k);
                }} catch(e) { r._err = e.message; }
                return r;
            }""")
            snap["js_state"] = self.page.evaluate("""() => ({
                readyState: document.readyState,
                referrer: document.referrer,
                epsfBase: window.epsfBase || null,
                epsfToken: window.epsfToken ? window.epsfToken.slice(0, 40) : null,
                client_ip: window.client_ip || null,
                webdriver: navigator.webdriver,
                hidden_inputs: Array.from(document.querySelectorAll('input[type=hidden]'))
                    .map(el => ({name: el.name, value: (el.value||'').slice(0, 80)}))
                    .slice(0, 30),
            })""")
        except Exception as e:
            snap["storage_err"] = str(e)[:200]
        return snap

    def _save_json(self, name: str, data: Any) -> Path:
        path = self.out_dir / name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        return path


# ────────────────────────────────────────────────────────────────────────
#  CLI helper para uso standalone
# ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Self-test mínimo (no requiere browser real)
    print(f"trace_enabled (TRACE_AUTH={os.environ.get('TRACE_AUTH', '<unset>')}): "
          f"{trace_enabled()}")
    print("AuthTracer class:", AuthTracer.__name__)
    print("Public API:")
    for m in ("attach", "detach", "export_har", "export_console",
              "export_exceptions", "export_failed_requests",
              "export_requests", "export_responses", "export_redirects"):
        print(f"  - {m}() exists:", hasattr(AuthTracer, m))
