package core

type ChaosStore interface {
	Get() (ChaosState, error)
	Set(ChaosState) error
}

type ServicePort interface {
	BuildWelcome() WelcomeResponse
	BuildHealth() HealthResponse
	ApplyChaos(ChaosCommand) (ChaosResponse, error)
	GetChaosState() (ChaosState, error)
	IsCanary() bool
}
