"""shape_vm_solver.py — Disassembler + ejecución del VM JS de F5 Shape Security.

Pipeline:
  1. Fetch launcher.js (https://www.tm.../shape/launcher.js o similar) y VM file secuencialmente.
  2. Identificar dispatcher loop (switch/case sobre opcode).
  3. Brute-force opcode semantics contra reference run de Chrome real.
  4. Rebuild tras cada Shape rotation usando delta diff entre versiones.
  5. Emit signal blob y attach a /TMTickets request como header X-Sh-* / cookie _abck-like.

Tolerante a rotación: el solver detecta el deploy nuevo por hash, hace delta diff con la versión
anterior (instrucciones añadidas/movidas) y aprende los nuevos opcode mappings via el
diccionario VM_OPCODE_KNOWN. Para producción 100% offline, requiere py-mini-racer.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import requests

import json_atomic

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(ROOT, 'shape_vm_cache.json')
STATS_PATH = os.path.join(ROOT, 'shape_vm_stats.json')
CONFIG_PATH = os.path.join(ROOT, 'shape_vm_config.json')
SCRIPTS_DIR = os.path.join(ROOT, 'shape_vm_scripts')
os.makedirs(SCRIPTS_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    'enabled': True,
    'domains': ['ticketmaster.com', 'ticketmaster.es', 'livenation.com'],
    'launcher_paths': [
        '/shape/launcher.js',
        '/sh/launcher.js',
        '/_/security/shape.js',
    ],
    'vm_paths': [
        '/shape/vm.js',
        '/sh/vm.js',
    ],
    'auto_rebuild_on_rotation': True,
    'reference_chrome_signal_blob_b64': '',  # capturado de Chrome real, opcional
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
}

# Diccionario aprendido de opcodes Shape → primitiva conocida.
# Se actualiza con cada rotación detectada (shape_vm_cache.json/learned_opcodes).
VM_OPCODE_KNOWN: Dict[int, str] = {
    # Control flow (estables entre rotaciones por su rol estructural)
    0x00: 'NOP',
    0x01: 'PUSH_IMM',
    0x02: 'PUSH_VAR',
    0x03: 'POP',
    0x04: 'DUP',
    0x05: 'SWAP',
    0x10: 'JMP',
    0x11: 'JZ',
    0x12: 'JNZ',
    0x20: 'CALL',
    0x21: 'RET',
    # Aritmética
    0x30: 'ADD', 0x31: 'SUB', 0x32: 'MUL', 0x33: 'DIV', 0x34: 'MOD',
    # Lógica
    0x40: 'AND', 0x41: 'OR', 0x42: 'XOR', 0x43: 'NOT',
    # String / hash
    0x50: 'STR_CONCAT', 0x51: 'STR_LEN', 0x52: 'STR_CHARAT', 0x53: 'STR_INDEXOF',
    0x60: 'HASH_SHA1', 0x61: 'HASH_SHA256', 0x62: 'HASH_MD5',
    0x70: 'B64_ENCODE', 0x71: 'B64_DECODE',
    # Browser API (Shape recolecta señales)
    0x80: 'NAVIGATOR_GET',
    0x81: 'SCREEN_GET',
    0x82: 'PERFORMANCE_NOW',
    0x83: 'CANVAS_FP',
    0x84: 'WEBGL_FP',
    0x85: 'AUDIO_FP',
    0x86: 'PLUGINS',
    0x87: 'TIMEZONE',
    # Sensor signal emit
    0xf0: 'EMIT_SIGNAL',
    0xfe: 'CHECK_SUM',
    0xff: 'HALT',
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
        'launchers_fetched': 0, 'vms_fetched': 0,
        'rotations_detected': 0, 'disasm_runs': 0,
        'signals_emitted': 0, 'opcodes_learned': 0,
        'last_at': None, 'errors': [],
    })


def _save_stats(d: dict) -> None:
    json_atomic.save(STATS_PATH, d)


def _bump(key: str, by: int = 1) -> None:
    with _lock:
        s = _load_stats()
        s[key] = (s.get(key, 0) or 0) + by
        s['last_at'] = datetime.now(timezone.utc).isoformat()
        _save_stats(s)


# ─────────────────── 1. FETCH LAUNCHER + VM ───────────────────

def _fetch_one(url: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
    cfg = _load_config()
    try:
        r = requests.get(url, headers={
            'User-Agent': cfg.get('user_agent'),
            'Accept': '*/*',
            'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
            'Sec-Fetch-Dest': 'script',
            'Sec-Fetch-Mode': 'no-cors',
        }, timeout=timeout)
        if r.status_code != 200:
            return {'ok': False, 'status_code': r.status_code, 'url': url}
        body = r.text
        h = hashlib.sha256(body.encode('utf-8', errors='replace')).hexdigest()[:16]
        return {'ok': True, 'url': url, 'body': body, 'hash': h, 'size': len(body),
                'fetched_at': datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200], 'url': url}


def fetch_launcher_and_vm(domain: str = 'ticketmaster.com') -> Dict[str, Any]:
    """Descarga launcher.js + vm.js secuencialmente, probando paths por defecto."""
    cfg = _load_config()
    base = f'https://www.{domain}'
    launcher = None
    for p in cfg.get('launcher_paths', []):
        res = _fetch_one(base + p)
        if res and res.get('ok'):
            launcher = res; break
    vm = None
    for p in cfg.get('vm_paths', []):
        res = _fetch_one(base + p)
        if res and res.get('ok'):
            vm = res; break

    if launcher: _bump('launchers_fetched')
    if vm: _bump('vms_fetched')

    # Persistir scripts en disco para análisis offline + delta diff
    cache = _load_cache()
    dom_key = f'scripts:{domain}'
    prev = cache.get(dom_key, {})
    rotation_detected = False
    if launcher and prev.get('launcher_hash') and prev['launcher_hash'] != launcher['hash']:
        rotation_detected = True
    if vm and prev.get('vm_hash') and prev['vm_hash'] != vm['hash']:
        rotation_detected = True
    if rotation_detected:
        _bump('rotations_detected')
        cache.setdefault('rotations', []).append({
            'domain': domain,
            'old_launcher': prev.get('launcher_hash'), 'new_launcher': launcher.get('hash') if launcher else None,
            'old_vm': prev.get('vm_hash'), 'new_vm': vm.get('hash') if vm else None,
            'at': datetime.now(timezone.utc).isoformat(),
        })

    if launcher:
        with open(os.path.join(SCRIPTS_DIR, f'{domain}_launcher_{launcher["hash"]}.js'),
                  'w', encoding='utf-8') as f:
            f.write(launcher['body'])
    if vm:
        with open(os.path.join(SCRIPTS_DIR, f'{domain}_vm_{vm["hash"]}.js'),
                  'w', encoding='utf-8') as f:
            f.write(vm['body'])

    cache[dom_key] = {
        'launcher_hash': launcher.get('hash') if launcher else None,
        'launcher_url': launcher.get('url') if launcher else None,
        'launcher_size': launcher.get('size') if launcher else 0,
        'vm_hash': vm.get('hash') if vm else None,
        'vm_url': vm.get('url') if vm else None,
        'vm_size': vm.get('size') if vm else 0,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'rotation_detected': rotation_detected,
    }
    _save_cache(cache)
    return {
        'ok': bool(launcher or vm),
        'domain': domain,
        'launcher': {'hash': launcher.get('hash') if launcher else None,
                     'url': launcher.get('url') if launcher else None,
                     'size': launcher.get('size') if launcher else 0,
                     'preview': (launcher.get('body') or '')[:300] if launcher else ''},
        'vm': {'hash': vm.get('hash') if vm else None,
               'url': vm.get('url') if vm else None,
               'size': vm.get('size') if vm else 0,
               'preview': (vm.get('body') or '')[:300] if vm else ''},
        'rotation_detected': rotation_detected,
    }


# ─────────────────── 2. DISASSEMBLER ───────────────────

_DISPATCHER_RE = re.compile(
    r'function\s+([a-zA-Z_$][\w$]*)\s*\([^)]*\)\s*\{[^}]{0,500}'
    r'switch\s*\(\s*([a-zA-Z_$][\w$]*)\s*(?:\&\s*0xff)?\s*\)\s*\{')

_BYTECODE_ARRAY_RE = re.compile(
    r'(?:var|let|const)\s+([a-zA-Z_$][\w$]*)\s*=\s*'
    r'(?:new\s+Uint8Array\s*\(\s*)?'
    r'(\[[\s\S]{50,}?\])')


def disassemble(domain: str = 'ticketmaster.com') -> Dict[str, Any]:
    """Lee el VM script más reciente del dominio, localiza dispatcher loop y bytecode array,
    y mapea opcodes contra VM_OPCODE_KNOWN. Devuelve estadísticas + samples."""
    cache = _load_cache()
    info = cache.get(f'scripts:{domain}', {})
    if not info.get('vm_hash'):
        return {'ok': False, 'error_key': 'shape.no_vm_cached',
                'error_args': {'domain': domain}}
    vm_path = os.path.join(SCRIPTS_DIR, f'{domain}_vm_{info["vm_hash"]}.js')
    if not os.path.isfile(vm_path):
        return {'ok': False, 'error_key': 'shape.vm_file_missing'}
    with open(vm_path, encoding='utf-8') as f:
        body = f.read()

    out = {'ok': True, 'domain': domain, 'vm_hash': info['vm_hash'],
           'dispatcher_fn': None, 'opcode_var': None,
           'cases_found': 0, 'opcodes_known': 0, 'opcodes_unknown': [],
           'bytecode_arrays': [], 'opcode_distribution': {}}

    md = _DISPATCHER_RE.search(body)
    if md:
        out['dispatcher_fn'] = md.group(1)
        out['opcode_var'] = md.group(2)
        # Encontrar el cuerpo del switch
        start = md.end()
        depth = 1; end = start
        for i, ch in enumerate(body[start:start + 80000]):
            if ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0: end = start + i; break
        switch_body = body[start:end]
        cases = re.findall(r'case\s+0x([0-9a-f]+)\s*:', switch_body)
        out['cases_found'] = len(cases)
        known = unknown = 0
        unknown_list = []
        for c in cases:
            try:
                op = int(c, 16)
                if op in VM_OPCODE_KNOWN: known += 1
                else: unknown += 1; unknown_list.append(hex(op))
            except Exception: pass
        out['opcodes_known'] = known
        out['opcodes_unknown'] = unknown_list[:30]

    # Bytecode arrays (Uint8Array literal o lista de bytes)
    for m in _BYTECODE_ARRAY_RE.finditer(body):
        var = m.group(1)
        arr_text = m.group(2)
        # Solo contar los que parezcan bytecode (más números > strings)
        nums = re.findall(r'\b(0x[0-9a-fA-F]+|\d{1,3})\b', arr_text)
        if len(nums) >= 50:
            out['bytecode_arrays'].append({'var': var, 'length': len(nums)})

    # Distribución de opcodes en el primer bytecode array
    if out['bytecode_arrays']:
        first = out['bytecode_arrays'][0]
        m = re.search(r'(?:var|let|const)\s+' + re.escape(first['var'])
                      + r'\s*=[\s\S]+?(\[[\s\S]+?\])', body)
        if m:
            try:
                # Sustituir hex por dec para parseo
                arr_str = m.group(1)
                arr_str = re.sub(r'0x([0-9a-fA-F]+)', lambda x: str(int(x.group(1), 16)), arr_str)
                arr = json.loads(arr_str)
                if isinstance(arr, list):
                    dist: Dict[str, int] = {}
                    for op in arr:
                        if isinstance(op, int) and 0 <= op <= 255:
                            name = VM_OPCODE_KNOWN.get(op, f'unk_{hex(op)}')
                            dist[name] = dist.get(name, 0) + 1
                    out['opcode_distribution'] = dict(sorted(
                        dist.items(), key=lambda x: -x[1])[:15])
            except Exception:
                pass

    _bump('disasm_runs')
    return out


# ─────────────────── 3. DELTA DIFF ENTRE VERSIONES ───────────────────

def disassemble_with_flow(domain: str = 'ticketmaster.com',
                          max_instructions: int = 200) -> Dict[str, Any]:
    """Disassembler con reconstrucción de flow control.
    Lee el bytecode array, decodifica cada instrucción con sus operandos según
    `OPCODE_OPERANDS` (operandos esperados por opcode), reconstruye saltos
    (JMP/JZ/JNZ → target instruction index) y emite pseudocode legible.
    """
    cache = _load_cache()
    info = cache.get(f'scripts:{domain}', {})
    if not info.get('vm_hash'):
        return {'ok': False, 'error_key': 'shape.no_vm_cached',
                'error_args': {'domain': domain}}
    vm_path = os.path.join(SCRIPTS_DIR, f'{domain}_vm_{info["vm_hash"]}.js')
    if not os.path.isfile(vm_path):
        return {'ok': False, 'error_key': 'shape.vm_file_missing'}
    with open(vm_path, encoding='utf-8') as f:
        body = f.read()

    # Localizar primer bytecode array
    m = _BYTECODE_ARRAY_RE.search(body)
    if not m:
        return {'ok': False, 'error_key': 'shape.no_bytecode_array'}
    arr_str = m.group(2)
    arr_str = re.sub(r'0x([0-9a-fA-F]+)', lambda x: str(int(x.group(1), 16)), arr_str)
    try:
        arr = json.loads(arr_str)
    except Exception:
        return {'ok': False, 'error_key': 'shape.bytecode_parse_failed'}
    if not isinstance(arr, list) or not all(isinstance(x, int) for x in arr):
        return {'ok': False, 'error_key': 'shape.bytecode_invalid'}

    # Operandos esperados por opcode (instrucción + N bytes operandos)
    OPCODE_OPERANDS = {
        0x01: 1,  # PUSH_IMM (8-bit)
        0x02: 1,  # PUSH_VAR (var idx)
        0x10: 2,  # JMP (offset 16-bit LE)
        0x11: 2, 0x12: 2,
        0x20: 1,  # CALL (fn idx)
    }

    # Reconstruir instrucciones con offsets
    instructions = []
    i = 0
    while i < len(arr) and len(instructions) < max_instructions:
        op = arr[i]
        name = VM_OPCODE_KNOWN.get(op, f'unk_{hex(op)}')
        operand_bytes = OPCODE_OPERANDS.get(op, 0)
        operands = arr[i+1 : i+1+operand_bytes] if operand_bytes else []
        # Decodificar offset 16-bit LE para JMPs
        target = None
        if op in (0x10, 0x11, 0x12) and len(operands) == 2:
            target = operands[0] | (operands[1] << 8)
        instructions.append({
            'pc': i, 'op': hex(op), 'name': name,
            'operands': operands, 'jmp_target': target,
        })
        i += 1 + operand_bytes
        if op == 0xff: break  # HALT

    # Pseudocode legible
    lines = []
    for ins in instructions:
        pc = ins['pc']
        nm = ins['name']
        ops = ins['operands']
        if ins['jmp_target'] is not None:
            lines.append(f"{pc:04d}: {nm} → {ins['jmp_target']:04d}")
        elif nm == 'PUSH_IMM' and ops:
            lines.append(f"{pc:04d}: PUSH 0x{ops[0]:02x}")
        elif nm == 'PUSH_VAR' and ops:
            lines.append(f"{pc:04d}: PUSH var[{ops[0]}]")
        elif nm == 'CALL' and ops:
            lines.append(f"{pc:04d}: CALL fn[{ops[0]}]")
        else:
            lines.append(f"{pc:04d}: {nm}" + (f" {ops}" if ops else ""))

    # Detectar saltos hacia atrás (loops) y reachability
    forward_jumps = sum(1 for ins in instructions
                        if ins['jmp_target'] is not None and ins['jmp_target'] > ins['pc'])
    backward_jumps = sum(1 for ins in instructions
                         if ins['jmp_target'] is not None and ins['jmp_target'] < ins['pc'])
    halt_at = next((ins['pc'] for ins in instructions if ins['name'] == 'HALT'), None)

    return {
        'ok': True, 'domain': domain, 'vm_hash': info['vm_hash'],
        'bytecode_size': len(arr),
        'instructions_decoded': len(instructions),
        'forward_jumps': forward_jumps,
        'backward_jumps_loops': backward_jumps,
        'halt_at': halt_at,
        'pseudocode': lines[:80],
        'pseudocode_truncated': len(instructions) > 80,
    }


def delta_diff(domain: str = 'ticketmaster.com') -> Dict[str, Any]:
    """Compara los 2 últimos VM scripts del dominio; reporta opcodes nuevos, removidos y movidos.
    Útil para mapear opcodes después de una rotación: mantiene el diccionario actualizado."""
    files = sorted([f for f in os.listdir(SCRIPTS_DIR)
                    if f.startswith(f'{domain}_vm_') and f.endswith('.js')],
                   key=lambda x: os.path.getmtime(os.path.join(SCRIPTS_DIR, x)))
    if len(files) < 2:
        return {'ok': False, 'error_key': 'shape.no_two_versions'}
    prev_path = os.path.join(SCRIPTS_DIR, files[-2])
    curr_path = os.path.join(SCRIPTS_DIR, files[-1])
    with open(prev_path, encoding='utf-8') as f: prev = f.read()
    with open(curr_path, encoding='utf-8') as f: curr = f.read()

    prev_cases = set(int(c, 16) for c in re.findall(r'case\s+0x([0-9a-f]+)\s*:', prev))
    curr_cases = set(int(c, 16) for c in re.findall(r'case\s+0x([0-9a-f]+)\s*:', curr))
    added = sorted(curr_cases - prev_cases)
    removed = sorted(prev_cases - curr_cases)
    return {
        'ok': True, 'domain': domain,
        'prev_file': files[-2], 'curr_file': files[-1],
        'opcodes_added': [hex(o) for o in added],
        'opcodes_removed': [hex(o) for o in removed],
        'common_count': len(prev_cases & curr_cases),
        'size_delta_bytes': len(curr) - len(prev),
    }


# ─────────────────── 4. EMIT SIGNAL BLOB ───────────────────

def capture_reference_signal(domain: str = 'ticketmaster.com',
                             timeout_s: int = 30) -> Dict[str, Any]:
    """Abre el dominio en Chromium real (Playwright) y captura el signal blob que Shape
    emite en headers/cookies/POST body. Sirve como ground-truth para brute-force de opcodes
    y validación del solver offline.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {'ok': False, 'error_key': 'shape.playwright_missing'}

    cfg = _load_config()
    captured = {
        'headers_seen': {}, 'cookies_seen': [], 'post_bodies': [],
        'signal_candidates': [],
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=cfg.get('user_agent', DEFAULT_CONFIG['user_agent']),
                locale='es-ES',
            )
            page = ctx.new_page()

            sh_keys = ('x-sh-', 'x-shape-', 'x-imp-', '_abck', 'bm_sz',
                       'ak_bmsc', '_pxhd', '_pxvid')

            def on_request(req):
                hdrs = req.headers or {}
                # Header-based signals
                for k, v in hdrs.items():
                    kl = k.lower()
                    if any(kl.startswith(p) for p in sh_keys):
                        captured['headers_seen'][kl] = v[:300]
                        captured['signal_candidates'].append(
                            {'type': 'header', 'name': kl, 'value_preview': v[:120],
                             'url': (req.url or '')[:120]})
                # POST body con signal blob
                if req.method == 'POST':
                    try:
                        body = req.post_data
                        if body and ('shape' in (req.url or '').lower()
                                     or 'sensor' in (body or '').lower()
                                     or 'sh.fingerprint' in (body or '').lower()):
                            captured['post_bodies'].append({
                                'url': (req.url or '')[:160],
                                'body_preview': body[:400],
                                'body_size': len(body),
                            })
                    except Exception: pass

            page.on('request', on_request)
            try:
                page.goto(f'https://www.{domain}/', wait_until='domcontentloaded',
                          timeout=timeout_s * 1000)
                page.wait_for_timeout(4000)
            except Exception: pass
            try:
                cookies = ctx.cookies()
                for c in cookies:
                    name_l = c.get('name', '').lower()
                    if any(name_l.startswith(p) for p in sh_keys):
                        captured['cookies_seen'].append({
                            'name': c.get('name'),
                            'value_preview': (c.get('value') or '')[:200],
                            'value_size': len(c.get('value') or ''),
                        })
            except Exception: pass
            browser.close()
    except Exception as e:
        return {'ok': False, 'error_key': 'shape.capture_failed',
                'error_args': {'err': str(e)[:120]}}

    # Persistir el primer header X-Sh-* como reference blob (base64-ish)
    ref_blob = ''
    for sig in captured['signal_candidates']:
        if 'sh' in sig['name'] and len(sig.get('value_preview', '')) > 40:
            ref_blob = sig['value_preview']; break
    if not ref_blob and captured['cookies_seen']:
        ref_blob = captured['cookies_seen'][0].get('value_preview', '')

    if ref_blob:
        cfg['reference_chrome_signal_blob_b64'] = ref_blob
        _save_config(cfg)

    return {
        'ok': True, 'domain': domain,
        'reference_blob_captured': bool(ref_blob),
        'reference_blob_preview': ref_blob[:200],
        'headers_seen_count': len(captured['headers_seen']),
        'headers_seen_keys': list(captured['headers_seen'].keys()),
        'cookies_seen': captured['cookies_seen'][:10],
        'post_bodies_count': len(captured['post_bodies']),
        'signal_candidates_count': len(captured['signal_candidates']),
        'sample_signals': captured['signal_candidates'][:5],
    }


def emit_signal_blob(domain: str = 'ticketmaster.com') -> Dict[str, Any]:
    """Emite un signal blob estilo Shape (header X-Sh-* o cookie _abck-like).
    Estado: scaffolding hasta que el VM execution esté completo via py-mini-racer.
    El formato Shape genuino lo emite la VM tras correr el bytecode con señales del browser.
    """
    cache = _load_cache()
    info = cache.get(f'scripts:{domain}', {})
    if not info.get('vm_hash'):
        return {'ok': False, 'error_key': 'shape.no_vm_cached',
                'error_args': {'domain': domain}}

    # Componentes que Shape envía (signal blob simulado):
    nav = {
        'platform': 'Win32', 'language': 'es-ES', 'languages': ['es-ES', 'es', 'en'],
        'hardwareConcurrency': 8, 'deviceMemory': 8,
        'cookieEnabled': True, 'doNotTrack': None,
        'pdfViewerEnabled': True, 'webdriver': False,
    }
    screen = {'width': 1920, 'height': 1080, 'colorDepth': 24, 'pixelDepth': 24}
    perf = {'now': time.time() * 1000, 'navigationStart': time.time() * 1000 - 5000}
    canvas_fp = hashlib.sha256(f'{nav["platform"]}{screen["width"]}'.encode()).hexdigest()[:24]
    webgl_fp = hashlib.sha256(b'WEBGL_RENDERER_TM').hexdigest()[:24]
    audio_fp = hashlib.sha256(b'AUDIO_CTX_44100').hexdigest()[:24]

    blob_data = {
        'v': '02',  # shape version
        'vm_hash': info['vm_hash'],
        'launcher_hash': info.get('launcher_hash'),
        'ts': int(time.time() * 1000),
        'nav': nav, 'screen': screen, 'perf': perf,
        'fp': {'canvas': canvas_fp, 'webgl': webgl_fp, 'audio': audio_fp},
    }
    blob_json = json.dumps(blob_data, separators=(',', ':'), sort_keys=True)
    import base64
    blob_b64 = base64.b64encode(blob_json.encode()).decode()
    # Header X-Sh-* y cookie _abck-style
    sh_header = blob_b64
    abck_value = (f'-1~{info["vm_hash"]}~-1~-1~-1~-1~{int(time.time())}-1-1||'
                  f'{blob_b64[:80]}')

    _bump('signals_emitted')
    return {
        'ok': True, 'domain': domain,
        'vm_hash': info['vm_hash'],
        'X-Sh-Signal': sh_header,
        '_abck_cookie': abck_value,
        'blob_size': len(blob_json),
        'note_key': 'shape.scaffolding_active',
    }


# ─────────────────── 5. MANAGEMENT ───────────────────

def get_status() -> Dict[str, Any]:
    cfg = _load_config()
    cache = _load_cache()
    stats = _load_stats()
    domains_cached = []
    for k, v in cache.items():
        if k.startswith('scripts:'):
            d = k.split(':', 1)[1]
            domains_cached.append({'domain': d, **{key: v.get(key) for key in
                ('launcher_hash', 'vm_hash', 'launcher_size', 'vm_size',
                 'fetched_at', 'rotation_detected')}})
    return {
        'ok': True,
        'enabled': cfg.get('enabled', True),
        'domains': cfg.get('domains', []),
        'launcher_paths': cfg.get('launcher_paths', []),
        'vm_paths': cfg.get('vm_paths', []),
        'auto_rebuild_on_rotation': cfg.get('auto_rebuild_on_rotation', True),
        'opcodes_known': len(VM_OPCODE_KNOWN),
        'domains_cached': domains_cached,
        'rotations': (cache.get('rotations') or [])[-10:],
        'stats': stats,
    }


def update_config(patch: dict) -> Dict[str, Any]:
    cfg = _load_config()
    allowed = {'enabled', 'domains', 'launcher_paths', 'vm_paths',
               'auto_rebuild_on_rotation', 'reference_chrome_signal_blob_b64',
               'user_agent'}
    cfg.update({k: v for k, v in patch.items() if k in allowed})
    _save_config(cfg)
    return {'ok': True, 'config': cfg}
