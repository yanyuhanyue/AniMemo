//go:build integration

package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"

	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/external"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
)

type fakeBangumi struct {
	t             *testing.T
	mu            sync.Mutex
	collections   map[int64]bangumi.Collection
	episodes      map[int64][]bangumi.EpisodeCollection
	writes        int
	episodeWrites int
	lostWrite     string
	offsets       []int
}

func newFakeBangumi(t *testing.T, count int) (*fakeBangumi, *bangumi.Client) {
	fake := &fakeBangumi{t: t, collections: map[int64]bangumi.Collection{}, episodes: map[int64][]bangumi.EpisodeCollection{}}
	for i := range count {
		subject := int64(101 + i)
		fake.collections[subject] = bangumi.Collection{SubjectID: subject, SubjectType: 2, Type: 3, Rate: 8, Comment: "remote note", Tags: []string{"原始"}, Episodes: 1, Private: true, Subject: bangumi.Subject{ID: subject, Type: 2, Name: fmt.Sprintf("Source %d", subject), NameCN: fmt.Sprintf("作品 %d", subject), Episodes: 3, TotalEpisodes: 4, Platform: "TV"}}
		for number := 1; number <= 3; number++ {
			state := 0
			if number == 1 {
				state = 2
			}
			fake.episodes[subject] = append(fake.episodes[subject], bangumi.EpisodeCollection{Episode: bangumi.Episode{ID: subject*10 + int64(number), Sort: float64(number), Type: 0}, Type: state})
		}
	}
	upstream := httptest.NewServer(http.HandlerFunc(fake.serve))
	t.Cleanup(upstream.Close)
	target, _ := url.Parse(upstream.URL)
	provider := bangumi.New(providerTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.Host != "api.bgm.tv" && r.URL.Host != "bgm.tv" {
			t.Errorf("unexpected external origin %s", r.URL.Host)
		}
		copy := r.Clone(r.Context())
		copy.URL.Host = target.Host
		copy.URL.Scheme = target.Scheme
		response, err := http.DefaultTransport.RoundTrip(copy)
		fake.mu.Lock()
		lose := err == nil && fake.lostWrite != "" && r.Method == fake.lostWrite && strings.Contains(r.URL.Path, "/collections/")
		if lose {
			fake.lostWrite = ""
		}
		fake.mu.Unlock()
		if lose {
			response.Body.Close()
			return nil, errors.New("synthetic response lost after upstream commit")
		}
		return response, err
	}))
	return fake, provider
}
func (f *fakeBangumi) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	if r.URL.Path == "/oauth/access_token" {
		if err := r.ParseForm(); err != nil {
			f.t.Error(err)
		}
		if r.Form.Get("client_id") != "synthetic-client" || r.Form.Get("client_secret") != "synthetic-secret" {
			f.t.Error("OAuth client configuration missing")
		}
		json.NewEncoder(w).Encode(bangumi.Token{AccessToken: "synthetic-access", RefreshToken: "synthetic-refresh", TokenType: "Bearer", ExpiresIn: 3600, UserID: 77})
		return
	}
	if r.URL.Path == "/v0/me" {
		json.NewEncoder(w).Encode(bangumi.User{ID: 77, Username: "isolated-fixture", Nickname: "隔离账号"})
		return
	}
	if r.Header.Get("Authorization") != "Bearer synthetic-access" {
		w.WriteHeader(401)
		return
	}
	if r.URL.Path == "/v0/users/77/collections" {
		offset, _ := strconv.Atoi(r.URL.Query().Get("offset"))
		f.offsets = append(f.offsets, offset)
		ids := []int{}
		for subject := range f.collections {
			ids = append(ids, int(subject))
		}
		sort.Ints(ids)
		data := []bangumi.Collection{}
		for _, subject := range ids[min(offset, len(ids)):min(offset+50, len(ids))] {
			data = append(data, f.collections[int64(subject)])
		}
		json.NewEncoder(w).Encode(bangumi.CollectionPage{Data: data, Total: len(ids), Offset: offset, Limit: 50})
		return
	}
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) >= 5 && parts[1] == "users" && parts[3] == "collections" {
		subject, _ := strconv.ParseInt(parts[4], 10, 64)
		collection, exists := f.collections[subject]
		if len(parts) == 6 && parts[5] == "episodes" {
			if r.Method == "GET" {
				json.NewEncoder(w).Encode(map[string]any{"total": len(f.episodes[subject]), "data": f.episodes[subject]})
				return
			}
			var input struct {
				IDs   []int64 `json:"episode_id"`
				State int     `json:"type"`
			}
			if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
				f.t.Error(err)
			}
			for i, episode := range f.episodes[subject] {
				for _, episodeID := range input.IDs {
					if episode.Episode.ID == episodeID {
						f.episodes[subject][i].Type = input.State
					}
				}
			}
			collection.Episodes = 0
			for _, episode := range f.episodes[subject] {
				if episode.Type == 2 {
					collection.Episodes++
				}
			}
			f.collections[subject] = collection
			f.episodeWrites++
			w.WriteHeader(204)
			return
		}
		if r.Method == "GET" {
			if !exists {
				w.WriteHeader(404)
				return
			}
			json.NewEncoder(w).Encode(collection)
			return
		}
		var input bangumi.CollectionWrite
		if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
			f.t.Error(err)
		}
		collection.SubjectID, collection.SubjectType = subject, 2
		collection.Type, collection.Rate, collection.Comment, collection.Tags, collection.Private = input.Type, input.Rate, input.Comment, input.Tags, input.Private
		f.collections[subject] = collection
		f.writes++
		w.WriteHeader(204)
		return
	}
	w.WriteHeader(404)
}

func oauthState(t *testing.T, client *http.Client, base string) string {
	t.Helper()
	reply := decodeAs[struct {
		URL string `json:"url"`
	}](t, request(t, client, base, "POST", "/api/v1/connections/bangumi/authorize", nil, 200))
	parsed, err := url.Parse(reply.URL)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Host != "bgm.tv" {
		t.Fatal("authorization destination is not fixed")
	}
	return parsed.Query().Get("state")
}
func oauthCallback(t *testing.T, client *http.Client, base, state string) response {
	t.Helper()
	direct := *client
	direct.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return request(t, &direct, base, "GET", "/api/v1/connections/bangumi/callback?code=synthetic-code&state="+url.QueryEscape(state), nil, 303)
}

func TestOAuthAndDurableCollectionSync(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	fake, provider := newFakeBangumi(t, 2)
	key := bytes.Repeat([]byte{27}, 32)
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	oauth := bangumi.OAuthConfig{ClientID: "synthetic-client", ClientSecret: "synthetic-secret"}
	newWorker := func() *external.Service {
		worker := external.New(pool, provider)
		if err := worker.ConfigureOAuth(oauth, key, base); err != nil {
			t.Fatal(err)
		}
		return worker
	}
	worker := newWorker()
	server.Config.Handler = New(pool, Config{PublicOrigin: base, External: worker, SecretKey: key})
	server.Start()
	defer server.Close()
	owner, user := registerBrowser(t, base, "oauth-owner@example.test")
	other, _ := registerBrowser(t, base, "oauth-other@example.test")
	state := oauthState(t, owner, base)
	if result := oauthCallback(t, other, base, state); !strings.Contains(result.header.Get("Location"), "failed") {
		t.Fatal("OAuth state crossed local accounts")
	}
	if result := oauthCallback(t, owner, base, state); !strings.Contains(result.header.Get("Location"), "connected") {
		t.Fatal("OAuth callback did not connect")
	}
	if result := oauthCallback(t, owner, base, state); !strings.Contains(result.header.Get("Location"), "failed") {
		t.Fatal("OAuth state replay accepted")
	}
	connection := request(t, owner, base, "GET", "/api/v1/connections/bangumi", nil, 200)
	if bytes.Contains(connection.body, []byte("synthetic-access")) || bytes.Contains(connection.body, []byte("refresh")) {
		t.Fatal("upstream credentials exposed")
	}
	var encrypted []byte
	if err := pool.QueryRow(ctx, `SELECT tokens FROM external_connections WHERE user_id=$1`, user.ID).Scan(&encrypted); err != nil {
		t.Fatal(err)
	}
	if json.Valid(encrypted) || bytes.Contains(encrypted, []byte("synthetic-access")) {
		t.Fatal("OAuth tokens stored without encryption")
	}
	if _, err := pool.Exec(ctx, `UPDATE external_connections SET expires_at=now() WHERE user_id=$1`, user.ID); err != nil {
		t.Fatal(err)
	}
	request(t, owner, base, "POST", "/api/v1/connections/bangumi/verify", nil, 200)
	newJob := func(mode string, progress bool) external.SyncJob {
		return decodeAs[external.SyncJob](t, request(t, owner, base, "POST", "/api/v1/connections/bangumi/sync", external.SyncRequest{Mode: mode, IncludeProgress: progress, RequestID: id.New()}, 202))
	}
	advance := func(jobID, want string) external.SyncJob {
		t.Helper()
		for range 15 {
			worked, err := worker.ProcessNextSync(ctx)
			if err != nil {
				t.Fatal(err)
			}
			job, err := worker.SyncJob(ctx, user.ID, jobID)
			if err != nil {
				t.Fatal(err)
			}
			if job.State == want {
				return job
			}
			if !worked || job.State == "failed" || job.State == "cancelled" {
				t.Fatalf("sync stopped in %s: %s", job.State, job.Error)
			}
		}
		t.Fatal("sync did not reach expected state")
		return external.SyncJob{}
	}
	confirm := func(job external.SyncJob, action string) external.SyncJob {
		choices := []external.SyncChoice{}
		for _, item := range job.Items {
			chosen := action
			if item.SubjectID != 101 && action == "push" {
				chosen = "skip"
			}
			choices = append(choices, external.SyncChoice{ID: item.ID, Action: chosen})
		}
		return decodeAs[external.SyncJob](t, request(t, owner, base, "POST", "/api/v1/connections/bangumi/sync/"+job.ID, external.SyncAction{Action: "apply", Choices: choices}, 200))
	}
	job := newJob("pull", false)
	request(t, other, base, "GET", "/api/v1/connections/bangumi/sync/"+job.ID, nil, 404)
	worker = newWorker() // Recover queued work in a new service instance.
	job = advance(job.ID, "ready")
	if len(job.Items) != 2 {
		t.Fatal("preview lost provider items")
	}
	confirm(job, "pull")
	job = advance(job.ID, "done")
	entries, err := journal.New(pool).SourceRecords(ctx, user.ID)
	if err != nil || len(entries) != 2 {
		t.Fatalf("stable source import failed: %v", err)
	}
	var local journal.SourceRecord
	for _, entry := range entries {
		if entry.SubjectID == 101 {
			local = entry
		}
	}
	var history int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM watch_records`).Scan(&history); err != nil || history != 0 {
		t.Fatal("provider progress fabricated watch dates")
	}
	// A crash between the local commit and acknowledgment must not reapply or duplicate it.
	if _, err = pool.Exec(ctx, `UPDATE external_sync_jobs SET state='applying' WHERE id=$1`, job.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `UPDATE external_sync_items SET state='running' WHERE job_id=$1 AND subject_id=101`, job.ID); err != nil {
		t.Fatal(err)
	}
	worker = newWorker()
	job = advance(job.ID, "done")
	var count int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM entries WHERE user_id=$1`, user.ID).Scan(&count); err != nil || count != 2 {
		t.Fatal("replay created duplicate entries")
	}
	// Decimal scoring is preserved locally; the explicit push preview shows the integer result.
	note := "local edit"
	score := 8.5
	updated, err := journal.New(pool).Update(ctx, user.ID, local.EntryID, journal.Patch{Version: local.Version, Notes: &note, Score: &score})
	if err != nil {
		t.Fatal(err)
	}
	job = newJob("push", false)
	job = advance(job.ID, "ready")
	for _, item := range job.Items {
		if item.SubjectID == 101 && (item.Decision != "push" || item.PushTarget.Value.Score != 9) {
			t.Fatal("push preview hid score conversion")
		}
	}
	confirm(job, "push")
	fake.mu.Lock()
	fake.lostWrite = "POST"
	writesBefore := fake.writes
	fake.mu.Unlock()
	if worked, err := worker.ProcessNextSync(ctx); !worked || err != nil {
		t.Fatal(err)
	}
	var started bool
	if err = pool.QueryRow(ctx, `SELECT write_started FROM external_sync_items WHERE job_id=$1 AND subject_id=101`, job.ID).Scan(&started); err != nil || !started {
		t.Fatal("uncertain write was not recorded durably")
	}
	if _, err = pool.Exec(ctx, `UPDATE external_sync_items SET available_at=now() WHERE job_id=$1`, job.ID); err != nil {
		t.Fatal(err)
	}
	worker = newWorker()
	job = advance(job.ID, "done")
	fake.mu.Lock()
	actualWrites := fake.writes - writesBefore
	remoteScore := fake.collections[101].Rate
	fake.mu.Unlock()
	if actualWrites != 1 || remoteScore != 9 {
		t.Fatal("uncertain POST was blindly repeated or did not apply")
	}
	entry, err := journal.New(pool).Get(ctx, user.ID, local.EntryID)
	if err != nil || *entry.Score != 8.5 {
		t.Fatal("push rounded the local rating")
	}
	// Two-sided edits must be a conflict; an additional edit after preview must stop all writes.
	note = "second local edit"
	updated, err = journal.New(pool).Update(ctx, user.ID, entry.ID, journal.Patch{Version: entry.Version, Notes: &note})
	if err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	remote := fake.collections[101]
	remote.Comment = "second remote edit"
	fake.collections[101] = remote
	fake.mu.Unlock()
	job = newJob("two_way", false)
	job = advance(job.ID, "ready")
	for _, item := range job.Items {
		if item.SubjectID == 101 && item.Decision != "conflict" {
			t.Fatal("two-sided edit was not surfaced")
		}
	}
	confirm(job, "push")
	note = "edit after preview"
	updated, err = journal.New(pool).Update(ctx, user.ID, updated.ID, journal.Patch{Version: updated.Version, Notes: &note})
	if err != nil {
		t.Fatal(err)
	}
	job = advance(job.ID, "done")
	for _, item := range job.Items {
		if item.SubjectID == 101 && item.State != "conflict" {
			t.Fatal("local CAS failure did not stop writeback")
		}
	}
	// Progress uses actual episode IDs and resumes after a lost response mid-sequence.
	_, err = journal.New(pool).RecordWatch(ctx, user.ID, updated.ID, journal.RecordInput{WatchedOn: "2026-10-07", EpisodeFrom: 1, EpisodeTo: 2, RequestID: id.New()})
	if err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	for i := range fake.episodes[101] {
		fake.episodes[101][i].Type = 0
	}
	fake.episodes[101][2].Type = 2
	remote = fake.collections[101]
	remote.Episodes = 1
	fake.collections[101] = remote
	fake.mu.Unlock()
	job = newJob("push", true)
	job = advance(job.ID, "ready")
	confirm(job, "push")
	fake.mu.Lock()
	fake.lostWrite = "PATCH"
	episodeBefore := fake.episodeWrites
	fake.mu.Unlock()
	if worked, err := worker.ProcessNextSync(ctx); !worked || err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `UPDATE external_sync_items SET available_at=now() WHERE job_id=$1`, job.ID); err != nil {
		t.Fatal(err)
	}
	worker = newWorker()
	job = advance(job.ID, "done")
	for _, item := range job.Items {
		if item.SubjectID == 101 && item.State != "done" {
			t.Fatalf("progress sync did not finish: %s", item.Result)
		}
	}
	fake.mu.Lock()
	episodes := append([]bangumi.EpisodeCollection{}, fake.episodes[101]...)
	episodeWrites := fake.episodeWrites - episodeBefore
	fake.mu.Unlock()
	if episodes[0].Type != 2 || episodes[1].Type != 2 || episodes[2].Type != 0 || episodeWrites != 2 {
		t.Fatal("episode reconciliation repeated completed patches or left wrong state")
	}
	job = newJob("pull", false)
	request(t, owner, base, "DELETE", "/api/v1/connections/bangumi", nil, 200)
	worker.ProcessNextSync(ctx)
	cancelled, err := worker.SyncJob(ctx, user.ID, job.ID)
	if err != nil || cancelled.State != "cancelled" {
		t.Fatal("disconnect did not cancel pending work")
	}
	if err = pool.QueryRow(ctx, `SELECT tokens FROM external_connections WHERE user_id=$1`, user.ID).Scan(&encrypted); err != nil || encrypted != nil {
		t.Fatal("disconnect retained reusable credentials")
	}
}

func TestSyncCursorSurvivesRestart(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	fake, provider := newFakeBangumi(t, 51)
	key := bytes.Repeat([]byte{28}, 32)
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	oauth := bangumi.OAuthConfig{ClientID: "synthetic-client", ClientSecret: "synthetic-secret"}
	worker := external.New(pool, provider)
	if err := worker.ConfigureOAuth(oauth, key, base); err != nil {
		t.Fatal(err)
	}
	server.Config.Handler = New(pool, Config{PublicOrigin: base, External: worker, SecretKey: key})
	server.Start()
	defer server.Close()
	owner, user := registerBrowser(t, base, "pages@example.test")
	oauthCallback(t, owner, base, oauthState(t, owner, base))
	job := decodeAs[external.SyncJob](t, request(t, owner, base, "POST", "/api/v1/connections/bangumi/sync", external.SyncRequest{Mode: "pull", RequestID: id.New()}, 202))
	if _, err := worker.ProcessNextSync(ctx); err != nil {
		t.Fatal(err)
	}
	job, err := worker.SyncJob(ctx, user.ID, job.ID)
	if err != nil || job.Cursor != 50 || job.State != "fetching" {
		t.Fatal("page cursor was not persisted")
	}
	worker = external.New(pool, provider)
	worker.ConfigureOAuth(oauth, key, base)
	if _, err = worker.ProcessNextSync(ctx); err != nil {
		t.Fatal(err)
	}
	job, err = worker.SyncJob(ctx, user.ID, job.ID)
	if err != nil || len(job.Items) != 51 || job.State != "ready" {
		t.Fatal("restarted preview lost pages")
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.offsets) != 2 || fake.offsets[0] != 0 || fake.offsets[1] != 50 {
		t.Fatalf("unexpected pagination requests: %v", fake.offsets)
	}
}
