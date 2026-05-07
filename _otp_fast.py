"""OTP express: pide nuevo código, polling IMAP cada 2s, pega y confirma.
Corrige el bug de timezone del polling original (compara UTC-aware).
Uso: python _otp_fast.py [PORT] [ALIAS] [MADRE] [PWD]
Defaults: PORT=57448 ALIAS=coping.fiesta3h@icloud.com MADRE=johnlockperdidos@icloud.com
"""
import sys, time, re, requests, random, socket
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')

PORT  = int(sys.argv[1]) if len(sys.argv) > 1 else 57448
ALIAS = sys.argv[2] if len(sys.argv) > 2 else 'coping.fiesta3h@icloud.com'
MADRE = sys.argv[3] if len(sys.argv) > 3 else 'johnlockperdidos@icloud.com'
PWD   = sys.argv[4] if len(sys.argv) > 4 else 'jzny-tfyh-qtuq-feou'

print(f'═══ OTP FAST — alias={ALIAS} madre={MADRE} port={PORT} ═══')

# ── 1. Conectar al browser ──
v = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=8).json()
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
ctx = b.contexts[0]
page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.bring_to_front()
print(f'URL: {page.url[:100]}')

# ── 2. Click "Verificar mi correo electrónico" / "Verificar" / "Enviar código" para emitir nuevo OTP ──
sent_at = datetime.now(timezone.utc)
clicked = page.evaluate(r"""() => {
  const btns = Array.from(document.querySelectorAll('button'));
  // Prioridad: botón que dispara envío (no el de confirmar)
  const send = btns.find(b => /verificar mi correo|enviar c[oó]digo|^verificar$|send code/i.test((b.innerText||'').trim()) && !b.disabled);
  if (send) { send.click(); return send.innerText.trim(); }
  return null;
}""")
print(f'  click envío: {clicked or "NO encontrado"}')
page.wait_for_timeout(2500)
sent_at = datetime.now(timezone.utc)
print(f'  sent_at(utc)={sent_at.isoformat()}')

# ── 3. Polling IMAP rápido (cada 2s) buscando OTP destinado al alias ──
from imap_tools import MailBox
socket.setdefaulttimeout(20)
deadline = time.time() + 180   # 3 minutos
seen_uids = set()
otp = None
poll_n = 0
while time.time() < deadline and not otp:
    poll_n += 1
    try:
        with MailBox('imap.mail.me.com').login(MADRE, PWD) as mb:
            for m in mb.fetch(reverse=True, limit=10, mark_seen=False, bulk=False):
                if m.uid in seen_uids: continue
                seen_uids.add(m.uid)
                # ---- normalizar fecha a UTC-aware ----
                msg_dt = m.date if m.date else datetime.now(timezone.utc)
                if msg_dt.tzinfo is None:
                    msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                else:
                    msg_dt = msg_dt.astimezone(timezone.utc)
                # ---- filtros: dirigido al alias + reciente + remitente TM ----
                to_low = ', '.join(m.to or []).lower()
                if ALIAS.lower() not in to_low: continue
                fr_low = (m.from_ or '').lower()
                subj_low = (m.subject or '').lower()
                is_tm = 'ticketmaster' in fr_low or 'ticketmaster' in subj_low or 'ticketmaster' in (m.from_ or '').lower()
                if not is_tm: continue
                # SOLO emails recibidos DESPUÉS del click "Reenviar" (sent_at)
                if msg_dt < sent_at:
                    continue
                age_s = (datetime.now(timezone.utc) - msg_dt).total_seconds()
                # ---- buscar 6 dígitos ----
                hay = (m.subject or '') + ' ' + (m.text or m.html or '')[:1500]
                # Patron prioritario: cerca de "código"
                m1 = re.search(r'(?:c[oó]digo|verificaci[oó]n|verification|otp)[^0-9]{0,40}?\b(\d{6})\b', hay, re.I)
                if m1:
                    otp = m1.group(1)
                else:
                    m2 = re.search(r'\b(\d{6})\b', hay)
                    if m2: otp = m2.group(1)
                if otp:
                    print(f'  📧 OTP={otp} (uid={m.uid} age={age_s:.0f}s subj="{(m.subject or "")[:50]}")')
                    break
    except Exception as e:
        print(f'  imap err: {e}')
    if otp: break
    if poll_n % 5 == 0:
        print(f'  …polling t+{int(time.time()-(deadline-180))}s sin OTP nuevo')
    time.sleep(2)

if not otp:
    print('❌ OTP no llegó en 180s')
    b.close(); pw.stop(); sys.exit(2)

# ── 4. Pegar en input correcto ──
# Localizar primero input one-time-code; si no, primer input visible en el modal/diálogo de OTP
inp = None
for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]', 'input[type="tel"]', 'input[type="text"]']:
    try:
        cand = page.locator(sel).first
        if cand.is_visible(timeout=1500):
            inp = cand
            print(f'  ✓ input via {sel}')
            break
    except Exception: pass

if inp is None:
    # Quizá el form de OTP no está aún visible — esperar y reintentar 5 veces
    for _ in range(5):
        page.wait_for_timeout(2000)
        for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]', 'input[type="tel"]']:
            try:
                cand = page.locator(sel).first
                if cand.is_visible(timeout=1000): inp = cand; print(f'  ✓ input retry via {sel}'); break
            except: pass
        if inp: break

if inp is None:
    print('❌ no encuentro el input de OTP'); page.screenshot(path=r'datos\buy\register_u1\99_no_input.png')
    b.close(); pw.stop(); sys.exit(3)

inp.click(); inp.fill('')
for ch in otp:
    inp.type(ch, delay=random.randint(60,110))
print(f'  ✓ OTP {otp} tipeado')
page.wait_for_timeout(1000)

# ── 5. Click confirmar ──
# Solo el botón de CONFIRMAR (no el de re-enviar). Buscar etiquetas exactas.
clicked = page.evaluate(r"""() => {
  const btns = Array.from(document.querySelectorAll('button'));
  const exact = btns.find(b => /^(confirmar c[oó]digo|confirmar|verify|verificar c[oó]digo|confirm)$/i.test((b.innerText||'').trim()) && !b.disabled);
  if (exact) { exact.click(); return exact.innerText.trim(); }
  // fallback: cualquier botón con "confirmar" pero NO "verificar mi correo"
  const any = btns.find(b => /confirmar/i.test((b.innerText||'').trim()) && !/verificar mi correo/i.test((b.innerText||'')) && !b.disabled);
  if (any) { any.click(); return any.innerText.trim(); }
  return null;
}""")
print(f'  ✓ click confirmar: {clicked or "NO encontrado"}')
page.wait_for_timeout(6000)

import os
os.makedirs(r'datos\buy\register_u1', exist_ok=True)
page.screenshot(path=r'datos\buy\register_u1\12_post_otp_confirm.png')
print(f'\nURL final: {page.url[:140]}')
print(f'Title:     {page.title()}')
print(f'BODY 400c: {page.evaluate("()=>document.body.innerText.slice(0,400)")}')

b.close(); pw.stop()
print('\n--- BROWSER SIGUE ABIERTO ---')
