package journal

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"animemo.local/server/internal/fault"
)

func (r *RecordInput) validateTime() error {
	if r.TimePrecision == "" {
		r.TimePrecision = "day"
	}
	if r.TimePrecision == "unknown" {
		if r.WatchedOn != "" {
			return fault.Field("watched_on", "未知日期不能附带精确日期。")
		}
		return nil
	}
	value := r.WatchedOn
	switch r.TimePrecision {
	case "day":
	case "month":
		if len(value) == 7 {
			value += "-01"
		}
		if !strings.HasSuffix(value, "-01") {
			return fault.Field("watched_on", "月份记录必须以该月第一天存储。")
		}
	case "year":
		if len(value) == 4 {
			value += "-01-01"
		}
		if !strings.HasSuffix(value, "-01-01") {
			return fault.Field("watched_on", "年份记录必须以该年第一天存储。")
		}
	default:
		return fault.Field("time_precision", "时间精度无效。")
	}
	d, err := time.Parse("2006-01-02", value)
	if err != nil || d.Year() < 1900 || d.Year() > 2100 {
		return fault.Field("watched_on", "请输入有效的观看日期。")
	}
	r.WatchedOn = value
	return nil
}

type MemoryRevision struct {
	EntryID    string          `json:"entry_id"`
	ID         string          `json:"id"`
	AnimeID    string          `json:"anime_id"`
	Kind       string          `json:"kind"`
	RecordedAt time.Time       `json:"recorded_at"`
	Snapshot   json.RawMessage `json:"snapshot"`
}

func (s *Service) Revisions(ctx context.Context, owner, entryID string) ([]MemoryRevision, error) {
	if _, err := s.Get(ctx, owner, entryID); err != nil {
		return nil, err
	}
	rows, err := s.pool.Query(ctx, `SELECT id,anime_id,entry_id,kind,recorded_at,snapshot FROM memory_revisions WHERE owner_id=$1 AND entry_id=$2 ORDER BY recorded_at DESC,id DESC LIMIT 100`, owner, entryID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []MemoryRevision{}
	for rows.Next() {
		var r MemoryRevision
		if err = rows.Scan(&r.ID, &r.AnimeID, &r.EntryID, &r.Kind, &r.RecordedAt, &r.Snapshot); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
