package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"github.com/jackc/pgx/v5"
	"strings"
	"time"
)

type MemoryShareInput struct {
	Kind       string `json:"kind"`
	ResourceID string `json:"resource_id"`
	Days       int    `json:"days"`
	Revoke     bool   `json:"revoke"`
}
type MemoryShareResult struct {
	Token     string     `json:"token"`
	ExpiresAt *time.Time `json:"expires_at"`
}
type SharedMemory struct {
	Kind         string       `json:"kind"`
	Title        string       `json:"title"`
	Introduction string       `json:"introduction"`
	Items        []YearlyItem `json:"items"`
	Revision     int          `json:"revision"`
	Year         int          `json:"year"`
}

func shareTable(kind string) string {
	switch kind {
	case "note":
		return "memory_notes"
	case "character":
		return "characters"
	case "collection":
		return "memory_collections"
	case "yearly":
		return "yearly_memories"
	}
	return ""
}
func (s *Service) MemoryShare(ctx context.Context, owner string, in MemoryShareInput) (MemoryShareResult, error) {
	out := MemoryShareResult{}
	table := shareTable(in.Kind)
	if table == "" || !id.Valid(in.ResourceID) || (!in.Revoke && (in.Days < 1 || in.Days > 90)) {
		return out, fault.Field("days", "分享有效期为 1–90 天，且需选择有效资源。")
	}
	tx, err := s.libraryTx(ctx, owner)
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	var visibility string
	extra := ""
	if in.Kind == "note" {
		extra = " AND deleted_at IS NULL"
	}
	if err = tx.QueryRow(ctx, `SELECT visibility FROM `+table+` WHERE owner_id=$1 AND id=$2`+extra+` FOR SHARE`, owner, in.ResourceID).Scan(&visibility); err != nil {
		return out, libraryMissing(err)
	}
	if _, err = tx.Exec(ctx, `DELETE FROM memory_share_tokens WHERE owner_id=$1 AND resource_id=$2 AND kind=$3`, owner, in.ResourceID, in.Kind); err != nil {
		return out, err
	}
	if !in.Revoke {
		var allowed bool
		if err = tx.QueryRow(ctx, `SELECT sharing_enabled AND NOT disabled FROM users WHERE id=$1`, owner).Scan(&allowed); err != nil {
			return out, err
		}
		if !allowed {
			return out, fault.Field("resource_id", "请先在账号与偏好中开启链接分享。")
		}
		if visibility == "private" {
			return out, fault.Field("resource_id", "私人资源不能分享，请明确选择仅链接可见或公开后再生成链接。")
		}
		token := make([]byte, 32)
		if _, err = rand.Read(token); err != nil {
			return out, err
		}
		out.Token = hex.EncodeToString(token)
		sum := sha256.Sum256([]byte(out.Token))
		expires := time.Now().UTC().Add(time.Duration(in.Days) * 24 * time.Hour)
		out.ExpiresAt = &expires
		if _, err = tx.Exec(ctx, `INSERT INTO memory_share_tokens(token_hash,owner_id,resource_id,kind,expires_at) VALUES($1,$2,$3,$4,$5)`, hex.EncodeToString(sum[:]), owner, in.ResourceID, in.Kind, expires); err != nil {
			return out, err
		}
	}
	return out, tx.Commit(ctx)
}
func (s *Service) sharedIdentity(ctx context.Context, token, kind, resource string) (string, string, string, error) {
	notFound := fault.New("not_found", "分享不存在、已撤回或已过期。")
	if token != "" {
		if len(token) != 64 {
			return "", "", "", notFound
		}
		sum := sha256.Sum256([]byte(token))
		var owner string
		err := s.pool.QueryRow(ctx, `SELECT t.owner_id,t.kind,t.resource_id FROM memory_share_tokens t JOIN users u ON u.id=t.owner_id WHERE token_hash=$1 AND expires_at>now() AND u.sharing_enabled AND NOT u.disabled`, hex.EncodeToString(sum[:])).Scan(&owner, &kind, &resource)
		if err != nil {
			return "", "", "", libraryMissing(err)
		}
		return owner, kind, resource, nil
	}
	table := shareTable(kind)
	if table == "" || !id.Valid(resource) {
		return "", "", "", notFound
	}
	var owner string
	err := s.pool.QueryRow(ctx, `SELECT owner_id FROM `+table+` t JOIN users u ON u.id=t.owner_id WHERE t.id=$1 AND t.visibility='public' AND u.sharing_enabled AND u.public_state='published' AND NOT u.disabled`, resource).Scan(&owner)
	return owner, kind, resource, libraryMissing(err)
}
func (s *Service) SharedMemory(ctx context.Context, token, kind, resource string) (SharedMemory, error) {
	owner, kind, resource, err := s.sharedIdentity(ctx, token, kind, resource)
	if err != nil {
		return SharedMemory{}, err
	}
	return s.projectSharedMemory(ctx, owner, kind, resource, token == "")
}
func (s *Service) projectSharedMemory(ctx context.Context, owner, kind, resource string, publicOnly bool) (SharedMemory, error) {
	out := SharedMemory{Kind: kind, Items: []YearlyItem{}}
	table := shareTable(kind)
	if table == "" {
		return out, fault.New("not_found", "分享不存在。")
	}
	var visibility string
	if err := s.pool.QueryRow(ctx, `SELECT visibility FROM `+table+` WHERE owner_id=$1 AND id=$2`, owner, resource).Scan(&visibility); err != nil {
		return out, libraryMissing(err)
	}
	if visibility == "private" || (publicOnly && visibility != "public") {
		return out, fault.New("not_found", "这份记忆已转为私人内容。")
	}
	switch kind {
	case "note":
		n, err := s.Note(ctx, owner, resource)
		if err != nil {
			return out, err
		}
		out.Title = n.Title
		out.Items = []YearlyItem{{SourceVisibility: n.Visibility, SourceVersion: n.Version, NoteID: n.ID, Kind: n.Kind, Title: n.Title, Body: n.Body, OccurredOn: n.OccurredOn, TimePrecision: n.TimePrecision, Spoiler: n.Spoiler, MediaIDs: n.MediaIDs}}
	case "character":
		c, err := s.Character(ctx, owner, resource)
		if err != nil {
			return out, err
		}
		out.Title = c.Name
		out.Introduction = c.Description
	case "yearly":
		y, err := s.Yearly(ctx, owner, resource)
		if err != nil {
			return out, err
		}
		if len(y.Revisions) == 0 {
			return out, fault.New("not_found", "年度记忆没有已保存的修订。")
		}
		r := y.Revisions[0]
		out.Title = r.Title
		out.Introduction = r.Introduction
		out.Revision = r.Revision
		out.Year = y.Year
		// A frozen selection is not a permanent public copy of a private source.
		for _, item := range r.Items {
			n, err := s.Note(ctx, owner, item.NoteID)
			if err != nil {
				if _, ok := err.(*fault.Error); !ok {
					return out, err
				}
				continue
			}
			if !libraryVisibility(item.SourceVisibility) || item.SourceVisibility == "private" || n.Visibility == "private" || (publicOnly && (n.Visibility != "public" || item.SourceVisibility != "public")) {
				continue
			}
			item.Spoiler = item.Spoiler || n.Spoiler
			out.Items = append(out.Items, item)
		}
	case "collection":
		c, err := scanCollection(s.pool.QueryRow(ctx, `SELECT `+collectionColumns+` FROM memory_collections WHERE owner_id=$1 AND id=$2`, owner, resource))
		if err != nil {
			return out, err
		}
		out.Title = c.Title
		out.Introduction = c.Description
		for _, item := range c.Items {
			if item.Kind == "note" || item.Kind == "moment" {
				n, err := s.Note(ctx, owner, item.ID)
				if err != nil {
					if _, ok := err.(*fault.Error); !ok {
						return out, err
					}
					continue
				}
				if n.Visibility == "private" || (publicOnly && n.Visibility != "public") {
					continue
				}
				out.Items = append(out.Items, YearlyItem{SourceVisibility: n.Visibility, SourceVersion: n.Version, NoteID: n.ID, Kind: n.Kind, Title: n.Title, Body: n.Body, OccurredOn: n.OccurredOn, TimePrecision: n.TimePrecision, Spoiler: n.Spoiler, MediaIDs: n.MediaIDs})
			} else if item.Kind == "character" {
				c, err := s.Character(ctx, owner, item.ID)
				if err != nil {
					return out, err
				}
				if c.Visibility == "private" || (publicOnly && c.Visibility != "public") {
					continue
				}
				out.Items = append(out.Items, YearlyItem{SourceVisibility: c.Visibility, SourceVersion: c.Version, Kind: "character", Title: c.Name, Body: c.Description, MediaIDs: []string{}})
			} else {
				var e Entry
				err := s.pool.QueryRow(ctx, `SELECT title,notes FROM entries WHERE user_id=$1 AND anime_id=$2 AND visibility='public' AND NOT moderated_hidden AND deleted_at IS NULL LIMIT 1`, owner, item.ID).Scan(&e.Title, &e.Notes)
				if err == pgx.ErrNoRows {
					continue
				}
				if err != nil {
					return out, err
				}
				out.Items = append(out.Items, YearlyItem{SourceVisibility: "public", Kind: "anime", Title: e.Title, Body: e.Notes, MediaIDs: []string{}})
			}
		}
	}
	for i := range out.Items {
		item := &out.Items[i]
		kept := []string{}
		for _, m := range item.MediaIDs {
			var ready bool
			if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM private_memory_media WHERE owner_id=$1 AND id=$2 AND state='ready')`, owner, m).Scan(&ready); err != nil {
				return out, err
			}
			if ready {
				kept = append(kept, m)
			} else {
				item.Unavailable = "SOURCE_MEDIA_DELETED"
			}
		}
		item.MediaIDs = kept
	}
	return out, nil
}
func (s *Service) SharedMemoryMedia(ctx context.Context, token, kind, resource, mediaID string) (media.Image, error) {
	var none media.Image
	if !id.Valid(mediaID) {
		return none, fault.New("not_found", "图片不存在。")
	}
	owner, kind, resource, err := s.sharedIdentity(ctx, token, kind, resource)
	if err != nil {
		return none, err
	}
	projection, err := s.projectSharedMemory(ctx, owner, kind, resource, token == "")
	if err != nil {
		return none, err
	}
	for _, item := range projection.Items {
		for _, m := range item.MediaIDs {
			if m == mediaID {
				return s.ReadMemoryMedia(ctx, owner, mediaID, true)
			}
		}
	}
	return none, fault.New("not_found", "图片不在当前可见的分享中。")
}

// Public consumers never infer an owner from administrative ordering.
func (s *Service) SiteHomepage(ctx context.Context) (PublicPage, error) {
	var slug string
	err := s.pool.QueryRow(ctx, `SELECT coalesce((SELECT u.public_slug::text FROM users u WHERE u.id=homepage_owner AND u.sharing_enabled AND u.public_state='published' AND NOT u.disabled),'') FROM site_settings`).Scan(&slug)
	if err != nil {
		return PublicPage{}, err
	}
	if strings.TrimSpace(slug) == "" {
		return PublicPage{Items: []PublicItem{}, Page: 1, PageSize: 12}, nil
	}
	return s.PublicEntries(ctx, slug, Filter{Page: 1, PageSize: 12})
}
