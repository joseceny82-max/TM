"""register_account.py — Registro TM end-to-end depurado.

Pipeline:
  1. pick_profile     — perfil ES nuevo (excluyendo quemados)
  2. warm_cookies     — cookie warming HTTP previo
  3. signup_form      — email + password + datos + Siguiente
  4. email_otp        — OTP IMAP (timezone-aware, filter post sent_at)
  5. phone_relentless — buy 5sim → paste → SMS poll → confirm (con blacklist + retries)
  6. finalize_account — Aceptar TOC + Skip passkey
  7. save_artifacts   — cookies + storage_state
  8. close_profile    — cleanup discriminado MoreLogin
  9. register_dash    — alta en account_manager + tarjeta + sync CSV

Uso:
  python register_account.py --row 3
  python register_account.py --rows 3-10
  python register_account.py --row 5 --first Julio --last "Martin Perez"
  python register_account.py --row 3 --burned P-170131,P-170130
"""
from __future__ import annotations
import argparse, csv, json, os, random, re, socket, sys, threading, time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

import requests
from playwright.sync_api import sync_playwright, Page, BrowserContext

import bot_orchestrator as _bo_morelogin

ENGINE = os.environ.get('TM_ENGINE', 'morelogin').lower()


def _load_engine(engine: str = None):
    """Motor de perfiles: 'morelogin' (default) o 'silent' (Silent del usuario).
    silent_engine expone la misma superficie que bot_orchestrator."""
    if (engine or ENGINE) == 'silent':
        import silent_engine
        return silent_engine
    return _bo_morelogin


bo = _load_engine()
import sms_receiver
import cookie_warmer
import account_manager as am
import accounts_csv_sync
from imap_tools import MailBox

# ─────────────────── config & constantes ───────────────────
ROOT = r'C:\Users\user\TM\proyecto'
PLANTILLA = os.path.join(ROOT, 'plantilla_tm.csv')
RUNS_DIR = os.path.join(ROOT, 'datos', 'buy', 'runs')
COOKIES_DIR = os.path.join(ROOT, 'datos', 'cookies')
SESSIONS_DIR = os.path.join(ROOT, 'datos', 'sessions')
SMS_TIMEOUT_S = 180  # antes 120s — logs muestran SMS legítimos de SMSPool a ~108s; ~10-15% se perdían con 120s
SMS_RESEND_EXTRA_S = 120  # espera extra tras pedir a TM que reenvíe el SMS al mismo número
SMS_RESEND_JS = r"""() => {
  const re = /p(edir|ide) uno nuevo|solicita(r)? (uno|un) nuevo|reenviar|enviar de nuevo|resend|send.*new code/i;
  const byId = document.querySelector('#asvd-request-code-link, [data-bdd="asvd-request-code-link"]');
  const el = (byId && byId.offsetParent !== null) ? byId : Array.from(document.querySelectorAll('button, a, [role=button]'))
    .find(e => re.test((e.innerText || '').trim()) && e.offsetParent !== null && !e.disabled);
  if (!el) return null;
  el.click();
  return (el.innerText || '').trim().slice(0, 40);
}"""
PENDING_ORDERS_RECHECK = []  # órdenes timeoutadas que mantenemos vivas en background por si llega SMS tarde
MAX_PHONE_CYCLES = 6
BUY_NO_STOCK_RETRIES = 10
BUY_BACKOFF_S = 5   # backoff base creciente: min(5*(k+1), 20) → 5,10,15,20,20…

# Prefetch del número SMS en background: la compra puede tardar minutos bajo
# rate-limit de los providers, así que se lanza un hilo daemon en cuanto el
# email OTP se verifica, solapando la espera con el resto del flow.
PREFETCH_MAX_AGE_S = 180
PREFETCHED_NUMBER: dict = {'lock': threading.Lock(), 'result': None,
                           'ts': 0.0, 'discarded': False}
DASHBOARD_LOGIN = ('admin', 'admin')

for d in (RUNS_DIR, COOKIES_DIR, SESSIONS_DIR):
    os.makedirs(d, exist_ok=True)

# ─────────────────── logging ───────────────────
def log(msg: str, *, level: str = 'INFO'):
    ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    print(f'[{ts}] {level:5} {msg}', flush=True)

# ─────────────────── data classes ───────────────────
@dataclass
class UserRow:
    n: int; email: str; password: str
    contact_email: str; contact_pwd: str
    first: str; last: str
    dob: str; nationality: str; passport: str
    phone_prefix: str; phone_local: str
    address1: str; city: str; state: str; postal: str; country: str
    card_number: str; card_holder: str; card_month: str; card_year: str; card_cvv: str
    notes: str

    @staticmethod
    def from_csv_row(idx: int, r: dict) -> 'UserRow':
        tel = r.get('Telefono', '+34000000000')
        prefix = '+34'
        local = tel.lstrip('+')
        if local.startswith('34'): local = local[2:]
        return UserRow(
            n=idx, email=r['Email_TM'].strip(), password=r['Password_TM'],
            contact_email=r['Email_Contacto'].strip(), contact_pwd=r['Password_Email'].strip(),
            first=r['Nombre'].strip(), last=r['Apellidos'].strip(),
            dob=r['Fecha_Nacimiento'], nationality=r['Nacionalidad'],
            passport=r['Pasaporte'],
            phone_prefix=prefix, phone_local=local,
            address1=r['Direccion'], city=r['Ciudad'], state=r['Estado'],
            postal=r['ZIP'], country=r['Pais'],
            card_number=r['Numero_Tarjeta'], card_holder=r['Titular'],
            card_month=r['Mes_Exp'], card_year=r['Anno_Exp'], card_cvv=r['CVV'],
            notes=r.get('Notas', '') or '',
        )

@dataclass
class RunState:
    run_id: str; row_idx: int; email: str
    started_at: str
    profile: Optional[str] = None; env_id: Optional[str] = None
    port: Optional[int] = None; ip: Optional[str] = None
    # Norma 2026-05-13: cuando create_fresh_profile arma proxy+env on-the-fly,
    # propagamos proxy_id+auto_created para que close_profile pueda destruirlos
    # via bot_orchestrator.destroy_fresh_profile (no acumular en MoreLogin).
    proxy_id: Optional[str] = None; auto_created: bool = False
    sessionid: Optional[str] = None
    burned_profiles: list = field(default_factory=list)
    blocked_phones: list = field(default_factory=list)
    phone_attempts: list = field(default_factory=list)
    stages: dict = field(default_factory=dict)
    final: dict = field(default_factory=dict)
    success: bool = False
    error: Optional[str] = None

    def stage_done(self, name: str, payload: Optional[dict] = None):
        self.stages[name] = {'at': datetime.now(timezone.utc).isoformat(),
                             **(payload or {})}
        self.persist()

    def persist(self):
        path = os.path.join(RUNS_DIR, f'{self.run_id}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

# ─────────────────── helpers ───────────────────
GARBAGE_RE = re.compile(r'^([a-z])\1+$|^[qwerty]+$', re.I)

def looks_garbage(s: str) -> bool:
    s = (s or '').strip()
    if len(s) < 3: return True
    if not re.match(r'^[A-Za-zÁÉÍÓÚáéíóúÑñ ]+$', s): return True
    return bool(GARBAGE_RE.match(s))

def load_user(row_idx: int) -> UserRow:
    with open(PLANTILLA, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if row_idx < 1 or row_idx > len(rows):
        raise ValueError(f'row {row_idx} fuera de rango (1..{len(rows)})')
    return UserRow.from_csv_row(row_idx, rows[row_idx - 1])

def load_burned() -> set:
    """Lee burned_profiles.json acumulativo entre runs."""
    p = os.path.join(RUNS_DIR, 'burned_profiles.json')
    try:
        with open(p, encoding='utf-8') as f: return set(json.load(f).get('burned', []))
    except Exception: return set()

def save_burned(s: set):
    p = os.path.join(RUNS_DIR, 'burned_profiles.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump({'burned': sorted(s)}, f, indent=2)

def load_blocked_phones() -> set:
    p = os.path.join(RUNS_DIR, 'blocked_phones.json')
    try:
        with open(p, encoding='utf-8') as f: return set(json.load(f).get('blocked', []))
    except Exception: return set()

def save_blocked_phones(s: set):
    p = os.path.join(RUNS_DIR, 'blocked_phones.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump({'blocked': sorted(s)}, f, indent=2)

def js_click(page: Page, regex: str, *, blue_only: bool = False, exact: bool = True) -> Optional[str]:
    """Click botón cuyo texto matchea regex. Tolerante a navigation/context destroyed."""
    try:
        return page.evaluate(rf"""(args) => {{
          const re = new RegExp(args.regex, 'i');
          // Ampliado: incluir <a> normales, [role=link], y elementos con onclick — para cazar
          // "Solicita un nuevo código" que TM renderiza como <a> inline sin role=button.
          const btns = Array.from(document.querySelectorAll('button,a,[role="button"],[role="link"],[onclick]')).filter(b => !b.disabled);
          const isBlue = b => /^rgb\(\s*2\s*,/.test(getComputedStyle(b).backgroundColor);
          const matchTxt = b => {{
            const t = (b.innerText || '').trim();
            return args.exact ? new RegExp('^(' + args.regex + ')$', 'i').test(t) : re.test(t);
          }};
          const cand = btns.find(b => matchTxt(b) && (!args.blueOnly || isBlue(b)))
                    || btns.find(b => matchTxt(b));
          if (cand) {{ cand.scrollIntoView({{block:'center'}}); cand.click(); return cand.innerText.trim(); }}
          return null;
        }}""", {"regex": regex, "blueOnly": blue_only, "exact": exact})
    except Exception as e:
        # Navigation during evaluate is OK — el click probablemente disparó la navegación
        if 'execution context was destroyed' in str(e).lower() or 'navigation' in str(e).lower():
            return 'navigated'
        return None

def find_visible_input(page: Page, selectors: list, timeout_ms: int = 1500):
    for sel in selectors:
        try:
            cand = page.locator(sel).first
            if cand.is_visible(timeout=timeout_ms): return cand, sel
        except Exception: pass
    return None, None


def paste_otp_robust(page: Page, inp, code: str, label: str = 'OTP') -> bool:
    """Pega un código de OTP con MÚLTIPLES estrategias y verificación post-fill.
    Asegura que TM realmente recibe el value antes de avanzar.

    Estrategias en orden:
    1. inp.fill(code)            — atómico, dispara React events
    2. inp.press_sequentially()  — emula teclado real
    3. char-by-char con type()   — fallback final

    Verifica `inp.input_value()` después de cada estrategia. Solo retorna True
    si el value queda igual al code esperado (o substring fuzzy).
    Returns True si éxito, False si todas las estrategias fallan."""
    if not inp or not code: return False
    code = str(code).strip()

    def _check_value() -> bool:
        try:
            v = (inp.input_value() or '').strip()
        except Exception: return False
        # Match estricto o fuzzy (TM puede formatear con espacios entre dígitos)
        return v == code or ''.join(c for c in v if c.isdigit()) == code

    # Estrategia 1: fill() atómico
    try:
        inp.click()
        inp.fill('')
        page.wait_for_timeout(50)
        inp.fill(code)
        page.wait_for_timeout(150)
        if _check_value():
            log(f'  ✓ {label} pegado vía fill() — value="{inp.input_value()}"')
            return True
        log(f'  ⚠ {label} fill() no quedó (value="{inp.input_value() if inp else "?"}") — probando press_sequentially')
    except Exception as e:
        log(f'  ⚠ {label} fill() error: {e}')

    # Estrategia 2: press_sequentially (más fiable con React strict)
    try:
        inp.click()
        inp.fill('')
        page.wait_for_timeout(50)
        inp.press_sequentially(code, delay=20)
        page.wait_for_timeout(200)
        if _check_value():
            log(f'  ✓ {label} pegado vía press_sequentially — value="{inp.input_value()}"')
            return True
        log(f'  ⚠ {label} press_sequentially no quedó — probando char-by-char')
    except Exception as e:
        log(f'  ⚠ {label} press_sequentially error: {e}')

    # Estrategia 3: char-by-char con type() (último recurso)
    try:
        inp.click()
        inp.fill('')
        page.wait_for_timeout(50)
        for ch in code:
            inp.type(ch, delay=random.randint(30, 60))
        page.wait_for_timeout(250)
        if _check_value():
            log(f'  ✓ {label} pegado vía char-by-char — value="{inp.input_value()}"')
            return True
        log(f'  ❌ {label} char-by-char tampoco — value="{inp.input_value() if inp else "?"}"')
    except Exception as e:
        log(f'  ❌ {label} char-by-char error: {e}')

    return False

def human_type(loc, text: str, lo: int = 35, hi: int = 70):
    loc.click(); loc.fill('')
    for ch in str(text): loc.type(ch, delay=random.randint(lo, hi))

# ─────────────────── pipeline ───────────────────
# Proveedor de proxy obligatorio para los perfiles (substring del host en MoreLogin).
# Con filtro activo NO se crean perfiles AUTO-ES (llevan Quantum fijo). '' lo desactiva.
PROXY_HOST_FILTER = os.environ.get('TM_PROXY_HOST', 'fast.froxy').strip().lower()


def _pick_es_profile_smart(burned: set) -> Optional[dict]:
    """Pick perfil ES detectando país por countryCode O por username del proxy.
    Verifica proxy via HTTP request directo ANTES de devolver. Si falla, sigue."""
    bo = _load_engine()
    used_ips = load_used_ips()
    with bo._pick_lock:
        for page_no in range(1, 21):
            try:
                r = requests.post(f'{bo.MORELOGIN_API}/api/env/page',
                                  json={'pageNo': page_no, 'pageSize': 100}, timeout=15).json()
            except Exception: break
            items = (r.get('data') or {}).get('dataList') or []
            if not items: break
            for it in items:
                p = it.get('proxy') or {}
                name = it.get('envName') or ''
                if not name or name in bo._active_profiles or name in burned: continue
                cc = (p.get('countryCode') or '').upper()
                user = p.get('username') or ''
                is_es = (cc == 'ES') or ('cc-ES' in user) or ('-ES-' in user)
                if not is_es: continue
                if PROXY_HOST_FILTER and PROXY_HOST_FILTER not in (p.get('proxyIp') or '').lower():
                    continue
                check = p.get('proxyCheckStatus')
                if check == 2: continue
                exp_ip = p.get('exportIp') or ''
                if exp_ip and exp_ip in used_ips: continue
                # Verify proxy via HTTP — sin tocar MoreLogin
                proxy_host = p.get('proxyIp') or 'residential.quantumproxies.net'
                # MoreLogin guarda el host en proxyIp (string)
                # Puerto real del perfil (Froxy 9000-9301, Quantum 8888); antes fijo a 8888.
                proxy_port = int(p.get('proxyPort') or 8888)
                real_ip = _verify_proxy_http(user, p.get('password',''),
                                              proxy_host=proxy_host, proxy_port=proxy_port)
                if not real_ip:
                    continue   # proxy no responde, siguiente
                if real_ip in used_ips:
                    continue   # IP ya usada antes
                bo._active_profiles.add(name)
                return {'profile': name, 'env_id': it['id'],
                        'ip': real_ip,
                        'check_status': check,
                        'proxy': p}   # incluir proxy info para pre-check HTTP
    return None


PROFILE_PICK_LOCK_PATH = os.path.join(RUNS_DIR, 'profile_pick.lock')
ACTIVE_STARTS_PATH = os.path.join(RUNS_DIR, 'active_starts.json')


def _load_active_starts() -> dict:
    """{env_id: {pid, port, profile, started_at}}. Stale (>30 min o pid muerto) se purga."""
    try:
        with open(ACTIVE_STARTS_PATH, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        d = {}
    # Purge stale: pid muerto o >30 min
    import psutil as _ps
    now = time.time()
    cleaned = {}
    for env_id, info in (d or {}).items():
        try:
            pid = info.get('pid')
            started = info.get('started_at', 0)
            if pid and _ps.pid_exists(pid) and (now - started) < 1800:
                cleaned[env_id] = info
        except Exception:
            pass
    return cleaned


def _save_active_starts(d: dict):
    try:
        with open(ACTIVE_STARTS_PATH, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        log(f'  ⚠ save active_starts err: {e}')


def _register_active_start(env_id: str, port: int, profile: str) -> bool:
    """Registra que ESTE proceso usa env_id+port. Si ya está usado por OTRO proceso vivo,
    devuelve False (caller debe descartar el perfil y pickear otro).
    """
    d = _load_active_starts()
    existing = d.get(env_id) or d.get(str(env_id))
    if existing:
        ex_pid = existing.get('pid')
        ex_port = existing.get('port')
        if ex_pid != os.getpid():
            log(f'  ⚠ COLISIÓN: env_id={env_id} ya en uso por PID={ex_pid} port={ex_port} — descartando perfil')
            return False
    d[str(env_id)] = {
        'pid': os.getpid(),
        'port': port,
        'profile': profile,
        'started_at': time.time(),
    }
    _save_active_starts(d)
    return True


def _release_active_start(env_id):
    """Libera la entrada. Llamado en cleanup tras cerrar el perfil."""
    try:
        d = _load_active_starts()
        if str(env_id) in d:
            del d[str(env_id)]
            _save_active_starts(d)
    except Exception: pass


def _with_profile_pick_lock(fn, *args, **kwargs):
    """Lock cross-process para pick + claim atómico de perfil MoreLogin.
    Evita que dos register_account.py paralelos elijan el mismo P-XXXXX y compartan navegador.
    Stale lock > 30s se descarta."""
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            fd = os.open(PROFILE_PICK_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try: os.write(fd, str(os.getpid()).encode())
            finally: os.close(fd)
            try: return fn(*args, **kwargs)
            finally:
                try: os.unlink(PROFILE_PICK_LOCK_PATH)
                except Exception: pass
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(PROFILE_PICK_LOCK_PATH)
                if age > 30:
                    try: os.unlink(PROFILE_PICK_LOCK_PATH)
                    except Exception: pass
            except Exception: pass
            time.sleep(0.4)
    log('  ⚠ profile_pick lock timeout — proceeding sin lock (riesgo race)')
    return fn(*args, **kwargs)

def _pick_and_claim_profile(country: str, burned_local: set) -> Optional[dict]:
    """NORMA USER 2026-05-13: por defecto CREA perfil FRESH con proxy custom
    Quantum + sessionid random. Si falla, fallback a pool existente.
    Garantiza IP nueva + perfil único cada registro."""
    if getattr(bo, '__name__', '') == 'silent_engine':
        prof = _create_silent_profile(country)
        if prof:
            all_burned = load_burned(); all_burned.add(prof['profile']); save_burned(all_burned)
        return prof

    if PROXY_HOST_FILTER:
        prof = _create_profile_on_unused_proxy(PROXY_HOST_FILTER, country)
        if prof:
            all_burned = load_burned(); all_burned.add(prof['profile']); save_burned(all_burned)
        return prof

    # ─── PRIMERO: crear perfil FRESH ───
    try:
        fresh = bo.create_fresh_profile(country_code=country)
        if fresh:
            log(f'  + perfil FRESH creado: {fresh.get("profile")} (sessid={fresh.get("sessionid")})')
            # Claim en burned para excluir en próximos procesos
            all_burned = load_burned()
            all_burned.add(fresh['profile'])
            save_burned(all_burned)
            return fresh
    except Exception as _fe:
        log(f'  fresh_create_err: {str(_fe)[:120]}')

    # ─── FALLBACK: pool existente (legacy) ───
    fresh_burned = set(burned_local) | load_burned()
    prof = _pick_es_profile_smart(fresh_burned) if country == 'ES' else None
    if not prof:
        prof = bo._pick_fresh_profile(country_code=country, quemados=fresh_burned)
    if not prof:
        prof = bo._pick_fresh_profile(country_code=country, quemados=fresh_burned,
                                      allow_unverified=True)
    if prof:
        all_burned = load_burned(); all_burned.add(prof['profile']); save_burned(all_burned)
    return prof


def open_profile(country: str, burned: set, run: RunState) -> tuple[BrowserContext, Page, dict, int]:
    log(f'pick fresh {country} profile (excluyendo {len(burned)} quemados)')
    burned_local = set(burned)
    prof = port = None
    real_ip = None
    for attempt in range(10):
        # ATÓMICO bajo lock: pick + claim en burned_profiles.json — evita race condition
        # entre procesos paralelos que pickeaban el mismo P-XXXXX.
        prof = _with_profile_pick_lock(_pick_and_claim_profile, country, burned_local)
        if not prof:
            raise RuntimeError('no hay ningún perfil disponible — '
                               'comprueba MoreLogin desktop / proxies.')
        burned_local.add(prof['profile'])  # propagar al set local del proceso
        candidate_port = bo._start_profile(prof['env_id'])
        if not candidate_port:
            log(f'  ⚠ start failed for {prof["profile"]}, blacklisting + retry ({attempt+1}/10)')
            burned_local.add(prof['profile'])
            bo._release_profile(prof['profile'])
            time.sleep(5); continue

        # ANTI-COLLISION 2026-05-10: verificar que ningún OTRO proceso register
        # tenga este env_id activo. Bug detectado: 2 procesos paralelos arrancaron
        # P-171238 con el mismo port=63132, el segundo robó el browser del primero.
        # _register_active_start devuelve False si hay colisión → blacklist + next.
        if not _register_active_start(prof['env_id'], candidate_port, prof['profile']):
            log(f'  ⚠ env_id {prof["env_id"]} ya en uso por otro proceso — blacklist + retry ({attempt+1}/10)')
            try: bo._close_profile_clean(prof['env_id'], candidate_port, prof['profile'])
            except Exception: pass
            burned_local.add(prof['profile'])
            bo._release_profile(prof['profile'])
            time.sleep(3); continue

        # DOUBLE-CLAIM tras start exitoso — garantiza que el perfil quede en
        # burned_profiles.json incluso si el claim atómico anterior se perdió por IO.
        # Verifica con load_burned() después de save_burned() para detectar fallos.
        try:
            _b = load_burned(); _b.add(prof['profile']); save_burned(_b)
            _check = load_burned()
            if prof['profile'] not in _check:
                log(f'  ⚠⚠ CRITICAL: claim de {prof["profile"]} no persistió tras save_burned')
            else:
                log(f'  🔒 claim verificado {prof["profile"]} en burned ({len(_check)} entries)')
        except Exception as e:
            log(f'  ⚠ double-claim err: {e}')

        # Registrar la IP como usada — evita que otros procesos elijan un perfil
        # con la misma IP (Quantum proxies pueden asignar misma IP a perfiles distintos).
        ip_addr = prof.get('ip')
        if ip_addr:
            try: save_used_ip(ip_addr, run.run_id)
            except Exception as e: log(f'  ⚠ save_used_ip err: {e}')

        # Sin verificación previa de IP — esa función cerraba el browser CDP por bug.
        # El IP-check se hace en goto_signup (TM home page tarda <30s en cargar si proxy OK).
        log(f'  ✓ perfil arrancado {prof["profile"]} port={candidate_port} ip={prof.get("ip")}')
        port = candidate_port; break

    if not port:
        raise RuntimeError('no se pudo arrancar perfil con proxy funcional tras 10 intentos. '
                           'PROXIES MORELOGIN ROTOS — arreglar en MoreLogin desktop antes de continuar.')
    log(f'  perfil={prof["profile"]} ip={prof["ip"]} port={port}')
    time.sleep(5)
    run.profile = prof['profile']; run.env_id = prof['env_id']
    run.port = port; run.ip = prof.get('ip')
    # 2026-05-13: propagar marca auto_created + proxy_id para destroy en cleanup.
    run.auto_created = bool(prof.get('auto_created'))
    run.proxy_id = prof.get('proxy_id')
    run.sessionid = prof.get('sessionid')
    run.stage_done('profile_started', {'profile': prof['profile'], 'ip': prof.get('ip'),
                                       'port': port, 'auto_created': run.auto_created,
                                       'sessionid': run.sessionid})

    # PRE-CHECK HTTP: hacer un GET con curl_cffi (TLS Chrome real) a www.ticketmaster.es
    # USANDO el proxy MoreLogin del perfil — si TM ya devuelve bot_block via ese proxy,
    # abortamos ANTES del browser y blacklisteamos la IP.
    try:
        import http2_frame_match
        proxies_arg = None
        try:
            # Intentar reusar el proxy del perfil (ya disponible via _verify_proxy_http)
            ml_user = (prof.get('proxy') or {}).get('username') if prof.get('proxy') else None
            ml_pass = (prof.get('proxy') or {}).get('password') if prof.get('proxy') else None
            ml_host = (prof.get('proxy') or {}).get('proxyIp') if prof.get('proxy') else None
            ml_port = (prof.get('proxy') or {}).get('proxyPort', 8888) if prof.get('proxy') else 8888
            ml_type = (prof.get('proxy') or {}).get('proxyType', 0) if prof.get('proxy') else 0
            if ml_user and ml_pass and ml_host:
                proxy_url = _proxy_url(ml_user, ml_pass, ml_host, ml_port, ml_type)
                proxies_arg = {'http': proxy_url, 'https': proxy_url}
        except Exception: pass
        if not proxies_arg:
            # Sin credenciales del proxy (Silent no las expone): el GET saldría desde la IP
            # real del PC y no validaría nada. La IP ya se comprobó desde el navegador.
            raise _SkipPreCheck()
        pre = http2_frame_match.request_via_curl_cffi(
            'GET', 'https://www.ticketmaster.es/', timeout=10,
            profile_name='chrome_131_windows',
            proxies=proxies_arg)
        if pre.get('ok'):
            body_low = (pre.get('body') or '').lower()
            if 'not in the whitelist' in body_low:
                # Fallo de configuración del proveedor, NO bot_block: no quemar la IP.
                _warn_whitelist_once(prof.get('proxy', {}).get('proxyIp', '?'))
                try: bo._close_profile_clean(run.env_id, run.port, run.profile)
                except Exception: pass
                try: _release_active_start(run.env_id)
                except Exception: pass
                raise RuntimeError('proxy_whitelist_denied')
            blockers = ('su sesión se ha suspendido', 'su sesion se ha suspendido',
                        'algo en su forma de navegar', 'session has been suspended',
                        'pardon our interruption', 'access denied',
                        'reference id:', 'unusual activity')
            if any(b in body_low for b in blockers):
                log(f'  ⚠ HTTP pre-check detectó bot_block — blacklist IP {prof.get("ip")} y abortar', level='WARN')
                try: save_used_ip(prof['ip'], f'bot_block_pre_check_{run.run_id}')
                except Exception: pass
                try: bo._close_profile_clean(run.env_id, run.port, run.profile)
                except Exception: pass
                try: _release_active_start(run.env_id)
                except Exception: pass
                raise RuntimeError(f'bot_block_detected: HTTP pre-check via_proxy ip={prof.get("ip")}')
            else:
                log(f'  ✓ HTTP pre-check OK ({pre.get("status_code")}) ip={prof.get("ip")}')
    except _SkipPreCheck:
        log('  · HTTP pre-check omitido (proxy sin credenciales visibles, IP ya verificada)')
    except RuntimeError: raise
    except Exception as e:
        log(f'  ⚠ HTTP pre-check err (no fatal): {str(e)[:100]}')

    # cookie warming HTTP
    warm = cookie_warmer.warm_cookies(domain_id='es', pages=4, timeout=10.0)
    log(f'  warmer ok={warm.get("ok")} cookies={len(warm.get("cookies") or {})}')
    run.stage_done('cookie_warming', {'ok': bool(warm.get('ok')), 'count': len(warm.get('cookies') or {})})

    # connect playwright
    v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
    pw = sync_playwright().start()
    b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
    ctx = b.contexts[0]
    ctx.add_init_script("""
      if (window.Notification) { Notification.requestPermission = () => Promise.resolve('denied');
        try { Object.defineProperty(Notification,'permission',{get:()=>'denied'}); } catch(e){} }
      if (navigator.serviceWorker?.register) navigator.serviceWorker.register = () => Promise.reject(new Error('blocked'));
    """)
    inj = 0
    for n, vc in (warm.get('cookies') or {}).items():
        try:
            ctx.add_cookies([{'name': n, 'value': str(vc),
                              'domain': '.ticketmaster.es', 'path': '/'}])
            inj += 1
        except Exception: pass
    log(f'  ✓ {inj} cookies warmed inyectadas')

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.bring_to_front()
    # FORZAR limpieza COMPLETA de cookies/localStorage antes de operar.
    # El cookie_filter.partition_cookies del run anterior puede dejar cookies de TM
    # vivas entre perfiles, haciendo que TM aparezca pre-logueado y skipee signup.
    try:
        ctx.clear_cookies()
        log('  ✓ ctx.clear_cookies() — empezamos sin cookies')
        # localStorage/sessionStorage requiere navegación a un origen primero
        page.goto('about:blank', wait_until='domcontentloaded', timeout=10000)
    except Exception as e:
        log(f'  ⚠ clear_cookies err: {e}')
    # devolver también el browser para poder cerrarlo, pero por simplicidad hacemos pw global
    page._pw = pw  # type: ignore
    page._b = b  # type: ignore
    return ctx, page, prof, port

def detect_bot_block(page: Page) -> Optional[str]:
    """Detecta pantallas anti-bot. Busca en title + innerHTML (no innerText, porque
    modales de cookies tapan el bot-block en innerText)."""
    try:
        text = page.evaluate(r"""() => {
          const t = (document.title || '').toLowerCase();
          // innerHTML contiene TODOS los nodos aunque estén tapados por modal
          const html = (document.documentElement?.innerHTML || '').toLowerCase();
          // Headings y párrafos visibles directamente
          const heads = Array.from(document.querySelectorAll('h1, h2, h3, p'))
            .map(e => (e.innerText || e.textContent || '')).join('\n').toLowerCase();
          return t + '\n' + heads + '\n' + html.slice(0, 50000);
        }""")
    except Exception:
        return None
    triggers = [
        'su sesión se ha suspendido', 'session has been suspended',
        'pensar que era un bot', 'think you might be a bot',
        'access denied', 'acceso denegado',
        'forma de navegar o en su red',
        'pardon our interruption', 'unusual activity',
        'su sesion se ha suspendido',
        'sospechosa', 'reference id:',
        'your session has been suspended',
        'algo en su forma de navegar',
        '¿cómo puedo resolverlo?', 'como puedo resolverlo?',
        'inténtalo de nuevo desde otro dispositivo',
        'asegúrese de activar javascript',
        'eliminar los plug-ins',
    ]
    for t in triggers:
        if t in text:
            return t
    return None

def goto_signup(page: Page, user: UserRow, run: RunState) -> str:
    """Returns 'signup' | 'signin' | 'unknown'. Aborta con BotBlocked si detección anti-bot."""
    # WARMING humano: visitar primero la home pública www.ticketmaster.es (se ve
    # natural en el referer), aceptar cookies y luego ir a my.ticketmaster.es.
    # TM Akamai/Kasada flagea menos cuando el flujo es: home → signup vs directo a signup.
    home_url = 'https://www.ticketmaster.es/'
    log(f'goto home {home_url} (warming humano)')
    try:
        page.goto(home_url, wait_until='domcontentloaded', timeout=8000)
        page.wait_for_timeout(random.randint(400, 700))
        # Detect early bot_block en la propia home (más rápido que esperar al signup)
        try:
            blk_home = detect_bot_block(page)
            if blk_home:
                screenshot_on_error(page, run, 'bot_blocked_home')
                raise RuntimeError(f'bot_block_detected: "{blk_home}" (home)')
        except RuntimeError: raise
        except Exception: pass
        # Aceptar cookies en la home
        for txt in ['Aceptar todas las cookies', 'Aceptar todas', 'Aceptar']:
            try:
                btn = page.get_by_text(txt, exact=False).first
                if btn.is_visible(timeout=1200): btn.click(); break
            except Exception: pass
        # Scroll humano
        try:
            page.evaluate('window.scrollTo(0, 200)')
            page.wait_for_timeout(random.randint(150, 300))
            page.evaluate('window.scrollTo(0, 0)')
            page.wait_for_timeout(random.randint(100, 200))
        except Exception: pass
    except Exception as e:
        log(f'  ⚠ home warming err (no fatal): {str(e)[:80]}')

    url = f'https://my.ticketmaster.es/?_={int(time.time())}{random.randint(1000,9999)}'
    log(f'goto {url[:60]}')
    last_err = None
    for i in range(3):
        try: page.goto(url, wait_until='domcontentloaded', timeout=10000); break
        except Exception as e: last_err = e; log(f'  retry {i+1}: {str(e)[:80]}'); time.sleep(0.8)

    # Detección bot_block TEMPRANA (antes del wait long) — TM a veces rota al
    # bot-block solo durante los primeros 1-2s, perderlo al esperar 3s.
    try:
        blk_early = detect_bot_block(page)
        if blk_early:
            screenshot_on_error(page, run, 'bot_blocked_early')
            raise RuntimeError(f'bot_block_detected: "{blk_early}" (early)')
    except RuntimeError: raise
    except Exception: pass

    # Esperar a que termine cualquier redirect (TM hace varias redirects al cargar)
    try:
        page.wait_for_load_state('networkidle', timeout=4000)
    except Exception: pass
    page.wait_for_timeout(600)

    # Anti-bot block detection — tolerante a context destroyed
    try:
        blk = detect_bot_block(page)
        if blk:
            screenshot_on_error(page, run, 'bot_blocked')
            raise RuntimeError(f'bot_block_detected: "{blk}"')
    except RuntimeError: raise
    except Exception as e:
        log(f'  ⚠ detect_bot_block err (no fatal): {e}')

    # Cookies banner — tolerante
    for txt in ['Aceptar todas las cookies', 'Rechazarlas todas']:
        try:
            btn = page.get_by_text(txt, exact=False).first
            if btn.is_visible(timeout=800): btn.click(); page.wait_for_timeout(700); break
        except Exception: pass

    # Idle natural — tolerante a fallo de evaluate
    try:
        page.evaluate('window.scrollTo(0, 100)')
        page.wait_for_timeout(random.randint(450, 900))
        page.evaluate('window.scrollTo(0, 0)')
        page.wait_for_timeout(random.randint(600, 1200))
    except Exception as e:
        log(f'  ⚠ idle scroll err (no fatal): {e}')

    _wait_waiting_room(page)

    # Esperar input email visible — si no aparece, comprobar bot_block antes de fallar
    em = None
    for retry in range(3):
        try:
            em = page.locator('input[type="email"]').first
            em.wait_for(state='visible', timeout=8000)
            break
        except Exception as e:
            log(f'  ⏳ email input no visible (retry {retry+1}/3): {str(e)[:80]}')
            # Si input email no aparece, probablemente TM nos puso pantalla de bot_block
            try:
                blk = detect_bot_block(page)
                if blk:
                    screenshot_on_error(page, run, 'bot_blocked_no_email_input')
                    raise RuntimeError(f'bot_block_detected: "{blk}" (no email input)')
            except RuntimeError: raise
            except Exception: pass
            try: page.wait_for_load_state('networkidle', timeout=5000)
            except Exception: pass
            page.wait_for_timeout(1500)
    if not em:
        # Último check antes de rendirse: bot_block?
        try:
            blk = detect_bot_block(page)
            if blk:
                screenshot_on_error(page, run, 'bot_blocked_final')
                raise RuntimeError(f'bot_block_detected: "{blk}"')
        except RuntimeError: raise
        except Exception: pass
        screenshot_on_error(page, run, 'no_email_input_final')
        raise RuntimeError('bot_block_detected: email input nunca apareció (probable bot_block)')
    # FAST atomic paste — char-by-char causa race en paralelo (keyboard events
    # pueden ir al window equivocado entre 3+ browsers MoreLogin visibles).
    em.click(); em.fill('')
    em.fill(user.email)
    page.wait_for_timeout(300)
    actual = ''
    try: actual = em.input_value()
    except Exception: pass
    if actual != user.email:
        log(f'  ⚠ email no coincide: "{actual}" vs "{user.email}" — reintentando fill')
        em.fill('')
        em.fill(user.email)
        page.wait_for_timeout(300)
        actual = em.input_value() if em else ''
    log(f'  ✓ email "{user.email}" tipeado (value="{actual}")')
    log('  email tipeado')
    page.wait_for_timeout(450)
    js_click(page, r'continuar', exact=True)
    page.wait_for_timeout(3500)

    # La pantalla "Su sesión se ha suspendido" también se titula "Ticketmaster
    # Sign In": sin este check se tomaba por cuenta existente y se lanzaba una
    # recuperación inútil (fila 479, 2026-11-07).
    blk = detect_bot_block(page)
    if blk:
        screenshot_on_error(page, run, 'bot_blocked_post_email')
        raise RuntimeError(f'bot_block_detected: "{blk}" (tras email)')

    title = page.title().lower()
    mode = 'signup' if 'sign up' in title else ('signin' if 'sign in' in title else 'unknown')
    log(f'  modo={mode} title={page.title()}')
    run.stage_done('form_mode', {'mode': mode, 'title': page.title()})
    return mode

def _generate_strong_password(n: int = 16) -> str:
    """Genera password TM-compatible: 16 chars con mayús + minús + número + símbolo."""
    import string
    syms = '!@#$%^&*?+='
    chars = string.ascii_letters + string.digits + syms
    while True:
        p = ''.join(random.choice(chars) for _ in range(n))
        if (any(c.isupper() for c in p) and any(c.islower() for c in p)
            and any(c.isdigit() for c in p) and any(c in syms for c in p)):
            return p


def _update_password_in_plantilla(email: str, new_password: str) -> bool:
    """Actualiza Password_TM en plantilla_tm.csv para esa fila (con lock cross-process)."""
    def _do():
        import csv as _csv, shutil as _sh
        if not os.path.isfile(PLANTILLA): return False
        try:
            _sh.copy2(PLANTILLA, PLANTILLA + '.bak.prepwd')
            with open(PLANTILLA, 'r', encoding='utf-8-sig', newline='') as f:
                reader = _csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                rows = list(reader)
            updated = False
            for row in rows:
                if (row.get('Email_TM') or '').strip().lower() == email.lower():
                    row['Password_TM'] = new_password
                    updated = True
                    break
            if not updated: return False
            # tmp único por proceso para evitar contención
            tmp = PLANTILLA + f'.tmp.{os.getpid()}'
            with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
                w = _csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader(); w.writerows(rows)
            os.replace(tmp, PLANTILLA)
            return True
        except Exception as e:
            log(f'  ⚠ no pude actualizar password en plantilla: {e}')
            return False
    return _with_plantilla_lock(_do)


def _update_phone_in_plantilla(email: str, new_phone_full: str) -> bool:
    """Actualiza Telefono en plantilla_tm.csv (con lock cross-process)."""
    def _do():
        import csv as _csv, shutil as _sh
        if not os.path.isfile(PLANTILLA): return False
        try:
            _sh.copy2(PLANTILLA, PLANTILLA + '.bak.prephone')
            with open(PLANTILLA, 'r', encoding='utf-8-sig', newline='') as f:
                reader = _csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                rows = list(reader)
            updated = False
            for row in rows:
                if (row.get('Email_TM') or '').strip().lower() == email.lower():
                    row['Telefono'] = new_phone_full
                    updated = True
                    break
            if not updated: return False
            tmp = PLANTILLA + f'.tmp.{os.getpid()}'
            with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
                w = _csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader(); w.writerows(rows)
            os.replace(tmp, PLANTILLA)
            return True
        except Exception as e:
            log(f'  ⚠ no pude actualizar telefono en plantilla: {e}')
            return False
    return _with_plantilla_lock(_do)


def fill_signup_form(page: Page, user: UserRow, run: 'RunState'):
    log('fill signup form')
    try:
        pw_el = page.locator('input[type="password"]').first
        pw_el.wait_for(state='visible', timeout=10000)
        # fill() atómico — char-by-char en paralelo provoca race entre browsers
        pw_el.click(); pw_el.fill('')
        pw_el.fill(user.password)
        log(f'  ✓ password ({len(user.password)} chars)')
        page.wait_for_timeout(800)
        # Detectar si TM rechaza el password — SOLO via [aria-invalid="true"] o role=alert
        # cerca del input, no por substring del body (los hints "al menos 12" son texto de ayuda
        # SIEMPRE visible junto al input, no errores reales).
        invalid = page.evaluate(r"""() => {
          const pw = document.querySelector('input[type="password"]');
          if (!pw) return false;
          if (pw.getAttribute('aria-invalid') === 'true') return true;
          // Buscar role=alert o .error visible cerca del input password
          const container = pw.closest('div, fieldset, form') || document.body;
          const alerts = container.querySelectorAll('[role="alert"], [data-error="true"], .error-message, .field-error');
          for (const a of alerts) {
            const t = (a.innerText || '').trim();
            if (t && (t.toLowerCase().includes('contraseña') || t.toLowerCase().includes('password'))) return true;
          }
          return false;
        }""")
        if invalid:
            log(f'  ⚠ TM rechaza password — generando nuevo y reintentando')
            new_pwd = _generate_strong_password(16)
            user.password = new_pwd  # actualiza en memoria para resto del flow
            pw_el.click(); pw_el.fill(''); pw_el.fill(new_pwd)
            log(f'  ✓ password regenerado ({len(new_pwd)} chars)')
            # Persistir el nuevo password en plantilla_tm.csv
            if _update_password_in_plantilla(user.email, new_pwd):
                log(f'  ✓ password guardado en plantilla_tm.csv')
            page.wait_for_timeout(600)
    except Exception as e:
        log(f'  ⚠ pwd: {e}')

    for sel, val, name in [
        ('input[autocomplete="given-name"]', user.first, 'first'),
        ('input[autocomplete="family-name"]', user.last, 'last'),
        ('input[autocomplete="postal-code"]', user.postal, 'zip'),
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500) and not loc.input_value().strip():
                # fill() atómico — sin race en paralelo
                loc.click(); loc.fill(''); loc.fill(str(val))
                log(f'  ✓ {name}={val}')
        except Exception: pass

    page.wait_for_timeout(800)
    page.evaluate(r"""() => {
      const m = Array.from(document.querySelectorAll('button')).find(b => /^acepto$/i.test((b.innerText||'').trim()));
      if (m) m.click();
      document.querySelectorAll('[id*=onetrust i]').forEach(e => e.remove());
    }""")
    _dismiss_ketch_consent(page)
    page.wait_for_timeout(800)
    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
    page.wait_for_timeout(1200)
    # Marcar TODOS los checkboxes obligatorios (T&C, marketing opt-out, etc.)
    cbs_checked = 0
    for cb in page.locator('input[type="checkbox"]').all():
        try:
            if cb.is_visible(timeout=200) and not cb.is_checked():
                cb.check(force=True); cbs_checked += 1
        except Exception: pass
    if cbs_checked: log(f'  ✓ {cbs_checked} checkboxes marcados')

    # Verificar errores de validación ANTES de clickar Siguiente
    page.wait_for_timeout(500)
    err_check = page.evaluate(r"""() => {
      const errors = [];
      document.querySelectorAll('[role="alert"], [aria-invalid="true"], .error, [data-error]').forEach(e => {
        const t = (e.innerText || '').trim().slice(0, 100);
        if (t) errors.push(t);
      });
      // Detectar mensajes de error específicos visibles
      const body = (document.body?.innerText || '').slice(0, 1500).toLowerCase();
      const triggers = ['no es válida', 'no es valido', 'requerido', 'required',
                        'completa', 'rellena', 'campo obligatorio', 'al menos 8'];
      const found = triggers.filter(t => body.includes(t));
      return { errors, validation_warnings: found };
    }""")
    if err_check.get('errors') or err_check.get('validation_warnings'):
        log(f'  ⚠ avisos pre-Siguiente: errors={err_check.get("errors")[:3]} '
            f'warnings={err_check.get("validation_warnings")}')

    # Esperar a que botón Siguiente esté ENABLED (no disabled/gris)
    log('  click Siguiente (esperando enabled)')
    btn_clicked = False
    for retry in range(10):
        clicked = page.evaluate(r"""() => {
          const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
          const c = btns.find(b => /^siguiente$/i.test((b.innerText||'').trim()));
          if (c) { c.scrollIntoView({block:'center'}); c.click(); return c.innerText.trim(); }
          return null;
        }""")
        if clicked:
            btn_clicked = True
            log(f'    ✓ click "{clicked}" (retry {retry+1})')
            break
        page.wait_for_timeout(1000)
    if not btn_clicked:
        log('  ❌ botón Siguiente nunca se enabled')
        screenshot_on_error(page, run, 'siguiente_disabled')
        raise RuntimeError('siguiente_disabled — TM no permitió avanzar (campos inválidos)')

    # Esperar networkidle + buscar verify form (extended hasta 60s — TM con proxies lentos
    # tarda más en redireccionar; antes 30s daba timeouts falsos en proxy ES residencial).
    try: page.wait_for_load_state('networkidle', timeout=20000)
    except Exception: pass
    for i in range(40):
        page.wait_for_timeout(1500)
        try:
            ttl = page.title().lower()
            body = page.evaluate('()=>document.body.innerText.slice(0,500).toLowerCase()')
            url_now = page.url.lower()
        except Exception: continue
        # bot_block ANTES de los éxitos heurísticos: la pantalla "Su sesión se ha
        # suspendido" vive en auth.ticketmaster.com y la heurística de URL '/auth'
        # la daba por formulario de verificación (fila 478, 2026-11-07).
        try:
            blk = detect_bot_block(page)
            if blk:
                screenshot_on_error(page, run, 'bot_blocked_post_siguiente')
                raise RuntimeError(f'bot_block_detected: "{blk}" (post-Siguiente)')
        except RuntimeError: raise
        except Exception: pass
        if 'verify' in ttl or 'verifica' in body or 'verificación' in body or 'pasos' in body:
            log(f'  ✓ verify form (t+{(i+1)*1.5:.0f}s)'); return
        # Heurística URL: si redirigió a /verify, /pasos o auth.* → ok
        if any(k in url_now for k in ('/verify', '/pasos', '/auth', '/identify', '/account')):
            log(f'  ✓ verify form via URL (t+{(i+1)*1.5:.0f}s, url contains expected path)'); return
        # Si TM mostró error tras click → log + abort
        if 'error' in body[:200] and 'verifica' not in body:
            try:
                err_msg = page.evaluate(r"""() => {
                  const e = document.querySelector('[role="alert"], .error, [aria-invalid="true"]');
                  return e ? (e.innerText || '').slice(0, 200) : '';
                }""")
                if err_msg: log(f'  ⚠ TM error visible: {err_msg}')
            except Exception: pass
    screenshot_on_error(page, run, 'no_verify_form_after_siguiente')
    raise RuntimeError('no llegó al verify form tras 60s (probable error validación)')

def fill_signin_form(page: Page, user: UserRow):
    log('fill signin form')
    try:
        pw_el = page.locator('input[type="password"]').first
        pw_el.wait_for(state='visible', timeout=10000)
    except Exception:
        # TM en signin pero password input no carga → cuenta semi-creada / TM bug
        # Aborta limpio para que se archive como abandoned por el caller
        raise RuntimeError('signin_password_input_missing — '
                           'cuenta probablemente semi-creada por intentos previos')
    human_type(pw_el, user.password, 20, 40)
    js_click(page, r'iniciar sesi[oó]n|sign in|continuar', exact=False)
    page.wait_for_timeout(3000)

EMAIL_OTP_LOCK_PATH = os.path.join(RUNS_DIR, 'email_otp.lock')
EMAIL_OTP_LOCK_TIMEOUT = 360   # 6 min máximo esperando lock
EMAIL_OTP_LOCK_STALE = 240     # locks > 4 min se consideran abandonados


def _acquire_email_otp_lock() -> bool:
    """Adquiere lock cross-process (file-based) para serializar el paso de email OTP.
    Evita throttling de iCloud HME cuando varios procesos corren en paralelo.
    Returns True if acquired, False on timeout."""
    deadline = time.time() + EMAIL_OTP_LOCK_TIMEOUT
    while time.time() < deadline:
        try:
            # Atomic create — falla si ya existe
            fd = os.open(EMAIL_OTP_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({
                    'pid': os.getpid(),
                    'at': datetime.now(timezone.utc).isoformat(),
                }).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            # Lock existe — comprobar si es stale
            try:
                with open(EMAIL_OTP_LOCK_PATH, encoding='utf-8') as f:
                    info = json.load(f)
                lock_at = datetime.fromisoformat(info['at'])
                age = (datetime.now(timezone.utc) - lock_at).total_seconds()
                if age > EMAIL_OTP_LOCK_STALE:
                    log(f'  🗝 lock stale ({age:.0f}s, pid={info.get("pid")}) — eliminando')
                    try: os.unlink(EMAIL_OTP_LOCK_PATH)
                    except Exception: pass
                    continue
            except Exception:
                # Lock corrupto — eliminar y reintentar
                try: os.unlink(EMAIL_OTP_LOCK_PATH)
                except Exception: pass
                continue
            time.sleep(2)
    return False


def _release_email_otp_lock():
    try:
        if os.path.isfile(EMAIL_OTP_LOCK_PATH):
            os.unlink(EMAIL_OTP_LOCK_PATH)
    except Exception: pass


def _click_verificar_with_lock(page: Page) -> datetime:
    """Lock cross-process serializa el click 'Verificar mi correo' + ESPACIO 12s para
    que iCloud HME digiera el envío antes de que otro proceso pida el suyo.
    iCloud HME aplica throttle si recibe múltiples solicitudes <8s entre sí.
    Polling IMAP queda paralelo después de liberar lock."""
    log('🔒 adquiriendo email_otp lock (12s espacio anti-iCloud-throttle)')
    got_lock = _acquire_email_otp_lock()
    if not got_lock:
        log('  ⚠ timeout lock — continuando sin él')
    else:
        log('  ✓ lock adquirido')
    try:
        log('click "Verificar mi correo"')
        js_click(page, r'verificar mi correo|verify my email', exact=False)
        page.wait_for_timeout(400)
        sent_at = datetime.now(timezone.utc)
        # Espaciado anti-throttle iCloud HME: 6s suficiente entre 2 peticiones.
        page.wait_for_timeout(6000)
    finally:
        if got_lock:
            _release_email_otp_lock()
            log('  🗝 lock liberado tras 6s (otros procs pueden ahora pedir OTP)')
    return sent_at


def email_otp(page: Page, user: UserRow, run: RunState) -> bool:
    # FIX 2026-05-09 (restaurado tras rollback): TM puede tardar hasta 6s en
    # renderizar el botón "Verificar mi correo" tras el click Siguiente. Antes
    # de declarar "no requerido", esperar hasta 6s para que la página termine
    # de cargar. Sin esto, el bot saltaba directo al teléfono dejando el email
    # SIN verificar (cuentas creadas a medias). Memoria: feedback_email_before_phone_order.
    button_seen = False
    for _ in range(12):  # 12 × 0.5s = 6s
        try:
            body = page.evaluate('()=>document.body.innerText.slice(0,800)').lower()
            title = (page.title() or '').lower()
            if 'verifica' in body or 'verify' in title or 'verifica' in title:
                button_seen = True; break
            # También detectar el botón explícitamente
            has_btn = page.evaluate(r"""() => {
              const btns = Array.from(document.querySelectorAll('button, a'));
              return btns.some(b => /verificar mi correo|verify my email/i.test((b.innerText||'').trim()));
            }""")
            if has_btn:
                button_seen = True; break
        except Exception: pass
        page.wait_for_timeout(500)
    if not button_seen:
        log('email-OTP no requerido (tras 6s sin botón ni texto "verifica/verify")'); return True

    sent_at = _click_verificar_with_lock(page)
    first_sent_at = sent_at   # guardar el timestamp del PRIMER click — usaremos en relajación final

    # Detectar bot_block después del click "Verificar mi correo"
    try:
        blk = detect_bot_block(page)
        if blk:
            screenshot_on_error(page, run, 'bot_blocked_post_verificar')
            raise RuntimeError(f'bot_block_detected: "{blk}" (post-verificar)')
    except RuntimeError: raise
    except Exception: pass

    socket.setdefaulttimeout(20)
    seen, otp = set(), None

    def _poll_imap_until(deadline_ts: float, sent_after: datetime) -> Optional[str]:
        """Mantiene UNA conexión IMAP viva durante el ciclo. Filtro EXACTO por destinatario:
        el email del user TIENE que ser uno de los `to:` (no substring) — evita cualquier
        confusión entre alias parecidos en runs paralelos."""
        nonlocal seen
        target_email = user.email.lower().strip()
        try:
            with MailBox('imap.mail.me.com').login(user.contact_email, user.contact_pwd) as mb:
                while time.time() < deadline_ts:
                    try:
                        for m in mb.fetch(reverse=True, limit=8, mark_seen=False, bulk=False):
                            if m.uid in seen: continue
                            seen.add(m.uid)
                            msg_dt = m.date or datetime.now(timezone.utc)
                            msg_dt = (msg_dt.replace(tzinfo=timezone.utc) if msg_dt.tzinfo is None
                                      else msg_dt.astimezone(timezone.utc))
                            if msg_dt < sent_after: continue
                            # MATCH EXACTO en lista de destinatarios — no substring
                            to_emails = [t.lower().strip() for t in (m.to or [])]
                            if target_email not in to_emails: continue
                            fr = (m.from_ or '').lower()
                            sj = (m.subject or '').lower()
                            if 'ticketmaster' not in fr and 'ticketmaster' not in sj: continue
                            g = re.search(r'\b(\d{6})\b', (m.subject or '') + ' ' + (m.text or m.html or '')[:1500])
                            if g:
                                log(f'  📧 OTP match exact: to={target_email} from={fr[:40]}')
                                return g.group(1)
                    except Exception as e:
                        log(f'  imap fetch err (no fatal): {e}')
                    time.sleep(0.7)
        except Exception as e:
            log(f'  imap login err: {e}')
        return None

    # Loop persistente: HASTA 24 ciclos × 30s + re-envío AGRESIVO TRAS CADA ciclo.
    # Total max ~12 min antes de rendir. Ciclos cortos = más reenvíos = más probabilidad
    # de que TM acabe enviando uno (TM a veces ignora silenciosamente el primer envío).
    N_CYCLES = 24
    CICLO_S = 30      # antes 60 — el user pidió aún más rápido
    POST_RESEND_WAIT_MS = 3000   # antes 6000 — iCloud digesta en 3s, acelerar
    for cycle in range(1, N_CYCLES + 1):
        effective_sent_at = first_sent_at  # filtro siempre relajado al primer click
        log(f'  ⏳ polling email OTP ciclo {cycle}/{N_CYCLES} ({CICLO_S}s, since=first_click)')
        otp = _poll_imap_until(time.time() + CICLO_S, effective_sent_at)
        if otp: break
        if cycle < N_CYCLES:
            log(f'  🔁 OTP no llegó en {CICLO_S}s — solicitando nuevo código (lock anti-throttle)')
            got_resend_lock = _acquire_email_otp_lock()
            try:
                # JS unificado: prueba TODOS los selectores de reenvío en 1 round-trip
                # Prioridad:
                #  1. "Solicitar nuevo código" / "Reenviar código" (botón explícito)
                #  2. "Verificar mi correo" / "Verify my email" (re-emite envío)
                #  3. Cualquier botón con texto "reenviar"/"resend"/"de nuevo"
                clicked_text = page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                    const patterns = [
                        /^solicita(r)? un nuevo c[oó]digo$|^solicitar nuevo c[oó]digo$|^reenviar c[oó]digo$|^resend code$|^enviar de nuevo$/i,
                        /solicitar.*nuevo|reenviar|resend|enviar de nuevo/i,
                        /^verificar mi correo$|^verify my email$/i,
                        /verificar.*correo|verify.*email/i,
                    ];
                    for (const pat of patterns) {
                        for (const b of btns) {
                            const t = (b.innerText||b.textContent||'').trim();
                            if (pat.test(t) && !b.disabled) {
                                b.scrollIntoView({block:'center'});
                                b.click();
                                return t;
                            }
                        }
                    }
                    return null;
                }""")
                if clicked_text:
                    log(f'    ✓ click "{clicked_text}"')
                else:
                    log('    ⚠ ningún botón de re-envío encontrado')
                page.wait_for_timeout(POST_RESEND_WAIT_MS)
            finally:
                if got_resend_lock: _release_email_otp_lock()
            sent_at = datetime.now(timezone.utc)

    # Last-chance scan extendido — aceptar OTPs últimos 10 min
    if not otp:
        log('  🔁 last-chance scan IMAP últimos 10 min (sin filtro tiempo)')
        from datetime import timedelta
        otp = _poll_imap_until(time.time() + 60,
                                datetime.now(timezone.utc) - timedelta(minutes=10))

    if not otp:
        log(f'  ❌ email OTP timeout tras {N_CYCLES} ciclos × {CICLO_S}s + last-chance'); return False
    log(f'  📧 OTP={otp}')

    # Buscar input OTP RÁPIDO — el modal suele estar visible cuando llega el OTP
    inp = None
    for retry in range(10):
        inp, sel_used = find_visible_input(page,
            ['input[autocomplete="one-time-code"]',
             'input[inputmode="numeric"]',
             'input[type="tel"]',
             'input[name*="code" i]',
             'input[name*="otp" i]',
             'input[type="text"]'], timeout_ms=500)
        if inp:
            log(f'  ✓ input OTP via {sel_used} (retry {retry+1})')
            break
        page.wait_for_timeout(500)   # antes 2000 — total max 5s en vez de 16s
    if not inp:
        log('  ❌ no input OTP'); screenshot_on_error(page, run, 'no_input_otp')
        return False
    # ULTRAFAST paste — JS unificado en 1 round-trip + click confirmar inline (~30-50ms total)
    paste_t0 = time.time()
    pasted = False
    confirm_clicked = None
    try:
        r = inp.evaluate("""(inp, code) => {
            if (!inp) return {ok:false, reason:'no_input'};
            inp.focus();
            const proto = Object.getPrototypeOf(inp);
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(inp, code);
            else inp.value = code;
            inp.dispatchEvent(new Event('input', {bubbles:true}));
            inp.dispatchEvent(new Event('change', {bubbles:true}));
            // Click confirmar EN EL MISMO frame
            const btns = Array.from(document.querySelectorAll('button'));
            const target = btns.find(b => {
                const t = (b.innerText||'').trim().toLowerCase();
                return /^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/.test(t)
                       && !b.disabled;
            });
            let confirmed = false;
            if (target) { target.click(); confirmed = true; }
            return {ok:true, value:inp.value, confirmed, btn:target?(target.innerText||'').trim():null};
        }""", str(otp))
        if r and r.get('ok') and (r.get('value') or '').strip() == str(otp):
            pasted = True
            log(f'  ⚡ email OTP pegado vía JS @ {(time.time()-paste_t0)*1000:.0f}ms — value="{r["value"]}" confirm_inline={r.get("confirmed")}')
            if r.get('confirmed'):
                confirm_clicked = r.get('btn') or 'Confirmar (inline)'
    except Exception as e:
        log(f'  ⚠ ultrafast JS path err: {e}')

    # Fallback: estrategia robusta (3 sub-estrategias Playwright)
    if not pasted:
        pasted = paste_otp_robust(page, inp, otp, label='email OTP')
    if not pasted:
        log('  ❌ email OTP no se pudo pegar correctamente — abortando')
        screenshot_on_error(page, run, 'email_otp_paste_failed')
        return False

    # Si NO se hizo click inline en JS path, intentar click separado
    if not confirm_clicked:
        page.wait_for_timeout(150)   # antes 300ms
        confirm_clicked = js_click(page, r'confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm', exact=True)
    log(f'  click confirmar email OTP: "{confirm_clicked}"')
    # Si no encontró botón, fallback con Enter en el input
    if not confirm_clicked:
        try:
            inp.press('Enter')
            log('  ✓ fallback: Enter en input OTP')
        except Exception: pass
    # 1ª ESPERA larga (5s) para no re-clickar prematuro mientras TM procesa
    page.wait_for_timeout(5000)

    # SOLO re-click confirmar si aparece error temporal — NO si solo está procesando
    EMAIL_ERR_TEMP = ('error temporal', 'inténtalo de nuevo', 'intentalo de nuevo',
                       'try again', 'algo ha ido mal', 'an error has occurred',
                       'error inesperado')
    for check_retry in range(6):
        try:
            still_otp = page.locator('input[autocomplete="one-time-code"]').first.is_visible(timeout=600)
            body_low = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
        except Exception:
            still_otp = False
            body_low = ''
        if not still_otp:
            log(f'  ✓ email OTP verificado (modal cerrado)')
            _start_prefetch()   # comprar número SMS en background mientras TM habilita el form
            break
        if any(p in body_low for p in EMAIL_ERR_TEMP):
            log(f'  🔁 error temporal email OTP — re-click confirmar')
            js_click(page, r'confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm', exact=True)
            page.wait_for_timeout(3500)
        else:
            log(f'  ⏳ TM procesando email OTP ({(check_retry+1)*2}s) — esperando sin re-click')
            page.wait_for_timeout(2000)

    page.wait_for_timeout(1500)
    run.stage_done('email_otp', {'otp': otp, 'sent_at': sent_at.isoformat()})
    return True

def open_phone_modal(page: Page) -> Optional[object]:
    # Esperar a que el botón "Añadir mi teléfono" esté ENABLED (post-email-OTP).
    # Poll ligero cada 300ms hasta 6s — responde en ms si el elemento ya está listo.
    try:
        page.wait_for_selector('text=/a[ñn]adir mi tel[eé]fono/i', timeout=6000, state='attached')
    except Exception:
        pass
    for _ in range(20):
        clicked = js_click(page, r'a[ñn]adir mi tel[eé]fono', exact=True)
        if clicked:
            log(f'  click "{clicked}"')
            break
        page.wait_for_timeout(300)
    else:
        log('  ⚠ botón "Añadir mi teléfono" no apareció en ~6s')

    # Esperar input teléfono hasta ~6s total (modal abre con animación)
    for _ in range(20):
        try:
            inp, sel = find_visible_input(page, ['input[type="tel"]', 'input[autocomplete*="tel" i]'], 400)
        except Exception:
            inp, sel = None, None
        if inp:
            log(f'  ✓ phone input via {sel}')
            return inp
        page.wait_for_timeout(300)
    return None

def reset_phone_form_after_error(page: Page) -> Optional[object]:
    """Cancel modal OTP del SMS y reabre 'Añadir mi teléfono'."""
    page.evaluate(r"""() => {
      const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
      const cancels = btns.filter(b => /^cancelar$/i.test((b.innerText||'').trim()));
      (cancels[cancels.length-1] || cancels[0])?.click();
    }""")
    page.wait_for_timeout(1500)
    return open_phone_modal(page)

# ─── PREFETCH número SMS en background ───
def _prefetch_buy_number():
    """Hilo daemon: compra el número en background. Nunca tumba el proceso
    principal — captura todo y guarda el resultado en PREFETCHED_NUMBER."""
    try:
        r = sms_receiver.buy_number(country='es', product='ticketmaster')
    except Exception as e:
        log(f'  ⚠ prefetch buy err (no fatal): {str(e)[:120]}')
        r = {'ok': False, 'error': str(e)[:160]}
    with PREFETCHED_NUMBER['lock']:
        discarded = PREFETCHED_NUMBER['discarded']
        if not discarded:
            PREFETCHED_NUMBER['result'] = r
            PREFETCHED_NUMBER['ts'] = time.time()
    # Si mientras comprábamos el proceso pidió descartar el prefetch, cancelar
    # la orden para no quemar saldo del provider.
    if discarded and isinstance(r, dict) and r.get('ok') and r.get('order_id'):
        try:
            c = sms_receiver.cancel_order(r['order_id'], provider=r.get('provider'))
            log(f'  🧹 prefetch tardío cancelado order={r["order_id"]} ok={c.get("ok")}')
        except Exception as e:
            log(f'  ⚠ cancel prefetch tardío err: {e}')


def _start_prefetch():
    with PREFETCHED_NUMBER['lock']:
        PREFETCHED_NUMBER['result'] = None
        PREFETCHED_NUMBER['ts'] = time.time()
        PREFETCHED_NUMBER['discarded'] = False
    t = threading.Thread(target=_prefetch_buy_number, daemon=True, name='sms-prefetch')
    t.start()
    log('  🚀 prefetch número SMS lanzado en background (post email OTP)')


def _take_prefetched(max_wait_s: int = 30) -> Optional[dict]:
    """Espera al prefetch hasta max_wait_s y devuelve el resultado si es fresco."""
    t0 = time.time()
    while time.time() - t0 < max_wait_s:
        with PREFETCHED_NUMBER['lock']:
            r = PREFETCHED_NUMBER['result']
            ts = PREFETCHED_NUMBER['ts']
        if r is not None:
            if time.time() - ts <= PREFETCH_MAX_AGE_S:
                _discard_prefetched()
                return r
            _discard_prefetched()
            return None
        time.sleep(0.25)
    return None   # aún no terminó: no bloquear más allá de max_wait_s


def _discard_prefetched():
    """Marca el holder como consumido/descartado: el hilo no sobreescribirá."""
    with PREFETCHED_NUMBER['lock']:
        PREFETCHED_NUMBER['result'] = None
        PREFETCHED_NUMBER['discarded'] = True


def _cancel_prefetched(reason: str):
    """Cancela la orden prefetchada si no se usó (fin de intento/fallback)."""
    with PREFETCHED_NUMBER['lock']:
        r = PREFETCHED_NUMBER['result']
        PREFETCHED_NUMBER['result'] = None
        PREFETCHED_NUMBER['discarded'] = True
    if isinstance(r, dict) and r.get('ok') and r.get('order_id'):
        try:
            c = sms_receiver.cancel_order(r['order_id'], provider=r.get('provider'))
            log(f'  🧹 prefetch cancelado ({reason}) order={r["order_id"]} ok={c.get("ok")}')
        except Exception as e:
            log(f'  ⚠ cancel prefetch err: {e}')


def buy_phone_skip_blocked(blocked: set, run: RunState) -> tuple[Optional[str], Optional[str], Optional[str]]:
    # Consumir prefetch del número (lanzado en background tras email OTP)
    pf = _take_prefetched(max_wait_s=30)
    if pf is not None:
        if pf.get('ok'):
            ph = pf.get('phone')
            prov = pf.get('provider') or '5sim'
            if ph not in blocked:
                log(f'  ✓ número prefetcheado {ph} ({prov}) — sin espera de buy')
                return ph, pf.get('order_id'), prov
            log(f'  ⚠ prefetch {ph} está blocked → cancel + loop normal')
            try: sms_receiver.cancel_order(pf['order_id'], provider=prov)
            except Exception: pass
        else:
            log(f'  ⚠ prefetch falló ({str(pf.get("error"))[:100]}) — buy normal')
    blocked_skips = 0
    for k in range(BUY_NO_STOCK_RETRIES):
        r = sms_receiver.buy_number(country='es', product='ticketmaster')
        if r.get('ok'):
            ph = r.get('phone')
            prov = r.get('provider') or '5sim'
            if ph in blocked:
                # El número devuelto ya dio problemas antes: cancelarlo y pedir OTRO.
                # No descuenta de BUY_NO_STOCK_RETRIES — no es fallo de stock.
                blocked_skips += 1
                log(f'  🚫 provider devolvió número bloqueado {ph} → cancel + pedir otro (skip #{blocked_skips})')
                try: sms_receiver.cancel_order(r['order_id'])
                except Exception: pass
                if blocked_skips >= 15:
                    log('  ⚠ demasiados números bloqueados seguidos — abortando buy (pool contaminado)')
                    break
                time.sleep(2); continue
            return ph, r.get('order_id'), prov
        if k % 3 == 0:
            err = (r.get('error') or
                   (r.get('raw', {}) or {}).get('raw') if isinstance(r.get('raw'), dict) else r.get('raw') or
                   r.get('msg') or '?')
            err = str(err)[:160]
            prov = r.get('provider') or 'n/a'
            log(f'  buy retry {k+1}/{BUY_NO_STOCK_RETRIES} via {prov}: {err}')
        # Rate-limit SMSPool: "Please wait 5 minutes" / "too many failed requests".
        # Abortar el ciclo rápido — martillear solo empeora el rate-limit.
        blob = (str(r.get('error') or '') + ' ' +
                str(r.get('raw') if isinstance(r.get('raw'), dict) else (r.get('raw') or '')) + ' ' +
                str(r.get('msg') or ''))
        if re.search(r'please wait \d+ minutes|too many failed requests', blob, re.I):
            log('  🚫 rate-limit SMSPool detectado — abortando ciclo de buy (no martillar)')
            break
        time.sleep(min(BUY_BACKOFF_S * (k + 1), 20))
    return None, None, None

def _drain_pending_orders_recheck(reason: str = 'cleanup'):
    """Cancela cualquier orden SMS que haya quedado en PENDING_ORDERS_RECHECK
    sin verificar. Idempotente — repetir cancel sobre orden ya refunded es seguro."""
    drained = 0
    while PENDING_ORDERS_RECHECK:
        prev = PENDING_ORDERS_RECHECK.pop(0)
        oid = prev.get('order')
        prov = prev.get('provider')
        if not oid: continue
        try:
            r = sms_receiver.cancel_order(oid, provider=prov)
            drained += 1
            log(f'  drain[{reason}] cancel {prov} order={oid} ok={r.get("ok")} '
                f'verified={r.get("verified_status","?")}')
        except Exception as e:
            log(f'  drain[{reason}] error oid={oid}: {e}')
    return drained


def phone_relentless(page: Page, user: UserRow, phone_inp, run: RunState) -> Optional[dict]:
    blocked = load_blocked_phones()
    try:
        return _phone_relentless_inner(page, user, phone_inp, run, blocked)
    finally:
        try: _cancel_prefetched('phone_relentless_exit')
        except Exception as _e: log(f'  cancel prefetch exception: {_e}')
        try: _drain_pending_orders_recheck('phone_relentless_exit')
        except Exception as _e: log(f'  drain exception: {_e}')


def _phone_relentless_inner(page: Page, user: UserRow, phone_inp, run: RunState, blocked: set) -> Optional[dict]:
    for cycle in range(1, MAX_PHONE_CYCLES + 1):
        log(f'cycle phone {cycle}/{MAX_PHONE_CYCLES}')

        # PRIMERO comprar (puede tardar 4 min si "no free phones")
        phone, order, provider = buy_phone_skip_blocked(blocked, run)
        if not phone: log('  stock SMS provider agotado'); break
        digits = phone.lstrip('+')
        if digits.startswith('34'): digits = digits[2:]
        log(f'  buy ok {phone} order={order} via {provider}')
        run.phone_attempts.append({'cycle': cycle, 'phone': phone, 'order_id': str(order),
                                    'provider': provider,
                                    'started_at': datetime.now(timezone.utc).isoformat()})
        run.persist()

        # AHORA RE-BUSCAR el input phone — el reference puede estar stale tras minutos esperando stock
        fresh_inp = None
        for retry in range(5):
            fresh_inp, sel_used = find_visible_input(page,
                ['input[type="tel"]', 'input[autocomplete*="tel" i]', 'input[name*="phone" i]'],
                timeout_ms=1500)
            if fresh_inp:
                log(f'  ✓ phone input fresh via {sel_used} (retry {retry+1})')
                break
            log(f'  ⏳ phone input no visible, intento reset modal...')
            fresh_inp = reset_phone_form_after_error(page)
            if fresh_inp:
                log(f'  ✓ phone input restaurado tras reset modal')
                break
            page.wait_for_timeout(2000)
        if not fresh_inp:
            log('  ❌ no input phone tras buy — cancelando orden')
            sms_receiver.cancel_order(order); blocked.add(phone); save_blocked_phones(blocked); continue
        phone_inp = fresh_inp

        # FAST paste con fill() — dispara eventos React. Validación tolera auto-formato TM
        # (TM añade prefix "+34" y espacios entre grupos: "636443875" → "+34 636 44 38 75")
        try:
            phone_inp.click()
            phone_inp.fill('')
            phone_inp.fill(digits)
            page.wait_for_timeout(200)
            actual_value = ''
            try: actual_value = phone_inp.input_value()
            except Exception: pass
            # Comparación tolerante: ambos solo dígitos, ignorando prefix +34
            actual_digits = ''.join(c for c in actual_value if c.isdigit())
            if actual_digits.endswith(digits) or actual_digits == digits:
                log(f'  ✓ digits {digits} ingresados (value="{actual_value}") — fast fill OK')
            else:
                log(f'  ⚠ value mismatched: "{actual_value}" no contiene {digits} — fallback char-by-char')
                phone_inp.fill('')
                for ch in digits: phone_inp.type(ch, delay=random.randint(15,30))
                page.wait_for_timeout(200)
                actual_value = phone_inp.input_value() if phone_inp else ''
                log(f'  ✓ fallback OK (value="{actual_value}")')
        except Exception as e:
            log(f'  ❌ tipeo phone falló: {e}')
            sms_receiver.cancel_order(order); blocked.add(phone); save_blocked_phones(blocked); continue

        sent_at = datetime.now(timezone.utc)
        clicked = js_click(page, r'a[ñn]ade n[uú]mero|a[ñn]ade tu n[uú]mero', blue_only=False, exact=False)
        sent_at = datetime.now(timezone.utc)
        log(f'  click "{clicked}" @ {sent_at.strftime("%H:%M:%S")}')
        if not clicked:
            sms_receiver.cancel_order(order); blocked.add(phone); save_blocked_phones(blocked); continue

        # Helper: extrae código SMS con priorización por contexto Ticketmaster.
        # Filtra falsos positivos (números de teléfono, fechas, IDs spam).
        def _extract_tm_code(txt: str):
            t = (txt or '').lower()
            if not t: return None
            # 1) Patrón directo "tu código TM es 123456" — más fiable
            for pat in (
                r'(?:c[oó]digo|code|verification|verificaci[oó]n)[\s\S]{0,30}?(\d{4,8})',
                r'(\d{4,8})[\s\S]{0,30}?(?:ticketmaster|tu c[oó]digo|tm)',
                r'\bticketmaster\b[\s\S]{0,30}?(\d{4,8})',
            ):
                m = re.search(pat, t)
                if m and 4 <= len(m.group(1)) <= 8: return m.group(1)
            # 2) Si solo hay UN número 4-8 dígitos, asumirlo como código (fallback)
            nums = re.findall(r'\b(\d{4,8})\b', t)
            # Filtrar números muy largos que parezcan teléfono (≥9 dígitos)
            cands = [n for n in nums if 4 <= len(n) <= 8]
            if len(cands) == 1: return cands[0]
            if len(cands) >= 2: return cands[0]   # primero típicamente
            return None

        # poll SMS — 0.5s interval (antes 0.7s) para detectar SMS apenas llega
        sms_code, sms_text = None, None
        deadline = time.time() + SMS_TIMEOUT_S
        # Re-check ROBUSTO: además de la orden actual, verificar las anteriores aún vivas.
        # SMS provider a veces entrega 60-180s tarde; recapturar en background evita perderlo.
        # Ventana extendida 5 min (antes 3 min) por casos TM Spain lentos.
        all_orders_to_check = [order]
        for prev in list(PENDING_ORDERS_RECHECK):
            if (time.time() - prev.get('ts', 0)) < 300:   # 5 min antes 3 min
                all_orders_to_check.append(prev['order'])
            else:
                PENDING_ORDERS_RECHECK.remove(prev)
        sms_resent = False
        _rescued_orders = set()
        next_resend_probe = time.time() + 20
        next_resend_reprobe = None  # 2º probe ~30s tras el reenvío por si el click no registró
        # Polling adaptativo: base 2.0s ± 0.5s jitter. Si la API devuelve
        # rate-limit, duplicar intervalo (cap 8s) hasta el próximo éxito. Con
        # 4 procesos en paralelo, polling a 0.5s ≈ 8-16 req/s activa el
        # rate-limit de los providers y la compra del número tarda minutos.
        poll_interval = 2.0
        poll_round = 0
        while time.time() < deadline and not sms_code:
            # TM a veces no entrega el primer SMS. Su enlace "puedes pedir uno
            # nuevo" solo se activa al acabar una cuenta atrás (~120 s): sondear
            # cada 5 s, pulsarlo una vez y dar SMS_RESEND_EXTRA_S más al reenvío.
            if not sms_resent and time.time() >= next_resend_probe:
                next_resend_probe = time.time() + 5
                try:
                    hit = page.evaluate(SMS_RESEND_JS)
                    if hit:
                        sms_resent = True
                        deadline = max(deadline, time.time() + SMS_RESEND_EXTRA_S)
                        next_resend_reprobe = time.time() + 30
                        log(f'  🔁 SMS no llegó → reenvío pedido ("{hit}"), +{SMS_RESEND_EXTRA_S}s')
                except Exception as e:
                    log(f'  ⚠ reenvío SMS err (no fatal): {str(e)[:80]}')
            elif sms_resent and next_resend_reprobe and time.time() >= next_resend_reprobe:
                # El primer click pudo no haber registrado (sin feedback visual):
                # si el enlace sigue visible a los ~30s, pulsarlo otra vez.
                next_resend_reprobe = None
                try:
                    hit2 = page.evaluate(SMS_RESEND_JS)
                    if hit2:
                        deadline = max(deadline, time.time() + SMS_RESEND_EXTRA_S)
                        log(f'  🔁 2º probe reenvío — enlace aún visible, re-click ("{hit2}")')
                except Exception as e:
                    log(f'  ⚠ 2º probe reenvío err (no fatal): {str(e)[:80]}')
            poll_round += 1
            # Orden actual CADA ronda; órdenes anteriores (PENDING_ORDERS_RECHECK)
            # solo 1 de cada 10 rondas (~cada 20s) para no multiplicar req/s.
            if poll_round % 10 == 1:
                orders_this_round = all_orders_to_check
            else:
                orders_this_round = [order]
            for ord_to_check in orders_this_round:
                try:
                    chk = sms_receiver.check_sms(ord_to_check)
                    # Backoff adaptativo: rate-limit detectado (ok=False o texto
                    # "wait"/"too many"/"retry later") → duplicar intervalo (cap 8s);
                    # éxito limpio de la orden actual → reset a base 2.0s.
                    chk_blob = (str(chk.get('error') or '') + ' ' +
                                str(chk.get('raw') if isinstance(chk.get('raw'), dict) else (chk.get('raw') or '')) + ' ' +
                                str(chk.get('msg') or ''))
                    if not chk.get('ok') or re.search(r'wait|too many|retry later', chk_blob, re.I):
                        poll_interval = min(poll_interval * 2, 8.0)
                    elif ord_to_check == order:
                        poll_interval = 2.0
                    # Rescate: orden anterior que la API ya no reconoce (ok=False) —
                    # intentar finish_order una vez para cerrarla limpio en vez de
                    # dejarla pudrirse hasta el drain.
                    if (not chk.get('ok') and ord_to_check != order
                            and ord_to_check not in _rescued_orders):
                        prev = next((p for p in PENDING_ORDERS_RECHECK
                                     if p.get('order') == ord_to_check), None)
                        if prev and prev.get('provider') in ('5sim', 'smsactivate'):
                            _rescued_orders.add(ord_to_check)
                            try:
                                fin = sms_receiver.finish_order(ord_to_check, provider=prev.get('provider'))
                                log(f'  🔧 rescue finish_order {ord_to_check} ok={fin.get("ok")}')
                            except Exception as fe:
                                log(f'  ⚠ rescue finish err: {fe}')
                    for s in (chk.get('sms') or []):
                        txt = s.get('text') or s.get('code') or ''
                        sms_text = txt
                        c = _extract_tm_code(txt)
                        if c:
                            if ord_to_check != order:
                                # El número de la orden anterior debe COINCIDIR con el
                                # que acabamos de meter en el modal — si no, el SMS
                                # pertenece a otro flujo y no vale para este número.
                                prev = next((p for p in PENDING_ORDERS_RECHECK
                                             if p.get('order') == ord_to_check), None)
                                if prev and prev.get('phone') != phone:
                                    log(f'  ⚠ SMS de orden {ord_to_check} llegó tarde pero phone '
                                        f'{prev.get("phone")} != {phone} actual — descartado')
                                    continue
                                log(f'  📱 SMS llegó tarde a orden anterior {ord_to_check} — reusar código')
                            sms_code = c
                            break
                except Exception as e:
                    log(f'  sms check err (no fatal): {e}')
                if sms_code: break
            if sms_code: break
            time.sleep(max(0.2, poll_interval + random.uniform(-0.5, 0.5)))

        if not sms_code:
            # ÚLTIMO check justo antes de cancelar — el SMS puede llegar en el
            # instante del timeout y cancelar encima lo mataría.
            try:
                chk = sms_receiver.check_sms(order)
                for s in (chk.get('sms') or []):
                    c = _extract_tm_code(s.get('text') or s.get('code') or '')
                    if c:
                        sms_code = c
                        sms_text = s.get('text') or s.get('code') or ''
                        log(f'  📱 SMS={sms_code} capturado en último check pre-cancel')
                        break
            except Exception as e:
                log(f'  ⚠ último check pre-cancel err (no fatal): {e}')

        if not sms_code:
            log(f'  ⏰ SMS timeout {SMS_TIMEOUT_S}s — cancelando {provider} order={order}')
            # Para SMSPool especialmente: cancelar SIEMPRE al timeout para liberar el número
            # y que la API marque refunded. Si llega SMS tarde, el rechecker leerá y
            # SMSPool rechazará el cancel posterior — no nos importa.
            try:
                cres = sms_receiver.cancel_order(order, provider=provider)
                if cres.get('ok'):
                    log(f'    ✓ cancel ok ({provider}) verified={cres.get("verified_status","?")}')
                else:
                    log(f'    ⚠ cancel falló: {cres.get("friendly_error") or cres.get("error")}')
                    # Rescate: si check_sms confirma que NO hay nada y la orden quedó
                    # colgada, intentar finish_order como cierre limpio (5sim/smsactivate).
                    if provider in ('5sim', 'smsactivate'):
                        try:
                            chk2 = sms_receiver.check_sms(order)
                            if not chk2.get('ok'):
                                fin = sms_receiver.finish_order(order, provider=provider)
                                log(f'    🔧 rescue finish_order ok={fin.get("ok")}')
                        except Exception as fe:
                            log(f'    ⚠ rescue finish_order err: {fe}')
            except Exception as e:
                log(f'    ⚠ cancel exception: {e}')
            # Guardar para recheck por si llega SMS tarde antes de que SMSPool refunde.
            # SÍ bloqueamos el número: si un número no recibió el SMS en 180s, no se
            # reutiliza — el siguiente ciclo compra uno DIFERENTE (política del usuario:
            # número problemático → cancelar y otro nuevo). El recheck sigue vivo por
            # si el SMS llega tarde a esta orden cancelada.
            blocked.add(phone); save_blocked_phones(blocked)
            log(f'  🚫 número {phone} bloqueado (timeout SMS {SMS_TIMEOUT_S}s sin recibir) — próximo ciclo usará otro')
            PENDING_ORDERS_RECHECK.append({'order': order, 'phone': phone,
                                           'ts': time.time(), 'provider': provider})
            continue
        log(f'  📱 SMS={sms_code} ({(datetime.now(timezone.utc)-sent_at).total_seconds():.1f}s)')

        # Buscar input SMS — modal puede tardar 5-15s en renderizar tras SMS recibido.
        # OPTIMIZADO: poll RÁPIDO al input SMS (200ms timeout × 60 retries × 100ms gap).
        # Total max ~18s pero responde en <300ms si el input ya está visible al llegar SMS.
        # Antes: 12 × 1500ms = 18s, pero cada retry 1.5s incluso si visible inmediatamente.
        code_inp = None
        sel_used = None
        sms_t0 = time.time()
        for retry in range(60):
            code_inp, sel_used = find_visible_input(page,
                ['input[autocomplete="one-time-code"]',
                 'input[inputmode="numeric"]',
                 'input[type="tel"]',
                 'input[name*="code" i]',
                 'input[name*="otp" i]',
                 'input[type="text"]'], timeout_ms=200)
            if code_inp:
                log(f'  ✓ input SMS @ {(time.time()-sms_t0)*1000:.0f}ms via {sel_used} (retry {retry+1})')
                break
            page.wait_for_timeout(100)   # antes 800ms — más responsivo
        if not code_inp:
            log(f'  ❌ no input SMS code tras 18s — SMS perdido, cancel + retry')
            screenshot_on_error(page, run, f'no_input_sms_cycle{cycle}')
            # Aunque el SMS llegó, la experiencia del usuario es que este número dio
            # problemas → cancelar y que el próximo ciclo use uno DIFERENTE.
            sms_receiver.cancel_order(order); blocked.add(phone); save_blocked_phones(blocked)
            log(f'  🚫 número {phone} bloqueado (modal código no apareció) — próximo ciclo usará otro')
            continue

        # ULTRAFAST paste — JS directo en 1 round-trip + click confirm JS en mismo frame
        # Antes: ~155ms (click + fill + 80ms wait + verify).
        # Ahora: ~30-50ms (1 evaluate dispara value+input+change+click confirm).
        paste_t0 = time.time()
        pasted = False
        try:
            # Estrategia 0: JS UNIFICADO — set value + dispatch events + click confirm
            # en una sola llamada page.evaluate (1 round-trip a CDP).
            # React/Vue lee el value del fiber tras el input event, así que basta con
            # nativeInputValueSetter para forzar synthetic events React.
            r = code_inp.evaluate("""(inp, code) => {
                if (!inp) return {ok:false, reason:'no_input'};
                inp.focus();
                // React strict mode: usar el setter nativo para que React detecte el cambio
                const proto = Object.getPrototypeOf(inp);
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(inp, code);
                else inp.value = code;
                inp.dispatchEvent(new Event('input', {bubbles:true}));
                inp.dispatchEvent(new Event('change', {bubbles:true}));
                // Click confirmar SIN esperar — botón debería estar en el mismo modal
                let confirmed = false;
                const btns = Array.from(document.querySelectorAll('button'));
                const targets = btns.filter(b => {
                    const t = (b.innerText||'').trim().toLowerCase();
                    return /^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/.test(t);
                });
                if (targets.length && !targets[0].disabled) {
                    targets[0].click();
                    confirmed = true;
                }
                return {ok:true, value:inp.value, confirmed};
            }""", str(sms_code))

            if r and r.get('ok') and (r.get('value') or '').strip() == sms_code:
                pasted = True
                log(f'  ⚡ SMS pegado vía JS @ {(time.time()-paste_t0)*1000:.0f}ms — value="{r["value"]}" confirm_inline={r.get("confirmed")}')
                if r.get('confirmed'):
                    # Skip el click confirmar separado más abajo
                    confirm_clicked = "Confirmar código (inline)"
        except Exception as e:
            log(f'  ⚠ ultrafast JS path err: {e} — fallback Playwright')

        # Fallback 1: fill() directo Playwright (era el path principal)
        if not pasted:
            try:
                code_inp.click()
                code_inp.fill('')
                code_inp.fill(str(sms_code))
                page.wait_for_timeout(50)
                actual = (code_inp.input_value() or '').strip()
                if actual == sms_code or ''.join(c for c in actual if c.isdigit()) == sms_code:
                    pasted = True
                    log(f'  ✓ SMS pegado vía fill @ {(time.time()-paste_t0)*1000:.0f}ms — value="{actual}"')
            except Exception: pass

        # Fallback 2: estrategia robusta (más lenta, press_sequentially + char-by-char)
        if not pasted:
            pasted = paste_otp_robust(page, code_inp, sms_code, label='SMS code')
        if not pasted:
            log('  ❌ SMS code no se pudo pegar — cancel + retry cycle')
            screenshot_on_error(page, run, f'sms_paste_failed_cycle{cycle}')
            sms_receiver.cancel_order(order); blocked.add(phone); save_blocked_phones(blocked); continue

        # Click confirmar (solo si NO se hizo inline en JS path)
        try:
            _ = confirm_clicked  # vino del JS path?
        except NameError:
            confirm_clicked = None
        if not confirm_clicked:
            confirm_clicked = js_click(page, r'confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm', exact=True)
        log(f'  click confirmar SMS: "{confirm_clicked}"')
        if not confirm_clicked:
            try:
                code_inp.press('Enter')
                log('  ✓ fallback: Enter en input SMS')
            except Exception: pass
        elapsed = (datetime.now(timezone.utc) - sent_at).total_seconds()
        log(f'  ✓ confirmar inicial @ {elapsed:.1f}s totales')

        # ── VERIFICACIÓN POST-CONFIRMAR ──
        # NO re-click si solo está procesando (modal visible sin error) — TM puede tardar.
        # SOLO re-click si aparece "error temporal" / "inténtalo de nuevo" tras 3-5s.
        ERROR_TEMP_PATTERNS = ('error temporal', 'temporal error', 'inténtalo de nuevo',
                                'intentalo de nuevo', 'try again', 'algo ha ido mal',
                                'something went wrong', 'error al validar',
                                'an error has occurred', 'error inesperado')
        REJECT_PATTERNS = ('alcanzado el límite', 'reached the limit',
                           'código no es válido', 'code is not valid',
                           'code you entered is not valid', 'no es válido el código')
        post_confirm_state = 'unknown'
        # 1ª ESPERA: 5s para que TM procese (no re-click prematuro)
        page.wait_for_timeout(5000)
        for check_retry in range(8):
            try:
                body_low = page.evaluate('()=>document.body.innerText.slice(0,800)').lower()
                still_modal = code_inp.is_visible(timeout=600)
            except Exception:
                body_low = ''
                still_modal = False

            # ÉXITO: modal cerró, sin error
            if not still_modal and not any(p in body_low for p in ERROR_TEMP_PATTERNS + REJECT_PATTERNS):
                log(f'  ✓ SMS OTP verificado y modal cerrado')
                post_confirm_state = 'success'; break

            # REJECT definitivo → blacklist
            if any(p in body_low for p in REJECT_PATTERNS):
                log(f'  ❌ TM rechazó código/número (rejection)')
                post_confirm_state = 'rejected'; break

            # ERROR temporal → re-click UNA VEZ confirmar
            if any(p in body_low for p in ERROR_TEMP_PATTERNS):
                log(f'  🔁 error temporal — re-click confirmar')
                js_click(page, r'confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm', exact=True)
                page.wait_for_timeout(4000)
                continue

            # MODAL aún visible sin error → solo esperar más (TM procesa lento, NO re-clickar)
            log(f'  ⏳ TM aún procesando ({(check_retry+1)*2}s) — esperando sin re-click')
            page.wait_for_timeout(2000)

        if post_confirm_state == 'rejected':
            sms_receiver.finish_order(order)
            blocked.add(phone); save_blocked_phones(blocked)
            try:
                page.evaluate(r"""() => {
                  const btns = Array.from(document.querySelectorAll('button')).filter(b=>!b.disabled);
                  const cancels = btns.filter(b => /^cancelar$/i.test((b.innerText||'').trim()));
                  (cancels[cancels.length-1] || cancels[0])?.click();
                }""")
                page.wait_for_timeout(1500)
            except Exception: pass
            continue

        page.wait_for_timeout(1500)

        body = page.evaluate('()=>document.body.innerText.slice(0,800)').lower()
        rejected_reasons = [
            ('error al validar', 'TM rechazó validación'),
            ('an error has occurred', 'error genérico TM'),
            ('error inesperado', 'error inesperado TM'),
            ('alcanzado el límite de intentos de verificación', 'límite de intentos del número alcanzado'),
            ('alcanzado el limite de intentos de verificacion', 'límite intentos (sin acentos)'),
            ('reached the verification attempt limit', 'verification attempt limit reached'),
            ('número de teléfono ha alcanzado', 'phone alcanzó límite'),
            ('numero de telefono ha alcanzado', 'phone alcanzó límite (sin acentos)'),
            ('reached the limit of', 'limit reached'),
            ('contacte', 'TM pide contacto soporte'),
        ]
        rejected_reason = None
        for trigger, reason_label in rejected_reasons:
            if trigger in body:
                rejected_reason = reason_label; break
        if rejected_reason:
            log(f'  ❌ TM rechazó {phone}: {rejected_reason} → blacklist + retry con OTRO número')
            sms_receiver.finish_order(order)
            blocked.add(phone); save_blocked_phones(blocked)
            # Reset modal del OTP del SMS para volver al form de teléfono
            try:
                page.evaluate(r"""() => {
                  const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.disabled);
                  const cancels = btns.filter(b => /^cancelar$/i.test((b.innerText||'').trim()));
                  (cancels[cancels.length-1] || cancels[0])?.click();
                }""")
                page.wait_for_timeout(1500)
            except Exception: pass
            continue

        sms_receiver.finish_order(order)
        result = {'phone': phone, 'phone_digits': digits,
                  'provider': provider, 'order_id': str(order),
                  'sms_text': sms_text, 'sms_code': sms_code,
                  'cycle': cycle, 'click_to_confirm_s': elapsed,
                  'verified_at': datetime.now(timezone.utc).isoformat()}
        run.stage_done('phone_verified', result)
        return result
    return None

def click_global_identity_consent(page: Page) -> bool:
    """Detecta y acepta el panel "Una nueva forma de iniciar sesión / Manténte al día"
    que TM muestra a veces (no siempre) entre signup y profile. Si está visible, click
    'Aceptar'. Devuelve True si lo cazó y clickó."""
    try:
        body = page.evaluate('()=>document.body.innerText.slice(0,1500)').lower()
    except Exception:
        return False
    triggers = (
        'una nueva forma de iniciar sesión',
        'una nueva forma de iniciar sesion',
        'identidad global',
        'manténte al día',
        'mantente al dia',
        'si continúas más allá de esta página',
        'si continuas mas alla de esta pagina',
        'puedes usar una sola cuenta y contraseña',
        'preventas exclusivas y ofertas',
    )
    if any(t in body for t in triggers):
        clicked = js_click(page, r'^aceptar$|^acepto$|^accept$|^continuar$|^ok$', exact=True)
        if clicked:
            log(f'  ✓ panel identidad global detectado — click "{clicked}"')
            try: page.wait_for_timeout(1500)
            except Exception: pass
            return True
        # Fallback no-exact si TM cambió el botón
        clicked = js_click(page, r'aceptar|acepto|accept|continuar', exact=False)
        if clicked:
            log(f'  ✓ panel identidad global (fallback) — click "{clicked}"')
            try: page.wait_for_timeout(1500)
            except Exception: pass
            return True
    return False


def finalize_terms_and_passkey(page: Page, run: RunState):
    """Último paso: click 'Aceptar' / 'Confirmar la cuenta' (TOC) → espera Profile →
    skip passkey → verifica persistencia. ESTE ES EL CLICK FINAL DEL REGISTRO."""
    # Defensa: panel "identidad global / manténte al día" puede aparecer ANTES del TOC
    try: click_global_identity_consent(page)
    except Exception: pass

    # Esperar HASTA 15s a que TM muestre la pantalla TOC ("Confirma tu cuenta")
    log('  esperando pantalla TOC final...')
    has_toc = False
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            body = page.evaluate('()=>document.body.innerText.slice(0,800)').lower()
        except Exception:
            body = ''
        has_toc = ('confirma tu cuenta' in body or 'confirmar tu cuenta' in body
                   or 'confirmar la cuenta' in body or 'confirmar cuenta' in body
                   or 'al tocar' in body or 'al pulsar' in body
                   or 'una nueva forma de iniciar sesión' in body
                   or 'una nueva forma de iniciar sesion' in body
                   or 'manténte al día' in body
                   or 'mantente al dia' in body
                   or 'identidad global' in body)
        if has_toc: break
        page.wait_for_timeout(800)
    if has_toc:
        log('  ✓ pantalla TOC visible')
        # Regex priorizado: primero "Confirmar la cuenta", luego "Aceptar"/"Acepto"
        clicked = js_click(page,
            r'confirmar la cuenta|confirmar cuenta|aceptar|acepto|accept|^confirmar$',
            exact=True)
        log(f'  ✓ click FINAL "{clicked}" (TOC / confirmar la cuenta)')

        log('  esperando ventana de cuenta confirmada...')
        confirmed = False
        deadline = time.time() + 15   # antes 30s — half time
        while time.time() < deadline:
            try:
                ttl = page.title().lower()
                url = page.url.lower()
                body2 = page.evaluate('()=>document.body.innerText.slice(0,400)').lower()
                if ('profile' in ttl
                    or '/settings' in url
                    or 'my.ticketmaster.es' in url
                    or 'mi perfil' in body2
                    or 'llave de acceso' in body2
                    or 'passkey' in body2
                    or 'añade una llave' in body2):
                    confirmed = True
                    log(f'  ✓ cuenta confirmada (title="{page.title()}" url={page.url[:80]})')
                    break
            except Exception: pass
            page.wait_for_timeout(700)   # antes 1000ms
        if not confirmed:
            log('  ⚠ no detecté ventana de confirmación tras 15s — sigo igual')

    body = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
    if 'llave de acceso' in body or 'passkey' in body or 'añade un' in body:
        clicked = js_click(page, r'ahora mismo no|ahora no|not now|skip|m[aá]s tarde', exact=False)
        if clicked: log(f'  click "{clicked}" en passkey')
        try: page.wait_for_load_state('networkidle', timeout=4000)
        except Exception: pass
        page.wait_for_timeout(1500)

    # Verificación final persistencia. Markers de éxito:
    #   A) URL llega a /settings, /account, /home/account o profile en title
    #   B) URL llega a auth.ticketmaster.com/as/authorization.oauth2 con
    #      title "Verify Your Information" — TM redirige aquí post-creación
    #      como pantalla legítima OAuth landing (cuenta YA creada).
    #   C) URL contiene /home/, /es/customer/, /pf-ws/ — flow continuó tras success.
    log('  verificación final cuenta persistida...')
    persisted = False
    deadline = time.time() + 6  # antes 12s — flow ya success en este punto
    while time.time() < deadline:
        try:
            url = page.url.lower()
            ttl = page.title().lower()
            body3 = page.evaluate('()=>document.body.innerText.slice(0,500)').lower()
            # Marker A: Settings/Profile clásico
            if (('my.ticketmaster.es/settings' in url or
                 '/account' in url or
                 'profile' in ttl) and
                ('mi perfil' in body3 or
                 'actualizar' in body3 or
                 'detalles del perfil' in body3 or
                 'profile details' in body3 or
                 'tu cuenta' in body3)):
                persisted = True
                log(f'  ✅ cuenta persistida (url={url[:80]}, title={page.title()[:60]})')
                break
            # Marker B: OAuth landing post-creación (verify your information)
            if (('auth.ticketmaster' in url and
                 ('as/authorization' in url or 'authorization.oauth2' in url)) and
                ('verify your information' in ttl or
                 'verifica tu información' in ttl or
                 'verifica' in ttl)):
                persisted = True
                log(f'  ✅ cuenta persistida vía OAuth landing (url={url[:80]}, title={page.title()[:60]})')
                break
            # Marker C: paths post-success genéricos
            if any(p in url for p in ('/home/', '/es/customer', '/pf-ws/', '/identity/')):
                persisted = True
                log(f'  ✅ cuenta persistida vía path post-success (url={url[:80]})')
                break
        except Exception: pass
        page.wait_for_timeout(800)
    if not persisted:
        log(f'  ⚠ no detecté persistencia clara — url={page.url[:100]} title={page.title()[:60]}')
        page.wait_for_timeout(800)

    run.stage_done('terms_passkey', {'url': page.url, 'title': page.title(),
                                      'persisted': persisted})

def logout_account(page: Page, run: RunState):
    """Logout fast-path — fire-and-forget. La cuenta YA está creada y persistida.
    El logout es nice-to-have para limpiar cookies, pero no debe bloquear el
    cierre del browser. Skip waits largos."""
    log('logout TM (fire-and-forget)')
    try:
        # Intentar click rápido sin esperar networkidle
        try:
            page.evaluate(r"""() => {
              const btns = Array.from(document.querySelectorAll('button,a')).filter(b => !b.disabled);
              const cand = btns.find(b => /cerrar sesi[oó]n|sign out|log out|logout/i.test((b.innerText||'').trim()));
              if (cand) cand.click();
            }""")
        except Exception: pass
        # Goto /logout con timeout corto, sin esperar networkidle
        try:
            page.goto('https://my.ticketmaster.es/logout',
                      wait_until='commit', timeout=5000)
        except Exception: pass
        run.stage_done('logout', {'url': page.url})
    except Exception as e:
        log(f'  ⚠ logout err (no fatal): {str(e)[:60]}')
        run.stage_done('logout', {'error': str(e)[:60]})

def save_artifacts(ctx, run: RunState) -> dict:
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    cookies_file = os.path.join(COOKIES_DIR, f'tm_morelogin_u{run.row_idx}_{ts}.json')
    state_file = os.path.join(SESSIONS_DIR, f'tm_u{run.row_idx}_{ts}_state.json')

    cookies = ctx.cookies()
    with open(cookies_file, 'w', encoding='utf-8') as f:
        json.dump({'profile': run.profile, 'env_id': run.env_id, 'tm_email': run.email,
                   'saved_at': datetime.now().isoformat(),
                   'cookies': cookies}, f, indent=2, ensure_ascii=False)
    log(f'  cookies → {cookies_file} ({len(cookies)} cookies)')

    try:
        ctx.storage_state(path=state_file)
        log(f'  storage_state → {state_file}')
    except Exception as e:
        log(f'  storage_state err: {e}')

    artifacts = {'cookies_file': cookies_file, 'state_file': state_file,
                 'cookies_count': len(cookies)}
    run.stage_done('artifacts_saved', artifacts)
    return artifacts

def close_profile(run: RunState):
    # Cierre inmediato — sin pausa. Pool manager replenish en <1s.
    log('cleanup + close perfil MoreLogin')
    res = bo._close_profile_clean(run.env_id, run.port, run.profile)
    log(f'  {res}')
    bo._release_profile(run.profile)
    # ANTI-COLLISION: liberar entry en active_starts.json para que otro proceso
    # pueda usar este env_id si se reabre (2026-05-10 bug fix).
    try: _release_active_start(run.env_id)
    except Exception: pass

    # NORMA 2026-05-13: destruir perfil + proxy si fue auto-creado, para no
    # acumular en MoreLogin (limite 20000) y garantizar IP fresh próximo run.
    if getattr(run, 'auto_created', False):
        try:
            import time as _time
            _time.sleep(2)  # esperar a que close termine antes de destruir
            destroy_prof = {
                'profile': run.profile,
                'env_id': run.env_id,
                'proxy_id': run.proxy_id,
                'sessionid': run.sessionid,
                'auto_created': True,
            }
            destroy_res = bo.destroy_fresh_profile(destroy_prof)
            log(f'  destroy: {destroy_res}')
            res['destroy'] = destroy_res
        except Exception as _de:
            log(f'  destroy_err: {str(_de)[:120]}')
            res['destroy_err'] = str(_de)[:120]
    run.stage_done('profile_closed', res)

def record_in_tm_registrations(user: UserRow, account_id: str, run: RunState) -> dict:
    """Crea/actualiza item en tm_registrations.json con status=done para que el
    contador de la pestaña 'Registros TM' del dashboard refleje el éxito."""
    import tm_registrations as tr
    import uuid
    with tr._lock:
        d = tr._load()
        items = d.setdefault('items', [])
        em_low = user.email.lower()
        existing = next((it for it in items if (it.get('email') or '').lower() == em_low), None)
        ts = datetime.now().isoformat()
        if existing:
            existing.update({
                'status': tr.STATUS_DONE,
                'completed_at': ts,
                'last_attempt_at': ts,
                'mode_used': 'register_account.py',
                'account_id': account_id,
            })
            existing.setdefault('history', []).append(
                {'at': ts, 'event': 'registered', 'run_id': run.run_id})
            tr._save(d)
            return {'ok': True, 'updated': True, 'item_id': existing['id']}
        item = {
            'id': uuid.uuid4().hex[:10],
            'email': user.email, 'password': user.password,
            'first_name': user.first, 'last_name': user.last,
            'country': 'es', 'phone': user.phone_local,
            'dob': user.dob, 'zip': user.postal,
            'address': user.address1, 'city': user.city,
            'proxy': '',
            'status': tr.STATUS_DONE,
            'created_at': ts, 'attempts': 1,
            'last_error': '', 'last_attempt_at': ts,
            'completed_at': ts,
            'tm_url': tr.TM_REGISTER_URLS.get('es', ''),
            'mode_used': 'register_account.py',
            'account_id': account_id,
            'history': [{'at': ts, 'event': 'registered', 'run_id': run.run_id}],
        }
        items.append(item)
        tr._save(d)
        return {'ok': True, 'created': True, 'item_id': item['id']}


def _record_problem_in_tm_registrations(email: str, problem_status: str, reason: str, run: RunState):
    """Crea/actualiza item en tm_registrations.json con status custom (abandoned/flaky).
    Reintentos en escritura para tolerar contención cross-process (Windows file lock)."""
    try:
        import tm_registrations as tr
        import uuid as _uuid
        ts = datetime.now().isoformat()
        last_err = None
        for retry in range(8):
            try:
                with tr._lock:
                    d = tr._load()
                    items = d.setdefault('items', [])
                    em_low = email.lower()
                    existing = next((it for it in items if (it.get('email') or '').lower() == em_low), None)
                    if existing:
                        existing['status'] = problem_status
                        existing['last_error'] = reason[:200]
                        existing['last_attempt_at'] = ts
                        existing.setdefault('history', []).append(
                            {'at': ts, 'event': problem_status, 'reason': reason[:200],
                             'run_id': run.run_id})
                    else:
                        items.append({
                            'id': _uuid.uuid4().hex[:10],
                            'email': email, 'password': '', 'first_name': '', 'last_name': '',
                            'country': 'es', 'phone': '', 'dob': '', 'zip': '',
                            'address': '', 'city': '', 'proxy': '',
                            'status': problem_status,
                            'created_at': ts, 'attempts': 1,
                            'last_error': reason[:200], 'last_attempt_at': ts,
                            'completed_at': None,
                            'tm_url': tr.TM_REGISTER_URLS.get('es', ''),
                            'mode_used': 'register_account.py',
                            'history': [{'at': ts, 'event': problem_status, 'reason': reason[:200],
                                         'run_id': run.run_id}],
                        })
                    tr._save(d)
                return  # éxito
            except Exception as e:
                last_err = e
                if 'WinError 32' in str(e) or 'being used by another process' in str(e).lower() or 'permission' in str(e).lower():
                    time.sleep(0.5 + random.random() * 0.5)
                    continue
                raise
        log(f'  ⚠ no pude registrar {problem_status} en tm_registrations tras 8 retries: {last_err}')
    except Exception as e:
        log(f'  ⚠ no pude registrar {problem_status} en tm_registrations: {e}')


def _archive_abandoned(email: str, run: RunState, reason: str):
    """Archiva la fila como abandoned (con lock cross-process)."""
    def _do():
        import csv as _csv, shutil as _sh
        abandoned_path = os.path.join(RUNS_DIR, 'abandoned_archive.csv')
        if not os.path.isfile(PLANTILLA): return
        _sh.copy2(PLANTILLA, PLANTILLA + '.bak.preabandon')
        with open(PLANTILLA, 'r', encoding='utf-8-sig', newline='') as f:
            reader = _csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            kept, archived = [], None
            for row in reader:
                if (row.get('Email_TM') or '').strip().lower() == email.lower():
                    archived = dict(row); continue
                kept.append(row)
        if not archived: return
        if fieldnames and fieldnames[0] == 'N_Usuario':
            for i, r in enumerate(kept, 1): r['N_Usuario'] = str(i)
        tmp = PLANTILLA + f'.tmp.{os.getpid()}'
        with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
            w = _csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(kept)
        os.replace(tmp, PLANTILLA)
        archived['_abandoned_at'] = datetime.now(timezone.utc).isoformat()
        archived['_reason'] = reason[:200]
        archived['_run_id'] = run.run_id
        write_header = not os.path.isfile(abandoned_path) or os.path.getsize(abandoned_path) == 0
        with open(abandoned_path, 'a', encoding='utf-8-sig', newline='') as f:
            w = _csv.DictWriter(f, fieldnames=list(archived.keys()))
            if write_header: w.writeheader()
            w.writerow(archived)
    _with_plantilla_lock(_do)
    _record_problem_in_tm_registrations(email, 'abandoned', reason, run)


def remove_from_plantilla(email: str, run: RunState) -> dict:
    """Elimina la fila de plantilla_tm.csv → registered_archive.csv (con lock)."""
    if not os.path.isfile(PLANTILLA):
        log(f'  ⚠ plantilla no existe: {PLANTILLA}')
        return {'ok': False, 'error': 'plantilla_missing'}

    archive_path = os.path.join(RUNS_DIR, 'registered_archive.csv')
    result_holder = {'archived_row': None, 'kept': [], 'fieldnames': []}

    def _do():
        import shutil
        try: shutil.copy2(PLANTILLA, PLANTILLA + '.bak.preregister')
        except Exception: pass
        with open(PLANTILLA, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            kept = []
            archived_row = None
            for row in reader:
                if (row.get('Email_TM') or '').strip().lower() == email.lower():
                    archived_row = dict(row); continue
                kept.append(row)
        if archived_row is None: return None
        if fieldnames and fieldnames[0] == 'N_Usuario':
            for idx, row in enumerate(kept, start=1): row['N_Usuario'] = str(idx)
        tmp = PLANTILLA + f'.tmp.{os.getpid()}'
        with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(kept)
        os.replace(tmp, PLANTILLA)
        result_holder['archived_row'] = archived_row
        result_holder['kept'] = kept
        return True

    _with_plantilla_lock(_do)

    if result_holder['archived_row'] is None:
        log(f'  ⚠ {email} no estaba en la plantilla (¿ya movido?)')
        return {'ok': False, 'error': 'not_in_plantilla'}

    archived_row = result_holder['archived_row']
    kept = result_holder['kept']

    # Append a archive con timestamp
    archived_row['_registered_at'] = datetime.now(timezone.utc).isoformat()
    archived_row['_run_id'] = run.run_id
    # Bug fix 2026-05-11: los headers del archive y de la plantilla pueden
    # divergir en ORDEN (plantilla tiene Seleccionado en [1], archive en [24]).
    # csv.DictWriter en modo append escribe por orden de `fieldnames`, así que
    # si pasamos las keys de archived_row, las columnas se desplazan respecto
    # a los headers existentes → Email_TM acaba con valor "1" (de Seleccionado)
    # y verify_account falla con archive_ADDED=False.
    # Fix: leer los headers EXISTENTES del archive y usarlos.
    write_header = not os.path.isfile(archive_path)
    archive_fields = None
    if not write_header:
        try:
            with open(archive_path, 'r', encoding='utf-8-sig', newline='') as f:
                rdr = csv.reader(f)
                archive_fields = next(rdr, None)
        except Exception:
            archive_fields = None
    if not archive_fields:
        # Archive nuevo o ilegible: usar plantilla fields + columnas extras
        archive_fields = list(archived_row.keys())
    else:
        # Garantizar que las nuevas claves (_registered_at, _run_id) están
        for k in ('_registered_at', '_run_id'):
            if k not in archive_fields:
                archive_fields.append(k)
    # Filtrar archived_row para que solo contenga las claves del archive
    # (extrañas extra van fuera, faltantes quedan vacías).
    safe_row = {k: archived_row.get(k, '') for k in archive_fields}
    with open(archive_path, 'a', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=archive_fields,
                           extrasaction='ignore')
        if write_header: w.writeheader()
        w.writerow(safe_row)

    log(f'  ✓ removed from plantilla, archived → {archive_path} (filas restantes: {len(kept)})')
    run.stage_done('plantilla_moved', {'archive_path': archive_path,
                                       'remaining_rows': len(kept)})
    return {'ok': True, 'archive_path': archive_path,
            'remaining_rows': len(kept)}


def register_in_dashboard(user: UserRow, phone_info: dict, run: RunState,
                          artifacts: dict) -> dict:
    label = f"U{user.n} · {user.first} {user.last} · {user.email}"
    acc = am.account_manager.add_account({
        'label': label,
        'tm_email': user.email, 'tm_password': user.password,
        'first_name': user.first, 'last_name': user.last,
        'dob': user.dob, 'nationality': user.nationality, 'passport': user.passport,
        'phone_prefix': user.phone_prefix, 'phone': phone_info['phone_digits'],
        'phone_full': phone_info['phone'], 'phone_provider': phone_info['provider'],
        'phone_order_id': phone_info['order_id'],
        'phone_sms_code': phone_info['sms_code'],
        'phone_verified_at': phone_info['verified_at'],
        'address1': user.address1, 'city': user.city, 'state': user.state,
        'postal': user.postal, 'country': user.country,
        'email': user.contact_email, 'email_password': user.contact_pwd,
        'morelogin_profile': run.profile, 'morelogin_env_id': run.env_id,
        'cookies_file': artifacts['cookies_file'],
        'storage_state_file': artifacts['state_file'],
        'notes': f'Registrada {datetime.now().strftime("%Y-%m-%d %H:%M")} en perfil {run.profile}.',
        'status': 'active',
        'purchase_ready': True,
        'registration_completed_at': datetime.now(timezone.utc).isoformat(),
        'source': f'plantilla_tm.csv row {user.n}',
    })
    log(f'  ✓ account_id={acc["id"]}')

    card = am.account_manager.add_card({
        'account_id': acc['id'],
        'label': f'Tarjeta principal U{user.n}',
        'holder': user.card_holder, 'number': user.card_number,
        'exp_month': user.card_month, 'exp_year': user.card_year,
        'cvv': user.card_cvv, 'last4': user.card_number[-4:],
        'brand': 'visa' if user.card_number.startswith('4') else 'mastercard',
    })
    log(f'  ✓ card_id={card["id"]} ****{card.get("last4")}')

    sync = accounts_csv_sync.sync_out()
    log(f'  cuentas_tarjetas.csv: {sync}')

    # verify
    visible = False
    try:
        s = requests.Session()
        s.post('http://127.0.0.1:5055/login',
               data={'username': DASHBOARD_LOGIN[0], 'password': DASHBOARD_LOGIN[1]},
               timeout=8, allow_redirects=False)
        api = s.get('http://127.0.0.1:5055/api/accounts/with-purchases', timeout=8).json()
        visible = any(a.get('id') == acc['id'] for a in (api.get('accounts') or []))
    except Exception as e:
        log(f'  dashboard verify err: {e}')
    log(f'  visible en dashboard: {"✅" if visible else "⚠ no"}')

    info = {'account_id': acc['id'], 'card_id': card['id'],
            'visible_in_dashboard': visible}
    run.stage_done('dashboard_registered', info)
    return info

# ─────────────────── pre-flight checks ───────────────────
def email_already_registered(email: str) -> bool:
    """True si la cuenta ya está en account_manager (evita gastar IP+SMS)."""
    em = email.lower()
    return any((a.get('tm_email') or '').lower() == em
               for a in am.account_manager.list_accounts())

def imap_health_check(madre: str, pwd: str, max_retries: int = 3) -> bool:
    """Valida que el IMAP madre responde antes de gastar recursos en signup.

    iCloud IMAP a veces tiene timeouts transitorios — reintenta hasta 3 veces
    con backoff exponencial (3s, 6s, 12s) antes de rendirse.
    """
    for attempt in range(1, max_retries + 1):
        try:
            socket.setdefaulttimeout(30)   # antes 15s — iCloud a veces tarda 20s
            with MailBox('imap.mail.me.com').login(madre, pwd):
                if attempt > 1:
                    log(f'  ✓ IMAP madre OK tras retry {attempt}')
                return True
        except Exception as e:
            err_str = str(e)
            is_transient = any(t in err_str.lower() for t in
                                ('timed out', 'timeout', 'eof',
                                 'connectionreset', 'broken pipe',
                                 'temporarily unavailable'))
            if attempt < max_retries and is_transient:
                wait_s = 3 * (2 ** (attempt - 1))   # 3s, 6s, 12s
                log(f'  ⚠ IMAP health fail (retry {attempt}/{max_retries}): {err_str[:80]} — esperando {wait_s}s')
                time.sleep(wait_s)
                continue
            log(f'  ⚠ IMAP health check fail tras {attempt} intentos: {err_str[:120]}', level='ERROR')
            return False
    return False

USED_IPS_PATH = os.path.join(RUNS_DIR, 'used_ips.json')

def load_used_ips() -> dict:
    """Histórico {ip: {first_used, last_used, runs:[run_id]}}."""
    try:
        with open(USED_IPS_PATH, encoding='utf-8') as f: return json.load(f)
    except Exception: return {}

def save_used_ip(ip: str, run_id: str):
    d = load_used_ips()
    now = datetime.now(timezone.utc).isoformat()
    if ip in d:
        d[ip]['last_used'] = now
        d[ip].setdefault('runs', []).append(run_id)
    else:
        d[ip] = {'first_used': now, 'last_used': now, 'runs': [run_id]}
    with open(USED_IPS_PATH, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=2)

def _verify_proxy_http(proxy_user: str, proxy_pass: str,
                        proxy_host: str = 'residential.quantumproxies.net',
                        proxy_port: int = 8888, proxy_type: int = 0) -> Optional[str]:
    """Verifica que el proxy funciona haciendo GET https://api.ipify.org via requests.
    NO toca Playwright/MoreLogin. Devuelve IP o None."""
    if not (proxy_user and proxy_pass): return None
    url = _proxy_url(proxy_user, proxy_pass, proxy_host, proxy_port, proxy_type)
    try:
        r = requests.get('https://api.ipify.org/?format=json',
                         proxies={'http': url, 'https': url}, timeout=15)
        return r.json().get('ip') if r.status_code == 200 else None
    except Exception as e:
        if 'whitelist' in str(e).lower() or '422' in str(e):
            _warn_whitelist_once(proxy_host)
        return None


def _proxy_url(user: str, pwd: str, host: str, port, proxy_type: int = 0) -> str:
    """URL de proxy con credenciales codificadas (Froxy usa ';' en la contraseña).
    proxy_type MoreLogin: 0=http, 2=socks5 (socks5h: DNS resuelto por el proxy)."""
    from urllib.parse import quote
    scheme = 'socks5h' if int(proxy_type or 0) == 2 else 'http'
    return f'{scheme}://{quote(user, safe="")}:{quote(pwd, safe="")}@{host}:{port}'


USED_PROXY_IDS_PATH = os.path.join(RUNS_DIR, 'used_proxy_ids.json')
BURNED_SUBNETS_PATH = os.path.join(RUNS_DIR, 'burned_subnets24.json')


def _burn_subnet24(ip: str, run_id: str) -> None:
    """Marca la /24 de la IP como quemada por bot_block (burned_subnets24.json)."""
    parts = (ip or '').split('.')
    if len(parts) != 4:
        return
    key = '.'.join(parts[:3]) + '.0/24'
    now = datetime.now(timezone.utc).isoformat()
    try:
        try:
            with open(BURNED_SUBNETS_PATH, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        prev = data.get(key) or {'first_burned': now, 'ips': [], 'runs': []}
        entry = {
            **prev,
            'last_burned': now,
            'reason': 'bot_block_register',
            'ips': sorted(set(prev.get('ips', [])) | {ip}),
            'runs': (prev.get('runs', []) + [run_id])[-20:],
        }
        tmp = f'{BURNED_SUBNETS_PATH}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({**data, key: entry}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, BURNED_SUBNETS_PATH)
        log(f'  🚫 subred {key} quemada por bot_block')
    except Exception as e:
        log(f'  ⚠ no se pudo quemar la /24 {key}: {type(e).__name__}: {e}')


def is_subnet24_burned(ip: str) -> bool:
    """True si la /24 de la IP está en burned_subnets24.json (quemada por bot_block)."""
    parts = (ip or '').split('.')
    if len(parts) != 4:
        return False
    try:
        with open(BURNED_SUBNETS_PATH, encoding='utf-8') as f:
            return '.'.join(parts[:3]) + '.0/24' in json.load(f)
    except Exception:
        return False


def _load_used_proxy_ids() -> set:
    try:
        with open(USED_PROXY_IDS_PATH, encoding='utf-8') as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_used_proxy_ids(ids: set) -> None:
    with open(USED_PROXY_IDS_PATH, 'w', encoding='utf-8') as f:
        json.dump(sorted(ids), f, indent=2)


SILENT_PROXY_HOST = os.environ.get('TM_SILENT_PROXY_HOST', 'lemonclub').strip().lower()
KETCH_DISMISS_JS = r"""() => {
  const root = document.querySelector('#lanyard_root, [id*=ketch i]');
  if (!root) return 'none';
  const btns = Array.from(root.querySelectorAll('button'));
  const find = re => btns.find(b => re.test((b.innerText || '').trim()));
  const reject = find(/^(rechazar todo|rechazar|reject all)$/i);
  if (reject) reject.click();
  const confirm = find(/^(confirmar|confirm|guardar|save)$/i);
  if (confirm) confirm.click();
  return reject ? 'rejected' : (confirm ? 'confirmed' : 'present');
}"""


def _dismiss_ketch_consent(page):
    """Cierra el gestor de consentimiento Ketch de TM (rechaza lo no esencial).
    Si sigue tapando el formulario tras los clics, se retira del DOM."""
    try:
        res = page.evaluate(KETCH_DISMISS_JS)
        if res == 'none':
            return
        page.wait_for_timeout(1200)
        if page.evaluate(KETCH_DISMISS_JS) != 'none':
            page.evaluate("() => document.querySelectorAll('#lanyard_root, [id*=ketch i]').forEach(e => e.remove())")
        log(f'  ✓ consentimiento Ketch cerrado ({res})')
    except Exception as e:
        log(f'  ⚠ consentimiento Ketch: {type(e).__name__}: {str(e)[:80]}')


# Motor del navegador en Silent: 'brave' (por defecto), 'chromium' (Chromium
# gestionado más reciente), 'chrome', 'edge', 'opera' o un engineId 'eng_...'.
SILENT_ENGINE = os.environ.get('TM_SILENT_ENGINE', 'brave').strip().lower()
# Operadores cuyas IPs TM bloquea siempre ("Su sesión se ha suspendido"):
# AS200845 Avatel — 3 de 3 perfiles bloqueados el 2026-11-07.
SILENT_BLOCKED_ASNS = tuple(a.strip().upper() for a in
                            os.environ.get('TM_BLOCKED_ASNS', 'AS200845').split(',') if a.strip())


def _version_key(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or '')[:4])


def _silent_engine_id(se):
    """Resuelve SILENT_ENGINE a un engineId de Silent (None = el de por defecto)."""
    if SILENT_ENGINE.startswith('eng_'):
        return SILENT_ENGINE
    engines = se._get('/v1/engines') or []
    if SILENT_ENGINE == 'chromium':
        cands = [e for e in engines if e.get('kind') == 'managed-chromium']
    else:
        cands = [e for e in engines if (e.get('brand') or '').lower() == SILENT_ENGINE]
    if not cands:
        log(f'  ⚠ motor Silent "{SILENT_ENGINE}" no disponible; uso el de por defecto')
        return None
    best = max(cands, key=lambda e: _version_key(e.get('version')))
    log(f'  motor Silent: {best.get("name")} (v{best.get("version")})')
    return best['id']


class _SkipPreCheck(Exception):
    """Señal interna: omitir el pre-check HTTP de open_profile."""


# Pantalla de espera de TM ("Casi terminado. No actualices la página...") en los
# idiomas que puede servir según la huella del navegador.
WAITING_ROOM_MARKERS = ('casi terminado', 'no actualices', 'quasi finito', 'non aggiornare',
                        'almost there', 'do not refresh', "don't refresh", 'presque terminé',
                        'fast geschafft', 'nicht aktualisieren')


def _wait_waiting_room(page, max_s: int = 180) -> None:
    """Si TM muestra la pantalla de espera, esperar a que pase (sin recargar)."""
    t0 = time.time()
    seen = False
    while time.time() - t0 < max_s:
        try:
            txt = (page.evaluate('() => document.body ? document.body.innerText : ""') or '').lower()
        except Exception:
            txt = ''
        if not any(m in txt for m in WAITING_ROOM_MARKERS):
            if seen:
                log(f'  ✓ pantalla de espera superada en {int(time.time() - t0)}s')
            return
        if not seen:
            log('  ⏳ TM muestra pantalla de espera — esperando sin recargar')
            seen = True
        page.wait_for_timeout(3000)
    log(f'  ⚠ pantalla de espera sigue tras {max_s}s')


SPAIN_LOCALE = {'languages': ['es-ES', 'es', 'en'], 'timezone': 'Europe/Madrid',
                'lat': 40.4168, 'lon': -3.7038}


def _silent_set_spain_locale(se, env_id: str) -> None:
    """Silent asigna idioma/zona/geo propios (p. ej. it-IT/Europe/Rome). Con IP
    española eso es incoherente: se fija es-ES + Europe/Madrid + geo Madrid."""
    fp = se._get(f'/v1/profiles/{env_id}').get('fingerprint') or {}
    fp['languages'] = SPAIN_LOCALE['languages']
    fp['timezone'] = SPAIN_LOCALE['timezone']
    geo = fp.get('geo') or {}
    geo.update({'latitude': SPAIN_LOCALE['lat'] + random.uniform(-0.05, 0.05),
                'longitude': SPAIN_LOCALE['lon'] + random.uniform(-0.05, 0.05)})
    fp['geo'] = geo
    r = requests.patch(f'{se.SILENT_API}/v1/profiles/{env_id}', headers=se._headers(),
                       json={'fingerprint': fp}, timeout=20)
    r.raise_for_status()


def _silent_browser_ip(port: int) -> Optional[dict]:
    """IP/país de salida vistos DESDE el navegador del perfil (Silent no expone
    las credenciales del proxy, así que no se puede probar con requests)."""
    from playwright.sync_api import sync_playwright
    v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=10).json()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto('https://ipinfo.io/json', timeout=30000)
        txt = page.evaluate('() => document.body.innerText')
        # Dejar la pestaña en blanco: Brave restaura las pestañas al reabrir el
        # perfil y la de ipinfo acababa en ctx.pages[0] durante el registro.
        try:
            page.goto('about:blank', timeout=5000)
        except Exception:
            pass
        b.close()
    return json.loads(txt[txt.find('{'):txt.rfind('}') + 1])


def _create_silent_profile(country: str = 'ES', max_tries: int = 6) -> Optional[dict]:
    """Perfil Silent NUEVO por registro, con un proxy (SILENT_PROXY_HOST) no usado.
    Se abre una vez para comprobar desde el navegador que sale por el país pedido;
    el proxy queda marcado como usado (una IP por cuenta) aunque se descarte."""
    se = bo
    used_ids = _load_used_proxy_ids()
    used_ips = load_used_ips()
    cands = [p for p in se._get('/v1/proxies')
             if SILENT_PROXY_HOST in (p.get('host') or '').lower() and p.get('id') not in used_ids]
    random.shuffle(cands)
    log(f'  proxies "{SILENT_PROXY_HOST}" sin usar en Silent: {len(cands)}')
    engine_id = _silent_engine_id(se)
    for px in cands[:max_tries]:
        used_ids.add(px['id'])
        _save_used_proxy_ids(used_ids)
        name = f'TM-{country.upper()}-{random.randint(100000, 999999)}'
        env_id = None
        info = None
        try:
            body = {'name': name, 'proxyId': px['id']}
            if engine_id:
                body['engineId'] = engine_id
            env_id = se._post('/v1/profiles', body)['id']
            _silent_set_spain_locale(se, env_id)
            port = se._start_profile(env_id)
            info = _silent_browser_ip(port) if port else None
        except Exception as e:
            log(f'  ⚠ Silent perfil/proxy {px["id"]} err: {str(e)[:120]}')
        finally:
            if env_id:
                try: se._post(f'/v1/profiles/{env_id}/close', timeout=30)
                except Exception: pass
        ip = (info or {}).get('ip')
        cc = (info or {}).get('country')
        org = (info or {}).get('org') or ''
        bad_asn = next((a for a in SILENT_BLOCKED_ASNS if org.upper().startswith(a)), None)
        if (not env_id or cc != country.upper() or not ip or ip in used_ips
                or is_subnet24_burned(ip) or bad_asn):
            log(f'  · {name} descartado (ip={ip} pais={cc}' + (f' asn={bad_asn} vetado' if bad_asn else '') + ')')
            continue
        with se._pick_lock:
            se._active_profiles.add(name)
        log(f'  + perfil Silent {name} proxy={px["id"]} ip={ip} ({(info or {}).get("org", "")})')
        return {'profile': name, 'env_id': env_id, 'ip': ip, 'proxy_id': px['id'],
                'verified': True}
    log('  ⚠ ningún proxy Silent válido tras los intentos')
    return None


def _create_profile_on_unused_proxy(host_filter: str, country: str = 'ES') -> Optional[dict]:
    """Crea un perfil MoreLogin nuevo con un proxy del proveedor `host_filter` que aún
    no se haya usado. Un proxy = una cuenta (se marca en used_proxy_ids.json).
    El proxy NO se borra al terminar (es del usuario); solo se marca como usado."""
    bo = _load_engine()
    api = bo.MORELOGIN_API
    used_ids = _load_used_proxy_ids()
    used_ips = load_used_ips()
    for page_no in range(1, 40):
        try:
            d = requests.post(f'{api}/api/proxyInfo/page',
                              json={'pageNo': page_no, 'pageSize': 200}, timeout=30).json()
        except Exception:
            break
        items = (d.get('data') or {}).get('dataList') or []
        if not items:
            break
        for x in items:
            if host_filter not in (x.get('proxyIp') or '').lower():
                continue
            pid = str(x.get('id') or '')
            if not pid or pid in used_ids:
                continue
            real_ip = _verify_proxy_http(x.get('username') or '', x.get('password') or '',
                                         proxy_host=x.get('proxyIp'),
                                         proxy_port=int(x.get('proxyPort') or 0),
                                         proxy_type=int(x.get('proxyType') or 0))
            used_ids.add(pid)
            _save_used_proxy_ids(used_ids)   # consumido aunque falle: no reintentar uno roto
            if not real_ip or real_ip in used_ips or is_subnet24_burned(real_ip):
                log(f'  · proxy {x.get("proxyIp")}:{x.get("proxyPort")} descartado (ip={real_ip})')
                continue
            env_name = f'FX-{country.upper()}-{x.get("proxyPort")}-{random.randint(1000, 9999)}'
            try:
                r = requests.post(f'{api}/api/env/create/advanced', json={
                    'browserTypeId': 1, 'operatorSystemId': 1, 'envName': env_name,
                    'proxyId': pid, 'groupId': 0, 'uaVersion': 0}, timeout=20).json()
            except Exception as e:
                log(f'  ⚠ create env err: {e}')
                continue
            env_id = r.get('data') if r.get('code') == 0 else None
            if not env_id:
                log(f'  ⚠ create env falló: {r}')
                continue
            with bo._pick_lock:
                bo._active_profiles.add(env_name)
            log(f'  + perfil {env_name} creado con proxy {x.get("proxyIp")}:{x.get("proxyPort")} ip={real_ip}')
            return {'profile': env_name, 'env_id': env_id, 'ip': real_ip,
                    'proxy': x, 'proxy_id': pid, 'verified': True}
    log(f'  ⚠ no quedan proxies "{host_filter}" sin usar')
    return None


_WHITELIST_WARNED = set()


def _warn_whitelist_once(host: str) -> None:
    if host in _WHITELIST_WARNED:
        return
    _WHITELIST_WARNED.add(host)
    try:
        my_ip = requests.get('https://api.ipify.org', timeout=8).text.strip()
    except Exception:
        my_ip = '?'
    log(f'  ⚠ proxy {host} rechaza la conexión (422 IP no en whitelist). '
        f'Añade la IP pública de este PC ({my_ip}) a la whitelist del proveedor.', level='WARN')

def screenshot_on_error(page, run: RunState, label: str):
    """Captura pantalla de diagnóstico al fallar."""
    try:
        os.makedirs(SHOTS_DIAG := os.path.join(RUNS_DIR, 'screenshots'), exist_ok=True)
        path = os.path.join(SHOTS_DIAG, f'{run.run_id}_{label}.png')
        page.screenshot(path=path, full_page=False)
        log(f'  📸 diag screenshot → {path}')
    except Exception: pass

# ─────────────────── orchestrator ───────────────────
# ────── Flaky tracking + clasificación de errores ──────
FLAKY_COUNTS_PATH = os.path.join(RUNS_DIR, 'flaky_counts.json')
PLANTILLA_LOCK_PATH = os.path.join(RUNS_DIR, 'plantilla.lock')
EMAIL_CLAIMS_PATH = os.path.join(RUNS_DIR, 'email_claims.json')


def _load_email_claims() -> dict:
    """Devuelve {email: timestamp} de claims activos. Limpia stales (>30 min) y los
    borra del disco — un registro real <10 min, así que stale >30 min es huérfano seguro
    de un proceso que murió con kill -9 sin liberar."""
    try:
        with open(EMAIL_CLAIMS_PATH, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return {}
    now = time.time()
    fresh = {e: ts for e, ts in d.items() if now - ts < 1800}  # 30 min
    # Si purgamos algún stale, persistir el cambio
    if len(fresh) != len(d):
        try:
            with open(EMAIL_CLAIMS_PATH, 'w', encoding='utf-8') as f:
                json.dump(fresh, f, indent=2)
        except Exception: pass
    return fresh


def _save_email_claims(d: dict):
    with open(EMAIL_CLAIMS_PATH, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=2)


def _claim_email_atomic(row_idx: int) -> Optional[dict]:
    """Bajo lock cross-process: lee plantilla, encuentra fila no claimed/registered/flaky,
    marca email como claimed. Devuelve dict con la fila o None si no hay disponibles.
    Si row_idx específico está libre, lo usa; si no, busca siguiente disponible."""
    result = {'row': None}
    def _do():
        with open(PLANTILLA, encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        if not rows: return
        claims = _load_email_claims()
        registered = {(a.get('tm_email') or '').lower()
                      for a in am.account_manager.list_accounts()}
        # Buscar desde row_idx en adelante; si no encuentra, desde 1
        order = list(range(row_idx - 1, len(rows))) + list(range(0, row_idx - 1))
        for idx in order:
            r = rows[idx]
            em = (r.get('Email_TM') or '').strip().lower()
            if not em: continue
            if em in claims: continue
            if em in registered: continue
            # Skip flaky already at threshold
            if get_flaky_count(em) >= FLAKY_THRESHOLD: continue
            # Claim it
            claims[em] = time.time()
            _save_email_claims(claims)
            result['row'] = (idx + 1, r)  # 1-based row index
            return
    _with_plantilla_lock(_do)
    return result['row']  # tuple (row_idx, csv_row) or None


def _release_email_claim(email: str):
    """Quita el claim del email (al terminar el run, sea éxito o fallo)."""
    try:
        claims = _load_email_claims()
        claims.pop(email.lower(), None)
        _save_email_claims(claims)
    except Exception: pass


def _with_plantilla_lock(fn, *args, **kwargs):
    """Ejecuta fn con lock cross-process sobre plantilla_tm.csv. Espera hasta 60s."""
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            fd = os.open(PLANTILLA_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            try:
                return fn(*args, **kwargs)
            finally:
                try: os.unlink(PLANTILLA_LOCK_PATH)
                except Exception: pass
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(PLANTILLA_LOCK_PATH)
                if age > 30:
                    try: os.unlink(PLANTILLA_LOCK_PATH)
                    except Exception: pass
            except Exception: pass
            time.sleep(0.5)
    log('  ⚠ plantilla lock timeout — proceeding sin lock (riesgo race)')
    return fn(*args, **kwargs)
FLAKY_THRESHOLD = 3              # tras 3 fallos consecutivos → flaky_archive
INNER_RETRY_MAX = 3              # reintentos dentro del mismo run con perfiles distintos

# Errores que indican TM bloqueó silenciosamente / proxy malo / browser flaky.
# NO archivar como abandoned, son transient.
_TRANSIENT_PATTERNS = (
    'no llegó al verify form',
    'page.evaluate', 'page.wait_for', 'execution context was destroyed',
    'target page, context or browser has been closed',
    'browser has been closed',
    'bot_block_detected',
    'email otp timeout',
    'no input otp', 'no input phone', 'no abrió form teléfono',
    'email input nunca apareció',
    'phone failed tras',
    'timeout',
    'no se pudo arrancar',
    'no hay ningún perfil disponible',
    'tm redirect roto',
    # Conectividad MoreLogin/CDP — proxy roto o puerto no listo
    'httpconnectionpool', 'connection refused', 'connection aborted',
    'failed to establish a new connection', 'winerror 10061',
    'max retries exceeded',
    # IO race conditions (json_atomic + Windows + procesos paralelos)
    'winerror 32', 'proceso no tiene acceso al archivo',
    'being used by another process',
    # Locator timeouts en signup — TM lento, no permanent
    'locator.click', 'locator.wait_for', 'locator.fill',
    'siguiente_disabled',
)

# Errores PERMANENTES → archive abandoned, no reintentar
_PERMANENT_PATTERNS = (
    'signin_password_input_missing',
    'tm_account_already_exists',
    'modo desconocido',
)


def is_transient_error(err: str) -> bool:
    e = (err or '').lower()
    if any(p in e for p in _PERMANENT_PATTERNS):
        return False
    return any(p in e for p in _TRANSIENT_PATTERNS)


def is_permanent_error(err: str) -> bool:
    return any(p in (err or '').lower() for p in _PERMANENT_PATTERNS)


def load_flaky_counts() -> dict:
    try:
        with open(FLAKY_COUNTS_PATH, encoding='utf-8') as f: return json.load(f)
    except Exception: return {}


def save_flaky_counts(d: dict):
    with open(FLAKY_COUNTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=2)


def bump_flaky(email: str, error: str) -> int:
    d = load_flaky_counts()
    em = email.lower()
    rec = d.setdefault(em, {'count': 0, 'errors': []})
    rec['count'] = rec.get('count', 0) + 1
    rec['errors'] = (rec.get('errors') or [])[-9:] + [
        {'at': datetime.now(timezone.utc).isoformat(), 'error': str(error)[:160]}]
    d[em] = rec
    save_flaky_counts(d)
    return rec['count']


def get_flaky_count(email: str) -> int:
    return (load_flaky_counts().get(email.lower()) or {}).get('count', 0)


def clear_flaky(email: str):
    d = load_flaky_counts()
    d.pop(email.lower(), None)
    save_flaky_counts(d)


def _move_row_to_end(email: str) -> bool:
    """Mueve la fila con Email_TM=email al final de plantilla_tm.csv.
    Útil para destrabar el pool cuando una cuenta falla repetidamente por infra
    sin culpa propia (libera la #1 para que otra cuenta avance)."""
    def _do():
        import csv as _csv, shutil as _sh
        if not os.path.isfile(PLANTILLA): return False
        try: _sh.copy2(PLANTILLA, PLANTILLA + '.bak.before_move')
        except Exception: pass
        with open(PLANTILLA, encoding='utf-8-sig', newline='') as f:
            rd = _csv.DictReader(f)
            fields = list(rd.fieldnames or [])
            rows = list(rd)
        moved = None
        kept = []
        em_low = email.lower().strip()
        for r in rows:
            if (r.get('Email_TM','') or '').strip().lower() == em_low:
                moved = r
            else:
                kept.append(r)
        if not moved: return False
        kept.append(moved)
        if fields and fields[0] == 'N_Usuario':
            for i, r in enumerate(kept, 1): r['N_Usuario'] = str(i)
        tmp = PLANTILLA + f'.tmp.{os.getpid()}.{int(time.time()*1000)}'
        with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
            w = _csv.DictWriter(f, fieldnames=fields)
            w.writeheader(); w.writerows(kept)
        os.replace(tmp, PLANTILLA)
        return True
    return _with_plantilla_lock(_do)


def archive_flaky(email: str, run: RunState, reason: str):
    """Archiva la fila a flaky_archive.csv (con lock cross-process)."""
    _record_problem_in_tm_registrations(email, 'flaky', reason, run)
    def _do():
        import csv as _csv, shutil as _sh
        flaky_path = os.path.join(RUNS_DIR, 'flaky_archive.csv')
        if not os.path.isfile(PLANTILLA): return
        try: _sh.copy2(PLANTILLA, PLANTILLA + '.bak.preflaky')
        except Exception: pass
        with open(PLANTILLA, 'r', encoding='utf-8-sig', newline='') as f:
            reader = _csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            kept, archived = [], None
            for row in reader:
                if (row.get('Email_TM') or '').strip().lower() == email.lower():
                    archived = dict(row); continue
                kept.append(row)
        if not archived: return
        if fieldnames and fieldnames[0] == 'N_Usuario':
            for i, r in enumerate(kept, 1): r['N_Usuario'] = str(i)
        tmp = PLANTILLA + f'.tmp.{os.getpid()}'
        with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
            w = _csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(kept)
        os.replace(tmp, PLANTILLA)
        archived['_flaky_at'] = datetime.now(timezone.utc).isoformat()
        archived['_reason'] = reason[:200]
        archived['_run_id'] = run.run_id
        write_header = not os.path.isfile(flaky_path) or os.path.getsize(flaky_path) == 0
        with open(flaky_path, 'a', encoding='utf-8-sig', newline='') as f:
            w = _csv.DictWriter(f, fieldnames=list(archived.keys()))
            if write_header: w.writeheader()
            w.writerow(archived)
    _with_plantilla_lock(_do)


def _attempt_browser_flow(user: UserRow, run: RunState, burned_acc: set) -> dict:
    """Una sola pasada del flow del browser. Devuelve {phone_info, artifacts, profile, ctx, page, pw, b}.
    Lanza excepción en cualquier error — el caller maneja retry."""
    ctx, page, prof, port = open_profile('ES', burned_acc, run)

    # claim ya hecho atómicamente en open_profile bajo lock → solo propagar a set local
    if run.profile:
        burned_acc.add(run.profile)
    try:
        mode = goto_signup(page, user, run)
        if mode == 'signup':
            fill_signup_form(page, user, run)
        elif mode == 'signin':
            # TM detectó que el email YA tiene cuenta → intenta auto-RECOVERY (reset password
            # via IMAP madre) antes de archivar como abandoned. Si recovery success: la
            # cuenta queda persistida con NUEVA password en plantilla + accounts.json.
            screenshot_on_error(page, run, 'tm_already_exists_signin_mode')
            try:
                import account_recovery
                rec = account_recovery.recover_inline(page, ctx, user, run, log=log,
                                                       max_imap_wait_s=300)
            except Exception as _re:
                rec = {'ok': False, 'error': f'recovery_call_failed: {_re}'}
            if rec.get('ok'):
                log(f'  🔓 RECOVERY SUCCESS — cuenta {user.email} recuperada con nueva pwd')
                run.stage_done('recovered_via_reset_password', {
                    'persist': rec.get('persist', {}),
                    'new_password_prefix': (rec.get('new_password') or '')[:4] + '***',
                })
                # Marcar señal en run.final para que el outer loop la trate como success
                run.final = {'recovered': True, 'new_password': rec.get('new_password')}
                # Salimos limpios sin lanzar excepción para que el caller continúe el flow
                # (el browser queda en estado post-reset; el caller cerrará perfil).
                # Devolvemos un resultado especial: phone_info vacío + flag recovered
                return {
                    'phone_info': {'phone': '', 'recovered': True},
                    'artifacts': {'recovery_only': True},
                    'page': page, 'ctx': ctx,
                }
            else:
                rec_err = rec.get('error', '?')
                if 'bot_block_detected' in rec_err:
                    # No es cuenta existente: es un bloqueo → reintento con otro perfil/IP
                    log(f'  ⚠ recovery falló por bot_block ({rec_err[:80]}) → otro perfil')
                    raise RuntimeError(rec_err)
                log(f'  ⚠ recovery falló ({rec_err[:80]}) → archive abandoned')
                raise RuntimeError(f'tm_account_already_exists (signin mode, title={page.title()})')
        elif mode == 'unknown' and 'profile' in page.title().lower():
            screenshot_on_error(page, run, 'tm_already_exists')
            raise RuntimeError(f'tm_account_already_exists (title={page.title()})')
        else:
            screenshot_on_error(page, run, 'unknown_mode')
            raise RuntimeError(f'modo desconocido: {page.title()}')

        if not email_otp(page, user, run):
            raise RuntimeError('email OTP timeout')

        # Panel "identidad global / manténte al día" puede salir TRAS verificar email,
        # ANTES del modal teléfono. Click "Aceptar" defensivo si está visible.
        try: click_global_identity_consent(page)
        except Exception: pass

        log('click "Añadir mi teléfono"')
        phone_inp = open_phone_modal(page)
        if not phone_inp:
            # Si el modal no apareció, quizás está oculto detrás del panel global.
            # Reintenta tras consent.
            try:
                if click_global_identity_consent(page):
                    page.wait_for_timeout(1500)
                    phone_inp = open_phone_modal(page)
            except Exception: pass
        if not phone_inp:
            blk = detect_bot_block(page)
            if blk:
                screenshot_on_error(page, run, 'bot_blocked_pre_phone')
                raise RuntimeError(f'bot_block_detected: "{blk}" (sin form teléfono)')
            raise RuntimeError('no abrió form teléfono')

        phone_info = phone_relentless(page, user, phone_inp, run)
        if not phone_info:
            log(f'  ❌ {MAX_PHONE_CYCLES} SMS sin llegar — cerrando navegador y blacklist perfil', level='ERROR')
            raise RuntimeError(f'phone failed tras {MAX_PHONE_CYCLES} ciclos sin SMS')

        finalize_terms_and_passkey(page, run)
        artifacts = save_artifacts(ctx, run)
        return {'phone_info': phone_info, 'artifacts': artifacts,
                'page': page, 'ctx': ctx}
    except Exception as _e:
        # Cleanup en caso de fallo (caller seguirá con OTRO perfil + OTRA IP)
        _err_str = str(_e).lower()
        try: _cancel_prefetched('attempt_error')
        except Exception: pass
        try: screenshot_on_error(page, run, 'attempt_error')
        except Exception: pass
        try: page._b.close(); page._pw.stop()
        except Exception: pass
        try: close_profile(run)
        except Exception: pass
        # SIEMPRE blacklistear el perfil quemado
        if run.profile:
            burned_acc.add(run.profile); save_burned(burned_acc)
        # ADICIONAL si fue bot_block: también blacklistear la IP (MoreLogin puede
        # asignar misma IP a otro perfil distinto del pool, evitar reusarla).
        if 'bot_block_detected' in _err_str and run.ip:
            try:
                save_used_ip(run.ip, f'bot_block_burned_{run.run_id}')
                log(f'  🚫 IP {run.ip} blacklisteada por bot_block — no se reusará en otro perfil')
            except Exception: pass
            _burn_subnet24(run.ip, run.run_id)
        raise


def register_one(row_idx: int, *, override_first: Optional[str] = None,
                 override_last: Optional[str] = None,
                 burned: Optional[set] = None) -> RunState:
    run_id = f'run_{datetime.now().strftime("%Y%m%d_%H%M%S")}_u{row_idx}'
    log(f'═══ {run_id} ═══')

    # CLAIM ATÓMICO de email — evita que dos procesos paralelos cojan la misma fila.
    # Si la fila pedida está claimed o registered, busca la siguiente disponible.
    claim = _claim_email_atomic(row_idx)
    if not claim:
        log(f'  ❌ no hay filas disponibles en plantilla (todas claimed/registered/flaky)', level='ERROR')
        run = RunState(run_id=run_id, row_idx=row_idx, email='',
                       started_at=datetime.now(timezone.utc).isoformat())
        run.error = 'no_available_rows'; run.persist(); return run

    actual_row_idx, csv_row = claim
    if actual_row_idx != row_idx:
        log(f'  ℹ fila {row_idx} ocupada — usando fila {actual_row_idx} en su lugar')
    user = UserRow.from_csv_row(actual_row_idx, csv_row)
    row_idx = actual_row_idx
    log(f'  email={user.email}  (CLAIMED)  raw name={user.first!r} {user.last!r}')

    if override_first or override_last:
        user.first = override_first or user.first
        user.last = override_last or user.last
    elif looks_garbage(user.first) or looks_garbage(user.last):
        user.first, user.last = 'Julio', 'Martin Perez'
        log(f'  auto-override → {user.first} {user.last}')

    burned_acc = (burned or set()) | load_burned()
    run = RunState(run_id=run_id, row_idx=row_idx, email=user.email,
                   started_at=datetime.now(timezone.utc).isoformat(),
                   burned_profiles=sorted(burned_acc))
    run.persist()

    # ── PRE-FLIGHT 1: ¿ya registrada? ──
    if email_already_registered(user.email):
        log(f'  ⏭ ya en account_manager — skip', level='WARN')
        _release_email_claim(user.email)
        run.error = 'already_registered'
        run.stage_done('preflight_skip', {'reason': 'already_in_account_manager'})
        run.persist(); return run

    # ── PRE-FLIGHT 2: ¿demasiado flaky ya? ──
    flaky_n = get_flaky_count(user.email)
    if flaky_n >= FLAKY_THRESHOLD:
        log(f'  ⛔ {user.email} con {flaky_n} fallos previos → flaky_archive', level='WARN')
        archive_flaky(user.email, run, f'flaky threshold reached ({flaky_n})')
        clear_flaky(user.email)
        _release_email_claim(user.email)
        run.error = f'flaky_threshold_{flaky_n}'
        run.persist(); return run

    # ── PRE-FLIGHT 3: IMAP madre ──
    log('preflight: IMAP madre health check')
    if not imap_health_check(user.contact_email, user.contact_pwd):
        _release_email_claim(user.email)
        run.error = 'imap_unreachable'; run.persist(); return run
    log(f'  ✓ IMAP madre OK')

    # ── INNER RETRY LOOP — hasta INNER_RETRY_MAX intentos con perfiles distintos ──
    last_err = None
    result = None
    for attempt in range(1, INNER_RETRY_MAX + 1):
        log(f'─── intento {attempt}/{INNER_RETRY_MAX} ───')
        try:
            result = _attempt_browser_flow(user, run, burned_acc)
            log(f'  ✓ flow OK en intento {attempt}'); break
        except Exception as e:
            err = str(e)
            log(f'  ❌ intento {attempt} falló: {err[:120]}', level='ERROR')
            last_err = err

            if is_permanent_error(err):
                log('  ⛔ error permanente → archive abandoned + stop retry')
                try: _archive_abandoned(user.email, run, err)
                except Exception: pass
                clear_flaky(user.email)
                _release_email_claim(user.email)
                run.error = err; run.persist(); return run

            if not is_transient_error(err):
                log(f'  ⛔ error desconocido (asumir permanente) → stop')
                _release_email_claim(user.email)
                run.error = err; run.persist(); return run

            # Transient — retry inmediato. Pool manager replenish en <1s así que
            # tampoco hay que ralentizar aquí. Mínimo 1s para evitar request-flood.
            if attempt < INNER_RETRY_MAX:
                wait = 1 if 'bot_block_detected' in err.lower() else 2
                log(f'  ⏳ retry en {wait}s')
                time.sleep(wait)

    if not result:
        # NO incrementar flaky si todos los fallos fueron de infraestructura (bot_block, browser
        # closed, network, proxy/CDP) — la cuenta es válida, solo los proxies/perfiles están
        # quemados. Solo bump flaky si fue un error que apunta a la cuenta misma.
        infra_only = bool(last_err) and any(p in last_err.lower() for p in
            ('bot_block_detected', 'suspendido', 'browser has been closed',
             'target page, context', 'execution context was destroyed',
             'no se pudo arrancar', 'no hay ningún perfil',
             # Conectividad MoreLogin/CDP — proxy roto o puerto del browser no listo
             'httpconnectionpool', 'connection refused', 'connection aborted',
             'failed to establish a new connection', 'winerror 10061',
             'max retries exceeded', 'newconnectionerror',
             # IO race en Windows entre procesos paralelos
             'winerror 32', 'proceso no tiene acceso al archivo',
             'being used by another process',
             # Locator timeouts en signup — TM lento o página no cargó por proxy
             'locator.click: timeout', 'locator.wait_for: timeout',
             # UI inestable de TM — modal/form no apareció (no es fault de la cuenta)
             'no abrió form teléfono', 'no input phone', 'no input otp',
             'no llegó al verify form', 'siguiente_disabled',
             'email otp timeout', 'phone failed tras', 'tm redirect roto',
             'email input nunca apareció'))
        if infra_only:
            log(f'  ⚠ {INNER_RETRY_MAX} intentos sin éxito (infra: proxies/perfiles quemados) — '
                f'NO bump flaky, fila volverá a la cola', level='WARN')
            # Mover la fila al FINAL de plantilla para no atascar el pool si la misma
            # cuenta falla 2+ veces seguidas en la #1. Otra cuenta toma su sitio.
            try:
                _move_row_to_end(user.email)
                log(f'  ↪ fila {user.email} movida al final de plantilla (no estancar pool)')
            except Exception as e:
                log(f'  ⚠ move_row_to_end err: {e}')
        else:
            cnt = bump_flaky(user.email, last_err or 'unknown')
            log(f'  ⚠ {INNER_RETRY_MAX} intentos sin éxito. flaky_count={cnt}/{FLAKY_THRESHOLD}', level='WARN')
        _release_email_claim(user.email)
        run.error = last_err
        run.persist(); return run

    # ── ÉXITO: registrar en dashboard + archive plantilla ──
    phone_info = result['phone_info']
    artifacts = result['artifacts']
    page = result['page']
    ctx = result['ctx']

    # Modo RECOVERY: register_in_dashboard ya lo hizo account_recovery._persist_recovered_password.
    # Saltamos los pasos que duplicarían trabajo, pero seguimos haciendo:
    #   - remove_from_plantilla (para que la fila ya no aparezca pendiente)
    #   - clear_flaky / release claim / cerrar browser / persistencia run
    recovered = bool(phone_info.get('recovered') or artifacts.get('recovery_only'))

    # ── PERSISTENCIA TRANSACCIONAL (antes de cerrar el browser) ──
    # Cada paso protegido en try/except — si uno falla, los siguientes igual se ejecutan
    # para evitar huérfano. Garantía: cuenta en TM ⇒ persistida en disco al 100%.
    persist_errors = []
    if phone_info.get('phone'):
        try:
            if _update_phone_in_plantilla(user.email, phone_info['phone']):
                log(f'  ✓ teléfono guardado en plantilla_tm.csv: {phone_info["phone"]}')
            d = phone_info['phone'].lstrip('+')
            if d.startswith('34'): d = d[2:]
            user.phone_local = d
        except Exception as e:
            persist_errors.append(f'update_phone: {e}')

    info = {}
    if recovered:
        log('  ⏭ recovery mode — saltando register_in_dashboard (account ya persistida)')
    else:
        try:
            info = register_in_dashboard(user, phone_info, run, artifacts)
        except Exception as e:
            persist_errors.append(f'register_in_dashboard: {e}')
            log(f'  ⚠ register_in_dashboard err: {e}', level='WARN')

    try:
        rec = record_in_tm_registrations(user, info.get('account_id', ''), run)
        log(f'  tm_registrations: {rec}')
    except Exception as e:
        persist_errors.append(f'tm_registrations: {e}')
        log(f'  ⚠ tm_registrations err: {e}', level='WARN')

    moved = {}
    try:
        moved = remove_from_plantilla(user.email, run)
    except Exception as e:
        persist_errors.append(f'remove_plantilla: {e}')
        log(f'  ⚠ remove_plantilla err: {e}', level='WARN')

    try: clear_flaky(user.email)
    except Exception: pass
    try: _release_email_claim(user.email)
    except Exception: pass

    if persist_errors:
        log(f'  ⚠⚠ PERSIST WARNINGS ({len(persist_errors)}) — cuenta creada en TM '
            f'pero algunos pasos de persistencia fallaron: {persist_errors[:2]}', level='WARN')
    # Persistencia 100% intentada — ahora SÍ es seguro cerrar (no perdemos cuenta TM).

    # ── CIERRE LIMPIO DE BROWSER + PERFIL ──
    try: logout_account(page, run)
    except Exception: pass
    try: page._b.close(); page._pw.stop()
    except Exception: pass
    try: close_profile(run)
    except Exception: pass

    run.success = True
    run.final = {'phone': phone_info.get('phone', ''), 'account_id': info.get('account_id', ''),
                 'visible_in_dashboard': info.get('visible_in_dashboard', False),
                 'plantilla_moved': moved.get('ok', False),
                 'plantilla_remaining_rows': moved.get('remaining_rows'),
                 'persist_errors': persist_errors}
    run.persist()
    log(f'═══ FIN {run_id} success=True (persist_errors={len(persist_errors)}) ═══')
    return run

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--row', type=int, help='fila plantilla 1-based')
    p.add_argument('--rows', help='rango p.ej. 3-10')
    p.add_argument('--first', help='override Nombre')
    p.add_argument('--last', help='override Apellidos')
    p.add_argument('--burned', help='perfiles a excluir CSV')
    p.add_argument('--engine', choices=['morelogin', 'silent'], default=None,
                   help='motor de perfiles (default: env TM_ENGINE o morelogin)')
    args = p.parse_args()

    if args.engine and args.engine != ENGINE:
        global bo
        bo = _load_engine(args.engine)

    burned = set((args.burned or '').split(',')) - {''}
    runs = []
    if args.rows:
        a, b = args.rows.split('-')
        rows_to_do = list(range(int(a), int(b) + 1))
        for i, r in enumerate(rows_to_do):
            runs.append(register_one(r, override_first=args.first,
                                     override_last=args.last, burned=burned))
            burned |= set(rs for rs in load_burned())
            # Delay random entre runs para no levantar rate-limit TM
            if i < len(rows_to_do) - 1:
                delay = random.randint(60, 120)
                log(f'⏱ delay anti rate-limit: {delay}s antes del siguiente run')
                time.sleep(delay)
    elif args.row:
        runs.append(register_one(args.row, override_first=args.first,
                                 override_last=args.last, burned=burned))
    else:
        p.error('--row o --rows requerido')

    print('\n═══ RESUMEN ═══')
    for r in runs:
        mark = '✅' if r.success else '❌'
        extra = (f'phone={r.final.get("phone")} acc={r.final.get("account_id")}'
                 if r.success else f'err={r.error}')
        print(f'  {mark} U{r.row_idx} {r.email}  {extra}')

def self_test() -> dict:
    """Validación básica del módulo (política del proyecto, módulos >50 LOC).
    Verifica imports, funciones clave y constantes definidas. NO ejecuta browser.
    """
    out = {'ok': True, 'checks': {}, 'errors': []}
    try:
        # Imports principales presentes
        for mod in ('bot_orchestrator', 'cookie_warmer', 'cookie_filter',
                     'http2_frame_match', 'tm_browser_engine'):
            try:
                __import__(mod); out['checks'][f'import:{mod}'] = True
            except Exception as e:
                out['checks'][f'import:{mod}'] = False
                out['errors'].append(f'{mod}: {e}')
                out['ok'] = False

        # Funciones clave del flow
        for fn in ('open_profile', 'goto_signup', 'fill_signup_form',
                    'email_otp', '_click_verificar_with_lock',
                    '_pick_es_profile_smart', 'detect_bot_block',
                    'load_used_ips', 'save_used_ip',
                    'load_burned', 'save_burned'):
            present = fn in globals() and callable(globals()[fn])
            out['checks'][f'fn:{fn}'] = present
            if not present:
                out['errors'].append(f'fn missing: {fn}')
                out['ok'] = False

        # Constantes / paths esenciales
        for const in ('USED_IPS_PATH', 'PLANTILLA', 'RUNS_DIR',
                       'PROFILE_PICK_LOCK_PATH'):
            present = const in globals() and globals()[const]
            out['checks'][f'const:{const}'] = bool(present)
            if not present:
                out['ok'] = False
                out['errors'].append(f'const missing: {const}')

    except Exception as e:
        out['ok'] = False
        out['errors'].append(f'self_test exception: {e}')
    return out


if __name__ == '__main__':
    main()
