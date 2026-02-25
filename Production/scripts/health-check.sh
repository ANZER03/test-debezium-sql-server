#!/usr/bin/env bash
# =============================================================================
# health-check.sh — Verify all stack components are healthy
# =============================================================================
# Checks every service and prints a pass/fail summary.
# Exit code 0 = all healthy. Exit code 1 = one or more failures.
#
# Usage:
#   ./scripts/health-check.sh
# =============================================================================

set -euo pipefail

CONNECT_URL="${CONNECT_URL:-http://localhost:8083}"
SCHEMA_REGISTRY_URL="${SCHEMA_REGISTRY_URL:-http://localhost:8081}"
KAFKA_UI_URL="${KAFKA_UI_URL:-http://localhost:8084}"

PASS=0
FAIL=0

check() {
    local label="$1"
    local result="$2"   # "ok" or anything else = fail
    local detail="${3:-}"

    if [[ "$result" == "ok" ]]; then
        printf "  %-35s [PASS]\n" "$label"
        (( PASS++ )) || true
    else
        printf "  %-35s [FAIL]  %s\n" "$label" "$detail"
        (( FAIL++ )) || true
    fi
}

http_ok() {
    local url="$1"
    local code
    code=$(curl -sf -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    [[ "$code" == "200" ]] && echo "ok" || echo "http_$code"
}

docker_healthy() {
    local container="$1"
    local state
    state=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "missing")
    [[ "$state" == "healthy" ]] && echo "ok" || echo "$state"
}

connector_running() {
    local name="$1"
    local state
    state=$(curl -s "$CONNECT_URL/connectors/$name/status" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('connector',{}).get('state','MISSING'))" 2>/dev/null \
        || echo "MISSING")
    [[ "$state" == "RUNNING" ]] && echo "ok" || echo "$state"
}

echo "============================================================"
echo " Health Check — $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
echo ""
echo "--- Docker containers ---"
check "kafka"              "$(docker_healthy kafka)"
check "kafka-broker-2"    "$(docker_healthy kafka-broker-2)"
check "schema-registry"   "$(docker_healthy schema-registry)"
check "kafka-connect"     "$(docker_healthy kafka-connect)"
check "sqlserver"         "$(docker_healthy sqlserver)"
check "postgres"          "$(docker_healthy postgres)"

echo ""
echo "--- REST APIs ---"
check "Kafka Connect REST"       "$(http_ok "$CONNECT_URL/connectors")"
check "Schema Registry REST"     "$(http_ok "$SCHEMA_REGISTRY_URL/subjects")"
check "Kafka UI"                 "$(http_ok "$KAFKA_UI_URL")"

echo ""
echo "--- Connectors ---"
CONNECTORS=$(curl -s "$CONNECT_URL/connectors" 2>/dev/null || echo "[]")
if [[ "$CONNECTORS" == "[]" || "$CONNECTORS" == "" ]]; then
    printf "  %-35s [WARN]  No connectors deployed\n" "deployed connectors"
else
    for name in $(echo "$CONNECTORS" | python3 -c "import sys,json; [print(c) for c in json.load(sys.stdin)]" 2>/dev/null); do
        check "connector: $name" "$(connector_running "$name")"
    done
fi

echo ""
echo "--- Kafka topics ---"
TOPICS=$(docker exec kafka kafka-topics \
    --bootstrap-server kafka:29092 \
    --list 2>/dev/null || echo "")

for topic in \
    "prod_mssql.AdventureWorks2019.Sales.SalesOrderDetailBig" \
    "prod_pg.public.sales_order_detail_big" \
    "connect-configs" "connect-offsets" "connect-status"; do
    if echo "$TOPICS" | grep -q "^${topic}$"; then
        check "topic: $topic" "ok"
    else
        check "topic: $topic" "missing" "run topics/create-topics.sh"
    fi
done

echo ""
echo "--- Schema Registry schemas ---"
SUBJECTS=$(curl -s "$SCHEMA_REGISTRY_URL/subjects" 2>/dev/null || echo "[]")
SUBJECT_COUNT=$(echo "$SUBJECTS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
check "registered schemas" "$( [[ $SUBJECT_COUNT -gt 0 ]] && echo ok || echo none )" "$SUBJECT_COUNT subject(s) found"

echo ""
echo "============================================================"
echo " Summary: ${PASS} passed, ${FAIL} failed"
echo "============================================================"

[[ $FAIL -eq 0 ]]
