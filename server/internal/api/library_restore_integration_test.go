//go:build integration

package api

import (
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
	"bytes"
	"context"
	"encoding/json"
	"testing"
)

func TestMemoryLibraryPortableRoundTrip(t *testing.T) {
	for _, format := range []string{"json", "zip"} {
		t.Run(format, func(t *testing.T) { memoryLibraryPortableRoundTrip(t, format) })
	}
}

func memoryLibraryPortableRoundTrip(t *testing.T, format string) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, au := registerBrowser(t, base, "library-export@example.test")
	bob, bu := registerBrowser(t, base, "library-restore@example.test")
	ctx := context.Background()
	s := journal.New(pool)
	e := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "记忆归途", "total_episodes": 24}, 201))
	c := decodeAs[journal.Character](t, request(t, alice, base, "POST", "/api/v1/memory/characters", journal.CharacterInput{Name: "星见", Aliases: []string{"小星"}, AnimeIDs: []string{e.AnimeID}, Favorite: true, Visibility: "unlisted"}, 201))
	c2 := decodeAs[journal.Character](t, request(t, alice, base, "POST", "/api/v1/memory/characters", journal.CharacterInput{Name: "星见旧名"}, 201))
	request(t, alice, base, "POST", "/api/v1/memory/identities/character/"+c2.ID, map[string]any{"version": 1, "target_id": c.ID}, 204)
	ep := decodeAs[journal.Episode](t, request(t, alice, base, "POST", "/api/v1/memory/episodes", journal.EpisodeInput{AnimeID: e.AnimeID, Title: "归途", Number: 1, Identities: []journal.EpisodeIdentity{{Provider: "local-source", ExternalID: "ep-one"}}}, 201))
	original := coverFixture(t, "image/png")
	media, err := s.ReserveMemoryMedia(ctx, au.ID, len(original))
	if err != nil {
		t.Fatal(err)
	}
	media, err = s.FinalizeMemoryMedia(ctx, au.ID, media.ID, "image/png", original)
	if err != nil {
		t.Fatal(err)
	}
	n := decodeAs[journal.MemoryNote](t, request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Kind: "moment", Title: "那一年", Body: "冻结前的文字", AnimeID: e.AnimeID, CharacterID: c2.ID, EpisodeID: ep.ID, MediaIDs: []string{media.ID}, TimePrecision: "month", OccurredOn: "2026-10", Visibility: "unlisted", Spoiler: true, Highlight: true, Tags: []string{"归途"}}, 201))
	request(t, alice, base, "POST", "/api/v1/memory/collections", journal.CollectionInput{Title: "归途小册", Items: []journal.CollectionItem{{Kind: "character", ID: c.ID}, {Kind: "moment", ID: n.ID}, {Kind: "anime", ID: e.AnimeID}}, Visibility: "unlisted"}, 201)
	request(t, alice, base, "POST", "/api/v1/memory/progress", journal.ProgressAssertionInput{AnimeID: e.AnimeID, Scope: "mainline", Precision: "caught_up", Episodes: []journal.VersionedID{{ID: ep.ID, Version: ep.Version}}, Note: "目录当时只有这一话"}, 201)
	y := decodeAs[journal.YearlyMemory](t, request(t, alice, base, "POST", "/api/v1/memory/yearly", journal.YearlyInput{Year: 2026, Timezone: "Asia/Shanghai", Title: "2026 的光", NoteIDs: []string{n.ID}, Visibility: "unlisted"}, 201))
	input := n.MemoryNoteInput
	input.Body = "当前的文字"
	request(t, alice, base, "PUT", "/api/v1/memory/notes/"+n.ID, input, 200)
	if _, err = s.ProcessAchievements(ctx); err != nil {
		t.Fatal(err)
	}
	center, err := s.Achievements(ctx, au.ID)
	if err != nil {
		t.Fatal(err)
	}
	var unlock string
	for _, a := range center.Items {
		if a.Granted {
			unlock = a.UnlockID
		}
	}
	if unlock == "" {
		t.Fatal("missing award")
	}
	if err = s.AchievementShowcase(ctx, au.ID, []string{unlock}); err != nil {
		t.Fatal(err)
	}
	before, err := s.Export(ctx, au.ID)
	if err != nil {
		t.Fatal(err)
	}
	if before.Schema != "animemo.journal/v3" || before.Library == nil {
		t.Fatal("library missing from export")
	}
	encoded, err := json.Marshal(before)
	if err != nil {
		t.Fatal(err)
	}
	if format == "zip" {
		encoded, err = s.Backup(ctx, au.ID)
		if err != nil {
			t.Fatal(err)
		}
	}
	job := importFile(t, bob, base, format, encoded, 202)
	if _, err = s.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job, err = s.Import(ctx, bu.ID, job.ID)
	if err != nil || job.State != "ready" {
		t.Fatalf("prepare %s %s %v", job.State, job.Error, err)
	}
	if _, err = s.ImportAction(ctx, bu.ID, job.ID, "apply"); err != nil {
		t.Fatal(err)
	}
	if _, err = s.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job, err = s.Import(ctx, bu.ID, job.ID)
	if err != nil || job.State != "done" {
		t.Fatalf("restore %s %s %v", job.State, job.Error, err)
	}
	after, err := s.Export(ctx, bu.ID)
	if err != nil {
		t.Fatal(err)
	}
	b := after.Library
	if after.Entries[0].Status != "recorded" || after.Entries[0].WatchedEpisodes != 0 || len(after.History) != 0 {
		t.Fatal("portable restore invented a completion state or episode history")
	}
	if b == nil || len(b.Notes) != 1 || len(b.Characters) != 2 || len(b.Yearlies) != 1 || len(b.Media) != 1 || len(b.Achievements.Unlocks) != 1 {
		t.Fatalf("missing restored content %+v", b)
	}
	got := b.Notes[0]
	if got.ID == n.ID || got.Body != "当前的文字" || got.TimePrecision != "month" || got.OccurredOn != "2026-10" || got.Visibility != "unlisted" || !got.Spoiler || !got.Highlight || !got.CreatedAt.Equal(n.CreatedAt) {
		t.Fatal("note semantics changed")
	}
	var alias, canonical journal.Character
	for _, v := range b.Characters {
		if v.Name == "星见旧名" {
			alias = v
		} else {
			canonical = v
		}
	}
	if got.CharacterID != alias.ID || alias.RedirectID != canonical.ID || canonical.AnimeIDs[0] != after.Entries[0].AnimeID || !canonical.Favorite {
		t.Fatal("identity redirects or ownership references not remapped")
	}
	if got.EpisodeID != b.Episodes[0].ID || b.Episodes[0].Identities[0].ExternalID != "ep-one" || b.Progress[0].EpisodeIDs[0] != got.EpisodeID {
		t.Fatal("episode scope or identity lost")
	}
	if !bytes.Equal(b.Media[0].Data, original) || got.MediaIDs[0] != b.Media[0].ID || b.Media[0].SHA256 != media.SHA256 {
		t.Fatal("media bytes or reference changed")
	}
	frozen := b.Yearlies[0].Revisions[0]
	if frozen.Items[0].Body != "冻结前的文字" || frozen.Items[0].NoteID != got.ID || frozen.Items[0].MediaIDs[0] != b.Media[0].ID || !frozen.Cutoff.Equal(y.Revisions[0].Cutoff) || frozen.Algorithm != y.Revisions[0].Algorithm {
		t.Fatal("yearly frozen revision changed")
	}
	if b.Collections[0].Items[0].ID != canonical.ID || b.Collections[0].Items[1].ID != got.ID || b.Collections[0].Visibility != "unlisted" {
		t.Fatal("collection ordering or visibility changed")
	}
	if b.Achievements.Unlocks[0].ShowcaseSlot != 1 || b.Achievements.Unlocks[0].Rule.Revision != 1 || !b.Achievements.Unlocks[0].UnlockedAt.Equal(before.Library.Achievements.Unlocks[0].UnlockedAt) {
		t.Fatal("achievement history changed")
	}
	for _, r := range b.Revisions {
		if r.Kind == "moment" && r.ResourceID != got.ID {
			t.Fatal("revision points to original account")
		}
	}
	// A tampered original must fail before any target memory is created.
	carol, cu := registerBrowser(t, base, "library-corrupt@example.test")
	before.Library.Media[0].Data[0] ^= 1
	bad, _ := json.Marshal(before)
	job = importFile(t, carol, base, "json", bad, 202)
	if _, err = s.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job, _ = s.Import(ctx, cu.ID, job.ID)
	if job.State != "failed" {
		t.Fatal("tampered media accepted")
	}
	var count int
	pool.QueryRow(ctx, `SELECT count(*) FROM anime_resources WHERE owner_id=$1`, cu.ID).Scan(&count)
	if count != 0 {
		t.Fatal("invalid import partially wrote core")
	}
}
func TestSelectedImportAppendProvenance(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, au := registerBrowser(t, base, "selected@example.test")
	ctx := context.Background()
	s := journal.New(pool)
	e := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "已在手账的作品", "total_episodes": 12, "notes": "原笔记不能覆盖", "score": 8.5}, 201))
	sourceID := id.New()
	exported := journal.Export{Schema: "animemo.journal/v1", Entries: []journal.Entry{{ID: sourceID, Title: e.Title, TotalEpisodes: 12, WatchedEpisodes: 3, Format: "tv", Status: "watching", Accent: "violet", AiringState: "unknown"}}, History: []journal.Record{{ID: id.New(), EntryID: sourceID, WatchedOn: "2026-10-01", EpisodeFrom: 1, EpisodeTo: 1, Rewatch: 1, TimePrecision: "day", SourceLine: 3, SourceFilename: "2026观看.txt", Note: "保留的记忆"}, {ID: id.New(), EntryID: sourceID, WatchedOn: "2026-10-02", EpisodeFrom: 2, EpisodeTo: 3, Rewatch: 1, TimePrecision: "day", SourceLine: 5, SourceFilename: "2026观看.txt", Note: "取消这条"}}}
	data, _ := json.Marshal(exported)
	run := func(version int) {
		job := importFile(t, alice, base, "json", data, 202)
		if _, err := s.ProcessNextImport(ctx); err != nil {
			t.Fatal(err)
		}
		job, err := s.Import(ctx, au.ID, job.ID)
		if err != nil || job.State != "ready" || len(job.Preview.Choices) != 1 || job.Preview.Choices[0].History[0].SourceLine != 3 {
			t.Fatalf("preview %+v %v", job, err)
		}
		request(t, alice, base, "POST", "/api/v1/imports/"+job.ID, map[string]any{"action": "apply", "selection": []journal.ImportSelection{{Index: 0, Records: []int{0}, TargetID: e.ID, TargetVersion: version}}}, 200)
		if _, err = s.ProcessNextImport(ctx); err != nil {
			t.Fatal(err)
		}
		job, _ = s.Import(ctx, au.ID, job.ID)
		if job.State != "done" {
			t.Fatal(job.Error)
		}
	}
	run(e.Version)
	got, err := s.Get(ctx, au.ID, e.ID)
	if err != nil {
		t.Fatal(err)
	}
	history, err := s.HistoryPage(ctx, au.ID, e.ID, "", "", 1)
	if err != nil {
		t.Fatal(err)
	}
	if history.Total != 1 || history.Items[0].SourceLine != 3 || history.Items[0].SourceFilename != "2026观看.txt" || got.Notes != e.Notes || *got.Score != *e.Score || got.WatchedEpisodes != 1 {
		t.Fatal("selection, provenance or existing fields changed")
	}
	run(got.Version)
	history, err = s.HistoryPage(ctx, au.ID, e.ID, "", "", 1)
	if err != nil || history.Total != 1 {
		t.Fatal("repeat import duplicated memory", err)
	}
}
