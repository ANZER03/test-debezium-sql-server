#!/usr/bin/env python3
"""
system_collector.py
===================
Background thread that polls the Docker daemon REST API every INTERVAL seconds
and collects per-container system metrics:

  - CPU %        (calculated from delta of cpu_usage vs system_cpu_usage)
  - Memory MB    (RSS: usage minus page cache)
  - Net RX MB/s  (receive throughput since last sample)
  - Net TX MB/s  (transmit throughput since last sample)
  - Disk Read MB/s
  - Disk Write MB/s

Usage:
    from system_collector import SystemCollector

    collector = SystemCollector(
        containers=["kafka-connect", "kafka", "sqlserver", "schema-registry"],
        interval=1.0,
    )
    collector.start()
    # ... run your workload ...
    collector.stop()
    data = collector.get_data()   # dict: container -> list of sample dicts
"""

import threading
import time
import json
import http.client
import socket


# ---------------------------------------------------------------------------
# Docker Unix-socket HTTP client (no extra deps)
# ---------------------------------------------------------------------------


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that talks over a Unix domain socket."""

    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._socket_path)
        self.sock = s


def _docker_get(path: str, socket_path="/var/run/docker.sock") -> dict:
    conn = _UnixSocketHTTPConnection(socket_path)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status != 200:
        raise RuntimeError(f"Docker API {path} => HTTP {resp.status}: {body[:200]}")
    return json.loads(body)


# ---------------------------------------------------------------------------
# Per-container stats parser
# ---------------------------------------------------------------------------


def _parse_stats(raw: dict, prev_raw: dict | None) -> dict:
    """Parse a Docker container stats snapshot into scalar metrics."""
    result = {}

    # ---- CPU %
    try:
        cpu = raw["cpu_stats"]["cpu_usage"]["total_usage"]
        sys_cpu = raw["cpu_stats"]["system_cpu_usage"]
        n_cpus = raw["cpu_stats"].get("online_cpus", 1)
        if prev_raw:
            prev_cpu = prev_raw["cpu_stats"]["cpu_usage"]["total_usage"]
            prev_sys_cpu = prev_raw["cpu_stats"]["system_cpu_usage"]
            delta_cpu = cpu - prev_cpu
            delta_sys = sys_cpu - prev_sys_cpu
            result["cpu_pct"] = (
                (delta_cpu / delta_sys) * n_cpus * 100 if delta_sys > 0 else 0.0
            )
        else:
            result["cpu_pct"] = 0.0
    except (KeyError, ZeroDivisionError):
        result["cpu_pct"] = 0.0

    # ---- Memory (RSS = usage - cache)
    try:
        mem_usage = raw["memory_stats"]["usage"]
        mem_limit = raw["memory_stats"]["limit"]
        cache = raw["memory_stats"].get("stats", {}).get("cache", 0)
        rss = mem_usage - cache
        result["mem_mb"] = rss / (1024 * 1024)
        result["mem_limit_mb"] = mem_limit / (1024 * 1024)
        result["mem_pct"] = (rss / mem_limit * 100) if mem_limit > 0 else 0.0
    except KeyError:
        result["mem_mb"] = result["mem_limit_mb"] = result["mem_pct"] = 0.0

    # ---- Network (delta bytes → MB/s)
    try:
        nets = raw.get("networks", {})
        rx_bytes = sum(v["rx_bytes"] for v in nets.values())
        tx_bytes = sum(v["tx_bytes"] for v in nets.values())
        result["net_rx_total_mb"] = rx_bytes / (1024 * 1024)
        result["net_tx_total_mb"] = tx_bytes / (1024 * 1024)
        if prev_raw:
            prev_nets = prev_raw.get("networks", {})
            prev_rx = sum(v["rx_bytes"] for v in prev_nets.values())
            prev_tx = sum(v["tx_bytes"] for v in prev_nets.values())
            dt = raw["_sample_ts"] - prev_raw.get("_sample_ts", raw["_sample_ts"] - 1)
            dt = max(dt, 0.001)
            result["net_rx_mbps"] = (rx_bytes - prev_rx) / (1024 * 1024) / dt
            result["net_tx_mbps"] = (tx_bytes - prev_tx) / (1024 * 1024) / dt
        else:
            result["net_rx_mbps"] = result["net_tx_mbps"] = 0.0
    except (KeyError, ZeroDivisionError):
        result["net_rx_mbps"] = result["net_tx_mbps"] = 0.0
        result["net_rx_total_mb"] = result["net_tx_total_mb"] = 0.0

    # ---- Disk I/O (delta bytes → MB/s)
    try:
        blk = raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
        rd = sum(e["value"] for e in blk if e.get("op", "").lower() == "read")
        wr = sum(e["value"] for e in blk if e.get("op", "").lower() == "write")
        result["disk_read_total_mb"] = rd / (1024 * 1024)
        result["disk_write_total_mb"] = wr / (1024 * 1024)
        if prev_raw:
            prev_blk = (
                prev_raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
            )
            prev_rd = sum(
                e["value"] for e in prev_blk if e.get("op", "").lower() == "read"
            )
            prev_wr = sum(
                e["value"] for e in prev_blk if e.get("op", "").lower() == "write"
            )
            dt = raw["_sample_ts"] - prev_raw.get("_sample_ts", raw["_sample_ts"] - 1)
            dt = max(dt, 0.001)
            result["disk_read_mbps"] = (rd - prev_rd) / (1024 * 1024) / dt
            result["disk_write_mbps"] = (wr - prev_wr) / (1024 * 1024) / dt
        else:
            result["disk_read_mbps"] = result["disk_write_mbps"] = 0.0
    except (KeyError, ZeroDivisionError):
        result["disk_read_mbps"] = result["disk_write_mbps"] = 0.0
        result["disk_read_total_mb"] = result["disk_write_total_mb"] = 0.0

    return result


# ---------------------------------------------------------------------------
# SystemCollector
# ---------------------------------------------------------------------------


class SystemCollector:
    """
    Background thread that samples Docker container stats at `interval` seconds.

    Parameters
    ----------
    containers : list[str]
        Docker container names to monitor.
    interval : float
        Sampling interval in seconds (default 1.0).
    socket_path : str
        Path to Docker Unix socket.
    """

    def __init__(
        self,
        containers: list,
        interval: float = 1.0,
        socket_path: str = "/var/run/docker.sock",
    ):
        self.containers = containers
        self.interval = interval
        self.socket_path = socket_path

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        self._data: dict[str, list] = {c: [] for c in containers}
        self._prev: dict[str, dict] = {}
        self._start_ts: float | None = None

    def start(self):
        self._start_ts = time.time()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="SystemCollector"
        )
        self._thread.start()
        print(
            f"  [SystemCollector] Started — sampling {self.containers} every {self.interval}s"
        )

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.interval * 3)
        total = sum(len(v) for v in self._data.values())
        print(f"  [SystemCollector] Stopped — {total} total samples")

    def get_data(self) -> dict:
        with self._lock:
            return {c: list(v) for c, v in self._data.items()}

    def _run(self):
        while not self._stop_event.is_set():
            t0 = time.time()
            for container in self.containers:
                try:
                    raw = _docker_get(
                        f"/containers/{container}/stats?stream=false",
                        self.socket_path,
                    )
                    raw["_sample_ts"] = t0
                    prev = self._prev.get(container)
                    metrics = _parse_stats(raw, prev)
                    metrics["ts"] = t0
                    metrics["elapsed"] = t0 - self._start_ts if self._start_ts else 0.0
                    self._prev[container] = raw
                    with self._lock:
                        self._data[container].append(metrics)
                except Exception:
                    pass

            elapsed = time.time() - t0
            self._stop_event.wait(max(0, self.interval - elapsed))


# ---------------------------------------------------------------------------
# Stand-alone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    containers = sys.argv[1:] or ["kafka-connect", "kafka", "sqlserver"]
    c = SystemCollector(containers, interval=1.0)
    c.start()
    print("Sampling for 10 seconds...")
    time.sleep(10)
    c.stop()
    data = c.get_data()
    for cname, samples in data.items():
        if not samples:
            print(f"  {cname}: no samples")
            continue
        last = samples[-1]
        print(
            f"  {cname}: cpu={last['cpu_pct']:.1f}% "
            f"mem={last['mem_mb']:.0f}MB ({last['mem_pct']:.1f}%) "
            f"net_rx={last['net_rx_mbps']:.2f}MB/s "
            f"net_tx={last['net_tx_mbps']:.2f}MB/s "
            f"disk_r={last['disk_read_mbps']:.2f}MB/s "
            f"disk_w={last['disk_write_mbps']:.2f}MB/s"
        )
