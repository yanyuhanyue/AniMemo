package journal

import (
	"animemo.local/server/internal/telemetry"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/mediastore"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Service struct {
	pool    *pgxpool.Pool
	storage *mediastore.Store
}

func New(pool *pgxpool.Pool) *Service {
	return &Service{pool: pool, storage: mediastore.New(pool, nil)}
}
func (s *Service) ConfigureStorage(storage *mediastore.Store) { s.storage = storage }

const columns = `id,anime_id,airing_state,title,original_title,format,status,total_episodes,watched_episodes,score,notes,tags,accent,version,created_at,updated_at,(SELECT revision FROM entry_covers WHERE entry_id=entries.id),details,visibility,share_slug,(SELECT jsonb_build_object('provider',provider,'subject_id',subject_id,'refreshed_at',refreshed_at) FROM entry_sources WHERE entry_id=entries.id)`
const recordColumns = `w.id,w.entry_id,e.title,e.accent,coalesce(to_char(w.watched_on,'YYYY-MM-DD'),''),w.episode_from,w.episode_to,w.note,w.request_id,w.created_at,w.rewatch,w.version,w.time_precision,w.source_line,w.source_filename`

func scanEntry(row pgx.Row, extra ...any) (Entry, error) {
	var e Entry
	args := []any{&e.ID, &e.AnimeID, &e.AiringState, &e.Title, &e.OriginalTitle, &e.Format, &e.Status, &e.TotalEpisodes, &e.WatchedEpisodes, &e.Score, &e.Notes, &e.Tags, &e.Accent, &e.Version, &e.CreatedAt, &e.UpdatedAt, &e.CoverRevision, &e.Details, &e.Visibility, &e.ShareSlug, &e.Source}
	err := row.Scan(append(args, extra...)...)
	if errors.Is(err, pgx.ErrNoRows) {
		return e, fault.New("not_found", "没有找到这部番剧。")
	}
	return e, err
}

func scanRecord(row pgx.Row) (Record, error) {
	var r Record
	err := row.Scan(&r.ID, &r.EntryID, &r.EntryTitle, &r.Accent, &r.WatchedOn, &r.EpisodeFrom, &r.EpisodeTo, &r.Note, &r.RequestID, &r.CreatedAt, &r.Rewatch, &r.Version, &r.TimePrecision, &r.SourceLine, &r.SourceFilename)
	return r, err
}

func (s *Service) Create(ctx context.Context, owner string, input Create) (Entry, error) {
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Entry{}, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226043))`, owner); err != nil {
		return Entry{}, err
	}
	e, err := insertEntry(ctx, tx, owner, input)
	if err != nil {
		return Entry{}, err
	}
	return e, tx.Commit(ctx)
}

func insertEntry(ctx context.Context, tx pgx.Tx, owner string, input Create) (Entry, error) {
	e := Entry{AnimeID: input.AnimeID, AiringState: input.AiringState, Visibility: input.Visibility, Details: input.Details, ID: id.New(), Title: input.Title, OriginalTitle: input.OriginalTitle, Format: input.Format, Status: input.Status, TotalEpisodes: input.TotalEpisodes, Score: input.Score, Notes: input.Notes, Tags: input.Tags, Accent: input.Accent}
	if e.Format == "" {
		e.Format = "tv"
	}
	if e.Status == "" {
		e.Status = "recorded"
	}
	if e.AiringState == "" {
		e.AiringState = "unknown"
	}
	if e.Accent == "" {
		e.Accent = "violet"
	}
	if e.Status == "completed" {
		e.WatchedEpisodes = e.TotalEpisodes
	}
	if err := e.Validate(); err != nil {
		return Entry{}, err
	}
	return scanEntry(tx.QueryRow(ctx, `INSERT INTO entries (id,user_id,title,original_title,format,status,total_episodes,watched_episodes,score,notes,tags,accent,details,visibility,airing_state,anime_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,nullif($16,'')::uuid) RETURNING `+columns, e.ID, owner, e.Title, e.OriginalTitle, e.Format, e.Status, e.TotalEpisodes, e.WatchedEpisodes, e.Score, e.Notes, e.Tags, e.Accent, e.Details, e.Visibility, e.AiringState, e.AnimeID))
}

func (s *Service) Get(ctx context.Context, owner, entryID string) (Entry, error) {
	if !id.Valid(entryID) {
		return Entry{}, fault.New("not_found", "没有找到这部番剧。")
	}
	return scanEntry(s.pool.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL`, entryID, owner))
}

func (s *Service) List(ctx context.Context, owner string, filter Filter) (Page, error) {
	if err := filter.Validate(); err != nil {
		return Page{}, err
	}
	tx, err := telemetry.Begin(ctx, s.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return Page{}, err
	}
	defer tx.Rollback(context.Background())
	page := Page{Items: []Entry{}, Page: filter.Page, PageSize: filter.PageSize}
	search := "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(filter.Search) + "%"
	where := ` FROM entries WHERE user_id=$1 AND deleted_at IS NULL AND ($2='' OR status=$2) AND (title ILIKE $3 OR original_title ILIKE $3 OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ILIKE $3))`
	if err = tx.QueryRow(ctx, `SELECT count(*)`+where, owner, filter.Status, search).Scan(&page.Total); err != nil {
		return Page{}, err
	}
	order := map[string]string{"updated": "updated_at DESC,id", "title": "title,id", "score": "score DESC NULLS LAST,updated_at DESC,id"}[filter.Sort]
	rows, err := tx.Query(ctx, `SELECT `+columns+where+` ORDER BY `+order+` LIMIT $4 OFFSET $5`, owner, filter.Status, search, filter.PageSize, (filter.Page-1)*filter.PageSize)
	if err != nil {
		return Page{}, err
	}
	defer rows.Close()
	for rows.Next() {
		e, err := scanEntry(rows)
		if err != nil {
			return Page{}, err
		}
		page.Items = append(page.Items, e)
	}
	if err = rows.Err(); err != nil {
		return Page{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Page{}, err
	}
	return page, nil
}

func (s *Service) Update(ctx context.Context, owner, entryID string, patch Patch) (Entry, error) {
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Entry{}, err
	}
	defer tx.Rollback(context.Background())
	e, err := updateEntry(ctx, tx, owner, entryID, patch)
	if err != nil {
		return Entry{}, err
	}
	return e, tx.Commit(ctx)
}

func updateEntry(ctx context.Context, tx pgx.Tx, owner, entryID string, patch Patch) (Entry, error) {
	if !id.Valid(entryID) {
		return Entry{}, fault.New("not_found", "没有找到这部番剧。")
	}
	if patch.Version < 1 {
		return Entry{}, fault.Field("version", "缺少记录版本，请刷新后重试。")
	}
	e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, entryID, owner))
	if err != nil {
		return Entry{}, err
	}
	if patch.Version != e.Version {
		return Entry{}, fault.New("version_conflict", "这条记录已在其他地方更新，请关闭编辑后重新打开。")
	}
	if patch.AiringState != nil {
		e.AiringState = *patch.AiringState
	}
	if patch.Visibility != nil {
		e.Visibility = *patch.Visibility
	}
	if patch.Details != nil {
		e.Details = *patch.Details
	}
	if patch.Title != nil {
		e.Title = *patch.Title
	}
	if patch.OriginalTitle != nil {
		e.OriginalTitle = *patch.OriginalTitle
	}
	if patch.Format != nil {
		e.Format = *patch.Format
	}
	if patch.Status != nil {
		e.Status = *patch.Status
	}
	if patch.TotalEpisodes != nil {
		e.TotalEpisodes = *patch.TotalEpisodes
	}
	if patch.Score != nil {
		e.Score = patch.Score
		if *patch.Score == 0 {
			e.Score = nil
		}
	}
	if patch.Notes != nil {
		e.Notes = *patch.Notes
	}
	if patch.Tags != nil {
		e.Tags = *patch.Tags
	}
	if patch.Accent != nil {
		e.Accent = *patch.Accent
	}
	if patch.Status != nil && *patch.Status == "completed" && e.TotalEpisodes > 0 {
		e.WatchedEpisodes = max(e.WatchedEpisodes, e.TotalEpisodes)
	}
	if err = e.Validate(); err != nil {
		return Entry{}, err
	}
	e, err = scanEntry(tx.QueryRow(ctx, `UPDATE entries SET title=$3,original_title=$4,format=$5,status=$6,total_episodes=$7,watched_episodes=$8,score=$9,notes=$10,tags=$11,accent=$12,details=$13,visibility=$14,airing_state=$15,version=version+1,updated_at=now() WHERE id=$1 AND user_id=$2 RETURNING `+columns, entryID, owner, e.Title, e.OriginalTitle, e.Format, e.Status, e.TotalEpisodes, e.WatchedEpisodes, e.Score, e.Notes, e.Tags, e.Accent, e.Details, e.Visibility, e.AiringState))
	if err != nil {
		return Entry{}, err
	}
	return e, nil
}

func (s *Service) Delete(ctx context.Context, owner, entryID string, version int) error {
	if !id.Valid(entryID) {
		return fault.New("not_found", "没有找到这部番剧。")
	}
	if version < 1 {
		return fault.Field("version", "缺少记录版本，请刷新后重试。")
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, entryID, owner))
	if err != nil {
		return err
	}
	if version != e.Version {
		return fault.New("version_conflict", "这条记录已更新，请重新打开后再删除。")
	}
	if _, err = tx.Exec(ctx, `DELETE FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL`, entryID, owner); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

type RecordResult struct {
	Record Record `json:"record"`
	Entry  Entry  `json:"entry"`
}

func (s *Service) RecordWatch(ctx context.Context, owner, entryID string, input RecordInput) (RecordResult, error) {
	if !id.Valid(entryID) {
		return RecordResult{}, fault.New("not_found", "没有找到这部番剧。")
	}
	if err := input.Validate(); err != nil {
		return RecordResult{}, err
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return RecordResult{}, err
	}
	defer tx.Rollback(context.Background())
	e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, entryID, owner))
	if err != nil {
		return RecordResult{}, err
	}
	existing, err := scanRecord(tx.QueryRow(ctx, `SELECT `+recordColumns+` FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE w.entry_id=$1 AND w.request_id=$2`, entryID, input.RequestID))
	if err == nil {
		if existing.WatchedOn != input.WatchedOn || existing.EpisodeFrom != input.EpisodeFrom || existing.EpisodeTo != input.EpisodeTo || existing.Note != input.Note || existing.Rewatch != input.Rewatch || existing.TimePrecision != input.TimePrecision {
			return RecordResult{}, fault.New("idempotency_conflict", "这次请求已保存过不同内容，请重新打开记录窗口。")
		}
		return RecordResult{Record: existing, Entry: e}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return RecordResult{}, err
	}
	if e.TotalEpisodes > 0 && input.EpisodeTo > e.TotalEpisodes {
		return RecordResult{}, fault.Field("episode_to", "观看话数不能超过总话数。")
	}
	recordID := id.New()
	_, err = tx.Exec(ctx, `INSERT INTO watch_records (id,entry_id,watched_on,episode_from,episode_to,note,request_id,rewatch,time_precision) VALUES ($1,$2,nullif($3,'')::date,$4,$5,$6,$7,$8,$9)`, recordID, entryID, input.WatchedOn, input.EpisodeFrom, input.EpisodeTo, input.Note, input.RequestID, input.Rewatch, input.TimePrecision)
	if err != nil {
		return RecordResult{}, err
	}
	e.WatchedEpisodes = max(e.WatchedEpisodes, input.EpisodeTo)
	if e.Status == "planned" || e.Status == "recorded" {
		e.Status = "watching"
	}
	if e.TotalEpisodes > 0 && e.WatchedEpisodes == e.TotalEpisodes {
		if e.AiringState == "finished" {
			e.Status = "completed"
		} else {
			e.Status = "caught_up"
		}
	}
	e, err = scanEntry(tx.QueryRow(ctx, `UPDATE entries SET watched_episodes=$3,status=$4,version=version+1,updated_at=now() WHERE id=$1 AND user_id=$2 RETURNING `+columns, entryID, owner, e.WatchedEpisodes, e.Status))
	if err != nil {
		return RecordResult{}, err
	}
	record, err := scanRecord(tx.QueryRow(ctx, `SELECT `+recordColumns+` FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE w.id=$1`, recordID))
	if err != nil {
		return RecordResult{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return RecordResult{}, err
	}
	return RecordResult{Record: record, Entry: e}, nil
}

func (s *Service) History(ctx context.Context, owner, entryID string) ([]Record, error) {
	if entryID != "" {
		if _, err := s.Get(ctx, owner, entryID); err != nil {
			return nil, err
		}
	}
	rows, err := s.pool.Query(ctx, `SELECT `+recordColumns+` FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1 AND e.deleted_at IS NULL AND ($2='' OR e.id::text=$2) ORDER BY w.watched_on DESC,w.created_at DESC,w.id LIMIT 100`, owner, entryID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	records := []Record{}
	for rows.Next() {
		r, err := scanRecord(rows)
		if err != nil {
			return nil, err
		}
		records = append(records, r)
	}
	return records, rows.Err()
}

func (s *Service) Stats(ctx context.Context, owner string) (Stats, error) {
	var stats Stats
	err := s.pool.QueryRow(ctx, `SELECT count(*),count(*) FILTER(WHERE status='recorded'),count(*) FILTER(WHERE status='watching'),count(*) FILTER(WHERE status='caught_up'),count(*) FILTER(WHERE status='completed'),count(*) FILTER(WHERE status='planned'),count(*) FILTER(WHERE status='on_hold'),count(*) FILTER(WHERE status='dropped'),COALESCE(sum(watched_episodes),0),(SELECT count(*) FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1 AND e.deleted_at IS NULL) FROM entries WHERE user_id=$1 AND deleted_at IS NULL`, owner).Scan(&stats.Total, &stats.Recorded, &stats.Watching, &stats.CaughtUp, &stats.Completed, &stats.Planned, &stats.OnHold, &stats.Dropped, &stats.WatchedEpisodes, &stats.WatchRecords)
	return stats, err
}

type Export struct {
	Library    *LibraryBundle   `json:"library,omitempty"`
	Resources  []AnimeResource  `json:"resources,omitempty"`
	Revisions  []MemoryRevision `json:"revisions,omitempty"`
	Schema     string           `json:"schema"`
	ExportedAt time.Time        `json:"exported_at"`
	Entries    []Entry          `json:"entries"`
	History    []Record         `json:"history"`
}

func (s *Service) Export(ctx context.Context, owner string) (Export, error) {
	tx, err := telemetry.Begin(ctx, s.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return Export{}, err
	}
	defer tx.Rollback(context.Background())
	out, err := exportJournal(ctx, tx, owner)
	if err != nil {
		return Export{}, err
	}
	data, err := json.Marshal(out)
	if err != nil {
		return Export{}, err
	}
	if len(data) > MaxJournalBytes {
		return Export{}, fault.New("export_too_large", "个人导出超过 144 MiB，请使用实例快照完整备份。")
	}
	return out, tx.Commit(ctx)
}

func exportJournal(ctx context.Context, tx pgx.Tx, owner string) (Export, error) {
	var entryCount, recordCount int
	err := tx.QueryRow(ctx, `SELECT (SELECT count(*) FROM entries WHERE user_id=$1 AND deleted_at IS NULL),(SELECT count(*) FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1 AND e.deleted_at IS NULL)`, owner).Scan(&entryCount, &recordCount)
	if err != nil {
		return Export{}, err
	}
	// ponytail: bounded in-memory export; move to streaming jobs above these limits.
	if entryCount > 5000 || recordCount > 20000 {
		return Export{}, fault.New("export_too_large", "当前直接导出支持最多 5000 部番剧和 20000 条观看记录。")
	}
	out := Export{Schema: "animemo.journal/v2", ExportedAt: time.Now().UTC(), Entries: []Entry{}, History: []Record{}}
	rows, err := tx.Query(ctx, `SELECT `+columns+` FROM entries WHERE user_id=$1 AND deleted_at IS NULL ORDER BY created_at,id`, owner)
	if err != nil {
		return Export{}, err
	}
	for rows.Next() {
		e, err := scanEntry(rows)
		if err != nil {
			rows.Close()
			return Export{}, err
		}
		out.Entries = append(out.Entries, e)
	}
	if err = rows.Err(); err != nil {
		return Export{}, err
	}
	rows.Close()
	rows, err = tx.Query(ctx, `SELECT `+recordColumns+` FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1 AND e.deleted_at IS NULL ORDER BY w.watched_on,w.created_at,w.id`, owner)
	if err != nil {
		return Export{}, err
	}
	defer rows.Close()
	for rows.Next() {
		r, err := scanRecord(rows)
		if err != nil {
			return Export{}, err
		}
		out.History = append(out.History, r)
	}
	if err = rows.Err(); err != nil {
		return Export{}, err
	}
	rows.Close()
	if err = exportMemory(ctx, tx, owner, &out); err != nil {
		return Export{}, err
	}
	out.Library, err = exportLibrary(ctx, tx, owner)
	if err != nil {
		return Export{}, err
	}
	if out.Library != nil {
		out.Schema = "animemo.journal/v3"
	}
	return out, nil
}
