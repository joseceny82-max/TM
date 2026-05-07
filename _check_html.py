import re
with open('templates/index.html', encoding='utf-8') as f:
    html = f.read()

print(f'HTML size: {len(html)} bytes')
print()

# Etiquetas balanceadas
print('Etiquetas balanceadas:')
for tag in ['details', 'div', 'script', 'section', 'select', 'input', 'span', 'button', 'table']:
    o = len(re.findall(f'<{tag}[\\s>]', html, re.I))
    c = len(re.findall(f'</{tag}>', html, re.I))
    diff = o - c
    sign = ' OK' if diff == 0 else f' DIFF={diff}'
    print(f'  <{tag}>={o:5d} </{tag}>={c:5d}  {sign}')

# Buscar template literals problematicos en JS
js_match = re.search(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
if js_match:
    js = js_match.group(1)
    backticks = js.count('`')
    print(f'\nBackticks en primer script: {backticks} ({"par" if backticks%2==0 else "IMPAR!"})')

# Funciones qm*
funcs = re.findall(r'function\s+(qm\w+|loadQueue\w+)\s*\(', html)
print(f'\nFunciones Queue Manager: {len(funcs)}')
for f in funcs[:15]:
    print(f'  {f}')
