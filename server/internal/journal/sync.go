package journal

import (
	"animemo.local/server/internal/telemetry"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"slices"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"github.com/jackc/pgx/v5"
)

type SyncValue struct {
	Status   string   `json:"status"`
	Score    float64  `json:"score"`
	Notes    string   `json:"notes"`
	Tags     []string `json:"tags"`
	Progress int      `json:"progress"`
}

func (v SyncValue) Normalized() SyncValue {
	v.Tags = append([]string{}, v.Tags...)
	slices.Sort(v.Tags)
	return v
}
func SyncValuesEqual(a, b SyncValue, progress bool) bool {
	a, b = a.Normalized(), b.Normalized()
	return a.Status == b.Status && a.Score == b.Score && a.Notes == b.Notes && slices.Equal(a.Tags, b.Tags) && (!progress || a.Progress == b.Progress)
}
func SyncValueOf(entry Entry) SyncValue {
	out := SyncValue{Status: entry.Status, Notes: entry.Notes, Tags: entry.Tags, Progress: entry.WatchedEpisodes}
	if entry.Score != nil {
		out.Score = *entry.Score
	}
	return out.Normalized()
}

type SourceRecord struct {
	EntryID          string         `json:"entry_id"`
	Version          int            `json:"version"`
	SubjectID        int64          `json:"subject_id"`
	Title            string         `json:"title"`
	TotalEpisodes    int            `json:"total_episodes"`
	RecordedProgress int            `json:"recorded_progress"`
	Value            SyncValue      `json:"value"`
	Metadata         SourceMetadata `json:"metadata"`
}
type SyncBaseline struct {
	Local       SyncValue `json:"local"`
	Remote      SyncValue `json:"remote"`
	EpisodeHash string    `json:"episode_hash"`
}

func (s *Service) SourceRecords(ctx context.Context, owner string) ([]SourceRecord, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+columns+`,coalesce((SELECT max(episode_to) FROM watch_records WHERE entry_id=entries.id),0) FROM entries WHERE user_id=$1 AND deleted_at IS NULL AND EXISTS(SELECT 1 FROM entry_sources WHERE entry_id=entries.id) ORDER BY id LIMIT 1001`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []SourceRecord{}
	for rows.Next() {
		var recorded int
		entry, err := scanEntry(rows, &recorded)
		if err != nil {
			return nil, err
		}
		out = append(out, SourceRecord{EntryID: entry.ID, Version: entry.Version, SubjectID: entry.Source.SubjectID, Title: entry.Title, TotalEpisodes: entry.TotalEpisodes, RecordedProgress: recorded, Value: SyncValueOf(entry), Metadata: SourceMetadata{SubjectID: entry.Source.SubjectID, Title: entry.Title, OriginalTitle: entry.OriginalTitle, Format: entry.Format, TotalEpisodes: entry.TotalEpisodes, Details: entry.Details}})
	}
	return out, rows.Err()
}
func (s *Service) SyncBaselines(ctx context.Context, owner string, remoteID int64) (map[int64]SyncBaseline, error) {
	rows, err := s.pool.Query(ctx, `SELECT s.subject_id,b.local_value,b.remote_value,b.episode_hash FROM source_sync_baselines b JOIN entry_sources s ON s.entry_id=b.entry_id WHERE s.user_id=$1 AND b.remote_user_id=$2`, owner, remoteID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]SyncBaseline{}
	for rows.Next() {
		var subject int64
		var baseline SyncBaseline
		if err = rows.Scan(&subject, &baseline.Local, &baseline.Remote, &baseline.EpisodeHash); err != nil {
			return nil, err
		}
		out[subject] = baseline
	}
	return out, rows.Err()
}

func LocalSyncTarget(local *SourceRecord, metadata SourceMetadata, value SyncValue, progress bool) (SyncValue, error) {
	value = value.Normalized()
	total := metadata.TotalEpisodes
	if local != nil {
		total = local.TotalEpisodes
		if !progress {
			value.Progress = local.Value.Progress
		}
		if value.Progress < local.RecordedProgress {
			return value, fault.New("version_conflict", "上游进度低于已有观看记录，不能覆盖。请保留本地进度或先调整观看记录。")
		}
	}
	if value.Status == "completed" && total > 0 && (local == nil || local.Value.Status != "completed") {
		value.Progress = max(value.Progress, total)
	}
	test := Entry{Title: "同步验证", Format: "tv", Status: value.Status, Notes: value.Notes, Tags: value.Tags, WatchedEpisodes: value.Progress, TotalEpisodes: total, Accent: "violet", Visibility: "private"}
	if value.Score != 0 {
		test.Score = &value.Score
	}
	err := test.Validate()
	value.Tags = test.Tags
	return value.Normalized(), err
}

// ApplyRemote is a journal transaction with a durable receipt, allowing the
// worker to recover after committing local changes but before acknowledging an item.
func (s *Service) ApplyRemote(ctx context.Context, owner, changeID string, remoteID int64, metadata SourceMetadata, local *SourceRecord, remote SyncValue, includeProgress bool, episodeHash string) (Entry, error) {
	if !id.Valid(changeID) || remoteID < 1 || metadata.SubjectID < 1 {
		return Entry{}, fault.New("validation_error", "同步身份无效。")
	}
	request, _ := json.Marshal(struct {
		RemoteID int64
		Metadata SourceMetadata
		Local    *SourceRecord
		Remote   SyncValue
		Progress bool
		Hash     string
	}{remoteID, metadata, local, remote, includeProgress, episodeHash})
	digest := sha256.Sum256(request)
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Entry{}, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226043))`, owner); err != nil {
		return Entry{}, err
	}
	var existingHash []byte
	var result Entry
	err = tx.QueryRow(ctx, `SELECT request_hash,result FROM source_sync_receipts WHERE change_id=$1 AND user_id=$2`, changeID, owner).Scan(&existingHash, &result)
	if err == nil {
		if !bytes.Equal(existingHash, digest[:]) {
			return Entry{}, fault.New("idempotency_conflict", "同步请求内容已改变。")
		}
		return result, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return Entry{}, err
	}
	var entry Entry
	if local == nil {
		var exists bool
		var count int
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM entry_sources WHERE user_id=$1 AND subject_id=$2),(SELECT count(*) FROM entries WHERE user_id=$1)`, owner, metadata.SubjectID).Scan(&exists, &count); err != nil {
			return Entry{}, err
		}
		if exists {
			return Entry{}, fault.New("version_conflict", "预览后同一作品已经加入手账，请重新生成预览。")
		}
		if count >= 5000 {
			return Entry{}, fault.New("validation_error", "手账最多保存 5000 部番剧。")
		}
	} else {
		entry, err = scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, local.EntryID, owner))
		if err != nil {
			return Entry{}, err
		}
		if entry.Version != local.Version || entry.Source == nil || entry.Source.SubjectID != metadata.SubjectID {
			return Entry{}, fault.New("version_conflict", "预览后本地记录已改变，请重新生成预览。")
		}
	}
	target, err := LocalSyncTarget(local, metadata, remote, includeProgress)
	if err != nil {
		return Entry{}, err
	}
	var score *float64
	if target.Score != 0 {
		score = &target.Score
	}
	if local == nil {
		entry, err = insertEntry(ctx, tx, owner, Create{Title: metadata.Title, OriginalTitle: metadata.OriginalTitle, Format: metadata.Format, TotalEpisodes: metadata.TotalEpisodes, Details: metadata.Details, Status: target.Status, Score: score, Notes: target.Notes, Tags: target.Tags})
		if err != nil {
			return Entry{}, err
		}
		if _, err = tx.Exec(ctx, `INSERT INTO entry_sources(entry_id,user_id,provider,subject_id,metadata) VALUES($1,$2,'bangumi',$3,$4)`, entry.ID, owner, metadata.SubjectID, metadata); err != nil {
			return Entry{}, err
		}
	}
	increment := 1
	if local == nil {
		increment = 0
	}
	entry, err = scanEntry(tx.QueryRow(ctx, `UPDATE entries SET status=$2,score=$3,notes=$4,tags=$5,watched_episodes=$6,version=version+$7,updated_at=now() WHERE id=$1 RETURNING `+columns, entry.ID, target.Status, score, target.Notes, target.Tags, target.Progress, increment))
	if err != nil {
		return Entry{}, err
	}
	if err = saveSyncBaseline(ctx, tx, entry.ID, remoteID, SyncValueOf(entry), remote, episodeHash); err != nil {
		return Entry{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO source_sync_receipts(change_id,user_id,request_hash,result) VALUES($1,$2,$3,$4)`, changeID, owner, digest[:], entry); err != nil {
		return Entry{}, err
	}
	return entry, tx.Commit(ctx)
}
func saveSyncBaseline(ctx context.Context, tx pgx.Tx, entryID string, remoteID int64, local, remote SyncValue, episodeHash string) error {
	_, err := tx.Exec(ctx, `INSERT INTO source_sync_baselines(entry_id,remote_user_id,local_value,remote_value,episode_hash) VALUES($1,$2,$3,$4,$5) ON CONFLICT(entry_id,remote_user_id) DO UPDATE SET local_value=excluded.local_value,remote_value=excluded.remote_value,episode_hash=excluded.episode_hash,updated_at=now()`, entryID, remoteID, local.Normalized(), remote.Normalized(), episodeHash)
	return err
}
func (s *Service) MarkSynced(ctx context.Context, owner string, remoteID int64, local SourceRecord, remote SyncValue, episodeHash string) error {
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	entry, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, local.EntryID, owner))
	if err != nil {
		return err
	}
	if entry.Version != local.Version || entry.Source == nil || entry.Source.SubjectID != local.SubjectID {
		return fault.New("version_conflict", "远端已完成核对，但本地记录已改变，请重新预览。")
	}
	if err = saveSyncBaseline(ctx, tx, entry.ID, remoteID, SyncValueOf(entry), remote, episodeHash); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) CompletedSync(ctx context.Context, owner, changeID string) (bool, error) {
	var completed bool
	err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM source_sync_receipts WHERE user_id=$1 AND change_id=$2)`, owner, changeID).Scan(&completed)
	return completed, err
}
