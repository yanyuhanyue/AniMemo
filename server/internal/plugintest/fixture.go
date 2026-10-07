// Package plugintest builds real WASI consumers for tests, without checked-in binaries.
package plugintest

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
)

func Build(t *testing.T, target string) []byte {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	root := filepath.Clean(filepath.Join(filepath.Dir(file), "../../.."))
	tmp := filepath.Join(root, ".local/tmp")
	if err := os.MkdirAll(tmp, 0700); err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp(tmp, "plugin-fixture-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	name := "go"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	cmd := exec.Command(filepath.Join(runtime.GOROOT(), "bin", name), "build", "-trimpath", "-ldflags=-s -w", "-o", filepath.Join(dir, "plugin.wasm"), target)
	cmd.Dir = filepath.Join(root, "server")
	cmd.Env = append(os.Environ(), "GOOS=wasip1", "GOARCH=wasm", "CGO_ENABLED=0", "GOTOOLCHAIN=local", "GOTELEMETRY=off")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build WASI consumer: %v\n%s", err, out)
	}
	data, err := os.ReadFile(filepath.Join(dir, "plugin.wasm"))
	if err != nil {
		t.Fatal(err)
	}
	return data
}
