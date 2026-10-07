package mediastore

import (
	"archive/zip"
	"context"
	"encoding/json"
	"errors"
	"io"

	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
)

type archiveItem struct {
	Revision    string `json:"revision"`
	ContentType string `json:"content_type"`
	SHA256      string `json:"sha256"`
	Bytes       int    `json:"bytes"`
}
type archiveManifest struct {
	Schema string        `json:"schema"`
	Items  []archiveItem `json:"items"`
}

// Export copies remote bodies into a portable archive. The operator stops the app
// before pg_dump and Export so the two files describe the same database state.
func (s *Store) Export(ctx context.Context, writer io.Writer) error {
	release, err := s.HoldReads(ctx)
	if err != nil {
		return err
	}
	defer release()
	rows, err := s.pool.Query(ctx, `SELECT r.revision,r.content_type,o.sha256,r.byte_size FROM media_references r LEFT JOIN media_objects o ON o.revision=r.revision WHERE r.data IS NULL ORDER BY r.revision`)
	if err != nil {
		return err
	}
	manifest := archiveManifest{Schema: "animemo.media/v1", Items: []archiveItem{}}
	for rows.Next() {
		var item archiveItem
		if err = rows.Scan(&item.Revision, &item.ContentType, &item.SHA256, &item.Bytes); err != nil {
			rows.Close()
			return err
		}
		manifest.Items = append(manifest.Items, item)
	}
	rows.Close()
	if err = rows.Err(); err != nil {
		return err
	}
	archive := zip.NewWriter(writer)
	for _, item := range manifest.Items {
		picture, err := s.Resolve(ctx, item.Revision, media.Image{ContentType: item.ContentType})
		if err != nil {
			return err
		}
		if len(picture.Data) != item.Bytes || digest(picture.Data) != item.SHA256 {
			return storageIntegrity()
		}
		file, err := archive.CreateHeader(&zip.FileHeader{Name: item.Revision + ".bin", Method: zip.Store})
		if err != nil {
			return err
		}
		if _, err = file.Write(picture.Data); err != nil {
			return err
		}
	}
	file, err := archive.Create("manifest.json")
	if err != nil {
		return err
	}
	if err = json.NewEncoder(file).Encode(manifest); err != nil {
		return err
	}
	return archive.Close()
}

// Restore hydrates a freshly restored database without touching the source R2
// namespace. Validation and all writes commit atomically. Run with the app stopped.
func (s *Store) Restore(ctx context.Context, reader io.ReaderAt, size int64) error {
	archive, err := zip.NewReader(reader, size)
	if err != nil {
		return err
	}
	files := make(map[string]*zip.File, len(archive.File))
	for _, file := range archive.File {
		if files[file.Name] != nil || (file.Name != "manifest.json" && (len(file.Name) != 40 || !id.Valid(file.Name[:36]) || file.Name[36:] != ".bin")) {
			return errors.New("invalid or duplicate media archive filename")
		}
		files[file.Name] = file
	}
	mf := files["manifest.json"]
	if mf == nil || mf.UncompressedSize64 > 32<<20 {
		return errors.New("invalid media archive manifest")
	}
	body, err := mf.Open()
	if err != nil {
		return err
	}
	var manifest archiveManifest
	decoder := json.NewDecoder(io.LimitReader(body, 32<<20))
	decoder.DisallowUnknownFields()
	err = decoder.Decode(&manifest)
	if err == nil {
		var extra any
		if decoder.Decode(&extra) != io.EOF {
			err = errors.New("trailing manifest content")
		}
	}
	body.Close()
	if err != nil {
		return err
	}
	if manifest.Schema != "animemo.media/v1" || len(files) != len(manifest.Items)+1 {
		return errors.New("invalid media archive inventory")
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(718226060)`); err != nil {
		return err
	}
	var missing int
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM media_references WHERE data IS NULL`).Scan(&missing); err != nil {
		return err
	}
	if missing != len(manifest.Items) {
		return errors.New("media archive does not match the restored database")
	}
	seen := map[string]bool{}
	for _, item := range manifest.Items {
		if !id.Valid(item.Revision) || seen[item.Revision] || item.Bytes < 1 || item.Bytes > media.MaxBytes {
			return storageIntegrity()
		}
		seen[item.Revision] = true
		file := files[item.Revision+".bin"]
		if file == nil || file.UncompressedSize64 != uint64(item.Bytes) {
			return storageIntegrity()
		}
		ref, err := readReference(tx.QueryRow(ctx, `SELECT `+referenceFields+` FROM media_references WHERE revision=$1 AND data IS NULL`, item.Revision))
		if err != nil {
			return err
		}
		var checksum string
		if err = tx.QueryRow(ctx, `SELECT sha256 FROM media_objects WHERE revision=$1`, item.Revision).Scan(&checksum); err != nil {
			return err
		}
		if ref.ContentType != item.ContentType || ref.Bytes != item.Bytes || checksum != item.SHA256 {
			return storageIntegrity()
		}
		body, err := file.Open()
		if err != nil {
			return err
		}
		data, err := io.ReadAll(io.LimitReader(body, int64(item.Bytes)+1))
		body.Close()
		if err != nil {
			return err
		}
		if len(data) != item.Bytes || digest(data) != checksum {
			return storageIntegrity()
		}
		if _, err = media.Validate(data, item.ContentType); err != nil {
			return err
		}
		changed, err := updateReference(ctx, tx, ref, data)
		if err != nil {
			return err
		}
		if !changed {
			return storageIntegrity()
		}
	}
	// A restored clone owns no objects in the source bucket, even if credentials
	// are configured later. Never turn its historical keys into deletion jobs.
	if _, err = tx.Exec(ctx, `DELETE FROM media_objects; UPDATE media_settings SET backend='postgres',backend_id='',namespace=gen_random_uuid(),version=version+1; UPDATE media_migrations SET state='superseded' WHERE state='running'; UPDATE media_migration_items SET original_data=NULL;`); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
