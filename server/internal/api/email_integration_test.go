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
	"strings"
	"sync"
	"testing"
	"time"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/mailer"
)

func TestEmailVerificationOutboxAndReset(t *testing.T) {
	pool := isolatedDatabase(t)
	ctx := context.Background()
	key := bytes.Repeat([]byte{19}, 32)
	var mu sync.Mutex
	var messages []mailer.Message
	var requestKeys []string
	failFirst := true
	transport := providerTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.String() != "https://api.resend.com/emails" || r.Header.Get("Authorization") != "Bearer synthetic-mail-key" {
			t.Error("unexpected mail destination or authentication")
		}
		var message mailer.Message
		if err := json.NewDecoder(r.Body).Decode(&message); err != nil {
			t.Fatal(err)
		}
		mu.Lock()
		messages = append(messages, message)
		requestKeys = append(requestKeys, r.Header.Get("Idempotency-Key"))
		fail := failFirst
		failFirst = false
		mu.Unlock()
		if fail {
			return &http.Response{StatusCode: 503, Body: io.NopCloser(strings.NewReader("sensitive upstream diagnostics")), Header: http.Header{"Retry-After": []string{"1"}}}, nil
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"id":"synthetic-delivery-id"}`)), Header: http.Header{}}, nil
	})
	mailClient, err := mailer.New("synthetic-mail-key", "AniMemo <test@example.test>", transport)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(nil)
	base := "http://" + server.Listener.Addr().String()
	server.Config.Handler = New(pool, Config{PublicOrigin: base, SecretKey: key, Mail: mailClient})
	server.Start()
	defer server.Close()
	newWorker := func() *accounts.Service {
		s := accounts.New(pool)
		if err := s.ConfigureEncryption(key); err != nil {
			t.Fatal(err)
		}
		if err := s.ConfigureMail(mailClient, base); err != nil {
			t.Fatal(err)
		}
		return s
	}
	worker := newWorker()
	owner := browser()
	registration := map[string]string{"email": "email-owner@example.test", "display_name": "邮箱用户", "password": "attacker-chosen-password"}
	first := request(t, owner, base, "POST", "/api/v1/auth/register", registration, 202)
	again := request(t, owner, base, "POST", "/api/v1/auth/register", registration, 202)
	if !bytes.Equal(first.body, again.body) || len(first.cookies) > 0 || len(messages) > 0 {
		t.Fatal("registration enumerates accounts, creates a session or sends synchronously")
	}
	request(t, owner, base, "GET", "/api/v1/auth/me", nil, 401)
	var queued int
	var encrypted []byte
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM email_outbox`).Scan(&queued); err != nil || queued != 1 {
		t.Fatalf("outbox count %d: %v", queued, err)
	}
	if err = pool.QueryRow(ctx, `SELECT payload FROM email_outbox`).Scan(&encrypted); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(encrypted, []byte(base)) || json.Valid(encrypted) {
		t.Fatal("mail payload persisted in plaintext")
	}
	if worked, err := worker.ProcessNextMail(ctx); !worked || err != nil {
		t.Fatalf("first delivery: %v", err)
	}
	var state string
	if err = pool.QueryRow(ctx, `SELECT state FROM email_outbox`).Scan(&state); err != nil || state != "pending" {
		t.Fatal("transient delivery did not remain retryable")
	}
	if _, err = pool.Exec(ctx, `UPDATE email_outbox SET available_at=now()`); err != nil {
		t.Fatal(err)
	}
	worker = newWorker() // Simulate process restart before retrying.
	if worked, err := worker.ProcessNextMail(ctx); !worked || err != nil {
		t.Fatalf("retry delivery: %v", err)
	}
	if len(messages) != 2 || requestKeys[0] != requestKeys[1] || messages[0].Text != messages[1].Text {
		t.Fatal("retry changed the idempotency key or payload")
	}
	if err = pool.QueryRow(ctx, `SELECT state,payload FROM email_outbox`).Scan(&state, &encrypted); err != nil || state != "accepted" || encrypted != nil {
		t.Fatal("accepted mail retained token-bearing payload")
	}
	tokenFrom := func(message mailer.Message) string {
		for _, word := range strings.Fields(message.Text) {
			if strings.HasPrefix(word, base+"/") {
				link, err := url.Parse(word)
				if err != nil {
					t.Fatal(err)
				}
				fragment, _ := url.ParseQuery(link.Fragment)
				return fragment.Get("token")
			}
		}
		t.Fatal("missing local verification link")
		return ""
	}
	verifyToken := tokenFrom(messages[1])
	password := "verified-owner-password"
	request(t, owner, base, "POST", "/api/v1/auth/email/verify", accounts.EmailConfirmation{Token: verifyToken, Password: password}, 200)
	request(t, owner, base, "POST", "/api/v1/auth/email/verify", accounts.EmailConfirmation{Token: verifyToken, Password: password}, 400)
	request(t, owner, base, "POST", "/api/v1/auth/login", accounts.Credentials{Email: registration["email"], Password: registration["password"]}, 401)
	u := decodeAs[accounts.User](t, request(t, owner, base, "POST", "/api/v1/auth/login", accounts.Credentials{Email: registration["email"], Password: password}, 200))
	if !u.EmailVerified || u.EmailVerificationRequired {
		t.Fatal("verification did not activate the account")
	}
	device := browser()
	request(t, device, base, "POST", "/api/v1/auth/login", accounts.Credentials{Email: u.Email, Password: password}, 200)
	// Allow a fresh request while retaining the previous hour's throttling history.
	if _, err = pool.Exec(ctx, `UPDATE email_outbox SET created_at=now()-interval '2 minutes'`); err != nil {
		t.Fatal(err)
	}
	unknown := request(t, owner, base, "POST", "/api/v1/auth/email/request", map[string]string{"email": "unknown@example.test", "purpose": "reset"}, 202)
	known := request(t, owner, base, "POST", "/api/v1/auth/email/request", map[string]string{"email": u.Email, "purpose": "reset"}, 202)
	limited := request(t, owner, base, "POST", "/api/v1/auth/email/request", map[string]string{"email": u.Email, "purpose": "reset"}, 202)
	if !bytes.Equal(unknown.body, known.body) || !bytes.Equal(known.body, limited.body) {
		t.Fatal("mail request reveals account or throttle state")
	}
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM email_outbox WHERE purpose='reset'`).Scan(&queued); err != nil || queued != 1 {
		t.Fatal("repeated mail request was not throttled")
	}
	if worked, err := worker.ProcessNextMail(ctx); !worked || err != nil {
		t.Fatal(err)
	}
	resetToken := tokenFrom(messages[len(messages)-1])
	// An active second factor remains mandatory for an email reset.
	enroll, err := worker.BeginTwoFactor(ctx, u.ID, password)
	if err != nil {
		t.Fatal(err)
	}
	_, codes, err := worker.ChangeTwoFactor(ctx, u.ID, accounts.TwoFactorInput{Action: "enable", Password: password, Code: enrollmentCode(t, enroll.Secret, time.Now())})
	if err != nil {
		t.Fatal(err)
	}
	input := accounts.EmailConfirmation{Token: resetToken, Password: "new-owner-password-456"}
	request(t, owner, base, "POST", "/api/v1/auth/password/reset", input, 401)
	input.Code = codes[0]
	request(t, owner, base, "POST", "/api/v1/auth/password/reset", input, 200)
	request(t, owner, base, "POST", "/api/v1/auth/password/reset", input, 400)
	request(t, device, base, "GET", "/api/v1/auth/me", nil, 401)
	request(t, owner, base, "POST", "/api/v1/auth/login", accounts.Credentials{Email: u.Email, Password: password, Code: codes[1]}, 401)
	request(t, owner, base, "POST", "/api/v1/auth/login", accounts.Credentials{Email: u.Email, Password: input.Password, Code: codes[1]}, 200)
}
