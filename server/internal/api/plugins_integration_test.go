//go:build integration

package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"

	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/plugins"
	"animemo.local/server/internal/plugintest"
	"animemo.local/server/pkg/pluginproto"
)

func pluginFile(t *testing.T, c *http.Client, base, path string, data []byte, want int) response {
	t.Helper()
	req, err := http.NewRequest("POST", base+path, bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Origin", base)
	req.Header.Set("Content-Type", "application/octet-stream")
	r, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	body, _ := io.ReadAll(r.Body)
	if r.StatusCode != want {
		t.Fatalf("%s: want %d got %d: %s", path, want, r.StatusCode, body)
	}
	return response{status: r.StatusCode, body: body}
}

func TestPluginLifecycleAndRealImport(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	ctx := context.Background()
	admin, user := registerBrowser(t, base, "plugin-admin@example.test")
	alice, _ := registerBrowser(t, base, "plugin-alice@example.test")
	bob, _ := registerBrowser(t, base, "plugin-bob@example.test")
	if _, err := pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, user.ID); err != nil {
		t.Fatal(err)
	}
	module := plugintest.Build(t, "./examples/watch-history-text")
	m := pluginproto.Manifest{Schema: 1, Slug: "watch-history-text", Name: "TXT", Version: "1.0.0", Protocol: 1, HostMin: 1, HostMax: 1, Capabilities: []string{"import.convert"}, ModuleSHA256: fmt.Sprintf("%x", sha256.Sum256(module))}
	pack := func(m pluginproto.Manifest) []byte {
		v, _ := json.Marshal(pluginproto.Package{Manifest: m, Module: module})
		return v
	}
	list := func(c *http.Client, path string) []plugins.Release {
		return decodeAs[struct {
			Items []plugins.Release `json:"items"`
		}](t, request(t, c, base, "GET", path, nil, 200)).Items
	}
	pluginFile(t, browser(), base, "/api/v1/admin/plugins", pack(m), 401)
	pluginFile(t, alice, base, "/api/v1/admin/plugins", pack(m), 403)
	pluginFile(t, admin, base, "/api/v1/admin/plugins", pack(m), 201)
	pluginFile(t, admin, base, "/api/v1/admin/plugins", pack(m), 201)
	if items := list(admin, "/api/v1/admin/plugins"); len(items) != 1 || items[0].Enabled || items[0].Revision != 1 {
		t.Fatalf("upload implicitly enabled or duplicated: %+v", items)
	}
	if len(list(alice, "/api/v1/plugins")) != 0 {
		t.Fatal("disabled plugin exposed")
	}
	mutated := m
	mutated.Name = "changed immutable package"
	pluginFile(t, admin, base, "/api/v1/admin/plugins", pack(mutated), 409)
	mutated = m
	mutated.Version = "2.0.0"
	mutated.HostMin = 2
	pluginFile(t, admin, base, "/api/v1/admin/plugins", pack(mutated), 400)
	actionPath := "/api/v1/admin/plugins/" + m.Slug
	activate := plugins.Action{Action: "activate", Version: m.Version, Revision: 1}
	request(t, alice, base, "POST", actionPath, activate, 403)
	request(t, admin, base, "POST", actionPath, activate, 200)
	request(t, admin, base, "POST", actionPath, activate, 409)
	if err := plugins.Preflight(ctx, pool); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(request(t, alice, base, "GET", "/api/v1/plugins", nil, 200).body), `"module":`) {
		t.Fatal("package binary exposed")
	}
	input := []byte("10月1日\n首刷 夏目友人帐 第1-3集\n10月2日\n二刷 夏目友人帐 第1集 -- 重温\n")
	path := "/api/v1/plugins/watch-history-text/imports?filename=2026.txt"
	job := decodeAs[journal.ImportJob](t, pluginFile(t, alice, base, path, input, 202))
	request(t, bob, base, "GET", "/api/v1/imports/"+job.ID, nil, 404)
	if got := decodeAs[journal.Page](t, request(t, alice, base, "GET", "/api/v1/entries", nil, 200)); got.Total != 0 {
		t.Fatal("plugin wrote before confirmation")
	}
	core := journal.New(pool)
	if _, err := core.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, alice, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.State != "ready" || job.Preview.Ready != 1 || job.Preview.Records != 2 || len(job.Preview.History) != 2 || job.Preview.History[1].Rewatch != 2 || job.Preview.History[1].Note != "重温" {
		t.Fatalf("bad preview: %+v", job)
	}
	request(t, admin, base, "POST", actionPath, plugins.Action{Action: "disable", Revision: 2}, 200)
	pluginFile(t, bob, base, path, input, 404)
	// A delivered conversion is now an ordinary user-owned import, not a delayed
	// plugin capability. User confirmation remains usable after plugin disable.
	request(t, alice, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "apply"}, 200)
	if _, err := core.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	page := decodeAs[journal.Page](t, request(t, alice, base, "GET", "/api/v1/entries", nil, 200))
	if page.Total != 1 || page.Items[0].WatchedEpisodes != 3 || page.Items[0].Visibility != "private" {
		t.Fatalf("bad imported entry: %+v", page)
	}
	history := decodeAs[journal.HistoryPage](t, request(t, alice, base, "GET", "/api/v1/history/page", nil, 200))
	if history.Total != 2 {
		t.Fatal("history lost")
	}
	if got := decodeAs[journal.Page](t, request(t, bob, base, "GET", "/api/v1/entries", nil, 200)); got.Total != 0 {
		t.Fatal("cross-owner write")
	}
	m.Version = "1.1.0"
	pluginFile(t, admin, base, "/api/v1/admin/plugins", pack(m), 201)
	request(t, admin, base, "POST", actionPath, plugins.Action{Action: "activate", Version: m.Version, Revision: 3}, 200)
	request(t, admin, base, "POST", actionPath, plugins.Action{Action: "activate", Version: "1.0.0", Revision: 4}, 200)
	if active := list(alice, "/api/v1/plugins"); len(active) != 1 || active[0].Manifest.Version != "1.0.0" {
		t.Fatal("version rollback failed")
	}
	// Optimistic revision + transaction serialize competing administrators.
	results := make([]response, 2)
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for i := range 2 {
		wg.Go(func() {
			results[i], errs[i] = send(admin, base, "POST", actionPath, plugins.Action{Action: "disable", Revision: 5}, base)
		})
	}
	wg.Wait()
	if errs[0] != nil || errs[1] != nil || !((results[0].status == 200 && results[1].status == 409) || (results[1].status == 200 && results[0].status == 409)) {
		t.Fatalf("concurrent activation: %+v %v", results, errs)
	}
	if _, err := pool.Exec(ctx, `UPDATE users SET is_admin=false WHERE id=$1`, user.ID); err != nil {
		t.Fatal(err)
	}
	request(t, admin, base, "POST", actionPath, plugins.Action{Action: "activate", Version: m.Version, Revision: 6}, 403)
	var audit int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM audit_log WHERE action LIKE 'plugin.%'`).Scan(&audit); err != nil || audit < 6 {
		t.Fatalf("missing plugin audit: %d %v", audit, err)
	}
}

func TestPluginUpgradePreflight(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	ctx := context.Background()
	_, admin := registerBrowser(t, base, "preflight@example.test")
	if _, err := pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, admin.ID); err != nil {
		t.Fatal(err)
	}
	module := plugintest.Build(t, "./examples/watch-history-text")
	m := pluginproto.Manifest{Schema: 1, Slug: "preflight", Name: "Preflight", Version: "1.0.0", Protocol: 1, HostMin: 1, HostMax: 1, Capabilities: []string{"import.convert"}, ModuleSHA256: fmt.Sprintf("%x", sha256.Sum256(module))}
	data, _ := json.Marshal(pluginproto.Package{Manifest: m, Module: module})
	service := plugins.New(pool)
	if err := service.Install(ctx, admin.ID, data); err != nil {
		t.Fatal(err)
	}
	if err := service.Change(ctx, admin.ID, m.Slug, plugins.Action{Action: "activate", Version: m.Version, Revision: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE plugin_releases SET manifest=jsonb_set(manifest,'{host_api_min}','2')`); err != nil {
		t.Fatal(err)
	}
	if err := plugins.Preflight(ctx, pool); err == nil || !strings.Contains(err.Error(), "incompatible") {
		t.Fatalf("incompatible active package passed: %v", err)
	}
	if err := service.Change(ctx, admin.ID, m.Slug, plugins.Action{Action: "disable", Revision: 2}); err != nil {
		t.Fatal(err)
	}
	if err := plugins.Preflight(ctx, pool); err != nil {
		t.Fatalf("disabled package blocked recovery: %v", err)
	}
	if err := service.Change(ctx, admin.ID, m.Slug, plugins.Action{Action: "activate", Version: m.Version, Revision: 3}); err == nil {
		t.Fatal("incompatible package enabled")
	}
	if _, err := pool.Exec(ctx, `UPDATE plugin_releases SET manifest=$1,module='corrupted'::bytea`, m); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE plugin_deployments SET enabled=true`); err != nil {
		t.Fatal(err)
	}
	if err := plugins.Preflight(ctx, pool); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("corrupt package passed preflight: %v", err)
	}
	if _, err := pool.Exec(ctx, `DROP TABLE plugin_deployments,plugin_releases`); err != nil {
		t.Fatal(err)
	}
	if err := plugins.Preflight(ctx, pool); err != nil {
		t.Fatalf("pre-plugin database cannot upgrade: %v", err)
	}
}
