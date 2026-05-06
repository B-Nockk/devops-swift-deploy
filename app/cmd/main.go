package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"

	"swiftdeploy/internal/adapters/dashboard"
	httpadapter "swiftdeploy/internal/adapters/http"
	metricsadapter "swiftdeploy/internal/adapters/metrics"
	"swiftdeploy/internal/adapters/store"
	"swiftdeploy/internal/core"
)

func resolveMode(raw string) core.Mode {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "canary":
		return core.ModeCanary
	default:
		return core.ModeStable
	}
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func modeInt(m core.Mode) int {
	if m == core.ModeCanary {
		return 1
	}
	return 0
}

func main() {
	mode := resolveMode(os.Getenv("MODE"))
	version := envOrDefault("APP_VERSION", "0.0.1")
	port := envOrDefault("APP_PORT", "3000")

	// DASHBOARD_STATIC_DIR tells the dashboard handler where to find the
	// frontend files. Defaults to ./dashboard (relative to working dir).
	// Override in .env or docker-compose to point elsewhere if needed.
	dashboardDir := envOrDefault("DASHBOARD_STATIC_DIR", "dashboard")

	log.Printf("SwiftDeploy service starting | mode=%s version=%s port=%s", mode, version, port)

	chaosStore := store.NewMemoryChaosStore()

	// chaosActiveFn lets the metrics adapter read chaos state without
	// holding a direct reference to chaosStore.
	// Mapping: ChaosModeNone=0, ChaosModeSlow=1, ChaosModeError=2
	chaosActiveFn := func() int {
		state, err := chaosStore.Get()
		if err != nil {
			return 0
		}
		switch state.Active {
		case core.ChaosModeSlow:
			return 1
		case core.ChaosModeError:
			return 2
		default:
			return 0
		}
	}

	metricsStore := metricsadapter.NewPrometheusStore(modeInt(mode), chaosActiveFn)
	svc := core.NewService(mode, version, chaosStore, metricsStore)

	mux := http.NewServeMux()

	// ── Adapters ─────────────────────────────────────────────────────────────
	// Each adapter registers its own routes. Order matters only if routes
	// overlap — they don't here.
	//
	// To add a new adapter: create it, call .Register(mux), done.
	// Nothing else in this file needs to change.

	httpadapter.NewHandler(svc).Register(mux)
	dashboard.NewHandler(svc, dashboardDir).Register(mux)

	addr := fmt.Sprintf(":%s", port)
	log.Printf("Listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("Server error: %v", err)
	}
}
