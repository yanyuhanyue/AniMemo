package external

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"slices"
	"strings"
	"unicode"

	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
)

func problemText(err error) string {
	var problem *fault.Error
	if errors.As(err, &problem) {
		return problem.Message
	}
	return "任务暂时无法完成，请重试或重新连接账号。"
}

type EpisodeState struct {
	ID     int64 `json:"id"`
	Number int   `json:"number"`
	State  int   `json:"state"`
}
type RemoteRecord struct {
	Value    journal.SyncValue `json:"value"`
	Private  bool              `json:"private"`
	Episodes []EpisodeState    `json:"episodes"`
}
type SyncItem struct {
	ID         string                 `json:"id"`
	SubjectID  int64                  `json:"subject_id"`
	Title      string                 `json:"title"`
	Metadata   journal.SourceMetadata `json:"metadata"`
	Local      *journal.SourceRecord  `json:"local"`
	Remote     *RemoteRecord          `json:"remote"`
	Catalog    []EpisodeState         `json:"catalog"`
	PullTarget *journal.SyncValue     `json:"pull_target"`
	PushTarget *RemoteRecord          `json:"push_target"`
	Decision   string                 `json:"decision"`
	Reason     string                 `json:"reason"`
	Allowed    []string               `json:"allowed"`
	State      string                 `json:"state"`
	Action     string                 `json:"action"`
	Result     string                 `json:"result"`
}

var remoteStatuses = map[int]string{1: "planned", 2: "completed", 3: "watching", 4: "on_hold", 5: "dropped"}
var localStatuses = map[string]int{"planned": 1, "completed": 2, "watching": 3, "on_hold": 4, "dropped": 5}

func collectionValue(in bangumi.Collection) journal.SyncValue {
	return journal.SyncValue{Status: remoteStatuses[in.Type], Score: float64(in.Rate), Notes: in.Comment, Tags: in.Tags, Progress: in.Episodes}.Normalized()
}
func episodeStates(in []bangumi.EpisodeCollection) ([]EpisodeState, error) {
	out := []EpisodeState{}
	ids := map[int64]bool{}
	numbers := map[int]bool{}
	for _, record := range in {
		ep := record.Episode
		number := int(ep.Sort)
		if ep.Type != 0 || ep.ID < 1 || ep.Sort != float64(number) || number < 1 || number > 10000 || record.Type < 0 || record.Type > 3 || ids[ep.ID] || numbers[number] {
			return nil, fault.New("validation_error", "正片目录包含重复或非整数话数，不能按连续进度同步。请关闭观看进度选项。")
		}
		ids[ep.ID] = true
		numbers[number] = true
		out = append(out, EpisodeState{ID: ep.ID, Number: number, State: record.Type})
	}
	slices.SortFunc(out, func(a, b EpisodeState) int { return a.Number - b.Number })
	return out, nil
}
func episodeHash(episodes []EpisodeState) string {
	data, _ := json.Marshal(episodes)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
func remoteEqual(a, b *RemoteRecord, progress bool) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return a.Private == b.Private && journal.SyncValuesEqual(a.Value, b.Value, progress) && (!progress || episodeHash(a.Episodes) == episodeHash(b.Episodes))
}
func pushTarget(item SyncItem, progress bool) (*RemoteRecord, error) {
	if item.Local == nil {
		return nil, fault.New("validation_error", "没有本地记录可以推送。")
	}
	value := item.Local.Value.Normalized()
	if value.Status == "recorded" {
		return nil, fault.New("validation_error", "仅记得看过的记录没有对应的 Bangumi 状态。请在需要推送时明确选择观看状态；本地回忆可继续保留。")
	}
	// Bangumi has no caught-up state; keep the local memory, project to watching.
	if value.Status == "caught_up" {
		value.Status = "watching"
	}
	value.Score = math.Round(value.Score)
	for _, tag := range value.Tags {
		if strings.IndexFunc(tag, unicode.IsSpace) >= 0 {
			return nil, fault.New("validation_error", "Bangumi 标签不能包含空格，请先调整本地标签。")
		}
	}
	target := &RemoteRecord{Value: value, Private: true, Episodes: []EpisodeState{}}
	if item.Remote != nil {
		target.Private = item.Remote.Private
		if !progress {
			target.Value.Progress = item.Remote.Value.Progress
		}
	}
	if progress {
		states := item.Catalog
		if item.Remote != nil {
			states = item.Remote.Episodes
		}
		if len(states) == 0 && value.Progress != 0 {
			return nil, fault.New("validation_error", "Bangumi 尚未提供可覆盖当前进度的正片目录。")
		}
		seen := map[int]bool{}
		for _, ep := range states {
			ep.State = 0
			if ep.Number <= value.Progress {
				ep.State = 2
				seen[ep.Number] = true
			}
			target.Episodes = append(target.Episodes, ep)
		}
		if len(seen) != value.Progress {
			return nil, fault.New("validation_error", "Bangumi 正片目录缺少当前进度中的话数，请先只同步其他字段。")
		}
	}
	return target, nil
}

func planItem(item *SyncItem, mode string, progress bool, baseline *journal.SyncBaseline) {
	item.Allowed = []string{"skip"}
	item.Decision = "skip"
	item.Reason = ""
	if item.Remote != nil {
		target, err := journal.LocalSyncTarget(item.Local, item.Metadata, item.Remote.Value, progress)
		if err == nil {
			item.PullTarget = &target
			if mode != "push" {
				item.Allowed = append(item.Allowed, "pull")
			}
		} else {
			item.Reason = problemText(err)
		}
	}
	if item.Local != nil {
		target, err := pushTarget(*item, progress)
		if err == nil {
			item.PushTarget = target
			if mode != "pull" {
				item.Allowed = append(item.Allowed, "push")
			}
		} else {
			if item.Reason != "" {
				item.Reason += "；"
			}
			item.Reason += problemText(err)
		}
	}
	if item.Local == nil {
		if mode != "push" && item.PullTarget != nil {
			item.Decision = "pull"
		}
		return
	}
	if item.Remote == nil {
		if baseline != nil {
			item.Decision = "conflict"
			item.Reason = "Bangumi 已不再收藏这部作品；选择推送会重新创建收藏。"
		} else if mode != "pull" && item.PushTarget != nil {
			item.Decision = "push"
		}
		return
	}
	if item.PushTarget != nil && remoteEqual(item.PushTarget, item.Remote, progress) {
		item.Decision = "none"
		return
	}
	if baseline == nil {
		item.Decision = "conflict"
		item.Reason = "双方内容不同且尚无共同基准，请选择保留哪一边。"
		return
	}
	localChanged := !journal.SyncValuesEqual(item.Local.Value, baseline.Local, progress)
	remoteChanged := !journal.SyncValuesEqual(item.Remote.Value, baseline.Remote, progress) || (progress && episodeHash(item.Remote.Episodes) != baseline.EpisodeHash)
	if !localChanged && !remoteChanged {
		item.Decision = "none"
		return
	}
	if localChanged && remoteChanged {
		item.Decision = "conflict"
		item.Reason = "双方都在上次同步后发生了修改，请选择方向。"
		return
	}
	if remoteChanged && mode != "push" && item.PullTarget != nil {
		item.Decision = "pull"
	}
	if localChanged && mode != "pull" && item.PushTarget != nil {
		item.Decision = "push"
	}
}

// Each component must still equal the confirmed before/after state when
// reconciling a partially completed push. An unrelated edit stops the task.
func compatibleIntermediate(current, before, target *RemoteRecord, progress bool) bool {
	if current == nil {
		return before == nil
	}
	if target == nil {
		return false
	}
	if before == nil {
		before = &RemoteRecord{Private: true, Value: journal.SyncValue{Status: "planned", Tags: []string{}}, Episodes: []EpisodeState{}}
	}
	a, b, z := current.Value.Normalized(), before.Value.Normalized(), target.Value.Normalized()
	if (a.Status != b.Status && a.Status != z.Status) || (a.Score != b.Score && a.Score != z.Score) || (a.Notes != b.Notes && a.Notes != z.Notes) || (!slices.Equal(a.Tags, b.Tags) && !slices.Equal(a.Tags, z.Tags)) || (current.Private != before.Private && current.Private != target.Private) {
		return false
	}
	if !progress {
		return true
	}
	old := map[int64]EpisodeState{}
	wanted := map[int64]EpisodeState{}
	for _, ep := range before.Episodes {
		old[ep.ID] = ep
	}
	for _, ep := range target.Episodes {
		wanted[ep.ID] = ep
	}
	if len(current.Episodes) != len(target.Episodes) {
		return false
	}
	for _, ep := range current.Episodes {
		end, ok := wanted[ep.ID]
		start := old[ep.ID]
		if !ok || ep.Number != end.Number || (ep.State != start.State && ep.State != end.State) {
			return false
		}
	}
	return true
}
