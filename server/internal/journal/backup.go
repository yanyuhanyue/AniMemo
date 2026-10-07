package journal

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
)

// Backup reads journal rows and image bytes from one database snapshot.
func (s *Service) Backup(ctx context.Context, owner string) ([]byte, error) {
	release, err := s.storage.HoldReads(ctx)
	if err != nil {
		return nil, err
	}
	defer release()
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(context.Background())
	document, err := exportJournal(ctx, tx, owner)
	if err != nil {
		return nil, err
	}
	data, err := json.Marshal(document)
	if err != nil {
		return nil, err
	}
	if len(data) > MaxJournalBytes {
		return nil, fault.New("export_too_large", "手账文字数据超过 64 MiB，请使用实例备份。")
	}
	var buffer bytes.Buffer
	archive := zip.NewWriter(&buffer)
	manifest := BundleManifest{Schema: "animemo.backup/v1", Files: map[string]string{}}
	add := func(name string, content []byte, method uint16) error {
		writer, err := archive.CreateHeader(&zip.FileHeader{Name: name, Method: method})
		if err != nil {
			return err
		}
		if _, err = writer.Write(content); err != nil {
			return err
		}
		sum := sha256.Sum256(content)
		manifest.Files[name] = hex.EncodeToString(sum[:])
		return nil
	}
	if err = add("journal.json", data, zip.Deflate); err != nil {
		return nil, err
	}
	rows, err := tx.Query(ctx, `SELECT c.entry_id,c.revision,c.content_type,c.data FROM entry_covers c JOIN entries e ON e.id=c.entry_id WHERE e.user_id=$1 AND e.deleted_at IS NULL ORDER BY c.entry_id`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var entryID, revision, kind string
		var image []byte
		if err = rows.Scan(&entryID, &revision, &kind, &image); err != nil {
			return nil, err
		}
		if image == nil {
			picture, err := s.storage.Resolve(ctx, revision, media.Image{ContentType: kind})
			if err != nil {
				return nil, err
			}
			image = picture.Data
		}
		ext := "png"
		if kind == "image/jpeg" {
			ext = "jpg"
		}
		if err = add("covers/"+entryID+"."+ext, image, zip.Store); err != nil {
			return nil, err
		}
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	rows.Close()
	manifestData, err := json.Marshal(manifest)
	if err != nil {
		return nil, err
	}
	writer, err := archive.Create("manifest.json")
	if err != nil {
		return nil, err
	}
	if _, err = writer.Write(manifestData); err != nil {
		return nil, err
	}
	if err = archive.Close(); err != nil {
		return nil, err
	}
	if buffer.Len() > MaxImportBytes {
		return nil, fault.New("export_too_large", "备份超过 160 MiB，请使用实例备份。")
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}
