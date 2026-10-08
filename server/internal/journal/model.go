package journal

import (
	"math"
	"strings"
	"time"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
)

type Entry struct {
	AnimeID         string    `json:"anime_id"`
	AiringState     string    `json:"airing_state"`
	Source          *Source   `json:"source"`
	Visibility      string    `json:"visibility"`
	ShareSlug       string    `json:"share_slug"`
	ID              string    `json:"id"`
	Title           string    `json:"title"`
	OriginalTitle   string    `json:"original_title"`
	Format          string    `json:"format"`
	Status          string    `json:"status"`
	TotalEpisodes   int       `json:"total_episodes"`
	WatchedEpisodes int       `json:"watched_episodes"`
	Score           *float64  `json:"score"`
	Notes           string    `json:"notes"`
	Tags            []string  `json:"tags"`
	Accent          string    `json:"accent"`
	Details         Details   `json:"details"`
	CoverRevision   *string   `json:"cover_revision"`
	Version         int       `json:"version"`
	CreatedAt       time.Time `json:"created_at"`
	UpdatedAt       time.Time `json:"updated_at"`
}

type Create struct {
	AnimeID       string   `json:"-"`
	AiringState   string   `json:"airing_state"`
	Visibility    string   `json:"visibility"`
	Details       Details  `json:"details"`
	Title         string   `json:"title"`
	OriginalTitle string   `json:"original_title"`
	Format        string   `json:"format"`
	Status        string   `json:"status"`
	TotalEpisodes int      `json:"total_episodes"`
	Score         *float64 `json:"score"`
	Notes         string   `json:"notes"`
	Tags          []string `json:"tags"`
	Accent        string   `json:"accent"`
}

// A patch always carries the version that was shown to the user.
// Score=0 clears a score; absence leaves it unchanged.
type Patch struct {
	AiringState   *string   `json:"airing_state"`
	Visibility    *string   `json:"visibility"`
	Details       *Details  `json:"details"`
	Version       int       `json:"version"`
	Title         *string   `json:"title"`
	OriginalTitle *string   `json:"original_title"`
	Format        *string   `json:"format"`
	Status        *string   `json:"status"`
	TotalEpisodes *int      `json:"total_episodes"`
	Score         *float64  `json:"score"`
	Notes         *string   `json:"notes"`
	Tags          *[]string `json:"tags"`
	Accent        *string   `json:"accent"`
}

type Record struct {
	SourceLine     int       `json:"source_line"`
	SourceFilename string    `json:"source_filename"`
	TimePrecision  string    `json:"time_precision"`
	Rewatch        int       `json:"rewatch"`
	Version        int       `json:"version"`
	ID             string    `json:"id"`
	EntryID        string    `json:"entry_id"`
	EntryTitle     string    `json:"entry_title"`
	Accent         string    `json:"accent"`
	WatchedOn      string    `json:"watched_on"`
	EpisodeFrom    int       `json:"episode_from"`
	EpisodeTo      int       `json:"episode_to"`
	Note           string    `json:"note"`
	RequestID      string    `json:"request_id"`
	CreatedAt      time.Time `json:"created_at"`
}

type RecordInput struct {
	TimePrecision string `json:"time_precision"`
	Rewatch       int    `json:"rewatch"`
	WatchedOn     string `json:"watched_on"`
	EpisodeFrom   int    `json:"episode_from"`
	EpisodeTo     int    `json:"episode_to"`
	Note          string `json:"note"`
	RequestID     string `json:"request_id"`
}

type Stats struct {
	Total           int `json:"total"`
	Recorded        int `json:"recorded"`
	Watching        int `json:"watching"`
	CaughtUp        int `json:"caught_up"`
	Completed       int `json:"completed"`
	Planned         int `json:"planned"`
	OnHold          int `json:"on_hold"`
	Dropped         int `json:"dropped"`
	WatchedEpisodes int `json:"watched_episodes"`
	WatchRecords    int `json:"watch_records"`
}

type Page struct {
	Items    []Entry `json:"items"`
	Total    int     `json:"total"`
	Page     int     `json:"page"`
	PageSize int     `json:"page_size"`
}

type Filter struct {
	Search, Status, Sort string
	Page, PageSize       int
}

func ValidStatus(s string) bool {
	return s == "recorded" || s == "planned" || s == "watching" || s == "caught_up" || s == "completed" || s == "on_hold" || s == "dropped"
}

func (e *Entry) Validate() error {
	if e.AiringState == "" {
		e.AiringState = "finished"
	}
	if e.AiringState != "finished" && e.AiringState != "airing" && e.AiringState != "unknown" {
		return fault.Field("airing_state", "播出状态无效。")
	}
	if e.Visibility == "" {
		e.Visibility = "private"
	}
	if e.Visibility != "private" && e.Visibility != "unlisted" && e.Visibility != "public" {
		return fault.Field("visibility", "可见性无效。")
	}
	for _, value := range append([]string{e.Title, e.OriginalTitle, e.Notes, e.Details.Studio, e.Details.AiringPeriod, e.Details.Description, e.Details.ReferenceURL}, e.Tags...) {
		if strings.ContainsRune(value, 0) {
			return fault.New("validation_error", "文字中不能包含空字节。")
		}
	}
	e.Title, e.OriginalTitle = strings.TrimSpace(e.Title), strings.TrimSpace(e.OriginalTitle)
	if n := utf8.RuneCountInString(e.Title); n < 1 || n > 160 {
		return fault.Field("title", "番剧名称需要 1–160 个字。")
	}
	if utf8.RuneCountInString(e.OriginalTitle) > 160 {
		return fault.Field("original_title", "原名不能超过 160 个字。")
	}
	if !ValidStatus(e.Status) {
		return fault.Field("status", "观看状态无效。")
	}
	if e.Format != "tv" && e.Format != "movie" && e.Format != "ova" && e.Format != "other" {
		return fault.Field("format", "番剧类型无效。")
	}
	if e.TotalEpisodes < 0 || e.TotalEpisodes > 10000 || e.WatchedEpisodes < 0 || e.WatchedEpisodes > 10000 {
		return fault.Field("total_episodes", "话数需要在 0–10000 之间。")
	}
	if e.TotalEpisodes > 0 && e.WatchedEpisodes > e.TotalEpisodes {
		return fault.Field("total_episodes", "总话数不能少于已经记录的观看进度。")
	}
	if e.Score != nil && (math.IsNaN(*e.Score) || math.IsInf(*e.Score, 0) || *e.Score < 1 || *e.Score > 10 || math.Abs(*e.Score*10-math.Round(*e.Score*10)) > 0.00001) {
		return fault.Field("score", "评分需要在 1–10 之间，最多一位小数。")
	}
	if utf8.RuneCountInString(e.Notes) > 4000 {
		return fault.Field("notes", "短评不能超过 4000 个字。")
	}
	if e.Accent != "violet" && e.Accent != "coral" && e.Accent != "blue" && e.Accent != "green" && e.Accent != "amber" {
		return fault.Field("accent", "封面颜色无效。")
	}
	tags := make([]string, 0, len(e.Tags))
	seen := map[string]bool{}
	for _, tag := range e.Tags {
		tag = strings.TrimSpace(tag)
		if tag == "" {
			continue
		}
		if utf8.RuneCountInString(tag) > 24 {
			return fault.Field("tags", "每个标签最多 24 个字。")
		}
		if !seen[tag] {
			tags = append(tags, tag)
			seen[tag] = true
		}
	}
	if len(tags) > 8 {
		return fault.Field("tags", "每部番剧最多添加 8 个标签。")
	}
	e.Tags = tags
	return e.Details.Validate()
}

func (r *RecordInput) Validate() error {
	if strings.ContainsRune(r.Note, 0) {
		return fault.Field("note", "观看笔记中不能包含空字节。")
	}
	if r.Rewatch == 0 {
		r.Rewatch = 1
	}
	if r.Rewatch < 1 || r.Rewatch > 1000 {
		return fault.Field("rewatch", "重看次数需要在 1–1000 之间。")
	}
	if err := r.validateTime(); err != nil {
		return err
	}
	if r.EpisodeFrom < 1 || r.EpisodeTo < r.EpisodeFrom || r.EpisodeTo > 10000 {
		return fault.Field("episode_to", "请填写有效的话数范围。")
	}
	if utf8.RuneCountInString(r.Note) > 2000 {
		return fault.Field("note", "观看笔记不能超过 2000 个字。")
	}
	if !id.Valid(r.RequestID) {
		return fault.Field("request_id", "请求标识无效，请刷新后重试。")
	}
	return nil
}

func (f *Filter) Validate() error {
	f.Search = strings.TrimSpace(f.Search)
	if utf8.RuneCountInString(f.Search) > 160 {
		return fault.Field("search", "搜索词不能超过 160 个字。")
	}
	if f.Status != "" && !ValidStatus(f.Status) {
		return fault.Field("status", "观看状态无效。")
	}
	if f.Page < 1 || f.Page > 100000 || f.PageSize < 1 || f.PageSize > 100 {
		return fault.New("validation_error", "分页参数无效。")
	}
	if f.Sort == "" {
		f.Sort = "updated"
	}
	if f.Sort != "updated" && f.Sort != "title" && f.Sort != "score" {
		return fault.Field("sort", "排序方式无效。")
	}
	return nil
}
