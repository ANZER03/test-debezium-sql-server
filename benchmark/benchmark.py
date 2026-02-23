#!/usr/bin/env python3
"""
benchmark.py
============
Benchmarks three Debezium CDC connectors against the Sales.SalesOrderDetailBig
table (2 million rows):

  1. bench-json-schema   — JSON with full Debezium envelope schema embedded
  2. bench-json-noschema — JSON payload only (no inline schema)
  3. bench-avro          — Confluent Avro (schema stored in Schema Registry)

For each connector the script:
  - Deletes any pre-existing connector via Kafka Connect REST API
  - (Re-)deploys the connector
  - Waits for it to reach RUNNING state
  - Consumes ALL messages from the connector's topic until the expected count
    is reached OR a configurable idle timeout fires
  - Collects per-message size (key + value bytes) and end-to-end latency
    (Kafka message timestamp vs. wall clock at consumption time)
  - After all connectors finish, prints a side-by-side comparison table
    and saves results.json

Usage:
  pip install -r requirements.txt
  python3 benchmark.py

Configuration (environment variables override defaults):
  KAFKA_BOOTSTRAP      Kafka broker(s)              default: localhost:9092
  SCHEMA_REGISTRY_URL  Confluent Schema Registry    default: http://localhost:8081
  CONNECT_URL          Kafka Connect REST API        default: http://localhost:8083
  EXPECTED_ROWS        Expected row count per topic default: 2000000
  IDLE_TIMEOUT_SEC     Seconds of silence = done    default: 60
  CONNECTOR_DIR        Directory with connector JSON default: ../connect
"""

import json
import os
import sys
import time
import statistics
import pathlib
import datetime

import requests
from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition
from tabulate import tabulate

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
SCHEMA_REGISTRY_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://localhost:8081")
CONNECT_URL = os.getenv("CONNECT_URL", "http://localhost:8083")
EXPECTED_ROWS = int(os.getenv("EXPECTED_ROWS", "2000000"))
IDLE_TIMEOUT_SEC = int(os.getenv("IDLE_TIMEOUT_SEC", "60"))
CONNECTOR_DIR = pathlib.Path(
    os.getenv("CONNECTOR_DIR", str(pathlib.Path(__file__).parent.parent / "connect"))
)

# Connector name → topic prefix → config file
CONNECTORS = [
    {
        "name": "bench-json-schema",
        "topic_prefix": "bench-json-schema",
        "config_file": CONNECTOR_DIR / "connector-json-schema.json",
        "format": "JSON + Schema",
    },
    {
        "name": "bench-json-noschema",
        "topic_prefix": "bench-json-noschema",
        "config_file": CONNECTOR_DIR / "connector-json-noschema.json",
        "format": "JSON (no schema)",
    },
    {
        "name": "bench-avro",
        "topic_prefix": "bench-avro",
        "config_file": CONNECTOR_DIR / "connector-avro.json",
        "format": "Avro",
    },
]


# ---------------------------------------------------------------------------
# Kafka Connect helpers
# ---------------------------------------------------------------------------


def connect_get(path):
    r = requests.get(f"{CONNECT_URL}{path}", timeout=10)
    r.raise_for_status()
    return r.json()


def connect_post(path, payload):
    r = requests.post(
        f"{CONNECT_URL}{path}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code not in (200, 201):
        print(f"  POST {path} => HTTP {r.status_code}: {r.text}")
        r.raise_for_status()
    return r.json()


def connect_delete(path):
    r = requests.delete(f"{CONNECT_URL}{path}", timeout=10)
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()


def wait_for_connect(timeout=120):
    print("Waiting for Kafka Connect...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            connect_get("/connectors")
            print("  Kafka Connect is ready.")
            return
        except Exception:
            time.sleep(3)
    raise RuntimeError("Kafka Connect did not become ready in time.")


def delete_connector(name):
    try:
        connect_delete(f"/connectors/{name}")
        print(f"  Deleted connector: {name}")
        time.sleep(2)
    except Exception:
        pass  # already gone


def deploy_connector(config_file: pathlib.Path):
    payload = json.loads(config_file.read_text())
    name = payload["name"]
    result = connect_post("/connectors", payload)
    print(f"  Deployed connector: {name}")
    return name


def wait_for_running(name, timeout=300):
    print(f"  Waiting for {name} to reach RUNNING state...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = connect_get(f"/connectors/{name}/status")
            connector_state = status["connector"]["state"]
            tasks = status.get("tasks", [])
            if (
                connector_state == "RUNNING"
                and tasks
                and all(t["state"] == "RUNNING" for t in tasks)
            ):
                print(f"  {name} is RUNNING ({len(tasks)} task(s)).")
                return
            elif connector_state == "FAILED":
                print(f"  ERROR: {name} FAILED. Status: {json.dumps(status, indent=2)}")
                raise RuntimeError(f"Connector {name} failed to start.")
            else:
                states = [t["state"] for t in tasks]
                print(f"    connector={connector_state}, tasks={states}")
        except RuntimeError:
            raise
        except Exception as e:
            print(f"    Status check error: {e}")
        time.sleep(5)
    raise RuntimeError(f"{name} did not reach RUNNING state within {timeout}s.")


# ---------------------------------------------------------------------------
# Topic discovery
# ---------------------------------------------------------------------------


def get_topic_for_connector(topic_prefix):
    """Return the CDC topic name: <prefix>.AdventureWorks2019.Sales.SalesOrderDetailBig"""
    return f"{topic_prefix}.AdventureWorks2019.Sales.SalesOrderDetailBig"


# ---------------------------------------------------------------------------
# Kafka log size helper (via kafka-log-dirs on the broker container)
# ---------------------------------------------------------------------------


def get_topic_log_size_bytes(topic):
    """
    Query Kafka Connect's /admin/logdirs endpoint is not available.
    Instead we use the Schema Registry REST API or just skip and return None.
    The actual on-disk size is computed from the byte totals we collect during
    consumption, which is the uncompressed wire size.
    """
    return None  # populated from consumption metrics instead


# ---------------------------------------------------------------------------
# Consumer benchmark
# ---------------------------------------------------------------------------


def benchmark_connector(connector_info):
    """
    Deploy the connector, consume all messages, return metrics dict.
    """
    name = connector_info["name"]
    topic_prefix = connector_info["topic_prefix"]
    config_file = connector_info["config_file"]
    fmt = connector_info["format"]
    topic = get_topic_for_connector(topic_prefix)

    print(f"\n{'=' * 70}")
    print(f"  Benchmarking: {fmt}  ({name})")
    print(f"  Topic: {topic}")
    print(f"{'=' * 70}")

    # -- Delete old connector + its offset state
    delete_connector(name)

    # -- Also delete the offset data by wiping the topic (best-effort)
    #    We achieve this by letting each connector use snapshot.mode=initial
    #    with a unique topic prefix, so there are no leftover offsets.

    # -- Deploy connector
    deploy_connector(config_file)
    wait_for_running(name)

    # -- Set up raw Kafka consumer (no deserializer — we measure raw bytes)
    consumer_conf = {
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": f"benchmark-{name}-{int(time.time())}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
        # Fetch large batches for throughput
        "fetch.max.bytes": str(50 * 1024 * 1024),  # 50 MB
        "max.partition.fetch.bytes": str(50 * 1024 * 1024),
    }
    consumer = Consumer(consumer_conf)
    consumer.subscribe([topic])

    msg_count = 0
    total_bytes = 0
    latencies_ms = []
    sizes_bytes = []
    first_msg_time = None
    last_msg_time = None
    idle_since = time.time()

    print(
        f"  Consuming messages... (idle timeout = {IDLE_TIMEOUT_SEC}s after last message)"
    )
    start_wall = time.time()
    report_every = 100_000

    while True:
        msg = consumer.poll(timeout=2.0)

        if msg is None:
            # No message — check idle timeout
            if time.time() - idle_since > IDLE_TIMEOUT_SEC:
                print(f"\n  Idle timeout reached after {msg_count:,} messages.")
                break
            continue

        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                # End of partition — but we keep waiting for more
                pass
            else:
                raise KafkaException(msg.error())
            continue

        # Valid message
        now = time.time()
        idle_since = now

        key_bytes = msg.key() or b""
        value_bytes = msg.value() or b""
        msg_size = len(key_bytes) + len(value_bytes)

        msg_count += 1
        total_bytes += msg_size
        sizes_bytes.append(msg_size)

        # End-to-end latency: Kafka timestamp → consumption time
        kafka_ts_ms = msg.timestamp()[1]  # (TIMESTAMP_TYPE, ms)
        if kafka_ts_ms and kafka_ts_ms > 0:
            latency_ms = (now * 1000) - kafka_ts_ms
            if latency_ms >= 0:
                latencies_ms.append(latency_ms)

        if first_msg_time is None:
            first_msg_time = now
        last_msg_time = now

        if msg_count % report_every == 0:
            elapsed = now - start_wall
            rate = msg_count / elapsed if elapsed > 0 else 0
            mb_sec = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            print(
                f"    {msg_count:>9,} / {EXPECTED_ROWS:,}  |  "
                f"{elapsed:6.1f}s  |  {rate:,.0f} msg/s  |  {mb_sec:.2f} MB/s"
            )

        if msg_count >= EXPECTED_ROWS:
            print(f"\n  Reached expected row count ({EXPECTED_ROWS:,}).")
            break

    end_wall = time.time()
    consumer.close()

    # -- Compute metrics
    elapsed_sec = (last_msg_time or end_wall) - start_wall
    stream_sec = (
        (last_msg_time - first_msg_time)
        if (first_msg_time and last_msg_time and last_msg_time > first_msg_time)
        else elapsed_sec
    )
    throughput_msg = msg_count / elapsed_sec if elapsed_sec > 0 else 0
    throughput_mb = (
        (total_bytes / (1024 * 1024)) / elapsed_sec if elapsed_sec > 0 else 0
    )
    avg_size = total_bytes / msg_count if msg_count > 0 else 0
    median_size = statistics.median(sizes_bytes) if sizes_bytes else 0
    p99_size = sorted(sizes_bytes)[int(len(sizes_bytes) * 0.99)] if sizes_bytes else 0
    total_mb = total_bytes / (1024 * 1024)

    lat_median = statistics.median(latencies_ms) if latencies_ms else 0
    lat_p95 = sorted(latencies_ms)[int(len(latencies_ms) * 0.95)] if latencies_ms else 0
    lat_p99 = sorted(latencies_ms)[int(len(latencies_ms) * 0.99)] if latencies_ms else 0

    result = {
        "connector": name,
        "format": fmt,
        "messages_consumed": msg_count,
        "total_time_sec": round(elapsed_sec, 2),
        "throughput_msg_sec": round(throughput_msg, 1),
        "throughput_mb_sec": round(throughput_mb, 3),
        "total_data_mb": round(total_mb, 2),
        "avg_msg_size_bytes": round(avg_size, 1),
        "median_msg_size_bytes": round(median_size, 1),
        "p99_msg_size_bytes": round(p99_size, 1),
        "latency_median_ms": round(lat_median, 1),
        "latency_p95_ms": round(lat_p95, 1),
        "latency_p99_ms": round(lat_p99, 1),
    }

    print(f"\n  Results for {fmt}:")
    for k, v in result.items():
        print(f"    {k:<30}: {v}")

    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_comparison(results):
    if not results:
        print("No results to compare.")
        return

    # ---- Size comparison
    avro_result = next((r for r in results if "avro" in r["connector"].lower()), None)
    for r in results:
        if avro_result and avro_result["total_data_mb"] > 0:
            r["size_vs_avro"] = (
                f"{r['total_data_mb'] / avro_result['total_data_mb']:.2f}x"
            )
        else:
            r["size_vs_avro"] = "N/A"

    headers = [
        "Format",
        "Messages",
        "Time (s)",
        "Throughput\n(msg/s)",
        "Throughput\n(MB/s)",
        "Total Size\n(MB)",
        "Avg Msg Size\n(bytes)",
        "Size vs Avro",
        "Latency P50\n(ms)",
        "Latency P95\n(ms)",
        "Latency P99\n(ms)",
    ]

    rows = []
    for r in results:
        rows.append(
            [
                r["format"],
                f"{r['messages_consumed']:,}",
                f"{r['total_time_sec']:,.1f}",
                f"{r['throughput_msg_sec']:,.0f}",
                f"{r['throughput_mb_sec']:.2f}",
                f"{r['total_data_mb']:,.2f}",
                f"{r['avg_msg_size_bytes']:.1f}",
                r["size_vs_avro"],
                f"{r['latency_median_ms']:.1f}",
                f"{r['latency_p95_ms']:.1f}",
                f"{r['latency_p99_ms']:.1f}",
            ]
        )

    print("\n")
    print("=" * 90)
    print("  BENCHMARK RESULTS — Debezium JSON vs Avro (2,000,000 rows)")
    print("=" * 90)
    print(tabulate(rows, headers=headers, tablefmt="grid"))

    # ---- Narrative summary
    if avro_result:
        print("\n  KEY FINDINGS:")
        for r in results:
            if r["connector"] == avro_result["connector"]:
                continue
            size_ratio = (
                r["total_data_mb"] / avro_result["total_data_mb"]
                if avro_result["total_data_mb"] > 0
                else float("inf")
            )
            speed_ratio = (
                r["throughput_msg_sec"] / avro_result["throughput_msg_sec"]
                if avro_result["throughput_msg_sec"] > 0
                else float("inf")
            )
            print(f"\n  [{r['format']} vs Avro]")
            print(f"    Size    : {r['format']} is {size_ratio:.2f}x LARGER than Avro")
            print(
                f"              ({r['total_data_mb']:.1f} MB vs {avro_result['total_data_mb']:.1f} MB)"
            )
            print(
                f"    Throughput: {r['format']} is {speed_ratio:.2f}x the msg/s of Avro"
            )
            print(
                f"    Avg msg : {r['avg_msg_size_bytes']:.0f} bytes  vs  {avro_result['avg_msg_size_bytes']:.0f} bytes (Avro)"
            )
    print("\n")


def save_results(results, path="results.json"):
    output = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "config": {
            "kafka_bootstrap": KAFKA_BOOTSTRAP,
            "schema_registry_url": SCHEMA_REGISTRY_URL,
            "connect_url": CONNECT_URL,
            "expected_rows": EXPECTED_ROWS,
            "idle_timeout_sec": IDLE_TIMEOUT_SEC,
        },
        "results": results,
    }
    p = pathlib.Path(__file__).parent / path
    p.write_text(json.dumps(output, indent=2))
    print(f"  Results saved to: {p}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 70)
    print("  Debezium CDC Format Benchmark")
    print(f"  Kafka:          {KAFKA_BOOTSTRAP}")
    print(f"  Schema Registry:{SCHEMA_REGISTRY_URL}")
    print(f"  Connect:        {CONNECT_URL}")
    print(f"  Expected rows:  {EXPECTED_ROWS:,}")
    print(f"  Connectors dir: {CONNECTOR_DIR}")
    print("=" * 70)

    wait_for_connect()

    results = []
    for connector_info in CONNECTORS:
        try:
            result = benchmark_connector(connector_info)
            results.append(result)
        except Exception as e:
            print(f"\nERROR benchmarking {connector_info['name']}: {e}")
            import traceback

            traceback.print_exc()
            results.append(
                {
                    "connector": connector_info["name"],
                    "format": connector_info["format"],
                    "error": str(e),
                }
            )

    print_comparison([r for r in results if "error" not in r])
    save_results(results)


if __name__ == "__main__":
    main()
