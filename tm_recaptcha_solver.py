"""tm_recaptcha_solver.py — Resuelve reCAPTCHA Enterprise v3 invisible de TM via 2Captcha.

reCAPTCHA Enterprise v3 (TM ES sitekey: 6LcvL3UrAAAAAO_9u8Seiuf-I6F_tP_jSS-zndXV)
es invisible (sin checkbox), evalúa comportamiento y devuelve un score 0.0-1.0.
Si el score es <0.5 TM rechaza con "cuenta no encontrada" o "sesión suspendida".

Usage:
    from tm_recaptcha_solver import solve_and_inject_recaptcha_v3
    solve_and_inject_recaptcha_v3(page, action='login', min_score=0.9)
    # → resuelve via 2Captcha + inyecta token en DOM (hidden input + callback)
    # → blockea hasta que termine (~10-30s)
"""
from __future__ import annotations
import json, os, re, time
from typing import Optional


def _api_key() -> str:
    """Lee API key 2Captcha del config."""
    p = os.path.join(os.path.dirname(__file__), "datos", "sniper_bot", "captcha", "captcha.json")
    try:
        d = json.load(open(p))
        return d.get("services", {}).get("2captcha", {}).get("api_key", "") or ""
    except Exception:
        return ""


def detect_recaptcha_sitekey(page) -> Optional[str]:
    """Extrae el sitekey de reCAPTCHA Enterprise v3 del HTML actual.
    TM ES usa render=6LcvL3UrAAAAAO_9u8Seiuf-I6F_tP_jSS-zndXV en /enterprise.js."""
    try:
        sks = page.evaluate("""() => {
            const out = [];
            // Scripts con ?render=SITEKEY
            document.querySelectorAll('script[src]').forEach(s => {
                const m = (s.src || '').match(/[?&]render=([^&]+)/);
                if (m) out.push({sitekey: m[1], src: s.src});
            });
            // Divs con data-sitekey
            document.querySelectorAll('[data-sitekey]').forEach(el => {
                out.push({sitekey: el.getAttribute('data-sitekey'), src: 'data-sitekey'});
            });
            return out;
        }""")
        if not sks: return None
        # Preferir el de enterprise.js si existe
        for s in sks:
            if 'enterprise' in (s.get('src') or '').lower():
                return s['sitekey']
        return sks[0]['sitekey']
    except Exception:
        return None


def solve_and_inject_recaptcha_v3(page, action: str = "login", min_score: float = 0.9,
                                    timeout_s: int = 120) -> dict:
    """Resuelve reCAPTCHA Enterprise v3 vía 2Captcha + inyecta el token.
    Returns: {ok, token?, sitekey?, error?, elapsed_s}"""
    started = time.time()
    api_key = _api_key()
    if not api_key:
        return {"ok": False, "error": "no_2captcha_api_key", "elapsed_s": 0}

    sitekey = detect_recaptcha_sitekey(page)
    if not sitekey:
        return {"ok": False, "error": "no_sitekey_found", "elapsed_s": round(time.time()-started, 1)}

    try:
        from twocaptcha import TwoCaptcha
    except ImportError:
        return {"ok": False, "error": "2captcha-python_not_installed",
                "elapsed_s": round(time.time()-started, 1)}

    page_url = page.url
    solver = TwoCaptcha(api_key, defaultTimeout=timeout_s, pollingInterval=5)

    try:
        result = solver.recaptcha(
            sitekey=sitekey,
            url=page_url,
            version='v3',
            action=action,
            score=min_score,
            enterprise=1,
        )
        token = result.get('code') or ''
        if not token:
            return {"ok": False, "error": "empty_token", "sitekey": sitekey,
                    "elapsed_s": round(time.time()-started, 1)}
    except Exception as e:
        return {"ok": False, "error": f"2captcha_failed: {str(e)[:120]}",
                "sitekey": sitekey, "elapsed_s": round(time.time()-started, 1)}

    # Inyectar token con override PERSISTENTE (se reaplica cada 500ms por 30s)
    # para vencer reasignaciones de grecaptcha.execute por parte de TM
    inject_result = page.evaluate("""(token) => {
        const out = {fields_set: 0, callbacks: 0, override_installed: false};

        // Helper: setear input + dispatch events React
        const setInputValue = (el, val) => {
            try {
                const proto = Object.getPrototypeOf(el);
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            } catch(e) { try { el.value = val; } catch(e2){} }
        };

        // 1) Hidden inputs recaptcha-token / g-recaptcha-response
        document.querySelectorAll('input[name="recaptcha-token"], input[name="g-recaptcha-response"], input[name="recaptcha_token"], textarea[name="g-recaptcha-response"]').forEach(el => {
            setInputValue(el, token);
            el.innerHTML = token;
            out.fields_set++;
        });

        // 2) Callbacks grecaptcha
        try {
            if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
                Object.keys(window.___grecaptcha_cfg.clients).forEach(k => {
                    const client = window.___grecaptcha_cfg.clients[k];
                    Object.keys(client).forEach(sub => {
                        const obj = client[sub];
                        if (typeof obj === 'object' && obj && obj.callback) {
                            try { obj.callback(token); out.callbacks++; } catch(e){}
                        }
                    });
                });
            }
        } catch(e) {}

        // 3) Override grecaptcha.execute / enterprise.execute PERSISTENTE
        // Se reaplica cada 500ms por 30s para vencer reasignaciones de TM
        const installOverride = () => {
            try {
                if (window.grecaptcha) {
                    if (!window.grecaptcha.__patched) {
                        const _exec = function() { return Promise.resolve(token); };
                        try { window.grecaptcha.execute = _exec; } catch(e){}
                        try {
                            if (window.grecaptcha.enterprise) {
                                window.grecaptcha.enterprise.execute = _exec;
                                window.grecaptcha.enterprise.__patched = true;
                            }
                        } catch(e){}
                        window.grecaptcha.__patched = true;
                        out.override_installed = true;
                    }
                }
                // También re-inyectar tokens en inputs que TM puede haber reseteado
                document.querySelectorAll('input[name="recaptcha-token"], input[name="g-recaptcha-response"], input[name="recaptcha_token"], textarea[name="g-recaptcha-response"]').forEach(el => {
                    if (el.value !== token) setInputValue(el, token);
                });
            } catch(e) {}
        };
        installOverride();
        // Reinstalar cada 500ms por 30s
        let count = 0;
        const interval = setInterval(() => {
            installOverride();
            count++;
            if (count > 60) clearInterval(interval);
        }, 500);

        return out;
    }""", token)

    return {"ok": True, "token": token[:20] + "..." + token[-10:],
            "sitekey": sitekey, "inject": inject_result,
            "elapsed_s": round(time.time()-started, 1)}


if __name__ == "__main__":
    print("API key configurada:", bool(_api_key()))
    print(f"  key prefix: {_api_key()[:10]}...{_api_key()[-4:]}")
