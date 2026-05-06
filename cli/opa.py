"""
opa.py — OPA policy client for SwiftDeploy.

Design principles:
  - The CLI collects facts (disk, CPU, metrics). OPA makes the decision.
  - No allow/deny logic lives here — only input construction and result parsing.
  - Each failure mode produces a distinct, actionable message.
  - Never crashes or hangs (all network calls are bounded by timeout).

Two check types:
  check_pre_deploy  — infrastructure domain (disk, CPU, memory)
  check_pre_promote — canary_safety domain (error rate, P99 latency)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Result type — returned by every check function
# ---------------------------------------------------------------------------


@dataclass
class PolicyResult:
    allowed: bool
    violations: list[str] = field(default_factory=list)
    domain: str = ""

    def __bool__(self) -> bool:
        return self.allowed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 5  # seconds — OPA is local, no reason for a long timeout


def _opa_host() -> str:
    return os.environ.get("OPA_HOST", "http://localhost:8181").rstrip("/")


def _query(domain: str, input_data: dict) -> PolicyResult:
    """
    POST to OPA's REST API for a single policy package.

    URL pattern: POST /v1/data/swiftdeploy/{domain}
    Body:        {"input": { ... }}
    Response:    {"result": {"allow": bool, "violations": [str, ...]}}

    Failure modes — each produces a distinct message and a blocked result:
      - OPA not running          → connection refused
      - Policy package not found → OPA returns {"result": null} or empty result
      - allow = false            → violations array surfaced to caller
      - Network timeout          → urllib.error.URLError with timeout
    """
    url = f"{_opa_host()}/v1/data/swiftdeploy/{domain}"
    payload = json.dumps({"input": input_data}).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
    except ConnectionRefusedError:
        return PolicyResult(
            allowed=False,
            violations=[
                "OPA is not available — cannot enforce policy. Deploy blocked.\n"
                "  Start the stack first: swiftdeploy deploy\n"
                "  Or check OPA is running: docker ps | grep opa"
            ],
            domain=domain,
        )
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "timed out" in reason.lower():
            return PolicyResult(
                allowed=False,
                violations=[
                    f"OPA query timed out after {_DEFAULT_TIMEOUT}s — is OPA healthy?"
                ],
                domain=domain,
            )
        return PolicyResult(
            allowed=False,
            violations=[
                f"OPA is not available — cannot enforce policy. Deploy blocked.\n"
                f"  Reason: {reason}"
            ],
            domain=domain,
        )

    # OPA returns {"result": null} when the package path doesn't exist
    result = body.get("result")
    if result is None:
        return PolicyResult(
            allowed=False,
            violations=[
                f"Policy package not found: swiftdeploy.{domain.replace('/', '.')}"
            ],
            domain=domain,
        )

    allowed = bool(result.get("allow", False))
    violations = list(result.get("violations", []))

    # Edge case: allow=false but no violations defined in the policy.
    # Surface a generic message so the operator isn't left with silent failure.
    if not allowed and not violations:
        violations = [
            f"Policy denied the request (no violation messages defined in {domain})"
        ]

    return PolicyResult(allowed=allowed, violations=violations, domain=domain)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_pre_deploy(host_stats: dict) -> PolicyResult:
    """
    Run the infrastructure policy check before deploying.

    host_stats must contain:
      disk_free_gb     float
      cpu_load         float  (1-minute load average)
      mem_free_percent float
    """
    input_data = {
        "check_type": "pre_deploy",
        "disk_free_gb": host_stats.get("disk_free_gb", 0.0),
        "cpu_load": host_stats.get("cpu_load", 0.0),
        "mem_free_percent": host_stats.get("mem_free_percent", 0.0),
    }
    return _query("infrastructure", input_data)


def check_pre_promote(metrics: dict, target_mode: str) -> PolicyResult:
    """
    Run the canary_safety policy check before promoting.

    metrics must contain:
      error_rate      float  (fraction, e.g. 0.005 = 0.5%)
      p99_latency_ms  int/float
    """
    input_data = {
        "check_type": "pre_promote",
        "error_rate": metrics.get("error_rate", 0.0),
        "p99_latency_ms": metrics.get("p99_latency_ms", 0),
        "target_mode": target_mode,
    }
    return _query("canary_safety", input_data)
