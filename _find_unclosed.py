import re
with open('templates/index.html', encoding='utf-8') as f:
    html = f.read()

# Buscar <select> sin </select> próximo
# Estrategia: track stack de selects y reportar líneas problemáticas

lines = html.split('\n')
stack = []
problems_select = []
for ln, line in enumerate(lines, 1):
    for m in re.finditer(r'<(/?)(select)[\s>]', line, re.I):
        is_close = m.group(1) == '/'
        if is_close:
            if stack:
                stack.pop()
            else:
                problems_select.append((ln, 'orphan </select>', line.strip()[:120]))
        else:
            stack.append((ln, line.strip()[:120]))

if stack:
    for ln, text in stack:
        problems_select.append((ln, 'unclosed <select>', text))

print(f'Problemas <select>: {len(problems_select)}')
for p in problems_select[:10]:
    print(f'  line {p[0]}: {p[1]}')
    print(f'    {p[2]}')

# div problems — too many to track all, just check filas section
print()
# extract sec-filas to /sec-filas
m = re.search(r'<div class="section" id="sec-filas".*?<!-- ═+ PURCHASE FLOW', html, re.DOTALL)
if m:
    fs = m.group(0)
    fs_div_o = len(re.findall(r'<div[\s>]', fs))
    fs_div_c = len(re.findall(r'</div>', fs))
    print(f'sec-filas: <div>={fs_div_o} </div>={fs_div_c} diff={fs_div_o-fs_div_c}')
    fs_sel_o = len(re.findall(r'<select[\s>]', fs, re.I))
    fs_sel_c = len(re.findall(r'</select>', fs, re.I))
    print(f'sec-filas: <select>={fs_sel_o} </select>={fs_sel_c} diff={fs_sel_o-fs_sel_c}')
    fs_span_o = len(re.findall(r'<span[\s>]', fs, re.I))
    fs_span_c = len(re.findall(r'</span>', fs, re.I))
    print(f'sec-filas: <span>={fs_span_o} </span>={fs_span_c} diff={fs_span_o-fs_span_c}')
    fs_det_o = len(re.findall(r'<details[\s>]', fs, re.I))
    fs_det_c = len(re.findall(r'</details>', fs, re.I))
    print(f'sec-filas: <details>={fs_det_o} </details>={fs_det_c} diff={fs_det_o-fs_det_c}')
