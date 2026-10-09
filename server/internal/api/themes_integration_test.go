//go:build integration

package api

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"testing"

	"animemo.local/server/internal/plugins"
	"animemo.local/server/pkg/pluginproto"
)

func TestNotesThemeLifecycle(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	ctx := context.Background()
	admin, owner := registerBrowser(t, base, "theme-admin@example.test")
	alice, _ := registerBrowser(t, base, "theme-alice@example.test")
	bob, _ := registerBrowser(t, base, "theme-bob@example.test")
	if _, err := pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, owner.ID); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile("../../examples/notes-hanami/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest pluginproto.Manifest
	if err = json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	manifest.ModuleSHA256 = fmt.Sprintf("%x", sha256.Sum256(nil))
	pack := func() []byte {
		v, _ := json.Marshal(pluginproto.Package{Manifest: manifest, Module: []byte{}})
		return v
	}
	list := func(c *http.Client) plugins.ThemeOptions {
		return decodeAs[plugins.ThemeOptions](t, request(t, c, base, "GET", "/api/v1/themes", nil, 200))
	}
	choose := func(c *http.Client, slug string, revision int64, status int) {
		request(t, c, base, "PUT", "/api/v1/themes/selection", plugins.ThemeSelection{Slug: slug, Revision: revision}, status)
	}
	action := func(name, version string, revision int64) {
		request(t, admin, base, "POST", "/api/v1/admin/plugins/notes-hanami", plugins.Action{Action: name, Version: version, Revision: revision}, 200)
	}
	request(t, browser(), base, "GET", "/api/v1/themes", nil, 401)
	choose(browser(), manifest.Slug, 1, 401)
	pluginFile(t, alice, base, "/api/v1/admin/plugins", pack(), 403)
	pluginFile(t, admin, base, "/api/v1/admin/plugins", pack(), 201)
	if got := list(alice); len(got.Items) != 0 || got.SelectedSlug != "" {
		t.Fatal("upload activated the theme")
	}
	choose(alice, manifest.Slug, 1, 404)
	action("activate", manifest.Version, 1)
	options := list(alice)
	if len(options.Items) != 1 || options.Items[0].PublisherID == "ANIMEMO_FIRST_PARTY" {
		t.Fatal("uploaded theme incorrectly claimed official identity")
	}
	choose(alice, manifest.Slug, 2, 200)
	if list(alice).SelectedSlug != manifest.Slug || list(bob).SelectedSlug != "" {
		t.Fatal("theme selection not isolated per account")
	}
	if converters := decodeAs[struct {
		Items []plugins.Release `json:"items"`
	}](t, request(t, alice, base, "GET", "/api/v1/plugins", nil, 200)); len(converters.Items) != 0 {
		t.Fatal("theme offered as a file converter")
	}
	pluginFile(t, alice, base, "/api/v1/plugins/notes-hanami/imports?filename=notes.txt", []byte("private input"), 400)
	if err = plugins.Preflight(ctx, pool); err != nil {
		t.Fatal(err)
	}
	if err = plugins.QuarantineInvalid(ctx, pool); err != nil {
		t.Fatal(err)
	}
	if list(alice).SelectedSlug != manifest.Slug {
		t.Fatal("declarative theme was quarantined as invalid WASM")
	}
	// A page that was open before withdrawal cannot reselect the disabled package.
	action("disable", "", 2)
	choose(bob, manifest.Slug, 2, 404)
	if got := list(alice); len(got.Items) != 0 || got.SelectedSlug != "" {
		t.Fatal("disabled theme did not fall back to Core")
	}
	action("activate", manifest.Version, 3)
	choose(alice, "", 0, 200)
	if list(alice).SelectedSlug != "" {
		t.Fatal("restore default failed")
	}
	// Uploading another version never switches the active theme; its appearance is immutable.
	manifest.Version = "1.1.0"
	manifest.NotesTheme.ReadingSize = "large"
	pluginFile(t, admin, base, "/api/v1/admin/plugins", pack(), 201)
	if list(alice).Items[0].Manifest.Version != "1.0.0" {
		t.Fatal("upload switched active theme")
	}
	manifest.NotesTheme.ReadingSize = "standard"
	pluginFile(t, admin, base, "/api/v1/admin/plugins", pack(), 409)
	action("activate", "1.1.0", 4)
	choose(alice, manifest.Slug, 4, 409)
	choose(alice, manifest.Slug, 5, 200)
	action("activate", "1.0.0", 5)
	if list(alice).Items[0].Manifest.NotesTheme.ReadingSize != "standard" {
		t.Fatal("theme rollback failed")
	}
	action("uninstall", "", 6)
	if got := list(alice); len(got.Items) != 0 || got.SelectedSlug != "" {
		t.Fatal("uninstall left a theme selection")
	}
	var count int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM user_note_themes`).Scan(&count); err != nil || count != 0 {
		t.Fatal("orphaned preferences", err)
	}
}

func TestBundledNotesThemeIntegrity(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	directory := t.TempDir()
	binary := []byte("exact test core")
	core := directory + "/core"
	if err := os.WriteFile(core, binary, 0600); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile("../../examples/notes-hanami/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest pluginproto.Manifest
	if err = json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	manifest.ModuleSHA256 = fmt.Sprintf("%x", sha256.Sum256(nil))
	pack, _ := json.Marshal(pluginproto.Package{Manifest: manifest, Module: []byte{}})
	file := manifest.Slug + ".animemo-plugin"
	inventory, _ := json.Marshal(map[string]any{"schema": "animemo.bundled/v1", "core_sha256": fmt.Sprintf("%x", sha256.Sum256(binary)), "packages": []map[string]string{{"file": file, "sha256": fmt.Sprintf("%x", sha256.Sum256(pack))}}})
	for name, data := range map[string][]byte{file: pack, "bundled-extensions.json": inventory} {
		if err = os.WriteFile(directory+"/"+name, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err = plugins.EnsureBundled(ctx, pool, directory, core); err != nil {
		t.Fatal(err)
	}
	service := plugins.New(pool)
	releases, err := service.List(ctx, true)
	if err != nil || len(releases) != 1 || releases[0].PublisherID != "ANIMEMO_FIRST_PARTY" || releases[0].Enabled {
		t.Fatal("bundled theme identity/activation", err)
	}
	if err = os.WriteFile(core, []byte("other core"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = plugins.EnsureBundled(ctx, pool, directory, core); err == nil {
		t.Fatal("mismatched core accepted")
	}
	if _, err = pool.Exec(ctx, `UPDATE plugin_deployments SET enabled=true; UPDATE plugin_releases SET module=decode('00','hex')`); err != nil {
		t.Fatal(err)
	}
	if err = plugins.Preflight(ctx, pool); err == nil {
		t.Fatal("damaged theme passed preflight")
	}
	if err = plugins.QuarantineInvalid(ctx, pool); err != nil {
		t.Fatal(err)
	}
	releases, err = service.List(ctx, true)
	if err != nil || releases[0].Enabled || releases[0].Health != "quarantined" {
		t.Fatal("damaged theme was not isolated", err)
	}
}
