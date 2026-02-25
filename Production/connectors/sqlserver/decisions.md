# SQL Server Connector — Decision Log

## What this connector does

Debezium `SqlServerConnector` performs an initial snapshot of
`AdventureWorks2019.Sales.SalesOrderDetailBig` (2,000,000 rows), streams each row
as an Avro-encoded Kafka message to topic
`prod_mssql.AdventureWorks2019.Sales.SalesOrderDetailBig`, then transitions to
streaming CDC changes (inserts, updates, deletes) from the SQL Server transaction
log in real time.

---

## Parameter decisions

### `tasks.max = 1`

**Benchmark result:** C1 Baseline (tasks=1) completed in 53.8s at 37,166 msg/s.
C3 MultiThread (tasks=4) took 71.4s at 28,022 msg/s — **24% slower**.

**Why:** The SQL Server Debezium connector snapshot is inherently single-threaded
per table. The CDC capture job runs as a single SQL Agent job that reads the
transaction log sequentially. Setting `tasks.max > 1` on a single table causes
multiple tasks to independently attempt the snapshot, creating lock contention and
competing I/O on SQL Server. The overhead outweighs any parallelism benefit.

**Rule:** For multiple tables, use one connector per table (each with tasks=1)
running in parallel — not one connector with tasks > 1.

---

### `max.batch.size = 2048`

**Benchmark result:** batch=2048 (C1) was 26% faster than batch=8192 (C2) and
27% faster than batch=16384 (C5).

**Why:** The connector accumulates up to `max.batch.size` records in memory before
flushing to Kafka. Larger batches:
1. Require longer fill time at the source's natural throughput rate (~37k/s), so
   Kafka sits idle waiting for the batch to fill.
2. Allocate larger Java heap objects, increasing GC pause frequency.
3. Do not change how fast SQL Server delivers rows — the source is the bottleneck.

batch=2048 fills in ~55ms at 37k msg/s, keeping Kafka continuously fed.
batch=16384 takes ~440ms to fill — Kafka receives nothing for 440ms, then gets
a burst. Net throughput is lower.

---

### `max.queue.size = 8192`

Must be strictly greater than `max.batch.size` (Kafka Connect validation rule).
8192 = 4× batch size, which is the recommended ratio. The queue is an in-memory
ring buffer between the source reader and the Kafka producer. A larger queue
(32768+) was tested and was slower due to GC pressure with no throughput benefit.

---

### `poll.interval.ms = 1000`

Debezium polls for new CDC records every `poll.interval.ms` milliseconds when the
queue is not full. During snapshot mode, the queue is always full so the poll
interval has no effect on snapshot throughput. It matters only during the
streaming CDC phase (post-snapshot), where 1000ms is aggressive enough to capture
changes within 1 second of commit.

Shorter intervals (100–200ms) were tested and showed no improvement during snapshot
and only increased CPU load in streaming mode.

---

### `snapshot.isolation.level = snapshot`

**Critical for production.** Without this setting, Debezium uses `READ_COMMITTED`
isolation, which acquires shared row locks on every row it reads during snapshot.
On a 2M-row table this means the entire table is locked for the duration of the
snapshot (~54 seconds), blocking all writes.

`snapshot` isolation uses SQL Server's row versioning mechanism (stored in TempDB)
instead of locks. No writes are blocked. Requires:
```sql
ALTER DATABASE AdventureWorks2019 SET READ_COMMITTED_SNAPSHOT ON;
ALTER DATABASE AdventureWorks2019 SET ALLOW_SNAPSHOT_ISOLATION ON;
```

---

### `snapshot.mode = initial`

Reads the full table contents once (snapshot), then switches to streaming CDC.
This is the correct mode for migration: you get all historical data first, then
continuous changes. Alternative modes:

| Mode | Use case |
|------|----------|
| `initial` | **Migration** — full snapshot + streaming (use this) |
| `initial_only` | Snapshot only, no streaming (one-time bulk copy) |
| `schema_only` | No data snapshot, only stream future changes |
| `never` | Assume snapshot already done, stream from current LSN |

---

### `topic.prefix = prod_mssql`

**Must be a valid Avro namespace** — only letters, digits, and underscores. No
hyphens (e.g., `prod-mssql` is invalid and causes schema registration failures in
the Schema Registry). The topic name becomes:
`prod_mssql.AdventureWorks2019.Sales.SalesOrderDetailBig`

---

### `tombstones.on.delete = false`

When a row is deleted, Debezium emits a DELETE event followed by a tombstone
(null-value message with the same key) to signal Kafka log compaction that the
key can be discarded. For migration use cases (snapshot-only or read-once CDC),
tombstones add unnecessary messages. Set to `true` only if downstream consumers
require compaction semantics.

---

### `schema.history.internal.kafka.bootstrap.servers` — both brokers listed

The schema history topic stores the DDL history of the source database. If only
one broker is listed and that broker restarts during migration, schema history
writes fail and the connector enters FAILED state. Listing both brokers provides
automatic failover.

---

### Avro converter (key + value)

Avro + Schema Registry produces 212-byte average messages vs ~840 bytes for JSON
with embedded schema — a 4× size reduction. Over 2,000,000 rows this saves
~1.25 GB of Kafka storage and proportionally reduces broker I/O.

The schema is registered once in Schema Registry on first connector start. All
subsequent messages carry only a 5-byte header (magic byte + 4-byte schema ID)
plus the binary-encoded field values.

---

### Password externalization (production note)

The connector JSON references a file-based secret:
```
"database.password": "${file:/kafka/external-secrets/mssql.properties:database.password}"
```
Mount `mssql.properties` as a Docker secret or Kubernetes secret at that path.
For local development, replace with the literal password — but never commit that
file to version control.
