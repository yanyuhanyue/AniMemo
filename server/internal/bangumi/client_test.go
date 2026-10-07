package bangumi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func response(status int, data string) *http.Response {
	return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(data)), Header: http.Header{}}
}

func TestProviderBoundariesAndBackoff(t *testing.T) {
	for _, address := range []string{"http://lain.bgm.tv/pic/cover/a.jpg", "https://127.0.0.1/pic/cover/a.jpg", "https://lain.bgm.tv.evil.test/pic/cover/a.jpg", "https://u:p@lain.bgm.tv/pic/cover/a.jpg", "https://lain.bgm.tv:8443/pic/cover/a.jpg", "https://lain.bgm.tv/api/secret", "https://lain.bgm.tv/pic/cover/a.jpg#secret"} {
		if ValidImageURL(address) {
			t.Fatalf("accepted unsafe cover address %q", address)
		}
	}
	if !ValidImageURL("https://lain.bgm.tv/pic/cover/l/a.jpg") {
		t.Fatal("rejected documented image origin")
	}
	calls := 0
	client := New(roundTrip(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.URL.Host != "api.bgm.tv" || r.Header.Get("User-Agent") != UserAgent {
			t.Fatal("wrong origin or missing User-Agent")
		}
		if r.Header.Get("Authorization") != "" {
			t.Fatal("anonymous metadata leaked credentials")
		}
		out := response(429, "private upstream error body")
		out.Header.Set("Retry-After", "30")
		return out, nil
	}))
	client.interval = 0
	_, err := client.Search(context.Background(), "中文作品", 1)
	var upstream *UpstreamError
	if !errors.As(err, &upstream) || upstream.Status != 429 || upstream.RetryAfter != 30*time.Second || strings.Contains(err.Error(), "private") {
		t.Fatalf("unexpected rate limit: %v", err)
	}
	_, err = client.Search(context.Background(), "中文作品", 1)
	if !errors.As(err, &upstream) || calls != 1 {
		t.Fatal("Retry-After did not prevent an early retry")
	}
	client = New(roundTrip(func(r *http.Request) (*http.Response, error) {
		out := response(302, "")
		out.Header.Set("Location", "http://127.0.0.1/private")
		return out, nil
	}))
	client.interval = 0
	if _, err = client.Subject(context.Background(), 1); err == nil {
		t.Fatal("redirect accepted")
	}
}

func TestProviderResponseLimitsAndCancellation(t *testing.T) {
	for _, data := range []string{`{"id":2,"type":2}`, `{"id":1,"type":1}`, `not json`, strings.Repeat(" ", 2<<20) + `{}`} {
		client := New(roundTrip(func(*http.Request) (*http.Response, error) { return response(200, data), nil }))
		client.interval = 0
		if _, err := client.Subject(context.Background(), 1); err == nil {
			t.Fatal("invalid upstream response accepted")
		}
	}
	client := New(roundTrip(func(*http.Request) (*http.Response, error) {
		t.Fatal("cancelled request reached upstream")
		return nil, nil
	}))
	client.next = time.Now().Add(time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := client.Subject(ctx, 1); !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation: %v", err)
	}
}

func TestLiveBangumi(t *testing.T) {
	if os.Getenv("ANIMEMO_LIVE_BANGUMI") != "1" {
		t.Skip("opt-in real public API verification")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	client := New(nil)
	page, err := client.Search(ctx, "葬送的芙莉莲", 1)
	if err != nil || len(page.Data) == 0 {
		t.Fatalf("live search failed: %v", err)
	}
	subject, err := client.Subject(ctx, page.Data[0].ID)
	if err != nil {
		t.Fatal(err)
	}
	picture, err := client.Cover(ctx, subject)
	if err != nil {
		t.Fatal(err)
	}
	if picture.Width < 1 || picture.Height < 1 || len(picture.Data) == 0 {
		t.Fatal("live cover was empty")
	}
	report := map[string]any{"verified_at": time.Now().UTC(), "subject_id": subject.ID, "search_total": page.Total, "cover_bytes": len(picture.Data), "cover_width": picture.Width, "cover_height": picture.Height, "result": "passed", "scope": "public search, subject detail and validated image"}
	data, _ := json.MarshalIndent(report, "", "  ")
	if path := os.Getenv("ANIMEMO_LIVE_REPORT"); path != "" {
		if err = os.WriteFile(path, append(bytes.TrimSpace(data), '\n'), 0600); err != nil {
			t.Fatal(err)
		}
	}
	t.Logf("Real Bangumi search, detail and %s image passed for subject %d", picture.ContentType, subject.ID)
}
