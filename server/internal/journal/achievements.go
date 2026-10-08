package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
	"context"
	"errors"
	"github.com/jackc/pgx/v5"
	"log/slog"
	"regexp"
	"time"
)

type AchievementRule struct {
	ID          string `json:"id"`
	SeriesID    string `json:"series_id"`
	SeriesTitle string `json:"series_title"`
	Tier        int    `json:"tier"`
	Revision    int    `json:"revision"`
	Title       string `json:"title"`
	Description string `json:"description"`
	Badge       string `json:"badge"`
	Metric      string `json:"metric"`
	Threshold   int    `json:"threshold"`
	Active      bool   `json:"active"`
}
type Achievement struct {
	AchievementRule
	Value        int        `json:"value"`
	UnlockID     string     `json:"unlock_id"`
	UnlockedAt   *time.Time `json:"unlocked_at"`
	Granted      bool       `json:"granted"`
	Notified     bool       `json:"notified"`
	ShowcaseSlot int        `json:"showcase_slot"`
}
type AchievementCenter struct {
	Items []Achievement `json:"items"`
}

const ruleColumns = `t.id,t.series_id,s.title,t.tier,r.revision,r.title,r.description,r.badge,r.metric,r.threshold,t.active`
const ruleFrom = ` FROM achievement_tiers t JOIN achievement_series s ON s.id=t.series_id JOIN achievement_rule_revisions r ON r.tier_id=t.id AND r.revision=t.current_revision`

func scanRule(row pgx.Row) (AchievementRule, error) {
	var a AchievementRule
	err := row.Scan(&a.ID, &a.SeriesID, &a.SeriesTitle, &a.Tier, &a.Revision, &a.Title, &a.Description, &a.Badge, &a.Metric, &a.Threshold, &a.Active)
	return a, libraryMissing(err)
}
func (s *Service) AchievementRules(ctx context.Context) ([]AchievementRule, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+ruleColumns+ruleFrom+` WHERE NOT t.archived ORDER BY t.series_id,t.tier`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []AchievementRule{}
	for rows.Next() {
		r, err := scanRule(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
func (s *Service) Achievements(ctx context.Context, owner string) (AchievementCenter, error) {
	out := AchievementCenter{Items: []Achievement{}}
	rows, err := s.pool.Query(ctx, `SELECT t.id,t.series_id,s.title,t.tier,r.revision,r.title,r.description,r.badge,r.metric,r.threshold,t.active,coalesce(p.value,0),coalesce(u.id::text,''),u.unlocked_at,coalesce(g.active,false),coalesce(g.notified,true),coalesce(g.showcase_slot,0) FROM achievement_tiers t JOIN achievement_series s ON s.id=t.series_id LEFT JOIN achievement_unlocks u ON u.tier_id=t.id AND u.owner_id=$1 JOIN achievement_rule_revisions r ON r.tier_id=t.id AND r.revision=coalesce(u.rule_revision,t.current_revision) LEFT JOIN achievement_progress p ON p.tier_id=t.id AND p.owner_id=$1 LEFT JOIN achievement_grants g ON g.unlock_id=u.id WHERE t.active OR u.id IS NOT NULL OR (t.archived AND p.owner_id IS NOT NULL) ORDER BY t.series_id,t.tier`, owner)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var a Achievement
		if err = rows.Scan(&a.ID, &a.SeriesID, &a.SeriesTitle, &a.Tier, &a.Revision, &a.Title, &a.Description, &a.Badge, &a.Metric, &a.Threshold, &a.Active, &a.Value, &a.UnlockID, &a.UnlockedAt, &a.Granted, &a.Notified, &a.ShowcaseSlot); err != nil {
			return out, err
		}
		out.Items = append(out.Items, a)
	}
	return out, rows.Err()
}
func metricValues(ctx context.Context, tx pgx.Tx, owner string) (map[string]int, error) {
	values := map[string]int{}
	var records, anime, completed, episodes, notes int
	err := tx.QueryRow(ctx, `SELECT (SELECT count(*) FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1),(SELECT count(DISTINCT e.anime_id) FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1),(SELECT count(*) FROM entries WHERE user_id=$1 AND status='completed' AND deleted_at IS NULL),(SELECT coalesce(sum(w.episode_to-w.episode_from+1),0) FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1),(SELECT count(*) FROM memory_notes WHERE owner_id=$1 AND deleted_at IS NULL)`, owner).Scan(&records, &anime, &completed, &episodes, &notes)
	values["watch_records"] = records
	values["distinct_anime"] = anime
	values["completed_anime"] = completed
	values["watched_episodes"] = episodes
	values["memory_notes"] = notes
	return values, err
}
func evaluateAchievements(ctx context.Context, tx pgx.Tx, owner string, rules []AchievementRule) (int, error) {
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226054))`, owner); err != nil {
		return 0, err
	}
	values, err := metricValues(ctx, tx, owner)
	if err != nil {
		return 0, err
	}
	granted := 0
	for _, r := range rules {
		if !r.Active {
			continue
		}
		value := values[r.Metric]
		if value > 0 {
			if _, err = tx.Exec(ctx, `INSERT INTO achievement_progress(owner_id,tier_id,rule_revision,value) VALUES($1,$2,$3,$4) ON CONFLICT(owner_id,tier_id) DO UPDATE SET rule_revision=excluded.rule_revision,value=excluded.value,updated_at=now()`, owner, r.ID, r.Revision, value); err != nil {
				return granted, err
			}
		} else {
			if _, err = tx.Exec(ctx, `DELETE FROM achievement_progress WHERE owner_id=$1 AND tier_id=$2`, owner, r.ID); err != nil {
				return granted, err
			}
		}
		if value < r.Threshold {
			continue
		}
		unlock := id.New()
		tag, err := tx.Exec(ctx, `INSERT INTO achievement_unlocks(id,owner_id,tier_id,rule_revision,source,value) VALUES($1,$2,$3,$4,'automatic',$5) ON CONFLICT(owner_id,tier_id) DO NOTHING`, unlock, owner, r.ID, r.Revision, value)
		if err != nil {
			return granted, err
		}
		if tag.RowsAffected() == 0 {
			continue
		}
		if _, err = tx.Exec(ctx, `INSERT INTO achievement_grants(unlock_id) VALUES($1)`, unlock); err != nil {
			return granted, err
		}
		if _, err = tx.Exec(ctx, `INSERT INTO achievement_grant_events(id,unlock_id,action,reason) VALUES($1,$2,'granted','满足规则自动解锁')`, id.New(), unlock); err != nil {
			return granted, err
		}
		granted++
	}
	return granted, nil
}
func (s *Service) ProcessAchievements(ctx context.Context) (bool, error) {
	rules, err := s.AchievementRules(ctx)
	if err != nil {
		return false, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(context.Background())
	var owner string
	var generation int64
	err = tx.QueryRow(ctx, `SELECT owner_id,generation FROM achievement_pending ORDER BY updated_at,owner_id FOR UPDATE SKIP LOCKED LIMIT 1`).Scan(&owner, &generation)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if _, err = evaluateAchievements(ctx, tx, owner, rules); err != nil {
		return true, err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM achievement_pending WHERE owner_id=$1 AND generation=$2`, owner, generation); err != nil {
		return true, err
	}
	return true, tx.Commit(ctx)
}
func (s *Service) AchievementShowcase(ctx context.Context, owner string, unlocks []string) error {
	if !libraryIDs(unlocks, 6) {
		return fault.Field("unlock_ids", "最多展示 6 枚已获得的徽章，不能重复。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226054))`, owner); err != nil {
		return err
	}
	var count int
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM achievement_unlocks u JOIN achievement_grants g ON g.unlock_id=u.id WHERE u.owner_id=$1 AND u.id=ANY($2::uuid[]) AND g.active`, owner, unlocks).Scan(&count); err != nil {
		return err
	}
	if count != len(unlocks) {
		return fault.Field("unlock_ids", "只能展示自己当前有效的徽章。")
	}
	if _, err = tx.Exec(ctx, `UPDATE achievement_grants g SET showcase_slot=NULL WHERE EXISTS(SELECT 1 FROM achievement_unlocks u WHERE u.id=g.unlock_id AND u.owner_id=$1)`, owner); err != nil {
		return err
	}
	for i, u := range unlocks {
		if _, err = tx.Exec(ctx, `UPDATE achievement_grants SET showcase_slot=$2 WHERE unlock_id=$1`, u, i+1); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}
func (s *Service) AcknowledgeAchievements(ctx context.Context, owner string) error {
	_, err := s.pool.Exec(ctx, `UPDATE achievement_grants g SET notified=true WHERE EXISTS(SELECT 1 FROM achievement_unlocks u WHERE u.id=g.unlock_id AND u.owner_id=$1)`, owner)
	return err
}

var seriesPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,39}$`)

func (s *Service) SaveAchievementRule(ctx context.Context, actor string, in AchievementRule) (AchievementRule, error) {
	if (in.ID != "" && !id.Valid(in.ID)) || !seriesPattern.MatchString(in.SeriesID) || in.SeriesTitle == "" || !libraryText(in.SeriesTitle, 80) || in.Title == "" || !libraryText(in.Title, 100) || !libraryText(in.Description, 1000) || in.Tier < 1 || in.Tier > 20 || in.Threshold < 1 || in.Threshold > 1000000 {
		return in, fault.Field("title", "请检查系列、等级、标题与阈值。")
	}
	switch in.Metric {
	case "watch_records", "distinct_anime", "completed_anime", "watched_episodes", "memory_notes":
	default:
		return in, fault.Field("metric", "只允许内置的统计规则。")
	}
	switch in.Badge {
	case "spark", "moon", "orbit", "flower", "book":
	default:
		return in, fault.Field("badge", "请选择内置徽章图案。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return in, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return in, err
	}
	if in.ID == "" {
		in.ID = id.New()
		in.Revision = 1
	} else {
		old, err := scanRule(tx.QueryRow(ctx, `SELECT `+ruleColumns+ruleFrom+` WHERE t.id=$1 FOR UPDATE OF t`, in.ID))
		if err != nil {
			return in, err
		}
		if err = libraryVersion(old.Revision, in.Revision); err != nil {
			return in, err
		}
		if old.SeriesID != in.SeriesID || old.Tier != in.Tier {
			return in, fault.Field("tier", "已有层级不能迁移系列；请新增规则。")
		}
		in.Revision++
	}
	if _, err = tx.Exec(ctx, `INSERT INTO achievement_series(id,title) VALUES($1,$2) ON CONFLICT(id) DO NOTHING`, in.SeriesID, in.SeriesTitle); err != nil {
		return in, err
	}
	var occupied bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM achievement_tiers WHERE series_id=$1 AND tier=$2 AND id<>$3)`, in.SeriesID, in.Tier, in.ID).Scan(&occupied); err != nil {
		return in, err
	}
	if occupied {
		return in, fault.Field("tier", "该系列等级已存在，请编辑原规则。")
	}
	if _, err = tx.Exec(ctx, `INSERT INTO achievement_tiers(id,series_id,tier,current_revision,active) VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO UPDATE SET current_revision=excluded.current_revision,active=excluded.active`, in.ID, in.SeriesID, in.Tier, in.Revision, in.Active); err != nil {
		return in, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO achievement_rule_revisions(tier_id,revision,title,description,badge,metric,threshold) VALUES($1,$2,$3,$4,$5,$6,$7)`, in.ID, in.Revision, in.Title, in.Description, in.Badge, in.Metric, in.Threshold); err != nil {
		return in, err
	}
	if err = governance.Audit(ctx, tx, actor, "achievement-rule", "achievement", in.ID, map[string]any{"revision": in.Revision, "active": in.Active}); err != nil {
		return in, err
	}
	return in, tx.Commit(ctx)
}

type AchievementGrantInput struct {
	OwnerID string `json:"owner_id"`
	TierID  string `json:"tier_id"`
	Grant   bool   `json:"grant"`
	Reason  string `json:"reason"`
}

func (s *Service) AdminAchievementGrant(ctx context.Context, actor string, in AchievementGrantInput) error {
	if !id.Valid(in.OwnerID) || !id.Valid(in.TierID) || len(in.Reason) == 0 || !libraryText(in.Reason, 1000) {
		return fault.Field("reason", "请选择账号、徽章并填写操作原因。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226054))`, in.OwnerID); err != nil {
		return err
	}
	var exists bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND NOT disabled)`, in.OwnerID).Scan(&exists); err != nil {
		return err
	}
	if !exists {
		return fault.Field("owner_id", "账号不存在或已停用。")
	}
	r, err := scanRule(tx.QueryRow(ctx, `SELECT `+ruleColumns+ruleFrom+` WHERE t.id=$1`, in.TierID))
	if err != nil {
		return err
	}
	var unlock string
	err = tx.QueryRow(ctx, `SELECT id FROM achievement_unlocks WHERE owner_id=$1 AND tier_id=$2`, in.OwnerID, in.TierID).Scan(&unlock)
	if errors.Is(err, pgx.ErrNoRows) {
		if !in.Grant {
			return fault.New("not_found", "该用户尚未获得这枚徽章。")
		}
		unlock = id.New()
		if _, err = tx.Exec(ctx, `INSERT INTO achievement_unlocks(id,owner_id,tier_id,rule_revision,source,value) VALUES($1,$2,$3,$4,'administrator',0)`, unlock, in.OwnerID, in.TierID, r.Revision); err != nil {
			return err
		}
	} else if err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO achievement_grants(unlock_id,active) VALUES($1,$2) ON CONFLICT(unlock_id) DO UPDATE SET active=excluded.active,notified=false,showcase_slot=CASE WHEN excluded.active THEN achievement_grants.showcase_slot ELSE NULL END`, unlock, in.Grant); err != nil {
		return err
	}
	action := "revoked"
	if in.Grant {
		action = "granted"
	}
	if _, err = tx.Exec(ctx, `INSERT INTO achievement_grant_events(id,unlock_id,action,actor_id,reason) VALUES($1,$2,$3,$4,$5)`, id.New(), unlock, action, actor, in.Reason); err != nil {
		return err
	}
	if err = governance.Audit(ctx, tx, actor, "achievement-"+action, "user", in.OwnerID, map[string]any{"tier_id": in.TierID, "reason": in.Reason}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// One lightweight worker reuses PostgreSQL for durable projections and bounded
// backfill batches. It never creates viewing facts or runs plugin expressions.
func (s *Service) RunMemoryWorker(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	cleanup := time.NewTicker(time.Minute)
	defer cleanup.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-cleanup.C:
			if err := s.CleanupMemoryMedia(ctx); err != nil && ctx.Err() == nil {
				slog.Error("private media cleanup failed", "error_type", "database_failure")
			}
		case <-ticker.C:
			if _, err := s.ProcessAchievements(ctx); err != nil && ctx.Err() == nil {
				slog.Error("achievement projection failed", "error_type", "database_or_task_failure")
			}
			if _, err := s.ProcessAchievementBackfill(ctx); err != nil && ctx.Err() == nil {
				slog.Error("achievement backfill failed", "error_type", "database_or_task_failure")
			}
		}
	}
}
