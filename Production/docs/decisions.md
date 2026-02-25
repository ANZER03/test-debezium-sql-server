# Decision Index

Every production configuration choice is documented here with a rationale
grounded in the 5-case benchmark results (2,000,000 rows, Avro, 2-broker Kafka).
For the full benchmark data, see `migration-perf-test/PRODUCTION_STRATEGY.md`.

---

## Infrastructure decisions

| Decision | Choice | File | Why |
|----------|--------|------|-----|
| Kafka mode | KRaft (no ZooKeeper) | `infra/docker-compose.yml` | ZooKeeper removed in Kafka 3.x+; KRaft reduces ops complexity |
| Image tags | Pinned (`7.7.7`, `2.3`, `15`) | `infra/docker-compose.yml` | `:latest` breaks on restart silently |
| Broker count | 2 | `infra/docker-compose.yml` | Minimum for RF=2; 1 broker forces RF=1 = data loss risk |
| Replication factor | 2 (all topics) | `infra/docker-compose.yml`, `topics/create-topics.sh` | Survives 1 broker failure without data loss or re-snapshot |
| Broker compression | lz4 | `infra/docker-compose.yml` | ~25% size reduction, <2% CPU cost |
| Broker JVM heap | 4 GB per broker | `infra/docker-compose.yml` | GC pauses observed at 1 GB default under 40 MB/s sustained |
| Connect image | Multi-stage Dockerfile | `infra/connect/Dockerfile` | Self-contained; no runtime JAR downloads |
| Connect JVM heap | 2 GB | `infra/docker-compose.yml` | Batch=2048, 4 tasks causes OOM at 256 MB default |
| Offset flush interval | 10s (vs 60s default) | `infra/docker-compose.yml` | Reduces reprocessing window on crash |
| Schema Registry | Karapace | `infra/docker-compose.yml` | Apache 2.0; wire-compatible with Confluent; no license cost |
| Schema compatibility | BACKWARD | `infra/docker-compose.yml` | Prevents schema changes that break running consumers |
| Secret management | `.env` file (not baked in) | `infra/.env.example` | Credentials must never be hardcoded in source control |
| PG WAL level | logical | `infra/docker-compose.yml` | Required for pgoutput logical replication |
| PG replication slots | max=10 | `infra/docker-compose.yml` | One slot per connector; need headroom for parallel table migrations |
| PG snapshot isolation | exported snapshot (automatic) | pgoutput default | Consistent point-in-time read without blocking writers |
| SQL Server snapshot isolation | `snapshot` mode | `connectors/sqlserver/connector.json` | Avoids shared row locks during snapshot; no write blocking |

---

## Connector decisions

| Decision | SQL Server value | PostgreSQL value | Why |
|----------|-----------------|-----------------|-----|
| `tasks.max` | **1** | **4** | MSSQL: tasks>1 causes contention (-24% throughput). PG: tasks=4 parallelizes pipeline (+15% throughput) |
| `max.batch.size` | **2048** | **2048** | Larger batches are slower on both databases. 2048 fills in ~55ms (MSSQL) / ~88ms (PG) at natural throughput rate |
| `max.queue.size` | **8192** | **8192** | 4× batch size. Must be > batch. Larger queues add GC pressure |
| `poll.interval.ms` | **1000ms** | **500ms** | MSSQL CDC poll <1s wastes CPU (source is bottleneck). PG 500ms keeps streaming CDC responsive |
| `snapshot.mode` | `initial` | `initial` | Full snapshot + streaming CDC. Correct for migration use case |
| `tombstones.on.delete` | `false` | `false` | Migration use case does not need compaction tombstones |
| `heartbeat.interval.ms` | N/A | **10000ms** | PG-specific: advances slot LSN when table is idle; prevents WAL retention bloat |
| Serialization | Avro | Avro | 4× smaller than JSON. 212B/msg (MSSQL) vs 224B/msg (PG) |
| `topic.prefix` | `prod_mssql` | `prod_pg` | Underscores only — hyphens are invalid Avro namespaces |

---

## Topic decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Pre-create topics | Yes (before connector start) | Avoids auto-creation with wrong defaults (RF=1, 1 partition) |
| Data topic partitions | **4** | Allows 4 parallel consumer threads; Debezium distributes by PK hash |
| Schema history partitions | **1** | DDL history must be strictly ordered; multiple partitions breaks replay |
| Connect internal topic partitions | **1** | Ordered by Connect internally |
| `cleanup.policy` (schema history, internal) | `compact` | Retains only latest value per key; prevents unbounded growth |
| `retention.ms` | 604800000 (7 days) | Long enough for consumer catch-up + replay window after migration |
| `segment.bytes` | 536870912 (512 MB) | Reduces file count for 400 MB+ datasets |
| `compression.type` | lz4 | Consistent with broker-level setting |

---

## Performance benchmark summary

| Case | MSSQL msg/s | PG msg/s | Winner |
|------|-------------|----------|--------|
| C1 tasks=1, batch=2048 | **37,166** | 20,294 | MSSQL C1 |
| C2 tasks=1, batch=8192 | 29,410 | 16,561 | — |
| C3 tasks=4, batch=2048 | 28,022 | **23,277** | PG C3 |
| C4 tasks=4, batch=8192 | 30,177 | 17,782 | — |
| C5 tasks=4, batch=16384 | 29,296 | 15,983 | — |

**SQL Server is 20–85% faster than PostgreSQL** across all configurations.
The bottleneck in both cases is the source database, not Kafka or Connect.

---

## What was deliberately NOT included

| Excluded | Reason |
|----------|--------|
| TLS/mTLS between services | Adds cert management complexity; document the pattern but leave for org-specific PKI setup |
| Confluent Schema Registry | Requires a commercial license; Karapace is wire-compatible and free |
| `tasks.max > 1` for SQL Server | Benchmark proved it is slower due to CDC contention |
| `max.batch.size > 2048` | Benchmark proved larger batches are slower on both databases |
| `poll.interval.ms < 500ms` | Busy-wait CPU overhead with no throughput benefit during snapshot |
| ZooKeeper | Removed from Kafka 3.x+; KRaft is the correct modern choice |
| JSON converter | 4× larger messages; Avro is strictly better for high-volume migration |
| Connector `FOR ALL TABLES` publication (PG) | Increases WAL volume unnecessarily; use table-specific publications |
