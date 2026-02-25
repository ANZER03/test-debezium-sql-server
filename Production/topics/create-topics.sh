#!/usr/bin/env bash
# =============================================================================
# create-topics.sh — Pre-create Kafka topics before deploying connectors
# =============================================================================
# Run this script BEFORE deploying any Debezium connector.
#
# Why pre-create topics instead of letting Debezium auto-create them?
#   When Debezium auto-creates a topic, Kafka applies cluster defaults:
#     - partitions = num.partitions (default: 1)
#     - replication.factor = default.replication.factor (default: 1 in dev)
#     - retention = log.retention.hours (cluster default)
#   These defaults are wrong for production:
#     - RF=1 means one broker failure loses the migration data permanently
#     - 1 partition limits consumer parallelism to 1 consumer thread
#     - Default retention may be too short (data deleted before consumers read it)
#
# This script pre-creates topics with production-appropriate settings before
# the connector starts, so Debezium finds the topic already exists and uses it
# as-is rather than creating it with defaults.
#
# Usage:
#   ./topics/create-topics.sh [sqlserver|postgres|both]
#
# Prerequisites:
#   - kafka and kafka-broker-2 containers must be running and healthy
#   - Run from the Production/ directory
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment variables if needed
# ---------------------------------------------------------------------------
KAFKA_CONTAINER="${KAFKA_CONTAINER:-kafka}"
KAFKA_INTERNAL="${KAFKA_INTERNAL:-kafka:29092}"

# Topic parameters — benchmark-validated best values
PARTITIONS="${PARTITIONS:-4}"
REPLICATION_FACTOR="${REPLICATION_FACTOR:-2}"

# Retention: 7 days (168 hours). Large enough for consumers to catch up after
# snapshot; trim after migration is confirmed complete.
RETENTION_MS="${RETENTION_MS:-604800000}"

# Segment size: 512 MB. Larger segments reduce the number of segment files for
# a 400 MB+ migration dataset, lowering metadata overhead.
SEGMENT_BYTES="${SEGMENT_BYTES:-536870912}"

# Compression: lz4 at the topic level (overrides broker default if set differently)
COMPRESSION_TYPE="${COMPRESSION_TYPE:-lz4}"

# Topic names (must match topic.prefix + table path in connector configs)
MSSQL_DATA_TOPIC="prod_mssql.AdventureWorks2019.Sales.SalesOrderDetailBig"
MSSQL_SCHEMA_TOPIC="schema-changes.prod_mssql"

PG_DATA_TOPIC="prod_pg.public.sales_order_detail_big"
PG_SCHEMA_TOPIC="schema-changes.prod_pg"

# Connect internal topics — RF=2, compacted, single partition per topic
CONNECT_TOPICS=("connect-configs" "connect-offsets" "connect-status")

# ---------------------------------------------------------------------------
# Helper: create a topic (idempotent — skips if already exists)
# ---------------------------------------------------------------------------
create_topic() {
    local topic="$1"
    local partitions="${2:-$PARTITIONS}"
    local replication="${3:-$REPLICATION_FACTOR}"
    local extra_config="${4:-}"

    # Check if topic already exists
    if docker exec "$KAFKA_CONTAINER" kafka-topics \
        --bootstrap-server "$KAFKA_INTERNAL" \
        --list 2>/dev/null | grep -q "^${topic}$"; then
        echo "  [SKIP] Topic already exists: ${topic}"
        return 0
    fi

    local cmd="kafka-topics --bootstrap-server $KAFKA_INTERNAL \
        --create \
        --topic $topic \
        --partitions $partitions \
        --replication-factor $replication \
        --config retention.ms=$RETENTION_MS \
        --config segment.bytes=$SEGMENT_BYTES \
        --config compression.type=$COMPRESSION_TYPE"

    if [[ -n "$extra_config" ]]; then
        cmd="$cmd $extra_config"
    fi

    docker exec "$KAFKA_CONTAINER" bash -c "$cmd"
    echo "  [OK]   Created: ${topic} (partitions=${partitions}, rf=${replication})"
}

# ---------------------------------------------------------------------------
# Create Connect internal topics (compact, RF=2, 1 partition each)
# These must exist before any connector is deployed.
#
# Decision: cleanup.policy=compact for internal topics
#   connect-offsets and connect-configs use compaction so only the latest
#   value per key is retained. This prevents unbounded growth and ensures
#   the latest connector offset/config survives retention expiry.
# ---------------------------------------------------------------------------
create_connect_internal_topics() {
    echo ""
    echo "--- Kafka Connect internal topics ---"
    for topic in "${CONNECT_TOPICS[@]}"; do
        create_topic "$topic" 1 "$REPLICATION_FACTOR" \
            "--config cleanup.policy=compact --config min.cleanable.dirty.ratio=0.01"
    done
}

# ---------------------------------------------------------------------------
# Create SQL Server migration topics
#
# Decision: partitions=4 for the data topic
#   With 1 partition, only 1 consumer thread can read the migration data
#   simultaneously. With 4 partitions, up to 4 consumer instances (e.g.,
#   4 threads writing to a target database) can read in parallel.
#   The Debezium SqlServerConnector (tasks=1) writes to all 4 partitions via
#   key hash — no connector-side changes needed.
#
# Decision: partitions=1 for schema-history topic
#   The schema history topic must be strictly ordered (it replays DDL history
#   in sequence on connector restart). Multiple partitions would break DDL
#   ordering. Always keep schema-history topics at 1 partition.
# ---------------------------------------------------------------------------
create_sqlserver_topics() {
    echo ""
    echo "--- SQL Server migration topics ---"
    create_topic "$MSSQL_DATA_TOPIC"   "$PARTITIONS" "$REPLICATION_FACTOR"
    create_topic "$MSSQL_SCHEMA_TOPIC" 1             "$REPLICATION_FACTOR" \
        "--config cleanup.policy=compact"
}

# ---------------------------------------------------------------------------
# Create PostgreSQL migration topics
# Same partition/RF rationale as SQL Server.
# ---------------------------------------------------------------------------
create_postgres_topics() {
    echo ""
    echo "--- PostgreSQL migration topics ---"
    create_topic "$PG_DATA_TOPIC"   "$PARTITIONS" "$REPLICATION_FACTOR"
    create_topic "$PG_SCHEMA_TOPIC" 1             "$REPLICATION_FACTOR" \
        "--config cleanup.policy=compact"
}

# ---------------------------------------------------------------------------
# Describe topics after creation (verification)
# ---------------------------------------------------------------------------
describe_topics() {
    local topics=("$@")
    echo ""
    echo "--- Topic details ---"
    for topic in "${topics[@]}"; do
        echo ""
        docker exec "$KAFKA_CONTAINER" kafka-topics \
            --bootstrap-server "$KAFKA_INTERNAL" \
            --describe --topic "$topic" 2>/dev/null || echo "  (not found: $topic)"
    done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
TARGET="${1:-both}"

echo "============================================================"
echo " Kafka Topic Pre-Creation"
echo " Kafka:       $KAFKA_INTERNAL (via container: $KAFKA_CONTAINER)"
echo " Partitions:  $PARTITIONS"
echo " Replication: $REPLICATION_FACTOR"
echo " Retention:   ${RETENTION_MS}ms ($(( RETENTION_MS / 86400000 )) days)"
echo "============================================================"

create_connect_internal_topics

case "$TARGET" in
    sqlserver)
        create_sqlserver_topics
        describe_topics "$MSSQL_DATA_TOPIC" "$MSSQL_SCHEMA_TOPIC"
        ;;
    postgres)
        create_postgres_topics
        describe_topics "$PG_DATA_TOPIC" "$PG_SCHEMA_TOPIC"
        ;;
    both)
        create_sqlserver_topics
        create_postgres_topics
        describe_topics \
            "$MSSQL_DATA_TOPIC" "$MSSQL_SCHEMA_TOPIC" \
            "$PG_DATA_TOPIC"    "$PG_SCHEMA_TOPIC"
        ;;
    *)
        echo "Usage: $0 [sqlserver|postgres|both]"
        exit 1
        ;;
esac

echo ""
echo "Done. Topics are ready. You can now deploy connectors."
