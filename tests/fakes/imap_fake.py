"""tests/fakes/imap_fake.py — IMAP fake server para tests sin red.

Provee:
  - FakeMailBox: drop-in replacement de imap_tools.MailBox
  - FakeIMAP4_SSL: drop-in replacement de imaplib.IMAP4_SSL
  - make_fake_message(...): factory de mensajes mockeables
  - install_fake_imap_tools(monkeypatch): contexto que inyecta el fake en imap_tools

Permite testear otp_manager.fetch_otps, otp_3ds.check_inbox, etc.
sin necesidad de IMAP server real.

Uso típico:
  from tests.fakes.imap_fake import FakeMailBox, make_fake_message

  msgs = [
      make_fake_message(
          uid="1", from_="customer_support@email.ticketmaster.com",
          subject="Verification code 482913", body="Tu código es 482913"),
  ]
  with patch("imap_tools.MailBox", FakeMailBox.with_messages(msgs)):
      r = otp_manager.fetch_otps()
      assert r["codes"][0]["code"] == "482913"
"""
from __future__ import annotations

import email
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
#  Mensaje fake compatible con imap_tools.MailMessage interface
# ═══════════════════════════════════════════════════════════════
class FakeMessage:
    """Compatible con imap_tools.MailMessage (subset usado por otp_manager)."""

    def __init__(self,
                  uid: str = "1",
                  from_: str = "noreply@ticketmaster.com",
                  to: Optional[List[str]] = None,
                  cc: Optional[List[str]] = None,
                  subject: str = "",
                  text: str = "",
                  html: str = "",
                  date: Optional[datetime] = None,
                  flags: Optional[List[str]] = None):
        self.uid = uid
        self.from_ = from_
        self.to = to or []
        self.cc = cc or []
        self.subject = subject
        self.text = text
        self.html = html
        self.date = date or datetime.now(timezone.utc)
        self.flags = flags or []
        self.headers = {}

    def __repr__(self):
        return f"<FakeMessage uid={self.uid} subj={self.subject!r}>"


def make_fake_message(uid="1", from_="noreply@ticketmaster.com",
                       to=None, subject="", body="", html="",
                       cc=None, date=None) -> FakeMessage:
    """Factory simple."""
    return FakeMessage(
        uid=str(uid), from_=from_, to=to or [], cc=cc or [],
        subject=subject, text=body, html=html, date=date)


# ═══════════════════════════════════════════════════════════════
#  FakeMailBox — reemplaza imap_tools.MailBox
# ═══════════════════════════════════════════════════════════════
class FakeFolderManager:
    def __init__(self, message_count: int = 0):
        self._count = message_count

    def status(self, folder: str = "INBOX") -> Dict[str, int]:
        return {"MESSAGES": self._count, "UIDNEXT": self._count + 1,
                 "UIDVALIDITY": 1}

    def list(self):
        return [{"flags": [], "delim": "/", "name": "INBOX"}]

    def set(self, folder: str):
        pass


class FakeMailBox:
    """Reemplaza imap_tools.MailBox (subset usado por otp_manager)."""

    # Class-level config (set por with_messages factory)
    _MESSAGES: List[FakeMessage] = []
    _LOGIN_OK: bool = True
    _LOGIN_ERROR: Optional[Exception] = None

    def __init__(self, host: str = "imap.fake.com", port: int = 993):
        self._host = host
        self._port = port
        self._logged_in = False
        self.folder = FakeFolderManager(len(self._MESSAGES))

    def login(self, user: str, password: str,
                initial_folder: str = "INBOX") -> "FakeMailBox":
        """Devuelve self (encadenable como en imap_tools real)."""
        if not self._LOGIN_OK:
            raise (self._LOGIN_ERROR or
                    Exception("FakeMailBox: login_failed"))
        if not user or not password:
            raise Exception("FakeMailBox: missing credentials")
        self._logged_in = True
        return self

    def __enter__(self):
        if not self._logged_in:
            # Si entra al with sin login, simular login implícito o error
            pass
        return self

    def __exit__(self, *args):
        self._logged_in = False
        return False

    def fetch(self, criteria=None, limit: int = 100,
              mark_seen: bool = True, reverse: bool = False,
              bulk: bool = False, **kwargs) -> List[FakeMessage]:
        """Itera mensajes. Filtra básicamente por sender si criteria
        contiene from_=...; otherwise devuelve todos (limited)."""
        msgs = list(self._MESSAGES)
        # Crude filter por from_ si criteria es AND-like
        target_from = None
        try:
            if criteria is not None and hasattr(criteria, "kwargs"):
                target_from = criteria.kwargs.get("from_")
            elif isinstance(criteria, dict):
                target_from = criteria.get("from_")
        except Exception:
            pass
        if target_from:
            msgs = [m for m in msgs if target_from.lower() in m.from_.lower()]
        if reverse:
            msgs = list(reversed(msgs))
        return msgs[:limit]

    def uids(self, criteria=None) -> List[str]:
        return [m.uid for m in self._MESSAGES]

    def logout(self):
        self._logged_in = False

    def idle(self):
        class _Idle:
            def __enter__(_): return _
            def __exit__(_, *a): return False
            def poll(_, timeout: float = 0): return []
            def wait(_, timeout: float = 0): return []
        return _Idle()

    @classmethod
    def with_messages(cls, msgs: List[FakeMessage],
                        login_ok: bool = True,
                        login_error: Optional[Exception] = None) -> type:
        """Crea una subclase de FakeMailBox con mensajes específicos."""
        attrs = {
            "_MESSAGES": list(msgs),
            "_LOGIN_OK": login_ok,
            "_LOGIN_ERROR": login_error,
        }
        return type("FakeMailBoxBound", (cls,), attrs)


# ═══════════════════════════════════════════════════════════════
#  FakeAND — reemplaza imap_tools.AND para tests
# ═══════════════════════════════════════════════════════════════
class FakeAND:
    """Captura los kwargs como atributo .kwargs para que FakeMailBox.fetch
    pueda inspeccionar el filtro."""
    def __init__(self, **kwargs):
        self.kwargs = kwargs


# ═══════════════════════════════════════════════════════════════
#  FakeIMAP4_SSL — reemplaza imaplib.IMAP4_SSL para otp_3ds
# ═══════════════════════════════════════════════════════════════
class FakeIMAP4_SSL:
    """Subset de imaplib.IMAP4_SSL para otp_3ds.check_inbox."""

    # Class-level state (configurable per test)
    _MESSAGES: List[Dict[str, Any]] = []  # cada item: {uid, from, subject, body}
    _LOGIN_OK: bool = True
    _SEARCH_RESULT: Optional[List[bytes]] = None

    class error(Exception):
        pass

    IMAP4 = type("IMAP4", (object,), {"error": error})

    def __init__(self, host: str = "", port: int = 993, **kwargs):
        self._host = host
        self._port = port
        self._logged_in = False
        self._selected_folder = None

    def login(self, user: str, password: str):
        if not self._LOGIN_OK:
            raise self.error("login_failed")
        self._logged_in = True
        return ("OK", [b"Login successful"])

    def select(self, folder: str = "INBOX"):
        self._selected_folder = folder
        return ("OK", [str(len(self._MESSAGES)).encode()])

    def search(self, charset, criterion: str):
        # Devuelve UIDs como string concatenado por espacios
        if self._SEARCH_RESULT is not None:
            return ("OK", self._SEARCH_RESULT)
        uids = " ".join(str(m["uid"]) for m in self._MESSAGES)
        return ("OK", [uids.encode()])

    def fetch(self, uid, parts):
        # Construye un email RFC822 falso
        target = None
        for m in self._MESSAGES:
            if str(m["uid"]).encode() == uid or str(m["uid"]) == uid:
                target = m
                break
        if not target:
            return ("OK", [None])
        msg = EmailMessage()
        msg["From"] = target.get("from", "noreply@ticketmaster.com")
        msg["To"] = target.get("to", "user@example.com")
        msg["Subject"] = target.get("subject", "")
        msg["Date"] = target.get("date_str") or "Mon, 7 May 2026 10:00:00 +0000"
        msg.set_content(target.get("body", ""))
        raw = msg.as_bytes()
        return ("OK", [(b"1 (RFC822 {%d}" % len(raw), raw)])

    def logout(self):
        self._logged_in = False
        return ("BYE", [b"Logging out"])

    def close(self):
        pass

    def store(self, uid, command, flags):
        return ("OK", [b"Store OK"])

    def list(self, directory='""', pattern='*'):
        return ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])

    @classmethod
    def with_messages(cls, msgs: List[Dict[str, Any]],
                        login_ok: bool = True) -> type:
        attrs = {
            "_MESSAGES": list(msgs),
            "_LOGIN_OK": login_ok,
        }
        return type("FakeIMAP4_SSL_Bound", (cls,), attrs)


# ═══════════════════════════════════════════════════════════════
#  Helpers de TM messages (templates típicos)
# ═══════════════════════════════════════════════════════════════
def make_tm_otp_message(code: str, alias_email: str = "user@icloud.com",
                          uid: str = "1",
                          sender: str = "customer_support@email.ticketmaster.com",
                          age_minutes: int = 1) -> FakeMessage:
    """Construye un mensaje TM con OTP."""
    from datetime import timedelta
    body = f"Tu código de verificación de Ticketmaster es {code}. No lo compartas."
    return FakeMessage(
        uid=uid,
        from_=sender,
        to=[alias_email],
        subject=f"Tu código de Ticketmaster: {code}",
        text=body,
        html=f"<html><body>{body}</body></html>",
        date=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    )


def make_tm_3ds_message(code: str, bank: str = "Santander",
                          uid: str = "1") -> Dict[str, Any]:
    """Construye dict para FakeIMAP4_SSL._MESSAGES."""
    return {
        "uid": uid,
        "from": f"3dsecure@{bank.lower()}.com",
        "to": "user@example.com",
        "subject": f"{bank} 3D Secure",
        "body": f"{bank} informa: codigo de pago {code} valido 5 min",
    }
