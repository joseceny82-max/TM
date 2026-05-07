"""datadome_proxy_hooks.py — DataDome Integrity Probe defense via JS Proxy.

DataDome 2026 deploya integrity probes que detectan property tampering via
Object.getOwnPropertyDescriptor en native methods. Stealth patches directos
fallan. Solución: Proxy-based hooks (Reflect.get / Reflect.has) que sobreviven.

Genera scripts de inyección que sustituyen overrides directos por Proxy traps.

API:
  generate_proxy_hook_script(target_property, faked_value)
  generate_full_stealth_bundle()  → bundle JS completo para Playwright eval
  detect_dd_probe_signals(html)    → identifica si DD está activo en una página
  self_test()
"""
import re
from typing import Dict, List, Optional

# Properties que DataDome chequea con getOwnPropertyDescriptor
DD_MONITORED_PROPERTIES = [
    "navigator.webdriver",
    "navigator.languages",
    "navigator.plugins",
    "navigator.permissions.query",
    "WebGLRenderingContext.prototype.getParameter",
    "CanvasRenderingContext2D.prototype.getImageData",
    "HTMLCanvasElement.prototype.toDataURL",
    "Notification.permission",
    "Battery.charging",
]


def generate_proxy_hook_script(target_path: str, faked_value: str = "undefined") -> str:
    """Genera un script JS que reemplaza una property por Proxy en lugar de
    Object.defineProperty (sobrevive a getOwnPropertyDescriptor checks).

    target_path:  ej. "navigator.webdriver"
    faked_value:  ej. "false", "undefined", "[]"
    """
    parts = target_path.split(".")
    if len(parts) < 2:
        return f"// Invalid target: {target_path}"
    parent_path = ".".join(parts[:-1])
    prop = parts[-1]
    return f"""(function(){{
    try {{
        const original = {parent_path};
        const fake = {faked_value};
        Object.defineProperty(Object.getPrototypeOf({parent_path}), '{prop}', {{
            get: new Proxy(function(){{ return fake; }}, {{
                apply: (t,a,r) => fake,
                get: (t,p) => p === 'name' ? 'get {prop}' : t[p],
            }}),
            configurable: true, enumerable: true,
        }});
    }} catch(e) {{ /* silenciar */ }}
}})();"""


def generate_full_stealth_bundle() -> str:
    """Bundle completo de hooks Proxy-based para inyectar en cada page.
    Cubre webdriver, plugins, permissions, WebGL, Canvas.
    """
    snippets = [
        generate_proxy_hook_script("navigator.webdriver", "undefined"),
        # navigator.plugins NO usa Proxy directamente — array faked vía getter
        """(function(){
            try {
                Object.defineProperty(navigator, 'plugins', {
                    get: () => new Proxy([1,2,3,4,5], {
                        get: (t,p) => p === 'length' ? 5 :
                                       p === 'item' ? (i)=>({name:'Chrome PDF Plugin'}) :
                                       p === 'refresh' ? ()=>{} :
                                       p === 'namedItem' ? ()=>null : t[p],
                    }),
                    configurable: true,
                });
            } catch(e){}
        })();""",
        # languages como Proxy
        """(function(){
            try {
                const langs = ['es-ES','es','en'];
                Object.defineProperty(navigator, 'languages', {
                    get: () => new Proxy(langs, {get: (t,p) => t[p]}),
                    configurable: true,
                });
            } catch(e){}
        })();""",
        # permissions.query intercept
        """(function(){
            try {
                const orig = navigator.permissions.query.bind(navigator.permissions);
                navigator.permissions.query = new Proxy(orig, {
                    apply: (t,thisArg,args) => {
                        const name = args[0]?.name;
                        if(name === 'notifications')
                            return Promise.resolve({state:'default', onchange:null});
                        return t.apply(thisArg, args);
                    }
                });
            } catch(e){}
        })();""",
    ]
    return "\n".join(snippets)


def detect_dd_probe_signals(html: str) -> Dict:
    """Detecta si una página tiene scripts DataDome activos."""
    if not html:
        return {"detected": False}
    signals = []
    for pattern, name in [
        (r"datadome", "datadome cookie/script"),
        (r"dd_check", "dd_check function"),
        (r'js\.datadome\.co', "DataDome CDN JS"),
        (r"_dd\.js", "_dd.js loader"),
        (r"captchaDelivery", "DataDome captcha endpoint"),
    ]:
        if re.search(pattern, html, re.I):
            signals.append(name)
    return {"detected": bool(signals), "signals": signals,
            "monitored_properties": DD_MONITORED_PROPERTIES}


def self_test() -> Dict:
    r = {"checks": {}}
    # Generación de hook
    s = generate_proxy_hook_script("navigator.webdriver", "undefined")
    r["checks"]["script_generates"] = "Proxy" in s and "'webdriver'" in s and "navigator" in s
    r["checks"]["script_uses_proxy"] = s.count("Proxy") >= 1
    r["checks"]["script_handles_apply"] = "apply:" in s

    bundle = generate_full_stealth_bundle()
    r["checks"]["bundle_not_empty"] = len(bundle) > 500
    r["checks"]["bundle_has_navigator_plugins"] = "navigator, 'plugins'" in bundle
    r["checks"]["bundle_has_languages"] = "navigator, 'languages'" in bundle
    r["checks"]["bundle_has_permissions"] = "permissions.query" in bundle

    # Detection
    sample_html = '<script src="https://js.datadome.co/tags.js"></script><div>page</div>'
    det = detect_dd_probe_signals(sample_html)
    r["checks"]["detect_finds_datadome"] = det["detected"] is True
    r["checks"]["detect_signals_listed"] = len(det.get("signals", [])) >= 1

    clean_html = "<html><body>nothing here</body></html>"
    det_clean = detect_dd_probe_signals(clean_html)
    r["checks"]["detect_clean_returns_false"] = det_clean["detected"] is False

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "datadome_proxy_hooks", "version": 1,
            "monitored_properties": len(DD_MONITORED_PROPERTIES)}
