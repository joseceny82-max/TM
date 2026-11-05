"""_pool_manager_loop.py — versión LOOP continuo del pool manager.

Corre en bucle (no one-shot como _pool_manager.py): detecta cuando un slot
termina e inmediatamente lanza el siguiente (0s stagger). Para 20+ registros
elimina la latencia de 270s entre wakeups.

Args:
    pool_size  (default 3) — N procesos paralelos
    target_n   (default 20) — cuentas nuevas a registrar
    baseline   (default current active count)

Auto-stop al alcanzar target_n. Stop manual: kill o STOP file.
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime

# Force UTF-8 stdout en Windows (evita UnicodeEncodeError con flechas/símbolos)
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                    line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                                    line_buffering=True)
except Exception:
    pass

PROYECTO = os.path.dirname(os.path.abspath(__file__))
POOL_SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 3
TARGET_N = int(sys.argv[2]) if len(sys.argv) > 2 else 20
TICK_S = 2  # check cada 2s (era 5s) — más responsive al detectar slot libre
STOP_FILE = os.path.join(PROYECTO, '_pool_manager_loop.STOP')

# Blacklist row que fallan repetidos: si una row se relanza 3 veces sin completar,
# skipear y pasar a la siguiente. Evita ciclo infinito en filas con claims rotos.
ROW_FAIL_THRESHOLD = 3
_row_launch_count = {}    # {row_idx: count}
_blacklisted_rows = set()  # rows que fallaron > THRESHOLD

# Anti-bot-block: si detectamos un fail por bot_block, pausar 60s antes de
# lanzar el siguiente para que TM "olvide" la IP/UA. Después marca el momento
# y NO vuelve a pausar dentro de la misma ventana (evita bucle).
BOT_BLOCK_COOLDOWN_S = 60
RECENT_FAILS_WINDOW_S = 600   # 10 min
BOT_BLOCK_COOLDOWN_TOLERANCE_S = 600   # tras pausar, no re-pausar en 10 min
BOT_BLOCK_THRESHOLD = 6        # subido de 3 a 6 (más tolerante, menos bucles)
_recent_bot_blocks = []        # timestamps de fails detectados
_last_block_pause_at = 0

# Auto-scale: si detectamos >=2 recoveries en últimos 5 min, escalar a pool+1
# (recovery es IO-bound esperando IMAP, no CPU; podemos paralelizar más).
AUTO_SCALE_RECOVERIES_THRESHOLD = 2
AUTO_SCALE_WINDOW_S = 300
AUTO_SCALE_MAX_POOL = 5


def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


# Tracking interno: PIDs lanzados con timestamp. Si wmic falla, usar este
# fallback para evitar bucle de relanzamiento que crea procesos huérfanos.
_launched_pids = []   # [(pid, row, timestamp), ...]
_LAUNCH_GRACE_S = 30  # 30s en lugar de 90s — wmic detecta procesos legítimos en <15s


def get_running_rows():
    """Devuelve set de rows N corriendo. Si wmic falla, usa tracking interno."""
    wmic_ok = False
    rows = set()
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where', "name='python.exe'",
             'get', 'processid,commandline'],
            stderr=subprocess.DEVNULL, timeout=15
        ).decode('utf-8', errors='ignore')
        wmic_ok = True
        for line in out.splitlines():
            if 'register_account.py' in line and '--row' in line:
                try:
                    idx = line.index('--row') + len('--row')
                    rest = line[idx:].strip()
                    num = ''
                    for ch in rest:
                        if ch.isdigit():
                            num += ch
                        elif num:
                            break
                    if num:
                        rows.add(int(num))
                except Exception:
                    continue
    except Exception:
        wmic_ok = False

    # UNION wmic + tracking interno: protege contra wmic timing (proceso recién
    # lanzado puede tardar 5-15s en aparecer en wmic). Esto evita el bucle de
    # relanzamiento que crea procesos huérfanos.
    now = time.time()
    recent_launches = [(p, r, t) for p, r, t in _launched_pids
                        if (now - t) < _LAUNCH_GRACE_S]
    _launched_pids[:] = recent_launches
    recent_rows = {r for _, r, _ in recent_launches}
    # Siempre devolver UNION: wmic + tracking. Evita falsos negativos de wmic.
    return rows | recent_rows


def get_pending_rows():
    with open(os.path.join(PROYECTO, 'datos', 'cuentas', 'accounts.json'),
              encoding='utf-8') as f:
        d = json.load(f)
    active = {(a.get('tm_email') or '').lower()
              for a in d.get('accounts', [])
              if a.get('status') == 'active'}
    out = []
    with open(os.path.join(PROYECTO, 'plantilla_tm.csv'), encoding='utf-8') as f:
        for i, r in enumerate(csv.DictReader(f), 1):
            em = (r.get('Email_TM') or '').strip().lower()
            if em and em not in active:
                out.append((i, em))
    return out


def detect_recent_recoveries():
    """Cuenta recoveries (signin mode → reset password) en últimos N minutos.

    Si hay muchos, el batch tiene cuentas ya registradas y conviene escalar el
    pool (recovery espera IMAP, es IO-bound).
    """
    import glob, re
    now = time.time()
    cutoff = now - AUTO_SCALE_WINDOW_S
    n = 0
    for log_path in glob.glob(os.path.join(PROYECTO, '_pool_*.log')):
        try:
            mt = os.path.getmtime(log_path)
            if mt < cutoff:
                continue
            with open(log_path, encoding='utf-8', errors='ignore') as f:
                txt = f.read()[-3000:]
            if 'RECOVERY iniciado' in txt or 'tm_account_already_exists' in txt:
                n += 1
        except Exception:
            continue
    return n


def detect_recent_bot_blocks():
    """Escanea logs _pool_*.log de los últimos 10 min y cuenta bot_blocks.

    Retorna: número de bot_blocks detectados en la ventana.
    Mantiene `_recent_bot_blocks` actualizado con timestamps únicos.
    """
    import glob, re
    now = time.time()
    cutoff = now - RECENT_FAILS_WINDOW_S
    # Limpiar antiguos
    global _recent_bot_blocks
    _recent_bot_blocks = [t for t in _recent_bot_blocks if t > cutoff]

    bot_block_signals = (
        'bot_block_detected', 'no_email_input',
        'Locator.click: Timeout 30000ms', 'wait_for_selector.*input.*type.*email',
    )
    for log_path in glob.glob(os.path.join(PROYECTO, '_pool_*.log')):
        try:
            mt = os.path.getmtime(log_path)
            if mt < cutoff:
                continue
            with open(log_path, encoding='utf-8', errors='ignore') as f:
                txt = f.read()[-5000:]
            # Buscar bot_block en últimos 5KB
            for sig in bot_block_signals:
                if re.search(sig, txt):
                    if mt not in _recent_bot_blocks:
                        _recent_bot_blocks.append(mt)
                    break
        except Exception:
            continue
    return len(_recent_bot_blocks)


def get_active_count():
    with open(os.path.join(PROYECTO, 'datos', 'cuentas', 'accounts.json'),
              encoding='utf-8') as f:
        d = json.load(f)
    return sum(1 for a in d.get('accounts', []) if a.get('status') == 'active')


def launch_one(row, email):
    short = ''.join(c for c in email.split('@')[0] if c.isalnum())[:15]
    log_path = os.path.join(PROYECTO, f'_pool_{short}.log')
    cmd = [sys.executable, 'register_account.py', '--row', str(row)]
    with open(log_path, 'w', encoding='utf-8') as logf:
        proc = subprocess.Popen(
            cmd, cwd=PROYECTO, stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0)
                          | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
        )
    # Tracking interno para fallback si wmic falla
    _launched_pids.append((proc.pid, row, time.time()))
    return proc.pid


def main():
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    baseline = get_active_count()
    log(f'POOL_LOOP start - pool_size={POOL_SIZE} target_n={TARGET_N} '
        f'baseline={baseline} -> goal active={baseline+TARGET_N}')

    while True:
        if os.path.exists(STOP_FILE):
            log('STOP file detected → exit')
            break

        active = get_active_count()
        gained = active - baseline
        if gained >= TARGET_N:
            log(f'TARGET REACHED · active={active} gained={gained}/{TARGET_N}')
            break

        running = get_running_rows()

        # Auto-scale: si hay muchos recoveries (IO-bound), escalar pool
        n_recov = detect_recent_recoveries()
        effective_pool = POOL_SIZE
        if n_recov >= AUTO_SCALE_RECOVERIES_THRESHOLD:
            effective_pool = min(AUTO_SCALE_MAX_POOL, POOL_SIZE + 1)
            if effective_pool > POOL_SIZE:
                if not getattr(main, '_logged_scale', False):
                    log(f'  [AUTO-SCALE] {n_recov} recoveries en 5min · '
                        f'pool {POOL_SIZE} -> {effective_pool}')
                    main._logged_scale = True
        else:
            main._logged_scale = False
        slots_free = effective_pool - len(running)

        # Anti-bot-block cooldown: si hay 6+ bot_blocks en últimos 10 min,
        # pausar 60s UNA VEZ. Tras pausar, no volver a pausar en 10 min
        # (evita bucle: los logs viejos siguen teniendo mtime reciente).
        global _last_block_pause_at
        time_since_last_pause = time.time() - _last_block_pause_at
        if slots_free > 0 and time_since_last_pause > BOT_BLOCK_COOLDOWN_TOLERANCE_S:
            n_blocks = detect_recent_bot_blocks()
            if n_blocks >= BOT_BLOCK_THRESHOLD:
                log(f'  ⚠ {n_blocks} bot_blocks detectados en últimos 10min · '
                    f'PAUSE {BOT_BLOCK_COOLDOWN_S}s (próximo cooldown disponible '
                    f'en {BOT_BLOCK_COOLDOWN_TOLERANCE_S}s)')
                _last_block_pause_at = time.time()
                time.sleep(BOT_BLOCK_COOLDOWN_S)
                continue   # vuelve al inicio del loop tras cooldown

        if slots_free > 0:
            pending = get_pending_rows()
            launched = 0
            for row, em in pending:
                if launched >= slots_free:
                    break
                if row in running:
                    continue
                # Skip rows con >= THRESHOLD fails (claim huérfano persistente)
                if row in _blacklisted_rows:
                    continue
                if _row_launch_count.get(row, 0) >= ROW_FAIL_THRESHOLD:
                    _blacklisted_rows.add(row)
                    log(f'  [BLACKLIST] row={row} {em} (>{ROW_FAIL_THRESHOLD} fails) — skip')
                    continue
                pid = launch_one(row, em)
                _row_launch_count[row] = _row_launch_count.get(row, 0) + 1
                log(f'  LAUNCH row={row} {em} pid={pid} '
                    f'(slots_free={slots_free}, gained={gained}/{TARGET_N}, '
                    f'attempt={_row_launch_count[row]})')
                running.add(row)
                launched += 1
                # NO stagger entre lanzamientos del mismo tick (ya hubo gap antes)
            if launched == 0 and len(pending) == 0:
                log('NO_MORE_PENDINGS')
                break
            # Si TODOS los pending están blacklistados (claims rotos), también salir
            if launched == 0 and all(r in _blacklisted_rows for r, _ in pending):
                log(f'NO_LAUNCHABLE_ROWS — todos blacklistados ({len(_blacklisted_rows)})')
                break

        time.sleep(TICK_S)

    log(f'POOL_LOOP end - final active={get_active_count()}')


if __name__ == '__main__':
    main()
