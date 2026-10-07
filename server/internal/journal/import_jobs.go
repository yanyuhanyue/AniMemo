package journal

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
)

type ImportJob struct {
	ID        string        `json:"id"`
	Format    string        `json:"format"`
	State     string        `json:"state"`
	Preview   ImportPreview `json:"preview"`
	Error     string        `json:"error"`
	Created   int           `json:"created"`
	CreatedAt time.Time     `json:"created_at"`
	ExpiresAt time.Time     `json:"expires_at"`
}

const importColumns = `id,format,state,preview,error,created,created_at,expires_at`

func scanImport(row pgx.Row) (ImportJob, error) {
	var job ImportJob
	err := row.Scan(&job.ID, &job.Format, &job.State, &job.Preview, &job.Error, &job.Created, &job.CreatedAt, &job.ExpiresAt)
	if errors.Is(err, pgx.ErrNoRows) {
		err = fault.New("not_found", "没有找到导入任务。")
	}
	if job.Preview.Warnings == nil {
		job.Preview.Warnings = []string{}
	}
	if job.Preview.Titles == nil {
		job.Preview.Titles = []string{}
	}
	if job.Preview.History == nil {
		job.Preview.History = []ImportRecordPreview{}
	}
	return job, err
}

func (s *Service) NewImport(ctx context.Context, owner, format string, data []byte) (ImportJob, error) {
	if format != "json" && format != "csv" && format != "zip" {
		return ImportJob{}, fault.New("unsupported_media_type", "请选择 JSON、CSV 或本项目的 ZIP 备份。")
	}
	limit := MaxImportBytes
	if format == "csv" {
		limit = MaxCSVBytes
	} else if format == "json" {
		limit = MaxJournalBytes
	}
	if len(data) == 0 || len(data) > limit {
		return ImportJob{}, fault.New("import_too_large", "导入文件为空或超过所选格式的大小限制。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return ImportJob{}, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226046))`, owner); err != nil {
		return ImportJob{}, err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM import_jobs WHERE user_id=$1 AND expires_at<now()`, owner); err != nil {
		return ImportJob{}, err
	}
	var active int
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM import_jobs WHERE user_id=$1 AND state IN ('validating','ready','applying')`, owner).Scan(&active); err != nil {
		return ImportJob{}, err
	}
	if active > 0 {
		return ImportJob{}, fault.New("version_conflict", "已有未结束的导入任务，请先完成或取消。")
	}
	job, err := scanImport(tx.QueryRow(ctx, `INSERT INTO import_jobs(id,user_id,format,source) VALUES($1,$2,$3,$4) RETURNING `+importColumns, id.New(), owner, format, data))
	if err != nil {
		return job, err
	}
	return job, tx.Commit(ctx)
}

func (s *Service) Import(ctx context.Context, owner, jobID string) (ImportJob, error) {
	if !id.Valid(jobID) {
		return ImportJob{}, fault.New("not_found", "没有找到导入任务。")
	}
	return scanImport(s.pool.QueryRow(ctx, `SELECT `+importColumns+` FROM import_jobs WHERE id=$1 AND user_id=$2`, jobID, owner))
}
func (s *Service) Imports(ctx context.Context, owner string) ([]ImportJob, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+importColumns+` FROM import_jobs WHERE user_id=$1 ORDER BY created_at DESC,id LIMIT 20`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ImportJob{}
	for rows.Next() {
		job, err := scanImport(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, job)
	}
	return out, rows.Err()
}

func (s *Service) ImportAction(ctx context.Context, owner, jobID, action string) (ImportJob, error) {
	if !id.Valid(jobID) {
		return ImportJob{}, fault.New("not_found", "没有找到导入任务。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return ImportJob{}, err
	}
	defer tx.Rollback(context.Background())
	job, err := scanImport(tx.QueryRow(ctx, `SELECT `+importColumns+` FROM import_jobs WHERE id=$1 AND user_id=$2 FOR UPDATE`, jobID, owner))
	if err != nil {
		return job, err
	}
	if action == "apply" && (job.State == "done" || job.State == "applying") {
		return job, nil
	}
	if action == "cancel" && job.State == "cancelled" {
		return job, nil
	}
	if !job.ExpiresAt.After(time.Now()) {
		return job, fault.New("version_conflict", "导入预览已过期，请重新上传。")
	}
	switch action {
	case "apply":
		if job.State != "ready" {
			return job, fault.New("version_conflict", "请等待校验完成后确认导入。")
		}
		job, err = scanImport(tx.QueryRow(ctx, `UPDATE import_jobs SET state='applying',updated_at=now() WHERE id=$1 RETURNING `+importColumns, jobID))
	case "cancel":
		if job.State == "done" {
			return job, fault.New("version_conflict", "任务已完成，不能取消已提交的导入。")
		}
		job, err = scanImport(tx.QueryRow(ctx, `UPDATE import_jobs SET state='cancelled',source=NULL,document=NULL,updated_at=now() WHERE id=$1 RETURNING `+importColumns, jobID))
	default:
		return job, fault.Field("action", "任务操作无效。")
	}
	if err != nil {
		return job, err
	}
	return job, tx.Commit(ctx)
}

func (s *Service) RunImportWorker(ctx context.Context) {
	timer := time.NewTicker(400 * time.Millisecond)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			_, err := s.ProcessNextImport(ctx)
			if err != nil && !errors.Is(err, context.Canceled) {
				slog.Error("import worker failed", "error_type", "database_or_task_failure")
			}
		}
	}
}

// A session advisory lock makes a task single-consumer across processes and is
// automatically released on process death. Pending work is durable in PostgreSQL.
func (s *Service) ProcessNextImport(ctx context.Context) (bool, error) {
	if _, err := s.pool.Exec(ctx, `UPDATE import_jobs SET state='cancelled',source=NULL,document=NULL,error='任务已过期，请重新上传。' WHERE expires_at<now() AND state IN ('validating','ready','applying')`); err != nil {
		return false, err
	}
	rows, err := s.pool.Query(ctx, `SELECT id FROM import_jobs WHERE state IN ('validating','applying') ORDER BY created_at,id LIMIT 8`)
	if err != nil {
		return false, err
	}
	ids := []string{}
	for rows.Next() {
		var value string
		if err = rows.Scan(&value); err != nil {
			rows.Close()
			return false, err
		}
		ids = append(ids, value)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return false, err
	}
	for _, jobID := range ids {
		conn, err := s.pool.Acquire(ctx)
		if err != nil {
			return false, err
		}
		var locked bool
		err = conn.QueryRow(ctx, `SELECT pg_try_advisory_lock(hashtextextended($1,718226045))`, jobID).Scan(&locked)
		if err != nil || !locked {
			conn.Release()
			if err != nil {
				return false, err
			}
			continue
		}
		func() {
			defer func() {
				cleanup, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				if _, unlockErr := conn.Exec(cleanup, `SELECT pg_advisory_unlock(hashtextextended($1,718226045))`, jobID); unlockErr != nil {
					_ = conn.Conn().Close(cleanup)
				}
				conn.Release()
			}()
			var owner, format, state string
			var source []byte
			err = conn.QueryRow(ctx, `SELECT user_id,format,state,source FROM import_jobs WHERE id=$1`, jobID).Scan(&owner, &format, &state, &source)
			if errors.Is(err, pgx.ErrNoRows) {
				err = nil
				return
			}
			if err != nil {
				return
			}
			taskCtx, cancel := context.WithTimeout(ctx, 2*time.Minute)
			defer cancel()
			if state == "validating" {
				err = s.prepareImport(taskCtx, owner, jobID, format, source)
			} else if state == "applying" {
				err = s.applyImport(taskCtx, owner, jobID, format, source)
			}
			if err != nil && !errors.Is(err, context.Canceled) {
				message := "导入失败，原手账未改变。请检查文件后重试。"
				var problem *fault.Error
				if errors.As(err, &problem) {
					message = problem.Message
				}
				_, saveErr := s.pool.Exec(ctx, `UPDATE import_jobs SET state='failed',error=$2,source=NULL,document=NULL,updated_at=now() WHERE id=$1 AND state IN ('validating','applying')`, jobID, message)
				if saveErr != nil {
					err = saveErr
				} else {
					err = nil
				}
			}
		}()
		return true, err
	}
	return false, nil
}

func journalFingerprint(ctx context.Context, tx pgx.Tx, owner string) (string, error) {
	var value string
	err := tx.QueryRow(ctx, `SELECT md5(coalesce(string_agg(id::text||':'||version::text,',' ORDER BY id),'')) FROM entries WHERE user_id=$1`, owner).Scan(&value)
	return value, err
}

func (s *Service) prepareImport(ctx context.Context, owner, jobID, format string, data []byte) error {
	document, _, err := parseImport(ctx, format, data)
	if err != nil {
		return err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead})
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	fingerprint, err := journalFingerprint(ctx, tx, owner)
	if err != nil {
		return err
	}
	rows, err := tx.Query(ctx, `SELECT title,coalesce((SELECT subject_id FROM entry_sources WHERE entry_id=entries.id),0) FROM entries WHERE user_id=$1`, owner)
	if err != nil {
		return err
	}
	existing := map[string]bool{}
	existingSources := map[int64]bool{}
	for rows.Next() {
		var e Entry
		var subjectID int64
		if err = rows.Scan(&e.Title, &subjectID); err != nil {
			rows.Close()
			return err
		}
		existing[importIdentity(e)] = true
		if subjectID > 0 {
			existingSources[subjectID] = true
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	preview := ImportPreview{Total: len(document.Items), Warnings: []string{}, Titles: []string{}}
	ready := ImportDocument{Items: []ImportItem{}}
	for _, item := range document.Items {
		identity := importIdentity(item.Entry)
		if existing[identity] || (item.Entry.Source != nil && existingSources[item.Entry.Source.SubjectID]) {
			preview.Duplicates++
			continue
		}
		existing[identity] = true
		if item.Entry.Source != nil {
			existingSources[item.Entry.Source.SubjectID] = true
		}
		ready.Items = append(ready.Items, item)
		preview.Ready++
		preview.Records += len(item.History)
		for _, record := range item.History {
			if len(preview.History) >= 100 {
				break
			}
			preview.History = append(preview.History, ImportRecordPreview{Title: item.Entry.Title, WatchedOn: record.WatchedOn, EpisodeFrom: record.EpisodeFrom, EpisodeTo: record.EpisodeTo, Rewatch: record.Rewatch, Note: record.Note})
		}
		if item.CoverPath != "" {
			preview.Covers++
		} else if item.Entry.CoverRevision != nil && len(preview.Warnings) == 0 {
			preview.Warnings = append(preview.Warnings, "JSON 不包含图片；需要原图时请使用 ZIP 备份。")
		}
		if len(preview.Titles) < 100 {
			preview.Titles = append(preview.Titles, item.Entry.Title)
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE import_jobs SET state='ready',document=$2,preview=$3,fingerprint=$4,updated_at=now() WHERE id=$1 AND state='validating'`, jobID, ready, preview, fingerprint); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) applyImport(ctx context.Context, owner, jobID, format string, source []byte) error {
	files := map[string][]byte{}
	var err error
	if format == "zip" {
		files, err = readBundle(ctx, source)
		if err != nil {
			return err
		}
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var state, fingerprint string
	var doc ImportDocument
	if err = tx.QueryRow(ctx, `SELECT state,document,fingerprint FROM import_jobs WHERE id=$1 FOR UPDATE`, jobID).Scan(&state, &doc, &fingerprint); err != nil {
		return err
	}
	if state != "applying" {
		return nil
	}
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226043))`, owner); err != nil {
		return err
	}
	// Lock entries in the same UUID order as bulk mutations; new entries take
	// the owner lock above. Any changed preview is rejected before writing.
	rows, err := tx.Query(ctx, `SELECT id FROM entries WHERE user_id=$1 ORDER BY id FOR UPDATE`, owner)
	if err != nil {
		return err
	}
	for rows.Next() {
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	current, err := journalFingerprint(ctx, tx, owner)
	if err != nil {
		return err
	}
	if current != fingerprint {
		return fault.New("version_conflict", "预览后手账已改变，请重新上传生成预览。")
	}
	var entryCount, recordCount, used int64
	if err = tx.QueryRow(ctx, `SELECT (SELECT count(*) FROM entries WHERE user_id=$1),(SELECT count(*) FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1),(SELECT coalesce(sum(c.byte_size),0) FROM entry_covers c JOIN entries e ON e.id=c.entry_id WHERE e.user_id=$1)`, owner).Scan(&entryCount, &recordCount, &used); err != nil {
		return err
	}
	for _, item := range doc.Items {
		recordCount += int64(len(item.History))
		used += int64(len(files[item.CoverPath]))
	}
	if entryCount+int64(len(doc.Items)) > 5000 || recordCount > 20000 {
		return fault.New("validation_error", "导入后将超过 5000 部番剧或 20000 条观看记录。")
	}
	if used > CoverQuotaBytes {
		return fault.New("cover_quota_exceeded", "导入后的封面总容量超过 100 MiB。")
	}
	for _, item := range doc.Items {
		e := item.Entry
		created, err := insertEntry(ctx, tx, owner, Create{Title: e.Title, OriginalTitle: e.OriginalTitle, Format: e.Format, Status: e.Status, TotalEpisodes: e.TotalEpisodes, Score: e.Score, Notes: e.Notes, Tags: e.Tags, Accent: e.Accent, Details: e.Details})
		if err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `UPDATE entries SET watched_episodes=$2 WHERE id=$1`, created.ID, e.WatchedEpisodes); err != nil {
			return err
		}
		if e.Source != nil {
			metadata := SourceMetadata{SubjectID: e.Source.SubjectID, Title: e.Title, OriginalTitle: e.OriginalTitle, Format: e.Format, TotalEpisodes: e.TotalEpisodes, Details: e.Details}
			if _, err = tx.Exec(ctx, `INSERT INTO entry_sources(entry_id,user_id,provider,subject_id,metadata) VALUES($1,$2,'bangumi',$3,$4)`, created.ID, owner, e.Source.SubjectID, metadata); err != nil {
				return err
			}
		}
		for _, r := range item.History {
			if _, err = tx.Exec(ctx, `INSERT INTO watch_records(id,entry_id,watched_on,episode_from,episode_to,note,request_id,rewatch) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, id.New(), created.ID, r.WatchedOn, r.EpisodeFrom, r.EpisodeTo, r.Note, id.New(), r.Rewatch); err != nil {
				return err
			}
		}
		if item.CoverPath != "" {
			data := files[item.CoverPath]
			kind := "image/png"
			if strings.HasSuffix(item.CoverPath, ".jpg") {
				kind = "image/jpeg"
			}
			picture, err := media.Validate(data, kind)
			if err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, `INSERT INTO entry_covers(entry_id,revision,content_type,width,height,byte_size,data) VALUES($1,$2,$3,$4,$5,$6,$7)`, created.ID, id.New(), kind, picture.Width, picture.Height, len(data), data); err != nil {
				return err
			}
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE import_jobs SET state='done',created=$2,source=NULL,document=NULL,updated_at=now() WHERE id=$1`, jobID, len(doc.Items)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
