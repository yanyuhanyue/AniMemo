package journal

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"

	"animemo.local/server/internal/id"
)

func TestCSVAndJournalImportValidation(t *testing.T) {
	ctx := context.Background()
	doc, _, err := parseImport(ctx, "csv", []byte("\ufefftitle,total_episodes,score,tags,description\r\n\"番剧,名称\",12,9.6,治愈|冒险,\"多行\n简介\"\r\n"))
	if err != nil {
		t.Fatal(err)
	}
	if len(doc.Items) != 1 || *doc.Items[0].Entry.Score != 9.6 || len(doc.Items[0].Entry.Tags) != 2 {
		t.Fatal("valid CSV lost fields")
	}
	for _, data := range []string{"title,title\na,b\n", "title,unknown\na,b\n", "title,score\na,NaN\n", "title,total_episodes\na,no\n", "title\n\"broken", "title\na\x00b\n"} {
		if _, _, err := parseImport(ctx, "csv", []byte(data)); err == nil {
			t.Fatalf("accepted invalid CSV: %q", data)
		}
	}
	e := validEntry()
	e.ID = id.New()
	e.WatchedEpisodes = 2
	r := Record{ID: id.New(), EntryID: e.ID, WatchedOn: "2026-10-07", EpisodeFrom: 1, EpisodeTo: 3, RequestID: id.New()}
	data, _ := json.Marshal(Export{Schema: "animemo.journal/v1", Entries: []Entry{e}, History: []Record{r}})
	if _, _, err := parseImport(ctx, "json", data); err == nil {
		t.Fatal("accepted history beyond entry progress")
	}
	if _, _, err := parseImport(ctx, "json", []byte(`{"schema":"unknown","entries":[]}`)); err == nil {
		t.Fatal("accepted unknown schema")
	}
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	data, _ = json.Marshal(Export{Schema: "animemo.journal/v1", Entries: []Entry{e}})
	if _, _, err := parseImport(cancelled, "json", data); err == nil {
		t.Fatal("cancelled import continued")
	}
}

func TestBackupRejectsTamperingPathsAndSymlinks(t *testing.T) {
	for _, test := range []struct {
		name    string
		path    string
		symlink bool
		badHash bool
	}{
		{"checksum", "journal.json", false, true}, {"traversal", "../journal.json", false, false}, {"symlink", "journal.json", true, false}, {"unknown", "script.js", false, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			var data bytes.Buffer
			archive := zip.NewWriter(&data)
			header := &zip.FileHeader{Name: test.path}
			if test.symlink {
				header.SetMode(os.ModeSymlink | 0777)
			}
			writer, err := archive.CreateHeader(header)
			if err != nil {
				t.Fatal(err)
			}
			body := []byte(`{"schema":"animemo.journal/v1","entries":[],"history":[]}`)
			writer.Write(body)
			sum := sha256.Sum256(body)
			hash := hex.EncodeToString(sum[:])
			if test.badHash {
				hash = "wrong"
			}
			manifest, _ := json.Marshal(BundleManifest{Schema: "animemo.backup/v1", Files: map[string]string{test.path: hash}})
			writer, err = archive.Create("manifest.json")
			if err != nil {
				t.Fatal(err)
			}
			writer.Write(manifest)
			if err = archive.Close(); err != nil {
				t.Fatal(err)
			}
			if _, err = readBundle(context.Background(), data.Bytes()); err == nil {
				t.Fatal("accepted invalid backup")
			}
		})
	}
}
