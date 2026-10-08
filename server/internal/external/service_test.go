package external

import (
	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/journal"
	"testing"
)

func TestMetadataUsesDeclaredEpisodes(t *testing.T) {
	for _, n := range []int{0, 28} {
		preview := metadataFor(bangumi.Subject{ID: 400602, Name: "test", Episodes: n, TotalEpisodes: 36})
		if preview.Metadata.TotalEpisodes != n {
			t.Fatalf("database chapter count replaced declared episodes: %+v", preview.Metadata)
		}
	}
}

func TestRememberedStatusCannotBeGuessedForRemotePush(t *testing.T) {
	item := SyncItem{Local: &journal.SourceRecord{Value: journal.SyncValue{Status: "recorded"}}}
	if _, err := pushTarget(item, false); err == nil {
		t.Fatal("an unspecified local recollection must not become a remote viewing status")
	}
}
