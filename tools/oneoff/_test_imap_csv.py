"""Verifica IMAP de cada par (madre, app-pwd) único en plantilla_tm.csv."""
import csv, sys, socket
from collections import defaultdict

try:
    from imap_tools import MailBox
except ImportError:
    print("ERROR: imap_tools no instalado")
    sys.exit(1)

CSV = r"C:\Users\user\TM\proyecto\plantilla_tm.csv"

def host_for(addr: str) -> str:
    dom = addr.split("@", 1)[1].lower()
    if dom in ("icloud.com", "me.com", "mac.com"):
        return "imap.mail.me.com"
    if dom in ("gmail.com", "googlemail.com"):
        return "imap.gmail.com"
    if dom in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
        return "outlook.office365.com"
    if dom in ("yahoo.com", "yahoo.es"):
        return "imap.mail.yahoo.com"
    return f"imap.{dom}"

pairs = defaultdict(list)
with open(CSV, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        m, p = (row.get("Email_Contacto") or "").strip(), (row.get("Password_Email") or "").strip()
        if m and p:
            pairs[(m, p)].append(row.get("N_Usuario", "?"))

print(f"[+] {len(pairs)} par(es) (madre, IMAP) unico(s) en {CSV}\n")
socket.setdefaulttimeout(20)
ok = fail = 0
for (mail, pwd), users in pairs.items():
    host = host_for(mail)
    label = f"{mail} -> {host} (usuarios: {len(users)}, ej: {users[0]}..{users[-1]})"
    try:
        with MailBox(host).login(mail, pwd) as mb:
            inbox_n = sum(1 for _ in mb.fetch(limit=1, reverse=True, mark_seen=False, bulk=False))
        print(f"  OK   {label}  | INBOX accesible")
        ok += 1
    except Exception as e:
        print(f"  FAIL {label}\n       -> {type(e).__name__}: {e}")
        fail += 1

print(f"\n=== Resultado: {ok} OK, {fail} FAIL ===")
sys.exit(0 if fail == 0 else 2)
