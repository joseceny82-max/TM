#!/usr/bin/env bash
# Orquestador: 10 tandas de 2 registros paralelos = 20 totales.
# Tras cada tanda, plantilla_tm.csv se renumera; siempre --row 1 y --row 2.
set -u
cd "$(dirname "$0")"
LOG=_run_20_pairs.log
: > "$LOG"
echo "[$(date '+%F %T')] === START 10x pairs ===" | tee -a "$LOG"

OK=0
FAIL=0
for i in $(seq 6 12); do
  echo "[$(date '+%F %T')] --- tanda $i: --row 1 + --row 2 ---" | tee -a "$LOG"
  python register_account.py --row 1 > "_pair${i}_a.log" 2>&1 &
  PID_A=$!
  sleep 8  # desfase para evitar colisión inicial de locks/perfiles
  python register_account.py --row 2 > "_pair${i}_b.log" 2>&1 &
  PID_B=$!
  wait $PID_A; RA=$?
  wait $PID_B; RB=$?
  echo "[$(date '+%F %T')] tanda $i: A=$RA B=$RB" | tee -a "$LOG"
  if [ $RA -eq 0 ]; then OK=$((OK+1)); else FAIL=$((FAIL+1)); fi
  if [ $RB -eq 0 ]; then OK=$((OK+1)); else FAIL=$((FAIL+1)); fi
  echo "[$(date '+%F %T')] acumulado: ok=$OK fail=$FAIL" | tee -a "$LOG"
done

echo "[$(date '+%F %T')] === FIN: ok=$OK fail=$FAIL ===" | tee -a "$LOG"
