import psutil, time, subprocess, sys, os
killed = []
for p in psutil.process_iter(['pid','cmdline']):
    try:
        cl = ' '.join(p.info['cmdline'] or [])
        if cl.endswith('dashboard.py') or 'dashboard.py' in cl and 'python' in cl.lower():
            p.terminate()
            killed.append(p.info['pid'])
    except: pass
print(f'Killed: {killed}')
time.sleep(2)
# Force kill si queda
for p in psutil.process_iter(['pid','cmdline']):
    try:
        cl = ' '.join(p.info['cmdline'] or [])
        if 'dashboard.py' in cl and 'python' in cl.lower():
            p.kill()
    except: pass
time.sleep(2)
# Verificar matados
mine = []
for p in psutil.process_iter(['pid','cmdline']):
    try:
        cl = ' '.join(p.info['cmdline'] or [])
        if 'dashboard.py' in cl and 'python' in cl.lower():
            mine.append(p.info['pid'])
    except: pass
print(f'Tras kill, vivos: {mine}')
