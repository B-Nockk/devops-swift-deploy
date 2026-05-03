package store

import (
	"swiftdeploy/internal/core"
	"sync"
)

type MemoryChaosStore struct {
	mu    sync.RWMutex
	state core.ChaosState
}

func NewMemoryChaosStore() *MemoryChaosStore {
	return &MemoryChaosStore{
		state: core.ChaosState{Active: core.ChaosModeNone},
	}
}

func (s *MemoryChaosStore) Get() (*core.ChaosState, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return &s.state, nil
}

func (s *MemoryChaosStore) Set(state *core.ChaosState) error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	s.state = *state
	return nil
}
