# Production — Kafka CDC Migration Stack

Production-ready Debezium + Kafka + Avro migration stack for SQL Server and
PostgreSQL. Every configuration decision is documented and grounded in benchmark
results from 2,000,000-row snapshot tests.

---

## Folder structure

```
Production/
├── infra/                          # Docker infrastructure
│   ├── docker-compose.yml          # All services (Kafka, Connect, DBs, Schema Registry)
│   ├── .env.example                # Environment variable template — copy to .env
│   ├── connect/
│   │   └── Dockerfile              # Debezium 2.3 + Confluent Avro JARs (multi-stage)
│   └── postgres/
│       └── init.sql                # PostgreSQL table + publication setup
│
├── connectors/
│   ├── sqlserver/
│   │   ├── connector.json          # Best-config SQL Server connector (tasks=1, batch=2048)
│   │   └── decisions.md            # Why every parameter has its value
│   └── postgres/
│       ├── connector.json          # Best-config PostgreSQL connector (tasks=4, batch=2048)
│       └── decisions.md            # Why every parameter has its value
│
├── topics/
│   ├── create-topics.sh            # Pre-create topics with production settings (RF=2, 4 partitions)
│   └── decisions.md                # Why partitions=4, RF=2, retention=7d, etc.
│
├── scripts/
│   ├── deploy.sh                   # Full deployment: infra → topics → connectors
│   ├── health-check.sh             # Verify all components are healthy (exit 0/1)
│   ├── monitor.sh                  # Live migration dashboard (lag, rate, ETA)
│   └── teardown.sh                 # Graceful shutdown + PG slot cleanup
│
└── docs/
    ├── architecture.md             # System diagram, component roles, data flow
    ├── runbook.md                  # Step-by-step operations (deploy, monitor, recover, scale)
    └── decisions.md                # Index of every decision with benchmark evidence
```

---

## Quick start

```bash
# 1. Configure environment
cp infra/.env.example infra/.env
# Edit infra/.env — set KAFKA_CLUSTER_ID, passwords, topic prefixes

# 2. Deploy everything
./scripts/deploy.sh both

# 3. Monitor progress
./scripts/monitor.sh

# 4. Verify completion
./scripts/health-check.sh

# 5. Tear down when done
./scripts/teardown.sh
```

---

## Benchmark-validated best configs

### SQL Server → Kafka

| Parameter | Value | Benchmark rank |
|-----------|-------|---------------|
| `tasks.max` | **1** | C1 = 37,166 msg/s (best) |
| `max.batch.size` | **2048** | C1 = 53.8s (vs 68–71s for larger batches) |
| `max.queue.size` | **8192** | 4× batch, minimum GC overhead |
| `poll.interval.ms` | **1000ms** | No benefit below 1s during snapshot |
| Expected throughput | ~37,000 msg/s | 2M rows in ~54s |

### PostgreSQL → Kafka

| Parameter | Value | Benchmark rank |
|-----------|-------|---------------|
| `tasks.max` | **4** | C3 = 23,277 msg/s (best) |
| `max.batch.size` | **2048** | C3 = 85.9s (vs 113–125s for larger batches) |
| `max.queue.size` | **8192** | 4× batch |
| `poll.interval.ms` | **500ms** | Optimal for PG WAL streaming phase |
| `heartbeat.interval.ms` | **10000ms** | Prevents WAL retention bloat on idle tables |
| Expected throughput | ~23,000 msg/s | 2M rows in ~86s |

### Kafka topics

| Setting | Value | Why |
|---------|-------|-----|
| `partitions` | **4** | Up to 4 parallel consumer threads |
| `replication.factor` | **2** | Survives 1 broker failure |
| `retention.ms` | **7 days** | Consumer catch-up + replay window |
| `compression.type` | **lz4** | ~25% size reduction, minimal CPU |

---

## Key constraints

1. **`topic.prefix` must use underscores only.** Hyphens are invalid Avro
   namespaces and cause schema registration failures.

2. **`max.queue.size` must be strictly greater than `max.batch.size`.** Kafka
   Connect enforces this at connector deployment and rejects the config if violated.

3. **Pre-create topics before deploying connectors.** Auto-created topics get
   `replication.factor=1` (data loss risk) and `partitions=1` (no consumer
   parallelism).

4. **Drop PostgreSQL replication slots when done.** An active slot with no
   consumer causes unbounded WAL disk growth. `teardown.sh` handles this automatically.

5. **SQL Server requires Snapshot Isolation enabled on the database** before
   deploying the connector. Without it, the snapshot holds shared row locks for
   its entire duration, blocking all writes.

---

## Further reading

- `docs/architecture.md` — System diagram and component explanations
- `docs/runbook.md` — Operational procedures (deploy, recover, scale, teardown)
- `docs/decisions.md` — Full decision index with benchmark evidence
- `connectors/sqlserver/decisions.md` — SQL Server connector parameter rationale
- `connectors/postgres/decisions.md` — PostgreSQL connector parameter rationale
- `topics/decisions.md` — Kafka topic configuration rationale
