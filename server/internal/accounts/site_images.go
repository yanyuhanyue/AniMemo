package accounts

import (
	"context"
	"errors"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
)

func (s *Service) SiteImage(ctx context.Context, kind, revision string) (media.Image, error) {
	var picture media.Image
	if (kind != "icon" && kind != "cover") || !id.Valid(revision) {
		return picture, fault.New("not_found", "没有找到站点图片。")
	}
	err := s.pool.QueryRow(ctx, `SELECT content_type,data FROM site_images WHERE kind=$1 AND revision=$2`, kind, revision).Scan(&picture.ContentType, &picture.Data)
	if errors.Is(err, pgx.ErrNoRows) {
		return picture, fault.New("not_found", "没有找到站点图片。")
	}
	return picture, err
}

func (s *Service) SetSiteImage(ctx context.Context, actor, kind string, version int, contentType string, data []byte) (SiteSettings, error) {
	var out SiteSettings
	if kind != "icon" && kind != "cover" {
		return out, fault.Field("kind", "请选择站点图标或名片封面。")
	}
	var picture media.Image
	var err error
	if data != nil {
		picture, err = media.Validate(data, contentType)
		if err != nil {
			return out, err
		}
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return out, err
	}
	var current int
	if err = tx.QueryRow(ctx, `SELECT version FROM site_settings FOR UPDATE`).Scan(&current); err != nil {
		return out, err
	}
	if version != current {
		return out, fault.New("version_conflict", "站点设置已更新，请刷新后重试。")
	}
	if data == nil {
		_, err = tx.Exec(ctx, `DELETE FROM site_images WHERE kind=$1`, kind)
	} else {
		_, err = tx.Exec(ctx, `INSERT INTO site_images(kind,revision,content_type,data) VALUES($1,$2,$3,$4) ON CONFLICT(kind) DO UPDATE SET revision=excluded.revision,content_type=excluded.content_type,data=excluded.data`, kind, id.New(), picture.ContentType, picture.Data)
	}
	if err != nil {
		return out, err
	}
	if _, err = tx.Exec(ctx, `UPDATE site_settings SET version=version+1`); err != nil {
		return out, err
	}
	if err = governance.Audit(ctx, tx, actor, "update-site-image", "site", kind, map[string]any{"removed": data == nil}); err != nil {
		return out, err
	}
	if err = scanSite(tx.QueryRow(ctx, siteQuery), &out); err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
