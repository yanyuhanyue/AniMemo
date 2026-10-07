package api

import (
	"io"
	"net/http"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/media"
)

func (a *API) settings(w http.ResponseWriter, r *http.Request) {
	var input accounts.Settings
	if !decode(w, r, &input) {
		return
	}
	u, err := a.accounts.Settings(r.Context(), currentUser(r).ID, input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, u)
}

func (a *API) password(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Current string `json:"current_password"`
		New     string `json:"new_password"`
	}
	if !decode(w, r, &input) {
		return
	}
	session, err := a.accounts.ChangePassword(r.Context(), currentUser(r).ID, input.Current, input.New)
	if err != nil {
		fail(w, err)
		return
	}
	a.setSession(w, session)
	write(w, 200, session.User)
}

func (a *API) logoutAll(w http.ResponseWriter, r *http.Request) {
	if err := a.accounts.LogoutAll(r.Context(), currentUser(r).ID); err != nil {
		fail(w, err)
		return
	}
	a.logout(w, r)
}

func (a *API) deleteAccount(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Password string `json:"password"`
	}
	if !decode(w, r, &input) {
		return
	}
	if err := a.accounts.Delete(r.Context(), currentUser(r).ID, input.Password); err != nil {
		fail(w, err)
		return
	}
	a.logout(w, r)
}

func (a *API) avatar(w http.ResponseWriter, r *http.Request) {
	picture, err := a.accounts.Avatar(r.Context(), currentUser(r).ID, r.PathValue("revision"))
	if err != nil {
		fail(w, err)
		return
	}
	w.Header().Set("Content-Type", picture.ContentType)
	w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; sandbox")
	w.Write(picture.Data)
}

func (a *API) setAvatar(w http.ResponseWriter, r *http.Request) {
	version, ok := coverVersion(w, r)
	if !ok {
		return
	}
	select {
	case a.coverSlots <- struct{}{}:
		defer func() { <-a.coverSlots }()
	default:
		w.Header().Set("Retry-After", "2")
		fail(w, fault.New("media_busy", "正在处理其他图片，请稍后重试。"))
		return
	}
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, media.MaxBytes))
	if err != nil {
		fail(w, fault.New("cover_too_large", "图片上传失败或超过 2 MB。"))
		return
	}
	u, err := a.accounts.SetAvatar(r.Context(), currentUser(r).ID, version, r.Header.Get("Content-Type"), data)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, u)
}

func (a *API) deleteAvatar(w http.ResponseWriter, r *http.Request) {
	version, ok := coverVersion(w, r)
	if !ok {
		return
	}
	u, err := a.accounts.SetAvatar(r.Context(), currentUser(r).ID, version, "", nil)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, u)
}
