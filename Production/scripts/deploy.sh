#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Full production deployment in the correct order
# =============================================================================
# Deploys the entire CDC migration stack:
#   1. Start infrastructure (docker compose)
#   2. Wait for all services to be healthy
#   3. Pre-create Kafka topics with production settings
#   4. Deploy Debezium connector(s)
#
# Usage:
#   ./scripts/deploy.sh sqlserver     # Deploy SQL Server connector only
#   ./scripts/deploy.sh postgres      # Deploy PostgreSQL connector only
#   ./scripts/deploy.sh both          # Deploy both connectors
#
# Run from the Production/ directory.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONNECT_URL="${CONNECT_URL:-http://localhost:8083}"
ENV_FILE="${ROOT_DIR}/infra/.env"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found."
    echo "  Copy infra/.env.example to infra/.env and fill in values."
    exit 1
fi
set -a; source "$ENV_FILE"; set +a

# ---------------------------------------------------------------------------
# Step 1: Start infrastructure
# ---------------------------------------------------------------------------
echo "=== [1/4] Starting infrastructure ==="
docker compose \
    --project-directory "$ROOT_DIR/infra" \
    --env-file "$ENV_FILE" \
    up -d --build

# ---------------------------------------------------------------------------
# Step 2: Wait for Kafka Connect to be healthy
# ---------------------------------------------------------------------------
echo ""
echo "=== [2/4] Waiting for Kafka Connect to be ready ==="
RETRIES=60
for i in $(seq 1 $RETRIES); do
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$CONNECT_URL/connectors" 2>/dev/null || echo "000")
    if [[ "$HTTP" == "200" ]]; then
        echo "  Kafka Connect is ready (attempt $i)."
        break
    fi
    if [[ $i -eq $RETRIES ]]; then
        echo "ERROR: Kafka Connect did not become ready after ${RETRIES}s."
        echo "  Check: docker logs kafka-connect"
        exit 1
    fi
    echo "  Attempt $i/$RETRIES — HTTP $HTTP, retrying in 5s..."
    sleep 5
done

# ---------------------------------------------------------------------------
# Step 3: Pre-create topics
# ---------------------------------------------------------------------------
echo ""
echo "=== [3/4] Pre-creating Kafka topics ==="
TARGET="${1:-both}"
bash "$ROOT_DIR/topics/create-topics.sh" "$TARGET"

# ---------------------------------------------------------------------------
# Step 4: Deploy connector(s)
# ---------------------------------------------------------------------------
echo ""
echo "=== [4/4] Deploying Debezium connector(s): $TARGET ==="

deploy_connector() {
    local name="$1"
    local config_file="$2"

    # Delete existing connector if present (clean slate)
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$CONNECT_URL/connectors/$name" 2>/dev/null || echo "000")
    if [[ "$HTTP" == "200" ]]; then
        echo "  Deleting existing connector: $name"
        curl -sf -X DELETE "$CONNECT_URL/connectors/$name" > /dev/null
        sleep 3
    fi

    echo "  Deploying: $name"
    RESPONSE=$(curl -sf -X POST \
        -H "Content-Type: application/json" \
        --data "@$config_file" \
        "$CONNECT_URL/connectors")
    echo "$RESPONSE" | python3 -m json.tool --no-ensure-ascii 2>/dev/null || echo "$RESPONSE"
    echo ""

    # Wait for RUNNING state
    echo "  Waiting for $name to reach RUNNING state..."
    for i in $(seq 1 30); do
        STATE=$(curl -s "$CONNECT_URL/connectors/$name/status" 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('connector',{}).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
        if [[ "$STATE" == "RUNNING" ]]; then
            echo "  $name is RUNNING."
            break
        fi
        if [[ $i -eq 30 ]]; then
            echo "  WARNING: $name did not reach RUNNING state. Check status."
        fi
        echo "  State: $STATE (attempt $i/30, retrying in 3s...)"
        sleep 3
    done
}

case "$TARGET" in
    sqlserver)
        deploy_connector "prod-sqlserver-migration" \
            "$ROOT_DIR/connectors/sqlserver/connector.json"
        ;;
    postgres)
        deploy_connector "prod-postgres-migration" \
            "$ROOT_DIR/connectors/postgres/connector.json"
        ;;
    both)
        deploy_connector "prod-sqlserver-migration" \
            "$ROOT_DIR/connectors/sqlserver/connector.json"
        deploy_connector "prod-postgres-migration" \
            "$ROOT_DIR/connectors/postgres/connector.json"
        ;;
esac

echo ""
echo "=== Deployment complete ==="
echo "  Monitor:  ./scripts/monitor.sh"
echo "  Health:   ./scripts/health-check.sh"
echo "  Kafka UI: http://localhost:${KAFKA_UI_PORT:-8084}"
