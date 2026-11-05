"""Verifica IMAP madre joaquina_polo0@icloud.com con app-pwd."""
import sys
try:
    from imap_tools import MailBox, AND
except ImportError:
    print("ERROR: imap_tools no instalado")
    sys.exit(1)

EMAIL = "joaquina_polo0@icloud.com"
PWD = "qspw-lshy-icdg-unbm"

try:
    with MailBox("imap.mail.me.com").login(EMAIL, PWD) as mb:
        folders = [f.name for f in mb.folder.list()]
        mb.folder.set("INBOX")
        msgs = list(mb.fetch(limit=5, reverse=True, mark_seen=False))
        print(f"OK login {EMAIL}")
        print(f"Folders: {folders}")
        print(f"INBOX last 5:")
        for m in msgs:
            print(f"  - {m.date.isoformat() if m.date else '?'} | {m.from_} | {m.subject[:60] if m.subject else ''}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")
    sys.exit(2)
