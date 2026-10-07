package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
	"context"
	"errors"
	"github.com/jackc/pgx/v5"
	"strings"
	"time"
	"unicode/utf8"
)

type Resource struct {
	ID         string     `json:"id"`
	Kind       string     `json:"kind"`
	Title      string     `json:"title"`
	OwnerName  string     `json:"owner_name"`
	OwnerEmail string     `json:"owner_email"`
	Version    int        `json:"version"`
	State      string     `json:"state"`
	Hidden     bool       `json:"hidden"`
	DeletedAt  *time.Time `json:"deleted_at"`
	UpdatedAt  time.Time  `json:"updated_at"`
}
type Resources struct {
	Items []Resource `json:"items"`
	Total int        `json:"total"`
	Page  int        `json:"page"`
}

func (s *Service) Resources(ctx context.Context, kind, state, search string, page int) (Resources, error) {
	out := Resources{Items: []Resource{}, Page: page}
	f := Filter{Search: search, Page: page, PageSize: 20}
	if err := f.Validate(); err != nil {
		return out, err
	}
	table, stateField, hidden := "entries", "visibility", "moderated_hidden"
	if kind == "column" {
		table, stateField, hidden = "columns", "state", "false"
	} else if kind != "entry" {
		return out, fault.Field("kind", "资源类型无效。")
	}
	where := ` FROM ` + table + ` r JOIN users u ON u.id=r.user_id WHERE (r.title ILIKE $1 OR u.email ILIKE $1)`
	switch state {
	case "trash":
		where += ` AND r.deleted_at IS NOT NULL`
	case "active", "":
		where += ` AND r.deleted_at IS NULL`
	case "pending", "published":
		if kind != "column" {
			return out, fault.Field("state", "筛选无效。")
		}
		where += ` AND r.deleted_at IS NULL AND r.state=$2`
	default:
		return out, fault.Field("state", "筛选无效。")
	}
	where += ` AND $2::text IS NOT NULL`
	search = "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(f.Search) + "%"
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = tx.QueryRow(ctx, `SELECT count(*)`+where, search, state).Scan(&out.Total); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT r.id,r.title,u.display_name,u.email,r.version,r.`+stateField+`,`+hidden+`,r.deleted_at,r.updated_at`+where+` ORDER BY r.updated_at DESC,r.id LIMIT 20 OFFSET $3`, search, state, (page-1)*20)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		v := Resource{Kind: kind}
		if err = rows.Scan(&v.ID, &v.Title, &v.OwnerName, &v.OwnerEmail, &v.Version, &v.State, &v.Hidden, &v.DeletedAt, &v.UpdatedAt); err != nil {
			rows.Close()
			return out, err
		}
		out.Items = append(out.Items, v)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
func (s *Service) AdminColumn(ctx context.Context, key string) (Column, error) {
	if !id.Valid(key) {
		return Column{}, fault.New("not_found", "专栏不存在。")
	}
	return scanColumn(s.pool.QueryRow(ctx, `SELECT `+columnFields+` FROM columns WHERE id=$1`, key))
}
func (s *Service) AdminEntry(ctx context.Context, key string) (Entry, error) {
	if !id.Valid(key) {
		return Entry{}, fault.New("not_found", "番剧不存在。")
	}
	return scanEntry(s.pool.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1`, key))
}
func (s *Service) ResourceAction(ctx context.Context, actor, kind, key, action, reason string, version int) error {
	if !id.Valid(key) {
		return fault.New("not_found", "资源不存在。")
	}
	if (kind != "entry" && kind != "column") || (action != "trash" && action != "restore" && action != "hide" && action != "unhide") {
		return fault.Field("action", "资源操作无效。")
	}
	if kind == "column" && (action == "hide" || action == "unhide") {
		return fault.Field("action", "专栏请使用审核操作。")
	}
	reason = strings.TrimSpace(reason)
	if reason == "" || utf8.RuneCountInString(reason) > 400 || strings.ContainsRune(reason, 0) {
		return fault.Field("reason", "请填写 1–400 字的操作原因。")
	}
	table := "entries"
	if kind == "column" {
		table = "columns"
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return err
	}
	var current int
	var deleted *time.Time
	err = tx.QueryRow(ctx, `SELECT version,deleted_at FROM `+table+` WHERE id=$1 FOR UPDATE`, key).Scan(&current, &deleted)
	if errors.Is(err, pgx.ErrNoRows) {
		return fault.New("not_found", "资源不存在。")
	}
	if err != nil {
		return err
	}
	if version != current {
		return fault.New("version_conflict", "资源已更新，请刷新。")
	}
	var update string
	switch action {
	case "trash":
		if deleted != nil {
			return fault.New("version_conflict", "资源已经在回收站。")
		}
		update = `deleted_at=now()`
	case "restore":
		if deleted == nil {
			return fault.New("version_conflict", "资源没有被删除。")
		}
		update = `deleted_at=NULL`
		if kind == "entry" {
			update += `,visibility='private',share_slug=gen_random_uuid()`
		} else {
			update += `,state='draft',featured=false`
		}
	case "hide":
		update = `moderated_hidden=true`
	case "unhide":
		update = `moderated_hidden=false`
	}
	if _, err = tx.Exec(ctx, `UPDATE `+table+` SET `+update+`,version=version+1,updated_at=now() WHERE id=$1`, key); err != nil {
		return err
	}
	if err = governance.Audit(ctx, tx, actor, action, kind, key, map[string]string{"reason": reason}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
