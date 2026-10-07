//go:build integration

package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"animemo.local/server/internal/database"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func isolatedDatabase(t *testing.T) *pgxpool.Pool {
	t.Helper()
	connection := os.Getenv("TEST_DATABASE_URL")
	if connection == "" {
		t.Fatal("TEST_DATABASE_URL is required for integration tests; run npm run test:api")
	}
	ctx := context.Background()
	admin, err := pgxpool.New(ctx, connection)
	if err != nil {
		t.Fatal(err)
	}
	schema := "test_" + strings.ReplaceAll(id.New(), "-", "")
	if _, err = admin.Exec(ctx, "CREATE SCHEMA "+pgx.Identifier{schema}.Sanitize()); err != nil {
		admin.Close()
		t.Fatal(err)
	}
	config, err := pgxpool.ParseConfig(connection)
	if err != nil {
		t.Fatal(err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Close()
		_, err := admin.Exec(context.Background(), "DROP SCHEMA "+pgx.Identifier{schema}.Sanitize()+" CASCADE")
		admin.Close()
		if err != nil {
			t.Errorf("clean test schema: %v", err)
		}
	})
	if err = database.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	return pool
}

func browser() *http.Client {
	jar, _ := cookiejar.New(nil)
	return &http.Client{Jar: jar, Timeout: 10 * time.Second}
}

type response struct {
	status  int
	body    []byte
	cookies []*http.Cookie
	header  http.Header
}

func send(client *http.Client, base, method, path string, body any, origin string) (response, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return response{}, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, base+path, reader)
	if err != nil {
		return response{}, err
	}
	if method != "GET" {
		req.Header.Set("Origin", origin)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := client.Do(req)
	if err != nil {
		return response{}, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	return response{status: resp.StatusCode, body: data, cookies: resp.Cookies(), header: resp.Header}, err
}

func request(t *testing.T, client *http.Client, base, method, path string, body any, want int) response {
	t.Helper()
	out, err := send(client, base, method, path, body, base)
	if err != nil {
		t.Fatal(err)
	}
	if out.status != want {
		t.Fatalf("%s %s: want %d, got %d: %s", method, path, want, out.status, out.body)
	}
	return out
}

func decodeAs[T any](t *testing.T, r response) T {
	t.Helper()
	var value T
	if err := json.Unmarshal(r.body, &value); err != nil {
		t.Fatalf("decode response: %v: %s", err, r.body)
	}
	return value
}

func TestJournalHTTPWorkflow(t *testing.T) {
	pool := isolatedDatabase(t)
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base})
	server.Start()
	defer server.Close()
	alice, bob := browser(), browser()
	request(t, alice, base, "GET", "/api/v1/entries", nil, 401)
	registration := map[string]any{"email": "alice@example.test", "display_name": "小春", "password": "test-passphrase-2026"}
	registered := request(t, alice, base, "POST", "/api/v1/auth/register", registration, 201)
	if len(registered.cookies) != 1 || !registered.cookies[0].HttpOnly || registered.cookies[0].SameSite != http.SameSiteLaxMode || registered.cookies[0].MaxAge <= 0 {
		t.Fatal("registration did not establish a protected cookie")
	}
	if registered.header.Get("Cache-Control") != "private, no-store" {
		t.Fatal("private response is cacheable")
	}
	if strings.Contains(string(registered.body), "password") || strings.Contains(string(registered.body), "token") {
		t.Fatal("credentials leaked in response")
	}
	request(t, alice, base, "GET", "/api/v1/auth/me", nil, 200)
	unknown := map[string]any{"title": "forbidden owner", "user_id": id.New()}
	request(t, alice, base, "POST", "/api/v1/entries", unknown, 400)
	entry := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "葬送的芙莉莲", "total_episodes": 12, "tags": []string{"治愈", "治愈", "冒险"}}, 201))
	if !id.Valid(entry.ID) || entry.Version != 1 || len(entry.Tags) != 2 {
		t.Fatalf("bad new entry: %+v", entry)
	}
	path := "/api/v1/entries/" + entry.ID
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "PATCH", path, map[string]any{"version": 1, "notes": "想多了解一个人。", "status": "watching"}, 200))
	request(t, alice, base, "PATCH", path, map[string]any{"version": 1, "title": "stale"}, 409)
	if entry.Version != 2 || entry.Title != "葬送的芙莉莲" {
		t.Fatal("partial update changed untouched fields")
	}

	request(t, bob, base, "POST", "/api/v1/auth/register", map[string]any{"email": "bob@example.test", "display_name": "小夏", "password": "another-passphrase-2026"}, 201)
	for _, method := range []string{"GET", "PATCH", "DELETE"} {
		var body any
		target := path
		if method == "PATCH" {
			body = map[string]any{"version": entry.Version, "title": "stolen"}
		}
		if method == "DELETE" {
			target += "?version=2"
		}
		request(t, bob, base, method, target, body, 404)
	}
	request(t, bob, base, "GET", path+"/history", nil, 404)
	bobPage := decodeAs[journal.Page](t, request(t, bob, base, "GET", "/api/v1/entries", nil, 200))
	if bobPage.Total != 0 || len(bobPage.Items) != 0 {
		t.Fatal("entries leaked to another account")
	}
	bobExport := decodeAs[journal.Export](t, request(t, bob, base, "GET", "/api/v1/export", nil, 200))
	if len(bobExport.Entries) != 0 || len(bobExport.History) != 0 {
		t.Fatal("export leaked another account")
	}

	watch := journal.RecordInput{WatchedOn: "2026-10-07", EpisodeFrom: 1, EpisodeTo: 3, Note: "慢慢走，也很好。", RequestID: id.New()}
	request(t, bob, base, "POST", path+"/history", watch, 404)
	saved := decodeAs[journal.RecordResult](t, request(t, alice, base, "POST", path+"/history", watch, 201))
	replayed := decodeAs[journal.RecordResult](t, request(t, alice, base, "POST", path+"/history", watch, 201))
	if saved.Record.ID != replayed.Record.ID || replayed.Entry.Version != 3 || replayed.Entry.WatchedEpisodes != 3 {
		t.Fatal("retry duplicated viewing or progress")
	}
	changed := watch
	changed.Note = "different content"
	request(t, alice, base, "POST", path+"/history", changed, 409)
	tooFar := watch
	tooFar.RequestID = id.New()
	tooFar.EpisodeTo = 13
	request(t, alice, base, "POST", path+"/history", tooFar, 400)
	csrf, err := send(alice, base, "PATCH", path, map[string]any{"version": 3, "title": "cross origin"}, "https://attacker.example")
	if err != nil || csrf.status != 403 {
		t.Fatalf("cross-origin write accepted: %d %v", csrf.status, err)
	}

	// Both browser tabs saw version 3. Exactly one write may commit.
	statuses := make(chan int, 2)
	problems := make(chan error, 2)
	var group sync.WaitGroup
	for _, patch := range []map[string]any{{"version": 3, "title": "芙莉莲·重看"}, {"version": 3, "notes": "新的短评"}} {
		group.Add(1)
		go func(body map[string]any) {
			defer group.Done()
			r, e := send(alice, base, "PATCH", path, body, base)
			statuses <- r.status
			problems <- e
		}(patch)
	}
	group.Wait()
	close(statuses)
	close(problems)
	for err := range problems {
		if err != nil {
			t.Fatal(err)
		}
	}
	counts := map[int]int{}
	for code := range statuses {
		counts[code]++
	}
	if counts[200] != 1 || counts[409] != 1 {
		t.Fatalf("concurrent writes not protected: %v", counts)
	}
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "GET", path, nil, 200))
	if entry.Version != 4 {
		t.Fatal("conflicting write advanced version")
	}

	// Identical concurrent submissions commit one viewing event, including after a retry.
	watch.RequestID = id.New()
	watch.EpisodeFrom = 4
	watch.EpisodeTo = 12
	results := make(chan response, 2)
	watchErrors := make(chan error, 2)
	for range 2 {
		group.Add(1)
		go func() {
			defer group.Done()
			r, e := send(alice, base, "POST", path+"/history", watch, base)
			results <- r
			watchErrors <- e
		}()
	}
	group.Wait()
	close(results)
	close(watchErrors)
	for err := range watchErrors {
		if err != nil {
			t.Fatal(err)
		}
	}
	for r := range results {
		if r.status != 201 {
			t.Fatalf("watch failed: %d %s", r.status, r.body)
		}
	}
	stats := decodeAs[journal.Stats](t, request(t, alice, base, "GET", "/api/v1/stats", nil, 200))
	if stats.WatchRecords != 2 || stats.Completed != 1 || stats.WatchedEpisodes != 12 {
		t.Fatalf("wrong aggregate after viewing: %+v", stats)
	}
	exported := decodeAs[journal.Export](t, request(t, alice, base, "GET", "/api/v1/export", nil, 200))
	if exported.Schema != "animemo.journal/v1" || len(exported.Entries) != 1 || len(exported.History) != 2 || exported.History[0].EntryID != exported.Entries[0].ID {
		t.Fatal("export is incomplete")
	}

	request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "100% 的夏天", "tags": []string{"校园"}}, 201)
	filtered := decodeAs[journal.Page](t, request(t, alice, base, "GET", "/api/v1/entries?search="+url.QueryEscape("%"), nil, 200))
	if filtered.Total != 1 || filtered.Items[0].Title != "100% 的夏天" {
		t.Fatal("search treats literal percent as a wildcard")
	}
	byTag := decodeAs[journal.Page](t, request(t, alice, base, "GET", "/api/v1/entries?search="+url.QueryEscape("校园"), nil, 200))
	if byTag.Total != 1 {
		t.Fatal("tag search failed")
	}
	request(t, alice, base, "GET", "/api/v1/entries?page=0", nil, 400)
	request(t, alice, base, "GET", "/api/v1/entries?status=invalid", nil, 400)

	request(t, alice, base, "DELETE", path+"?version=4", nil, 409)
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "GET", path, nil, 200))
	request(t, alice, base, "DELETE", fmt.Sprintf("%s?version=%d", path, entry.Version), nil, 204)
	request(t, alice, base, "GET", path, nil, 404)
	stats = decodeAs[journal.Stats](t, request(t, alice, base, "GET", "/api/v1/stats", nil, 200))
	if stats.Total != 1 || stats.WatchRecords != 0 {
		t.Fatal("delete did not remove associated history")
	}
	oldCookie := registered.cookies[0]
	request(t, alice, base, "POST", "/api/v1/auth/logout", nil, 204)
	request(t, alice, base, "GET", "/api/v1/auth/me", nil, 401)
	replayClient := browser()
	u, _ := url.Parse(base)
	replayClient.Jar.SetCookies(u, []*http.Cookie{oldCookie})
	request(t, replayClient, base, "GET", "/api/v1/auth/me", nil, 401)
	request(t, alice, base, "POST", "/api/v1/auth/login", map[string]any{"email": "alice@example.test", "password": "wrong password"}, 401)
	request(t, alice, base, "POST", "/api/v1/auth/login", map[string]any{"email": "ALICE@EXAMPLE.TEST", "password": "test-passphrase-2026"}, 200)
	request(t, alice, base, "GET", "/api/v1/auth/me", nil, 200)
	request(t, bob, base, "GET", "/api/v1/auth/me", nil, 200)
}

func TestMigrationsAreIdempotentAndDetectEditedHistory(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatalf("repeat migration: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE schema_migrations SET checksum='modified'`); err != nil {
		t.Fatal(err)
	}
	if database.Migrate(ctx, pool) == nil {
		t.Fatal("accepted a changed applied migration")
	}
}
