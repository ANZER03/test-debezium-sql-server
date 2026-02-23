#!/usr/bin/env bash
# =============================================================================
# deploy-benchmark.sh
# Purpose: Deploy/delete the 3 benchmark Debezium connectors.
#
# Usage:
#   ./connect/deploy-benchmark.sh deploy    # Deploy all 3 connectors
#   ./connect/deploy-benchmark.sh delete    # Delete all 3 connectors
#   ./connect/deploy-benchmark.sh status    # Show status of all 3 connectors
# =============================================================================

set -euo pipefail

CONNECT_URL="http://localhost:8083"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONNECTORS=(
    "bench-json-schema:${SCRIPT_DIR}/connector-json-schema.json"
    "bench-json-noschema:${SCRIPT_DIR}/connector-json-noschema.json"
    "bench-avro:${SCRIPT_DIR}/connector-avro.json"
)

wait_for_connect() {
    echo "Waiting for Kafka Connect to be ready..."
    for i in $(seq 1 30); do
        if curl -sf "${CONNECT_URL}/connectors" > /dev/null 2>&1; then
            echo "Kafka Connect is ready."
            return 0
        fi
        echo "  attempt $i/30 - not ready yet, retrying in 3s..."
        sleep 3
    done
    echo "ERROR: Kafka Connect did not become ready in time."
    exit 1
}

deploy_connectors() {
    wait_for_connect
    for entry in "${CONNECTORS[@]}"; do
        name="${entry%%:*}"
        config_file="${entry##*:}"

        # Delete if already exists
        if curl -sf "${CONNECT_URL}/connectors/${name}" > /dev/null 2>&1; then
            echo "Deleting existing connector: ${name}"
            curl -sf -X DELETE "${CONNECT_URL}/connectors/${name}"
            sleep 2
        fi

        echo "Deploying connector: ${name} (${config_file})"
        curl -sf -X POST \
            -H "Content-Type: application/json" \
            --data "@${config_file}" \
            "${CONNECT_URL}/connectors" | python3 -m json.tool --no-ensure-ascii
        echo ""
    done

    echo ""
    echo "All connectors deployed. Waiting 5s before status check..."
    sleep 5
    show_status
}

delete_connectors() {
    for entry in "${CONNECTORS[@]}"; do
        name="${entry%%:*}"
        if curl -sf "${CONNECT_URL}/connectors/${name}" > /dev/null 2>&1; then
            echo "Deleting connector: ${name}"
            curl -sf -X DELETE "${CONNECT_URL}/connectors/${name}"
            echo "  Deleted."
        else
            echo "Connector not found (already deleted?): ${name}"
        fi
    done
}

show_status() {
    for entry in "${CONNECTORS[@]}"; do
        name="${entry%%:*}"
        echo "--- ${name} ---"
        STATUS=$(curl -sf "${CONNECT_URL}/connectors/${name}/status" 2>/dev/null || echo '{"error":"not found"}')
        echo "${STATUS}" | python3 -m json.tool --no-ensure-ascii 2>/dev/null || echo "${STATUS}"
        echo ""
    done
}

CMD="${1:-status}"
case "$CMD" in
    deploy)  deploy_connectors ;;
    delete)  delete_connectors ;;
    status)  show_status ;;
    *)
        echo "Usage: $0 {deploy|delete|status}"
        exit 1
        ;;
esac
