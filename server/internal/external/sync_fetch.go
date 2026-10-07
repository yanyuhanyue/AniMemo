package external

import (
	"context"
	"errors"
	"time"

	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/journal"
)

func retryable(err error) bool {
	var problem *fault.Error
	return bangumi.Retryable(err) || (errors.As(err, &problem) && (problem.Code == "service_unavailable" || problem.Code == "rate_limited"))
}
func retryDelay(err error, attempts int) time.Duration {
	delay := time.Duration(1<<min(attempts, 8)) * time.Second
	var upstream *bangumi.UpstreamError
	if errors.As(err, &upstream) {
		delay = max(delay, upstream.RetryAfter)
	}
	return min(delay, time.Hour)
}
func (s *Service) collectionRecord(ctx context.Context, collection bangumi.Collection, token string, progress bool) (*RemoteRecord, error) {
	if collection.SubjectID < 1 || collection.SubjectType != 2 || remoteStatuses[collection.Type] == "" || collection.Rate < 0 || collection.Rate > 10 || collection.Episodes < 0 || collection.Episodes > 10000 || len(collection.Tags) > 64 {
		return nil, fault.New("validation_error", "Bangumi 返回的收藏格式超出支持范围。")
	}
	out := &RemoteRecord{Value: collectionValue(collection), Private: collection.Private, Episodes: []EpisodeState{}}
	if progress {
		raw, err := s.provider.Episodes(ctx, collection.SubjectID, token)
		if err != nil {
			return nil, err
		}
		out.Episodes, err = episodeStates(raw)
		if err != nil {
			return nil, err
		}
	}
	return out, nil
}
func (s *Service) readRemote(ctx context.Context, remoteID, subject int64, token string, progress bool) (*RemoteRecord, error) {
	collection, err := s.provider.Collection(ctx, remoteID, subject, token)
	if err != nil || collection == nil {
		return nil, err
	}
	return s.collectionRecord(ctx, *collection, token, progress)
}
func (s *Service) fetchSync(ctx context.Context, job SyncJob) error {
	err := s.fetchSyncPage(ctx, job)
	if err == nil || ctx.Err() != nil {
		return err
	}
	if retryable(err) && job.Attempts < 5 {
		_, saveErr := s.pool.Exec(ctx, `UPDATE external_sync_jobs SET attempts=attempts+1,available_at=$2,error=$3,updated_at=now() WHERE id=$1 AND state='fetching'`, job.ID, time.Now().Add(retryDelay(err, job.Attempts+1)), problemText(err))
		return saveErr
	}
	_, saveErr := s.pool.Exec(ctx, `UPDATE external_sync_jobs SET state='failed',error=$2,updated_at=now() WHERE id=$1 AND state='fetching'`, job.ID, problemText(err))
	return saveErr
}
func (s *Service) fetchSyncPage(ctx context.Context, job SyncJob) error {
	token, remoteID, err := s.connectionToken(ctx, job.Owner, job.Generation)
	if err != nil {
		return err
	}
	if remoteID != job.RemoteID {
		return fault.New("version_conflict", "外部账号身份已改变。")
	}
	page, err := s.provider.Collections(ctx, remoteID, token, job.Cursor)
	if err != nil {
		return err
	}
	if page.Total < 0 || page.Total > 1000 || len(page.Data) > 50 || page.Offset != job.Cursor || (len(page.Data) == 0 && job.Cursor < page.Total) {
		return fault.New("validation_error", "收藏分页已变化或超过单次 1000 部的限制，请重新生成预览。")
	}
	for _, collection := range page.Data {
		remote, err := s.collectionRecord(ctx, collection, token, job.IncludeProgress)
		if err != nil {
			return err
		}
		subject := collection.Subject
		subject.ID = collection.SubjectID
		subject.Type = 2
		metadata := metadataFor(subject).Metadata
		item := SyncItem{ID: id.New(), SubjectID: collection.SubjectID, Title: metadata.Title, Metadata: metadata, Remote: remote, Catalog: []EpisodeState{}, Allowed: []string{"skip"}, Decision: "skip", State: "pending", Action: "skip"}
		tag, err := s.pool.Exec(ctx, `INSERT INTO external_sync_items(id,job_id,subject_id,data) SELECT $1,$2,$3,$4 WHERE EXISTS(SELECT 1 FROM external_sync_jobs WHERE id=$2 AND state='fetching') ON CONFLICT(job_id,subject_id) DO NOTHING`, item.ID, job.ID, item.SubjectID, item)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var active bool
			if err = s.pool.QueryRow(ctx, `SELECT state='fetching' FROM external_sync_jobs WHERE id=$1`, job.ID).Scan(&active); err != nil {
				return err
			}
			if !active {
				return nil
			}
		}
	}
	cursor := job.Cursor + len(page.Data)
	if _, err = s.pool.Exec(ctx, `UPDATE external_sync_jobs SET cursor=$2,remote_total=$3,attempts=0,available_at=now(),error='',updated_at=now() WHERE id=$1 AND state='fetching'`, job.ID, cursor, page.Total); err != nil {
		return err
	}
	if cursor >= page.Total {
		return s.prepareSync(ctx, job, token)
	}
	return nil
}
func (s *Service) prepareSync(ctx context.Context, job SyncJob, token string) error {
	current, err := s.SyncJob(ctx, job.Owner, job.ID)
	if err != nil {
		return err
	}
	if current.State != "fetching" {
		return nil
	}
	locals, err := s.journal.SourceRecords(ctx, job.Owner)
	if err != nil {
		return err
	}
	if len(locals) > 1000 {
		return fault.New("validation_error", "单次同步最多预览 1000 部已绑定的番剧。")
	}
	baselines, err := s.journal.SyncBaselines(ctx, job.Owner, job.RemoteID)
	if err != nil {
		return err
	}
	items := map[int64]SyncItem{}
	for _, item := range current.Items {
		items[item.SubjectID] = item
	}
	for _, local := range locals {
		item, exists := items[local.SubjectID]
		if !exists {
			remote, err := s.readRemote(ctx, job.RemoteID, local.SubjectID, token, job.IncludeProgress)
			if err != nil {
				return err
			}
			item = SyncItem{ID: id.New(), SubjectID: local.SubjectID, Title: local.Title, Metadata: local.Metadata, Remote: remote, Catalog: []EpisodeState{}, State: "pending", Action: "skip"}
			if remote == nil && job.IncludeProgress {
				catalog, err := s.provider.EpisodeCatalog(ctx, local.SubjectID)
				if err != nil {
					return err
				}
				item.Catalog, err = episodeStates(catalog)
				if err != nil {
					return err
				}
			}
		}
		copy := local
		item.Local = &copy
		items[local.SubjectID] = item
	}
	if len(items) > 1000 {
		return fault.New("validation_error", "双方合计超过单次 1000 部的预览限制。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var state string
	if err = tx.QueryRow(ctx, `SELECT state FROM external_sync_jobs WHERE id=$1 FOR UPDATE`, job.ID).Scan(&state); err != nil {
		return err
	}
	if state != "fetching" {
		return nil
	}
	for _, item := range items {
		var baseline *journal.SyncBaseline
		if value, ok := baselines[item.SubjectID]; ok {
			copy := value
			baseline = &copy
		}
		planItem(&item, job.Mode, job.IncludeProgress, baseline)
		if _, err = tx.Exec(ctx, `INSERT INTO external_sync_items(id,job_id,subject_id,data) VALUES($1,$2,$3,$4) ON CONFLICT(job_id,subject_id) DO UPDATE SET data=excluded.data`, item.ID, job.ID, item.SubjectID, item); err != nil {
			return err
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE external_sync_jobs SET state='ready',error='',updated_at=now() WHERE id=$1`, job.ID); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
