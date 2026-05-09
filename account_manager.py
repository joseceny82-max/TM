"""account_manager.py — Gestion de cuentas Ticketmaster y tarjetas de pago."""
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
import config
import json_atomic


@contextmanager
def _file_lock(lock_path: str, timeout: float = 30.0):
    """Lock cross-process basado en archivo (atomic create + retry).
    FIX 2026-05-09: account_manager.add_account/update/delete y
    add_card/update/delete tenian race condition cuando 2 register_account.py
    paralelos terminaban a la vez (20:41:07 exacto -> 2 cuentas perdidas en
    accounts.json). Lock previene la lectura-modificacion-escritura concurrente.
    """
    start = time.time()
    acquired = False
    fd = None
    try:
        while time.time() - start < timeout:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                acquired = True
                break
            except FileExistsError:
                # Lock stale > 30s -> remover y reintentar
                try:
                    age = time.time() - os.path.getmtime(lock_path)
                    if age > 30:
                        os.unlink(lock_path)
                        continue
                except Exception:
                    pass
                time.sleep(0.05)
        yield acquired
    finally:
        if fd is not None:
            try: os.close(fd)
            except Exception: pass
        if acquired:
            try: os.unlink(lock_path)
            except Exception: pass


class AccountManager:
    def __init__(self):
        self.acc_path = config.ruta("accounts.json")
        self.card_path = config.ruta("cards.json")
        # Locks separados para accounts.json y cards.json (paths con .lock sufijo).
        self._acc_lock = self.acc_path + ".lock"
        self._card_lock = self.card_path + ".lock"

    def list_accounts(self):
        d = json_atomic.load(self.acc_path, default={"accounts": []})
        return d.get("accounts", [])

    def add_account(self, account):
        with _file_lock(self._acc_lock):
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
        with _file_lock(self._acc_lock):
            d = json_atomic.load(self.acc_path, default={"accounts": []})
            for a in d["accounts"]:
                if a.get("id") == aid:
                    a.update(patch)
                    json_atomic.save(self.acc_path, d)
                    return a
        return None

    def delete_account(self, aid):
        with _file_lock(self._acc_lock):
            d = json_atomic.load(self.acc_path, default={"accounts": []})
            d["accounts"] = [a for a in d["accounts"] if a.get("id") != aid]
            json_atomic.save(self.acc_path, d)
        return True

    def list_cards(self):
        d = json_atomic.load(self.card_path, default={"cards": []})
        return d.get("cards", [])

    def add_card(self, card):
        with _file_lock(self._card_lock):
            d = json_atomic.load(self.card_path, default={"cards": []})
            card = dict(card)
            card.setdefault("id", uuid.uuid4().hex[:8])
            card.setdefault("created_at", datetime.now().isoformat())
            d["cards"].append(card)
            json_atomic.save(self.card_path, d)
        return card

    def delete_card(self, cid):
        with _file_lock(self._card_lock):
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


def self_test() -> dict:
    """Validación básica del módulo (política del proyecto, módulos >50 LOC).
    Verifica que AccountManager se instancia, paths se computan y el lock
    cross-process se puede crear/liberar.
    """
    out = {'ok': True, 'checks': {}, 'errors': []}
    try:
        out['checks']['instance'] = account_manager is not None
        out['checks']['acc_path'] = bool(account_manager.acc_path)
        out['checks']['card_path'] = bool(account_manager.card_path)
        out['checks']['acc_lock_path'] = bool(account_manager._acc_lock)
        out['checks']['list_accounts_callable'] = callable(account_manager.list_accounts)
        out['checks']['list_cards_callable'] = callable(account_manager.list_cards)
        # Test del file lock (acquire + release)
        import tempfile, os as _os
        tmp_lock = _os.path.join(tempfile.gettempdir(), f'_test_lock_{_os.getpid()}.lock')
        with _file_lock(tmp_lock, timeout=2.0) as acquired:
            out['checks']['file_lock_acquire'] = bool(acquired)
        out['checks']['file_lock_released'] = not _os.path.exists(tmp_lock)
        # Cuentas listables
        accs = account_manager.list_accounts()
        out['checks']['accounts_count'] = len(accs)
        for k, v in out['checks'].items():
            if v is False:
                out['ok'] = False
                out['errors'].append(f'check failed: {k}')
    except Exception as e:
        out['ok'] = False
        out['errors'].append(f'self_test exception: {e}')
    return out
