//go:build integration

package api

import (
	"context"
	"testing"

	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
)

func TestRememberedWorkWithoutInventedViewing(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, owner := registerBrowser(t, base, "remembered@example.test")
	bob, _ := registerBrowser(t, base, "remembered-other@example.test")
	ctx := context.Background()
	s := journal.New(pool)
	e := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "学生时代的动画", "notes": "高中时看过，记不清哪一天。"}, 201))
	if e.Status != "recorded" || e.AiringState != "unknown" || e.WatchedEpisodes != 0 || e.TotalEpisodes != 0 || e.Score != nil || e.Visibility != "private" {
		t.Fatalf("minimal record invented details: %+v", e)
	}
	request(t, bob, base, "GET", "/api/v1/entries/"+e.ID, nil, 404)
	request(t, alice, base, "POST", "/api/v1/filters", journal.QuickFilter{Name: "随手记下的番", Status: "recorded", Sort: "updated"}, 201)
	page := decodeAs[journal.Page](t, request(t, alice, base, "GET", "/api/v1/entries?status=recorded", nil, 200))
	if page.Total != 1 || page.Items[0].ID != e.ID {
		t.Fatal("remembered work cannot be found")
	}
	note := decodeAs[journal.MemoryNote](t, request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: e.Title, Body: "放学后和朋友一起看的。", AnimeID: e.AnimeID, TimePrecision: "unknown"}, 201))
	stats := decodeAs[journal.Stats](t, request(t, alice, base, "GET", "/api/v1/stats", nil, 200))
	if stats.Recorded != 1 || stats.Completed != 0 || stats.WatchedEpisodes != 0 || stats.WatchRecords != 0 || note.OccurredOn != "" || note.EpisodeID != "" || note.WatchID != "" {
		t.Fatal("an incomplete recollection was treated as exact viewing")
	}
	fromSource, err := s.ApplySource(ctx, owner.ID, "", 0, journal.SourceMetadata{SubjectID: 42, Title: "补充作品资料", TotalEpisodes: 24}, []string{"total_episodes"}, nil)
	if err != nil || fromSource.Status != "recorded" || fromSource.WatchedEpisodes != 0 || fromSource.AiringState != "unknown" {
		t.Fatalf("metadata lookup inferred viewing: %+v %v", fromSource, err)
	}
	watch := decodeAs[journal.RecordResult](t, request(t, alice, base, "POST", "/api/v1/entries/"+e.ID+"/history", journal.RecordInput{RequestID: id.New(), TimePrecision: "unknown", EpisodeFrom: 1, EpisodeTo: 3, Rewatch: 1}, 201))
	if watch.Record.WatchedOn != "" || watch.Entry.WatchedEpisodes != 3 || watch.Entry.Status != "watching" {
		t.Fatal("explicit episode evidence was not preserved")
	}
	request(t, alice, base, "DELETE", "/api/v1/entries/"+e.ID+"/history/"+watch.Record.ID+"?version=1", nil, 200)
	current := decodeAs[journal.Entry](t, request(t, alice, base, "GET", "/api/v1/entries/"+e.ID, nil, 200))
	if current.Status != "recorded" || current.WatchedEpisodes != 0 || current.Notes != e.Notes {
		t.Fatal("withdrawing precise viewing changed the recollection into a future plan")
	}
	request(t, alice, base, "GET", "/api/v1/memory/notes/"+note.ID, nil, 200)
}
