package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"errors"
	"github.com/jackc/pgx/v5"
	"strconv"
	"time"
)

type PortableGrantEvent struct {
	Action    string    `json:"action"`
	Reason    string    `json:"reason"`
	CreatedAt time.Time `json:"created_at"`
}
type PortableAchievement struct {
	Rule         AchievementRule      `json:"rule"`
	UnlockID     string               `json:"unlock_id"`
	Source       string               `json:"source"`
	Value        int                  `json:"value"`
	UnlockedAt   time.Time            `json:"unlocked_at"`
	Granted      bool                 `json:"granted"`
	Notified     bool                 `json:"notified"`
	ShowcaseSlot int                  `json:"showcase_slot"`
	Events       []PortableGrantEvent `json:"events"`
}
type PortableAchievementProgress struct {
	Rule      AchievementRule `json:"rule"`
	Value     int             `json:"value"`
	UpdatedAt time.Time       `json:"updated_at"`
}
type AchievementBundle struct {
	Unlocks  []PortableAchievement         `json:"unlocks"`
	Progress []PortableAchievementProgress `json:"progress"`
}

func exportAchievements(ctx context.Context, tx pgx.Tx, owner string) (AchievementBundle, error) {
	out := AchievementBundle{Unlocks: []PortableAchievement{}, Progress: []PortableAchievementProgress{}}
	rows, err := tx.Query(ctx, `SELECT t.id,t.series_id,s.title,t.tier,r.revision,r.title,r.description,r.badge,r.metric,r.threshold,t.active,u.id,u.source,u.value,u.unlocked_at,g.active,g.notified,coalesce(g.showcase_slot,0) FROM achievement_unlocks u JOIN achievement_tiers t ON t.id=u.tier_id JOIN achievement_series s ON s.id=t.series_id JOIN achievement_rule_revisions r ON r.tier_id=t.id AND r.revision=u.rule_revision JOIN achievement_grants g ON g.unlock_id=u.id WHERE u.owner_id=$1 ORDER BY u.unlocked_at,u.id`, owner)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		var a PortableAchievement
		r := &a.Rule
		if err = rows.Scan(&r.ID, &r.SeriesID, &r.SeriesTitle, &r.Tier, &r.Revision, &r.Title, &r.Description, &r.Badge, &r.Metric, &r.Threshold, &r.Active, &a.UnlockID, &a.Source, &a.Value, &a.UnlockedAt, &a.Granted, &a.Notified, &a.ShowcaseSlot); err != nil {
			rows.Close()
			return out, err
		}
		out.Unlocks = append(out.Unlocks, a)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	for i := range out.Unlocks {
		a := &out.Unlocks[i]
		a.Events, err = collectLibrary(ctx, tx, `SELECT action,reason,created_at FROM achievement_grant_events WHERE unlock_id=$1 ORDER BY created_at,id`, a.UnlockID, func(row pgx.Row) (PortableGrantEvent, error) {
			var e PortableGrantEvent
			err := row.Scan(&e.Action, &e.Reason, &e.CreatedAt)
			return e, err
		})
		if err != nil {
			return out, err
		}
	}
	rows, err = tx.Query(ctx, `SELECT t.id,t.series_id,s.title,t.tier,r.revision,r.title,r.description,r.badge,r.metric,r.threshold,t.active,p.value,p.updated_at FROM achievement_progress p JOIN achievement_tiers t ON t.id=p.tier_id JOIN achievement_series s ON s.id=t.series_id JOIN achievement_rule_revisions r ON r.tier_id=t.id AND r.revision=p.rule_revision WHERE p.owner_id=$1 ORDER BY t.id`, owner)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var p PortableAchievementProgress
		r := &p.Rule
		if err = rows.Scan(&r.ID, &r.SeriesID, &r.SeriesTitle, &r.Tier, &r.Revision, &r.Title, &r.Description, &r.Badge, &r.Metric, &r.Threshold, &r.Active, &p.Value, &p.UpdatedAt); err != nil {
			return out, err
		}
		out.Progress = append(out.Progress, p)
	}
	return out, rows.Err()
}
func validateAchievementBundle(b AchievementBundle) error {
	bad := fault.New("validation_error", "成就备份包含无效或重复的规则、解锁或授予历史。")
	if len(b.Unlocks) > 2000 || len(b.Progress) > 2000 {
		return bad
	}
	rules := map[string]AchievementRule{}
	unlocks := map[string]bool{}
	tiers := map[string]bool{}
	slots := map[int]bool{}
	ruleOK := func(r AchievementRule) bool {
		if !id.Valid(r.ID) || r.SeriesID == "" || !libraryText(r.SeriesID, 100) || r.SeriesTitle == "" || !libraryText(r.SeriesTitle, 80) || r.Tier < 1 || r.Tier > 20 || r.Revision < 1 || r.Revision > 100000 || r.Title == "" || !libraryText(r.Title, 100) || !libraryText(r.Description, 1000) || r.Threshold < 1 || r.Threshold > 1000000 {
			return false
		}
		switch r.Badge {
		case "spark", "moon", "orbit", "flower", "book":
		default:
			return false
		}
		switch r.Metric {
		case "watch_records", "distinct_anime", "completed_anime", "watched_episodes", "memory_notes":
		default:
			return false
		}
		key := r.ID + ":" + strconv.Itoa(r.Revision)
		if old, ok := rules[key]; ok && old != r {
			return false
		}
		rules[key] = r
		return true
	}
	for _, a := range b.Unlocks {
		if !ruleOK(a.Rule) || !id.Valid(a.UnlockID) || unlocks[a.UnlockID] || tiers[a.Rule.ID] || (a.Source != "automatic" && a.Source != "administrator") || a.Value < 0 || a.UnlockedAt.IsZero() || a.ShowcaseSlot < 0 || a.ShowcaseSlot > 6 || len(a.Events) > 10000 {
			return bad
		}
		unlocks[a.UnlockID] = true
		tiers[a.Rule.ID] = true
		if a.ShowcaseSlot > 0 {
			if !a.Granted || slots[a.ShowcaseSlot] {
				return bad
			}
			slots[a.ShowcaseSlot] = true
		}
		for _, e := range a.Events {
			if (e.Action != "granted" && e.Action != "revoked") || !libraryText(e.Reason, 1000) || e.CreatedAt.IsZero() {
				return bad
			}
		}
	}
	progress := map[string]bool{}
	for _, p := range b.Progress {
		if !ruleOK(p.Rule) || progress[p.Rule.ID] || p.Value < 1 || p.UpdatedAt.IsZero() {
			return bad
		}
		progress[p.Rule.ID] = true
	}
	return nil
}
func restoreAchievements(ctx context.Context, tx pgx.Tx, owner string, b AchievementBundle) error {
	// Imported rules can preserve a personal award, but can never activate a site rule.
	mapping := map[string]string{}
	byTier := map[string]map[int]AchievementRule{}
	remember := func(r AchievementRule) {
		if byTier[r.ID] == nil {
			byTier[r.ID] = map[int]AchievementRule{}
		}
		byTier[r.ID][r.Revision] = r
	}
	for _, a := range b.Unlocks {
		remember(a.Rule)
	}
	for _, p := range b.Progress {
		remember(p.Rule)
	}
	series := map[string]string{}
	ensure := func(r AchievementRule) (string, error) {
		target := mapping[r.ID]
		if target == "" {
			matching := true
			var err error
			for _, required := range byTier[r.ID] {
				var same bool
				err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM achievement_tiers t JOIN achievement_series s ON s.id=t.series_id JOIN achievement_rule_revisions r ON r.tier_id=t.id WHERE t.id=$1 AND t.series_id=$2 AND s.title=$3 AND t.tier=$4 AND r.revision=$5 AND r.title=$6 AND r.description=$7 AND r.badge=$8 AND r.metric=$9 AND r.threshold=$10)`, required.ID, required.SeriesID, required.SeriesTitle, required.Tier, required.Revision, required.Title, required.Description, required.Badge, required.Metric, required.Threshold).Scan(&same)
				if err != nil {
					return "", err
				}
				if !same {
					matching = false
					break
				}
			}
			if matching {
				target = r.ID
			} else {
				target = id.New()
				sid := series[r.SeriesID]
				if sid == "" {
					sid = "archive." + id.New()
					series[r.SeriesID] = sid
					if _, err = tx.Exec(ctx, `INSERT INTO achievement_series(id,title) VALUES($1,$2)`, sid, r.SeriesTitle); err != nil {
						return "", err
					}
				}
				if _, err = tx.Exec(ctx, `INSERT INTO achievement_tiers(id,series_id,tier,current_revision,active,archived) VALUES($1,$2,$3,$4,false,true)`, target, sid, r.Tier, r.Revision); err != nil {
					return "", err
				}
			}
			mapping[r.ID] = target
		}
		var old AchievementRule
		var err error
		old, err = scanRule(tx.QueryRow(ctx, `SELECT `+ruleColumns+` FROM achievement_tiers t JOIN achievement_series s ON s.id=t.series_id JOIN achievement_rule_revisions r ON r.tier_id=t.id WHERE t.id=$1 AND r.revision=$2`, target, r.Revision))
		var missing *fault.Error
		if errors.As(err, &missing) && missing.Code == "not_found" {
			_, err = tx.Exec(ctx, `INSERT INTO achievement_rule_revisions(tier_id,revision,title,description,badge,metric,threshold) VALUES($1,$2,$3,$4,$5,$6,$7)`, target, r.Revision, r.Title, r.Description, r.Badge, r.Metric, r.Threshold)
			return target, err
		}
		if err != nil {
			return "", err
		}
		if old.Title != r.Title || old.Description != r.Description || old.Metric != r.Metric || old.Badge != r.Badge || old.Threshold != r.Threshold {
			return "", fault.New("validation_error", "目标实例存在同编号但内容不同的成就修订，未恢复任何内容。")
		}
		return target, nil
	}
	for _, a := range b.Unlocks {
		tier, err := ensure(a.Rule)
		if err != nil {
			return err
		}
		unlock := id.New()
		if _, err = tx.Exec(ctx, `INSERT INTO achievement_unlocks(id,owner_id,tier_id,rule_revision,source,value,unlocked_at) VALUES($1,$2,$3,$4,$5,$6,$7)`, unlock, owner, tier, a.Rule.Revision, a.Source, a.Value, a.UnlockedAt); err != nil {
			return err
		}
		var slot any
		if a.ShowcaseSlot > 0 {
			slot = a.ShowcaseSlot
		}
		if _, err = tx.Exec(ctx, `INSERT INTO achievement_grants(unlock_id,active,notified,showcase_slot) VALUES($1,$2,$3,$4)`, unlock, a.Granted, a.Notified, slot); err != nil {
			return err
		}
		for _, e := range a.Events {
			if _, err = tx.Exec(ctx, `INSERT INTO achievement_grant_events(id,unlock_id,action,reason,created_at) VALUES($1,$2,$3,$4,$5)`, id.New(), unlock, e.Action, e.Reason, e.CreatedAt); err != nil {
				return err
			}
		}
	}
	for _, p := range b.Progress {
		tier, err := ensure(p.Rule)
		if err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `INSERT INTO achievement_progress(owner_id,tier_id,rule_revision,value,updated_at) VALUES($1,$2,$3,$4,$5)`, owner, tier, p.Rule.Revision, p.Value, p.UpdatedAt); err != nil {
			return err
		}
	}
	return nil
}
