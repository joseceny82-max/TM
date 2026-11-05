"""Rellena campos vacíos de plantilla_tm.csv con datos ES generados (DNI con letra,
tarjeta Luhn válida, dirección/ciudad/zip coherentes). NO sobrescribe celdas con datos."""
import sys, csv, random, string, secrets, shutil, os
from datetime import date
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\user\TM\proyecto')

CSV = 'plantilla_tm.csv'
shutil.copy(CSV, CSV + '.bak.before_fill')

NOMBRES_M = ['Carlos','Jose','Antonio','Manuel','Francisco','Juan','David','Javier','Daniel','Pedro','Pablo','Alberto','Alejandro','Miguel','Diego','Luis','Jorge','Sergio','Adrian','Ruben','Ivan','Raul','Oscar','Andres','Roberto','Angel','Fernando','Mario','Marcos','Hugo','Alvaro','Victor','Rafael','Eduardo','Ignacio','Enrique','Tomas','Vicente','Gonzalo','Cristian']
NOMBRES_F = ['Maria','Carmen','Ana','Isabel','Laura','Cristina','Elena','Marta','Sara','Paula','Lucia','Andrea','Patricia','Sofia','Beatriz','Nuria','Silvia','Rocio','Eva','Pilar','Raquel','Monica','Natalia','Ines','Alba','Irene','Clara','Sandra','Julia','Claudia','Marina','Esther','Angela','Daniela','Nerea','Olga','Teresa','Yolanda','Veronica','Lourdes']
APELLIDOS = ['Garcia','Martinez','Lopez','Gonzalez','Rodriguez','Perez','Sanchez','Fernandez','Gomez','Diaz','Hernandez','Romero','Alvarez','Ramirez','Torres','Flores','Vazquez','Ramos','Reyes','Cruz','Morales','Ortiz','Gutierrez','Castro','Castillo','Mendoza','Rojas','Soto','Vargas','Vega','Aguilar','Sandoval','Salazar','Dominguez','Iglesias','Suarez','Molina','Ortega','Delgado','Gil','Marin','Nunez','Cortes','Mendez','Cabrera','Serrano','Cano','Prieto','Mora','Lozano']

CIUDADES = [
    ('Madrid','Madrid','28'),('Barcelona','Barcelona','08'),('Valencia','Valencia','46'),
    ('Sevilla','Sevilla','41'),('Zaragoza','Zaragoza','50'),('Malaga','Malaga','29'),
    ('Murcia','Murcia','30'),('Palma','Baleares','07'),('Bilbao','Vizcaya','48'),
    ('Alicante','Alicante','03'),('Cordoba','Cordoba','14'),('Valladolid','Valladolid','47'),
    ('Vigo','Pontevedra','36'),('Granada','Granada','18'),('A Coruna','A Coruna','15'),
    ('Pamplona','Navarra','31'),('Toledo','Toledo','45'),('Salamanca','Salamanca','37'),
    ('Cadiz','Cadiz','11'),('Albacete','Albacete','02'),('Santander','Cantabria','39'),
    ('Logrono','La Rioja','26'),('Burgos','Burgos','09'),('Caceres','Caceres','10'),
    ('Tarragona','Tarragona','43'),('Leon','Leon','24'),('Almeria','Almeria','04'),('Ourense','Ourense','32')
]
CALLES = ['Calle Mayor','Avenida de la Constitucion','Calle Real','Plaza de Espana','Calle Cervantes','Avenida del Mar','Calle del Sol','Avenida de Europa','Calle San Juan','Paseo de Gracia','Calle Goya','Avenida de la Paz','Calle del Carmen','Calle Nueva','Calle Velazquez','Calle de Alcala','Avenida del Puerto','Calle Alfonso X','Calle Princesa','Calle de la Luna','Plaza Mayor','Avenida de Andalucia','Calle Triana','Carrer de Balmes','Carrer del Comte','Rua do Principe']

DNI_LETRAS = "TRWAGMYFPDXBNJZSQVHLCKE"

def gen_dni():
    n = random.randint(10000000, 99999999)
    return f'{n}{DNI_LETRAS[n % 23]}'

def gen_password(n=16):
    syms = '@#!$&%*?='
    chars = string.ascii_letters + string.digits + syms
    while True:
        p = ''.join(secrets.choice(chars) for _ in range(n))
        # TM exige: mayús + minús + dígito + símbolo + len ≥ 12
        if (any(c.isupper() for c in p) and any(c.islower() for c in p)
            and any(c.isdigit() for c in p) and any(c in syms for c in p)):
            return p

def gen_phone():
    return '+346' + str(random.randint(10, 99)) + ''.join(str(random.randint(0,9)) for _ in range(6))

def luhn_checksum(card):
    """Calcula el check digit Luhn para `card` (dígitos sin el check final).

    Recorre derecha→izquierda; en posiciones 0-indexed PARES desde la derecha
    duplica el dígito (porque al añadir el check, esos quedarán en posiciones
    impares 1-indexed desde el final, que es lo que dobla Luhn).
    """
    digs = [int(d) for d in card]
    total = 0
    for i, d in enumerate(digs[::-1]):
        if i % 2 == 0:
            x = d * 2
            total += x if x < 10 else x - 9
        else:
            total += d
    return (10 - (total % 10)) % 10

def gen_card():
    bins = ['4548','4093','4978','5469','5226']
    bin_ = random.choice(bins)
    body = bin_ + ''.join(str(random.randint(0,9)) for _ in range(11))
    body += str(luhn_checksum(body))
    return body

def gen_dob():
    today = date.today()
    age = random.randint(25, 55)
    year = today.year - age
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f'{year:04d}-{month:02d}-{day:02d}'

def gen_address():
    return f'{random.choice(CALLES)} {random.randint(1,250)}'


with open(CSV, encoding='utf-8-sig') as f:
    rows = list(csv.reader(f))
header = rows[0]
data = rows[1:]
print(f'Total filas: {len(data)}')
H = {name: i for i, name in enumerate(header)}

filled = {col: 0 for col in header}

for ri, row in enumerate(data):
    while len(row) < len(header): row.append('')

    if not row[H['N_Usuario']].strip():
        row[H['N_Usuario']] = str(ri + 1)
        filled['N_Usuario'] += 1

    if not row[H['Password_TM']].strip():
        row[H['Password_TM']] = gen_password(16)
        filled['Password_TM'] += 1

    if not row[H['Nombre']].strip():
        gender = random.choice(['M','F'])
        row[H['Nombre']] = random.choice(NOMBRES_M if gender == 'M' else NOMBRES_F)
        filled['Nombre'] += 1
    if not row[H['Apellidos']].strip():
        row[H['Apellidos']] = f'{random.choice(APELLIDOS)} {random.choice(APELLIDOS)}'
        filled['Apellidos'] += 1

    if not row[H['Fecha_Nacimiento']].strip():
        row[H['Fecha_Nacimiento']] = gen_dob()
        filled['Fecha_Nacimiento'] += 1

    if not row[H['Nacionalidad']].strip():
        row[H['Nacionalidad']] = 'ES'
        filled['Nacionalidad'] += 1
    if not row[H['Pais']].strip():
        row[H['Pais']] = 'Spain'
        filled['Pais'] += 1

    if not row[H['Pasaporte']].strip():
        row[H['Pasaporte']] = gen_dni()
        filled['Pasaporte'] += 1

    if not row[H['Telefono']].strip():
        row[H['Telefono']] = gen_phone()
        filled['Telefono'] += 1

    ciudad_data = random.choice(CIUDADES)
    if not row[H['Direccion']].strip():
        row[H['Direccion']] = gen_address()
        filled['Direccion'] += 1
    if not row[H['Ciudad']].strip():
        row[H['Ciudad']] = ciudad_data[0]
        filled['Ciudad'] += 1
    if not row[H['Estado']].strip():
        row[H['Estado']] = ciudad_data[1]
        filled['Estado'] += 1
    if not row[H['ZIP']].strip():
        zip_code = ciudad_data[2] + str(random.randint(0,9)) + str(random.randint(0,99)).zfill(2)
        row[H['ZIP']] = zip_code
        filled['ZIP'] += 1

    if not row[H['Numero_Tarjeta']].strip():
        row[H['Numero_Tarjeta']] = gen_card()
        filled['Numero_Tarjeta'] += 1
    if not row[H['Titular']].strip():
        row[H['Titular']] = f"{row[H['Nombre']]} {row[H['Apellidos']]}".strip().upper()
        filled['Titular'] += 1
    if not row[H['Mes_Exp']].strip():
        row[H['Mes_Exp']] = str(random.randint(1,12)).zfill(2)
        filled['Mes_Exp'] += 1
    if not row[H['Anno_Exp']].strip():
        row[H['Anno_Exp']] = str(random.randint(27,30))
        filled['Anno_Exp'] += 1
    if not row[H['CVV']].strip():
        row[H['CVV']] = str(random.randint(100,999))
        filled['CVV'] += 1

with open(CSV, 'w', encoding='utf-8', newline='') as f:
    w = csv.writer(f)
    w.writerow(header)
    for r in data:
        w.writerow(r)

print(f'\nGenerado. Backup: {CSV}.bak.before_fill')
print(f'\nCeldas rellenadas (total filas {len(data)}):')
for col, n in filled.items():
    if n > 0:
        print(f'  {col:20} {n:4}')

print(f'\nFila #31 (primera generada):')
for ci, val in enumerate(data[30]):
    print(f'  {header[ci]:18} = {val[:60]}')
print(f'\nFila #200:')
for ci, val in enumerate(data[199]):
    print(f'  {header[ci]:18} = {val[:60]}')
print(f'\nFila #500:')
for ci, val in enumerate(data[499]):
    print(f'  {header[ci]:18} = {val[:60]}')
