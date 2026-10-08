package journal

import (
	"animemo.local/server/internal/telemetry"
	"context"
	"errors"
	"strings"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
)

type PublicEntry struct {
	Slug            string   `json:"slug"`
	Title           string   `json:"title"`
	OriginalTitle   string   `json:"original_title"`
	Format          string   `json:"format"`
	Status          string   `json:"status"`
	TotalEpisodes   int      `json:"total_episodes"`
	WatchedEpisodes int      `json:"watched_episodes"`
	Score           *float64 `json:"score"`
	Notes           string   `json:"notes"`
	Tags            []string `json:"tags"`
	Accent          string   `json:"accent"`
	Details         Details  `json:"details"`
	CoverRevision   *string  `json:"cover_revision"`
}
type PublicOwner struct {
	Slug           string  `json:"slug"`
	Name           string  `json:"name"`
	Bio            string  `json:"bio"`
	Accent         string  `json:"accent"`
	AvatarRevision *string `json:"avatar_revision"`
	Entries        int     `json:"entries"`
}
type PublicItem struct {
	Entry PublicEntry `json:"entry"`
	Owner PublicOwner `json:"owner"`
}
type PublicPage struct {
	Owner    *PublicOwner `json:"owner"`
	Items    []PublicItem `json:"items"`
	Total    int          `json:"total"`
	Page     int          `json:"page"`
	PageSize int          `json:"page_size"`
}
type Directory struct {
	Items []PublicOwner `json:"items"`
	Total int           `json:"total"`
	Page  int           `json:"page"`
}

const publicOwnerFields = `u.public_slug,u.display_name,u.bio,u.accent,CASE WHEN u.public_state='published' THEN (SELECT revision FROM avatars WHERE user_id=u.id) ELSE NULL END,CASE WHEN u.public_state='published' THEN (SELECT count(*) FROM entries WHERE user_id=u.id AND visibility='public' AND NOT moderated_hidden AND deleted_at IS NULL) ELSE 0 END`
const publicOwnerPredicate = `u.sharing_enabled AND u.public_state='published' AND NOT u.disabled`

func scanPublicOwner(row pgx.Row) (PublicOwner, error) {
	var o PublicOwner
	err := row.Scan(&o.Slug, &o.Name, &o.Bio, &o.Accent, &o.AvatarRevision, &o.Entries)
	if errors.Is(err, pgx.ErrNoRows) {
		err = fault.New("not_found", "没有找到公开手账。")
	}
	return o, err
}
func projectPublic(e Entry) PublicEntry {
	return PublicEntry{Slug: e.ShareSlug, Title: e.Title, OriginalTitle: e.OriginalTitle, Format: e.Format, Status: e.Status, TotalEpisodes: e.TotalEpisodes, WatchedEpisodes: e.WatchedEpisodes, Score: e.Score, Notes: e.Notes, Tags: e.Tags, Accent: e.Accent, Details: e.Details, CoverRevision: e.CoverRevision}
}

func (s *Service) Directory(ctx context.Context, search string, page int) (Directory, error) {
	f := Filter{Search: search, Page: page, PageSize: 12}
	if err := f.Validate(); err != nil {
		return Directory{}, err
	}
	out := Directory{Items: []PublicOwner{}, Page: page}
	tx, err := telemetry.Begin(ctx, s.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	search = "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(f.Search) + "%"
	where := ` FROM users u WHERE ` + publicOwnerPredicate + ` AND u.display_name ILIKE $1`
	if err = tx.QueryRow(ctx, `SELECT count(*)`+where, search).Scan(&out.Total); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT `+publicOwnerFields+where+` ORDER BY u.display_name,u.id LIMIT 12 OFFSET $2`, search, (page-1)*12)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		o, err := scanPublicOwner(rows)
		if err != nil {
			rows.Close()
			return out, err
		}
		out.Items = append(out.Items, o)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}

func (s *Service) PublicEntries(ctx context.Context, slug string, filter Filter) (PublicPage, error) {
	if err := filter.Validate(); err != nil {
		return PublicPage{}, err
	}
	out := PublicPage{Items: []PublicItem{}, Page: filter.Page, PageSize: filter.PageSize}
	if slug != "" && !id.Valid(slug) {
		return out, fault.New("not_found", "没有找到公开手账。")
	}
	tx, err := telemetry.Begin(ctx, s.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if slug != "" {
		owner, err := scanPublicOwner(tx.QueryRow(ctx, `SELECT `+publicOwnerFields+` FROM users u WHERE u.public_slug=$1 AND `+publicOwnerPredicate, slug))
		if err != nil {
			return out, err
		}
		out.Owner = &owner
	}
	search := "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(filter.Search) + "%"
	where := ` FROM entries WHERE visibility='public' AND NOT moderated_hidden AND deleted_at IS NULL AND EXISTS(SELECT 1 FROM users u WHERE u.id=entries.user_id AND ` + publicOwnerPredicate + ` AND ($1='' OR u.public_slug::text=$1)) AND ($2='' OR status=$2) AND (title ILIKE $3 OR original_title ILIKE $3 OR EXISTS(SELECT 1 FROM unnest(tags) t WHERE t ILIKE $3))`
	if err = tx.QueryRow(ctx, `SELECT count(*)`+where, slug, filter.Status, search).Scan(&out.Total); err != nil {
		return out, err
	}
	order := map[string]string{"updated": "updated_at DESC,id", "title": "title,id", "score": "score DESC NULLS LAST,updated_at DESC,id"}[filter.Sort]
	rows, err := tx.Query(ctx, `SELECT `+columns+where+` ORDER BY `+order+` LIMIT $4 OFFSET $5`, slug, filter.Status, search, filter.PageSize, (filter.Page-1)*filter.PageSize)
	if err != nil {
		return out, err
	}
	entries := []Entry{}
	for rows.Next() {
		e, err := scanEntry(rows)
		if err != nil {
			rows.Close()
			return out, err
		}
		entries = append(entries, e)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	for _, e := range entries {
		owner, err := scanPublicOwner(tx.QueryRow(ctx, `SELECT `+publicOwnerFields+` FROM users u JOIN entries e ON e.user_id=u.id WHERE e.id=$1`, e.ID))
		if err != nil {
			return out, err
		}
		out.Items = append(out.Items, PublicItem{Entry: projectPublic(e), Owner: owner})
	}
	return out, tx.Commit(ctx)
}

const sharePredicate = `visibility IN ('unlisted','public') AND NOT moderated_hidden AND deleted_at IS NULL AND EXISTS(SELECT 1 FROM users u WHERE u.id=entries.user_id AND u.sharing_enabled AND NOT u.disabled)`

func (s *Service) Shared(ctx context.Context, slug string) (PublicItem, error) {
	if !id.Valid(slug) {
		return PublicItem{}, fault.New("not_found", "分享不存在或已关闭。")
	}
	tx, err := telemetry.Begin(ctx, s.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return PublicItem{}, err
	}
	defer tx.Rollback(context.Background())
	e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE share_slug=$1 AND `+sharePredicate, slug))
	if err != nil {
		return PublicItem{}, err
	}
	owner, err := scanPublicOwner(tx.QueryRow(ctx, `SELECT `+publicOwnerFields+` FROM users u JOIN entries e ON e.user_id=u.id WHERE e.id=$1`, e.ID))
	if err != nil {
		return PublicItem{}, err
	}
	return PublicItem{Entry: projectPublic(e), Owner: owner}, tx.Commit(ctx)
}

func (s *Service) PublicCover(ctx context.Context, slug, revision string) (media.Image, error) {
	var out media.Image
	if !id.Valid(slug) || !id.Valid(revision) {
		return out, fault.New("not_found", "图片不存在或已关闭。")
	}
	err := s.pool.QueryRow(ctx, `SELECT content_type,data FROM entry_covers WHERE revision=$1 AND entry_id IN(SELECT id FROM entries WHERE share_slug=$2 AND `+sharePredicate+`)`, revision, slug).Scan(&out.ContentType, &out.Data)
	if errors.Is(err, pgx.ErrNoRows) {
		err = fault.New("not_found", "图片不存在或已关闭。")
	}
	if err != nil {
		return out, err
	}
	return s.storage.Resolve(ctx, revision, out)
}
func (s *Service) PublicAvatar(ctx context.Context, slug, revision string) (media.Image, error) {
	var out media.Image
	if !id.Valid(slug) || !id.Valid(revision) {
		return out, fault.New("not_found", "图片不存在。")
	}
	err := s.pool.QueryRow(ctx, `SELECT content_type,data FROM avatars WHERE revision=$1 AND user_id IN(SELECT u.id FROM users u WHERE u.public_slug=$2 AND `+publicOwnerPredicate+`)`, revision, slug).Scan(&out.ContentType, &out.Data)
	if errors.Is(err, pgx.ErrNoRows) {
		err = fault.New("not_found", "图片不存在或已关闭。")
	}
	if err != nil {
		return out, err
	}
	return s.storage.Resolve(ctx, revision, out)
}

func (s *Service) ResetShare(ctx context.Context, owner, entryID string, version int) (Entry, error) {
	if !id.Valid(entryID) {
		return Entry{}, fault.New("not_found", "没有找到这部番剧。")
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Entry{}, err
	}
	defer tx.Rollback(context.Background())
	e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, entryID, owner))
	if err != nil {
		return e, err
	}
	if e.Version != version {
		return e, fault.New("version_conflict", "番剧已更新，请重新打开详情。")
	}
	e, err = scanEntry(tx.QueryRow(ctx, `UPDATE entries SET share_slug=$2,version=version+1,updated_at=now() WHERE id=$1 RETURNING `+columns, entryID, id.New()))
	if err != nil {
		return e, err
	}
	return e, tx.Commit(ctx)
}
