package api

import (
	"animemo.local/server/internal/accounts"
	"net/http"
)

func (a *API) beginTwoFactor(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Password string `json:"password"`
	}
	if !decode(w, r, &input) {
		return
	}
	out, err := a.accounts.BeginTwoFactor(r.Context(), currentUser(r).ID, input.Password)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) changeTwoFactor(w http.ResponseWriter, r *http.Request) {
	var input accounts.TwoFactorInput
	if !decode(w, r, &input) {
		return
	}
	session, codes, err := a.accounts.ChangeTwoFactor(r.Context(), currentUser(r).ID, input)
	if err != nil {
		fail(w, err)
		return
	}
	a.setSession(w, session)
	write(w, 200, map[string]any{"user": session.User, "recovery_codes": codes})
}
