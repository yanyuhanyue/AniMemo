//go:build integration

package api

import (
	"context"
	"fmt"
	"net/http"
	"testing"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
)

func registerBrowser(t *testing.T, base, email string) (*http.Client, accounts.User) {
	t.Helper()
	client := browser()
	user := decodeAs[accounts.User](t, request(t, client, base, "POST", "/api/v1/auth/register", map[string]string{"email": email, "password": "foundation-passphrase", "display_name": "测试手账"}, 201))
	return client, user
}

func TestSettingsAndCredentialLifecycle(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, user := registerBrowser(t, base, "settings@example.test")
	bob, _ := registerBrowser(t, base, "other@example.test")
	device := browser()
	request(t, device, base, "POST", "/api/v1/auth/login", map[string]string{"email": user.Email, "password": "foundation-passphrase"}, 200)
	settings := accounts.Settings{Version: user.Version, DisplayName: "新昵称", Bio: "一段简介", Accent: "green", DefaultView: "list"}
	updated := decodeAs[accounts.User](t, request(t, alice, base, "PUT", "/api/v1/settings", settings, 200))
	if updated.DisplayName != "新昵称" || updated.DefaultView != "list" || updated.Version != 2 {
		t.Fatalf("settings not saved: %+v", updated)
	}
	request(t, alice, base, "PUT", "/api/v1/settings", settings, 409)
	updated = decodeAs[accounts.User](t, uploadCover(t, alice, base, "/api/v1/avatar?version=2", "image/png", coverFixture(t, "image/png"), 200))
	if updated.AvatarRevision == nil || updated.Version != 3 {
		t.Fatal("avatar metadata missing")
	}
	avatarPath := "/api/v1/avatar/" + *updated.AvatarRevision
	request(t, alice, base, "GET", avatarPath, nil, 200)
	request(t, bob, base, "GET", avatarPath, nil, 404)
	request(t, browser(), base, "GET", avatarPath, nil, 401)
	request(t, alice, base, "POST", "/api/v1/auth/password", map[string]string{"current_password": "wrong", "new_password": "changed-passphrase"}, 401)
	request(t, device, base, "GET", "/api/v1/auth/me", nil, 200)
	request(t, alice, base, "POST", "/api/v1/auth/password", map[string]string{"current_password": "foundation-passphrase", "new_password": "changed-passphrase"}, 200)
	request(t, device, base, "GET", "/api/v1/auth/me", nil, 401)
	request(t, device, base, "POST", "/api/v1/auth/login", map[string]string{"email": user.Email, "password": "foundation-passphrase"}, 401)
	request(t, device, base, "POST", "/api/v1/auth/login", map[string]string{"email": user.Email, "password": "changed-passphrase"}, 200)
	request(t, alice, base, "POST", "/api/v1/auth/logout-all", nil, 204)
	request(t, alice, base, "GET", "/api/v1/auth/me", nil, 401)
	request(t, device, base, "GET", "/api/v1/auth/me", nil, 401)
	request(t, alice, base, "POST", "/api/v1/auth/login", map[string]string{"email": user.Email, "password": "changed-passphrase"}, 200)
	request(t, alice, base, "POST", "/api/v1/entries", map[string]string{"title": "注销时级联删除"}, 201)
	request(t, alice, base, "DELETE", "/api/v1/auth/account", map[string]string{"password": "wrong"}, 401)
	request(t, alice, base, "DELETE", "/api/v1/auth/account", map[string]string{"password": "changed-passphrase"}, 204)
	request(t, alice, base, "GET", "/api/v1/auth/me", nil, 401)
	var count int
	err := pool.QueryRow(context.Background(), `SELECT (SELECT count(*) FROM users WHERE id=$1)+(SELECT count(*) FROM entries WHERE user_id=$1)+(SELECT count(*) FROM avatars WHERE user_id=$1)+(SELECT count(*) FROM sessions WHERE user_id=$1)`, user.ID).Scan(&count)
	if err != nil || count != 0 {
		t.Fatalf("account cascade: %d %v", count, err)
	}
}

func TestJournalManagementAndHistory(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, _ := registerBrowser(t, base, "journal@example.test")
	bob, _ := registerBrowser(t, base, "other@example.test")
	create := func(client *http.Client, title string) journal.Entry {
		return decodeAs[journal.Entry](t, request(t, client, base, "POST", "/api/v1/entries", map[string]any{"title": title, "total_episodes": 12, "score": 9.7, "details": map[string]string{"studio": "制作公司", "airing_period": "2026 年春季", "description": "简介", "reference_url": "https://example.test/work"}}, 201))
	}
	a, b, foreign := create(alice, "A"), create(alice, "B"), create(bob, "private")
	if a.Score == nil || *a.Score != 9.7 || a.Details.Studio != "制作公司" {
		t.Fatal("extended fields were lost")
	}
	request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "invalid", "score": 9.77}, 400)
	request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "invalid", "details": map[string]string{"reference_url": "javascript:alert(1)"}}, 400)
	bulk := journal.BulkInput{Entries: []journal.VersionedID{{ID: a.ID, Version: 1}, {ID: b.ID, Version: 1}}, Action: "tag-add", Value: "治愈"}
	request(t, alice, base, "POST", "/api/v1/entries/bulk", bulk, 200)
	bulk.Action, bulk.Value = "status", "completed"
	bulk.Entries = []journal.VersionedID{{ID: a.ID, Version: 2}, {ID: foreign.ID, Version: 1}}
	request(t, alice, base, "POST", "/api/v1/entries/bulk", bulk, 404)
	still := decodeAs[journal.Entry](t, request(t, alice, base, "GET", "/api/v1/entries/"+a.ID, nil, 200))
	if still.Status != "planned" || still.Version != 2 {
		t.Fatal("failed bulk mutation partially committed")
	}
	bulk.Entries = []journal.VersionedID{{ID: a.ID, Version: 2}, {ID: b.ID, Version: 1}}
	request(t, alice, base, "POST", "/api/v1/entries/bulk", bulk, 409)
	request(t, alice, base, "PUT", "/api/v1/tags", journal.Tag{Name: "治愈", Color: "#123456"}, 204)
	request(t, bob, base, "PUT", "/api/v1/tags", journal.Tag{Name: "治愈", Color: "#abcdef"}, 204)
	for client, color := range map[*http.Client]string{alice: "#123456", bob: "#abcdef"} {
		tags := decodeAs[struct {
			Items []journal.Tag `json:"items"`
		}](t, request(t, client, base, "GET", "/api/v1/tags", nil, 200))
		if len(tags.Items) != 1 || tags.Items[0].Color != color {
			t.Fatal("tag colors crossed owners")
		}
	}
	f := decodeAs[journal.QuickFilter](t, request(t, alice, base, "POST", "/api/v1/filters", journal.QuickFilter{Name: "想看的治愈番", Search: "治愈", Status: "planned", Sort: "score"}, 201))
	request(t, bob, base, "DELETE", "/api/v1/filters/"+f.ID, nil, 404)
	request(t, alice, base, "DELETE", "/api/v1/filters/"+f.ID, nil, 204)
	watchPath := "/api/v1/entries/" + a.ID + "/history"
	watch := journal.RecordInput{WatchedOn: "2026-10-01", EpisodeFrom: 1, EpisodeTo: 3, Rewatch: 2, RequestID: id.New()}
	first := decodeAs[journal.RecordResult](t, request(t, alice, base, "POST", watchPath, watch, 201))
	if first.Record.Rewatch != 2 || first.Record.Version != 1 {
		t.Fatal("rewatch fields missing")
	}
	watch.RequestID = id.New()
	watch.WatchedOn = "2026-10-02"
	watch.EpisodeFrom = 4
	watch.EpisodeTo = 8
	second := decodeAs[journal.RecordResult](t, request(t, alice, base, "POST", watchPath, watch, 201))
	patch := journal.RecordPatch{Version: 1, WatchedOn: "2026-10-03", EpisodeFrom: 4, EpisodeTo: 6, Rewatch: 2, Note: "更正"}
	corrected := decodeAs[journal.Entry](t, request(t, alice, base, "PATCH", watchPath+"/"+second.Record.ID, patch, 200))
	if corrected.WatchedEpisodes != 6 {
		t.Fatal("progress not recomputed after correction")
	}
	request(t, alice, base, "PATCH", watchPath+"/"+second.Record.ID, patch, 409)
	request(t, bob, base, "DELETE", watchPath+"/"+second.Record.ID+"?version=2", nil, 404)
	analytics := decodeAs[journal.Analytics](t, request(t, alice, base, "GET", "/api/v1/analytics?from=2026-10-01&to=2026-10-03", nil, 200))
	if analytics.ActiveDays != 2 || analytics.Episodes != 6 || analytics.Records != 2 || len(analytics.Months) != 1 {
		t.Fatalf("wrong analytics: %+v", analytics)
	}
	page := decodeAs[journal.HistoryPage](t, request(t, alice, base, "GET", "/api/v1/history/page?from=2026-10-03&to=2026-10-03", nil, 200))
	if page.Total != 1 || page.Items[0].ID != second.Record.ID {
		t.Fatal("date boundaries not inclusive")
	}
	request(t, alice, base, "GET", "/api/v1/history/page?from=2026-10-03&to=2026-10-01", nil, 400)
	request(t, alice, base, "GET", "/api/v1/analytics?from=2026-02-30", nil, 400)
	remaining := decodeAs[journal.Entry](t, request(t, alice, base, "DELETE", watchPath+"/"+second.Record.ID+"?version=2", nil, 200))
	if remaining.WatchedEpisodes != 3 {
		t.Fatal("history deletion lost remaining progress")
	}
	// Verify stable real pagination beyond the old 100-record display ceiling.
	ctx := context.Background()
	for range 105 {
		_, err := pool.Exec(ctx, `INSERT INTO watch_records(id,entry_id,watched_on,episode_from,episode_to,request_id) VALUES($1,$2,'2026-09-01',1,1,$3)`, id.New(), a.ID, id.New())
		if err != nil {
			t.Fatal(err)
		}
	}
	page = decodeAs[journal.HistoryPage](t, request(t, alice, base, "GET", fmt.Sprintf("/api/v1/history/page?entry_id=%s&page=3", a.ID), nil, 200))
	if page.Total != 106 || len(page.Items) != 6 {
		t.Fatalf("pagination: %+v", page)
	}
	private := decodeAs[journal.HistoryPage](t, request(t, bob, base, "GET", "/api/v1/history/page", nil, 200))
	if private.Total != 0 {
		t.Fatal("history leaked")
	}
}
