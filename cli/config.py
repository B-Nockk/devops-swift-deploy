"""
config.py — Layered configuration resolver for SwiftDeploy.

Resolution order (highest → lowest priority):
  1. CLI flags       e.g. --nginx.port=9090           (session-only, never written back)
  2. manifest.yaml   the stored source of truth
  3. .env file       environment overrides
  4. REQUIRED_ENV    defaults from cli/env.py          (bottom of the stack)

The golden rule:
  CLI flags and .env are session-level overrides. Only `swiftdeploy promote`
  is allowed to mutate manifest.yaml (the `mode` field only).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .env import REQUIRED_ENV, resolve_required

# ---------------------------------------------------------------------------
# REQUIRED_FIELDS — validated against the raw manifest by `swiftdeploy validate`.
# These are the minimum fields the spec mandates must be present in manifest.yaml.
# Defaults from REQUIRED_ENV cover them if absent — but validate still flags
# missing manifest fields explicitly so the operator knows to add them.
# Uniqueness is enforced by using a list of tuples (no duplicate paths possible).
# ---------------------------------------------------------------------------
REQUIRED_FIELDS: list[tuple[str, ...]] = [
    ("services", "image"),
    ("services", "port"),
    ("nginx", "image"),
    ("nginx", "port"),
    ("network", "name"),
    ("network", "driver_type"),
    ("mode",),
]


# ---------------------------------------------------------------------------
# Resolved config — what the rest of the CLI works with.
# All values fully resolved; no Optional fields.
# ---------------------------------------------------------------------------
@dataclass
class ResolvedConfig:
    # services
    service_image: str
    service_port: int
    service_version: str
    restart_policy: str

    # nginx
    nginx_image: str
    nginx_port: int
    proxy_timeout: int
    contact: str

    # network
    network_name: str
    network_driver: str

    # runtime
    mode: str

    # container env vars
    container_app_name: str = "nockk-swiftdeploy-v1"
    container_nginx_name: str = "nockk-nginx-v1"
    container_opa_name: str = "swiftdeploy-opa"

    # derived — computed for templates, not in manifest
    service_host: str = "app"
    service_name: str = "nockk-swiftdeploy-v1"

    def as_template_vars(self) -> dict[str, Any]:
        """Return a flat dict suitable for Jinja2 template rendering."""
        return {
            "service_image": self.service_image,
            "service_port": self.service_port,
            "service_host": self.service_host,
            "service_name": self.service_name,
            "app_version": self.service_version,
            "restart_policy": self.restart_policy,
            "nginx_image": self.nginx_image,
            "nginx_port": self.nginx_port,
            "proxy_timeout": self.proxy_timeout,
            "contact": self.contact,
            "network_name": self.network_name,
            "network_driver": self.network_driver,
            "container_app_name": self.container_app_name,
            "container_nginx_name": self.container_nginx_name,
            "container_opa_name": self.container_opa_name,
            "mode": self.mode,
        }


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------
def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """
    Load and parse the manifest file.
    Path is resolved from SWIFTDEPLOY_MANIFEST env var in the caller.
    Exits with a clear message if missing or malformed.
    """
    if not manifest_path.exists():
        print(
            f"[ERROR] Manifest not found at: {manifest_path}\n"
            f"  If your file is named differently, set SWIFTDEPLOY_MANIFEST in .env\n"
            f"  e.g. SWIFTDEPLOY_MANIFEST=manifest.yml",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with manifest_path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(f"[ERROR] Manifest is not valid YAML:\n  {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print(
            "[ERROR] Manifest must be a YAML mapping at the top level.",
            file=sys.stderr,
        )
        sys.exit(1)

    return data


# ---------------------------------------------------------------------------
# Flag parser
# ---------------------------------------------------------------------------
def parse_flag_overrides(args: list[str]) -> dict[str, Any]:
    """
    Parse dotted --key=value flags from CLI args into a nested dict.

      --nginx.port=9090        → {"nginx": {"port": 9090}}
      --services.version=2.0   → {"services": {"version": "2.0"}}
      --mode=canary            → {"mode": "canary"}
    """
    overrides: dict[str, Any] = {}
    for arg in args:
        if not arg.startswith("--") or "=" not in arg:
            continue
        key, _, value_str = arg[2:].partition("=")
        value = _coerce(value_str)
        parts = key.split(".")
        target = overrides
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return overrides


def _coerce(value: str) -> Any:
    if value.lstrip("-").isdigit():
        return int(value)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------
def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Values in override win."""
    result = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# env → nested dict
# Converts flat env vars (SERVICE_PORT=3000) into the nested structure
# used by the manifest so they can participate in deep_merge.
# ---------------------------------------------------------------------------
def _env_to_manifest_shape(env: dict[str, str]) -> dict[str, Any]:
    """
    Map resolved env vars into the same nested shape as manifest.yaml.
    Only known keys are mapped — unknown env vars are ignored.
    """

    def _int(v: str) -> int:
        try:
            return int(v)
        except ValueError:
            return v  # type: ignore[return-value]

    return {
        "services": {
            "image": env.get("SERVICE_IMAGE"),
            "port": _int(env.get("SERVICE_PORT", "3000")),
            "version": env.get("SERVICE_VERSION"),
            "restart": env.get("RESTART_POLICY"),
        },
        "nginx": {
            "image": env.get("NGINX_IMAGE"),
            "port": _int(env.get("NGINX_PORT", "8080")),
            "proxy_timeout": _int(env.get("NGINX_PROXY_TIMEOUT", "30")),
            "contact": env.get("NGINX_CONTACT"),
        },
        "network": {
            "name": env.get("NETWORK_NAME"),
            "driver_type": env.get("NETWORK_DRIVER"),
        },
        "mode": env.get("MODE"),
    }


# ---------------------------------------------------------------------------
# Main resolution function
# ---------------------------------------------------------------------------
def resolve(manifest_path: Path, extra_args: list[str] | None = None) -> ResolvedConfig:
    """
    Resolve the final configuration by merging four layers:

      Layer 1 (lowest):  REQUIRED_ENV defaults from cli/env.py
      Layer 2:           .env file / shell environment vars
      Layer 3:           manifest.yaml values
      Layer 4 (highest): CLI flag overrides (--key=value)

    Args:
        manifest_path: Path to the manifest file (from SWIFTDEPLOY_MANIFEST)
        extra_args:    Raw CLI args for --flag=value parsing

    Returns:
        A fully resolved ResolvedConfig with no missing fields.
    """
    # Layer 1 + 2: env defaults merged with actual environment
    env_resolved = resolve_required()
    env_as_manifest = _env_to_manifest_shape(env_resolved)

    # Layer 3: manifest (wins over env)
    manifest_raw = load_manifest(manifest_path)
    merged = deep_merge(env_as_manifest, manifest_raw)

    # Layer 4: CLI flags (wins over everything)
    flag_overrides = parse_flag_overrides(extra_args or [])
    merged = deep_merge(merged, flag_overrides)

    svc = merged.get("services", {})
    ngx = merged.get("nginx", {})
    net = merged.get("network", {})

    return ResolvedConfig(
        service_image=str(svc.get("image")),
        service_port=int(svc.get("port")),
        service_version=str(svc.get("version")),
        restart_policy=str(svc.get("restart")),
        nginx_image=str(ngx.get("image")),
        nginx_port=int(ngx.get("port")),
        proxy_timeout=int(ngx.get("proxy_timeout")),
        contact=str(ngx.get("contact")),
        network_name=str(net.get("name")),
        network_driver=str(net.get("driver_type")),
        mode=str(merged.get("mode")),
        container_app_name=str(
            env_resolved.get("CONTAINER_APP_NAME", "nockk-swiftdeploy-v1")
        ),
        container_nginx_name=str(
            env_resolved.get("CONTAINER_NGINX_NAME", "nockk-nginx-v1")
        ),
        container_opa_name=str(
            env_resolved.get("CONTAINER_OPA_NAME", "swiftdeploy-opa")
        ),
    )


# ---------------------------------------------------------------------------
# Required field check — runs against raw manifest only.
# Env defaults do NOT mask missing manifest fields here — that's intentional.
# validate surfaces them so the operator knows to add them to the manifest.
# ---------------------------------------------------------------------------
def check_required_fields(manifest_raw: dict) -> list[str]:
    """Return error messages for missing/empty required manifest fields."""
    errors = []
    for path in REQUIRED_FIELDS:
        node = manifest_raw
        for part in path:
            if not isinstance(node, dict) or part not in node:
                errors.append(f"Missing required field: {'.'.join(path)}")
                break
            node = node[part]
        else:
            if node is None or node == "":
                errors.append(f"Required field is empty: {'.'.join(path)}")
    return errors
