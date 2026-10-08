//go:build integration

package api

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"sync"
	"testing"

	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
)

func importFile(t *testing.T, client *http.Client, base, format string, data []byte, want int) journal.ImportJob {
	t.Helper()
	req, err := http.NewRequest("POST", base+"/api/v1/imports?format="+format, bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Origin", base)
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != want {
		t.Fatalf("import status %d != %d: %s", resp.StatusCode, want, body)
	}
	if want != 202 {
		return journal.ImportJob{}
	}
	return decodeAs[journal.ImportJob](t, response{body: body})
}

func TestDurableImportBackupAndIsolation(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, aliceUser := registerBrowser(t, base, "backup@example.test")
	bob, _ := registerBrowser(t, base, "restore@example.test")
	ctx := context.Background()
	service := journal.New(pool)
	e := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "完整备份", "total_episodes": 12, "score": 8.6}, 201))
	path := "/api/v1/entries/" + e.ID
	data := coverFixture(t, "image/png")
	uploadCover(t, alice, base, path+"/cover?version=1", "image/png", data, 200)
	request(t, alice, base, "POST", path+"/history", journal.RecordInput{WatchedOn: "2026-10-07", EpisodeFrom: 1, EpisodeTo: 4, Note: "原始观看记录", Rewatch: 2, RequestID: id.New()}, 201)
	if _, err := service.ApplySource(ctx, aliceUser.ID, e.ID, 3, journal.SourceMetadata{SubjectID: 101, Title: "完整备份", Format: "tv", TotalEpisodes: 12}, nil, nil); err != nil {
		t.Fatal(err)
	}
	backup := request(t, alice, base, "GET", "/api/v1/backup", nil, 200)
	if backup.header.Get("Content-Type") != "application/zip" {
		t.Fatal("backup MIME missing")
	}
	job := importFile(t, bob, base, "zip", backup.body, 202)
	request(t, alice, base, "GET", "/api/v1/imports/"+job.ID, nil, 404)
	request(t, alice, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "cancel"}, 404)
	importFile(t, bob, base, "csv", []byte("title\nsecond\n"), 409)
	// Emulate a disconnected worker holding the claim. A fresh service can
	// resume as soon as PostgreSQL releases the dead connection's advisory lock.
	conn, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = conn.Exec(ctx, `SELECT pg_advisory_lock(hashtextextended($1,718226045))`, job.ID); err != nil {
		t.Fatal(err)
	}
	if worked, err := service.ProcessNextImport(ctx); err != nil || worked {
		t.Fatalf("competing worker took locked task: %v %v", worked, err)
	}
	if err = conn.Conn().Close(ctx); err != nil {
		t.Fatal(err)
	}
	conn.Release()
	if worked, err := journal.New(pool).ProcessNextImport(ctx); err != nil || !worked {
		t.Fatalf("resume validation: %v %v", worked, err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, bob, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.State != "ready" || job.Preview.Ready != 1 || job.Preview.Covers != 1 || job.Preview.Records != 1 {
		t.Fatalf("bad preview: %+v", job)
	}
	request(t, bob, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "apply"}, 200)
	var wg sync.WaitGroup
	errs := make([]error, 2)
	for i := range 2 {
		wg.Go(func() { _, errs[i] = journal.New(pool).ProcessNextImport(ctx) })
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	job = decodeAs[journal.ImportJob](t, request(t, bob, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "apply"}, 200))
	if job.State != "done" || job.Created != 1 {
		t.Fatalf("apply not idempotent: %+v", job)
	}
	page := decodeAs[journal.Page](t, request(t, bob, base, "GET", "/api/v1/entries", nil, 200))
	if page.Total != 1 || page.Items[0].ID == e.ID || page.Items[0].WatchedEpisodes != 4 || *page.Items[0].Score != 8.6 {
		t.Fatal("restored entry/owner identity incorrect")
	}
	restored := page.Items[0]
	if restored.Source == nil || restored.Source.SubjectID != 101 || restored.Source.Provider != "bangumi" {
		t.Fatal("backup lost stable source binding")
	}
	picture := request(t, bob, base, "GET", "/api/v1/entries/"+restored.ID+"/cover/"+*restored.CoverRevision, nil, 200)
	if !bytes.Equal(picture.body, data) {
		t.Fatal("backup image bytes changed")
	}
	history := decodeAs[journal.HistoryPage](t, request(t, bob, base, "GET", "/api/v1/history/page", nil, 200))
	if history.Total != 1 || history.Items[0].Rewatch != 2 || history.Items[0].Note != "原始观看记录" {
		t.Fatal("history restore lost fields")
	}
	request(t, bob, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "cancel"}, 409)
	job = importFile(t, bob, base, "zip", backup.body, 202)
	if _, err = service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, bob, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.State != "failed" || job.Error == "" {
		t.Fatal("complete memory restore must reject occupied journals, not drop duplicate history")
	}
}

func TestImportCancellationConflictAndAtomicFailure(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	client, user := registerBrowser(t, base, "import@example.test")
	service := journal.New(pool)
	ctx := context.Background()
	job := importFile(t, client, base, "csv", []byte("title,total_episodes,score\nfirst,12,9.2\nfirst,12,9.2\n"), 202)
	request(t, client, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "cancel"}, 200)
	if _, err := service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = importFile(t, client, base, "csv", []byte("title\nfirst\nfirst\n"), 202)
	if _, err := service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, client, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.Preview.Ready != 1 || job.Preview.Duplicates != 1 {
		t.Fatal("in-file duplicate handling failed")
	}
	request(t, client, base, "POST", "/api/v1/entries", map[string]string{"title": "预览后的新记录"}, 201)
	request(t, client, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "apply"}, 200)
	if _, err := service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, client, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.State != "failed" || job.Error == "" {
		t.Fatal("stale preview was applied")
	}
	_, err := pool.Exec(ctx, `CREATE FUNCTION reject_import_fixture() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.title='rollback target' THEN RAISE EXCEPTION 'synthetic insertion failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER import_failure BEFORE INSERT ON entries FOR EACH ROW EXECUTE FUNCTION reject_import_fixture()`)
	if err != nil {
		t.Fatal(err)
	}
	job = importFile(t, client, base, "csv", []byte("title\nwould be partial\nrollback target\n"), 202)
	if _, err = service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	request(t, client, base, "POST", "/api/v1/imports/"+job.ID, map[string]string{"action": "apply"}, 200)
	if _, err = service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, client, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.State != "failed" || job.Created != 0 {
		t.Fatalf("failed task receipt incorrect: %+v", job)
	}
	var count int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM entries WHERE user_id=$1`, user.ID).Scan(&count); err != nil || count != 1 {
		t.Fatalf("partial import persisted: %d %v", count, err)
	}
	job = importFile(t, client, base, "csv", []byte("title,score\ninvalid,NaN\n"), 202)
	if _, err = service.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	job = decodeAs[journal.ImportJob](t, request(t, client, base, "GET", "/api/v1/imports/"+job.ID, nil, 200))
	if job.State != "failed" {
		t.Fatal("invalid CSV accepted")
	}
}
