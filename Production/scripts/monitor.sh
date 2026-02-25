#!/usr/bin/env bash
# =============================================================================
# monitor.sh — Live migration progress dashboard
# =============================================================================
# Polls Kafka Connect and Kafka broker every N seconds and prints:
#   - Connector state (RUNNING / FAILED / PAUSED)
#   - Consumer lag per topic (messages behind = not yet consumed)
#   - Messages produced per topic (log-end-offset)
#   - Estimated time remaining (based on current lag drain rate)
#
# Usage:
#   ./scripts/monitor.sh              # Monitor all topics, refresh every 5s
#   ./scripts/monitor.sh 10           # Refresh every 10s
#   ./scripts/monitor.sh 5 sqlserver  # Monitor SQL Server topic only
#
# Press Ctrl+C to stop.
# =============================================================================

set -uo pipefail

INTERVAL="${1:-5}"
TARGET="${2:-both}"
CONNECT_URL="${CONNECT_URL:-http://localhost:8083}"

MSSQL_TOPIC="prod_mssql.AdventureWorks2019.Sales.SalesOrderDetailBig"
PG_TOPIC="prod_pg.public.sales_order_detail_big"

# Consumer group names used by benchmark consumer (adjust if using your own group)
MSSQL_GROUP="migration-monitor-mssql"
PG_GROUP="migration-monitor-pg"

# Total expected rows (adjust per table)
TOTAL_ROWS=2000000

# Track previous lag for rate calculation
declare -A PREV_LAG
declare -A PREV_TIME

format_number() {
    printf "%'d" "$1" 2>/dev/null || echo "$1"
}

connector_status() {
    local name="$1"
    local status
    status=$(curl -s "$CONNECT_URL/connectors/$name/status" 2>/dev/null || echo "{}")
    local state task_state
    state=$(echo "$status" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('connector',{}).get('state','N/A'))" 2>/dev/null || echo "N/A")
    task_state=$(echo "$status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
tasks = d.get('tasks', [])
if not tasks:
    print('no tasks')
else:
    states = [t.get('state','?') for t in tasks]
    print(', '.join(states))
" 2>/dev/null || echo "N/A")
    printf "  %-40s  connector=%-10s  tasks=[%s]\n" "$name" "$state" "$task_state"
}

topic_lag() {
    local topic="$1"
    local group="$2"
    local label="$3"

    # Get log-end-offset (total messages produced)
    local leo
    leo=$(docker exec kafka kafka-run-class \
        kafka.tools.GetOffsetShell \
        --bootstrap-server kafka:29092 \
        --topic "$topic" \
        --time -1 2>/dev/null \
        | awk -F: '{sum += $3} END {print sum+0}' || echo "0")

    # Get consumer committed offset
    local committed
    committed=$(docker exec kafka kafka-consumer-groups \
        --bootstrap-server kafka:29092 \
        --group "$group" \
        --describe 2>/dev/null \
        | awk 'NR>1 && /'"$topic"'/ {sum += $4} END {print sum+0}' || echo "0")

    local lag=$(( leo - committed ))
    local pct=0
    [[ $leo -gt 0 ]] && pct=$(( (leo - lag) * 100 / leo ))

    # Rate estimation
    local now rate eta_s eta
    now=$(date +%s)
    if [[ -n "${PREV_LAG[$topic]+x}" ]]; then
        local elapsed=$(( now - PREV_TIME[$topic] ))
        local drained=$(( PREV_LAG[$topic] - lag ))
        if [[ $elapsed -gt 0 && $drained -gt 0 ]]; then
            rate=$(( drained / elapsed ))
            eta_s=$(( lag / rate ))
            if [[ $eta_s -lt 60 ]]; then
                eta="${eta_s}s"
            elif [[ $eta_s -lt 3600 ]]; then
                eta="$(( eta_s / 60 ))m $(( eta_s % 60 ))s"
            else
                eta="$(( eta_s / 3600 ))h $(( (eta_s % 3600) / 60 ))m"
            fi
        else
            rate=0; eta="--"
        fi
    else
        rate=0; eta="--"
    fi

    PREV_LAG[$topic]=$lag
    PREV_TIME[$topic]=$now

    printf "  %-15s  produced=%-10s  lag=%-10s  consumed=%3d%%  rate=%-8s  ETA=%s\n" \
        "$label" \
        "$(format_number $leo)" \
        "$(format_number $lag)" \
        "$pct" \
        "$(format_number $rate)/s" \
        "$eta"
}

echo "Starting monitor (refresh every ${INTERVAL}s, Ctrl+C to stop)"
echo ""

while true; do
    clear
    echo "╔══════════════════════════════════════════════════════════════════╗"
    printf  "║  Migration Monitor — %-44s ║\n" "$(date '+%Y-%m-%d %H:%M:%S')"
    echo "╚══════════════════════════════════════════════════════════════════╝"

    echo ""
    echo "--- Connectors ---"
    case "$TARGET" in
        sqlserver) connector_status "prod-sqlserver-migration" ;;
        postgres)  connector_status "prod-postgres-migration" ;;
        both)
            connector_status "prod-sqlserver-migration"
            connector_status "prod-postgres-migration"
            ;;
    esac

    echo ""
    echo "--- Consumer lag (messages not yet read by downstream consumers) ---"
    echo "  A lag of 0 means all produced messages have been consumed."
    echo "  During snapshot, lag equals total rows remaining to consume."
    echo ""
    case "$TARGET" in
        sqlserver) topic_lag "$MSSQL_TOPIC" "$MSSQL_GROUP" "SQL Server" ;;
        postgres)  topic_lag "$PG_TOPIC"    "$PG_GROUP"    "PostgreSQL" ;;
        both)
            topic_lag "$MSSQL_TOPIC" "$MSSQL_GROUP" "SQL Server"
            topic_lag "$PG_TOPIC"    "$PG_GROUP"    "PostgreSQL"
            ;;
    esac

    echo ""
    echo "--- Quick links ---"
    echo "  Kafka UI:         http://localhost:${KAFKA_UI_PORT:-8084}"
    echo "  Connect REST:     $CONNECT_URL/connectors"
    echo "  Schema Registry:  ${SCHEMA_REGISTRY_URL:-http://localhost:8081}/subjects"
    echo ""
    echo "  Refreshing in ${INTERVAL}s...  (Ctrl+C to stop)"

    sleep "$INTERVAL"
done
