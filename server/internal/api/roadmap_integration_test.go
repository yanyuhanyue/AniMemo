//go:build integration

package api

import (
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/jobs"
	"animemo.local/server/internal/journal"
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"testing"
)

func TestRoadmapMemoryAndDurableEvents(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base})
	server.Start()
	defer server.Close()
	alice, bob := browser(), browser()
	request(t, alice, base, "POST", "/api/v1/auth/register", map[string]any{"email": "memory@example.test", "display_name": "星见", "password": "memory-passphrase-2026"}, 201)
	request(t, bob, base, "POST", "/api/v1/auth/register", map[string]any{"email": "other@example.test", "display_name": "路人", "password": "memory-passphrase-2026"}, 201)
	entry := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "连载中的故事", "airing_state": "airing", "total_episodes": 12, "score": 8}, 201))
	if !id.Valid(entry.AnimeID) || entry.AnimeID == entry.ID {
		t.Fatal("resource must have independent identity")
	}
	route := "/api/v1/entries/" + entry.ID
	result := decodeAs[journal.RecordResult](t, request(t, alice, base, "POST", route+"/history", map[string]any{"watched_on": "2026-10", "time_precision": "month", "episode_from": 1, "episode_to": 12, "request_id": id.New()}, 201))
	if result.Entry.Status != "caught_up" || result.Record.TimePrecision != "month" || result.Record.WatchedOn != "2026-10-01" {
		t.Fatalf("lost time or ongoing-series semantics: %+v", result)
	}
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "PATCH", route, map[string]any{"version": result.Entry.Version, "score": 9}, 200))
	request(t, bob, base, "GET", route+"/revisions", nil, 404)
	revisions := decodeAs[struct {
		Items []journal.MemoryRevision `json:"items"`
	}](t, request(t, alice, base, "GET", route+"/revisions", nil, 200))
	if len(revisions.Items) < 4 {
		t.Fatal("meaningful revisions missing")
	}
	request(t, alice, base, "POST", route+"/history", map[string]any{"watched_on": "", "time_precision": "unknown", "episode_from": 1, "episode_to": 1, "rewatch": 2, "request_id": id.New()}, 201)
	request(t, alice, base, "POST", route+"/history", map[string]any{"watched_on": "2026-02-31", "time_precision": "day", "episode_from": 1, "episode_to": 1, "request_id": id.New()}, 400)
	// A rollback must not publish a revision or outbox event.
	var before, after int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM core_outbox`).Scan(&before); err != nil {
		t.Fatal(err)
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, `UPDATE entries SET notes='rolled back' WHERE id=$1`, entry.ID); err != nil {
		t.Fatal(err)
	}
	tx.Rollback(ctx)
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM core_outbox`).Scan(&after); err != nil || before != after {
		t.Fatal("rolled-back event escaped")
	}
	executor := jobs.New(pool)
	stale, err := executor.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `UPDATE core_outbox SET lease_until=now()-interval '1 minute' WHERE id=$1`, stale.ID); err != nil {
		t.Fatal(err)
	}
	reclaimed, err := executor.Claim(ctx)
	if err != nil || reclaimed.ID != stale.ID {
		t.Fatalf("reclaim: %+v %v", reclaimed, err)
	}
	if err = executor.Complete(ctx, stale); err == nil {
		t.Fatal("stale executor committed")
	}
	if err = executor.Complete(ctx, reclaimed); err != nil {
		t.Fatal(err)
	}
	for {
		worked, err := executor.ProcessNext(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if !worked {
			break
		}
	}
	var delivered int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM memory_activity`).Scan(&delivered); err != nil || delivered != after {
		t.Fatalf("delivery lost or duplicated: %d / %d: %v", delivered, after, err)
	}
	// External identity removal does not remove the resource or its memories.
	if _, err = pool.Exec(ctx, `INSERT INTO entry_sources(entry_id,user_id,provider,subject_id,metadata) SELECT id,user_id,'bangumi',123,'{}'::jsonb FROM entries WHERE id=$1`, entry.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `DELETE FROM entry_sources WHERE entry_id=$1`, entry.ID); err != nil {
		t.Fatal(err)
	}
	var retained bool
	if err = pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM anime_external_identities WHERE anime_id=$1 AND NOT active)`, entry.AnimeID).Scan(&retained); err != nil || !retained {
		t.Fatal("provider unbinding lost identity history")
	}
	current := decodeAs[journal.Entry](t, request(t, alice, base, "GET", route, nil, 200))
	if current.AnimeID != entry.AnimeID {
		t.Fatal("identity changed")
	}
	analytics := decodeAs[journal.Analytics](t, request(t, alice, base, "GET", "/api/v1/analytics", nil, 200))
	if analytics.ActiveDays != 0 || analytics.Records != 2 || len(analytics.Months) != 1 {
		t.Fatalf("imprecise dates must not invent active days: %+v", analytics)
	}
	retired := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "已移出清单的回忆", "notes": "仍属于我"}, 201))
	request(t, alice, base, "DELETE", fmt.Sprintf("/api/v1/entries/%s?version=%d", retired.ID, retired.Version), nil, 204)
	original := decodeAs[journal.Export](t, request(t, alice, base, "GET", "/api/v1/export", nil, 200))
	if original.Schema != "animemo.journal/v2" || len(original.Resources) != 2 || len(original.Revisions) < 7 {
		t.Fatal("export lost retired resources or revisions")
	}
	data, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	job := importFile(t, bob, base, "json", data, 202)
	service := journal.New(pool)
	if _, err = service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, bob, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.State != "ready" {
		t.Fatalf("memory preview: %+v", job)
	}
	request(t, bob, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "apply"}, 200)
	if _, err = service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, bob, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.State != "done" {
		t.Fatalf("memory restore: %+v", job)
	}
	restored := decodeAs[journal.Export](t, request(t, bob, base, "GET", "/api/v1/export", nil, 200))
	if len(restored.Resources) != len(original.Resources) || len(restored.Revisions) != len(original.Revisions) || len(restored.History) != 2 || len(restored.Entries) != 1 {
		t.Fatal("round trip changed memory counts")
	}
	if restored.Entries[0].AnimeID == entry.AnimeID || restored.Entries[0].ID == entry.ID {
		t.Fatal("restored owner reused source identifiers")
	}
	counts := map[string]int{}
	originalIDs := map[string]bool{}
	for _, revision := range original.Revisions {
		counts[revision.Kind+revision.RecordedAt.String()]++
		originalIDs[revision.AnimeID] = true
		originalIDs[revision.EntryID] = true
	}
	for _, revision := range restored.Revisions {
		counts[revision.Kind+revision.RecordedAt.String()]--
		if originalIDs[revision.AnimeID] || originalIDs[revision.EntryID] {
			t.Fatal("historical owner references not remapped")
		}
	}
	for _, count := range counts {
		if count != 0 {
			t.Fatal("round trip rewrote memory time or kind")
		}
	}
	if restored.History[0].TimePrecision != original.History[0].TimePrecision || restored.History[1].TimePrecision != original.History[1].TimePrecision {
		t.Fatal("round trip invented date precision")
	}
}
