package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"github.com/jackc/pgx/v5"
	"strconv"
	"strings"
	"time"
)

type YearlyInput struct {
	Version      int      `json:"version"`
	Year         int      `json:"year"`
	Timezone     string   `json:"timezone"`
	Title        string   `json:"title"`
	Introduction string   `json:"introduction"`
	NoteIDs      []string `json:"note_ids"`
	Visibility   string   `json:"visibility"`
}
type YearlyStats struct {
	WatchRecords     int `json:"watch_records"`
	Anime            int `json:"anime"`
	Episodes         int `json:"episodes"`
	Notes            int `json:"notes"`
	ImpreciseRecords int `json:"imprecise_records"`
}
type YearlyItem struct {
	SourceVisibility string   `json:"source_visibility"`
	SourceVersion    int      `json:"source_version"`
	NoteID           string   `json:"note_id"`
	Kind             string   `json:"kind"`
	Title            string   `json:"title"`
	Body             string   `json:"body"`
	OccurredOn       string   `json:"occurred_on"`
	TimePrecision    string   `json:"time_precision"`
	Spoiler          bool     `json:"spoiler"`
	MediaIDs         []string `json:"media_ids"`
	Unavailable      string   `json:"unavailable"`
}
type YearlyRevision struct {
	ID           string       `json:"id"`
	Revision     int          `json:"revision"`
	Cutoff       time.Time    `json:"cutoff"`
	Algorithm    string       `json:"algorithm"`
	Title        string       `json:"title"`
	Introduction string       `json:"introduction"`
	Stats        YearlyStats  `json:"stats"`
	Items        []YearlyItem `json:"items"`
	CreatedAt    time.Time    `json:"created_at"`
}
type YearlyMemory struct {
	ID           string           `json:"id"`
	Year         int              `json:"year"`
	Timezone     string           `json:"timezone"`
	Title        string           `json:"title"`
	Introduction string           `json:"introduction"`
	Version      int              `json:"version"`
	Visibility   string           `json:"visibility"`
	CreatedAt    time.Time        `json:"created_at"`
	UpdatedAt    time.Time        `json:"updated_at"`
	Revisions    []YearlyRevision `json:"revisions"`
}

const yearlyColumns = `id,year,timezone,title,introduction,version,visibility,created_at,updated_at`

func scanYearly(row pgx.Row) (YearlyMemory, error) {
	var y YearlyMemory
	err := row.Scan(&y.ID, &y.Year, &y.Timezone, &y.Title, &y.Introduction, &y.Version, &y.Visibility, &y.CreatedAt, &y.UpdatedAt)
	y.Revisions = []YearlyRevision{}
	return y, libraryMissing(err)
}
func (s *Service) Yearlies(ctx context.Context, owner string) ([]YearlyMemory, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+yearlyColumns+` FROM yearly_memories WHERE owner_id=$1 ORDER BY year DESC,updated_at DESC,id LIMIT 300`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []YearlyMemory{}
	for rows.Next() {
		y, err := scanYearly(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, y)
	}
	return out, rows.Err()
}
func (s *Service) Yearly(ctx context.Context, owner, resource string) (YearlyMemory, error) {
	if !id.Valid(resource) {
		return YearlyMemory{}, fault.New("not_found", "年度记忆不存在。")
	}
	y, err := scanYearly(s.pool.QueryRow(ctx, `SELECT `+yearlyColumns+` FROM yearly_memories WHERE owner_id=$1 AND id=$2`, owner, resource))
	if err != nil {
		return y, err
	}
	rows, err := s.pool.Query(ctx, `SELECT id,revision,cutoff,algorithm,title,introduction,stats,items,created_at FROM yearly_revisions WHERE yearly_id=$1 ORDER BY revision DESC LIMIT 100`, resource)
	if err != nil {
		return y, err
	}
	defer rows.Close()
	for rows.Next() {
		var r YearlyRevision
		if err = rows.Scan(&r.ID, &r.Revision, &r.Cutoff, &r.Algorithm, &r.Title, &r.Introduction, &r.Stats, &r.Items, &r.CreatedAt); err != nil {
			return y, err
		}
		y.Revisions = append(y.Revisions, r)
	}
	return y, rows.Err()
}
func (s *Service) SaveYearly(ctx context.Context, owner, resource string, in YearlyInput) (YearlyMemory, error) {
	in.Title = strings.TrimSpace(in.Title)
	if in.Visibility == "" {
		in.Visibility = "private"
	}
	if in.Timezone == "" {
		in.Timezone = "Asia/Shanghai"
	}
	_, tzErr := time.LoadLocation(in.Timezone)
	if in.Title == "" || !libraryText(in.Title, 160) || !libraryText(in.Introduction, 10000) || in.Year < 1900 || in.Year > 2100 || tzErr != nil || !libraryIDs(in.NoteIDs, 200) || !libraryVisibility(in.Visibility) {
		return YearlyMemory{}, fault.Field("title", "请检查标题、年份、时区和选材（最多 200 项）。")
	}
	if resource != "" && !id.Valid(resource) {
		return YearlyMemory{}, fault.New("not_found", "年度记忆不存在。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return YearlyMemory{}, err
	}
	defer tx.Rollback(context.Background())
	// Viewing writes lock their entry. Hold the same rows while freezing all
	// counts, so edits cannot mix two different points in time.
	locked, err := tx.Query(ctx, `SELECT id FROM entries WHERE user_id=$1 ORDER BY id FOR SHARE`, owner)
	if err != nil {
		return YearlyMemory{}, err
	}
	for locked.Next() {
	}
	err = locked.Err()
	locked.Close()
	if err != nil {
		return YearlyMemory{}, err
	}
	if resource != "" {
		old, err := scanYearly(tx.QueryRow(ctx, `SELECT `+yearlyColumns+` FROM yearly_memories WHERE owner_id=$1 AND id=$2 FOR UPDATE`, owner, resource))
		if err != nil {
			return old, err
		}
		if err = libraryVersion(old.Version, in.Version); err != nil {
			return old, err
		}
		if old.Year != in.Year || old.Timezone != in.Timezone {
			return old, fault.Field("year", "已创建年度的年份和时区不可覆盖；请建立另一份年度记忆。")
		}
	} else {
		resource = id.New()
		var count int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM yearly_memories WHERE owner_id=$1`, owner).Scan(&count); err != nil {
			return YearlyMemory{}, err
		}
		if count >= 300 {
			return YearlyMemory{}, fault.Field("title", "年度记忆已达 300 份上限。")
		}
	}
	y, err := scanYearly(tx.QueryRow(ctx, `INSERT INTO yearly_memories(id,owner_id,year,timezone,title,introduction,visibility) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(id) DO UPDATE SET title=excluded.title,introduction=excluded.introduction,visibility=excluded.visibility,version=yearly_memories.version+1,updated_at=now() RETURNING `+yearlyColumns, resource, owner, in.Year, in.Timezone, in.Title, in.Introduction, in.Visibility))
	if err != nil {
		return y, err
	}
	var revision int
	if err = tx.QueryRow(ctx, `SELECT coalesce(max(revision),0)+1 FROM yearly_revisions WHERE yearly_id=$1`, resource).Scan(&revision); err != nil {
		return y, err
	}
	if revision > 100 {
		return y, fault.Field("title", "该年度已达 100 次修订上限，请另建年度册。")
	}
	r := YearlyRevision{ID: id.New(), Revision: revision, Algorithm: "memory-year/v1;calendar-year;range-counts;no-inferred-facts", Title: in.Title, Introduction: in.Introduction, Items: []YearlyItem{}}
	if err = tx.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&r.Cutoff); err != nil {
		return y, err
	}
	if err = tx.QueryRow(ctx, `SELECT count(*),count(DISTINCT e.anime_id),coalesce(sum(w.episode_to-w.episode_from+1),0),count(*) FILTER(WHERE w.time_precision<>'day') FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1 AND extract(year FROM w.watched_on)=$2 AND w.created_at<=$3`, owner, in.Year, r.Cutoff).Scan(&r.Stats.WatchRecords, &r.Stats.Anime, &r.Stats.Episodes, &r.Stats.ImpreciseRecords); err != nil {
		return y, err
	}
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM memory_notes WHERE owner_id=$1 AND deleted_at IS NULL AND left(occurred_on,4)=$2 AND created_at<=$3`, owner, strconv.Itoa(in.Year), r.Cutoff).Scan(&r.Stats.Notes); err != nil {
		return y, err
	}
	for _, note := range in.NoteIDs {
		n, err := scanNote(tx.QueryRow(ctx, `SELECT `+noteColumns+` FROM memory_notes WHERE owner_id=$1 AND id=$2 AND deleted_at IS NULL FOR SHARE`, owner, note))
		if err != nil {
			return y, err
		}
		r.Items = append(r.Items, YearlyItem{SourceVisibility: n.Visibility, SourceVersion: n.Version, NoteID: n.ID, Kind: n.Kind, Title: n.Title, Body: n.Body, OccurredOn: n.OccurredOn, TimePrecision: n.TimePrecision, Spoiler: n.Spoiler, MediaIDs: n.MediaIDs})
	}
	if err = tx.QueryRow(ctx, `INSERT INTO yearly_revisions(id,yearly_id,revision,cutoff,algorithm,title,introduction,stats,items) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING created_at`, r.ID, resource, r.Revision, r.Cutoff, r.Algorithm, r.Title, r.Introduction, r.Stats, r.Items).Scan(&r.CreatedAt); err != nil {
		return y, err
	}
	y.Revisions = []YearlyRevision{r}
	return y, tx.Commit(ctx)
}
