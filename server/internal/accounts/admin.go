package accounts

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
	"context"
	"errors"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
	"strings"
	"time"
	"unicode/utf8"
)

func (s *Service) SetupAvailable(ctx context.Context) (bool, error) {
	var exists bool
	err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM users WHERE is_admin)`).Scan(&exists)
	return !exists, err
}
func (s *Service) Setup(ctx context.Context, input Registration) (Session, error) {
	if err := input.Validate(); err != nil {
		return Session{}, err
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(input.Password), passwordCost)
	if err != nil {
		return Session{}, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Session{}, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Lock(ctx, tx); err != nil {
		return Session{}, err
	}
	var exists bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM users WHERE is_admin)`).Scan(&exists); err != nil {
		return Session{}, err
	}
	if exists {
		return Session{}, fault.New("version_conflict", "实例已经完成初始化。")
	}
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM users WHERE email=$1)`, input.Email).Scan(&exists); err != nil {
		return Session{}, err
	}
	if exists {
		return Session{}, fault.New("email_taken", "请用尚未注册的邮箱创建管理员账号。")
	}
	u, err := scanUser(tx.QueryRow(ctx, `INSERT INTO users(id,email,display_name,password_hash,is_admin) VALUES($1,$2,$3,$4,true) RETURNING `+userColumns, id.New(), input.Email, input.DisplayName, string(hash)))
	if err != nil {
		return Session{}, err
	}
	if err = governance.Audit(ctx, tx, u.ID, "setup", "user", u.ID, map[string]string{}); err != nil {
		return Session{}, err
	}
	session, err := createSession(ctx, tx, u)
	if err != nil {
		return Session{}, err
	}
	return session, tx.Commit(ctx)
}

type AdminUser struct {
	User
	Disabled bool `json:"disabled"`
}
type AdminUsers struct {
	Items []AdminUser `json:"items"`
	Total int         `json:"total"`
	Page  int         `json:"page"`
}

func (s *Service) AdminUsers(ctx context.Context, search, state string, page int) (AdminUsers, error) {
	out := AdminUsers{Items: []AdminUser{}, Page: page}
	if page < 1 || page > 100000 || utf8.RuneCountInString(search) > 160 {
		return out, fault.Field("search", "搜索或页码无效。")
	}
	if state != "" && state != "pending" && state != "published" && state != "disabled" {
		return out, fault.Field("state", "筛选无效。")
	}
	search = "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(strings.TrimSpace(search)) + "%"
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	where := ` FROM users WHERE (email ILIKE $1 OR display_name ILIKE $1) AND ($2='' OR public_state=$2 OR ($2='disabled' AND disabled))`
	if err = tx.QueryRow(ctx, `SELECT count(*)`+where, search, state).Scan(&out.Total); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT `+userColumns+`,disabled`+where+` ORDER BY email,id LIMIT 20 OFFSET $3`, search, state, (page-1)*20)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		var u AdminUser
		u.User, err = scanUser(rows, &u.Disabled)
		if err != nil {
			rows.Close()
			return out, err
		}
		out.Items = append(out.Items, u)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}

type AdminAction struct {
	Action  string `json:"action"`
	Version int    `json:"version"`
	Reason  string `json:"reason"`
}

func (s *Service) AdminUserAction(ctx context.Context, actor, target string, input AdminAction) (AdminUser, error) {
	var out AdminUser
	if !id.Valid(target) {
		return out, fault.New("not_found", "账号不存在。")
	}
	input.Reason = strings.TrimSpace(input.Reason)
	if utf8.RuneCountInString(input.Reason) > 400 || strings.ContainsRune(input.Reason, 0) {
		return out, fault.Field("reason", "原因最多 400 字。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return out, err
	}
	out.User, err = scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+`,disabled FROM users WHERE id=$1 FOR UPDATE`, target), &out.Disabled)
	if err != nil {
		return out, err
	}
	if out.Version != input.Version {
		return out, fault.New("version_conflict", "账号已更新，请刷新后重试。")
	}
	if input.Action == "disable" || input.Action == "remove-admin" {
		var others int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM users WHERE is_admin AND NOT disabled AND id<>$1`, target).Scan(&others); err != nil {
			return out, err
		}
		if out.IsAdmin && !out.Disabled && others == 0 {
			return out, fault.New("version_conflict", "必须保留至少一位可登录的管理员。")
		}
	}
	switch input.Action {
	case "grant-admin":
		out.IsAdmin = true
	case "remove-admin":
		out.IsAdmin = false
	case "disable":
		out.Disabled = true
	case "enable":
		out.Disabled = false
	case "revoke-sessions":
	case "approve-public":
		if out.PublicState != "pending" || !out.SharingEnabled || out.Disabled {
			return out, fault.New("version_conflict", "该账号没有待审核的公开申请。")
		}
		out.PublicState = "published"
	case "reject-public", "hide-public":
		if input.Reason == "" {
			return out, fault.Field("reason", "请填写原因。")
		}
		out.PublicState = "rejected"
	default:
		return out, fault.Field("action", "账号操作无效。")
	}
	out.User, err = scanUser(tx.QueryRow(ctx, `UPDATE users SET is_admin=$2,disabled=$3,public_state=$4,public_reason=$5,version=version+1 WHERE id=$1 RETURNING `+userColumns, target, out.IsAdmin, out.Disabled, out.PublicState, input.Reason))
	if err != nil {
		return out, err
	}
	if input.Action == "disable" || input.Action == "remove-admin" || input.Action == "grant-admin" || input.Action == "revoke-sessions" {
		if _, err = tx.Exec(ctx, `DELETE FROM sessions WHERE user_id=$1`, target); err != nil {
			return out, err
		}
	}
	if input.Action == "disable" {
		if _, err = tx.Exec(ctx, `UPDATE external_connections SET state='disconnected',generation=gen_random_uuid(),tokens=NULL,expires_at=NULL,updated_at=now() WHERE user_id=$1`, target); err != nil {
			return out, err
		}
		if _, err = tx.Exec(ctx, `DELETE FROM external_oauth_states WHERE user_id=$1`, target); err != nil {
			return out, err
		}
		if _, err = tx.Exec(ctx, `UPDATE external_sync_jobs SET state='cancelled',error='账号已停用。',updated_at=now() WHERE user_id=$1 AND state IN ('fetching','ready','applying')`, target); err != nil {
			return out, err
		}
	}
	if err = governance.Audit(ctx, tx, actor, input.Action, "user", target, map[string]string{"reason": input.Reason}); err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}

type SiteSettings struct {
	Name             string `json:"name"`
	Description      string `json:"description"`
	RegistrationOpen bool   `json:"registration_open"`
	Version          int    `json:"version"`
}

func (s *Service) Site(ctx context.Context) (SiteSettings, error) {
	var v SiteSettings
	err := s.pool.QueryRow(ctx, `SELECT name,description,registration_open,version FROM site_settings`).Scan(&v.Name, &v.Description, &v.RegistrationOpen, &v.Version)
	return v, err
}
func (s *Service) UpdateSite(ctx context.Context, actor string, input SiteSettings) (SiteSettings, error) {
	input.Name = strings.TrimSpace(input.Name)
	input.Description = strings.TrimSpace(input.Description)
	if len([]rune(input.Name)) < 1 || len([]rune(input.Name)) > 60 || len([]rune(input.Description)) > 400 || strings.ContainsRune(input.Name+input.Description, 0) {
		return input, fault.Field("name", "站点名需要 1–60 字，简介最多 400 字。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return input, err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return input, err
	}
	err = tx.QueryRow(ctx, `UPDATE site_settings SET name=$1,description=$2,registration_open=$3,version=version+1 WHERE version=$4 RETURNING version`, input.Name, input.Description, input.RegistrationOpen, input.Version).Scan(&input.Version)
	if errors.Is(err, pgx.ErrNoRows) {
		return input, fault.New("version_conflict", "站点设置已更新，请刷新。")
	}
	if err != nil {
		return input, err
	}
	if err = governance.Audit(ctx, tx, actor, "update-settings", "site", "singleton", map[string]any{"name": input.Name, "registration_open": input.RegistrationOpen}); err != nil {
		return input, err
	}
	return input, tx.Commit(ctx)
}

type AuditEvent struct {
	ID        string         `json:"id"`
	Actor     *string        `json:"actor"`
	Action    string         `json:"action"`
	Kind      string         `json:"kind"`
	Target    string         `json:"target"`
	Detail    map[string]any `json:"detail"`
	CreatedAt time.Time      `json:"created_at"`
}
type AuditPage struct {
	Items []AuditEvent `json:"items"`
	Total int          `json:"total"`
	Page  int          `json:"page"`
}

func (s *Service) Audit(ctx context.Context, page int) (AuditPage, error) {
	out := AuditPage{Items: []AuditEvent{}, Page: page}
	if page < 1 || page > 100000 {
		return out, fault.Field("page", "页码无效。")
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM audit_log`).Scan(&out.Total); err != nil {
		return out, err
	}
	rows, err := tx.Query(ctx, `SELECT a.id,u.display_name,a.action,a.target_type,a.target_id,a.detail,a.created_at FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.created_at DESC,a.id LIMIT 30 OFFSET $1`, (page-1)*30)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		var v AuditEvent
		if err = rows.Scan(&v.ID, &v.Actor, &v.Action, &v.Kind, &v.Target, &v.Detail, &v.CreatedAt); err != nil {
			rows.Close()
			return out, err
		}
		out.Items = append(out.Items, v)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
