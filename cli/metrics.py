"""
metrics.py — Prometheus metrics scraper and calculator for SwiftDeploy.

Scrapes GET /metrics from the running service (via nginx), parses the
Prometheus text format, and computes derived values needed by OPA and
the status dashboard.

Derived values:
  error_rate      — requests with status >= 500 / total (over a 30s window)
  p99_latency_ms  — 99th percentile latency from the histogram

The 30-second window is implemented by taking two scrapes 30s apart and
computing the delta. This avoids accumulating all-time counters which would
dilute short bursts of errors.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class MetricsScrape:
    """Parsed values from a single /metrics scrape."""

    # http_requests_total — keyed by (method, path, status_code)
    requests: dict[tuple[str, str, str], int] = field(default_factory=dict)

    # http_request_duration_seconds histogram
    # bucket_counts: le boundary (float) → cumulative count (int)
    bucket_counts: dict[float, int] = field(default_factory=dict)
    duration_sum: float = 0.0
    duration_count: int = 0

    # Gauges
    uptime_seconds: float = 0.0
    mode: int = 0  # 0=stable, 1=canary
    chaos_active: int = 0  # 0=none, 1=slow, 2=error

    # When this scrape was taken (monotonic)
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def total_requests(self) -> int:
        return sum(self.requests.values())

    @property
    def error_requests(self) -> int:
        return sum(
            count
            for (_, _, status), count in self.requests.items()
            if status.startswith("5")
        )


@dataclass
class MetricsWindow:
    """
    Computed values derived from a 30-second delta between two scrapes.
    This is what gets passed to OPA and displayed on the status dashboard.
    """

    error_rate: float  # fraction 0.0–1.0
    p99_latency_ms: float  # milliseconds
    req_per_sec: float
    uptime_seconds: float
    mode: int
    chaos_active: int
    total_requests: int  # raw total from latest scrape (for display)
    error_requests: int


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


def scrape(nginx_port: int) -> MetricsScrape | None:
    """
    Fetch and parse GET /metrics from the running service via nginx.

    Returns None if the endpoint is unreachable or the response is malformed.
    Callers must handle None — the stack might not be running.
    """
    url = f"http://localhost:{nginx_port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError):
        return None

    return _parse(body)


def _parse(text: str) -> MetricsScrape:
    """
    Parse Prometheus text exposition format into a MetricsScrape.

    Format:
      # HELP metric_name description
      # TYPE metric_name type
      metric_name{label="value"} numeric_value
    """
    result = MetricsScrape()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        space_idx = line.rfind(" ")
        if space_idx == -1:
            continue

        metric_part = line[:space_idx].strip()
        value_str = line[space_idx:].strip()

        try:
            value = float(value_str)
        except ValueError:
            continue

        if metric_part.startswith("http_requests_total"):
            labels = _parse_labels(metric_part)
            method = labels.get("method", "")
            path = labels.get("path", "")
            status = labels.get("status_code", "")
            if method and path and status:
                result.requests[(method, path, status)] = int(value)

        elif metric_part.startswith("http_request_duration_seconds_bucket"):
            labels = _parse_labels(metric_part)
            le_str = labels.get("le", "")
            if le_str == "+Inf":
                result.duration_count = int(value)
            else:
                try:
                    result.bucket_counts[float(le_str)] = int(value)
                except ValueError:
                    pass

        elif metric_part == "http_request_duration_seconds_sum":
            result.duration_sum = value

        elif metric_part == "http_request_duration_seconds_count":
            result.duration_count = int(value)

        elif metric_part == "app_uptime_seconds":
            result.uptime_seconds = value

        elif metric_part == "app_mode":
            result.mode = int(value)

        elif metric_part == "chaos_active":
            result.chaos_active = int(value)

    return result


def _parse_labels(metric_part: str) -> dict[str, str]:
    """
    Extract label key-value pairs from a Prometheus metric identifier.

    'http_requests_total{method="GET",path="/",status_code="200"}'
    → {"method": "GET", "path": "/", "status_code": "200"}
    """
    brace_open = metric_part.find("{")
    brace_close = metric_part.rfind("}")
    if brace_open == -1 or brace_close == -1:
        return {}

    labels_str = metric_part[brace_open + 1 : brace_close]
    labels: dict[str, str] = {}

    for pair in labels_str.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        labels[k.strip()] = v.strip().strip('"')

    return labels


# ---------------------------------------------------------------------------
# Window calculator
# ---------------------------------------------------------------------------


def compute_window(older: MetricsScrape, newer: MetricsScrape) -> MetricsWindow:
    """
    Compute a MetricsWindow from two scrapes taken ~30s apart.

    All rate and latency values are derived from the delta, giving a rolling
    window rather than all-time totals.
    """
    elapsed = max(newer.timestamp - older.timestamp, 0.001)  # guard div/0

    total_delta = _counter_delta(newer.total_requests, older.total_requests)
    error_delta = _counter_delta(newer.error_requests, older.error_requests)

    error_rate = (error_delta / total_delta) if total_delta > 0 else 0.0
    req_per_sec = total_delta / elapsed

    count_delta = _counter_delta(newer.duration_count, older.duration_count)
    p99_latency_ms = _compute_p99(
        older_buckets=older.bucket_counts,
        newer_buckets=newer.bucket_counts,
        total_count_delta=count_delta,
    )

    return MetricsWindow(
        error_rate=round(error_rate, 6),
        p99_latency_ms=round(p99_latency_ms, 2),
        req_per_sec=round(req_per_sec, 2),
        uptime_seconds=newer.uptime_seconds,
        mode=newer.mode,
        chaos_active=newer.chaos_active,
        total_requests=newer.total_requests,
        error_requests=newer.error_requests,
    )


def _counter_delta(newer: int, older: int) -> int:
    """
    Safe counter delta. If newer < older the counter reset (container restart)
    — return newer directly as a conservative undercount rather than negative.
    """
    return newer if newer < older else newer - older


def _compute_p99(
    older_buckets: dict[float, int],
    newer_buckets: dict[float, int],
    total_count_delta: int,
) -> float:
    """
    Compute P99 latency in milliseconds from histogram bucket deltas.

    Prometheus histograms are cumulative. We find the first bucket where the
    cumulative delta count >= 99% of total, then linearly interpolate within
    that bucket for a more precise estimate.

    Returns 0.0 if there is no data.
    """
    if total_count_delta <= 0:
        return 0.0

    target = 0.99 * total_count_delta
    boundaries = sorted(set(list(older_buckets.keys()) + list(newer_buckets.keys())))

    prev_count = 0.0
    prev_le = 0.0

    for le in boundaries:
        newer_count = newer_buckets.get(le, 0)
        older_count = older_buckets.get(le, 0)
        delta = float(_counter_delta(newer_count, older_count))

        if delta >= target:
            # Linear interpolation within this bucket
            fraction = (
                (target - prev_count) / (delta - prev_count)
                if delta > prev_count
                else 0.0
            )
            prev_le_ms = prev_le * 1000
            le_ms = le * 1000
            return prev_le_ms + fraction * (le_ms - prev_le_ms)

        prev_count = delta
        prev_le = le

    # All observations fell beyond the last finite bucket
    return boundaries[-1] * 1000 if boundaries else 0.0


# ---------------------------------------------------------------------------
# Convenience: single-scrape snapshot (no delta window)
# Used for the first tick of the status dashboard before a second scrape.
# ---------------------------------------------------------------------------


def snapshot_from_scrape(s: MetricsScrape) -> MetricsWindow:
    """
    Build a MetricsWindow from a single scrape.
    error_rate and p99 will be all-time values, not windowed.
    """
    total = s.total_requests
    errors = s.error_requests
    error_rate = (errors / total) if total > 0 else 0.0
    p99 = _compute_p99({}, s.bucket_counts, s.duration_count)

    return MetricsWindow(
        error_rate=round(error_rate, 6),
        p99_latency_ms=round(p99, 2),
        req_per_sec=0.0,
        uptime_seconds=s.uptime_seconds,
        mode=s.mode,
        chaos_active=s.chaos_active,
        total_requests=total,
        error_requests=errors,
    )
