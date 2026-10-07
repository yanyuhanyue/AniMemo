package accounts

import (
	"context"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"net/mail"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/mailer"
	"animemo.local/server/internal/mediastore"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"
)

const SessionLifetime = 14 * 24 * time.Hour
const passwordCost = 12

type User struct {
	EmailVerified             bool    `json:"email_verified"`
	EmailVerificationRequired bool    `json:"email_verification_required"`
	OTPEnabled                bool    `json:"otp_enabled"`
	ID                        string  `json:"id"`
	Email                     string  `json:"email"`
	DisplayName               string  `json:"display_name"`
	Bio                       string  `json:"bio"`
	Accent                    string  `json:"accent"`
	DefaultView               string  `json:"default_view"`
	Version                   int     `json:"version"`
	IsAdmin                   bool    `json:"is_admin"`
	AvatarRevision            *string `json:"avatar_revision"`
	SharingEnabled            bool    `json:"sharing_enabled"`
	PublicSlug                string  `json:"public_slug"`
	PublicState               string  `json:"public_state"`
	PublicReason              string  `json:"public_reason"`
}

type Registration struct {
	Email       string `json:"email"`
	Password    string `json:"password"`
	DisplayName string `json:"display_name"`
}

type Credentials struct {
	Code     string `json:"code,omitempty"`
	Email    string `json:"email"`
	Password string `json:"password"`
}

type Session struct {
	User      User
	Token     string
	ExpiresAt time.Time
}

type Service struct {
	storage    *mediastore.Store
	pool       *pgxpool.Pool
	dummyHash  []byte
	encryption cipher.AEAD
	mailer     *mailer.Client
	mailOrigin string
}

var sharedDummyHash = sync.OnceValue(func() []byte {
	dummy, err := bcrypt.GenerateFromPassword([]byte(id.New()), passwordCost)
	if err != nil {
		panic(err)
	}
	return dummy
})

func New(pool *pgxpool.Pool) *Service {
	return &Service{pool: pool, dummyHash: sharedDummyHash(), storage: mediastore.New(pool, nil)}
}
func (s *Service) ConfigureStorage(storage *mediastore.Store) { s.storage = storage }

const userColumns = `users.id,users.email,users.display_name,users.bio,users.accent,users.default_view,users.version,users.is_admin,(SELECT revision FROM avatars WHERE user_id=users.id),users.sharing_enabled,users.public_slug,users.public_state,users.public_reason,(users.otp_secret IS NOT NULL),(users.email_verified_at IS NOT NULL),users.email_verification_required`

func scanUser(row pgx.Row, extra ...any) (User, error) {
	var u User
	args := []any{&u.ID, &u.Email, &u.DisplayName, &u.Bio, &u.Accent, &u.DefaultView, &u.Version, &u.IsAdmin, &u.AvatarRevision, &u.SharingEnabled, &u.PublicSlug, &u.PublicState, &u.PublicReason, &u.OTPEnabled, &u.EmailVerified, &u.EmailVerificationRequired}
	err := row.Scan(append(args, extra...)...)
	if errors.Is(err, pgx.ErrNoRows) {
		return User{}, fault.New("unauthorized", "登录已过期，请重新登录。")
	}
	return u, err
}

func (r *Registration) Validate() error {
	r.Email = strings.ToLower(strings.TrimSpace(r.Email))
	r.DisplayName = strings.TrimSpace(r.DisplayName)
	address, err := mail.ParseAddress(r.Email)
	if err != nil || address.Address != r.Email || len(r.Email) > 254 {
		return fault.Field("email", "请输入有效的邮箱地址。")
	}
	if n := utf8.RuneCountInString(r.DisplayName); n < 1 || n > 32 || strings.ContainsRune(r.DisplayName, 0) {
		return fault.Field("display_name", "昵称需要 1–32 个字。")
	}
	if utf8.RuneCountInString(r.Password) < 12 || len(r.Password) > 72 {
		return fault.Field("password", "密码至少 12 个字符，且不超过 72 字节。")
	}
	return nil
}

func (s *Service) Register(ctx context.Context, input Registration) (Session, error) {
	// With verified registration, the mailbox owner chooses a password on the
	// verification page. A pre-registration cannot reserve an attacker-known password.
	if s.mailer != nil {
		input.Password = id.New()
	}
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
	var registrationOpen bool
	if err = tx.QueryRow(ctx, `SELECT registration_open FROM site_settings FOR SHARE`).Scan(&registrationOpen); err != nil {
		return Session{}, err
	}
	if !registrationOpen {
		return Session{}, fault.New("forbidden", "本站暂时关闭注册。")
	}
	user := User{ID: id.New(), Email: input.Email, DisplayName: input.DisplayName}
	user, err = scanUser(tx.QueryRow(ctx, `INSERT INTO users (id,email,display_name,password_hash,email_verification_required) VALUES ($1,$2,$3,$4,$5) RETURNING `+userColumns, user.ID, user.Email, user.DisplayName, string(hash), s.mailer != nil))
	var pgerr *pgconn.PgError
	if errors.As(err, &pgerr) && pgerr.Code == "23505" {
		if s.mailer != nil {
			return Session{}, nil
		}
		return Session{}, fault.New("email_taken", "这个邮箱已经注册，请直接登录。")
	}
	if err != nil {
		return Session{}, err
	}
	if s.mailer != nil {
		if err = s.queueEmail(ctx, tx, user, "verify"); err != nil {
			return Session{}, err
		}
		return Session{User: user}, tx.Commit(ctx)
	}
	session, err := createSession(ctx, tx, user)
	if err != nil {
		return Session{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Session{}, err
	}
	return session, nil
}

func (s *Service) Login(ctx context.Context, input Credentials) (Session, error) {
	input.Email = strings.ToLower(strings.TrimSpace(input.Email))
	var hash string
	err := s.pool.QueryRow(ctx, `SELECT password_hash FROM users WHERE email=$1 AND NOT disabled`, input.Email).Scan(&hash)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return Session{}, err
	}
	passwordHash := []byte(hash)
	if err != nil {
		passwordHash = s.dummyHash
	}
	passwordErr := bcrypt.CompareHashAndPassword(passwordHash, []byte(input.Password))
	if err != nil || passwordErr != nil {
		return Session{}, fault.New("invalid_credentials", "邮箱或密码不正确。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Session{}, err
	}
	defer tx.Rollback(context.Background())
	// Recheck the verified credential under the same row lock used by password
	// changes and account disabling, so a racing login cannot revive a revoked session.
	var currentHash string
	user, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+`,password_hash FROM users WHERE email=$1 AND NOT disabled FOR UPDATE`, input.Email), &currentHash)
	if err != nil {
		return Session{}, err
	}
	if currentHash != hash {
		return Session{}, fault.New("invalid_credentials", "账号凭据已更新，请重新登录。")
	}
	if user.EmailVerificationRequired && !user.EmailVerified {
		return Session{}, fault.New("email_verification_required", "请先通过验证邮件设置密码，再登录。")
	}
	if err = s.verifySecondFactor(ctx, tx, user.ID, input.Code); err != nil {
		return Session{}, err
	}
	// Keep expired sessions from accumulating without a separate maintenance worker.
	if _, err = tx.Exec(ctx, `DELETE FROM sessions WHERE user_id=$1 AND expires_at <= now()`, user.ID); err != nil {
		return Session{}, err
	}
	session, err := createSession(ctx, tx, user)
	if err != nil {
		return Session{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Session{}, err
	}
	return session, nil
}

func createSession(ctx context.Context, tx pgx.Tx, user User) (Session, error) {
	var secret [32]byte
	if _, err := rand.Read(secret[:]); err != nil {
		return Session{}, err
	}
	token := base64.RawURLEncoding.EncodeToString(secret[:])
	hash := sha256.Sum256([]byte(token))
	expires := time.Now().UTC().Add(SessionLifetime)
	_, err := tx.Exec(ctx, `INSERT INTO sessions (token_hash,user_id,expires_at) VALUES ($1,$2,$3)`, hash[:], user.ID, expires)
	if err != nil {
		return Session{}, err
	}
	return Session{User: user, Token: token, ExpiresAt: expires}, nil
}

func (s *Service) Authenticate(ctx context.Context, token string) (User, error) {
	if len(token) != 43 {
		return User{}, fault.New("unauthorized", "请先登录。")
	}
	hash := sha256.Sum256([]byte(token))
	user, err := scanUser(s.pool.QueryRow(ctx, `SELECT `+userColumns+` FROM users JOIN sessions s ON s.user_id=users.id WHERE s.token_hash=$1 AND s.expires_at > now() AND NOT users.disabled`, hash[:]))
	if errors.Is(err, pgx.ErrNoRows) {
		return User{}, fault.New("unauthorized", "登录已过期，请重新登录。")
	}
	return user, err
}

func (s *Service) Logout(ctx context.Context, token string) error {
	hash := sha256.Sum256([]byte(token))
	_, err := s.pool.Exec(ctx, `DELETE FROM sessions WHERE token_hash=$1`, hash[:])
	return err
}
