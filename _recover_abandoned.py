"""_recover_abandoned.py — añade los 19 abandoned a plantilla_tm.csv para recovery.

Para los que tienen datos en abandoned_archive.csv usa esos. Para los que no,
genera datos plausibles via _fill_plantilla helpers.
"""
import csv
import json
import os
import random
import string
from datetime import datetime, timedelta

PROYECTO = os.path.dirname(os.path.abspath(__file__))


def gen_password():
    """Password 16 chars con mayús+minús+núm+símbolo (validado por TM)."""
    while True:
        chars = string.ascii_letters + string.digits + "!@#$%&*?+="
        pwd = ''.join(random.choice(chars) for _ in range(16))
        if (any(c.isupper() for c in pwd) and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd)
                and any(c in "!@#$%&*?+=" for c in pwd)):
            return pwd


SP_FIRST = ['Carlos','Ana','Juan','Maria','Pedro','Lucia','Diego','Carmen',
            'Antonio','Laura','Miguel','Sofia','Pablo','Eva','Daniel','Marta',
            'Javier','Cristina','Alvaro','Patricia','Sergio','Beatriz']
SP_LAST = ['Garcia','Martinez','Lopez','Sanchez','Gonzalez','Rodriguez','Fernandez',
           'Perez','Diaz','Ruiz','Hernandez','Jimenez','Alvarez','Moreno','Munoz']
SP_CITIES = [('Madrid','Madrid','28001'),('Barcelona','Barcelona','08001'),
             ('Valencia','Valencia','46001'),('Sevilla','Sevilla','41001'),
             ('Zaragoza','Zaragoza','50001'),('Malaga','Malaga','29001'),
             ('Murcia','Murcia','30001'),('Bilbao','Bizkaia','48001')]
SP_STREETS = ['Calle Mayor','Avenida de la Constitucion','Calle Real','Plaza Mayor',
              'Calle del Carmen','Avenida del Puerto','Calle Princesa','Paseo de Gracia']


def gen_dni():
    n = ''.join(random.choices(string.digits, k=8))
    letter = "TRWAGMYFPDXBNJZSQVHLCKE"[int(n) % 23]
    return f"{n}{letter}"


def gen_phone():
    return f"+346{random.randint(10000000, 99999999)}"


def gen_card():
    bin4 = random.choice(['4548', '5469', '5226'])
    rest = ''.join(random.choices(string.digits, k=12))
    return bin4 + rest


def gen_dob():
    base = datetime(1980, 1, 1)
    days = random.randint(0, 365 * 25)
    dt = base + timedelta(days=days)
    return dt.strftime('%Y-%m-%d')


def make_row_for(email, n_user, real_data=None):
    """Construye fila plantilla para un email. Si real_data dict, usa esos campos."""
    if real_data:
        return {
            'N_Usuario': str(n_user),
            'Email_TM': email,
            'Password_TM': real_data.get('Password_TM') or gen_password(),
            'Email_Contacto': real_data.get('Email_Contacto') or 'johnlockperdidos@icloud.com',
            'Password_Email': real_data.get('Password_Email') or 'jzny-tfyh-qtuq-feou',
            'Nombre': real_data.get('Nombre') or random.choice(SP_FIRST),
            'Apellidos': real_data.get('Apellidos') or f'{random.choice(SP_LAST)} {random.choice(SP_LAST)}',
            'Fecha_Nacimiento': real_data.get('Fecha_Nacimiento') or gen_dob(),
            'Nacionalidad': real_data.get('Nacionalidad') or 'ES',
            'Pasaporte': real_data.get('Pasaporte') or gen_dni(),
            'Telefono': real_data.get('Telefono') or gen_phone(),
            'Direccion': real_data.get('Direccion') or f"{random.choice(SP_STREETS)} {random.randint(1, 200)}",
            'Ciudad': real_data.get('Ciudad') or random.choice(SP_CITIES)[0],
            'Estado': real_data.get('Estado') or 'Madrid',
            'ZIP': real_data.get('ZIP') or '28001',
            'Pais': real_data.get('Pais') or 'Spain',
            'Numero_Tarjeta': real_data.get('Numero_Tarjeta') or gen_card(),
            'Titular': real_data.get('Titular') or 'TITULAR ANONIMO',
            'Mes_Exp': real_data.get('Mes_Exp') or f"{random.randint(1, 12):02d}",
            'Anno_Exp': real_data.get('Anno_Exp') or str(random.randint(27, 30)),
            'CVV': real_data.get('CVV') or f"{random.randint(100, 999)}",
            'Notas': 'recovery_abandoned',
        }
    city = random.choice(SP_CITIES)
    fn = random.choice(SP_FIRST)
    ln = f"{random.choice(SP_LAST)} {random.choice(SP_LAST)}"
    return {
        'N_Usuario': str(n_user),
        'Email_TM': email,
        'Password_TM': gen_password(),
        'Email_Contacto': 'johnlockperdidos@icloud.com',
        'Password_Email': 'jzny-tfyh-qtuq-feou',
        'Nombre': fn,
        'Apellidos': ln,
        'Fecha_Nacimiento': gen_dob(),
        'Nacionalidad': 'ES',
        'Pasaporte': gen_dni(),
        'Telefono': gen_phone(),
        'Direccion': f"{random.choice(SP_STREETS)} {random.randint(1, 200)}",
        'Ciudad': city[0],
        'Estado': city[1],
        'ZIP': city[2],
        'Pais': 'Spain',
        'Numero_Tarjeta': gen_card(),
        'Titular': f"{fn} {ln}".upper(),
        'Mes_Exp': f"{random.randint(1, 12):02d}",
        'Anno_Exp': str(random.randint(27, 30)),
        'CVV': f"{random.randint(100, 999)}",
        'Notas': 'recovery_abandoned',
    }


def main():
    # Leer abandoned_archive.csv para datos reales
    real_by_email = {}
    archive_path = os.path.join(PROYECTO, 'datos', 'buy', 'runs', 'abandoned_archive.csv')
    if os.path.exists(archive_path):
        with open(archive_path, encoding='utf-8') as f:
            for r in csv.DictReader(f):
                em = (r.get('Email_TM') or '').strip().lower()
                if em:
                    real_by_email[em] = r
    print(f'datos en abandoned_archive: {len(real_by_email)}')

    # Leer 19 abandoned emails
    with open(os.path.join(PROYECTO, '_abandoned_emails.json'), encoding='utf-8') as f:
        abandoned_emails = json.load(f)

    # Leer plantilla actual (count rows existentes). Usar utf-8-sig para BOM.
    plant_path = os.path.join(PROYECTO, 'plantilla_tm.csv')
    with open(plant_path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        existing = list(reader)
        original_fieldnames = list(reader.fieldnames or [])
    existing_emails = {(r.get('Email_TM') or '').strip().lower() for r in existing}

    # Construir filas nuevas para los emails NO presentes en plantilla
    next_n = max([int(r.get('N_Usuario') or 0) for r in existing], default=0) + 1
    new_rows = []
    skipped = 0
    for em in abandoned_emails:
        em_low = em.lower()
        if em_low in existing_emails:
            skipped += 1
            continue
        real = real_by_email.get(em_low)
        new_row = make_row_for(em, next_n, real_data=real)
        new_rows.append(new_row)
        next_n += 1

    if not new_rows:
        print('Nada que añadir. Todos los abandoned ya están en plantilla.')
        return 0

    # Append a plantilla. Usar fieldnames del CSV existente (sin BOM)
    fieldnames = original_fieldnames or list(new_rows[0].keys())
    with open(plant_path, 'a', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not existing:
            w.writeheader()
        for r in new_rows:
            # Solo escribir keys que están en fieldnames (filtra extras)
            row_filtered = {k: r.get(k, '') for k in fieldnames}
            w.writerow(row_filtered)

    print(f'Añadidos {len(new_rows)} abandoned/flaky a plantilla_tm.csv (skip {skipped})')
    print(f'Con datos reales: {sum(1 for em in abandoned_emails if em.lower() in real_by_email)}/{len(abandoned_emails)}')
    return 0


if __name__ == '__main__':
    main()
