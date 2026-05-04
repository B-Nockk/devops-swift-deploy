package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	httpadapter "swiftdeploy/internal/adapters/http"
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

func main() {
	mode := resolveMode(os.Getenv("MODE"))
	version := envOrDefault("APP_VERSION", "0.0.1")
	port := envOrDefault("APP_PORT", "3000")

	log.Printf("SwiftDeploy service starting | mode=%s version=%s port=%s", mode, version, port)
	choasStore := store.NewMemoryChaosStore()
	svc := core.NewService(mode, version, choasStore)
	handler := httpadapter.NewHandler(svc)

	mux := http.NewServeMux()
	handler.Register(mux)

	addr := fmt.Sprintf(":%s", port)
	log.Printf("Listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("Server error: %v", err)
	}

}
