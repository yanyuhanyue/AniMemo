package accounts

import (
	"bytes"
	"testing"
	"time"
)

func TestTOTPRFC6238AndReplay(t *testing.T) {
	secret := []byte("12345678901234567890")
	for _, v := range []struct {
		unix int64
		code string
	}{{59, "287082"}, {1111111109, "081804"}, {1111111111, "050471"}, {1234567890, "005924"}, {2000000000, "279037"}, {20000000000, "353130"}} {
		got := totp(secret, v.unix/30)
		if got != v.code {
			t.Fatalf("RFC vector %d: %s", v.unix, got)
		}
		counter, valid := validCounter(secret, v.code, time.Unix(v.unix, 0), -1)
		if !valid || counter != v.unix/30 {
			t.Fatal("valid code rejected")
		}
		if _, valid = validCounter(secret, v.code, time.Unix(v.unix, 0), counter); valid {
			t.Fatal("replay accepted")
		}
	}
}
func TestEnrollmentEncryptionBoundToOwner(t *testing.T) {
	s := &Service{}
	if err := s.ConfigureEncryption(bytes.Repeat([]byte{1}, 32)); err != nil {
		t.Fatal(err)
	}
	encrypted := s.seal("alice", []byte("secret"))
	if bytes.Contains(encrypted, []byte("secret")) {
		t.Fatal("plaintext stored")
	}
	plain, err := s.open("alice", encrypted)
	if err != nil || string(plain) != "secret" {
		t.Fatal("roundtrip failed")
	}
	if _, err = s.open("bob", encrypted); err == nil {
		t.Fatal("cross-owner ciphertext accepted")
	}
	encrypted[len(encrypted)-1] ^= 1
	if _, err = s.open("alice", encrypted); err == nil {
		t.Fatal("tampering accepted")
	}
}
