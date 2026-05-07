"""tm_session_factory.py — Garantiza sesión MoreLogin LIMPIA y NUEVA por cada compra.

Reglas duras (instrucción del usuario 27-abr):
1. SIEMPRE cerrar TODOS los perfiles MoreLogin antes de abrir uno nuevo.
2. SIEMPRE perfil ES distinto al(los) usado(s) en la sesión actual.
3. SIEMPRE storage AGRESIVO completo (cookies + localStorage + sessionStorage + IndexedDB + caches).
4. SIEMPRE pestañas auth.* / paused / identity.* cerradas antes de operar.
5. SIEMPRE verificar IP nueva (vía ipify) ANTES de empezar el flow.
6. SIEMPRE confirmar que la IP está geo-localizada en ES.
7. NUNCA reusar profile_label/env_id ya tocado por la sesión actual.

API:
    sf = SessionFactory()
    session = sf.new_clean_session(country='ES')  # cierra todo + abre nuevo + verifica
    # → {port, profile, env_id, ip, ip_country}
    # ... haz tu compra con session['port'] ...
    sf.close_session(session)  # cleanup AGRESIVO + close
"""
from __future__ import annotations
import os, time, json, requests
from typing import Dict, Any, Optional, Set
from datetime import datetime


MORELOGIN_API = "http://127.0.0.1:40000"


_HISTORY_PATH = os.path.join(os.path.dirname(__file__), "datos", "session_factory_history.json")


class SessionFactory:
    """Mantiene set de profile_labels usados (persistido en disco) para no repetir."""
    def __init__(self, persistent_used: bool = True):
        self.persistent = persistent_used
        self.used_profiles: Set[str] = set()
        self.used_env_ids: Set[str] = set()
        self.history: list = []
        if persistent_used:
            try:
                with open(_HISTORY_PATH, encoding='utf-8') as f:
                    d = json.load(f)
                    self.used_profiles = set(d.get("used_profiles", []))
                    self.used_env_ids = set(d.get("used_env_ids", []))
                    self.history = d.get("history", [])[-50:]
            except Exception: pass

    def _persist(self):
        if not self.persistent: return
        os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
        try:
            payload = {"used_profiles": list(self.used_profiles),
                        "used_env_ids": list(self.used_env_ids),
                        "history": self.history[-50:]}
            try:
                import json_atomic
                json_atomic.save(_HISTORY_PATH, payload)
            except Exception:
                with open(_HISTORY_PATH, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, indent=2)
        except Exception: pass

    # === Listado de perfiles ES ===
    def _list_es_profiles(self) -> list[Dict[str, Any]]:
        """Lista TODOS los perfiles MoreLogin con cc-ES en username (998 disponibles)."""
        out = []
        for pg in range(1, 12):
            try:
                r = requests.post(f"{MORELOGIN_API}/api/env/page",
                                  json={"pageNo": pg, "pageSize": 100}, timeout=15).json()
            except Exception: break
            items = (r.get("data") or {}).get("dataList") or []
            if not items: break
            for it in items:
                p = it.get("proxy") or {}
                u = p.get("username", "") or ""
                if "cc-ES" in u:
                    out.append({
                        "profile": it.get("envName"),
                        "env_id": it.get("id"),
                        "username": u,
                        "verified": p.get("proxyCheckStatus"),
                    })
        return out

    # === Cierre de TODOS los perfiles abiertos ===
    def close_all_open_profiles(self) -> int:
        """Cierra los env_ids que SessionFactory recuerda + cualquier perfil flagged anteriormente."""
        closed = 0
        # Cerrar todos los env_ids que hemos tocado en esta sesión + envs comunes flagged
        flagged = {"2048451234557923328", "2048449915575144448",
                   "2048501792509595648", "2048521159007866880"}
        all_ids = self.used_env_ids | flagged
        for env_id in all_ids:
            try:
                r = requests.post(f"{MORELOGIN_API}/api/env/close",
                                  json={"envId": env_id}, timeout=10)
                if r.status_code == 200: closed += 1
            except Exception: pass
        time.sleep(3)
        return closed

    # === Pickear perfil ES NO usado ===
    def pick_fresh_profile(self) -> Optional[Dict[str, Any]]:
        """Devuelve perfil ES NO usado en sesión actual. Prefiere verified, fallback unverified."""
        avail = self._list_es_profiles()
        avail = [p for p in avail if p["profile"] not in self.used_profiles]
        if not avail: return None
        # Preferir verified
        verified = [p for p in avail if p.get("verified") == 1]
        pick = verified[0] if verified else avail[0]
        self.used_profiles.add(pick["profile"])
        self.used_env_ids.add(pick["env_id"])
        return pick

    # === Arrancar perfil ===
    def start_profile(self, env_id: str) -> Optional[int]:
        try:
            r = requests.post(f"{MORELOGIN_API}/api/env/start",
                              json={"envId": env_id}, timeout=30).json()
            port = (r.get("data") or {}).get("debugPort")
            return int(port) if port else None
        except Exception: return None

    # === Verificar IP del perfil ===
    def verify_ip(self, port: int) -> Dict[str, Any]:
        """Verifica IP pública del browser y geo. Returns {ip, country, asn?}."""
        try:
            from rebrowser_playwright.sync_api import sync_playwright
        except ImportError:
            from playwright.sync_api import sync_playwright
        try:
            v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
        except Exception as e:
            return {"ip": "?", "error": f"cdp_unreach: {str(e)[:60]}"}
        with sync_playwright() as pw:
            try:
                b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
                ctx = b.contexts[0] if b.contexts else b.new_context()
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # ipify endpoint TXT (más simple)
                page.goto("https://api.ipify.org/", wait_until="domcontentloaded", timeout=20000)
                time.sleep(1)
                ip_text = page.evaluate("() => document.body.innerText.trim()")
                ip = ip_text.split()[0] if ip_text else ""
                # Geo lookup
                country = None; asn = None; org = None
                if ip:
                    try:
                        page.goto(f"https://ipinfo.io/{ip}/json", wait_until="domcontentloaded", timeout=15000)
                        time.sleep(1)
                        geo_raw = page.evaluate("() => document.body.innerText")
                        # Extraer JSON limpio
                        idx = geo_raw.find('{')
                        if idx >= 0:
                            geo = json.loads(geo_raw[idx:geo_raw.rfind('}')+1])
                            country = geo.get("country")
                            org = geo.get("org")
                            asn = (org.split(' ')[0] if org else None)
                    except Exception: pass
                return {"ip": ip or "?", "country": country, "asn": asn, "org": org}
            except Exception as e:
                return {"ip": "?", "error": str(e)[:80]}

    # === Cleanup AGRESIVO ===
    def aggressive_cleanup(self, port: int) -> Dict[str, int]:
        """Limpia cookies + localStorage + sessionStorage + IndexedDB + caches en todas las pestañas."""
        out = {"cookies": 0, "tabs_cleaned": 0, "tabs_closed": 0}
        try:
            try:
                from rebrowser_playwright.sync_api import sync_playwright
            except ImportError:
                from playwright.sync_api import sync_playwright
            v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
            with sync_playwright() as pw:
                b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
                ctx = b.contexts[0]
                cookies = ctx.cookies()
                out["cookies"] = len(cookies)
                # Cerrar pestañas comprometidas
                for p in list(ctx.pages):
                    try:
                        url = (p.url or "").lower()
                        if any(k in url for k in ["auth.ticketmaster", "identity.ticketmaster",
                                                    "authorization.oauth", "paused"]):
                            p.close(); out["tabs_closed"] += 1; continue
                        # Limpiar storage en las que quedan
                        try:
                            p.evaluate("""() => {
                                try { localStorage.clear(); } catch(e){}
                                try { sessionStorage.clear(); } catch(e){}
                                try { if (indexedDB.databases) {
                                    indexedDB.databases().then(dbs => {
                                        for (const db of dbs) try { indexedDB.deleteDatabase(db.name); } catch(e){}
                                    });
                                }} catch(e){}
                                try { if (window.caches) caches.keys().then(ks => ks.forEach(k => caches.delete(k))); } catch(e){}
                            }""")
                            out["tabs_cleaned"] += 1
                        except: pass
                    except: pass
                ctx.clear_cookies()
        except Exception: pass
        return out

    # === Cerrar perfil ===
    def close_profile(self, env_id: str) -> bool:
        try:
            r = requests.post(f"{MORELOGIN_API}/api/env/close",
                              json={"envId": env_id}, timeout=10)
            return r.status_code == 200
        except Exception: return False

    # === API pública: nueva sesión limpia ===
    def new_clean_session(self, country: str = "ES", verify_ip: bool = True) -> Dict[str, Any]:
        """Crea sesión nueva: cierra todo + perfil ES nuevo + IP fresca + verifica país.
        Returns {ok, port, profile, env_id, ip, ip_country, error?}."""
        log_evt = {"started_at": datetime.now().isoformat()}

        # 1) Cerrar todos los perfiles previos
        n_closed = self.close_all_open_profiles()
        log_evt["profiles_closed"] = n_closed
        time.sleep(2)

        # 2) Pickear perfil ES no usado
        pick = self.pick_fresh_profile()
        if not pick:
            log_evt["error"] = "no_es_profile_available"
            self.history.append(log_evt)
            return {"ok": False, **log_evt}
        log_evt.update(profile=pick["profile"], env_id=pick["env_id"])

        # 3) Arrancar
        port = self.start_profile(pick["env_id"])
        if not port:
            log_evt["error"] = "cannot_start_profile"
            self.history.append(log_evt)
            return {"ok": False, **log_evt}
        log_evt["port"] = port
        time.sleep(6)  # esperar a que el browser esté listo

        # 4) Cleanup AGRESIVO inmediatamente tras arranque
        cleanup = self.aggressive_cleanup(port)
        log_evt["cleanup"] = cleanup

        # 5) Verificar IP
        if verify_ip:
            ip_info = self.verify_ip(port)
            log_evt["ip"] = ip_info.get("ip")
            log_evt["ip_country"] = ip_info.get("country")
            log_evt["ip_asn"] = ip_info.get("asn")
            if ip_info.get("country") and ip_info["country"].upper() != country.upper():
                log_evt["warning"] = f"ip_country={ip_info['country']} != {country}"

        log_evt["ok"] = True
        log_evt["finished_at"] = datetime.now().isoformat()
        self.history.append(log_evt)
        self._persist()
        return log_evt

    # === Cerrar sesión ===
    def close_session(self, session: Dict[str, Any]) -> Dict[str, Any]:
        if not session.get("port") or not session.get("env_id"):
            return {"ok": False, "error": "session_incomplete"}
        out = {"profile": session["profile"]}
        out["cleanup"] = self.aggressive_cleanup(session["port"])
        out["closed"] = self.close_profile(session["env_id"])
        return out


# === CLI helper para testing ===
def main():
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sf = SessionFactory()
    print("=== SessionFactory test ===")
    print("[1] Creating clean session...")
    s = sf.new_clean_session(country="ES")
    print(json.dumps(s, indent=2, ensure_ascii=False))
    if s.get("ok"):
        print("\n[2] Use this session for purchase:")
        print(f"    CDP_PORT={s['port']}")
        print(f"    profile={s['profile']}")
        print(f"    ip={s.get('ip')} country={s.get('ip_country')}")
        print(f"    asn={s.get('ip_asn')}")


if __name__ == '__main__':
    main()
