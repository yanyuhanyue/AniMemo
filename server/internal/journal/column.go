package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"animemo.local/server/internal/telemetry"
	"context"
	"errors"
	"github.com/jackc/pgx/v5"
	"strings"
	"time"
	"unicode/utf8"
)

type Column struct {
	ID            string    `json:"id"`
	Title         string    `json:"title"`
	Summary       string    `json:"summary"`
	Body          string    `json:"body"`
	State         string    `json:"state"`
	Featured      bool      `json:"featured"`
	Reason        string    `json:"reason"`
	Version       int       `json:"version"`
	EntryIDs      []string  `json:"entry_ids"`
	CoverRevision *string   `json:"cover_revision"`
	UpdatedAt     time.Time `json:"updated_at"`
}
type ColumnInput struct {
	Title    string   `json:"title"`
	Summary  string   `json:"summary"`
	Body     string   `json:"body"`
	EntryIDs []string `json:"entry_ids"`
	Version  int      `json:"version"`
}

func (v *ColumnInput) Validate() error {
	v.Title = strings.TrimSpace(v.Title)
	v.Summary = strings.TrimSpace(v.Summary)
	if utf8.RuneCountInString(v.Title) < 1 || utf8.RuneCountInString(v.Title) > 200 || utf8.RuneCountInString(v.Summary) > 400 || utf8.RuneCountInString(v.Body) > 30000 || strings.ContainsRune(v.Title+v.Summary+v.Body, 0) {
		return fault.Field("title", "标题需要 1–200 字，摘要最多 400 字，正文最多 30000 字。")
	}
	if len(v.EntryIDs) > 30 {
		return fault.Field("entry_ids", "一篇专栏最多关联 30 部番剧。")
	}
	seen := map[string]bool{}
	for _, key := range v.EntryIDs {
		if !id.Valid(key) || seen[key] {
			return fault.Field("entry_ids", "关联番剧无效或重复。")
		}
		seen[key] = true
	}
	return nil
}

const columnFields = `id,title,summary,body,state,featured,reason,version,coalesce((SELECT array_agg(entry_id::text ORDER BY entry_id) FROM column_entries WHERE column_id=columns.id),'{}'),(SELECT revision FROM column_covers WHERE column_id=columns.id),updated_at`

func scanColumn(row pgx.Row) (Column, error) {
	var c Column
	err := row.Scan(&c.ID, &c.Title, &c.Summary, &c.Body, &c.State, &c.Featured, &c.Reason, &c.Version, &c.EntryIDs, &c.CoverRevision, &c.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		err = fault.New("not_found", "没有找到这篇专栏。")
	}
	return c, err
}

type ColumnPage struct {
	Items []Column `json:"items"`
	Total int      `json:"total"`
	Page  int      `json:"page"`
}

func (s *Service) Columns(ctx context.Context, owner string, page int) (ColumnPage, error) {
	out := ColumnPage{Items: []Column{}, Page: page}
	if page < 1 || page > 100000 {
		return out, fault.Field("page", "页码无效。")
	}
	tx, err := telemetry.Begin(ctx, s.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM columns WHERE user_id=$1 AND deleted_at IS NULL`, owner).Scan(&out.Total); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT `+columnFields+` FROM columns WHERE user_id=$1 AND deleted_at IS NULL ORDER BY updated_at DESC,id LIMIT 20 OFFSET $2`, owner, (page-1)*20)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		c, e := scanColumn(rows)
		if e != nil {
			rows.Close()
			return out, e
		}
		out.Items = append(out.Items, c)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
func (s *Service) Column(ctx context.Context, owner, key string) (Column, error) {
	if !id.Valid(key) {
		return Column{}, fault.New("not_found", "没有找到这篇专栏。")
	}
	return scanColumn(s.pool.QueryRow(ctx, `SELECT `+columnFields+` FROM columns WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL`, key, owner))
}
func (s *Service) SaveColumn(ctx context.Context, owner, key string, input ColumnInput) (Column, error) {
	if err := input.Validate(); err != nil {
		return Column{}, err
	}
	if key != "" && !id.Valid(key) {
		return Column{}, fault.New("not_found", "没有找到这篇专栏。")
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Column{}, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226047))`, owner); err != nil {
		return Column{}, err
	}
	if key == "" {
		var count int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM columns WHERE user_id=$1`, owner).Scan(&count); err != nil {
			return Column{}, err
		}
		if count >= 100 {
			return Column{}, fault.New("validation_error", "每个账号最多保存 100 篇专栏。")
		}
		key = id.New()
		_, err = tx.Exec(ctx, `INSERT INTO columns(id,user_id,title) VALUES($1,$2,$3)`, key, owner, input.Title)
	} else {
		var c Column
		c, err = scanColumn(tx.QueryRow(ctx, `SELECT `+columnFields+` FROM columns WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, key, owner))
		if err == nil && c.Version != input.Version {
			err = fault.New("version_conflict", "专栏已修改，请刷新后重试。")
		}
	}
	if err != nil {
		return Column{}, err
	}
	// Only the author's own active entries may be attached. Private attachments are never projected publicly.
	for _, entry := range input.EntryIDs {
		var exists bool
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL)`, entry, owner).Scan(&exists); err != nil {
			return Column{}, err
		}
		if !exists {
			return Column{}, fault.Field("entry_ids", "只能关联自己手账中的番剧。")
		}
	}
	if _, err = tx.Exec(ctx, `DELETE FROM column_entries WHERE column_id=$1`, key); err != nil {
		return Column{}, err
	}
	for _, entry := range input.EntryIDs {
		if _, err = tx.Exec(ctx, `INSERT INTO column_entries(column_id,entry_id) VALUES($1,$2)`, key, entry); err != nil {
			return Column{}, err
		}
	}
	c, err := scanColumn(tx.QueryRow(ctx, `UPDATE columns SET title=$2,summary=$3,body=$4,state='draft',featured=false,reason='',version=version+1,updated_at=now() WHERE id=$1 RETURNING `+columnFields, key, input.Title, input.Summary, input.Body))
	if err != nil {
		return c, err
	}
	return c, tx.Commit(ctx)
}
func (s *Service) ColumnAction(ctx context.Context, owner, key, action string, version int) (Column, error) {
	if !id.Valid(key) {
		return Column{}, fault.New("not_found", "没有找到这篇专栏。")
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Column{}, err
	}
	defer tx.Rollback(context.Background())
	c, err := scanColumn(tx.QueryRow(ctx, `SELECT `+columnFields+` FROM columns WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, key, owner))
	if err != nil {
		return c, err
	}
	if c.Version != version {
		return c, fault.New("version_conflict", "专栏已更新，请刷新。")
	}
	switch action {
	case "submit":
		var sharing bool
		if err = tx.QueryRow(ctx, `SELECT sharing_enabled AND NOT disabled FROM users WHERE id=$1`, owner).Scan(&sharing); err != nil {
			return c, err
		}
		if !sharing {
			return c, fault.New("validation_error", "请先在账号设置开启分享。")
		}
		if strings.TrimSpace(c.Body) == "" {
			return c, fault.Field("body", "请先完成专栏正文。")
		}
		if c.State == "published" || c.State == "pending" {
			return c, fault.New("version_conflict", "专栏已经公开或等待审核。")
		}
		c.State = "pending"
	case "withdraw":
		c.State = "draft"
	case "delete":
		if _, err = tx.Exec(ctx, `DELETE FROM columns WHERE id=$1`, key); err != nil {
			return c, err
		}
		return c, tx.Commit(ctx)
	default:
		return c, fault.Field("action", "专栏操作无效。")
	}
	c, err = scanColumn(tx.QueryRow(ctx, `UPDATE columns SET state=$2,reason='',featured=false,version=version+1,updated_at=now() WHERE id=$1 RETURNING `+columnFields, key, c.State))
	if err != nil {
		return c, err
	}
	return c, tx.Commit(ctx)
}
func (s *Service) SetColumnCover(ctx context.Context, owner, key string, version int, data []byte, contentType string) (Column, error) {
	if !id.Valid(key) {
		return Column{}, fault.New("not_found", "没有找到这篇专栏。")
	}
	var picture media.Image
	var err error
	if data != nil {
		picture, err = media.Validate(data, contentType)
		if err != nil {
			return Column{}, err
		}
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Column{}, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226047))`, owner); err != nil {
		return Column{}, err
	}
	c, err := scanColumn(tx.QueryRow(ctx, `SELECT `+columnFields+` FROM columns WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, key, owner))
	if err != nil {
		return c, err
	}
	if c.Version != version {
		return c, fault.New("version_conflict", "专栏已更新，请刷新。")
	}
	if data != nil {
		var used int
		if err = tx.QueryRow(ctx, `SELECT coalesce(sum(byte_size),0) FROM column_covers WHERE column_id IN(SELECT id FROM columns WHERE user_id=$1 AND id<>$2)`, owner, key).Scan(&used); err != nil {
			return c, err
		}
		if used+len(data) > 20<<20 {
			return c, fault.New("cover_quota_exceeded", "专栏封面总容量上限 20 MiB。")
		}
		_, err = tx.Exec(ctx, `INSERT INTO column_covers(column_id,revision,content_type,data,byte_size) VALUES($1,$2,$3,$4,$5) ON CONFLICT(column_id) DO UPDATE SET revision=excluded.revision,content_type=excluded.content_type,data=excluded.data,byte_size=excluded.byte_size`, key, id.New(), picture.ContentType, picture.Data, len(picture.Data))
	} else {
		_, err = tx.Exec(ctx, `DELETE FROM column_covers WHERE column_id=$1`, key)
	}
	if err != nil {
		return c, err
	}
	c, err = scanColumn(tx.QueryRow(ctx, `UPDATE columns SET state='draft',featured=false,version=version+1,updated_at=now() WHERE id=$1 RETURNING `+columnFields, key))
	if err != nil {
		return c, err
	}
	return c, tx.Commit(ctx)
}

const publicColumnPredicate = `state='published' AND deleted_at IS NULL AND EXISTS(SELECT 1 FROM users u WHERE u.id=columns.user_id AND u.sharing_enabled AND NOT u.disabled)`

type PublicColumn struct {
	ID            string        `json:"id"`
	Title         string        `json:"title"`
	Summary       string        `json:"summary"`
	Body          string        `json:"body"`
	Featured      bool          `json:"featured"`
	CoverRevision *string       `json:"cover_revision"`
	Owner         PublicOwner   `json:"owner"`
	Entries       []PublicEntry `json:"entries"`
	UpdatedAt     time.Time     `json:"updated_at"`
}

func (s *Service) PublicColumn(ctx context.Context, key string) (PublicColumn, error) {
	var out PublicColumn
	if !id.Valid(key) {
		return out, fault.New("not_found", "专栏不存在或尚未公开。")
	}
	tx, err := telemetry.Begin(ctx, s.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	c, err := scanColumn(tx.QueryRow(ctx, `SELECT `+columnFields+` FROM columns WHERE id=$1 AND `+publicColumnPredicate, key))
	if err != nil {
		return out, err
	}
	owner, err := scanPublicOwner(tx.QueryRow(ctx, `SELECT `+publicOwnerFields+` FROM users u JOIN columns c ON c.user_id=u.id WHERE c.id=$1`, key))
	if err != nil {
		return out, err
	}
	out = PublicColumn{ID: c.ID, Title: c.Title, Summary: c.Summary, Body: c.Body, Featured: c.Featured, CoverRevision: c.CoverRevision, Owner: owner, Entries: []PublicEntry{}, UpdatedAt: c.UpdatedAt}
	rows, err := tx.Query(ctx, `SELECT `+columns+` FROM entries WHERE id IN(SELECT entry_id FROM column_entries WHERE column_id=$1) AND visibility='public' AND `+sharePredicate+` ORDER BY title,id`, key)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		e, err := scanEntry(rows)
		if err != nil {
			rows.Close()
			return out, err
		}
		out.Entries = append(out.Entries, projectPublic(e))
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}

type PublicColumnSummary struct {
	ID            string  `json:"id"`
	Title         string  `json:"title"`
	Summary       string  `json:"summary"`
	Featured      bool    `json:"featured"`
	CoverRevision *string `json:"cover_revision"`
	Author        string  `json:"author"`
}
type PublicColumns struct {
	Items []PublicColumnSummary `json:"items"`
	Total int                   `json:"total"`
	Page  int                   `json:"page"`
}

func (s *Service) PublicColumns(ctx context.Context, search string, page int) (PublicColumns, error) {
	out := PublicColumns{Items: []PublicColumnSummary{}, Page: page}
	f := Filter{Search: search, Page: page, PageSize: 12}
	if err := f.Validate(); err != nil {
		return out, err
	}
	search = "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(f.Search) + "%"
	tx, err := telemetry.Begin(ctx, s.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	where := ` FROM columns WHERE ` + publicColumnPredicate + ` AND (title ILIKE $1 OR summary ILIKE $1)`
	if err = tx.QueryRow(ctx, `SELECT count(*)`+where, search).Scan(&out.Total); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT id,title,summary,featured,(SELECT revision FROM column_covers WHERE column_id=columns.id),(SELECT display_name FROM users WHERE id=columns.user_id)`+where+` ORDER BY featured DESC,updated_at DESC,id LIMIT 12 OFFSET $2`, search, (page-1)*12)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		var c PublicColumnSummary
		if err = rows.Scan(&c.ID, &c.Title, &c.Summary, &c.Featured, &c.CoverRevision, &c.Author); err != nil {
			rows.Close()
			return out, err
		}
		out.Items = append(out.Items, c)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
func (s *Service) ColumnCover(ctx context.Context, owner, key, revision string, public bool) (media.Image, error) {
	var picture media.Image
	if !id.Valid(key) || !id.Valid(revision) {
		return picture, fault.New("not_found", "封面不存在。")
	}
	predicate := `user_id::text=$3 AND deleted_at IS NULL`
	if public {
		predicate = publicColumnPredicate + ` AND $3=''`
	}
	err := s.pool.QueryRow(ctx, `SELECT content_type,data FROM column_covers WHERE column_id=$1 AND revision=$2 AND column_id IN(SELECT id FROM columns WHERE id=$1 AND `+predicate+`)`, key, revision, owner).Scan(&picture.ContentType, &picture.Data)
	if errors.Is(err, pgx.ErrNoRows) {
		err = fault.New("not_found", "封面不存在或未公开。")
	}
	if err != nil {
		return picture, err
	}
	return s.storage.Resolve(ctx, revision, picture)
}
func (s *Service) ModerateColumn(ctx context.Context, actor, key, action, reason string, version int) (Column, error) {
	if !id.Valid(key) {
		return Column{}, fault.New("not_found", "专栏不存在。")
	}
	reason = strings.TrimSpace(reason)
	if utf8.RuneCountInString(reason) > 400 || strings.ContainsRune(reason, 0) {
		return Column{}, fault.Field("reason", "原因最多 400 字。")
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Column{}, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return Column{}, err
	}
	c, err := scanColumn(tx.QueryRow(ctx, `SELECT `+columnFields+` FROM columns WHERE id=$1 AND deleted_at IS NULL FOR UPDATE`, key))
	if err != nil {
		return c, err
	}
	if c.Version != version {
		return c, fault.New("version_conflict", "专栏已更新，请刷新。")
	}
	switch action {
	case "approve":
		if c.State != "pending" {
			return c, fault.New("version_conflict", "专栏没有待审核申请。")
		}
		c.State = "published"
	case "reject", "hide":
		if reason == "" {
			return c, fault.Field("reason", "请填写审核原因。")
		}
		c.State = "rejected"
		c.Featured = false
	case "feature", "unfeature":
		if c.State != "published" {
			return c, fault.New("version_conflict", "只有已公开的专栏可以精选。")
		}
		c.Featured = action == "feature"
	default:
		return c, fault.Field("action", "审核操作无效。")
	}
	c, err = scanColumn(tx.QueryRow(ctx, `UPDATE columns SET state=$2,featured=$3,reason=$4,version=version+1,updated_at=now() WHERE id=$1 RETURNING `+columnFields, key, c.State, c.Featured, reason))
	if err != nil {
		return c, err
	}
	if err = governance.Audit(ctx, tx, actor, action, "column", key, map[string]string{"reason": reason}); err != nil {
		return c, err
	}
	return c, tx.Commit(ctx)
}
