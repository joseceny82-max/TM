"""venue_scanner.py — Escanea el mapa de un evento TM y guarda venue + secciones + imagen.

Usa la regla 'perfil MoreLogin nuevo por ejecución' + cleanup discriminado al cerrar.
Reusa ticket_selector para detectar secciones.

API:
    scan_event(tm_id, tm_url, name, city='', country='') -> dict
        Pasos: arranca perfil ES nuevo → navega → modo seatmap → captura screenshot
        del SVG + secciones → cierra perfil con cleanup → persiste venue.
"""
from __future__ import annotations
import os, time, base64
from typing import Dict, Any, Optional
import requests

import venue_cache
import ticket_selector
import cookie_filter

MORELOGIN_API = "http://127.0.0.1:40000"


def _pick_fresh_es_profile(quemados: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """Devuelve el primer perfil ES no quemado. Acepta perfiles unchecked
    (countryCode='' + proxyCheckStatus=0) cuyo username contiene cc-ES — son
    proxies Quantum cc-ES reales según memoria. Solo excluye proxyCheckStatus=2
    (failed). None si no hay."""
    quemados = quemados or set()
    for page_no in range(1, 21):
        try:
            r = requests.post(f"{MORELOGIN_API}/api/env/page",
                              json={"pageNo": page_no, "pageSize": 100},
                              timeout=10).json()
        except Exception:
            return None
        items = (r.get("data", {}).get("dataList") or [])
        if not items:
            break
        for it in items:
            p = it.get("proxy") or {}
            name = it.get("envName") or ""
            if not name or name in quemados:
                continue
            cc = (p.get("countryCode") or "").upper()
            user = p.get("username") or ""
            is_es = (cc == "ES") or ("cc-ES" in user) or ("-ES-" in user)
            if not is_es:
                continue
            if p.get("proxyCheckStatus") == 2:
                continue
            return {"profile": name, "env_id": it["id"],
                    "ip": p.get("exportIp")}
    return None


def _start_profile(env_id: str) -> Optional[int]:
    try:
        r = requests.post(f"{MORELOGIN_API}/api/env/start",
                          json={"envId": env_id}, timeout=20).json()
        return int(r["data"]["debugPort"])
    except Exception:
        return None


def _close_profile_with_cleanup(env_id: str, port: int, profile_label: str = "") -> Dict[str, Any]:
    """Cleanup discriminado + close del perfil MoreLogin.
    Aplica regla del usuario: preservar cookies útiles (TM/antibot/Adyen/queue-it),
    borrar las "malas" (tracking/consent), persistir KEEP a disco para reuso futuro."""
    out = {"keep": 0, "drop": 0, "unknown": 0, "closed": False}
    try:
        from playwright.sync_api import sync_playwright
        from datetime import datetime
        import json as _json
        v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=5).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
            ctx = b.contexts[0] if b.contexts else None
            if ctx:
                cookies = ctx.cookies()
                keep, drop, unknown = cookie_filter.partition_cookies(cookies)
                out["keep"], out["drop"], out["unknown"] = len(keep), len(drop), len(unknown)
                ctx.clear_cookies()
                if keep:
                    norm = []
                    for c in keep:
                        nc = {k: vv for k, vv in c.items() if vv is not None and k in
                              ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "url")}
                        if "sameSite" in nc:
                            ss = str(nc["sameSite"]).capitalize()
                            nc["sameSite"] = ss if ss in ("Strict", "Lax", "None") else "Lax"
                        norm.append(nc)
                    try: ctx.add_cookies(norm)
                    except Exception: pass

                # Persistir KEEP a disco (warm-start futuro / auditoría)
                try:
                    keep_dir = os.path.join(os.path.dirname(__file__), "datos", "cookies_kept")
                    os.makedirs(keep_dir, exist_ok=True)
                    label = profile_label or env_id
                    fp = os.path.join(keep_dir, f"{label}_venuescan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
                    with open(fp, "w", encoding="utf-8") as f:
                        _json.dump({"profile": label, "env_id": env_id,
                                    "cookies_keep": keep, "cookies_dropped": len(drop),
                                    "cookies_unknown": len(unknown),
                                    "saved_at": datetime.now().isoformat(),
                                    "source": "venue_scanner"},
                                   f, indent=2, ensure_ascii=False, default=str)
                    out["keep_path"] = os.path.relpath(fp, os.path.dirname(__file__)).replace("\\", "/")
                except Exception as e:
                    out["keep_persist_err"] = str(e)[:120]

                # Limpiar caches HTTP (no localStorage — preservar fingerprint TM)
                for p in ctx.pages:
                    try:
                        p.evaluate("""() => {
                            try { if (caches && caches.keys) caches.keys().then(ks => ks.forEach(k => caches.delete(k))); } catch(e){}
                        }""")
                    except Exception: pass
                # Política usuario: borrar historial COMPLETO al cerrar.
                try:
                    import browser_cleanup as _bc
                    out["history"] = _bc.clear_history_via_cdp(ctx)
                except Exception as _e:
                    out["history_err"] = str(_e)[:120]
            b.close()
    except Exception as e:
        out["cleanup_err"] = str(e)[:120]
    try:
        r = requests.post(f"{MORELOGIN_API}/api/env/close",
                          json={"envId": env_id}, timeout=15).json()
        out["closed"] = r.get("code") == 0
    except Exception as e:
        out["close_err"] = str(e)[:80]
    return out


def scan_event(tm_id: str, tm_url: str, name: str, city: str = "",
               country: str = "", reuse_cached: bool = True) -> Dict[str, Any]:
    """Escanea el mapa de un evento. Si el venue ya está cacheado y `reuse_cached`,
    no relanza navegador — devuelve el venue existente vinculando este tm_id."""
    venue_id = venue_cache.venue_id_from_meta(name, city)
    cached = venue_cache.get_venue(venue_id) if reuse_cached else None

    if cached and cached.get("sections") and cached.get("image_path"):
        # Solo vincular este evento al venue
        venue_cache.link_event_to_venue(tm_id, venue_id)
        return {
            "ok": True, "cached": True, "venue_id": venue_id,
            "venue": venue_cache.get_venue(venue_id),
            "stats": {"sections": len(cached.get("sections") or []),
                      "linked_events": len(cached.get("linked_events") or [])},
        }

    # No hay caché: lanzar perfil y escanear
    prof = _pick_fresh_es_profile()
    if not prof:
        return {"ok": False, "error": "no_es_profile_available"}
    port = _start_profile(prof["env_id"])
    if not port:
        return {"ok": False, "error": "cannot_start_profile"}

    debug = {"profile": prof["profile"], "env_id": prof["env_id"], "port": port,
             "ip": prof.get("ip")}

    try:
        from playwright.sync_api import sync_playwright
        time.sleep(5)  # esperar init browser
        v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.bring_to_front()

            # Bloquear prompts de notificaciones desde antes de navegar (init script
            # se ejecuta en cada documento nuevo del context).
            try:
                ctx.add_init_script("""
                    try {
                        if (window.Notification) {
                            Notification.requestPermission = () => Promise.resolve('denied');
                            try { Object.defineProperty(Notification, 'permission', {get:()=>'denied', configurable:true}); } catch(e){}
                        }
                        if (navigator.serviceWorker && navigator.serviceWorker.register) {
                            navigator.serviceWorker.register = function() { return Promise.reject(new Error('blocked')); };
                        }
                    } catch(e){}
                """)
            except Exception:
                pass

            # Navegar al evento (con retries)
            for attempt in range(3):
                try:
                    page.goto(tm_url, wait_until="domcontentloaded", timeout=30000)
                    debug["goto_attempts"] = attempt + 1
                    break
                except Exception as e:
                    debug.setdefault("goto_errors", []).append(str(e)[:80])
                    time.sleep(3)
            page.wait_for_timeout(7000)

            # Cerrar cookies banner exterior
            for txt in ["Aceptar todas las cookies", "Rechazarlas todas"]:
                try:
                    btn = page.get_by_text(txt, exact=False).first
                    if btn.is_visible(timeout=1500):
                        btn.click()
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue

            # ─── PASO 1: detectar OFERTAS en quick-mode ANTES de cambiar a seatmap ───
            # TM muestra el panel de tipos (PIT, Golden Circle, Pista, Reservado…) en
            # modo "Ver lo mejor disponible". Capturamos nombre + precio min/max de cada uno
            # para que el frontend pueda mostrar al usuario qué tipos hay disponibles.
            offers = []
            try:
                ticket_selector.switch_to_quick_mode(page)
                page.wait_for_timeout(2500)
                # Aceptar consent intermedio si saltó
                try:
                    page.evaluate("""() => {
                        const m = Array.from(document.querySelectorAll('button')).find(b => /^acepto$/i.test((b.innerText||'').trim()));
                        if (m) m.click();
                    }""")
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                offers = ticket_selector.detect_offers(page) or []
                debug["offers_detected"] = len(offers)
            except Exception as e:
                debug["offers_err"] = f"{type(e).__name__}: {str(e)[:120]}"

            # ─── PASO 2: switch a Modo Ver mapa de asientos ───
            ticket_selector.switch_to_seatmap_mode(page)
            page.wait_for_timeout(4000)

            # Aceptar segundo modal "Acepto"
            page.evaluate("""() => {
                const m = Array.from(document.querySelectorAll('button')).find(b => /^acepto$/i.test((b.innerText||'').trim()));
                if (m) m.click();
                document.querySelectorAll('[id*="onetrust" i],[class*="onetrust" i]').forEach(e => e.remove());
            }""")
            page.wait_for_timeout(2000)

            # Esperar SVG con secciones
            n_sections = ticket_selector._wait_for_map(page, max_wait_s=20)
            debug["map_sections_loaded"] = n_sections

            # Extraer secciones (incluye activas e inactivas — el user querrá ver todas)
            sections = ticket_selector._detect_map_sections(page)
            debug["sections_detected"] = len(sections)

            # ─── API ismds: extraer secciones reales TM (más rápido que SVG) ───
            # Cada sección del SVG se cruza con el ID real de la API. Cuando el
            # bot vaya a comprar en "seatmap mode", podrá llamar a la API
            # directamente por section_id en vez de hacer click en el SVG.
            try:
                import tm_section_api as _tsa
                api_res = _tsa.fetch_via_browser(page, tm_id, qty=4)
                if not api_res.get("ok"):
                    # Fallback: HTTP directo
                    api_res = _tsa.fetch_availability(tm_id, country=country, qty=4)
                api_sections = api_res.get("sections", []) or []
                debug["api_sections_count"] = len(api_sections)
                debug["api_url"] = api_res.get("url", "")
                debug["api_via"] = api_res.get("via", "http")
                # Cruzar SVG ↔ API por nombre normalizado
                if api_sections and sections:
                    mapping = _tsa.best_section_match(api_sections, sections)
                    for s in sections:
                        api_id = mapping.get(s.get("section_id"))
                        if api_id:
                            s["tm_api_id"] = api_id
                    debug["sections_mapped_to_api"] = sum(
                        1 for s in sections if s.get("tm_api_id"))
            except Exception as _e:
                debug["api_sections_err"] = f"{type(_e).__name__}: {str(_e)[:120]}"
                api_sections = []

            # Screenshot del SVG (recortar al área del mapa)
            image_bytes = b""
            try:
                # Identificar el SVG del mapa (el más grande visible)
                svg_box = page.evaluate("""() => {
                    const svgs = Array.from(document.querySelectorAll('svg'));
                    svgs.sort((a,b)=>{const ra=a.getBoundingClientRect(),rb=b.getBoundingClientRect();return (rb.width*rb.height)-(ra.width*ra.height);});
                    if (!svgs.length) return null;
                    const r = svgs[0].getBoundingClientRect();
                    if (r.width < 100 || r.height < 100) return null;
                    return {x:Math.round(r.x), y:Math.round(r.y), width:Math.round(r.width), height:Math.round(r.height)};
                }""")
                if svg_box and svg_box.get("width") and svg_box.get("height"):
                    # Asegurar que el clip está dentro del viewport
                    vp = page.viewport_size or {"width": 1920, "height": 1080}
                    svg_box["x"] = max(0, svg_box["x"])
                    svg_box["y"] = max(0, svg_box["y"])
                    svg_box["width"] = min(svg_box["width"], vp["width"] - svg_box["x"])
                    svg_box["height"] = min(svg_box["height"], vp["height"] - svg_box["y"])
                    image_bytes = page.screenshot(clip=svg_box)
                    debug["image_clip"] = svg_box
                else:
                    image_bytes = page.screenshot(full_page=False)
                    debug["image_clip"] = "full"
            except Exception as e:
                debug["screenshot_err"] = str(e)[:120]

            b.close()
    except Exception as e:
        debug["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    finally:
        cleanup = _close_profile_with_cleanup(prof["env_id"], port, profile_label=prof["profile"])
        debug["cleanup"] = cleanup

    # Persistir en caché
    if not sections:
        return {"ok": False, "error": "no_sections_detected", "debug": debug,
                "offers": locals().get("offers", [])}

    venue = venue_cache.save_venue(
        venue_id=venue_id, name=name, city=city, country=country,
        sections=sections, image_bytes=image_bytes if image_bytes else None,
        tm_id=tm_id, source_url=tm_url,
    )

    # Persistir las api_sections (TM ismds IDs) en venue_cache para que el bot
    # las use como índice rápido en futuras compras con seatmap mode.
    try:
        if locals().get("api_sections"):
            import json_atomic as _ja, config as _cfg
            from datetime import datetime as _dt
            d = _ja.load(_cfg.ruta("venue_cache.json"),
                          default={"venues": {}, "by_event": {}})
            v = d.get("venues", {}).get(venue_id)
            if v:
                v["api_sections"] = api_sections
                v["api_sections_updated_at"] = _dt.now().isoformat()
                _ja.save(_cfg.ruta("venue_cache.json"), d)
    except Exception as _e:
        debug["api_persist_err"] = str(_e)[:120]

    # Persistir ofertas detectadas en data/event_offers.json (mismo path que
    # dashboard._OFFERS_PATH). El dashboard las lee desde ahí para poblar
    # el dropdown "Sección global (fallback)".
    offers_persisted = 0
    try:
        import json as _json
        from datetime import datetime
        offers_path = os.path.join(os.path.dirname(__file__), "data",
                                     "event_offers.json")
        os.makedirs(os.path.dirname(offers_path), exist_ok=True)
        try:
            with open(offers_path, encoding="utf-8") as f:
                offers_data = _json.load(f)
        except Exception:
            offers_data = {}
        if (locals().get("offers") or []):
            # Filtrar reventa fan-a-fan (Verified Resale): TM las lista junto con
            # las oficiales pero NO queremos que el bot las elija ni que el user
            # las vea en "Sección global (fallback)".
            FAN_RESALE_PATTERNS = (
                "fan a fan", "fan to fan", "fan-a-fan", "verified resale",
                "reventa", "resale", "fan-2-fan",
            )
            def _is_resale(o):
                name = (o.get("name") or "").lower()
                raw = (o.get("raw_text") or "").lower()
                return any(p in name or p in raw for p in FAN_RESALE_PATTERNS)
            offers_clean = [o for o in offers if not _is_resale(o)]
            debug["offers_filtered_resale"] = len(offers) - len(offers_clean)
            offers = offers_clean
            # Filtrar paquetes VIP / hospitality / meet&greet — el user los excluye
            # globalmente. NO confundir con "Entrada Platinum Oficial" (precios premium
            # dinámicos, no es paquete VIP) ni con la categoría genérica VIP/Premium.
            VIP_PACKAGE_PATTERNS = (
                "vip package", "paquete vip", "vip experience", "vip experiencia",
                "hospitality", "meet & greet", "meet and greet", "m&g",
                "backstage pass", "all-access", "all access", "fan experience",
                "premium experience", "premium package", "experience package",
            )
            def _is_vip_package(o):
                name = (o.get("name") or "").lower()
                raw = (o.get("raw_text") or "").lower()
                return any(p in name or p in raw for p in VIP_PACKAGE_PATTERNS)
            offers_clean2 = [o for o in offers if not _is_vip_package(o)]
            debug["offers_filtered_vip"] = len(offers) - len(offers_clean2)
            offers = offers_clean2
            normalized = []
            for o in offers:
                normalized.append({
                    "index": o.get("index"),
                    "name": o.get("name", ""),
                    "category": o.get("category", ""),
                    "price_min": o.get("price_min"),
                    "price_max": o.get("price_max"),
                    "raw_text": (o.get("raw_text") or "")[:300],
                })
            offers_data[str(tm_id)] = {
                "tm_id": str(tm_id), "tm_url": tm_url,
                "event_name": name, "venue": name,
                "offers": normalized,
                "scanned_at": datetime.now().isoformat(),
                "scan_method": "venue_scanner",
            }
            with open(offers_path, "w", encoding="utf-8") as f:
                _json.dump(offers_data, f, indent=2, ensure_ascii=False)
            offers_persisted = len(normalized)
    except Exception as e:
        debug["offers_persist_err"] = f"{type(e).__name__}: {str(e)[:120]}"

    return {"ok": True, "cached": False, "venue_id": venue_id,
            "venue": venue,
            "offers": locals().get("offers", []),
            "stats": {"sections": len(sections),
                      "active_sections": sum(1 for s in sections if s.get("active")),
                      "image_kb": (len(image_bytes) // 1024) if image_bytes else 0,
                      "offers_persisted": offers_persisted},
            "debug": debug}
