package journal

import (
	"animemo.local/server/internal/telemetry"
	"context"
	"errors"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
)

const CoverQuotaBytes = 100 * 1024 * 1024

func (s *Service) Cover(ctx context.Context, owner, entryID, revision string) (media.Image, error) {
	var cover media.Image
	if !id.Valid(entryID) || !id.Valid(revision) {
		return cover, fault.New("not_found", "没有找到封面。")
	}
	err := s.pool.QueryRow(ctx, `SELECT c.content_type,c.width,c.height,c.data FROM entry_covers c JOIN entries e ON e.id=c.entry_id WHERE e.id=$1 AND e.user_id=$2 AND e.deleted_at IS NULL AND c.revision=$3`, entryID, owner, revision).Scan(&cover.ContentType, &cover.Width, &cover.Height, &cover.Data)
	if errors.Is(err, pgx.ErrNoRows) {
		err = fault.New("not_found", "没有找到封面。")
	}
	if err != nil {
		return cover, err
	}
	return s.storage.Resolve(ctx, revision, cover)
}

func (s *Service) SetCover(ctx context.Context, owner, entryID string, version int, contentType string, data []byte) (Entry, error) {
	cover, err := media.Validate(data, contentType)
	if err != nil {
		return Entry{}, err
	}
	return s.changeCover(ctx, owner, entryID, version, &cover)
}

func (s *Service) DeleteCover(ctx context.Context, owner, entryID string, version int) (Entry, error) {
	return s.changeCover(ctx, owner, entryID, version, nil)
}

func (s *Service) changeCover(ctx context.Context, owner, entryID string, version int, cover *media.Image) (Entry, error) {
	if !id.Valid(entryID) {
		return Entry{}, fault.New("not_found", "没有找到这部番剧。")
	}
	if version < 1 {
		return Entry{}, fault.Field("version", "缺少记录版本，请刷新后重试。")
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Entry{}, err
	}
	defer tx.Rollback(context.Background())
	// Serialize quota changes across this owner's entries, including across API
	// processes. Always acquire this lock before the entry lock.
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226042))`, owner); err != nil {
		return Entry{}, err
	}
	e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, entryID, owner))
	if err != nil {
		return Entry{}, err
	}
	if version != e.Version {
		return Entry{}, fault.New("version_conflict", "这条记录已更新，请关闭详情后重新打开，再操作封面。")
	}
	if cover == nil {
		if e.CoverRevision == nil {
			return e, nil
		}
		_, err = tx.Exec(ctx, `DELETE FROM entry_covers WHERE entry_id=$1`, entryID)
	} else {
		err = saveCover(ctx, tx, owner, entryID, cover)
	}
	if err != nil {
		return Entry{}, err
	}
	e, err = scanEntry(tx.QueryRow(ctx, `UPDATE entries SET version=version+1,updated_at=now() WHERE id=$1 RETURNING `+columns, entryID))
	if err != nil {
		return Entry{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Entry{}, err
	}
	return e, nil
}

// The caller holds the owner's quota lock and the entry row lock.
func saveCover(ctx context.Context, tx pgx.Tx, owner, entryID string, cover *media.Image) error {
	var used int64
	if err := tx.QueryRow(ctx, `SELECT coalesce(sum(c.byte_size),0) FROM entry_covers c JOIN entries e ON e.id=c.entry_id WHERE e.user_id=$1 AND e.id<>$2`, owner, entryID).Scan(&used); err != nil {
		return err
	}
	if used+int64(len(cover.Data)) > CoverQuotaBytes {
		return fault.New("cover_quota_exceeded", "封面总容量不能超过 100 MB，请先移除一些封面。")
	}
	_, err := tx.Exec(ctx, `INSERT INTO entry_covers (entry_id,revision,content_type,width,height,byte_size,data) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (entry_id) DO UPDATE SET revision=excluded.revision,content_type=excluded.content_type,width=excluded.width,height=excluded.height,byte_size=excluded.byte_size,data=excluded.data`, entryID, id.New(), cover.ContentType, cover.Width, cover.Height, len(cover.Data), cover.Data)
	return err
}
