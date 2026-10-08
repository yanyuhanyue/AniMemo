package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"github.com/jackc/pgx/v5"
	"time"
)

// v3 carries the original private attachment bytes. Cover originals remain in ZIP.
// No share bearer token is exported; visibility and relationships are preserved.
type PortableMemoryMedia struct {
	MemoryMedia
	Data []byte `json:"data"`
}
type LibraryBundle struct {
	Achievements AchievementBundle     `json:"achievements"`
	Characters   []Character           `json:"characters"`
	Episodes     []Episode             `json:"episodes"`
	Relations    []AnimeRelation       `json:"relations"`
	Progress     []ProgressAssertion   `json:"progress"`
	Notes        []MemoryNote          `json:"notes"`
	Media        []PortableMemoryMedia `json:"media"`
	Collections  []MemoryCollection    `json:"collections"`
	Yearlies     []YearlyMemory        `json:"yearlies"`
	Revisions    []LibraryRevision     `json:"revisions"`
}

func collectLibrary[T any](ctx context.Context, tx pgx.Tx, query, owner string, scan func(pgx.Row) (T, error)) ([]T, error) {
	rows, err := tx.Query(ctx, query, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []T{}
	for rows.Next() {
		v, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func exportLibrary(ctx context.Context, tx pgx.Tx, owner string) (*LibraryBundle, error) {
	b := &LibraryBundle{}
	var err error
	b.Characters, err = collectLibrary(ctx, tx, `SELECT `+characterColumns+` FROM characters WHERE owner_id=$1 ORDER BY id`, owner, scanCharacter)
	if err != nil {
		return nil, err
	}
	b.Episodes, err = collectLibrary(ctx, tx, `SELECT `+episodeColumns+` FROM episodes WHERE owner_id=$1 ORDER BY id`, owner, scanEpisode)
	if err != nil {
		return nil, err
	}
	b.Notes, err = collectLibrary(ctx, tx, `SELECT `+noteColumns+` FROM memory_notes WHERE owner_id=$1 ORDER BY id`, owner, scanNote)
	if err != nil {
		return nil, err
	}
	b.Collections, err = collectLibrary(ctx, tx, `SELECT `+collectionColumns+` FROM memory_collections WHERE owner_id=$1 ORDER BY id`, owner, scanCollection)
	if err != nil {
		return nil, err
	}
	b.Yearlies, err = collectLibrary(ctx, tx, `SELECT `+yearlyColumns+` FROM yearly_memories WHERE owner_id=$1 ORDER BY id`, owner, scanYearly)
	if err != nil {
		return nil, err
	}
	for i := range b.Yearlies {
		b.Yearlies[i].Revisions, err = collectLibrary(ctx, tx, `SELECT id,revision,cutoff,algorithm,title,introduction,stats,items,created_at FROM yearly_revisions WHERE yearly_id=$1 ORDER BY revision`, b.Yearlies[i].ID, func(row pgx.Row) (YearlyRevision, error) {
			var r YearlyRevision
			err := row.Scan(&r.ID, &r.Revision, &r.Cutoff, &r.Algorithm, &r.Title, &r.Introduction, &r.Stats, &r.Items, &r.CreatedAt)
			return r, err
		})
		if err != nil {
			return nil, err
		}
	}
	b.Relations, err = collectLibrary(ctx, tx, `SELECT from_id,to_id,relation FROM anime_relations WHERE owner_id=$1 ORDER BY from_id,to_id,relation`, owner, func(row pgx.Row) (AnimeRelation, error) {
		var r AnimeRelation
		err := row.Scan(&r.FromID, &r.ToID, &r.Relation)
		return r, err
	})
	if err != nil {
		return nil, err
	}
	b.Progress, err = collectLibrary(ctx, tx, `SELECT id,anime_id,scope,precision,episode_ids,note,created_at FROM progress_assertions WHERE owner_id=$1 ORDER BY id`, owner, func(row pgx.Row) (ProgressAssertion, error) {
		var p ProgressAssertion
		err := row.Scan(&p.ID, &p.AnimeID, &p.Scope, &p.Precision, &p.EpisodeIDs, &p.Note, &p.CreatedAt)
		return p, err
	})
	if err != nil {
		return nil, err
	}
	b.Revisions, err = collectLibrary(ctx, tx, `SELECT id,resource_id,kind,action,snapshot,recorded_at FROM library_revisions WHERE owner_id=$1 ORDER BY recorded_at,id`, owner, func(row pgx.Row) (LibraryRevision, error) {
		var r LibraryRevision
		err := row.Scan(&r.ID, &r.ResourceID, &r.Kind, &r.Action, &r.Snapshot, &r.RecordedAt)
		return r, err
	})
	if err != nil {
		return nil, err
	}
	b.Media, err = collectLibrary(ctx, tx, `SELECT `+memoryMediaColumns+`,data FROM private_memory_media WHERE owner_id=$1 AND state<>'reserved' ORDER BY id`, owner, func(row pgx.Row) (PortableMemoryMedia, error) {
		var m PortableMemoryMedia
		err := row.Scan(&m.ID, &m.State, &m.ContentType, &m.SHA256, &m.ByteSize, &m.Width, &m.Height, &m.CreatedAt, &m.DeletedAt, &m.Data)
		return m, err
	})
	if err != nil {
		return nil, err
	}
	b.Achievements, err = exportAchievements(ctx, tx, owner)
	if err != nil {
		return nil, err
	}
	if len(b.Achievements.Unlocks)+len(b.Achievements.Progress)+len(b.Characters)+len(b.Episodes)+len(b.Notes)+len(b.Collections)+len(b.Yearlies)+len(b.Revisions)+len(b.Media)+len(b.Progress)+len(b.Relations) == 0 {
		return nil, nil
	}
	if len(b.Revisions) > 50000 || len(b.Progress) > 20000 || len(b.Relations) > 20000 {
		return nil, fault.New("export_too_large", "记忆库超过直接导出范围，请使用实例快照。")
	}
	return b, nil
}
func validateLibrary(b *LibraryBundle, resources []AnimeResource) error {
	if b == nil {
		return nil
	}
	if err := validateAchievementBundle(b.Achievements); err != nil {
		return err
	}
	bad := fault.New("validation_error", "记忆库包含无效、重复或悬空的身份/日期/媒体；整份导入已拒绝。")
	if len(b.Characters) > 5000 || len(b.Episodes) > 50000 || len(b.Notes) > 10000 || len(b.Collections) > 500 || len(b.Yearlies) > 300 || len(b.Revisions) > 50000 || len(b.Progress) > 20000 || len(b.Relations) > 20000 || len(b.Media) > 10000 {
		return bad
	}
	all := map[string]bool{}
	anime := map[string]bool{}
	chars := map[string]Character{}
	episodes := map[string]Episode{}
	notes := map[string]MemoryNote{}
	images := map[string]PortableMemoryMedia{}
	add := func(value string) bool {
		if !id.Valid(value) || all[value] {
			return false
		}
		all[value] = true
		return true
	}
	for _, r := range resources {
		anime[r.ID] = true
		all[r.ID] = true
	}
	for _, c := range b.Characters {
		if !add(c.ID) || c.Name == "" || !libraryText(c.Name, 160) || !libraryText(c.Description, 10000) || !libraryTags(c.Aliases, 30) || !libraryIDs(c.AnimeIDs, 100) || !libraryVisibility(c.Visibility) || c.Version < 1 || c.CreatedAt.IsZero() || c.UpdatedAt.IsZero() {
			return bad
		}
		for _, a := range c.AnimeIDs {
			if !anime[a] {
				return bad
			}
		}
		chars[c.ID] = c
	}
	for _, c := range b.Characters {
		seen := map[string]bool{c.ID: true}
		target := c.RedirectID
		for target != "" {
			next, ok := chars[target]
			if !ok || seen[target] {
				return bad
			}
			seen[target] = true
			target = next.RedirectID
		}
	}
	for _, e := range b.Episodes {
		if !add(e.ID) || !anime[e.AnimeID] || e.Title == "" || !libraryText(e.Title, 160) || e.Number < 0 || e.Number > 100000 || (e.Kind != "main" && e.Kind != "special" && e.Kind != "ova" && e.Kind != "movie") || (e.ProgressRole != "required" && e.ProgressRole != "optional" && e.ProgressRole != "excluded") || e.Version < 1 || len(e.Identities) > 10 || e.CreatedAt.IsZero() || e.UpdatedAt.IsZero() {
			return bad
		}
		seen := map[string]bool{}
		for _, v := range e.Identities {
			key := v.Provider + ":" + v.ExternalID
			if v.Provider == "" || v.ExternalID == "" || !libraryText(v.Provider, 40) || !libraryText(v.ExternalID, 160) || seen[key] {
				return bad
			}
			seen[key] = true
		}
		episodes[e.ID] = e
	}
	for _, e := range b.Episodes {
		seen := map[string]bool{e.ID: true}
		target := e.RedirectID
		for target != "" {
			next, ok := episodes[target]
			if !ok || next.AnimeID != e.AnimeID || seen[target] {
				return bad
			}
			seen[target] = true
			target = next.RedirectID
		}
	}
	used := 0
	for _, m := range b.Media {
		if !add(m.ID) || m.CreatedAt.IsZero() {
			return bad
		}
		if m.State == "deleted" {
			if len(m.Data) != 0 {
				return bad
			}
		} else if m.State == "ready" {
			pic, err := media.Validate(m.Data, m.ContentType)
			hash := sha256.Sum256(m.Data)
			if err != nil || m.SHA256 != hex.EncodeToString(hash[:]) || m.ByteSize != len(m.Data) || pic.Width != m.Width || pic.Height != m.Height {
				return bad
			}
			used += m.ByteSize
		} else {
			return bad
		}
		images[m.ID] = m
	}
	if used > MemoryMediaQuota {
		return bad
	}
	for _, n := range b.Notes {
		if !add(n.ID) || n.Version < 1 || n.CreatedAt.IsZero() || n.UpdatedAt.IsZero() {
			return bad
		}
		input := n.MemoryNoteInput
		if err := input.validate(); err != nil {
			return err
		}
		if n.AnimeID != "" && !anime[n.AnimeID] {
			return bad
		}
		if n.CharacterID != "" {
			if _, ok := chars[n.CharacterID]; !ok {
				return bad
			}
		}
		if n.EpisodeID != "" {
			e, ok := episodes[n.EpisodeID]
			if !ok || (n.AnimeID != "" && e.AnimeID != n.AnimeID) {
				return bad
			}
		}
		for _, m := range n.MediaIDs {
			if _, ok := images[m]; !ok {
				return bad
			}
		}
		notes[n.ID] = n
	}
	for _, c := range b.Collections {
		if !add(c.ID) || c.Title == "" || !libraryText(c.Title, 160) || !libraryText(c.Description, 4000) || !libraryVisibility(c.Visibility) || len(c.Items) > 500 || c.Version < 1 || c.CreatedAt.IsZero() || c.UpdatedAt.IsZero() {
			return bad
		}
		seen := map[string]bool{}
		for _, i := range c.Items {
			if seen[i.Kind+i.ID] {
				return bad
			}
			seen[i.Kind+i.ID] = true
			switch i.Kind {
			case "anime":
				if !anime[i.ID] {
					return bad
				}
			case "character":
				if _, ok := chars[i.ID]; !ok {
					return bad
				}
			case "note", "moment":
				if n, ok := notes[i.ID]; !ok || n.Kind != i.Kind {
					return bad
				}
			default:
				return bad
			}
		}
	}
	seenRelations := map[string]bool{}
	for _, r := range b.Relations {
		key := r.FromID + r.ToID + r.Relation
		if !anime[r.FromID] || !anime[r.ToID] || r.FromID == r.ToID || seenRelations[key] || (r.Relation != "sequel" && r.Relation != "prequel" && r.Relation != "side_story" && r.Relation != "same_franchise") {
			return bad
		}
		seenRelations[key] = true
	}
	for _, p := range b.Progress {
		if !add(p.ID) || !anime[p.AnimeID] || p.CreatedAt.IsZero() || !libraryIDs(p.EpisodeIDs, 2000) || len(p.EpisodeIDs) == 0 || !libraryText(p.Note, 4000) || (p.Scope != "custom" && p.Scope != "all" && p.Scope != "mainline") || (p.Precision != "exact" && p.Precision != "approximate" && p.Precision != "caught_up") {
			return bad
		}
		for _, ep := range p.EpisodeIDs {
			if e, ok := episodes[ep]; !ok || e.AnimeID != p.AnimeID {
				return bad
			}
		}
	}
	for _, y := range b.Yearlies {
		_, tzErr := time.LoadLocation(y.Timezone)
		if !add(y.ID) || y.Year < 1900 || y.Year > 2100 || tzErr != nil || y.Title == "" || !libraryText(y.Title, 160) || !libraryText(y.Introduction, 10000) || !libraryVisibility(y.Visibility) || y.Version < 1 || y.CreatedAt.IsZero() || y.UpdatedAt.IsZero() || len(y.Revisions) < 1 || len(y.Revisions) > 100 {
			return bad
		}
		versions := map[int]bool{}
		for _, r := range y.Revisions {
			if !add(r.ID) || r.Revision < 1 || versions[r.Revision] || r.Cutoff.IsZero() || r.CreatedAt.IsZero() || !libraryText(r.Title, 160) || !libraryText(r.Introduction, 10000) || len(r.Items) > 200 || r.Algorithm != "memory-year/v1;calendar-year;range-counts;no-inferred-facts" {
				return bad
			}
			versions[r.Revision] = true
			for _, i := range r.Items {
				if _, ok := notes[i.NoteID]; !ok {
					return bad
				}
				if !libraryVisibility(i.SourceVisibility) || i.SourceVersion < 1 || !libraryText(i.Title, 160) || !libraryText(i.Body, 40000) || !validMemoryTime(i.OccurredOn, i.TimePrecision) || !libraryIDs(i.MediaIDs, 8) {
					return bad
				}
				for _, m := range i.MediaIDs {
					if _, ok := images[m]; !ok {
						return bad
					}
				}
			}
		}
	}
	for _, r := range b.Revisions {
		if !add(r.ID) || !id.Valid(r.ResourceID) || !json.Valid(r.Snapshot) || len(r.Snapshot) > 200000 || r.RecordedAt.IsZero() {
			return bad
		}
		switch r.Kind {
		case "note", "moment", "character", "episode", "collection", "progress", "media":
		default:
			return bad
		}
	}
	return nil
}
func mapLibrarySnapshot(data json.RawMessage, mapping map[string]string) json.RawMessage {
	var value any
	if json.Unmarshal(data, &value) != nil {
		return data
	}
	singles := map[string]bool{"id": true, "resource_id": true, "anime_id": true, "character_id": true, "episode_id": true, "watch_id": true, "redirect_id": true, "note_id": true, "from_id": true, "to_id": true, "from": true, "to": true}
	arrays := map[string]bool{"anime_ids": true, "media_ids": true, "episode_ids": true}
	var walk func(any)
	walk = func(v any) {
		switch obj := v.(type) {
		case map[string]any:
			for k, child := range obj {
				if singles[k] {
					if old, ok := child.(string); ok && mapping[old] != "" {
						obj[k] = mapping[old]
					}
				} else if arrays[k] {
					if values, ok := child.([]any); ok {
						for i, raw := range values {
							if old, ok := raw.(string); ok && mapping[old] != "" {
								values[i] = mapping[old]
							}
						}
					}
				} else {
					walk(child)
				}
			}
		case []any:
			for _, child := range obj {
				walk(child)
			}
		}
	}
	walk(value)
	out, _ := json.Marshal(value)
	return out
}
func restoreLibrary(ctx context.Context, tx pgx.Tx, owner string, b *LibraryBundle, resources, records map[string]string) error {
	if b == nil {
		return nil
	}
	mapping := map[string]string{}
	for old, local := range resources {
		mapping[old] = local
	}
	for old, local := range records {
		mapping[old] = local
	}
	assign := func(old string) {
		if old != "" && mapping[old] == "" {
			mapping[old] = id.New()
		}
	}
	for _, c := range b.Characters {
		assign(c.ID)
	}
	for _, e := range b.Episodes {
		assign(e.ID)
	}
	for _, n := range b.Notes {
		assign(n.ID)
		assign(n.WatchID)
	}
	for _, m := range b.Media {
		assign(m.ID)
	}
	for _, c := range b.Collections {
		assign(c.ID)
	}
	for _, y := range b.Yearlies {
		assign(y.ID)
		for _, r := range y.Revisions {
			assign(r.ID)
		}
	}
	for _, p := range b.Progress {
		assign(p.ID)
	}
	for _, r := range b.Revisions {
		assign(r.ID)
		assign(r.ResourceID)
	}
	remap := func(values []string) []string {
		out := []string{}
		for _, v := range values {
			out = append(out, mapping[v])
		}
		return out
	}
	for _, c := range b.Characters {
		if _, err := tx.Exec(ctx, `INSERT INTO characters(id,owner_id,name,aliases,description,anime_ids,favorite,visibility,version,created_at,updated_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, mapping[c.ID], owner, c.Name, c.Aliases, c.Description, remap(c.AnimeIDs), c.Favorite, c.Visibility, c.Version, c.CreatedAt, c.UpdatedAt); err != nil {
			return err
		}
	}
	for _, c := range b.Characters {
		if c.RedirectID != "" {
			if _, err := tx.Exec(ctx, `UPDATE characters SET redirect_id=$2 WHERE id=$1`, mapping[c.ID], mapping[c.RedirectID]); err != nil {
				return err
			}
		}
	}
	for _, e := range b.Episodes {
		if _, err := tx.Exec(ctx, `INSERT INTO episodes(id,owner_id,anime_id,title,number,kind,progress_role,identities,version,created_at,updated_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, mapping[e.ID], owner, mapping[e.AnimeID], e.Title, e.Number, e.Kind, e.ProgressRole, e.Identities, e.Version, e.CreatedAt, e.UpdatedAt); err != nil {
			return err
		}
	}
	for _, e := range b.Episodes {
		if e.RedirectID != "" {
			if _, err := tx.Exec(ctx, `UPDATE episodes SET redirect_id=$2 WHERE id=$1`, mapping[e.ID], mapping[e.RedirectID]); err != nil {
				return err
			}
		}
	}
	for _, m := range b.Media {
		var thumb []byte
		var err error
		if m.State == "ready" {
			thumb, err = memoryThumbnail(m.Data)
			if err != nil {
				return err
			}
		}
		if _, err = tx.Exec(ctx, `INSERT INTO private_memory_media(id,owner_id,state,content_type,sha256,byte_size,width,height,data,thumbnail,created_at,deleted_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`, mapping[m.ID], owner, m.State, m.ContentType, m.SHA256, m.ByteSize, m.Width, m.Height, m.Data, thumb, m.CreatedAt, m.DeletedAt); err != nil {
			return err
		}
	}
	for _, n := range b.Notes {
		if _, err := tx.Exec(ctx, `INSERT INTO memory_notes(id,owner_id,kind,title,body,anime_id,character_id,episode_id,watch_id,anchor,media_ids,tags,occurred_on,time_precision,visibility,spoiler,highlight,version,created_at,updated_at,deleted_at) VALUES($1,$2,$3,$4,$5,nullif($6,'')::uuid,nullif($7,'')::uuid,nullif($8,'')::uuid,nullif($9,'')::uuid,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)`, mapping[n.ID], owner, n.Kind, n.Title, n.Body, mapping[n.AnimeID], mapping[n.CharacterID], mapping[n.EpisodeID], mapping[n.WatchID], n.Anchor, remap(n.MediaIDs), n.Tags, n.OccurredOn, n.TimePrecision, n.Visibility, n.Spoiler, n.Highlight, n.Version, n.CreatedAt, n.UpdatedAt, n.DeletedAt); err != nil {
			return err
		}
	}
	for _, c := range b.Collections {
		items := append([]CollectionItem{}, c.Items...)
		for i := range items {
			items[i].ID = mapping[items[i].ID]
		}
		if _, err := tx.Exec(ctx, `INSERT INTO memory_collections(id,owner_id,title,description,items,visibility,version,created_at,updated_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, mapping[c.ID], owner, c.Title, c.Description, items, c.Visibility, c.Version, c.CreatedAt, c.UpdatedAt); err != nil {
			return err
		}
	}
	for _, r := range b.Relations {
		if _, err := tx.Exec(ctx, `INSERT INTO anime_relations(owner_id,from_id,to_id,relation) VALUES($1,$2,$3,$4)`, owner, mapping[r.FromID], mapping[r.ToID], r.Relation); err != nil {
			return err
		}
	}
	for _, p := range b.Progress {
		if _, err := tx.Exec(ctx, `INSERT INTO progress_assertions(id,owner_id,anime_id,scope,precision,episode_ids,note,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, mapping[p.ID], owner, mapping[p.AnimeID], p.Scope, p.Precision, remap(p.EpisodeIDs), p.Note, p.CreatedAt); err != nil {
			return err
		}
	}
	for _, y := range b.Yearlies {
		if _, err := tx.Exec(ctx, `INSERT INTO yearly_memories(id,owner_id,year,timezone,title,introduction,version,visibility,created_at,updated_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`, mapping[y.ID], owner, y.Year, y.Timezone, y.Title, y.Introduction, y.Version, y.Visibility, y.CreatedAt, y.UpdatedAt); err != nil {
			return err
		}
		for _, r := range y.Revisions {
			items := append([]YearlyItem{}, r.Items...)
			for i := range items {
				items[i].NoteID = mapping[items[i].NoteID]
				items[i].MediaIDs = remap(items[i].MediaIDs)
			}
			if _, err := tx.Exec(ctx, `INSERT INTO yearly_revisions(id,yearly_id,revision,cutoff,algorithm,title,introduction,stats,items,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`, mapping[r.ID], mapping[y.ID], r.Revision, r.Cutoff, r.Algorithm, r.Title, r.Introduction, r.Stats, items, r.CreatedAt); err != nil {
				return err
			}
		}
	}
	for _, r := range b.Revisions {
		if _, err := tx.Exec(ctx, `INSERT INTO library_revisions(id,owner_id,resource_id,kind,action,snapshot,recorded_at) VALUES($1,$2,$3,$4,$5,$6,$7)`, mapping[r.ID], owner, mapping[r.ResourceID], r.Kind, r.Action, mapLibrarySnapshot(r.Snapshot, mapping), r.RecordedAt); err != nil {
			return err
		}
	}
	return restoreAchievements(ctx, tx, owner, b.Achievements)
}
