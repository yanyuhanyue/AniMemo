package accounts

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net/mail"
	"net/url"
	"strings"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/mailer"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

func (s *Service) ConfigureMail(client *mailer.Client, origin string) error {
	if client != nil && s.encryption == nil {
		return errors.New("transactional mail requires ANIMEMO_SECRET_KEY to protect queued messages")
	}
	s.mailer = client
	s.mailOrigin = origin
	return nil
}
func (s *Service) MailEnabled() bool { return s.mailer != nil }

func (s *Service) queueEmail(ctx context.Context, tx pgx.Tx, user User, purpose string) error {
	if s.mailer == nil {
		return fault.New("service_unavailable", "实例尚未启用验证邮件，请联系管理员。")
	}
	if _, err := tx.Exec(ctx, `UPDATE email_tokens SET consumed_at=now() WHERE user_id=$1 AND purpose=$2 AND consumed_at IS NULL`, user.ID, purpose); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `UPDATE email_outbox SET state='cancelled',payload=NULL,updated_at=now() WHERE user_id=$1 AND purpose=$2 AND state IN ('pending','sending')`, user.ID, purpose); err != nil {
		return err
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return err
	}
	token := base64.RawURLEncoding.EncodeToString(raw)
	digest := sha256.Sum256([]byte(token))
	jobID := id.New()
	expires := time.Now().UTC().Add(30 * time.Minute)
	path, subject, instruction := "/reset-password", "重置 AniMemo 密码", "你正在申请重置 AniMemo 密码。"
	if purpose == "verify" {
		path = "/verify-email"
		subject = "验证你的 AniMemo 邮箱"
		instruction = "请验证邮箱并设置登录密码，完成 AniMemo 账号创建。"
	}
	link := s.mailOrigin + path + "#token=" + url.QueryEscape(token)
	message := mailer.Message{From: s.mailer.From, To: []string{user.Email}, Subject: subject, Text: instruction + "\n\n" + link + "\n\n链接 30 分钟内有效，仅可使用一次。如果不是你本人申请，请忽略此邮件。"}
	payload, err := json.Marshal(message)
	if err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO email_tokens(token_hash,user_id,purpose,expires_at) VALUES($1,$2,$3,$4)`, digest[:], user.ID, purpose, expires); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO email_outbox(id,user_id,purpose,payload,expires_at) VALUES($1,$2,$3,$4,$5)`, jobID, user.ID, purpose, s.seal("mail:"+jobID, payload), expires)
	return err
}

// RequestEmail has the same result for absent, disabled, verified and throttled accounts.
func (s *Service) RequestEmail(ctx context.Context, email, purpose string) error {
	if !s.MailEnabled() {
		return fault.New("service_unavailable", "实例尚未启用验证邮件，请联系管理员。")
	}
	if purpose != "verify" && purpose != "reset" {
		return fault.Field("purpose", "邮件用途无效。")
	}
	email = strings.ToLower(strings.TrimSpace(email))
	address, err := mail.ParseAddress(email)
	if err != nil || address.Address != email || len(email) > 254 {
		return fault.Field("email", "请输入有效的邮箱地址。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	user, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+` FROM users WHERE email=$1 AND NOT disabled FOR UPDATE`, email))
	var problem *fault.Error
	if errors.As(err, &problem) && problem.Code == "unauthorized" {
		return nil
	}
	if err != nil {
		return err
	}
	if purpose == "verify" && user.EmailVerified {
		return nil
	}
	var count int
	var recent bool
	if err = tx.QueryRow(ctx, `SELECT count(*),coalesce(bool_or(created_at>now()-interval '60 seconds'),false) FROM email_outbox WHERE user_id=$1 AND created_at>now()-interval '1 hour'`, user.ID).Scan(&count, &recent); err != nil {
		return err
	}
	if recent || count >= 3 {
		return nil
	}
	if err = s.queueEmail(ctx, tx, user, purpose); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

type EmailConfirmation struct {
	Token    string `json:"token"`
	Password string `json:"password"`
	Code     string `json:"code"`
}

func (s *Service) ConfirmEmail(ctx context.Context, purpose string, input EmailConfirmation) error {
	if len(input.Token) != 43 {
		return fault.New("invalid_email_token", "链接无效或已过期，请重新申请邮件。")
	}
	check := Registration{Email: "check@example.test", DisplayName: "check", Password: input.Password}
	if err := check.Validate(); err != nil {
		return err
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(input.Password), passwordCost)
	if err != nil {
		return err
	}
	digest := sha256.Sum256([]byte(input.Token))
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	// Lock the account before the token, matching password changes and email issuance.
	var owner string
	if err = tx.QueryRow(ctx, `SELECT user_id FROM email_tokens WHERE token_hash=$1 AND purpose=$2`, digest[:], purpose).Scan(&owner); errors.Is(err, pgx.ErrNoRows) {
		return fault.New("invalid_email_token", "链接无效或已过期，请重新申请邮件。")
	} else if err != nil {
		return err
	}
	user, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+` FROM users WHERE id=$1 AND NOT disabled FOR UPDATE`, owner))
	if err != nil {
		return fault.New("invalid_email_token", "链接无效或已过期，请重新申请邮件。")
	}
	var valid bool
	if err = tx.QueryRow(ctx, `SELECT consumed_at IS NULL AND expires_at>now() FROM email_tokens WHERE token_hash=$1 FOR UPDATE`, digest[:]).Scan(&valid); err != nil {
		return err
	}
	if !valid {
		return fault.New("invalid_email_token", "链接无效或已过期，请重新申请邮件。")
	}
	// Mailbox access does not bypass an already-enrolled second factor.
	if err = s.verifySecondFactor(ctx, tx, user.ID, input.Code); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE users SET password_hash=$2,email_verified_at=now(),email_verification_required=false,version=version+1 WHERE id=$1`, owner, string(hash)); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE email_tokens SET consumed_at=now() WHERE user_id=$1 AND consumed_at IS NULL`, owner); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE email_outbox SET state='cancelled',payload=NULL,updated_at=now() WHERE user_id=$1 AND state IN ('pending','sending')`, owner); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM sessions WHERE user_id=$1`, owner); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) RunMailWorker(ctx context.Context) {
	if !s.MailEnabled() {
		return
	}
	timer := time.NewTicker(time.Second)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			if _, err := s.ProcessNextMail(ctx); err != nil && ctx.Err() == nil {
				slog.Error("mail worker failed", "error_type", "database_or_delivery_failure")
			}
		}
	}
}

// The advisory lock is released on process death. Resend's persisted idempotency
// key also covers a crash after the provider accepts mail but before our commit.
func (s *Service) ProcessNextMail(ctx context.Context) (bool, error) {
	if !s.MailEnabled() {
		return false, nil
	}
	if _, err := s.pool.Exec(ctx, `UPDATE email_outbox SET state='cancelled',payload=NULL,updated_at=now() WHERE expires_at<=now() AND state IN ('pending','sending')`); err != nil {
		return false, err
	}
	var jobID string
	err := s.pool.QueryRow(ctx, `SELECT id FROM email_outbox WHERE state IN ('pending','sending') AND available_at<=now() ORDER BY created_at,id LIMIT 1`).Scan(&jobID)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return false, err
	}
	var locked bool
	err = conn.QueryRow(ctx, `SELECT pg_try_advisory_lock(hashtextextended($1,718226050))`, jobID).Scan(&locked)
	if err != nil || !locked {
		conn.Release()
		return false, err
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if _, err := conn.Exec(cleanup, `SELECT pg_advisory_unlock(hashtextextended($1,718226050))`, jobID); err != nil {
			_ = conn.Conn().Close(cleanup)
		}
		conn.Release()
	}()
	var payload []byte
	var attempts int
	err = conn.QueryRow(ctx, `UPDATE email_outbox SET state='sending',attempts=attempts+1,updated_at=now() WHERE id=$1 AND state IN ('pending','sending') AND available_at<=now() AND expires_at>now() RETURNING payload,attempts`, jobID).Scan(&payload, &attempts)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	plain, err := s.open("mail:"+jobID, payload)
	if err != nil {
		return true, err
	}
	var message mailer.Message
	if err = json.Unmarshal(plain, &message); err != nil {
		return true, err
	}
	providerID, deliveryErr := s.mailer.Send(ctx, "animemo-mail/"+jobID, message)
	if deliveryErr == nil {
		_, err = conn.Exec(ctx, `UPDATE email_outbox SET state='accepted',provider_id=$2,payload=NULL,updated_at=now() WHERE id=$1 AND state='sending'`, jobID, providerID)
		return true, err
	}
	var failure *mailer.DeliveryError
	retry := errors.As(deliveryErr, &failure) && failure.Retryable && attempts < 6
	if retry {
		delay := max(time.Duration(1<<min(attempts, 8))*time.Second, failure.RetryAfter)
		_, err = conn.Exec(ctx, `UPDATE email_outbox SET state='pending',available_at=$2,updated_at=now() WHERE id=$1 AND state='sending'`, jobID, time.Now().Add(delay))
	} else {
		_, err = conn.Exec(ctx, `UPDATE email_outbox SET state='failed',payload=NULL,updated_at=now() WHERE id=$1 AND state='sending'`, jobID)
	}
	return true, err
}
