package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"github.com/jackc/pgx/v5"
	"image"
	"image/jpeg"
	"time"
)

const MemoryMediaQuota = 100 * 1024 * 1024

type MemoryMedia struct {
	ID          string     `json:"id"`
	State       string     `json:"state"`
	ContentType string     `json:"content_type"`
	SHA256      string     `json:"sha256"`
	ByteSize    int        `json:"byte_size"`
	Width       int        `json:"width"`
	Height      int        `json:"height"`
	CreatedAt   time.Time  `json:"created_at"`
	DeletedAt   *time.Time `json:"deleted_at"`
}

const memoryMediaColumns = `id,state,content_type,sha256,byte_size,width,height,created_at,deleted_at`

func scanMemoryMedia(row pgx.Row) (MemoryMedia, error) {
	var m MemoryMedia
	err := row.Scan(&m.ID, &m.State, &m.ContentType, &m.SHA256, &m.ByteSize, &m.Width, &m.Height, &m.CreatedAt, &m.DeletedAt)
	return m, libraryMissing(err)
}
func (s *Service) ReserveMemoryMedia(ctx context.Context, owner string, size int) (MemoryMedia, error) {
	if size < 1 || size > media.MaxBytes {
		return MemoryMedia{}, fault.Field("byte_size", "单张图片需为 1 字节至 2 MiB。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return MemoryMedia{}, err
	}
	defer tx.Rollback(context.Background())
	// Expired uploads were never attached and have no retained memory references.
	if _, err = tx.Exec(ctx, `DELETE FROM private_memory_media m WHERE owner_id=$1 AND expires_at<now() AND NOT EXISTS(SELECT 1 FROM memory_notes n WHERE n.owner_id=$1 AND m.id=ANY(n.media_ids))`, owner); err != nil {
		return MemoryMedia{}, err
	}
	var used int64
	var count int
	if err = tx.QueryRow(ctx, `SELECT coalesce(sum(byte_size),0),count(*) FROM private_memory_media WHERE owner_id=$1 AND state<>'deleted'`, owner).Scan(&used, &count); err != nil {
		return MemoryMedia{}, err
	}
	if count >= 10000 {
		return MemoryMedia{}, fault.Field("byte_size", "私人图片达到 10000 个上限，请先清理原件。")
	}
	if used+int64(size) > MemoryMediaQuota {
		return MemoryMedia{}, fault.Field("byte_size", "私人记忆图片超过 100 MiB 配额，请先清理不需要的原件。")
	}
	m, err := scanMemoryMedia(tx.QueryRow(ctx, `INSERT INTO private_memory_media(id,owner_id,state,byte_size,expires_at) VALUES($1,$2,'reserved',$3,now()+interval '1 day') RETURNING `+memoryMediaColumns, id.New(), owner, size))
	if err != nil {
		return m, err
	}
	return m, tx.Commit(ctx)
}
func memoryThumbnail(data []byte) ([]byte, error) {
	source, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	b := source.Bounds()
	w, h := b.Dx(), b.Dy()
	if w > 480 {
		h = h * 480 / w
		w = 480
	}
	if h > 480 {
		w = w * 480 / h
		h = 480
	}
	if w < 1 {
		w = 1
	}
	if h < 1 {
		h = 1
	}
	target := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			target.Set(x, y, source.At(b.Min.X+x*b.Dx()/w, b.Min.Y+y*b.Dy()/h))
		}
	}
	var out bytes.Buffer
	err = jpeg.Encode(&out, target, &jpeg.Options{Quality: 82})
	return out.Bytes(), err
}
func (s *Service) FinalizeMemoryMedia(ctx context.Context, owner, resource, contentType string, data []byte) (MemoryMedia, error) {
	if !id.Valid(resource) {
		return MemoryMedia{}, fault.New("not_found", "图片预留不存在。")
	}
	pic, err := media.Validate(data, contentType)
	if err != nil {
		return MemoryMedia{}, err
	}
	thumb, err := memoryThumbnail(data)
	if err != nil {
		return MemoryMedia{}, err
	}
	hash := sha256.Sum256(data)
	digest := hex.EncodeToString(hash[:])
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return MemoryMedia{}, err
	}
	defer tx.Rollback(context.Background())
	m, err := scanMemoryMedia(tx.QueryRow(ctx, `SELECT `+memoryMediaColumns+` FROM private_memory_media WHERE owner_id=$1 AND id=$2 AND (expires_at IS NULL OR expires_at>now()) FOR UPDATE`, owner, resource))
	if err != nil {
		return m, err
	}
	if m.State == "ready" && m.SHA256 == digest {
		return m, nil
	}
	if m.State != "reserved" || m.ByteSize != len(data) {
		return m, fault.New("version_conflict", "上传已完成、已过期或大小与预留不同，请重新选择图片。")
	}
	m, err = scanMemoryMedia(tx.QueryRow(ctx, `UPDATE private_memory_media SET state='ready',content_type=$3,sha256=$4,width=$5,height=$6,data=$7,thumbnail=$8 WHERE owner_id=$1 AND id=$2 RETURNING `+memoryMediaColumns, owner, resource, pic.ContentType, digest, pic.Width, pic.Height, data, thumb))
	if err != nil {
		return m, err
	}
	return m, tx.Commit(ctx)
}

type MemoryMediaPage struct {
	LibraryPage[MemoryMedia]
	UsedBytes  int64 `json:"used_bytes"`
	QuotaBytes int64 `json:"quota_bytes"`
}

func (s *Service) MemoryMedia(ctx context.Context, owner string, page int) (MemoryMediaPage, error) {
	out := MemoryMediaPage{LibraryPage: LibraryPage[MemoryMedia]{Items: []MemoryMedia{}, Page: libraryPage(page), PageSize: 50}, QuotaBytes: MemoryMediaQuota}
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FILTER(WHERE state='ready'),coalesce(sum(byte_size) FILTER(WHERE state<>'deleted'),0) FROM private_memory_media WHERE owner_id=$1`, owner).Scan(&out.Total, &out.UsedBytes); err != nil {
		return out, err
	}
	rows, err := s.pool.Query(ctx, `SELECT `+memoryMediaColumns+` FROM private_memory_media WHERE owner_id=$1 AND state='ready' ORDER BY created_at DESC,id DESC LIMIT 50 OFFSET $2`, owner, (out.Page-1)*50)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		m, err := scanMemoryMedia(rows)
		if err != nil {
			return out, err
		}
		out.Items = append(out.Items, m)
	}
	return out, rows.Err()
}
func (s *Service) ReadMemoryMedia(ctx context.Context, owner, resource string, thumbnail bool) (media.Image, error) {
	var out media.Image
	if !id.Valid(resource) {
		return out, fault.New("not_found", "图片不存在。")
	}
	field := "data"
	if thumbnail {
		field = "thumbnail"
	}
	err := s.pool.QueryRow(ctx, `SELECT content_type,width,height,`+field+` FROM private_memory_media WHERE owner_id=$1 AND id=$2 AND state='ready'`, owner, resource).Scan(&out.ContentType, &out.Width, &out.Height, &out.Data)
	if thumbnail {
		out.ContentType = "image/jpeg"
	}
	return out, libraryMissing(err)
}
func (s *Service) DeleteMemoryMedia(ctx context.Context, owner, resource string) error {
	if !id.Valid(resource) {
		return fault.New("not_found", "图片不存在。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	m, err := scanMemoryMedia(tx.QueryRow(ctx, `SELECT `+memoryMediaColumns+` FROM private_memory_media WHERE owner_id=$1 AND id=$2 FOR UPDATE`, owner, resource))
	if err != nil {
		return err
	}
	if m.State == "deleted" {
		return nil
	}
	if _, err = tx.Exec(ctx, `UPDATE private_memory_media SET state='deleted',data=NULL,thumbnail=NULL,deleted_at=now(),expires_at=NULL WHERE owner_id=$1 AND id=$2`, owner, resource); err != nil {
		return err
	}
	if err = libraryRevision(ctx, tx, owner, resource, "media", "deleted", map[string]any{"sha256": m.SHA256, "reason": "SOURCE_MEDIA_DELETED"}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
