package journal

import (
	"animemo.local/server/internal/telemetry"
	"context"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/media"
)

type Source struct {
	Provider    string    `json:"provider"`
	SubjectID   int64     `json:"subject_id"`
	RefreshedAt time.Time `json:"refreshed_at"`
}
type SourceMetadata struct {
	SubjectID     int64   `json:"subject_id"`
	Title         string  `json:"title"`
	OriginalTitle string  `json:"original_title"`
	Format        string  `json:"format"`
	TotalEpisodes int     `json:"total_episodes"`
	Details       Details `json:"details"`
}

// ApplySource owns identity, quota and version checks. Network I/O is finished before entering this transaction.
func (s *Service) ApplySource(ctx context.Context, owner, entryID string, version int, metadata SourceMetadata, fields []string, cover *media.Image) (Entry, error) {
	if metadata.SubjectID < 1 || metadata.SubjectID > 2147483647 {
		return Entry{}, fault.Field("subject_id", "Bangumi 条目标识无效。")
	}
	selected := map[string]bool{}
	for _, field := range fields {
		switch field {
		case "title", "original_title", "format", "total_episodes", "studio", "airing_period", "description", "reference_url":
			selected[field] = true
		default:
			return Entry{}, fault.Field("fields", "请选择受支持的作品资料字段。")
		}
	}
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Entry{}, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226043))`, owner); err != nil {
		return Entry{}, err
	}
	if cover != nil {
		if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,718226042))`, owner); err != nil {
			return Entry{}, err
		}
	}
	var duplicate bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM entry_sources WHERE user_id=$1 AND provider='bangumi' AND subject_id=$2 AND entry_id::text<>$3)`, owner, metadata.SubjectID, entryID).Scan(&duplicate); err != nil {
		return Entry{}, err
	}
	if duplicate {
		return Entry{}, fault.New("version_conflict", "这部 Bangumi 作品已绑定到手账中的另一条记录，请打开已有记录。")
	}
	var entry Entry
	if entryID == "" {
		var count int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM entries WHERE user_id=$1`, owner).Scan(&count); err != nil {
			return Entry{}, err
		}
		if count >= 5000 {
			return Entry{}, fault.New("validation_error", "手账最多保存 5000 部番剧。")
		}
		entry = Entry{Title: metadata.Title, Status: "recorded", AiringState: "unknown", Format: "tv", Accent: "violet", Tags: []string{}, Visibility: "private"}
	} else {
		entry, err = scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id::text=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, entryID, owner))
		if err != nil {
			return Entry{}, err
		}
		if version != entry.Version {
			return Entry{}, fault.New("version_conflict", "资料预览后记录已改变，请重新打开预览。")
		}
	}
	if selected["title"] {
		entry.Title = metadata.Title
	}
	if selected["original_title"] {
		entry.OriginalTitle = metadata.OriginalTitle
	}
	if selected["format"] {
		entry.Format = metadata.Format
	}
	if selected["total_episodes"] {
		entry.TotalEpisodes = metadata.TotalEpisodes
	}
	if selected["studio"] {
		entry.Details.Studio = metadata.Details.Studio
	}
	if selected["airing_period"] {
		entry.Details.AiringPeriod = metadata.Details.AiringPeriod
	}
	if selected["description"] {
		entry.Details.Description = metadata.Details.Description
	}
	if selected["reference_url"] {
		entry.Details.ReferenceURL = metadata.Details.ReferenceURL
	}
	if err = entry.Validate(); err != nil {
		return Entry{}, err
	}
	if entryID == "" {
		entry, err = insertEntry(ctx, tx, owner, Create{Title: entry.Title, OriginalTitle: entry.OriginalTitle, Format: entry.Format, TotalEpisodes: entry.TotalEpisodes, Details: entry.Details})
		entryID = entry.ID
	} else {
		// Metadata refresh never changes personal scores, tags, notes, visibility or watch progress.
		entry, err = updateEntry(ctx, tx, owner, entryID, Patch{Version: version, Title: &entry.Title, OriginalTitle: &entry.OriginalTitle, Format: &entry.Format, TotalEpisodes: &entry.TotalEpisodes, Details: &entry.Details})
	}
	if err != nil {
		return Entry{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO entry_sources(entry_id,user_id,provider,subject_id,metadata) VALUES($1,$2,'bangumi',$3,$4) ON CONFLICT(entry_id) DO UPDATE SET subject_id=excluded.subject_id,metadata=excluded.metadata,refreshed_at=now()`, entryID, owner, metadata.SubjectID, metadata); err != nil {
		return Entry{}, err
	}
	if cover != nil {
		if err = saveCover(ctx, tx, owner, entryID, cover); err != nil {
			return Entry{}, err
		}
	}
	entry, err = scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1`, entryID))
	if err != nil {
		return Entry{}, err
	}
	return entry, tx.Commit(ctx)
}

func (s *Service) UnbindSource(ctx context.Context, owner, entryID string, version int) (Entry, error) {
	tx, err := telemetry.Begin(ctx, s.pool)
	if err != nil {
		return Entry{}, err
	}
	defer tx.Rollback(context.Background())
	if _, err = updateEntry(ctx, tx, owner, entryID, Patch{Version: version}); err != nil {
		return Entry{}, err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM entry_sources WHERE entry_id=$1 AND user_id=$2`, entryID, owner); err != nil {
		return Entry{}, err
	}
	entry, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1`, entryID))
	if err != nil {
		return Entry{}, err
	}
	return entry, tx.Commit(ctx)
}
