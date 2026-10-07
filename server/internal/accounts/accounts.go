package accounts

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"net/mail"
	"strings"
	"time"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"
)

const SessionLifetime = 14 * 24 * time.Hour
const passwordCost = 12

type User struct {
	ID          string `json:"id"`
	Email       string `json:"email"`
	DisplayName string `json:"display_name"`
}

type Registration struct {
	Email       string `json:"email"`
	Password    string `json:"password"`
	DisplayName string `json:"display_name"`
}

type Credentials struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type Session struct {
	User      User
	Token     string
	ExpiresAt time.Time
}

type Service struct {
	pool      *pgxpool.Pool
	dummyHash []byte
}

func New(pool *pgxpool.Pool) *Service {
	// An unknown email follows the same expensive password check as an existing one.
	dummy, err := bcrypt.GenerateFromPassword([]byte(id.New()), passwordCost)
	if err != nil {
		panic(err)
	}
	return &Service{pool: pool, dummyHash: dummy}
}

func (r *Registration) Validate() error {
	r.Email = strings.ToLower(strings.TrimSpace(r.Email))
	r.DisplayName = strings.TrimSpace(r.DisplayName)
	address, err := mail.ParseAddress(r.Email)
	if err != nil || address.Address != r.Email || len(r.Email) > 254 {
		return fault.Field("email", "请输入有效的邮箱地址。")
	}
	if n := utf8.RuneCountInString(r.DisplayName); n < 1 || n > 32 {
		return fault.Field("display_name", "昵称需要 1–32 个字。")
	}
	if utf8.RuneCountInString(r.Password) < 12 || len(r.Password) > 72 {
		return fault.Field("password", "密码至少 12 个字符，且不超过 72 字节。")
	}
	return nil
}

func (s *Service) Register(ctx context.Context, input Registration) (Session, error) {
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
	user := User{ID: id.New(), Email: input.Email, DisplayName: input.DisplayName}
	_, err = tx.Exec(ctx, `INSERT INTO users (id,email,display_name,password_hash) VALUES ($1,$2,$3,$4)`, user.ID, user.Email, user.DisplayName, string(hash))
	var pgerr *pgconn.PgError
	if errors.As(err, &pgerr) && pgerr.Code == "23505" {
		return Session{}, fault.New("email_taken", "这个邮箱已经注册，请直接登录。")
	}
	if err != nil {
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

func (s *Service) Login(ctx context.Context, input Credentials) (Session, error) {
	input.Email = strings.ToLower(strings.TrimSpace(input.Email))
	var user User
	var hash string
	err := s.pool.QueryRow(ctx, `SELECT id,email,display_name,password_hash FROM users WHERE email=$1`, input.Email).Scan(&user.ID, &user.Email, &user.DisplayName, &hash)
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
	var user User
	err := s.pool.QueryRow(ctx, `SELECT u.id,u.email,u.display_name FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=$1 AND s.expires_at > now()`, hash[:]).Scan(&user.ID, &user.Email, &user.DisplayName)
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
