# Implementation Plan: High-Performance CDC Pipeline for Large SQL Databases

## Overview

This plan enhances the existing SQL Server CDC pipeline with two critical features optimized for large-scale data migration:

| Part | Feature | Complexity | Benefit |
|------|---------|-----------|---------|
| **1** | **Incremental Snapshot with Kafka Signals** | Medium | Non-blocking, resumable, concurrent with streaming, row-level filtering |
| **2** | **Avro + Confluent Schema Registry** | High | ~90% message size reduction, strict schema evolution, industry standard |

**Implementation order: Part 1 → Part 2** (Incremental first establishes the data movement strategy, Avro optimizes the format)

---

## Current State (Before Changes)

```
test-kafka-structred/
├── docker-compose.yml                    # 6 services, JSON converter, debezium/connect:2.3 image
├── connect/
│   ├── connector-config.json             # Debezium SQL Server connector (snapshot.mode: initial)
│   └── deploy-connector.sh              # deploy/status/delete/topics commands
├── producer/
│   ├── producer.py                       # pymssql — INSERT/UPDATE/DELETE into SQL Server
│   └── requirements.txt                  # pymssql>=2.2.8
├── consumer/
│   ├── consumer.py                       # confluent-kafka Consumer — JSON parsing of Debezium envelope
│   └── requirements.txt                  # confluent-kafka>=2.3.0
├── sqlserver/
│   ├── setup-adventureworks.sh           # Download + restore AW2019 + enable CDC
│   └── enable-cdc.sql                    # Enable CDC on DB + 5 tables
└── schemas/
    └── user_event.avsc                   # Legacy Avro schema (unused, kept for reference)
```

**Key constraint:** `debezium/connect:2.3` does NOT include the Confluent Avro Converter (`io.confluent.connect.avro.AvroConverter`). It was removed in Debezium 2.0.

---

## Part 1: Incremental Snapshot with Kafka Signals

### Goal

Enable **on-demand, resumable, concurrent** snapshots that can be triggered at any time while the streaming connector is running. Any subset of tables can be re-snapshotted without disrupting live CDC streaming.

### Why Incremental Snapshot for Large SQL Databases?

When migrating data from a large SQL database, the traditional `initial` snapshot mode has critical limitations:

| Problem | Impact on Large DBs |
|---------|-------------------|
| **Long-running transaction** | A 500GB table snapshot can take hours, holding locks and impacting production |
| **Non-resumable** | Network/connector failure at 90% means restarting from 0% |
| **All-or-nothing** | Cannot filter rows (e.g., "only 2024 orders") during snapshot |
| **Blocks streaming** | New CDC changes queue up until snapshot completes |

**Incremental Snapshot solves all of these problems.**

### How Incremental Snapshot Works (vs Initial Snapshot)

```
Initial snapshot (current):

  Connector starts
         │
    ┌────┴────────────────────────────────┐
    │  SNAPSHOT (blocks streaming)        │
    │  SELECT * FROM all tables           │
    │  Crash → restart from scratch       │
    └────┬────────────────────────────────┘
         │
    STREAMING starts from recorded LSN
         │ forever...


Incremental snapshot (this part):

  Connector already STREAMING (reading live CDC changes)
         │
         │  ◄─── Kafka signal arrives: "snapshot Production.Product WHERE Year > 2020"
         │
    ┌────┴────────────────────────────────────────────────┐
    │  INCREMENTAL SNAPSHOT runs CONCURRENTLY             │
    │                                                     │
    │  Splits table by PK into chunks (default 1024):     │
    │                                                     │
    │  Chunk 1: ProductID 1–1024                          │
    │  ┌────────────────────────────────────────┐         │
    │  │  Open "snapshot window"                │         │
    │  │  SELECT rows → buffer as op:"r"        │         │
    │  │  Meanwhile, live CDC continues:        │         │
    │  │    If UPDATE for same PK arrives       │         │
    │  │    → CDC event WINS (discard READ)     │  ← watermark dedup
    │  │  Close window → emit remaining READs   │         │
    │  └────────────────────────────────────────┘         │
    │                                                     │
    │  Chunk 2: ProductID 1025–2048 ...                   │
    │                                                     │
    │  Crash mid-chunk → resumes from last chunk          │
    └────┬────────────────────────────────────────────────┘
         │
    STREAMING continues (it never stopped)
```

### Side-by-Side Comparison

| Aspect | `initial` snapshot | Incremental snapshot |
|--------|-------------------|---------------------|
| **Trigger** | Automatic, on first startup | Manual, via Kafka signal or SQL signal |
| **Timing** | Before streaming starts | While streaming is running |
| **Concurrency** | Blocks streaming | Concurrent with streaming |
| **Crash recovery** | Restarts from scratch | Resumes from last completed chunk |
| **Re-triggerable** | No (must delete connector offset) | Yes, any time, any table subset |
| **Scope** | All configured tables | You specify which tables |
| **Partial table** | No | Yes (via `additional-condition` filter) |
| **Requires signal table** | No | Yes (for watermark dedup mechanism) |
| **Use case** | First-time backfill | Re-sync, backfill new table, **filtered migration** |

### The Race Condition Problem (Why Watermarking Exists)

```
Initial snapshot:
  Time 0: Record LSN 500
  Time 1: SELECT rows (streaming blocked — nothing can change)
  Time 2: Streaming starts from LSN 500
  → No overlap. Clean.

Incremental snapshot:
  Time 0: Streaming is at LSN 700
  Time 1: Start snapshotting Product chunk (ProductID 1–1024)
  Time 2: Live UPDATE on ProductID 42 arrives (LSN 701) → Debezium emits op:"u"
  Time 3: Snapshot reads ProductID 42 → would emit op:"r" with OLD data
  → CONFLICT: which event wins for ProductID 42?
  → Watermark says: CDC event (op:"u") wins. Buffered op:"r" is discarded.
```

### Why Both Signal Table AND Kafka Signal Are Required

Even when triggering via Kafka, the SQL Server signaling table is required because Debezium writes **watermark markers** into it. These markers flow through the CDC transaction log, allowing the connector to know when a snapshot chunk's "window" opens and closes. Without the signaling table, watermark dedup cannot work.

```
Kafka signal topic (aw-signal)          SQL Server signaling table
        │                                     (dbo.debezium_signal)
        │  trigger signal                            │
        ▼                                            │  watermark open/close markers
  Debezium reads signal ──────────────────────> Debezium writes watermark rows
        │                                            │
        │                                            ▼
        │                                   CDC captures watermark rows
        │                                            │
        ▼                                            ▼
  Starts chunked snapshot ◄──── uses watermarks to dedup snapshot reads vs CDC events
```

### Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `sqlserver/create-signal-table.sql` | **CREATE** | SQL script to create and CDC-enable the signaling table |
| `connect/connector-config.json` | **MODIFY** | Add signal channel config properties |
| `connect/send-signal.sh` | **CREATE** | Helper script to send Kafka signals (supports filtering) |
| `connect/deploy-connector.sh` | **MODIFY** | Add `signal` sub-command |

### New File: `sqlserver/create-signal-table.sql`

```sql
-- Create the Debezium signaling table in AdventureWorks2019
-- This table is used by the incremental snapshot watermark mechanism
USE AdventureWorks2019;
GO

CREATE TABLE dbo.debezium_signal (
    id   VARCHAR(42)   PRIMARY KEY,   -- unique signal ID
    type VARCHAR(32)   NOT NULL,      -- signal type: 'execute-snapshot', 'stop-snapshot'
    data VARCHAR(2048) NULL           -- signal payload (JSON)
);
GO

-- Enable CDC on the signaling table (REQUIRED for watermarking)
EXEC sys.sp_cdc_enable_table
    @source_schema = N'dbo',
    @source_name   = N'debezium_signal',
    @role_name     = NULL,
    @supports_net_changes = 1;
GO
```

### Modified File: `connect/connector-config.json`

Add the following properties to the existing `config` object:

```json
{
    "signal.enabled.channels": "source,kafka",
    "signal.data.collection": "AdventureWorks2019.dbo.debezium_signal",
    "signal.kafka.topic": "aw-signal",
    "signal.kafka.bootstrap.servers": "kafka:29092",
    "signal.kafka.groupId": "debezium-signal-consumer",
    "incremental.snapshot.chunk.size": "1024"
}
```

| Property | Value | Purpose |
|----------|-------|---------|
| `signal.enabled.channels` | `source,kafka` | Enable both SQL signaling table and Kafka signal channel |
| `signal.data.collection` | `AdventureWorks2019.dbo.debezium_signal` | Fully-qualified signaling table (watermarks) |
| `signal.kafka.topic` | `aw-signal` | Kafka topic the connector polls for signals |
| `signal.kafka.bootstrap.servers` | `kafka:29092` | Kafka brokers for the signal consumer |
| `signal.kafka.groupId` | `debezium-signal-consumer` | Consumer group for signal polling |
| `incremental.snapshot.chunk.size` | `1024` | Rows per chunk (smaller = more frequent dedup, larger = faster) |

### New File: `connect/send-signal.sh`

```bash
#!/bin/bash

# Usage examples:
# ./connect/send-signal.sh snapshot Production.Product
# ./connect/send-signal.sh snapshot "Production.Product,Person.Person"
# ./connect/send-signal.sh snapshot Production.Product "ProductID > 500"
# ./connect/send-signal.sh stop-snapshot Production.Product

SIGNAL_TYPE=$1
TABLES=$2
CONDITION=$3

if [ -z "$SIGNAL_TYPE" ] || [ -z "$TABLES" ]; then
    echo "Usage: $0 <snapshot|stop-snapshot> <table[,table2,...]> [additional-condition]"
    exit 1
fi

# Build data-collections array
IFS=',' read -ra TABLE_ARRAY <<< "$TABLES"
DATA_COLLECTIONS="["
for table in "${TABLE_ARRAY[@]}"; do
    DATA_COLLECTIONS+="\"AdventureWorks2019.$table\","
done
DATA_COLLECTIONS="${DATA_COLLECTIONS%,}]"

# Build signal payload
if [ "$SIGNAL_TYPE" = "snapshot" ]; then
    if [ -n "$CONDITION" ]; then
        PAYLOAD="{\"type\":\"execute-snapshot\",\"data\":{\"data-collections\":$DATA_COLLECTIONS,\"type\":\"INCREMENTAL\",\"additional-condition\":\"$CONDITION\"}}"
    else
        PAYLOAD="{\"type\":\"execute-snapshot\",\"data\":{\"data-collections\":$DATA_COLLECTIONS,\"type\":\"INCREMENTAL\"}}"
    fi
elif [ "$SIGNAL_TYPE" = "stop-snapshot" ]; then
    PAYLOAD="{\"type\":\"stop-snapshot\",\"data\":{\"data-collections\":$DATA_COLLECTIONS,\"type\":\"INCREMENTAL\"}}"
else
    echo "Invalid signal type: $SIGNAL_TYPE"
    exit 1
fi

# Send to Kafka
echo "$PAYLOAD" | docker exec -i kafka kafka-console-producer \
    --bootstrap-server kafka:29092 \
    --topic aw-signal \
    --property "parse.key=true" \
    --property "key.separator=:"
```

**Key feature:** The `additional-condition` parameter allows row-level filtering:

```bash
# Snapshot only products with high IDs
./connect/send-signal.sh snapshot Production.Product "ProductID > 500"

# Snapshot only orders from 2024
./connect/send-signal.sh snapshot Sales.SalesOrderHeader "YEAR(OrderDate) = 2024"
```

### Kafka Signal Topic Requirements

The `aw-signal` topic **must have exactly 1 partition** for ordering guarantees. Create it before deploying the connector:

```bash
docker exec kafka kafka-topics \
    --bootstrap-server kafka:29092 \
    --create \
    --topic aw-signal \
    --partitions 1 \
    --replication-factor 1
```

### Test Plan

1. Create the signal table in SQL Server:
   ```bash
   docker exec -i sqlserver /opt/mssql-tools/bin/sqlcmd -S localhost -U sa -P 'YourStrong!Passw0rd' -i /scripts/create-signal-table.sql
   ```
2. Create the `aw-signal` Kafka topic (1 partition)
3. Redeploy the connector with signal config: `./connect/deploy-connector.sh deploy`
4. Verify connector is RUNNING and streaming
5. Trigger incremental snapshot: `./connect/send-signal.sh snapshot Production.Product`
6. Verify snapshot events appear (op: "r") interleaved with live CDC events
7. Run producer simultaneously to verify concurrent streaming works
8. Test filtering: `./connect/send-signal.sh snapshot Production.Product "ProductID > 500"`
9. Test stop: `./connect/send-signal.sh stop-snapshot Production.Product`

---

## Part 2: Avro + Confluent Schema Registry

### Goal

Switch the entire pipeline from JSON (with embedded schemas in every message) to Avro (binary format with 5-byte schema ID referencing Confluent Schema Registry).

### Why Avro Over JSON

```
JSON message (current):
┌────────────────────────────────────────────────────┐
│ { "schema": { 3KB of field definitions... },       │  ← repeated in EVERY message
│   "payload": { "before": ..., "after": ... } }     │
│                                                    │
│ Total: ~4-6KB per message                          │
└────────────────────────────────────────────────────┘

Avro message (after this change):
┌────────────────────────────────────────────────────┐
│ [0x00][schema_id: 4 bytes][avro binary payload]    │
│                                                    │
│ Total: ~200-500 bytes per message                  │
│                                                    │
│ Schema stored ONCE in Schema Registry              │
│ Consumer fetches schema by ID on first read        │
└────────────────────────────────────────────────────┘
```

**Size comparison for 193K snapshot rows:**

| Format | Avg message size | Total snapshot data |
|--------|-----------------|---------------------|
| JSON + embedded schema | ~4-6KB | ~580MB–1.1GB |
| Avro + Schema Registry | ~200-500B | ~40-95MB |

**For large SQL databases:** If you're moving 10M+ rows, Avro can reduce Kafka storage from **40-60GB to 2-5GB**.

### How a Sink Connector Reads Avro Data

This is relevant for any downstream consumer (Python, sink connector, etc.):

```
SQL Server
    │  (transaction log)
    ▼
Debezium Source Connector
    │  Serializes with AvroConverter
    │  Registers schema in Schema Registry (once per schema change)
    │  Publishes: [0x00][schema_id (4 bytes)][avro binary]
    ▼
Kafka Topic (compact binary messages)
    │
    ├──> Sink Connector (e.g. JDBC Sink, S3 Sink, Elasticsearch)
    │    │  value.converter = AvroConverter
    │    │  Fetches schema from Schema Registry by ID
    │    │  Deserializes Avro → Debezium envelope struct
    │    │  Optionally: ExtractNewRecordState SMT flattens envelope
    │    ▼
    │  Target system (Postgres, S3, etc.)
    │
    └──> Python Consumer
         │  confluent_kafka AvroDeserializer
         │  Fetches schema from Schema Registry by ID
         │  Deserializes Avro → Python dict (same Debezium envelope)
         ▼
       Application logic
```

**Important for sink connectors:** Most sink connectors (like JDBC Sink) expect a flat record, not the nested Debezium envelope (`before`/`after`/`op`/`source`). Use the `ExtractNewRecordState` SMT to flatten:

```json
{
    "transforms": "unwrap",
    "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
    "transforms.unwrap.drop.tombstones": "true",
    "transforms.unwrap.delete.handling.mode": "rewrite"
}
```

### Why a Custom Docker Image Is Required

`debezium/connect:2.3` removed the Confluent Avro Converter in Debezium 2.0. The image includes Apicurio converters, but those require Apicurio Registry (not Confluent).

Since we already have `confluentinc/cp-schema-registry:7.7.7` running, we need to add the Confluent JARs to the Debezium Connect image.

**JARs required (from Confluent Maven repo):**

| JAR | Purpose |
|-----|---------|
| `kafka-connect-avro-converter` | The main converter class (`io.confluent.connect.avro.AvroConverter`) |
| `kafka-connect-avro-data` | Avro data conversion utilities |
| `kafka-avro-serializer` | Avro serialization/deserialization |
| `kafka-schema-serializer` | Schema-aware serialization |
| `kafka-schema-registry-client` | HTTP client for Schema Registry |
| `common-config` | Confluent shared configuration utilities |
| `common-utils` | Confluent shared utilities |

### Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `connect/Dockerfile` | **CREATE** | Custom image extending `debezium/connect:2.3` with Confluent Avro JARs |
| `docker-compose.yml` | **MODIFY** | Change `kafka-connect` from `image:` to `build: ./connect`, add Schema Registry env vars |
| `connect/connector-config.json` | **MODIFY** | Switch `key.converter` and `value.converter` to `io.confluent.connect.avro.AvroConverter` |
| `consumer/consumer.py` | **MODIFY** | Replace `json.loads()` with `confluent_kafka` `AvroDeserializer` |
| `consumer/requirements.txt` | **MODIFY** | Change to `confluent-kafka[avro]>=2.3.0` (adds Avro extra) |

**Note:** `producer/producer.py` does NOT change — it writes to SQL Server, not Kafka.

### New File: `connect/Dockerfile`

```dockerfile
FROM debezium/connect:2.3

# Confluent platform version — must be compatible with cp-schema-registry:7.7.7
ENV CONFLUENT_VERSION=7.7.7

# Download Confluent Avro converter JARs into a Kafka Connect plugin directory
# The Debezium image auto-discovers plugins in /kafka/connect/ subdirectories
RUN mkdir -p /kafka/connect/confluent-avro && \
    cd /kafka/connect/confluent-avro && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-connect-avro-converter/${CONFLUENT_VERSION}/kafka-connect-avro-converter-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-connect-avro-data/${CONFLUENT_VERSION}/kafka-connect-avro-data-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-avro-serializer/${CONFLUENT_VERSION}/kafka-avro-serializer-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-schema-serializer/${CONFLUENT_VERSION}/kafka-schema-serializer-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/kafka-schema-registry-client/${CONFLUENT_VERSION}/kafka-schema-registry-client-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/common-config/${CONFLUENT_VERSION}/common-config-${CONFLUENT_VERSION}.jar -O && \
    curl -fSL https://packages.confluent.io/maven/io/confluent/common-utils/${CONFLUENT_VERSION}/common-utils-${CONFLUENT_VERSION}.jar -O
```

### Modified: `docker-compose.yml` (kafka-connect service)

Before:
```yaml
kafka-connect:
    image: debezium/connect:2.3
    environment:
        KEY_CONVERTER: org.apache.kafka.connect.json.JsonConverter
        VALUE_CONVERTER: org.apache.kafka.connect.json.JsonConverter
        ENABLE_DEBEZIUM_KC_REST_EXTENSION: "true"
        ENABLE_DEBEZIUM_SCRIPTING: "true"
```

After:
```yaml
kafka-connect:
    build: ./connect
    environment:
        KEY_CONVERTER: io.confluent.connect.avro.AvroConverter
        VALUE_CONVERTER: io.confluent.connect.avro.AvroConverter
        CONNECT_KEY_CONVERTER_SCHEMA_REGISTRY_URL: http://schema-registry:8081
        CONNECT_VALUE_CONVERTER_SCHEMA_REGISTRY_URL: http://schema-registry:8081
        ENABLE_DEBEZIUM_KC_REST_EXTENSION: "true"
        ENABLE_DEBEZIUM_SCRIPTING: "true"
    depends_on:
        - kafka
        - kafka-broker-2
        - schema-registry
```

### Modified: `connect/connector-config.json`

Add converter overrides at the connector level:

```json
{
    "key.converter": "io.confluent.connect.avro.AvroConverter",
    "key.converter.schema.registry.url": "http://schema-registry:8081",
    "value.converter": "io.confluent.connect.avro.AvroConverter",
    "value.converter.schema.registry.url": "http://schema-registry:8081"
}
```

### Modified: `consumer/consumer.py`

Replace JSON parsing with Avro deserialization:

```python
# Before (JSON):
from confluent_kafka import Consumer
value = json.loads(msg.value().decode("utf-8"))

# After (Avro):
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka import DeserializingConsumer

schema_registry_client = SchemaRegistryClient({"url": "http://localhost:8081"})
avro_deserializer = AvroDeserializer(schema_registry_client)

consumer = DeserializingConsumer({
    "bootstrap.servers": "localhost:9092,localhost:9093",
    "group.id": "cdc-consumer-group",
    "auto.offset.reset": "latest",
    "value.deserializer": avro_deserializer,
})

# msg.value() now returns a Python dict (already deserialized)
# The dict structure is the same Debezium envelope: {before, after, op, source, ts_ms}
event = msg.value()
```

The `process_message()` function stays mostly the same — it already expects a dict with `before`, `after`, `op`, `source` fields. The change is only in how the message is deserialized (Avro instead of JSON).

### Modified: `consumer/requirements.txt`

```
confluent-kafka[avro]>=2.3.0
```

The `[avro]` extra installs `fastavro` and the Avro serializer/deserializer classes.

### Test Plan

1. Build the custom image: `docker-compose build kafka-connect`
2. Recreate the connect container: `docker-compose up -d kafka-connect`
3. Delete and redeploy the connector: `./connect/deploy-connector.sh delete && ./connect/deploy-connector.sh deploy`
4. Verify schemas registered in Schema Registry:
   ```bash
   curl -s http://localhost:8081/subjects | python3 -m json.tool
   # Should list: aw.AdventureWorks2019.Production.Product-value, etc.
   ```
5. Run producer to generate CDC events: `python3 producer/producer.py`
6. Run consumer to verify Avro deserialization: `python3 consumer/consumer.py --topics Product`
7. Verify message size reduction in Kafka UI (http://localhost:8084)

### Impact on Existing Data

**Important:** After switching to Avro, existing JSON messages in Kafka topics will NOT be readable by the Avro deserializer. Two options:

- **Option A:** Delete old topics and let the connector re-snapshot (clean start)
- **Option B:** Create new topics with a different `topic.prefix` (e.g., `aw2.`)

Recommended: **Option A** (delete connector, delete topics, redeploy).

---

## Key Decisions and Tradeoffs

### Confluent Avro vs Apicurio Avro

| Aspect | Confluent Avro (chosen) | Apicurio Avro |
|--------|------------------------|---------------|
| Schema Registry | `cp-schema-registry:7.7.7` (already running) | Requires Apicurio Registry (separate service) |
| Debezium built-in | No (custom Dockerfile required) | Yes (`ENABLE_APICURIO_CONVERTERS=true`) |
| Python client support | `confluent-kafka[avro]` (mature, well-documented) | Requires custom HTTP client or Apicurio SDK |
| Community adoption | Industry standard | Growing, Red Hat backed |
| Setup complexity | Medium (Dockerfile + JARs) | Low (env var only) |

**Why Confluent:** We already have the Confluent Schema Registry running, and the Python `confluent-kafka[avro]` library provides first-class Avro deserialization. Switching to Apicurio would mean replacing the Schema Registry container and losing the existing `confluent-kafka` Avro library integration.

### JSON vs Avro Message Size

| Format | Schema location | Avg message size | 193K snapshot total |
|--------|----------------|-----------------|---------------------|
| JSON + embedded schema | In every message | ~4-6KB | ~580MB–1.1GB |
| JSON without schema | Not available to consumer | ~500B-1KB | ~95-190MB |
| Avro + Schema Registry | Once in registry | ~200-500B | ~40-95MB |

### Snapshot Modes Summary

```
initial          = snapshot all tables → stream CDC changes forever
schema_only      = snapshot schema only → stream from current LSN (no historical data)
incremental      = on-demand snapshot while streaming (triggered via signal, supports filtering)
```

---

## Implementation Checklist

### Part 1: Incremental Snapshot
- [ ] Create `sqlserver/create-signal-table.sql`
- [ ] Execute signal table script in SQL Server container
- [ ] Create `aw-signal` Kafka topic (1 partition)
- [ ] Add signal config to `connect/connector-config.json`
- [ ] Create `connect/send-signal.sh` (with filtering support)
- [ ] Make script executable: `chmod +x connect/send-signal.sh`
- [ ] Test: redeploy connector, trigger snapshot via Kafka signal
- [ ] Test: verify concurrent streaming + snapshot
- [ ] Test: trigger filtered snapshot with `additional-condition`
- [ ] Test: stop snapshot signal

### Part 2: Avro + Schema Registry
- [ ] Create `connect/Dockerfile`
- [ ] Modify `docker-compose.yml` (build, env vars, depends_on)
- [ ] Modify `connect/connector-config.json` (Avro converters)
- [ ] Build custom image: `docker-compose build kafka-connect`
- [ ] Delete old connector and topics (clean start for Avro)
- [ ] Redeploy connector with Avro config
- [ ] Test: verify schemas in Schema Registry
- [ ] Modify `consumer/consumer.py` (AvroDeserializer)
- [ ] Modify `consumer/requirements.txt` (add [avro] extra)
- [ ] Install updated dependencies: `pip install -r consumer/requirements.txt`
- [ ] Test: run producer + consumer end-to-end with Avro
- [ ] Test: verify message size reduction in Kafka UI
