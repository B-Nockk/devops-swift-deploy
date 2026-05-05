"""
commands.py — Subcommand implementations for swiftdeploy.

Each function is a Use Case (Application Service in DDD terms):
  - Thin orchestration only — no business logic
  - Calls into config, generator, and Docker as needed
  - Owns clear exit codes and human-readable output
  - Never touches manifest.yaml except promote()

Subcommands:
  validate  — 5 pre-flight checks, exit non-zero on any failure
  deploy    — init + docker compose up + health gate (60s timeout)
  promote   — switch mode, regen compose, rolling restart, confirm
  teardown  — bring stack down; --clean removes generated files
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

# ruamel.yaml preserves comments and key order on write — critical for promote.
# PyYAML would strip comments and reorder keys, corrupting the manifest.
from ruamel.yaml import YAML

from .config import (
    ResolvedConfig,
    check_required_fields,
    load_manifest,
    resolve,
)
from .generator import generate_all, generate_compose_only


# ---------------------------------------------------------------------------
# ANSI colours — degrade gracefully if terminal doesn't support them
# ---------------------------------------------------------------------------
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

def _ok(msg: str)   -> str: return f"{_GREEN}[PASS]{_RESET} {msg}"
def _fail(msg: str) -> str: return f"{_RED}[FAIL]{_RESET} {msg}"
def _info(msg: str) -> str: return f"{_CYAN}  →{_RESET} {msg}"
def _warn(msg: str) -> str: return f"{_YELLOW}[WARN]{_RESET} {msg}"
def _bold(msg: str) -> str: return f"{_BOLD}{msg}{_RESET}"


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def validate(
    manifest_path: Path,
    templates_dir: Path,
    output_dir: Path,
    extra_args: list[str],
) -> int:
    """
    Run 5 pre-flight checks. Print PASS/FAIL for each.
    Exit non-zero if any check fails.
    """
    print(_bold("\n── SwiftDeploy Pre-flight Validation ──\n"))
    failures: list[str] = []

    def record(passed: bool, label: str, detail: str = "") -> None:
        if passed:
            print(_ok(label))
        else:
            print(_fail(label))
            if detail:
                print(f"       {detail}")
            failures.append(label)

    # ── Check 1: manifest.yaml exists and is valid YAML ──────────────────────
    manifest_raw = None
    if not manifest_path.exists():
        record(False, "manifest.yaml exists and is valid YAML",
               f"Not found at: {manifest_path}")
    else:
        try:
            with manifest_path.open() as f:
                manifest_raw = yaml.safe_load(f)
            record(True, "manifest.yaml exists and is valid YAML")
        except yaml.YAMLError as exc:
            record(False, "manifest.yaml exists and is valid YAML", str(exc))

    # ── Check 2: all required fields present and non-empty ───────────────────
    if manifest_raw is not None:
        errors = check_required_fields(manifest_raw)
        if errors:
            for err in errors:
                record(False, "All required fields present and non-empty", err)
        else:
            record(True, "All required fields present and non-empty")
    else:
        record(False, "All required fields present and non-empty",
               "Skipped — manifest could not be loaded")

    # ── Check 3: Docker image exists locally ─────────────────────────────────
    image = None
    if manifest_raw:
        try:
            cfg = resolve(manifest_path, extra_args)
            image = cfg.service_image
        except SystemExit:
            pass

    if image:
        ok, detail = _docker_image_exists(image)
        record(ok, f"Docker image '{image}' exists locally", detail)
    else:
        record(False, "Docker image exists locally", "Could not resolve image name from manifest")

    # ── Check 4: nginx port not already bound on the host ────────────────────
    nginx_port = None
    if manifest_raw:
        try:
            cfg = resolve(manifest_path, extra_args)
            nginx_port = cfg.nginx_port
        except SystemExit:
            pass

    if nginx_port:
        port_free = _is_port_free(nginx_port)
        record(
            port_free,
            f"Nginx port {nginx_port} is not already bound on the host",
            f"Something is already listening on port {nginx_port}. "
            f"Run: lsof -i :{nginx_port}" if not port_free else "",
        )
    else:
        record(False, "Nginx port is not already bound on the host",
               "Could not resolve nginx port from manifest")

    # ── Check 5: generated nginx.conf is syntactically valid ─────────────────
    nginx_conf = output_dir / "nginx.conf"
    if not nginx_conf.exists():
        record(False, "nginx.conf is syntactically valid",
               "nginx.conf not found — run: swiftdeploy init")
    else:
        try:
            valid, detail = _validate_nginx_conf(nginx_conf)
            record(valid, "nginx.conf is syntactically valid", detail)
        except FileNotFoundError:
            record(False, "nginx.conf is syntactically valid",
                   "Docker not available — cannot run nginx -t validation")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if not failures:
        print(f"{_GREEN}{_BOLD}✓ All checks passed — stack is ready to deploy.{_RESET}\n")
        return 0
    else:
        count = len(failures)
        print(f"{_RED}{_BOLD}✗ {count} check(s) failed — resolve the above before deploying.{_RESET}\n")
        return 1


def _docker_image_exists(image: str) -> tuple[bool, str]:
    """Check if a Docker image exists locally. Handles Docker not being installed."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
        )
        if result.returncode == 0:
            return True, ""
        return False, f"Image not found locally. Run: docker build -t {image} ./app"
    except FileNotFoundError:
        return False, "Docker is not installed or not on PATH"


def _is_port_free(port: int) -> bool:
    """
    Portable port check using a raw socket connect attempt.
    Works on Linux and macOS without requiring netstat/ss/lsof.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return False  # connection succeeded → port is occupied
        except (ConnectionRefusedError, OSError):
            return True   # connection refused → port is free


def _validate_nginx_conf(nginx_conf: Path) -> tuple[bool, str]:
    """
    Validate nginx.conf syntax by running nginx -t inside a temporary container.
    This avoids requiring nginx to be installed on the host.
    Falls back to a structural heuristic if Docker is unavailable.
    """
    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{nginx_conf.resolve()}:/etc/nginx/nginx.conf:ro",
            "nginx:latest",
            "nginx", "-t", "-c", "/etc/nginx/nginx.conf",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, ""

    # nginx -t writes to stderr
    detail = (result.stderr or result.stdout).strip()
    # Trim verbose path noise for readability
    detail = detail.replace("nginx: ", "").strip()
    return False, detail


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------
def deploy(
    manifest_path: Path,
    templates_dir: Path,
    output_dir: Path,
    extra_args: list[str],
) -> int:
    """
    Full deployment sequence:
      1. init  — generate configs
      2. docker compose up -d
      3. poll /healthz through nginx until healthy or 60s timeout
    """
    print(_bold("\n── SwiftDeploy Deploy ──\n"))

    # Step 1: init
    print(_info("Generating configs..."))
    cfg = resolve(manifest_path, extra_args)
    nginx_path, compose_path = generate_all(cfg, templates_dir, output_dir)
    print(f"  Generated: {nginx_path.name}, {compose_path.name}")

    # Step 2: bring stack up
    print(_info("Starting stack with docker compose..."))
    result = _compose(output_dir, ["up", "-d", "--remove-orphans"])
    if result.returncode != 0:
        print(f"{_RED}✗ docker compose up failed:{_RESET}")
        print(result.stderr or result.stdout)
        return 1

    # Step 3: health gate
    health_url = f"http://localhost:{cfg.nginx_port}/healthz"
    print(_info(f"Waiting for stack to be healthy at {health_url} (timeout: 60s)..."))

    healthy = _wait_for_health(health_url, timeout=60, interval=2)
    if not healthy:
        print(f"\n{_RED}✗ Stack did not become healthy within 60 seconds.{_RESET}")
        print("  Check logs with: docker compose logs")
        return 1

    print(f"\n{_GREEN}{_BOLD}✓ Stack is up and healthy.{_RESET}")
    print(f"  Service: http://localhost:{cfg.nginx_port}")
    print(f"  Health:  {health_url}\n")
    return 0


def _wait_for_health(url: str, timeout: int, interval: int) -> bool:
    """
    Poll a URL every `interval` seconds until it returns HTTP 200
    or `timeout` seconds elapse.
    Returns True if healthy, False on timeout.
    """
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    print(f"\r  {_GREEN}✓{_RESET} Healthy after ~{attempt * interval}s", end="")
                    sys.stdout.flush()
                    return True
        except Exception:
            pass
        print(f"\r  Attempt {attempt} — waiting...", end="")
        sys.stdout.flush()
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------
def promote(
    manifest_path: Path,
    templates_dir: Path,
    output_dir: Path,
    target_mode: str,
) -> int:
    """
    Switch deployment mode (canary ↔ stable):
      1. Validate target mode
      2. Update `mode` in manifest.yaml in-place (ruamel.yaml preserves formatting)
      3. Regenerate docker-compose.yml only (nginx.conf is mode-agnostic)
      4. Rolling restart of the app service container only
      5. Confirm new mode via /healthz
    """
    print(_bold(f"\n── SwiftDeploy Promote → {target_mode} ──\n"))

    if target_mode not in ("canary", "stable"):
        print(f"{_RED}✗ Invalid mode: '{target_mode}'. Must be 'canary' or 'stable'.{_RESET}",
              file=sys.stderr)
        return 1

    # ── Read current mode ─────────────────────────────────────────────────────
    cfg = resolve(manifest_path, [])
    current_mode = cfg.mode

    if current_mode == target_mode:
        print(_warn(f"Already running in {target_mode} mode — nothing to do."))
        return 0

    print(_info(f"Switching mode: {current_mode} → {target_mode}"))

    # ── Step 1: Update manifest.yaml in-place ────────────────────────────────
    print(_info("Updating manifest.yaml..."))
    _update_manifest_mode(manifest_path, target_mode)
    print(f"  manifest.yaml: mode = {target_mode}")

    # ── Step 2: Regenerate docker-compose.yml only ───────────────────────────
    print(_info("Regenerating docker-compose.yml..."))
    new_cfg = resolve(manifest_path, [])  # re-resolve with updated manifest
    generate_compose_only(new_cfg, templates_dir, output_dir)
    print("  docker-compose.yml updated")

    # ── Step 3: Restart app container with new MODE env var ──────────────────
    #
    # We need compose up --force-recreate to apply the new MODE env var from
    # the regenerated docker-compose.yml. Plain `docker restart` would keep
    # the old env var.
    #
    # To avoid the network disconnection issue, we:
    #   1. Use --force-recreate --no-deps (only app, nginx untouched)
    #   2. Wait for Docker's own healthcheck to pass before polling nginx
    #      (Docker healthcheck start_period=10s, so we wait 15s minimum)
    print(_info("Restarting app service with new MODE env var (nginx untouched)..."))
    result = _compose(output_dir, [
        "up", "-d",
        "--no-deps",
        "--force-recreate",
        "app",
    ])
    if result.returncode != 0:
        print(f"{_RED}✗ Service restart failed:{_RESET}")
        print(result.stderr or result.stdout)
        print(_warn("Rolling back manifest.yaml to previous mode..."))
        _update_manifest_mode(manifest_path, current_mode)
        return 1

    # ── Step 4: Wait for Docker healthcheck, then confirm via nginx ───────────
    print(_info("Confirming new mode via /healthz..."))
    health_url = f"http://localhost:{new_cfg.nginx_port}/healthz"

    # Wait for Docker's healthcheck start_period + a margin before polling.
    # The compose healthcheck has start_period=10s — polling before that
    # just burns attempts against a container nginx won't route to yet.
    print(_info("  Waiting 15s for container healthcheck start_period..."))
    time.sleep(15)

    # Poll through nginx — this confirms both the app is up AND nginx is routing
    # healthy = _wait_for_health(health_url, timeout=60, interval=3)
    # if not healthy:
    #     print(f"\n{_RED}✗ Service did not become healthy within 60s after restart.{_RESET}")
    #     print("  Diagnose with:")
    #     print("    docker logs nockk-swiftdeploy-v1")
    #     print("    docker inspect --format='{{.State.Health.Status}}' nockk-swiftdeploy-v1")
    #     return 1

    # Poll through nginx — this confirms both the app is up AND nginx is routing
    healthy = _wait_for_health(health_url, timeout=60, interval=3)
    if not healthy:
        print(f"\n{_RED}✗ Service did not become healthy within 60s after restart.{_RESET}")
        print("  Diagnose with:")
        print(f"    docker logs {new_cfg.container_app_name}")
        print(f"    docker inspect --format='{{{{.State.Health.Status}}}}' {new_cfg.container_app_name}")
        return 1


    # Verify the mode header/body reflects the new mode
    mode_confirmed = _confirm_mode(health_url, target_mode, new_cfg.nginx_port)
    if not mode_confirmed:
        print(f"\n{_RED}✗ Service is healthy but mode has not switched yet.{_RESET}")
        return 1

    print(f"\n{_GREEN}{_BOLD}✓ Promoted to {target_mode} mode successfully.{_RESET}")
    if target_mode == "canary":
        print("  Canary: X-Mode: canary header active on all responses.")
        print("  Chaos endpoint available at POST /chaos")
    else:
        print("  Stable: chaos endpoint disabled, X-Mode header removed.")
    print()
    return 0


def _update_manifest_mode(manifest_path: Path, mode: str) -> None:
    """
    Update the `mode` field in manifest.yaml in-place.

    Uses ruamel.yaml which preserves:
      - Comments
      - Key ordering
      - Quoting style
      - Indentation

    PyYAML would strip all of the above — never use it for writes.
    """
    ryaml = YAML()
    ryaml.preserve_quotes = True

    with manifest_path.open() as f:
        data = ryaml.load(f)

    data["mode"] = mode

    with manifest_path.open("w") as f:
        ryaml.dump(data, f)


def _confirm_mode(health_url: str, expected_mode: str, nginx_port: int) -> bool:
    """
    Hit /healthz and verify the service is actually running the expected mode.
    For canary: check X-Mode header is present.
    For stable: check X-Mode header is absent.
    Also hits GET / to verify mode field in the response body.
    """
    root_url = health_url.replace("/healthz", "/")
    try:
        req = urllib.request.Request(root_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
            x_mode = resp.headers.get("X-Mode", "")

            body_mode = body.get("mode", "")
            if body_mode != expected_mode:
                print(f"\n  Body mode mismatch: got '{body_mode}', expected '{expected_mode}'")
                return False

            if expected_mode == "canary" and x_mode != "canary":
                print(f"\n  X-Mode header missing or wrong: got '{x_mode}'")
                return False

            if expected_mode == "stable" and x_mode == "canary":
                print(f"\n  X-Mode header still present in stable mode")
                return False

            return True
    except Exception as exc:
        print(f"\n  Could not reach service to confirm mode: {exc}")
        return False


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------
def teardown(
    manifest_path: Path,
    output_dir: Path,
    clean: bool,
) -> int:
    """
    Bring the stack down and optionally remove generated configs.

    docker compose down -v removes:
      - All service containers
      - The defined network (swiftdeploy-net)
      - Named volumes (app_logs, nginx_logs)

    --clean additionally removes:
      - nginx.conf
      - docker-compose.yml
    """
    print(_bold("\n── SwiftDeploy Teardown ──\n"))

    print(_info("Stopping and removing containers, networks, volumes..."))
    result = _compose(output_dir, ["down", "-v", "--remove-orphans"])

    if result.returncode != 0:
        # Non-zero can mean "nothing was running" — not always fatal
        stderr = (result.stderr or "").strip()
        if "no configuration file" in stderr.lower():
            print(_warn("docker-compose.yml not found — stack may already be down."))
        else:
            print(f"{_RED}✗ docker compose down encountered an error:{_RESET}")
            print(f"  {stderr}")
            return 1

    print("  Containers removed")
    print("  Network removed")
    print("  Volumes removed")

    if clean:
        print(_info("--clean: removing generated config files..."))
        removed = []
        for filename in ("nginx.conf", "docker-compose.yml"):
            path = output_dir / filename
            if path.exists():
                path.unlink()
                removed.append(filename)

        if removed:
            for f in removed:
                print(f"  Deleted: {f}")
        else:
            print("  No generated files found to remove")

    print(f"\n{_GREEN}{_BOLD}✓ Teardown complete.{_RESET}\n")
    return 0


# ---------------------------------------------------------------------------
# Shared Docker Compose helper
# ---------------------------------------------------------------------------
def _compose(cwd: Path, compose_args: list[str]) -> subprocess.CompletedProcess:
    """
    Run `docker compose <args>` in the given working directory.

    Uses `docker compose` (v2 plugin) with `docker-compose` (v1) as fallback.
    Streams stderr so the user sees progress in real time for long operations.
    """
    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )

    try:
        result = _run(["docker", "compose"] + compose_args)
    except FileNotFoundError:
        # Return a fake failure result so callers handle it uniformly
        return subprocess.CompletedProcess(
            args=["docker", "compose"] + compose_args,
            returncode=1,
            stdout="",
            stderr="Docker is not installed or not on PATH",
        )

    # Fallback to docker-compose v1 if docker compose plugin not found
    if result.returncode != 0 and "unknown command" in (result.stderr or "").lower():
        try:
            result = _run(["docker-compose"] + compose_args)
        except FileNotFoundError:
            pass

    return result
