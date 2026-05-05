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
# Pulls the rest of the Go source code from the app/ directory.
COPY app/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -trimpath \
    -ldflags="-s -w" \
    -o /build/swiftdeploy-app \
    ./cmd/main.go

# ============================================================
# Stage 2: Runtime
# Alpine 3.19 — ~7MB base. Final image typically ~12-15MB total.
# Target: well under the 300MB limit.
# ============================================================
FROM alpine:3.19

# Runtime dependencies only:
#   ca-certificates — TLS verification for any outbound calls
#   tzdata          — correct UTC timestamps in logs
#   wget            — used by HEALTHCHECK; busybox wget is in this package
RUN apk add --no-cache ca-certificates tzdata wget

# Non-root user — CIS Docker Benchmark requirement.
# UID/GID 1001 avoids collision with Alpine system users (nobody=65534, nginx=101).
RUN addgroup -g 1001 -S appgroup && \
    adduser  -u 1001 -S appuser -G appgroup

# Create and pre-own the log directory BEFORE switching to non-root user.
# When Docker mounts the named volume over /var/log/swiftdeploy, it preserves
# the directory ownership set here — so appuser can write logs without root.
RUN mkdir -p /var/log/swiftdeploy && \
    chown -R appuser:appgroup /var/log/swiftdeploy

# Copy compiled binary from builder. Root owns it; appuser can execute, not overwrite.
COPY --from=builder /build/swiftdeploy-app /usr/local/bin/swiftdeploy-app
RUN chmod 755 /usr/local/bin/swiftdeploy-app

# Drop privileges for all subsequent layers and at runtime.
USER appuser

# Declare volume. docker-compose mounts app_logs here.
# Pre-ownership above ensures the mount is writable by appuser.
VOLUME ["/var/log/swiftdeploy"]

# Environment defaults — all overridden by docker-compose environment: block.
ENV APP_PORT=3000 \
    MODE=stable \
    APP_VERSION=0.0.1

EXPOSE ${APP_PORT}

# Container-level health check (belt-and-suspenders alongside compose healthcheck).
# Uses wget from the apk install above — available even as non-root.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:${APP_PORT}/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/swiftdeploy-app"]
