package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"github.com/jackc/pgx/v5"
	"strings"
	"time"
)

type MemoryAnchor struct {
	Kind             string `json:"kind"`
	TimestampSeconds *int   `json:"timestamp_seconds"`
	Quote            string `json:"quote"`
	Scene            string `json:"scene"`
}
type MemoryNoteInput struct {
	Version       int          `json:"version"`
	Kind          string       `json:"kind"`
	Title         string       `json:"title"`
	Body          string       `json:"body"`
	AnimeID       string       `json:"anime_id"`
	CharacterID   string       `json:"character_id"`
	EpisodeID     string       `json:"episode_id"`
	WatchID       string       `json:"watch_id"`
	Anchor        MemoryAnchor `json:"anchor"`
	MediaIDs      []string     `json:"media_ids"`
	Tags          []string     `json:"tags"`
	OccurredOn    string       `json:"occurred_on"`
	TimePrecision string       `json:"time_precision"`
	Visibility    string       `json:"visibility"`
	Spoiler       bool         `json:"spoiler"`
	Highlight     bool         `json:"highlight"`
}
type MemoryNote struct {
	MemoryNoteInput
	ID           string     `json:"id"`
	CreatedAt    time.Time  `json:"created_at"`
	UpdatedAt    time.Time  `json:"updated_at"`
	DeletedAt    *time.Time `json:"deleted_at"`
	WatchMissing bool       `json:"watch_missing"`
}

const noteColumns = `id,kind,title,body,coalesce(anime_id::text,''),coalesce(character_id::text,''),coalesce(episode_id::text,''),coalesce(watch_id::text,''),anchor,media_ids,tags,occurred_on,time_precision,visibility,spoiler,highlight,version,created_at,updated_at,deleted_at,watch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM watch_records WHERE id=memory_notes.watch_id)`

func scanNote(row pgx.Row) (MemoryNote, error) {
	var n MemoryNote
	err := row.Scan(&n.ID, &n.Kind, &n.Title, &n.Body, &n.AnimeID, &n.CharacterID, &n.EpisodeID, &n.WatchID, &n.Anchor, &n.MediaIDs, &n.Tags, &n.OccurredOn, &n.TimePrecision, &n.Visibility, &n.Spoiler, &n.Highlight, &n.Version, &n.CreatedAt, &n.UpdatedAt, &n.DeletedAt, &n.WatchMissing)
	return n, libraryMissing(err)
}
func (n *MemoryNoteInput) validate() error {
	n.Title = strings.TrimSpace(n.Title)
	if n.Kind == "" {
		n.Kind = "note"
	}
	if n.Visibility == "" {
		n.Visibility = "private"
	}
	if n.TimePrecision == "" {
		n.TimePrecision = "unknown"
	}
	if n.MediaIDs == nil {
		n.MediaIDs = []string{}
	}
	if n.Tags == nil {
		n.Tags = []string{}
	}
	if n.Title == "" || !libraryText(n.Title, 160) || !libraryText(n.Body, 40000) || !libraryTags(n.Tags, 20) || !libraryIDs(n.MediaIDs, 8) {
		return fault.Field("title", "标题最多 160 字，正文最多 40000 字，附件最多 8 个且不能重复。")
	}
	if n.Kind != "note" && n.Kind != "moment" {
		return fault.Field("kind", "记忆类型无效。")
	}
	if n.Kind == "moment" && len(n.MediaIDs) == 0 {
		return fault.Field("media_ids", "记忆瞬间需要 1–8 张图片。")
	}
	if !libraryVisibility(n.Visibility) || !validMemoryTime(n.OccurredOn, n.TimePrecision) {
		return fault.Field("occurred_on", "请核对可见性与日期精度；未知日期应留空。")
	}
	for _, value := range []string{n.AnimeID, n.CharacterID, n.EpisodeID, n.WatchID} {
		if value != "" && !id.Valid(value) {
			return fault.Field("anime_id", "记忆关联无效。")
		}
	}
	if n.Anchor.Kind != "" && n.Anchor.Kind != "episode" && n.Anchor.Kind != "timestamp" && n.Anchor.Kind != "quote" && n.Anchor.Kind != "scene" && n.Anchor.Kind != "freeform" {
		return fault.Field("anchor", "定位类型无效。")
	}
	if !libraryText(n.Anchor.Quote, 4000) || !libraryText(n.Anchor.Scene, 4000) || (n.Anchor.TimestampSeconds != nil && (*n.Anchor.TimestampSeconds < 0 || *n.Anchor.TimestampSeconds > 86400)) {
		return fault.Field("anchor", "定位信息超过限制。")
	}
	return nil
}
func noteReferences(ctx context.Context, tx pgx.Tx, owner string, n MemoryNoteInput, previous *MemoryNote) error {
	var valid bool
	err := tx.QueryRow(ctx, `SELECT
 ($2='' OR EXISTS(SELECT 1 FROM anime_resources WHERE owner_id=$1 AND id=nullif($2,'')::uuid)) AND
 ($3='' OR EXISTS(SELECT 1 FROM characters WHERE owner_id=$1 AND id=nullif($3,'')::uuid)) AND
 ($4='' OR EXISTS(SELECT 1 FROM episodes WHERE owner_id=$1 AND id=nullif($4,'')::uuid AND ($2='' OR anime_id=nullif($2,'')::uuid)))`, owner, n.AnimeID, n.CharacterID, n.EpisodeID).Scan(&valid)
	if err != nil {
		return err
	}
	if !valid {
		return fault.Field("anime_id", "关联资源不存在或不属于当前账号。")
	}
	// Retain a previously valid missing watch locator when its fact is retracted.
	if n.WatchID != "" && (previous == nil || previous.WatchID != n.WatchID || previous.AnimeID != n.AnimeID) {
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE w.id=$2 AND e.user_id=$1 AND ($3='' OR e.anime_id=nullif($3,'')::uuid))`, owner, n.WatchID, n.AnimeID).Scan(&valid); err != nil {
			return err
		}
		if !valid {
			return fault.Field("watch_id", "观看上下文不存在或属于另一作品。")
		}
	}
	for _, media := range n.MediaIDs {
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM private_memory_media WHERE owner_id=$1 AND id=$2 AND state='ready')`, owner, media).Scan(&valid); err != nil {
			return err
		}
		if !valid {
			retained := false
			if previous != nil {
				for _, old := range previous.MediaIDs {
					if old == media {
						retained = true
					}
				}
			}
			if !retained {
				return fault.Field("media_ids", "图片尚未上传完成、已删除或不属于当前账号。")
			}
		}
	}
	return nil
}
func (s *Service) Notes(ctx context.Context, owner string, f LibraryFilter) (LibraryPage[MemoryNote], error) {
	out := LibraryPage[MemoryNote]{Items: []MemoryNote{}, Page: libraryPage(f.Page), PageSize: 30}
	if (f.AnimeID != "" && !id.Valid(f.AnimeID)) || (f.CharacterID != "" && !id.Valid(f.CharacterID)) {
		return out, fault.Field("anime_id", "筛选无效。")
	}
	where := ` FROM memory_notes WHERE owner_id=$1 AND deleted_at IS NULL AND ($2='' OR kind=$2) AND ($3='' OR anime_id=nullif($3,'')::uuid) AND ($4='' OR character_id IN (WITH RECURSIVE family AS (SELECT id FROM characters WHERE owner_id=$1 AND id=nullif($4,'')::uuid UNION SELECT c.id FROM characters c JOIN family f ON c.redirect_id=f.id WHERE c.owner_id=$1) SELECT id FROM family)) AND ($5='' OR title ILIKE $6 OR body ILIKE $6 OR array_to_string(tags,' ') ILIKE $6) AND (NOT $7 OR highlight) AND ($8='' OR left(occurred_on,4)=$8)`
	args := []any{owner, f.Kind, f.AnimeID, f.CharacterID, f.Search, "%" + strings.NewReplacer("%", "\\%", "_", "\\_").Replace(f.Search) + "%", f.Highlight, f.Year}
	if err := s.pool.QueryRow(ctx, `SELECT count(*)`+where, args...).Scan(&out.Total); err != nil {
		return out, err
	}
	args = append(args, (out.Page-1)*30)
	rows, err := s.pool.Query(ctx, `SELECT `+noteColumns+where+` ORDER BY updated_at DESC,id DESC LIMIT 30 OFFSET $9`, args...)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		n, err := scanNote(rows)
		if err != nil {
			return out, err
		}
		out.Items = append(out.Items, n)
	}
	return out, rows.Err()
}
func (s *Service) Note(ctx context.Context, owner, resource string) (MemoryNote, error) {
	if !id.Valid(resource) {
		return MemoryNote{}, fault.New("not_found", "记忆不存在。")
	}
	return scanNote(s.pool.QueryRow(ctx, `SELECT `+noteColumns+` FROM memory_notes WHERE owner_id=$1 AND id=$2 AND deleted_at IS NULL`, owner, resource))
}
func (s *Service) SaveNote(ctx context.Context, owner, resource string, input MemoryNoteInput) (MemoryNote, error) {
	if err := input.validate(); err != nil {
		return MemoryNote{}, err
	}
	if resource != "" && !id.Valid(resource) {
		return MemoryNote{}, fault.New("not_found", "记忆不存在。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return MemoryNote{}, err
	}
	defer tx.Rollback(context.Background())
	action := "created"
	var old *MemoryNote
	if resource != "" {
		n, err := scanNote(tx.QueryRow(ctx, `SELECT `+noteColumns+` FROM memory_notes WHERE owner_id=$1 AND id=$2 AND deleted_at IS NULL FOR UPDATE`, owner, resource))
		if err != nil {
			return n, err
		}
		if err = libraryVersion(n.Version, input.Version); err != nil {
			return n, err
		}
		old = &n
		action = "changed"
	} else {
		resource = id.New()
	}
	if err = noteReferences(ctx, tx, owner, input, old); err != nil {
		return MemoryNote{}, err
	}
	var count int
	if old == nil {
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM memory_notes WHERE owner_id=$1`, owner).Scan(&count); err != nil {
			return MemoryNote{}, err
		}
		if count >= 10000 {
			return MemoryNote{}, fault.Field("title", "记忆已达 10000 条直接管理上限。")
		}
	}
	args := []any{resource, owner, input.Kind, input.Title, input.Body, input.AnimeID, input.CharacterID, input.EpisodeID, input.WatchID, input.Anchor, input.MediaIDs, input.Tags, input.OccurredOn, input.TimePrecision, input.Visibility, input.Spoiler, input.Highlight}
	n, err := scanNote(tx.QueryRow(ctx, `INSERT INTO memory_notes(id,owner_id,kind,title,body,anime_id,character_id,episode_id,watch_id,anchor,media_ids,tags,occurred_on,time_precision,visibility,spoiler,highlight) VALUES($1,$2,$3,$4,$5,nullif($6,'')::uuid,nullif($7,'')::uuid,nullif($8,'')::uuid,nullif($9,'')::uuid,$10,$11,$12,$13,$14,$15,$16,$17) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,title=excluded.title,body=excluded.body,anime_id=excluded.anime_id,character_id=excluded.character_id,episode_id=excluded.episode_id,watch_id=excluded.watch_id,anchor=excluded.anchor,media_ids=excluded.media_ids,tags=excluded.tags,occurred_on=excluded.occurred_on,time_precision=excluded.time_precision,visibility=excluded.visibility,spoiler=excluded.spoiler,highlight=excluded.highlight,version=memory_notes.version+1,updated_at=now() RETURNING `+noteColumns, args...))
	if err != nil {
		return n, err
	}
	if _, err = tx.Exec(ctx, `UPDATE private_memory_media SET expires_at=NULL WHERE owner_id=$1 AND id=ANY($2::uuid[])`, owner, input.MediaIDs); err != nil {
		return n, err
	}
	if err = libraryRevision(ctx, tx, owner, n.ID, n.Kind, action, n); err != nil {
		return n, err
	}
	return n, tx.Commit(ctx)
}
func (s *Service) DeleteNote(ctx context.Context, owner, resource string, version int) error {
	if !id.Valid(resource) {
		return fault.New("not_found", "记忆不存在。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	n, err := scanNote(tx.QueryRow(ctx, `SELECT `+noteColumns+` FROM memory_notes WHERE owner_id=$1 AND id=$2 AND deleted_at IS NULL FOR UPDATE`, owner, resource))
	if err != nil {
		return err
	}
	if err = libraryVersion(n.Version, version); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE memory_notes SET deleted_at=now(),version=version+1,updated_at=now() WHERE id=$1`, resource); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM memory_share_tokens WHERE owner_id=$1 AND resource_id=$2`, owner, resource); err != nil {
		return err
	}
	if err = libraryRevision(ctx, tx, owner, resource, n.Kind, "removed", n); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
