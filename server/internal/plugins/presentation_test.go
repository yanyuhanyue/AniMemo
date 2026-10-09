package plugins

import (
	"animemo.local/server/internal/plugintest"
	"animemo.local/server/pkg/pluginproto"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestThemePresentationPackage(t *testing.T) {
	p := plugintest.Gallery(t)
	raw, _ := json.Marshal(p)
	_, digest, err := ParsePackage(raw)
	if err != nil {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*pluginproto.Package){
		"old host":    func(p *pluginproto.Package) { p.Manifest.HostMin = 3 },
		"wrong scope": func(p *pluginproto.Package) { p.Manifest.NotesTheme.Presentation.Scope = "site" },
		"script":      func(p *pluginproto.Package) { p.Manifest.NotesTheme.Presentation.Card += "<script>alert(1)</script>" },
		"event handler": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.Card = strings.Replace(p.Manifest.NotesTheme.Presentation.Card, "<article", "<article onclick='alert(1)'", 1)
		},
		"foreign namespace": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.Card += "<svg onload='alert(1)'></svg>"
		},
		"external image": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.Card += "<img src='https://example.test/a.png' alt=''>"
		},
		"inline style": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.Card += "<div style='position:fixed'></div>"
		},
		"missing slot": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.Reader = "<div><slot name='metadata'></slot></div>"
		},
		"duplicate slot": func(p *pluginproto.Package) { p.Manifest.NotesTheme.Presentation.Reader += "<slot name='body'></slot>" },
		"unknown slot":   func(p *pluginproto.Package) { p.Manifest.NotesTheme.Presentation.Card += "<slot name='delete'></slot>" },
		"stylesheet import": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.CSS += "@import 'https://example.test/a.css';"
		},
		"external css url": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.CSS += ".a{background:url(https://example.test/a)}"
		},
		"image set": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.CSS += ".a{background:image-set('https://example.test/a' 1x)}"
		},
		"escaped url": func(p *pluginproto.Package) {
			p.Manifest.NotesTheme.Presentation.CSS += `.a{background:u\72l(https://example.test/a)}`
		},
		"fixed overlay":  func(p *pluginproto.Package) { p.Manifest.NotesTheme.Presentation.CSS += ".a{position:fixed}" },
		"host selector":  func(p *pluginproto.Package) { p.Manifest.NotesTheme.Presentation.CSS += ":host{display:none}" },
		"missing asset":  func(p *pluginproto.Package) { delete(p.Assets, "notebook.png") },
		"tampered asset": func(p *pluginproto.Package) { p.Assets["notebook.png"][10] ^= 1 },
		"extra asset":    func(p *pluginproto.Package) { p.Assets["extra.png"] = []byte("extra") },
		"invalid font": func(p *pluginproto.Package) {
			p.Assets["display.woff2"] = []byte("invalid")
			p.Manifest.NotesTheme.Presentation.Assets[1].SHA256 = fmt.Sprintf("%x", sha256.Sum256(p.Assets["display.woff2"]))
		},
	} {
		t.Run(name, func(t *testing.T) {
			p := plugintest.Gallery(t)
			change(&p)
			raw, _ := json.Marshal(p)
			if _, _, err := ParsePackage(raw); err == nil {
				t.Fatal("invalid presentation accepted")
			}
		})
	}
	p.Manifest.NotesTheme.Presentation.CSS += ".test{background:url('asset:notebook.png')}"
	raw, _ = json.Marshal(p)
	_, changed, err := ParsePackage(raw)
	if err != nil {
		t.Fatal(err)
	}
	if changed == digest {
		t.Fatal("template content not bound to package identity")
	}
}
