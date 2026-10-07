package accounts

import (
	"context"

	"animemo.local/server/internal/fault"
)

func (s *Service) Publication(ctx context.Context, owner, action string, version int) (User, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return User{}, err
	}
	defer tx.Rollback(context.Background())
	u, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+` FROM users WHERE id=$1 AND NOT disabled FOR UPDATE`, owner))
	if err != nil {
		return u, err
	}
	if version != u.Version {
		return u, fault.New("version_conflict", "设置已更新，请重新打开设置。")
	}
	switch action {
	case "enable-sharing":
		u.SharingEnabled = true
	case "disable-sharing":
		u.SharingEnabled = false
		u.PublicState = "private"
	case "request-public":
		u.SharingEnabled = true
		u.PublicState = "pending"
	case "withdraw-public":
		u.PublicState = "private"
	default:
		return u, fault.Field("action", "公开设置操作无效。")
	}
	u, err = scanUser(tx.QueryRow(ctx, `UPDATE users SET sharing_enabled=$2,public_state=$3,public_reason='',version=version+1 WHERE id=$1 RETURNING `+userColumns, owner, u.SharingEnabled, u.PublicState))
	if err != nil {
		return u, err
	}
	return u, tx.Commit(ctx)
}
