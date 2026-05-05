# SwiftDeploy

SwiftDeploy is a declarative infrastructure-as-code CLI tool and self-contained Go application stack. It parses a single `manifest.yaml` source of truth to programmatically generate Nginx and Docker Compose configurations, managing the entire lifecycle of a containerized application with zero-downtime canary deployments.

## Table of Contents
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Detailed Setup](#detailed-setup)
- [Configuration](#configuration)
- [CLI Commands](#cli-commands)
- [Architecture](#architecture)

## Prerequisites
- **Docker & Docker Compose** (Ensure the Docker daemon is running)
- **Python 3.10+**
- **Make** (Required for the automated setup and deployment commands)
- **Go 1.22+** (Only required for local development; Docker handles compilation otherwise)

## Quick Start
The fastest way to get the stack running using the isolated virtual environment approach:

```bash
# 1. Clone the repository and enter the directory
git clone https://github.com/B-Nockk/devops-swift-deploy.git swift-deploy
cd swift-deploy

# 2. Copy the environment variables template
cp .env.example .env

# 3. Setup the virtual environment, install dependencies, and build the Docker image
make setup-venv

# 4. Generate configs, validate the stack, and deploy
make deploy
```
*Stack is now live at `http://localhost:8080`!*

## Detailed Setup

<details>
<summary><strong>Option A: Setup via Makefile (Recommended)</strong></summary>

The `Makefile` abstracts the setup process. It automatically detects if a virtual environment is active and routes Python commands accordingly.

```bash
# Option 1: Isolated Venv Install (Creates ./venv and installs deps there)
make setup-venv

# Option 2: System/User Install (Installs deps to your global/user Python)
make setup

# After running either setup command, deploy the stack:
make deploy
```
</details>

<details>
<summary><strong>Option B: Manual Setup (Without Make)</strong></summary>

If you prefer to run commands manually or do not have `make` installed:

```bash
# 1. Create and activate a Python virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install CLI dependencies (PyYAML, Jinja2, ruamel.yaml)
pip install -r requirements.txt

# 3. Make the CLI executable
chmod +x ./swiftdeploy

# 4. Build the application Docker image
docker build -t swift-deploy-1-node:latest -f Dockerfile .

# 5. Initialize, validate, and deploy the stack
./swiftdeploy deploy
```
</details>

## Configuration

SwiftDeploy relies on two main configuration files:

1. **`manifest.yaml`**: The absolute source of truth for the stack. Do not manually edit the generated `nginx.conf` or `docker-compose.yml`—update the manifest instead.
2. **`.env`**: Local environment overrides. Copy `.env.example` to `.env` to customize paths, ports, and image names. *Note: Shell environment variables take priority over `.env` values.*

## CLI Commands

The `swiftdeploy` CLI manages the stack. If you used `make setup-venv`, `make` will automatically use the correct Python binary without needing manual activation.

*   `make init` - Generates `nginx.conf` and `docker-compose.yml` from `manifest.yaml`.
*   `make validate` - Runs 5 pre-flight checks (manifest syntax, required fields, local image existence, port availability, Nginx syntax).
*   `make deploy` - Runs init, starts the stack, and blocks until health checks pass.
*   `make promote-canary` - Switches the stack to canary mode (activates `/chaos` endpoint and `X-Mode` headers) via rolling restart.
*   `make promote-stable` - Switches the stack back to stable mode.
*   `make teardown` - Stops and removes all containers, networks, and volumes.
*   `make clean` - Runs teardown and deletes generated configuration files.

*For a full list of observability and testing commands, run `make help`.*

## Architecture
*   **Reverse Proxy:** Nginx (alpine), strictly handling external traffic on port 8080.
*   **Service:** Go 1.22 statically compiled binary running on Alpine 3.19 (distroless/minimal footprint). 
*   **Security:** Containers run as non-root users (`uid 1001`) with dropped Linux capabilities (`cap_drop: ALL`).
```
