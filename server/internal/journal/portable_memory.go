package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
	"time"
)

type AnimeResource struct {
	ID         string             `json:"id"`
	Title      string             `json:"title"`
	Identities []ExternalIdentity `json:"identities"`
}
type ExternalIdentity struct {
	Provider   string          `json:"provider"`
	ExternalID string          `json:"external_id"`
	Active     bool            `json:"active"`
	Metadata   json.RawMessage `json:"metadata"`
	RecordedAt time.Time       `json:"recorded_at"`
}

func exportMemory(ctx context.Context, tx pgx.Tx, owner string, out *Export) error {
	var resources, revisions int
	if err := tx.QueryRow(ctx, `SELECT (SELECT count(*) FROM anime_resources WHERE owner_id=$1),(SELECT count(*) FROM memory_revisions WHERE owner_id=$1)`, owner).Scan(&resources, &revisions); err != nil {
		return err
	}
	if resources > 10000 || revisions > 50000 {
		return fault.New("export_too_large", "完整记忆导出超过直接导出上限，请使用实例备份。")
	}
	rows, err := tx.Query(ctx, `SELECT a.id,a.title,coalesce((SELECT jsonb_agg(jsonb_build_object('provider',provider,'external_id',external_id,'active',active,'metadata',metadata,'recorded_at',recorded_at) ORDER BY provider,external_id) FROM anime_external_identities WHERE anime_id=a.id),'[]'::jsonb) FROM anime_resources a WHERE owner_id=$1 ORDER BY id`, owner)
	if err != nil {
		return err
	}
	out.Resources = []AnimeResource{}
	out.Revisions = []MemoryRevision{}
	for rows.Next() {
		var r AnimeResource
		if err = rows.Scan(&r.ID, &r.Title, &r.Identities); err != nil {
			rows.Close()
			return err
		}
		out.Resources = append(out.Resources, r)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	rows, err = tx.Query(ctx, `SELECT id,anime_id,entry_id,kind,recorded_at,snapshot FROM memory_revisions WHERE owner_id=$1 ORDER BY recorded_at,id`, owner)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var r MemoryRevision
		if err = rows.Scan(&r.ID, &r.AnimeID, &r.EntryID, &r.Kind, &r.RecordedAt, &r.Snapshot); err != nil {
			return err
		}
		out.Revisions = append(out.Revisions, r)
	}
	return rows.Err()
}

func validateMemory(out Export) error {
	if len(out.Resources) > 10000 || len(out.Revisions) > 50000 {
		return fault.New("validation_error", "记忆包超过数量限制。")
	}
	resources := map[string]bool{}
	revisions := map[string]bool{}
	for _, r := range out.Resources {
		if !id.Valid(r.ID) || resources[r.ID] || len(r.Title) == 0 || len(r.Title) > 640 || len(r.Identities) > 100 {
			return fault.New("validation_error", "作品身份无效或重复。")
		}
		resources[r.ID] = true
		seen := map[string]bool{}
		for _, external := range r.Identities {
			key := external.Provider + ":" + external.ExternalID
			if external.Provider != "bangumi" || len(external.ExternalID) > 30 || external.ExternalID == "" || seen[key] || len(external.Metadata) > 100000 {
				return fault.New("validation_error", "来源身份无效或重复。")
			}
			seen[key] = true
		}
	}
	for _, e := range out.Entries {
		if !resources[e.AnimeID] {
			return fault.New("validation_error", "条目缺少作品身份。")
		}
	}
	for _, r := range out.Revisions {
		if !id.Valid(r.ID) || !id.Valid(r.EntryID) || !resources[r.AnimeID] || revisions[r.ID] || r.RecordedAt.IsZero() || len(r.Snapshot) > 16000 {
			return fault.New("validation_error", "记忆修订引用无效或重复。")
		}
		revisions[r.ID] = true
		switch r.Kind {
		case "created", "changed", "removed", "watch.created", "watch.corrected", "watch.retracted":
		default:
			return fault.New("validation_error", "不支持的记忆修订类型。")
		}
		var snapshot map[string]json.RawMessage
		if err := json.Unmarshal(r.Snapshot, &snapshot); err != nil || snapshot == nil {
			return fault.New("validation_error", "记忆修订内容无效。")
		}
	}
	return nil
}

func restoreMemoryResources(ctx context.Context, tx pgx.Tx, owner string, resources []AnimeResource) (map[string]string, error) {
	mapping := map[string]string{}
	for _, r := range resources {
		local := id.New()
		mapping[r.ID] = local
		if _, err := tx.Exec(ctx, `INSERT INTO anime_resources(id,owner_id,title) VALUES($1,$2,$3)`, local, owner, r.Title); err != nil {
			return nil, err
		}
		for _, external := range r.Identities {
			if _, err := tx.Exec(ctx, `INSERT INTO anime_external_identities(anime_id,provider,external_id,active,metadata,recorded_at) VALUES($1,$2,$3,$4,$5,$6)`, local, external.Provider, external.ExternalID, external.Active, external.Metadata, external.RecordedAt); err != nil {
				return nil, err
			}
		}
	}
	return mapping, nil
}

func restoreMemoryRevisions(ctx context.Context, tx pgx.Tx, owner string, revisions []MemoryRevision, resources, entries, records map[string]string) error {
	for _, r := range revisions {
		entry := entries[r.EntryID]
		if entry == "" {
			entry = id.New()
			entries[r.EntryID] = entry
		}
		var snapshot map[string]json.RawMessage
		if err := json.Unmarshal(r.Snapshot, &snapshot); err != nil {
			return err
		}
		if raw, ok := snapshot["id"]; ok {
			var old string
			json.Unmarshal(raw, &old)
			if records[old] == "" {
				records[old] = id.New()
			}
			snapshot["id"], _ = json.Marshal(records[old])
		}
		if _, ok := snapshot["entry_id"]; ok {
			snapshot["entry_id"], _ = json.Marshal(entry)
		}
		// The old request token is provenance only; never recreate a mutation token.
		delete(snapshot, "request_id")
		if _, err := tx.Exec(ctx, `INSERT INTO memory_revisions(owner_id,anime_id,entry_id,kind,recorded_at,snapshot) VALUES($1,$2,$3,$4,$5,$6)`, owner, resources[r.AnimeID], entry, r.Kind, r.RecordedAt, snapshot); err != nil {
			return err
		}
	}
	return nil
}
