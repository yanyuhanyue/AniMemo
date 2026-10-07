//go:build integration

package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"image"
	"image/jpeg"
	"image/png"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/database"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"
)

func coverServer(t *testing.T, pool *pgxpool.Pool) string {
	t.Helper()
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base})
	server.Start()
	t.Cleanup(server.Close)
	return base
}

func coverFixture(t *testing.T, kind string) []byte {
	t.Helper()
	var data bytes.Buffer
	picture := image.NewRGBA(image.Rect(0, 0, 4, 6))
	var err error
	if kind == "image/jpeg" {
		err = jpeg.Encode(&data, picture, nil)
	} else {
		err = png.Encode(&data, picture)
	}
	if err != nil {
		t.Fatal(err)
	}
	return data.Bytes()
}

func sendCover(client *http.Client, base, path, kind string, data []byte, origin string) (response, error) {
	req, err := http.NewRequest(http.MethodPut, base+path, bytes.NewReader(data))
	if err != nil {
		return response{}, err
	}
	req.Header.Set("Content-Type", kind)
	req.Header.Set("Origin", origin)
	resp, err := client.Do(req)
	if err != nil {
		return response{}, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return response{status: resp.StatusCode, body: body, header: resp.Header}, err
}

func uploadCover(t *testing.T, client *http.Client, base, path, kind string, data []byte, want int) response {
	t.Helper()
	r, err := sendCover(client, base, path, kind, data, base)
	if err != nil {
		t.Fatal(err)
	}
	if r.status != want {
		t.Fatalf("upload: want %d, got %d: %s", want, r.status, r.body)
	}
	return r
}

func TestPrivateCoverLifecycle(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	alice, bob := browser(), browser()
	for name, client := range map[string]*http.Client{"alice": alice, "bob": bob} {
		request(t, client, base, "POST", "/api/v1/auth/register", map[string]string{"email": name + "@example.test", "display_name": name, "password": "test-cover-passphrase"}, 201)
	}
	e := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]string{"title": "私有封面"}, 201))
	path := "/api/v1/entries/" + e.ID
	pngData, jpegData := coverFixture(t, "image/png"), coverFixture(t, "image/jpeg")
	upload := path + "/cover?version=1"
	uploadCover(t, browser(), base, upload, "image/png", pngData, 401)
	uploadCover(t, bob, base, upload, "image/png", pngData, 404)
	r, err := sendCover(alice, base, upload, "image/png", pngData, "https://elsewhere.invalid")
	if err != nil || r.status != 403 {
		t.Fatalf("origin: %d %v", r.status, err)
	}
	for _, version := range []string{"", "0", "oops"} {
		uploadCover(t, alice, base, path+"/cover?version="+version, "image/png", pngData, 400)
	}
	e = decodeAs[journal.Entry](t, uploadCover(t, alice, base, upload, "image/png", pngData, 200))
	if e.Version != 2 || e.CoverRevision == nil {
		t.Fatalf("cover did not update entry: %+v", e)
	}
	imagePath := path + "/cover/" + *e.CoverRevision
	imageResponse := request(t, alice, base, "GET", imagePath, nil, 200)
	if !bytes.Equal(imageResponse.body, pngData) || imageResponse.header.Get("Content-Type") != "image/png" || imageResponse.header.Get("Cache-Control") != "private, no-store" || imageResponse.header.Get("X-Content-Type-Options") != "nosniff" || imageResponse.header.Get("Cross-Origin-Resource-Policy") != "same-origin" {
		t.Fatal("image bytes or privacy headers missing")
	}
	request(t, browser(), base, "GET", imagePath, nil, 401)
	request(t, bob, base, "GET", imagePath, nil, 404)
	request(t, bob, base, "DELETE", path+"/cover?version=2", nil, 404)
	request(t, alice, base, "GET", path+"/cover/invalid", nil, 404)
	request(t, alice, base, "GET", path+"/cover/"+id.New(), nil, 404)
	for _, invalid := range []struct {
		kind   string
		data   []byte
		status int
	}{
		{"image/png", pngData[:40], 400},
		{"image/png", jpegData, 400},
		{"image/svg+xml", []byte("<svg/>"), 415},
		{"image/png", make([]byte, media.MaxBytes+1), 413},
	} {
		uploadCover(t, alice, base, path+"/cover?version=2", invalid.kind, invalid.data, invalid.status)
	}
	uploadCover(t, alice, base, upload, "image/jpeg", jpegData, 409)
	request(t, alice, base, "DELETE", path+"/cover?version=1", nil, 409)
	unchanged := decodeAs[journal.Entry](t, request(t, alice, base, "GET", path, nil, 200))
	if unchanged.Version != 2 || *unchanged.CoverRevision != *e.CoverRevision {
		t.Fatal("failed replacement changed cover or version")
	}
	if !bytes.Equal(request(t, alice, base, "GET", imagePath, nil, 200).body, pngData) {
		t.Fatal("failed replacement changed bytes")
	}
	page := decodeAs[journal.Page](t, request(t, alice, base, "GET", "/api/v1/entries", nil, 200))
	export := decodeAs[journal.Export](t, request(t, alice, base, "GET", "/api/v1/export", nil, 200))
	if *page.Items[0].CoverRevision != *e.CoverRevision || *export.Entries[0].CoverRevision != *e.CoverRevision {
		t.Fatal("cover metadata absent from list/export")
	}
	e = decodeAs[journal.Entry](t, uploadCover(t, alice, base, path+"/cover?version=2", "image/jpeg", jpegData, 200))
	request(t, alice, base, "GET", imagePath, nil, 404)
	imagePath = path + "/cover/" + *e.CoverRevision
	if !bytes.Equal(request(t, alice, base, "GET", imagePath, nil, 200).body, jpegData) {
		t.Fatal("JPEG replacement lost")
	}
	e = decodeAs[journal.Entry](t, request(t, alice, base, "DELETE", path+"/cover?version=3", nil, 200))
	if e.Version != 4 || e.CoverRevision != nil {
		t.Fatal("removal metadata invalid")
	}
	request(t, alice, base, "GET", imagePath, nil, 404)
	noOp := decodeAs[journal.Entry](t, request(t, alice, base, "DELETE", path+"/cover?version=4", nil, 200))
	if noOp.Version != 4 {
		t.Fatal("absent-cover removal is not a no-op")
	}
	e = decodeAs[journal.Entry](t, uploadCover(t, alice, base, path+"/cover?version=4", "image/png", pngData, 200))
	imagePath = path + "/cover/" + *e.CoverRevision
	request(t, alice, base, "POST", "/api/v1/auth/logout", nil, 204)
	request(t, alice, base, "GET", imagePath, nil, 401)
	request(t, alice, base, "POST", "/api/v1/auth/login", map[string]string{"email": "alice@example.test", "password": "test-cover-passphrase"}, 200)
	request(t, alice, base, "DELETE", path+"?version=5", nil, 204)
	request(t, alice, base, "GET", imagePath, nil, 404)
	var count int
	if err := pool.QueryRow(context.Background(), `SELECT count(*) FROM entry_covers`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("cover did not cascade: %d %v", count, err)
	}
}

func TestConcurrentCoverVersionAndQuota(t *testing.T) {
	pool := isolatedDatabase(t)
	// Separate HTTP handlers have separate decode limits; quota/version locks
	// must still work through PostgreSQL rather than process-local mutexes.
	bases := []string{coverServer(t, pool), coverServer(t, pool)}
	client := browser()
	user := decodeAs[accounts.User](t, request(t, client, bases[0], "POST", "/api/v1/auth/register", map[string]string{"email": "quota@example.test", "display_name": "quota", "password": "test-cover-passphrase"}, 201))
	data := coverFixture(t, "image/png")
	create := func() journal.Entry {
		return decodeAs[journal.Entry](t, request(t, client, bases[0], "POST", "/api/v1/entries", map[string]string{"title": "并发封面"}, 201))
	}
	concurrent := func(paths []string, wantA, wantB int) []response {
		t.Helper()
		var wg sync.WaitGroup
		start := make(chan struct{})
		results := make([]response, 2)
		errors := make([]error, 2)
		for i := range 2 {
			wg.Go(func() {
				<-start
				results[i], errors[i] = sendCover(client, bases[i], paths[i], "image/png", data, bases[i])
			})
		}
		close(start)
		wg.Wait()
		counts := map[int]int{}
		for i, r := range results {
			if errors[i] != nil {
				t.Fatal(errors[i])
			}
			counts[r.status]++
		}
		if counts[wantA] != 1 || counts[wantB] != 1 {
			t.Fatalf("want %d/%d, got %v: %s / %s", wantA, wantB, counts, results[0].body, results[1].body)
		}
		return results
	}
	e := create()
	path := "/api/v1/entries/" + e.ID + "/cover?version=1"
	concurrent([]string{path, path}, 200, 409)
	request(t, client, bases[0], "DELETE", "/api/v1/entries/"+e.ID+"?version=2", nil, 204)
	// Fill the owner's quota to leave room for precisely one small image.
	// PostgreSQL compresses these synthetic bytes; no large test download needed.
	ctx := context.Background()
	for i := range 50 {
		entryID := id.New()
		if _, err := pool.Exec(ctx, `INSERT INTO entries (id,user_id,title) VALUES ($1,$2,'quota fixture')`, entryID, user.ID); err != nil {
			t.Fatal(err)
		}
		size := media.MaxBytes
		if i == 49 {
			size -= len(data)
		}
		if _, err := pool.Exec(ctx, `INSERT INTO entry_covers (entry_id,revision,content_type,width,height,byte_size,data) VALUES ($1,$2,'image/png',1,1,$3,decode(repeat('00',$3),'hex'))`, entryID, id.New(), size); err != nil {
			t.Fatal(err)
		}
	}
	a, b := create(), create()
	results := concurrent([]string{"/api/v1/entries/" + a.ID + "/cover?version=1", "/api/v1/entries/" + b.ID + "/cover?version=1"}, 200, 413)
	var winner journal.Entry
	for _, r := range results {
		if r.status == 200 {
			winner = decodeAs[journal.Entry](t, r)
		}
	}
	var used int64
	if err := pool.QueryRow(ctx, `SELECT sum(byte_size) FROM entry_covers`).Scan(&used); err != nil || used != journal.CoverQuotaBytes {
		t.Fatalf("quota violated: %d %v", used, err)
	}
	// Replacing subtracts the existing cover from quota; rejection is atomic.
	winnerPath := "/api/v1/entries/" + winner.ID + "/cover?version=2"
	uploadCover(t, client, bases[0], winnerPath, "image/jpeg", coverFixture(t, "image/jpeg"), 413)
	winner = decodeAs[journal.Entry](t, uploadCover(t, client, bases[0], winnerPath, "image/png", data, 200))
	request(t, client, bases[0], "DELETE", fmt.Sprintf("/api/v1/entries/%s/cover?version=%d", winner.ID, winner.Version), nil, 200)
	loser := a
	if winner.ID == a.ID {
		loser = b
	}
	uploadCover(t, client, bases[0], "/api/v1/entries/"+loser.ID+"/cover?version=1", "image/png", data, 200)
}

func TestCoverMigrationPreservesExistingJournal(t *testing.T) {
	pool := isolatedSchema(t)
	ctx := context.Background()
	initial, err := os.ReadFile("../database/migrations/001_initial.sql")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, string(initial)); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `CREATE TABLE schema_migrations(name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now());`); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `INSERT INTO schema_migrations(name,checksum) VALUES('migrations/001_initial.sql',$1)`, fmt.Sprintf("%x", sha256.Sum256(initial))); err != nil {
		t.Fatal(err)
	}
	userID, entryID, recordID := id.New(), id.New(), id.New()
	hash, _ := bcrypt.GenerateFromPassword([]byte("test-cover-passphrase"), bcrypt.DefaultCost)
	if _, err = pool.Exec(ctx, `INSERT INTO users(id,email,display_name,password_hash) VALUES($1,'upgrade@example.test','upgrade',$2)`, userID, string(hash)); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `INSERT INTO entries(id,user_id,title,watched_episodes,version) VALUES($1,$2,'升级前的手账',2,2)`, entryID, userID); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, `INSERT INTO watch_records(id,entry_id,watched_on,episode_from,episode_to,request_id) VALUES($1,$2,'2026-10-07',1,2,$3)`, recordID, entryID, id.New()); err != nil {
		t.Fatal(err)
	}
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	base, client := coverServer(t, pool), browser()
	request(t, client, base, "POST", "/api/v1/auth/login", map[string]string{"email": "upgrade@example.test", "password": "test-cover-passphrase"}, 200)
	path := "/api/v1/entries/" + entryID
	e := decodeAs[journal.Entry](t, request(t, client, base, "GET", path, nil, 200))
	if e.CoverRevision != nil || e.Version != 2 || e.WatchedEpisodes != 2 || e.Title != "升级前的手账" {
		t.Fatalf("upgrade changed existing entry: %+v", e)
	}
	history := decodeAs[struct {
		Items []journal.Record `json:"items"`
	}](t, request(t, client, base, "GET", path+"/history", nil, 200))
	if len(history.Items) != 1 || history.Items[0].ID != recordID {
		t.Fatal("upgrade lost existing history")
	}
	uploadCover(t, client, base, path+"/cover?version=2", "image/png", coverFixture(t, "image/png"), 200)
}

func TestCoverUploadCapacityRejectsBeforeReading(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	session, err := accounts.New(pool).Register(ctx, accounts.Registration{Email: "busy@example.test", DisplayName: "busy", Password: "test-cover-passphrase"})
	if err != nil {
		t.Fatal(err)
	}
	service := journal.New(pool)
	e, err := service.Create(ctx, session.User.ID, journal.Create{Title: "上传容量"})
	if err != nil {
		t.Fatal(err)
	}
	a := &API{journal: service, coverSlots: make(chan struct{}, 2)}
	a.coverSlots <- struct{}{}
	a.coverSlots <- struct{}{}
	req := httptest.NewRequest("PUT", "/?version=1", nil).WithContext(context.WithValue(ctx, userKey{}, session.User))
	req.SetPathValue("id", e.ID)
	req.Body = unreadCoverBody{t}
	w := httptest.NewRecorder()
	a.setCover(w, req)
	if w.Code != 503 || w.Header().Get("Retry-After") != "2" {
		t.Fatalf("unexpected capacity response: %d %s", w.Code, w.Body.String())
	}
	if len(a.coverSlots) != 2 {
		t.Fatal("busy request released another upload's slot")
	}
}

type unreadCoverBody struct{ t *testing.T }

func (b unreadCoverBody) Read([]byte) (int, error) {
	b.t.Error("busy upload consumed its body")
	return 0, io.EOF
}
func (b unreadCoverBody) Close() error { return nil }
