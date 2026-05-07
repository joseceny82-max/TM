"""account_manager.py — Gestion de cuentas Ticketmaster y tarjetas de pago."""
import uuid
from datetime import datetime
import config
import json_atomic


class AccountManager:
    def __init__(self):
        self.acc_path = config.ruta("accounts.json")
        self.card_path = config.ruta("cards.json")

    def list_accounts(self):
        d = json_atomic.load(self.acc_path, default={"accounts": []})
        return d.get("accounts", [])

    def add_account(self, account):
        d = json_atomic.load(self.acc_path, default={"accounts": []})
        account = dict(account)
        account.setdefault("id", uuid.uuid4().hex[:8])
        account.setdefault("created_at", datetime.now().isoformat())
        account.setdefault("status", "active")
        account.setdefault("purchases", 0)
        account.setdefault("sessions", 0)
        d["accounts"].append(account)
        json_atomic.save(self.acc_path, d)
        return account

    def update_account(self, aid, patch):
        d = json_atomic.load(self.acc_path, default={"accounts": []})
        for a in d["accounts"]:
            if a.get("id") == aid:
                a.update(patch)
                json_atomic.save(self.acc_path, d)
                return a
        return None

    def delete_account(self, aid):
        d = json_atomic.load(self.acc_path, default={"accounts": []})
        d["accounts"] = [a for a in d["accounts"] if a.get("id") != aid]
        json_atomic.save(self.acc_path, d)
        return True

    def list_cards(self):
        d = json_atomic.load(self.card_path, default={"cards": []})
        return d.get("cards", [])

    def add_card(self, card):
        d = json_atomic.load(self.card_path, default={"cards": []})
        card = dict(card)
        card.setdefault("id", uuid.uuid4().hex[:8])
        card.setdefault("created_at", datetime.now().isoformat())
        d["cards"].append(card)
        json_atomic.save(self.card_path, d)
        return card

    def delete_card(self, cid):
        d = json_atomic.load(self.card_path, default={"cards": []})
        d["cards"] = [c for c in d["cards"] if c.get("id") != cid]
        json_atomic.save(self.card_path, d)
        return True

    # ───────────────── proxy_per_account binding ─────────────────
    # Cada cuenta TM se ata a 1 proxy_id estable. Solo se rota tras logout
    # limpio (mismo principio que JA4 pinning). Reduce el "drift" que TM
    # detecta cuando una misma cuenta presenta múltiples IPs en sesiones
    # consecutivas.
    def assign_proxy(self, aid: str, proxy_id: str) -> dict:
        """Ata una cuenta a un proxy_id. Sobrescribe cualquier asignación previa."""
        return self.update_account(aid, {"assigned_proxy_id": proxy_id,
                                          "proxy_assigned_at": datetime.now().isoformat()})

    def get_assigned_proxy(self, aid: str) -> str:
        """Devuelve el proxy_id atado a la cuenta o '' si no hay."""
        for a in self.list_accounts():
            if a.get("id") == aid:
                return a.get("assigned_proxy_id", "") or ""
        return ""

    def clear_assigned_proxy(self, aid: str) -> bool:
        """Libera el binding (típicamente tras logout limpio)."""
        a = self.update_account(aid, {"assigned_proxy_id": "",
                                       "proxy_cleared_at": datetime.now().isoformat()})
        return a is not None

    def list_proxy_bindings(self) -> list:
        """Devuelve [{account_id, email, proxy_id, since}] para auditoría."""
        out = []
        for a in self.list_accounts():
            pid = a.get("assigned_proxy_id", "")
            if pid:
                out.append({"account_id": a.get("id"),
                            "email": a.get("email", ""),
                            "proxy_id": pid,
                            "since": a.get("proxy_assigned_at", "")})
        return out


account_manager = AccountManager()
