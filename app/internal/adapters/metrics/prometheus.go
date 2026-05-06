package metrics

import (
	"fmt"
	"math"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"swiftdeploy/internal/core"
)

// bucketBoundaries are the histogram bucket upper bounds in seconds,
// matching the spec: .005,.01,.025,.05,.1,.25,.5,1,2.5,5,10
var bucketBoundaries = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10}

// requestKey builds the label set key for http_requests_total.
// Format: "METHOD /path status_code" — compact, unambiguous, easy to parse.
func requestKey(method, path string, statusCode int) string {
	return fmt.Sprintf("%s %s %d", method, path, statusCode)
}

// prometheusStore implements core.MetricsStore.
// All exported methods are safe for concurrent use.
type prometheusStore struct {
	startTime time.Time
	mode      int // snapshot at construction; doesn't change at runtime

	// requestCounts maps requestKey → count.
	// sync.Map is appropriate here: many distinct keys, written once per request,
	// read infrequently (only on /metrics scrape or Snapshot call).
	requestCounts sync.Map

	// histogram is protected by its own mutex because all fields must be
	// updated atomically together (bucket, sum, count in one observation).
	histMu  sync.Mutex
	buckets [len(bucketBoundaries)]int64 // cumulative counts per bucket
	sum     float64
	count   int64

	// chaosActive is read from the ChaosStore on each Snapshot call via
	// the chaosActive function injected at construction — keeps the adapter
	// decoupled from the ChaosStore directly.
	chaosActiveFn func() int
}

// NewPrometheusStore returns a MetricsStore that formats in Prometheus text format.
// mode: 0=stable, 1=canary
// chaosActiveFn: called on each Snapshot to get current chaos state (0=none,1=slow,2=error)
func NewPrometheusStore(mode int, chaosActiveFn func() int) core.MetricsStore {
	return &prometheusStore{
		startTime:     time.Now(),
		mode:          mode,
		chaosActiveFn: chaosActiveFn,
	}
}

// Record implements core.MetricsStore.
// Safe for concurrent use. The handler must never call this for /metrics itself.
func (p *prometheusStore) Record(method, path string, statusCode int, durationSeconds float64) {
	// --- http_requests_total counter ---
	key := requestKey(method, path, statusCode)
	// LoadOrStore returns the existing value or stores the new one;
	// we then do an atomic increment on the *int64 we get back.
	actual, _ := p.requestCounts.LoadOrStore(key, new(int64))
	atomic.AddInt64(actual.(*int64), 1)

	// --- http_request_duration_seconds histogram ---
	p.histMu.Lock()
	for i, le := range bucketBoundaries {
		if durationSeconds <= le {
			p.buckets[i]++
		}
	}
	p.sum += durationSeconds
	p.count++
	p.histMu.Unlock()
}

// Snapshot implements core.MetricsStore.
func (p *prometheusStore) Snapshot() core.MetricsSnapshot {
	snap := core.MetricsSnapshot{
		RequestsTotal:    make(map[string]int64),
		HistogramBuckets: make(map[float64]int64),
		UptimeSeconds:    time.Since(p.startTime).Seconds(),
		Mode:             p.mode,
		ChaosActive:      p.chaosActiveFn(),
	}

	p.requestCounts.Range(func(k, v any) bool {
		snap.RequestsTotal[k.(string)] = atomic.LoadInt64(v.(*int64))
		return true
	})

	p.histMu.Lock()
	for i, le := range bucketBoundaries {
		snap.HistogramBuckets[le] = p.buckets[i]
	}
	snap.HistogramSum = p.sum
	snap.HistogramCount = p.count
	p.histMu.Unlock()

	return snap
}

// Render implements core.MetricsStore.
// Returns a valid Prometheus text exposition body.
// See: https://prometheus.io/docs/instrumenting/exposition_formats/
func (p *prometheusStore) Render() string {
	snap := p.Snapshot()
	var b strings.Builder

	// --- http_requests_total ---
	b.WriteString("# HELP http_requests_total Total HTTP requests by method, path, and status code.\n")
	b.WriteString("# TYPE http_requests_total counter\n")
	for key, count := range snap.RequestsTotal {
		// key format: "METHOD /path statusCode"
		parts := strings.SplitN(key, " ", 3)
		if len(parts) != 3 {
			continue
		}
		fmt.Fprintf(&b,
			"http_requests_total{method=%q,path=%q,status_code=%q} %d\n",
			parts[0], parts[1], parts[2], count,
		)
	}

	// --- http_request_duration_seconds histogram ---
	b.WriteString("# HELP http_request_duration_seconds HTTP request latency histogram.\n")
	b.WriteString("# TYPE http_request_duration_seconds histogram\n")
	for _, le := range bucketBoundaries {
		count := snap.HistogramBuckets[le]
		leStr := formatFloat(le)
		fmt.Fprintf(&b,
			"http_request_duration_seconds_bucket{le=%q} %d\n",
			leStr, count,
		)
	}
	// +Inf bucket = total count (required by Prometheus spec)
	fmt.Fprintf(&b,
		"http_request_duration_seconds_bucket{le=\"+Inf\"} %d\n",
		snap.HistogramCount,
	)
	fmt.Fprintf(&b, "http_request_duration_seconds_sum %s\n", formatFloat(snap.HistogramSum))
	fmt.Fprintf(&b, "http_request_duration_seconds_count %d\n", snap.HistogramCount)

	// --- app_uptime_seconds gauge ---
	b.WriteString("# HELP app_uptime_seconds Seconds since the service started.\n")
	b.WriteString("# TYPE app_uptime_seconds gauge\n")
	fmt.Fprintf(&b, "app_uptime_seconds %s\n", formatFloat(snap.UptimeSeconds))

	// --- app_mode gauge ---
	b.WriteString("# HELP app_mode Current deployment mode. 0=stable 1=canary.\n")
	b.WriteString("# TYPE app_mode gauge\n")
	fmt.Fprintf(&b, "app_mode %d\n", snap.Mode)

	// --- chaos_active gauge ---
	b.WriteString("# HELP chaos_active Current chaos state. 0=none 1=slow 2=error.\n")
	b.WriteString("# TYPE chaos_active gauge\n")
	fmt.Fprintf(&b, "chaos_active %d\n", snap.ChaosActive)

	return b.String()
}

// formatFloat renders a float64 without trailing zeros, but always with at
// least one decimal place so Prometheus parsers don't mistake it for an int.
func formatFloat(f float64) string {
	if math.IsInf(f, 0) || math.IsNaN(f) {
		return "0"
	}
	s := fmt.Sprintf("%g", f)
	// Ensure there's a decimal point — "5" → "5.0"
	if !strings.Contains(s, ".") && !strings.Contains(s, "e") {
		s += ".0"
	}
	return s
}
