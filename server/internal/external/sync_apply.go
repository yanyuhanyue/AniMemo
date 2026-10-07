package external

import (
	"context"
	"errors"
	"time"

	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
	"github.com/jackc/pgx/v5"
)

func (s *Service) syncActive(ctx context.Context, job SyncJob) error {
	var active bool
	err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM external_sync_jobs j JOIN external_connections c ON c.user_id=j.user_id JOIN users u ON u.id=j.user_id WHERE j.id=$1 AND j.state='applying' AND c.generation=j.generation AND c.state='connected' AND NOT u.disabled)`, job.ID).Scan(&active)
	if err != nil {
		return err
	}
	if !active {
		return fault.New("version_conflict", "同步已取消或账号连接已经改变。")
	}
	return nil
}
func (s *Service) checkLocal(ctx context.Context, job SyncJob, item SyncItem) error {
	if item.Local == nil {
		return nil
	}
	entry, err := s.journal.Get(ctx, job.Owner, item.Local.EntryID)
	if err != nil {
		return err
	}
	if entry.Version != item.Local.Version || entry.Source == nil || entry.Source.SubjectID != item.SubjectID {
		return fault.New("version_conflict", "预览后本地记录已修改，当前条目停止同步。")
	}
	return nil
}
func (s *Service) applyNextSync(ctx context.Context, job SyncJob) error {
	var item SyncItem
	var action string
	var attempts int
	var writeStarted bool
	err := s.pool.QueryRow(ctx, `UPDATE external_sync_items SET state='running',attempts=attempts+1 WHERE id=(SELECT id FROM external_sync_items WHERE job_id=$1 AND state IN ('pending','running') AND available_at<=now() ORDER BY subject_id LIMIT 1) AND EXISTS(SELECT 1 FROM external_sync_jobs WHERE id=$1 AND state='applying') RETURNING data,action,attempts,write_started`, job.ID).Scan(&item, &action, &attempts, &writeStarted)
	if errors.Is(err, pgx.ErrNoRows) {
		_, err = s.pool.Exec(ctx, `UPDATE external_sync_jobs SET state='done',error=CASE WHEN EXISTS(SELECT 1 FROM external_sync_items WHERE job_id=$1 AND state IN ('failed','conflict')) THEN '部分条目未同步，请核对结果后重新预览。' ELSE '' END,updated_at=now() WHERE id=$1 AND state='applying' AND NOT EXISTS(SELECT 1 FROM external_sync_items WHERE job_id=$1 AND state IN ('pending','running'))`, job.ID)
		return err
	}
	if err != nil {
		return err
	}
	if err = s.syncActive(ctx, job); err != nil {
		return s.finishSyncItem(ctx, job, item, "conflict", problemText(err))
	}
	if action == "skip" {
		if item.Decision == "none" && item.Local != nil && item.Remote != nil {
			err = s.journal.MarkSynced(ctx, job.Owner, job.RemoteID, *item.Local, item.Remote.Value, progressHash(item.Remote, job.IncludeProgress))
			if err != nil {
				return s.finishSyncItem(ctx, job, item, "conflict", problemText(err))
			}
		}
		return s.finishSyncItem(ctx, job, item, "skipped", "已保留现状。")
	}
	if action == "pull" {
		if completed, err := s.journal.CompletedSync(ctx, job.Owner, item.ID); err != nil {
			return err
		} else if completed {
			return s.finishSyncItem(ctx, job, item, "done", "本地提交已恢复确认。")
		}
	}
	token, remoteID, err := s.connectionToken(ctx, job.Owner, job.Generation)
	if err == nil && remoteID != job.RemoteID {
		err = fault.New("version_conflict", "外部账号身份已改变。")
	}
	if err == nil {
		err = s.executeSyncItem(ctx, job, item, action, token, writeStarted)
	}
	if err == nil {
		return s.finishSyncItem(ctx, job, item, "done", "已完成并核对结果。")
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if retryable(err) && attempts < 6 {
		_, saveErr := s.pool.Exec(ctx, `UPDATE external_sync_items SET state='pending',available_at=$2,result='上游请求未完成，稍后核对实际状态。' WHERE id=$1 AND EXISTS(SELECT 1 FROM external_sync_jobs WHERE id=$3 AND state='applying')`, item.ID, time.Now().Add(retryDelay(err, attempts)), job.ID)
		return saveErr
	}
	state := "failed"
	var problem *fault.Error
	if errors.As(err, &problem) && (problem.Code == "version_conflict" || problem.Code == "not_found") {
		state = "conflict"
	}
	return s.finishSyncItem(ctx, job, item, state, problemText(err))
}
func progressHash(remote *RemoteRecord, progress bool) string {
	if progress && remote != nil {
		return episodeHash(remote.Episodes)
	}
	return ""
}
func (s *Service) finishSyncItem(ctx context.Context, job SyncJob, item SyncItem, state, result string) error {
	_, err := s.pool.Exec(ctx, `UPDATE external_sync_items SET state=$2,result=$3 WHERE id=$1 AND EXISTS(SELECT 1 FROM external_sync_jobs WHERE id=$4 AND state='applying')`, item.ID, state, result, job.ID)
	return err
}
func (s *Service) executeSyncItem(ctx context.Context, job SyncJob, item SyncItem, action, token string, writeStarted bool) error {
	if err := s.checkLocal(ctx, job, item); err != nil {
		return err
	}
	current, err := s.readRemote(ctx, job.RemoteID, item.SubjectID, token, job.IncludeProgress)
	if err != nil {
		return err
	}
	if action == "pull" {
		if item.Remote == nil || !remoteEqual(current, item.Remote, job.IncludeProgress) {
			return fault.New("version_conflict", "预览后 Bangumi 收藏已改变，当前条目停止导入。")
		}
		if err = s.syncActive(ctx, job); err != nil {
			return err
		}
		_, err = s.journal.ApplyRemote(ctx, job.Owner, item.ID, job.RemoteID, item.Metadata, item.Local, current.Value, job.IncludeProgress, progressHash(current, job.IncludeProgress))
		return err
	}
	if action != "push" || item.Local == nil || item.PushTarget == nil {
		return fault.New("validation_error", "当前条目不支持所选同步方向。")
	}
	target := item.PushTarget
	if remoteEqual(current, target, job.IncludeProgress) {
		return s.journal.MarkSynced(ctx, job.Owner, job.RemoteID, *item.Local, current.Value, progressHash(current, job.IncludeProgress))
	}
	if !remoteEqual(current, item.Remote, job.IncludeProgress) && (!writeStarted || !compatibleIntermediate(current, item.Remote, target, job.IncludeProgress)) {
		return fault.New("version_conflict", "预览后 Bangumi 收藏已改变，当前条目停止写回。")
	}
	beforeWrite := func() error {
		if err = s.checkLocal(ctx, job, item); err != nil {
			return err
		}
		if err = s.syncActive(ctx, job); err != nil {
			return err
		}
		tag, saveErr := s.pool.Exec(ctx, `UPDATE external_sync_items SET write_started=true WHERE id=$1 AND EXISTS(SELECT 1 FROM external_sync_jobs WHERE id=$2 AND state='applying')`, item.ID, job.ID)
		if saveErr != nil {
			return saveErr
		}
		if tag.RowsAffected() != 1 {
			return fault.New("version_conflict", "同步已取消。")
		}
		return nil
	}
	if current == nil || !journal.SyncValuesEqual(current.Value, target.Value, false) || current.Private != target.Private {
		if err = beforeWrite(); err != nil {
			return err
		}
		payload := bangumi.CollectionWrite{Type: localStatuses[target.Value.Status], Rate: int(target.Value.Score), Comment: target.Value.Notes, Tags: target.Value.Tags, Private: target.Private}
		if err = s.provider.WriteCollection(ctx, item.SubjectID, token, payload); err != nil {
			return err
		}
		current, err = s.readRemote(ctx, job.RemoteID, item.SubjectID, token, job.IncludeProgress)
		if err != nil {
			return err
		}
		if !compatibleIntermediate(current, item.Remote, target, job.IncludeProgress) {
			return fault.New("version_conflict", "Bangumi 已接收写入，但返回状态与预览不一致，请重新核对。")
		}
	}
	if job.IncludeProgress {
		for _, state := range []int{0, 2} {
			actual := map[int64]int{}
			if current != nil {
				for _, ep := range current.Episodes {
					actual[ep.ID] = ep.State
				}
			}
			ids := []int64{}
			for _, ep := range target.Episodes {
				if ep.State == state && actual[ep.ID] != state {
					ids = append(ids, ep.ID)
				}
			}
			if len(ids) == 0 {
				continue
			}
			if err = beforeWrite(); err != nil {
				return err
			}
			if err = s.provider.WriteEpisodes(ctx, item.SubjectID, token, ids, state); err != nil {
				return err
			}
			current, err = s.readRemote(ctx, job.RemoteID, item.SubjectID, token, true)
			if err != nil {
				return err
			}
			if !compatibleIntermediate(current, item.Remote, target, true) {
				return fault.New("version_conflict", "逐话状态写入后又发生变化，请核对 Bangumi 收藏。")
			}
		}
	}
	if !remoteEqual(current, target, job.IncludeProgress) {
		return fault.New("version_conflict", "Bangumi 返回状态未达到预览目标，请重新核对。")
	}
	return s.journal.MarkSynced(ctx, job.Owner, job.RemoteID, *item.Local, current.Value, progressHash(current, job.IncludeProgress))
}
