#!/bin/bash
# =============================================================================
# deploy-connector.sh
# =============================================================================
# Registers (or updates) the Debezium SQL Server CDC connector with Kafka Connect.
# This script reads the connector configuration from connector-config.json
# and sends it to the Kafka Connect REST API.
#
# Usage:
#   ./connect/deploy-connector.sh          # Deploy/update the connector
#   ./connect/deploy-connector.sh status   # Check connector status
#   ./connect/deploy-connector.sh delete   # Delete the connector
#   ./connect/deploy-connector.sh topics   # List CDC topics created by Debezium
#
# Prerequisites:
#   - All Docker containers must be running (docker-compose up -d)
#   - SQL Server must have AdventureWorks2019 restored with CDC enabled
#   - curl and jq must be installed on the host
# =============================================================================

# --- Configuration -----------------------------------------------------------

# Kafka Connect REST API base URL (port 8083 is mapped in docker-compose.yml)
CONNECT_URL="http://localhost:8083"

# Path to the connector JSON config file (relative to project root)
# We resolve the script's directory first so the script works from any location
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/connector-config.json"

# Connector name — must match the "name" field in connector-config.json
CONNECTOR_NAME="adventureworks-sqlserver-connector"

# Kafka bootstrap servers for topic listing (host-facing port)
KAFKA_BOOTSTRAP="localhost:9092"

# --- Helper: pretty-print JSON if jq is available ---------------------------
pretty_json() {
    # If jq is installed, use it; otherwise cat the raw JSON
    if command -v jq &> /dev/null; then
        jq .
    else
        cat
    fi
}

# --- Helper: wait for Kafka Connect to be ready -----------------------------
wait_for_connect() {
    echo "Waiting for Kafka Connect to be ready..."
    # Try up to 30 times (30 seconds) for the REST API to respond
    for i in $(seq 1 30); do
        # curl -s: silent, -o /dev/null: discard output, -w: print HTTP code
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${CONNECT_URL}/connectors" 2>/dev/null)
        if [ "$HTTP_CODE" = "200" ]; then
            echo "Kafka Connect is ready."
            return 0
        fi
        echo "  Attempt $i/30 - HTTP $HTTP_CODE (waiting 1s...)"
        sleep 1
    done
    # If we get here, Connect never became ready
    echo "ERROR: Kafka Connect did not become ready after 30 seconds."
    echo "Check if the kafka-connect container is running: docker ps"
    exit 1
}

# --- Helper: check that the config file exists -------------------------------
check_config_file() {
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "ERROR: Config file not found at: $CONFIG_FILE"
        echo "Make sure connector-config.json exists in the connect/ directory."
        exit 1
    fi
}

# --- Command: deploy (default) ----------------------------------------------
deploy_connector() {
    echo "============================================="
    echo " Deploying Debezium SQL Server CDC Connector"
    echo "============================================="

    # Ensure prerequisites are met
    check_config_file
    wait_for_connect

    # Check if connector already exists (GET /connectors/<name>)
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${CONNECT_URL}/connectors/${CONNECTOR_NAME}" 2>/dev/null)

    if [ "$HTTP_CODE" = "200" ]; then
        # Connector exists — update its config using PUT
        echo ""
        echo "Connector '${CONNECTOR_NAME}' already exists. Updating configuration..."
        echo ""

        # Extract just the "config" object from the JSON file (PUT expects config only, not the wrapper)
        # jq extracts .config; if jq is missing, we fall back to Python for the extraction
        if command -v jq &> /dev/null; then
            CONFIG_PAYLOAD=$(jq '.config' "$CONFIG_FILE")
        else
            CONFIG_PAYLOAD=$(python3 -c "import json,sys; data=json.load(open('$CONFIG_FILE')); print(json.dumps(data['config']))")
        fi

        # PUT /connectors/<name>/config — updates the running connector config
        RESPONSE=$(curl -s -w "\n%{http_code}" \
            -X PUT \
            -H "Content-Type: application/json" \
            -d "$CONFIG_PAYLOAD" \
            "${CONNECT_URL}/connectors/${CONNECTOR_NAME}/config")

    else
        # Connector does not exist — create it using POST
        echo ""
        echo "Creating new connector '${CONNECTOR_NAME}'..."
        echo ""

        # POST /connectors — expects the full JSON with "name" and "config" fields
        RESPONSE=$(curl -s -w "\n%{http_code}" \
            -X POST \
            -H "Content-Type: application/json" \
            -d @"$CONFIG_FILE" \
            "${CONNECT_URL}/connectors")
    fi

    # Split response body and HTTP status code (last line is the code)
    HTTP_BODY=$(echo "$RESPONSE" | head -n -1)
    HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)

    echo "HTTP Status: $HTTP_CODE"
    echo ""

    # Check if deployment succeeded (201 = created, 200 = updated)
    if [ "$HTTP_CODE" = "201" ] || [ "$HTTP_CODE" = "200" ]; then
        echo "SUCCESS: Connector deployed/updated successfully!"
        echo ""
        echo "Response:"
        echo "$HTTP_BODY" | pretty_json
        echo ""
        echo "---------------------------------------------"
        echo "Next steps:"
        echo "  1. Check connector status:  $0 status"
        echo "  2. List CDC topics:          $0 topics"
        echo "  3. View in Kafka UI:         http://localhost:8084"
        echo "---------------------------------------------"
    else
        echo "FAILED: Connector deployment returned HTTP $HTTP_CODE"
        echo ""
        echo "Error response:"
        echo "$HTTP_BODY" | pretty_json
        echo ""
        echo "Common issues:"
        echo "  - SQL Server not running or AdventureWorks not restored"
        echo "  - CDC not enabled on the database or tables"
        echo "  - Wrong credentials in connector-config.json"
        echo "  - Kafka brokers not reachable from the Connect container"
        exit 1
    fi
}

# --- Command: status ---------------------------------------------------------
check_status() {
    echo "============================================="
    echo " Connector Status: ${CONNECTOR_NAME}"
    echo "============================================="

    wait_for_connect

    # GET /connectors/<name>/status — returns connector and task states
    echo ""
    echo "Connector status:"
    curl -s "${CONNECT_URL}/connectors/${CONNECTOR_NAME}/status" | pretty_json

    echo ""
    echo "---------------------------------------------"
    echo "Task states explanation:"
    echo "  RUNNING  = actively capturing CDC changes"
    echo "  PAUSED   = connector is paused (resume via REST API)"
    echo "  FAILED   = connector encountered an error (check trace field)"
    echo "  UNASSIGNED = task not yet assigned to a worker"
    echo "---------------------------------------------"
}

# --- Command: delete ---------------------------------------------------------
delete_connector() {
    echo "============================================="
    echo " Deleting Connector: ${CONNECTOR_NAME}"
    echo "============================================="

    wait_for_connect

    # DELETE /connectors/<name> — removes the connector and stops all its tasks
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -X DELETE \
        "${CONNECT_URL}/connectors/${CONNECTOR_NAME}")

    if [ "$HTTP_CODE" = "204" ]; then
        echo "SUCCESS: Connector '${CONNECTOR_NAME}' deleted."
        echo "Note: Existing Kafka topics with CDC data are NOT deleted."
    elif [ "$HTTP_CODE" = "404" ]; then
        echo "Connector '${CONNECTOR_NAME}' does not exist (already deleted?)."
    else
        echo "Unexpected HTTP status: $HTTP_CODE"
    fi
}

# --- Command: topics ---------------------------------------------------------
list_topics() {
    echo "============================================="
    echo " Kafka Topics (CDC-related)"
    echo "============================================="

    # Use docker exec to run kafka-topics inside the broker container
    # The confluent Kafka image places binaries in /usr/bin/ (not /opt/kafka/bin/)
    # Filter for topics starting with the "aw." prefix (our topic.prefix from config)
    echo ""
    echo "All topics with 'aw.' prefix (Debezium CDC topics):"
    echo ""
    docker exec kafka kafka-topics --bootstrap-server kafka:29092 \
        --list 2>/dev/null | grep "^aw\." || echo "  (no CDC topics found yet - connector may still be snapshotting)"

    echo ""
    echo "Schema history topic:"
    docker exec kafka kafka-topics --bootstrap-server kafka:29092 \
        --list 2>/dev/null | grep "schema-changes" || echo "  (not created yet)"

    echo ""
    echo "Kafka Connect internal topics:"
    docker exec kafka kafka-topics --bootstrap-server kafka:29092 \
        --list 2>/dev/null | grep "connect-" || echo "  (not found)"
    echo ""
}

# --- Main: parse command-line argument ---------------------------------------

# Default action is "deploy" if no argument is given
ACTION="${1:-deploy}"

case "$ACTION" in
    deploy)
        deploy_connector
        ;;
    status)
        check_status
        ;;
    delete)
        delete_connector
        ;;
    topics)
        list_topics
        ;;
    *)
        # Unknown command — show usage
        echo "Usage: $0 [deploy|status|delete|topics]"
        echo ""
        echo "  deploy  - Register or update the connector (default)"
        echo "  status  - Check connector and task status"
        echo "  delete  - Remove the connector"
        echo "  topics  - List CDC-related Kafka topics"
        exit 1
        ;;
esac
