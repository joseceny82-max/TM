"""turnstile_solver.py — Cloudflare Turnstile auto-solver end-to-end.

Workflow:
  1. detect(page) — busca el widget Turnstile en la página
  2. solve(site_key, url) — pide token a CapSolver/2Captcha/AntiCaptcha
  3. inject(page, token) — inyecta cf-turnstile-response + dispara callback
  4. solve_and_inject(page) — orquesta detect → solve → inject

Selectores típicos Turnstile (2026):
  - <div class="cf-turnstile" data-sitekey="...">
  - <iframe src="https://challenges.cloudflare.com/cdn-cgi/...">
  - <input name="cf-turnstile-response" value="...">

API pública:
  detect(page) → {found, site_key, action, cdata, container_selector}
  solve_token(site_key, url, action=None, cdata=None) → {ok, token, elapsed_s}
  inject_token(page, token, container_selector=None) → {ok, fired_callback}
  solve_and_inject(page, url=None) → {ok, token, elapsed_total_s}
  get_stats() / self_test()
"""
from __future__ import annotations

import time
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List

import config
import json_atomic

_STATS_KEY = "turnstile_stats.json"
_LOG_KEY = "turnstile_log.json"
_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now().isoformat()


def _load_stats() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_STATS_KEY), default={
        "detections": 0,
        "solve_attempts": 0,
        "solve_success": 0,
        "inject_attempts": 0,
        "inject_success": 0,
        "total_elapsed_s": 0.0,
        "by_service": {},
        "last_solve_at": "",
        "last_error": "",
    })


def _save_stats(s: Dict[str, Any]):
    json_atomic.save(config.ruta(_STATS_KEY), s)


def _bump(key: str, by=1, service: Optional[str] = None,
          elapsed_s: Optional[float] = None, error: Optional[str] = None):
    with _lock:
        s = _load_stats()
        s[key] = s.get(key, 0) + by
        if elapsed_s is not None:
            s["total_elapsed_s"] = round(s.get("total_elapsed_s", 0.0) + elapsed_s, 2)
        if service:
            bs = s.setdefault("by_service", {}).setdefault(service, {
                "attempts": 0, "success": 0
            })
            bs["attempts"] += 1
            if key == "solve_success":
                bs["success"] += 1
        if key == "solve_success":
            s["last_solve_at"] = _now_iso()
        if error:
            s["last_error"] = error[:200]
        _save_stats(s)


def _append_log(entry: Dict[str, Any]):
    try:
        cur = json_atomic.load(config.ruta(_LOG_KEY), default={"entries": []})
        cur.setdefault("entries", []).insert(0, entry)
        cur["entries"] = cur["entries"][:200]
        json_atomic.save(config.ruta(_LOG_KEY), cur)
    except Exception:
        pass


def _emit_ws(event: str, payload: Dict[str, Any]):
    """Push a Socket.IO si dashboard está corriendo."""
    try:
        from dashboard import socketio  # type: ignore
        socketio.emit(event, payload)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  1. DETECT — busca widget Turnstile en página Playwright
# ═══════════════════════════════════════════════════════════════
DETECT_JS = r"""
() => {
    // Helper: itera doc raíz + todos los iframes accesibles (same-origin)
    const findInDoc = (doc, depth, source) => {
        try {
            // 1. Busca <div class="cf-turnstile">
            const div = doc.querySelector('.cf-turnstile, [data-sitekey][class*="turnstile" i]');
            if (div) return {
                found: true,
                site_key: div.getAttribute('data-sitekey') || '',
                action: div.getAttribute('data-action') || '',
                cdata: div.getAttribute('data-cdata') || '',
                container_selector: '.cf-turnstile',
                method: 'div_attr', frame_depth: depth, frame_source: source,
            };
            // 2. Iframe interno Turnstile
            const iframe = doc.querySelector('iframe[src*="challenges.cloudflare.com" i]');
            if (iframe) {
                const src = iframe.src || '';
                const m = src.match(/sitekey=([^&]+)/i);
                return {
                    found: true,
                    site_key: m ? decodeURIComponent(m[1]) : '',
                    action: '', cdata: '',
                    container_selector: 'iframe[src*="challenges.cloudflare.com"]',
                    method: 'iframe_src', frame_depth: depth, frame_source: source,
                };
            }
            // 3. Input dentro del doc
            const inp = doc.querySelector('input[name="cf-turnstile-response"]');
            if (inp) return {
                found: true, site_key: '', action: '', cdata: '',
                container_selector: 'input[name="cf-turnstile-response"]',
                method: 'input_only', frame_depth: depth, frame_source: source,
                note: 'input present — possibly hidden/JS-rendered',
            };
        } catch (e) { /* cross-origin doc skip */ }
        return null;
    };
    // 0. Doc raíz
    let r = findInDoc(document, 0, 'main');
    if (r) return r;
    // 0b. Shadow DOM traversal — sites Vercel/Next.js a veces renderizan
    // Turnstile en Shadow DOM (componentes web aislados).
    const traverseShadow = (root, depth) => {
        if (!root || depth > 4) return null;
        let found = findInDoc(root, depth, 'shadow_' + depth);
        if (found) {
            found.shadow_dom = true;
            return found;
        }
        // Busca elementos con shadowRoot
        try {
            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (let i = 0; i < all.length && i < 500; i++) {
                if (all[i].shadowRoot) {
                    const r2 = traverseShadow(all[i].shadowRoot, depth + 1);
                    if (r2) return r2;
                }
            }
        } catch (e) { /* skip */ }
        return null;
    };
    r = traverseShadow(document, 0);
    if (r) return r;
    // 4. Recorre iframes same-origin (Stripe, login flows, embeds)
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (let i = 0; i < iframes.length && i < 20; i++) {
        const fr = iframes[i];
        try {
            const doc = fr.contentDocument || (fr.contentWindow && fr.contentWindow.document);
            if (!doc) continue;
            r = findInDoc(doc, 1, fr.src || fr.name || ('iframe_' + i));
            if (r) return r;
            // Recursivo 1 nivel más (max 2 de profundidad)
            const nested = doc.querySelectorAll('iframe');
            for (let j = 0; j < nested.length && j < 10; j++) {
                try {
                    const ndoc = nested[j].contentDocument
                                  || (nested[j].contentWindow && nested[j].contentWindow.document);
                    if (!ndoc) continue;
                    r = findInDoc(ndoc, 2, nested[j].src || ('nested_' + i + '_' + j));
                    if (r) return r;
                } catch (e) { /* cross-origin */ }
            }
        } catch (e) { /* cross-origin iframe — skip */ }
    }
    // 5. Window.turnstile global (renderiza después)
    if (typeof window.turnstile !== 'undefined') {
        return {
            found: true, site_key: '', action: '', cdata: '',
            container_selector: '',
            method: 'window_global', frame_depth: 0, frame_source: 'main',
            note: 'window.turnstile detected but no widget rendered yet',
        };
    }
    return { found: false };
}
"""


def detect(page) -> Dict[str, Any]:
    """Detecta widget Turnstile en una página Playwright. Devuelve metadata."""
    if page is None:
        return {"found": False, "error": "no_page"}
    try:
        result = page.evaluate(DETECT_JS)
        if result.get("found"):
            _bump("detections")
            _append_log({"ts": _now_iso(), "event": "detected",
                          "site_key": result.get("site_key", "")[:50],
                          "method": result.get("method")})
        return result or {"found": False}
    except Exception as e:
        return {"found": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══════════════════════════════════════════════════════════════
#  2. SOLVE — pide token a CapSolver/2Captcha
# ═══════════════════════════════════════════════════════════════
def solve_token(site_key: str, url: str,
                action: Optional[str] = None,
                cdata: Optional[str] = None,
                preferred_service: Optional[str] = None) -> Dict[str, Any]:
    """Pide token Turnstile al servicio CAPTCHA configurado.

    Soporta CapSolver (AntiTurnstileTaskProxyless) y 2Captcha (turnstile method).
    Action y cdata son opcionales (Turnstile ProtoActions feature).
    """
    if not site_key or not url:
        return {"ok": False, "error": "site_key_and_url_required"}
    started = time.time()
    try:
        import captcha_manager
    except ImportError:
        return {"ok": False, "error": "captcha_manager_missing"}

    _bump("solve_attempts")
    try:
        # captcha_manager.solve_turnstile no acepta action/cdata todavía,
        # extendemos el task para CapSolver si los pasamos.
        if action or cdata:
            # Llamada directa que sí soporta extras
            api_key = captcha_manager._api_key("capsolver")
            if api_key:
                task = {
                    "type": "AntiTurnstileTaskProxyless",
                    "websiteKey": site_key,
                    "websiteURL": url,
                }
                if action:
                    task["action"] = action
                if cdata:
                    task["cdata"] = cdata
                r = captcha_manager._solve_with_capsolver(task, "turnstile")
            else:
                r = captcha_manager.solve_turnstile(site_key, url)
        else:
            r = captcha_manager.solve_turnstile(site_key, url)

        elapsed = time.time() - started
        ok = bool(r.get("ok"))
        token = r.get("token") or r.get("solution") or ""
        if ok and token:
            _bump("solve_success", service=r.get("service"), elapsed_s=elapsed)
            _append_log({"ts": _now_iso(), "event": "solved",
                          "service": r.get("service"),
                          "elapsed_s": round(elapsed, 2),
                          "token_preview": token[:30]})
            return {"ok": True, "token": token,
                     "service": r.get("service"),
                     "elapsed_s": round(elapsed, 2)}
        # Fallo
        err = r.get("error", "unknown")
        _bump("solve_attempts", by=0, error=err)
        _append_log({"ts": _now_iso(), "event": "solve_failed",
                      "error": err[:200],
                      "elapsed_s": round(elapsed, 2)})
        return {"ok": False, "error": err,
                 "elapsed_s": round(elapsed, 2)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══════════════════════════════════════════════════════════════
#  3. INJECT — inyecta token en página + dispara callback
# ═══════════════════════════════════════════════════════════════
INJECT_JS = r"""
(payload) => {
    const { token, container_selector } = payload;
    let injected = 0;
    let fired_callback = false;
    // 1. Setea TODOS los inputs cf-turnstile-response
    const inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
    inputs.forEach(inp => {
        inp.value = token;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
        injected++;
    });
    // 2. Si window.turnstile existe, dispara callback registrado
    try {
        if (typeof window.turnstile !== 'undefined') {
            // Intenta encontrar widget IDs y disparar render callback
            const containers = document.querySelectorAll('.cf-turnstile, [data-sitekey]');
            containers.forEach(c => {
                const cb = c.getAttribute('data-callback');
                if (cb && typeof window[cb] === 'function') {
                    try { window[cb](token); fired_callback = true; } catch(e) {}
                }
                // Algunos widgets usan callback inline en data-callback="onTurnstileSuccess"
                const onCb = c.dataset.callback;
                if (onCb && window[onCb] && typeof window[onCb] === 'function') {
                    try { window[onCb](token); fired_callback = true; } catch(e) {}
                }
            });
        }
    } catch(e) {}
    // 3. Si hay un form, intenta también poner como hidden field
    const forms = document.querySelectorAll('form');
    forms.forEach(f => {
        let h = f.querySelector('input[name="cf-turnstile-response"]');
        if (!h) {
            h = document.createElement('input');
            h.type = 'hidden';
            h.name = 'cf-turnstile-response';
            h.value = token;
            f.appendChild(h);
            injected++;
        } else if (!h.value) {
            h.value = token;
            injected++;
        }
    });
    return { injected, fired_callback, inputs_count: inputs.length };
}
"""


def inject_token(page, token: str,
                  container_selector: Optional[str] = None) -> Dict[str, Any]:
    """Inyecta el token Turnstile en la página y dispara callback si existe."""
    if page is None or not token:
        return {"ok": False, "error": "page_and_token_required"}
    _bump("inject_attempts")
    try:
        result = page.evaluate(INJECT_JS, {
            "token": token,
            "container_selector": container_selector or "",
        })
        ok = result and (result.get("injected", 0) > 0 or result.get("fired_callback"))
        if ok:
            _bump("inject_success")
        _append_log({"ts": _now_iso(), "event": "injected" if ok else "inject_failed",
                      "injected": result.get("injected", 0),
                      "fired_callback": result.get("fired_callback", False)})
        return {"ok": bool(ok),
                "injected": result.get("injected", 0),
                "fired_callback": result.get("fired_callback", False),
                "inputs_count": result.get("inputs_count", 0)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══════════════════════════════════════════════════════════════
#  4. ORCHESTRATOR — detect → solve → inject
# ═══════════════════════════════════════════════════════════════
def solve_and_inject(page, url: Optional[str] = None,
                       max_wait_render_s: int = 10) -> Dict[str, Any]:
    """End-to-end: detecta, resuelve, inyecta. Devuelve resumen."""
    started = time.time()
    if page is None:
        return {"ok": False, "error": "no_page"}

    # 1. Detecta — espera hasta max_wait para que el widget renderice
    deadline = time.time() + max_wait_render_s
    det = {}
    while time.time() < deadline:
        det = detect(page)
        if det.get("found") and det.get("site_key"):
            break
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    if not det.get("found") or not det.get("site_key"):
        # Métrica edge-case: contador de DOM cambios — early warning
        _bump("no_widget_or_no_sitekey", error="DOM detection missed widget")
        try:
            with _lock:
                s = _load_stats()
                # Alarma si los misses crecen rápido (>5 en última hora)
                history = s.setdefault("no_widget_history", [])
                history.append(_now_iso())
                # Limpiar entradas >1h
                cutoff = time.time() - 3600
                history = [h for h in history if
                            datetime.fromisoformat(h).timestamp() > cutoff]
                s["no_widget_history"] = history[-200:]
                s["no_widget_last_1h"] = len(history)
                if len(history) >= 5 and not s.get("dom_change_alert_sent"):
                    s["dom_change_alert_sent"] = _now_iso()
                    s["dom_change_alert_active"] = True
                    # WS push live al dispararse alarma
                    _emit_ws("turnstile.dom_alert", {
                        "alert_at": s["dom_change_alert_sent"],
                        "misses_last_1h": len(history),
                        "hint": ("Cloudflare cambió DOM — actualiza selectors "
                                  "en turnstile_solver.detect() o llama a "
                                  "suggest_selectors_via_llm()."),
                    })
                _save_stats(s)
        except Exception:
            pass
        return {"ok": False, "error": "no_widget_or_no_sitekey",
                 "detect_result": det,
                 "elapsed_total_s": round(time.time() - started, 2)}

    site_key = det["site_key"]
    page_url = url or page.url

    # 2. Solve
    sol = solve_token(site_key, page_url,
                       action=det.get("action"), cdata=det.get("cdata"))
    if not sol.get("ok"):
        return {"ok": False, "error": "solve_failed",
                 "solve_result": sol, "detect_result": det,
                 "elapsed_total_s": round(time.time() - started, 2)}

    token = sol["token"]

    # 3. Inject
    inj = inject_token(page, token,
                       container_selector=det.get("container_selector"))
    if not inj.get("ok"):
        return {"ok": False, "error": "inject_failed",
                 "inject_result": inj, "solve_result": sol,
                 "elapsed_total_s": round(time.time() - started, 2)}

    return {
        "ok": True,
        "token": token,
        "site_key": site_key,
        "service": sol.get("service"),
        "elapsed_solve_s": sol.get("elapsed_s"),
        "elapsed_total_s": round(time.time() - started, 2),
        "detect_method": det.get("method"),
        "inject_count": inj.get("injected"),
        "fired_callback": inj.get("fired_callback"),
    }


# ═══════════════════════════════════════════════════════════════
#  5. STATS / SELF-TEST
# ═══════════════════════════════════════════════════════════════
def get_stats() -> Dict[str, Any]:
    s = _load_stats()
    s["solve_success_rate_pct"] = round(
        100 * s.get("solve_success", 0) / max(1, s.get("solve_attempts", 0)), 1)
    s["inject_success_rate_pct"] = round(
        100 * s.get("inject_success", 0) / max(1, s.get("inject_attempts", 0)), 1)
    return s


def get_log(limit: int = 50) -> List[Dict[str, Any]]:
    try:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"entries": []})
        return d.get("entries", [])[:limit]
    except Exception:
        return []


def self_test() -> Dict[str, Any]:
    """Test sin Playwright real — valida estructura módulo + JS string + stats."""
    out = {"ok": True, "checks": {}}
    # 1. JS strings parseables (no syntax error obvio)
    out["checks"]["detect_js_present"] = (
        "querySelector(.cf-turnstile" in DETECT_JS.replace("'", "")[:300]
        or "cf-turnstile" in DETECT_JS)
    out["checks"]["inject_js_present"] = "cf-turnstile-response" in INJECT_JS
    # 2. Funciones públicas exponen ok=False con argumentos vacíos
    r1 = solve_token("", "")
    out["checks"]["solve_token_validates_args"] = (r1.get("ok") is False)
    r2 = inject_token(None, "")
    out["checks"]["inject_token_validates_args"] = (r2.get("ok") is False)
    r3 = detect(None)
    out["checks"]["detect_validates_page"] = (r3.get("found") is False)
    r4 = solve_and_inject(None)
    out["checks"]["orchestrator_validates_page"] = (r4.get("ok") is False)
    # 3. Stats inicializa con todas las claves
    s = get_stats()
    needed = {"detections", "solve_attempts", "solve_success", "inject_attempts",
               "inject_success", "solve_success_rate_pct"}
    out["checks"]["stats_keys_complete"] = needed.issubset(s.keys())
    # 4. captcha_manager.solve_turnstile existe (dependencia)
    try:
        import captcha_manager
        out["checks"]["captcha_manager_has_turnstile"] = hasattr(
            captcha_manager, "solve_turnstile")
    except ImportError:
        out["checks"]["captcha_manager_has_turnstile"] = False
    out["passed"] = sum(1 for v in out["checks"].values() if v)
    out["total"] = len(out["checks"])
    out["ok"] = out["passed"] == out["total"]
    return out


def test_against_demo(demo_site_key: str = "1x00000000000000000000AA",
                       demo_url: str = "https://www.turnstile-demo.com/") -> Dict[str, Any]:
    """Test live: pide token Turnstile contra una demo pública.

    Cloudflare ofrece site keys de testing oficiales:
      - 1x00000000000000000000AA → siempre passes (visible)
      - 1x00000000000000000000BB → siempre passes (invisible)
      - 2x00000000000000000000AB → siempre fails
      - 3x00000000000000000000FF → forces interactive challenge

    Por defecto usa la primera (passes). Útil para validar que el solver
    pipeline funciona sin tocar TM real (sin gastar saldo en sites de prod).

    Refs:
      https://developers.cloudflare.com/turnstile/troubleshooting/testing/
    """
    started = time.time()
    r = solve_token(site_key=demo_site_key, url=demo_url)
    elapsed = time.time() - started
    return {
        "ok": r.get("ok", False),
        "test_type": "demo_token_only",
        "demo_site_key": demo_site_key,
        "demo_url": demo_url,
        "token_obtained": bool(r.get("token")),
        "service": r.get("service"),
        "elapsed_total_s": round(elapsed, 2),
        "raw": r,
        "note": ("Para test full (detect+inject) abre la URL demo con "
                 "Playwright y llama solve_and_inject(page) manualmente."),
    }


def suggest_selectors_via_llm(page=None,
                                 html_snippet: Optional[str] = None,
                                 screenshot_path: Optional[str] = None,
                                 model: str = "claude-3-5-sonnet-20241022") -> Dict[str, Any]:
    """Cuando se dispara dom_change_alert_active, llama a Claude/GPT con
    el screenshot + HTML para sugerir nuevos selectors Turnstile.

    Args:
      page: Playwright page (preferido — captura screenshot + HTML auto)
      html_snippet: HTML a analizar si no hay page
      screenshot_path: ruta a screenshot existente
      model: anthropic/openai model id

    Devuelve {ok, suggested_selectors:[...], reasoning, cost_estimate_usd}.
    """
    started = time.time()
    # 1. Captura screenshot + HTML si tenemos page
    snapshot_html = html_snippet or ""
    snapshot_b64 = ""
    if page is not None:
        try:
            snapshot_html = page.content()
            png = page.screenshot(full_page=False, type="png")
            import base64 as _b64
            snapshot_b64 = _b64.b64encode(png).decode()
        except Exception as e:
            return {"ok": False, "error": f"capture_failed: {e}"}
    elif screenshot_path:
        try:
            import base64 as _b64
            with open(screenshot_path, "rb") as f:
                snapshot_b64 = _b64.b64encode(f.read()).decode()
        except Exception as e:
            return {"ok": False, "error": f"file_read_failed: {e}"}

    if not snapshot_html and not snapshot_b64:
        return {"ok": False, "error": "no_input",
                 "hint": "Provide page, html_snippet, or screenshot_path"}

    # 2. Construye prompt
    prompt = ("Analiza este HTML/screenshot de una página con Cloudflare "
                "Turnstile. Los selectors actuales fallan al detectar el "
                "widget. Sugiere selectors CSS/XPath alternativos que aún "
                "funcionen. Responde JSON: "
                '{"selectors":["...","..."], "reasoning":"...", "confidence":0-1}\n\n'
                "Selectors que actualmente fallan:\n"
                "- .cf-turnstile, [data-sitekey]\n"
                "- iframe[src*=\"challenges.cloudflare.com\"]\n"
                "- input[name=\"cf-turnstile-response\"]\n\n"
                f"HTML (primeros 8KB):\n{snapshot_html[:8000]}")

    # 3. Llama al LLM (lazy import — opcional)
    try:
        if "claude" in model.lower():
            return _ask_anthropic(model, prompt, snapshot_b64, started)
        return _ask_openai(model, prompt, snapshot_b64, started)
    except ImportError as e:
        return {"ok": False, "error": f"llm_lib_missing: {e}",
                 "hint": "pip install anthropic or openai"}
    except Exception as e:
        return {"ok": False,
                 "error": f"{type(e).__name__}: {str(e)[:300]}",
                 "elapsed_s": round(time.time() - started, 2)}


def _ask_anthropic(model: str, prompt: str, image_b64: str,
                     started: float) -> Dict[str, Any]:
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY missing",
                 "hint": "export ANTHROPIC_API_KEY=sk-ant-..."}
    try:
        import anthropic
    except ImportError:
        return {"ok": False, "error": "anthropic_not_installed"}
    client = anthropic.Anthropic(api_key=api_key)
    content = [{"type": "text", "text": prompt}]
    if image_b64:
        content.insert(0, {"type": "image",
                             "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": image_b64}})
    msg = client.messages.create(
        model=model, max_tokens=600,
        messages=[{"role": "user", "content": content}])
    response_text = msg.content[0].text if msg.content else ""
    parsed = _try_parse_llm_json(response_text)
    return {
        "ok": True,
        "model": model,
        "suggested_selectors": parsed.get("selectors", []),
        "reasoning": parsed.get("reasoning", "")[:500],
        "confidence": parsed.get("confidence", 0),
        "raw_response": response_text[:2000],
        "elapsed_s": round(time.time() - started, 2),
        "cost_estimate_usd": round(0.003 * (msg.usage.input_tokens / 1000) +
                                       0.015 * (msg.usage.output_tokens / 1000), 4),
    }


def _ask_openai(model: str, prompt: str, image_b64: str,
                  started: float) -> Dict[str, Any]:
    import os
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY missing"}
    try:
        from openai import OpenAI
    except ImportError:
        return {"ok": False, "error": "openai_not_installed"}
    client = OpenAI(api_key=api_key)
    content = [{"type": "text", "text": prompt}]
    if image_b64:
        content.append({"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{image_b64}"}})
    resp = client.chat.completions.create(
        model=model, max_tokens=600,
        messages=[{"role": "user", "content": content}])
    response_text = resp.choices[0].message.content or ""
    parsed = _try_parse_llm_json(response_text)
    return {
        "ok": True, "model": model,
        "suggested_selectors": parsed.get("selectors", []),
        "reasoning": parsed.get("reasoning", "")[:500],
        "confidence": parsed.get("confidence", 0),
        "raw_response": response_text[:2000],
        "elapsed_s": round(time.time() - started, 2),
    }


def _try_parse_llm_json(text: str) -> Dict[str, Any]:
    """Intenta extraer un JSON de la respuesta del LLM."""
    import json as _json
    import re as _re
    # Busca {...} balanceado
    m = _re.search(r'\{[^{}]*"selectors"[^{}]*\}', text, _re.DOTALL)
    if not m:
        m = _re.search(r'\{.*?\}', text, _re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(0))
        except _json.JSONDecodeError:
            pass
    return {"selectors": [], "reasoning": text[:300], "confidence": 0}


def apply_suggested_selectors(selectors: List[str],
                                 dry_run: bool = False) -> Dict[str, Any]:
    """Aplica los selectors sugeridos por el LLM al DETECT_JS del módulo.

    Hace backup del archivo .py original (timestamp suffix) antes de modificar.
    Inserta los nuevos selectors como branches PRIORITARIOS en el querySelector
    al inicio del JS para que se prueben primero.

    Args:
      selectors: lista de selectors CSS sugeridos por el LLM
      dry_run: si True devuelve el patch propuesto sin escribir disco
    """
    import os
    if not selectors:
        return {"ok": False, "error": "no_selectors_provided"}
    module_path = os.path.abspath(__file__)
    try:
        with open(module_path, "r", encoding="utf-8") as f:
            current = f.read()
    except Exception as e:
        return {"ok": False, "error": f"read_failed: {e}"}

    # Construye el bloque JS: prueba los nuevos selectors PRIMERO
    new_branch_lines = []
    for i, sel in enumerate(selectors):
        sel_safe = sel.replace("'", "\\'").replace("\\", "\\\\")
        new_branch_lines.append(
            f"    // LLM-suggested {i+1}\n"
            f"    const llm_{i} = document.querySelector('{sel_safe}');\n"
            f"    if (llm_{i}) {{\n"
            f"        return {{ found: true, "
            f"site_key: llm_{i}.getAttribute('data-sitekey') || '', "
            f"action: llm_{i}.getAttribute('data-action') || '', "
            f"cdata: llm_{i}.getAttribute('data-cdata') || '', "
            f"container_selector: '{sel_safe}', "
            f"method: 'llm_suggested_{i+1}' }};\n"
            f"    }}")
    new_branch = "\n".join(new_branch_lines)

    # Localiza el bloque DETECT_JS y inyecta el nuevo branch al inicio
    # del cuerpo de la función (justo después de `() => {`).
    marker = '() => {\n    // Helper: itera doc raíz'
    if marker not in current:
        # Fallback al marker antiguo (compat con versiones previas)
        marker_old = '() => {\n    // 1. Busca <div class="cf-turnstile">'
        if marker_old in current:
            marker = marker_old
        else:
            return {"ok": False, "error": "marker_not_found_in_DETECT_JS",
                     "hint": "Estructura del módulo cambió — revisar manualmente"}
    new_marker = (f'() => {{\n'
                   f'    // ── LLM-suggested selectors (priority) ──\n'
                   f'{new_branch}\n\n'
                   f'{marker[len("() => {"):]}')
    new_content = current.replace(marker, new_marker, 1)

    # Backup del original
    backup_path = ""
    if not dry_run:
        backup_path = (module_path
                        + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(current)
        except Exception as e:
            return {"ok": False, "error": f"backup_failed: {e}"}
        try:
            with open(module_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return {"ok": False, "error": f"write_failed: {e}",
                     "backup_path": backup_path}

    return {
        "ok": True,
        "dry_run": dry_run,
        "backup_path": backup_path,
        "selectors_added": selectors,
        "new_size": len(new_content),
        "old_size": len(current),
        "patch_preview": new_branch,
        "note": ("Reinicia dashboard para que el cambio surta efecto. "
                 "Si el detect sigue fallando, restaura del backup_path.")
        if not dry_run else "Dry run — no se escribió nada.",
    }


def list_backups() -> List[Dict[str, Any]]:
    """Lista los backups del módulo (turnstile_solver.py.bak.*) con metadata."""
    import os
    import glob
    module_path = os.path.abspath(__file__)
    pattern = module_path + ".bak.*"
    out = []
    for p in sorted(glob.glob(pattern), reverse=True):
        try:
            st = os.stat(p)
            out.append({
                "path": p,
                "filename": os.path.basename(p),
                "size_bytes": st.st_size,
                "mtime_iso": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "age_seconds": int(time.time() - st.st_mtime),
            })
        except Exception:
            continue
    return out


def restore_backup(backup_filename: str) -> Dict[str, Any]:
    """Restaura un backup al módulo actual.

    Args:
      backup_filename: nombre del archivo (no path completo) por seguridad.
        Debe matchear `turnstile_solver.py.bak.*` y existir en el dir del módulo.
    """
    import os
    module_path = os.path.abspath(__file__)
    module_dir = os.path.dirname(module_path)
    bn = os.path.basename(backup_filename)
    expected_prefix = "turnstile_solver.py.bak."
    if not bn.startswith(expected_prefix):
        return {"ok": False, "error": "invalid_backup_filename",
                 "hint": f"must start with {expected_prefix}"}
    backup_path = os.path.join(module_dir, bn)
    if not os.path.exists(backup_path):
        return {"ok": False, "error": "backup_not_found"}
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            backup_content = f.read()
        # Backup del actual antes de restaurar (rollback safety)
        rescue_path = (module_path
                        + f".rescue.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        with open(module_path, "r", encoding="utf-8") as f:
            current = f.read()
        with open(rescue_path, "w", encoding="utf-8") as f:
            f.write(current)
        # Restaurar
        with open(module_path, "w", encoding="utf-8") as f:
            f.write(backup_content)
        return {"ok": True, "restored_from": backup_path,
                 "rescue_path": rescue_path,
                 "restored_size": len(backup_content),
                 "note": "Reinicia dashboard para aplicar cambios."}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def cleanup_old_backups(keep_last: int = 10,
                          keep_days: int = 30,
                          dry_run: bool = False) -> Dict[str, Any]:
    """Auto-prune de backups: conserva los últimos N + los recientes (días).

    Reglas (un backup se preserva si cumple CUALQUIERA):
      - Está entre los `keep_last` más recientes
      - Su mtime es < `keep_days` días
    El resto se borra (salvo dry_run=True).
    """
    import os
    backups = list_backups()
    if not backups:
        return {"ok": True, "kept": 0, "deleted": 0, "skipped": 0,
                 "message": "no_backups_found"}
    cutoff_age = keep_days * 86400
    keep_paths = set()
    # Conservar los keep_last más recientes (ya están ordenados desc por mtime)
    for b in backups[:keep_last]:
        keep_paths.add(b["path"])
    # Conservar los recientes por edad
    for b in backups:
        if b.get("age_seconds", 0) < cutoff_age:
            keep_paths.add(b["path"])
    deleted = []
    skipped = []
    for b in backups:
        if b["path"] in keep_paths:
            skipped.append(b["filename"])
            continue
        if dry_run:
            deleted.append(b["filename"])
            continue
        try:
            os.remove(b["path"])
            deleted.append(b["filename"])
        except Exception:
            continue
    return {
        "ok": True,
        "dry_run": dry_run,
        "kept": len(skipped),
        "deleted": len(deleted),
        "deleted_files": deleted,
        "rules": {"keep_last": keep_last, "keep_days": keep_days},
    }


def diff_backup_vs_current(backup_filename: str,
                              context_lines: int = 3) -> Dict[str, Any]:
    """Compara unified diff entre un backup y la versión actual del módulo.

    Útil para revisar exactamente QUÉ cambia el restore antes de ejecutarlo.
    """
    import os
    import difflib
    module_path = os.path.abspath(__file__)
    module_dir = os.path.dirname(module_path)
    bn = os.path.basename(backup_filename)
    if not bn.startswith(("turnstile_solver.py.bak.",
                            "turnstile_solver.py.rescue.")):
        return {"ok": False, "error": "invalid_backup_filename"}
    backup_path = os.path.join(module_dir, bn)
    if not os.path.exists(backup_path):
        return {"ok": False, "error": "backup_not_found"}
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            backup_lines = f.read().splitlines(keepends=True)
        with open(module_path, "r", encoding="utf-8") as f:
            current_lines = f.read().splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            backup_lines, current_lines,
            fromfile=f"backup ({bn})",
            tofile="current (turnstile_solver.py)",
            n=context_lines))
        # Resumen
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        return {
            "ok": True,
            "backup_filename": bn,
            "diff": "".join(diff),
            "stats": {
                "added_lines": added,
                "removed_lines": removed,
                "net_change": added - removed,
                "backup_lines": len(backup_lines),
                "current_lines": len(current_lines),
            },
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def delete_backup(backup_filename: str) -> Dict[str, Any]:
    """Borra un backup específico (security: solo del prefijo válido)."""
    import os
    module_dir = os.path.dirname(os.path.abspath(__file__))
    bn = os.path.basename(backup_filename)
    if not bn.startswith(("turnstile_solver.py.bak.",
                            "turnstile_solver.py.rescue.")):
        return {"ok": False, "error": "invalid_backup_filename"}
    backup_path = os.path.join(module_dir, bn)
    if not os.path.exists(backup_path):
        return {"ok": False, "error": "backup_not_found"}
    try:
        os.remove(backup_path)
        return {"ok": True, "deleted": bn}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


_backup_scheduler_state = {"running": False, "thread": None,
                              "stop_event": None, "started_at": "",
                              "ticks": 0, "last_run": "",
                              "last_deleted": 0,
                              "keep_last": 10, "keep_days": 30}


def _backup_cleanup_loop(stop_evt):
    """Cada noche entre 03:00-03:59 ejecuta cleanup_old_backups."""
    last_cleanup_date = ""
    while not stop_evt.is_set():
        try:
            now = datetime.now()
            if now.hour == 3:
                today = now.date().isoformat()
                if last_cleanup_date != today:
                    r = cleanup_old_backups(
                        keep_last=_backup_scheduler_state["keep_last"],
                        keep_days=_backup_scheduler_state["keep_days"],
                        dry_run=False)
                    last_cleanup_date = today
                    _backup_scheduler_state["last_run"] = now.isoformat()
                    _backup_scheduler_state["last_deleted"] = r.get("deleted", 0)
            _backup_scheduler_state["ticks"] += 1
        except Exception:
            pass
        # Sleep 1 hora (chunks de 60s para parada rápida)
        for _ in range(3600):
            if stop_evt.is_set():
                break
            time.sleep(1)


def start_backup_scheduler(keep_last: int = 10, keep_days: int = 30) -> Dict[str, Any]:
    """Daemon que ejecuta cleanup_old_backups cada noche 03:00."""
    if _backup_scheduler_state.get("running"):
        return {"ok": True, "status": "already_running"}
    _backup_scheduler_state["keep_last"] = max(1, int(keep_last))
    _backup_scheduler_state["keep_days"] = max(1, int(keep_days))
    evt = threading.Event()
    th = threading.Thread(target=_backup_cleanup_loop, args=(evt,),
                           daemon=True, name="turnstile_backup_scheduler")
    _backup_scheduler_state.update({
        "running": True, "thread": th, "stop_event": evt,
        "started_at": _now_iso(),
        "ticks": 0,
    })
    th.start()
    return {"ok": True, "status": "started",
             "schedule": "Cada noche 03:00 (chequeo horario)",
             "keep_last": _backup_scheduler_state["keep_last"],
             "keep_days": _backup_scheduler_state["keep_days"]}


def stop_backup_scheduler() -> Dict[str, Any]:
    if not _backup_scheduler_state.get("running"):
        return {"ok": True, "status": "not_running"}
    evt = _backup_scheduler_state.get("stop_event")
    if evt:
        evt.set()
    _backup_scheduler_state["running"] = False
    return {"ok": True, "status": "stopped"}


def backup_scheduler_status() -> Dict[str, Any]:
    return {
        "running": _backup_scheduler_state.get("running", False),
        "started_at": _backup_scheduler_state.get("started_at", ""),
        "ticks": _backup_scheduler_state.get("ticks", 0),
        "last_run": _backup_scheduler_state.get("last_run", ""),
        "last_deleted": _backup_scheduler_state.get("last_deleted", 0),
        "keep_last": _backup_scheduler_state.get("keep_last", 10),
        "keep_days": _backup_scheduler_state.get("keep_days", 30),
        "schedule": "Cada noche 03:00 (chequeo horario)",
    }


def reset_dom_alert() -> Dict[str, Any]:
    """Limpia la alarma de DOM change (tras revisar y actualizar selectors)."""
    with _lock:
        s = _load_stats()
        s["dom_change_alert_active"] = False
        s["dom_change_alert_sent"] = ""
        s["no_widget_history"] = []
        s["no_widget_last_1h"] = 0
        _save_stats(s)
    return {"ok": True, "reset_at": _now_iso()}


def test_e2e_playwright(demo_url: str = "https://www.turnstile-demo.com/",
                          headless: bool = True,
                          submit_form: bool = False,
                          timeout_s: int = 60) -> Dict[str, Any]:
    """Test end-to-end real con Playwright: launch → goto demo → solve_and_inject.

    Si submit_form=True, intenta hacer submit del form y verificar respuesta.
    Devuelve resumen con tiempos, token, success del form (si aplica).

    Requiere: playwright + browser instalado (chromium).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright_not_installed"}
    started = time.time()
    out: Dict[str, Any] = {"demo_url": demo_url, "headless": headless,
                            "submit_form": submit_form}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless,
                                          args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0 Safari/537.36"),
                locale="en-US",
            )
            page = ctx.new_page()
            try:
                t_goto = time.time()
                page.goto(demo_url, timeout=timeout_s * 1000,
                            wait_until="domcontentloaded")
                out["goto_elapsed_s"] = round(time.time() - t_goto, 2)
                # Espera a que el widget renderice
                page.wait_for_timeout(2500)
                # solve_and_inject
                t_solve = time.time()
                e2e = solve_and_inject(page, url=demo_url,
                                          max_wait_render_s=15)
                out["solve_and_inject"] = e2e
                out["solve_elapsed_s"] = round(time.time() - t_solve, 2)
                # Verifica el input cf-turnstile-response
                try:
                    has_token = page.evaluate(
                        '() => { const i = document.querySelector(\'input[name="cf-turnstile-response"]\'); '
                        'return i && i.value && i.value.length > 0; }')
                    out["input_has_token"] = bool(has_token)
                except Exception:
                    out["input_has_token"] = False
                # Submit form opcional
                if submit_form and e2e.get("ok"):
                    try:
                        page.evaluate('() => { const f = document.querySelector("form"); if(f) f.submit(); }')
                        page.wait_for_timeout(3000)
                        body_after = page.evaluate(
                            '() => document.body.innerText.toLowerCase().slice(0, 800)')
                        out["form_body_after"] = body_after[:300]
                        out["form_success"] = any(m in body_after for m in
                                                    ("success", "verified", "passed",
                                                     "valid", "thank you"))
                    except Exception as e:
                        out["form_error"] = str(e)[:200]
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        out["ok"] = bool(out.get("solve_and_inject", {}).get("ok"))
        out["total_elapsed_s"] = round(time.time() - started, 2)
        return out
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["total_elapsed_s"] = round(time.time() - started, 2)
        return out


def get_info() -> Dict[str, Any]:
    return {
        "module": "turnstile_solver",
        "version": "1.0",
        "stats": get_stats(),
        "supported_services": ["capsolver", "2captcha", "anti-captcha", "capmonster"],
        "endpoints": [
            "POST /api/turnstile/solve",
            "POST /api/turnstile/inject (CDP-attached page only — usado vía bot)",
            "GET /api/turnstile/stats",
            "GET /api/turnstile/log",
            "POST /api/turnstile/self-test",
        ],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=2))
