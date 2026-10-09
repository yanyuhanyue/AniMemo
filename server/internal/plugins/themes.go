package plugins

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strconv"

	"animemo.local/server/internal/fault"
	"animemo.local/server/pkg/pluginproto"
	"github.com/jackc/pgx/v5"
)

var themeColor = regexp.MustCompile(`^#[0-9a-fA-F]{6}$`)

func luminance(color string) float64 {
	value, _ := strconv.ParseUint(color[1:], 16, 32)
	channel := func(v uint64) float64 {
		c := float64(v) / 255
		if c <= 0.04045 {
			return c / 12.92
		}
		return math.Pow((c+0.055)/1.055, 2.4)
	}
	return 0.2126*channel(value>>16) + 0.7152*channel((value>>8)&255) + 0.0722*channel(value&255)
}

func validateNotesTheme(t *pluginproto.NotesTheme) error {
	if t == nil {
		return invalid("主题缺少札记外观声明。")
	}
	for _, color := range []string{t.Canvas, t.Paper, t.Ink, t.Muted, t.Primary, t.Border, t.Rule} {
		if !themeColor.MatchString(color) {
			return invalid("主题颜色需要完整的六位十六进制色值。")
		}
	}
	if luminance(t.Paper) < 0.8 || luminance(t.Canvas) < 0.7 {
		return invalid("当前主题接口只支持浅色阅读页面。")
	}
	for _, background := range []string{t.Paper, t.Canvas} {
		for _, foreground := range []string{t.Ink, t.Muted, t.Primary} {
			a, b := luminance(background), luminance(foreground)
			if (math.Max(a, b)+0.05)/(math.Min(a, b)+0.05) < 4.5 {
				return invalid("主题文字与背景的对比度不足。")
			}
		}
	}
	if (t.HeadingFont != "serif" && t.HeadingFont != "sans") || (t.ReadingSize != "standard" && t.ReadingSize != "large") || (t.Spacing != "comfortable" && t.Spacing != "relaxed") {
		return invalid("主题字体、字号或留白选项无效。")
	}
	return validatePresentation(t.Presentation)
}

// Theme selections confer no access to records and never execute plugin code.
type ThemeOptions struct {
	Items        []Release `json:"items"`
	SelectedSlug string    `json:"selected_slug"`
}
type ThemeSelection struct {
	Slug     string `json:"slug"`
	Revision int64  `json:"revision"`
}

func themeManifestValid(m pluginproto.Manifest, digest string) bool {
	if m.NotesTheme == nil || Compatible(m) != nil {
		return false
	}
	canonical, _ := json.Marshal(m)
	return fmt.Sprintf("%x", sha256.Sum256(canonical)) == digest && m.ModuleSHA256 == fmt.Sprintf("%x", sha256.Sum256(nil))
}

func (s *Service) Themes(ctx context.Context, owner string) (ThemeOptions, error) {
	out := ThemeOptions{Items: []Release{}}
	releases, err := s.List(ctx, false)
	if err != nil {
		return out, err
	}
	var selected string
	err = s.pool.QueryRow(ctx, `SELECT slug FROM user_note_themes WHERE user_id=$1`, owner).Scan(&selected)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return out, err
	}
	for _, r := range releases {
		if r.Health != "ready" || !themeManifestValid(r.Manifest, r.Digest) {
			continue
		}
		out.Items = append(out.Items, r)
		if r.Manifest.Slug == selected {
			out.SelectedSlug = selected
		}
	}
	return out, nil
}

func (s *Service) SelectTheme(ctx context.Context, owner string, input ThemeSelection) error {
	if input.Slug == "" {
		_, err := s.pool.Exec(ctx, `DELETE FROM user_note_themes WHERE user_id=$1`, owner)
		return err
	}
	if !slugPattern.MatchString(input.Slug) || input.Revision < 1 {
		return invalid("请选择可用的札记主题。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var m pluginproto.Manifest
	var digest string
	var revision int64
	// Serialize selection with activation/disable/uninstall so a stale dialog cannot
	// select a package that has just been withdrawn or switched to another version.
	err = tx.QueryRow(ctx, `SELECT r.manifest,r.digest,d.revision FROM plugin_deployments d JOIN plugin_releases r ON r.slug=d.slug AND r.version=d.active_version WHERE d.slug=$1 AND d.enabled AND d.health='ready' FOR SHARE OF d`, input.Slug).Scan(&m, &digest, &revision)
	if errors.Is(err, pgx.ErrNoRows) {
		return fault.New("not_found", "主题已停用或不可用，请重新选择。")
	}
	if err != nil {
		return err
	}
	if revision != input.Revision {
		return fault.New("version_conflict", "主题版本已变化，请重新预览。")
	}
	if !themeManifestValid(m, digest) {
		return invalid("主题声明无效，请选择其他主题。")
	}
	if _, err = tx.Exec(ctx, `INSERT INTO user_note_themes(user_id,slug) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET slug=excluded.slug`, owner, input.Slug); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// Assets are decorative package files, never memory images. Access still requires
// login and a currently enabled, healthy release (including preview use).
func (s *Service) ThemeAsset(ctx context.Context, slug, version, name string) ([]byte, string, error) {
	if !slugPattern.MatchString(slug) || !versionPattern.MatchString(version) || !assetName.MatchString(name) {
		return nil, "", fault.New("not_found", "主题资源不存在。")
	}
	var m pluginproto.Manifest
	var digest string
	var data []byte
	err := s.pool.QueryRow(ctx, `SELECT r.manifest,r.digest,decode(r.assets->>$3,'base64') FROM plugin_releases r JOIN plugin_deployments d ON d.slug=r.slug AND d.active_version=r.version WHERE r.slug=$1 AND r.version=$2 AND d.enabled AND d.health='ready'`, slug, version, name).Scan(&m, &digest, &data)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, "", fault.New("not_found", "主题资源不可用。")
	}
	if err != nil {
		return nil, "", err
	}
	if !themeManifestValid(m, digest) || m.NotesTheme.Presentation == nil {
		return nil, "", fault.New("not_found", "主题资源不可用。")
	}
	for _, a := range m.NotesTheme.Presentation.Assets {
		if a.Name == name && fmt.Sprintf("%x", sha256.Sum256(data)) == a.SHA256 {
			return data, a.ContentType, nil
		}
	}
	return nil, "", fault.New("not_found", "主题资源不存在或已损坏。")
}
