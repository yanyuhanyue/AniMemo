package accounts

import (
	"strings"
	"testing"
)

func TestRegistrationNormalizesIdentityWithoutChangingPassword(t *testing.T) {
	r := Registration{Email: " Person@Example.com ", DisplayName: "  小春  ", Password: " spaces are meaningful "}
	if err := r.Validate(); err != nil {
		t.Fatal(err)
	}
	if r.Email != "person@example.com" || r.DisplayName != "小春" || r.Password != " spaces are meaningful " {
		t.Fatalf("invalid normalization: email=%q name=%q", r.Email, r.DisplayName)
	}
}

func TestPasswordLimitsPreventBcryptTruncation(t *testing.T) {
	r := Registration{Email: "person@example.com", DisplayName: "小春", Password: strings.Repeat("动", 24)}
	if err := r.Validate(); err != nil {
		t.Fatalf("72-byte password should fit: %v", err)
	}
	r.Password += "画"
	if r.Validate() == nil {
		t.Fatal("accepted password beyond bcrypt's byte limit")
	}
	r.Password = "short"
	if r.Validate() == nil {
		t.Fatal("accepted short password")
	}
}
