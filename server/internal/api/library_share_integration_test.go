//go:build integration

package api

import (
	"animemo.local/server/internal/journal"
	"context"
	"encoding/json"
	"net/url"
	"testing"
)

func TestMemorySharingRevocationAndHomepage(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, au := registerBrowser(t, base, "memory-share@example.test")
	anon := browser()
	ctx := context.Background()
	s := journal.New(pool)
	if _, err := pool.Exec(ctx, `UPDATE users SET sharing_enabled=true,public_state='published',is_admin=true WHERE id=$1`, au.ID); err != nil {
		t.Fatal(err)
	}
	// Merely having an eligible administrator cannot elect the site homepage.
	home := request(t, anon, base, "GET", "/api/v1/homepage", nil, 200)
	if string(home.body) == "" {
		t.Fatal("empty homepage response")
	}
	var page map[string]any
	if err := json.Unmarshal(home.body, &page); err != nil {
		t.Fatal(err)
	}
	if page["owner"] != nil {
		t.Fatal("administrator implicitly became homepage owner")
	}
	if _, err := pool.Exec(ctx, `UPDATE site_settings SET homepage_owner=$1`, au.ID); err != nil {
		t.Fatal(err)
	}
	page = decodeAs[map[string]any](t, request(t, anon, base, "GET", "/api/v1/homepage", nil, 200))
	if page["owner"] == nil {
		t.Fatal("configured homepage missing")
	}
	raw := coverFixture(t, "image/png")
	m, err := s.ReserveMemoryMedia(ctx, au.ID, len(raw))
	if err != nil {
		t.Fatal(err)
	}
	m, err = s.FinalizeMemoryMedia(ctx, au.ID, m.ID, "image/png", raw)
	if err != nil {
		t.Fatal(err)
	}
	public := decodeAs[journal.MemoryNote](t, request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Kind: "moment", Title: "分享的光", Body: "可展示文字", MediaIDs: []string{m.ID}, Visibility: "unlisted"}, 201))
	private := decodeAs[journal.MemoryNote](t, request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: "私人选材", Body: "这段不能泄漏"}, 201))
	y := decodeAs[journal.YearlyMemory](t, request(t, alice, base, "POST", "/api/v1/memory/yearly", journal.YearlyInput{Year: 2026, Timezone: "Asia/Shanghai", Title: "一年的光", NoteIDs: []string{public.ID, private.ID}, Visibility: "unlisted"}, 201))
	token := decodeAs[journal.MemoryShareResult](t, request(t, alice, base, "POST", "/api/v1/memory/shares", journal.MemoryShareInput{Kind: "yearly", ResourceID: y.ID, Days: 1}, 200))
	path := "/api/v1/memory/shared/" + url.PathEscape(token.Token)
	shared := decodeAs[journal.SharedMemory](t, request(t, anon, base, "GET", path, nil, 200))
	if len(shared.Items) != 1 || shared.Items[0].Body != "可展示文字" {
		t.Fatal("private selection leaked")
	}
	resp := request(t, anon, base, "GET", path+"/media/"+m.ID, nil, 200)
	if resp.header.Get("Cache-Control") != "private, no-store" {
		t.Fatal("shared image cached")
	}
	request(t, anon, base, "GET", "/api/v1/memory/public/yearly/"+y.ID, nil, 404)
	// Publishing a revised source cannot expose its older private frozen text.
	privateInput := private.MemoryNoteInput
	privateInput.Visibility = "unlisted"
	privateInput.Body = "新公开的文字"
	request(t, alice, base, "PUT", "/api/v1/memory/notes/"+private.ID, privateInput, 200)
	shared = decodeAs[journal.SharedMemory](t, request(t, anon, base, "GET", path, nil, 200))
	if len(shared.Items) != 1 {
		t.Fatal("old private revision became shared")
	}
	// Original deletion also revokes the frozen projection's thumbnail access.
	request(t, alice, base, "DELETE", "/api/v1/memory/media/"+m.ID, nil, 204)
	request(t, anon, base, "GET", path+"/media/"+m.ID, nil, 404)
	shared = decodeAs[journal.SharedMemory](t, request(t, anon, base, "GET", path, nil, 200))
	if shared.Items[0].Unavailable == "" {
		t.Fatal("deleted original still advertised")
	}
	in := public.MemoryNoteInput
	in.Visibility = "private"
	request(t, alice, base, "PUT", "/api/v1/memory/notes/"+public.ID, in, 200)
	shared = decodeAs[journal.SharedMemory](t, request(t, anon, base, "GET", path, nil, 200))
	if len(shared.Items) != 0 {
		t.Fatal("private source remains shared")
	}
	next := decodeAs[journal.MemoryShareResult](t, request(t, alice, base, "POST", "/api/v1/memory/shares", journal.MemoryShareInput{Kind: "yearly", ResourceID: y.ID, Days: 1}, 200))
	if next.Token == token.Token {
		t.Fatal("rotation reused token")
	}
	request(t, anon, base, "GET", path, nil, 404)
	request(t, alice, base, "POST", "/api/v1/memory/shares", journal.MemoryShareInput{Kind: "yearly", ResourceID: y.ID, Revoke: true}, 200)
	request(t, anon, base, "GET", "/api/v1/memory/shared/"+next.Token, nil, 404)
	if _, err = pool.Exec(ctx, `UPDATE users SET sharing_enabled=false WHERE id=$1`, au.ID); err != nil {
		t.Fatal(err)
	}
	page = decodeAs[map[string]any](t, request(t, anon, base, "GET", "/api/v1/homepage", nil, 200))
	if page["owner"] != nil {
		t.Fatal("homepage ignored sharing revocation")
	}
}
