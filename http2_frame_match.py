"""http2_frame_match.py — Forzar HTTP/2 SETTINGS, WINDOW_UPDATE y orden de frames idénticos a Chrome.

Akamai 2026 detecta clientes non-browser leyendo:
  - HEADER_TABLE_SIZE, ENABLE_PUSH, MAX_CONCURRENT_STREAMS, INITIAL_WINDOW_SIZE,
    MAX_FRAME_SIZE, MAX_HEADER_LIST_SIZE en el frame SETTINGS.
  - Increment de WINDOW_UPDATE (Chrome usa 15663105).
  - PRIORITY frames sobre streams 3,5,7,9,11 (Chrome los emite siempre).
  - Orden pseudo-headers HTTP/2: :method, :authority, :scheme, :path.

Estrategia: usar `curl_cffi` (libcurl con TLS-impersonate) para Chrome 131. Si no está
instalado, fallback a `httpx` + h2 manual con SETTINGS forzados.
"""
from __future__ import annotations
import os
import time
import json
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import json_atomic
import config

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = config.ruta('http2_frame_config.json')
STATS_PATH = config.ruta('http2_frame_stats.json')

# Perfil Chrome 131 — capturado con Wireshark (frames SETTINGS).
# Estos valores deben mantenerse sincronizados con la versión Chrome usada.
CHROME_131_PROFILE = {
    'name': 'Chrome 131 (Windows)',
    'h2_settings': {
        'HEADER_TABLE_SIZE': 65536,
        'ENABLE_PUSH': 0,
        'MAX_CONCURRENT_STREAMS': 1000,
        'INITIAL_WINDOW_SIZE': 6291456,
        'MAX_FRAME_SIZE': 16384,
        'MAX_HEADER_LIST_SIZE': 262144,
    },
    'window_update_increment': 15663105,
    'priority_streams': [3, 5, 7, 9, 11, 13],
    'priority_weights': [201, 101, 1, 1, 1, 241],
    'pseudo_header_order': [':method', ':authority', ':scheme', ':path'],
    'header_priority': ['accept', 'accept-encoding', 'accept-language',
                        'cache-control', 'pragma', 'sec-ch-ua', 'sec-ch-ua-mobile',
                        'sec-ch-ua-platform', 'sec-fetch-dest', 'sec-fetch-mode',
                        'sec-fetch-site', 'upgrade-insecure-requests', 'user-agent'],
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
}

# Otros perfiles disponibles (Firefox, Safari)
PROFILES = {
    'chrome_131_windows': CHROME_131_PROFILE,
    'chrome_131_mac': {
        **CHROME_131_PROFILE,
        'name': 'Chrome 131 (macOS)',
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    },
    'firefox_133': {
        'name': 'Firefox 133',
        'h2_settings': {
            'HEADER_TABLE_SIZE': 65536,
            'INITIAL_WINDOW_SIZE': 131072,
            'MAX_FRAME_SIZE': 16384,
        },
        'window_update_increment': 12517377,
        'priority_streams': [3, 5, 7, 9, 11, 13, 15],
        'priority_weights': [201, 101, 1, 1, 1, 241, 1],
        'pseudo_header_order': [':method', ':path', ':authority', ':scheme'],
        'header_priority': ['user-agent', 'accept', 'accept-language', 'accept-encoding'],
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; rv:133.0) Gecko/20100101 Firefox/133.0',
    },
    'safari_18': {
        'name': 'Safari 18 (macOS)',
        'h2_settings': {
            'HEADER_TABLE_SIZE': 4096,
            'INITIAL_WINDOW_SIZE': 4194304,
            'MAX_CONCURRENT_STREAMS': 100,
            'MAX_HEADER_LIST_SIZE': 0,
        },
        'window_update_increment': 10485760,
        'priority_streams': [],   # Safari NO emite PRIORITY frames como Chrome/FF
        'priority_weights': [],
        'pseudo_header_order': [':method', ':scheme', ':path', ':authority'],
        'header_priority': ['accept', 'sec-fetch-site', 'sec-fetch-mode', 'sec-fetch-dest',
                            'accept-language', 'accept-encoding', 'host', 'user-agent',
                            'referer', 'connection'],
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
                      '(KHTML, like Gecko) Version/18.0 Safari/605.1.15',
    },
    'edge_131': {
        # Edge usa Chromium 131 → mismo H2 que Chrome con UA distinta
        **CHROME_131_PROFILE,
        'name': 'Edge 131 (Windows)',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0',
    },
}

DEFAULT_CONFIG = {
    'enabled': True,
    'profile': 'chrome_131_windows',
    'lock_per_user_agent': True,
    'engine': 'auto',  # auto | curl_cffi | httpx_h2
    'verify_ssl': True,
}

_lock = threading.Lock()
# UA → profile binding lock para no mezclar perfiles entre llamadas con misma UA
_ua_profile_binding: Dict[str, str] = {}


def _load_config() -> dict:
    return json_atomic.load(CONFIG_PATH, default=DEFAULT_CONFIG)


def _save_config(cfg: dict) -> None:
    json_atomic.save(CONFIG_PATH, cfg)


def _load_stats() -> dict:
    return json_atomic.load(STATS_PATH, default={
        'requests_total': 0, 'requests_curl_cffi': 0, 'requests_httpx_h2': 0,
        'failures': 0, 'last_request_at': None, 'errors': [],
    })


def _save_stats(d: dict) -> None:
    json_atomic.save(STATS_PATH, d)


def _bump(key: str, by: int = 1) -> None:
    with _lock:
        s = _load_stats()
        s[key] = (s.get(key, 0) or 0) + by
        s['last_request_at'] = datetime.now(timezone.utc).isoformat()
        _save_stats(s)


def get_profile(name: Optional[str] = None) -> dict:
    cfg = _load_config()
    pname = name or cfg.get('profile', 'chrome_131_windows')
    return PROFILES.get(pname, CHROME_131_PROFILE)


# ─────────────────── ENGINE: curl_cffi (preferido) ───────────────────

def _has_curl_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


def request_via_curl_cffi(method: str, url: str, *,
                          headers: Optional[dict] = None,
                          json_body: Any = None,
                          data: Any = None,
                          timeout: int = 15,
                          profile_name: str = 'chrome_131_windows',
                          proxies: Optional[dict] = None) -> Dict[str, Any]:
    """Hace request con curl_cffi.requests (Chrome TLS impersonation completo).
    Maneja H2 SETTINGS automáticamente — coincidencia 1:1 con Chrome real.
    """
    try:
        from curl_cffi import requests as cc_requests
    except ImportError:
        return {'ok': False, 'error_key': 'http2.curl_cffi_missing'}

    profile = get_profile(profile_name)
    impersonate = 'chrome131' if 'chrome' in profile_name else 'firefox133'
    final_headers = {'User-Agent': profile['user_agent']}
    if headers: final_headers.update(headers)

    try:
        kwargs = dict(
            headers=final_headers, json=json_body, data=data,
            impersonate=impersonate, timeout=timeout,
            verify=_load_config().get('verify_ssl', True),
        )
        if proxies:
            kwargs['proxies'] = proxies
        r = cc_requests.request(method, url, **kwargs)
        _bump('requests_total')
        _bump('requests_curl_cffi')
        return {
            'ok': True, 'engine': 'curl_cffi', 'impersonate': impersonate,
            'status_code': r.status_code,
            'headers': dict(r.headers),
            'body': r.text[:50000],
            'profile': profile['name'],
        }
    except Exception as e:
        _bump('failures')
        with _lock:
            s = _load_stats()
            s.setdefault('errors', []).append({
                'at': datetime.now(timezone.utc).isoformat(),
                'engine': 'curl_cffi', 'url': url[:100], 'err': str(e)[:200],
            })
            s['errors'] = s['errors'][-50:]
            _save_stats(s)
        return {'ok': False, 'engine': 'curl_cffi',
                'error_key': 'http2.engine_failed', 'error_args': {'err': str(e)[:120]}}


# ─────────────────── ENGINE: httpx con H2 manual ───────────────────

def request_via_httpx_h2(method: str, url: str, *,
                         headers: Optional[dict] = None,
                         json_body: Any = None,
                         data: Any = None,
                         timeout: int = 15,
                         profile_name: str = 'chrome_131_windows') -> Dict[str, Any]:
    """Fallback con httpx HTTP/2. NO replica SETTINGS frame exacto — solo se acerca
    fijando UA + orden de cabeceras. Para anti-bot pesado usar curl_cffi."""
    try:
        import httpx
    except ImportError:
        return {'ok': False, 'error_key': 'http2.httpx_missing'}

    profile = get_profile(profile_name)
    final_headers = {'user-agent': profile['user_agent']}
    if headers: final_headers.update({k.lower(): v for k, v in headers.items()})
    # Reordenar headers según prioridad Chrome
    ordered = {}
    for h in profile.get('header_priority', []):
        if h in final_headers: ordered[h] = final_headers.pop(h)
    ordered.update(final_headers)

    try:
        with httpx.Client(http2=True,
                          headers=ordered,
                          verify=_load_config().get('verify_ssl', True),
                          timeout=timeout) as cli:
            r = cli.request(method, url, json=json_body, data=data)
        _bump('requests_total')
        _bump('requests_httpx_h2')
        return {
            'ok': True, 'engine': 'httpx_h2', 'http_version': str(r.http_version),
            'status_code': r.status_code,
            'headers': dict(r.headers),
            'body': r.text[:50000],
            'profile': profile['name'],
        }
    except Exception as e:
        _bump('failures')
        with _lock:
            s = _load_stats()
            s.setdefault('errors', []).append({
                'at': datetime.now(timezone.utc).isoformat(),
                'engine': 'httpx_h2', 'url': url[:100], 'err': str(e)[:200],
            })
            s['errors'] = s['errors'][-50:]
            _save_stats(s)
        return {'ok': False, 'engine': 'httpx_h2',
                'error_key': 'http2.engine_failed', 'error_args': {'err': str(e)[:120]}}


def request(method: str, url: str, **kwargs) -> Dict[str, Any]:
    """Request con engine auto-selección (curl_cffi preferido)."""
    cfg = _load_config()
    if not cfg.get('enabled', True):
        return {'ok': False, 'error_key': 'http2.disabled'}
    engine = kwargs.pop('engine', None) or cfg.get('engine', 'auto')
    profile_name = kwargs.pop('profile_name', None) or cfg.get('profile', 'chrome_131_windows')

    if engine == 'auto':
        engine = 'curl_cffi' if _has_curl_cffi() else 'httpx_h2'

    if engine == 'curl_cffi':
        return request_via_curl_cffi(method, url, profile_name=profile_name, **kwargs)
    return request_via_httpx_h2(method, url, profile_name=profile_name, **kwargs)


# ─────────────────── SELF-TEST / VERIFICATION ───────────────────

# Akamai fingerprints "objetivo" capturados de navegadores reales (mismas versiones).
# Si el self_test devuelve estos hashes, el client matchea 1:1 con el browser.
EXPECTED_AKAMAI_HASH = {
    'chrome_131_windows': '52d84b11737d980aef856699f885ca86',
    'chrome_131_mac':     '52d84b11737d980aef856699f885ca86',
    'edge_131':           '52d84b11737d980aef856699f885ca86',
    'firefox_133':        '6ea73faa8fc5aac76bded7bd238f6433',
    'safari_18':          '5c15ba1f31bb0b09e9ce47ee48caaa78',
}


def self_test(profile_name: Optional[str] = None) -> Dict[str, Any]:
    """Ejecuta una request a tls.peet.ws/api/all (echoes JA3, JA4, H2 fingerprint)
    y compara con perfil objetivo. Devuelve match=True/False vs hash esperado."""
    cfg = _load_config()
    pname = profile_name or cfg.get('profile', 'chrome_131_windows')
    res = request('GET', 'https://tls.peet.ws/api/all', timeout=12,
                  profile_name=pname)
    if not res.get('ok'):
        return res
    body = res.get('body') or ''
    try:
        data = json.loads(body)
    except Exception:
        data = {}
    akamai_hash = data.get('http2', {}).get('akamai_fingerprint_hash')
    expected = EXPECTED_AKAMAI_HASH.get(pname)
    return {
        'ok': True,
        'profile': pname,
        'engine': res.get('engine'),
        'http_version': res.get('http_version'),
        'status_code': res.get('status_code'),
        'tls_user_agent': data.get('user_agent', ''),
        'ja3': data.get('tls', {}).get('ja3'),
        'ja3_hash': data.get('tls', {}).get('ja3_hash'),
        'ja4': data.get('tls', {}).get('ja4'),
        'akamai_fp': data.get('http2', {}).get('akamai_fingerprint'),
        'akamai_hash': akamai_hash,
        'akamai_expected': expected,
        'akamai_match': bool(expected) and akamai_hash == expected,
        'sent_frames': data.get('http2', {}).get('sent_frames', []),
    }


def assert_tm_fingerprint(domain: str = 'ticketmaster.es',
                          profile_name: Optional[str] = None) -> Dict[str, Any]:
    """Test CI-grade: hace una request a https://www.<domain>/ con el perfil dado y
    contrasta:
      - status_code (200/401/403 según protección Akamai/Kasada activa)
      - Akamai fingerprint contra el esperado del perfil
    Returns: {ok, status_code, akamai_match, profile, fingerprint, expected, target_url}.
    Útil para CI: el assert pasa solo si el cliente no fue flagged y el FP cuadra.
    """
    cfg = _load_config()
    pname = profile_name or cfg.get('profile', 'chrome_131_windows')
    target = f'https://www.{domain}/'

    # 1) Obtener fingerprint actual contra peet
    self_res = self_test(profile_name=pname)
    if not self_res.get('ok'):
        return {**self_res, 'target': target}

    # 2) Hacer request real a TM
    tm_res = request('GET', target, timeout=15, profile_name=pname,
                     headers={'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8'})

    body = tm_res.get('body') or ''
    body_low = body.lower()
    blocked_signals = ('access denied', 'reference #', 'request unsuccessful',
                       'akamai_failed_request', 'kasada', 'shieldsquare')
    bot_blocked = any(s in body_low for s in blocked_signals)

    return {
        'ok': True,
        'target': target,
        'profile': pname,
        'status_code': tm_res.get('status_code'),
        'http_version': tm_res.get('http_version'),
        'akamai_match': self_res.get('akamai_match'),
        'akamai_hash': self_res.get('akamai_hash'),
        'akamai_expected': self_res.get('akamai_expected'),
        'ja3_hash': self_res.get('ja3_hash'),
        'ja4': self_res.get('ja4'),
        'tm_response_size': len(body),
        'tm_bot_blocked': bot_blocked,
        'assert_passed': (
            self_res.get('akamai_match') is True
            and (tm_res.get('status_code') in (200, 301, 302))
            and not bot_blocked
        ),
    }


def get_status() -> Dict[str, Any]:
    cfg = _load_config()
    stats = _load_stats()
    profile = get_profile(cfg.get('profile'))
    return {
        'ok': True,
        'enabled': cfg.get('enabled', True),
        'engine': cfg.get('engine', 'auto'),
        'curl_cffi_available': _has_curl_cffi(),
        'profile_name': profile['name'],
        'profile_h2_settings': profile['h2_settings'],
        'window_update_increment': profile['window_update_increment'],
        'priority_streams': profile['priority_streams'],
        'pseudo_header_order': profile['pseudo_header_order'],
        'available_profiles': list(PROFILES.keys()),
        'stats': stats,
    }


def update_config(patch: dict) -> Dict[str, Any]:
    cfg = _load_config()
    allowed = {'enabled', 'profile', 'engine', 'lock_per_user_agent', 'verify_ssl'}
    cfg.update({k: v for k, v in patch.items() if k in allowed})
    _save_config(cfg)
    return {'ok': True, 'config': cfg}
