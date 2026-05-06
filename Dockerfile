# ============================================================
# Stage 1: Builder
# Full Go toolchain — this stage is discarded after compile.
# Nothing from here lands in the final image except the binary.
# ============================================================
FROM golang:1.22-alpine AS builder

RUN apk add --no-cache git ca-certificates

WORKDIR /build

# Copy dependency manifests first.
# Prefixed with app/ because the Dockerfile is at the root.
COPY app/go.mod app/go.sum* ./
RUN go mod download

# Copy source and compile.
COPY app/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -trimpath \
    -ldflags="-s -w" \
    -o /build/swiftdeploy-app \
    ./cmd/main.go

# ============================================================
# Stage 2: Runtime
# Alpine 3.19 — ~7MB base. Final image typically ~12-15MB total.
# ============================================================
FROM alpine:3.19

# Runtime dependencies only:
#   ca-certificates — TLS verification for any outbound calls
#   tzdata          — correct UTC timestamps in logs
#   wget            — used by HEALTHCHECK
RUN apk add --no-cache ca-certificates tzdata wget

# Non-root user — CIS Docker Benchmark requirement.
# UID/GID 1001 avoids collision with Alpine system users.
RUN addgroup -g 1001 -S appgroup && \
    adduser  -u 1001 -S appuser -G appgroup

# Create and pre-own the log directory BEFORE switching to non-root user.
# Named volume mount preserves this ownership so appuser can write logs.
RUN mkdir -p /var/log/swiftdeploy && \
    chown -R appuser:appgroup /var/log/swiftdeploy

# Create dashboard directory and pre-own it.
# The docker-compose volume mount (./dashboard:/app/dashboard:ro) overlays
# this at runtime. We create it here so the path exists even if the volume
# isn't mounted (e.g. during local go run outside compose).
RUN mkdir -p /app/dashboard && \
    chown -R appuser:appgroup /app/dashboard

# Copy compiled binary from builder.
COPY --from=builder /build/swiftdeploy-app /usr/local/bin/swiftdeploy-app
RUN chmod 755 /usr/local/bin/swiftdeploy-app

# Copy dashboard static files into the image.
# These are also available via the docker-compose volume mount (read-only),
# which takes precedence at runtime — so you can update dashboard/ files
# without rebuilding the image by doing `docker compose up -d --force-recreate app`.
# The COPY here is a fallback and keeps the image self-contained.
COPY dashboard/ /app/dashboard/

# Drop privileges for all subsequent layers and at runtime.
USER appuser

# Declare volumes. docker-compose mounts these.
VOLUME ["/var/log/swiftdeploy"]

# Environment defaults — all overridden by docker-compose environment: block.
ENV APP_PORT=3000 \
    MODE=stable \
    APP_VERSION=0.0.1 \
    DASHBOARD_STATIC_DIR=/app/dashboard

EXPOSE ${APP_PORT}

# Container-level health check.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:${APP_PORT}/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/swiftdeploy-app"]
