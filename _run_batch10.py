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


def _account_created_in_window(started_at: float, exclude: set = frozenset()) -> tuple:
    """Busca cuenta creada en accounts.json con created_at >= started_at - 5s.
    Devuelve (email, ok_bool) si encuentra una verificada al 16/16, sino (None, False).

    exclude: emails ya contados por otro slot — se saltan y se sigue buscando
    candidatos más antiguos en la ventana. Sin esto, cuando dos procesos
    terminan en el mismo ciclo de poll, el primer slot reivindica la cuenta
    más nueva aunque sea del otro proceso, y el segundo slot recibe FAIL
    pese a haber persistido una cuenta real (falso FAIL + refill innecesario).

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
            if not em or em in exclude: continue
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


def _auto_update():
    """git pull de main antes de arrancar — el sistema siempre corre la versión
    nueva si hay cambios subidos. Si el pull falla (sin red, conflicto, archivos
    sucios), se lanza igual con el código local y se avisa en el log."""
    try:
        r = subprocess.run(['git', 'fetch', 'origin', 'main'],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log(f'  ⚠ auto-update fetch falló (sigo con versión local): {r.stderr.strip()[:100]}')
            return
        behind = subprocess.run(['git', 'rev-list', '--count', 'HEAD..origin/main'],
                                capture_output=True, text=True, timeout=15)
        n = int((behind.stdout or '0').strip() or 0)
        if n == 0:
            return
        log(f'  ⬇ auto-update: {n} commit(s) nuevos en main — actualizando...')
        dirty = subprocess.run(['git', 'status', '--porcelain', 'register_account.py',
                                'sms_receiver.py', '_run_batch10.py', 'silent_engine.py'],
                               capture_output=True, text=True, timeout=15)
        if dirty.stdout.strip():
            log('  ⚠ auto-update: archivos clave con cambios locales sin commit — '
                'NO actualizo (haz commit primero). Sigo con versión local.')
            return
        up = subprocess.run(['git', 'merge', '--ff-only', 'origin/main'],
                            capture_output=True, text=True, timeout=60)
        if up.returncode == 0:
            log(f'  ✓ auto-update OK: {up.stdout.strip().splitlines()[-1] if up.stdout.strip() else "fast-forward"}')
        else:
            log(f'  ⚠ auto-update merge falló (sigo con versión local): {up.stderr.strip()[:100]}')
    except Exception as e:
        log(f'  ⚠ auto-update err (no fatal): {e}')


def _cleanup_silent_profiles():
    """Borra perfiles TM-* de Silent con más de 6h (los abiertos/in-flight son
    recientes y se conservan). Evita la acumulación de miles de perfiles que
    inestabilizaba Silent y provocaba cierres aleatorios de navegador."""
    try:
        if os.environ.get('TM_ENGINE', 'silent') != 'silent':
            return
        import silent_engine as se
        import requests as _rq
        from datetime import datetime, timezone, timedelta
        h = {'Authorization': 'Bearer ' + se._token()}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        deleted = 0
        for page in range(1, 31):
            d = se._get(f'/v1/profiles?page={page}&limit=100')
            items = d if isinstance(d, list) else (d.get('items') or d.get('data') or [])
            if not items:
                break
            for p in items:
                name = str(p.get('name') or '')
                ca = p.get('createdAt') or ''
                if not name.startswith('TM-'):
                    continue
                # Cerrar ventana del perfil si sigue abierta (batches matados
                # dejan navegadores huérfanos acumulados; close es idempotente).
                try:
                    _rq.post(f'{se.SILENT_API}/v1/profiles/{p["id"]}/close', headers=h, timeout=10)
                except Exception:
                    pass
                try:
                    created = datetime.fromisoformat(ca.replace('Z', '+00:00'))
                except ValueError:
                    continue
                if created >= cutoff:
                    continue
                try:
                    r = _rq.delete(f'{se.SILENT_API}/v1/profiles/{p["id"]}', headers=h, timeout=10)
                    if r.ok and r.json().get('deleted'):
                        deleted += 1
                except Exception:
                    pass
            if len(items) < 100:
                break
        if deleted:
            log(f'  🧹 cleanup Silent: {deleted} perfiles TM antiguos borrados')
    except Exception as e:
        log(f'  ⚠ cleanup Silent err (no fatal): {e}')


def main():
    env = dict(os.environ)
    env['TM_IMAP_MADRE_PWD'] = 'jzny-tfyh-qtuq-feou'
    env['PYTHONIOENCODING'] = 'utf-8'

    _auto_update()
    _cleanup_silent_profiles()

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

    pool_exhaust_consec = 0   # fallos consecutivos por agotamiento de pool
    last_success_ts = time.time()   # se resetea con cada éxito
    POOL_EXHAUST_MAX_CONSEC = 10
    POOL_EXHAUST_MAX_MIN = 15

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
            # Se excluyen cuentas ya contadas por otros slots para evitar el
            # falso FAIL cuando dos procesos terminan en el mismo ciclo.
            real_em, ok = _account_created_in_window(slot['started_at'], completed_emails)
            if ok:
                pool_exhaust_consec = 0
                last_success_ts = time.time()
                completed_emails.add(real_em)
                successes += 1
                if real_em.lower() == slot['email'].lower():
                    log(f"     ✅ OK ({successes}/{TARGET_SUCCESSES}) · {real_em}")
                else:
                    log(f"     ✅ OK ({successes}/{TARGET_SUCCESSES}) · {real_em} (script eligió este email del pool, no fila {slot['row']})")
            else:
                failures.append(slot['email'])
                log(f"     ❌ FAIL · {slot['email']} · refill nueva fila")
                # ¿Fallo por agotamiento del pool de perfiles/proxies?
                exhaust = False
                try:
                    with open(slot['log_path'], encoding='utf-8', errors='ignore') as _lf:
                        _tail = _lf.read()[-8000:].lower()
                    exhaust = ('no hay ningún perfil disponible' in _tail
                               or 'sin usar en silent: 0' in _tail)
                except Exception:
                    pass
                if exhaust:
                    pool_exhaust_consec += 1
                    log(f"     ⚠ pool agotado ({pool_exhaust_consec} consecutivos)")
                else:
                    pool_exhaust_consec = 0
                # Refill: añadir 1 nueva fila al buffer
                more = fresh_rows(queued_emails, want=1)
                if more:
                    pending.append(more[0])
                    log(f"     ↻ refill candidata: fila {more[0][0]} {more[0][1][:30]}")
                # Abortar si el pool está agotado: 10 fallos consecutivos o
                # 15 min sin ningún éxito nuevo.
                if pool_exhaust_consec >= POOL_EXHAUST_MAX_CONSEC \
                        or time.time() - last_success_ts > POOL_EXHAUST_MAX_MIN * 60:
                    log("")
                    log("═══ ABORT: pool de perfiles agotado ═══")
                    log(f"  ({pool_exhaust_consec} fallos consecutivos por agotamiento · "
                        f"{int((time.time() - last_success_ts) / 60)} min sin éxito)")
                    in_flight.clear()
                    pending.clear()
                    break

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
