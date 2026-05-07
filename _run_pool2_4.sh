#!/usr/bin/env bash
cd "$(dirname "$0")"
LOG=_run_pool2_4.log
: > "$LOG"

TARGET=4
PLANTILLA=plantilla_tm.csv
START_ROWS=$(wc -l < "$PLANTILLA")
START_ROWS=$((START_ROWS - 1))

echo "[$(date '+%F %T')] === START pool2x4 target=$TARGET start_rows=$START_ROWS ===" | tee -a "$LOG"

declare -A PIDS
N_LAUNCHED=0
LAUNCHED_TOTAL=0

launch_one() {
  local slot=$1
  N_LAUNCHED=$((N_LAUNCHED + 1))
  LAUNCHED_TOTAL=$((LAUNCHED_TOTAL + 1))
  python register_account.py --row 1 > "_pool24_${N_LAUNCHED}.log" 2>&1 &
  PIDS[$slot]=$!
  echo "[$(date '+%F %T')] LAUNCH #$N_LAUNCHED slot=$slot pid=${PIDS[$slot]} → _pool24_${N_LAUNCHED}.log" | tee -a "$LOG"
}

while true; do
  current_rows=$(wc -l < "$PLANTILLA")
  current_rows=$((current_rows - 1))
  archived=$((START_ROWS - current_rows))

  for slot in a b; do
    pid=${PIDS[$slot]:-}
    if [ -n "$pid" ] && ! kill -0 $pid 2>/dev/null; then
      wait $pid 2>/dev/null
      rc=$?
      echo "[$(date '+%F %T')] DONE  slot=$slot pid=$pid rc=$rc archived=$archived/$TARGET launched=$LAUNCHED_TOTAL" | tee -a "$LOG"
      unset 'PIDS[$slot]'
    fi
  done

  # Si ya lanzamos >= TARGET launches, esperar a que acaben
  if [ $LAUNCHED_TOTAL -ge $TARGET ]; then
    if [ ${#PIDS[@]} -eq 0 ]; then
      break
    fi
    sleep 5
    continue
  fi

  while [ ${#PIDS[@]} -lt 2 ] && [ $LAUNCHED_TOTAL -lt $TARGET ]; do
    if [ -z "${PIDS[a]:-}" ]; then slot=a
    elif [ -z "${PIDS[b]:-}" ]; then slot=b
    else break
    fi
    launch_one $slot
    sleep 8
  done

  if [ ${#PIDS[@]} -eq 0 ] && [ $LAUNCHED_TOTAL -lt $TARGET ]; then
    echo "[$(date '+%F %T')] ABORT" | tee -a "$LOG"
    break
  fi

  sleep 5
done

final_rows=$(wc -l < "$PLANTILLA")
final_rows=$((final_rows - 1))
final_archived=$((START_ROWS - final_rows))
echo "[$(date '+%F %T')] === FIN archivados_reales=$final_archived lanzados=$N_LAUNCHED ===" | tee -a "$LOG"
