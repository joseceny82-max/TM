"""error_manager.py — Manejo centralizado de errores."""
from datetime import datetime
from collections import deque


class ErrorManager:
    def __init__(self, max_history=500):
        self._errors = deque(maxlen=max_history)

    def record(self, category, message, severity="error", meta=None):
        entry = {
            "ts": datetime.now().isoformat(),
            "category": category,
            "message": message,
            "severity": severity,
            "meta": meta or {},
        }
        self._errors.append(entry)
        return entry

    def get_recent(self, limit=50):
        return list(self._errors)[-limit:][::-1]

    def clear(self):
        self._errors.clear()


error_manager = ErrorManager()
