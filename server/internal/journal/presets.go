package journal

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"context"
	"strings"
	"unicode/utf8"
)

type Preset struct {
	Name    string `json:"name"`
	Color   string `json:"color"`
	Version int    `json:"version"`
}

func (s *Service) Presets(ctx context.Context) ([]Preset, error) {
	rows, err := s.pool.Query(ctx, `SELECT name,color,version FROM tag_presets ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Preset{}
	for rows.Next() {
		var p Preset
		if err = rows.Scan(&p.Name, &p.Color, &p.Version); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}
func (s *Service) SavePreset(ctx context.Context, actor string, p Preset, remove bool) error {
	p.Name = strings.TrimSpace(p.Name)
	if utf8.RuneCountInString(p.Name) < 1 || utf8.RuneCountInString(p.Name) > 24 || strings.ContainsRune(p.Name, 0) || (!remove && !hexColor.MatchString(p.Color)) {
		return fault.New("validation_error", "标签名需要 1–24 字，请选择有效颜色。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return err
	}
	if remove {
		tag, err := tx.Exec(ctx, `DELETE FROM tag_presets WHERE name=$1 AND version=$2`, p.Name, p.Version)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return fault.New("version_conflict", "预设已更新，请刷新。")
		}
	} else if p.Version == 0 {
		var count int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM tag_presets`).Scan(&count); err != nil {
			return err
		}
		if count >= 100 {
			return fault.New("validation_error", "最多设置 100 个标签预设。")
		}
		tag, err := tx.Exec(ctx, `INSERT INTO tag_presets(name,color) VALUES($1,$2) ON CONFLICT(name) DO NOTHING`, p.Name, p.Color)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return fault.New("version_conflict", "已存在同名预设，请刷新。")
		}
	} else {
		tag, err := tx.Exec(ctx, `UPDATE tag_presets SET color=$2,version=version+1 WHERE name=$1 AND version=$3`, p.Name, p.Color, p.Version)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return fault.New("version_conflict", "预设已更新，请刷新。")
		}
	}
	action := "save-preset"
	if remove {
		action = "delete-preset"
	}
	if err = governance.Audit(ctx, tx, actor, action, "tag", p.Name, map[string]string{"color": p.Color}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
