# /cli/env.py
"""
env.py — Environment variable resolution for SwiftDeploy.

Design principles:
  - Required vars: fail fast with a clear message if missing and no default provided
  - Optional vars: return a default silently if missing
  - Deduplication: if a key appears multiple times in the env (e.g. from shell +
    .env file), only the first resolved value is used — no ambiguity
  - .env loading: called once at startup in swiftdeploy; idempotent if called again

Required vars with defaults (the REQUIRED_ENV list):
  These must be present at runtime. If absent from the environment, the listed
  default is used rather than failing — this covers the minimum viable config
  described in the project spec. Any key in this list appearing multiple times
  in the environment resolves to the first value found.

Non-required vars:
  Use get_env(key, default=None). If not set and no default given, returns None.
  Use require_env(key) for vars you want to fail fast on with no default.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Required env vars with defaults
# These match the minimum manifest requirements from the spec.
# Changing a value here changes the fallback for all callers.
# ---------------------------------------------------------------------------
REQUIRED_ENV: dict[str, str] = {
    # Paths
    "SWIFTDEPLOY_MANIFEST":  "manifest.yaml",
    "SWIFTDEPLOY_TEMPLATES": "templates",
    "SWIFTDEPLOY_OUTPUT":    ".",

    # Service
    "SERVICE_IMAGE":   "swift-deploy-1-node:latest",
    "SERVICE_PORT":    "3000",
    "SERVICE_VERSION": "0.0.1",
    "RESTART_POLICY":  "unless-stopped",

    # Nginx
    "NGINX_IMAGE":         "nginx:latest",
    "NGINX_PORT":          "8080",
    "NGINX_PROXY_TIMEOUT": "30",
    "NGINX_CONTACT":       "ops@swiftdeploy.internal",

    # Network
    "NETWORK_NAME":   "swiftdeploy-net",
    "NETWORK_DRIVER": "bridge",

    # container named
    "CONTAINER_APP_NAME":   "nockk-swiftdeploy-v1",
    "CONTAINER_NGINX_NAME": "nockk-nginx-v1",

    # Runtime
    "MODE": "stable",
}


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------
def load_dotenv(env_path: Path | None = None) -> None:
    """
    Load a .env file into os.environ.

    Rules:
    - Lines starting with # are comments — skipped
    - Blank lines are skipped
    - KEY=VALUE and KEY="VALUE" and KEY='VALUE' are all supported
    - If a key is already set in the environment, it is NOT overwritten
      (shell environment takes priority over .env file)
    - Duplicate keys within the .env file: first occurrence wins
    - If the file doesn't exist, this is a no-op (not an error)

    Args:
        env_path: Path to the .env file. Defaults to ./.env
    """
    path = env_path or Path(".env")
    if not path.exists():
        return

    seen: set[str] = set()

    with path.open() as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()

            # Skip comments and blank lines
            if not line or line.startswith("#"):
                continue

            if "=" not in line:
                continue

            key, _, raw_value = line.partition("=")
            key = key.strip()
            value = raw_value.strip().strip("'\"")  # strip quotes

            if not key:
                continue

            # Deduplication: first occurrence in file wins
            if key in seen:
                continue
            seen.add(key)

            # Shell environment takes priority over .env
            if key not in os.environ:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Resolution functions
# ---------------------------------------------------------------------------
def resolve_required() -> dict[str, str]:
    """
    Resolve all REQUIRED_ENV vars.

    For each key:
      1. Use the value from os.environ if present (set by shell or .env)
      2. Fall back to the default in REQUIRED_ENV
      3. Deduplicate: only one value per key regardless of source

    Returns a flat dict of all resolved required vars.
    Never raises — required vars always have a default.
    """
    resolved: dict[str, str] = {}
    for key, default in REQUIRED_ENV.items():
        resolved[key] = os.environ.get(key, default)
    return resolved


def require_env(key: str) -> str:
    """
    Get a required env var with NO default — fail fast if missing.

    Use this for vars you explicitly do not want to have a default,
    where the absence is always an error (e.g. secrets, external endpoints).

    Args:
        key: Environment variable name

    Returns:
        The value as a string

    Raises:
        SystemExit with a clear message if the key is not set
    """
    value = os.environ.get(key)
    if value is None:
        print(
            f"\n[ERROR] Required environment variable not set: {key}\n"
            f"  Add it to your .env file or export it in your shell.\n"
            f"  See .env.example for reference.\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def get_env(key: str, default: str | None = None) -> str | None:
    """
    Get an optional env var. Returns default (None by default) if not set.
    No failure — purely optional.
    """
    return os.environ.get(key, default)
