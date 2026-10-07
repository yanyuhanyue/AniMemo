package api

import (
	"animemo.local/server/internal/external"
	"net/http"
)

func (a *API) bangumiConnection(w http.ResponseWriter, r *http.Request) {
	out, err := a.external.Connection(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) beginBangumi(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(cookieName)
	if err != nil {
		fail(w, err)
		return
	}
	address, err := a.external.BeginOAuth(r.Context(), currentUser(r).ID, cookie.Value)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]string{"url": address})
}
func (a *API) disconnectBangumi(w http.ResponseWriter, r *http.Request) {
	if err := a.external.Disconnect(r.Context(), currentUser(r).ID); err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]string{"message": "连接已断开，后续同步已取消。"})
}
func (a *API) verifyBangumi(w http.ResponseWriter, r *http.Request) {
	out, err := a.external.VerifyConnection(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) bangumiCallback(w http.ResponseWriter, r *http.Request) {
	// Return only a fixed status to the page, never OAuth code, state or provider errors.
	destination := "/connections?connection=failed"
	if cookie, err := r.Cookie(cookieName); err == nil {
		if user, err := a.accounts.Authenticate(r.Context(), cookie.Value); err == nil && (!user.EmailVerificationRequired || user.EmailVerified) {
			if err := a.external.CompleteOAuth(r.Context(), user.ID, cookie.Value, r.URL.Query().Get("state"), r.URL.Query().Get("code")); err == nil {
				destination = "/connections?connection=connected"
			}
		}
	}
	http.Redirect(w, r, destination, http.StatusSeeOther)
}

func (a *API) syncJobs(w http.ResponseWriter, r *http.Request) {
	jobs, err := a.external.SyncJobs(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": jobs})
}
func (a *API) syncJob(w http.ResponseWriter, r *http.Request) {
	job, err := a.external.SyncJob(r.Context(), currentUser(r).ID, r.PathValue("id"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, job)
}
func (a *API) startSync(w http.ResponseWriter, r *http.Request) {
	var input external.SyncRequest
	if !decode(w, r, &input) {
		return
	}
	job, err := a.external.StartSync(r.Context(), currentUser(r).ID, input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 202, job)
}
func (a *API) changeSync(w http.ResponseWriter, r *http.Request) {
	var input external.SyncAction
	if !decode(w, r, &input) {
		return
	}
	job, err := a.external.ChangeSync(r.Context(), currentUser(r).ID, r.PathValue("id"), input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, job)
}
