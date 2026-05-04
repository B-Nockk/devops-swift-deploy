from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#! should be gotten from env
DEFAULTS: dict[str, Any] = {
    "services": {
        "image": "swift-deploy-1-node:latest",
        "port": 3000,
        "version": "0.0.1",
        "restart": "unless-stopped",
    },
    "nginx": {
        "image": "nginx:latest",
        "port": 8080,
        "proxy_timeout": 30,
        "contact": "ops@swiftdeploy.internal",
    },
    "network": {
        "name": "swiftdeploy-net",
        "driver_type": "bridge",
    },
    "mode": "stable",
}

#! should be gotten from env
REQUIRED_FIELDS: list[tuple[str, ...]] = [
    ("services", "image"),
    ("services", "port"),
    ("nginx", "image"),
    ("nginx", "port"),
    ("network", "name"),
    ("network", "driver_type"),
    ("mode",),
]


@dataclass
class ResolvedConfig:
    service_image: str
    service_port: int
    service_version: str
    restart_policy: str

    nginx_image: str
    nginx_port: int
    proxy_timeout: int
    contact: str

    network_name: str
    network_driver: str

    mode: str

    service_host: str = "app"
    service_name: str = "swiftdeploy-app"

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
            "mode": self.mode,
        }


def _coerce(value: str) -> Any:
    """Convert a string CLI value to the most appropriate Python type."""
    if value.lstrip("-").isdigit():
        return int(value)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge `override` into `base`.
    Values in `override` win. Neither input is mutated.
    """
    result = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """
    Load and parse manifest.yaml.

    Returns:
       - the raw parsed dict;
       - does not validate required fields here —
       - that's validate's job.

    Raises:
        - SystemExit with a clear message if the file is missing or malformed.
    """

    if not manifest_path.exists():
        print(f"[ERROR] manifest.yaml not found at: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with manifest_path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(f"[ERROR] manifest.yaml is not valid YAML:\n  {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print(
            "[ERROR] manifest.yaml must be a YAML mapping at the top level.",
            file=sys.stderr,
        )

        sys.exit(1)

    return data


def parse_flag_overrides(args: list[str]) -> dict[str, Any]:
    """
    Parse dotted --key=value flags from CLI args into a nested dict.

    Examples:
      --nginx.port=9090          → {"nginx": {"port": 9090}}
      --nginx.contact=x@y.com    → {"nginx": {"contact": "x@y.com"}}
      --services.port=4000       → {"services": {"port": 4000}}
      --mode=canary              → {"mode": "canary"}
      --network.name=mynet       → {"network": {"name": "mynet"}}

    Type coercion:
      Integers are detected by str.isdigit().
      Floats are NOT coerced (all manifest numeric values are ints).
      Everything else stays a string.
    """
    overrides: dict[str, Any] = {}

    for arg in args:
        if not arg.startswith("--"):
            continue

        arg = arg[2:]
        if "=" not in arg:
            continue

        key, _, value_str = arg.partition("=")
        value = _coerce(value_str)

        parts = key.split(".")
        target = overrides
        for part in parts[:-1]:
            target = target.setdefault(part, {})

        target[parts[-1]] = value

    return overrides


def resolve(manifest_path: Path, extra_args: list[str] | None = None) -> ResolvedConfig:
    """
    Resolve the final configuration by merging:
      DEFAULTS  ←  manifest.yaml  ←  CLI flag overrides

    Args:
        manifest_path: Path to manifest.yaml
        extra_args:    Raw CLI args to scan for --key=value overrides.
                       Pass sys.argv[2:] from your subcommand handler.

    Returns:
        A fully resolved ResolvedConfig with no missing fields.
    """
    manifest_raw = load_manifest(manifest_path)
    flag_overrides = parse_flag_overrides(extra_args or [])

    # Layer 1: start from hardcoded defaults
    merged = deep_merge(DEFAULTS, manifest_raw)
    # Layer 2: apply CLI flag overrides (highest priority)
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
    )


def check_required_fields(manifest_raw: dict) -> list[str]:
    """
    Validates required fields against the RAW manifest (not merged config).

    - catch missing manifest fields specifically, not mask them with defaults.
    - The validate subcommand calls this directly.

    Returns:
       - a list of error messages for any required field that is:
            - missing
            - or empty in the raw manifest.

        - Empty list = all fields present.
    """
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
