"""llm_captcha.py — LLM multimodal CAPTCHA solver (GPT-4o, Claude, Gemini).

Fallback inteligente cuando CapSolver/2Captcha fallan.
Gemini 1.5 Pro es líder en visual reasoning abril 2026.

Providers soportados:
  - OpenAI GPT-4o (multimodal)
  - Anthropic Claude 3.7+ (multimodal)
  - Google Gemini 1.5 Pro / 2.0 (multimodal visión)

API:
  save_config({openai_key, anthropic_key, google_key, preferred_provider})
  solve_image_captcha(image_base64, prompt, provider?)
  solve_text_captcha(text, context)
  test_provider(provider)
  get_stats()
"""
import base64
import threading
import time
from datetime import datetime
from typing import Optional, Dict

import config
import json_atomic

_CONFIG_KEY = "llm_captcha_config.json"
_LOG_KEY = "llm_captcha_log.json"
_lock = threading.Lock()

PROVIDERS = ("openai", "anthropic", "google", "auto")

DEFAULT_PROMPTS = {
    "recaptcha_image": """You're helping a user accessibility solve a CAPTCHA.
The image is a 3x3 grid. Identify which cells contain the object: {target_object}.
Respond ONLY with a JSON list like [1,3,7] indicating the cell numbers (1-9).""",

    "hcaptcha_image": """Identify objects in this image.
Target: {target_object}.
Respond ONLY with JSON {{"contains": true/false}}.""",

    "text_captcha": """Read the distorted text in this CAPTCHA image and respond with ONLY the text shown, no explanation.""",

    "math_captcha": """Solve this math equation from the image. Respond with ONLY the numeric result.""",
}


def get_config() -> dict:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "openai_api_key": "",
        "anthropic_api_key": "",
        "google_api_key": "",
        "preferred_provider": "auto",     # auto|openai|anthropic|google
        "openai_model": "gpt-4o",
        "anthropic_model": "claude-3-5-sonnet-latest",
        "google_model": "gemini-1.5-pro-latest",
        "mode": "simulate",               # simulate | live
        "max_tokens": 256,
        "timeout_s": 30,
        "fallback_chain": ["google", "openai", "anthropic"],  # orden si preferred falla
    })


def save_config(patch: dict) -> dict:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def available_providers() -> Dict[str, dict]:
    """Qué proveedores están configurados + con cliente disponible."""
    cfg = get_config()
    result = {}

    # OpenAI
    try:
        import openai
        result["openai"] = {
            "library_installed": True,
            "configured": bool(cfg.get("openai_api_key")),
            "model": cfg.get("openai_model"),
        }
    except ImportError:
        result["openai"] = {"library_installed": False}

    # Anthropic
    try:
        import anthropic
        result["anthropic"] = {
            "library_installed": True,
            "configured": bool(cfg.get("anthropic_api_key")),
            "model": cfg.get("anthropic_model"),
        }
    except ImportError:
        result["anthropic"] = {"library_installed": False}

    # Google
    try:
        import google.generativeai
        result["google"] = {
            "library_installed": True,
            "configured": bool(cfg.get("google_api_key")),
            "model": cfg.get("google_model"),
        }
    except ImportError:
        result["google"] = {"library_installed": False}

    return result


def _pick_provider(cfg: dict) -> str:
    """Elige provider según preferred + fallback_chain."""
    pref = cfg.get("preferred_provider", "auto")
    avail = available_providers()

    if pref in avail and avail[pref].get("configured"):
        return pref
    # fallback chain
    for p in cfg.get("fallback_chain", ["google", "openai", "anthropic"]):
        if p in avail and avail[p].get("configured"):
            return p
    return ""


def _log_call(provider: str, captcha_type: str, success: bool,
                elapsed_ms: int, tokens_used: int = 0, error: str = ""):
    try:
        with _lock:
            d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
            d["log"].insert(0, {
                "ts": datetime.now().isoformat(),
                "provider": provider, "captcha_type": captcha_type,
                "success": success, "elapsed_ms": elapsed_ms,
                "tokens_used": tokens_used,
                "error": error[:200],
            })
            d["log"] = d["log"][:200]
            json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


# ═══════════════════════════════════════════
#  SOLVERS POR PROVIDER
# ═══════════════════════════════════════════
def _solve_with_openai(image_b64: str, prompt: str, cfg: dict) -> dict:
    import openai
    client = openai.OpenAI(api_key=cfg["openai_api_key"],
                            timeout=cfg.get("timeout_s", 30))
    started = time.time()
    try:
        resp = client.chat.completions.create(
            model=cfg.get("openai_model", "gpt-4o"),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }],
            max_tokens=cfg.get("max_tokens", 256),
        )
        answer = resp.choices[0].message.content.strip()
        tokens = resp.usage.total_tokens if resp.usage else 0
        return {"ok": True, "provider": "openai", "answer": answer,
                 "tokens_used": tokens,
                 "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "provider": "openai",
                 "error": f"{type(e).__name__}: {str(e)[:200]}",
                 "elapsed_ms": int((time.time() - started) * 1000)}


def _solve_with_anthropic(image_b64: str, prompt: str, cfg: dict) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"],
                                    timeout=cfg.get("timeout_s", 30))
    started = time.time()
    try:
        resp = client.messages.create(
            model=cfg.get("anthropic_model", "claude-3-5-sonnet-latest"),
            max_tokens=cfg.get("max_tokens", 256),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png",
                                 "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        answer = resp.content[0].text.strip() if resp.content else ""
        tokens = resp.usage.input_tokens + resp.usage.output_tokens if resp.usage else 0
        return {"ok": True, "provider": "anthropic", "answer": answer,
                 "tokens_used": tokens,
                 "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "provider": "anthropic",
                 "error": f"{type(e).__name__}: {str(e)[:200]}",
                 "elapsed_ms": int((time.time() - started) * 1000)}


def _solve_with_google(image_b64: str, prompt: str, cfg: dict) -> dict:
    import google.generativeai as genai
    genai.configure(api_key=cfg["google_api_key"])
    started = time.time()
    try:
        model = genai.GenerativeModel(cfg.get("google_model", "gemini-1.5-pro-latest"))
        # Decode image
        import base64 as b64
        img_bytes = b64.b64decode(image_b64)
        resp = model.generate_content(
            [prompt, {"mime_type": "image/png", "data": img_bytes}],
        )
        answer = resp.text.strip() if resp.text else ""
        tokens = 0  # Google no siempre lo reporta
        return {"ok": True, "provider": "google", "answer": answer,
                 "tokens_used": tokens,
                 "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "provider": "google",
                 "error": f"{type(e).__name__}: {str(e)[:200]}",
                 "elapsed_ms": int((time.time() - started) * 1000)}


# ═══════════════════════════════════════════
#  API PRINCIPAL
# ═══════════════════════════════════════════
def solve_image_captcha(image_base64: str,
                          prompt: Optional[str] = None,
                          captcha_type: str = "recaptcha_image",
                          target_object: str = "car",
                          provider: Optional[str] = None) -> dict:
    """Resuelve un CAPTCHA con LLM multimodal."""
    cfg = get_config()

    # Prompt
    if not prompt:
        prompt_tpl = DEFAULT_PROMPTS.get(captcha_type, DEFAULT_PROMPTS["text_captcha"])
        try:
            prompt = prompt_tpl.format(target_object=target_object)
        except Exception:
            prompt = prompt_tpl

    # Provider
    prov = provider or _pick_provider(cfg)
    if not prov and cfg.get("mode") != "simulate":
        return {"ok": False, "error": "no provider configured",
                 "hint": "Configura al menos 1 api_key via /api/llm-captcha/config"}

    # Simulate mode
    if cfg.get("mode") == "simulate" or not prov:
        # Mock response
        mock_answer = {
            "recaptcha_image": "[1,3,7]",
            "hcaptcha_image": '{"contains": true}',
            "text_captcha": "ABC123",
            "math_captcha": "42",
        }.get(captcha_type, "mock-answer")
        _log_call("simulate", captcha_type, True, 50, 0)
        return {"ok": True, "mode": "simulate", "provider": "simulate",
                 "answer": mock_answer,
                 "captcha_type": captcha_type,
                 "note": "simulate — configura api_key para usar LLM real"}

    # Live mode
    solvers = {
        "openai": _solve_with_openai,
        "anthropic": _solve_with_anthropic,
        "google": _solve_with_google,
    }
    solver = solvers.get(prov)
    if not solver:
        return {"ok": False, "error": f"provider '{prov}' desconocido"}

    result = solver(image_base64, prompt, cfg)
    _log_call(prov, captcha_type, result.get("ok", False),
               result.get("elapsed_ms", 0),
               result.get("tokens_used", 0),
               result.get("error", ""))

    # Fallback si falla
    if not result.get("ok") and cfg.get("mode") == "live":
        for fallback_prov in cfg.get("fallback_chain", []):
            if fallback_prov == prov:
                continue
            avail = available_providers().get(fallback_prov, {})
            if avail.get("configured"):
                fb_solver = solvers.get(fallback_prov)
                if fb_solver:
                    fb = fb_solver(image_base64, prompt, cfg)
                    _log_call(fallback_prov, captcha_type, fb.get("ok", False),
                               fb.get("elapsed_ms", 0), 0, fb.get("error", ""))
                    if fb.get("ok"):
                        fb["fallback_from"] = prov
                        return fb

    result["captcha_type"] = captcha_type
    return result


def solve_text_captcha(image_base64: str) -> dict:
    return solve_image_captcha(image_base64,
                                 captcha_type="text_captcha",
                                 prompt=DEFAULT_PROMPTS["text_captcha"])


def test_provider(provider: str = "auto") -> dict:
    """Test con imagen simple. Usado para verificar API key."""
    cfg = get_config()
    prov = provider if provider != "auto" else _pick_provider(cfg)
    avail = available_providers().get(prov, {})
    if not avail.get("library_installed"):
        return {"ok": False, "error": f"library '{prov}' no instalada"}
    if not avail.get("configured"):
        return {"ok": False, "error": f"api_key para '{prov}' no configurada"}

    # Imagen mínima PNG 1x1 píxel blanco
    tiny_png = base64.b64encode(bytes([
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
        0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41,
        0x54, 0x08, 0xD7, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
        0x00, 0x00, 0x03, 0x00, 0x01, 0x5B, 0xE8, 0xAB,
        0x3E, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E,
        0x44, 0xAE, 0x42, 0x60, 0x82,
    ])).decode()

    return solve_image_captcha(
        tiny_png,
        prompt="What color is this image? Respond in ONE word.",
        captcha_type="text_captcha",
        provider=prov,
    )


def get_stats() -> dict:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"log": []})
    log = d.get("log", [])
    by_provider = {}
    total_tokens = 0
    for e in log:
        p = e.get("provider", "?")
        pd = by_provider.setdefault(p, {"total": 0, "success": 0, "fail": 0, "tokens": 0})
        pd["total"] += 1
        if e.get("success"):
            pd["success"] += 1
        else:
            pd["fail"] += 1
        pd["tokens"] += e.get("tokens_used", 0)
        total_tokens += e.get("tokens_used", 0)

    # success rates
    for p in by_provider.values():
        if p["total"] > 0:
            p["success_rate_pct"] = round(100 * p["success"] / p["total"], 1)

    cfg = get_config()
    return {
        "mode": cfg.get("mode"),
        "preferred_provider": cfg.get("preferred_provider"),
        "total_calls": len(log),
        "total_tokens_used": total_tokens,
        "by_provider": by_provider,
        "providers_available": available_providers(),
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    cfg = get_config()
    results["checks"]["config_mode"] = cfg.get("mode")

    avail = available_providers()
    results["checks"]["openai_library"] = avail.get("openai", {}).get("library_installed", False)
    results["checks"]["anthropic_library"] = avail.get("anthropic", {}).get("library_installed", False)
    results["checks"]["google_library"] = avail.get("google", {}).get("library_installed", False)

    # Simulate call (siempre responde)
    r = solve_image_captcha("fake_b64", captcha_type="recaptcha_image",
                               target_object="car")
    results["checks"]["simulate_call_ok"] = r.get("ok", False)
    results["checks"]["answer_returned"] = bool(r.get("answer"))

    # Stats
    s = get_stats()
    results["checks"]["stats_readable"] = "total_calls" in s

    return results


def get_info() -> dict:
    return {
        "module": "llm_captcha",
        "providers_available": available_providers(),
        "default_prompts": list(DEFAULT_PROMPTS.keys()),
        "stats": get_stats(),
    }
