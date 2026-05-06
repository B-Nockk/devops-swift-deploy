package core

// ChaosStore is the outbound port for reading and writing chaos state.
type ChaosStore interface {
	Get() (ChaosState, error)
	Set(ChaosState) error
}

// MetricsStore is the outbound port for recording and rendering request metrics.
type MetricsStore interface {
	// Record captures one completed request. Safe for concurrent use.
	// Never call this for /metrics itself — would create self-referential noise.
	Record(method, path string, statusCode int, durationSeconds float64)

	// Snapshot returns a point-in-time view of all metric values.
	Snapshot() MetricsSnapshot

	// Render formats all metrics in Prometheus text exposition format.
	Render() string
}

// MetricsSnapshot is a raw point-in-time view of metric counters.
// Used internally by the dashboard adapter to build DashboardSnapshot.
// Adding a new metric: add the field here + populate it in prometheus.go.
type MetricsSnapshot struct {
	RequestsTotal    map[string]int64  // key: "METHOD path status_code"
	HistogramBuckets map[float64]int64 // cumulative counts per le boundary
	HistogramSum     float64           // sum of all observed durations (seconds)
	HistogramCount   int64             // total number of observations
	UptimeSeconds    float64
	Mode             int // 0=stable, 1=canary
	ChaosActive      int // 0=none, 1=slow, 2=error
}

// DashboardPort is the inbound port for the dashboard adapter.
// It exposes a pre-computed, frontend-friendly snapshot so the JS
// does zero calculation — just renders what it receives.
//
// To change what the dashboard shows:
//   1. Add/remove fields on DashboardSnapshot below
//   2. Update BuildDashboardSnapshot() in service.go to populate them
//   3. Update dashboard/metrics.js to render the new fields
//
// The nginx proxy config and Go handler never need to change.
type DashboardPort interface {
	BuildDashboardSnapshot() DashboardSnapshot
}

// DashboardSnapshot is the pre-computed JSON payload served at
// GET /api/dashboard/snapshot. All values are ready to display.
//
// ── To add a new field ──────────────────────────────────────────────────────
// 1. Add it here with a clear json tag and a comment explaining the unit/range
// 2. Populate it in service.go BuildDashboardSnapshot()
// 3. Reference it in dashboard/metrics.js (search for existing field names)
// ────────────────────────────────────────────────────────────────────────────
type DashboardSnapshot struct {
	// Service identity
	Mode    string `json:"mode"`    // "stable" | "canary"
	Version string `json:"version"` // from APP_VERSION env var

	// Uptime
	UptimeSeconds float64 `json:"uptime_seconds"`
	UptimeHuman   string  `json:"uptime_human"` // e.g. "2h 14m 33s"

	// Request counts (all-time, since last container start)
	TotalRequests int64 `json:"total_requests"`
	ErrorRequests int64 `json:"error_requests"` // status >= 500

	// Derived rates (all-time — windowed rates need two scrapes, done in JS or status.py)
	ErrorRatePct float64 `json:"error_rate_pct"` // 0.0–100.0

	// Latency — P99 in milliseconds derived from histogram
	P99LatencyMs float64 `json:"p99_latency_ms"`

	// Chaos state — easy to display as a badge
	ChaosActive     int    `json:"chaos_active"`      // 0=none, 1=slow, 2=error
	ChaosActiveText string `json:"chaos_active_text"` // "none" | "slow" | "error"

	// Per-route breakdown — each entry is one label combination
	// Sorted by request count descending for easy table rendering
	Routes []RouteStats `json:"routes"`
}

// RouteStats is one row in the per-route breakdown table.
// To add columns: add fields here + populate in service.go.
type RouteStats struct {
	Method     string `json:"method"`
	Path       string `json:"path"`
	StatusCode string `json:"status_code"`
	Count      int64  `json:"count"`
}

// ServicePort is the inbound port — the full set of operations the HTTP
// adapter (and any future adapter) may call on the core service.
type ServicePort interface {
	BuildWelcome() WelcomeResponse
	BuildHealth() HealthResponse
	ApplyChaos(ChaosCommand) (ChaosResponse, error)
	GetChaosState() (ChaosState, error)
	IsCanary() bool

	// Metrics delegation — called by the HTTP adapter's metrics middleware.
	RecordRequest(method, path string, statusCode int, durationSeconds float64)

	// MetricsSnapshot returns a point-in-time metrics view for /metrics rendering.
	MetricsSnapshot() MetricsSnapshot

	// RenderMetrics returns the Prometheus text body for GET /metrics.
	RenderMetrics() string

	// Dashboard — called by the dashboard adapter's BFF endpoint.
	BuildDashboardSnapshot() DashboardSnapshot
}
