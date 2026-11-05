#!/usr/bin/env bash
# Lanza 4 instancias paralelas de _compra_parallel.py
# Cada una: perfil MoreLogin distinto + cuenta TM distinta + browser visible.
# Compra 4 entradas Asiento Reservado (idx=3) hasta tarjeta rellenada.
# NO pulsa Pagar — espera flag _compra_parallel.STOP.

cd "$(dirname "$0")"
LOG=_run_compra_parallel.log
: > "$LOG"

# Configuración por defecto (cambiable por env)
EVENT_URL="${EVENT_URL:-https://www.ticketmaster.es/event/karol-g-viajando-por-el-mundo-tropitour-entradas/1438881928}"
OFFER_IDX="${OFFER_IDX:-3}"   # Asiento Reservado en el listing
QTY="${QTY:-4}"
N_SLOTS="${N_SLOTS:-4}"

# Limpiar locks previos
rm -f _compra_parallel_used_accounts.lock _compra_parallel.STOP
echo "[]" > _compra_parallel_used_accounts.lock

echo "[$(date '+%F %T')] === START $N_SLOTS compras paralelas (offer_idx=$OFFER_IDX qty=$QTY) ===" | tee -a "$LOG"
echo "[$(date '+%F %T')] event_url=$EVENT_URL" | tee -a "$LOG"

declare -a PIDS

for slot in $(seq 1 $N_SLOTS); do
  python _compra_parallel.py \
    --slot $slot \
    --event-url "$EVENT_URL" \
    --offer-idx $OFFER_IDX \
    --qty $QTY \
    > "_compra_parallel_slot${slot}.log" 2>&1 &
  PIDS[$slot]=$!
  echo "[$(date '+%F %T')] LAUNCH slot=$slot pid=${PIDS[$slot]} → _compra_parallel_slot${slot}.log" | tee -a "$LOG"
  # Desfase entre slots para evitar pick race en MoreLogin
  sleep 12
done

echo "[$(date '+%F %T')] === TODOS lanzados. Esperando 4 ===" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "📋 Para CERRAR todos los browsers (cuando hayas revisado el checkout):" | tee -a "$LOG"
echo "    touch _compra_parallel.STOP" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "📋 Para ver progreso live:" | tee -a "$LOG"
echo "    tail -f _compra_parallel_slot1.log _compra_parallel_slot2.log _compra_parallel_slot3.log _compra_parallel_slot4.log" | tee -a "$LOG"
echo "" | tee -a "$LOG"

wait
echo "[$(date '+%F %T')] === FIN ===" | tee -a "$LOG"
