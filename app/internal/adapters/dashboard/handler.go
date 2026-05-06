// Package dashboard provides two things:
//
//  1. A static file server for the frontend (GET /dashboard and assets)
//  2. A BFF (Backend-for-Frontend) endpoint at GET /api/dashboard/snapshot
//     that returns pre-computed dashboard data as JSON.
//
// ── To change what data the dashboard receives ───────────────────────────────
// Edit DashboardSnapshot in core/ports.go and BuildDashboardSnapshot in
// core/service.go. This handler never needs to change.
//
// ── To swap the frontend ─────────────────────────────────────────────────────
// Replace the files in dashboard/ at the project root. The BFF endpoint
// contract (/api/dashboard/snapshot response shape) is the only thing that
// must stay compatible. If you change the shape, update metrics.js too.
//
// ── To add authentication or rate limiting ───────────────────────────────────
// Wrap Register() calls with middleware in main.go. Nothing here changes.
// ─────────────────────────────────────────────────────────────────────────────
package dashboard

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path/filepath"

	"swiftdeploy/internal/core"
)

// Handler serves the dashboard static files and the BFF snapshot endpoint.
type Handler struct {
	svc        core.DashboardPort
	staticDir  string // absolute path to the dashboard/ directory
}

// NewHandler creates a dashboard handler.
//
// svc:       the core service (implements DashboardPort)
// staticDir: path to the dashboard/ directory containing index.html etc.
//            Resolved relative to the working directory if not absolute.
func NewHandler(svc core.DashboardPort, staticDir string) *Handler {
	if !filepath.IsAbs(staticDir) {
		wd, err := os.Getwd()
		if err == nil {
			staticDir = filepath.Join(wd, staticDir)
		}
	}
	return &Handler{svc: svc, staticDir: staticDir}
}

// Register wires the dashboard routes onto the provided mux.
//
// Routes added:
//   GET /dashboard         → serves index.html
//   GET /dashboard/        → serves index.html
//   GET /dashboard/*       → serves static assets (css, js)
//   GET /api/dashboard/snapshot → BFF JSON endpoint
func (h *Handler) Register(mux *http.ServeMux) {
	// BFF endpoint — must be registered before the static catch-all
	mux.HandleFunc("/api/dashboard/snapshot", h.snapshot)

	// Static file server for the dashboard frontend
	fs := http.FileServer(http.Dir(h.staticDir))
	mux.Handle("/dashboard/", http.StripPrefix("/dashboard", fs))

	// Redirect bare /dashboard to /dashboard/ so relative asset paths work
	mux.HandleFunc("/dashboard", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/dashboard/", http.StatusMovedPermanently)
	})
}

// snapshot is the BFF endpoint. It calls BuildDashboardSnapshot() on the
// core service and returns the result as JSON.
//
// The frontend polls this endpoint on an interval (configured in metrics.js).
// No business logic lives here — this is pure translation: core type → JSON.
func (h *Handler) snapshot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
		return
	}

	snap := h.svc.BuildDashboardSnapshot()

	w.Header().Set("Content-Type", "application/json")
	// Allow the browser to cache for 1 second — prevents hammering on fast intervals
	w.Header().Set("Cache-Control", "max-age=1")

	if err := json.NewEncoder(w).Encode(snap); err != nil {
		log.Printf("dashboard: snapshot encode error: %v", err)
	}
}
