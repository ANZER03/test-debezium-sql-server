# Topic Configuration — Decision Log

## Why pre-create topics

When a Debezium connector starts and its target topic does not exist, Kafka
auto-creates it using cluster-level defaults. Those defaults are wrong for
production:

| Setting | Cluster default | Production need | Risk if left at default |
|---------|----------------|-----------------|------------------------|
| `partitions` | 1 | 4 | Consumer parallelism capped at 1 thread |
| `replication.factor` | 1 | 2 | One broker restart = permanent data loss |
| `retention.ms` | 604800000 (7d) | 604800000 (7d) | OK but fragile if cluster default differs |
| `cleanup.policy` | delete | compact (internal topics) | Unbounded growth on connect-offsets |

Pre-creating topics with explicit settings gives deterministic behavior regardless
of the cluster's default configuration.

---

## Data topic settings

### `partitions = 4`

**Why 4, not 1:**

With 1 partition, the Kafka consumer protocol allows at most 1 consumer instance
in a consumer group to read from that topic simultaneously. If your downstream
sink (target database writer, ETL process, etc.) can process data faster than a
single thread, additional consumer instances sit idle.

With 4 partitions, up to 4 consumer instances read in parallel:

```
Producer (Debezium):   37,000 msg/s  →  split across 4 partitions
Consumer thread 1:     reads partition 0
Consumer thread 2:     reads partition 1
Consumer thread 3:     reads partition 2
Consumer thread 4:     reads partition 3
Combined sink speed:   4× single-thread speed
```

Debezium routes messages to partitions by hashing the row's primary key. All
events for a given row (INSERT, UPDATE, DELETE) land on the same partition,
preserving per-row ordering. Global ordering across rows is not guaranteed — this
is acceptable for migration workloads.

**Why not more than 4:**

- Each partition is a directory on the broker's disk. 4 partitions × 2 brokers =
  8 partition replicas. Beyond 8–12 partitions per topic, the broker's partition
  leadership overhead becomes measurable.
- Our 2-broker cluster handles 4 partitions with minimal overhead.
- If you need more consumer parallelism (> 4 sink threads), increase to 8 partitions —
  but only if you have ≥ 3 brokers (RF=3, 8 × 3 = 24 replicas).

### `replication.factor = 2`

Every message written by Debezium is replicated to both brokers before the
producer receives an `ack`. If broker 1 fails mid-migration:

- RF=1: all data on broker 1's partitions is lost. The connector must restart
  from snapshot — re-reading the entire 2M-row table.
- RF=2: broker 2 has a complete copy. The migration resumes from the last committed
  offset within seconds (no re-snapshot needed).

RF=2 is the minimum for production with 2 brokers. Add a third broker and set
RF=3 for higher durability.

### `retention.ms = 604800000` (7 days)

Migration data should persist long enough for:
1. Downstream consumers to finish processing (hours to days depending on sink speed)
2. A replay window in case of consumer failure during migration
3. Post-migration verification queries

7 days is safe for most migration scenarios. After migration and verification are
confirmed complete, reduce retention or delete the topic.

### `compression.type = lz4`

Applied at the topic level. lz4 compresses Avro-encoded CDC records by ~20–30%
with negligible CPU overhead. Reduces:
- Broker disk usage per topic
- Network bandwidth during inter-broker replication
- Kafka UI / consumer network reads

---

## Schema history topic settings

### `partitions = 1` (always)

The schema history topic stores the sequence of DDL statements (CREATE TABLE,
ALTER TABLE, etc.) that Debezium replays on connector restart to reconstruct the
schema at any given LSN. This sequence must be strictly ordered.

Kafka only guarantees ordering within a single partition. Multiple partitions would
interleave DDL statements from different partitions, corrupting the history replay.
Schema history topics must always have exactly 1 partition.

### `cleanup.policy = compact`

Compaction retains only the latest message per key, allowing old DDL history
entries to be garbage collected without hitting the retention window. Without
compaction, the schema history topic grows unbounded on long-running CDC deployments.

---

## Connect internal topics

### `connect-offsets` (compact, 1 partition)

Stores the CDC offset (SQL Server LSN / PostgreSQL LSN) for every source partition.
This is the most critical internal topic: if it is lost, all connectors restart
from snapshot. Must be:
- RF=2 (survives one broker failure without losing offset)
- compact (latest offset per source partition is what matters)
- 1 partition (ordered by Kafka Connect internally)

### `connect-configs` (compact, 1 partition)

Stores connector configurations. RF=2 and compact for same reasons.

### `connect-status` (compact, 1 partition)

Stores connector and task states. RF=2 preferred but loss is non-critical
(state is re-derived from running connectors on restart).
