# syntax=docker/dockerfile:1.6
# ═══════════════════════════════════════════════════════════
#  TM PERROS — Imagen multi-propósito (dashboard + bot workers)
# ═══════════════════════════════════════════════════════════
FROM python:3.12-slim AS base

# Paquetes del sistema necesarios para Playwright + OCR + TLS
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget ca-certificates \
    build-essential \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libxfixes3 libxshmfence1 libxtst6 \
    libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 \
    libcairo2 libatspi2.0-0 fonts-liberation \
    tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer de deps (cache friendly)
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps chromium firefox

# Copia del código
COPY . .

# Data persistente + logs
RUN mkdir -p /app/data /app/logs && \
    chown -R nobody:nogroup /app/data /app/logs

# Non-root para seguridad
USER nobody

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_ENV=production \
    DASHBOARD_PORT=5055

EXPOSE 5055

# Healthcheck: el healthz del dashboard
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -q --spider http://localhost:5055/api/healthz || exit 1

# tini maneja señales PID 1 correctamente (SIGTERM graceful shutdown)
ENTRYPOINT ["/usr/bin/tini", "--"]
# Producción: gunicorn+gevent-websocket (4 workers, max_requests 1000)
# Desarrollo: cambia a CMD ["python", "dashboard.py"] si TM_DEV=1
CMD ["sh", "-c", "if [ \"$TM_DEV\" = \"1\" ]; then python dashboard.py; else gunicorn -c gunicorn_config.py dashboard:app; fi"]
