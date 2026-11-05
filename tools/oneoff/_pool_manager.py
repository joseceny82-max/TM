"""_pool_manager.py — auto-replenish pool de N register_account.py paralelos.

Detecta procesos `register_account.py --row N` activos vía wmic, identifica los
rows en ejecución, y lanza nuevos del pending hasta llegar a POOL_SIZE.

Uso: python _pool_manager.py [pool_size]
"""
import csv
import json
import os
import subprocess
import sys
import time


POOL_SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def get_running_register_account_rows():
    """Devuelve set de rows N actualmente corriendo (wmic-based)."""
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where', "name='python.exe'",
             'get', 'processid,commandline'],
            stderr=subprocess.DEVNULL, timeout=10
        ).decode('utf-8', errors='ignore')
    except Exception:
        return set()
    rows = set()
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
    return rows


def get_pending_rows():
    """Devuelve [(row_idx, email)] de cuentas en plantilla que NO están active."""
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, 'datos', 'cuentas', 'accounts.json'),
              encoding='utf-8') as f:
        d = json.load(f)
    active = {(a.get('tm_email') or '').lower()
              for a in d.get('accounts', [])
              if a.get('status') == 'active'}
    out = []
    with open(os.path.join(base, 'plantilla_tm.csv'), encoding='utf-8') as f:
        for i, r in enumerate(csv.DictReader(f), 1):
            em = (r.get('Email_TM') or '').strip().lower()
            if em and em not in active:
                out.append((i, em))
    return out


def launch_one(row, email, slot_label='pool'):
    base = os.path.dirname(os.path.abspath(__file__))
    short = ''.join(c for c in email.split('@')[0] if c.isalnum())[:15]
    log_path = os.path.join(base, f'_pool_{short}.log')
    cmd = [sys.executable, 'register_account.py', '--row', str(row)]
    with open(log_path, 'w', encoding='utf-8') as logf:
        proc = subprocess.Popen(
            cmd, cwd=base, stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0)
                          | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
        )
    return proc.pid


def main():
    running = get_running_register_account_rows()
    pending = get_pending_rows()
    print(f'pool_size={POOL_SIZE} running_rows={sorted(running)} '
          f'pending_total={len(pending)}', flush=True)

    to_launch = max(0, POOL_SIZE - len(running))
    if to_launch == 0:
        print('pool full, nothing to launch', flush=True)
        return

    # Primer N pending no en running
    launched = 0
    for row, em in pending:
        if launched >= to_launch:
            break
        if row in running:
            continue
        pid = launch_one(row, em)
        print(f'  LAUNCH row={row} {em} pid={pid}', flush=True)
        launched += 1
        time.sleep(5)  # stagger

    if launched == 0:
        print('NO_MORE_PENDINGS', flush=True)
    else:
        print(f'launched {launched}/{to_launch}', flush=True)


if __name__ == '__main__':
    main()
