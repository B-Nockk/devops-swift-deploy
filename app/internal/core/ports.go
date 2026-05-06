package core

// ChaosStore is the outbound port for reading and writing chaos state.
type ChaosStore interface {
	Get() (ChaosState, error)
	Set(ChaosState) error
}

// MetricsStore is the outbound port for recording and rendering request metrics.
type MetricsStore interface {
	// Record captures one completed request. Must be safe for concurrent use.
	// path is the matched route pattern (e.g. "/", "/healthz"), not the raw URL.
	// Skip recording when path == "/metrics" to avoid self-referential noise.
	Record(method, path string, statusCode int, durationSeconds float64)

	// Snapshot returns the current metric values for the status dashboard.
	Snapshot() MetricsSnapshot

	// Render formats all metrics in Prometheus text exposition format.
	// Returns the full text/plain body including HELP, TYPE, and all samples.
	Render() string
}

// MetricsSnapshot is a point-in-time view of the metrics for use by the
// status dashboard and OPA pre-promote checks.
type MetricsSnapshot struct {
	RequestsTotal    map[string]int64  // key: "METHOD path status_code"
	HistogramBuckets map[float64]int64 // key: le boundary, value: cumulative count
	HistogramSum     float64           // sum of all durations in seconds
	HistogramCount   int64             // total number of observations
	UptimeSeconds    float64
	Mode             int // 0=stable, 1=canary
	ChaosActive      int // 0=none, 1=slow, 2=error
}

// ServicePort is the inbound port — the set of operations the HTTP adapter
// (and any future adapter) may call on the core service.
type ServicePort interface {
	BuildWelcome() WelcomeResponse
	BuildHealth() HealthResponse
	ApplyChaos(ChaosCommand) (ChaosResponse, error)
	GetChaosState() (ChaosState, error)
	IsCanary() bool

	// RecordRequest delegates metric recording to the MetricsStore.
	// Called by the HTTP adapter's metrics middleware after each request.
	RecordRequest(method, path string, statusCode int, durationSeconds float64)

	// MetricsSnapshot returns a point-in-time metrics view for /metrics rendering.
	MetricsSnapshot() MetricsSnapshot

	// RenderMetrics returns the Prometheus text body for GET /metrics.
	RenderMetrics() string
}
