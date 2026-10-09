//go:build integration

package api

import (
	"fmt"
	"net/http"
	"testing"

	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
)

func TestMemoryReferenceCovers(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, bob := browser(), browser()
	for name, client := range map[string]*http.Client{"alice": alice, "bob": bob} {
		request(t, client, base, "POST", "/api/v1/auth/register", map[string]string{"email": name + "@example.test", "display_name": name, "password": "reference-cover-passphrase"}, 201)
	}
	entry := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]string{"title": "有关联封面的作品"}, 201))
	plain := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]string{"title": "没有封面的作品"}, 201))
	note := decodeAs[journal.MemoryNote](t, request(t, alice, base, "POST", "/api/v1/memory/notes", journal.MemoryNoteInput{Title: "作品感想", AnimeID: entry.AnimeID}, 201))
	entry = decodeAs[journal.Entry](t, uploadCover(t, alice, base, "/api/v1/entries/"+entry.ID+"/cover?version=1", "image/png", coverFixture(t, "image/png"), 200))
	items := []journal.CollectionItem{{Kind: "anime", ID: entry.AnimeID}, {Kind: "anime", ID: plain.AnimeID}, {Kind: "note", ID: note.ID}, {Kind: "anime", ID: id.New()}}
	lookup := func(client *http.Client) []journal.MemoryReference {
		t.Helper()
		return decodeAs[struct {
			Items []journal.MemoryReference `json:"items"`
		}](t, request(t, client, base, "POST", "/api/v1/memory/references", map[string]any{"items": items}, 200)).Items
	}
	refs := lookup(alice)
	if len(refs) != len(items) || refs[0].EntryID != entry.ID || refs[0].CoverRevision != *entry.CoverRevision || !refs[0].Available {
		t.Fatalf("missing owner cover: %+v", refs)
	}
	if !refs[1].Available || refs[1].CoverRevision != "" || !refs[2].Available || refs[2].EntryID != "" || refs[2].CoverRevision != "" || refs[3].Available {
		t.Fatalf("plain, note or missing reference changed: %+v", refs)
	}
	for _, ref := range lookup(bob) {
		if ref.Available || ref.Title != "" || ref.EntryID != "" || ref.CoverRevision != "" {
			t.Fatalf("another owner obtained private reference metadata: %+v", ref)
		}
	}
	imagePath := "/api/v1/entries/" + entry.ID + "/cover/" + *entry.CoverRevision
	request(t, alice, base, "GET", imagePath, nil, 200)
	request(t, bob, base, "GET", imagePath, nil, 404)
	request(t, browser(), base, "GET", imagePath, nil, 401)
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "DELETE", fmt.Sprintf("/api/v1/entries/%s/cover?version=%d", entry.ID, entry.Version), nil, 200))
	if refs = lookup(alice); refs[0].CoverRevision != "" || !refs[0].Available {
		t.Fatalf("removed cover survived in references: %+v", refs[0])
	}
	entry = decodeAs[journal.Entry](t, uploadCover(t, alice, base, fmt.Sprintf("/api/v1/entries/%s/cover?version=%d", entry.ID, entry.Version), "image/jpeg", coverFixture(t, "image/jpeg"), 200))
	request(t, alice, base, "DELETE", fmt.Sprintf("/api/v1/entries/%s?version=%d", entry.ID, entry.Version), nil, 204)
	if refs = lookup(alice); !refs[0].Available || refs[0].Title == "" || refs[0].EntryID != "" || refs[0].CoverRevision != "" {
		t.Fatalf("deleted entry should retain its memory identity without exposing its cover: %+v", refs[0])
	}
}
