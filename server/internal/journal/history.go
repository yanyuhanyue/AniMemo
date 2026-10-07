package journal

import (
	"context"
	"errors"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"github.com/jackc/pgx/v5"
)

type HistoryPage struct {
	Items    []Record `json:"items"`
	Total    int      `json:"total"`
	Page     int      `json:"page"`
	PageSize int      `json:"page_size"`
}

func ValidDateRange(from, to string) error {
	for _, value := range []string{from, to} {
		if value != "" {
			date, err := time.Parse("2006-01-02", value)
			if err != nil || date.Year() < 1900 || date.Year() > 2100 {
				return fault.Field("date", "日期必须是 1900–2100 年间的有效日期。")
			}
		}
	}
	if from != "" && to != "" && from > to {
		return fault.Field("date", "起始日期不能晚于结束日期。")
	}
	return nil
}

func (s *Service) HistoryPage(ctx context.Context, owner, entryID, from, to string, page int) (HistoryPage, error) {
	out := HistoryPage{Items: []Record{}, Page: page, PageSize: 50}
	if err := ValidDateRange(from, to); err != nil {
		return out, err
	}
	if page < 1 || page > 100000 {
		return out, fault.Field("page", "页码无效。")
	}
	if entryID != "" {
		if _, err := s.Get(ctx, owner, entryID); err != nil {
			return out, err
		}
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	where := ` FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1 AND e.deleted_at IS NULL AND ($2='' OR e.id::text=$2) AND ($3='' OR w.watched_on>=nullif($3,'')::date) AND ($4='' OR w.watched_on<=nullif($4,'')::date)`
	if err = tx.QueryRow(ctx, `SELECT count(*)`+where, owner, entryID, from, to).Scan(&out.Total); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT `+recordColumns+where+` ORDER BY w.watched_on DESC,w.created_at DESC,w.id LIMIT 50 OFFSET $5`, owner, entryID, from, to, (page-1)*50)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		r, err := scanRecord(rows)
		if err != nil {
			rows.Close()
			return out, err
		}
		out.Items = append(out.Items, r)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}

type RecordPatch struct {
	Version     int    `json:"version"`
	WatchedOn   string `json:"watched_on"`
	EpisodeFrom int    `json:"episode_from"`
	EpisodeTo   int    `json:"episode_to"`
	Note        string `json:"note"`
	Rewatch     int    `json:"rewatch"`
}

func (s *Service) ChangeRecord(ctx context.Context, owner, entryID, recordID string, version int, input *RecordPatch) (Entry, error) {
	if !id.Valid(entryID) || !id.Valid(recordID) {
		return Entry{}, fault.New("not_found", "没有找到观看记录。")
	}
	if version < 1 {
		return Entry{}, fault.Field("version", "请重新打开观看记录。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Entry{}, err
	}
	defer tx.Rollback(context.Background())
	e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, entryID, owner))
	if err != nil {
		return Entry{}, err
	}
	var actual int
	err = tx.QueryRow(ctx, `SELECT version FROM watch_records WHERE id=$1 AND entry_id=$2 FOR UPDATE`, recordID, entryID).Scan(&actual)
	if errors.Is(err, pgx.ErrNoRows) {
		return Entry{}, fault.New("not_found", "没有找到观看记录。")
	}
	if err != nil {
		return Entry{}, err
	}
	if version != actual {
		return Entry{}, fault.New("version_conflict", "观看记录已更新，请关闭后重新打开。")
	}
	if input == nil {
		_, err = tx.Exec(ctx, `DELETE FROM watch_records WHERE id=$1`, recordID)
	} else {
		r := RecordInput{WatchedOn: input.WatchedOn, EpisodeFrom: input.EpisodeFrom, EpisodeTo: input.EpisodeTo, Note: input.Note, Rewatch: input.Rewatch, RequestID: id.New()}
		if err = r.Validate(); err != nil {
			return Entry{}, err
		}
		if e.TotalEpisodes > 0 && r.EpisodeTo > e.TotalEpisodes {
			return Entry{}, fault.Field("episode_to", "观看话数不能超过总话数。")
		}
		_, err = tx.Exec(ctx, `UPDATE watch_records SET watched_on=$2,episode_from=$3,episode_to=$4,note=$5,rewatch=$6,version=version+1 WHERE id=$1`, recordID, r.WatchedOn, r.EpisodeFrom, r.EpisodeTo, r.Note, r.Rewatch)
	}
	if err != nil {
		return Entry{}, err
	}
	if err = tx.QueryRow(ctx, `SELECT coalesce(max(episode_to),0) FROM watch_records WHERE entry_id=$1`, entryID).Scan(&e.WatchedEpisodes); err != nil {
		return Entry{}, err
	}
	if e.Status == "completed" || e.Status == "watching" || e.Status == "planned" {
		e.Status = "watching"
		if e.WatchedEpisodes == 0 {
			e.Status = "planned"
		} else if e.TotalEpisodes > 0 && e.WatchedEpisodes == e.TotalEpisodes {
			e.Status = "completed"
		}
	}
	e, err = scanEntry(tx.QueryRow(ctx, `UPDATE entries SET watched_episodes=$2,status=$3,version=version+1,updated_at=now() WHERE id=$1 RETURNING `+columns, entryID, e.WatchedEpisodes, e.Status))
	if err != nil {
		return Entry{}, err
	}
	return e, tx.Commit(ctx)
}

type MonthlyActivity struct {
	Month    string `json:"month"`
	Records  int    `json:"records"`
	Episodes int    `json:"episodes"`
}
type Analytics struct {
	From         string            `json:"from"`
	To           string            `json:"to"`
	ActiveDays   int               `json:"active_days"`
	Records      int               `json:"records"`
	Episodes     int               `json:"episodes"`
	AverageScore *float64          `json:"average_score"`
	Months       []MonthlyActivity `json:"months"`
}

func (s *Service) Analytics(ctx context.Context, owner, from, to string) (Analytics, error) {
	out := Analytics{From: from, To: to, Months: []MonthlyActivity{}}
	if err := ValidDateRange(from, to); err != nil {
		return out, err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	where := ` FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1 AND e.deleted_at IS NULL AND ($2='' OR w.watched_on>=nullif($2,'')::date) AND ($3='' OR w.watched_on<=nullif($3,'')::date)`
	if err = tx.QueryRow(ctx, `SELECT count(DISTINCT w.watched_on),count(*),coalesce(sum(w.episode_to-w.episode_from+1),0)`+where, owner, from, to).Scan(&out.ActiveDays, &out.Records, &out.Episodes); err != nil {
		return out, err
	}
	if err = tx.QueryRow(ctx, `SELECT avg(score)::float8 FROM entries WHERE user_id=$1 AND deleted_at IS NULL`, owner).Scan(&out.AverageScore); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT to_char(w.watched_on,'YYYY-MM'),count(*),sum(w.episode_to-w.episode_from+1)`+where+` GROUP BY 1 ORDER BY 1`, owner, from, to)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		var m MonthlyActivity
		if err = rows.Scan(&m.Month, &m.Records, &m.Episodes); err != nil {
			rows.Close()
			return out, err
		}
		out.Months = append(out.Months, m)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
