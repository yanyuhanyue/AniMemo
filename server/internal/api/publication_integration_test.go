//go:build integration

package api

import (
	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/journal"
	"bytes"
	"context"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const testSetupToken = "isolated-test-setup-token-32-characters"

func publicationServer(t *testing.T, pool *pgxpool.Pool) string {
	t.Helper()
	s := httptest.NewUnstartedServer(nil)
	base := "http://" + s.Listener.Addr().String()
	s.Config.Handler = New(pool, Config{PublicOrigin: base, SetupToken: testSetupToken})
	s.Start()
	t.Cleanup(s.Close)
	return base
}
func setupAdmin(t *testing.T, base string) (*http.Client, accounts.User) {
	t.Helper()
	c := browser()
	u := decodeAs[accounts.User](t, request(t, c, base, "POST", "/api/v1/setup", map[string]string{"token": testSetupToken, "email": "admin@example.test", "password": "admin-test-passphrase", "display_name": "实例管理"}, 201))
	return c, u
}
func TestPublicSharingRevocationAndModeration(t *testing.T) {
	pool := isolatedDatabase(t)
	base := publicationServer(t, pool)
	admin, _ := setupAdmin(t, base)
	alice, u := registerBrowser(t, base, "alice@example.test")
	bob, _ := registerBrowser(t, base, "bob@example.test")
	guest := browser()
	request(t, bob, base, "GET", "/api/v1/admin/users", nil, 403)
	entry := decodeAs[journal.Entry](t, request(t, alice, base, "POST", "/api/v1/entries", map[string]any{"title": "公开的作品", "notes": "可以展示的短评", "visibility": "unlisted", "total_episodes": 12}, 201))
	path := "/api/v1/entries/" + entry.ID
	entry = decodeAs[journal.Entry](t, uploadCover(t, alice, base, path+"/cover?version=1", "image/png", coverFixture(t, "image/png"), 200))
	watch := map[string]any{"watched_on": "2026-10-01", "episode_from": 1, "episode_to": 2, "note": "NEVER-PUBLIC-WATCH-NOTE", "request_id": "d6c98217-eede-4a46-a0ce-607ab2c5cf75"}
	request(t, alice, base, "POST", path+"/history", watch, 201)
	share := "/api/v1/public/shared/" + entry.ShareSlug
	image := share + "/cover/" + *entry.CoverRevision
	request(t, guest, base, "GET", share, nil, 404)
	request(t, guest, base, "GET", image, nil, 404)
	publish := func(action string) {
		u = decodeAs[accounts.User](t, request(t, alice, base, "POST", "/api/v1/settings/publication", map[string]any{"action": action, "version": u.Version}, 200))
	}
	publish("enable-sharing")
	shared := request(t, guest, base, "GET", share, nil, 200)
	for _, private := range []string{"NEVER-PUBLIC-WATCH-NOTE", u.Email, u.ID, entry.ID, "version", "created_at"} {
		if bytes.Contains(shared.body, []byte(private)) {
			t.Fatalf("private field leaked: %s", private)
		}
	}
	request(t, guest, base, "GET", image, nil, 200)
	directory := decodeAs[journal.Directory](t, request(t, guest, base, "GET", "/api/v1/public/showcases", nil, 200))
	if directory.Total != 0 {
		t.Fatal("unlisted owner exposed in directory")
	}
	request(t, guest, base, "GET", "/api/v1/public/showcases/"+u.PublicSlug, nil, 404)
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "GET", path, nil, 200))
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "PATCH", path, map[string]any{"version": entry.Version, "visibility": "public"}, 200))
	publish("request-public")
	request(t, guest, base, "GET", "/api/v1/public/showcases/"+u.PublicSlug, nil, 404)
	reviewed := decodeAs[accounts.AdminUser](t, request(t, admin, base, "POST", "/api/v1/admin/users/"+u.ID, accounts.AdminAction{Action: "approve-public", Version: u.Version}, 200))
	u = reviewed.User
	catalog := decodeAs[journal.PublicPage](t, request(t, guest, base, "GET", "/api/v1/public/catalog?search=公开", nil, 200))
	if catalog.Total != 1 {
		t.Fatal("approved catalog missing")
	}
	directory = decodeAs[journal.Directory](t, request(t, guest, base, "GET", "/api/v1/public/showcases", nil, 200))
	if directory.Total != 1 || directory.Items[0].Entries != 1 {
		t.Fatal("directory count wrong")
	}
	request(t, bob, base, "POST", path+"/share/reset", map[string]int{"version": entry.Version}, 404)
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "POST", path+"/share/reset", map[string]int{"version": entry.Version}, 200))
	request(t, guest, base, "GET", share, nil, 404)
	request(t, guest, base, "GET", image, nil, 404)
	share = "/api/v1/public/shared/" + entry.ShareSlug
	image = share + "/cover/" + *entry.CoverRevision
	request(t, guest, base, "GET", share, nil, 200)
	publish("disable-sharing")
	request(t, guest, base, "GET", share, nil, 404)
	request(t, guest, base, "GET", image, nil, 404)
	publish("enable-sharing")
	action := func(action string, version int) {
		request(t, admin, base, "POST", "/api/v1/admin/resources/entry/"+entry.ID, map[string]any{"action": action, "version": version, "reason": "权限测试"}, 204)
	}
	action("trash", entry.Version)
	request(t, alice, base, "GET", path, nil, 404)
	request(t, guest, base, "GET", share, nil, 404)
	stats := decodeAs[journal.Stats](t, request(t, alice, base, "GET", "/api/v1/stats", nil, 200))
	if stats.Total != 0 || stats.WatchRecords != 0 {
		t.Fatal("trash counted in journal")
	}
	exported := decodeAs[journal.Export](t, request(t, alice, base, "GET", "/api/v1/export", nil, 200))
	if len(exported.Entries) != 0 || len(exported.History) != 0 {
		t.Fatal("trash exported")
	}
	request(t, alice, base, "GET", path+"/cover/"+*entry.CoverRevision, nil, 404)
	action("restore", entry.Version+1)
	entry = decodeAs[journal.Entry](t, request(t, alice, base, "GET", path, nil, 200))
	if entry.Visibility != "private" {
		t.Fatal("restoration accidentally republished entry")
	}
	request(t, guest, base, "GET", share, nil, 404)
	audit := decodeAs[accounts.AuditPage](t, request(t, admin, base, "GET", "/api/v1/admin/audit", nil, 200))
	if audit.Total != 4 {
		t.Fatalf("audit count %d", audit.Total)
	}
}
func TestAdminSetupRolesAndRegistration(t *testing.T) {
	pool := isolatedDatabase(t)
	base := publicationServer(t, pool)
	request(t, browser(), base, "POST", "/api/v1/setup", map[string]string{"token": "bad"}, 403)
	admin, u := setupAdmin(t, base)
	status := decodeAs[map[string]bool](t, request(t, browser(), base, "GET", "/api/v1/setup", nil, 200))
	if status["available"] {
		t.Fatal("setup still open")
	}
	request(t, browser(), base, "POST", "/api/v1/setup", map[string]string{"token": testSetupToken, "email": "another@example.test", "password": "another-test-passphrase", "display_name": "another"}, 409)
	for _, action := range []string{"remove-admin", "disable"} {
		request(t, admin, base, "POST", "/api/v1/admin/users/"+u.ID, accounts.AdminAction{Action: action, Version: u.Version}, 409)
	}
	alice, a := registerBrowser(t, base, "alice@example.test")
	a = decodeAs[accounts.AdminUser](t, request(t, admin, base, "POST", "/api/v1/admin/users/"+a.ID, accounts.AdminAction{Action: "grant-admin", Version: a.Version}, 200)).User
	request(t, alice, base, "GET", "/api/v1/auth/me", nil, 401)
	request(t, alice, base, "POST", "/api/v1/auth/login", accounts.Credentials{Email: a.Email, Password: "foundation-passphrase"}, 200)
	request(t, alice, base, "POST", "/api/v1/admin/users/"+u.ID, accounts.AdminAction{Action: "remove-admin", Version: u.Version}, 200)
	request(t, admin, base, "GET", "/api/v1/admin/users", nil, 401)
	request(t, admin, base, "POST", "/api/v1/auth/login", accounts.Credentials{Email: u.Email, Password: "admin-test-passphrase"}, 200)
	request(t, admin, base, "GET", "/api/v1/admin/users", nil, 403)
	site := decodeAs[accounts.SiteSettings](t, request(t, alice, base, "GET", "/api/v1/site", nil, 200))
	site.Name = "测试实例"
	site.RegistrationOpen = false
	updated := decodeAs[accounts.SiteSettings](t, request(t, alice, base, "PUT", "/api/v1/admin/site", site, 200))
	if updated.Version != site.Version+1 {
		t.Fatal("site version")
	}
	request(t, alice, base, "PUT", "/api/v1/admin/site", site, 409)
	request(t, browser(), base, "POST", "/api/v1/auth/register", accounts.Registration{Email: "closed@example.test", DisplayName: "closed", Password: "closed-passphrase"}, 403)
	users := decodeAs[accounts.AdminUsers](t, request(t, alice, base, "GET", "/api/v1/admin/users?search=alice", nil, 200))
	if users.Total != 1 {
		t.Fatal("user filter")
	}
	// Authorization is also rechecked in the mutation transaction, not just middleware.
	_, err := accounts.New(pool).AdminUserAction(context.Background(), u.ID, a.ID, accounts.AdminAction{Action: "disable", Version: a.Version})
	if err == nil {
		t.Fatal("stale actor authorization accepted")
	}
}
func TestColumnsPublicationAndPrivacy(t *testing.T) {
	pool := isolatedDatabase(t)
	base := publicationServer(t, pool)
	admin, _ := setupAdmin(t, base)
	alice, u := registerBrowser(t, base, "author@example.test")
	bob, _ := registerBrowser(t, base, "other@example.test")
	guest := browser()
	create := func(c *http.Client, title, visibility string) journal.Entry {
		return decodeAs[journal.Entry](t, request(t, c, base, "POST", "/api/v1/entries", map[string]string{"title": title, "visibility": visibility}, 201))
	}
	pub, private, foreign := create(alice, "附录作品", "public"), create(alice, "PRIVATE-ATTACHMENT", "private"), create(bob, "OTHER-OWNER", "public")
	input := journal.ColumnInput{Title: "写给动画的一封信", Summary: "摘要", Body: strings.Repeat("好", 30000), EntryIDs: []string{pub.ID, private.ID}}
	request(t, alice, base, "POST", "/api/v1/columns", journal.ColumnInput{Title: "越权", EntryIDs: []string{foreign.ID}}, 400)
	c := decodeAs[journal.Column](t, request(t, alice, base, "POST", "/api/v1/columns", input, 201))
	path := "/api/v1/columns/" + c.ID
	public := "/api/v1/public/columns/" + c.ID
	request(t, bob, base, "GET", path, nil, 404)
	request(t, guest, base, "GET", public, nil, 404)
	request(t, alice, base, "POST", path, map[string]any{"action": "submit", "version": c.Version}, 400)
	request(t, alice, base, "POST", "/api/v1/settings/publication", map[string]any{"action": "enable-sharing", "version": u.Version}, 200)
	c = decodeAs[journal.Column](t, uploadCover(t, alice, base, path+fmt.Sprintf("/cover?version=%d", c.Version), "image/png", coverFixture(t, "image/png"), 200))
	image := public + "/cover/" + *c.CoverRevision
	request(t, guest, base, "GET", image, nil, 404)
	c = decodeAs[journal.Column](t, request(t, alice, base, "POST", path, map[string]any{"action": "submit", "version": c.Version}, 200))
	resource := decodeAs[journal.Resources](t, request(t, admin, base, "GET", "/api/v1/admin/resources?kind=column&state=pending", nil, 200))
	if resource.Total != 1 {
		t.Fatal("pending column not visible to moderator")
	}
	request(t, admin, base, "GET", "/api/v1/admin/resources/column/"+c.ID, nil, 200)
	c = decodeAs[journal.Column](t, request(t, admin, base, "POST", "/api/v1/admin/columns/"+c.ID, map[string]any{"action": "approve", "reason": "", "version": c.Version}, 200))
	p := decodeAs[journal.PublicColumn](t, request(t, guest, base, "GET", public, nil, 200))
	if len(p.Entries) != 1 || p.Entries[0].Title != pub.Title {
		t.Fatal("private attachments leaked")
	}
	request(t, guest, base, "GET", image, nil, 200)
	c = decodeAs[journal.Column](t, request(t, admin, base, "POST", "/api/v1/admin/columns/"+c.ID, map[string]any{"action": "feature", "reason": "精选", "version": c.Version}, 200))
	directory := decodeAs[journal.PublicColumns](t, request(t, guest, base, "GET", "/api/v1/public/columns?search=动画", nil, 200))
	if directory.Total != 1 || !directory.Items[0].Featured {
		t.Fatal("featured listing")
	}
	input.Version = c.Version
	input.Body = "修改后的草稿"
	c = decodeAs[journal.Column](t, request(t, alice, base, "PUT", path, input, 200))
	if c.State != "draft" || c.Featured {
		t.Fatal("published edit not reset")
	}
	request(t, guest, base, "GET", public, nil, 404)
	request(t, guest, base, "GET", image, nil, 404)
	request(t, alice, base, "PUT", path, input, 409)
	c = decodeAs[journal.Column](t, request(t, alice, base, "POST", path, map[string]any{"action": "submit", "version": c.Version}, 200))
	c = decodeAs[journal.Column](t, request(t, alice, base, "POST", path, map[string]any{"action": "withdraw", "version": c.Version}, 200))
	if c.State != "draft" {
		t.Fatal("withdrawal failed")
	}
	request(t, alice, base, "POST", path, map[string]any{"action": "delete", "version": c.Version}, 200)
	request(t, alice, base, "GET", path, nil, 404)
	request(t, alice, base, "GET", "/api/v1/entries/"+pub.ID, nil, 200)
}
