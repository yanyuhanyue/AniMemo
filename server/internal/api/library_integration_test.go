//go:build integration

package api

import (
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
	"bytes"
	"context"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"net/http/httptest"
	"testing"
)

func TestMemoryLibraryOwnershipAndHistory(t *testing.T) {
	pool := isolatedDatabase(t)
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base})
	server.Start()
	defer server.Close()
	alice, bob := browser(), browser()
	request(t, alice, base, "POST", "/api/v1/auth/register", map[string]any{"email": "library@example.test", "display_name": "星见", "password": "library-passphrase-2026"}, 201)
	request(t, bob, base, "POST", "/api/v1/auth/register", map[string]any{"email": "other@example.test", "display_name": "别人的记忆", "password": "library-passphrase-2026"}, 201)
	e := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "长篇故事", "total_episodes": 1200}, 201))
	c := decodeAs[journal.Character](t, request(t, alice, base, "POST", "/api/v1/memory/characters", journal.CharacterInput{Name: "夏目", AnimeIDs: []string{e.AnimeID}, Aliases: []string{"夏目贵志"}}, 201))
	request(t, bob, base, "POST", "/api/v1/memory/characters", journal.CharacterInput{Name: "非法引用", AnimeIDs: []string{e.AnimeID}}, 400)
	ep := decodeAs[journal.Episode](t, request(t, alice, base, "POST", "/api/v1/memory/episodes", journal.EpisodeInput{AnimeID: e.AnimeID, Title: "第一话", Number: 1}, 201))
	n := decodeAs[journal.MemoryNote](t, request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: "第一次遇见", Body: "这是独立的长笔记", AnimeID: e.AnimeID, CharacterID: c.ID, EpisodeID: ep.ID, OccurredOn: "2026-10", TimePrecision: "month", Anchor: journal.MemoryAnchor{Kind: "quote", Quote: "温柔的回忆"}}, 201))
	request(t, bob, base, "GET", "/api/v1/memory/notes/"+n.ID, nil, 404)
	request(t, bob, base, "PUT", "/api/v1/memory/notes/"+n.ID, n.MemoryNoteInput, 404)
	input := n.MemoryNoteInput
	input.Body = "更正后的记忆"
	n = decodeAs[journal.MemoryNote](t, request(t, alice, base, "PUT", "/api/v1/memory/notes/"+n.ID, input, 200))
	request(t, alice, base, "PUT", "/api/v1/memory/notes/"+n.ID, input, 409)
	input = n.MemoryNoteInput
	input.OccurredOn = "2026-02-31"
	input.TimePrecision = "day"
	request(t, alice, base, "POST", "/api/v1/memory/notes", input, 400)
	list := decodeAs[journal.LibraryPage[journal.MemoryNote]](t, request(t, alice, base, "GET", "/api/v1/memory/notes?search=更正&year=2026", nil, 200))
	if list.Total != 1 || list.Items[0].ID != n.ID {
		t.Fatal("search lost note")
	}
	target := decodeAs[journal.Character](t, request(t, alice, base, "POST", "/api/v1/memory/characters", journal.CharacterInput{Name: "夏目 · 规范身份"}, 201))
	request(t, alice, base, "POST", "/api/v1/memory/identities/character/"+c.ID, map[string]any{"version": c.Version, "target_id": target.ID}, 204)
	list = decodeAs[journal.LibraryPage[journal.MemoryNote]](t, request(t, alice, base, "GET", "/api/v1/memory/notes?character_id="+target.ID, nil, 200))
	if list.Total != 1 {
		t.Fatal("merged character lost note projection")
	}
	request(t, alice, base, "POST", "/api/v1/memory/identities/character/"+target.ID, map[string]any{"version": target.Version, "target_id": c.ID}, 400)
	request(t, alice, base, "POST", "/api/v1/memory/identities/character/"+c.ID, map[string]any{"version": c.Version + 1, "target_id": ""}, 204)
	before := decodeAs[journal.Entry](t, request(t, alice, base, "GET", "/api/v1/entries/"+e.ID, nil, 200))
	assertion := decodeAs[journal.ProgressAssertion](t, request(t, alice, base, "POST", "/api/v1/memory/progress", journal.ProgressAssertionInput{AnimeID: e.AnimeID, Scope: "mainline", Precision: "caught_up", Episodes: []journal.VersionedID{{ID: ep.ID, Version: ep.Version}}}, 201))
	if len(assertion.EpisodeIDs) != 1 {
		t.Fatal("missing scope snapshot")
	}
	after := decodeAs[journal.Entry](t, request(t, alice, base, "GET", "/api/v1/entries/"+e.ID, nil, 200))
	if before.WatchedEpisodes != after.WatchedEpisodes {
		t.Fatal("assertion invented viewing facts")
	}
	collection := decodeAs[journal.MemoryCollection](t, request(t, alice, base, "POST", "/api/v1/memory/collections", journal.CollectionInput{Title: "温柔的人", Items: []journal.CollectionItem{{Kind: "character", ID: c.ID}, {Kind: "note", ID: n.ID}}}, 201))
	if len(collection.Items) != 2 {
		t.Fatal("collection incomplete")
	}
	request(t, bob, base, "POST", "/api/v1/memory/collections", journal.CollectionInput{Title: "越权", Items: []journal.CollectionItem{{Kind: "note", ID: n.ID}}}, 400)
	yearly := decodeAs[journal.YearlyMemory](t, request(t, alice, base, "POST", "/api/v1/memory/yearly", journal.YearlyInput{Year: 2026, Timezone: "Asia/Shanghai", Title: "我的 2026", NoteIDs: []string{n.ID}}, 201))
	if len(yearly.Revisions) != 1 || yearly.Revisions[0].Items[0].Body != n.Body {
		t.Fatal("yearly snapshot missing")
	}
	input = n.MemoryNoteInput
	input.Body = "后来想法变了"
	request(t, alice, base, "PUT", "/api/v1/memory/notes/"+n.ID, input, 200)
	yearly = decodeAs[journal.YearlyMemory](t, request(t, alice, base, "GET", "/api/v1/memory/yearly/"+yearly.ID, nil, 200))
	if yearly.Revisions[0].Items[0].Body != n.Body {
		t.Fatal("finalized memory changed with source")
	}
	request(t, alice, base, "DELETE", fmt.Sprintf("/api/v1/entries/%s?version=%d", e.ID, after.Version), nil, 204)
	preserved := decodeAs[journal.MemoryNote](t, request(t, alice, base, "GET", "/api/v1/memory/notes/"+n.ID, nil, 200))
	if preserved.AnimeID != e.AnimeID || preserved.Body != "后来想法变了" {
		t.Fatal("removing entry erased memory")
	}
	request(t, bob, base, "GET", "/api/v1/memory/yearly/"+yearly.ID, nil, 404)
}
func TestPrivateMemoryMediaLifecycle(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base})
	server.Start()
	defer server.Close()
	alice, bob := browser(), browser()
	request(t, alice, base, "POST", "/api/v1/auth/register", map[string]any{"email": "media@example.test", "display_name": "星见", "password": "library-passphrase-2026"}, 201)
	request(t, bob, base, "POST", "/api/v1/auth/register", map[string]any{"email": "other@example.test", "display_name": "他人", "password": "library-passphrase-2026"}, 201)
	var owner string
	if err := pool.QueryRow(ctx, `SELECT id FROM users WHERE email='media@example.test'`).Scan(&owner); err != nil {
		t.Fatal(err)
	}
	img := image.NewRGBA(image.Rect(0, 0, 20, 10))
	img.Set(0, 0, color.RGBA{R: 250, A: 255})
	var encoded bytes.Buffer
	png.Encode(&encoded, img)
	data := encoded.Bytes()
	service := journal.New(pool)
	reserved := decodeAs[journal.MemoryMedia](t, request(t, alice, base, "POST", "/api/v1/memory/media", map[string]int{"byte_size": len(data)}, 201))
	request(t, alice, base, "GET", "/api/v1/memory/media/"+reserved.ID, nil, 404)
	ready, err := service.FinalizeMemoryMedia(ctx, owner, reserved.ID, "image/png", data)
	if err != nil {
		t.Fatal(err)
	}
	if ready.SHA256 == "" || ready.State != "ready" {
		t.Fatal("missing authoritative image metadata")
	}
	request(t, bob, base, "GET", "/api/v1/memory/media/"+reserved.ID, nil, 404)
	response := request(t, alice, base, "GET", "/api/v1/memory/media/"+reserved.ID, nil, 200)
	if !bytes.Equal(response.body, data) {
		t.Fatal("original bytes changed")
	}
	request(t, alice, base, "GET", "/api/v1/memory/media/"+reserved.ID+"?thumbnail=true", nil, 200)
	moment := decodeAs[journal.MemoryNote](t, request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Kind: "moment", Title: "那束光", MediaIDs: []string{ready.ID}}, 201))
	request(t, bob, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Kind: "moment", Title: "越权", MediaIDs: []string{ready.ID}}, 400)
	request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Kind: "moment", Title: "不存在", MediaIDs: []string{id.New()}}, 400)
	request(t, alice, base, "DELETE", "/api/v1/memory/media/"+ready.ID, nil, 204)
	request(t, alice, base, "GET", "/api/v1/memory/media/"+ready.ID, nil, 404)
	moment = decodeAs[journal.MemoryNote](t, request(t, alice, base, "GET", "/api/v1/memory/notes/"+moment.ID, nil, 200))
	if moment.MediaIDs[0] != ready.ID {
		t.Fatal("deleted original lost tombstone reference")
	}
	moment.Body = "原图删除，文字保留"
	request(t, alice, base, "PUT", "/api/v1/memory/notes/"+moment.ID, moment.MemoryNoteInput, 200)
	var empty bool
	if err = pool.QueryRow(ctx, `SELECT data IS NULL AND thumbnail IS NULL FROM private_memory_media WHERE id=$1`, ready.ID).Scan(&empty); err != nil || !empty {
		t.Fatal("deletion retained reachable image bytes")
	}
}

func TestMemoryAchievementsAndSearch(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base})
	server.Start()
	defer server.Close()
	alice, bob := browser(), browser()
	request(t, alice, base, "POST", "/api/v1/auth/register", map[string]any{"email": "achievement@example.test", "display_name": "星见", "password": "library-passphrase-2026"}, 201)
	request(t, bob, base, "POST", "/api/v1/auth/register", map[string]any{"email": "stranger@example.test", "display_name": "他人", "password": "library-passphrase-2026"}, 201)
	var owner string
	if err := pool.QueryRow(ctx, `SELECT id FROM users WHERE email='achievement@example.test'`).Scan(&owner); err != nil {
		t.Fatal(err)
	}
	entry := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "星光长篇", "total_episodes": 1200}, 201))
	request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: "星光札记", Body: "雨后的温柔", AnimeID: entry.AnimeID, OccurredOn: "2026", TimePrecision: "year"}, 201)
	search := decodeAs[journal.LibraryPage[journal.MemorySearchItem]](t, request(t, alice, base, "GET", "/api/v1/memory/search?search=星光", nil, 200))
	if search.Total != 2 {
		t.Fatalf("search total=%d", search.Total)
	}
	search = decodeAs[journal.LibraryPage[journal.MemorySearchItem]](t, request(t, bob, base, "GET", "/api/v1/memory/search?search=星光", nil, 200))
	if search.Total != 0 {
		t.Fatal("cross-owner search leak")
	}
	request(t, alice, base, "POST", "/api/v1/memory/episodes/batch", journal.EpisodeBatchInput{AnimeID: entry.AnimeID, From: 1, To: 1200, Kind: "main", ProgressRole: "required"}, 201)
	request(t, alice, base, "POST", "/api/v1/memory/episodes/batch", journal.EpisodeBatchInput{AnimeID: entry.AnimeID, From: 1200, To: 1210, Kind: "main", ProgressRole: "required"}, 400)
	eps := decodeAs[journal.LibraryPage[journal.Episode]](t, request(t, alice, base, "GET", "/api/v1/memory/episodes?anime_id="+entry.AnimeID+"&page=24", nil, 200))
	if eps.Total != 1200 || len(eps.Items) != 50 {
		t.Fatal("long episode pagination")
	}
	service := journal.New(pool)
	if _, err := service.ProcessAchievements(ctx); err != nil {
		t.Fatal(err)
	}
	center := decodeAs[journal.AchievementCenter](t, request(t, alice, base, "GET", "/api/v1/memory/achievements", nil, 200))
	var unlock string
	const tier = "a1000000-0000-4000-8000-000000000005"
	for _, a := range center.Items {
		if a.ID == tier && a.Granted {
			unlock = a.UnlockID
		}
	}
	if unlock == "" {
		t.Fatal("note achievement not projected")
	}
	request(t, bob, base, "PUT", "/api/v1/memory/achievements/showcase", map[string]any{"unlock_ids": []string{unlock}}, 400)
	request(t, alice, base, "PUT", "/api/v1/memory/achievements/showcase", map[string]any{"unlock_ids": []string{unlock}}, 204)
	request(t, alice, base, "POST", "/api/v1/memory/achievements/acknowledge", nil, 204)
	request(t, bob, base, "GET", "/api/v1/admin/achievements/rules", nil, 403)
	if _, err := pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, owner); err != nil {
		t.Fatal(err)
	}
	rules, err := service.AchievementRules(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var changed journal.AchievementRule
	for _, r := range rules {
		if r.ID == tier {
			changed = r
		}
	}
	changed.Threshold = 2
	changed.Title = "新门槛"
	changed, err = service.SaveAchievementRule(ctx, owner, changed)
	if err != nil {
		t.Fatal(err)
	}
	if err = service.AdminAchievementGrant(ctx, owner, journal.AchievementGrantInput{OwnerID: owner, TierID: tier, Grant: false, Reason: "测试撤回"}); err != nil {
		t.Fatal(err)
	}
	job, err := service.PreviewAchievementBackfill(ctx, owner)
	if err != nil {
		t.Fatal(err)
	}
	if job.State != "preview" || job.Total != 2 {
		t.Fatal("incorrect preview")
	}
	if _, err = service.AchievementBackfillAction(ctx, owner, job.ID, "run"); err != nil {
		t.Fatal(err)
	}
	if _, err = service.AchievementBackfillAction(ctx, owner, job.ID, "pause"); err != nil {
		t.Fatal(err)
	}
	if worked, err := service.ProcessAchievementBackfill(ctx); err != nil || worked {
		t.Fatal("paused task executed", err)
	}
	if _, err = service.AchievementBackfillAction(ctx, owner, job.ID, "run"); err != nil {
		t.Fatal(err)
	}
	if _, err = service.ProcessAchievementBackfill(ctx); err != nil {
		t.Fatal(err)
	}
	center, err = service.Achievements(ctx, owner)
	if err != nil {
		t.Fatal(err)
	}
	for _, a := range center.Items {
		if a.ID == tier && (a.Granted || a.ShowcaseSlot != 0 || a.Title == changed.Title || a.Revision != 1) {
			t.Fatal("revocation or immutable rule history lost")
		}
	}
	var count int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM achievement_unlocks WHERE owner_id=$1 AND tier_id=$2`, owner, tier).Scan(&count); err != nil || count != 1 {
		t.Fatal("duplicate unlock", err)
	}
}
