//go:build integration

package api

import (
	"animemo.local/server/internal/external"
	"animemo.local/server/internal/journal"
	"os"
	"testing"
)

func TestLiveBangumiMetadataWorkflow(t *testing.T) {
	if os.Getenv("ANIMEMO_LIVE_BANGUMI") != "1" {
		t.Skip("opt-in live public API")
	}
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	owner, _ := registerBrowser(t, base, "live-metadata@example.test")
	page := decodeAs[external.SearchResult](t, request(t, owner, base, "GET", "/api/v1/providers/bangumi/subjects?query=%E8%91%AC%E9%80%81%E7%9A%84%E8%8A%99%E8%8E%89%E8%8E%B2&page=1", nil, 200))
	if len(page.Items) == 0 {
		t.Fatal("live Chinese search returned no results")
	}
	preview := decodeAs[external.SubjectPreview](t, request(t, owner, base, "GET", "/api/v1/providers/bangumi/subjects/400602", nil, 200))
	if preview.Metadata.TotalEpisodes != 28 {
		t.Fatalf("live metadata regressed chapter count: %d", preview.Metadata.TotalEpisodes)
	}
	input := external.ApplyInput{SubjectID: 400602, Snapshot: preview.Snapshot, Fields: []string{"title", "original_title", "format", "total_episodes", "description", "reference_url"}, Cover: true}
	created := decodeAs[journal.Entry](t, request(t, owner, base, "POST", "/api/v1/entries/from-bangumi", input, 201))
	if created.TotalEpisodes != 28 || created.Source == nil || created.CoverRevision == nil || created.Visibility != "private" {
		t.Fatal("live source did not create a complete private entry")
	}
	request(t, owner, base, "GET", "/api/v1/entries/"+created.ID+"/cover/"+*created.CoverRevision, nil, 200)
	t.Log("real Chinese search, 28-episode metadata and validated cover imported through HTTP into isolated PostgreSQL")
}
