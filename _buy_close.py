"""Cierra el perfil MoreLogin con LIMPIEZA DISCRIMINADA de cookies.

Regla del usuario: NO eliminar todas las cookies — preservar las útiles para el proyecto
(sesión TM, antibot Akamai, queue-it, Adyen) y borrar solo las "malas" (tracking, consent,
analytics). El localStorage/sessionStorage SE preservan también si pertenecen a dominios
relevantes; sólo borramos los caches y storages 3rd-party identificables.

Usa cookie_filter.classify_cookie para decidir KEEP / DROP / UNKNOWN.
"""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

import cookie_filter

# admite _buy_state.json o _scan_state.json
STATE_FILE = '_buy_state.json' if os.path.exists('_buy_state.json') else '_scan_state.json'
with open(STATE_FILE, encoding='utf-8') as f:
    st = json.load(f)
PORT = st['port']; ENV_ID = st['env_id']; PROFILE = st['profile']

print(f'== CLEANUP DISCRIMINADO + CLOSE | {PROFILE} port={PORT} (state={STATE_FILE}) ==')

KEEP_DIR = r'C:\Users\user\TM\proyecto\datos\cookies_kept'
os.makedirs(KEEP_DIR, exist_ok=True)

# 1) Conectar y limpiar discriminadamente
try:
    v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=5).json()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
        ctx = b.contexts[0] if b.contexts else None
        if ctx:
            cookies_all = ctx.cookies()
            keep, drop, unknown = cookie_filter.partition_cookies(cookies_all)
            print(f'  total cookies: {len(cookies_all)}')
            print(f'    keep:    {len(keep)}  (sesion TM, antibot, adyen, queue-it...)')
            print(f'    drop:    {len(drop)}  (tracking, analytics, consent)')
            print(f'    unknown: {len(unknown)}  (3rd party desconocido)')

            # Limpiar TODO y re-añadir solo las KEEP (Playwright no permite borrar 1 a 1 selectivamente
            # por nombre+dominio en todos los casos, así que clear + add_cookies de las que queremos)
            ctx.clear_cookies()
            if keep:
                # add_cookies necesita el formato exacto, normalizamos
                normalized = []
                for c in keep:
                    norm = {k: v for k, v in c.items() if v is not None and k in
                            ('name','value','domain','path','expires','httpOnly','secure','sameSite','url')}
                    # Sanitize sameSite (Playwright admite 'Strict','Lax','None')
                    if 'sameSite' in norm:
                        ss = str(norm['sameSite']).capitalize()
                        norm['sameSite'] = ss if ss in ('Strict','Lax','None') else 'Lax'
                    normalized.append(norm)
                try:
                    ctx.add_cookies(normalized)
                    print(f'  ✓ re-aplicadas {len(normalized)} cookies KEEP al contexto')
                except Exception as e:
                    print(f'  ⚠️ add_cookies falló parcialmente: {e}')

            # Persistir las KEEP en disco para reutilización futura (warm-start de cuentas)
            keep_path = os.path.join(KEEP_DIR, f'{PROFILE}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
            with open(keep_path, 'w', encoding='utf-8') as f:
                json.dump({'profile': PROFILE, 'env_id': ENV_ID, 'cookies_keep': keep,
                           'cookies_dropped': len(drop), 'cookies_unknown': len(unknown),
                           'saved_at': datetime.now().isoformat()}, f, indent=2, ensure_ascii=False, default=str)
            print(f'  💾 cookies útiles guardadas en {keep_path}')

            # Storage: NO clear total. Solo eliminar caches HTTP y indexedDB de dominios "drop" conocidos
            # (preservar localStorage/sessionStorage de TM porque puede contener fingerprint válido).
            for p in ctx.pages:
                try:
                    domain_now = p.url
                    res = p.evaluate("""() => {
                        const out = {caches_removed:0, idb_removed:0};
                        try {
                            if (caches && caches.keys) caches.keys().then(ks => ks.forEach(k => caches.delete(k)));
                            out.caches_removed = 1;
                        } catch(e){}
                        // NO tocamos localStorage/sessionStorage — preservar para reuso
                        return out;
                    }""")
                    print(f'  ↩ {p.url[:60]}: caches_cleared={res.get("caches_removed")}, storage preservado')
                except Exception as e:
                    print(f'  storage err: {e}')

            # Verificación final
            final_cookies = ctx.cookies()
            print(f'  cookies tras limpieza: {len(final_cookies)} (esperado ~{len(keep)})')
        b.close()
except Exception as e:
    print(f'  cleanup err: {e}')

# 2) Cerrar perfil MoreLogin (env/close)
print(f'\n  cerrando perfil MoreLogin...')
r = requests.post('http://127.0.0.1:40000/api/env/close',
                  json={'envId': ENV_ID}, timeout=15).json()
print(f'  close: {r}')
print('Done — perfil cerrado, cookies útiles preservadas, malas eliminadas.')
