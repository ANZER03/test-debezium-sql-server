# PostgreSQL Connector — Decision Log

## What this connector does

Debezium `PostgresConnector` performs an initial snapshot of
`benchdb.public.sales_order_detail_big` (2,000,000 rows), streams each row as an
Avro-encoded Kafka message to topic `prod_pg.public.sales_order_detail_big`, then
transitions to streaming CDC changes via a PostgreSQL logical replication slot.

---

## Parameter decisions

### `tasks.max = 4`

**Benchmark result:** C3 MultiThread (tasks=4, batch=2048) completed in 85.9s at
23,277 msg/s — the fastest PostgreSQL configuration, 15% faster than C1
(tasks=1, 98.6s, 20,294 msg/s).

**Why this differs from SQL Server (which uses tasks=1):**
PostgreSQL WAL decoding via `pgoutput` can dispatch work across multiple WAL
sender processes. With tasks=4, the Connect worker creates 4 task threads that
pipeline record deserialization and Kafka producer calls in parallel. The
PostgreSQL connector's snapshot uses an exported snapshot (a consistent point-in-
time view) that all tasks share safely — there is no contention risk unlike
SQL Server where multiple tasks compete for the same CDC table scan.

**Important:** `tasks.max > 1` on the PostgreSQL connector does NOT create multiple
replication slots. All tasks share one slot. The parallelism is in Connect's
internal pipeline, not in the WAL read.

---

### `max.batch.size = 2048`

Same reasoning as SQL Server: the WAL decoder (PostgreSQL server side) is the
throughput ceiling (~23k msg/s). Larger batches take longer to fill, leaving Kafka
idle, and increase JVM GC pressure. batch=2048 fills in ~88ms at 23k msg/s.

batch=8192 (C4): 112.5s, 17,782 msg/s — **31% slower**.
batch=16384 (C5): 125.1s, 15,983 msg/s — **45% slower**.

---

### `max.queue.size = 8192`

Must be strictly > `max.batch.size`. 4× ratio (8192 = 4 × 2048) provides enough
buffer depth that the WAL reader is never stalled waiting for the producer to
drain the queue, without over-allocating heap.

---

### `poll.interval.ms = 500`

Unlike SQL Server (where 1000ms is optimal), PostgreSQL's WAL sender delivers
records continuously without a poll gap during snapshot. 500ms is used as a
conservative value that keeps the pipeline responsive during the streaming CDC
phase without busy-waiting.

---

### `plugin.name = pgoutput`

`pgoutput` is the built-in PostgreSQL logical replication output plugin (available
since PostgreSQL 10). It requires no additional installation on the server.

The alternative, `decoderbufs`, requires a C extension compiled and installed on
the PostgreSQL server — an operational burden. `pgoutput` is the recommended
plugin for all modern PostgreSQL versions.

---

### `slot.name = debezium_prod`

**Critical operational concern:** each connector deployment must use a unique slot
name. If you redeploy the connector with a different configuration but the same
slot name, it resumes from the existing slot's LSN (potentially replaying events
or missing events). If you deploy two connectors with the same slot name, one will
fail to start.

**After migration is complete, always drop the slot:**
```sql
SELECT pg_drop_replication_slot('debezium_prod');
```
An unconsumed replication slot causes PostgreSQL to retain all WAL segments since
the slot's confirmed_flush_lsn. On a busy database this fills the disk.

---

### `publication.name = debezium_pub`

The publication must exist before the connector is deployed. Created in
`postgres/init.sql`:
```sql
CREATE PUBLICATION debezium_pub FOR TABLE sales_order_detail_big;
```

Using a table-specific publication (not `FOR ALL TABLES`) limits the WAL decoded
by the slot to only the tables being migrated, reducing WAL decoding overhead.

---

### `heartbeat.interval.ms = 10000`

**PostgreSQL-specific requirement.** If the source table has no write activity
(e.g., a read-only replica or a quiet table), the replication slot's
`confirmed_flush_lsn` is never advanced. PostgreSQL must retain all WAL since that
LSN, causing unbounded disk growth.

The heartbeat causes Debezium to periodically write a small event to a heartbeat
topic, which advances the slot's LSN even when the monitored table is idle.
10 seconds is a safe interval for most workloads.

---

### `snapshot.mode = initial`

Same as SQL Server — full snapshot then streaming. See SQL Server decisions.md
for the full mode comparison table.

---

### Message size: 224 bytes vs SQL Server 212 bytes

PostgreSQL Debezium messages include a `before`/`after` envelope in the Avro
schema. The `before` field contains the row state before the change (populated for
UPDATE and DELETE events). This adds ~12 bytes per message compared to the SQL
Server connector, which omits `before` by default.

You cannot eliminate this overhead without a custom SMT (Single Message Transform).
For snapshot (INSERT-only events), `before` is always null — so the overhead is
wire-present but null-valued.

---

### `tombstones.on.delete = false`

Same reasoning as SQL Server. See SQL Server decisions.md.

---

### Avro converter

Same reasoning as SQL Server. See SQL Server decisions.md.
