package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"github.com/jackc/pgx/v5"
	"strings"
	"time"
)

type EpisodeIdentity struct {
	Provider   string `json:"provider"`
	ExternalID string `json:"external_id"`
}
type EpisodeInput struct {
	Version      int               `json:"version"`
	AnimeID      string            `json:"anime_id"`
	Title        string            `json:"title"`
	Number       int               `json:"number"`
	Kind         string            `json:"kind"`
	ProgressRole string            `json:"progress_role"`
	Identities   []EpisodeIdentity `json:"identities"`
}
type Episode struct {
	EpisodeInput
	ID         string    `json:"id"`
	RedirectID string    `json:"redirect_id"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

const episodeColumns = `id,anime_id,title,number,kind,progress_role,identities,coalesce(redirect_id::text,''),version,created_at,updated_at`

func scanEpisode(row pgx.Row) (Episode, error) {
	var e Episode
	err := row.Scan(&e.ID, &e.AnimeID, &e.Title, &e.Number, &e.Kind, &e.ProgressRole, &e.Identities, &e.RedirectID, &e.Version, &e.CreatedAt, &e.UpdatedAt)
	return e, libraryMissing(err)
}
func (s *Service) Episodes(ctx context.Context, owner string, f LibraryFilter) (LibraryPage[Episode], error) {
	out := LibraryPage[Episode]{Items: []Episode{}, Page: libraryPage(f.Page), PageSize: 50}
	if f.AnimeID != "" && !id.Valid(f.AnimeID) {
		return out, fault.Field("anime_id", "作品筛选无效。")
	}
	where := ` FROM episodes WHERE owner_id=$1 AND ($2='' OR anime_id=nullif($2,'')::uuid) AND ($3='' OR title ILIKE $4)`
	args := []any{owner, f.AnimeID, f.Search, "%" + f.Search + "%"}
	if err := s.pool.QueryRow(ctx, `SELECT count(*)`+where, args...).Scan(&out.Total); err != nil {
		return out, err
	}
	args = append(args, (out.Page-1)*50)
	rows, err := s.pool.Query(ctx, `SELECT `+episodeColumns+where+` ORDER BY number,id LIMIT 50 OFFSET $5`, args...)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		e, err := scanEpisode(rows)
		if err != nil {
			return out, err
		}
		out.Items = append(out.Items, e)
	}
	return out, rows.Err()
}
func (s *Service) SaveEpisode(ctx context.Context, owner, resource string, in EpisodeInput) (Episode, error) {
	in.Title = strings.TrimSpace(in.Title)
	if in.Kind == "" {
		in.Kind = "main"
	}
	if in.ProgressRole == "" {
		in.ProgressRole = "required"
	}
	if in.Identities == nil {
		in.Identities = []EpisodeIdentity{}
	}
	if !id.Valid(in.AnimeID) || in.Title == "" || !libraryText(in.Title, 160) || in.Number < 0 || in.Number > 100000 || (in.Kind != "main" && in.Kind != "special" && in.Kind != "ova" && in.Kind != "movie") || (in.ProgressRole != "required" && in.ProgressRole != "optional" && in.ProgressRole != "excluded") || len(in.Identities) > 10 {
		return Episode{}, fault.Field("title", "请检查集数名称、编号、类型与观看角色。")
	}
	seen := map[string]bool{}
	for _, v := range in.Identities {
		key := v.Provider + ":" + v.ExternalID
		if v.Provider == "" || v.ExternalID == "" || !libraryText(v.Provider, 40) || !libraryText(v.ExternalID, 160) || seen[key] {
			return Episode{}, fault.Field("identities", "外部集数身份无效或重复。")
		}
		seen[key] = true
	}
	if resource != "" && !id.Valid(resource) {
		return Episode{}, fault.New("not_found", "集数不存在。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return Episode{}, err
	}
	defer tx.Rollback(context.Background())
	var exists bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM anime_resources WHERE id=$2 AND owner_id=$1)`, owner, in.AnimeID).Scan(&exists); err != nil {
		return Episode{}, err
	}
	if !exists {
		return Episode{}, fault.Field("anime_id", "作品不存在。")
	}
	action := "created"
	if resource != "" {
		old, err := scanEpisode(tx.QueryRow(ctx, `SELECT `+episodeColumns+` FROM episodes WHERE owner_id=$1 AND id=$2 FOR UPDATE`, owner, resource))
		if err != nil {
			return old, err
		}
		if err = libraryVersion(old.Version, in.Version); err != nil {
			return old, err
		}
		if old.AnimeID != in.AnimeID {
			return old, fault.Field("anime_id", "已有集数不能静默迁到另一作品；请另建并修复关联。")
		}
		action = "changed"
	} else {
		resource = id.New()
		var count int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM episodes WHERE owner_id=$1`, owner).Scan(&count); err != nil {
			return Episode{}, err
		}
		if count >= 50000 {
			return Episode{}, fault.Field("title", "集数目录达到 50000 个上限。")
		}
	}
	e, err := scanEpisode(tx.QueryRow(ctx, `INSERT INTO episodes(id,owner_id,anime_id,title,number,kind,progress_role,identities) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(id) DO UPDATE SET title=excluded.title,number=excluded.number,kind=excluded.kind,progress_role=excluded.progress_role,identities=excluded.identities,version=episodes.version+1,updated_at=now() RETURNING `+episodeColumns, resource, owner, in.AnimeID, in.Title, in.Number, in.Kind, in.ProgressRole, in.Identities))
	if err != nil {
		return e, err
	}
	if err = libraryRevision(ctx, tx, owner, e.ID, "episode", action, e); err != nil {
		return e, err
	}
	return e, tx.Commit(ctx)
}

type AnimeRelation struct {
	FromID   string `json:"from_id"`
	ToID     string `json:"to_id"`
	Relation string `json:"relation"`
}

func (s *Service) Relations(ctx context.Context, owner string) ([]AnimeRelation, error) {
	rows, err := s.pool.Query(ctx, `SELECT from_id,to_id,relation FROM anime_relations WHERE owner_id=$1 ORDER BY from_id,to_id,relation`, owner)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []AnimeRelation{}
	for rows.Next() {
		var v AnimeRelation
		if err = rows.Scan(&v.FromID, &v.ToID, &v.Relation); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (s *Service) SetRelation(ctx context.Context, owner string, in AnimeRelation, remove bool) error {
	if !id.Valid(in.FromID) || !id.Valid(in.ToID) || in.FromID == in.ToID || (in.Relation != "sequel" && in.Relation != "prequel" && in.Relation != "side_story" && in.Relation != "same_franchise") {
		return fault.Field("relation", "作品关系无效。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var count int
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM anime_resources WHERE owner_id=$1 AND id=ANY($2::uuid[])`, owner, []string{in.FromID, in.ToID}).Scan(&count); err != nil {
		return err
	}
	if count != 2 {
		return fault.Field("from_id", "作品不存在。")
	}
	if remove {
		_, err = tx.Exec(ctx, `DELETE FROM anime_relations WHERE owner_id=$1 AND from_id=$2 AND to_id=$3 AND relation=$4`, owner, in.FromID, in.ToID, in.Relation)
	} else {
		_, err = tx.Exec(ctx, `INSERT INTO anime_relations(owner_id,from_id,to_id,relation) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, owner, in.FromID, in.ToID, in.Relation)
	}
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

type ProgressAssertionInput struct {
	AnimeID   string        `json:"anime_id"`
	Scope     string        `json:"scope"`
	Precision string        `json:"precision"`
	Episodes  []VersionedID `json:"episodes"`
	Note      string        `json:"note"`
}
type ProgressAssertion struct {
	ID         string    `json:"id"`
	AnimeID    string    `json:"anime_id"`
	Scope      string    `json:"scope"`
	Precision  string    `json:"precision"`
	EpisodeIDs []string  `json:"episode_ids"`
	Note       string    `json:"note"`
	CreatedAt  time.Time `json:"created_at"`
}

func (s *Service) AssertProgress(ctx context.Context, owner string, in ProgressAssertionInput) (ProgressAssertion, error) {
	var out ProgressAssertion
	if !id.Valid(in.AnimeID) || (in.Scope != "mainline" && in.Scope != "all" && in.Scope != "custom") || (in.Precision != "exact" && in.Precision != "approximate" && in.Precision != "caught_up") || len(in.Episodes) == 0 || len(in.Episodes) > 2000 || !libraryText(in.Note, 4000) {
		return out, fault.Field("episodes", "请选择 1–2000 个集数及有效的范围/精度。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	seen := map[string]bool{}
	out.EpisodeIDs = []string{}
	// The UI sends the exact selected set and versions, never a mutable filter.
	for _, v := range in.Episodes {
		if !id.Valid(v.ID) || seen[v.ID] {
			return out, fault.Field("episodes", "选择包含无效或重复集数。")
		}
		seen[v.ID] = true
		e, err := scanEpisode(tx.QueryRow(ctx, `SELECT `+episodeColumns+` FROM episodes WHERE owner_id=$1 AND id=$2 AND anime_id=$3 FOR SHARE`, owner, v.ID, in.AnimeID))
		if err != nil {
			return out, err
		}
		if err = libraryVersion(e.Version, v.Version); err != nil {
			return out, err
		}
		if e.RedirectID != "" || e.ProgressRole == "excluded" || (in.Scope == "mainline" && e.Kind != "main") {
			return out, fault.Field("episodes", "归并/排除的集数或范围外集数不能进入本次进度快照。")
		}
		out.EpisodeIDs = append(out.EpisodeIDs, v.ID)
	}
	out.ID = id.New()
	out.AnimeID = in.AnimeID
	out.Scope = in.Scope
	out.Precision = in.Precision
	out.Note = in.Note
	if err = tx.QueryRow(ctx, `INSERT INTO progress_assertions(id,owner_id,anime_id,scope,precision,episode_ids,note) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING created_at`, out.ID, owner, in.AnimeID, in.Scope, in.Precision, out.EpisodeIDs, in.Note).Scan(&out.CreatedAt); err != nil {
		return out, err
	}
	if err = libraryRevision(ctx, tx, owner, in.AnimeID, "progress", "asserted", out); err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
func (s *Service) ProgressAssertions(ctx context.Context, owner, anime string) ([]ProgressAssertion, error) {
	if !id.Valid(anime) {
		return nil, fault.Field("anime_id", "作品无效。")
	}
	rows, err := s.pool.Query(ctx, `SELECT id,anime_id,scope,precision,episode_ids,note,created_at FROM progress_assertions WHERE owner_id=$1 AND anime_id=$2 ORDER BY created_at DESC,id DESC LIMIT 100`, owner, anime)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ProgressAssertion{}
	for rows.Next() {
		var p ProgressAssertion
		if err = rows.Scan(&p.ID, &p.AnimeID, &p.Scope, &p.Precision, &p.EpisodeIDs, &p.Note, &p.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// The numeric interval is an explicit creation target, never a viewing claim.
type EpisodeBatchInput struct {
	AnimeID      string `json:"anime_id"`
	From         int    `json:"from"`
	To           int    `json:"to"`
	Kind         string `json:"kind"`
	ProgressRole string `json:"progress_role"`
}

func (s *Service) CreateEpisodeBatch(ctx context.Context, owner string, in EpisodeBatchInput) ([]Episode, error) {
	if !id.Valid(in.AnimeID) || in.From < 1 || in.To < in.From || in.To > 100000 || in.To-in.From >= 2000 || (in.Kind != "main" && in.Kind != "special" && in.Kind != "ova" && in.Kind != "movie") || (in.ProgressRole != "required" && in.ProgressRole != "optional" && in.ProgressRole != "excluded") {
		return nil, fault.Field("from", "请选择有效类型与范围，一次最多 2000 话。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(context.Background())
	var exists bool
	var total int
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM anime_resources WHERE owner_id=$1 AND id=$2),(SELECT count(*) FROM episodes WHERE owner_id=$1)`, owner, in.AnimeID).Scan(&exists, &total); err != nil {
		return nil, err
	}
	if !exists {
		return nil, fault.Field("anime_id", "作品不存在。")
	}
	if total+in.To-in.From+1 > 50000 {
		return nil, fault.Field("from", "集数目录达到 50000 个上限。")
	}
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM episodes WHERE owner_id=$1 AND anime_id=$2 AND kind=$3 AND number BETWEEN $4 AND $5)`, owner, in.AnimeID, in.Kind, in.From, in.To).Scan(&exists); err != nil {
		return nil, err
	}
	if exists {
		return nil, fault.Field("from", "范围中已有同类型集数，请缩小范围；没有写入任何新集数。")
	}
	rows, err := tx.Query(ctx, `INSERT INTO episodes(id,owner_id,anime_id,title,number,kind,progress_role) SELECT gen_random_uuid(),$1,$2,'第 '||n||' 话',n,$3,$4 FROM generate_series($5::integer,$6::integer) n RETURNING `+episodeColumns, owner, in.AnimeID, in.Kind, in.ProgressRole, in.From, in.To)
	if err != nil {
		return nil, err
	}
	out := []Episode{}
	for rows.Next() {
		e, err := scanEpisode(rows)
		if err != nil {
			rows.Close()
			return nil, err
		}
		out = append(out, e)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	for _, e := range out {
		if err = libraryRevision(ctx, tx, owner, e.ID, "episode", "created", e); err != nil {
			return nil, err
		}
	}
	return out, tx.Commit(ctx)
}
