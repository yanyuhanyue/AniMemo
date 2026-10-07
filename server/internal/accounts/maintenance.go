package accounts

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"context"
	"time"
)

type StorageOwner struct {
	ID    string `json:"id"`
	Name  string `json:"name"`
	Bytes int64  `json:"bytes"`
}
type InstanceStatus struct {
	Database        string         `json:"database"`
	ServerTime      time.Time      `json:"server_time"`
	Users           int            `json:"users"`
	Entries         int            `json:"entries"`
	Columns         int            `json:"columns"`
	DatabaseBytes   int64          `json:"database_bytes"`
	MediaBytes      int64          `json:"media_bytes"`
	ImportsActive   int            `json:"imports_active"`
	ExpiredSessions int            `json:"expired_sessions"`
	Owners          []StorageOwner `json:"owners"`
}

func (s *Service) InstanceStatus(ctx context.Context) (InstanceStatus, error) {
	out := InstanceStatus{Database: "ready", ServerTime: time.Now().UTC(), Owners: []StorageOwner{}}
	err := s.pool.QueryRow(ctx, `SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM entries),(SELECT count(*) FROM columns),pg_database_size(current_database()),(SELECT coalesce(sum(byte_size),0) FROM media_references),(SELECT count(*) FROM import_jobs WHERE state IN ('validating','ready','applying')),(SELECT count(*) FROM sessions WHERE expires_at<=now())`).Scan(&out.Users, &out.Entries, &out.Columns, &out.DatabaseBytes, &out.MediaBytes, &out.ImportsActive, &out.ExpiredSessions)
	if err != nil {
		return out, err
	}
	rows, err := s.pool.Query(ctx, `SELECT u.id,u.display_name,coalesce((SELECT sum(byte_size) FROM media_references WHERE owner_id=u.id),0) AS used FROM users u ORDER BY used DESC,u.id LIMIT 20`)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var o StorageOwner
		if err = rows.Scan(&o.ID, &o.Name, &o.Bytes); err != nil {
			return out, err
		}
		out.Owners = append(out.Owners, o)
	}
	return out, rows.Err()
}

type MaintenanceResult struct {
	Sessions    int64 `json:"sessions"`
	Imports     int64 `json:"imports"`
	Enrollments int64 `json:"enrollments"`
}

func (s *Service) Maintenance(ctx context.Context, actor, action string) (MaintenanceResult, error) {
	var out MaintenanceResult
	if action != "clear-expired" {
		return out, fault.Field("action", "维护操作无效。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return out, err
	}
	tag, err := tx.Exec(ctx, `DELETE FROM sessions WHERE expires_at<=now()`)
	if err != nil {
		return out, err
	}
	out.Sessions = tag.RowsAffected()
	tag, err = tx.Exec(ctx, `DELETE FROM import_jobs WHERE expires_at<=now() AND state IN ('done','cancelled','failed')`)
	if err != nil {
		return out, err
	}
	out.Imports = tag.RowsAffected()
	tag, err = tx.Exec(ctx, `UPDATE users SET otp_pending=NULL,otp_pending_expires=NULL WHERE otp_pending_expires<=now()`)
	if err != nil {
		return out, err
	}
	out.Enrollments = tag.RowsAffected()
	if err = governance.Audit(ctx, tx, actor, "clear-expired", "instance", "singleton", out); err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
