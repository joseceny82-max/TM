"""parallel_register.py — Lanza N registros simultáneos.

Uso:
  python parallel_register.py --count 3                    # 3 a la vez (filas 202, 203, 204)
  python parallel_register.py --count 5 --start 202        # 5 a la vez (filas 202..206)
  python parallel_register.py --rows 202,203,204           # filas específicas

Cada proceso usa perfil MoreLogin distinto (claim atómico en burned_profiles.json),
IP/proxy distinto, número 5sim distinto. Logs separados en datos/buy/runs/_par<N>.log.
"""
import argparse, os, subprocess, sys, time
sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Users\user\TM\proyecto'
LOG_DIR = os.path.join(ROOT, 'datos', 'buy', 'runs')
os.makedirs(LOG_DIR, exist_ok=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--count', type=int, default=3, help='cuántos lanzar (default 3)')
    p.add_argument('--start', type=int, default=202, help='fila inicial (default 202)')
    p.add_argument('--rows', help='filas explícitas separadas por coma (override count/start)')
    p.add_argument('--first', help='override Nombre')
    p.add_argument('--last', help='override Apellidos')
    args = p.parse_args()

    if args.rows:
        rows = [int(r.strip()) for r in args.rows.split(',') if r.strip()]
    else:
        rows = [args.start + i for i in range(args.count)]

    print(f'═══ Lanzando {len(rows)} procesos simultáneos: filas {rows} ═══\n')
    procs = []
    for i, row in enumerate(rows, 1):
        log_path = os.path.join(LOG_DIR, f'_par{i}.log')
        # Limpiar log anterior
        try: open(log_path, 'w').close()
        except Exception: pass
        cmd = [sys.executable, '-u', os.path.join(ROOT, 'register_account.py'),
               '--row', str(row)]
        if args.first: cmd += ['--first', args.first]
        if args.last:  cmd += ['--last',  args.last]
        # creationflags=DETACHED_PROCESS para que sobreviva si el padre muere
        try: from subprocess import CREATE_NEW_PROCESS_GROUP
        except Exception: CREATE_NEW_PROCESS_GROUP = 0
        with open(log_path, 'w', encoding='utf-8') as logf:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                    cwd=ROOT, creationflags=CREATE_NEW_PROCESS_GROUP)
        procs.append({'proc': proc, 'row': row, 'log': log_path, 'idx': i})
        print(f'  ▶ proc {i}/{len(rows)} → row {row} → PID {proc.pid} → {log_path}')
        # SIN offset — todos arrancan a la vez. El claim atómico en burned_profiles.json
        # garantiza que cada uno pickee perfil distinto sin race condition.

    print(f'\n═══ {len(procs)} procesos lanzados — esperando completion ═══')
    print('Tail de cada log con:  tail -f datos/buy/runs/_parN.log\n')

    # Esperar todos
    for p in procs:
        p['proc'].wait()
        rc = p['proc'].returncode
        # Leer última línea con resumen
        try:
            with open(p['log'], encoding='utf-8') as f:
                lines = [l.rstrip() for l in f if l.strip()]
            summary = next((l for l in reversed(lines) if '✅' in l or '❌' in l), lines[-1] if lines else '?')
        except Exception:
            summary = f'(no log) rc={rc}'
        print(f'  [{p["idx"]}] row {p["row"]} rc={rc}: {summary[:140]}')


if __name__ == '__main__':
    main()
