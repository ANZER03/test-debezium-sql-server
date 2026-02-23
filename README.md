# SQL Server CDC Pipeline with Debezium, Kafka, and Python

A complete Change Data Capture (CDC) data pipeline that streams real-time database changes from SQL Server to Kafka using Debezium.

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│                  │     │                  │     │                  │     │                  │
│   SQL Server     │────>│  Debezium Kafka  │────>│  Apache Kafka    │────>│  Python CDC      │
│   2019           │ CDC │  Connect         │     │  (KRaft, 2       │     │  Consumer        │
│                  │     │                  │     │   brokers)       │     │                  │
│  AdventureWorks  │     │  Captures CDC    │     │  Stores change   │     │  Reads & parses  │
│  2019 (OLTP)     │     │  change events   │     │  events in       │     │  Debezium CDC    │
│                  │     │                  │     │  topics          │     │  envelope events  │
└──────────────────┘     └──────────────────┘     └──────────────────┘     └──────────────────┘
        ^                                                                          
        │                                                                          
┌──────────────────┐                              ┌──────────────────┐     ┌──────────────────┐
│                  │                              │                  │     │                  │
│  Python CDC      │                              │  Schema Registry │     │  Kafka UI        │
│  Producer        │                              │  (Confluent)     │     │  (Provectus)     │
│                  │                              │                  │     │                  │
│  INSERT/UPDATE/  │                              │  Schema mgmt     │     │  Visual monitor  │
│  DELETE rows     │                              │  (available for  │     │  for topics,     │
│  in SQL Server   │                              │   future use)    │     │  messages, etc.  │
└──────────────────┘                              └──────────────────┘     └──────────────────┘
```

### Component Summary

| Component | Technology | Port | Purpose |
|-----------|-----------|------|---------|
| Kafka Broker 1 | Confluent `cp-kafka:7.7.7` (KRaft) | 9092 | Message broker + KRaft controller |
| Kafka Broker 2 | Confluent `cp-kafka:7.7.7` (KRaft) | 9093 | Message broker (voter) |
| Schema Registry | Confluent `cp-schema-registry:7.7.7` | 8081 | Schema management (available for future Avro use) |
| SQL Server | Microsoft `mssql/server:2019-latest` | 1433 | Source database with CDC enabled |
| Kafka Connect | Debezium `connect:2.3` | 8083 | Runs the Debezium SQL Server CDC connector |
| Kafka UI | Provectus `kafka-ui:latest` | 8084 | Web UI for monitoring topics and messages |

### CDC-Enabled Tables (5 tables)

| Schema | Table | Kafka Topic | Description |
|--------|-------|------------|-------------|
| Person | Person | `aw.AdventureWorks2019.Person.Person` | Contact/person records |
| Sales | Customer | `aw.AdventureWorks2019.Sales.Customer` | Customer accounts |
| Sales | SalesOrderHeader | `aw.AdventureWorks2019.Sales.SalesOrderHeader` | Order headers |
| Sales | SalesOrderDetail | `aw.AdventureWorks2019.Sales.SalesOrderDetail` | Order line items |
| Production | Product | `aw.AdventureWorks2019.Production.Product` | Product catalog |

---

## Prerequisites

- **Docker** and **Docker Compose** installed
- **Python 3.8+** with a virtual environment
- **curl** and **jq** (for the connector deploy script)
- ~4GB free disk space (SQL Server image is ~1.5GB, AdventureWorks backup is ~50MB)

---

## Quick Start

### Step 1: Start all infrastructure

```bash
docker-compose up -d
```

Wait for all 6 containers to be healthy:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
```

Expected output — all containers should show "Up" (sqlserver should show "healthy"):
```
NAMES             STATUS
kafka-ui          Up
kafka-connect     Up
schema-registry   Up
kafka-broker-2    Up
kafka             Up
sqlserver         Up (healthy)
```

### Step 2: Restore AdventureWorks and enable CDC

Run the setup script inside the SQL Server container:
```bash
docker exec -it sqlserver bash /setup/setup-adventureworks.sh
```

This script:
1. Downloads the AdventureWorks2019 backup (~50MB)
2. Restores it to the SQL Server instance
3. Enables CDC on the database and 5 target tables

Verify CDC is enabled:
```bash
docker exec sqlserver /opt/mssql-tools18/bin/sqlcmd -C \
  -S localhost -U sa -P 'YourStrong!Passw0rd' \
  -d AdventureWorks2019 \
  -Q "SELECT OBJECT_SCHEMA_NAME(source_object_id) AS [schema], OBJECT_NAME(source_object_id) AS [table] FROM cdc.change_tables"
```

### Step 3: Deploy the Debezium connector

```bash
./connect/deploy-connector.sh
```

Check connector status:
```bash
./connect/deploy-connector.sh status
```

Both the connector and task 0 should show `RUNNING`.

List the CDC topics created:
```bash
./connect/deploy-connector.sh topics
```

Expected topics: 5 CDC topics (`aw.AdventureWorks2019.*`), plus `schema-changes.adventureworks` and `connect-*` internal topics.

### Step 4: Set up Python environment

```bash
# Create virtual environment (if not already created)
python3 -m venv env
source env/bin/activate

# Install dependencies
pip install -r producer/requirements.txt
pip install -r consumer/requirements.txt
```

### Step 5: Run the Producer (generates SQL Server changes)

The producer connects to SQL Server and performs random INSERT, UPDATE, and DELETE operations on the CDC-enabled tables:

```bash
python3 producer/producer.py
```

It runs continuously, performing one operation every 3 seconds. Press `Ctrl+C` to stop.

### Step 6: Run the Consumer (reads CDC events from Kafka)

In a separate terminal:

```bash
source env/bin/activate

# Consume only new live CDC events (default: --from latest)
python3 consumer/consumer.py

# Consume from beginning (includes all 193K+ snapshot records)
python3 consumer/consumer.py --from-beginning

# Consume only specific tables
python3 consumer/consumer.py --topics Product Person

# Consume a limited number of messages
python3 consumer/consumer.py --topics Product --from-beginning --count 20
```

---

## Verification

### Check Kafka UI
Open [http://localhost:8084](http://localhost:8084) to see:
- **Topics**: All CDC topics with message counts
- **Messages**: Browse individual CDC events
- **Connect**: Connector status

### Check Schema Registry
```bash
curl -s http://localhost:8081/subjects | python3 -m json.tool
```

### Read a sample CDC message
```bash
docker exec kafka kafka-console-consumer \
  --bootstrap-server kafka:29092 \
  --topic aw.AdventureWorks2019.Production.Product \
  --from-beginning --max-messages 1 | python3 -m json.tool
```

### Connector management
```bash
./connect/deploy-connector.sh deploy   # Create or update connector
./connect/deploy-connector.sh status   # Check connector + task status
./connect/deploy-connector.sh topics   # List CDC topics
./connect/deploy-connector.sh delete   # Remove connector (keeps topics)
```

---

## Debezium CDC Event Format

Each CDC event uses the Debezium envelope format (JSON with schemas enabled):

```json
{
  "schema": { "..." },
  "payload": {
    "before": null,
    "after": {
      "ProductID": 1001,
      "Name": "Pro Widget 42",
      "ListPrice": "base64-encoded-decimal"
    },
    "source": {
      "connector": "sqlserver",
      "db": "AdventureWorks2019",
      "schema": "Production",
      "table": "Product",
      "commit_lsn": "00000037:00000d40:003a"
    },
    "op": "c",
    "ts_ms": 1740162420000
  }
}
```

### Operation types

| Code | Meaning | `before` | `after` |
|------|---------|----------|---------|
| `r` | Snapshot (initial read) | null | row data |
| `c` | Insert (CREATE) | null | new row data |
| `u` | Update | old row data | new row data |
| `d` | Delete | old row data | null |

---

## Project Structure

```
test-kafka-structred/
├── docker-compose.yml              # 6 Docker services (all commented)
├── sqlserver/
│   ├── setup-adventureworks.sh     # Download + restore AW2019 + enable CDC
│   └── enable-cdc.sql             # T-SQL: enable CDC on DB + 5 tables
├── connect/
│   ├── connector-config.json      # Debezium SQL Server connector config
│   └── deploy-connector.sh        # Script to deploy/status/delete/topics
├── producer/
│   ├── producer.py                # SQL Server DML generator (INSERT/UPDATE/DELETE)
│   └── requirements.txt           # pymssql
├── consumer/
│   ├── consumer.py                # Kafka CDC event consumer with envelope parsing
│   └── requirements.txt           # confluent-kafka
├── schemas/
│   └── user_event.avsc            # Original Avro schema (from prior project, kept for reference)
├── env/                           # Python virtual environment
└── README.md                      # This file
```

---

## Troubleshooting

### SQL Server container exits immediately
- Check Docker has at least 2GB RAM allocated (SQL Server 2019 requirement)
- Verify the password meets SQL Server complexity rules (uppercase, lowercase, digit, special char, 8+ chars)

### Kafka broker fails to start
- With 2 brokers, ensure `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1` and `KAFKA_DEFAULT_REPLICATION_FACTOR: 1` are set on the controller
- Default RF=3 will fail with only 2 brokers

### Connector fails with 500 error
- Ensure AdventureWorks2019 is restored and CDC is enabled before deploying the connector
- Check that the SQL Server container is healthy: `docker ps`
- Verify credentials match between `docker-compose.yml` and `connector-config.json`

### sqlcmd not found or TLS error
- SQL Server 2019 uses `/opt/mssql-tools18/bin/sqlcmd` (not `/opt/mssql-tools/bin/sqlcmd`)
- Always use the `-C` flag to trust the self-signed certificate

### No CDC topics appear
- Wait 30-60 seconds after deploying the connector (snapshot takes time for large tables)
- Check connector status: `./connect/deploy-connector.sh status`
- Check logs: `docker logs kafka-connect --tail 50`

### Consumer shows base64-encoded values for prices
- This is expected. SQL Server `money`/`decimal` columns are encoded as base64 big-endian two's complement by Debezium when using JSON converter
- The consumer's `decode_debezium_decimal()` function handles most cases automatically

### Producer pymssql installation fails
- Ensure `python3-dev` and `freetds-dev` system packages are installed (needed for pymssql compilation on some systems)
- Alternatively: `pip install pymssql` uses pre-built wheels on most platforms

---

## Cleanup

Stop and remove all containers and volumes:
```bash
docker-compose down -v
```

Stop containers but keep data volumes (for faster restart):
```bash
docker-compose down
```

Delete the Debezium connector (but keep the infrastructure running):
```bash
./connect/deploy-connector.sh delete
```

---

## Key Discoveries During Development

1. **Kafka replication factor**: With 2 brokers, `__consumer_offsets` defaults to RF=3 and fails. Must set RF=1.
2. **SQL Server 2019 sqlcmd path**: `/opt/mssql-tools18/bin/sqlcmd` (not the older `/opt/mssql-tools/`) + requires `-C` flag.
3. **SQL Server container has `wget` but NOT `curl`**: Setup scripts must use `wget` for downloads.
4. **Debezium 2.3 lacks Confluent Avro Converter**: The `debezium/connect:2.3` image doesn't include `io.confluent.connect.avro.AvroConverter`. Solution: use JSON converter with schemas enabled.
5. **JSON config comments**: Kafka Connect's REST API rejects unknown top-level fields. `_comment_*` keys work inside the `config` object but NOT at the top level.
6. **CDC change_tables schema**: Use `OBJECT_SCHEMA_NAME(ct.source_object_id)` and `OBJECT_NAME(ct.source_object_id)` to query CDC-enabled tables — the `source_schema`/`source_name` columns don't exist in SQL Server 2019.
7. **Snapshot data**: The initial snapshot captured 193,078 rows across all 5 tables before switching to streaming mode.
