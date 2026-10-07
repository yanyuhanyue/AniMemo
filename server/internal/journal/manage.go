package journal

import (
	"context"
	"errors"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"github.com/jackc/pgx/v5/pgconn"
)

type VersionedID struct {
	ID      string `json:"id"`
	Version int    `json:"version"`
}
type BulkInput struct {
	Entries []VersionedID `json:"entries"`
	Action  string        `json:"action"`
	Value   string        `json:"value"`
}

func (s *Service) Bulk(ctx context.Context, owner string, input BulkInput) ([]Entry, error) {
	if len(input.Entries) < 1 || len(input.Entries) > 100 {
		return nil, fault.Field("entries", "每次请选择 1–100 部番剧。")
	}
	if input.Action != "status" && input.Action != "tag-add" && input.Action != "tag-remove" && input.Action != "visibility" {
		return nil, fault.Field("action", "批量操作无效。")
	}
	if input.Action == "status" && !ValidStatus(input.Value) {
		return nil, fault.Field("value", "观看状态无效。")
	}
	input.Value = strings.TrimSpace(input.Value)
	if input.Action == "visibility" && input.Value != "private" && input.Value != "unlisted" && input.Value != "public" {
		return nil, fault.Field("value", "可见性无效。")
	}
	if input.Action != "status" && input.Action != "visibility" && (utf8.RuneCountInString(input.Value) < 1 || utf8.RuneCountInString(input.Value) > 24) {
		return nil, fault.Field("value", "标签需要 1–24 个字。")
	}
	slices.SortFunc(input.Entries, func(a, b VersionedID) int { return strings.Compare(a.ID, b.ID) })
	for i, e := range input.Entries {
		if !id.Valid(e.ID) || e.Version < 1 || (i > 0 && input.Entries[i-1].ID == e.ID) {
			return nil, fault.Field("entries", "条目标识、版本或重复选择无效。")
		}
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(context.Background())
	out := make([]Entry, 0, len(input.Entries))
	for _, selected := range input.Entries {
		e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, selected.ID, owner))
		if err != nil {
			return nil, err
		}
		patch := Patch{Version: selected.Version}
		switch input.Action {
		case "status":
			patch.Status = &input.Value
		case "visibility":
			patch.Visibility = &input.Value
		case "tag-add":
			tags := append(e.Tags, input.Value)
			patch.Tags = &tags
		case "tag-remove":
			tags := slices.DeleteFunc(e.Tags, func(t string) bool { return t == input.Value })
			patch.Tags = &tags
		}
		e, err = updateEntry(ctx, tx, owner, selected.ID, patch)
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, tx.Commit(ctx)
}

type Tag struct {
	Name  string `json:"name"`
	Color string `json:"color"`
}

var hexColor = regexp.MustCompile(`^#[0-9a-fA-F]{6}$`)

func (s *Service) Tags(ctx context.Context, owner string) ([]Tag, error) {
	rows, err := s.pool.Query(ctx, `SELECT n.name,coalesce(t.color,'#8974cc') FROM (SELECT name FROM tags WHERE user_id=$1 UNION SELECT unnest(tags) FROM entries WHERE user_id=$1 AND deleted_at IS NULL) n LEFT JOIN tags t ON t.name=n.name AND t.user_id=$1 ORDER BY n.name`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Tag{}
	for rows.Next() {
		var t Tag
		if err = rows.Scan(&t.Name, &t.Color); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

func (s *Service) SetTag(ctx context.Context, owner string, input Tag) error {
	input.Name = strings.TrimSpace(input.Name)
	if n := utf8.RuneCountInString(input.Name); n < 1 || n > 24 || strings.ContainsRune(input.Name, 0) {
		return fault.Field("name", "标签需要 1–24 个字。")
	}
	if !hexColor.MatchString(input.Color) {
		return fault.Field("color", "请选择有效的标签颜色。")
	}
	_, err := s.pool.Exec(ctx, `INSERT INTO tags(user_id,name,color) VALUES($1,$2,$3) ON CONFLICT(user_id,name) DO UPDATE SET color=excluded.color`, owner, input.Name, input.Color)
	return err
}

type QuickFilter struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Search string `json:"search"`
	Status string `json:"status"`
	Sort   string `json:"sort"`
}

func (s *Service) Filters(ctx context.Context, owner string) ([]QuickFilter, error) {
	rows, err := s.pool.Query(ctx, `SELECT id,name,search,status,sort FROM quick_filters WHERE user_id=$1 ORDER BY name,id`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []QuickFilter{}
	for rows.Next() {
		var f QuickFilter
		if err = rows.Scan(&f.ID, &f.Name, &f.Search, &f.Status, &f.Sort); err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rows.Err()
}
func (s *Service) SaveFilter(ctx context.Context, owner string, input QuickFilter) (QuickFilter, error) {
	input.Name = strings.TrimSpace(input.Name)
	if n := utf8.RuneCountInString(input.Name); n < 1 || n > 40 || strings.ContainsRune(input.Name, 0) {
		return input, fault.Field("name", "筛选名称需要 1–40 个字。")
	}
	f := Filter{Search: input.Search, Status: input.Status, Sort: input.Sort, Page: 1, PageSize: 12}
	if err := f.Validate(); err != nil {
		return input, err
	}
	input.Search, input.Sort = f.Search, f.Sort
	input.ID = id.New()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return input, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226044))`, owner); err != nil {
		return input, err
	}
	var count int
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM quick_filters WHERE user_id=$1`, owner).Scan(&count); err != nil {
		return input, err
	}
	if count >= 20 {
		return input, fault.New("validation_error", "最多保存 20 个快捷筛选。")
	}
	_, err = tx.Exec(ctx, `INSERT INTO quick_filters(id,user_id,name,search,status,sort) VALUES($1,$2,$3,$4,$5,$6)`, input.ID, owner, input.Name, input.Search, input.Status, input.Sort)
	var pgerr *pgconn.PgError
	if errors.As(err, &pgerr) && pgerr.Code == "23505" {
		return input, fault.Field("name", "已经有同名筛选。")
	}
	if err != nil {
		return input, err
	}
	return input, tx.Commit(ctx)
}
func (s *Service) DeleteFilter(ctx context.Context, owner, filterID string) error {
	if !id.Valid(filterID) {
		return fault.New("not_found", "没有找到筛选。")
	}
	r, err := s.pool.Exec(ctx, `DELETE FROM quick_filters WHERE id=$1 AND user_id=$2`, filterID, owner)
	if err != nil {
		return err
	}
	if r.RowsAffected() == 0 {
		return fault.New("not_found", "没有找到筛选。")
	}
	return nil
}
