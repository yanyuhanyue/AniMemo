//go:build integration

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/external"
	"animemo.local/server/internal/journal"
)

type providerTransport func(*http.Request) (*http.Response, error)

func (f providerTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestBangumiMetadataIsolationAndConflicts(t *testing.T) {
	pool := isolatedDatabase(t)
	picture := coverFixture(t, "image/png")
	var revision atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Error("public metadata carried an account credential")
		}
		if strings.HasPrefix(r.URL.Path, "/pic/") {
			w.Header().Set("Content-Type", "image/png")
			w.Write(picture)
			return
		}
		subject := map[string]any{"id": 101, "type": 2, "name": "原文", "name_cn": fmt.Sprintf("资料作品 %d", revision.Load()), "platform": "TV", "eps": 12, "total_episodes": 15, "date": "2026-01-01", "summary": "上游简介", "images": map[string]string{"large": "https://lain.bgm.tv/pic/cover/l/test.png"}}
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/v0/search/subjects" {
			json.NewEncoder(w).Encode(map[string]any{"data": []any{subject}, "total": 1, "limit": 12, "offset": 0})
			return
		}
		json.NewEncoder(w).Encode(subject)
	}))
	defer upstream.Close()
	target, _ := url.Parse(upstream.URL)
	provider := bangumi.New(providerTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.Host != "api.bgm.tv" && r.URL.Host != "lain.bgm.tv" {
			t.Fatalf("unexpected upstream host %s", r.URL.Host)
		}
		copy := r.Clone(r.Context())
		copy.URL.Scheme = target.Scheme
		copy.URL.Host = target.Host
		return http.DefaultTransport.RoundTrip(copy)
	}))
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base, Bangumi: provider})
	server.Start()
	defer server.Close()
	owner, other := browser(), browser()
	request(t, owner, base, "POST", "/api/v1/auth/register", map[string]any{"email": "source-owner@example.test", "password": "source-password-123", "display_name": "资料用户"}, 201)
	request(t, other, base, "POST", "/api/v1/auth/register", map[string]any{"email": "source-other@example.test", "password": "source-password-123", "display_name": "另一用户"}, 201)
	request(t, browser(), base, "GET", "/api/v1/providers/bangumi/subjects?query=test", nil, 401)
	search := decodeAs[external.SearchResult](t, request(t, owner, base, "GET", "/api/v1/providers/bangumi/subjects?query=%E4%B8%AD%E6%96%87", nil, 200))
	if len(search.Items) != 1 {
		t.Fatal("search response missing")
	}
	preview := decodeAs[external.SubjectPreview](t, request(t, owner, base, "GET", "/api/v1/providers/bangumi/subjects/101", nil, 200))
	input := external.ApplyInput{SubjectID: 101, Snapshot: preview.Snapshot, Fields: []string{"title", "original_title", "total_episodes", "description", "reference_url"}, Cover: true}
	entry := decodeAs[journal.Entry](t, request(t, owner, base, "POST", "/api/v1/entries/from-bangumi", input, 201))
	if entry.Source == nil || entry.Source.SubjectID != 101 || entry.CoverRevision == nil || entry.Visibility != "private" {
		t.Fatalf("incomplete source import: %+v", entry)
	}
	request(t, owner, base, "POST", "/api/v1/entries/from-bangumi", input, 409)
	request(t, other, base, "POST", "/api/v1/entries/"+entry.ID+"/source", input, 404)
	entry = decodeAs[journal.Entry](t, request(t, owner, base, "PATCH", "/api/v1/entries/"+entry.ID, map[string]any{"version": entry.Version, "notes": "自己的短评", "score": 8.5, "tags": []string{"手动标签"}}, 200))
	input.Version = entry.Version
	input.Cover = false
	input.Fields = []string{"description"}
	updated := decodeAs[journal.Entry](t, request(t, owner, base, "POST", "/api/v1/entries/"+entry.ID+"/source", input, 200))
	if updated.Notes != "自己的短评" || updated.Score == nil || *updated.Score != 8.5 || len(updated.Tags) != 1 {
		t.Fatal("refresh overwrote personal fields")
	}
	request(t, owner, base, "POST", "/api/v1/entries/"+entry.ID+"/source", input, 409)
	input.Version = updated.Version
	revision.Store(1)
	request(t, owner, base, "POST", "/api/v1/entries/"+entry.ID+"/source", input, 409)
	preview = decodeAs[external.SubjectPreview](t, request(t, owner, base, "GET", "/api/v1/providers/bangumi/subjects/101", nil, 200))
	input.Snapshot = preview.Snapshot
	input.Fields = []string{"notes"}
	request(t, owner, base, "POST", "/api/v1/entries/"+entry.ID+"/source", input, 400)
	input.Fields = []string{"title"}
	var wg sync.WaitGroup
	statuses := make(chan int, 2)
	for range 2 {
		wg.Go(func() {
			out, err := send(owner, base, "POST", "/api/v1/entries/"+entry.ID+"/source", input, base)
			if err != nil {
				t.Error(err)
				return
			}
			statuses <- out.status
		})
	}
	wg.Wait()
	close(statuses)
	counts := map[int]int{}
	for status := range statuses {
		counts[status]++
	}
	if counts[200] != 1 || counts[409] != 1 {
		t.Fatalf("concurrent refresh did not use CAS: %v", counts)
	}
	updated, err := journal.New(pool).Get(context.Background(), decodeAs[struct {
		ID string `json:"id"`
	}](t, request(t, owner, base, "GET", "/api/v1/auth/me", nil, 200)).ID, entry.ID)
	if err != nil {
		t.Fatal(err)
	}
	unbound := decodeAs[journal.Entry](t, request(t, owner, base, "DELETE", fmt.Sprintf("/api/v1/entries/%s/source?version=%d", entry.ID, updated.Version), nil, 200))
	if unbound.Source != nil || unbound.Notes != "自己的短评" {
		t.Fatal("unbind damaged journal")
	}
}
