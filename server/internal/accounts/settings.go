package accounts

import (
	"context"
	"errors"
	"strings"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

type Settings struct {
	Version     int    `json:"version"`
	DisplayName string `json:"display_name"`
	Bio         string `json:"bio"`
	Accent      string `json:"accent"`
	DefaultView string `json:"default_view"`
}

func (s *Service) Settings(ctx context.Context, owner string, input Settings) (User, error) {
	input.DisplayName, input.Bio = strings.TrimSpace(input.DisplayName), strings.TrimSpace(input.Bio)
	if n := utf8.RuneCountInString(input.DisplayName); n < 1 || n > 32 || strings.ContainsRune(input.DisplayName, 0) {
		return User{}, fault.Field("display_name", "昵称需要 1–32 个字。")
	}
	if utf8.RuneCountInString(input.Bio) > 240 || strings.ContainsRune(input.Bio, 0) {
		return User{}, fault.Field("bio", "简介不能超过 240 个字。")
	}
	if input.Accent != "violet" && input.Accent != "coral" && input.Accent != "blue" && input.Accent != "green" && input.Accent != "amber" {
		return User{}, fault.Field("accent", "强调色无效。")
	}
	if input.DefaultView != "cards" && input.DefaultView != "list" {
		return User{}, fault.Field("default_view", "视图无效。")
	}
	if input.Version < 1 {
		return User{}, fault.Field("version", "请重新打开设置后再试。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return User{}, err
	}
	defer tx.Rollback(context.Background())
	u, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+` FROM users WHERE id=$1 AND NOT disabled FOR UPDATE`, owner))
	if err != nil {
		return User{}, err
	}
	if u.Version != input.Version {
		return User{}, fault.New("version_conflict", "设置已更新，请重新打开设置。")
	}
	u, err = scanUser(tx.QueryRow(ctx, `UPDATE users SET display_name=$2,bio=$3,accent=$4,default_view=$5,version=version+1 WHERE id=$1 RETURNING `+userColumns, owner, input.DisplayName, input.Bio, input.Accent, input.DefaultView))
	if err != nil {
		return User{}, err
	}
	return u, tx.Commit(ctx)
}

func (s *Service) ChangePassword(ctx context.Context, owner, current, newPassword string) (Session, error) {
	check := Registration{Email: "check@example.test", DisplayName: "check", Password: newPassword}
	if err := check.Validate(); err != nil {
		return Session{}, err
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(newPassword), passwordCost)
	if err != nil {
		return Session{}, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Session{}, err
	}
	defer tx.Rollback(context.Background())
	var stored string
	u, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+`,password_hash FROM users WHERE id=$1 AND NOT disabled FOR UPDATE`, owner), &stored)
	if err != nil {
		return Session{}, err
	}
	if bcrypt.CompareHashAndPassword([]byte(stored), []byte(current)) != nil {
		return Session{}, fault.New("invalid_credentials", "当前密码不正确。")
	}
	if _, err = tx.Exec(ctx, `UPDATE users SET password_hash=$2,version=version+1 WHERE id=$1`, owner, string(hash)); err != nil {
		return Session{}, err
	}
	if _, err = tx.Exec(ctx, `UPDATE email_tokens SET consumed_at=now() WHERE user_id=$1 AND consumed_at IS NULL`, owner); err != nil {
		return Session{}, err
	}
	if _, err = tx.Exec(ctx, `UPDATE email_outbox SET state='cancelled',payload=NULL,updated_at=now() WHERE user_id=$1 AND state IN ('pending','sending')`, owner); err != nil {
		return Session{}, err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM sessions WHERE user_id=$1`, owner); err != nil {
		return Session{}, err
	}
	u.Version++
	session, err := createSession(ctx, tx, u)
	if err != nil {
		return Session{}, err
	}
	return session, tx.Commit(ctx)
}

func (s *Service) LogoutAll(ctx context.Context, owner string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT id FROM users WHERE id=$1 FOR UPDATE`, owner); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM sessions WHERE user_id=$1`, owner); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) Delete(ctx context.Context, owner, password string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var stored string
	var admin bool
	if err = tx.QueryRow(ctx, `SELECT password_hash,is_admin FROM users WHERE id=$1 FOR UPDATE`, owner).Scan(&stored, &admin); err != nil {
		return err
	}
	if bcrypt.CompareHashAndPassword([]byte(stored), []byte(password)) != nil {
		return fault.New("invalid_credentials", "当前密码不正确。")
	}
	if admin {
		return fault.New("validation_error", "管理员请先由其他管理员解除管理权限，再注销账号。")
	}
	if _, err = tx.Exec(ctx, `DELETE FROM users WHERE id=$1`, owner); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) Avatar(ctx context.Context, owner, revision string) (media.Image, error) {
	var out media.Image
	if !id.Valid(revision) {
		return out, fault.New("not_found", "没有找到头像。")
	}
	err := s.pool.QueryRow(ctx, `SELECT content_type,data FROM avatars WHERE user_id=$1 AND revision=$2`, owner, revision).Scan(&out.ContentType, &out.Data)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, fault.New("not_found", "没有找到头像。")
	}
	if err != nil {
		return out, err
	}
	return s.storage.Resolve(ctx, revision, out)
}

func (s *Service) SetAvatar(ctx context.Context, owner string, version int, kind string, data []byte) (User, error) {
	var picture media.Image
	var err error
	if data != nil {
		picture, err = media.Validate(data, kind)
		if err != nil {
			return User{}, err
		}
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return User{}, err
	}
	defer tx.Rollback(context.Background())
	u, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+` FROM users WHERE id=$1 FOR UPDATE`, owner))
	if err != nil {
		return User{}, err
	}
	if version != u.Version {
		return User{}, fault.New("version_conflict", "设置已更新，请重新打开设置。")
	}
	if data == nil {
		_, err = tx.Exec(ctx, `DELETE FROM avatars WHERE user_id=$1`, owner)
	} else {
		_, err = tx.Exec(ctx, `INSERT INTO avatars(user_id,revision,content_type,data,byte_size) VALUES($1,$2,$3,$4,$5) ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,content_type=excluded.content_type,data=excluded.data,byte_size=excluded.byte_size`, owner, id.New(), picture.ContentType, picture.Data, len(picture.Data))
	}
	if err != nil {
		return User{}, err
	}
	u, err = scanUser(tx.QueryRow(ctx, `UPDATE users SET version=version+1 WHERE id=$1 RETURNING `+userColumns, owner))
	if err != nil {
		return User{}, err
	}
	return u, tx.Commit(ctx)
}
