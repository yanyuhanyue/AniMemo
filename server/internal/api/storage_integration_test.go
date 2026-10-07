//go:build integration

package api

import (
	"archive/zip"
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/mediastore"
)

type fakeR2 struct {
	t             *testing.T
	mu            sync.Mutex
	objects       map[string][]byte
	fail, corrupt bool
	puts, deletes int
}

func (f *fakeR2) RoundTrip(r *http.Request) (*http.Response, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.URL.Scheme != "https" || r.URL.Host != strings.Repeat("a", 32)+".r2.cloudflarestorage.com" || !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 ") || !strings.Contains(r.Header.Get("Authorization"), "/auto/s3/aws4_request") {
		f.t.Error("missing fixed endpoint or SigV4 signing")
	}
	status := 200
	data := []byte{}
	if f.fail {
		status = 403
		data = []byte(`<Error><Code>AccessDenied</Code></Error>`)
	} else {
		switch r.Method {
		case "PUT":
			if r.Header.Get("If-None-Match") != "*" || r.Header.Get("Cache-Control") != "private, no-store" {
				f.t.Error("immutable private upload headers missing")
			}
			if f.objects[r.URL.Path] != nil {
				status = 412
				data = []byte(`<Error><Code>PreconditionFailed</Code></Error>`)
			} else {
				body, err := io.ReadAll(r.Body)
				if err != nil {
					return nil, err
				}
				f.objects[r.URL.Path] = body
				f.puts++
			}
		case "GET":
			var ok bool
			data, ok = f.objects[r.URL.Path]
			if !ok {
				status = 404
				data = []byte(`<Error><Code>NoSuchKey</Code></Error>`)
			} else if f.corrupt {
				data = []byte("corrupt")
			}
		case "DELETE":
			delete(f.objects, r.URL.Path)
			f.deletes++
			status = 204
		default:
			f.t.Errorf("unexpected S3 method %s", r.Method)
		}
	}
	return &http.Response{StatusCode: status, Header: http.Header{"Content-Type": []string{"application/octet-stream"}}, ContentLength: int64(len(data)), Body: io.NopCloser(bytes.NewReader(data)), Request: r}, nil
}
func TestR2MigrationPrivacyRecoveryAndPortableBackup(t *testing.T) {
	ctx := context.Background()
	pool := isolatedDatabase(t)
	fake := &fakeR2{t: t, objects: map[string][]byte{}}
	r2, err := mediastore.NewR2(mediastore.R2Config{Endpoint: "https://" + strings.Repeat("a", 32) + ".r2.cloudflarestorage.com", Bucket: "isolated-test", AccessKeyID: "test-key", SecretAccessKey: "test-secret"}, fake)
	if err != nil {
		t.Fatal(err)
	}
	storage := mediastore.New(pool, r2)
	if err = storage.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base, SetupToken: testSetupToken, Storage: storage})
	server.Start()
	defer server.Close()
	admin, _ := setupAdmin(t, base)
	alice, user := registerBrowser(t, base, "media@example.test")
	bob, _ := registerBrowser(t, base, "other@example.test")
	guest := browser()
	statusPath := "/api/v1/admin/media/storage"
	request(t, alice, base, "GET", statusPath, nil, 403)
	request(t, admin, base, "POST", statusPath+"/probe", nil, 200)
	if len(fake.objects) != 0 || fake.deletes != 1 {
		t.Fatal("probe object leaked")
	}
	picture := coverFixture(t, "image/png")
	entry := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]string{"title": "R2 image", "visibility": "unlisted"}, 201))
	entry = decodeAs[journal.Entry](t, uploadCover(t, alice, base, "/api/v1/entries/"+entry.ID+"/cover?version=1", "image/png", picture, 200))
	user = decodeAs[accounts.User](t, uploadCover(t, alice, base, "/api/v1/avatar?version=1", "image/png", picture, 200))
	column := decodeAs[journal.Column](t, request(t, alice, base, "POST", "/api/v1/columns", journal.ColumnInput{Title: "R2 column", Summary: "图片", Body: "正文", EntryIDs: []string{entry.ID}}, 201))
	column = decodeAs[journal.Column](t, uploadCover(t, alice, base, "/api/v1/columns/"+column.ID+"/cover?version="+strconv.Itoa(column.Version), "image/png", picture, 200))
	status := func() mediastore.Status {
		return decodeAs[mediastore.Status](t, request(t, admin, base, "GET", statusPath, nil, 200))
	}
	change := func(backend string) {
		s := status()
		request(t, admin, base, "PUT", statusPath, map[string]any{"backend": backend, "version": s.Version}, 200)
	}
	drain := func() {
		t.Helper()
		for range 20 {
			worked, err := storage.ProcessNext(ctx)
			if err != nil {
				t.Fatal(err)
			}
			if !worked {
				return
			}
		}
		t.Fatal("worker did not settle")
	}
	paths := []string{"/api/v1/entries/" + entry.ID + "/cover/" + *entry.CoverRevision, "/api/v1/avatar/" + *user.AvatarRevision, "/api/v1/columns/" + column.ID + "/cover/" + *column.CoverRevision}
	change("r2")
	fake.fail = true
	drain()
	if s := status(); s.PostgresImages != 3 || s.RemoteImages != 0 || s.Failures != 3 {
		t.Fatalf("failed upload lost staging: %+v", s)
	}
	for _, path := range paths {
		if !bytes.Equal(request(t, alice, base, "GET", path, nil, 200).body, picture) {
			t.Fatal("staging image unreadable")
		}
	}
	fake.fail = false
	if _, err = pool.Exec(ctx, `UPDATE media_objects SET available_at=now()`); err != nil {
		t.Fatal(err)
	}
	drain()
	s := status()
	if s.RemoteImages != 3 || s.Pending != 0 || s.OriginalsBytes != int64(3*len(picture)) || s.MediaBytes != int64(3*len(picture)) {
		t.Fatalf("migration state/quota incorrect: %+v", s)
	}
	request(t, admin, base, "GET", "/api/v1/admin/media/migrations/"+s.Migrations[0].ID, nil, 200)
	for _, path := range paths {
		image := request(t, alice, base, "GET", path, nil, 200)
		if !bytes.Equal(image.body, picture) || !strings.Contains(image.header.Get("Cache-Control"), "no-store") {
			t.Fatal("remote read privacy or bytes changed")
		}
		request(t, bob, base, "GET", path, nil, 404)
		request(t, guest, base, "GET", path, nil, 401)
	}
	user = decodeAs[accounts.User](t, request(t, alice, base, "POST", "/api/v1/settings/publication", map[string]any{"action": "enable-sharing", "version": user.Version}, 200))
	shared := "/api/v1/public/shared/" + entry.ShareSlug + "/cover/" + *entry.CoverRevision
	request(t, guest, base, "GET", shared, nil, 200)
	request(t, alice, base, "POST", "/api/v1/settings/publication", map[string]any{"action": "disable-sharing", "version": user.Version}, 200)
	request(t, guest, base, "GET", shared, nil, 404)
	backup := request(t, alice, base, "GET", "/api/v1/backup", nil, 200).body
	zipfile, err := zip.NewReader(bytes.NewReader(backup), int64(len(backup)))
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, file := range zipfile.File {
		if strings.HasPrefix(file.Name, "covers/") {
			r, _ := file.Open()
			data, _ := io.ReadAll(r)
			r.Close()
			found = bytes.Equal(data, picture)
		}
	}
	if !found {
		t.Fatal("journal backup lost R2 cover")
	}
	// Returning to PostgreSQL works even when R2 is unavailable, using retained originals.
	fake.fail = true
	change("postgres")
	drain()
	if status().PostgresImages != 3 {
		t.Fatal("rollback originals not restored")
	}
	fake.fail = false
	if _, err = pool.Exec(ctx, `UPDATE media_objects SET available_at=now(),updated_at=now()-interval '3 minutes'`); err != nil {
		t.Fatal(err)
	}
	drain()
	if len(fake.objects) != 0 {
		t.Fatal("rollback orphan objects not collected")
	}
	change("r2")
	drain()
	// A replacement after migration has no retained original; corrupted remote bytes must fail closed.
	entry = decodeAs[journal.Entry](t, uploadCover(t, alice, base, "/api/v1/entries/"+entry.ID+"/cover?version=2", "image/png", picture, 200))
	drain()
	replacement := "/api/v1/entries/" + entry.ID + "/cover/" + *entry.CoverRevision
	fake.corrupt = true
	request(t, alice, base, "GET", replacement, nil, 503)
	fake.corrupt = false
	request(t, alice, base, "GET", paths[0], nil, 404)
	// Cleanup failure survives restart, then succeeds without touching live images.
	if _, err = pool.Exec(ctx, `UPDATE media_objects SET available_at=now(),updated_at=now()-interval '3 minutes' WHERE state='orphan'`); err != nil {
		t.Fatal(err)
	}
	fake.fail = true
	drain()
	if status().CleanupPending != 1 {
		t.Fatal("failed delete lost cleanup job")
	}
	fake.fail = false
	if _, err = pool.Exec(ctx, `UPDATE media_objects SET available_at=now() WHERE state='orphan'`); err != nil {
		t.Fatal(err)
	}
	storage = mediastore.New(pool, r2)
	drain()
	if len(fake.objects) != 3 {
		t.Fatal("cleanup deleted live object or retained old revision")
	}
	var archive bytes.Buffer
	if err = storage.Export(ctx, &archive); err != nil {
		t.Fatal(err)
	}
	var invalid bytes.Buffer
	z := zip.NewWriter(&invalid)
	f, _ := z.Create("manifest.json")
	f.Write([]byte(`{"schema":"animemo.media/v1","items":[]}`))
	z.Close()
	offline := mediastore.New(pool, nil)
	if offline.Initialize(ctx) == nil {
		t.Fatal("remote-only data started without required credentials")
	}
	if offline.Restore(ctx, bytes.NewReader(invalid.Bytes()), int64(invalid.Len())) == nil {
		t.Fatal("incomplete archive accepted")
	}
	if status().RemoteImages != 3 {
		t.Fatal("failed restore partially changed references")
	}
	fake.fail = true
	remoteCount := len(fake.objects)
	if err = offline.Restore(ctx, bytes.NewReader(archive.Bytes()), int64(archive.Len())); err != nil {
		t.Fatal(err)
	}
	if err = offline.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	if status().PostgresImages != 3 || status().Backend != "postgres" || len(fake.objects) != remoteCount {
		t.Fatal("offline restore lost images or touched source bucket")
	}
	for _, path := range []string{replacement, paths[1], paths[2]} {
		if !bytes.Equal(request(t, alice, base, "GET", path, nil, 200).body, picture) {
			t.Fatal("portable restore image differs")
		}
	}
	var objects int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM media_objects`).Scan(&objects); err != nil || objects != 0 {
		t.Fatal("clone retained source cleanup authority")
	}
}
