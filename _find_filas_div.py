import re
with open('templates/index.html', encoding='utf-8') as f:
    html = f.read()

# Extraer sec-filas
m = re.search(r'(<div class="section" id="sec-filas".*?)(<!-- ═+ PURCHASE FLOW)', html, re.DOTALL)
if not m:
    print('NO match sec-filas')
    exit(1)
fs = m.group(0)
start_offset = m.start()

# Encontrar líneas de sec-filas con su offset
lines = html.split('\n')
# Línea de inicio
acc = 0
start_line = 0
for i, ln in enumerate(lines, 1):
    if acc + len(ln) + 1 > start_offset:
        start_line = i
        break
    acc += len(ln) + 1

# Stack-based check
stack = []
issues = []
ln = start_line
for line in fs.split('\n'):
    for m in re.finditer(r'<(/?)div[\s>]', line):
        is_close = m.group(1) == '/'
        if is_close:
            if stack:
                stack.pop()
            else:
                issues.append((ln, 'orphan </div>', line.strip()[:120]))
        else:
            stack.append((ln, line.strip()[:130]))
    ln += 1

print(f'div sin cerrar tras sec-filas:')
for s in stack:
    print(f'  line {s[0]}: {s[1]}')
print(f'\norphans:')
for s in issues[:5]:
    print(f'  line {s[0]}: {s[1]}: {s[2]}')
