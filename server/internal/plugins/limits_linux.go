//go:build linux

package plugins

import "golang.org/x/sys/unix"

func limitProcess() error {
	if err := unix.Setrlimit(unix.RLIMIT_CPU, &unix.Rlimit{Cur: 6, Max: 6}); err != nil {
		return err
	}
	return unix.Setrlimit(unix.RLIMIT_AS, &unix.Rlimit{Cur: 2 << 30, Max: 2 << 30})
}
