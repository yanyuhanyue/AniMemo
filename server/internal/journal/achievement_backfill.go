package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
	"time"
)

type AchievementBackfill struct {
	ID        string            `json:"id"`
	State     string            `json:"state"`
	Rules     []AchievementRule `json:"rules"`
	Total     int               `json:"total"`
	Cursor    int               `json:"cursor"`
	Granted   int               `json:"granted"`
	CreatedAt time.Time         `json:"created_at"`
	UpdatedAt time.Time         `json:"updated_at"`
}

const backfillColumns = `id,state,rules,cardinality(owners),cursor,granted,created_at,updated_at`

func scanBackfill(row pgx.Row) (AchievementBackfill, error) {
	var b AchievementBackfill
	err := row.Scan(&b.ID, &b.State, &b.Rules, &b.Total, &b.Cursor, &b.Granted, &b.CreatedAt, &b.UpdatedAt)
	return b, libraryMissing(err)
}
func (s *Service) AchievementBackfills(ctx context.Context) ([]AchievementBackfill, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+backfillColumns+` FROM achievement_backfills ORDER BY created_at DESC LIMIT 50`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []AchievementBackfill{}
	for rows.Next() {
		b, err := scanBackfill(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}
func (s *Service) PreviewAchievementBackfill(ctx context.Context, actor string) (AchievementBackfill, error) {
	var out AchievementBackfill
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT `+ruleColumns+ruleFrom+` WHERE t.active ORDER BY t.id`)
	if err != nil {
		return out, err
	}
	rules := []AchievementRule{}
	for rows.Next() {
		r, e := scanRule(rows)
		if e != nil {
			rows.Close()
			return out, e
		}
		rules = append(rules, r)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	data, err := json.Marshal(rules)
	if err != nil {
		return out, err
	}
	out, err = scanBackfill(tx.QueryRow(ctx, `INSERT INTO achievement_backfills(id,actor_id,state,rules,owners) SELECT $1,$2,'preview',$3,coalesce(array_agg(id ORDER BY id),'{}'::uuid[]) FROM users WHERE NOT disabled RETURNING `+backfillColumns, id.New(), actor, data))
	if err != nil {
		return out, err
	}
	if err = governance.Audit(ctx, tx, actor, "achievement-backfill-preview", "achievement-backfill", out.ID, map[string]any{"users": out.Total, "rules": len(rules)}); err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
func (s *Service) AchievementBackfillAction(ctx context.Context, actor, key, action string) (AchievementBackfill, error) {
	var out AchievementBackfill
	if !id.Valid(key) {
		return out, fault.New("not_found", "任务不存在。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return out, err
	}
	out, err = scanBackfill(tx.QueryRow(ctx, `SELECT `+backfillColumns+` FROM achievement_backfills WHERE id=$1 FOR UPDATE`, key))
	if err != nil {
		return out, err
	}
	next := ""
	switch action {
	case "run":
		if out.State == "preview" || out.State == "paused" {
			next = "running"
		}
	case "pause":
		if out.State == "running" {
			next = "paused"
		}
	}
	if next == "" {
		return out, fault.Field("action", "当前状态不支持该操作。")
	}
	out, err = scanBackfill(tx.QueryRow(ctx, `UPDATE achievement_backfills SET state=$2,actor_id=$3,updated_at=now() WHERE id=$1 RETURNING `+backfillColumns, key, next, actor))
	if err != nil {
		return out, err
	}
	if err = governance.Audit(ctx, tx, actor, "achievement-backfill-"+action, "achievement-backfill", key, map[string]any{}); err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
func (s *Service) ProcessAchievementBackfill(ctx context.Context) (bool, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Lock(ctx, tx); err != nil {
		return false, err
	}
	var key, actor string
	var rules []AchievementRule
	var owners []string
	var cursor int
	err = tx.QueryRow(ctx, `SELECT id,coalesce(actor_id::text,''),rules,owners,cursor FROM achievement_backfills WHERE state='running' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1`).Scan(&key, &actor, &rules, &owners, &cursor)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var allowed bool
	if actor != "" {
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND is_admin AND NOT disabled)`, actor).Scan(&allowed); err != nil {
			return true, err
		}
	}
	if !allowed {
		if _, err = tx.Exec(ctx, `UPDATE achievement_backfills SET state='paused',updated_at=now() WHERE id=$1`, key); err != nil {
			return true, err
		}
		return true, tx.Commit(ctx)
	}
	end := min(cursor+10, len(owners))
	granted := 0
	for _, owner := range owners[cursor:end] {
		var active bool
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND NOT disabled)`, owner).Scan(&active); err != nil {
			return true, err
		}
		if !active {
			continue
		}
		n, err := evaluateAchievements(ctx, tx, owner, rules)
		if err != nil {
			return true, err
		}
		granted += n
	}
	state := "running"
	if end == len(owners) {
		state = "done"
	}
	if _, err = tx.Exec(ctx, `UPDATE achievement_backfills SET cursor=$2,granted=granted+$3,state=$4,updated_at=now() WHERE id=$1`, key, end, granted, state); err != nil {
		return true, err
	}
	return true, tx.Commit(ctx)
}
