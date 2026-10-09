package plugins

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	"animemo.local/server/pkg/pluginproto"
)

func hanamiPackage(t *testing.T) pluginproto.Package {
	t.Helper()
	raw, err := os.ReadFile("../../examples/notes-hanami/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var m pluginproto.Manifest
	if err = json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	m.ModuleSHA256 = fmt.Sprintf("%x", sha256.Sum256(nil))
	return pluginproto.Package{Manifest: m, Module: []byte{}}
}
func TestNotesThemePackage(t *testing.T) {
	p := hanamiPackage(t)
	raw, _ := json.Marshal(p)
	parsed, digest, err := ParsePackage(raw)
	if err != nil || digest == "" || parsed.Module == nil {
		t.Fatalf("valid declarative package: %v", err)
	}
	if err = validatePackageRuntime(context.Background(), parsed.Manifest, parsed.Module); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*pluginproto.Package){
		"older host":               func(p *pluginproto.Package) { p.Manifest.HostMin = 2 },
		"CSS payload":              func(p *pluginproto.Package) { p.Manifest.NotesTheme.Paper = "url(https://example.test/)" },
		"unreadable text":          func(p *pluginproto.Package) { p.Manifest.NotesTheme.Ink = "#ffffff" },
		"unsupported dark surface": func(p *pluginproto.Package) { p.Manifest.NotesTheme.Paper = "#151515" },
		"external font":            func(p *pluginproto.Package) { p.Manifest.NotesTheme.HeadingFont = "https://example.test/font" },
		"unbounded spacing":        func(p *pluginproto.Package) { p.Manifest.NotesTheme.Spacing = "900px" },
		"mixed capabilities": func(p *pluginproto.Package) {
			p.Manifest.Capabilities = append(p.Manifest.Capabilities, "import.convert")
		},
		"executable theme": func(p *pluginproto.Package) {
			p.Module = []byte("code")
			p.Manifest.ModuleSHA256 = fmt.Sprintf("%x", sha256.Sum256(p.Module))
		},
		"theme masquerades as converter": func(p *pluginproto.Package) { p.Manifest.Capabilities = []string{"import.convert"} },
		"missing theme":                  func(p *pluginproto.Package) { p.Manifest.NotesTheme = nil },
	} {
		t.Run(name, func(t *testing.T) {
			candidate := hanamiPackage(t)
			mutate(&candidate)
			data, _ := json.Marshal(candidate)
			if _, _, err := ParsePackage(data); err == nil {
				t.Fatal("invalid theme accepted")
			}
		})
	}
	unknown := strings.Replace(string(raw), `"notes_theme":{`, `"notes_theme":{"css":"body{display:none}",`, 1)
	if _, _, err := ParsePackage([]byte(unknown)); err == nil {
		t.Fatal("undeclared CSS accepted")
	}
	p.Manifest.NotesTheme.ReadingSize = "large"
	data, _ := json.Marshal(p)
	_, changed, err := ParsePackage(data)
	if err != nil || changed == digest {
		t.Fatal("theme contents must be part of immutable package identity")
	}
}
