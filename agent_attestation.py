"""agent_attestation.py — SOTA P2-19: Authorized AI Agent Attestation.

Fingerprint.com (Feb 2026) propuso un estandar de "attestation" para que los
agentes AI legitimos (ChatGPT Operator, Claude Computer Use, Perplexity Browse)
firmen sus requests con un token JWT-like reconocido por websites de tier-1.

Si TM (Akamai/Imperva) adopta el banner attestation:
  - Modo "sign": generamos token de un agent_id legit que TM aceptara
  - Modo "evade": detectamos el banner attestation y lo eludimos quitando
                  los headers/banners que la pagina inyecta para forzar el opt-in

Modelo de tokens:
    header.payload.signature
    header  = {"alg": "HS256", "typ": "AAT"}     # AAT = Authorized Agent Token
    payload = {"agent": "claude-cu", "ver": "1.4", "iat": ts, "exp": ts+600,
               "scope": "browse", "rp": "ticketmaster.com"}
    sig     = HMAC-SHA256(header.payload, secret_key_per_agent)

API publica:
  generate_attestation_token(agent_name, version)  -> str token
  evade_detection_banner(html)                      -> str html limpio
  validate_attestation(token)                       -> dict {valid, agent, ...}
  register_legit_agent(agent_id)                    -> dict
  list_registered_agents()                          -> list
  unregister_agent(agent_id)                        -> dict
  rotate_secret(agent_id)                           -> dict
  self_test()
"""
from __future__ import annotations
import os, time, json, hashlib, hmac, base64, secrets, threading, re
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import json_atomic


_CONFIG_KEY = "agent_attestation_config.json"
_AGENTS_KEY = "agent_attestation_registry.json"
_LOG_KEY = "agent_attestation_log.json"
_lock = threading.RLock()

# Lista de agentes "legitimos" segun el draft del estandar (mock)
_KNOWN_LEGIT_AGENTS = {
    "chatgpt-operator": {"vendor": "OpenAI", "tier": "premium"},
    "claude-cu":        {"vendor": "Anthropic", "tier": "premium"},
    "perplexity-pro":   {"vendor": "Perplexity", "tier": "standard"},
    "gemini-agent":     {"vendor": "Google", "tier": "premium"},
    "browser-use":      {"vendor": "BrowserUse", "tier": "standard"},
}

# Banners conocidos que paginas pueden inyectar para forzar opt-in del agent
_BANNER_PATTERNS = [
    r'<div[^>]*class="[^"]*aat-banner[^"]*"[^>]*>.*?</div>',
    r'<script[^>]*src="[^"]*attestation\.js[^"]*"[^>]*></script>',
    r'<meta[^>]*name="aat-required"[^>]*>',
    r'<div[^>]*data-aat-prompt[^>]*>.*?</div>',
    r'<noscript[^>]*data-aat[^>]*>.*?</noscript>',
]


# ============================================================
# CONFIG
# ============================================================
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "mode": "evade",                       # sign | evade | passive
        "default_agent": "claude-cu",
        "token_ttl_s": 600,                    # 10 min
        "auto_rotate_secret_h": 24,
        "alg": "HS256",
        "typ": "AAT",
        "include_rp_binding": True,            # bind a ticketmaster.com
        "evade_strip_inline_scripts": True,
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur: cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


# ============================================================
# AGENT REGISTRY
# ============================================================
def _load_registry() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_AGENTS_KEY), default={"agents": {}})


def _save_registry(reg: Dict[str, Any]) -> None:
    json_atomic.save(config.ruta(_AGENTS_KEY), reg)


def register_legit_agent(agent_id: str,
                          version: str = "1.0",
                          vendor: Optional[str] = None,
                          tier: str = "standard") -> Dict[str, Any]:
    """Registra un agente legitimo con secret key propia."""
    if not agent_id or len(agent_id) < 3:
        return {"ok": False, "error": "agent_id_too_short"}

    with _lock:
        reg = _load_registry()
        known = _KNOWN_LEGIT_AGENTS.get(agent_id, {})
        reg["agents"][agent_id] = {
            "agent_id": agent_id,
            "version": version,
            "vendor": vendor or known.get("vendor", "unknown"),
            "tier": tier or known.get("tier", "standard"),
            "secret": secrets.token_hex(32),
            "registered_at": datetime.now().isoformat(),
            "rotated_at": datetime.now().isoformat(),
            "tokens_issued": 0,
        }
        _save_registry(reg)

    return {"ok": True, "agent_id": agent_id,
            "vendor": reg["agents"][agent_id]["vendor"],
            "tier": reg["agents"][agent_id]["tier"]}


def list_registered_agents() -> List[Dict[str, Any]]:
    reg = _load_registry()
    return [
        {"agent_id": a["agent_id"], "vendor": a.get("vendor"),
         "tier": a.get("tier"), "version": a.get("version"),
         "tokens_issued": a.get("tokens_issued", 0)}
        for a in reg.get("agents", {}).values()
    ]


def unregister_agent(agent_id: str) -> Dict[str, Any]:
    with _lock:
        reg = _load_registry()
        was = reg.get("agents", {}).pop(agent_id, None)
        _save_registry(reg)
    return {"ok": was is not None, "agent_id": agent_id}


def rotate_secret(agent_id: str) -> Dict[str, Any]:
    with _lock:
        reg = _load_registry()
        a = reg.get("agents", {}).get(agent_id)
        if not a:
            return {"ok": False, "error": "agent_not_found"}
        a["secret"] = secrets.token_hex(32)
        a["rotated_at"] = datetime.now().isoformat()
        _save_registry(reg)
    return {"ok": True, "agent_id": agent_id, "rotated_at": a["rotated_at"]}


def _get_agent_secret(agent_id: str) -> Optional[str]:
    reg = _load_registry()
    a = reg.get("agents", {}).get(agent_id)
    return a.get("secret") if a else None


# ============================================================
# TOKEN GENERATION (JWT-like)
# ============================================================
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode((s + "=" * pad).encode())


def generate_attestation_token(agent_name: str = "",
                                version: str = "",
                                scope: str = "browse",
                                rp: str = "ticketmaster.com") -> Dict[str, Any]:
    """Genera token AAT firmado para un agente registrado."""
    cfg = get_config()
    aid = agent_name or cfg["default_agent"]

    # Auto-register si no existe pero esta en la lista de conocidos
    if not _get_agent_secret(aid):
        if aid in _KNOWN_LEGIT_AGENTS:
            register_legit_agent(aid, version=version or "1.0")
        else:
            return {"ok": False, "error": "agent_not_registered",
                    "hint": "call register_legit_agent first"}

    secret = _get_agent_secret(aid)
    if not secret:
        return {"ok": False, "error": "secret_unavailable"}

    now = int(time.time())
    header = {"alg": cfg["alg"], "typ": cfg["typ"]}
    payload = {
        "agent": aid,
        "ver": version or "1.0",
        "iat": now,
        "exp": now + cfg["token_ttl_s"],
        "scope": scope,
        "jti": secrets.token_hex(8),
    }
    if cfg.get("include_rp_binding") and rp:
        payload["rp"] = rp

    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{h_b64}.{p_b64}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    s_b64 = _b64url(sig)
    token = f"{h_b64}.{p_b64}.{s_b64}"

    # Bump counter
    with _lock:
        reg = _load_registry()
        a = reg.get("agents", {}).get(aid)
        if a:
            a["tokens_issued"] = a.get("tokens_issued", 0) + 1
            _save_registry(reg)

    return {"ok": True, "token": token, "agent": aid,
            "exp": payload["exp"], "ttl_s": cfg["token_ttl_s"]}


# ============================================================
# TOKEN VALIDATION
# ============================================================
def validate_attestation(token: str) -> Dict[str, Any]:
    """Valida estructura + firma + expiracion del token."""
    if not token or token.count(".") != 2:
        return {"valid": False, "error": "malformed"}

    try:
        h_b64, p_b64, s_b64 = token.split(".")
        header = json.loads(_b64url_decode(h_b64).decode())
        payload = json.loads(_b64url_decode(p_b64).decode())
    except Exception as e:
        return {"valid": False, "error": f"decode_failed:{type(e).__name__}"}

    if header.get("typ") != "AAT":
        return {"valid": False, "error": "wrong_typ"}

    aid = payload.get("agent", "")
    secret = _get_agent_secret(aid)
    if not secret:
        return {"valid": False, "error": "agent_unknown", "agent": aid}

    signing_input = f"{h_b64}.{p_b64}".encode()
    expected_sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    expected_b64 = _b64url(expected_sig)

    if not hmac.compare_digest(expected_b64, s_b64):
        return {"valid": False, "error": "bad_signature", "agent": aid}

    now = int(time.time())
    if payload.get("exp", 0) < now:
        return {"valid": False, "error": "expired", "agent": aid,
                "expired_ago_s": now - payload["exp"]}

    return {"valid": True, "agent": aid, "scope": payload.get("scope"),
            "rp": payload.get("rp"), "exp": payload.get("exp"),
            "iat": payload.get("iat"), "jti": payload.get("jti")}


# ============================================================
# EVADE DETECTION (strip banners injected by website)
# ============================================================
def evade_detection_banner(html: str) -> Dict[str, Any]:
    """Elimina banners/scripts AAT inyectados por la pagina.

    Util cuando TM mete el banner pero queremos pasar como humano regular
    sin presentar token AAT.
    """
    if not isinstance(html, str) or len(html) == 0:
        return {"ok": False, "error": "empty_html"}

    cfg = get_config()
    out = html
    removed = 0
    for pat in _BANNER_PATTERNS:
        new_out, n = re.subn(pat, "", out, flags=re.DOTALL | re.IGNORECASE)
        out = new_out
        removed += n

    # Strip inline scripts referencing AAT
    if cfg.get("evade_strip_inline_scripts"):
        new_out, n = re.subn(
            r'<script[^>]*>[^<]*(?:aat|attestation)[^<]*</script>',
            "", out, flags=re.DOTALL | re.IGNORECASE)
        out = new_out
        removed += n

    return {"ok": True, "html": out, "elements_removed": removed,
            "size_before": len(html), "size_after": len(out)}


def detect_attestation_banner(html: str) -> Dict[str, Any]:
    """Detecta si la pagina tiene un banner AAT. No modifica."""
    if not isinstance(html, str):
        return {"detected": False, "matches": 0}
    matches = 0
    for pat in _BANNER_PATTERNS:
        matches += len(re.findall(pat, html, flags=re.DOTALL | re.IGNORECASE))
    return {"detected": matches > 0, "matches": matches,
            "patterns_checked": len(_BANNER_PATTERNS)}


# ============================================================
# SELF TEST
# ============================================================
def self_test() -> Dict[str, Any]:
    checks = {}

    # 1. Config
    cfg = get_config()
    checks["config_has_mode"] = "mode" in cfg
    checks["config_has_alg"] = cfg.get("alg") == "HS256"

    # 2. Register legit agent
    r = register_legit_agent("claude-cu", version="1.4")
    checks["register_ok"] = r.get("ok") is True
    checks["register_vendor_anthropic"] = r.get("vendor") == "Anthropic"

    # 3. Generate token
    r = generate_attestation_token("claude-cu", version="1.4")
    checks["token_generated"] = r.get("ok") is True
    checks["token_has_3_parts"] = r.get("token", "").count(".") == 2
    token = r.get("token", "")

    # 4. Validate token
    v = validate_attestation(token)
    checks["validate_ok"] = v.get("valid") is True
    checks["validate_agent_match"] = v.get("agent") == "claude-cu"
    checks["validate_has_rp"] = v.get("rp") == "ticketmaster.com"

    # 5. Validate malformed
    v_bad = validate_attestation("not.a.token")
    checks["validate_malformed_rejects"] = v_bad.get("valid") is False

    # 6. Validate tampered (flip a byte in signature)
    if token:
        parts = token.split(".")
        bad = parts[0] + "." + parts[1] + "." + ("A" + parts[2][1:] if parts[2][0] != "A" else "B" + parts[2][1:])
        v_t = validate_attestation(bad)
        checks["validate_tampered_rejects"] = v_t.get("valid") is False

    # 7. List agents
    agents = list_registered_agents()
    checks["list_includes_claude"] = any(a["agent_id"] == "claude-cu" for a in agents)

    # 8. Detect banner
    html_with_banner = '<html><body><div class="aat-banner">Confirm AI</div><p>x</p></body></html>'
    d = detect_attestation_banner(html_with_banner)
    checks["detect_banner_yes"] = d.get("detected") is True

    # 9. Evade
    e = evade_detection_banner(html_with_banner)
    checks["evade_ok"] = e.get("ok") is True
    checks["evade_removed_at_least_1"] = e.get("elements_removed", 0) >= 1
    checks["evade_smaller_html"] = e.get("size_after", 999) < e.get("size_before", 0)

    # 10. Detect on clean html (no banner)
    d_clean = detect_attestation_banner("<html><body><p>hi</p></body></html>")
    checks["detect_clean_no"] = d_clean.get("detected") is False

    # 11. Rotate secret
    r_rot = rotate_secret("claude-cu")
    checks["rotate_ok"] = r_rot.get("ok") is True
    # token previo debe quedar invalido tras rotar
    v_after = validate_attestation(token)
    checks["old_token_invalid_after_rotate"] = v_after.get("valid") is False

    # 12. Auto-register from known list
    r2 = generate_attestation_token("perplexity-pro")
    checks["auto_register_known_agent"] = r2.get("ok") is True

    # 13. Unknown agent rejects
    r3 = generate_attestation_token("evil-bot-9000")
    checks["unknown_agent_rejected"] = r3.get("ok") is False

    # 14. Unregister
    r_u = unregister_agent("perplexity-pro")
    checks["unregister_ok"] = r_u.get("ok") is True

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total, "total_checks": total,
            "passed_checks": passed, "checks": checks}


def get_info() -> Dict[str, Any]:
    reg = _load_registry()
    return {
        "module": "agent_attestation",
        "version": 1,
        "registered_agents": len(reg.get("agents", {})),
        "known_legit_agents": list(_KNOWN_LEGIT_AGENTS.keys()),
        "mode": get_config().get("mode"),
        "purpose": "Authorized AI Agent attestation (Fingerprint.com Feb 2026 draft)",
    }
