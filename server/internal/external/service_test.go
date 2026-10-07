package external

import (
	"animemo.local/server/internal/bangumi"
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
