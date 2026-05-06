"""
audit.py — Audit report generator for SwiftDeploy.

Reads history.jsonl (written by `swiftdeploy status`) and produces
audit_report.md — a GitHub Flavored Markdown report with a timeline
table and a policy violations table.

Usage:
    swiftdeploy audit

Output:
    audit_report.md  (in the working directory)

To change report content: edit _build_summary, _build_timeline_table,
or _build_violations_table. The entry point (run_audit) never needs
to change for content modifications.

To change output path: the caller (swiftdeploy dispatcher) passes the path.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Column definitions ────────────────────────────────────────────────────────
# Each dict defines one column in a report table.
# To add/remove columns: edit these lists only.
# Keys map directly to history.jsonl field names (or "_computed" fields).

TIMELINE_COLUMNS = [
    {"header": "Timestamp",   "key": "timestamp",       "fmt": None},
    {"header": "Mode",        "key": "mode",             "fmt": None},
    {"header": "Chaos",       "key": "_chaos_text",      "fmt": None},
    {"header": "Total Req",   "key": "total_requests",   "fmt": lambda v: f"{int(v):,}"},
    {"header": "Error Rate",  "key": "error_rate_pct",   "fmt": lambda v: f"{float(v):.2f}%"},
    {"header": "P99 (ms)",    "key": "p99_latency_ms",   "fmt": lambda v: f"{float(v):.1f}"},
]

VIOLATION_COLUMNS = [
    {"header": "Timestamp",   "key": "timestamp",  "fmt": None},
    {"header": "Domain",      "key": "_domain",    "fmt": None},
    {"header": "Status",      "key": "_status",    "fmt": None},
]


# ── Entry point ───────────────────────────────────────────────────────────────

def run_audit(history_path: Path, output_path: Path) -> int:
    """
    Entry point for `swiftdeploy audit`.
    Reads history_path, writes output_path, returns exit code.
    """
    if not history_path.exists():
        print(
            f"[ERROR] history.jsonl not found at: {history_path}\n"
            f"  Run `swiftdeploy status` first to generate history.",
            file=sys.stderr,
        )
        return 1

    records = _read_history(history_path)
    if not records:
        print(
            f"[WARN] history.jsonl is empty or contains no valid records.\n"
            f"  Run `swiftdeploy status` to populate it.",
            file=sys.stderr,
        )
        return 1

    print(f"  Read {len(records)} records from {history_path}")

    summary  = _build_summary(records)
    timeline = _build_timeline_table(records)
    violations = _build_violations_table(records)

    _write_report(summary, timeline, violations, output_path)

    print(f"  Report written to {output_path}")
    return 0


# ── Reader ────────────────────────────────────────────────────────────────────

def _read_history(path: Path) -> list[dict]:
    """
    Read history.jsonl line by line.
    Malformed or empty lines are skipped with a warning — never crash.
    """
    records: list[dict] = []
    skipped = 0

    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
                if skipped <= 5:  # only warn on first few to avoid spam
                    print(f"  [WARN] Skipping malformed line {i}", file=sys.stderr)

    if skipped > 5:
        print(f"  [WARN] {skipped} malformed lines skipped total", file=sys.stderr)

    return records


# ── Summary stats ─────────────────────────────────────────────────────────────

def _build_summary(records: list[dict]) -> dict:
    """
    Compute summary statistics from all records.
    To add a new summary stat: add it to the returned dict and reference
    it in _write_report().
    """
    if not records:
        return {}

    timestamps = [r.get("timestamp", "") for r in records if r.get("timestamp")]
    first_ts = min(timestamps) if timestamps else "unknown"
    last_ts  = max(timestamps) if timestamps else "unknown"

    # Count mode changes (consecutive same-mode ticks don't count)
    mode_changes = 0
    prev_mode = None
    for r in records:
        mode = r.get("mode")
        if mode and mode != prev_mode:
            if prev_mode is not None:
                mode_changes += 1
            prev_mode = mode

    # Count ticks where any policy domain failed
    violation_ticks = sum(
        1 for r in records
        if any(v == "fail" for k, v in r.items() if k.startswith("policy_"))
    )

    # Collect unique policy domains seen across all records
    domains: set[str] = set()
    for r in records:
        for k in r:
            if k.startswith("policy_"):
                domains.add(k[len("policy_"):])

    return {
        "total_ticks":      len(records),
        "first_timestamp":  first_ts,
        "last_timestamp":   last_ts,
        "mode_changes":     mode_changes,
        "violation_ticks":  violation_ticks,
        "policy_domains":   sorted(domains),
    }


# ── Table builders ────────────────────────────────────────────────────────────

def _build_timeline_table(records: list[dict]) -> str:
    """
    Build a GFM markdown table for all records using TIMELINE_COLUMNS.
    Computed fields (prefix _) are resolved inline.
    """
    rows = [_resolve_row(r, TIMELINE_COLUMNS) for r in records]
    return _gfm_table(TIMELINE_COLUMNS, rows)


def _build_violations_table(records: list[dict]) -> str:
    """
    Build a GFM markdown table for ticks with at least one policy failure.
    Each failing domain gets its own row.
    """
    rows: list[list[str]] = []

    for r in records:
        failing_domains = [
            k[len("policy_"):] for k, v in r.items()
            if k.startswith("policy_") and v == "fail"
        ]
        for domain in failing_domains:
            # Inject computed fields for this specific domain
            augmented = dict(r)
            augmented["_domain"] = domain
            augmented["_status"] = "FAIL"
            rows.append(_resolve_row(augmented, VIOLATION_COLUMNS))

    if not rows:
        return "_No policy violations recorded._\n"

    return _gfm_table(VIOLATION_COLUMNS, rows)


def _resolve_row(record: dict, columns: list[dict]) -> list[str]:
    """
    Resolve one table row from a record dict using the column definitions.
    Computed keys (prefix _) must already be injected into record before calling.
    """
    cells: list[str] = []
    for col in columns:
        key = col["key"]
        fmt = col["fmt"]

        # Computed fields are pre-injected by the caller
        if key.startswith("_"):
            value = record.get(key, "")
        else:
            value = record.get(key, "")

        # _chaos_text is derived from chaos_active
        if key == "_chaos_text":
            chaos = record.get("chaos_active", 0)
            value = {1: "slow", 2: "error"}.get(int(chaos), "none")

        if value == "" or value is None:
            cells.append("—")
        elif fmt:
            try:
                cells.append(fmt(value))
            except (ValueError, TypeError):
                cells.append(str(value))
        else:
            cells.append(str(value))

    return cells


def _gfm_table(columns: list[dict], rows: list[list[str]]) -> str:
    """
    Render a GitHub Flavored Markdown table.
    Columns are left-aligned. All cells are padded to column width for readability.
    """
    headers = [col["header"] for col in columns]

    # Compute column widths (max of header or any cell value)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def _pad(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    separator = "| " + " | ".join("-" * w for w in widths) + " |"

    lines = [_pad(headers), separator]
    for row in rows:
        lines.append(_pad(row))

    return "\n".join(lines) + "\n"


# ── Report writer ─────────────────────────────────────────────────────────────

def _write_report(
    summary: dict,
    timeline: str,
    violations: str,
    output_path: Path,
) -> None:
    """
    Assemble and write audit_report.md.
    To change the report structure: edit the f-string below.
    To add new sections: add them between the existing ones.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    domains_str = ", ".join(summary.get("policy_domains", [])) or "none"

    report = f"""# SwiftDeploy Audit Report

Generated: {generated_at}

## Summary

| Metric | Value |
| --- | --- |
| Total ticks recorded | {summary.get('total_ticks', 0):,} |
| First tick | {summary.get('first_timestamp', '—')} |
| Last tick | {summary.get('last_timestamp', '—')} |
| Mode changes | {summary.get('mode_changes', 0)} |
| Ticks with policy violations | {summary.get('violation_ticks', 0)} |
| Policy domains tracked | {domains_str} |

## Timeline

{timeline}
## Policy Violations

{violations}
"""

    output_path.write_text(report, encoding="utf-8")
