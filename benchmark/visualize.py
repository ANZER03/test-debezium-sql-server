#!/usr/bin/env python3
"""
visualize.py
============
Reads migration_results.json and produces a full observability dashboard:

KAFKA / CONSUMER METRICS (per case, time-series)
  01  throughput_msg_rate.png     — msg/s over time, all 4 cases
  02  throughput_mb_rate.png      — MB/s over time, all 4 cases
  03  cumulative_messages.png     — progress curve, all 4 cases
  04  latency_p50.png             — end-to-end latency P50 (ms)
  05  latency_p99.png             — end-to-end latency P99 (ms)
  06  avg_message_size.png        — bytes/msg stability over time

SUMMARY BARS
  07  bar_total_time.png          — total migration time per case
  08  bar_avg_throughput.png      — average msg/s per case
  09  bar_total_data.png          — total MB transferred per case

SYSTEM METRICS — kafka-connect (per case, time-series)
  10  sys_connect_cpu.png         — CPU % of kafka-connect container
  11  sys_connect_mem.png         — RAM MB of kafka-connect container
  12  sys_connect_net_rx.png      — Network RX MB/s (kafka-connect)
  13  sys_connect_net_tx.png      — Network TX MB/s (kafka-connect)
  14  sys_connect_disk_r.png      — Disk Read MB/s (kafka-connect)
  15  sys_connect_disk_w.png      — Disk Write MB/s (kafka-connect)

SYSTEM METRICS — kafka broker (per case)
  16  sys_kafka_cpu.png
  17  sys_kafka_mem.png
  18  sys_kafka_net_tx.png        — broker TX (= data written to consumers)

SYSTEM METRICS — sqlserver (per case)
  19  sys_sql_cpu.png
  20  sys_sql_mem.png
  21  sys_sql_disk_r.png          — SQL Server disk reads

COMPARATIVE HEATMAPS
  22  heatmap_kafka.png           — Kafka metrics heatmap
  23  heatmap_system.png          — System avg metrics heatmap (all containers)
  24  radar_chart.png             — Radar / spider chart

INTERACTIVE HTML
  25  dashboard.html              — Full Plotly dashboard (all panels, zoomable)
"""

import json
import sys
import pathlib
import math
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.gridspec as gridspec
import numpy as np

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
CASE_COLOURS = ["#4C72B0", "#DD8452", "#55A868", "#C44E52"]
CASE_MARKERS = ["o", "s", "^", "D"]

CONTAINER_COLOURS = {
    "kafka-connect": "#4C72B0",
    "kafka": "#DD8452",
    "kafka-broker-2": "#55A868",
    "sqlserver": "#C44E52",
    "schema-registry": "#9467BD",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load(path: pathlib.Path):
    data = json.loads(path.read_text())
    return [r for r in data["results"] if "error" not in r]


def smooth(arr, w=5):
    if len(arr) < w:
        return arr
    out = []
    for i in range(len(arr)):
        lo, hi = max(0, i - w // 2), min(len(arr), i + w // 2 + 1)
        out.append(sum(arr[lo:hi]) / (hi - lo))
    return out


def save_fig(fig, name, out_dir):
    p = out_dir / name
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  {name}")


def short(r):
    return r.get("short_label", r["connector"])


def sys_series(r, container, key, w=3):
    """Extract a smoothed time-series for a system metric from a case result."""
    samples = r.get("system", {}).get(container, [])
    if not samples:
        return [], []
    x = [s["elapsed"] for s in samples]
    y = smooth([s.get(key, 0) for s in samples], w)
    return x, y


def avg_sys(r, container, key):
    samples = r.get("system", {}).get(container, [])
    vals = [s.get(key, 0) for s in samples if s.get(key, 0) > 0]
    return statistics.mean(vals) if vals else 0.0


def peak_sys(r, container, key):
    samples = r.get("system", {}).get(container, [])
    vals = [s.get(key, 0) for s in samples]
    return max(vals) if vals else 0.0


# ---------------------------------------------------------------------------
# Section A  — Kafka / consumer time-series
# ---------------------------------------------------------------------------


def kafka_ts_chart(results, out_dir, ts_key, title, ylabel, fname, w=5):
    fig, ax = plt.subplots(figsize=(11, 4.5))
    for i, r in enumerate(results):
        ts = r["timeseries"]
        x = ts["elapsed_sec"]
        y = smooth(ts[ts_key], w)
        ax.plot(
            x,
            y,
            color=CASE_COLOURS[i],
            marker=CASE_MARKERS[i],
            markersize=3,
            linewidth=1.8,
            markevery=max(1, len(x) // 25),
            label=short(r),
        )
    ax.set_title(title, fontsize=13, fontweight="bold", pad=8)
    ax.set_xlabel("Elapsed time (s)", fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{v:,.0f}" if v >= 10 else f"{v:.1f}")
    )
    fig.tight_layout()
    save_fig(fig, fname, out_dir)


def cumulative_chart(results, out_dir):
    fig, ax = plt.subplots(figsize=(11, 4.5))
    for i, r in enumerate(results):
        ts = r["timeseries"]
        x = ts["elapsed_sec"]
        y = [v / 1_000_000 for v in ts["cumulative_msgs"]]
        lbl = f"{short(r)}  — {r['total_time_sec']:.1f}s"
        ax.plot(x, y, color=CASE_COLOURS[i], linewidth=2.2, label=lbl)
    ax.axhline(
        2.0, color="#555", linestyle=":", linewidth=1.2, label="Target — 2M rows"
    )
    ax.set_title(
        "Cumulative Messages Consumed Over Time", fontsize=13, fontweight="bold", pad=8
    )
    ax.set_xlabel("Elapsed time (s)", fontsize=11)
    ax.set_ylabel("Millions of messages", fontsize=11)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, linestyle="--")
    fig.tight_layout()
    save_fig(fig, "03_cumulative_messages.png", out_dir)


# ---------------------------------------------------------------------------
# Section B  — Summary bars
# ---------------------------------------------------------------------------


def bar_chart(
    results,
    out_dir,
    key,
    title,
    ylabel,
    fname,
    fmt="{:.1f}",
    lower_is_better=False,
    unit="",
):
    labels = [short(r) for r in results]
    values = [r[key] for r in results]
    best = min(values) if lower_is_better else max(values)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    bars = ax.bar(
        labels,
        values,
        color=CASE_COLOURS[: len(results)],
        edgecolor="white",
        linewidth=0.8,
        width=0.52,
    )
    for bar, v in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + max(values) * 0.01,
            fmt.format(v) + unit,
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )
        if v == best:
            bar.set_edgecolor("#FFD700")
            bar.set_linewidth(2.5)

    ax.set_title(title, fontsize=13, fontweight="bold", pad=8)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.set_ylim(0, max(values) * 1.18)
    ax.grid(True, axis="y", alpha=0.3, linestyle="--")
    fig.tight_layout()
    save_fig(fig, fname, out_dir)


# ---------------------------------------------------------------------------
# Section C  — System metrics time-series per container
# ---------------------------------------------------------------------------


def sys_ts_chart(results, out_dir, container, metric_key, title, ylabel, fname, w=3):
    fig, ax = plt.subplots(figsize=(11, 4.5))
    has_data = False
    for i, r in enumerate(results):
        x, y = sys_series(r, container, metric_key, w)
        if not x:
            continue
        has_data = True
        ax.plot(
            x,
            y,
            color=CASE_COLOURS[i],
            marker=CASE_MARKERS[i],
            markersize=2,
            linewidth=1.6,
            markevery=max(1, len(x) // 25),
            label=short(r),
        )
    if not has_data:
        plt.close(fig)
        return
    ax.set_title(title, fontsize=13, fontweight="bold", pad=8)
    ax.set_xlabel("Elapsed time (s)", fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.1f}"))
    fig.tight_layout()
    save_fig(fig, fname, out_dir)


# ---------------------------------------------------------------------------
# Section C2  — Multi-container system panel (all containers, one case each)
# ---------------------------------------------------------------------------


def multi_container_sys_panel(results, out_dir, metric_key, title, ylabel, fname, w=3):
    """
    Grid: rows = cases, cols = containers.
    Shows how EVERY container behaves for this metric across all 4 cases.
    """
    containers = [
        "kafka-connect",
        "kafka",
        "kafka-broker-2",
        "sqlserver",
        "schema-registry",
    ]
    n_rows = len(results)
    n_cols = len(containers)

    fig, axes = plt.subplots(
        n_rows, n_cols, figsize=(4 * n_cols, 3 * n_rows), sharex=False, sharey=False
    )
    if n_rows == 1:
        axes = [axes]

    fig.suptitle(title, fontsize=14, fontweight="bold", y=1.01)

    for row_i, r in enumerate(results):
        for col_j, cname in enumerate(containers):
            ax = axes[row_i][col_j]
            x, y = sys_series(r, cname, metric_key, w)
            colour = CONTAINER_COLOURS.get(cname, "#888")
            if x:
                ax.plot(x, y, color=colour, linewidth=1.4)
                ax.fill_between(x, y, alpha=0.15, color=colour)
            if row_i == 0:
                ax.set_title(cname, fontsize=9, fontweight="bold", color=colour)
            if col_j == 0:
                ax.set_ylabel(f"{short(r)}\n{ylabel}", fontsize=8)
            ax.grid(True, alpha=0.25, linestyle="--")
            ax.tick_params(labelsize=7)

    fig.tight_layout()
    save_fig(fig, fname, out_dir)


# ---------------------------------------------------------------------------
# Section D  — Heatmaps
# ---------------------------------------------------------------------------


def heatmap_kafka(results, out_dir):
    metrics = [
        ("total_time_sec", "Time (s)", True),
        ("throughput_msg_sec", "Throughput msg/s", False),
        ("throughput_mb_sec", "Throughput MB/s", False),
        ("total_data_mb", "Data (MB)", True),
        ("avg_msg_size_bytes", "Avg Size (B)", True),
    ]
    labels = [short(r) for r in results]
    metric_labels = [m[1] for m in metrics]

    matrix = np.zeros((len(results), len(metrics)))
    raw = np.zeros((len(results), len(metrics)))
    for j, (key, _, lb) in enumerate(metrics):
        col = [r[key] for r in results]
        raw[:, j] = col
        mn, mx = min(col), max(col)
        if mx > mn:
            norm = [(v - mn) / (mx - mn) for v in col]
            matrix[:, j] = [1 - v if lb else v for v in norm]
        else:
            matrix[:, j] = 0.5

    fmts = {
        "total_time_sec": "{:.1f}s",
        "throughput_msg_sec": "{:,.0f}",
        "throughput_mb_sec": "{:.2f}",
        "total_data_mb": "{:.1f}",
        "avg_msg_size_bytes": "{:.0f}B",
    }

    fig, ax = plt.subplots(figsize=(10, 4))
    im = ax.imshow(matrix.T, cmap="RdYlGn", aspect="auto", vmin=0, vmax=1)
    ax.set_xticks(range(len(results)))
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_yticks(range(len(metrics)))
    ax.set_yticklabels(metric_labels, fontsize=10)
    for i in range(len(results)):
        for j, (key, _, _) in enumerate(metrics):
            txt = fmts[key].format(raw[i, j])
            score = matrix[i, j]
            fc = "white" if score < 0.35 or score > 0.75 else "black"
            ax.text(
                i,
                j,
                txt,
                ha="center",
                va="center",
                fontsize=9,
                fontweight="bold",
                color=fc,
            )
    ax.set_title(
        "Kafka Metrics Heatmap — Green=Best, Red=Worst",
        fontsize=12,
        fontweight="bold",
        pad=10,
    )
    plt.colorbar(im, ax=ax, fraction=0.03, pad=0.02, label="Relative score")
    fig.tight_layout()
    save_fig(fig, "22_heatmap_kafka.png", out_dir)


def heatmap_system(results, out_dir):
    """Heatmap of average system metrics across cases × containers."""
    containers = ["kafka-connect", "kafka", "sqlserver"]
    sys_metrics = [
        ("cpu_pct", "CPU %", False),  # higher CPU not necessarily better
        ("mem_mb", "RAM MB", True),  # lower = better
        ("net_rx_mbps", "Net RX MB/s", False),
        ("net_tx_mbps", "Net TX MB/s", False),
        ("disk_write_mbps", "Disk W MB/s", False),
    ]

    row_labels = []
    col_labels = [f"{c}\n{mk}" for c in containers for mk, _, _ in sys_metrics]
    matrix = []
    raw_vals = []

    for r in results:
        row = []
        row_raw = []
        for c in containers:
            for mk, _, _ in sys_metrics:
                v = avg_sys(r, c, mk)
                row.append(v)
                row_raw.append(v)
        matrix.append(row)
        raw_vals.append(row_raw)
        row_labels.append(short(r))

    matrix = np.array(matrix, dtype=float)
    raw_arr = np.array(raw_vals, dtype=float)

    # Normalize column-wise
    norm_matrix = np.zeros_like(matrix)
    for j in range(matrix.shape[1]):
        col = matrix[:, j]
        mn, mx = col.min(), col.max()
        if mx > mn:
            norm_matrix[:, j] = (col - mn) / (mx - mn)
        else:
            norm_matrix[:, j] = 0.5

    fig, ax = plt.subplots(figsize=(max(12, len(col_labels) * 1.1), 4))
    im = ax.imshow(norm_matrix, cmap="YlOrRd", aspect="auto", vmin=0, vmax=1)
    ax.set_xticks(range(len(col_labels)))
    ax.set_xticklabels(col_labels, fontsize=7, rotation=45, ha="right")
    ax.set_yticks(range(len(row_labels)))
    ax.set_yticklabels(row_labels, fontsize=10)
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            v = raw_arr[i, j]
            txt = f"{v:.1f}" if v < 100 else f"{v:.0f}"
            fc = "white" if norm_matrix[i, j] > 0.65 else "black"
            ax.text(j, i, txt, ha="center", va="center", fontsize=8, color=fc)
    ax.set_title(
        "System Resource Heatmap — Average per Case × Container\n(Darker = Higher usage)",
        fontsize=12,
        fontweight="bold",
        pad=10,
    )
    plt.colorbar(im, ax=ax, fraction=0.02, pad=0.02, label="Relative intensity")
    fig.tight_layout()
    save_fig(fig, "23_heatmap_system.png", out_dir)


# ---------------------------------------------------------------------------
# Section E  — Radar
# ---------------------------------------------------------------------------


def radar_chart(results, out_dir):
    metric_labels = [
        "Throughput\n(msg/s)",
        "Throughput\n(MB/s)",
        "Speed\n(1/time)",
        "Data Eff.\n(1/MB)",
        "Low Latency\n(1/P50)",
    ]

    def score(key, invert=False):
        vals = [r[key] for r in results]
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return [0.5] * len(results)
        norm = [(v - mn) / (mx - mn) for v in vals]
        return [1 - v if invert else v for v in norm]

    def lat_score():
        vals = []
        for r in results:
            ts = r["timeseries"]["latency_p50_ms"]
            avg = statistics.mean(ts) if ts else 9999
            vals.append(avg)
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return [0.5] * len(results)
        return [1 - (v - mn) / (mx - mn) for v in vals]

    scores_per_dim = [
        score("throughput_msg_sec"),
        score("throughput_mb_sec"),
        score("total_time_sec", invert=True),
        score("total_data_mb", invert=True),
        lat_score(),
    ]

    N = len(metric_labels)
    angles = [n / N * 2 * math.pi for n in range(N)] + [0]

    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
    ax.set_theta_offset(math.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_thetagrids(
        [a * 180 / math.pi for a in angles[:-1]], metric_labels, fontsize=10
    )
    ax.set_ylim(0, 1)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["25%", "50%", "75%", "100%"], fontsize=7, color="gray")
    ax.grid(color="gray", linestyle="--", linewidth=0.5, alpha=0.5)

    for i, r in enumerate(results):
        vals = [scores_per_dim[j][i] for j in range(N)] + [scores_per_dim[0][i]]
        ax.plot(angles, vals, color=CASE_COLOURS[i], linewidth=2, label=short(r))
        ax.fill(angles, vals, color=CASE_COLOURS[i], alpha=0.12)

    ax.set_title(
        "Performance Radar — 5 Dimensions (outer = better)",
        fontsize=12,
        fontweight="bold",
        pad=22,
    )
    ax.legend(loc="upper right", bbox_to_anchor=(1.38, 1.15), fontsize=9)
    fig.tight_layout()
    save_fig(fig, "24_radar_chart.png", out_dir)


# ---------------------------------------------------------------------------
# Section F  — Interactive Plotly dashboard
# ---------------------------------------------------------------------------


def plotly_dashboard(results, out_dir):
    if not HAS_PLOTLY:
        print("  Plotly not available — skipping dashboard.html")
        return

    # ---- Panel layout
    # Row 1: Kafka throughput msg/s | MB/s
    # Row 2: Cumulative messages | Latency P50
    # Row 3: Latency P99 | Avg msg size
    # Row 4: kafka-connect CPU % | kafka-connect RAM MB
    # Row 5: kafka-connect Net TX | kafka broker CPU %
    # Row 6: kafka broker Net TX | SQL Server CPU %
    # Row 7: SQL Server RAM MB | SQL Server Disk Write

    specs = [[{"type": "scatter"}, {"type": "scatter"}]] * 7

    row_titles = [
        "Throughput (msg/s)",
        "Throughput (MB/s)",
        "Cumulative messages (M)",
        "Latency P50 (ms)",
        "Latency P99 (ms)",
        "Avg message size (B)",
        "kafka-connect CPU %",
        "kafka-connect RAM (MB)",
        "kafka-connect Net TX (MB/s)",
        "Kafka broker CPU %",
        "Kafka broker Net TX (MB/s)",
        "SQL Server CPU %",
        "SQL Server RAM (MB)",
        "SQL Server Disk Write (MB/s)",
    ]

    subplot_titles = row_titles
    fig = make_subplots(
        rows=7,
        cols=2,
        subplot_titles=subplot_titles,
        vertical_spacing=0.055,
        horizontal_spacing=0.07,
    )

    PCOLORS = CASE_COLOURS

    # Helper: add Kafka ts trace
    def add_kafka(ts_key, row, col, transform=None):
        for i, r in enumerate(results):
            ts = r["timeseries"]
            x = ts["elapsed_sec"]
            y = smooth(ts[ts_key], 5)
            if transform:
                y = transform(y)
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=y,
                    mode="lines",
                    name=short(r),
                    line=dict(color=PCOLORS[i], width=1.8),
                    showlegend=(row == 1 and col == 1),
                    legendgroup=short(r),
                ),
                row=row,
                col=col,
            )

    # Helper: add system ts trace
    def add_sys(container, metric, row, col):
        for i, r in enumerate(results):
            x, y = sys_series(r, container, metric, w=3)
            if not x:
                continue
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=y,
                    mode="lines",
                    name=short(r),
                    line=dict(color=PCOLORS[i], width=1.8),
                    showlegend=False,
                    legendgroup=short(r),
                ),
                row=row,
                col=col,
            )

    # Row 1
    add_kafka("msg_rate", 1, 1)
    add_kafka("mb_rate", 1, 2)
    # Row 2
    add_kafka("cumulative_msgs", 2, 1, transform=lambda y: [v / 1e6 for v in y])
    add_kafka("latency_p50_ms", 2, 2)
    # Row 3
    add_kafka("latency_p99_ms", 3, 1)
    add_kafka("avg_size_bytes", 3, 2)
    # Row 4
    add_sys("kafka-connect", "cpu_pct", 4, 1)
    add_sys("kafka-connect", "mem_mb", 4, 2)
    # Row 5
    add_sys("kafka-connect", "net_tx_mbps", 5, 1)
    add_sys("kafka", "cpu_pct", 5, 2)
    # Row 6
    add_sys("kafka", "net_tx_mbps", 6, 1)
    add_sys("sqlserver", "cpu_pct", 6, 2)
    # Row 7
    add_sys("sqlserver", "mem_mb", 7, 1)
    add_sys("sqlserver", "disk_write_mbps", 7, 2)

    # Axis labels
    ylabels_by_pos = {
        (1, 1): "msg/s",
        (1, 2): "MB/s",
        (2, 1): "Millions",
        (2, 2): "ms",
        (3, 1): "ms",
        (3, 2): "bytes",
        (4, 1): "%",
        (4, 2): "MB",
        (5, 1): "MB/s",
        (5, 2): "%",
        (6, 1): "MB/s",
        (6, 2): "%",
        (7, 1): "MB",
        (7, 2): "MB/s",
    }
    for (row, col), lbl in ylabels_by_pos.items():
        fig.update_yaxes(title_text=lbl, row=row, col=col)
        fig.update_xaxes(title_text="Elapsed (s)", row=row, col=col)

    fig.update_layout(
        title=dict(
            text="<b>Debezium Avro Migration — Full Observability Dashboard</b><br>"
            "<sup>4 Tuning Cases × 2M rows | Kafka + System metrics</sup>",
            x=0.5,
            font=dict(size=17),
        ),
        height=3500,
        template="plotly_white",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="right",
            x=1,
            font=dict(size=11),
        ),
        font=dict(size=10),
    )

    out_path = out_dir / "dashboard.html"
    fig.write_html(str(out_path), include_plotlyjs="cdn")
    print(f"  dashboard.html  ({out_path.stat().st_size // 1024} KB)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    path = (
        pathlib.Path(sys.argv[1])
        if len(sys.argv) > 1
        else pathlib.Path(__file__).parent / "migration_results.json"
    )
    if not path.exists():
        print(f"ERROR: {path} not found.")
        sys.exit(1)

    out_dir = pathlib.Path(__file__).parent / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  Source : {path}")
    print(f"  Output : {out_dir}\n")

    results = load(path)
    if not results:
        print("No valid results.")
        sys.exit(1)

    print("  Kafka / consumer charts:")
    kafka_ts_chart(
        results,
        out_dir,
        "msg_rate",
        "Throughput over Time — msg/s",
        "Messages / second",
        "01_throughput_msg_rate.png",
    )
    kafka_ts_chart(
        results,
        out_dir,
        "mb_rate",
        "Throughput over Time — MB/s",
        "MB / second",
        "02_throughput_mb_rate.png",
    )
    cumulative_chart(results, out_dir)
    kafka_ts_chart(
        results,
        out_dir,
        "latency_p50_ms",
        "End-to-End Latency P50 over Time",
        "ms",
        "04_latency_p50.png",
    )
    kafka_ts_chart(
        results,
        out_dir,
        "latency_p99_ms",
        "End-to-End Latency P99 over Time",
        "ms",
        "05_latency_p99.png",
    )
    kafka_ts_chart(
        results,
        out_dir,
        "avg_size_bytes",
        "Average Message Size over Time",
        "Bytes / message",
        "06_avg_message_size.png",
    )

    print("\n  Summary bars:")
    bar_chart(
        results,
        out_dir,
        "total_time_sec",
        "Total Migration Time per Case",
        "Seconds",
        "07_bar_total_time.png",
        fmt="{:.1f}",
        unit="s",
        lower_is_better=True,
    )
    bar_chart(
        results,
        out_dir,
        "throughput_msg_sec",
        "Average Throughput — msg/s",
        "Messages / second",
        "08_bar_avg_throughput.png",
        fmt="{:,.0f}",
    )
    bar_chart(
        results,
        out_dir,
        "total_data_mb",
        "Total Data Transferred per Case",
        "MB",
        "09_bar_total_data.png",
        fmt="{:.1f}",
        unit=" MB",
        lower_is_better=True,
    )

    print("\n  System metrics — kafka-connect:")
    sys_ts_chart(
        results,
        out_dir,
        "kafka-connect",
        "cpu_pct",
        "kafka-connect — CPU %",
        "CPU %",
        "10_sys_connect_cpu.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "kafka-connect",
        "mem_mb",
        "kafka-connect — RAM (MB)",
        "RAM MB",
        "11_sys_connect_mem.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "kafka-connect",
        "net_rx_mbps",
        "kafka-connect — Network RX (MB/s)",
        "MB/s",
        "12_sys_connect_net_rx.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "kafka-connect",
        "net_tx_mbps",
        "kafka-connect — Network TX (MB/s)",
        "MB/s",
        "13_sys_connect_net_tx.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "kafka-connect",
        "disk_read_mbps",
        "kafka-connect — Disk Read (MB/s)",
        "MB/s",
        "14_sys_connect_disk_r.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "kafka-connect",
        "disk_write_mbps",
        "kafka-connect — Disk Write (MB/s)",
        "MB/s",
        "15_sys_connect_disk_w.png",
    )

    print("\n  System metrics — Kafka broker:")
    sys_ts_chart(
        results,
        out_dir,
        "kafka",
        "cpu_pct",
        "Kafka Broker — CPU %",
        "CPU %",
        "16_sys_kafka_cpu.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "kafka",
        "mem_mb",
        "Kafka Broker — RAM (MB)",
        "RAM MB",
        "17_sys_kafka_mem.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "kafka",
        "net_tx_mbps",
        "Kafka Broker — Network TX (MB/s)",
        "MB/s",
        "18_sys_kafka_net_tx.png",
    )

    print("\n  System metrics — SQL Server:")
    sys_ts_chart(
        results,
        out_dir,
        "sqlserver",
        "cpu_pct",
        "SQL Server — CPU %",
        "CPU %",
        "19_sys_sql_cpu.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "sqlserver",
        "mem_mb",
        "SQL Server — RAM (MB)",
        "RAM MB",
        "20_sys_sql_mem.png",
    )
    sys_ts_chart(
        results,
        out_dir,
        "sqlserver",
        "disk_read_mbps",
        "SQL Server — Disk Read (MB/s)",
        "MB/s",
        "21_sys_sql_disk_r.png",
    )

    print("\n  Multi-container panels:")
    multi_container_sys_panel(
        results,
        out_dir,
        "cpu_pct",
        "CPU % — All Containers × All Cases",
        "%",
        "sys_all_cpu_panel.png",
    )
    multi_container_sys_panel(
        results,
        out_dir,
        "net_tx_mbps",
        "Network TX MB/s — All Containers × All Cases",
        "MB/s",
        "sys_all_net_tx_panel.png",
    )

    print("\n  Heatmaps:")
    heatmap_kafka(results, out_dir)
    heatmap_system(results, out_dir)

    print("\n  Radar chart:")
    radar_chart(results, out_dir)

    print("\n  Interactive dashboard:")
    plotly_dashboard(results, out_dir)

    print(f"\n  Total files in {out_dir}:")
    for f in sorted(out_dir.iterdir()):
        print(f"    {f.name:<55} {f.stat().st_size // 1024:>6} KB")


if __name__ == "__main__":
    main()
