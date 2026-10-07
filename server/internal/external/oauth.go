package external

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"time"

	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/secrets"
	"github.com/jackc/pgx/v5"
)

func (s *Service) ConfigureOAuth(config bangumi.OAuthConfig, key []byte, origin string) error {
	if config.ClientID == "" && config.ClientSecret == "" {
		return nil
	}
	if config.RedirectURI == "" {
		config.RedirectURI = origin + "/api/v1/connections/bangumi/callback"
	}
	if !config.Enabled() || config.RedirectURI != origin+"/api/v1/connections/bangumi/callback" {
		return errors.New("Bangumi OAuth requires both client credentials and a callback at PUBLIC_ORIGIN/api/v1/connections/bangumi/callback")
	}
	box, err := secrets.Cipher(key)
	if err != nil {
		return err
	}
	if box == nil {
		return errors.New("Bangumi OAuth requires ANIMEMO_SECRET_KEY")
	}
	s.oauth = config
	s.encryption = box
	return nil
}

type Connection struct {
	Configured   bool       `json:"configured"`
	State        string     `json:"state"`
	RemoteUserID *int64     `json:"remote_user_id"`
	Username     string     `json:"username"`
	Nickname     string     `json:"nickname"`
	ExpiresAt    *time.Time `json:"expires_at"`
}

func (s *Service) Connection(ctx context.Context, owner string) (Connection, error) {
	out := Connection{Configured: s.oauth.Enabled(), State: "disconnected"}
	err := s.pool.QueryRow(ctx, `SELECT state,remote_user_id,username,nickname,expires_at FROM external_connections WHERE user_id=$1`, owner).Scan(&out.State, &out.RemoteUserID, &out.Username, &out.Nickname, &out.ExpiresAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	return out, err
}
func (s *Service) BeginOAuth(ctx context.Context, owner, session string) (string, error) {
	if !s.oauth.Enabled() {
		return "", fault.New("service_unavailable", "部署者尚未配置 Bangumi OAuth 应用。")
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	state := base64.RawURLEncoding.EncodeToString(raw)
	stateHash := sha256.Sum256([]byte(state))
	sessionHash := sha256.Sum256([]byte(session))
	generation := id.New()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `INSERT INTO external_connections(user_id,generation,state) VALUES($1,$2,'authorizing') ON CONFLICT(user_id) DO UPDATE SET generation=excluded.generation,state='authorizing',tokens=NULL,remote_user_id=NULL,username='',nickname='',expires_at=NULL,updated_at=now()`, owner, generation); err != nil {
		return "", err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM external_oauth_states WHERE user_id=$1 OR expires_at<=now()`, owner); err != nil {
		return "", err
	}
	if _, err = tx.Exec(ctx, `UPDATE external_sync_jobs SET state='cancelled',error='账号正在重新授权。',updated_at=now() WHERE user_id=$1 AND state IN ('fetching','ready','applying')`, owner); err != nil {
		return "", err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO external_oauth_states(state_hash,user_id,session_hash,generation,redirect_uri,expires_at) VALUES($1,$2,$3,$4,$5,now()+interval '10 minutes')`, stateHash[:], owner, sessionHash[:], generation, s.oauth.RedirectURI); err != nil {
		return "", err
	}
	if err = tx.Commit(ctx); err != nil {
		return "", err
	}
	return s.oauth.AuthorizeURL(state), nil
}
func (s *Service) CompleteOAuth(ctx context.Context, owner, session, state, code string) error {
	if !s.oauth.Enabled() || len(state) != 43 || len(code) > 2048 {
		return fault.New("invalid_oauth_state", "授权已过期，请从账号设置重新连接。")
	}
	stateHash := sha256.Sum256([]byte(state))
	sessionHash := sha256.Sum256([]byte(session))
	var generation string
	err := s.pool.QueryRow(ctx, `DELETE FROM external_oauth_states WHERE state_hash=$1 AND user_id=$2 AND session_hash=$3 AND redirect_uri=$4 AND expires_at>now() RETURNING generation`, stateHash[:], owner, sessionHash[:], s.oauth.RedirectURI).Scan(&generation)
	if errors.Is(err, pgx.ErrNoRows) {
		return fault.New("invalid_oauth_state", "授权已过期或浏览器登录已改变，请重新连接。")
	}
	if err != nil {
		return err
	}
	if code == "" {
		return fault.New("invalid_oauth_state", "授权已取消，请重新连接。")
	}
	token, err := s.provider.Exchange(ctx, s.oauth, "authorization_code", code)
	if err != nil {
		return err
	}
	remote, err := s.provider.Me(ctx, token.AccessToken)
	if err != nil {
		return err
	}
	if token.UserID != 0 && token.UserID != remote.ID {
		return fault.New("forbidden", "Bangumi 返回的授权身份不一致。")
	}
	encoded, _ := json.Marshal(token)
	tag, err := s.pool.Exec(ctx, `UPDATE external_connections SET state='connected',remote_user_id=$3,username=$4,nickname=$5,tokens=$6,expires_at=$7,updated_at=now() WHERE user_id=$1 AND generation=$2 AND state='authorizing' AND EXISTS(SELECT 1 FROM users WHERE id=$1 AND NOT disabled)`, owner, generation, remote.ID, clipped(remote.Username, 160), clipped(remote.Nickname, 160), secrets.Seal(s.encryption, "oauth:"+owner+":"+generation, encoded), time.Now().UTC().Add(time.Duration(token.ExpiresIn)*time.Second))
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return fault.New("version_conflict", "连接已被取消或重新发起，请使用最新的授权。")
	}
	return nil
}
func (s *Service) Disconnect(ctx context.Context, owner string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `UPDATE external_connections SET state='disconnected',generation=$2,tokens=NULL,remote_user_id=NULL,username='',nickname='',expires_at=NULL,updated_at=now() WHERE user_id=$1`, owner, id.New()); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM external_oauth_states WHERE user_id=$1`, owner); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE external_sync_jobs SET state='cancelled',error='账号连接已断开；已发出的远端请求可能已生效，请重新连接后核对。',updated_at=now() WHERE user_id=$1 AND state IN ('fetching','ready','applying')`, owner); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) connectionToken(ctx context.Context, owner, generation string) (string, int64, error) {
	if !s.oauth.Enabled() {
		return "", 0, fault.New("service_unavailable", "Bangumi OAuth 尚未配置。")
	}
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return "", 0, err
	}
	var locked bool
	err = conn.QueryRow(ctx, `SELECT pg_try_advisory_lock(hashtextextended($1,718226051))`, owner).Scan(&locked)
	if err != nil || !locked {
		conn.Release()
		if err != nil {
			return "", 0, err
		}
		return "", 0, fault.New("service_unavailable", "账号授权正在更新，请稍后重试。")
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if _, err := conn.Exec(cleanup, `SELECT pg_advisory_unlock(hashtextextended($1,718226051))`, owner); err != nil {
			_ = conn.Conn().Close(cleanup)
		}
		conn.Release()
	}()
	var storedGeneration string
	var remoteID int64
	var encrypted []byte
	var expires time.Time
	err = conn.QueryRow(ctx, `SELECT c.generation,c.remote_user_id,c.tokens,c.expires_at FROM external_connections c JOIN users u ON u.id=c.user_id WHERE c.user_id=$1 AND c.state='connected' AND NOT u.disabled`, owner).Scan(&storedGeneration, &remoteID, &encrypted, &expires)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", 0, fault.New("forbidden", "请先连接 Bangumi 账号。")
	}
	if err != nil {
		return "", 0, err
	}
	if generation != "" && generation != storedGeneration {
		return "", 0, fault.New("version_conflict", "账号连接已改变，请重新生成预览。")
	}
	plain, err := secrets.Open(s.encryption, "oauth:"+owner+":"+storedGeneration, encrypted)
	if err != nil {
		return "", 0, err
	}
	var token bangumi.Token
	if err = json.Unmarshal(plain, &token); err != nil {
		return "", 0, fault.New("service_unavailable", "保存的授权无效，请重新连接。")
	}
	if expires.After(time.Now().Add(time.Minute)) {
		return token.AccessToken, remoteID, nil
	}
	refreshed, err := s.provider.Exchange(ctx, s.oauth, "refresh_token", token.RefreshToken)
	if err == nil {
		var user bangumi.User
		user, err = s.provider.Me(ctx, refreshed.AccessToken)
		if err == nil && user.ID != remoteID {
			err = fault.New("forbidden", "授权身份已改变，请重新连接。")
		}
	}
	if err != nil {
		// Refresh token rotation has no upstream idempotency key. An uncertain
		// exchange is resolved by a new authorization, never by blind retries.
		_, saveErr := conn.Exec(ctx, `UPDATE external_connections SET state='reauthorize',tokens=NULL,updated_at=now() WHERE user_id=$1 AND generation=$2`, owner, storedGeneration)
		if saveErr != nil {
			return "", 0, saveErr
		}
		return "", 0, fault.New("forbidden", "授权刷新未完成，请重新连接 Bangumi。")
	}
	encoded, _ := json.Marshal(refreshed)
	tag, err := conn.Exec(ctx, `UPDATE external_connections SET tokens=$3,expires_at=$4,updated_at=now() WHERE user_id=$1 AND generation=$2 AND state='connected'`, owner, storedGeneration, secrets.Seal(s.encryption, "oauth:"+owner+":"+storedGeneration, encoded), time.Now().UTC().Add(time.Duration(refreshed.ExpiresIn)*time.Second))
	if err != nil {
		return "", 0, err
	}
	if tag.RowsAffected() != 1 {
		return "", 0, fault.New("version_conflict", "账号连接已被取消。")
	}
	return refreshed.AccessToken, remoteID, nil
}
func (s *Service) VerifyConnection(ctx context.Context, owner string) (Connection, error) {
	token, remoteID, err := s.connectionToken(ctx, owner, "")
	if err != nil {
		return Connection{}, err
	}
	remote, err := s.provider.Me(ctx, token)
	if err != nil {
		return Connection{}, err
	}
	if remote.ID != remoteID {
		return Connection{}, fault.New("forbidden", "授权身份与连接不一致，请断开后重新连接。")
	}
	return s.Connection(ctx, owner)
}
