package core

import (
	"errors"
	"time"
)

// ===========================================================================
// types
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
	Duration int
	Rate     float64
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
// variable declarations
// ===========================================================================

// Domain-level errors — adapters translate these to HTTP status codes.
var (
	ErrChaosUnavailable = errors.New("chaos endpoint only available in canary mode")
	ErrBadRequest       = errors.New("bad request")
)

// ===========================================================================
// Behaviour
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

func (c ChaosCommand) Validate() error {
	switch c.Mode {
	case ChaosModeSlow:
		if c.Duration <= 0 {
			return errors.New("slow mode requires duration > 0")
		}
	case ChaosModeError:
		if c.Rate <= 0 || c.Rate > 1 {
			return errors.New("error mode require rate between 0 & 1 (exclusive)")
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
	if s.mode == ModeStable {
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

// RecordRequest delegates to the MetricsStore. Called by the HTTP adapter's
// metrics middleware after each completed request.
func (s *Service) RecordRequest(method, path string, statusCode int, durationSeconds float64) {
	s.metrics.Record(method, path, statusCode, durationSeconds)
}

// MetricsSnapshot returns a point-in-time view of all metrics.
func (s *Service) MetricsSnapshot() MetricsSnapshot {
	return s.metrics.Snapshot()
}

// RenderMetrics returns the Prometheus text exposition body.
func (s *Service) RenderMetrics() string {
	return s.metrics.Render()
}
