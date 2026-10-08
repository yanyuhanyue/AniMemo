package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"github.com/jackc/pgx/v5"
	"strings"
	"time"
)

type CollectionItem struct {
	Kind string `json:"kind"`
	ID   string `json:"id"`
}
type CollectionInput struct {
	Version     int              `json:"version"`
	Title       string           `json:"title"`
	Description string           `json:"description"`
	Items       []CollectionItem `json:"items"`
	Visibility  string           `json:"visibility"`
}
type MemoryCollection struct {
	CollectionInput
	ID        string    `json:"id"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

const collectionColumns = `id,title,description,items,visibility,version,created_at,updated_at`

func scanCollection(row pgx.Row) (MemoryCollection, error) {
	var c MemoryCollection
	err := row.Scan(&c.ID, &c.Title, &c.Description, &c.Items, &c.Visibility, &c.Version, &c.CreatedAt, &c.UpdatedAt)
	return c, libraryMissing(err)
}
func (s *Service) Collections(ctx context.Context, owner string) ([]MemoryCollection, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+collectionColumns+` FROM memory_collections WHERE owner_id=$1 ORDER BY updated_at DESC,id DESC LIMIT 500`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []MemoryCollection{}
	for rows.Next() {
		c, err := scanCollection(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}
func collectionReference(ctx context.Context, tx pgx.Tx, owner string, item CollectionItem) error {
	if !id.Valid(item.ID) {
		return fault.Field("items", "收藏引用无效。")
	}
	table := ""
	switch item.Kind {
	case "anime":
		table = "anime_resources"
	case "character":
		table = "characters"
	case "note", "moment":
		table = "memory_notes"
	default:
		return fault.Field("items", "收藏类型无效。")
	}
	var exists bool
	extra := ""
	args := []any{owner, item.ID}
	if table == "memory_notes" {
		extra = " AND kind=$3 AND deleted_at IS NULL"
		args = append(args, item.Kind)
	}
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM `+table+` WHERE owner_id=$1 AND id=$2`+extra+`)`, args...).Scan(&exists); err != nil {
		return err
	}
	if !exists {
		return fault.Field("items", "收藏中的资源不存在或不属于当前账号。")
	}
	return nil
}
func (s *Service) SaveCollection(ctx context.Context, owner, resource string, in CollectionInput) (MemoryCollection, error) {
	in.Title = strings.TrimSpace(in.Title)
	if in.Items == nil {
		in.Items = []CollectionItem{}
	}
	if in.Visibility == "" {
		in.Visibility = "private"
	}
	if in.Title == "" || !libraryText(in.Title, 160) || !libraryText(in.Description, 4000) || len(in.Items) > 500 || !libraryVisibility(in.Visibility) {
		return MemoryCollection{}, fault.Field("title", "收藏夹标题、说明或项目数量无效（最多 500 项）。")
	}
	if resource != "" && !id.Valid(resource) {
		return MemoryCollection{}, fault.New("not_found", "收藏夹不存在。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return MemoryCollection{}, err
	}
	defer tx.Rollback(context.Background())
	action := "created"
	if resource != "" {
		old, err := scanCollection(tx.QueryRow(ctx, `SELECT `+collectionColumns+` FROM memory_collections WHERE owner_id=$1 AND id=$2 FOR UPDATE`, owner, resource))
		if err != nil {
			return old, err
		}
		if err = libraryVersion(old.Version, in.Version); err != nil {
			return old, err
		}
		action = "changed"
	} else {
		resource = id.New()
		var count int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM memory_collections WHERE owner_id=$1`, owner).Scan(&count); err != nil {
			return MemoryCollection{}, err
		}
		if count >= 500 {
			return MemoryCollection{}, fault.Field("title", "收藏夹已达 500 个上限。")
		}
	}
	seen := map[string]bool{}
	for _, item := range in.Items {
		key := item.Kind + item.ID
		if seen[key] {
			return MemoryCollection{}, fault.Field("items", "同一资源不能重复加入。")
		}
		seen[key] = true
		if err = collectionReference(ctx, tx, owner, item); err != nil {
			return MemoryCollection{}, err
		}
	}
	c, err := scanCollection(tx.QueryRow(ctx, `INSERT INTO memory_collections(id,owner_id,title,description,items,visibility) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(id) DO UPDATE SET title=excluded.title,description=excluded.description,items=excluded.items,visibility=excluded.visibility,version=memory_collections.version+1,updated_at=now() RETURNING `+collectionColumns, resource, owner, in.Title, in.Description, in.Items, in.Visibility))
	if err != nil {
		return c, err
	}
	if err = libraryRevision(ctx, tx, owner, c.ID, "collection", action, c); err != nil {
		return c, err
	}
	return c, tx.Commit(ctx)
}
func (s *Service) DeleteCollection(ctx context.Context, owner, resource string, version int) error {
	if !id.Valid(resource) {
		return fault.New("not_found", "收藏夹不存在。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	c, err := scanCollection(tx.QueryRow(ctx, `SELECT `+collectionColumns+` FROM memory_collections WHERE owner_id=$1 AND id=$2 FOR UPDATE`, owner, resource))
	if err != nil {
		return err
	}
	if err = libraryVersion(c.Version, version); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM memory_collections WHERE owner_id=$1 AND id=$2`, owner, resource); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM memory_share_tokens WHERE owner_id=$1 AND resource_id=$2`, owner, resource); err != nil {
		return err
	}
	if err = libraryRevision(ctx, tx, owner, resource, "collection", "removed", c); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
