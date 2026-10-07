package plugins

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"animemo.local/server/internal/plugintest"
	"animemo.local/server/pkg/pluginproto"
)

func TestRealWASISandbox(t *testing.T) {
	module := plugintest.Build(t, "./internal/plugins/testdata/sandbox")
	t.Setenv("ANIMEMO_PLUGIN_TEST_SECRET", "synthetic-must-not-inherit")
	for _, mode := range []string{"inspect", "loop", "output", "memory", "trap", "protocol", "unknown"} {
		t.Run(mode, func(t *testing.T) {
			start := time.Now()
			out, err := Execute(context.Background(), module, pluginproto.Request{Protocol: 1, Filename: "file.txt", Text: mode})
			if mode == "inspect" {
				if err != nil || out.Data != "title\nisolated\n" {
					t.Fatalf("sandbox: %+v %v", out, err)
				}
			} else {
				if err == nil || !strings.HasPrefix(err.Error(), "plugin_failed:") {
					t.Fatalf("expected guest execution failure, got %v", err)
				}
				if strings.Contains(err.Error(), "private-file-content") {
					t.Fatal("guest diagnostics leaked")
				}
			}
			if time.Since(start) > RunTimeout+2*time.Second {
				t.Fatal("execution was not bounded")
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := Execute(ctx, module, pluginproto.Request{Protocol: 1, Filename: "file.txt", Text: "loop"}); err == nil {
		t.Fatal("cancelled execution succeeded")
	}
}

func TestPackageIdentityAndCompatibility(t *testing.T) {
	module := plugintest.Build(t, "./examples/watch-history-text")
	m := pluginproto.Manifest{Schema: 1, Slug: "watch-history-text", Name: "TXT", Version: "1.0.0", Protocol: 1, HostMin: 1, HostMax: 1, Capabilities: []string{"import.convert"}, ModuleSHA256: fmt.Sprintf("%x", sha256.Sum256(module))}
	pack := func(m pluginproto.Manifest, b []byte) []byte {
		out, _ := json.Marshal(pluginproto.Package{Manifest: m, Module: b})
		return out
	}
	_, digest, err := ParsePackage(pack(m, module))
	if err != nil || digest == "" {
		t.Fatal(err)
	}
	for _, mutate := range []func(*pluginproto.Manifest){func(m *pluginproto.Manifest) { m.Protocol = 2 }, func(m *pluginproto.Manifest) { m.Schema = 2 }, func(m *pluginproto.Manifest) { m.HostMin = 2 }, func(m *pluginproto.Manifest) { m.Capabilities = []string{"journal.write"} }, func(m *pluginproto.Manifest) { m.Slug = "../escape" }, func(m *pluginproto.Manifest) { m.Version = "01.0.0" }, func(m *pluginproto.Manifest) { m.ModuleSHA256 = strings.Repeat("0", 64) }} {
		copy := m
		mutate(&copy)
		if _, _, err := ParsePackage(pack(copy, module)); err == nil {
			t.Fatalf("invalid manifest accepted: %+v", copy)
		}
	}
	if _, _, err := ParsePackage(append(pack(m, module), []byte(`{}`)...)); err == nil {
		t.Fatal("trailing payload accepted")
	}
	if err := ValidateModule(context.Background(), []byte("invalid")); err == nil {
		t.Fatal("non-WASM module accepted")
	}
	// Still a valid WASM binary, but its imported WASI function does not exist.
	unknownImport := bytes.Replace(module, []byte("fd_write"), []byte("xx_write"), 1)
	if bytes.Equal(module, unknownImport) {
		t.Fatal("consumer lacks fd_write fixture")
	}
	if err := ValidateModule(context.Background(), unknownImport); err == nil {
		t.Fatal("unknown WASI import passed installation/preflight")
	}
	// A valid exported function, but _start incorrectly expects an i32 argument.
	badEntry, _ := hex.DecodeString("0061736d0100000001050160017f0003020100070a01065f737461727400000a040102000b")
	if err := ValidateModule(context.Background(), badEntry); err == nil {
		t.Fatal("invalid entry ABI passed installation/preflight")
	}
	out, err := Execute(context.Background(), module, pluginproto.Request{Protocol: 1, Filename: "2026记录.txt", Text: "10月1日\n首刷 夏目友人帐 第1-3集\n10月2日\n二刷 夏目友人帐 第1集 -- 重温\n"})
	if err != nil || !strings.Contains(out.Data, `"rewatch":2`) || !strings.Contains(out.Data, "2026-10-02") {
		t.Fatalf("reference consumer: %v %+v", err, out)
	}
}
