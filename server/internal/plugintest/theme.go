package plugintest

import (
	"animemo.local/server/pkg/pluginproto"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func Gallery(t *testing.T) pluginproto.Package {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	root := filepath.Join(filepath.Dir(file), "../../examples/notes-gallery")
	read := func(name string) []byte {
		data, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	var pack pluginproto.Package
	if err := json.Unmarshal(read("manifest.json"), &pack.Manifest); err != nil {
		t.Fatal(err)
	}
	pack.Module = []byte{}
	pack.Assets = map[string][]byte{}
	pack.Manifest.ModuleSHA256 = fmt.Sprintf("%x", sha256.Sum256(nil))
	p := pack.Manifest.NotesTheme.Presentation
	p.List = string(read("list.html"))
	p.Card = string(read("card.html"))
	p.Reader = string(read("reader.html"))
	p.CSS = string(read("theme.css"))
	for i := range p.Assets {
		a := &p.Assets[i]
		data := read(filepath.Join("assets", a.Name))
		a.SHA256 = fmt.Sprintf("%x", sha256.Sum256(data))
		pack.Assets[a.Name] = data
	}
	if pack.Manifest.JournalTheme != nil {
		j := &pack.Manifest.JournalTheme.Presentation
		j.List = string(read("journal-list.html"))
		j.Card = string(read("journal-card.html"))
		j.Row = string(read("journal-row.html"))
		j.Detail = string(read("journal-detail.html"))
		j.CSS = string(read("journal.css"))
		for i := range j.Assets {
			a := &j.Assets[i]
			data := read(filepath.Join("assets", a.Name))
			a.SHA256 = fmt.Sprintf("%x", sha256.Sum256(data))
			pack.Assets[a.Name] = data
		}
	}
	return pack
}
