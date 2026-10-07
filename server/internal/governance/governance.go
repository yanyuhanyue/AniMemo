// Package governance keeps administrative authorization and audit writes in one transaction.
package governance

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"context"
	"github.com/jackc/pgx/v5"
)

func Lock(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(718226050)`)
	return err
}
func Authorize(ctx context.Context, tx pgx.Tx, actor string) error {
	if err := Lock(ctx, tx); err != nil {
		return err
	}
	var allowed bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND is_admin AND NOT disabled)`, actor).Scan(&allowed); err != nil {
		return err
	}
	if !allowed {
		return fault.New("forbidden", "需要管理员权限。")
	}
	return nil
}
func Audit(ctx context.Context, tx pgx.Tx, actor, action, kind, target string, detail any) error {
	_, err := tx.Exec(ctx, `INSERT INTO audit_log(id,actor_id,action,target_type,target_id,detail) VALUES($1,$2,$3,$4,$5,$6)`, id.New(), actor, action, kind, target, detail)
	return err
}
