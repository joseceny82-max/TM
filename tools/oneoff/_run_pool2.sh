#!/usr/bin/env bash
# Pool de 2 slots simultáneos. Cuando uno termina (éxito o fail), se lanza otro
# inmediatamente. Sigue hasta archivar TARGET filas en plantilla_tm.csv.
cd "$(dirname "$0")"
LOG=_run_pool2.log
: > "$LOG"

TARGET=7
PLANTILLA=plantilla_tm.csv
START_ROWS=$(wc -l < "$PLANTILLA")
START_ROWS=$((START_ROWS - 1))   # restar header

echo "[$(date '+%F %T')] === START pool2 target=$TARGET start_rows=$START_ROWS ===" | tee -a "$LOG"

declare -A PIDS
N_LAUNCHED=0

launch_one() {
  local slot=$1
  N_LAUNCHED=$((N_LAUNCHED + 1))
  python register_account.py --row 1 > "_pool_${N_LAUNCHED}.log" 2>&1 &
  PIDS[$slot]=$!
  echo "[$(date '+%F %T')] LAUNCH #$N_LAUNCHED slot=$slot pid=${PIDS[$slot]} → _pool_${N_LAUNCHED}.log" | tee -a "$LOG"
}

while true; do
  current_rows=$(wc -l < "$PLANTILLA")
  current_rows=$((current_rows - 1))
  archived=$((START_ROWS - current_rows))

  # Limpiar slots con proceso muerto
  for slot in a b; do
    pid=${PIDS[$slot]:-}
    if [ -n "$pid" ] && ! kill -0 $pid 2>/dev/null; then
      wait $pid 2>/dev/null
      rc=$?
      echo "[$(date '+%F %T')] DONE  slot=$slot pid=$pid rc=$rc archived=$archived/$TARGET" | tee -a "$LOG"
      unset 'PIDS[$slot]'
    fi
  done

  # Target alcanzado → esperar a los aún vivos y salir
  if [ $archived -ge $TARGET ]; then
    echo "[$(date '+%F %T')] target $TARGET alcanzado — esperando $((${#PIDS[@]})) activos..." | tee -a "$LOG"
    wait
    break
  fi

  # Cuántos slots faltan por relanzar (sin pasarse de TARGET de comprometidos)
  remaining=$((TARGET - archived))
  active=${#PIDS[@]}
  # comprometidos = archivados + activos. No queremos > TARGET.
  while [ ${#PIDS[@]} -lt 2 ] && [ $((archived + ${#PIDS[@]})) -lt $TARGET ]; do
    if [ -z "${PIDS[a]:-}" ]; then slot=a
    elif [ -z "${PIDS[b]:-}" ]; then slot=b
    else break
    fi
    launch_one $slot
    sleep 8   # desfase entre lanzamientos paralelos
  done

  # Si no hay activos y aún faltan → algo malo; abort
  if [ ${#PIDS[@]} -eq 0 ]; then
    echo "[$(date '+%F %T')] ABORT: 0 activos pero archived=$archived < $TARGET" | tee -a "$LOG"
    break
  fi

  sleep 5
done

final_rows=$(wc -l < "$PLANTILLA")
final_rows=$((final_rows - 1))
final_archived=$((START_ROWS - final_rows))
echo "[$(date '+%F %T')] === FIN archivados_reales=$final_archived lanzados=$N_LAUNCHED ===" | tee -a "$LOG"
