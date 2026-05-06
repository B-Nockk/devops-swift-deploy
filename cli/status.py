"""
status.py — Live terminal dashboard for SwiftDeploy.

Scrapes /api/dashboard/snapshot every N seconds, displays a rich terminal
table, queries OPA for policy compliance, and appends each tick to
history.jsonl.

The browser dashboard (dashboard/) is the primary UI.
This is the secondary terminal view — useful when SSH'd in or headless.

To change what's displayed: edit _render_table() below.
To change what's written to history: edit _build_history_line() below.
To change the OPA domains queried: edit POLICY_DOMAINS below.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from . import opa

# ── Configuration ─────────────────────────────────────────────────────────────
# Change these to adjust dashboard behaviour without touching logic below.

# OPA policy domains to check each tick.
# To add a new domain: add its package name here. No other changes needed.
POLICY_DOMAINS = ["infrastructure", "canary_safety"]

# Path where history is appended.
HISTORY_FILE = Path("history.jsonl")

# Max lines kept in history.jsonl before the file is trimmed.
# Set to None to grow unbounded.
HISTORY_MAX_LINES = 10_000

# Snapshot endpoint — must match the BFF route in dashboard/handler.go
# and the nginx proxy location in nginx.conf.j2.
# Change this if you move the endpoint.
def _snapshot_url(nginx_port: int) -> str:
    return f"http://localhost:{nginx_port}/api/dashboard/snapshot"

# ── OPA input builders ────────────────────────────────────────────────────────
# Each domain gets its own input shape.
# To add a new domain: add a case here returning the right input dict.

def _opa_input_for(domain: str, snap: dict) -> dict:
    if domain == "infrastructure":
        import shutil
        disk = shutil.disk_usage("/")
        disk_free_gb = round(disk.free / (1024 ** 3), 2)
        try:
            with open("/proc/loadavg") as f:
                cpu_load = round(float(f.read().split()[0]), 2)
        except OSError:
            cpu_load = 0.0
        return {
            "check_type": "status_tick",
            "disk_free_gb": disk_free_gb,
            "cpu_load": cpu_load,
            "mem_free_percent": 100.0,
        }
    if domain == "canary_safety":
        return {
            "check_type": "status_tick",
            "error_rate": snap.get("error_rate_pct", 0.0) / 100,
            "p99_latency_ms": snap.get("p99_latency_ms", 0.0),
            "target_mode": snap.get("mode", "stable"),
        }
    return {}


# ── Snapshot fetcher ──────────────────────────────────────────────────────────

def _fetch_snapshot(nginx_port: int) -> dict | None:
    try:
        url = _snapshot_url(nginx_port)
        with urllib.request.urlopen(url, timeout=4) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


# ── History ───────────────────────────────────────────────────────────────────

def _build_history_line(snap: dict, policy_results: dict[str, bool]) -> dict:
    """
    Build one history.jsonl line from a snapshot and policy results.
    To add new fields: add them here. history.jsonl format will update automatically.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": snap.get("mode", "unknown"),
        "chaos_active": snap.get("chaos_active", 0),
        "error_rate_pct": snap.get("error_rate_pct", 0.0),
        "p99_latency_ms": snap.get("p99_latency_ms", 0.0),
        "total_requests": snap.get("total_requests", 0),
        **{f"policy_{d}": "pass" if ok else "fail" for d, ok in policy_results.items()},
    }


def _append_history(line: dict) -> None:
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(line) + "\n")

    if HISTORY_MAX_LINES is not None:
        _trim_history()


def _trim_history() -> None:
    if not HISTORY_FILE.exists():
        return
    lines = HISTORY_FILE.read_text().splitlines()
    if len(lines) > HISTORY_MAX_LINES:
        HISTORY_FILE.write_text("\n".join(lines[-HISTORY_MAX_LINES:]) + "\n")


# ── Rich rendering ────────────────────────────────────────────────────────────

def _render_rich(snap: dict, policy_results: dict[str, bool], tick: int) -> Panel:
    """
    Build the rich terminal panel from a snapshot.
    To change what's shown: edit this function only.
    """
    console_width = 80

    # ── Mode and chaos badges
    mode = snap.get("mode", "?")
    chaos_text = snap.get("chaos_active_text", "none")
    mode_color = "yellow" if mode == "canary" else "blue"
    header = Text()
    header.append(f" {mode.upper()} ", style=f"bold white on {mode_color}")
    if chaos_text != "none":
        header.append(f" ⚡ CHAOS:{chaos_text.upper()} ", style="bold white on red")
    header.append(f"  v{snap.get('version','?')}  up {snap.get('uptime_human','?')}",
                  style="dim")

    # ── Key metrics table
    metrics = Table(box=box.SIMPLE, show_header=True, header_style="bold dim",
                    pad_edge=False, expand=True)
    metrics.add_column("Metric", style="dim", width=20)
    metrics.add_column("Value", justify="right")

    err_pct = snap.get("error_rate_pct", 0.0)
    err_style = "red" if err_pct >= 5 else "yellow" if err_pct >= 1 else "green"

    p99 = snap.get("p99_latency_ms", 0.0)
    p99_style = "red" if p99 >= 500 else "yellow" if p99 >= 250 else "green"

    metrics.add_row("Total Requests",  f"[bold]{snap.get('total_requests', 0):,}[/bold]")
    metrics.add_row("Error Requests",  f"[bold]{snap.get('error_requests', 0):,}[/bold]")
    metrics.add_row("Error Rate",      f"[{err_style}]{err_pct:.2f}%[/{err_style}]")
    metrics.add_row("P99 Latency",     f"[{p99_style}]{p99:.1f}ms[/{p99_style}]")

    # ── Policy compliance
    policy = Table(box=box.SIMPLE, show_header=True, header_style="bold dim",
                   pad_edge=False, expand=True)
    policy.add_column("Policy", style="dim")
    policy.add_column("Status", justify="right")

    for domain, passed in policy_results.items():
        label = domain.replace("_", " ").title()
        status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        policy.add_row(label, status)

    # ── Per-route breakdown
    routes_table = Table(box=box.SIMPLE, show_header=True, header_style="bold dim",
                         pad_edge=False, expand=True)
    routes_table.add_column("Method", style="dim", width=8)
    routes_table.add_column("Path")
    routes_table.add_column("Status", width=8)
    routes_table.add_column("Count", justify="right")

    for r in (snap.get("routes") or []):
        code = str(r.get("status_code", ""))
        code_style = (
            "green" if code.startswith("2") else
            "yellow" if code.startswith("4") else
            "red" if code.startswith("5") else ""
        )
        routes_table.add_row(
            r.get("method", ""),
            r.get("path", ""),
            f"[{code_style}]{code}[/{code_style}]" if code_style else code,
            f"{r.get('count', 0):,}",
        )

    now = datetime.now().strftime("%H:%M:%S")
    footer = f"[dim]tick #{tick}  ·  {now}  ·  Ctrl+C to exit[/dim]"

    return Panel(
        # Stack sections vertically inside the panel
        _vstack([header, metrics, policy, routes_table, Text(footer)]),
        title="[bold]⚡ SwiftDeploy[/bold]",
        border_style="bright_black",
    )


def _vstack(items) -> Table:
    """Vertically stack rich renderables using a single-column table."""
    t = Table.grid(expand=True)
    t.add_column()
    for item in items:
        t.add_row(item)
        t.add_row("")  # spacer
    return t


# ── Fallback plain-text renderer (when rich is not installed) ─────────────────

def _render_plain(snap: dict, policy_results: dict[str, bool], tick: int) -> str:
    lines = [
        f"\n── SwiftDeploy Status  tick #{tick} ──",
        f"Mode:           {snap.get('mode','?')}",
        f"Version:        {snap.get('version','?')}",
        f"Uptime:         {snap.get('uptime_human','?')}",
        f"Chaos:          {snap.get('chaos_active_text','?')}",
        f"Total requests: {snap.get('total_requests',0):,}",
        f"Error requests: {snap.get('error_requests',0):,}",
        f"Error rate:     {snap.get('error_rate_pct',0.0):.2f}%",
        f"P99 latency:    {snap.get('p99_latency_ms',0.0):.1f}ms",
        "",
        "Policy compliance:",
    ]
    for domain, passed in policy_results.items():
        lines.append(f"  {domain}: {'PASS' if passed else 'FAIL'}")
    lines.append(f"\n{datetime.now().strftime('%H:%M:%S')}  Ctrl+C to exit")
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def run_status_dashboard(manifest_path: Path, interval: int = 5) -> int:
    """
    Main loop for `swiftdeploy status`.

    manifest_path: used to resolve nginx_port.
    interval:      seconds between ticks.
    """
    from .config import resolve

    cfg = resolve(manifest_path, [])
    nginx_port = cfg.nginx_port

    print(f"SwiftDeploy status dashboard — polling every {interval}s")
    print(f"Browser dashboard: http://localhost:{nginx_port}/dashboard")
    print("Ctrl+C to exit\n")

    tick = 0
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if HAS_RICH:
        console = Console()
        with Live(console=console, refresh_per_second=1, screen=False) as live:
            while running:
                tick += 1
                snap = _fetch_snapshot(nginx_port)

                if snap is None:
                    live.update(Panel("[red]Cannot reach /api/dashboard/snapshot — is the stack running?[/red]"))
                    time.sleep(interval)
                    continue

                # Query OPA for each domain
                policy_results: dict[str, bool] = {}
                for domain in POLICY_DOMAINS:
                    input_data = _opa_input_for(domain, snap)
                    try:
                        result = opa._query(domain, input_data)
                        policy_results[domain] = result.allowed
                    except Exception:
                        policy_results[domain] = False

                _append_history(_build_history_line(snap, policy_results))
                live.update(_render_rich(snap, policy_results, tick))
                time.sleep(interval)
    else:
        # Fallback: plain text, clear screen each tick
        while running:
            tick += 1
            snap = _fetch_snapshot(nginx_port)

            if snap is None:
                print("Cannot reach /api/dashboard/snapshot — is the stack running?")
                time.sleep(interval)
                continue

            policy_results = {}
            for domain in POLICY_DOMAINS:
                input_data = _opa_input_for(domain, snap)
                try:
                    result = opa._query(domain, input_data)
                    policy_results[domain] = result.allowed
                except Exception:
                    policy_results[domain] = False

            _append_history(_build_history_line(snap, policy_results))
            print("\033[H\033[J", end="")  # clear screen
            print(_render_plain(snap, policy_results, tick))
            time.sleep(interval)

    print("\nStatus dashboard stopped.")
    return 0
