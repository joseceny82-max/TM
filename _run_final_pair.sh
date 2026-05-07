#!/usr/bin/env bash
# Final: 1 tanda de 2 paralelos para alcanzar 20+ registros con fix de race condition.
set -u
cd "$(dirname "$0")"
LOG=_run_final_pair.log
: > "$LOG"
echo "[$(date '+%F %T')] === START tanda final ===" | tee -a "$LOG"
python register_account.py --row 1 > _final_a.log 2>&1 &
PID_A=$!
sleep 8
python register_account.py --row 2 > _final_b.log 2>&1 &
PID_B=$!
wait $PID_A; RA=$?
wait $PID_B; RB=$?
echo "[$(date '+%F %T')] tanda final: A=$RA B=$RB" | tee -a "$LOG"
echo "[$(date '+%F %T')] === FIN ===" | tee -a "$LOG"
