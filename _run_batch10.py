"""_run_batch10.py — Orquesta 10 registros en pool de 2 simultáneos.

Lanza siempre 2 register_account.py en paralelo. Cuando uno termina,
lanza el siguiente, hasta que alcance TARGET éxitos verificados.

Tras cada éxito ejecuta register_verify.verify_account() para asegurar 16/16.
"""
import csv, os, subprocess, sys, time
from datetime import datetime

PROYECTO = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROYECTO)

TARGET_SUCCESSES = 10
SLOTS = 2
LAUNCH_GRACE_S = 4   # esperar 4s entre lanzamientos para no saturar pool MoreLogin

LOG_FILE = os.path.join('logs', f'batch10_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
os.makedirs('logs', exist_ok=True)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def fresh_rows(skip_emails: set, want: int = 10):
    """Devuelve lista de tuplas (row_number, email, name)."""
    with open('plantilla_tm.csv', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    out = []
    for i, r in enumerate(rows, 1):
        em = (r.get('Email_TM') or '').strip()
        if not em or em in skip_emails: continue
        if (r.get('Notas') or '').strip(): continue
        if i < 3: continue   # filas 1, 2 son recovery_abandoned
        out.append((i, em, f"{r['Nombre']} {r['Apellidos']}"))
        if len(out) >= want: break
    return out


def verify(email: str) -> bool:
    """Llama a register_verify.verify_account(email) y devuelve ok bool."""
    try:
        import register_verify
        r = register_verify.verify_account(email=email)
        return bool(r.get('ok'))
    except Exception:
        return False


def main():
    env = dict(os.environ)
    env['TM_IMAP_MADRE_PWD'] = 'jzny-tfyh-qtuq-feou'
    env['PYTHONIOENCODING'] = 'utf-8'

    successes = 0
    failures = []
    in_flight = []   # list of {proc, row, email, name, log_path, started_at}
    queued_emails = set()
    pending = fresh_rows(set(), want=TARGET_SUCCESSES * 3)  # buffer
    log(f"Pool de {SLOTS} slots · TARGET={TARGET_SUCCESSES} éxitos · {len(pending)} candidatas en buffer")

    while successes < TARGET_SUCCESSES:
        # Llenar slots hasta SLOTS
        while len(in_flight) < SLOTS and pending and successes + len(in_flight) < TARGET_SUCCESSES:
            row, email, name = pending.pop(0)
            if email in queued_emails: continue
            queued_emails.add(email)
            log_path = os.path.join('logs', f'batch10_row{row}_{email.split("@")[0]}.log')
            try:
                proc = subprocess.Popen(
                    [sys.executable, 'register_account.py', '--row', str(row)],
                    stdout=open(log_path, 'w', encoding='utf-8'),
                    stderr=subprocess.STDOUT,
                    env=env)
                in_flight.append({
                    'proc': proc, 'row': row, 'email': email, 'name': name,
                    'log_path': log_path, 'started_at': time.time(),
                })
                log(f"  ▶ LAUNCH fila {row} · {email[:30]} · {name} · pid={proc.pid}")
                time.sleep(LAUNCH_GRACE_S)
            except Exception as e:
                log(f"  ❌ launch fail row {row}: {e}")

        # Esperar a que cualquiera termine
        if not in_flight:
            log("  ⚠ sin procesos activos y sin pending — break")
            break
        time.sleep(5)
        for slot in list(in_flight):
            rc = slot['proc'].poll()
            if rc is None: continue   # sigue corriendo
            elapsed = int(time.time() - slot['started_at'])
            in_flight.remove(slot)
            log(f"  ◀ EXIT  fila {slot['row']} · {slot['email'][:30]} · rc={rc} · {elapsed}s")
            # Verify
            if verify(slot['email']):
                successes += 1
                log(f"     ✅ OK ({successes}/{TARGET_SUCCESSES}) · {slot['email']}")
            else:
                failures.append(slot['email'])
                log(f"     ❌ FAIL · {slot['email']} · refill nueva fila")
                # Refill: añadir 1 nueva fila al buffer
                more = fresh_rows(queued_emails, want=1)
                if more:
                    pending.append(more[0])
                    log(f"     ↻ refill candidata: fila {more[0][0]} {more[0][1][:30]}")

    log("")
    log("═══ FIN ═══")
    log(f"  Éxitos:   {successes}/{TARGET_SUCCESSES}")
    log(f"  Fallos:   {len(failures)}")
    if failures:
        for em in failures:
            log(f"    - {em}")


if __name__ == '__main__':
    main()
