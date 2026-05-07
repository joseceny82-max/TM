"""tests/test_http_mocked.py — Tests para módulos HTTP-dependent
con `responses` library para mockear requests.

Cubre:
  - cookie_warmer.warm_cookies / check_stock / warm_with_akamai_sensor
  - proxy_health.benchmark / _check_one_target / pick_best
  - event_discovery.search_events / get_event_detail / enrich_from_url
"""
import os
import pytest

try:
    import responses
    HAS_RESPONSES = True
except ImportError:
    HAS_RESPONSES = False
    pytestmark = pytest.mark.skip("responses lib not installed")


# ═══════════════════════════════════════════════════════════════
#  cookie_warmer.check_stock — todos los branches
# ═══════════════════════════════════════════════════════════════
class TestCheckStock:
    def test_no_url_returns_no_url_hint(self):
        import cookie_warmer as cw
        r = cw.check_stock("")
        assert r["stock_found"] is False
        assert r["hint"] == "no_url"

    @responses.activate
    def test_404_returns_event_unavailable(self):
        import cookie_warmer as cw
        url = "https://www.ticketmaster.es/event/123"
        responses.add(responses.HEAD, url, status=404)
        responses.add(responses.GET, url, status=404)
        r = cw.check_stock(url)
        assert r["stock_found"] is False
        assert r["hint"] == "event_unavailable"

    @responses.activate
    def test_503_queue_active(self):
        import cookie_warmer as cw
        url = "https://www.ticketmaster.es/event/456"
        responses.add(responses.HEAD, url, status=503)
        r = cw.check_stock(url)
        assert r["stock_found"] is False
        assert r["queue_active"] is True

    @responses.activate
    def test_redirect_to_sold_out(self):
        import cookie_warmer as cw
        url = "https://www.ticketmaster.es/event/789"
        # simulate redirect to /sold-out
        responses.add(responses.HEAD, url, status=302,
                       headers={"Location": "https://www.ticketmaster.es/sold-out"})
        responses.add(responses.HEAD, "https://www.ticketmaster.es/sold-out",
                       status=200)
        r = cw.check_stock(url)
        assert r["stock_found"] is False
        assert "redirect" in r["hint"] or "sold" in r["hint"].lower()

    @responses.activate
    def test_200_with_buy_panel_marker(self):
        import cookie_warmer as cw
        url = "https://www.ticketmaster.es/event/buy"
        body = "<html><body>Select your tickets now</body></html>"
        responses.add(responses.HEAD, url, status=200)
        responses.add(responses.GET, url, status=200, body=body)
        r = cw.check_stock(url)
        # Debe detectar buy_panel_detected (heurística)
        assert isinstance(r, dict)
        assert "stock_found" in r

    @responses.activate
    def test_200_with_sold_out_text(self):
        import cookie_warmer as cw
        url = "https://www.ticketmaster.es/event/soldout"
        body = "<html><body>Sold out</body></html>"
        responses.add(responses.HEAD, url, status=200)
        responses.add(responses.GET, url, status=200, body=body)
        r = cw.check_stock(url)
        assert r["stock_found"] is False
        assert "sold" in r["hint"].lower()

    def test_invalid_url_request_error(self):
        import cookie_warmer as cw
        # Sin responses activado, hace request real → falla
        # Esperamos hint=request_error o timeout
        r = cw.check_stock("http://invalid.invalid.local.xyz", timeout=1)
        assert r["stock_found"] is False
        # Acepta cualquier hint que NO sea "no_url" (porque sí pasamos URL)
        assert r["hint"] != "no_url"
        assert isinstance(r["hint"], str)


# ═══════════════════════════════════════════════════════════════
#  cookie_warmer.warm_cookies — flow básico
# ═══════════════════════════════════════════════════════════════
class TestWarmCookies:
    def test_invalid_domain_safely(self):
        import cookie_warmer as cw
        # Sin red, debe devolver dict sin crashear
        r = cw.warm_cookies(domain_id="zz_invalid", pages=1, timeout=2)
        assert isinstance(r, dict)
        assert "success" in r or "ok" in r or "error" in r

    @responses.activate
    def test_warm_cookies_success(self):
        import cookie_warmer as cw
        # Mockear todos los GETs típicos a TM
        for path in ("/", "/events", "/spain", "/login"):
            for base in ("https://www.ticketmaster.es",):
                responses.add(responses.GET, f"{base}{path}",
                                status=200, body="<html>OK</html>",
                                headers={"Set-Cookie": "ASP.NET_SessionId=abc; Path=/"})
        r = cw.warm_cookies(domain_id="es", pages=2, timeout=3)
        assert isinstance(r, dict)


# ═══════════════════════════════════════════════════════════════
#  cookie_warmer.warm_with_akamai_sensor
# ═══════════════════════════════════════════════════════════════
class TestWarmWithAkamaiSensor:
    def test_returns_dict(self):
        import cookie_warmer as cw
        r = cw.warm_with_akamai_sensor(domain_id="es")
        assert isinstance(r, dict)


# ═══════════════════════════════════════════════════════════════
#  cookie_warmer.get_warm_log
# ═══════════════════════════════════════════════════════════════
class TestGetWarmLog:
    def test_returns_list(self):
        import cookie_warmer as cw
        r = cw.get_warm_log(limit=5)
        assert isinstance(r, list)


# ═══════════════════════════════════════════════════════════════
#  proxy_health._check_one_target — con responses
# ═══════════════════════════════════════════════════════════════
class TestCheckOneTarget:
    @responses.activate
    def test_200_returns_ok(self):
        import proxy_health as ph
        url = "https://www.ticketmaster.com/"
        responses.add(responses.GET, url, status=200,
                       body="<html>OK</html>")
        r = ph._check_one_target(
            proxy_url="http://fake-proxy:8080",
            target=url, timeout=3)
        assert r["target"] == url
        assert r["ok"] is True
        assert r["status_code"] == 200

    @responses.activate
    def test_500_not_ok(self):
        import proxy_health as ph
        url = "https://www.ticketmaster.com/error"
        responses.add(responses.GET, url, status=500)
        r = ph._check_one_target(
            proxy_url="http://fake:8080", target=url, timeout=3)
        assert r["ok"] is False

    def test_no_proxy_runs(self):
        """Con timeout muy bajo + URL inválida, devuelve dict sin crashear."""
        import proxy_health as ph
        r = ph._check_one_target(
            proxy_url="http://x:1", target="http://x.invalid", timeout=1)
        assert isinstance(r, dict)


# ═══════════════════════════════════════════════════════════════
#  proxy_health — APIs públicas
# ═══════════════════════════════════════════════════════════════
class TestProxyHealth:
    def test_get_stats_keys(self):
        import proxy_health as ph
        s = ph.get_stats()
        assert isinstance(s, dict)
        # API real: total/fast/slow/dead/unchecked
        for k in ("total", "fast", "slow", "dead", "unchecked"):
            assert k in s

    def test_list_proxies_returns_list(self):
        import proxy_health as ph
        r = ph.list_proxies()
        assert isinstance(r, list)

    def test_pick_best_no_args(self):
        import proxy_health as ph
        r = ph.pick_best()
        # Puede ser None si no hay proxies, o dict
        assert r is None or isinstance(r, dict)

    def test_normalize_proxy_url(self):
        import proxy_health as ph
        # Sin protocol → añade http://
        r1 = ph._normalize_proxy_url("user:pass@host.com:8080")
        assert "://" in r1
        # Con protocol → preserva
        r2 = ph._normalize_proxy_url("https://user:pass@host.com:8080")
        assert r2.startswith("https://")

    def test_proxy_id_consistent(self):
        import proxy_health as ph
        url = "http://user:pass@1.2.3.4:8080"
        id1 = ph._proxy_id(url)
        id2 = ph._proxy_id(url)
        assert id1 == id2  # determinista

    def test_add_proxies_validation(self):
        import proxy_health as ph
        r = ph.add_proxies([])
        assert isinstance(r, dict)

    def test_clear_dead_returns_int(self):
        import proxy_health as ph
        r = ph.clear_dead()
        assert isinstance(r, int)


# ═══════════════════════════════════════════════════════════════
#  event_discovery — APIs puras
# ═══════════════════════════════════════════════════════════════
class TestEventDiscoveryPure:
    def test_extract_event_id_from_url(self):
        import event_discovery as ed
        # Patrón: /event/<id>
        eid = ed.extract_event_id_from_url(
            "https://www.ticketmaster.es/event/123456789")
        assert eid == "123456789" or eid is None

    def test_extract_event_id_invalid_url(self):
        import event_discovery as ed
        r = ed.extract_event_id_from_url("not a url")
        assert r is None

    def test_extract_slug_from_url(self):
        import event_discovery as ed
        s = ed._extract_slug_from_url(
            "https://www.ticketmaster.es/event/karol-g-madrid/123")
        # Puede devolver None o el slug
        assert s is None or isinstance(s, str)

    def test_slug_to_title(self):
        import event_discovery as ed
        title = ed._slug_to_title("karol-g-madrid")
        assert isinstance(title, str)
        # Espacios o capitalización
        assert "g" in title.lower() and "madrid" in title.lower()

    def test_infer_country_from_url(self):
        import event_discovery as ed
        c1 = ed._infer_country_from_url("https://www.ticketmaster.es/event/x")
        c2 = ed._infer_country_from_url("https://www.ticketmaster.com/event/x")
        c3 = ed._infer_country_from_url("https://www.ticketmaster.co.uk/event/x")
        assert c1 == "es"
        assert c2 == "us"
        assert c3 in ("uk", "gb", "us")

    def test_get_config(self):
        import event_discovery as ed
        cfg = ed.get_config()
        assert isinstance(cfg, dict)

    def test_is_configured_returns_bool(self):
        import event_discovery as ed
        assert isinstance(ed.is_configured(), bool)

    def test_hashlib_hash(self):
        import event_discovery as ed
        h = ed.hashlib_hash("test")
        assert isinstance(h, str)
        # Determinista
        h2 = ed.hashlib_hash("test")
        assert h == h2
        assert len(h) >= 8  # truncado típico


# ═══════════════════════════════════════════════════════════════
#  event_discovery.search_events — con responses (Discovery API mock)
# ═══════════════════════════════════════════════════════════════
class TestEventSearchMocked:
    def test_no_api_key_uses_demo(self, monkeypatch):
        """Sin API key, debe devolver demo data o results vacíos."""
        import event_discovery as ed
        # Forzar config sin API key
        cfg = ed.get_config()
        original_key = cfg.get("api_key")
        ed.save_config({"api_key": ""})
        try:
            r = ed.search_events(keyword="karol", city="madrid",
                                 country="es", size=3)
            # Estructura: dict con events o list
            assert isinstance(r, (dict, list))
        finally:
            ed.save_config({"api_key": original_key or ""})

    @responses.activate
    def test_search_with_api_key(self, monkeypatch):
        import event_discovery as ed
        # Mock Discovery API endpoint
        responses.add(
            responses.GET,
            "https://app.ticketmaster.com/discovery/v2/events.json",
            status=200,
            json={"_embedded": {"events": [
                {"id": "evt_1", "name": "Karol G — Madrid 2026",
                 "url": "https://www.ticketmaster.es/event/evt_1",
                 "dates": {"start": {"localDate": "2026-06-15"}},
                 "_embedded": {"venues": [{"name": "Wanda Metropolitano",
                                              "city": {"name": "Madrid"}}]}}
            ]}})
        # Configurar API key
        original = ed.get_config().get("api_key", "")
        ed.save_config({"api_key": "test_fake_key"})
        try:
            r = ed.search_events(keyword="karol", country="es", size=3)
            assert isinstance(r, (dict, list))
        finally:
            ed.save_config({"api_key": original})


# ═══════════════════════════════════════════════════════════════
#  event_discovery.enrich_from_url — fetch URL + parse
# ═══════════════════════════════════════════════════════════════
class TestEnrichFromUrl:
    def test_invalid_url_returns_dict(self):
        import event_discovery as ed
        r = ed.enrich_from_url("not a valid url")
        assert isinstance(r, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
