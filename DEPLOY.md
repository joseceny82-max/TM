# 🚀 TM PERROS — Guía de Deployment Multi-Host

## 🎯 Arquitecturas soportadas

```
┌─────────────────────────────────────────────────────────────┐
│  OPCIÓN 1 — SINGLE HOST (dev / 1 usuario)                   │
│                                                              │
│     [Usuario]  ──▶  [dashboard.py]  ──▶  data/*.json         │
│                     (todo en local)                          │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  OPCIÓN 2 — MULTI-USER / 1 SERVIDOR (recomendado casa)      │
│                                                              │
│  [Usuario A]──┐                                              │
│  [Usuario B]──┼──▶ [dashboard] ──▶ [redis] ──▶ data/        │
│  [Usuario C]──┘       ↓                                      │
│                    [worker 1]                                │
│                    [worker 2]   ← docker compose --scale     │
│                    [worker N]                                │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  OPCIÓN 3 — MULTI-HOST / CLÚSTER (varios equipos)           │
│                                                              │
│   Host A (dashboard + worker)  ──┐                           │
│   Host B (worker)              ──┼──▶ [Redis central]        │
│   Host C (worker)              ──┘                           │
│                                                              │
│   Los hosts comparten: AcpAT pool, flow locks, cookies,      │
│   heartbeat, broadcasts. Todo via Redis.                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔧 Opción 1 — Single host (lo que ya tienes)

```bash
python dashboard.py
# http://127.0.0.1:5055  —  admin / admin
```

---

## 🐳 Opción 2 — Docker compose (1 máquina, N workers)

### 1. Preparar `.env`
```bash
cp .env.example .env
# Edita y rellena: SECRET_KEY, ADMIN_PASSWORD, API keys...
openssl rand -hex 32   # ← usa esto como SECRET_KEY
```

### 2. Arrancar
```bash
docker compose up -d
docker compose logs -f dashboard
# → http://localhost:5055
```

### 3. Escalar workers
```bash
docker compose up -d --scale worker=5
docker compose ps
```

### 4. Añadir usuarios
Desde el dashboard → **Usuarios** → "Crear usuario".
Cada uno con permisos independientes (viewer / operator / admin).

---

## 🌐 Opción 3 — Multi-host distribuido

### Host A (servidor principal — dashboard + redis)
```bash
# 1. En un servidor con IP pública o VPN (p.ej. tu PC principal)
docker compose up -d dashboard redis

# 2. Abre Redis al clúster (SOLO a IPs permitidas!)
# Edita docker-compose.yml → redis ports: "6379:6379"
# O mejor: usa Upstash/Railway con TLS
```

### Host B, C, D... (workers remotos)
```bash
# 1. Copia el proyecto a cada host
git clone <repo> && cd proyecto
cp .env.example .env

# 2. Apunta a Redis central + INSTANCE_ID único
cat >> .env <<EOF
INSTANCE_ID=worker-laptop-marcos
REDIS_URL=redis://:PASSWORD@IP-DEL-HOST-A:6379/0
EOF

# 3. Arranca solo el worker (sin dashboard)
docker compose up -d worker

# O sin docker:
python worker.py
```

### Verificación
En dashboard → **Cluster** aparecen todos los hosts con heartbeat verde.
También: `GET /api/cluster/hosts` devuelve la lista.

---

## 🔐 Conexión desde scripts externos

### 1. Generar API token
Dashboard → **Usuarios** → tu usuario → **"Crear API token"**
→ scope: `read,write` → copia el token `tm_xyz...` (solo se muestra una vez).

### 2. Usar desde cualquier script / host
```bash
curl -H "Authorization: Bearer tm_xyz..." \
     http://dashboard:5055/api/cluster/hosts
```

```python
import requests
s = requests.Session()
s.headers["Authorization"] = "Bearer tm_xyz..."
r = s.post("http://dashboard:5055/api/purchase-flow/run",
           json={"event_id": "123", "mode": "simulate"})
```

---

## 🏗️ Infraestructura recomendada (por presupuesto)

### 🟢 Barato (≤30 €/mes)
- **Dashboard**: tu PC de casa / VPS 2GB (~5 €)
- **Redis**: Upstash free tier (10k commands/día)
- **Proxies**: IPRoyal ISP (~15 €)
- **Workers**: 2-3 contenedores en la misma máquina

### 🟡 Medio (≤150 €/mes)
- **Dashboard**: VPS 4GB + 2 vCPU (Hetzner ~10 €)
- **Redis**: Redis Cloud 30MB free o self-hosted
- **Proxies**: NetNut ISP 5GB/mes (~50 €)
- **Workers**: 5-10 contenedores en 2-3 VPS
- **CAPTCHA**: CapSolver $50

### 🔴 Pro (500 €+/mes)
- **Dashboard**: k3s / ECS con HA
- **Redis**: ElastiCache / Upstash Pro
- **Proxies**: BrightData mobile 20GB (~300 €)
- **Workers**: 20+ en cluster k8s, autoescalado
- **Monitoring**: Grafana Cloud + Sentry

---

## ⚠️ Seguridad producción

1. **NO uses** `admin/admin` en producción. Cambia `ADMIN_PASSWORD`.
2. **Genera SECRET_KEY fuerte**: `openssl rand -hex 32`.
3. **Pon Redis detrás de VPN** o con password + TLS (`rediss://`).
4. **Activa 2FA** en cuentas dashboard.
5. **Usa nginx/caddy** delante del dashboard con TLS (cert Let's Encrypt).
6. **Rate-limit** ya activo: 10 logins/min por IP.
7. **Firewall**: expone SOLO el puerto 443, NO 6379 (Redis).
8. **Backups**: el volumen `tm_data` debe tener backup (rsync/restic).

---

## 🧪 Verificación rápida (post-deploy)

```bash
# Salud general
curl http://dashboard:5055/api/healthz

# Info del cluster
curl -b cookies.txt http://dashboard:5055/api/cluster/info

# Lista hosts
curl -b cookies.txt http://dashboard:5055/api/cluster/hosts
```

En dashboard, sección **Cluster** verás:
- Cantidad de hosts alive (con heartbeat <30 s)
- CPU + memoria agregada
- Flow locks activos (por evento)
- Pub/sub redis healthy

---

## 🔁 Upgrades sin downtime

```bash
# Host A: rolling update
docker compose build
docker compose up -d --no-deps dashboard   # restart solo dashboard
# workers siguen corriendo contra el mismo Redis
docker compose up -d --no-deps worker
```

---

**Última actualización**: 2026-04-23
