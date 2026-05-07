import re
with open('templates/index.html', encoding='utf-8') as f:
    html = f.read()

# Extraer SOLO los bloques <script>...</script>
js_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
print(f'Bloques script: {len(js_blocks)}')

main_js = '\n'.join(js_blocks)
print(f'Tamaño JS total: {len(main_js)} chars')

# Buscar patrones rotos comunes
import sys
for ln_no, line in enumerate(main_js.split('\n'), 1):
    # template literals abiertos sin cerrar en una línea
    bt = line.count('`')
    if bt % 2 != 0:
        # buscar contexto: ¿es un comentario? ignora
        l = line.strip()
        if l.startswith('//') or l.startswith('*'):
            continue
        # check si la línea completa no abre/cierra balanceado
        # (puede ser template multilinea legítimo, no error)
        pass

# Buscar funciones declaradas y verificar count
funcs_qm = re.findall(r'function\s+(qm\w+)\s*\(', main_js)
funcs_load = re.findall(r'function\s+(loadQueue\w+)\s*\(', main_js)
print(f'qm* funcs: {len(funcs_qm)}')
print(f'loadQueue* funcs: {len(funcs_load)}')

# Buscar "qmEnqueue" y derivados — verificar definiciones
expected = ['qmEnqueue', 'qmEnqueueCustom', 'qmAbandon', 'qmRejoin',
            'qmInjectToBot', 'qmDaemon', 'qmClearCompleted', 'qmSwarmBroadcast',
            'qmSniperMode', 'qmLoadPresets', 'qmSavePreset', 'qmApplyPreset',
            'qmLaunchPreset', 'qmDeletePreset', 'qmInspectCookie', 'qmExportCsv',
            'qmLoadLogs', 'qmPlaySound']
for f in expected:
    found = f in main_js
    if not found:
        print(f'  FALTA: {f}')

# Check para errores comunes: ${} sin cerrar, () sin cerrar
unclosed_template = main_js.count('${') - main_js.count('}')
if abs(unclosed_template) > 100:  # tolerancia, JS tiene muchas {}
    print(f'WARN: ${{}} unbalanced (delta={unclosed_template})')
