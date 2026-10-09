package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"strings"
	"time"
)

type MemorySearchFilter struct {
	LibraryFilter
	Status string
}
type MemorySearchItem struct {
	ID            string    `json:"id"`
	Kind          string    `json:"kind"`
	Title         string    `json:"title"`
	Excerpt       string    `json:"excerpt"`
	AnimeID       string    `json:"anime_id"`
	CharacterID   string    `json:"character_id"`
	OccurredOn    string    `json:"occurred_on"`
	TimePrecision string    `json:"time_precision"`
	Status        string    `json:"status"`
	Spoiler       bool      `json:"spoiler"`
	UpdatedAt     time.Time `json:"updated_at"`
}

// This read projection never stores a second copy of memory content.
const memorySearchSource = `WITH RECURSIVE character_scope AS (
 SELECT id FROM characters WHERE owner_id=$1 AND id=nullif($7,'')::uuid
 UNION SELECT c.id FROM characters c JOIN character_scope s ON c.redirect_id=s.id WHERE c.owner_id=$1
), documents AS (
 SELECT n.id,n.kind,n.title,n.body AS excerpt,coalesce(n.anime_id::text,'') AS anime_id,coalesce(n.character_id::text,'') AS character_id,n.occurred_on,n.time_precision,coalesce(e.status,'') AS status,n.spoiler,n.updated_at,array_to_string(n.tags,' ') AS tags FROM memory_notes n LEFT JOIN entries e ON e.anime_id=n.anime_id AND e.user_id=n.owner_id AND e.deleted_at IS NULL WHERE n.owner_id=$1 AND n.deleted_at IS NULL
 UNION ALL SELECT c.id,'character',c.name,c.description,coalesce(c.anime_ids[1]::text,''),c.id::text,'','unknown','',false,c.updated_at,array_to_string(c.aliases,' ') FROM characters c WHERE c.owner_id=$1
 UNION ALL SELECT a.id,'anime',a.title,coalesce(e.notes,''),a.id::text,'','','unknown',coalesce(e.status,''),false,a.created_at,'' FROM anime_resources a LEFT JOIN entries e ON e.anime_id=a.id AND e.user_id=a.owner_id AND e.deleted_at IS NULL WHERE a.owner_id=$1
 UNION ALL SELECT c.id,'collection',c.title,c.description,'','','','unknown','',false,c.updated_at,'' FROM memory_collections c WHERE c.owner_id=$1
 UNION ALL SELECT y.id,'yearly',y.title,y.introduction,'','',y.year::text,'year','',false,y.updated_at,'' FROM yearly_memories y WHERE y.owner_id=$1
 UNION ALL SELECT w.id,'watch',e.title,concat('第 ',w.episode_from,'–',w.episode_to,' 话 · 第 ',w.rewatch,' 刷',CASE WHEN w.note='' THEN '' ELSE ' · '||w.note END),e.anime_id::text,'',coalesce(w.watched_on::text,''),w.time_precision,e.status,false,w.created_at,'' FROM watch_records w JOIN entries e ON e.id=w.entry_id WHERE e.user_id=$1 AND e.deleted_at IS NULL
 UNION ALL SELECT p.id,'progress',a.title,p.note,p.anime_id::text,'','','unknown',coalesce(e.status,''),false,p.created_at,'' FROM progress_assertions p JOIN anime_resources a ON a.id=p.anime_id LEFT JOIN entries e ON e.anime_id=a.id AND e.user_id=p.owner_id AND e.deleted_at IS NULL WHERE p.owner_id=$1
), matched AS (SELECT * FROM documents WHERE ($2='' OR title ILIKE $3 OR excerpt ILIKE $3 OR tags ILIKE $3) AND ($4='' OR kind=$4) AND ($5='' OR left(occurred_on,4)=$5) AND ($6='' OR anime_id=$6 OR (kind='character' AND EXISTS(SELECT 1 FROM characters c WHERE c.owner_id=$1 AND c.id=documents.id AND nullif($6,'')::uuid=ANY(c.anime_ids)))) AND ($7='' OR character_id IN(SELECT id::text FROM character_scope)) AND ($8='' OR status=$8)) `

func (s *Service) SearchMemory(ctx context.Context, owner string, f MemorySearchFilter) (LibraryPage[MemorySearchItem], error) {
	out := LibraryPage[MemorySearchItem]{Items: []MemorySearchItem{}, Page: libraryPage(f.Page), PageSize: 30}
	f.Search = strings.TrimSpace(f.Search)
	if !libraryText(f.Search, 160) || (f.AnimeID != "" && !id.Valid(f.AnimeID)) || (f.CharacterID != "" && !id.Valid(f.CharacterID)) {
		return out, fault.Field("search", "搜索条件无效。")
	}
	if f.Year != "" && !validMemoryTime(f.Year, "year") {
		return out, fault.Field("year", "年份无效。")
	}
	args := []any{owner, f.Search, "%" + f.Search + "%", f.Kind, f.Year, f.AnimeID, f.CharacterID, f.Status}
	if err := s.pool.QueryRow(ctx, memorySearchSource+`SELECT count(*) FROM matched`, args...).Scan(&out.Total); err != nil {
		return out, err
	}
	args = append(args, (out.Page-1)*out.PageSize)
	rows, err := s.pool.Query(ctx, memorySearchSource+`SELECT id,kind,title,left(excerpt,400),anime_id,character_id,occurred_on,time_precision,status,spoiler,updated_at FROM matched ORDER BY occurred_on DESC,updated_at DESC,id LIMIT 30 OFFSET $9`, args...)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var v MemorySearchItem
		if err = rows.Scan(&v.ID, &v.Kind, &v.Title, &v.Excerpt, &v.AnimeID, &v.CharacterID, &v.OccurredOn, &v.TimePrecision, &v.Status, &v.Spoiler, &v.UpdatedAt); err != nil {
			return out, err
		}
		out.Items = append(out.Items, v)
	}
	return out, rows.Err()
}

type MemoryReference struct {
	CollectionItem
	Title         string `json:"title"`
	Available     bool   `json:"available"`
	EntryID       string `json:"entry_id,omitempty"`
	CoverRevision string `json:"cover_revision,omitempty"`
}

func (s *Service) MemoryReferences(ctx context.Context, owner string, items []CollectionItem) ([]MemoryReference, error) {
	if len(items) > 500 {
		return nil, fault.Field("items", "最多查询 500 个引用。")
	}
	ids := []string{}
	for _, v := range items {
		if !id.Valid(v.ID) {
			return nil, fault.Field("items", "引用格式无效。")
		}
		ids = append(ids, v.ID)
	}
	rows, err := s.pool.Query(ctx, `SELECT a.id,a.title,'anime',coalesce(e.id::text,''),coalesce(c.revision::text,'')
 FROM anime_resources a
 LEFT JOIN entries e ON e.anime_id=a.id AND e.user_id=a.owner_id AND e.deleted_at IS NULL
 LEFT JOIN entry_covers c ON c.entry_id=e.id
 WHERE a.owner_id=$1 AND a.id=ANY($2::uuid[])
 UNION ALL SELECT id,name,'character','','' FROM characters WHERE owner_id=$1 AND id=ANY($2::uuid[])
 UNION ALL SELECT id,title,kind,'','' FROM memory_notes WHERE owner_id=$1 AND id=ANY($2::uuid[]) AND deleted_at IS NULL`, owner, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	references := map[string]MemoryReference{}
	for rows.Next() {
		var ref MemoryReference
		if err = rows.Scan(&ref.ID, &ref.Title, &ref.Kind, &ref.EntryID, &ref.CoverRevision); err != nil {
			return nil, err
		}
		references[ref.Kind+":"+ref.ID] = ref
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	out := []MemoryReference{}
	for _, v := range items {
		ref, ok := references[v.Kind+":"+v.ID]
		ref.CollectionItem, ref.Available = v, ok
		out = append(out, ref)
	}
	return out, nil
}
