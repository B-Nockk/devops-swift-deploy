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

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		log.Printf("writeJSON encode error: %v", err)
	}
}

func (h *Handler) withCommonHeaders(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Deployed-By", "swiftdeploy")
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
			if r.Method != http.MethodPost {
				writeJSON(w, http.StatusMethodNotAllowed, core.ErrorResponse{Error: "method not allowed"})
				return
			}
			writeJSON(w, http.StatusOK, h.svc.BuildHealth())
		},

		h.withCommonHeaders,
	)
	handler(w, r)
}

func (h *Handler) Register(mux *http.ServeMux) {
	mux.HandleFunc("/", h.welcome)
	mux.HandleFunc("/healthz", h.healthz)
	mux.HandleFunc("/chaos", h.chaos)
}

func NewHandler(svc core.ServicePort) *Handler {
	return &Handler{svc: svc}
}
