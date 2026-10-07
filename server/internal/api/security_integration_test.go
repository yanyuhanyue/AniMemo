//go:build integration

package api

import (
	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/journal"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha1"
	"encoding/base32"
	"encoding/binary"
	"fmt"
	"net/http/httptest"
	"testing"
	"time"
)

func enrollmentCode(t *testing.T, secret string, when time.Time) string {
	t.Helper()
	key, err := base32.StdEncoding.WithPadding(base32.NoPadding).DecodeString(secret)
	if err != nil {
		t.Fatal(err)
	}
	var data [8]byte
	binary.BigEndian.PutUint64(data[:], uint64(when.Unix()/30))
	h := hmac.New(sha1.New, key)
	h.Write(data[:])
	sum := h.Sum(nil)
	n := binary.BigEndian.Uint32(sum[sum[19]&15:]) & 0x7fffffff
	return fmt.Sprintf("%06d", n%1000000)
}
func TestTwoFactorRecoveryAndSessionRevocation(t *testing.T) {
	pool := isolatedDatabase(t)
	s := httptest.NewUnstartedServer(nil)
	base := "http://" + s.Listener.Addr().String()
	s.Config.Handler = New(pool, Config{PublicOrigin: base, SecretKey: bytes.Repeat([]byte{1}, 32)})
	s.Start()
	t.Cleanup(s.Close)
	alice, u := registerBrowser(t, base, "twofactor@example.test")
	device := browser()
	credentials := accounts.Credentials{Email: u.Email, Password: "foundation-passphrase"}
	request(t, device, base, "POST", "/api/v1/auth/login", credentials, 200)
	enroll := decodeAs[accounts.TwoFactorEnrollment](t, request(t, alice, base, "POST", "/api/v1/auth/two-factor/begin", map[string]string{"password": credentials.Password}, 200))
	code := enrollmentCode(t, enroll.Secret, time.Now())
	type result struct {
		User  accounts.User `json:"user"`
		Codes []string      `json:"recovery_codes"`
	}
	enabled := decodeAs[result](t, request(t, alice, base, "POST", "/api/v1/auth/two-factor", accounts.TwoFactorInput{Action: "enable", Password: credentials.Password, Code: code}, 200))
	if !enabled.User.OTPEnabled || len(enabled.Codes) != 8 {
		t.Fatal("enrollment not completed")
	}
	request(t, device, base, "GET", "/api/v1/auth/me", nil, 401)
	request(t, device, base, "POST", "/api/v1/auth/login", credentials, 401)
	// The enrollment code has already been consumed; logging in cannot replay it.
	credentials.Code = code
	request(t, device, base, "POST", "/api/v1/auth/login", credentials, 401)
	credentials.Code = enabled.Codes[0]
	request(t, device, base, "POST", "/api/v1/auth/login", credentials, 200)
	request(t, browser(), base, "POST", "/api/v1/auth/login", credentials, 401)
	regenerated := decodeAs[result](t, request(t, alice, base, "POST", "/api/v1/auth/two-factor", accounts.TwoFactorInput{Action: "recovery", Password: credentials.Password, Code: enabled.Codes[1]}, 200))
	if len(regenerated.Codes) != 8 {
		t.Fatal("missing regenerated recovery codes")
	}
	request(t, device, base, "GET", "/api/v1/auth/me", nil, 401)
	credentials.Code = enabled.Codes[2]
	request(t, device, base, "POST", "/api/v1/auth/login", credentials, 401)
	disabled := decodeAs[result](t, request(t, alice, base, "POST", "/api/v1/auth/two-factor", accounts.TwoFactorInput{Action: "disable", Password: credentials.Password, Code: regenerated.Codes[0]}, 200))
	if disabled.User.OTPEnabled || len(disabled.Codes) != 0 {
		t.Fatal("disable failed")
	}
	credentials.Code = ""
	request(t, device, base, "POST", "/api/v1/auth/login", credentials, 200)
	var count int
	if err := pool.QueryRow(context.Background(), `SELECT count(*) FROM recovery_codes WHERE user_id=$1`, u.ID).Scan(&count); err != nil || count != 0 {
		t.Fatal("recovery codes not deleted")
	}
}
func TestPresetsAndSafeMaintenance(t *testing.T) {
	pool := isolatedDatabase(t)
	base := publicationServer(t, pool)
	admin, _ := setupAdmin(t, base)
	alice, u := registerBrowser(t, base, "maint@example.test")
	preset := journal.Preset{Name: "治愈", Color: "#123456"}
	request(t, alice, base, "PUT", "/api/v1/admin/presets", preset, 403)
	request(t, admin, base, "PUT", "/api/v1/admin/presets", preset, 204)
	request(t, admin, base, "PUT", "/api/v1/admin/presets", preset, 409)
	presets := decodeAs[struct {
		Items []journal.Preset `json:"items"`
	}](t, request(t, alice, base, "GET", "/api/v1/presets", nil, 200))
	if len(presets.Items) != 1 || presets.Items[0].Version != 1 {
		t.Fatal("preset missing")
	}
	request(t, alice, base, "PUT", "/api/v1/tags", journal.Tag{Name: preset.Name, Color: preset.Color}, 204)
	preset.Version = 1
	preset.Color = "#abcdef"
	request(t, admin, base, "PUT", "/api/v1/admin/presets", preset, 204)
	request(t, admin, base, "DELETE", "/api/v1/admin/presets", preset, 409)
	preset.Version = 2
	request(t, admin, base, "DELETE", "/api/v1/admin/presets", preset, 204)
	ctx := context.Background()
	if _, err := pool.Exec(ctx, `INSERT INTO sessions(token_hash,user_id,expires_at) VALUES($1,$2,now()-interval '1 day')`, bytes.Repeat([]byte{1}, 32), u.ID); err != nil {
		t.Fatal(err)
	}
	status := decodeAs[accounts.InstanceStatus](t, request(t, admin, base, "GET", "/api/v1/admin/status", nil, 200))
	if status.Users != 2 || status.ExpiredSessions != 1 || status.DatabaseBytes <= 0 {
		t.Fatal("instance counts")
	}
	request(t, alice, base, "POST", "/api/v1/admin/maintenance", map[string]string{"action": "clear-expired"}, 403)
	cleaned := decodeAs[accounts.MaintenanceResult](t, request(t, admin, base, "POST", "/api/v1/admin/maintenance", map[string]string{"action": "clear-expired"}, 200))
	if cleaned.Sessions != 1 {
		t.Fatal("expired session not cleared")
	}
	request(t, alice, base, "GET", "/api/v1/auth/me", nil, 200)
}
