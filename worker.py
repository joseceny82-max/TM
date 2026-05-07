"""worker.py — Worker standalone que consume flows de la cola distribuida.

Se registra en el cluster_manager, escucha cola Redis `tm:flow:queue`,
y ejecuta flows en paralelo con lock distribuido (evita que 2 workers
ejecuten el mismo flow).

Arranque:
  python worker.py                        # 1 worker
  docker compose up -d --scale worker=5   # 5 workers paralelos

Enviar flow a la cola:
  rb.backend.publish("tm:flow:queue", {"event_id": "...", "mode": "simulate"})
"""
import sys
import time
import logging
import threading
import signal

import config
import redis_backend as rb
import cluster_manager
import resilience


log = logging.getLogger("worker")
logging.basicConfig(
    level=config.LOG_LEVEL if hasattr(config, "LOG_LEVEL") else "INFO",
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

_stop = threading.Event()


def handle_flow(data: dict):
    """Callback cuando llega un flow por pub/sub."""
    if not isinstance(data, dict):
        return
    flow_id = data.get("flow_id") or f"flow_{int(time.time())}"
    event_id = data.get("event_id")
    if not event_id:
        log.warning("flow sin event_id, descartado")
        return

    # lock distribuido — solo 1 worker lo procesa
    owner = cluster_manager.acquire_flow(flow_id, ttl=600)
    if not owner:
        log.info(f"flow {flow_id} ya tiene owner, skip")
        return

    log.info(f"▶ flow {flow_id} adquirido por {owner}")
    try:
        import tm_purchase_flow
        result = tm_purchase_flow.run_flow(
            event_id=event_id,
            quantity=data.get("quantity", 2),
            mode=data.get("mode", "simulate"),
            strategy=data.get("strategy", "best_available"),
        )
        # Publica resultado
        rb.backend.publish("tm:flow:result", {
            "flow_id": flow_id,
            "owner": owner,
            "ok": result.get("ok"),
            "order_id": result.get("order_id"),
            "error": result.get("error"),
            "elapsed_ms": result.get("elapsed_ms"),
        })
        log.info(f"✓ flow {flow_id}: {'OK' if result.get('ok') else 'FAIL - '+str(result.get('error'))[:60]}")
    except Exception as e:
        log.exception(f"✗ flow {flow_id} error: {e}")
    finally:
        cluster_manager.release_flow(flow_id, owner)


def _on_signal(signum, frame):
    log.info(f"señal {signum} recibida, parando worker...")
    _stop.set()


def main():
    log.info(f"=== TM Worker arrancando ===")
    log.info(f"Instance ID:  {rb.INSTANCE_ID}")
    log.info(f"Backend:      {type(rb.backend).__name__}")
    log.info(f"Distribuido:  {rb.is_distributed()}")

    # Señales
    try:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
    except (ValueError, AttributeError):
        pass

    # Heartbeat cluster
    cluster_manager.start_heartbeat()

    # Suscribirse a la cola
    cluster_manager.listen("tm:flow:queue", handle_flow)
    log.info("Suscrito a cluster:tm:flow:queue, esperando flows...")

    # Main loop: mantener vivo
    while not _stop.is_set():
        time.sleep(1)

    log.info("shutdown graceful")
    cluster_manager.stop_heartbeat()


if __name__ == "__main__":
    main()
