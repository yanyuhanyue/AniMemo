package api

import (
	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/fault"
	"crypto/sha256"
	"crypto/subtle"
	"net/http"
	"strconv"
)

func (a *API) admin(next http.HandlerFunc) http.HandlerFunc {
	return a.require(func(w http.ResponseWriter, r *http.Request) {
		if !currentUser(r).IsAdmin {
			fail(w, fault.New("forbidden", "需要管理员权限。"))
			return
		}
		next(w, r)
	})
}
func (a *API) setupStatus(w http.ResponseWriter, r *http.Request) {
	available, err := a.accounts.SetupAvailable(r.Context())
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]bool{"available": available && len(a.config.SetupToken) >= 32})
}
func (a *API) setup(w http.ResponseWriter, r *http.Request) {
	var input struct {
		accounts.Registration
		Token string `json:"token"`
	}
	if !decode(w, r, &input) {
		return
	}
	expected, provided := sha256.Sum256([]byte(a.config.SetupToken)), sha256.Sum256([]byte(input.Token))
	if len(a.config.SetupToken) < 32 || subtle.ConstantTimeCompare(expected[:], provided[:]) != 1 {
		fail(w, fault.New("forbidden", "初始化口令无效。"))
		return
	}
	session, err := a.accounts.Setup(r.Context(), input.Registration)
	if err != nil {
		fail(w, err)
		return
	}
	a.setSession(w, session)
	write(w, 201, session.User)
}
func queryPage(r *http.Request) int {
	if !r.URL.Query().Has("page") {
		return 1
	}
	n, _ := strconv.Atoi(r.URL.Query().Get("page"))
	return n
}
func (a *API) adminUsers(w http.ResponseWriter, r *http.Request) {
	out, err := a.accounts.AdminUsers(r.Context(), r.URL.Query().Get("search"), r.URL.Query().Get("state"), queryPage(r))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) adminUserAction(w http.ResponseWriter, r *http.Request) {
	var input accounts.AdminAction
	if !decode(w, r, &input) {
		return
	}
	out, err := a.accounts.AdminUserAction(r.Context(), currentUser(r).ID, r.PathValue("id"), input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) site(w http.ResponseWriter, r *http.Request) {
	out, err := a.accounts.Site(r.Context())
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) updateSite(w http.ResponseWriter, r *http.Request) {
	var input accounts.SiteSettings
	if !decode(w, r, &input) {
		return
	}
	out, err := a.accounts.UpdateSite(r.Context(), currentUser(r).ID, input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) audit(w http.ResponseWriter, r *http.Request) {
	out, err := a.accounts.Audit(r.Context(), queryPage(r))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
