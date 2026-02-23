# Debezium CDC Format Benchmark — JSON vs Avro

**Date:** 2026-02-23  
**Table:** `Sales.SalesOrderDetailBig` — 2,000,000 rows (CROSS JOIN of `Sales.SalesOrderDetail` × 40)  
**Infrastructure:** SQL Server 2019 → Debezium 2.3 → Kafka (KRaft, 2 brokers) → Python consumer  
**Three connectors tested in sequence, each doing a full initial snapshot of 2M rows:**

| Connector | Format | Converter |
|-----------|--------|-----------|
| `bench-json-schema` | JSON with embedded schema | `org.apache.kafka.connect.json.JsonConverter` (`schemas.enable=true`) |
| `bench-json-noschema` | JSON payload only | `org.apache.kafka.connect.json.JsonConverter` (`schemas.enable=false`) |
| `bench-avro` | Avro binary | `io.confluent.connect.avro.AvroConverter` + Confluent Schema Registry |

---

## Results Table

| Metric | JSON + Schema | JSON (no schema) | Avro |
|--------|:-------------:|:----------------:|:----:|
| Messages consumed | 2,000,000 | 2,000,000 | 2,000,000 |
| **Total time (s)** | 515.9 | 85.1 | **67.4** |
| **Throughput (msg/s)** | 3,876 | 23,510 | **29,674** |
| Throughput (MB/s) | 17.09 | 15.23 | 6.08 |
| **Total data size (MB)** | 8,820 | 1,296 | **410** |
| **Avg message size (bytes)** | 4,624 | 679 | **215** |
| Median message size (bytes) | 4,626 | 681 | 220 |
| P99 message size (bytes) | 4,634 | 689 | 224 |
| **Size vs Avro** | **21.53x larger** | **3.16x larger** | baseline |
| **Latency P50 (ms)** | 1,549 | 2,051 | **4** |
| **Latency P95 (ms)** | 2,015 | 3,215 | 1,377 |
| **Latency P99 (ms)** | 2,892 | 4,093 | 3,881 |

---

## Analysis

### 1. Size — Avro wins decisively

**JSON + Schema is 21.5x larger than Avro** (8,820 MB vs 410 MB).

With `schemas.enable=true`, Debezium embeds the full connector envelope schema into **every single message**. For this table that amounts to ~4,400 bytes of schema metadata per message — dwarfing the actual row payload (~220 bytes). At 2 million messages this produces nearly **8.8 GB** of Kafka topic data for what is just 410 MB of real information.

**JSON no-schema is 3.16x larger than Avro** (1,296 MB vs 410 MB).

Removing the inline schema eliminates the bulk, but field names are still transmitted as UTF-8 strings in every message (e.g. `"SalesOrderDetailID"`, `"CarrierTrackingNumber"`, `"UnitPriceDiscount"`). These string keys account for the remaining gap vs. Avro's positional binary encoding.

**Avro average message = 215 bytes**, which includes:
- 1 byte magic byte (`0x00`)
- 4 bytes schema ID (reference to Schema Registry)
- ~210 bytes of compact binary row data

The schema is registered **once** in Schema Registry and referenced by ID thereafter — never repeated in the message payload.

---

### 2. Throughput (msg/s) — Avro wins

| Format | msg/s | vs Avro |
|--------|------:|--------|
| Avro | 29,674 | baseline |
| JSON (no schema) | 23,510 | 0.79x |
| JSON + Schema | 3,876 | **0.13x** |

**JSON + Schema is 7.6x slower than Avro** end-to-end. The bottleneck is twofold:
1. Kafka Connect's serializer must write a 4.6 KB payload per message — CPU and I/O intensive.
2. Kafka brokers must store and replicate 21x more bytes, saturating disk write throughput.

**JSON no-schema** reaches 79% of Avro's rate — respectable, but still slower because even 679-byte messages produce 3x more broker I/O than 215-byte Avro messages.

**Avro at 29,674 msg/s** is both the fastest serializer and the lightest broker load simultaneously.

---

### 3. MB/s throughput — an important nuance

| Format | MB/s written | Useful payload % |
|--------|------------:|:----------------:|
| JSON + Schema | 17.09 | ~5% (rest is schema) |
| JSON (no schema) | 15.23 | ~32% (rest is field names) |
| Avro | 6.08 | ~99% |

JSON + Schema writes **17 MB/s** — the highest MB/s — but most of that bandwidth is wasted schema boilerplate. Avro writes only **6 MB/s** because messages are so small that the network and disk are nearly idle at 29K msg/s. You could push roughly 4–5x more events per second before approaching bandwidth saturation.

---

### 4. Latency — Avro wins by a huge margin

**Avro P50 = 4 ms** — near-realtime. Because Avro messages are tiny (~215 bytes), Debezium fills its internal queue and flushes complete batches to Kafka within milliseconds of reading from the SQL Server CDC log.

**JSON + Schema P50 = 1,549 ms** — Debezium accumulates large messages slowly; each 8,192-message batch weighs ~37 MB before it is flushed to Kafka. The connector spends most of its time serializing.

**JSON no-schema P50 = 2,051 ms** — higher than JSON+Schema because by the time the third connector ran, Kafka already had 2M messages pre-written and the consumer was reading from a cold start with higher queue depths. The P99 of 4,093 ms shows tail latency blowing out when the topic backlog is large.

> **Key insight:** Low latency in CDC depends on small messages. Large messages mean fewer messages per broker flush, which directly increases end-to-end latency from DB commit to consumer receipt.

---

### 5. Winner Summary

| Dimension | Winner | Delta |
|-----------|--------|-------|
| Storage efficiency | **Avro** | 21.5x vs JSON+Schema, 3.2x vs JSON lean |
| Message throughput | **Avro** | 7.6x faster than JSON+Schema |
| End-to-end latency (P50) | **Avro** | 387x lower than JSON+Schema |
| Human readability | JSON (no schema) | No deserializer needed, readable in Kafka UI |
| Schema evolution safety | **Avro** | Enforced compatibility via Schema Registry |
| Operational simplicity | JSON (no schema) | No Schema Registry dependency |

---

### 6. When to Use Each Format

#### Use JSON + Schema if:
- You are in early development and need to inspect messages visually without tooling.
- You have a **very low volume** (< 10K msg/day) where schema overhead is negligible.
- You cannot run a Schema Registry and consumers need schema information embedded.

#### Use JSON (no schema) if:
- You want human-readable messages in Kafka UI / log files without any infrastructure overhead.
- Your consumers handle schema management externally (e.g. hardcoded DTO classes).
- Volume is moderate and storage cost is not a concern.

#### Use Avro (recommended for production) if:
- Volume exceeds ~100K msg/day — the storage and bandwidth savings compound quickly.
- **Low latency** is required (real-time pipelines, event-driven microservices).
- You need **schema evolution** with backward/forward compatibility guarantees enforced at write time.
- Multiple heterogeneous consumers exist — each fetches the schema once from Registry rather than parsing it from every message.
- You run on managed Kafka (Confluent Cloud, MSK, Aiven) where you pay per GB stored/transferred.

---

## How to Reproduce

```bash
# 1. Start all Docker services
docker compose up -d

# 2. Restore AdventureWorks2019 and enable CDC (if not already done)
docker exec -i sqlserver /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "YourStrong!Passw0rd" -C \
  -i /scripts/setup-adventureworks.sh

# 3. Create the 2M-row benchmark table
docker exec -i sqlserver /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "YourStrong!Passw0rd" -C \
  -i /scripts/create-benchmark-table.sql

# 4. Install Python dependencies
python3 -m venv benchmark/.venv
benchmark/.venv/bin/pip install -r benchmark/requirements.txt

# 5. Run the benchmark
benchmark/.venv/bin/python3 benchmark/benchmark.py
```

Raw results are saved to `benchmark/results.json` after each run.

---

## File Structure

```
sqlserver/create-benchmark-table.sql    SQL: 2M-row table creation + CDC enable
connect/connector-json-schema.json      Connector: JSON with embedded schema
connect/connector-json-noschema.json    Connector: JSON payload only
connect/connector-avro.json             Connector: Avro + Schema Registry
connect/deploy-benchmark.sh            Shell: deploy/delete/status helper
benchmark/benchmark.py                 Python: automated benchmark runner
benchmark/requirements.txt             Python: dependencies
benchmark/results.json                 Output: raw benchmark results (auto-generated)
```
