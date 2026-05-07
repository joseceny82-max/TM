"""gunicorn_config.py — config producción gunicorn+gevent-websocket.

Uso:
    pip install gunicorn gevent gevent-websocket
    gunicorn -c gunicorn_config.py dashboard:app

Variables ENV:
    TM_GUNICORN_WORKERS=4         (default: 2*CPU+1)
    TM_GUNICORN_BIND=0.0.0.0:5055
    TM_GUNICORN_TIMEOUT=120
    TM_GUNICORN_LOG=logs/gunicorn.log
"""
import multiprocessing
import os

# Worker class: GeventWebSocketWorker para soportar flask-socketio + WebSocket
worker_class = "geventwebsocket.gunicorn.workers.GeventWebSocketWorker"

# Workers: 2*CPU+1 default (recomendación gunicorn). Mínimo 2 para WS broadcast.
workers = int(os.environ.get("TM_GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
threads = int(os.environ.get("TM_GUNICORN_THREADS", "2"))

# Bind
bind = os.environ.get("TM_GUNICORN_BIND", "0.0.0.0:5055")

# Timeouts
timeout = int(os.environ.get("TM_GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

# Logs
loglevel = os.environ.get("TM_GUNICORN_LOGLEVEL", "info")
accesslog = os.environ.get("TM_GUNICORN_ACCESSLOG", "-")    # stdout
errorlog = os.environ.get("TM_GUNICORN_ERRORLOG", "-")
access_log_format = '%(h)s %(t)s "%(r)s" %(s)s %(b)s %(D)us'

# Restart workers tras N requests (mitiga memory leaks)
max_requests = int(os.environ.get("TM_GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = 100

# Preload app: arranca todo antes de fork → menos RAM, daemons compartidos
preload_app = True

# WebSocket buffer
worker_connections = int(os.environ.get("TM_GUNICORN_WORKER_CONNECTIONS", "1000"))

# Capture stdout/stderr de los workers
capture_output = True
enable_stdio_inheritance = True

# Hooks
def post_fork(server, worker):
    """Tras fork, configurar logger del worker."""
    server.log.info(f"Worker spawned (pid: {worker.pid})")


def worker_int(worker):
    worker.log.info("worker received INT or QUIT signal")


def on_starting(server):
    server.log.info("=" * 60)
    server.log.info(f"TM PERROS gunicorn server starting")
    server.log.info(f"Workers: {workers} ({worker_class})")
    server.log.info(f"Bind: {bind}")
    server.log.info(f"Timeout: {timeout}s · Max requests: {max_requests}")
    server.log.info("=" * 60)
