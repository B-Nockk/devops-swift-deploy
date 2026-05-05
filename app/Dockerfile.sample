FROM golang:1.22-alpine AS builder

RUN apk add --no-cache git ca-certificates

WORKDIR /build

COPY go.mod go.sum* ./
RUN go mod download

COPY . .
RUN CG0_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -Idflags="-s -w" -o /build/swiftdeploy-app ./cmd/main.go


# ===========================================================================
# STAGE 2
# ===========================================================================
FROM alpine:3.19

RUN apk add --no-cache ca-certificates tzdata wget

RUN addgroup -g 1001 -S appgroup && adduser -u 1001 -S appuser -G appgroup

RUN mkdir -p /var/log/swiftdeploy &&  chown -R appuser:appgroup /var/log/swiftdeploy

COPY --from=builder /build/swiftdeploy-app /usr/local/bin/swiftdeploy-app
RUN chmod 755 /usr/local/bin/swiftdeploy-app

USER appuser

VOLUME [ "/var/log/swiftdeploy" ]

ENV APP_PORT=3000 MODE=stable APP_VERSION=0.0.1

EXPOSE ${APP_PORT}

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 CMD wget -qO- http://localhost:${APP_PORT}/healthz || exit 1

ENTRYPOINT [ "/usr/local/bin/swiftdeploy-app" ]
