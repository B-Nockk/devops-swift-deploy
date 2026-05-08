# /Makefile
# SwiftDeploy — project automation
# Usage: make <target>  |  make help

# ============================================================
# Configuration
# ============================================================
# IMAGE_NAME   := swift-deploy-1-node
NGINX_CONTAINER_IMAGE  	:= nockk-nginx-v1
NGINX_LOG_FILE			:= hng-access.log
HOST_URI				:= http://localhost:8080
IMAGE_NAME   			:= nockk-swift-deploy
IMAGE_TAG    			:= latest
APP_DIR      			:= ./app
CLI          			:= ./swiftdeploy

# ── Python / venv detection ───────────────────────────────────────────────────
# If a venv exists at ./venv, use its Python and pip binaries directly by path.
# This works whether or not the venv is activated in the current shell —
# we never rely on `source activate` because each Make recipe runs in its
# own subshell and activation would not carry across lines.
#
# Priority:
#   1. ./venv  (created by make setup-venv)
#   2. system python3 / pip3  (used by make setup)
#
# Result: `make deploy` works correctly after EITHER setup path, no manual
# activation required.
ifneq ($(wildcard venv/bin/python3),)
  PYTHON := venv/bin/python3
  PIP    := venv/bin/pip
else
  PYTHON := python3
  PIP    := pip3
endif

BOLD  := \033[1m
RESET := \033[0m
GREEN := \033[32m
CYAN  := \033[36m
YELLOW := \033[33m

.DEFAULT_GOAL := help

# ============================================================
# Help
# ============================================================
.PHONY: help
help:
	@echo ""
	@echo "$(BOLD)SwiftDeploy$(RESET) — available commands"
	@echo ""
	@echo "$(CYAN)Setup$(RESET)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "setup"           "System install: deps + chmod + build image (no venv)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "setup-venv"      "Venv install: create venv + deps + chmod + build image"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "deps"            "Install Python deps only (into active env)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "build"           "Build the app Docker image"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "build-no-cache"  "Build the app Docker image with no layer cache"
	@echo ""
	@echo "$(CYAN)Stack lifecycle$(RESET)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "init"            "Generate nginx.conf and docker-compose.yml from manifest.yaml"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "validate"        "Run 5 pre-flight checks (manifest, image, port, nginx syntax)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "deploy"          "Init + bring stack up + wait until healthy (60s timeout)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "promote-canary"  "Switch to canary mode (X-Mode header + /chaos endpoint active)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "promote-stable"  "Switch back to stable mode"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "teardown"        "Stop and remove all containers, networks, and volumes"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "clean"           "Teardown + delete generated config files"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "rebuild"         "Teardown -> rebuild image -> deploy"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "reset"           "Clean -> rebuild image -> deploy (full reset)"
	@echo ""
	@echo "$(CYAN)Observability$(RESET)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "status"          "Show running containers and health status"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "logs"            "Tail logs from all containers"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "logs-app"        "Tail logs from the app container only"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "logs-nginx"      "Tail logs from the nginx container only"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "logs-access"     "Tail the nginx access log (custom format)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "healthcheck"     "Hit /healthz and print response + HTTP status code"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "inspect"         "Print the fully resolved config from manifest.yaml"
	@echo ""
	@echo "$(CYAN)Observability & Policy$(RESET)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "metrics-status"  "Live terminal dashboard with OPA policy compliance"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "status-watch"    "Live dashboard with custom interval (make status-watch INTERVAL=10)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "audit"           "Generate audit_report.md from history.jsonl"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "policy-check"    "Run pre-deploy policy checks without deploying"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "dashboard"       "Print the browser dashboard URL"
	@echo ""
	@echo "$(CYAN)Smoke tests$(RESET)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "smoke"           "Hit /, /healthz and check headers (stack must be running)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "smoke-chaos"     "Test slow + error + recover chaos (must be in canary mode)"
	@echo ""
	@echo "$(CYAN)Development$(RESET)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "fmt"             "Format Go source code with gofmt"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "lint"            "Lint Go source code (requires golangci-lint)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "test"            "Run Go unit tests"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "test-cover"      "Run Go tests with HTML coverage report"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "shell-app"       "Open a shell inside the app container"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "shell-nginx"     "Open a shell inside the nginx container"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "verify"          "Verify stack is up: docker ps + /healthz + /api/dashboard/snapshot"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "traffic"         "Send 20 requests to the stack so dashboards have data"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "traffic-loop"    "Continuous traffic generator (Ctrl+C to stop)"

	@echo ""
	@echo "$(CYAN)Utilities$(RESET)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "ps"              "List all Docker containers (system-wide)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "image-size"      "Show the size of the built app image"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "prune"           "Remove unused Docker images, networks, volumes (system-wide)"
	@printf "  $(GREEN)%-22s$(RESET) %s\n" "which-python"    "Show which Python and pip are active (venv or system)"
	@echo ""
	@echo "$(YELLOW)Python env:$(RESET) $(PYTHON)"
	@echo ""

# ============================================================
# Setup
# ============================================================

# Shared install logic used by both setup paths
define install_deps
	$(PIP) install pyyaml jinja2 ruamel.yaml
endef

.PHONY: setup
setup: ## System install: deps + chmod + build image (no venv)
	@echo "Checking Python..."
	@python3 --version
	@echo "Installing Python dependencies..."
	@pip3 install -r requirements.txt --break-system-packages 2>/dev/null || \
	 pip3 install -r requirements.txt --user
	@echo "Making CLI executable..."
	chmod +x $(CLI)
	@echo "Checking for go.mod before building image..."
	@if [ ! -f app/go.mod ]; then \
		echo "go.mod not found — running go mod init in app/..."; \
		cd app && go mod init github.com/swiftdeploy/app 2>/dev/null || true; \
	fi
	@if command -v go >/dev/null 2>&1 && [ -f app/go.mod ]; then \
		echo "Running go mod tidy..."; \
		cd app && go mod tidy 2>/dev/null || true; \
	fi
	@echo "Building Docker image..."
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) -f Dockerfile . || \
		echo "Docker build failed. Run 'make build' to see detailed errors."
	@echo ""
	@echo "✓ SwiftDeploy is ready. Run 'make deploy' to start the stack."

.PHONY: setup-venv
setup-venv: ## Venv install: create venv + deps + chmod + build (no activation needed after)
	@echo "Checking for python3-venv..."
	@python3 -m venv --help >/dev/null 2>&1 || { \
		echo "python3-venv not found — attempting install..."; \
		sudo apt-get install -y python3-venv 2>/dev/null || \
		sudo apt-get install -y python-venv 2>/dev/null || \
		{ echo "[ERROR] Could not install python3-venv. Install manually and retry."; exit 1; }; \
	}
	@echo "Creating virtual environment at ./venv ..."
	python3 -m venv venv
	@echo "Upgrading pip..."
	venv/bin/pip install --upgrade pip -q
	@echo "Installing Python dependencies..."
	venv/bin/pip install -r requirements.txt
	@echo "Making CLI executable..."
	chmod +x $(CLI)
	@echo "Checking for go.mod before building image..."
	@if [ ! -f app/go.mod ]; then \
		echo "go.mod not found — running go mod init in app/..."; \
		cd app && go mod init github.com/swiftdeploy/app 2>/dev/null || true; \
	fi
	@if command -v go >/dev/null 2>&1 && [ -f app/go.mod ]; then \
		echo "Running go mod tidy..."; \
		cd app && go mod tidy 2>/dev/null || true; \
	fi
	@echo "Building Docker image..."
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) -f Dockerfile . || \
		echo "Docker build failed. Run 'make build' to see detailed errors."
	@echo ""
	@echo "✓ SwiftDeploy is ready. Run 'make deploy' — no activation needed."
	@echo "  (make auto-detects the venv and uses it for all subsequent commands)"

.PHONY: deps
deps: ## Install Python deps into whichever env is active (venv or system)
	$(PIP) install pyyaml jinja2 ruamel.yaml
	@echo "✓ Installed into: $(PYTHON)"

.PHONY: build
build: ## Build the app Docker image
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) -f Dockerfile .
	@echo "✓ Built $(IMAGE_NAME):$(IMAGE_TAG)"

.PHONY: build-no-cache
build-no-cache: ## Build the app Docker image with no layer cache
	docker build --no-cache -t $(IMAGE_NAME):$(IMAGE_TAG) -f Dockerfile .
	@echo "✓ Built $(IMAGE_NAME):$(IMAGE_TAG) (no cache)"

.PHONY: which-python
which-python: ## Show which Python and pip are active (venv or system)
	@echo "PYTHON = $(PYTHON)"
	@echo "PIP    = $(PIP)"
	@$(PYTHON) --version

# ============================================================
# Stack lifecycle
# ============================================================
.PHONY: init
init: ## Generate nginx.conf and docker-compose.yml from manifest.yaml
	$(PYTHON) $(CLI) init

.PHONY: validate
validate: ## Run 5 pre-flight checks (manifest, image, port, nginx syntax)
	$(PYTHON) $(CLI) validate

.PHONY: deploy
deploy: ## Init + bring stack up + wait until healthy (60s timeout)
	$(PYTHON) $(CLI) deploy

.PHONY: promote-canary
promote-canary: ## Switch to canary mode (X-Mode header + /chaos endpoint active)
	$(PYTHON) $(CLI) promote canary

.PHONY: promote-stable
promote-stable: ## Switch back to stable mode
	$(PYTHON) $(CLI) promote stable

.PHONY: teardown
teardown: ## Stop and remove all containers, networks, and volumes
	$(PYTHON) $(CLI) teardown

.PHONY: clean
clean: ## Teardown + delete generated config files (nginx.conf, docker-compose.yml)
	$(PYTHON) $(CLI) teardown --clean

.PHONY: rebuild
rebuild: teardown build deploy ## Teardown -> rebuild image -> deploy

.PHONY: reset
reset: clean build deploy ## Clean -> rebuild image -> deploy (full reset from scratch)


# ============================================================
# Observability & Policy
# ============================================================

.PHONY: metrics-status
metrics-status: ## Live terminal metrics dashboard with OPA policy compliance (Ctrl+C to exit)
	$(PYTHON) $(CLI) status

.PHONY: status-watch
status-watch: ## Live terminal dashboard with custom interval: make status-watch INTERVAL=10
	$(PYTHON) $(CLI) status --interval=$(or $(INTERVAL),5)

.PHONY: audit
audit: ## Generate audit_report.md from history.jsonl
	$(PYTHON) $(CLI) audit
	@echo "Report written to audit_report.md"

.PHONY: policy-check
policy-check: ## Run pre-deploy infrastructure policy checks without deploying
	$(PYTHON) $(CLI) policy-check

.PHONY: dashboard
dashboard: ## Print the browser dashboard URL (stack must be running)
	@PORT=$$($(PYTHON) -c "\
	import sys; sys.path.insert(0, '.'); \
	from cli.config import resolve; from pathlib import Path; \
	cfg = resolve(Path('manifest.yaml'), []); print(cfg.nginx_port)"); \
	echo ""; \
	echo "Browser dashboard: http://localhost:$$PORT/dashboard"; \
	echo ""; \
	echo "Open that URL in your browser. It polls every 5 seconds automatically."

# ============================================================
# Observability
# ============================================================
.PHONY: status
status: ## Show running containers and health status
	docker compose ps

.PHONY: logs
logs: ## Tail logs from all containers
	docker compose logs -f

.PHONY: logs-app
logs-app: ## Tail logs from the app container only
	docker compose logs -f app

.PHONY: logs-nginx
logs-nginx: ## Tail logs from the nginx container only
	docker compose logs -f nginx

.PHONY: logs-access
logs-access: ## Tail the nginx access log (custom swiftdeploy format)
	docker exec $(NGINX_CONTAINER_IMAGE) tail -f /var/log/nginx/$(NGINX_LOG_FILE)

.PHONY: healthcheck
healthcheck: ## Hit /healthz and print response + HTTP status code
	@STATUS=$$(curl -s -o /tmp/sd_health.json -w "%{http_code}" $(HOST_URI)/healthz); \
	echo "HTTP $$STATUS"; \
	cat /tmp/sd_health.json | $(PYTHON) -m json.tool

.PHONY: inspect
inspect: ## Print the fully resolved config from manifest.yaml (no Docker needed)
	@$(PYTHON) -c "\
	import sys; sys.path.insert(0, '.'); \
	from cli.config import resolve; \
	from pathlib import Path; \
	c = resolve(Path('manifest.yaml'), []); \
	print('\nResolved configuration:'); \
	[print(f'  {k:<20} {v}') for k, v in vars(c).items()]; \
	print()"

# ============================================================
# Smoke tests
# ============================================================
.PHONY: smoke
smoke: ## Hit /, /healthz and check headers (stack must be running)
	@echo "--- GET / ---"
	@curl -s $(HOST_URI)/ | $(PYTHON) -m json.tool
	@echo ""
	@echo "--- GET /healthz ---"
	@curl -s $(HOST_URI)/healthz | $(PYTHON) -m json.tool
	@echo ""
	@echo "--- Response headers ---"
	@curl -sI $(HOST_URI)/ | grep -E "X-Deployed-By|X-Mode|Content-Type|HTTP/"

.PHONY: smoke-chaos
smoke-chaos: ## Test slow + error + recover chaos cycle (must be in canary mode)
	@echo "--- Arming slow chaos (2s delay) ---"
	@curl -s -X POST $(HOST_URI)/chaos \
		-H "Content-Type: application/json" \
		-d '{"mode":"slow","duration":2}' | $(PYTHON) -m json.tool
	@echo ""
	@echo "--- GET / (expect ~2s delay) ---"
	@curl -s -w "\nElapsed: %{time_total}s\n" $(HOST_URI)/
	@echo ""
	@echo "--- Arming error chaos (50% rate) ---"
	@curl -s -X POST $(HOST_URI)/chaos \
		-H "Content-Type: application/json" \
		-d '{"mode":"error","rate":0.5}' | $(PYTHON) -m json.tool
	@echo ""
	@echo "--- 5 requests (expect ~50%% to return 500) ---"
	@for i in 1 2 3 4 5; do \
		STATUS=$$(curl -s -o /dev/null -w "%{http_code}" $(HOST_URI)/); \
		echo "  Request $$i: HTTP $$STATUS"; \
	done
	@echo ""
	@echo "--- Recovering ---"
	@curl -s -X POST $(HOST_URI)/chaos \
		-H "Content-Type: application/json" \
		-d '{"mode":"recover"}' | $(PYTHON) -m json.tool


.PHONY: verify
verify: ## Verify stack is up — docker ps + /healthz + /api/dashboard/snapshot
	@echo "--- Running containers ---"
	@docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
	@echo ""
	@echo "--- GET /healthz ---"
	@curl -s $(HOST_URI)/healthz | $(PYTHON) -m json.tool
	@echo ""
	@echo "--- GET /api/dashboard/snapshot ---"
	@curl -s $(HOST_URI)/api/dashboard/snapshot | $(PYTHON) -m json.tool

.PHONY: traffic
traffic: ## Send 20 requests to the stack so dashboards have data to display
	@echo "Sending 20 requests to $(HOST_URI)/ ..."
	@for i in $$(seq 1 20); do \
		STATUS=$$(curl -s -o /dev/null -w "%{http_code}" $(HOST_URI)/); \
		printf "  Request $$i: HTTP $$STATUS\n"; \
	done
	@echo "Done — refresh the dashboard to see updated metrics."

.PHONY: traffic-loop
traffic-loop: ## Continuous traffic generator (Ctrl+C to stop) — useful for watching status dashboard
	@echo "Sending continuous requests to $(HOST_URI)/ (Ctrl+C to stop)..."
	@while true; do \
		curl -s -o /dev/null $(HOST_URI)/; \
		sleep 0.5; \
	done

# ============================================================
# Development
# ============================================================
.PHONY: fmt
fmt: ## Format Go source code with gofmt
	cd $(APP_DIR) && gofmt -w ./...

.PHONY: lint
lint: ## Lint Go source code (requires golangci-lint)
	cd $(APP_DIR) && golangci-lint run ./...

.PHONY: test
test: ## Run Go unit tests
	cd $(APP_DIR) && go test ./... -v

.PHONY: test-cover
test-cover: ## Run Go tests with HTML coverage report
	cd $(APP_DIR) && go test ./... -coverprofile=coverage.out && go tool cover -html=coverage.out

.PHONY: shell-app
shell-app: ## Open a shell inside the app container
	docker exec -it swiftdeploy-app sh

.PHONY: shell-nginx
shell-nginx: ## Open a shell inside the nginx container
	docker exec -it swiftdeploy-nginx sh

.PHONY: go-tidy
go-tidy: ## Tidy Go modules (run after adding new packages or adapters)
	cd app && go mod tidy

# ============================================================
# Utilities
# ============================================================
.PHONY: ps
ps: ## List all Docker containers (system-wide)
	docker ps -a

.PHONY: image-size
image-size: ## Show the size of the built app image
	docker images $(IMAGE_NAME):$(IMAGE_TAG) \
		--format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"

.PHONY: prune
prune: ## Remove unused Docker images, networks, and volumes (system-wide — use with care)
	docker system prune -f
	docker volume prune -f
