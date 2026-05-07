"""continuous_register.py — Pool de N navegadores SIEMPRE activos.

A diferencia de parallel_register.py (que espera que todos terminen antes de la
siguiente tanda), aquí cuando UN proceso termina otro arranca al instante,
manteniendo siempre `--concurrency` navegadores trabajando.

Uso:
  python continuous_register.py --total 30                    # 30 registros con 3 simultáneos
  python continuous_register.py --total 50 --concurrency 4    # 50 registros con 4 simultáneos
  python continuous_register.py --total 30 --row 202          # siempre fila 202 (default)
"""
import argparse, os, subprocess, sys, time
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Users\user\TM\proyecto'
LOG_DIR = os.path.join(ROOT, 'datos', 'buy', 'runs')
os.makedirs(LOG_DIR, exist_ok=True)

try: from subprocess import CREATE_NEW_PROCESS_GROUP
except Exception: CREATE_NEW_PROCESS_GROUP = 0


def _now(): return datetime.now().strftime('%H:%M:%S')

def spawn(idx: int, row: int, first: str = None, last: str = None):
    """Lanza un nuevo proceso de register_account.py. Devuelve dict con info."""
    log_path = os.path.join(LOG_DIR, f'_cont{idx}.log')
    try: open(log_path, 'w').close()
    except Exception: pass
    cmd = [sys.executable, '-u', os.path.join(ROOT, 'register_account.py'),
           '--row', str(row)]
    if first: cmd += ['--first', first]
    if last:  cmd += ['--last', last]
    logf = open(log_path, 'w', encoding='utf-8')
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                         cwd=ROOT, creationflags=CREATE_NEW_PROCESS_GROUP)
    return {'proc': p, 'idx': idx, 'log': log_path, 'logf': logf,
            'started_at': time.time(), 'row': row}


def get_summary(log_path: str) -> str:
    try:
        with open(log_path, encoding='utf-8') as f:
            lines = [l.rstrip() for l in f if l.strip()]
        for ln in reversed(lines):
            if '✅' in ln or '❌' in ln: return ln[:140]
        return lines[-1] if lines else '(empty)'
    except Exception: return '(no log)'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--total', type=int, required=True, help='registros totales a procesar')
    p.add_argument('--concurrency', type=int, default=3, help='workers paralelos (default 3)')
    p.add_argument('--row', type=int, default=202, help='fila plantilla a usar (default 202)')
    p.add_argument('--first', help='override Nombre')
    p.add_argument('--last', help='override Apellidos')
    args = p.parse_args()

    print(f'═══ POOL CONTINUO — {args.total} registros · {args.concurrency} simultáneos ═══')
    print(f'  fila base: {args.row} (siempre primera no registrada del momento)\n')

    workers = {}     # idx → worker dict
    next_idx = 0
    completed = 0
    successes = 0
    failures = 0
    results = []     # tabla resumen final

    # Limpiar lock al empezar
    try:
        lock_path = os.path.join(LOG_DIR, 'email_otp.lock')
        if os.path.isfile(lock_path): os.unlink(lock_path)
    except Exception: pass

    # Spawn iniciales
    for _ in range(min(args.concurrency, args.total)):
        next_idx += 1
        w = spawn(next_idx, args.row, args.first, args.last)
        workers[next_idx] = w
        print(f'[{_now()}] ▶ spawn proc#{next_idx} (PID {w["proc"].pid}) → {w["log"]}')

    # Loop principal: cuando un worker termina, spawn otro hasta completar
    while workers:
        time.sleep(2)
        finished = []
        for idx, w in workers.items():
            rc = w['proc'].poll()
            if rc is not None:
                finished.append((idx, w, rc))

        for idx, w, rc in finished:
            try: w['logf'].close()
            except Exception: pass
            elapsed = time.time() - w['started_at']
            summary = get_summary(w['log'])
            ok = '✅' in summary
            mark = '✅' if ok else '❌'
            if ok: successes += 1
            else: failures += 1
            completed += 1
            results.append({'idx': idx, 'ok': ok, 'summary': summary, 'elapsed': elapsed})
            print(f'[{_now()}] {mark} proc#{idx} done ({elapsed:.0f}s) — {summary}')
            print(f'        progreso: {completed}/{args.total} '
                  f'(✅ {successes} · ❌ {failures} · activos {len(workers)-1})')
            del workers[idx]

            # Si quedan registros por hacer, spawn nuevo
            if completed + len(workers) < args.total:
                next_idx += 1
                # Limpiar lock entre spawns para evitar contención si proceso anterior crashed
                try:
                    lock_path = os.path.join(LOG_DIR, 'email_otp.lock')
                    if os.path.isfile(lock_path):
                        # Solo limpiar si stale (>5 min)
                        age = time.time() - os.path.getmtime(lock_path)
                        if age > 300: os.unlink(lock_path)
                except Exception: pass
                nw = spawn(next_idx, args.row, args.first, args.last)
                workers[next_idx] = nw
                print(f'[{_now()}] ▶ spawn proc#{next_idx} (PID {nw["proc"].pid}) — '
                      f'pool ahora: {len(workers)}')

    # Resumen final
    print(f'\n═══ POOL FINALIZADO ═══')
    print(f'  Total: {completed} · ✅ {successes} · ❌ {failures}')
    for r in results:
        mark = '✅' if r['ok'] else '❌'
        print(f'  {mark} #{r["idx"]:3} ({r["elapsed"]:5.0f}s) {r["summary"][:120]}')


if __name__ == '__main__':
    main()
