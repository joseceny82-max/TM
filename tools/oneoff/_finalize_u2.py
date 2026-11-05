"""Finaliza U2: guarda cookies+state, cierra perfil, registra cuenta+tarjeta en dashboard."""
import sys, os, json, csv, requests
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

PORT = 49292
PROFILE = 'P-170130'
ENV_ID = None  # se rellena tras consulta MoreLogin
SHOTS = r'datos\buy\register_u2'
COOKIES_DIR = r'datos\cookies'
SESSIONS_DIR = r'datos\sessions'
os.makedirs(COOKIES_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ── 1. Cargar phone_used.json + plantilla U2 ──
with open(os.path.join(SHOTS, 'phone_used.json'), encoding='utf-8') as f:
    pu = json.load(f)
ENV_ID = pu['env_id']
EMAIL = pu['tm_email']
PHONE = pu['phone']
PHONE_DIGITS = pu['phone_digits']

with open('plantilla_tm.csv', encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
u = next(r for r in rows if r['Email_TM'] == EMAIL)
print(f'═══ FINALIZAR U2 — {EMAIL} ═══')

# ── 2. Conectar Playwright + dump cookies + storage_state ──
v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=8).json()
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
ctx = b.contexts[0]
page = ctx.pages[0]

ts = datetime.now().strftime('%Y%m%d_%H%M%S')
cookies_file = os.path.join(COOKIES_DIR, f'tm_morelogin_u2_{ts}.json')
state_file = os.path.join(SESSIONS_DIR, f'tm_u2_{ts}_state.json')

cookies = ctx.cookies()
with open(cookies_file, 'w', encoding='utf-8') as f:
    json.dump({'profile': PROFILE, 'env_id': ENV_ID, 'tm_email': EMAIL,
               'saved_at': datetime.now().isoformat(),
               'cookies': cookies}, f, indent=2, ensure_ascii=False)
print(f'  cookies → {cookies_file} ({len(cookies)} cookies)')

# storage_state via Playwright API
try:
    state = ctx.storage_state(path=state_file)
    print(f'  storage_state → {state_file}')
except Exception as e:
    print(f'  storage_state err: {e}')

# Logout opcional — saltamos para preservar la sesión en cookies
b.close(); pw.stop()

# ── 3. Cerrar perfil MoreLogin con cleanup discriminado ──
import bot_orchestrator as bo
print('\n[2] cleanup + close perfil')
res = bo._close_profile_clean(ENV_ID, PORT, PROFILE)
print(f'  {res}')
bo._release_profile(PROFILE)

# ── 4. Registrar cuenta en account_manager ──
import account_manager as am
print('\n[3] alta en account_manager')

label = f"U2 · {u['Nombre']} {u['Apellidos']} · {EMAIL}".strip()
# Override etiqueta con nombre real usado
label = f"U2 · Julio Martin Perez · {EMAIL}"

acc = am.account_manager.add_account({
    'label': label,
    'tm_email': EMAIL,
    'tm_password': u['Password_TM'],
    'first_name': 'Julio',
    'last_name': 'Martin Perez',
    'dob': u['Fecha_Nacimiento'],
    'nationality': u['Nacionalidad'],
    'passport': u['Pasaporte'],
    'phone_prefix': '+34',
    'phone': PHONE_DIGITS,
    'phone_full': PHONE,
    'phone_provider': '5sim',
    'phone_order_id': pu.get('order_id'),
    'phone_sms_code': pu.get('sms_code'),
    'phone_verified_at': pu.get('verified_at'),
    'address1': u['Direccion'],
    'city': u['Ciudad'],
    'state': u['Estado'],
    'postal': u['ZIP'],
    'country': u['Pais'],
    'email': u['Email_Contacto'],
    'email_password': u['Password_Email'],
    'notes': f'Registrada {datetime.now().strftime("%Y-%m-%d %H:%M")} en perfil {PROFILE}. '
             f'Cookies en {cookies_file}.',
    'morelogin_profile': PROFILE,
    'morelogin_env_id': ENV_ID,
    'cookies_file': cookies_file,
    'storage_state_file': state_file,
    'status': 'active',
    'source': 'plantilla_tm.csv row 2',
})
ACC_ID = acc['id']
print(f'  ✓ account_id={ACC_ID}  label={label}')

# ── 5. Tarjeta asociada ──
card = am.account_manager.add_card({
    'account_id': ACC_ID,
    'label': f'Tarjeta principal U2',
    'holder': u['Titular'],
    'number': u['Numero_Tarjeta'],
    'exp_month': u['Mes_Exp'],
    'exp_year': u['Anno_Exp'],
    'cvv': u['CVV'],
    'last4': u['Numero_Tarjeta'][-4:],
    'brand': 'visa' if u['Numero_Tarjeta'].startswith('4') else 'mastercard',
})
print(f"  ✓ card_id={card['id']}  ****{card.get('last4')}")

# ── 6. Sync CSV maestro ──
print('\n[4] sync cuentas_tarjetas.csv')
import accounts_csv_sync
sync = accounts_csv_sync.sync_out()
print(f'  {sync}')

# ── 7. Verificación dashboard ──
print('\n[5] verify /api/accounts/with-purchases')
try:
    r = requests.post('http://127.0.0.1:5055/login',
                      data={'username':'admin','password':'admin'}, timeout=8, allow_redirects=False)
    cookies = r.cookies
    api = requests.get('http://127.0.0.1:5055/api/accounts/with-purchases', cookies=cookies, timeout=8).json()
    found = [a for a in (api.get('accounts') or []) if a.get('id') == ACC_ID]
    if found:
        print(f'  ✅ visible en dashboard: {found[0].get("label")} | tm_email={found[0].get("tm_email")}')
    else:
        print(f'  ⚠ no encontrada (puede tardar 1s en sync)')
except Exception as e:
    print(f'  err: {e}')

print('\n═══ DONE U2 ═══')
print(f'  account_id : {ACC_ID}')
print(f'  cookies    : {cookies_file}')
print(f'  state      : {state_file}')
print(f'  visible en : http://127.0.0.1:5055 → Cuentas y Tarjetas → "🎯 Cuentas TM por evento"')
