//go:build integration

package api

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"animemo.local/server/internal/plugins"
	"animemo.local/server/internal/plugintest"
)

func TestJournalThemeAndVersionCleanup(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	ctx := context.Background()
	admin, owner := registerBrowser(t, base, "journal-theme-admin@example.test")
	alice, _ := registerBrowser(t, base, "journal-theme-alice@example.test")
	bob, _ := registerBrowser(t, base, "journal-theme-bob@example.test")
	if _, err := pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, owner.ID); err != nil {
		t.Fatal(err)
	}
	pack := plugintest.Gallery(t)
	upload := func(status int) {
		raw, _ := json.Marshal(pack)
		pluginFile(t, admin, base, "/api/v1/admin/plugins", raw, status)
	}
	action := func(actor *http.Client, name, version string, revision int64, status int) {
		request(t, actor, base, "POST", "/api/v1/admin/plugins/notes-gallery", plugins.Action{Action: name, Version: version, Revision: revision}, status)
	}
	list := func(actor *http.Client, scope string) plugins.ThemeOptions {
		return decodeAs[plugins.ThemeOptions](t, request(t, actor, base, "GET", "/api/v1/themes?scope="+scope, nil, 200))
	}
	inventory := func() []plugins.Release {
		return decodeAs[struct {
			Items []plugins.Release `json:"items"`
		}](t, request(t, admin, base, "GET", "/api/v1/admin/plugins", nil, 200)).Items
	}
	selectTheme := func(scope string, revision int64) {
		request(t, alice, base, "PUT", "/api/v1/themes/selection", plugins.ThemeSelection{Slug: "notes-gallery", Revision: revision, Scope: scope}, 200)
	}
	upload(201)
	upload(201)
	if len(inventory()) != 1 {
		t.Fatal("repeated install duplicated package")
	}
	initial := inventory()[0]
	if initial.StorageBytes <= 0 || initial.CanRemove || !initial.CanUninstall {
		t.Fatal("incorrect initial package storage/lifecycle")
	}
	var assetCount int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM plugin_releases r,jsonb_object_keys(r.assets) WHERE slug='notes-gallery'`).Scan(&assetCount); err != nil || assetCount != 3 {
		t.Fatalf("shared resources duplicated: %d %v", assetCount, err)
	}
	action(admin, "activate", "1.1.0", 1, 200)
	selectTheme("private.journal", 2)
	if list(alice, "private.journal").SelectedSlug != "notes-gallery" || list(alice, "private.notes").SelectedSlug != "" || list(bob, "private.journal").SelectedSlug != "" {
		t.Fatal("theme scope/user selection leaked")
	}
	selectTheme("private.notes", 2)
	request(t, alice, base, "PUT", "/api/v1/themes/selection", plugins.ThemeSelection{Scope: "private.journal"}, 200)
	if list(alice, "private.notes").SelectedSlug != "notes-gallery" {
		t.Fatal("reset changed another surface")
	}
	selectTheme("private.journal", 2)
	request(t, alice, base, "GET", "/api/v1/themes?scope=public", nil, 400)
	request(t, alice, base, "PUT", "/api/v1/themes/selection", plugins.ThemeSelection{Scope: "site"}, 400)

	pack.Manifest.Version = "1.2.0"
	upload(201)
	action(bob, "remove_version", "1.2.0", 2, 403)
	action(admin, "remove_version", "1.1.0", 2, 400) // Even a disabled current version stays protected.
	action(admin, "activate", "1.2.0", 2, 200)
	action(admin, "remove_version", "1.1.0", 2, 409)
	var old plugins.Release
	for _, item := range inventory() {
		if item.Manifest.Version == "1.1.0" {
			old = item
		}
	}
	if !old.CanRemove {
		t.Fatal("unused version is not removable")
	}
	action(admin, "remove_version", "1.1.0", 3, 200)
	items := inventory()
	if len(items) != 1 || items[0].Revision != 3 || items[0].Manifest.Version != "1.2.0" || items[0].StorageBytes != initial.StorageBytes {
		t.Fatal("cleanup changed active revision or did not free old resource data")
	}
	if list(alice, "private.journal").SelectedSlug != "notes-gallery" {
		t.Fatal("cleanup reset preference")
	}
	request(t, alice, base, "GET", "/api/v1/themes/notes-gallery/1.1.0/assets/notebook.png", nil, 404)
	action(admin, "activate", "1.1.0", 3, 404)
	pack.Manifest.Version = "1.1.0"
	pack.Manifest.Description += "changed"
	upload(409) // Removed bytes cannot allow changing a known version identity.
	pack = plugintest.Gallery(t)
	upload(201)
	if len(inventory()) != 2 {
		t.Fatal("identical historical package could not be reinstalled")
	}
	if _, err := pool.Exec(ctx, `INSERT INTO bundled_extensions(slug,version,digest,core_sha256) SELECT slug,version,digest,repeat('a',64) FROM plugin_releases WHERE slug='notes-gallery' AND version='1.1.0'`); err != nil {
		t.Fatal(err)
	}
	for _, item := range inventory() {
		if item.CanUninstall || (item.Manifest.Version == "1.1.0" && item.CanRemove) {
			t.Fatal("bundled baseline not protected")
		}
	}
	action(admin, "remove_version", "1.1.0", 3, 400)
	if _, err := pool.Exec(ctx, `UPDATE bundled_extensions SET version='1.2.0' WHERE slug='notes-gallery'`); err != nil {
		t.Fatal(err)
	}
	action(admin, "remove_version", "1.1.0", 3, 200) // Older bundled releases can be cleaned once superseded.
	action(admin, "disable", "1.2.0", 3, 200)
	if list(alice, "private.notes").SelectedSlug != "" || list(alice, "private.journal").SelectedSlug != "" {
		t.Fatal("disabled theme did not fall back on both surfaces")
	}
	action(admin, "remove_version", "1.2.0", 4, 400)
}
