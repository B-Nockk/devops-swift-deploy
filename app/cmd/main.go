package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"

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

	log.Printf("SwiftDeploy service starting | mode=%s version=%s port=%s", mode, version, port)

	chaosStore := store.NewMemoryChaosStore()

	// chaosActiveFn is a closure over chaosStore so the MetricsStore can read
	// the current chaos state without holding a direct reference to chaosStore.
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
	handler := httpadapter.NewHandler(svc)

	mux := http.NewServeMux()
	handler.Register(mux)

	addr := fmt.Sprintf(":%s", port)
	log.Printf("Listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("Server error: %v", err)
	}
}
