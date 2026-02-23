# =============================================================================
# consumer.py — CDC Event Consumer (reads Debezium CDC events from Kafka)
# =============================================================================
# This script consumes Change Data Capture (CDC) events from Kafka topics
# published by the Debezium SQL Server connector. It parses the Debezium
# envelope format and displays human-readable change summaries.
#
# Part 2 — Avro + Confluent Schema Registry:
# Messages are serialized in Avro binary format by the Debezium connector using
# io.confluent.connect.avro.AvroConverter. Each message starts with:
#   [0x00][schema_id: 4 bytes][avro binary payload]
# The AvroDeserializer fetches the schema from Schema Registry by ID
# (on first message for each schema version) and deserializes to a Python dict.
#
# Debezium Avro envelope structure (same as JSON, now as a Python dict):
#   {
#     "before": { ... } or None,   <-- row data BEFORE the change (null for inserts)
#     "after":  { ... } or None,   <-- row data AFTER the change (null for deletes)
#     "source": { ... },           <-- metadata (database, table, LSN, timestamp)
#     "op": "c|u|d|r",             <-- operation type
#     "ts_ms": 1234567890,         <-- Debezium processing timestamp
#     "transaction": { ... }       <-- transaction metadata (may be None)
#   }
#
# Note: There is NO "schema" wrapper key — AvroDeserializer handles schema
# resolution transparently via Schema Registry. The dict is the payload directly.
#
# Operation types:
#   r = read (initial snapshot)
#   c = create (INSERT)
#   u = update (UPDATE)
#   d = delete (DELETE)
#
# Usage:
#   python3 consumer/consumer.py                    # Consume all 5 CDC topics
#   python3 consumer/consumer.py --topics Product   # Consume only Product topic
#   python3 consumer/consumer.py --from-beginning   # Start from earliest offset
#
# Prerequisites:
#   - Kafka running with CDC topics created by Debezium (Avro format)
#   - Schema Registry running at localhost:8081
#   - pip install "confluent-kafka[avro]"  (see requirements.txt)
# =============================================================================

import sys  # For command-line argument handling
import signal  # For graceful shutdown on Ctrl+C
import argparse  # For parsing command-line arguments
import base64  # For decoding Debezium's base64-encoded decimal/money fields
from datetime import datetime  # For timestamp formatting
from confluent_kafka import KafkaError, KafkaException, DeserializingConsumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer

# =============================================================================
# Configuration
# =============================================================================

# Kafka bootstrap servers — connect to brokers via host-mapped ports
# Matches the ports in docker-compose.yml: kafka:9092, kafka-broker-2:9093
KAFKA_BOOTSTRAP_SERVERS = "localhost:9092,localhost:9093"

# Schema Registry URL — must match CONNECT_VALUE_CONVERTER_SCHEMA_REGISTRY_URL
# in docker-compose.yml (schema-registry:8081 inside Docker, localhost:8081 on host)
SCHEMA_REGISTRY_URL = "http://localhost:8081"

# Consumer group ID — consumers in the same group share partitions
# Each group maintains its own offset tracking
CONSUMER_GROUP_ID = "cdc-consumer-group"

# Topic prefix — must match topic.prefix in connector-config.json
# Debezium creates topics as: <prefix>.<database>.<schema>.<table>
TOPIC_PREFIX = "aw"

# Database name — matches database.names in connector-config.json
DATABASE_NAME = "AdventureWorks2019"

# Map of short table names to their full Kafka topic names
# These are the 5 tables with CDC enabled in SQL Server
CDC_TOPICS = {
    "Person": f"{TOPIC_PREFIX}.{DATABASE_NAME}.Person.Person",
    "Customer": f"{TOPIC_PREFIX}.{DATABASE_NAME}.Sales.Customer",
    "SalesOrderHeader": f"{TOPIC_PREFIX}.{DATABASE_NAME}.Sales.SalesOrderHeader",
    "SalesOrderDetail": f"{TOPIC_PREFIX}.{DATABASE_NAME}.Sales.SalesOrderDetail",
    "Product": f"{TOPIC_PREFIX}.{DATABASE_NAME}.Production.Product",
}

# Human-readable names for Debezium operation codes
OPERATION_NAMES = {
    "r": "SNAPSHOT",  # Initial snapshot read (existing rows when connector first starts)
    "c": "INSERT",  # New row inserted
    "u": "UPDATE",  # Existing row updated
    "d": "DELETE",  # Row deleted
}

# =============================================================================
# Debezium envelope parsing helpers
# =============================================================================


def decode_debezium_decimal(value):
    """Decode a Debezium base64-encoded decimal/money value.

    With Avro, Debezium encodes SQL Server money/decimal columns using the
    Avro 'bytes' type with a 'logicalType' of 'decimal'. The confluent-kafka
    AvroDeserializer decodes these to Python bytes objects (unscaled integer
    in big-endian two's complement). We apply scale=4 for money columns.

    Args:
        value: bytes object (Avro decimal), base64 str (legacy), or numeric

    Returns:
        Decoded float value, or the original value if decoding fails
    """
    # If it's already a number, return as-is
    if isinstance(value, (int, float)):
        return value

    # Handle Avro-decoded bytes (decimal logical type)
    if isinstance(value, bytes):
        try:
            int_val = int.from_bytes(value, byteorder="big", signed=True)
            scale = 4  # SQL Server money type uses scale=4
            return int_val / (10**scale)
        except Exception:
            return value

    # Handle legacy base64 strings (from JSON converter)
    if isinstance(value, str):
        try:
            raw_bytes = base64.b64decode(value)
            int_val = int.from_bytes(raw_bytes, byteorder="big", signed=True)
            scale = 4
            return int_val / (10**scale)
        except Exception:
            return value

    return value


def parse_debezium_timestamp(ts_ms):
    """Convert a Debezium timestamp (milliseconds since epoch) to human-readable format.

    Args:
        ts_ms: timestamp in milliseconds since Unix epoch

    Returns:
        Formatted datetime string in local time
    """
    if ts_ms is None:
        return "N/A"
    try:
        # Convert milliseconds to seconds for datetime conversion
        dt = datetime.fromtimestamp(ts_ms / 1000.0)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return str(ts_ms)


def extract_key_fields(table_short_name, row_data):
    """Extract the most relevant fields from a row for display.

    Different tables have different primary keys and interesting columns.
    This function picks the most useful fields for each table type.

    Args:
        table_short_name: short name like 'Person', 'Product', etc.
        row_data: dict of column_name -> value from the Debezium event

    Returns:
        dict of selected key fields for display
    """
    if row_data is None:
        return {}

    # Define which fields to display for each table
    # These are the most useful columns for understanding each change
    display_fields = {
        "Person": [
            "BusinessEntityID",
            "PersonType",
            "FirstName",
            "LastName",
            "EmailPromotion",
        ],
        "Customer": [
            "CustomerID",
            "PersonID",
            "StoreID",
            "TerritoryID",
            "AccountNumber",
        ],
        "SalesOrderHeader": [
            "SalesOrderID",
            "OrderDate",
            "Status",
            "CustomerID",
            "TotalDue",
            "SalesOrderNumber",
        ],
        "SalesOrderDetail": [
            "SalesOrderID",
            "SalesOrderDetailID",
            "ProductID",
            "OrderQty",
            "UnitPrice",
            "LineTotal",
        ],
        "Product": [
            "ProductID",
            "Name",
            "ProductNumber",
            "Color",
            "StandardCost",
            "ListPrice",
        ],
    }

    # Get the list of fields to show for this table (default: show all fields)
    fields_to_show = display_fields.get(table_short_name, list(row_data.keys()))

    # Build a filtered dict with only the selected fields
    result = {}
    for field in fields_to_show:
        if field in row_data:
            value = row_data[field]
            # Decode Avro bytes-encoded decimal values (money, decimal columns)
            if field in (
                "StandardCost",
                "ListPrice",
                "UnitPrice",
                "UnitPriceDiscount",
                "LineTotal",
                "SubTotal",
                "TaxAmt",
                "Freight",
                "TotalDue",
            ):
                value = decode_debezium_decimal(value)
            result[field] = value

    return result


def get_table_short_name(topic):
    """Extract a short table name from a Kafka topic name.

    Example: 'aw.AdventureWorks2019.Person.Person' -> 'Person'
    Example: 'aw.AdventureWorks2019.Sales.SalesOrderHeader' -> 'SalesOrderHeader'

    Args:
        topic: full Kafka topic name

    Returns:
        short table name string
    """
    # Topic format: <prefix>.<database>.<schema>.<table>
    # Split by '.' and take the last part (table name)
    parts = topic.split(".")
    if len(parts) >= 4:
        return parts[-1]  # Last segment is the table name
    return topic  # Fallback: return the full topic name


# =============================================================================
# Message processing — parse and display each CDC event
# =============================================================================


def process_message(msg):
    """Parse a Debezium Avro CDC message and print a human-readable summary.

    With Avro, msg.value() returns a Python dict already deserialized by
    AvroDeserializer — no manual JSON parsing needed.

    Args:
        msg: Kafka message object from consumer.poll()
    """
    # Get the topic name to identify which table this event is for
    topic = msg.topic()
    table_name = get_table_short_name(topic)

    # With AvroDeserializer, msg.value() is already a Python dict (Debezium envelope)
    # No need for json.loads() or .decode() — deserialization is done by the consumer
    event = msg.value()

    # Handle tombstone messages (null payload for deletes with log compaction)
    if event is None:
        print(
            f"  [{table_name}] Tombstone message (null value) — used for log compaction"
        )
        return

    # Extract operation type (c=create, u=update, d=delete, r=read/snapshot)
    # In Avro envelope, 'op' is a direct key (no 'payload' wrapper)
    op_code = event.get("op", "?")
    op_name = OPERATION_NAMES.get(op_code, f"UNKNOWN({op_code})")

    # Extract source metadata (database, schema, table, LSN, etc.)
    source = event.get("source") or {}
    lsn = source.get("commit_lsn", "?")

    # Extract the Debezium processing timestamp
    ts_ms = event.get("ts_ms")
    timestamp = parse_debezium_timestamp(ts_ms)

    # Extract before and after row data
    before = event.get("before")  # None for inserts and snapshots
    after = event.get("after")  # None for deletes

    # Print the event header
    print(f"  [{table_name}] {op_name} | {timestamp} | LSN: {lsn}")

    # Print the relevant row data based on operation type
    if op_code == "c" or op_code == "r":
        # INSERT or SNAPSHOT — show the new row data
        fields = extract_key_fields(table_name, after)
        print(f"    NEW: {fields}")

    elif op_code == "u":
        # UPDATE — show both old and new data for comparison
        before_fields = extract_key_fields(table_name, before)
        after_fields = extract_key_fields(table_name, after)

        # Find which fields actually changed
        changed = {}
        for key in after_fields:
            old_val = before_fields.get(key)
            new_val = after_fields.get(key)
            if old_val != new_val:
                changed[key] = f"{old_val} -> {new_val}"

        if changed:
            print(f"    CHANGED: {changed}")
        else:
            print(f"    AFTER: {after_fields}")
            print(f"    (no visible changes in display fields)")

    elif op_code == "d":
        # DELETE — show the deleted row data
        fields = extract_key_fields(table_name, before)
        print(f"    DELETED: {fields}")

    else:
        # Unknown operation — dump what we have
        print(f"    BEFORE: {before}")
        print(f"    AFTER:  {after}")


# =============================================================================
# Consumer setup and main loop
# =============================================================================


def parse_args():
    """Parse command-line arguments.

    Returns:
        argparse.Namespace with parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="CDC Event Consumer — reads Debezium Avro CDC events from Kafka"
    )

    # --topics: optional filter to consume only specific tables
    parser.add_argument(
        "--topics",
        nargs="+",
        choices=list(CDC_TOPICS.keys()),
        default=list(CDC_TOPICS.keys()),
        help="Table names to consume (default: all 5 tables). "
        "Choices: Person, Customer, SalesOrderHeader, SalesOrderDetail, Product",
    )

    # --from-beginning: start from the earliest offset (includes snapshot data)
    parser.add_argument(
        "--from-beginning",
        action="store_true",
        default=False,
        help="Start consuming from the earliest offset (includes all snapshot data). "
        "Default: start from latest (only new CDC changes)",
    )

    # --count: limit the number of messages to consume
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Stop after consuming N messages (0 = unlimited, default: 0)",
    )

    return parser.parse_args()


def main():
    """Main entry point: sets up the Kafka Avro consumer and processes CDC events."""

    # Parse command-line arguments
    args = parse_args()

    # Resolve which topics to subscribe to
    topics_to_consume = [CDC_TOPICS[name] for name in args.topics]

    # Determine the auto.offset.reset strategy
    # 'earliest' = read from beginning (includes snapshot), 'latest' = only new events
    offset_reset = "earliest" if args.from_beginning else "latest"

    print("=" * 70)
    print(" CDC Event Consumer — Debezium SQL Server CDC (Avro + Schema Registry)")
    print("=" * 70)
    print(f" Kafka:           {KAFKA_BOOTSTRAP_SERVERS}")
    print(f" Schema Registry: {SCHEMA_REGISTRY_URL}")
    print(f" Consumer Group:  {CONSUMER_GROUP_ID}")
    print(f" Offset Reset:    {offset_reset}")
    print(f" Format:          Avro (io.confluent.connect.avro.AvroConverter)")
    print(f" Topics ({len(topics_to_consume)}):")
    for t in topics_to_consume:
        print(f"   - {t}")
    if args.count > 0:
        print(f" Message Limit:   {args.count}")
    print("=" * 70)
    print()

    # Set up Schema Registry client and Avro deserializer
    # The deserializer fetches the schema from Schema Registry on first message
    # for each schema version and caches it for subsequent messages
    schema_registry_client = SchemaRegistryClient({"url": SCHEMA_REGISTRY_URL})
    avro_deserializer = AvroDeserializer(schema_registry_client)

    # Configure the Kafka DeserializingConsumer with Avro deserialization
    consumer_config = {
        # Kafka broker addresses (host-mapped ports from docker-compose)
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        # Consumer group — all consumers with same group.id share partitions
        "group.id": CONSUMER_GROUP_ID,
        # Where to start reading if no committed offset exists for this group
        "auto.offset.reset": offset_reset,
        # Avro deserializer for message values
        # Automatically fetches schema from Schema Registry by the 4-byte schema ID
        # embedded in each message, then deserializes to a Python dict
        "value.deserializer": avro_deserializer,
        # Automatically commit offsets every 5 seconds (default)
        "enable.auto.commit": True,
        "auto.commit.interval.ms": 5000,
        # Session timeout for consumer group heartbeats
        "session.timeout.ms": 30000,
    }

    # Create the DeserializingConsumer instance
    # DeserializingConsumer extends Consumer with support for key/value deserializers
    consumer = DeserializingConsumer(consumer_config)

    # Subscribe to the selected CDC topics
    consumer.subscribe(topics_to_consume)

    # Track message count for --count limit
    message_count = 0

    # Set up graceful shutdown on Ctrl+C
    running = True

    def signal_handler(sig, frame):
        """Handle SIGINT (Ctrl+C) for graceful shutdown."""
        nonlocal running
        running = False
        print("\n\nReceived shutdown signal. Finishing current message...")

    signal.signal(signal.SIGINT, signal_handler)

    print("Listening for CDC events (Ctrl+C to stop)...\n")

    try:
        while running:
            # Poll Kafka for new messages (timeout: 1 second)
            # DeserializingConsumer.poll() automatically deserializes Avro messages
            # using the registered value.deserializer
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                # No message received — loop and try again
                continue

            # Check for Kafka-level errors
            if msg.error():
                error = msg.error()
                if error.code() == KafkaError._PARTITION_EOF:
                    # Reached end of partition — normal, not an error
                    pass
                else:
                    # Actual error — print it and continue
                    print(f"  [ERROR] Kafka error: {error}")
                continue

            # Skip null/tombstone messages (produced for DELETE + log compaction)
            if msg.value() is None:
                continue

            # Process the CDC event (msg.value() is already a Python dict)
            message_count += 1
            process_message(msg)

            # Check if we've reached the message count limit
            if args.count > 0 and message_count >= args.count:
                print(f"\nReached message limit ({args.count}). Stopping.")
                break

    except KafkaException as e:
        # Handle Kafka-specific exceptions
        print(f"\n[FATAL] Kafka exception: {e}")
        sys.exit(1)

    finally:
        # Always close the consumer to commit final offsets and leave the group cleanly
        print(f"\n{'=' * 70}")
        print(f" Consumer stopped. Total messages processed: {message_count}")
        print(f"{'=' * 70}")
        consumer.close()


# Standard Python entry point guard
if __name__ == "__main__":
    main()
