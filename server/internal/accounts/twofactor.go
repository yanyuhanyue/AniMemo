package accounts

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/secrets"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha1"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base32"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
	"net/url"
	"strings"
	"time"
)

// ConfigureEncryption requires an instance key retained with the database backup.
func (s *Service) ConfigureEncryption(key []byte) error {
	var err error
	s.encryption, err = secrets.Cipher(key)
	return err
}
func (s *Service) seal(owner string, plain []byte) []byte {
	return secrets.Seal(s.encryption, owner, plain)
}
func (s *Service) open(owner string, value []byte) ([]byte, error) {
	return secrets.Open(s.encryption, owner, value)
}
func totp(secret []byte, counter int64) string {
	var data [8]byte
	binary.BigEndian.PutUint64(data[:], uint64(counter))
	h := hmac.New(sha1.New, secret)
	h.Write(data[:])
	sum := h.Sum(nil)
	offset := sum[len(sum)-1] & 15
	n := binary.BigEndian.Uint32(sum[offset:offset+4]) & 0x7fffffff
	return fmt.Sprintf("%06d", n%1000000)
}
func validCounter(secret []byte, code string, now time.Time, last int64) (int64, bool) {
	if len(code) != 6 {
		return 0, false
	}
	step := now.Unix() / 30
	for _, counter := range []int64{step, step - 1, step + 1} {
		if counter > last && subtle.ConstantTimeCompare([]byte(totp(secret, counter)), []byte(code)) == 1 {
			return counter, true
		}
	}
	return 0, false
}
func recoveryHash(code string) [32]byte {
	return sha256.Sum256([]byte(strings.ToLower(strings.ReplaceAll(strings.TrimSpace(code), "-", ""))))
}
func (s *Service) verifySecondFactor(ctx context.Context, tx pgx.Tx, owner, code string) error {
	var encrypted []byte
	var last int64
	if err := tx.QueryRow(ctx, `SELECT otp_secret,otp_counter FROM users WHERE id=$1`, owner).Scan(&encrypted, &last); err != nil {
		return err
	}
	if encrypted == nil {
		return nil
	}
	secret, err := s.open(owner, encrypted)
	if err != nil {
		return err
	}
	if counter, valid := validCounter(secret, strings.TrimSpace(code), time.Now(), last); valid {
		_, err = tx.Exec(ctx, `UPDATE users SET otp_counter=$2 WHERE id=$1`, owner, counter)
		return err
	}
	hash := recoveryHash(code)
	tag, err := tx.Exec(ctx, `DELETE FROM recovery_codes WHERE user_id=$1 AND code_hash=$2`, owner, hash[:])
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 1 {
		return nil
	}
	return fault.New("invalid_credentials", "验证码或恢复码无效；已使用的验证码不能再次使用。")
}

type TwoFactorEnrollment struct {
	Secret    string    `json:"secret"`
	URI       string    `json:"uri"`
	ExpiresAt time.Time `json:"expires_at"`
}
type TwoFactorInput struct {
	Action   string `json:"action"`
	Password string `json:"password"`
	Code     string `json:"code"`
}

func (s *Service) BeginTwoFactor(ctx context.Context, owner, password string) (TwoFactorEnrollment, error) {
	var out TwoFactorEnrollment
	if s.encryption == nil {
		return out, fault.New("service_unavailable", "部署者尚未配置实例加密密钥。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return out, err
	}
	defer tx.Rollback(context.Background())
	var hash string
	u, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+`,password_hash FROM users WHERE id=$1 AND NOT disabled FOR UPDATE`, owner), &hash)
	if err != nil {
		return out, err
	}
	if bcrypt.CompareHashAndPassword([]byte(hash), []byte(password)) != nil {
		return out, fault.New("invalid_credentials", "当前密码不正确。")
	}
	if u.OTPEnabled {
		return out, fault.New("version_conflict", "两步验证已开启，请先停用后再更换。")
	}
	secret := make([]byte, 20)
	if _, err = rand.Read(secret); err != nil {
		return out, err
	}
	out.Secret = base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(secret)
	out.ExpiresAt = time.Now().UTC().Add(10 * time.Minute)
	out.URI = "otpauth://totp/" + url.PathEscape("AniMemo:"+u.Email) + "?" + url.Values{"secret": {out.Secret}, "issuer": {"AniMemo"}, "algorithm": {"SHA1"}, "digits": {"6"}, "period": {"30"}}.Encode()
	if _, err = tx.Exec(ctx, `UPDATE users SET otp_pending=$2,otp_pending_expires=$3 WHERE id=$1`, owner, s.seal(owner, secret), out.ExpiresAt); err != nil {
		return out, err
	}
	return out, tx.Commit(ctx)
}
func (s *Service) ChangeTwoFactor(ctx context.Context, owner string, input TwoFactorInput) (Session, []string, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Session{}, nil, err
	}
	defer tx.Rollback(context.Background())
	var hash string
	u, err := scanUser(tx.QueryRow(ctx, `SELECT `+userColumns+`,password_hash FROM users WHERE id=$1 AND NOT disabled FOR UPDATE`, owner), &hash)
	if err != nil {
		return Session{}, nil, err
	}
	if bcrypt.CompareHashAndPassword([]byte(hash), []byte(input.Password)) != nil {
		return Session{}, nil, fault.New("invalid_credentials", "当前密码不正确。")
	}
	switch input.Action {
	case "enable":
		if u.OTPEnabled {
			return Session{}, nil, fault.New("version_conflict", "两步验证已经开启。")
		}
		var encrypted []byte
		var expires *time.Time
		if err = tx.QueryRow(ctx, `SELECT otp_pending,otp_pending_expires FROM users WHERE id=$1`, owner).Scan(&encrypted, &expires); err != nil {
			return Session{}, nil, err
		}
		if expires == nil || !expires.After(time.Now()) {
			return Session{}, nil, fault.New("version_conflict", "设置已过期，请重新生成密钥。")
		}
		secret, err := s.open(owner, encrypted)
		if err != nil {
			return Session{}, nil, err
		}
		counter, valid := validCounter(secret, strings.TrimSpace(input.Code), time.Now(), -1)
		if !valid {
			return Session{}, nil, fault.New("invalid_credentials", "验证码无效。")
		}
		if _, err = tx.Exec(ctx, `UPDATE users SET otp_secret=otp_pending,otp_counter=$2 WHERE id=$1`, owner, counter); err != nil {
			return Session{}, nil, err
		}
		u.OTPEnabled = true
	case "disable", "recovery":
		if !u.OTPEnabled {
			return Session{}, nil, fault.New("version_conflict", "尚未开启两步验证。")
		}
		if err = s.verifySecondFactor(ctx, tx, owner, input.Code); err != nil {
			return Session{}, nil, err
		}
		if input.Action == "disable" {
			if _, err = tx.Exec(ctx, `UPDATE users SET otp_secret=NULL,otp_counter=-1 WHERE id=$1`, owner); err != nil {
				return Session{}, nil, err
			}
			u.OTPEnabled = false
		}
	default:
		return Session{}, nil, fault.Field("action", "两步验证操作无效。")
	}
	codes := []string{}
	if _, err = tx.Exec(ctx, `DELETE FROM recovery_codes WHERE user_id=$1`, owner); err != nil {
		return Session{}, nil, err
	}
	if input.Action != "disable" {
		for range 8 {
			raw := make([]byte, 12)
			if _, err = rand.Read(raw); err != nil {
				return Session{}, nil, err
			}
			text := hex.EncodeToString(raw)
			code := text[:8] + "-" + text[8:16] + "-" + text[16:]
			hash := recoveryHash(code)
			if _, err = tx.Exec(ctx, `INSERT INTO recovery_codes(user_id,code_hash) VALUES($1,$2)`, owner, hash[:]); err != nil {
				return Session{}, nil, err
			}
			codes = append(codes, code)
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE users SET otp_pending=NULL,otp_pending_expires=NULL,version=version+1 WHERE id=$1`, owner); err != nil {
		return Session{}, nil, err
	}
	u.Version++
	if _, err = tx.Exec(ctx, `DELETE FROM sessions WHERE user_id=$1`, owner); err != nil {
		return Session{}, nil, err
	}
	session, err := createSession(ctx, tx, u)
	if err != nil {
		return Session{}, nil, err
	}
	return session, codes, tx.Commit(ctx)
}
