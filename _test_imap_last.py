"""Muestra el ultimo correo del INBOX de la madre."""
import sys, socket
from imap_tools import MailBox

EMAIL = "johnlockperdidos@icloud.com"
PWD = "jzny-tfyh-qtuq-feou"
HOST = "imap.mail.me.com"

socket.setdefaulttimeout(30)
with MailBox(HOST).login(EMAIL, PWD) as mb:
    msgs = list(mb.fetch(limit=1, reverse=True, mark_seen=False, bulk=False))
    if not msgs:
        print("INBOX vacio")
        sys.exit(0)
    m = msgs[0]
    body = (m.text or m.html or "").strip().replace("\r", "")
    print(f"Fecha   : {m.date.isoformat() if m.date else '?'}")
    print(f"De      : {m.from_}")
    print(f"Para    : {', '.join(m.to) if m.to else '?'}")
    print(f"Asunto  : {m.subject}")
    print(f"UID     : {m.uid}")
    print("--- cuerpo (primeros 800 chars) ---")
    print(body[:800])
