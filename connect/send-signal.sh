#!/bin/bash
# =============================================================================
# send-signal.sh
# =============================================================================
# Sends snapshot signals to the Debezium connector via Kafka.
# Supports triggering incremental snapshots with optional row-level filtering.
#
# Usage:
#   ./connect/send-signal.sh snapshot Production.Product
#   ./connect/send-signal.sh snapshot "Production.Product,Person.Person"
#   ./connect/send-signal.sh snapshot Production.Product "ProductID > 500"
#   ./connect/send-signal.sh stop-snapshot Production.Product
#
# Prerequisites:
#   - Kafka broker must be running
#   - aw-signal topic must exist (created during setup)
#   - Connector must be deployed with signal config enabled
# =============================================================================

SIGNAL_TYPE=$1
TABLES=$2
CONDITION=$3

# Validate arguments
if [ -z "$SIGNAL_TYPE" ] || [ -z "$TABLES" ]; then
    echo "Usage: $0 <snapshot|stop-snapshot> <table[,table2,...]> [additional-condition]"
    echo ""
    echo "Examples:"
    echo "  # Snapshot a single table:"
    echo "  $0 snapshot Production.Product"
    echo ""
    echo "  # Snapshot multiple tables:"
    echo "  $0 snapshot \"Production.Product,Person.Person\""
    echo ""
    echo "  # Snapshot with row filter:"
    echo "  $0 snapshot Production.Product \"ProductID > 500\""
    echo ""
    echo "  # Stop an in-progress snapshot:"
    echo "  $0 stop-snapshot Production.Product"
    exit 1
fi

# Generate unique signal ID (timestamp-based)
SIGNAL_ID="signal-$(date +%s)"

# The Kafka signal key must match the connector's topic.prefix (not the signal ID)
# This is how Debezium identifies which connector the signal is for
SIGNAL_KEY="aw"

# Build data-collections array for JSON payload
IFS=',' read -ra TABLE_ARRAY <<< "$TABLES"
DATA_COLLECTIONS="["
for table in "${TABLE_ARRAY[@]}"; do
    # Trim whitespace
    table=$(echo "$table" | xargs)
    DATA_COLLECTIONS+="\"AdventureWorks2019.$table\","
done
# Remove trailing comma and close array
DATA_COLLECTIONS="${DATA_COLLECTIONS%,}]"

# Build signal payload based on signal type
if [ "$SIGNAL_TYPE" = "snapshot" ]; then
    if [ -n "$CONDITION" ]; then
        # Incremental snapshot WITH row filter
        PAYLOAD="{\"id\":\"$SIGNAL_ID\",\"type\":\"execute-snapshot\",\"data\":{\"data-collections\":$DATA_COLLECTIONS,\"type\":\"INCREMENTAL\",\"additional-condition\":\"$CONDITION\"}}"
    else
        # Incremental snapshot WITHOUT row filter
        PAYLOAD="{\"id\":\"$SIGNAL_ID\",\"type\":\"execute-snapshot\",\"data\":{\"data-collections\":$DATA_COLLECTIONS,\"type\":\"INCREMENTAL\"}}"
    fi
elif [ "$SIGNAL_TYPE" = "stop-snapshot" ]; then
    # Stop snapshot signal
    PAYLOAD="{\"id\":\"$SIGNAL_ID\",\"type\":\"stop-snapshot\",\"data\":{\"data-collections\":$DATA_COLLECTIONS,\"type\":\"INCREMENTAL\"}}"
else
    echo "ERROR: Invalid signal type: $SIGNAL_TYPE"
    echo "Valid types: snapshot, stop-snapshot"
    exit 1
fi

# Display what we're about to send
echo "============================================="
echo " Sending Signal to Debezium Connector"
echo "============================================="
echo "Signal Type:  $SIGNAL_TYPE"
echo "Signal ID:    $SIGNAL_ID"
echo "Tables:       $TABLES"
if [ -n "$CONDITION" ]; then
    echo "Condition:    $CONDITION"
fi
echo ""
echo "Payload:"
echo "$PAYLOAD" | python3 -m json.tool 2>/dev/null || echo "$PAYLOAD"
echo ""

# Send to Kafka signal topic
# Note: The key must be the connector's topic.prefix (not the signal ID)
# This is how Debezium identifies which connector should process the signal
# We use echo with a key:value format for kafka-console-producer
echo "${SIGNAL_KEY}:${PAYLOAD}" | docker exec -i kafka kafka-console-producer \
    --bootstrap-server kafka:29092 \
    --topic aw-signal \
    --property "parse.key=true" \
    --property "key.separator=:"

if [ $? -eq 0 ]; then
    echo ""
    echo "SUCCESS: Signal sent to 'aw-signal' topic."
    echo ""
    echo "---------------------------------------------"
    echo "Next steps:"
    echo "  1. Monitor connector logs:"
    echo "     docker logs -f kafka-connect"
    echo ""
    echo "  2. Check for snapshot events in topics:"
    echo "     docker exec kafka kafka-console-consumer \\"
    echo "       --bootstrap-server kafka:29092 \\"
    echo "       --topic aw.AdventureWorks2019.${TABLE_ARRAY[0]} \\"
    echo "       --from-beginning --max-messages 10"
    echo ""
    echo "  3. Verify signal was consumed:"
    echo "     docker exec kafka kafka-consumer-groups \\"
    echo "       --bootstrap-server kafka:29092 \\"
    echo "       --group debezium-signal-consumer --describe"
    echo "---------------------------------------------"
else
    echo ""
    echo "ERROR: Failed to send signal to Kafka."
    exit 1
fi
