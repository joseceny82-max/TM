"""Lanza 3 bots MoreLogin en PARALELO con configuraciones distintas para ver el flujo en directo.
Cada thread arranca un perfil ES nuevo, ejecuta el orquestador, y DEJA EL BROWSER ABIERTO.
"""
import sys, threading, time, json, os
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

import bot_orchestrator as bo

EVENT_URL = 'https://www.ticketmaster.es/event/el-ultimo-de-la-fila-entradas/2059118961'

# Tres bots con ofertas distintas (los 3 que tenemos)
TASKS = [
    {'name': 'BOT-1 Pista',         'qty': 4, 'prefer': [0]},
    {'name': 'BOT-2 Reservado',     'qty': 4, 'prefer': [1]},
    {'name': 'BOT-3 Vis.Reducida',  'qty': 4, 'prefer': [2]},
]

results = {}
results_lock = threading.Lock()

def run_one(task):
    name = task['name']
    print(f'  [{name}] iniciando...', flush=True)
    def emit(stage, data):
        try:
            payload = {k:v for k,v in data.items() if k!='debug'} if isinstance(data,dict) else data
            print(f'  [{name}] {stage}: {str(payload)[:140]}', flush=True)
        except Exception:
            pass
    try:
        res = bo.run_purchase_flow(
            url=EVENT_URL,
            quantity=task['qty'],
            selection_mode='quick',
            prefer_offer_indices=task['prefer'],
            queue_max_minutes=2,
            broadcast=emit,
            keep_browser_open=True,  # NO cerrar — quiero verlo
        )
    except Exception as e:
        res = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    with results_lock:
        results[name] = res
    pretty = {k:v for k,v in res.items() if k not in ('debug','selection')}
    print(f'  [{name}] FIN: {pretty}', flush=True)


def main():
    print('═══════════════════════════════════════════════════════════')
    print(' BATERÍA PARALELA — 3 bots MoreLogin · El Último de la Fila')
    print('═══════════════════════════════════════════════════════════')
    print()
    threads = []
    for t in TASKS:
        th = threading.Thread(target=run_one, args=(t,), name=t['name'], daemon=False)
        th.start()
        threads.append(th)
        time.sleep(3)  # spread arranque para no saturar MoreLogin API
    for th in threads:
        th.join(timeout=600)

    print()
    print('═══════ RESUMEN FINAL ═══════')
    for name, r in results.items():
        sel = r.get('selection') or {}
        print(f'  {name}:')
        print(f'    stage={r.get("stage")}  ok={r.get("ok")}')
        print(f'    selection: ok={sel.get("ok")} qty={sel.get("applied_qty")} '
              f'offer={sel.get("offer_name") or sel.get("section_clicked")}')
        prof = r.get("debug",{}).get("profile") or '-'
        port = r.get("debug",{}).get("port") or '-'
        kept = r.get("debug",{}).get("browser_kept_open") or False
        print(f'    profile={prof} port={port} kept_open={kept}')

    # Persist resumen
    with open('_battery_parallel_result.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    print()
    print('Browsers ABIERTOS — los puedes ver en MoreLogin.')
    print('Para cerrarlos, usa MoreLogin → Stop o ejecuta env/close manual.')

if __name__ == '__main__':
    main()
