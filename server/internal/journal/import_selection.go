package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"github.com/jackc/pgx/v5"
)

type ImportSelection struct {
	Index         int    `json:"index"`
	Records       []int  `json:"records"`
	TargetID      string `json:"target_id"`
	TargetVersion int    `json:"target_version"`
}

func selectImportItems(ctx context.Context, tx pgx.Tx, owner, job string, selection []ImportSelection) error {
	var doc ImportDocument
	if err := tx.QueryRow(ctx, `SELECT document FROM import_jobs WHERE id=$1`, job).Scan(&doc); err != nil {
		return err
	}
	if doc.CompleteMemory && selection != nil {
		return fault.Field("selection", "完整记忆包需整体恢复，不能拆散身份关系。")
	}
	selected := []ImportItem{}
	if selection == nil {
		for _, item := range doc.Items {
			if !item.Duplicate {
				selected = append(selected, item)
			}
		}
	} else {
		if len(selection) == 0 || len(selection) > 5000 {
			return fault.Field("selection", "请选择至少一项。")
		}
		seen := map[int]bool{}
		targets := map[string]bool{}
		for _, choice := range selection {
			if choice.Index < 0 || choice.Index >= len(doc.Items) || seen[choice.Index] {
				return fault.Field("selection", "预览项目无效或重复。")
			}
			seen[choice.Index] = true
			item := doc.Items[choice.Index]
			if choice.TargetID != "" {
				if !id.Valid(choice.TargetID) || targets[choice.TargetID] {
					return fault.Field("selection", "目标作品无效或重复；每次只对同一作品追加一组。")
				}
				targets[choice.TargetID] = true
				var version int
				if err := tx.QueryRow(ctx, `SELECT version FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL`, choice.TargetID, owner).Scan(&version); err != nil {
					return libraryMissing(err)
				}
				if err := libraryVersion(version, choice.TargetVersion); err != nil {
					return err
				}
				item.TargetID = choice.TargetID
				item.TargetVersion = choice.TargetVersion
			} else if item.Duplicate {
				return fault.Field("selection", "重复项目需要明确匹配已有作品，或者取消选择。")
			}
			records := []RecordInput{}
			sources := []ImportSource{}
			seenRecord := map[int]bool{}
			maxEpisode := 0
			for _, index := range choice.Records {
				if index < 0 || index >= len(item.History) || seenRecord[index] {
					return fault.Field("selection", "所选观看记录无效或重复。")
				}
				seenRecord[index] = true
				r := item.History[index]
				records = append(records, r)
				source := ImportSource{}
				if index < len(item.Sources) {
					source = item.Sources[index]
				}
				sources = append(sources, source)
				maxEpisode = max(maxEpisode, r.EpisodeTo)
			}
			if item.TargetID != "" && len(records) == 0 {
				return fault.Field("selection", "追加到已有作品至少需要选择一条观看记录。")
			}
			if len(item.History) > 0 {
				item.Entry.WatchedEpisodes = maxEpisode
				if maxEpisode == 0 {
					item.Entry.Status = "planned"
				} else if item.Entry.TotalEpisodes == 0 || maxEpisode < item.Entry.TotalEpisodes {
					item.Entry.Status = "watching"
				}
			}
			item.History = records
			item.Sources = sources
			item.HistoryIDs = nil
			selected = append(selected, item)
		}
	}
	doc.Items = selected
	_, err := tx.Exec(ctx, `UPDATE import_jobs SET document=$2,selection=$3 WHERE id=$1`, job, doc, selection)
	return err
}
func appendImportedRecords(ctx context.Context, tx pgx.Tx, owner string, item ImportItem) error {
	e, err := scanEntry(tx.QueryRow(ctx, `SELECT `+columns+` FROM entries WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE`, item.TargetID, owner))
	if err != nil {
		return err
	}
	if err = libraryVersion(e.Version, item.TargetVersion); err != nil {
		return err
	}
	inserted := false
	for i, r := range item.History {
		if e.TotalEpisodes > 0 && r.EpisodeTo > e.TotalEpisodes {
			return fault.Field("episode_to", "追加话数超过目标作品总话数，整批导入未提交。")
		}
		var duplicate bool
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM watch_records WHERE entry_id=$1 AND watched_on IS NOT DISTINCT FROM nullif($2,'')::date AND time_precision=$3 AND episode_from=$4 AND episode_to=$5 AND rewatch=$6 AND note=$7)`, e.ID, r.WatchedOn, r.TimePrecision, r.EpisodeFrom, r.EpisodeTo, r.Rewatch, r.Note).Scan(&duplicate); err != nil {
			return err
		}
		if duplicate {
			continue
		}
		source := ImportSource{}
		if i < len(item.Sources) {
			source = item.Sources[i]
		}
		if _, err = tx.Exec(ctx, `INSERT INTO watch_records(id,entry_id,watched_on,time_precision,episode_from,episode_to,rewatch,note,request_id,source_line,source_filename) VALUES($1,$2,nullif($3,'')::date,$4,$5,$6,$7,$8,$9,$10,$11)`, id.New(), e.ID, r.WatchedOn, r.TimePrecision, r.EpisodeFrom, r.EpisodeTo, r.Rewatch, r.Note, id.New(), source.Line, source.Filename); err != nil {
			return err
		}
		inserted = true
		e.WatchedEpisodes = max(e.WatchedEpisodes, r.EpisodeTo)
	}
	if !inserted {
		return nil
	}
	if e.Status == "recorded" || e.Status == "planned" || e.Status == "watching" || e.Status == "caught_up" || e.Status == "completed" {
		e.Status = "watching"
		if e.TotalEpisodes > 0 && e.WatchedEpisodes == e.TotalEpisodes {
			e.Status = "caught_up"
			if e.AiringState == "finished" {
				e.Status = "completed"
			}
		}
	}
	_, err = tx.Exec(ctx, `UPDATE entries SET watched_episodes=$2,status=$3,version=version+1,updated_at=now() WHERE id=$1`, e.ID, e.WatchedEpisodes, e.Status)
	return err
}
