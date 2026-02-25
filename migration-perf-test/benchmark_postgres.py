#!/usr/bin/env python3
"""
benchmark_postgres.py
=====================
Mirrors benchmark.py but targets the Debezium PostgreSQL connector.
5 cases with escalating tuning, 2,000,000 rows, Avro serialization.

Key PostgreSQL differences vs SQL Server:
  - Uses pgoutput logical replication plugin (no CDC agent needed)
  - Each connector gets its own replication slot (slot.name per case)
  - Snapshot uses a transaction snapshot — no TempDB, no shared locks
  - tasks.max > 1 on a single table also doesn't parallelize snapshot
    (PostgreSQL connector is also single-threaded per slot)

Cases
-----
  C1 — Baseline    : tasks=1, batch=2048,  queue=8192,  poll=1000ms
  C2 — LargeBatch  : tasks=1, batch=8192,  queue=32768, poll=500ms
  C3 — MultiThread : tasks=4, batch=2048,  queue=8192,  poll=500ms
  C4 — FullyTuned  : tasks=4, batch=8192,  queue=32768, poll=200ms
  C5 — UltraTuned  : tasks=4, batch=16384, queue=65536, poll=100ms
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
from confluent_kafka.admin import AdminClient

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from system_collector import SystemCollector

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
CONNECT_URL = os.getenv("CONNECT_URL", "http://localhost:8083")
EXPECTED_ROWS = int(os.getenv("EXPECTED_ROWS", "2000000"))
IDLE_TIMEOUT_SEC = int(os.getenv("IDLE_TIMEOUT_SEC", "90"))
SAMPLE_INTERVAL = float(os.getenv("SAMPLE_INTERVAL", "2"))
SYS_SAMPLE_SEC = float(os.getenv("SYS_SAMPLE_SEC", "1"))
CONNECT_POLL_SEC = float(os.getenv("CONNECT_POLL_SEC", "5"))

BASE_DIR = pathlib.Path(__file__).parent
CONNECTOR_DIR = BASE_DIR / "connect" / "postgres"
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

MONITORED_CONTAINERS = [
    "kafka-connect",
    "kafka",
    "kafka-broker-2",
    "postgres",
    "schema-registry",
]

CASES = [
    {
        "name": "pg-perf-case1-baseline",
        "short_label": "PG-C1-Baseline",
        "label": "PG Case 1 — Baseline\n(tasks=1, batch=2048, queue=8192, poll=1000ms)",
        "config_file": CONNECTOR_DIR / "case1-baseline.json",
        "tasks_max": 1,
        "batch_size": 2048,
        "queue_size": 8192,
        "poll_ms": 1000,
        "topic_prefix": "pg_perf_c1",
        "slot_name": "debezium_c1",
    },
    {
        "name": "pg-perf-case2-largebatch",
        "short_label": "PG-C2-LargeBatch",
        "label": "PG Case 2 — Large Batches\n(tasks=1, batch=8192, queue=32768, poll=500ms)",
        "config_file": CONNECTOR_DIR / "case2-largebatch.json",
        "tasks_max": 1,
        "batch_size": 8192,
        "queue_size": 32768,
        "poll_ms": 500,
        "topic_prefix": "pg_perf_c2",
        "slot_name": "debezium_c2",
    },
    {
        "name": "pg-perf-case3-multithread",
        "short_label": "PG-C3-MultiThread",
        "label": "PG Case 3 — Multi-Thread\n(tasks=4, batch=2048, queue=8192, poll=500ms)",
        "config_file": CONNECTOR_DIR / "case3-multithread.json",
        "tasks_max": 4,
        "batch_size": 2048,
        "queue_size": 8192,
        "poll_ms": 500,
        "topic_prefix": "pg_perf_c3",
        "slot_name": "debezium_c3",
    },
    {
        "name": "pg-perf-case4-fullytuned",
        "short_label": "PG-C4-FullyTuned",
        "label": "PG Case 4 — Fully Tuned\n(tasks=4, batch=8192, queue=32768, poll=200ms)",
        "config_file": CONNECTOR_DIR / "case4-fullytuned.json",
        "tasks_max": 4,
        "batch_size": 8192,
        "queue_size": 32768,
        "poll_ms": 200,
        "topic_prefix": "pg_perf_c4",
        "slot_name": "debezium_c4",
    },
    {
        "name": "pg-perf-case5-ultratuned",
        "short_label": "PG-C5-UltraTuned",
        "label": "PG Case 5 — Ultra Tuned\n(tasks=4, batch=16384, queue=65536, poll=100ms)",
        "config_file": CONNECTOR_DIR / "case5-ultratuned.json",
        "tasks_max": 4,
        "batch_size": 16384,
        "queue_size": 65536,
        "poll_ms": 100,
        "topic_prefix": "pg_perf_c5",
        "slot_name": "debezium_c5",
    },
]

# ---------------------------------------------------------------------------
# Connect REST helpers (identical to benchmark.py)
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
    raise RuntimeError("Kafka Connect not ready after timeout.")


def delete_connector(name):
    try:
        connect_delete(f"/connectors/{name}")
        print(f"  Deleted connector: {name}")
        time.sleep(2)
    except Exception:
        pass


def deploy_connector(config_file):
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
                print(f"  {name} RUNNING ({len(tasks)} task(s)).")
                return
            if cstate == "FAILED":
                raise RuntimeError(
                    f"Connector {name} FAILED:\n{json.dumps(status, indent=2)}"
                )
            states = [t["state"] for t in tasks]
            print(f"    connector={cstate}  tasks={states}")
        except RuntimeError:
            raise
        except Exception as e:
            print(f"    status error: {e}")
        time.sleep(5)
    raise RuntimeError(f"{name} not RUNNING within {timeout}s.")


def fetch_connector_metrics(name):
    out = {
        "connector_state": "unknown",
        "tasks_running": 0,
        "tasks_failed": 0,
        "tasks_total": 0,
    }
    try:
        status = connect_get(f"/connectors/{name}/status")
        out["connector_state"] = status["connector"]["state"]
        tasks = status.get("tasks", [])
        out["tasks_total"] = len(tasks)
        out["tasks_running"] = sum(1 for t in tasks if t["state"] == "RUNNING")
        out["tasks_failed"] = sum(1 for t in tasks if t["state"] == "FAILED")
    except Exception:
        pass
    return out


def get_consumer_lag(group_id, topic):
    try:
        from confluent_kafka import TopicPartition

        c = Consumer(
            {
                "bootstrap.servers": KAFKA_BOOTSTRAP,
                "group.id": group_id,
                "enable.auto.commit": False,
            }
        )
        cluster_meta = c.list_topics(topic=topic, timeout=5)
        if topic not in cluster_meta.topics:
            c.close()
            return -1
        tps = [
            TopicPartition(topic, p)
            for p in cluster_meta.topics[topic].partitions.keys()
        ]
        committed_tps = c.committed(tps, timeout=5)
        committed_map = {
            tp.partition: tp.offset for tp in committed_tps if tp.offset >= 0
        }
        total_lag = 0
        for tp in tps:
            try:
                _lo, hi = c.get_watermark_offsets(tp, timeout=3)
                total_lag += max(0, hi - committed_map.get(tp.partition, 0))
            except Exception:
                pass
        c.close()
        return total_lag
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# ConnectMetricsCollector
# ---------------------------------------------------------------------------


class ConnectMetricsCollector:
    def __init__(self, connector_name, group_id, topic, interval=5.0):
        self.connector_name = connector_name
        self.group_id = group_id
        self.topic = topic
        self.interval = interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._data = []
        self._start_ts = None

    def start(self):
        self._start_ts = time.time()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ConnectMetrics"
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.interval * 3)

    def get_data(self):
        with self._lock:
            return list(self._data)

    def _run(self):
        while not self._stop_event.is_set():
            t0 = time.time()
            sample = {"elapsed": t0 - (self._start_ts or t0), "ts": t0}
            sample.update(fetch_connector_metrics(self.connector_name))
            sample["consumer_lag"] = get_consumer_lag(self.group_id, self.topic)
            with self._lock:
                self._data.append(sample)
            self._stop_event.wait(max(0, self.interval - (time.time() - t0)))


# ---------------------------------------------------------------------------
# Topic cleanup
# ---------------------------------------------------------------------------


def delete_topics_for_prefix(prefix):
    try:
        admin = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})
        meta = admin.list_topics(timeout=10)
        to_delete = [t for t in meta.topics if t.startswith(prefix)]
        if not to_delete:
            return
        print(f"  Deleting {len(to_delete)} old topic(s) for prefix '{prefix}'...")
        fs = admin.delete_topics(to_delete, operation_timeout=30)
        for topic, fut in fs.items():
            try:
                fut.result()
                print(f"    Deleted: {topic}")
            except Exception as e:
                print(f"    Could not delete {topic}: {e}")
        time.sleep(3)
    except Exception as e:
        print(f"  Warning: topic cleanup error: {e}")


# ---------------------------------------------------------------------------
# Run one case
# ---------------------------------------------------------------------------


def run_case(case):
    name = case["name"]
    topic_prefix = case["topic_prefix"]
    topic = f"{topic_prefix}.public.sales_order_detail_big"
    group_id = f"bench-{name}-{int(time.time())}"

    print(f"\n{'=' * 72}")
    print(f"  {case['short_label']}")
    print(
        f"  tasks={case['tasks_max']}  batch={case['batch_size']}  queue={case['queue_size']}  poll={case['poll_ms']}ms"
    )
    print(f"{'=' * 72}")

    delete_connector(name)
    delete_topics_for_prefix(topic_prefix)
    time.sleep(2)  # allow slot drop to propagate if connector held it

    deploy_connector(case["config_file"])
    wait_for_running(name)

    sys_collector = SystemCollector(
        containers=MONITORED_CONTAINERS, interval=SYS_SAMPLE_SEC
    )
    sys_collector.start()

    connect_collector = ConnectMetricsCollector(
        connector_name=name, group_id=group_id, topic=topic, interval=CONNECT_POLL_SEC
    )
    connect_collector.start()

    # Wait for topic to appear
    print(f"  Waiting for topic '{topic}' to appear...")
    admin_probe = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})
    topic_deadline = time.time() + 300
    while time.time() < topic_deadline:
        meta = admin_probe.list_topics(timeout=5)
        if topic in meta.topics:
            print(f"  Topic found: {topic}")
            break
        time.sleep(3)
    else:
        raise RuntimeError(f"Topic '{topic}' never appeared within 300s")

    # Consumer
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "fetch.max.bytes": str(50 * 1024 * 1024),
            "max.partition.fetch.bytes": str(50 * 1024 * 1024),
            "fetch.wait.max.ms": "500",
        }
    )
    consumer.subscribe([topic])

    msg_count = 0
    total_bytes = 0
    start_wall = time.time()
    last_sample = start_wall
    idle_since = start_wall

    ts_elapsed, ts_msg_rate, ts_mb_rate, ts_cumul = [], [], [], []
    ts_avg_size, ts_lat_p50, ts_lat_p99 = [], [], []
    win_msgs, win_bytes, win_lats = 0, 0, []

    print(
        f"  Consuming {EXPECTED_ROWS:,} messages (idle_timeout={IDLE_TIMEOUT_SEC}s) ..."
    )
    print(
        f"  {'Count':>10}  {'%':>5}  {'Elapsed':>8}  {'msg/s':>10}  {'MB/s':>7}  {'P50ms':>7}  {'AvgB':>7}"
    )
    print("  " + "-" * 68)

    while True:
        msg = consumer.poll(timeout=1.0)
        now = time.time()

        if msg is None:
            if now - idle_since > IDLE_TIMEOUT_SEC:
                print(f"\n  Idle timeout — {msg_count:,} messages consumed.")
                break
        elif msg.error():
            err = msg.error()
            if err and err.code() not in (-191, -168, 3):
                raise KafkaException(err)
        else:
            idle_since = now
            sz = len(msg.key() or b"") + len(msg.value() or b"")
            msg_count += 1
            total_bytes += sz
            win_msgs += 1
            win_bytes += sz
            kafka_ts = msg.timestamp()[1]
            if kafka_ts and kafka_ts > 0:
                lat = now * 1000 - kafka_ts
                if lat >= 0:
                    win_lats.append(lat)

        if now - last_sample >= SAMPLE_INTERVAL:
            dt = max(now - last_sample, 0.001)
            elapsed = now - start_wall
            ts_elapsed.append(round(elapsed, 2))
            ts_msg_rate.append(round(win_msgs / dt, 1))
            ts_mb_rate.append(round((win_bytes / 1_048_576) / dt, 3))
            ts_cumul.append(msg_count)
            ts_avg_size.append(round(win_bytes / win_msgs, 1) if win_msgs else 0.0)
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
                f"  {msg_count:>10,}  {pct:>5.1f}%  {elapsed:>7.1f}s  "
                f"{ts_msg_rate[-1]:>10,.0f}  {ts_mb_rate[-1]:>7.2f}  "
                f"{ts_lat_p50[-1]:>7.0f}  {ts_avg_size[-1]:>7.0f}"
            )
            win_msgs, win_bytes, win_lats = 0, 0, []
            last_sample = now

        if msg_count >= EXPECTED_ROWS:
            print(f"\n  Reached {EXPECTED_ROWS:,} — done.")
            break

    end_wall = time.time()
    consumer.close()
    sys_collector.stop()
    connect_collector.stop()

    total_sec = end_wall - start_wall
    avg_msg_sec = msg_count / total_sec if total_sec > 0 else 0
    avg_mb_sec = (total_bytes / 1_048_576) / total_sec if total_sec > 0 else 0
    total_mb = total_bytes / 1_048_576
    avg_size = total_bytes / msg_count if msg_count > 0 else 0

    connect_samples = connect_collector.get_data()
    peak_lag = max(
        (
            s.get("consumer_lag", 0)
            for s in connect_samples
            if s.get("consumer_lag", -1) >= 0
        ),
        default=0,
    )
    min_lag = min(
        (
            s.get("consumer_lag", 0)
            for s in connect_samples
            if s.get("consumer_lag", -1) >= 0
        ),
        default=0,
    )

    print(
        f"\n  Result: {msg_count:,} msgs | {total_sec:.1f}s | {avg_msg_sec:,.0f} msg/s | {total_mb:.1f} MB | {avg_size:.0f} B/msg"
    )
    print(f"         Peak consumer lag: {peak_lag:,} msgs | Min lag: {min_lag:,} msgs")

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
        "peak_consumer_lag": peak_lag,
        "min_consumer_lag": min_lag,
        "timeseries": {
            "elapsed_sec": ts_elapsed,
            "msg_rate": ts_msg_rate,
            "mb_rate": ts_mb_rate,
            "cumulative_msgs": ts_cumul,
            "avg_size_bytes": ts_avg_size,
            "latency_p50_ms": ts_lat_p50,
            "latency_p99_ms": ts_lat_p99,
        },
        "connect_metrics": connect_samples,
        "system": sys_collector.get_data(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 72)
    print("  Debezium Avro Migration Benchmark — PostgreSQL Edition")
    print(f"  5 Cases | {EXPECTED_ROWS:,} rows | System: {MONITORED_CONTAINERS}")
    print(f"  Kafka: {KAFKA_BOOTSTRAP}  |  Connect: {CONNECT_URL}")
    print("=" * 72)

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

    output = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "database": "postgresql",
        "config": {
            "kafka_bootstrap": KAFKA_BOOTSTRAP,
            "expected_rows": EXPECTED_ROWS,
            "sample_interval_sec": SAMPLE_INTERVAL,
            "sys_sample_sec": SYS_SAMPLE_SEC,
            "connect_poll_sec": CONNECT_POLL_SEC,
            "monitored_containers": MONITORED_CONTAINERS,
        },
        "results": results,
    }

    ts_tag = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"pg_results_{ts_tag}.json"
    latest = RESULTS_DIR / "pg_results_latest.json"
    out_path.write_text(json.dumps(output, indent=2))
    latest.write_text(json.dumps(output, indent=2))
    print(f"\n  Results saved: {out_path}")

    good = [r for r in results if "error" not in r]
    if good:
        print(
            f"\n  {'Case':<20} {'Tasks':>5} {'Batch':>6} {'Time(s)':>8} {'msg/s':>10} {'MB/s':>7} {'AvgB':>7} {'PeakLag':>10}"
        )
        print("  " + "-" * 80)
        for r in good:
            print(
                f"  {r['short_label']:<20} {r['tasks_max']:>5} {r['batch_size']:>6} "
                f"{r['total_time_sec']:>8.1f} {r['throughput_msg_sec']:>10,.0f} "
                f"{r['throughput_mb_sec']:>7.2f} {r['avg_msg_size_bytes']:>6.0f}B "
                f"{r.get('peak_consumer_lag', 0):>10,}"
            )

    # Generate charts using the same visualize.py
    print("\n  Generating charts...")
    import subprocess

    vis = str(BASE_DIR / "visualize.py")
    subprocess.run([sys.executable, vis, str(latest)], check=False)


if __name__ == "__main__":
    main()
