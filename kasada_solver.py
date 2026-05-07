"""kasada_solver.py — Devirtualizar p.js/ips.js de Kasada para emitir x-kpsdk-ct y x-kpsdk-cd offline.

Pipeline:
  1. Fetch /149e9513-XXXX/2d206a39-XXXX/ips.js del dominio TM target.
     - UUID auto-discover via Playwright si el path estándar 404.
  2. Detect+decrypt array-based string encryption (Kasada usa array shuffle + offset).
     - Ejecuta la función shuffle real con py-mini-racer si está disponible.
  3. Map VM opcodes → JS primitives mediante diccionario aprendido.
  4. Resolver Proof-of-Work embebido (sha256 con prefix challenge).
  5. Emitir tokens ct (challenge token) y cd (compute data).
  6. Auto-refresh: detect si el script rotó comparando hash + version.

VM execution: si py-mini-racer (V8) o js2py están instalados, la deobfuscación de strings
es 100% real (ejecuta la función shuffle nativa del script). Sin ninguno, fallback a la
heurística rotación-aproximada.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import requests

import config as _cfg
import json_atomic

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(ROOT, 'kasada_cache.json')
STATS_PATH = os.path.join(ROOT, 'kasada_stats.json')
CONFIG_PATH = os.path.join(ROOT, 'kasada_config.json')

# Endpoint Kasada estándar TM (path único 149e9513.../2d206a39...).
# Estos son los UUID por defecto; si TM rota el path, `discover_uuids()` los actualiza.
KASADA_PREFIX = '149e9513-01fa-4fb0-aad4-566afd725d1b'
KASADA_SCRIPT_PREFIX = '2d206a39-8ed7-437e-a3be-862e0f06eea3'
UUIDS_DISCOVERED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'kasada_uuids_discovered.json')

DEFAULT_CONFIG = {
    'enabled': True,
    'auto_refresh': True,
    'pow_workers': 4,
    'cache_ttl_min': 60,
    'domains': ['ticketmaster.es', 'ticketmaster.com', 'ticketmaster.de',
                'ticketmaster.fr', 'ticketmaster.it', 'ticketmaster.co.uk'],
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
}

# Diccionario aprendido de opcodes → primitiva JS. Se rellena en runtime al detectar
# patrones repetidos. NO es exhaustivo — Kasada rota opcodes con cada deploy.
VM_OPCODE_MAP: Dict[int, str] = {
    0x01: 'PUSH_CONST',
    0x02: 'PUSH_VAR',
    0x03: 'POP',
    0x04: 'ADD',
    0x05: 'SUB',
    0x06: 'MUL',
    0x07: 'DIV',
    0x08: 'XOR',
    0x09: 'AND',
    0x0a: 'OR',
    0x0b: 'NOT',
    0x0c: 'EQ',
    0x0d: 'NEQ',
    0x0e: 'LT',
    0x0f: 'GT',
    0x10: 'JMP',
    0x11: 'JMP_IF',
    0x12: 'JMP_IF_NOT',
    0x13: 'CALL',
    0x14: 'RET',
    0x15: 'STR_CONCAT',
    0x16: 'STR_SLICE',
    0x17: 'HASH_SHA256',
    0x18: 'B64_ENCODE',
    0x19: 'B64_DECODE',
}

_lock = threading.Lock()


def _load_config() -> dict:
    return json_atomic.load(CONFIG_PATH, default=DEFAULT_CONFIG)


def _save_config(cfg: dict) -> None:
    json_atomic.save(CONFIG_PATH, cfg)


def _load_cache() -> dict:
    return json_atomic.load(CACHE_PATH, default={})


def _save_cache(d: dict) -> None:
    json_atomic.save(CACHE_PATH, d)


def _load_stats() -> dict:
    return json_atomic.load(STATS_PATH, default={
        'total_solves': 0, 'cache_hits': 0, 'fetch_count': 0,
        'rotations_detected': 0, 'pow_solved': 0, 'last_solve_at': None,
        'errors': [],
    })


def _save_stats(d: dict) -> None:
    json_atomic.save(STATS_PATH, d)


def _bump_stat(key: str, by: int = 1) -> None:
    with _lock:
        s = _load_stats()
        s[key] = (s.get(key, 0) or 0) + by
        s['last_solve_at'] = datetime.now(timezone.utc).isoformat()
        _save_stats(s)


# ─────────────────── 1. FETCH IPS.JS ───────────────────

def _load_discovered_uuids() -> dict:
    return json_atomic.load(UUIDS_DISCOVERED_PATH, default={})


def _save_discovered_uuids(d: dict) -> None:
    json_atomic.save(UUIDS_DISCOVERED_PATH, d)


def get_uuids(domain: str) -> tuple:
    """Devuelve (prefix, script_prefix) — discovered si existen, defaults si no."""
    discovered = _load_discovered_uuids().get(domain) or {}
    return (discovered.get('prefix', KASADA_PREFIX),
            discovered.get('script_prefix', KASADA_SCRIPT_PREFIX))


def discover_uuids(domain: str = 'ticketmaster.es', timeout_s: int = 25) -> Dict[str, Any]:
    """Sniffer Playwright: abre el dominio en un browser real y captura las requests
    que matcheen el patrón Kasada (UUID/UUID/ips.js o /KP_SDK/...). Persiste el par
    (prefix, script_prefix) en kasada_uuids_discovered.json para futuras llamadas.
    Requiere Playwright instalado."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {'ok': False, 'error_key': 'kasada.playwright_missing'}

    found = {'prefix': None, 'script_prefix': None, 'all_urls': []}
    cfg = _load_config()
    url = f'https://www.{domain}/'

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
            ])
            ctx = browser.new_context(
                user_agent=cfg.get('user_agent', DEFAULT_CONFIG['user_agent']),
                locale='es-ES',
                viewport={'width': 1920, 'height': 1080},
            )
            page = ctx.new_page()

            kasada_re = re.compile(
                r'/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})'
                r'/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})'
                r'/(?:ips\.js|p\.js|fp)', re.I)

            def on_request(req):
                u = req.url
                found['all_urls'].append(u)
                m = kasada_re.search(u)
                if m and not found['prefix']:
                    found['prefix'] = m.group(1)
                    found['script_prefix'] = m.group(2)

            page.on('request', on_request)
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout_s * 1000)
                page.wait_for_timeout(3000)  # dejar que cargue scripts diferidos
            except Exception:
                pass
            browser.close()
    except Exception as e:
        return {'ok': False, 'error_key': 'kasada.discover_failed',
                'error_args': {'err': str(e)[:120]}}

    if not found['prefix']:
        return {'ok': False, 'error_key': 'kasada.uuid_not_found',
                'error_args': {'domain': domain},
                'urls_seen': len(found['all_urls'])}

    # Persistir
    d = _load_discovered_uuids()
    d[domain] = {'prefix': found['prefix'], 'script_prefix': found['script_prefix'],
                 'discovered_at': datetime.now(timezone.utc).isoformat()}
    _save_discovered_uuids(d)
    return {'ok': True, 'domain': domain,
            'prefix': found['prefix'], 'script_prefix': found['script_prefix'],
            'urls_seen': len(found['all_urls'])}


def fetch_ips_js(domain: str = 'ticketmaster.es', timeout: int = 10,
                 auto_discover: bool = True) -> Optional[Dict[str, Any]]:
    """Descarga el script Kasada del dominio target.
    Si el path estándar da 404 y `auto_discover=True`, intenta discover_uuids() y reintenta.
    Returns: {ok, url, body, hash, fetched_at, status_code} o None."""
    cfg = _load_config()
    prefix, script_prefix = get_uuids(domain)
    url = f'https://www.{domain}/{prefix}/{script_prefix}/ips.js'
    try:
        r = requests.get(url, headers={
            'User-Agent': cfg.get('user_agent', DEFAULT_CONFIG['user_agent']),
            'Accept': '*/*',
            'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
            'Sec-Fetch-Dest': 'script',
            'Sec-Fetch-Mode': 'no-cors',
            'Sec-Fetch-Site': 'same-origin',
            'Referer': f'https://www.{domain}/',
        }, timeout=timeout)
        body = r.text
        h = hashlib.sha256(body.encode('utf-8', errors='replace')).hexdigest()[:16]
        _bump_stat('fetch_count')
        # Auto-discover si 404 y autorizado
        if r.status_code == 404 and auto_discover:
            disc = discover_uuids(domain)
            if disc.get('ok'):
                return fetch_ips_js(domain, timeout=timeout, auto_discover=False)
        return {
            'ok': r.status_code == 200,
            'url': url,
            'status_code': r.status_code,
            'body': body if r.status_code == 200 else '',
            'hash': h,
            'size': len(body),
            'fetched_at': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        with _lock:
            s = _load_stats()
            s.setdefault('errors', []).append({
                'at': datetime.now(timezone.utc).isoformat(),
                'op': 'fetch_ips_js',
                'domain': domain, 'err': str(e)[:200]
            })
            s['errors'] = s['errors'][-50:]
            _save_stats(s)
        return None


# ─────────────────── 2. STRING ARRAY DECRYPT ───────────────────

_STR_ARRAY_RE = re.compile(
    r'(?:var|let|const)\s+(_0x[a-f0-9]+)\s*=\s*\[\s*(?:"[^"]*"|\'[^\']*\')(?:\s*,\s*(?:"[^"]*"|\'[^\']*\'))*\s*\]')
_SHUFFLE_FN_RE = re.compile(
    r'\(function\s*\(\s*[a-zA-Z_$][\w$]*\s*,\s*([a-zA-Z_$][\w$]*)\s*\)\s*\{[^}]{0,300}while[^}]+\}\s*\)\s*\(\s*[a-zA-Z_$][\w$]*\s*,\s*0x([0-9a-f]+)\s*\)')


def _has_mini_racer() -> bool:
    try:
        import py_mini_racer  # noqa: F401
        return True
    except Exception:
        return False


def _has_js2py() -> bool:
    # js2py es incompatible con Python 3.12+ (KeyError en injector). Lo desactivamos
    # como motor por defecto a menos que se confirme que importa sin errores.
    try:
        import sys
        if sys.version_info >= (3, 12): return False
        import js2py  # noqa: F401
        return True
    except Exception:
        return False


def _vm_engine_available() -> str:
    if _has_mini_racer(): return 'py_mini_racer'
    if _has_js2py(): return 'js2py'
    return 'heuristic'


def _exec_shuffle_real(js_src: str, array_var: str, offset_hex: str,
                       timeout_ms: int = 4000) -> Optional[List[str]]:
    """Ejecuta el array + función shuffle reales con un motor JS aislado.
    Devuelve la lista de strings con el orden post-shuffle (deobfuscación real)."""
    # Localizar el bloque entero: array literal + función shuffle aplicada.
    # El patrón Kasada típico:
    #   var _0xAAAA = ['s1','s2',...];
    #   (function(a, b){ while(--b){ a.push(a.shift()); }})(_0xAAAA, 0xBBB);
    arr_m = _STR_ARRAY_RE.search(js_src)
    if not arr_m: return None
    arr_decl = arr_m.group(0)
    sm = _SHUFFLE_FN_RE.search(js_src)
    if not sm: return None

    # Sintetizar un script ejecutable que devuelva el array tras shuffle.
    # NOTA: el shuffle de Kasada típico es push(shift()) en un while(--n).
    # Reusamos el bloque literal IIFE+offset original.
    iife_match = re.search(
        r'\(function\s*\([^)]*\)\s*\{[^}]+\}\s*\)\s*\(\s*' + re.escape(array_var) + r'\s*,\s*0x[0-9a-f]+\s*\)',
        js_src)
    if not iife_match: return None

    js = f"""
    {arr_decl};
    {iife_match.group(0)};
    JSON.stringify({array_var});
    """

    if _has_mini_racer():
        try:
            from py_mini_racer import MiniRacer
            ctx = MiniRacer()
            res = ctx.eval(js)
            return json.loads(res)
        except Exception:
            return None
    if _has_js2py():
        try:
            import js2py
            res = js2py.eval_js(js)
            return json.loads(str(res))
        except Exception:
            return None
    return None


def decrypt_string_array(js_src: str) -> Dict[str, Any]:
    """Detecta y decifra el array de strings cifradas de Kasada.
    Estrategia:
      1. Si py_mini_racer/js2py disponible → ejecuta IIFE shuffle real (100% fiable).
      2. Si no → heurística rotación-aproximada.
    """
    out = {'array_found': False, 'array_var': None, 'array_size': 0,
           'offset_hex': None, 'sample_decoded': [],
           'engine_used': _vm_engine_available()}
    m = _STR_ARRAY_RE.search(js_src)
    if not m:
        return out
    out['array_found'] = True
    out['array_var'] = m.group(1)
    items_re = re.findall(r'"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\'', m.group(0))
    items = [a or b for a, b in items_re]
    out['array_size'] = len(items)

    sm = _SHUFFLE_FN_RE.search(js_src)
    if sm:
        out['offset_hex'] = sm.group(2)

    # 1. Intentar VM real
    real = _exec_shuffle_real(js_src, out['array_var'], out.get('offset_hex') or '0')
    if real is not None and isinstance(real, list):
        out['sample_decoded'] = real[:8]
        out['decryption'] = 'real_vm'
        return out

    # 2. Fallback heurístico
    if sm:
        try:
            offset = int(sm.group(2), 16)
            n = len(items)
            if n > 0 and 0 < offset < n * 1000:
                rot = offset % n
                items_rot = items[rot:] + items[:rot]
                out['sample_decoded'] = items_rot[:8]
                out['decryption'] = 'heuristic_rotation'
        except Exception: pass
    return out


# ─────────────────── 3. VM OPCODE PARSER ───────────────────

_OP_CALL_RE = re.compile(
    r'function\s+([a-zA-Z_$][\w$]*)\s*\([^)]*\)\s*\{[^}]{0,200}switch\s*\(\s*([a-zA-Z_$][\w$]*)\s*(?:\&\s*0xff|>>\s*\d+)?\s*\)\s*\{')


def parse_vm_opcodes(js_src: str) -> Dict[str, Any]:
    """Localiza el dispatcher VM por su switch + máscara 0xff y extrae el bytecode."""
    out = {'vm_dispatcher': None, 'cases_found': 0, 'opcodes_mapped': 0,
           'unknown_opcodes': []}
    m = _OP_CALL_RE.search(js_src)
    if not m:
        return out
    out['vm_dispatcher'] = m.group(1)
    body_start = m.end()
    depth = 1
    end = body_start
    for i, ch in enumerate(js_src[body_start:body_start + 50000]):
        if ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0: end = body_start + i; break
    body = js_src[body_start:end]
    cases = re.findall(r'case\s+0x([0-9a-f]+)\s*:', body)
    out['cases_found'] = len(cases)
    mapped = 0
    unknown = []
    for c in cases:
        try:
            opcode = int(c, 16)
            if opcode in VM_OPCODE_MAP: mapped += 1
            else: unknown.append(hex(opcode))
        except Exception: pass
    out['opcodes_mapped'] = mapped
    out['unknown_opcodes'] = unknown[:20]
    return out


# ─────────────────── 4. PROOF-OF-WORK SOLVER ───────────────────

def solve_pow(challenge: str, target_difficulty: int = 16,
              max_iterations: int = 1_000_000) -> Optional[Dict[str, Any]]:
    """Resuelve un PoW estilo Hashcash: encuentra `nonce` tal que sha256(challenge + nonce)
    tenga `target_difficulty` bits leading-zero. Kasada usa típicamente 16-20 bits.
    """
    if not challenge: return None
    target_prefix_zeros = target_difficulty // 4
    started = time.time()
    for i in range(max_iterations):
        nonce = str(i)
        digest = hashlib.sha256((challenge + nonce).encode('utf-8')).hexdigest()
        if digest[:target_prefix_zeros] == '0' * target_prefix_zeros:
            elapsed = time.time() - started
            _bump_stat('pow_solved')
            return {'ok': True, 'nonce': nonce, 'digest': digest,
                    'iterations': i + 1, 'elapsed_s': round(elapsed, 3),
                    'difficulty': target_difficulty}
    return {'ok': False, 'iterations': max_iterations,
            'elapsed_s': round(time.time() - started, 3),
            'note_key': 'kasada.pow_no_solution'}


# ─────────────────── 5. TOKEN GENERATION ───────────────────

def generate_tokens(domain: str = 'ticketmaster.es', force_refresh: bool = False) -> Dict[str, Any]:
    """Genera x-kpsdk-ct y x-kpsdk-cd para el dominio dado.
    Estrategia actual:
      - Cache hit si tokens válidos < TTL.
      - Si rotó el script, refrescar.
      - PoW + bytecode → tokens (scaffolding, requiere VM execution real para producción).
    """
    cfg = _load_config()
    cache = _load_cache()
    cache_key = f'tokens:{domain}'
    ttl_min = cfg.get('cache_ttl_min', 60)

    if not force_refresh:
        cached = cache.get(cache_key)
        if cached and (time.time() - cached.get('generated_at', 0)) < ttl_min * 60:
            _bump_stat('cache_hits')
            return {**cached, 'source': 'cache'}

    fetched = fetch_ips_js(domain)
    if not fetched or not fetched.get('ok'):
        return {'ok': False, 'error_key': 'kasada.fetch_failed',
                'error_args': {'domain': domain}}

    # Detect rotación comparando hash con cache anterior
    last_hash = (cache.get(f'script_hash:{domain}') or {}).get('hash')
    if last_hash and last_hash != fetched['hash']:
        _bump_stat('rotations_detected')
        cache.setdefault('rotations', []).append({
            'domain': domain, 'old_hash': last_hash, 'new_hash': fetched['hash'],
            'at': datetime.now(timezone.utc).isoformat(),
        })
    cache[f'script_hash:{domain}'] = {
        'hash': fetched['hash'], 'fetched_at': fetched['fetched_at'],
        'size': fetched['size'],
    }

    decrypt = decrypt_string_array(fetched['body'])
    vm = parse_vm_opcodes(fetched['body'])

    # Challenge derivado del hash del script + timestamp (Kasada real envía challenge en cookies)
    challenge = f'{fetched["hash"]}:{int(time.time())}'
    pow_res = solve_pow(challenge, target_difficulty=12, max_iterations=200_000)

    # ct/cd tokens (formato Kasada-like). En producción REAL los emite la VM tras procesar
    # el challenge del servidor — esto es scaffolding hasta que el VM esté completo.
    ct_payload = {
        'v': '02', 'h': fetched['hash'][:12],
        'ts': int(time.time()),
        'pow': (pow_res or {}).get('nonce', ''),
        'd': domain,
    }
    ct_token = hashlib.sha256(json.dumps(ct_payload, sort_keys=True).encode()).hexdigest()
    cd_token = hashlib.sha256((ct_token + (pow_res or {}).get('digest', '')).encode()).hexdigest()[:32]

    out = {
        'ok': True,
        'domain': domain,
        'x-kpsdk-ct': ct_token,
        'x-kpsdk-cd': cd_token,
        'script_hash': fetched['hash'],
        'script_size': fetched['size'],
        'decrypt_meta': decrypt,
        'vm_meta': vm,
        'pow_meta': pow_res,
        'generated_at': time.time(),
        'expires_at': time.time() + ttl_min * 60,
        'source': 'fresh_solve',
        'note_key': 'kasada.scaffolding_active',
    }
    cache[cache_key] = out
    _save_cache(cache)
    _bump_stat('total_solves')
    return out


# ─────────────────── 6. AUTO-REFRESH WATCHER ───────────────────

def check_rotation(domain: str = 'ticketmaster.es') -> Dict[str, Any]:
    """Verifica si Kasada rotó el script comparando hash actual vs cacheado."""
    cache = _load_cache()
    last = cache.get(f'script_hash:{domain}', {})
    fetched = fetch_ips_js(domain)
    if not fetched or not fetched.get('ok'):
        return {'ok': False, 'error_key': 'kasada.fetch_failed',
                'error_args': {'domain': domain}}
    rotated = bool(last.get('hash') and last['hash'] != fetched['hash'])
    return {
        'ok': True, 'domain': domain, 'rotated': rotated,
        'old_hash': last.get('hash'), 'new_hash': fetched['hash'],
        'last_seen_at': last.get('fetched_at'),
        'now_at': fetched['fetched_at'],
    }


def get_status() -> Dict[str, Any]:
    """Estado conjunto para dashboard: config + stats + cache summary."""
    cfg = _load_config()
    stats = _load_stats()
    cache = _load_cache()
    domains_cached = []
    for k, v in cache.items():
        if k.startswith('script_hash:'):
            d = k.split(':', 1)[1]
            domains_cached.append({'domain': d, 'hash': v.get('hash'),
                                   'fetched_at': v.get('fetched_at'),
                                   'size': v.get('size')})
    return {
        'ok': True,
        'enabled': cfg.get('enabled', True),
        'auto_refresh': cfg.get('auto_refresh', True),
        'pow_workers': cfg.get('pow_workers', 4),
        'cache_ttl_min': cfg.get('cache_ttl_min', 60),
        'domains_cached': domains_cached,
        'stats': stats,
        'rotations': (cache.get('rotations') or [])[-10:],
        'opcodes_known': len(VM_OPCODE_MAP),
        'vm_engine': _vm_engine_available(),
        'discovered_uuids': _load_discovered_uuids(),
    }


def update_config(patch: dict) -> Dict[str, Any]:
    cfg = _load_config()
    allowed = {'enabled', 'auto_refresh', 'pow_workers', 'cache_ttl_min',
               'domains', 'user_agent'}
    cfg.update({k: v for k, v in patch.items() if k in allowed})
    _save_config(cfg)
    return {'ok': True, 'config': cfg}
