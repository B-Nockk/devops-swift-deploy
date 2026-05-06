package http

import (
	"encoding/json"
	"errors"
	"log"
	"math/rand/v2"
	"net/http"
	"swiftdeploy/internal/core"
	"time"
)

type Handler struct {
	svc core.ServicePort
}

// statusRecorder wraps http.ResponseWriter to capture the status code written
// by downstream handlers. Go's ResponseWriter doesn't expose the written status
// after the fact, so we intercept WriteHeader and remember it.
// The zero value is intentionally not useful — always construct via newStatusRecorder.
type statusRecorder struct {
	http.ResponseWriter
	status  int
	written bool
}

func newStatusRecorder(w http.ResponseWriter) *statusRecorder {
	return &statusRecorder{ResponseWriter: w, status: http.StatusOK}
}

func (r *statusRecorder) WriteHeader(code int) {
	if !r.written {
		r.status = code
		r.written = true
		r.ResponseWriter.WriteHeader(code)
	}
}

// Write satisfies http.ResponseWriter. If the handler calls Write without a
// prior WriteHeader, the status defaults to 200 — match that behaviour here.
func (r *statusRecorder) Write(b []byte) (int, error) {
	if !r.written {
		r.written = true
		// status is already defaulted to 200 in newStatusRecorder
	}
	return r.ResponseWriter.Write(b)
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		log.Printf("writeJSON encode error: %v", err)
	}
}

// withMetrics records method, matched path, status code, and duration for
// every request EXCEPT /metrics itself (to avoid self-referential noise).
// Must be the outermost middleware so it captures the full round-trip duration.
func (h *Handler) withMetrics(pattern string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if pattern == "/metrics" {
			next(w, r)
			return
		}
		rec := newStatusRecorder(w)
		start := time.Now()
		next(rec, r)
		h.svc.RecordRequest(r.Method, pattern, rec.status, time.Since(start).Seconds())
	}
}

func (h *Handler) withCommonHeaders(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// w.Header().Set("X-Deployed-By", "swiftdeploy")
		if h.svc.IsCanary() {
			// nginx is configured with proxy_pass_header X-Mode to forward this.
			w.Header().Set("X-Mode", "canary")
		}
		next(w, r)
	}
}

// chain composes middleware right-to-left so the first argument is outermost.
// chain(a, b)(h) executes: a → b → h
func chain(handler http.HandlerFunc, middleware ...func(http.HandlerFunc) http.HandlerFunc) http.HandlerFunc {
	for i := len(middleware) - 1; i >= 0; i-- {
		handler = middleware[i](handler)
	}
	return handler
}

func (h *Handler) chaos(w http.ResponseWriter, r *http.Request) {
	handler := chain(
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				writeJSON(w, http.StatusMethodNotAllowed, core.ErrorResponse{Error: "method not allowed"})
				return
			}
			var cmd core.ChaosCommand
			if err := json.NewDecoder(r.Body).Decode(&cmd); err != nil {
				writeJSON(w, http.StatusBadRequest, core.ErrorResponse{Error: "invalid JSON body"})
				return
			}
			resp, err := h.svc.ApplyChaos(cmd)
			if err != nil {
				switch {
				case errors.Is(err, core.ErrChaosUnavailable):
					// Policy: chaos is only available in canary mode.
					// This decision lives in core; the error is only translated here.
					writeJSON(w, http.StatusForbidden, core.ErrorResponse{Error: err.Error()})
				default:
					writeJSON(w, http.StatusBadRequest, core.ErrorResponse{Error: err.Error()})
				}
				return
			}
			writeJSON(w, http.StatusOK, resp)
		},
		h.withCommonHeaders,
	)
	handler(w, r)
}

func (h *Handler) withChaos(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		state, err := h.svc.GetChaosState()
		if err != nil || state.Active == core.ChaosModeNone {
			next(w, r)
			return
		}
		switch state.Active {
		case core.ChaosModeSlow:
			time.Sleep(time.Duration(state.Duration) * time.Second)
		case core.ChaosModeError:
			if rand.Float64() < state.Rate {
				writeJSON(w, http.StatusInternalServerError, core.ErrorResponse{
					Error: "chaos-induced error",
				})
				return
			}
		}
		next(w, r)
	}
}

func (h *Handler) welcome(w http.ResponseWriter, r *http.Request) {
	handler := chain(
		func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/" {
				writeJSON(w, http.StatusNotFound, core.ErrorResponse{Error: "not found"})
				return
			}
			if r.Method != http.MethodGet {
				writeJSON(w, http.StatusMethodNotAllowed, core.ErrorResponse{Error: "method not allowed"})
				return
			}
			writeJSON(w, http.StatusOK, h.svc.BuildWelcome())
		},
		h.withChaos,
		h.withCommonHeaders, // outermost — runs first, sets headers before anything else
	)
	handler(w, r)
}

func (h *Handler) healthz(w http.ResponseWriter, r *http.Request) {
	handler := chain(
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet {
				writeJSON(w, http.StatusMethodNotAllowed, core.ErrorResponse{Error: "method not allowed"})
				return
			}
			writeJSON(w, http.StatusOK, h.svc.BuildHealth())
		},

		h.withCommonHeaders,
	)
	handler(w, r)
}

func (h *Handler) metrics(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, core.ErrorResponse{Error: "method not allowed"})
		return
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	if _, err := w.Write([]byte(h.svc.RenderMetrics())); err != nil {
		log.Printf("metrics write error: %v", err)
	}
}

// Register wires all routes. withMetrics is the outermost wrapper on every
// route — it sees the full request/response cycle including all inner middleware.
func (h *Handler) Register(mux *http.ServeMux) {
	mux.HandleFunc("/", h.withMetrics("/", h.welcome))
	mux.HandleFunc("/healthz", h.withMetrics("/healthz", h.healthz))
	mux.HandleFunc("/chaos", h.withMetrics("/chaos", h.chaos))
	mux.HandleFunc("/metrics", h.withMetrics("/metrics", h.metrics))
}

func NewHandler(svc core.ServicePort) *Handler {
	return &Handler{svc: svc}
}
