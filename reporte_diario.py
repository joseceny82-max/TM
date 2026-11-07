"""reporte_diario.py — Registros de cuentas TM por día.

Uso:
  python reporte_diario.py              # resumen de todos los días
  python reporte_diario.py 2026-11-07   # detalle de un día concreto
"""
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, 'datos', 'cuentas', 'accounts.json')


def main() -> int:
    only_day = sys.argv[1] if len(sys.argv) > 1 else None
    with open(PATH, encoding='utf-8') as f:
        accs = json.load(f).get('accounts', [])
    by_day = collections.defaultdict(list)
    for a in accs:
        day = str(a.get('created_at', ''))[:10]
        if day:
            by_day[day].append(a)
    days = sorted(by_day, reverse=True)
    if only_day:
        days = [d for d in days if d == only_day]
        if not days:
            print(f'sin registros para {only_day}')
            return 1
    total = len(accs)
    print(f'=== REGISTROS TM — total {total} cuentas ===')
    for d in days:
        rows = sorted(by_day[d], key=lambda a: a.get('created_at') or '')
        print(f'\n{d}: {len(rows)} registro(s)')
        for a in rows:
            print(f"  {str(a.get('created_at'))[11:19]}  {(a.get('tm_email') or '?')[:38]:40} "
                  f"tlf {a.get('phone') or a.get('phone_full') or '?'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
