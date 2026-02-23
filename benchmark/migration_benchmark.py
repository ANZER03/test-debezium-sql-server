#!/usr/bin/env python3
"""
migration_benchmark.py
======================
Benchmarks 4 Debezium Avro connectors migrating Sales.SalesOrderDetailBig
(2,000,000 rows) with different tuning parameters AND captures full system
observability (CPU %, RAM MB, Net MB/s, Disk MB/s) for every Docker container
while each case runs.

  Case 1 — Baseline      : tasks.max=1, batch=2048,  queue=8192,  poll=1000ms
  Case 2 — Large Batches : tasks.max=1, batch=8192,  queue=32768, poll=500ms
  Case 3 — Multi-Thread  : tasks.max=4, batch=2048,  queue=8192,  poll=500ms
  Case 4 — Fully Tuned   : tasks.max=4, batch=8192,  queue=32768, poll=200ms

Per case, two parallel threads run:
  A) Consumer thread   — polls Kafka, records throughput + latency time-series
  B) System collector  — polls Docker API every 1s for per-container metrics

Output: benchmark/migration_results.json  (fed to visualize.py)
"""

import json
import os
import sys
import time
import pathlib
import datetime
import statistics
import threading

import requests
from confluent_kafka import Consumer, KafkaError, KafkaException

# Local collector
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from system_collector import SystemCollector

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
SCHEMA_REGISTRY_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://localhost:8081")
CONNECT_URL = os.getenv("CONNECT_URL", "http://localhost:8083")
EXPECTED_ROWS = int(os.getenv("EXPECTED_ROWS", "2000000"))
IDLE_TIMEOUT_SEC = int(os.getenv("IDLE_TIMEOUT_SEC", "90"))
SAMPLE_INTERVAL_SEC = float(os.getenv("SAMPLE_INTERVAL_SEC", "2"))
SYS_SAMPLE_SEC = float(os.getenv("SYS_SAMPLE_SEC", "1"))

CONNECTOR_DIR = pathlib.Path(
    os.getenv("CONNECTOR_DIR", str(pathlib.Path(__file__).parent.parent / "connect"))
)

# Containers to monitor during migration
MONITORED_CONTAINERS = [
    "kafka-connect",
    "kafka",
    "kafka-broker-2",
    "sqlserver",
    "schema-registry",
]

CASES = [
    {
        "name": "migration-case1-baseline",
        "label": "Case 1 — Baseline\n(tasks=1, batch=2048, queue=8192, poll=1000ms)",
        "short_label": "C1-Baseline",
        "config_file": CONNECTOR_DIR / "migration-case1-baseline.json",
        "tasks_max": 1,
        "batch_size": 2048,
        "queue_size": 8192,
        "poll_ms": 1000,
        "topic_prefix": "migration-c1",
    },
    {
        "name": "migration-case2-largebatch",
        "label": "Case 2 — Large Batches\n(tasks=1, batch=8192, queue=32768, poll=500ms)",
        "short_label": "C2-LargeBatch",
        "config_file": CONNECTOR_DIR / "migration-case2-largebatch.json",
        "tasks_max": 1,
        "batch_size": 8192,
        "queue_size": 32768,
        "poll_ms": 500,
        "topic_prefix": "migration-c2",
    },
    {
        "name": "migration-case3-multithread",
        "label": "Case 3 — Multi-Thread\n(tasks=4, batch=2048, queue=8192, poll=500ms)",
        "short_label": "C3-MultiThread",
        "config_file": CONNECTOR_DIR / "migration-case3-multithread.json",
        "tasks_max": 4,
        "batch_size": 2048,
        "queue_size": 8192,
        "poll_ms": 500,
        "topic_prefix": "migration-c3",
    },
    {
        "name": "migration-case4-fullytuned",
        "label": "Case 4 — Fully Tuned\n(tasks=4, batch=8192, queue=32768, poll=200ms)",
        "short_label": "C4-FullyTuned",
        "config_file": CONNECTOR_DIR / "migration-case4-fullytuned.json",
        "tasks_max": 4,
        "batch_size": 8192,
        "queue_size": 32768,
        "poll_ms": 200,
        "topic_prefix": "migration-c4",
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
        raise RuntimeError(f"POST {path} => HTTP {r.status_code}: {r.text}")
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
    raise RuntimeError("Kafka Connect not ready.")


def delete_connector(name):
    try:
        connect_delete(f"/connectors/{name}")
        print(f"  Deleted connector: {name}")
        time.sleep(2)
    except Exception:
        pass


def deploy_connector(config_file: pathlib.Path):
    payload = json.loads(config_file.read_text())
    payload["config"] = {
        k: v for k, v in payload["config"].items() if not k.startswith("_")
    }
    connect_post("/connectors", payload)
    print(f"  Deployed: {payload['name']}")


def wait_for_running(name, timeout=300):
    print(f"  Waiting for {name} RUNNING...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = connect_get(f"/connectors/{name}/status")
            cstate = status["connector"]["state"]
            tasks = status.get("tasks", [])
            if (
                cstate == "RUNNING"
                and tasks
                and all(t["state"] == "RUNNING" for t in tasks)
            ):
                print(f"  {name} RUNNING ({len(tasks)} tasks).")
                return
            if cstate == "FAILED":
                raise RuntimeError(f"Connector {name} FAILED: {json.dumps(status)}")
            states = [t["state"] for t in tasks]
            print(f"    connector={cstate} tasks={states}")
        except RuntimeError:
            raise
        except Exception as e:
            print(f"    status error: {e}")
        time.sleep(5)
    raise RuntimeError(f"{name} not RUNNING within {timeout}s.")


# ---------------------------------------------------------------------------
# Consumer + metric collection
# ---------------------------------------------------------------------------


def run_case(case: dict) -> dict:
    name = case["name"]
    topic = f"{case['topic_prefix']}.AdventureWorks2019.Sales.SalesOrderDetailBig"
    cfg = case["config_file"]

    print(f"\n{'=' * 72}")
    print(f"  {case['short_label']}")
    print(
        f"  tasks.max={case['tasks_max']}  batch={case['batch_size']}  "
        f"queue={case['queue_size']}  poll={case['poll_ms']}ms"
    )
    print(f"{'=' * 72}")

    # Deploy connector
    delete_connector(name)
    deploy_connector(cfg)
    wait_for_running(name)

    # Start system observability collector BEFORE consumer loop
    collector = SystemCollector(
        containers=MONITORED_CONTAINERS,
        interval=SYS_SAMPLE_SEC,
    )
    collector.start()

    # Kafka consumer (raw bytes — no deserializer for pure throughput measurement)
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": f"bench-{name}-{int(time.time())}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "fetch.max.bytes": str(50 * 1024 * 1024),
            "max.partition.fetch.bytes": str(50 * 1024 * 1024),
        }
    )
    consumer.subscribe([topic])

    # --- State
    msg_count = 0
    total_bytes = 0
    start_wall = time.time()
    last_sample_t = start_wall
    idle_since = start_wall
    first_msg_ts = None

    # Time-series (Kafka / consumer side)
    ts_elapsed = []
    ts_msg_rate = []
    ts_mb_rate = []
    ts_cumulative = []
    ts_avg_size = []
    ts_lat_p50 = []
    ts_lat_p99 = []

    # Window accumulators
    win_msgs = 0
    win_bytes = 0
    win_lats = []

    print(
        f"  Consuming {EXPECTED_ROWS:,} messages (idle_timeout={IDLE_TIMEOUT_SEC}s) ..."
    )

    while True:
        msg = consumer.poll(timeout=1.0)
        now = time.time()

        if msg is None:
            if now - idle_since > IDLE_TIMEOUT_SEC:
                print(f"\n  Idle timeout — {msg_count:,} messages consumed.")
                break
        elif msg.error():
            err = msg.error()
            if err and err.code() != KafkaError._PARTITION_EOF:
                raise KafkaException(err)
        else:
            idle_since = now
            if first_msg_ts is None:
                first_msg_ts = now
            kb = msg.key() or b""
            vb = msg.value() or b""
            sz = len(kb) + len(vb)
            msg_count += 1
            total_bytes += sz
            win_msgs += 1
            win_bytes += sz
            kafka_ts = msg.timestamp()[1]
            if kafka_ts and kafka_ts > 0:
                lat = now * 1000 - kafka_ts
                if lat >= 0:
                    win_lats.append(lat)

        # Sample every SAMPLE_INTERVAL_SEC
        if now - last_sample_t >= SAMPLE_INTERVAL_SEC:
            dt = max(now - last_sample_t, 0.001)
            elapsed = now - start_wall
            ts_elapsed.append(round(elapsed, 2))
            ts_msg_rate.append(round(win_msgs / dt, 1))
            ts_mb_rate.append(round((win_bytes / 1048576) / dt, 3))
            ts_cumulative.append(msg_count)
            ts_avg_size.append(round(win_bytes / win_msgs, 1) if win_msgs else 0)
            ts_lat_p50.append(
                round(statistics.median(win_lats), 1) if win_lats else 0.0
            )
            ts_lat_p99.append(
                round(sorted(win_lats)[int(len(win_lats) * 0.99)], 1)
                if len(win_lats) >= 100
                else (round(max(win_lats), 1) if win_lats else 0.0)
            )
            pct = msg_count / EXPECTED_ROWS * 100
            print(
                f"    {msg_count:>9,} ({pct:5.1f}%)  {elapsed:6.1f}s  "
                f"{ts_msg_rate[-1]:>8,.0f} msg/s  {ts_mb_rate[-1]:5.2f} MB/s  "
                f"lat_p50={ts_lat_p50[-1]:.0f}ms"
            )
            win_msgs = 0
            win_bytes = 0
            win_lats = []
            last_sample_t = now

        if msg_count >= EXPECTED_ROWS:
            print(f"\n  Reached {EXPECTED_ROWS:,} messages.")
            break

    end_wall = time.time()
    consumer.close()
    collector.stop()

    total_sec = end_wall - start_wall
    avg_msg_sec = msg_count / total_sec if total_sec > 0 else 0
    avg_mb_sec = (total_bytes / 1048576) / total_sec if total_sec > 0 else 0
    total_mb = total_bytes / 1048576
    avg_size = total_bytes / msg_count if msg_count > 0 else 0

    print(
        f"\n  Done: {msg_count:,} msgs | {total_sec:.1f}s | "
        f"{avg_msg_sec:,.0f} msg/s | {total_mb:.1f} MB | {avg_size:.0f} B/msg"
    )

    return {
        "connector": name,
        "short_label": case["short_label"],
        "label": case["label"],
        "tasks_max": case["tasks_max"],
        "batch_size": case["batch_size"],
        "queue_size": case["queue_size"],
        "poll_ms": case["poll_ms"],
        "messages_consumed": msg_count,
        "total_time_sec": round(total_sec, 2),
        "throughput_msg_sec": round(avg_msg_sec, 1),
        "throughput_mb_sec": round(avg_mb_sec, 3),
        "total_data_mb": round(total_mb, 2),
        "avg_msg_size_bytes": round(avg_size, 1),
        "timeseries": {
            "elapsed_sec": ts_elapsed,
            "msg_rate": ts_msg_rate,
            "mb_rate": ts_mb_rate,
            "cumulative_msgs": ts_cumulative,
            "avg_size_bytes": ts_avg_size,
            "latency_p50_ms": ts_lat_p50,
            "latency_p99_ms": ts_lat_p99,
        },
        # system metrics: {container: [{elapsed, cpu_pct, mem_mb, ...}, ...]}
        "system": collector.get_data(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 72)
    print("  Debezium Avro Migration Benchmark — Observability Edition")
    print(f"  4 Cases | 2M rows | System monitoring: {MONITORED_CONTAINERS}")
    print(f"  Kafka: {KAFKA_BOOTSTRAP}  |  Connect: {CONNECT_URL}")
    print(
        f"  Kafka sample: {SAMPLE_INTERVAL_SEC}s  |  System sample: {SYS_SAMPLE_SEC}s"
    )
    print("=" * 72)

    out_dir = pathlib.Path(__file__).parent / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    wait_for_connect()

    results = []
    for case in CASES:
        try:
            r = run_case(case)
            results.append(r)
        except Exception as e:
            import traceback

            print(f"\nERROR in {case['name']}: {e}")
            traceback.print_exc()
            results.append(
                {
                    "connector": case["name"],
                    "short_label": case["short_label"],
                    "label": case["label"],
                    "error": str(e),
                }
            )

    # Save raw results
    output = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "config": {
            "kafka_bootstrap": KAFKA_BOOTSTRAP,
            "expected_rows": EXPECTED_ROWS,
            "sample_interval_sec": SAMPLE_INTERVAL_SEC,
            "sys_sample_sec": SYS_SAMPLE_SEC,
            "monitored_containers": MONITORED_CONTAINERS,
        },
        "results": results,
    }

    results_path = pathlib.Path(__file__).parent / "migration_results.json"
    results_path.write_text(json.dumps(output, indent=2))
    print(f"\n  Results saved: {results_path}")

    # Console summary
    good = [r for r in results if "error" not in r]
    if good:
        print(
            f"\n  {'Case':<18} {'Time(s)':>8} {'msg/s':>10} {'MB/s':>8} {'TotalMB':>9} {'AvgSize':>9}"
        )
        print("  " + "-" * 68)
        for r in good:
            print(
                f"  {r['short_label']:<18} "
                f"{r['total_time_sec']:>8.1f} "
                f"{r['throughput_msg_sec']:>10,.0f} "
                f"{r['throughput_mb_sec']:>8.2f} "
                f"{r['total_data_mb']:>9.1f} "
                f"{r['avg_msg_size_bytes']:>8.0f}B"
            )

    # Launch visualizer
    print("\n  Generating observability charts...")
    import subprocess

    vis = str(pathlib.Path(__file__).parent / "visualize.py")
    subprocess.run([sys.executable, vis, str(results_path)], check=True)


if __name__ == "__main__":
    main()
