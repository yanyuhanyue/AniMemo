package journal

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/telemetry"
	"github.com/jackc/pgx/v5"
)

type LibraryRevision struct {
	ID         string          `json:"id"`
	ResourceID string          `json:"resource_id"`
	Kind       string          `json:"kind"`
	Action     string          `json:"action"`
	Snapshot   json.RawMessage `json:"snapshot"`
	RecordedAt time.Time       `json:"recorded_at"`
}

type LibraryFilter struct {
	Search, AnimeID, CharacterID, Kind, Year string
	Page                                     int
	Highlight                                bool
}
type LibraryPage[T any] struct {
	Items    []T `json:"items"`
	Total    int `json:"total"`
	Page     int `json:"page"`
	PageSize int `json:"page_size"`
}

func libraryPage(page int) int {
	if page < 1 {
		return 1
	}
	if page > 10000 {
		return 10000
	}
	return page
}
func libraryMissing(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return fault.New("not_found", "没有找到这份记忆或资源。")
	}
	return err
}
func libraryVersion(actual, expected int) error {
	if actual != expected {
		return fault.New("version_conflict", "内容已在其他窗口修改，请重新打开后再保存。")
	}
	return nil
}
func libraryVisibility(value string) bool {
	return value == "private" || value == "unlisted" || value == "public"
}
func libraryText(value string, max int) bool {
	return utf8.ValidString(value) && utf8.RuneCountInString(value) <= max && !strings.ContainsRune(value, 0)
}
func libraryIDs(values []string, max int) bool {
	if len(values) > max {
		return false
	}
	seen := map[string]bool{}
	for _, v := range values {
		if !id.Valid(v) || seen[v] {
			return false
		}
		seen[v] = true
	}
	return true
}
func libraryTags(values []string, max int) bool {
	if len(values) > max {
		return false
	}
	seen := map[string]bool{}
	for _, v := range values {
		if strings.TrimSpace(v) == "" || !libraryText(v, 80) || seen[v] {
			return false
		}
		seen[v] = true
	}
	return true
}
func (s *Service) libraryTx(ctx context.Context, owner string) (pgx.Tx, error) {
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226043))`, owner); err != nil {
		tx.Rollback(context.Background())
		return nil, err
	}
	return tx, nil
}
func libraryRevision(ctx context.Context, tx pgx.Tx, owner, resource, kind, action string, snapshot any) error {
	_, err := tx.Exec(ctx, `INSERT INTO library_revisions(owner_id,resource_id,kind,action,snapshot) VALUES($1,$2,$3,$4,$5)`, owner, resource, kind, action, snapshot)
	return err
}
func (s *Service) LibraryRevisions(ctx context.Context, owner, resource string) ([]LibraryRevision, error) {
	if !id.Valid(resource) {
		return nil, fault.New("not_found", "资源不存在。")
	}
	rows, err := s.pool.Query(ctx, `SELECT id,resource_id,kind,action,snapshot,recorded_at FROM library_revisions WHERE owner_id=$1 AND resource_id=$2 ORDER BY recorded_at DESC,id DESC LIMIT 100`, owner, resource)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []LibraryRevision{}
	for rows.Next() {
		var r LibraryRevision
		if err = rows.Scan(&r.ID, &r.ResourceID, &r.Kind, &r.Action, &r.Snapshot, &r.RecordedAt); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// References stay on their original local identity through a merge. Removing a
// redirect is a reversible split; it never rewrites historical snapshots.
func (s *Service) RedirectIdentity(ctx context.Context, owner, kind, resource, target string, version int) error {
	table := ""
	switch kind {
	case "character":
		table = "characters"
	case "episode":
		table = "episodes"
	default:
		return fault.Field("kind", "身份类型无效。")
	}
	if !id.Valid(resource) || (target != "" && !id.Valid(target)) || target == resource {
		return fault.Field("target_id", "归并目标无效。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var actual int
	var old *string
	if err = tx.QueryRow(ctx, `SELECT version,redirect_id FROM `+table+` WHERE owner_id=$1 AND id=$2 FOR UPDATE`, owner, resource).Scan(&actual, &old); err != nil {
		return libraryMissing(err)
	}
	if err = libraryVersion(actual, version); err != nil {
		return err
	}
	if target != "" {
		var exists bool
		condition := ""
		if kind == "episode" {
			condition = ` AND anime_id=(SELECT anime_id FROM episodes WHERE id=$3 AND owner_id=$1)`
		}
		args := []any{owner, target}
		if kind == "episode" {
			args = append(args, resource)
		}
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM `+table+` WHERE owner_id=$1 AND id=$2 AND redirect_id IS NULL`+condition+`)`, args...).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return fault.Field("target_id", "请选择同一账号下未归并的目标；集数还必须属于同一作品。")
		}
		// Reject a cycle including targets whose own incoming chain reaches source.
		var cycle bool
		if err = tx.QueryRow(ctx, `WITH RECURSIVE chain AS (SELECT id,redirect_id FROM `+table+` WHERE owner_id=$1 AND id=$2 UNION SELECT c.id,c.redirect_id FROM `+table+` c JOIN chain p ON c.id=p.redirect_id WHERE c.owner_id=$1) SELECT EXISTS(SELECT 1 FROM chain WHERE id=$3)`, owner, target, resource).Scan(&cycle); err != nil {
			return err
		}
		if cycle {
			return fault.Field("target_id", "不能形成循环归并。")
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE `+table+` SET redirect_id=nullif($3,'')::uuid,version=version+1,updated_at=now() WHERE owner_id=$1 AND id=$2`, owner, resource, target); err != nil {
		return err
	}
	action := "merged"
	if target == "" {
		action = "split"
	}
	if err = libraryRevision(ctx, tx, owner, resource, kind, action, map[string]any{"from": old, "to": target}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func validMemoryTime(date, precision string) bool {
	if precision == "unknown" {
		return date == ""
	}
	layout := "2006-01-02"
	switch precision {
	case "day", "approximate":
	case "month":
		layout = "2006-01"
	case "year":
		layout = "2006"
	default:
		return false
	}
	d, err := time.Parse(layout, date)
	return err == nil && d.Year() >= 1900 && d.Year() <= 2100
}
