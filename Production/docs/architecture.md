# Architecture

## System overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Docker Network                             │
│                                                                     │
│  ┌────────────┐    CDC     ┌──────────────┐   Avro    ┌──────────┐ │
│  │ SQL Server │──────────▶│              │──────────▶│  Kafka   │ │
│  │    2019    │  (snapshot │    Kafka     │  (binary) │ Broker 1 │ │
│  │  (CDC on)  │ + stream)  │   Connect    │           │  :29092  │ │
│  └────────────┘            │  (Debezium)  │           └──────────┘ │
│                            │   :8083      │                │       │
│  ┌────────────┐    WAL     │              │           ┌──────────┐ │
│  │ PostgreSQL │──────────▶│              │──────────▶│  Kafka   │ │
│  │    15      │  (logical  └──────────────┘  Avro    │ Broker 2 │ │
│  │(wal=logic) │ replication)      │           (binary) │  :29092  │ │
│  └────────────┘                   │           └──────────┘ │       │
│                                   │                  │      │       │
│                            ┌──────▼──────┐           │      │       │
│                            │   Karapace  │◀──────────┘      │       │
│                            │   Schema    │  (schema lookup) │       │
│                            │  Registry   │                  │       │
│                            │   :8081     │                  │       │
│                            └─────────────┘                  │       │
│                                                             │       │
│                            ┌─────────────┐                  │       │
│                            │  Kafka UI   │◀─────────────────┘       │
│                            │   :8084     │  (observability)          │
│                            └─────────────┘                           │
└─────────────────────────────────────────────────────────────────────┘
```

## Component roles

### Kafka (2-broker KRaft cluster)

Two brokers share the message store. Broker 1 also serves as the KRaft
controller (cluster metadata). Broker 2 is a pure broker.

- **KRaft mode:** no ZooKeeper dependency. Metadata is stored in a Kafka
  topic (`__cluster_metadata`) managed by the controller quorum.
- **Replication factor = 2:** every partition has one replica on each broker.
  A single broker restart does not cause data loss or consumer disruption.
- **lz4 compression:** applied broker-side to all topics. ~20-30% storage
  reduction on Avro-encoded CDC records.

### Kafka Connect (Debezium)

Single worker node running both the SQL Server and PostgreSQL Debezium connectors.
Each connector runs as one or more tasks inside the worker JVM.

- **Custom Docker image:** extends `debezium/connect:2.3` with the Confluent
  Avro JARs copied from the official Confluent image at build time. No runtime
  downloads. The image is fully self-contained.
- **Avro converter set at worker level:** all connectors inherit Avro by default.
  No per-connector converter configuration needed.
- **JVM heap = 2 GB:** prevents GC pauses under 4-task, batch=2048 load.
- **offset.flush.interval.ms = 10s:** reduces reprocessing window on crash from
  60s (default) to 10s.

### Karapace Schema Registry

Open-source, Apache 2.0 licensed, wire-compatible replacement for Confluent
Schema Registry. Stores Avro schemas in a Kafka topic (`_schemas`).

Each Debezium message carries a 5-byte header (magic byte `0x00` + 4-byte
schema ID) instead of embedding the full schema. This is what reduces average
message size from ~840 bytes (JSON+schema) to ~212-224 bytes (Avro).

### SQL Server 2019

Source database. CDC is enabled via SQL Server Agent capture jobs that read the
transaction log and write to change tables. Debezium queries the change tables
via JDBC, not the transaction log directly.

**Required setup (one-time):**
```sql
-- Enable CDC on database
EXEC sys.sp_cdc_enable_db;

-- Enable snapshot isolation (prevents table locks during snapshot)
ALTER DATABASE AdventureWorks2019 SET READ_COMMITTED_SNAPSHOT ON;
ALTER DATABASE AdventureWorks2019 SET ALLOW_SNAPSHOT_ISOLATION ON;

-- Enable CDC on the table
EXEC sys.sp_cdc_enable_table
    @source_schema = 'Sales',
    @source_name = 'SalesOrderDetailBig',
    @role_name = NULL;
```

### PostgreSQL 15

Source database. Logical replication via `pgoutput` plugin. Debezium creates a
replication slot and reads the WAL stream decoded by `pgoutput`.

**Required setup (one-time, handled by init.sql):**
```sql
-- wal_level=logical set via postgres -c args in docker-compose
-- Publication for the specific table
CREATE PUBLICATION debezium_pub FOR TABLE public.sales_order_detail_big;
```

## Data flow: snapshot phase

```
1. Connector starts → Connect REST API → Deploy task(s)
2. Task opens JDBC connection (MSSQL) / replication slot (PG)
3. Task reads rows in batches (max.batch.size=2048)
4. Each batch → Avro-encode → schema ID lookup in Schema Registry
5. Kafka producer → ProducerRecord → Kafka broker (partitioned by PK hash)
6. Kafka broker replicates to second broker (RF=2)
7. Broker acks → producer commits → task reads next batch
8. Repeat until all rows are read (2M rows → ~54s MSSQL / ~86s PG)
9. Connector switches to streaming mode (reads CDC log / WAL continuously)
```

## Data flow: streaming CDC phase (post-snapshot)

```
MSSQL:
  SQL Agent capture job → change tables → Debezium polls every 1000ms
  → new CDC rows → Avro encode → Kafka topic

PostgreSQL:
  Write to table → WAL record → pgoutput decodes → replication slot
  → Debezium reads slot every 500ms → Avro encode → Kafka topic
```

## Network layout

| Service | Internal address | Host port |
|---------|-----------------|-----------|
| Kafka broker 1 | `kafka:29092` | `9092` |
| Kafka broker 2 | `kafka-broker-2:29092` | `9093` |
| Kafka Connect | `kafka-connect:8083` | `8083` |
| Schema Registry | `schema-registry:8081` | `8081` |
| SQL Server | `sqlserver:1433` | `1433` |
| PostgreSQL | `postgres:5432` | `5432` |
| Kafka UI | `kafka-ui:8080` | `8084` |

All services communicate via Docker's internal bridge network using service
names as DNS hostnames. Host ports are only for local development access.

## Performance characteristics (from benchmark)

| Source | Best config | Throughput | Message size |
|--------|------------|------------|--------------|
| SQL Server | tasks=1, batch=2048, poll=1000ms | 37,166 msg/s | 212 B (Avro) |
| PostgreSQL | tasks=4, batch=2048, poll=500ms | 23,277 msg/s | 224 B (Avro) |

At-scale estimates (single connector, single table):

| Rows | SQL Server | PostgreSQL |
|------|-----------|-----------|
| 1M   | ~27s      | ~43s      |
| 10M  | ~4.5min   | ~7min     |
| 50M  | ~22min    | ~36min    |
| 100M | ~45min    | ~72min    |

For tables larger than 50M rows, use `snapshot.select.statement.overrides` to
split the snapshot across multiple connector instances with non-overlapping key
ranges running in parallel.
