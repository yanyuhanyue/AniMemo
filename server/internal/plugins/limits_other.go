//go:build !linux

package plugins

// Development fallback retains process, WASI and deadline boundaries. Production
// support and hard CPU/address-space limits are verified on Linux containers.
func limitProcess() error { return nil }
