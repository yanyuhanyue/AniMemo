package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"github.com/jackc/pgx/v5"
	"strings"
	"time"
)

type CharacterInput struct {
	Version     int      `json:"version"`
	Name        string   `json:"name"`
	Aliases     []string `json:"aliases"`
	Description string   `json:"description"`
	AnimeIDs    []string `json:"anime_ids"`
	Favorite    bool     `json:"favorite"`
	Visibility  string   `json:"visibility"`
}
type Character struct {
	CharacterInput
	ID         string    `json:"id"`
	RedirectID string    `json:"redirect_id"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

const characterColumns = `id,name,aliases,description,anime_ids,favorite,visibility,coalesce(redirect_id::text,''),version,created_at,updated_at`

func scanCharacter(row pgx.Row) (Character, error) {
	var c Character
	err := row.Scan(&c.ID, &c.Name, &c.Aliases, &c.Description, &c.AnimeIDs, &c.Favorite, &c.Visibility, &c.RedirectID, &c.Version, &c.CreatedAt, &c.UpdatedAt)
	return c, libraryMissing(err)
}
func (s *Service) Characters(ctx context.Context, owner string, f LibraryFilter) (LibraryPage[Character], error) {
	out := LibraryPage[Character]{Items: []Character{}, Page: libraryPage(f.Page), PageSize: 30}
	if f.AnimeID != "" && !id.Valid(f.AnimeID) {
		return out, fault.Field("anime_id", "作品筛选无效。")
	}
	where := ` FROM characters WHERE owner_id=$1 AND ($2='' OR name ILIKE $3 OR array_to_string(aliases,' ') ILIKE $3) AND ($4='' OR nullif($4,'')::uuid=ANY(anime_ids)) AND (NOT $5 OR favorite)`
	args := []any{owner, f.Search, "%" + f.Search + "%", f.AnimeID, f.Highlight}
	if err := s.pool.QueryRow(ctx, `SELECT count(*)`+where, args...).Scan(&out.Total); err != nil {
		return out, err
	}
	args = append(args, (out.Page-1)*30)
	rows, err := s.pool.Query(ctx, `SELECT `+characterColumns+where+` ORDER BY favorite DESC,updated_at DESC,id DESC LIMIT 30 OFFSET $6`, args...)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		c, err := scanCharacter(rows)
		if err != nil {
			return out, err
		}
		out.Items = append(out.Items, c)
	}
	return out, rows.Err()
}
func (s *Service) Character(ctx context.Context, owner, resource string) (Character, error) {
	if !id.Valid(resource) {
		return Character{}, fault.New("not_found", "角色不存在。")
	}
	return scanCharacter(s.pool.QueryRow(ctx, `SELECT `+characterColumns+` FROM characters WHERE owner_id=$1 AND id=$2`, owner, resource))
}
func (s *Service) SaveCharacter(ctx context.Context, owner, resource string, in CharacterInput) (Character, error) {
	in.Name = strings.TrimSpace(in.Name)
	if in.Visibility == "" {
		in.Visibility = "private"
	}
	if in.Aliases == nil {
		in.Aliases = []string{}
	}
	if in.AnimeIDs == nil {
		in.AnimeIDs = []string{}
	}
	if in.Name == "" || !libraryText(in.Name, 160) || !libraryText(in.Description, 10000) || !libraryTags(in.Aliases, 30) || !libraryIDs(in.AnimeIDs, 100) || !libraryVisibility(in.Visibility) {
		return Character{}, fault.Field("name", "请检查角色名称、别名、作品与可见性。")
	}
	if resource != "" && !id.Valid(resource) {
		return Character{}, fault.New("not_found", "角色不存在。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return Character{}, err
	}
	defer tx.Rollback(context.Background())
	action := "created"
	if resource != "" {
		old, err := scanCharacter(tx.QueryRow(ctx, `SELECT `+characterColumns+` FROM characters WHERE owner_id=$1 AND id=$2 FOR UPDATE`, owner, resource))
		if err != nil {
			return old, err
		}
		if err = libraryVersion(old.Version, in.Version); err != nil {
			return old, err
		}
		action = "changed"
	} else {
		resource = id.New()
		var count int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM characters WHERE owner_id=$1`, owner).Scan(&count); err != nil {
			return Character{}, err
		}
		if count >= 5000 {
			return Character{}, fault.Field("name", "角色已达 5000 个上限。")
		}
	}
	var count int
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM anime_resources WHERE owner_id=$1 AND id=ANY($2::uuid[])`, owner, in.AnimeIDs).Scan(&count); err != nil {
		return Character{}, err
	}
	if count != len(in.AnimeIDs) {
		return Character{}, fault.Field("anime_ids", "关联作品不属于当前账号。")
	}
	c, err := scanCharacter(tx.QueryRow(ctx, `INSERT INTO characters(id,owner_id,name,aliases,description,anime_ids,favorite,visibility) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(id) DO UPDATE SET name=excluded.name,aliases=excluded.aliases,description=excluded.description,anime_ids=excluded.anime_ids,favorite=excluded.favorite,visibility=excluded.visibility,version=characters.version+1,updated_at=now() RETURNING `+characterColumns, resource, owner, in.Name, in.Aliases, in.Description, in.AnimeIDs, in.Favorite, in.Visibility))
	if err != nil {
		return c, err
	}
	if err = libraryRevision(ctx, tx, owner, c.ID, "character", action, c); err != nil {
		return c, err
	}
	return c, tx.Commit(ctx)
}
