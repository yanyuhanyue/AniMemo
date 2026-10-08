package journal

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"image"
	"image/png"
	"regexp"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
)

type AchievementImage struct {
	ID     string `json:"id"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
	Data   []byte `json:"data,omitempty"`
}

var achievementImageID = regexp.MustCompile(`^[a-f0-9]{64}$`)

func achievementBadgeValid(badge string) bool {
	switch badge {
	case "spark", "moon", "orbit", "flower", "book", "ticket", "shelf", "album":
		return true
	}
	return false
}

func (s *Service) UploadAchievementImage(ctx context.Context, actor, kind string, data []byte) (AchievementImage, error) {
	var out AchievementImage
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return out, err
	}
	if _, err = media.Validate(data, kind); err != nil {
		return out, err
	}
	source, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return out, err
	}
	b := source.Bounds()
	w, h := b.Dx(), b.Dy()
	if w > 256 {
		h = max(1, h*256/w)
		w = 256
	}
	if h > 256 {
		w = max(1, w*256/h)
		h = 256
	}
	// Rasterize to a small PNG: strip metadata and retain transparent artwork.
	target := image.NewNRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			target.Set(x, y, source.At(b.Min.X+x*b.Dx()/w, b.Min.Y+y*b.Dy()/h))
		}
	}
	var encoded bytes.Buffer
	if err = png.Encode(&encoded, target); err != nil {
		return out, err
	}
	out = AchievementImage{ID: fmt.Sprintf("%x", sha256.Sum256(encoded.Bytes())), Width: w, Height: h, Data: encoded.Bytes()}
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(718226055)`); err != nil {
		return out, err
	}
	// Remove abandoned previews, never art referenced by a rule revision.
	if _, err = tx.Exec(ctx, `DELETE FROM achievement_images i WHERE id<>$1 AND created_at<now()-interval '1 day' AND NOT EXISTS(SELECT 1 FROM achievement_rule_revisions r WHERE r.badge_image_id=i.id)`, out.ID); err != nil {
		return out, err
	}
	if err = saveAchievementImage(ctx, tx, actor, out); err != nil {
		return out, err
	}
	if err = governance.Audit(ctx, tx, actor, "achievement-image", "achievement-image", out.ID, map[string]any{"width": w, "height": h}); err != nil {
		return out, err
	}
	out.Data = nil
	return out, tx.Commit(ctx)
}

func saveAchievementImage(ctx context.Context, tx pgx.Tx, owner string, im AchievementImage) error {
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(718226055)`); err != nil {
		return err
	}
	var exists bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM achievement_images WHERE id=$1)`, im.ID).Scan(&exists); err != nil {
		return err
	}
	if exists {
		return nil
	}
	var size int64
	if err := tx.QueryRow(ctx, `SELECT coalesce(sum(octet_length(data)),0) FROM achievement_images`).Scan(&size); err != nil {
		return err
	}
	if size+int64(len(im.Data)) > 100<<20 {
		return fault.Field("badge_image_id", "成就图案已达到 100 MiB 存储上限。")
	}
	_, err := tx.Exec(ctx, `INSERT INTO achievement_images(id,data,width,height,uploaded_by) VALUES($1,$2,$3,$4,$5)`, im.ID, im.Data, im.Width, im.Height, owner)
	return err
}

func (s *Service) AchievementImage(ctx context.Context, owner, key string) (AchievementImage, error) {
	var out AchievementImage
	if !achievementImageID.MatchString(key) {
		return out, fault.New("not_found", "图案不存在。")
	}
	err := s.pool.QueryRow(ctx, `SELECT i.id,i.width,i.height,i.data FROM achievement_images i WHERE i.id=$2 AND (
 i.uploaded_by=$1 OR EXISTS(SELECT 1 FROM users WHERE id=$1 AND is_admin AND NOT disabled) OR
 EXISTS(SELECT 1 FROM achievement_rule_revisions r JOIN achievement_tiers t ON t.id=r.tier_id WHERE r.badge_image_id=i.id AND (
   (t.active AND NOT t.archived AND t.current_revision=r.revision) OR
   EXISTS(SELECT 1 FROM achievement_unlocks u WHERE u.owner_id=$1 AND u.tier_id=r.tier_id AND u.rule_revision=r.revision) OR
   EXISTS(SELECT 1 FROM achievement_progress p WHERE p.owner_id=$1 AND p.tier_id=r.tier_id AND p.rule_revision=r.revision))))`, owner, key).Scan(&out.ID, &out.Width, &out.Height, &out.Data)
	return out, libraryMissing(err)
}

func validateAchievementImages(images []AchievementImage) (map[string]bool, error) {
	seen := map[string]bool{}
	bad := fault.Field("badge_image_id", "成就图案缺失、损坏或校验不符。")
	if len(images) > 2000 {
		return nil, bad
	}
	for _, im := range images {
		if seen[im.ID] || !achievementImageID.MatchString(im.ID) || len(im.Data) > 300000 || fmt.Sprintf("%x", sha256.Sum256(im.Data)) != im.ID {
			return nil, bad
		}
		config, format, err := image.DecodeConfig(bytes.NewReader(im.Data))
		if err != nil || format != "png" || config.Width != im.Width || config.Height != im.Height || im.Width < 1 || im.Height < 1 || im.Width > 256 || im.Height > 256 {
			return nil, bad
		}
		if _, err = png.Decode(bytes.NewReader(im.Data)); err != nil {
			return nil, bad
		}
		seen[im.ID] = true
	}
	return seen, nil
}
