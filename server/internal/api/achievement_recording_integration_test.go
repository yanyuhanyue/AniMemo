//go:build integration

package api

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"

	"animemo.local/server/internal/database"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
)

func drainAchievementQueue(t *testing.T, s *journal.Service) {
	t.Helper()
	for i := 0; i < 100; i++ {
		worked, err := s.ProcessAchievements(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if !worked {
			return
		}
	}
	t.Fatal("achievement queue did not drain")
}

func TestRecordingAchievementFacts(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	client, owner := registerBrowser(t, base, "recording-awards@example.test")
	other, stranger := registerBrowser(t, base, "other-awards@example.test")
	ctx := context.Background()
	s := journal.New(pool)
	check := func(suffix string, value int, granted bool) journal.Achievement {
		t.Helper()
		center, err := s.Achievements(ctx, owner.ID)
		if err != nil {
			t.Fatal(err)
		}
		for _, a := range center.Items {
			if a.ID == "a1000000-0000-4000-8000-000000000"+suffix {
				if a.Value != value || a.Granted != granted {
					t.Fatalf("%s: value=%d granted=%v, want %d/%v", a.Title, a.Value, a.Granted, value, granted)
				}
				return a
			}
		}
		t.Fatalf("missing rule %s", suffix)
		return journal.Achievement{}
	}
	rules, err := s.AchievementRules(ctx)
	if err != nil || len(rules) != 12 {
		t.Fatalf("default catalog: %d rules, %v", len(rules), err)
	}
	e := decodeAs[journal.Entry](t, request(t, client, base, "POST", "/api/v1/entries", map[string]any{"title": "只记得这个名字"}, 201))
	request(t, client, base, "POST", "/api/v1/entries", map[string]any{"title": "已有想看分类", "status": "planned"}, 201)
	request(t, other, base, "POST", "/api/v1/entries", map[string]any{"title": "别人的作品"}, 201)
	// Reusing a core identity cannot inflate the distinct-recorded-work metric.
	duplicate, err := s.Create(ctx, owner.ID, journal.Create{Title: "同一作品的另一条目", AnimeID: e.AnimeID})
	if err != nil {
		t.Fatal(err)
	}
	drainAchievementQueue(t, s)
	first := check("006", 1, true)
	check("007", 1, false)
	check("001", 0, false)
	check("003", 0, false)
	if e.Status != "recorded" || e.WatchedEpisodes != 0 {
		t.Fatal("title-only entry invented viewing facts")
	}
	n := decodeAs[journal.MemoryNote](t, request(t, client, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: "记不清哪一年", Body: "但还记得这个故事"}, 201))
	for i := 1; i < 5; i++ {
		request(t, client, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: fmt.Sprintf("无日期回忆 %d", i)}, 201)
	}
	drainAchievementQueue(t, s)
	check("008", 5, true)
	check("009", 5, false)
	if n.TimePrecision != "unknown" || n.OccurredOn != "" {
		t.Fatal("award required an invented date")
	}
	collection := decodeAs[journal.MemoryCollection](t, request(t, client, base, "POST", "/api/v1/memory/collections", journal.CollectionInput{Title: "还没整理的收藏"}, 201))
	yearly := decodeAs[journal.YearlyMemory](t, request(t, client, base, "POST", "/api/v1/memory/yearly", journal.YearlyInput{Title: "还没选材的年度册", Year: 2026}, 201))
	drainAchievementQueue(t, s)
	check("010", 0, false)
	check("012", 0, false)
	collection.Items = []journal.CollectionItem{{Kind: "note", ID: n.ID}}
	collection = decodeAs[journal.MemoryCollection](t, request(t, client, base, "PUT", "/api/v1/memory/collections/"+collection.ID, collection.CollectionInput, 200))
	drainAchievementQueue(t, s)
	check("010", 1, true)
	// No other pending mutation can hide a missing yearly projection trigger.
	albumInput := journal.YearlyInput{Version: yearly.Version, Title: yearly.Title, Year: yearly.Year, Timezone: yearly.Timezone, NoteIDs: []string{n.ID}}
	yearly = decodeAs[journal.YearlyMemory](t, request(t, client, base, "PUT", "/api/v1/memory/yearly/"+yearly.ID, albumInput, 200))
	drainAchievementQueue(t, s)
	albumAward := check("012", 1, true)
	for i := 0; i < 2; i++ {
		albumInput.Version = yearly.Version
		yearly = decodeAs[journal.YearlyMemory](t, request(t, client, base, "PUT", "/api/v1/memory/yearly/"+yearly.ID, albumInput, 200))
		collection = decodeAs[journal.MemoryCollection](t, request(t, client, base, "PUT", "/api/v1/memory/collections/"+collection.ID, collection.CollectionInput, 200))
	}
	// Multiple workers must still produce just one unlock and one automatic event.
	var workers sync.WaitGroup
	errors := make(chan error, 4)
	for i := 0; i < 4; i++ {
		workers.Add(1)
		go func() { defer workers.Done(); _, e := s.ProcessAchievements(ctx); errors <- e }()
	}
	workers.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	drainAchievementQueue(t, s)
	if a := check("012", 1, true); a.UnlockID != albumAward.UnlockID || !a.UnlockedAt.Equal(*albumAward.UnlockedAt) {
		t.Fatal("re-saving album changed unlock history")
	}
	check("010", 1, true)
	var events int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM achievement_grant_events WHERE unlock_id=$1`, albumAward.UnlockID).Scan(&events); err != nil || events != 1 {
		t.Fatal("duplicate automatic event", err)
	}
	albumInput.Version = yearly.Version
	albumInput.NoteIDs = nil
	request(t, client, base, "PUT", "/api/v1/memory/yearly/"+yearly.ID, albumInput, 200)
	request(t, client, base, "DELETE", fmt.Sprintf("/api/v1/memory/collections/%s?version=%d", collection.ID, collection.Version), nil, 204)
	request(t, client, base, "DELETE", fmt.Sprintf("/api/v1/entries/%s?version=%d", duplicate.ID, duplicate.Version), nil, 204)
	if _, err = pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, owner.ID); err != nil {
		t.Fatal(err)
	}
	if err = s.ResourceAction(ctx, owner.ID, "entry", e.ID, "trash", "合成记录回收验证", e.Version); err != nil {
		t.Fatal(err)
	}
	drainAchievementQueue(t, s)
	check("006", 0, true)
	check("010", 0, true)
	check("012", 0, true)
	if err = s.ResourceAction(ctx, owner.ID, "entry", e.ID, "restore", "合成记录恢复验证", e.Version+1); err != nil {
		t.Fatal(err)
	}
	drainAchievementQueue(t, s)
	if check("006", 1, true).UnlockID != first.UnlockID {
		t.Fatal("restore duplicated award")
	}
	// The higher thresholds count independent memories and collections, not saves.
	for i := 5; i < 20; i++ {
		request(t, client, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: fmt.Sprintf("回忆 %d", i)}, 201)
	}
	for i := 0; i < 3; i++ {
		request(t, client, base, "POST", "/api/v1/memory/collections", journal.CollectionInput{Title: fmt.Sprintf("收藏 %d", i), Items: []journal.CollectionItem{{Kind: "note", ID: n.ID}}}, 201)
	}
	for i := 1; i < 10; i++ {
		request(t, client, base, "POST", "/api/v1/entries", map[string]any{"title": fmt.Sprintf("另一部作品 %d", i)}, 201)
	}
	drainAchievementQueue(t, s)
	check("007", 10, true)
	check("009", 20, true)
	check("011", 3, true)
	request(t, client, base, "DELETE", fmt.Sprintf("/api/v1/memory/notes/%s?version=%d", n.ID, n.Version), nil, 204)
	drainAchievementQueue(t, s)
	check("009", 19, true)
	strangerCenter, err := s.Achievements(ctx, stranger.ID)
	if err != nil {
		t.Fatal(err)
	}
	for _, a := range strangerCenter.Items {
		if a.Metric != "recorded_anime" && (a.Value != 0 || a.Granted) {
			t.Fatal("cross-owner achievement leak")
		}
	}

	// Frozen backfills use the new metric and preserve the rule revision they previewed.
	custom, err := s.SaveAchievementRule(ctx, owner.ID, journal.AchievementRule{SeriesID: "collection-review", SeriesTitle: "测试整理", Tier: 1, Title: "三份收藏", Badge: "flower", Metric: "memory_collections", Threshold: 3, Active: true})
	if err != nil {
		t.Fatal(err)
	}
	job, err := s.PreviewAchievementBackfill(ctx, owner.ID)
	if err != nil {
		t.Fatal(err)
	}
	custom.Threshold = 99
	if _, err = s.SaveAchievementRule(ctx, owner.ID, custom); err != nil {
		t.Fatal(err)
	}
	if err = s.AdminAchievementGrant(ctx, owner.ID, journal.AchievementGrantInput{OwnerID: owner.ID, TierID: first.ID, Grant: false, Reason: "补算不能恢复撤回的授予"}); err != nil {
		t.Fatal(err)
	}
	if _, err = s.AchievementBackfillAction(ctx, owner.ID, job.ID, "run"); err != nil {
		t.Fatal(err)
	}
	if _, err = s.ProcessAchievementBackfill(ctx); err != nil {
		t.Fatal(err)
	}
	check("006", 10, false)
	center, err := s.Achievements(ctx, owner.ID)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, a := range center.Items {
		if a.ID == custom.ID {
			found = a.Granted && a.Threshold == 3 && a.Revision == 1
		}
	}
	if !found {
		t.Fatal("backfill did not honor frozen collection rule")
	}
}

func TestRecordingAchievementUpgrade(t *testing.T) {
	pool := isolatedSchema(t)
	ctx := context.Background()
	manifest, err := database.Manifest()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `CREATE TABLE schema_migrations(name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())`); err != nil {
		t.Fatal(err)
	}
	for _, m := range manifest {
		if m.Name >= "migrations/020_" {
			break
		}
		data, err := os.ReadFile("../database/" + m.Name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = pool.Exec(ctx, string(data)); err != nil {
			t.Fatal(err)
		}
		if _, err = pool.Exec(ctx, `INSERT INTO schema_migrations(name,checksum) VALUES($1,$2)`, m.Name, m.Checksum); err != nil {
			t.Fatal(err)
		}
	}
	base := coverServer(t, pool)
	client, owner := registerBrowser(t, base, "award-upgrade@example.test")
	s := journal.New(pool)
	request(t, client, base, "POST", "/api/v1/entries", map[string]any{"title": "升级前的记录"}, 201)
	n := decodeAs[journal.MemoryNote](t, request(t, client, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: "升级前的回忆"}, 201))
	request(t, client, base, "POST", "/api/v1/memory/collections", journal.CollectionInput{Title: "旧收藏", Items: []journal.CollectionItem{{Kind: "note", ID: n.ID}}}, 201)
	request(t, client, base, "POST", "/api/v1/memory/yearly", journal.YearlyInput{Title: "旧年度册", Year: 2026, NoteIDs: []string{n.ID}}, 201)
	if _, err = pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, owner.ID); err != nil {
		t.Fatal(err)
	}
	custom := journal.AchievementRule{ID: id.New(), SeriesID: "memory-pages", SeriesTitle: "记忆的页码", Tier: 2, Revision: 1, Title: "站点自己的等级", Badge: "book", Metric: "memory_notes", Threshold: 500, Active: true}
	if _, err = pool.Exec(ctx, `INSERT INTO achievement_tiers(id,series_id,tier) VALUES($1,$2,$3)`, custom.ID, custom.SeriesID, custom.Tier); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `INSERT INTO achievement_rule_revisions(tier_id,revision,title,description,badge,metric,threshold) VALUES($1,1,$2,'','book','memory_notes',500)`, custom.ID, custom.Title); err != nil {
		t.Fatal(err)
	}
	unlock := id.New()
	if _, err = pool.Exec(ctx, `INSERT INTO achievement_unlocks(id,owner_id,tier_id,rule_revision,source,value) VALUES($1,$2,'a1000000-0000-4000-8000-000000000005',1,'automatic',1)`, unlock, owner.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `INSERT INTO achievement_grants(unlock_id) VALUES($1)`, unlock); err != nil {
		t.Fatal(err)
	}
	var before string
	if err = pool.QueryRow(ctx, `SELECT row_to_json(u)::text FROM achievement_unlocks u WHERE id=$1`, unlock).Scan(&before); err != nil {
		t.Fatal(err)
	}
	if err = database.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	drainAchievementQueue(t, s)
	after, err := s.Export(ctx, owner.ID)
	if err != nil || len(after.Library.Achievements.Unlocks) != 4 {
		t.Fatal("upgrade failed to evaluate existing records", err)
	}
	var preserved string
	if err = pool.QueryRow(ctx, `SELECT row_to_json(u)::text FROM achievement_unlocks u WHERE id=$1`, unlock).Scan(&preserved); err != nil || preserved != before {
		t.Fatal("upgrade rewrote old unlock", err)
	}
	rules, err := s.AchievementRules(ctx)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, r := range rules {
		if r.SeriesID == "memory-pages" && r.Tier == 2 {
			found = r == custom
		}
	}
	if !found {
		t.Fatal("default seed replaced an administrator-defined tier")
	}
	if err = database.Migrate(ctx, pool); err != nil {
		t.Fatal("migration not idempotent", err)
	}
}
