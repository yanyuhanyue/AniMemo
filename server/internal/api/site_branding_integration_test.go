//go:build integration

package api

import (
	"bytes"
	"fmt"
	"testing"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/media"
)

func TestSiteBrandingLifecycleAndPermissions(t *testing.T) {
	base := publicationServer(t, isolatedDatabase(t))
	admin, _ := setupAdmin(t, base)
	alice, _ := registerBrowser(t, base, "branding@example.test")
	guest := browser()
	site := decodeAs[accounts.SiteSettings](t, request(t, guest, base, "GET", "/api/v1/site", nil, 200))
	site.Name, site.Description = "星夜手账", "记住那些喜欢的故事。"
	request(t, alice, base, "PUT", "/api/v1/admin/site", site, 403)
	site = decodeAs[accounts.SiteSettings](t, request(t, admin, base, "PUT", "/api/v1/admin/site", site, 200))
	image := coverFixture(t, "image/png")
	path := fmt.Sprintf("/api/v1/admin/site/images/icon?version=%d", site.Version)
	uploadCover(t, alice, base, path, "image/png", image, 403)
	uploadCover(t, guest, base, path, "image/png", image, 401)
	uploadCover(t, admin, base, path, "image/svg+xml", []byte("<svg/>"), 415)
	uploadCover(t, admin, base, path, "image/png", bytes.Repeat([]byte{1}, media.MaxBytes+1), 413)
	site = decodeAs[accounts.SiteSettings](t, uploadCover(t, admin, base, path, "image/png", image, 200))
	icon := site.IconRevision
	if icon == "" || site.CoverRevision != "" || site.Name != "星夜手账" {
		t.Fatal("incomplete branding response")
	}
	got := request(t, guest, base, "GET", "/api/v1/site/images/icon/"+icon, nil, 200)
	if !bytes.Equal(got.body, image) {
		t.Fatal("public branding bytes changed")
	}
	uploadCover(t, admin, base, path, "image/png", image, 409)
	site = decodeAs[accounts.SiteSettings](t, uploadCover(t, admin, base, fmt.Sprintf("/api/v1/admin/site/images/cover?version=%d", site.Version), "image/png", image, 200))
	cover := site.CoverRevision
	// Existing settings clients may omit the read-only image revision fields.
	site = decodeAs[accounts.SiteSettings](t, request(t, admin, base, "PUT", "/api/v1/admin/site", map[string]any{"name": site.Name, "description": site.Description, "homepage_owner_slug": "", "registration_open": true, "version": site.Version}, 200))
	if site.IconRevision != icon || site.CoverRevision != cover || cover == "" {
		t.Fatal("text settings replaced images")
	}
	path = fmt.Sprintf("/api/v1/admin/site/images/icon?version=%d", site.Version)
	request(t, alice, base, "DELETE", path, nil, 403)
	site = decodeAs[accounts.SiteSettings](t, request(t, admin, base, "DELETE", path, nil, 200))
	if site.IconRevision != "" || site.CoverRevision != cover {
		t.Fatal("restoring icon affected cover")
	}
	request(t, guest, base, "GET", "/api/v1/site/images/icon/"+icon, nil, 404)
	request(t, guest, base, "GET", "/api/v1/site/images/cover/"+cover, nil, 200)
	request(t, admin, base, "DELETE", fmt.Sprintf("/api/v1/admin/site/images/other?version=%d", site.Version), nil, 400)
}

func TestPresetTagsAvailableWithPersonalOverrides(t *testing.T) {
	base := publicationServer(t, isolatedDatabase(t))
	admin, _ := setupAdmin(t, base)
	alice, _ := registerBrowser(t, base, "tags-visible@example.test")
	bob, _ := registerBrowser(t, base, "tags-private@example.test")
	request(t, admin, base, "PUT", "/api/v1/admin/presets", journal.Preset{Name: "治愈", Color: "#123456"}, 204)
	tags := func() []journal.Tag {
		return decodeAs[struct {
			Items []journal.Tag `json:"items"`
		}](t, request(t, alice, base, "GET", "/api/v1/tags", nil, 200)).Items
	}
	if got := tags(); len(got) != 1 || got[0].Color != "#123456" {
		t.Fatal("new user cannot see preset", got)
	}
	request(t, admin, base, "PUT", "/api/v1/admin/presets", journal.Preset{Name: "治愈", Color: "#abcdef", Version: 1}, 204)
	if got := tags(); got[0].Color != "#abcdef" {
		t.Fatal("default did not follow preset", got)
	}
	request(t, alice, base, "PUT", "/api/v1/tags", journal.Tag{Name: "治愈", Color: "#654321"}, 204)
	request(t, bob, base, "PUT", "/api/v1/tags", journal.Tag{Name: "别人的私人标签", Color: "#111111"}, 204)
	request(t, admin, base, "PUT", "/api/v1/admin/presets", journal.Preset{Name: "治愈", Color: "#fedcba", Version: 2}, 204)
	if got := tags(); len(got) != 1 || got[0].Color != "#654321" {
		t.Fatal("personal color overridden or another user's tag leaked", got)
	}
	request(t, admin, base, "DELETE", "/api/v1/admin/presets", journal.Preset{Name: "治愈", Color: "#fedcba", Version: 3}, 204)
	if got := tags(); len(got) != 1 || got[0].Color != "#654321" {
		t.Fatal("preset removal deleted personal tag", got)
	}
}
