"""Importa filas 1-5 de plantilla_tm.csv directamente a account_manager
como cuentas activas (ya registradas en TM, lista para comprar).
Idempotente: salta cuentas con tm_email ya existente."""
from __future__ import annotations
import csv, os, re
import account_manager as am

CSV_PATH = os.path.join(os.path.dirname(__file__), "plantilla_tm.csv")

def split_phone(phone):
    if not phone: return ('', '')
    p = str(phone).strip()
    m = re.match(r'^(\+\d{1,3})(.+)$', p)
    if m: return (m.group(1), re.sub(r'\D', '', m.group(2)))
    digits = re.sub(r'\D', '', p)
    if digits.startswith('34') and len(digits) > 9: return ('+34', digits[2:])
    return ('', digits)

def row_to_account(r):
    pp, num = split_phone(r['Telefono'])
    return {
        'label': f"User-{r['N_Usuario']} ({r['Email_TM']})",
        'status': 'active',
        'tm_email': r['Email_TM'].strip(),
        'tm_password': r['Password_TM'].strip(),
        'contact_email': r['Email_Contacto'].strip(),
        'contact_password': r['Password_Email'].strip(),
        'first_name': r['Nombre'].strip(),
        'last_name': r['Apellidos'].strip(),
        'dob': r['Fecha_Nacimiento'].strip(),
        'gender': '',
        'nationality': r['Nacionalidad'].strip() or 'ES',
        'passport': r['Pasaporte'].strip(),
        'passport_country': r['Nacionalidad'].strip() or 'ES',
        'phone_prefix': pp,
        'phone': num,
        'address1': r['Direccion'].strip(),
        'address2': '',
        'city': r['Ciudad'].strip(),
        'state': r['Estado'].strip(),
        'zip': r['ZIP'].strip(),
        'country': r['Pais'].strip() or 'Spain',
        'notes': r['Notas'].strip() or 'Importada manual — ya registrada en TM',
        'tm_registered': True,
    }, {
        'label': f"Tarjeta User-{r['N_Usuario']}",
        'holder': r['Titular'].strip(),
        'number': r['Numero_Tarjeta'].strip(),
        'month': str(r['Mes_Exp']).strip().zfill(2),
        'year': str(r['Anno_Exp']).strip(),
        'cvv': str(r['CVV']).strip(),
    }

def main():
    with open(CSV_PATH, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    targets = [r for r in rows if r.get('N_Usuario','').strip() in {'1','2','3','4','5'}]
    print(f"Filas a importar: {len(targets)}")
    existing = {(a.get('tm_email') or '').strip().lower() for a in am.account_manager.list_accounts()}
    added, skipped = [], []
    for r in targets:
        acc_data, card_data = row_to_account(r)
        if acc_data['tm_email'].lower() in existing:
            skipped.append({'n': r['N_Usuario'], 'email': acc_data['tm_email'], 'reason': 'already_exists'})
            continue
        acc = am.account_manager.add_account(acc_data)
        card = am.account_manager.add_card({**card_data, 'account_id': acc['id']})
        added.append({'n': r['N_Usuario'], 'email': acc['tm_email'], 'account_id': acc['id'], 'card_id': card['id']})
    print(f"\n=== RESULTADO ===")
    print(f"Añadidas: {len(added)}")
    for a in added: print(f"  + N{a['n']} {a['email']} → acc={a['account_id']} card={a['card_id']}")
    if skipped:
        print(f"Omitidas: {len(skipped)}")
        for s in skipped: print(f"  - N{s['n']} {s['email']} ({s['reason']})")
    return {'added': len(added), 'skipped': len(skipped), 'detail': {'added': added, 'skipped': skipped}}

if __name__ == '__main__':
    import json
    r = main()
    print('\n' + json.dumps(r, indent=2, ensure_ascii=False))
