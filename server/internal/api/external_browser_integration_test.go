//go:build integration

package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/external"
	"animemo.local/server/internal/mailer"
	"animemo.local/server/internal/mediastore"
)

// Opt-in interactive fixture: isolated PostgreSQL schema, real API and built UI,
// synthetic mail/OAuth/S3, and optional live anonymous Bangumi metadata only.
func TestExternalBrowserFixture(t *testing.T) {
	directory := os.Getenv("ANIMEMO_BROWSER_FIXTURE")
	if directory == "" {
		t.Skip("opt-in browser fixture")
	}
	if !filepath.IsAbs(directory) {
		t.Fatal("fixture output must be an absolute private directory")
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		t.Fatal(err)
	}
	pool := isolatedDatabase(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	fake, _ := newFakeBangumi(t, 2)
	upstream := httptest.NewServer(http.HandlerFunc(fake.serve))
	defer upstream.Close()
	target, _ := url.Parse(upstream.URL)
	provider := bangumi.New(providerTransport(func(r *http.Request) (*http.Response, error) {
		if r.Header.Get("Authorization") == "" && r.URL.Host != "bgm.tv" {
			return http.DefaultTransport.RoundTrip(r)
		}
		copy := r.Clone(r.Context())
		copy.URL.Host = target.Host
		copy.URL.Scheme = target.Scheme
		return http.DefaultTransport.RoundTrip(copy)
	}))
	var mailMu sync.Mutex
	mail, err := mailer.New("synthetic-mail-key", "AniMemo <test@example.test>", providerTransport(func(r *http.Request) (*http.Response, error) {
		var message mailer.Message
		if err := json.NewDecoder(r.Body).Decode(&message); err != nil {
			return nil, err
		}
		mailMu.Lock()
		defer mailMu.Unlock()
		data, _ := json.Marshal(message)
		if err := os.WriteFile(filepath.Join(directory, "mail.json"), data, 0600); err != nil {
			return nil, err
		}
		return &http.Response{StatusCode: 200, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{"id":"synthetic-delivery"}`))}, nil
	}))
	if err != nil {
		t.Fatal(err)
	}
	objectTransport := &fakeR2{t: t, objects: map[string][]byte{}}
	r2, err := mediastore.NewR2(mediastore.R2Config{Endpoint: "https://" + strings.Repeat("a", 32) + ".r2.cloudflarestorage.com", Bucket: "isolated-browser", AccessKeyID: "synthetic-key", SecretAccessKey: "synthetic-secret"}, objectTransport)
	if err != nil {
		t.Fatal(err)
	}
	storage := mediastore.New(pool, r2)
	key := bytes.Repeat([]byte{31}, 32)
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	service := external.New(pool, provider)
	if err = service.ConfigureOAuth(bangumi.OAuthConfig{ClientID: "synthetic-client", ClientSecret: "synthetic-secret"}, key, base); err != nil {
		t.Fatal(err)
	}
	mailWorker := accounts.New(pool)
	if err = mailWorker.ConfigureEncryption(key); err != nil {
		t.Fatal(err)
	}
	if err = mailWorker.ConfigureMail(mail, base); err != nil {
		t.Fatal(err)
	}
	server.Config.Handler = New(pool, Config{PublicOrigin: base, WebDir: os.Getenv("ANIMEMO_BROWSER_WEB"), SetupToken: testSetupToken, SecretKey: key, Mail: mail, External: service, Storage: storage})
	server.Start()
	defer server.Close()
	setupAdmin(t, base)
	var workers sync.WaitGroup
	for _, run := range []func(context.Context){mailWorker.RunMailWorker, service.RunSyncWorker, storage.Run} {
		workers.Add(1)
		go func() { defer workers.Done(); run(ctx) }()
	}
	defer func() { cancel(); workers.Wait() }()
	data, _ := json.Marshal(map[string]string{"base": base, "admin_email": "admin@example.test", "admin_password": "admin-test-passphrase"})
	if err = os.WriteFile(filepath.Join(directory, "ready.json"), data, 0600); err != nil {
		t.Fatal(err)
	}
	timer := time.NewTimer(20 * time.Minute)
	defer timer.Stop()
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-timer.C:
			t.Fatal("browser fixture timed out")
		case <-tick.C:
			if _, err = os.Stat(filepath.Join(directory, "stop")); err == nil {
				return
			}
		}
	}
}
