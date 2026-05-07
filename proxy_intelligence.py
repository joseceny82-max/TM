"""proxy_intelligence.py — Gestion y rotacion de proxies."""
import uuid
from datetime import datetime
import config
import json_atomic


PROXY_PROVIDERS = [
    {"id": "brightdata",  "name": "Bright Data",   "type": "residential", "url": "https://brightdata.com"},
    {"id": "oxylabs",     "name": "Oxylabs",       "type": "residential", "url": "https://oxylabs.io"},
    {"id": "smartproxy",  "name": "Smartproxy",    "type": "residential", "url": "https://smartproxy.com"},
    {"id": "iproyal",     "name": "IPRoyal",       "type": "residential", "url": "https://iproyal.com"},
    {"id": "soax",        "name": "SOAX",          "type": "residential", "url": "https://soax.com"},
    {"id": "netnut",      "name": "NetNut",        "type": "residential", "url": "https://netnut.io"},
    {"id": "proxy_empire", "name": "Proxy Empire", "type": "mobile",      "url": "https://proxyempire.io"},
]


USE_CASE_RECOMMENDATIONS = {
    "ticketmaster_us": "residential_rotating",
    "ticketmaster_eu": "residential_sticky",
    "captcha_solve":   "datacenter",
    "queue_wait":      "residential_sticky",
}


class ProxyManager:
    def list_proxies(self):
        d = json_atomic.load(config.ruta("proxies.json"), default={"proxies": []})
        return d.get("proxies", [])

    def add_proxy(self, proxy):
        d = json_atomic.load(config.ruta("proxies.json"), default={"proxies": []})
        proxy = dict(proxy)
        proxy.setdefault("id", uuid.uuid4().hex[:8])
        proxy.setdefault("created_at", datetime.now().isoformat())
        proxy.setdefault("health", 100)
        d["proxies"].append(proxy)
        json_atomic.save(config.ruta("proxies.json"), d)
        return proxy

    def delete_proxy(self, pid):
        d = json_atomic.load(config.ruta("proxies.json"), default={"proxies": []})
        d["proxies"] = [p for p in d["proxies"] if p.get("id") != pid]
        json_atomic.save(config.ruta("proxies.json"), d)

    def rotate(self, count=1):
        proxies = self.list_proxies()
        return proxies[:count] if proxies else []


proxy_manager = ProxyManager()
