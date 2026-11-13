#!/usr/bin/env bash
# usage: run_stage.sh <vus> <run-number> [label]
set -u
cd "$(dirname "$0")"
VUS=$1; RUN=$2; LABEL=${3:-default}
OUT="results/${LABEL}_${VUS}"
mkdir -p results
export MSYS_NO_PATHCONV=1
mysqlq() { docker exec dmload-db-1 mysql -uroot -ploadtest -N -e "$1" 2>/dev/null; }
mysqlq "FLUSH STATUS"
( while true; do
    docker stats --no-stream --format "{{.Name}},{{.CPUPerc}},{{.MemUsage}}" dmload-app-1 dmload-db-1
    echo "conns,$(mysqlq "SHOW GLOBAL STATUS LIKE 'Threads_connected'" | awk '{print $2}')"
    sleep 5
  done ) > "$OUT.stats.csv" 2>/dev/null &
SAMPLER=$!
docker compose run --rm -e VUS="$VUS" -e RUN="$RUN" -e RAMP=${RAMP:-1m} -e HOLD=${HOLD:-3m} k6 \
  run --quiet --summary-export "results/${LABEL}_${VUS}.summary.json" dairymitr.js > "$OUT.k6.txt" 2>&1
kill $SAMPLER 2>/dev/null
{
  echo "max_used_connections $(mysqlq "SHOW GLOBAL STATUS LIKE 'Max_used_connections'" | awk '{print $2}')"
  echo "aborted_connects $(mysqlq "SHOW GLOBAL STATUS LIKE 'Aborted_connects'" | awk '{print $2}')"
  echo "questions $(mysqlq "SHOW GLOBAL STATUS LIKE 'Questions'" | awk '{print $2}')"
  echo "slow_queries $(mysqlq "SHOW GLOBAL STATUS LIKE 'Slow_queries'" | awk '{print $2}')"
  echo "app_5xx $(docker compose logs --since 6m app 2>/dev/null | grep -cE '" 5[0-9][0-9] ')"
  echo "app_errors $(docker compose logs --since 6m app 2>/dev/null | grep -cE 'ERROR|Traceback|WORKER TIMEOUT')"
} > "$OUT.db.txt"
echo "stage ${LABEL} ${VUS} done"
