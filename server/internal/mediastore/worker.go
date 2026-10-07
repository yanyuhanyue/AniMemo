package mediastore

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func (s *Store) Run(ctx context.Context) {
	for ctx.Err() == nil {
		worked, err := s.ProcessNext(ctx)
		if err != nil && ctx.Err() == nil {
			slog.Error("media worker failed", "error_type", "database_or_storage_failure")
		}
		if !worked || err != nil {
			timer := time.NewTimer(500 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
		}
	}
}
func (s *Store) revisionLock(ctx context.Context, revision string) (*pgxpool.Conn, bool, error) {
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return nil, false, err
	}
	var locked bool
	err = conn.QueryRow(ctx, `SELECT pg_try_advisory_lock(hashtextextended($1,718226061))`, revision).Scan(&locked)
	if err != nil || !locked {
		conn.Release()
		return nil, false, err
	}
	return conn, true, nil
}
func (s *Store) ProcessNext(ctx context.Context) (bool, error) {
	// Keep manifests truthful when normal edits replace or remove a queued revision.
	if _, err := s.pool.Exec(ctx, `UPDATE media_migration_items m SET state='superseded',original_data=NULL WHERE state='pending' AND NOT EXISTS(SELECT 1 FROM media_references r WHERE r.revision=m.revision)`); err != nil {
		return false, err
	}
	if _, err := s.pool.Exec(ctx, `UPDATE media_migrations m SET state='done' WHERE state='running' AND NOT EXISTS(SELECT 1 FROM media_migration_items WHERE migration_id=m.id AND state='pending')`); err != nil {
		return false, err
	}
	if s.r2 != nil {
		if worked, err := s.collectOne(ctx); worked || err != nil {
			return worked, err
		}
	}
	var backend, namespace, backendID string
	if err := s.pool.QueryRow(ctx, `SELECT backend,namespace,backend_id FROM media_settings`).Scan(&backend, &namespace, &backendID); err != nil {
		return false, err
	}
	if backend == "r2" && (s.r2 == nil || s.r2.ID != backendID) {
		return false, nil
	}
	ref, err := readReference(s.pool.QueryRow(ctx, `SELECT `+referenceFields+` FROM media_references r WHERE (($1='r2' AND data IS NOT NULL) OR ($1='postgres' AND data IS NULL)) AND NOT EXISTS(SELECT 1 FROM media_objects o WHERE o.revision=r.revision AND o.available_at>now()) ORDER BY revision LIMIT 1`, backend))
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	conn, locked, err := s.revisionLock(ctx, ref.Revision)
	if err != nil || !locked {
		return false, err
	}
	defer releaseLock(conn, `SELECT pg_advisory_unlock(hashtextextended($1,718226061))`, ref.Revision)
	// Shared with readers/backups, exclusive against object collection and offline restore.
	if _, err = conn.Exec(ctx, `SELECT pg_advisory_lock_shared(718226060)`); err != nil {
		return false, err
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if _, err := conn.Exec(cleanup, `SELECT pg_advisory_unlock_shared(718226060)`); err != nil {
			_ = conn.Conn().Close(cleanup)
		}
	}()
	operation, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	if backend == "r2" {
		err = s.moveToR2(operation, ref, namespace, backendID)
	} else {
		err = s.moveToPostgres(operation, ref)
	}
	if err != nil && ctx.Err() == nil {
		_, saveErr := s.pool.Exec(ctx, `UPDATE media_objects SET attempts=attempts+1,error='存储操作未完成；原图或远端对象已保留，将重试。',available_at=now()+make_interval(secs=>least(300,power(2,least(attempts+1,8)))::double precision),updated_at=now() WHERE revision=$1 AND state<>'deleted'`, ref.Revision)
		return true, saveErr
	}
	return true, err
}

func (s *Store) moveToR2(ctx context.Context, ref reference, namespace, backendID string) error {
	// The image already occupies its owner's database quota. This row reserves
	// the immutable remote key before any network write, including uncertain PUTs.
	key := "animemo/" + namespace + "/" + ref.Owner + "/" + ref.Revision
	checksum := digest(ref.Data)
	tag, err := s.pool.Exec(ctx, `INSERT INTO media_objects(revision,owner_id,kind,target_id,backend_id,object_key,content_type,byte_size,sha256,state) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'uploading') ON CONFLICT(revision) DO UPDATE SET state='uploading',backend_id=excluded.backend_id,object_key=excluded.object_key,updated_at=now() WHERE media_objects.sha256=excluded.sha256`, ref.Revision, ref.Owner, ref.Kind, ref.Target, backendID, key, ref.ContentType, ref.Bytes, checksum)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 || len(ref.Data) != ref.Bytes {
		return storageIntegrity()
	}
	if err = s.r2.Put(ctx, key, ref.ContentType, ref.Data); err != nil {
		return err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var current, physical string
	if err = tx.QueryRow(ctx, `SELECT backend,backend_id FROM media_settings FOR SHARE`).Scan(&current, &physical); err != nil {
		return err
	}
	committed := false
	if current == "r2" && physical == backendID {
		committed, err = updateReference(ctx, tx, ref, nil)
		if err != nil {
			return err
		}
	}
	if committed {
		// Only pre-existing images in the operator's migration manifest retain a
		// rollback copy. New uploads release their temporary database body after verification.
		if _, err = tx.Exec(ctx, `UPDATE media_migration_items SET original_data=$2,state='copied' WHERE revision=$1 AND sha256=$3 AND state='pending' AND migration_id IN(SELECT id FROM media_migrations WHERE backend='r2' AND state='running')`, ref.Revision, ref.Data, checksum); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE media_objects SET state='stored',attempts=0,error='',available_at=now(),updated_at=now() WHERE revision=$1`, ref.Revision)
	} else {
		_, err = tx.Exec(ctx, `UPDATE media_objects SET state='orphan',available_at=now()+interval '2 minutes',updated_at=now() WHERE revision=$1`, ref.Revision)
	}
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (s *Store) moveToPostgres(ctx context.Context, ref reference) error {
	var data []byte
	var checksum string
	err := s.pool.QueryRow(ctx, `SELECT sha256 FROM media_objects WHERE revision=$1`, ref.Revision).Scan(&checksum)
	if err != nil {
		return err
	}
	err = s.pool.QueryRow(ctx, `SELECT original_data FROM media_migration_items WHERE revision=$1 AND original_data IS NOT NULL AND sha256=$2 LIMIT 1`, ref.Revision, checksum).Scan(&data)
	if errors.Is(err, pgx.ErrNoRows) {
		picture, readErr := s.Resolve(ctx, ref.Revision, imageFor(ref))
		if readErr != nil {
			return readErr
		}
		data = picture.Data
	} else if err != nil {
		return err
	}
	if len(data) != ref.Bytes || digest(data) != checksum {
		return storageIntegrity()
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var backend string
	if err = tx.QueryRow(ctx, `SELECT backend FROM media_settings FOR SHARE`).Scan(&backend); err != nil {
		return err
	}
	if backend != "postgres" {
		return nil
	}
	committed, err := updateReference(ctx, tx, ref, data)
	if err != nil {
		return err
	}
	if committed {
		if _, err = tx.Exec(ctx, `UPDATE media_migration_items SET state='copied' WHERE revision=$1 AND migration_id IN(SELECT id FROM media_migrations WHERE backend='postgres' AND state='running')`, ref.Revision); err != nil {
			return err
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE media_objects SET state='orphan',available_at=now()+interval '2 minutes',error='',attempts=0,updated_at=now() WHERE revision=$1`, ref.Revision); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (s *Store) collectOne(ctx context.Context) (bool, error) {
	var revision string
	err := s.pool.QueryRow(ctx, `SELECT revision FROM media_objects o WHERE backend_id=$1 AND state IN ('orphan','uploading') AND available_at<=now() AND updated_at<now()-interval '2 minutes' AND NOT EXISTS(SELECT 1 FROM media_references r WHERE r.revision=o.revision AND r.data IS NULL) AND (state='orphan' OR NOT EXISTS(SELECT 1 FROM media_references r WHERE r.revision=o.revision)) ORDER BY created_at LIMIT 1`, s.r2.ID).Scan(&revision)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	conn, locked, err := s.revisionLock(ctx, revision)
	if err != nil || !locked {
		return false, err
	}
	defer releaseLock(conn, `SELECT pg_advisory_unlock(hashtextextended($1,718226061))`, revision)
	var exclusive bool
	if err = conn.QueryRow(ctx, `SELECT pg_try_advisory_lock(718226060)`).Scan(&exclusive); err != nil || !exclusive {
		return false, err
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if _, err := conn.Exec(cleanup, `SELECT pg_advisory_unlock(718226060)`); err != nil {
			_ = conn.Conn().Close(cleanup)
		}
	}()
	var key string
	var referenced bool
	err = conn.QueryRow(ctx, `SELECT object_key,EXISTS(SELECT 1 FROM media_references r WHERE r.revision=o.revision AND r.data IS NULL) FROM media_objects o WHERE revision=$1 AND state IN ('orphan','uploading')`, revision).Scan(&key, &referenced)
	if errors.Is(err, pgx.ErrNoRows) || referenced {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if err = s.r2.Delete(ctx, key); err != nil {
		_, saveErr := conn.Exec(ctx, `UPDATE media_objects SET state='orphan',error='旧图清理未完成，将重试。',attempts=attempts+1,available_at=now()+interval '30 seconds' WHERE revision=$1`, revision)
		return true, saveErr
	}
	_, err = conn.Exec(ctx, `UPDATE media_objects SET state='deleted',error='',updated_at=now() WHERE revision=$1`, revision)
	return true, err
}
