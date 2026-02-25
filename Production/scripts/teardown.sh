#!/usr/bin/env bash
# =============================================================================
# teardown.sh — Clean shutdown of the migration stack
# =============================================================================
# Safely stops connectors before stopping containers to avoid offset loss.
# Optionally drops PostgreSQL replication slots.
#
# Usage:
#   ./scripts/teardown.sh             # Stop connectors + containers (keep volumes)
#   ./scripts/teardown.sh --volumes   # Stop everything AND delete all data volumes
#   ./scripts/teardown.sh --connectors-only  # Stop connectors only (leave infra up)
#
# WARNING: --volumes deletes ALL Kafka, SQL Server, and PostgreSQL data.
#          Only use this to reset the environment from scratch.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONNECT_URL="${CONNECT_URL:-http://localhost:8083}"
ENV_FILE="${ROOT_DIR}/infra/.env"

MODE="${1:---default}"

# ---------------------------------------------------------------------------
# Step 1: Gracefully stop all Debezium connectors
# ---------------------------------------------------------------------------
stop_connectors() {
    echo "=== Stopping Debezium connectors ==="
    CONNECTORS=$(curl -s "$CONNECT_URL/connectors" 2>/dev/null || echo "[]")
    NAMES=$(echo "$CONNECTORS" | python3 -c \
        "import sys,json; [print(c) for c in json.load(sys.stdin)]" 2>/dev/null || true)

    if [[ -z "$NAMES" ]]; then
        echo "  No connectors running."
        return
    fi

    for name in $NAMES; do
        echo "  Stopping: $name"
        # PUT /pause first — flushes in-flight offsets before deletion
        curl -sf -X PUT "$CONNECT_URL/connectors/$name/pause" > /dev/null 2>&1 || true
        sleep 2
        curl -sf -X DELETE "$CONNECT_URL/connectors/$name" > /dev/null 2>&1 || true
        echo "  Deleted:  $name"
    done
    echo "  All connectors stopped."
}

# ---------------------------------------------------------------------------
# Step 2: Drop PostgreSQL replication slots (prevent WAL retention bloat)
#
# Decision: always drop slots on teardown
#   An unconsumed replication slot causes PostgreSQL to retain all WAL since
#   the slot's confirmed_flush_lsn. On a busy database this fills the disk
#   within hours. Dropping the slot on teardown is a hard requirement.
# ---------------------------------------------------------------------------
drop_pg_slots() {
    echo ""
    echo "=== Dropping PostgreSQL replication slots ==="

    SLOTS=$(docker exec postgres psql \
        -U "${POSTGRES_USER:-postgres}" \
        -d "${POSTGRES_DB:-benchdb}" \
        -t -c "SELECT slot_name FROM pg_replication_slots;" 2>/dev/null \
        | tr -d ' ' | grep -v '^$' || true)

    if [[ -z "$SLOTS" ]]; then
        echo "  No active replication slots."
        return
    fi

    for slot in $SLOTS; do
        echo "  Dropping slot: $slot"
        docker exec postgres psql \
            -U "${POSTGRES_USER:-postgres}" \
            -d "${POSTGRES_DB:-benchdb}" \
            -c "SELECT pg_drop_replication_slot('$slot');" > /dev/null 2>&1 || \
            echo "  WARNING: Could not drop slot $slot (may already be gone)"
    done
    echo "  All replication slots dropped."
}

# ---------------------------------------------------------------------------
# Step 3: Stop Docker containers
# ---------------------------------------------------------------------------
stop_containers() {
    echo ""
    echo "=== Stopping containers ==="
    if [[ -f "$ENV_FILE" ]]; then
        docker compose \
            --project-directory "$ROOT_DIR/infra" \
            --env-file "$ENV_FILE" \
            down
    else
        docker compose \
            --project-directory "$ROOT_DIR/infra" \
            down
    fi
    echo "  Containers stopped."
}

stop_containers_and_volumes() {
    echo ""
    echo "=== Stopping containers AND removing volumes ==="
    echo "  WARNING: This will delete all Kafka, SQL Server, and PostgreSQL data!"
    read -r -p "  Are you sure? (type 'yes' to confirm): " confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "  Aborted."
        exit 0
    fi

    if [[ -f "$ENV_FILE" ]]; then
        docker compose \
            --project-directory "$ROOT_DIR/infra" \
            --env-file "$ENV_FILE" \
            down --volumes
    else
        docker compose \
            --project-directory "$ROOT_DIR/infra" \
            down --volumes
    fi
    echo "  Containers and volumes removed."
}

# ---------------------------------------------------------------------------
# Load .env for POSTGRES_USER / POSTGRES_DB if available
# ---------------------------------------------------------------------------
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; } || true

# ---------------------------------------------------------------------------
# Execute based on mode
# ---------------------------------------------------------------------------
case "$MODE" in
    --connectors-only)
        stop_connectors
        drop_pg_slots
        ;;
    --volumes)
        stop_connectors
        drop_pg_slots
        stop_containers_and_volumes
        ;;
    *)
        stop_connectors
        drop_pg_slots
        stop_containers
        ;;
esac

echo ""
echo "=== Teardown complete ==="
