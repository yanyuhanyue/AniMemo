package mediastore

import (
	"context"
	"errors"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Store struct {
	pool *pgxpool.Pool
	r2   *R2
}

func New(pool *pgxpool.Pool, r2 *R2) *Store { return &Store{pool: pool, r2: r2} }
func (s *Store) Initialize(ctx context.Context) error {
	var missing int
	if s.r2 == nil {
		err := s.pool.QueryRow(ctx, `SELECT count(*) FROM media_references r WHERE r.data IS NULL AND NOT EXISTS(SELECT 1 FROM media_migration_items m WHERE m.revision=r.revision AND m.original_data IS NOT NULL)`).Scan(&missing)
		if err != nil {
			return err
		}
		if missing > 0 {
			return errors.New("R2 credentials are required for existing remote images; restore a complete media backup or the original storage configuration")
		}
		return nil
	}
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM media_objects WHERE backend_id<>$1 AND state<>'deleted'`, s.r2.ID).Scan(&missing); err != nil {
		return err
	}
	if missing > 0 {
		return errors.New("R2 bucket identity changed while objects remain; move images back to PostgreSQL and finish cleanup with the original configuration first")
	}
	return nil
}

func releaseLock(conn *pgxpool.Conn, query string, args ...any) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if _, err := conn.Exec(ctx, query, args...); err != nil {
		_ = conn.Conn().Close(ctx)
	}
	conn.Release()
}
func (s *Store) readLock(ctx context.Context) (*pgxpool.Conn, error) {
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	if _, err = conn.Exec(ctx, `SELECT pg_advisory_lock_shared(718226060)`); err != nil {
		conn.Release()
		return nil, err
	}
	return conn, nil
}

// HoldReads prevents collection of retired objects while a database snapshot is exported.
func (s *Store) HoldReads(ctx context.Context) (func(), error) {
	conn, err := s.readLock(ctx)
	if err != nil {
		return nil, err
	}
	return func() { releaseLock(conn, `SELECT pg_advisory_unlock_shared(718226060)`) }, nil
}

// Resolve is called only after the caller's owner/publication query has authorized this revision.
func (s *Store) Resolve(ctx context.Context, revision string, picture media.Image) (media.Image, error) {
	if picture.Data != nil {
		return picture, nil
	}
	conn, err := s.readLock(ctx)
	if err != nil {
		return picture, err
	}
	defer releaseLock(conn, `SELECT pg_advisory_unlock_shared(718226060)`)
	var backend, key, checksum string
	var size int
	err = conn.QueryRow(ctx, `SELECT backend_id,object_key,sha256,byte_size FROM media_objects WHERE revision=$1 AND state IN ('stored','orphan')`, revision).Scan(&backend, &key, &checksum, &size)
	if err != nil {
		return picture, storageIntegrity()
	}
	if s.r2 != nil && backend == s.r2.ID {
		picture.Data, err = s.r2.Get(ctx, key, picture.ContentType, checksum, size)
	} else {
		err = objectFailure()
	}
	if err != nil {
		// A retained migration original also permits recovery while R2 is unavailable.
		var original []byte
		readErr := conn.QueryRow(ctx, `SELECT data FROM media_references WHERE revision=$1 AND data IS NOT NULL UNION ALL SELECT original_data FROM media_migration_items WHERE revision=$1 AND original_data IS NOT NULL LIMIT 1`, revision).Scan(&original)
		if readErr == nil && len(original) == size && digest(original) == checksum {
			picture.Data = original
			return picture, nil
		}
		return picture, err
	}
	return picture, nil
}

type Status struct {
	Backend        string      `json:"backend"`
	Version        int         `json:"version"`
	Configured     bool        `json:"configured"`
	Endpoint       string      `json:"endpoint"`
	Bucket         string      `json:"bucket"`
	PostgresImages int         `json:"postgres_images"`
	RemoteImages   int         `json:"remote_images"`
	MediaBytes     int64       `json:"media_bytes"`
	Pending        int         `json:"pending"`
	CleanupPending int         `json:"cleanup_pending"`
	Failures       int         `json:"failures"`
	OriginalsBytes int64       `json:"originals_bytes"`
	Migrations     []Migration `json:"migrations"`
}
type Migration struct {
	ID        string    `json:"id"`
	Backend   string    `json:"backend"`
	State     string    `json:"state"`
	Total     int       `json:"total"`
	Copied    int       `json:"copied"`
	CreatedAt time.Time `json:"created_at"`
}
type ManifestItem struct {
	Revision         string `json:"revision"`
	Kind             string `json:"kind"`
	Bytes            int    `json:"bytes"`
	SHA256           string `json:"sha256"`
	State            string `json:"state"`
	OriginalRetained bool   `json:"original_retained"`
}

func (s *Store) Status(ctx context.Context) (Status, error) {
	out := Status{Configured: s.r2 != nil, Migrations: []Migration{}}
	if s.r2 != nil {
		out.Endpoint = s.r2.Endpoint
		out.Bucket = s.r2.Bucket
	}
	err := s.pool.QueryRow(ctx, `SELECT backend,version FROM media_settings`).Scan(&out.Backend, &out.Version)
	if err != nil {
		return out, err
	}
	err = s.pool.QueryRow(ctx, `SELECT count(*) FILTER(WHERE data IS NOT NULL),count(*) FILTER(WHERE data IS NULL),coalesce(sum(byte_size),0) FROM media_references`).Scan(&out.PostgresImages, &out.RemoteImages, &out.MediaBytes)
	if err != nil {
		return out, err
	}
	err = s.pool.QueryRow(ctx, `SELECT (SELECT count(*) FROM media_objects WHERE state='orphan'),(SELECT count(*) FROM media_objects WHERE error<>'' AND state<>'deleted'),(SELECT coalesce(sum(octet_length(original_data)),0) FROM media_migration_items)`).Scan(&out.CleanupPending, &out.Failures, &out.OriginalsBytes)
	if err != nil {
		return out, err
	}
	if out.Backend == "r2" {
		out.Pending = out.PostgresImages
	} else {
		out.Pending = out.RemoteImages
	}
	rows, err := s.pool.Query(ctx, `SELECT id,backend,state,(SELECT count(*) FROM media_migration_items WHERE migration_id=m.id),(SELECT count(*) FROM media_migration_items WHERE migration_id=m.id AND state='copied'),created_at FROM media_migrations m ORDER BY created_at DESC LIMIT 10`)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var migration Migration
		if err = rows.Scan(&migration.ID, &migration.Backend, &migration.State, &migration.Total, &migration.Copied, &migration.CreatedAt); err != nil {
			return out, err
		}
		out.Migrations = append(out.Migrations, migration)
	}
	return out, rows.Err()
}
func (s *Store) Manifest(ctx context.Context, migrationID string) ([]ManifestItem, error) {
	if !id.Valid(migrationID) {
		return nil, fault.New("not_found", "没有找到迁移清单。")
	}
	rows, err := s.pool.Query(ctx, `SELECT revision,kind,byte_size,sha256,state,(original_data IS NOT NULL) FROM media_migration_items WHERE migration_id=$1 ORDER BY revision`, migrationID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ManifestItem{}
	for rows.Next() {
		var item ManifestItem
		if err = rows.Scan(&item.Revision, &item.Kind, &item.Bytes, &item.SHA256, &item.State, &item.OriginalRetained); err != nil {
			return nil, err
		}
		out = append(out, item)
	}
	return out, rows.Err()
}
func (s *Store) SetBackend(ctx context.Context, actor, backend string, version int) (Status, error) {
	if backend != "postgres" && backend != "r2" {
		return Status{}, fault.Field("backend", "存储后端无效。")
	}
	if backend == "r2" && s.r2 == nil {
		return Status{}, fault.New("service_unavailable", "部署者尚未配置 R2 专用 bucket 和凭据。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Status{}, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return Status{}, err
	}
	var previous string
	var actual int
	if err = tx.QueryRow(ctx, `SELECT backend,version FROM media_settings FOR UPDATE`).Scan(&previous, &actual); err != nil {
		return Status{}, err
	}
	if version != actual {
		return Status{}, fault.New("version_conflict", "存储设置已经改变，请刷新后重试。")
	}
	if previous == backend {
		if err = tx.Commit(ctx); err != nil {
			return Status{}, err
		}
		return s.Status(ctx)
	}
	backendID := ""
	if s.r2 != nil {
		backendID = s.r2.ID
	}
	if _, err = tx.Exec(ctx, `UPDATE media_settings SET backend=$1,backend_id=$2,version=version+1`, backend, backendID); err != nil {
		return Status{}, err
	}
	if _, err = tx.Exec(ctx, `UPDATE media_migrations SET state='superseded' WHERE state='running'`); err != nil {
		return Status{}, err
	}
	migrationID := id.New()
	if _, err = tx.Exec(ctx, `INSERT INTO media_migrations(id,backend) VALUES($1,$2)`, migrationID, backend); err != nil {
		return Status{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO media_migration_items(migration_id,revision,kind,target_id,owner_id,content_type,byte_size,sha256) SELECT $1,r.revision,r.kind,r.target_id,r.owner_id,r.content_type,r.byte_size,CASE WHEN r.data IS NOT NULL THEN encode(sha256(r.data),'hex') ELSE o.sha256 END FROM media_references r LEFT JOIN media_objects o ON o.revision=r.revision WHERE ($2='r2' AND r.data IS NOT NULL) OR ($2='postgres' AND r.data IS NULL)`, migrationID, backend); err != nil {
		return Status{}, err
	}
	if err = governance.Audit(ctx, tx, actor, "change-media-backend", "instance", "singleton", map[string]string{"backend": backend, "migration": migrationID}); err != nil {
		return Status{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Status{}, err
	}
	return s.Status(ctx)
}

type reference struct {
	Kind, Owner, Target, Revision, ContentType string
	Bytes                                      int
	Data                                       []byte
}

func imageFor(ref reference) media.Image {
	return media.Image{ContentType: ref.ContentType, Data: ref.Data}
}
func readReference(row pgx.Row) (reference, error) {
	var out reference
	err := row.Scan(&out.Kind, &out.Owner, &out.Target, &out.Revision, &out.ContentType, &out.Bytes, &out.Data)
	return out, err
}

const referenceFields = `kind,owner_id,target_id,revision,content_type,byte_size,data`

func updateReference(ctx context.Context, tx pgx.Tx, ref reference, data []byte) (bool, error) {
	table, key := "", ""
	switch ref.Kind {
	case "entry":
		table, key = "entry_covers", "entry_id"
	case "avatar":
		table, key = "avatars", "user_id"
	case "column":
		table, key = "column_covers", "column_id"
	default:
		return false, storageIntegrity()
	}
	tag, err := tx.Exec(ctx, `UPDATE `+table+` SET data=$3 WHERE `+key+`=$1 AND revision=$2`, ref.Target, ref.Revision, data)
	return tag.RowsAffected() == 1, err
}
