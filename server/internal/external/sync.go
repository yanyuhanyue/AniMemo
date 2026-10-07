package external

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"github.com/jackc/pgx/v5"
)

type SyncJob struct {
	ID              string     `json:"id"`
	Mode            string     `json:"mode"`
	IncludeProgress bool       `json:"include_progress"`
	State           string     `json:"state"`
	Cursor          int        `json:"cursor"`
	RemoteTotal     int        `json:"remote_total"`
	Error           string     `json:"error"`
	Items           []SyncItem `json:"items"`
	CreatedAt       time.Time  `json:"created_at"`
	ExpiresAt       time.Time  `json:"expires_at"`
	Owner           string     `json:"-"`
	Generation      string     `json:"-"`
	RemoteID        int64      `json:"-"`
	Attempts        int        `json:"-"`
}

const syncColumns = `id,mode,include_progress,state,cursor,remote_total,error,created_at,expires_at,user_id,generation,remote_user_id,attempts`

func scanSync(row pgx.Row) (SyncJob, error) {
	job := SyncJob{Items: []SyncItem{}}
	err := row.Scan(&job.ID, &job.Mode, &job.IncludeProgress, &job.State, &job.Cursor, &job.RemoteTotal, &job.Error, &job.CreatedAt, &job.ExpiresAt, &job.Owner, &job.Generation, &job.RemoteID, &job.Attempts)
	if errors.Is(err, pgx.ErrNoRows) {
		err = fault.New("not_found", "没有找到同步任务。")
	}
	return job, err
}

type SyncRequest struct {
	Mode            string `json:"mode"`
	IncludeProgress bool   `json:"include_progress"`
	RequestID       string `json:"request_id"`
}

func (s *Service) StartSync(ctx context.Context, owner string, input SyncRequest) (SyncJob, error) {
	if !s.oauth.Enabled() {
		return SyncJob{}, fault.New("service_unavailable", "Bangumi OAuth 尚未配置。")
	}
	if !id.Valid(input.RequestID) || !slices.Contains([]string{"pull", "push", "two_way"}, input.Mode) {
		return SyncJob{}, fault.New("validation_error", "同步模式或请求标识无效。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return SyncJob{}, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226053))`, owner); err != nil {
		return SyncJob{}, err
	}
	previous, err := scanSync(tx.QueryRow(ctx, `SELECT `+syncColumns+` FROM external_sync_jobs WHERE user_id=$1 AND request_id=$2`, owner, input.RequestID))
	if err == nil {
		if previous.Mode != input.Mode || previous.IncludeProgress != input.IncludeProgress {
			return SyncJob{}, fault.New("idempotency_conflict", "同一请求标识不能用于不同的同步设置。")
		}
		return previous, nil
	}
	var problem *fault.Error
	if !errors.As(err, &problem) || problem.Code != "not_found" {
		return SyncJob{}, err
	}
	var generation string
	var remoteID int64
	err = tx.QueryRow(ctx, `SELECT generation,remote_user_id FROM external_connections WHERE user_id=$1 AND state='connected' FOR SHARE`, owner).Scan(&generation, &remoteID)
	if errors.Is(err, pgx.ErrNoRows) {
		return SyncJob{}, fault.New("forbidden", "请先连接 Bangumi 账号。")
	}
	if err != nil {
		return SyncJob{}, err
	}
	var active bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM external_sync_jobs WHERE user_id=$1 AND state IN ('fetching','ready','applying'))`, owner).Scan(&active); err != nil {
		return SyncJob{}, err
	}
	if active {
		return SyncJob{}, fault.New("version_conflict", "已有未完成的同步预览，请先完成或取消。")
	}
	job, err := scanSync(tx.QueryRow(ctx, `INSERT INTO external_sync_jobs(id,user_id,remote_user_id,generation,request_id,mode,include_progress) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING `+syncColumns, id.New(), owner, remoteID, generation, input.RequestID, input.Mode, input.IncludeProgress))
	if err != nil {
		return job, err
	}
	return job, tx.Commit(ctx)
}
func (s *Service) SyncJobs(ctx context.Context, owner string) ([]SyncJob, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+syncColumns+` FROM external_sync_jobs WHERE user_id=$1 ORDER BY created_at DESC,id LIMIT 20`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []SyncJob{}
	for rows.Next() {
		job, err := scanSync(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, job)
	}
	return out, rows.Err()
}
func (s *Service) SyncJob(ctx context.Context, owner, jobID string) (SyncJob, error) {
	if !id.Valid(jobID) {
		return SyncJob{}, fault.New("not_found", "没有找到同步任务。")
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return SyncJob{}, err
	}
	defer tx.Rollback(context.Background())
	job, err := scanSync(tx.QueryRow(ctx, `SELECT `+syncColumns+` FROM external_sync_jobs WHERE id=$1 AND user_id=$2`, jobID, owner))
	if err != nil {
		return job, err
	}
	rows, err := tx.Query(ctx, `SELECT data,state,action,result FROM external_sync_items WHERE job_id=$1 ORDER BY subject_id`, jobID)
	if err != nil {
		return job, err
	}
	for rows.Next() {
		var item SyncItem
		var state, action, result string
		if err = rows.Scan(&item, &state, &action, &result); err != nil {
			rows.Close()
			return job, err
		}
		item.State, item.Action, item.Result = state, action, result
		job.Items = append(job.Items, item)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return job, err
	}
	return job, tx.Commit(ctx)
}

type SyncChoice struct {
	ID     string `json:"id"`
	Action string `json:"action"`
}
type SyncAction struct {
	Action  string       `json:"action"`
	Choices []SyncChoice `json:"choices"`
}

func (s *Service) ChangeSync(ctx context.Context, owner, jobID string, input SyncAction) (SyncJob, error) {
	if !id.Valid(jobID) || len(input.Choices) > 1000 {
		return SyncJob{}, fault.New("validation_error", "同步任务或选择数量无效。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return SyncJob{}, err
	}
	defer tx.Rollback(context.Background())
	job, err := scanSync(tx.QueryRow(ctx, `SELECT `+syncColumns+` FROM external_sync_jobs WHERE id=$1 AND user_id=$2 FOR UPDATE`, jobID, owner))
	if err != nil {
		return job, err
	}
	if input.Action == "cancel" {
		if job.State == "done" {
			return job, fault.New("version_conflict", "已完成的同步不能撤销，请重新预览差异。")
		}
		if _, err = tx.Exec(ctx, `UPDATE external_sync_jobs SET state='cancelled',updated_at=now() WHERE id=$1`, jobID); err != nil {
			return job, err
		}
	} else if input.Action == "apply" {
		if job.State == "applying" || job.State == "done" {
			return job, nil
		}
		if job.State != "ready" || !job.ExpiresAt.After(time.Now()) {
			return job, fault.New("version_conflict", "请等待预览完成；已过期的预览需要重新生成。")
		}
		var connected bool
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM external_connections WHERE user_id=$1 AND generation=$2 AND state='connected')`, owner, job.Generation).Scan(&connected); err != nil {
			return job, err
		}
		if !connected {
			return job, fault.New("version_conflict", "账号连接已改变，请重新预览。")
		}
		rows, err := tx.Query(ctx, `SELECT data FROM external_sync_items WHERE job_id=$1`, jobID)
		if err != nil {
			return job, err
		}
		items := map[string]SyncItem{}
		for rows.Next() {
			var item SyncItem
			if err = rows.Scan(&item); err != nil {
				rows.Close()
				return job, err
			}
			items[item.ID] = item
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return job, err
		}
		seen := map[string]bool{}
		for _, choice := range input.Choices {
			item, ok := items[choice.ID]
			if !ok || seen[choice.ID] || !slices.Contains(item.Allowed, choice.Action) {
				return job, fault.New("validation_error", "同步选择无效或重复，请重新打开预览。")
			}
			seen[choice.ID] = true
			if _, err = tx.Exec(ctx, `UPDATE external_sync_items SET action=$2 WHERE id=$1 AND job_id=$3`, choice.ID, choice.Action, jobID); err != nil {
				return job, err
			}
		}
		if _, err = tx.Exec(ctx, `UPDATE external_sync_jobs SET state='applying',expires_at=now()+interval '24 hours',updated_at=now() WHERE id=$1`, jobID); err != nil {
			return job, err
		}
	} else {
		return job, fault.Field("action", "同步操作无效。")
	}
	if err = tx.Commit(ctx); err != nil {
		return job, err
	}
	return s.SyncJob(ctx, owner, jobID)
}

func (s *Service) RunSyncWorker(ctx context.Context) {
	if !s.oauth.Enabled() {
		return
	}
	for ctx.Err() == nil {
		worked, err := s.ProcessNextSync(ctx)
		if err != nil && ctx.Err() == nil {
			slog.Error("external sync worker failed", "error_type", "database_or_provider_failure")
		}
		if !worked || err != nil {
			timer := time.NewTimer(500 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
		}
	}
}
func (s *Service) ProcessNextSync(ctx context.Context) (bool, error) {
	if !s.oauth.Enabled() {
		return false, nil
	}
	if _, err := s.pool.Exec(ctx, `UPDATE external_sync_jobs j SET state='cancelled',error='任务过期或连接已经改变，请重新预览。',updated_at=now() WHERE state IN ('fetching','ready','applying') AND (expires_at<=now() OR NOT EXISTS(SELECT 1 FROM external_connections c JOIN users u ON u.id=c.user_id WHERE c.user_id=j.user_id AND c.generation=j.generation AND c.state='connected' AND NOT u.disabled))`); err != nil {
		return false, err
	}
	var jobID string
	err := s.pool.QueryRow(ctx, `SELECT id FROM external_sync_jobs j WHERE (state='fetching' AND available_at<=now()) OR (state='applying' AND (EXISTS(SELECT 1 FROM external_sync_items WHERE job_id=j.id AND state IN ('pending','running') AND available_at<=now()) OR NOT EXISTS(SELECT 1 FROM external_sync_items WHERE job_id=j.id AND state IN ('pending','running')))) ORDER BY created_at,id LIMIT 1`).Scan(&jobID)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return false, err
	}
	var locked bool
	err = conn.QueryRow(ctx, `SELECT pg_try_advisory_lock(hashtextextended($1,718226052))`, jobID).Scan(&locked)
	if err != nil || !locked {
		conn.Release()
		return false, err
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if _, err := conn.Exec(cleanup, `SELECT pg_advisory_unlock(hashtextextended($1,718226052))`, jobID); err != nil {
			_ = conn.Conn().Close(cleanup)
		}
		conn.Release()
	}()
	job, err := scanSync(conn.QueryRow(ctx, `SELECT `+syncColumns+` FROM external_sync_jobs WHERE id=$1`, jobID))
	if err != nil {
		return false, err
	}
	switch job.State {
	case "fetching":
		return true, s.fetchSync(ctx, job)
	case "applying":
		return true, s.applyNextSync(ctx, job)
	default:
		return false, nil
	}
}
