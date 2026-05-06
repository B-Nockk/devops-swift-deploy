package core

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
)

// ===========================================================================
// Types
// ===========================================================================

type Mode string
type ChaosMode string

const (
	ModeStable Mode = "stable"
	ModeCanary Mode = "canary"
)

const (
	ChaosModeNone    ChaosMode = ""
	ChaosModeSlow    ChaosMode = "slow"
	ChaosModeError   ChaosMode = "error"
	ChaosModeRecover ChaosMode = "recover"
)

type ChaosState struct {
	Active   ChaosMode
	Duration int
	Rate     float64
}

type ChaosCommand struct {
	Mode     ChaosMode `json:"mode"`
	Duration int       `json:"duration"`
	Rate     float64   `json:"rate"`
}

type Service struct {
	mode      Mode
	version   string
	startTime time.Time
	store     ChaosStore
	metrics   MetricsStore
}

// ===========================================================================
// Response types
// ===========================================================================

type WelcomeResponse struct {
	Message   string `json:"message"`
	Mode      string `json:"mode"`
	Version   string `json:"version"`
	Timestamp string `json:"timestamp"`
}

type HealthResponse struct {
	Status string  `json:"status"`
	Uptime float64 `json:"uptime_seconds"`
}

type ChaosResponse struct {
	Status string `json:"status"`
	Choas  string `json:"active_mode"`
}

type ErrorResponse struct {
	Error string `json:"error"`
}

// ===========================================================================
// Errors
// ===========================================================================

var (
	ErrChaosUnavailable = errors.New("chaos endpoint only available in canary mode")
	ErrBadRequest       = errors.New("bad request")
)

// ===========================================================================
// Constructor
// ===========================================================================

func NewService(mode Mode, version string, store ChaosStore, metrics MetricsStore) *Service {
	return &Service{
		mode:      mode,
		version:   version,
		startTime: time.Now(),
		store:     store,
		metrics:   metrics,
	}
}

// ===========================================================================
// ChaosCommand helpers
// ===========================================================================

func (c ChaosCommand) Validate() error {
	switch c.Mode {
	case ChaosModeSlow:
		if c.Duration <= 0 {
			return errors.New("slow mode requires duration > 0")
		}
	case ChaosModeError:
		if c.Rate <= 0 || c.Rate > 1 {
			return errors.New("error mode requires rate between 0 and 1 (exclusive)")
		}
	case ChaosModeRecover:
		// always valid
	default:
		return errors.New("unknown chaos mode: must be slow, error, or recover")
	}
	return nil
}

func (c ChaosCommand) ToChaosState() ChaosState {
	if c.Mode == ChaosModeRecover {
		return ChaosState{Active: ChaosModeNone}
	}
	return ChaosState{
		Active:   c.Mode,
		Duration: c.Duration,
		Rate:     c.Rate,
	}
}

// ===========================================================================
// ServicePort implementation
// ===========================================================================

func (s *Service) BuildWelcome() WelcomeResponse {
	return WelcomeResponse{
		Message:   "Welcome to SwiftDeploy — running in " + string(s.mode) + " mode",
		Mode:      string(s.mode),
		Version:   s.version,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}
}

func (s *Service) BuildHealth() HealthResponse {
	return HealthResponse{
		Status: "ok",
		Uptime: time.Since(s.startTime).Seconds(),
	}
}

func (s *Service) ApplyChaos(cmd ChaosCommand) (ChaosResponse, error) {
	if s.mode != ModeCanary {
		return ChaosResponse{}, ErrChaosUnavailable
	}
	if err := cmd.Validate(); err != nil {
		return ChaosResponse{}, err
	}
	state := cmd.ToChaosState()
	if err := s.store.Set(state); err != nil {
		return ChaosResponse{}, err
	}
	return ChaosResponse{
		Status: "ok",
		Choas:  string(state.Active),
	}, nil
}

func (s *Service) GetChaosState() (ChaosState, error) {
	return s.store.Get()
}

func (s *Service) IsCanary() bool {
	return s.mode == ModeCanary
}

// Metrics delegation — the HTTP adapter calls these after each request.

func (s *Service) RecordRequest(method, path string, statusCode int, durationSeconds float64) {
	s.metrics.Record(method, path, statusCode, durationSeconds)
}

func (s *Service) MetricsSnapshot() MetricsSnapshot {
	return s.metrics.Snapshot()
}

func (s *Service) RenderMetrics() string {
	return s.metrics.Render()
}

// ===========================================================================
// DashboardPort implementation
//
// BuildDashboardSnapshot computes all values the frontend needs.
// The JS receives ready-to-display data — no math in the browser.
//
// To add a new dashboard metric:
//   1. Add the field to DashboardSnapshot in ports.go
//   2. Populate it here
//   3. Render it in dashboard/metrics.js
// ===========================================================================

func (s *Service) BuildDashboardSnapshot() DashboardSnapshot {
	snap := s.metrics.Snapshot()
	uptime := time.Since(s.startTime).Seconds()

	// --- Request totals ---
	var totalRequests, errorRequests int64
	var routes []RouteStats

	for key, count := range snap.RequestsTotal {
		parts := strings.SplitN(key, " ", 3)
		if len(parts) != 3 {
			continue
		}
		method, path, status := parts[0], parts[1], parts[2]

		totalRequests += count
		if strings.HasPrefix(status, "5") {
			errorRequests += count
		}

		routes = append(routes, RouteStats{
			Method:     method,
			Path:       path,
			StatusCode: status,
			Count:      count,
		})
	}

	// Sort routes by count descending for easy table rendering
	sort.Slice(routes, func(i, j int) bool {
		return routes[i].Count > routes[j].Count
	})

	// --- Error rate ---
	var errorRatePct float64
	if totalRequests > 0 {
		errorRatePct = (float64(errorRequests) / float64(totalRequests)) * 100
		errorRatePct = math.Round(errorRatePct*100) / 100 // 2 decimal places
	}

	// --- P99 latency from histogram ---
	p99Ms := computeP99Ms(snap.HistogramBuckets, snap.HistogramCount)

	// --- Chaos text ---
	chaosText := chaosActiveText(snap.ChaosActive)

	// --- Mode string ---
	modeStr := "stable"
	if snap.Mode == 1 {
		modeStr = "canary"
	}

	return DashboardSnapshot{
		Mode:            modeStr,
		Version:         s.version,
		UptimeSeconds:   math.Round(uptime*10) / 10,
		UptimeHuman:     formatUptime(uptime),
		TotalRequests:   totalRequests,
		ErrorRequests:   errorRequests,
		ErrorRatePct:    errorRatePct,
		P99LatencyMs:    p99Ms,
		ChaosActive:     snap.ChaosActive,
		ChaosActiveText: chaosText,
		Routes:          routes,
	}
}

// ===========================================================================
// Helpers — pure functions, easy to test and modify independently
// ===========================================================================

// computeP99Ms derives P99 latency in milliseconds from cumulative histogram
// bucket counts. Returns 0 if there is no data.
//
// To change the P99 percentile (e.g. to P95): change 0.99 → 0.95.
func computeP99Ms(buckets map[float64]int64, totalCount int64) float64 {
	if totalCount <= 0 || len(buckets) == 0 {
		return 0
	}

	target := 0.99 * float64(totalCount)

	// Sort bucket boundaries ascending
	boundaries := make([]float64, 0, len(buckets))
	for le := range buckets {
		boundaries = append(boundaries, le)
	}
	sort.Float64s(boundaries)

	var prevCount float64
	var prevLe float64

	for _, le := range boundaries {
		count := float64(buckets[le])
		if count >= target {
			// Linear interpolation within this bucket
			fraction := 0.0
			if count > prevCount {
				fraction = (target - prevCount) / (count - prevCount)
			}
			ms := (prevLe + fraction*(le-prevLe)) * 1000
			return math.Round(ms*100) / 100
		}
		prevCount = count
		prevLe = le
	}

	// All observations beyond the last finite bucket
	if len(boundaries) > 0 {
		return boundaries[len(boundaries)-1] * 1000
	}
	return 0
}

// formatUptime converts seconds into a human-readable string.
// To change the format: edit only this function.
func formatUptime(seconds float64) string {
	d := time.Duration(seconds) * time.Second
	h := int(d.Hours())
	m := int(d.Minutes()) % 60
	s := int(d.Seconds()) % 60

	if h > 0 {
		return fmt.Sprintf("%dh %dm %ds", h, m, s)
	}
	if m > 0 {
		return fmt.Sprintf("%dm %ds", m, s)
	}
	return strconv.Itoa(s) + "s"
}

// chaosActiveText maps the chaos_active int gauge to a display string.
// To add new chaos modes: extend this switch + the Go chaos logic.
func chaosActiveText(active int) string {
	switch active {
	case 1:
		return "slow"
	case 2:
		return "error"
	default:
		return "none"
	}
}
