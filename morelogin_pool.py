"""morelogin_pool.py — Gestor del pool de perfiles MoreLogin.

Garantiza que SIEMPRE haya N perfiles listos antes de cualquier registro/compra/cola.
Verifica IP real, asigna proxies del pool ES (proxy_health.json), tracking persistente.

Uso:
  python morelogin_pool.py status                        # inventario actual
  python morelogin_pool.py ensure --country ES --min 100 # garantiza N listos
  python morelogin_pool.py validate                      # re-checkea cada perfil con IP test real
  python morelogin_pool.py reassign-broken               # intenta arreglar proxies failed
  python morelogin_pool.py rotate-sessionids             # rotar sessionids QuantumProxies
"""
from __future__ import annotations
import argparse, json, os, random, re, sys, time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding='utf-8')
import requests

ROOT = r'C:\Users\user\TM\proyecto'
MORELOGIN_API = 'http://127.0.0.1:40000'
PROXY_HEALTH = os.path.join(ROOT, 'proxy_health.json')
RUNS_DIR = os.path.join(ROOT, 'datos', 'buy', 'runs')
POOL_STATE_PATH = os.path.join(RUNS_DIR, 'pool_state.json')
USED_IPS_PATH = os.path.join(RUNS_DIR, 'used_ips.json')
BURNED_PATH = os.path.join(RUNS_DIR, 'burned_profiles.json')

os.makedirs(RUNS_DIR, exist_ok=True)


def log(msg: str, level: str = 'INFO'):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {level:5} {msg}', flush=True)


# ─────────────── persistencia ───────────────
def load_pool_state() -> dict:
    try:
        with open(POOL_STATE_PATH, encoding='utf-8') as f: return json.load(f)
    except Exception: return {'profiles': {}, 'last_validated': None}

def save_pool_state(s: dict):
    s['last_validated'] = datetime.now(timezone.utc).isoformat()
    try:
        import json_atomic
        json_atomic.save(POOL_STATE_PATH, s)
    except Exception:
        with open(POOL_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(s, f, indent=2)

def load_used_ips() -> dict:
    try:
        with open(USED_IPS_PATH, encoding='utf-8') as f: return json.load(f)
    except Exception: return {}

def load_burned() -> set:
    try:
        with open(BURNED_PATH, encoding='utf-8') as f:
            return set(json.load(f).get('burned', []))
    except Exception: return set()


# ─────────────── inventario MoreLogin ───────────────
def list_morelogin_profiles() -> list:
    """Lista TODOS los perfiles MoreLogin paginando hasta 2000."""
    out = []
    for page_no in range(1, 21):
        try:
            r = requests.post(f'{MORELOGIN_API}/api/env/page',
                              json={'pageNo': page_no, 'pageSize': 100}, timeout=15).json()
            items = (r.get('data') or {}).get('dataList') or []
            if not items: break
            out.extend(items)
        except Exception as e:
            log(f'page {page_no} err: {e}', 'WARN'); break
    return out

def filter_country(all_profiles: list, country: str) -> list:
    return [p for p in all_profiles
            if (p.get('proxy') or {}).get('countryCode', '').upper() == country.upper()]


# ─────────────── proxy pool desde proxy_health.json ───────────────
def load_proxy_pool(geo: str = 'es') -> List[dict]:
    """Devuelve lista de proxies parseados {host, port, user, pass, sessionid}."""
    try:
        with open(PROXY_HEALTH, encoding='utf-8') as f:
            d = json.load(f)
    except Exception as e:
        log(f'no proxy_health.json: {e}', 'ERROR'); return []
    out = []
    for p in d.get('proxies', []):
        if (p.get('geo') or '').lower() != geo.lower(): continue
        url = p.get('url') or ''
        u = urlparse(url)
        sid_m = re.search(r'sessionid-(\d+)-', u.username or '')
        out.append({
            'id': p.get('id'),
            'host': u.hostname, 'port': u.port,
            'user': u.username, 'pass': u.password,
            'sessionid': sid_m.group(1) if sid_m else None,
            'score': p.get('score', 0),
            'status': p.get('status', '?'),
        })
    return sorted(out, key=lambda p: -p.get('score', 0))


# ─────────────── verificación IP real ───────────────
def verify_profile_internet(env_id: str, profile_name: str = '') -> dict:
    """Arranca el perfil, verifica IP via api.ipify.org, lo cierra. Devuelve {ok, ip, error}."""
    import bot_orchestrator as bo
    port = bo._start_profile(env_id)
    if not port:
        return {'ok': False, 'error': 'start_failed', 'profile': profile_name}
    time.sleep(3)
    try:
        v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto('https://api.ipify.org/?format=json',
                          wait_until='domcontentloaded', timeout=15000)
                txt = page.evaluate('()=>document.body.innerText') or ''
                m = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', txt)
                ip = m.group(1) if m else None
            except Exception as e:
                ip = None
            try: b.close()
            except Exception: pass
        finally:
            pw.stop()
    except Exception as e:
        ip = None
    finally:
        try: bo._close_profile_clean(env_id, port, profile_name)
        except Exception: pass
        bo._release_profile(profile_name)

    if ip: return {'ok': True, 'ip': ip, 'profile': profile_name}
    return {'ok': False, 'error': 'no_internet', 'profile': profile_name}


def reassign_proxy(env_id: str, proxy: dict) -> dict:
    """Intenta /api/env/update con el proxy nuevo. La API local de MoreLogin
    ACEPTA el payload (code:0) pero NO siempre lo aplica → devolvemos warning."""
    payload = {
        'envId': env_id,
        'proxy': {
            'proxyType': 'http',
            'proxyHost': proxy['host'],
            'proxyPort': str(proxy['port']),
            'proxyUserName': proxy['user'],
            'proxyPassword': proxy['pass'],
            'countryCode': 'ES',
        },
    }
    try:
        r = requests.post(f'{MORELOGIN_API}/api/env/update', json=payload, timeout=12).json()
        return {'ok': r.get('code') == 0, 'data': r}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ─────────────── comandos CLI ───────────────
def cmd_status(country: str = 'ES'):
    burned = load_burned()
    used = load_used_ips()
    state = load_pool_state()
    all_p = list_morelogin_profiles()
    es = filter_country(all_p, country)

    verified = [p for p in es if (p.get('proxy') or {}).get('proxyCheckStatus') == 1]
    unverified = [p for p in es if (p.get('proxy') or {}).get('proxyCheckStatus') != 1]
    burned_in_pool = [p for p in es if p.get('envName') in burned]
    available = [p for p in verified if p.get('envName') not in burned]

    pool_proxies = load_proxy_pool(country.lower())

    print(f'\n══════ MORELOGIN POOL STATUS ({country}) ══════')
    print(f'  Total perfiles {country}:           {len(es)}')
    print(f'  Verified (proxyCheck=1):     {len(verified)}')
    print(f'  Unverified/failed:           {len(unverified)}')
    print(f'  Burned (ya usados):          {len(burned_in_pool)}')
    print(f'  ✅ DISPONIBLES NOW:          {len(available)}')
    print(f'')
    print(f'  Proxies en proxy_health.json {country}: {len(pool_proxies)}')
    print(f'  IPs usadas históricamente:   {len(used)}')
    print(f'')

    if available:
        print(f'  Lista (siguientes 20 disponibles):')
        for p in available[:20]:
            pr = p.get('proxy') or {}
            print(f'    {p.get("envName"):15} ip={pr.get("exportIp"):18}')
    if unverified:
        print(f'\n  ❌ Failed/unverified ({len(unverified)} — necesitan fix):')
        for p in unverified:
            pr = p.get('proxy') or {}
            check = pr.get('proxyCheckStatus')
            sid_m = re.search(r'sessionid-(\d+)-', pr.get('username') or '')
            print(f'    {p.get("envName"):15} ip={pr.get("exportIp",""):16} check={check} sessionid={sid_m.group(1) if sid_m else "?"}')
    return {'available': len(available), 'broken': len(unverified), 'burned': len(burned_in_pool)}


def cmd_validate(country: str = 'ES', limit: int = 30):
    """Valida cada perfil ES no quemado abriéndolo y testeando IP REAL."""
    burned = load_burned()
    all_p = list_morelogin_profiles()
    es = [p for p in filter_country(all_p, country) if p.get('envName') not in burned]
    log(f'Validando {min(limit, len(es))} perfiles {country} (test internet real)')

    state = load_pool_state()
    state.setdefault('profiles', {})
    ok = bad = 0
    for p in es[:limit]:
        name = p.get('envName')
        env_id = p.get('id')
        log(f'  testing {name}...')
        res = verify_profile_internet(env_id, name)
        rec = {
            'env_id': env_id,
            'last_check': datetime.now(timezone.utc).isoformat(),
            'real_ip': res.get('ip'),
            'reachable': res.get('ok', False),
            'error': res.get('error'),
            'expected_ip': (p.get('proxy') or {}).get('exportIp'),
        }
        state['profiles'][name] = rec
        if res.get('ok'):
            ok += 1; log(f'    ✓ {name} ip={res.get("ip")}')
        else:
            bad += 1; log(f'    ❌ {name} {res.get("error")}', 'WARN')
        save_pool_state(state)
        time.sleep(2)

    log(f'Done — {ok} OK, {bad} BAD')


def cmd_reassign_broken(country: str = 'ES'):
    """Para perfiles failed, asigna proxy del pool y re-testea."""
    burned = load_burned()
    all_p = list_morelogin_profiles()
    es = filter_country(all_p, country)
    broken = [p for p in es if (p.get('proxy') or {}).get('proxyCheckStatus') != 1
              and p.get('envName') not in burned]
    pool = load_proxy_pool(country.lower())
    log(f'Broken: {len(broken)} | Proxies disponibles: {len(pool)}')
    if not broken:
        log('Nada que reasignar'); return
    if not pool:
        log('Sin pool de proxies — añade en proxy_health.json', 'ERROR'); return

    for i, p in enumerate(broken):
        name = p.get('envName'); env_id = p.get('id')
        new_proxy = pool[i % len(pool)]
        log(f'\n  {name} ← proxy sessionid={new_proxy["sessionid"]}')
        r = reassign_proxy(env_id, new_proxy)
        log(f'    update API: {r.get("data",{}).get("code")}')
        # Re-verificar
        res = verify_profile_internet(env_id, name)
        if res.get('ok'):
            log(f'    ✅ {name} REPARADO ip={res.get("ip")}')
        else:
            log(f'    ❌ {name} sigue fallando ({res.get("error")}). '
                f'⚠ La API local de MoreLogin no aplica el cambio de proxy. '
                f'NECESITA fix manual en MoreLogin desktop.', 'WARN')


def cmd_ensure(country: str = 'ES', min_count: int = 100):
    """Garantiza min_count perfiles disponibles. Reporta gap + plan."""
    s = cmd_status(country)
    avail = s['available']
    print(f'\n══════ ENSURE MIN={min_count} ({country}) ══════')
    print(f'  Disponibles ahora:  {avail}')
    print(f'  Necesarios:         {min_count}')
    print(f'  Faltan:             {max(0, min_count - avail)}')
    if avail >= min_count:
        print(f'  ✅ POOL OK — listo para batches grandes'); return True

    gap = min_count - avail
    print(f'\n  📋 Plan para llenar el gap de {gap} perfiles:')
    print(f'  1. ARREGLAR los {s["broken"]} con proxy roto (5-10 min):')
    print(f'     - Abre MoreLogin desktop')
    print(f'     - Para cada uno: Editar → Proxy → cambia sessionid → Comprobar conexión')
    print(f'     - Sessionids ES disponibles ({len(load_proxy_pool(country.lower()))} en proxy_health.json):')
    pool = load_proxy_pool(country.lower())
    sids = ', '.join(p['sessionid'] for p in pool[:10] if p['sessionid'])
    print(f'       Top 10: {sids}')
    new_needed = max(0, min_count - avail - s['broken'])
    if new_needed > 0:
        print(f'  2. CREAR {new_needed} perfiles nuevos en MoreLogin desktop:')
        print(f'     - Click "Nuevo perfil" → Spain')
        print(f'     - Proxy: residential.quantumproxies.net:8888')
        print(f'     - User: Quantum_927evhgx-cc-ES-pool-qntm-sessionid-XXXXXX-sesstime-60')
        print(f'     - Password: ycp4e57m')
        print(f'     - Country: ES')
        print(f'     - Variar XXXXXX por sessionids de proxy_health.json')
    print(f'\n  Cuando termines, ejecuta: python morelogin_pool.py validate')
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument('cmd', choices=['status', 'ensure', 'validate', 'reassign-broken', 'rotate-sessionids'])
    p.add_argument('--country', default='ES')
    p.add_argument('--min', type=int, default=100)
    p.add_argument('--limit', type=int, default=30)
    args = p.parse_args()

    os.chdir(ROOT)
    if args.cmd == 'status': cmd_status(args.country)
    elif args.cmd == 'ensure': cmd_ensure(args.country, args.min)
    elif args.cmd == 'validate': cmd_validate(args.country, args.limit)
    elif args.cmd == 'reassign-broken': cmd_reassign_broken(args.country)
    elif args.cmd == 'rotate-sessionids':
        log('rotate-sessionids requiere acción manual en MoreLogin desktop')
        cmd_status(args.country)


if __name__ == '__main__':
    main()
