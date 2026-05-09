"""_run_batch10.py — Orquesta 10 registros en pool de 2 simultáneos.

Lanza siempre 2 register_account.py en paralelo. Cuando uno termina,
lanza el siguiente, hasta que alcance TARGET éxitos verificados.

Tras cada éxito ejecuta register_verify.verify_account() para asegurar 16/16.
"""
import csv, os, subprocess, sys, time
from datetime import datetime

PROYECTO = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROYECTO)

TARGET_SUCCESSES = int(os.environ.get('TM_TARGET', '10'))
SLOTS = int(os.environ.get('TM_SLOTS', '2'))
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
    """Devuelve lista de tuplas (row_number, email, name).
    TM_START_ROW (env, default 3) — empezar desde esa fila.
    TM_ONLY_SELECTED (env, default 0) — solo filas con Seleccionado=1 (UI checkbox).
    Filas 1-2 son recovery_abandoned por defecto."""
    start_row = int(os.environ.get('TM_START_ROW', '3'))
    only_selected = os.environ.get('TM_ONLY_SELECTED', '0').strip() in ('1', 'true', 'yes')
    with open('plantilla_tm.csv', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    out = []
    for i, r in enumerate(rows, 1):
        em = (r.get('Email_TM') or '').strip()
        if not em or em in skip_emails: continue
        if (r.get('Notas') or '').strip(): continue
        if only_selected:
            sel = (r.get('Seleccionado') or '').strip()
            if sel not in ('1', 'x', 'X', 'true'): continue
        else:
            if i < start_row: continue
        out.append((i, em, f"{r['Nombre']} {r['Apellidos']}"))
        if len(out) >= want: break
    return out


def _list_accounts():
    """Lee accounts.json y devuelve lista normalizada."""
    import json
    p = os.path.join(PROYECTO, 'datos', 'cuentas', 'accounts.json')
    if not os.path.exists(p): return []
    try:
        d = json.loads(open(p, encoding='utf-8').read())
        return d if isinstance(d, list) else d.get('accounts', [])
    except Exception:
        return []


def _account_created_in_window(started_at: float) -> tuple:
    """Busca cuenta creada en accounts.json con created_at >= started_at - 5s.
    Devuelve (email, ok_bool) si encuentra una verificada al 16/16, sino (None, False).

    Se ocupa del bug del email pool claim: el script puede registrar un email
    distinto al pasado por --row. Verificamos por timestamp.

    Fix race condition 2026-05-09: re-leer accounts.json varias veces con backoff
    porque la escritura atómica del script puede no haber terminado cuando el
    orquestador llama esta función justo tras proc.poll()."""
    import register_verify, time
    from datetime import datetime

    # Retry hasta 3 veces con backoff (cuenta puede aún estar siendo escrita)
    for retry in range(3):
        accs = _list_accounts()
        cutoff = started_at - 5
        candidates = []
        for a in accs:
            ts = a.get('created_at') or a.get('registration_completed_at') or ''
            try:
                t = datetime.fromisoformat(ts.replace('Z', '+00:00').split('+')[0])
                if t.timestamp() >= cutoff:
                    candidates.append((t.timestamp(), a))
            except Exception:
                continue
        candidates.sort(key=lambda x: x[0], reverse=True)
        for ts, a in candidates:
            em = (a.get('tm_email') or a.get('email') or '').strip()
            if not em: continue
            r = register_verify.verify_account(email=em)
            if r.get('ok'):
                return em, True
            # Si verify dice not ok pero la cuenta existe en accounts.json,
            # puede ser que el archive aún no se haya añadido (race) — retry
            if retry < 2 and 'archive_ADDED' in (r.get('failures') or []):
                break  # break for-loop interno → al while retry exterior
        else:
            # No candidates encontradas → retry no ayuda
            break
        time.sleep(2)   # esperar 2s y releer accounts.json + archive
    return None, False


def verify(email: str) -> bool:
    """Compatibilidad con verify por email; ahora solo se usa al final."""
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

    # Reap SMS órdenes stale ANTES de empezar (norma user 2026-05-09:
    # cancelar siempre tlf no usado o problemático para no acumular cargos).
    # reap_stale_smspool detecta órdenes PENDING >25 min sin actividad y las
    # cancela en el provider + verifica status final EXPIRED.
    try:
        import sms_receiver as _sr
        rep = _sr.reap_stale_smspool(min_age_min=15, max_age_min=240)
        if rep.get('reaped', 0) > 0 or rep.get('failed', 0) > 0:
            log(f"  🧹 reap_stale SMS: scanned={rep.get('scanned',0)} "
                f"reaped={rep.get('reaped',0)} failed={rep.get('failed',0)}")
    except Exception as _e:
        log(f"  ⚠ reap_stale_smspool err (no fatal): {_e}")

    successes = 0
    failures = []
    completed_emails = set()  # emails ya contados como éxito (para no duplicar entre slots)
    in_flight = []   # list of {proc, row, email, name, log_path, started_at}
    queued_emails = set()
    pending = fresh_rows(set(), want=TARGET_SUCCESSES * 3)  # buffer
    log(f"Pool de {SLOTS} slots · TARGET={TARGET_SUCCESSES} éxitos · {len(pending)} candidatas en buffer")
    log(f"  (verify por timestamp accounts.json — corrige bug email-pool-claim)")

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
            # Verify por timestamp accounts.json — el script puede registrar un
            # email distinto al --row pasado por bug del email pool claim.
            real_em, ok = _account_created_in_window(slot['started_at'])
            # Evitar contar la misma cuenta dos veces si 2 slots ven el mismo nuevo email
            if ok and real_em in completed_emails:
                ok = False
            if ok:
                completed_emails.add(real_em)
                successes += 1
                if real_em.lower() == slot['email'].lower():
                    log(f"     ✅ OK ({successes}/{TARGET_SUCCESSES}) · {real_em}")
                else:
                    log(f"     ✅ OK ({successes}/{TARGET_SUCCESSES}) · {real_em} (script eligió este email del pool, no fila {slot['row']})")
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


def self_test() -> dict:
    """Validación básica del orquestador (política proyecto, módulos >50 LOC).
    Verifica que las dependencias y funciones clave existen sin ejecutar batch.
    """
    out = {'ok': True, 'checks': {}, 'errors': []}
    try:
        for fn in ('main', 'fresh_rows', '_list_accounts', '_account_created_in_window',
                    'verify', 'log'):
            present = fn in globals() and callable(globals()[fn])
            out['checks'][f'fn:{fn}'] = present
            if not present:
                out['ok'] = False; out['errors'].append(f'fn missing: {fn}')
        # Constantes esenciales
        for c in ('TARGET_SUCCESSES', 'SLOTS', 'LAUNCH_GRACE_S', 'PROYECTO', 'LOG_FILE'):
            out['checks'][f'const:{c}'] = c in globals()
        # Imports laterales
        for mod in ('register_verify', 'sms_receiver'):
            try:
                __import__(mod); out['checks'][f'import:{mod}'] = True
            except Exception as e:
                out['checks'][f'import:{mod}'] = False
                out['errors'].append(f'{mod}: {e}'); out['ok'] = False
    except Exception as e:
        out['ok'] = False; out['errors'].append(str(e))
    return out


if __name__ == '__main__':
    main()
