"""queue_worker.py — Manejo de colas Queue-it y workers de cookies.

Ticketmaster usa Queue-it masivamente. Este modulo gestiona:
- Deteccion de queue page
- Espera virtual (con jitter humano)
- Paso a tienda una vez liberado
"""
import uuid
from datetime import datetime
import config
import json_atomic


class QueueManager:
    def list_queues(self):
        d = json_atomic.load(config.ruta("queues.json"), default={"queues": []})
        return d.get("queues", [])

    def start(self, event_id, domain_id, worker_count=1):
        d = json_atomic.load(config.ruta("queues.json"), default={"queues": []})
        q = {
            "id": uuid.uuid4().hex[:8],
            "event_id": event_id,
            "domain_id": domain_id,
            "worker_count": worker_count,
            "status": "waiting",
            "position": 0,
            "started_at": datetime.now().isoformat(),
            "logs": [],
        }
        d["queues"].append(q)
        json_atomic.save(config.ruta("queues.json"), d)
        return q

    def update_status(self, qid, patch):
        d = json_atomic.load(config.ruta("queues.json"), default={"queues": []})
        for q in d["queues"]:
            if q["id"] == qid:
                q.update(patch)
                json_atomic.save(config.ruta("queues.json"), d)
                return q
        return None


class CookieManager:
    def get_pool_for(self, domain_id):
        return []


queue_manager = QueueManager()
cookie_manager = CookieManager()
