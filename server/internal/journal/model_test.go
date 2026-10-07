package journal

import (
	"strings"
	"testing"
)

func validEntry() Entry {
	return Entry{Title: "葬送的芙莉莲", Status: "watching", Format: "tv", TotalEpisodes: 28, WatchedEpisodes: 3, Accent: "violet"}
}

func TestEntryPreservesUnicodeAndNormalizesTags(t *testing.T) {
	e := validEntry()
	e.Title = "  葬送的芙莉莲  "
	e.Tags = []string{" 治愈 ", "治愈", "", "冒险"}
	if err := e.Validate(); err != nil {
		t.Fatal(err)
	}
	if e.Title != "葬送的芙莉莲" || len(e.Tags) != 2 || e.Tags[0] != "治愈" {
		t.Fatalf("unexpected normalization: %+v", e)
	}
	e.Title = strings.Repeat("番", 160)
	if err := e.Validate(); err != nil {
		t.Fatalf("160 Chinese characters should fit: %v", err)
	}
	e.Title += "剧"
	if e.Validate() == nil {
		t.Fatal("accepted a title above the character limit")
	}
}

func TestEntryCannotHideExistingProgressByReducingTotal(t *testing.T) {
	e := validEntry()
	e.TotalEpisodes = 2
	if e.Validate() == nil {
		t.Fatal("accepted total below existing progress")
	}
	e.TotalEpisodes = 0
	if err := e.Validate(); err != nil {
		t.Fatalf("unknown episode total is valid: %v", err)
	}
}

func TestWatchRecordRequiresRealDateOrderedEpisodesAndRetryIdentity(t *testing.T) {
	input := RecordInput{WatchedOn: "2026-02-28", EpisodeFrom: 1, EpisodeTo: 3, RequestID: "f87f0330-2219-477c-9c56-b9e519740291"}
	if err := input.Validate(); err != nil {
		t.Fatal(err)
	}
	invalid := input
	invalid.WatchedOn = "2026-02-30"
	if invalid.Validate() == nil {
		t.Fatal("accepted a nonexistent date")
	}
	invalid = input
	invalid.EpisodeFrom = 4
	if invalid.Validate() == nil {
		t.Fatal("accepted reversed episode range")
	}
	invalid = input
	invalid.RequestID = ""
	if invalid.Validate() == nil {
		t.Fatal("accepted write without retry identity")
	}
}
