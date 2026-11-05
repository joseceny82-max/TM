import psutil, time, subprocess, sys, os
killed = []
for p in psutil.process_iter(['pid','cmdline']):
    try:
        cl = ' '.join(p.info['cmdline'] or [])
        if 'dashboard.py' in cl and ('python' in cl.lower() or '.exe' in cl.lower()):
            p.kill()
            killed.append(p.info['pid'])
    except: pass
print(f'Killed: {killed}')
time.sleep(4)

# Verificar que nada quedó vivo
mine = []
for p in psutil.process_iter(['pid','cmdline']):
    try:
        cl = ' '.join(p.info['cmdline'] or [])
        if 'dashboard.py' in cl:
            mine.append(p.info['pid'])
    except: pass
print(f'Antes de arrancar, vivos: {mine}')
